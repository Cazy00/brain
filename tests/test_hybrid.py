#!/usr/bin/env python3
"""Search by meaning beside FTS5 (BRAIN_VECTORS, vectors.db, `brain index --vectors`).

A fake embedder (tests/fake_embedder.py: a hashed bag of words on a loopback port) and the
invented corpus in memverbs_support.py stand in for a model and a brain, so nothing here
needs a network or can touch real notes. What is proved:

  * indexing is incremental by content hash, embeds exactly the FTS-indexed set, and
    drops the vectors of notes that left it (archived, purged);
  * a note whose text changed after its vector was made is served by keyword only;
  * fusion order and the evidence markers; the scope and trust filters of keyword search
    hold for the vector arm (inbox/journal only in scope all, archive never);
  * the embedder failing, stalling or refusing degrades to the keyword answer;
  * off / shadow / on; brain_search stays keyword-only;
  * keyword answers are byte-identical to 0.5.0 (tests/fixtures/golden/recall-0.5.0.json).

Run:  python3 -m unittest tests.test_hybrid -v
"""
import json
import math
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memverbs_support as support  # noqa: E402
from brainlib import mcp, mcpcore, vectors  # noqa: E402
from fake_embedder import FakeEmbedder  # noqa: E402
from hybrid_snapshot import snapshot  # noqa: E402

BRAIN = str(support.ENGINE / "bin" / "brain")
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "recall-0.5.0.json"
KEYS = ("BRAIN_VECTORS", "BRAIN_EMBEDDER_URL", "BRAIN_EMBEDDER_MODEL",
        "BRAIN_VECTOR_FLOOR", "BRAIN_VECTOR_HIGH", "BRAIN_EMBEDDER_QUERY_PREFIX",
        "BRAIN_EMBEDDER_DOC_PREFIX")


def recall(**args):
    result = mcp.call_tool("recall", args)
    return json.loads(result["content"][0]["text"])


def paths(reply):
    return [r["path"] for r in reply["results"]]


class UnitTests(unittest.TestCase):
    def test_settings_defaults_and_a_typo_means_off(self):
        self.assertEqual(vectors.settings({})["mode"], "off")
        self.assertEqual(vectors.settings({"BRAIN_VECTORS": "ON"})["mode"], "on")
        self.assertEqual(vectors.settings({"BRAIN_VECTORS": "onn"})["mode"], "off")
        cfg = vectors.settings({"BRAIN_VECTOR_FLOOR": "0.5", "BRAIN_VECTOR_HIGH": "nan",
                                "BRAIN_EMBEDDER_URL": " http://x "})
        self.assertEqual((cfg["floor"], cfg["high"], cfg["url"]),
                         (0.5, vectors.DEFAULT_HIGH, "http://x"))

    def test_the_embedder_url_may_end_in_a_slash_or_v1_and_a_wrong_path_is_a_404(self):
        with FakeEmbedder() as fake:
            for url in (fake.url, fake.url + "/", fake.url + "/v1", fake.url + "/v1/"):
                cfg = dict(vectors.settings({"BRAIN_EMBEDDER_URL": url}))
                self.assertEqual(len(vectors.embed(cfg, ["alpha", "beta"], "search_document: ", 5)), 2, url)
            self.assertEqual(len(fake.calls), 8)
            with self.assertRaises(vectors.EmbedError):             # not the embeddings path
                vectors.embed({"url": fake.url + "/v2", "model": "m"}, ["alpha"], "search_document: ", 5)

    def test_chunks_start_with_title_aliases_topics_then_paragraph_bodies(self):
        rec = {"title": "T", "aliases": "a1, a2", "topics": "t1",
               "body": "\n\n".join(("para %d " % i) + "x" * 600 for i in range(5))}
        chunks = vectors.chunk_texts(rec)
        self.assertEqual(chunks[0], "T\nAliases: a1, a2\nTopics: t1")
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(len(c) <= vectors.CHUNK_CHARS for c in chunks[1:]))
        self.assertTrue(all(c.startswith("T\n") for c in chunks[1:]))   # every body chunk names its note
        self.assertEqual("\n\n".join(c[2:] for c in chunks[1:]), rec["body"])   # cut at breaks only

    def test_a_long_paragraph_is_cut_at_a_space_and_nothing_is_lost(self):
        rec = {"title": "T", "body": " ".join("word%d" % i for i in range(1000))}
        chunks = vectors.chunk_texts(rec)
        self.assertTrue(all(len(c) <= vectors.CHUNK_CHARS for c in chunks))
        self.assertEqual(" ".join(c[2:] for c in chunks[1:]).split(), rec["body"].split())

    def test_the_prefixes_follow_the_model_and_the_environment_overrides_them(self):
        qwen = vectors.settings({"BRAIN_EMBEDDER_MODEL": "Qwen3-Embedding-0.6B"})
        self.assertTrue(qwen["query_prefix"].startswith("Instruct: ") and qwen["query_prefix"].endswith("\nQuery:"))
        self.assertEqual(qwen["doc_prefix"], "")
        nomic = vectors.settings({"BRAIN_EMBEDDER_MODEL": "nomic-embed-text-v1.5"})
        self.assertEqual((nomic["query_prefix"], nomic["doc_prefix"]), ("search_query: ", "search_document: "))
        other = vectors.settings({"BRAIN_EMBEDDER_MODEL": "x", "BRAIN_EMBEDDER_DOC_PREFIX": "doc:\\n"})
        self.assertEqual((other["query_prefix"], other["doc_prefix"]), ("", "doc:\n"))
        self.assertEqual(vectors.settings({})["model"], vectors.DEFAULT_MODEL)

    def test_a_note_scores_by_its_best_chunk_plus_a_tenth_of_its_second_and_the_arm_keeps_ten_notes(self):
        def unit(x, y):
            norm = math.hypot(x, y)
            return array("f", (x / norm, y / norm, 0.0))
        query = array("f", (1.0, 0.0, 0.0)).tobytes()
        rows = [("one", 0, "h", unit(1.0, 1.0)), ("one", 1, "h", unit(1.0, 1.1)),
                ("two", 0, "h", unit(1.0, 0.99)), ("old", 0, "h", unit(1.0, 0.0))]
        allowed = {"one": "reference", "two": "reference", "old": "reference"}
        out = vectors.vector_list(query, rows, allowed, {}, 0.0, lambda n, h: n != "old")
        self.assertEqual([h["id"] for h in out], ["one", "two"])    # the second chunk breaks the near tie
        self.assertEqual(out[0]["chunk_no"], 0)                       # the entry names the best chunk
        many = [("n%d" % i, 0, "h", unit(1.0, 1.0 + i)) for i in range(25)]
        out = vectors.vector_list(query, many, {n: "reference" for n, *_ in many}, {}, 0.0, lambda n, h: True)
        self.assertEqual(len(out), vectors.VECTOR_NOTES)

    def test_the_hash_follows_the_text_the_vector_is_made_from(self):
        base = {"title": "T", "aliases": "a", "topics": "t", "body": "b"}
        self.assertEqual(vectors.record_hash(base), vectors.record_hash(dict(base, id="other")))
        for key in base:
            self.assertNotEqual(vectors.record_hash(base), vectors.record_hash(dict(base, **{key: "z"})))

    def test_rrf_sums_ranks_breaks_ties_by_keyword_order_and_marks_evidence(self):
        vec = [{"id": "v1", "cosine": 0.9, "chunk_no": 0}, {"id": "k1", "cosine": 0.5, "chunk_no": 0},
               {"id": "v3", "cosine": 0.4, "chunk_no": 0}]
        fused = vectors.fuse(["k1", "k2", "k3"], vec, high=0.6)
        self.assertEqual([e["id"] for e in fused], ["k1", "v1", "k2", "k3", "v3"])
        self.assertAlmostEqual(fused[0]["score"], 1 / 61 + 1 / 62)
        # v1 (vector rank 1) and k2 (keyword rank 2) tie on score only if ranks match; here
        # k3 (kw 3) and v3 (vec 3) tie exactly, and the keyword side wins.
        self.assertEqual([e["evidence"] for e in fused],
                         ["keyword_exact", "high_vector_match", "keyword_exact",
                          "keyword_exact", "weak_semantic"])

    def test_the_vector_list_weights_folders_filters_and_caps_chunks(self):
        def vec(*xs):
            from array import array
            a = array("f", xs)
            n = sum(x * x for x in a) ** 0.5
            return array("f", (x / n for x in a))
        q = vec(1, 0).tobytes()
        rows = [("dec", 0, "h", vec(1, 0.1)), ("jour", 0, "h", vec(1, 0)),
                ("gone", 0, "h", vec(1, 0)), ("far", 0, "h", vec(0.1, 1)),
                ("stale", 0, "old", vec(1, 0)), ("dec", 1, "h", vec(1, 0.9))]
        allowed = {"dec": "decisions", "jour": "journal", "far": "decisions", "stale": "decisions"}
        out = vectors.vector_list(q, rows, allowed, {"decisions": 1.2, "journal": 0.6}, 0.3,
                                  lambda nid, h: h == "h")
        # "gone" is not allowed, "stale" has a changed hash, "far" is under the floor; the
        # decision beats the journal note despite the lower raw cosine (1.2 vs 0.6 weight),
        # and "dec" appears once, at its best chunk.
        self.assertEqual([(o["id"], o["chunk_no"]) for o in out], [("dec", 0), ("jour", 0)])
        capped = vectors.vector_list(q, rows, allowed, {}, 0.3, lambda nid, h: True, limit=1)
        self.assertEqual(len(capped), 1)


class HybridCase(unittest.TestCase):
    """A corpus, a fake embedder, and the environment pointing at both."""

    def setUp(self):
        self.corpus = support.Corpus()
        self.addCleanup(self.corpus.close)
        self.fake = FakeEmbedder().__enter__()
        self.addCleanup(self.fake.__exit__)
        saved = {k: os.environ.get(k) for k in KEYS}
        self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                 for k, v in saved.items()])
        for key in KEYS:
            os.environ.pop(key, None)
        # The fake is a bag of words, so its cosines are small; the real floor is
        # calibrated elsewhere. These keep the tests about behaviour, not calibration.
        # The fake strips nomic's prefixes, so the tests ask for them whatever the model is called.
        self.env(BRAIN_EMBEDDER_URL=self.fake.url, BRAIN_VECTOR_FLOOR="0.05",
                 BRAIN_VECTOR_HIGH="0.30", BRAIN_EMBEDDER_QUERY_PREFIX="search_query: ",
                 BRAIN_EMBEDDER_DOC_PREFIX="search_document: ")

    def env(self, **kw):
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def index(self, *flags):
        done = subprocess.run([sys.executable, BRAIN, "index", "--vectors", *flags],
                              capture_output=True, text=True, cwd=support.ENGINE)
        return done

    def vector_ids(self):
        db = sqlite3.connect(str(self.corpus.state / "vectors.db"))
        try:
            return {r[0] for r in db.execute("SELECT DISTINCT note_id FROM vectors")}
        finally:
            db.close()

    def fts_ids(self):
        subprocess.run([sys.executable, BRAIN, "index", "--quiet"], check=True,
                       capture_output=True, cwd=support.ENGINE)
        db = sqlite3.connect(str(self.corpus.state / "index.db"))
        try:
            return {r[0] for r in db.execute("SELECT id FROM fts")}
        finally:
            db.close()

    def note(self, rel):
        return self.corpus.root / "knowledge" / rel


class IndexingTests(HybridCase):
    def test_it_embeds_exactly_the_fts_indexed_set_and_never_the_unindexed_folders(self):
        done = self.index()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.vector_ids(), self.fts_ids())
        everything = " ".join(self.fake.embedded_texts("search_document: "))
        for never in ("We used the cog engine", "needs a sprocket supplier", "Old engine"):
            self.assertNotIn(never, everything)         # archive/ and handoff/ text
        self.assertIn("Choose the widget engine", everything)

    def test_a_slow_embed_does_not_hold_the_tree_lock_but_reading_the_notes_waits_for_an_install(self):
        """`brain sync` installs a revision under the exclusive tree lock; an embed that takes
        minutes must not hold it off, while the read of the notes still waits for a running install."""
        from brainlib import locks
        self.fake.delay = 4.0
        proc = subprocess.Popen([sys.executable, BRAIN, "index", "--vectors"], cwd=support.ENGINE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.kill)
        until = time.monotonic() + 30
        while not self.fake.arrivals and time.monotonic() < until:
            time.sleep(0.05)
        self.assertTrue(self.fake.arrivals, "the build never reached the embedder")
        began = time.monotonic()
        with locks.tree_lock(self.corpus.state, exclusive=True):      # blocks while a reader holds it shared
            pass
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertIsNone(proc.poll(), "the build ended before the lock was tried")
        proc.communicate(timeout=300)
        self.assertEqual(proc.returncode, 0)
        with locks.tree_lock(self.corpus.state, exclusive=True):      # an install running: the read waits
            waiting = subprocess.Popen([sys.executable, BRAIN, "index", "--vectors"],
                                       cwd=support.ENGINE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True)
            self.addCleanup(waiting.kill)
            with self.assertRaises(subprocess.TimeoutExpired):
                waiting.communicate(timeout=1.5)
        out, _ = waiting.communicate(timeout=120)
        self.assertEqual(waiting.returncode, 0, out)

    def test_documents_and_queries_carry_the_task_prefixes(self):
        self.index()
        self.assertTrue(self.fake.embedded_texts("search_document: "))
        self.assertFalse(self.fake.embedded_texts("search_query: "))
        self.env(BRAIN_VECTORS="on")
        recall(query="gizmo")
        self.assertEqual(self.fake.embedded_texts("search_query: "), ["gizmo"])

    def test_it_is_incremental_by_content_hash(self):
        self.index()
        first = len(self.fake.calls)
        self.assertGreater(first, 0)
        done = self.index()
        self.assertEqual(len(self.fake.calls), first, "an unchanged corpus was re-embedded")
        self.assertIn("0 notes embedded", done.stdout)
        glossary = self.note("reference/widget-glossary.md")
        glossary.write_text(glossary.read_text().replace("toothed wheel", "toothed disc", 1))
        self.index()
        again = self.fake.calls[first:]
        self.assertTrue(again and all("disc" in t or "Widget glossary" in t for _p, t in again))
        self.assertEqual({t for _p, t in again if "glossary" in t.lower() and "Aliases" in t},
                         {"Widget glossary\nAliases: widget terms\nTopics: widgets"})

    def test_a_new_model_key_re_embeds_everything(self):
        self.index()
        first = len(self.fake.calls)
        self.env(BRAIN_EMBEDDER_MODEL="other-model")
        self.index()
        self.assertGreater(len(self.fake.calls), first)

    def test_archiving_or_purging_a_note_drops_its_vectors(self):
        self.index()
        self.assertIn("2026-03-01-choose-widget-engine", self.vector_ids())
        archived = self.note("archive/decisions/2026-03-01-choose-widget-engine.md")
        archived.parent.mkdir(parents=True, exist_ok=True)
        self.note("decisions/2026-03-01-choose-widget-engine.md").rename(archived)
        self.note("inbox/2026-03-06-090000-widget-idea.md").unlink()           # purged
        calls = len(self.fake.calls)
        self.index()
        ids = self.vector_ids()
        self.assertNotIn("2026-03-01-choose-widget-engine", ids)
        self.assertNotIn("inbox-2026-03-06-090000-widget-idea", ids)
        self.assertEqual(ids, self.fts_ids())
        self.assertEqual(len(self.fake.calls), calls, "removing notes should embed nothing")

    def test_removed_notes_lose_their_vectors_even_when_the_embedder_is_down(self):
        self.index()
        self.note("inbox/2026-03-06-090000-widget-idea.md").unlink()
        self.fake.fail = 503
        done = self.index()
        self.assertEqual(done.returncode, 0)             # nothing was left to embed
        self.assertNotIn("inbox-2026-03-06-090000-widget-idea", self.vector_ids())

    def test_full_rebuilds_and_a_failed_full_keeps_the_old_file(self):
        self.index()
        first = len(self.fake.calls)
        self.index("--full")
        self.assertEqual(len(self.fake.calls), 2 * first)
        before = self.vector_ids()
        self.fake.fail = 500
        done = self.index("--full")
        self.assertEqual(done.returncode, 1)
        self.assertIn("embedder", done.stdout)
        self.assertEqual(self.vector_ids(), before)
        self.assertEqual(sorted(p.name for p in self.corpus.state.glob("vectors.db*")),
                         ["vectors.db"])

    def test_the_scheduled_form_warns_and_exits_0_when_vectors_are_off_or_the_embedder_is_down(self):
        """The sync and maintenance units run `index --vectors --quiet`: it must never fail them."""
        self.env(BRAIN_VECTORS="off")
        off = self.index("--quiet")
        self.assertEqual((off.returncode, off.stdout), (0, ""), off.stdout + off.stderr)
        self.assertIn("BRAIN_VECTORS is off", off.stderr)
        self.assertFalse((self.corpus.state / "vectors.db").exists(), "off must embed nothing")
        self.env(BRAIN_VECTORS="shadow")
        self.fake.fail = 503
        down = self.index("--quiet")
        self.assertEqual((down.returncode, down.stdout), (0, ""), down.stdout + down.stderr)
        self.assertIn("warning", down.stderr)
        self.env(BRAIN_EMBEDDER_URL="http://127.0.0.1:9")                  # nothing listens
        gone = self.index("--quiet")
        self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
        self.assertIn("warning", gone.stderr)
        self.env(BRAIN_EMBEDDER_URL="")                                     # unset
        unset = self.index("--quiet")
        self.assertEqual(unset.returncode, 0, unset.stdout + unset.stderr)
        self.fake.fail = None
        self.env(BRAIN_EMBEDDER_URL=self.fake.url)                          # recovers: next run embeds
        back = self.index("--quiet")
        self.assertEqual(back.returncode, 0, back.stdout + back.stderr)
        self.assertEqual(self.vector_ids(), self.fts_ids())
        # a person asking by hand still gets the exit code
        self.fake.fail = 503
        self.env(BRAIN_EMBEDDER_MODEL="other-model")
        self.assertEqual(self.index().returncode, 1)

    def test_full_without_vectors_is_refused(self):
        done = subprocess.run([sys.executable, BRAIN, "index", "--full"], capture_output=True,
                              text=True, cwd=support.ENGINE)
        self.assertEqual(done.returncode, 2)


class SearchTests(HybridCase):
    def setUp(self):
        super().setUp()
        self.index()
        self.env(BRAIN_VECTORS="on")

    GLOSSARY = "knowledge/reference/widget-glossary.md"

    def test_the_meaning_arm_finds_what_keyword_search_cannot(self):
        self.assertEqual(paths(recall(query="gizmo", mode="keyword")), [])
        found = recall(query="gizmo", mode="hybrid")
        self.assertNotIn("search_degraded", found)
        self.assertEqual(found["match_mode"], "hybrid")
        self.assertIn(self.GLOSSARY, paths(found))
        top = next(r for r in found["results"] if r["path"] == self.GLOSSARY)
        self.assertIn(top["evidence"], ("high_vector_match", "weak_semantic"))
        self.assertEqual(top["trust"], "stale")                # review_by 2026-04-01 passed
        self.assertEqual(top["markers"][0], "[STALE — review_by 2026-04-01 passed]")
        self.assertTrue(top["chunk"])

    def test_vector_only_hits_are_high_above_the_threshold_and_weak_below(self):
        self.env(BRAIN_VECTOR_HIGH="0.99")
        weak = {r["evidence"] for r in recall(query="gizmo", mode="hybrid")["results"]}
        self.assertEqual(weak, {"weak_semantic"})
        self.env(BRAIN_VECTOR_HIGH="0.01")
        high = {r["evidence"] for r in recall(query="gizmo", mode="hybrid")["results"]}
        self.assertEqual(high, {"high_vector_match"})

    def test_the_floor_removes_weak_matches(self):
        self.env(BRAIN_VECTOR_FLOOR="0.999")
        self.assertEqual(paths(recall(query="gizmo", mode="hybrid")), [])

    def test_a_hit_in_both_lists_is_keyword_exact_and_leads_the_fusion(self):
        keyword = recall(query="sprocket engine", mode="keyword")
        hybrid = recall(query="sprocket engine", mode="hybrid")
        self.assertEqual(hybrid["results"][0]["path"], keyword["results"][0]["path"])
        by_path = {r["path"]: r["evidence"] for r in hybrid["results"]}
        for hit in keyword["results"]:
            self.assertEqual(by_path[hit["path"]], hit["evidence"])
        # nothing keyword search found is lost
        self.assertTrue(set(paths(keyword)) <= set(paths(hybrid)))

    def test_fusion_ranks_a_note_found_by_both_arms_above_one_found_by_one(self):
        both = recall(query="sprocket gizmo", mode="hybrid")["results"]
        self.assertEqual(both[0]["evidence"], "keyword_exact")
        evidence = [r["evidence"] for r in both]
        first_vector_only = next((i for i, e in enumerate(evidence) if e != "keyword_exact"),
                                 len(evidence))
        self.assertTrue(all(e == "keyword_exact" for e in evidence[:first_vector_only]))

    def test_inbox_and_journal_only_in_scope_all_with_their_markers(self):
        canonical = paths(recall(query="gizmo", mode="hybrid"))
        self.assertFalse([p for p in canonical if "/inbox/" in p or "/journal/" in p])
        wide = recall(query="gizmo", mode="hybrid", scope="all", limit=20)
        by_path = {r["path"]: r for r in wide["results"]}
        inbox = by_path["knowledge/inbox/2026-03-06-090000-widget-idea.md"]
        self.assertEqual(inbox["trust"], "provisional")
        self.assertEqual(inbox["markers"], ["[provisional — unconsolidated]"])
        self.assertEqual(by_path["knowledge/journal/2026/2026-03-05.md"]["trust"], "journal")

    def test_a_pending_correction_marker_reaches_a_vector_only_hit(self):
        wide = recall(query="gizmo", mode="hybrid", scope="all", limit=20)
        glossary = next(r for r in wide["results"] if r["path"] == self.GLOSSARY)
        self.assertTrue(any(m.startswith("[correction pending") for m in glossary["markers"]),
                        glossary["markers"])

    def test_archive_and_handoff_are_never_served(self):
        for query in ("gizmo", "cog engine", "supplier", "old"):
            for scope in ("canonical", "all"):
                got = paths(recall(query=query, mode="hybrid", scope=scope, limit=20))
                self.assertFalse([p for p in got if "/archive/" in p or "/handoff/" in p], got)

    def test_a_vector_left_behind_by_an_archived_note_is_not_served(self):
        archived = self.note("archive/reference/widget-glossary.md")
        archived.parent.mkdir(parents=True, exist_ok=True)
        self.note("reference/widget-glossary.md").rename(archived)
        self.assertIn("widget-glossary", self.vector_ids())          # vectors not yet cleaned
        self.assertNotIn(self.GLOSSARY, paths(recall(query="gizmo", mode="hybrid", scope="all")))

    def test_a_note_edited_after_its_vector_was_made_is_served_by_keyword_only(self):
        self.assertIn(self.GLOSSARY, paths(recall(query="gizmo", mode="hybrid")))
        glossary = self.note("reference/widget-glossary.md")
        glossary.write_text(glossary.read_text().replace("A sprocket is a toothed wheel.",
                                                         "A sprocket is a wheel.", 1))
        got = recall(query="gizmo", mode="hybrid")
        self.assertNotIn(self.GLOSSARY, paths(got))                # old meaning not served
        self.assertNotIn("search_degraded", got)
        # the keyword arm still finds the edited note by its words
        self.assertIn(self.GLOSSARY, paths(recall(query="sprocket", mode="hybrid")))
        self.index()                                               # a fresh vector brings it back
        self.assertIn(self.GLOSSARY, paths(recall(query="gizmo", mode="hybrid")))

    def test_hidden_private_captures_stay_out_of_the_vector_arm(self):
        private = self.note("inbox/2026-03-06-090000-widget-idea.md")
        private.write_text(private.read_text().replace("status: draft", "status: draft\nvisibility: private"))
        self.index()
        local = paths(recall(query="gizmo", mode="hybrid", scope="all", limit=20))
        self.assertIn("knowledge/inbox/2026-03-06-090000-widget-idea.md", local)
        remote = json.loads(mcpcore.call(
            "recall", dict(query="gizmo", mode="hybrid", scope="all", limit=20),
            transport="remote")["content"][0]["text"])
        self.assertNotIn("knowledge/inbox/2026-03-06-090000-widget-idea.md", paths(remote))
        self.assertNotIn("search_degraded", remote)


class DegradedTests(HybridCase):
    def setUp(self):
        super().setUp()
        self.index()
        self.env(BRAIN_VECTORS="on")

    def assert_keyword_only(self, **args):
        keyword = recall(mode="keyword", **args)
        got = recall(mode="hybrid", **args)
        self.assertEqual(got.pop("search_degraded"), "embed_unavailable")
        self.assertEqual(got, keyword)

    def test_a_refusing_embedder_degrades_and_never_errors(self):
        self.fake.fail = 503
        self.assert_keyword_only(query="sprocket engine")
        self.assert_keyword_only(query="gizmo")

    def test_an_embedder_that_stalls_past_two_seconds_degrades(self):
        self.fake.delay = vectors.QUERY_TIMEOUT + 1.0
        self.assert_keyword_only(query="sprocket engine")

    def test_an_embedder_that_drips_its_answer_is_cut_off_at_the_timeout(self):
        """Headers late, then one byte per 1.9 s: each socket read is within its own timeout,
        so only a deadline on the whole call ends it."""
        self.fake.delay = vectors.QUERY_TIMEOUT - 0.1
        self.fake.drip = vectors.QUERY_TIMEOUT - 0.1
        cfg = vectors.settings()
        began = time.monotonic()
        with self.assertRaises(vectors.EmbedError):
            vectors.embed(cfg, ["sprocket"], "search_query: ", 1.0)
        self.assertLess(time.monotonic() - began, 1.5)
        began = time.monotonic()
        self.assert_keyword_only(query="sprocket engine")
        self.assertLess(time.monotonic() - began, 2 * (vectors.QUERY_TIMEOUT + 2.5))   # two calls, each with a cold start

    def test_an_unreachable_embedder_degrades(self):
        self.env(BRAIN_EMBEDDER_URL="http://127.0.0.1:9")
        self.assert_keyword_only(query="sprocket engine")

    def test_a_url_that_is_not_http_degrades(self):
        self.env(BRAIN_EMBEDDER_URL="file:///etc/passwd")
        self.assert_keyword_only(query="sprocket engine")
        self.env(BRAIN_EMBEDDER_URL=None)
        self.assert_keyword_only(query="sprocket engine")

    def test_a_missing_vectors_db_degrades(self):
        (self.corpus.state / "vectors.db").unlink()
        self.assert_keyword_only(query="sprocket engine")

    def test_vectors_made_with_another_model_are_not_used(self):
        self.env(BRAIN_EMBEDDER_MODEL="a-newer-model")
        self.assert_keyword_only(query="gizmo")

    def test_the_default_mode_on_also_reports_the_degradation(self):
        self.fake.fail = 503
        self.assertEqual(recall(query="sprocket engine")["search_degraded"], "embed_unavailable")


class RolloutTests(HybridCase):
    def setUp(self):
        super().setUp()
        self.index()

    def test_off_answers_keyword_and_flags_an_explicit_hybrid_request(self):
        self.env(BRAIN_VECTORS="off")
        self.assertEqual(paths(recall(query="gizmo")), [])
        got = recall(query="gizmo", mode="hybrid")
        self.assertEqual((paths(got), got["search_degraded"]), ([], "embed_unavailable"))
        self.assertEqual(self.fake.embedded_texts("search_query: "), [])

    def test_shadow_is_hybrid_only_for_calls_that_ask(self):
        self.env(BRAIN_VECTORS="shadow")
        default = recall(query="gizmo")
        self.assertEqual((paths(default), "search_degraded" in default), ([], False))
        self.assertTrue(paths(recall(query="gizmo", mode="hybrid")))
        self.assertEqual(paths(recall(query="gizmo", mode="keyword")), [])

    def test_on_makes_hybrid_the_default_and_keyword_still_wins_when_asked(self):
        self.env(BRAIN_VECTORS="on")
        self.assertTrue(paths(recall(query="gizmo")))
        self.assertEqual(paths(recall(query="gizmo")), paths(recall(query="gizmo", mode="hybrid")))
        self.assertEqual(paths(recall(query="gizmo", mode="keyword")), [])

    def test_brain_search_stays_keyword_only_in_every_mode(self):
        for mode in ("off", "shadow", "on"):
            self.env(BRAIN_VECTORS=mode)
            text = mcp.call_tool("brain_search", {"query": "gizmo"})["content"][0]["text"]
            self.assertTrue(text.startswith("no hits"), (mode, text))
        self.assertEqual(self.fake.embedded_texts("search_query: "), [])


class KeywordIdentityTests(HybridCase):
    """Keyword answers are the 0.5.0 answers, byte for byte, whatever BRAIN_VECTORS says
    and whether or not vectors exist."""

    def test_keyword_answers_equal_the_recorded_0_5_0_ones(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        for built in (False, True):
            if built:
                self.index()
            for mode in ("off", "shadow", "on"):
                self.env(BRAIN_VECTORS=mode)
                got = snapshot()
                self.assertEqual(set(got), set(golden))
                for key, answers in golden.items():
                    for tool, text in answers.items():
                        if tool == "recall_default" and mode == "on":
                            continue         # on: hybrid IS the default (or says it degraded)
                        with self.subTest(built=built, mode=mode, query=key, tool=tool):
                            self.assertEqual(got[key][tool], text)


if __name__ == "__main__":
    unittest.main()
