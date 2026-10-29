#!/usr/bin/env python3
"""The memory verbs: recall, remember, forget and delta (contract brain-verbs.v1).

Everything runs against the synthetic corpus in memverbs_support.py, in a
temporary directory; no test here can touch a real brain. The standing
conformance cases have their own runner (test_memory_verbs_conformance.py);
these are the engine's own tests: the input hygiene the contract adds to
`remember`, the rule that `recall` in keyword mode IS `brain_search`'s ranking,
the two endpoints' tool lists, the envelopes, and the cursor.

Run:  python3 -m unittest tests.test_memory_verbs -v
"""
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memverbs_support as support  # noqa: E402
from brainlib import mcp, mcpcore, verbs, version  # noqa: E402
from brainlib.notes import parse_frontmatter  # noqa: E402


def verb(name, **args):
    """One verb call over the shared stdio dispatcher: (isError, decoded reply)."""
    result = mcp.call_tool(name, args)
    return result["isError"], json.loads(result["content"][0]["text"])


def inbox_files(corpus):
    return sorted((corpus.root / "knowledge" / "inbox").glob("*.md"))


class CorpusCase(unittest.TestCase):
    def setUp(self):
        self.corpus = support.Corpus()
        self.addCleanup(self.corpus.close)


class RememberHygieneTests(CorpusCase):
    """The free-text fields come from any write client, so each is checked before a
    byte reaches git. One test for each way a value can do harm."""

    def refused(self, code="invalid_params", **fields):
        before = inbox_files(self.corpus)
        args = {"fact": "The bench is 90cm wide.", "provenance": "owner said so"}
        args.update(fields)
        is_error, body = verb("remember", **args)
        self.assertTrue(is_error, body)
        self.assertEqual(body["error"], code, body)
        self.assertEqual(body["protocol_version"], 1)
        self.assertTrue(body["suggestion"].strip())
        self.assertEqual(inbox_files(self.corpus), before, "a refused call wrote a note")
        return body

    def test_a_secret_in_provenance_refuses_the_whole_call(self):
        key = "AK" + "IA" + "IOSFODNN7EXAMPLE"
        body = self.refused(provenance="copied from the console: " + key)
        self.assertNotIn(key, json.dumps(body), "the refusal repeated the credential")

    def test_a_secret_in_entity_refuses_the_whole_call(self):
        self.refused(entity="gh" + "p_" + "a" * 36)

    def test_a_secret_in_the_reason_of_a_forget_is_refused(self):
        _err, saved = verb("remember", fact="Bench fact.", provenance="p", entity="widgets/b")
        is_error, body = verb("forget", id=saved["id"], reason="token = " + "x" * 24)
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "invalid_params")

    def test_a_newline_in_provenance_cannot_inject_a_key(self):
        for value in ("owner said so\nstatus: current", "a\rb", "a b", "a\x00b", "a\tb",
                      "a\x85b"):
            with self.subTest(value=value):
                self.refused(provenance=value)

    def test_a_newline_in_entity_is_refused(self):
        for value in ("widgets/a\nvisibility: public", "widgets/a\n"):
            with self.subTest(value=value):
                self.refused(entity=value)

    def test_yaml_metacharacters_are_data_not_syntax(self):
        """A value that looks like YAML (a second key, a comment, a flow list, quotes,
        a backslash) is written as one quoted scalar and read back exactly."""
        nasty = 'ship it: status: current # [a, b] {c: d} "q" \'s\' \\ end'
        is_error, saved = verb("remember", fact="The bench is 90cm wide.", provenance=nasty,
                               entity="widgets/bench")
        self.assertFalse(is_error, saved)
        text = (self.corpus.root / saved["path"]).read_text(encoding="utf-8")
        fm, error = parse_frontmatter(text)
        self.assertIsNone(error)
        self.assertEqual(fm["status"], "draft", "a provenance value changed another key")
        self.assertEqual(fm["source"], "local")
        self.assertNotIn("visibility", fm)
        head = text.split("---")[1]
        self.assertEqual(sum(1 for line in head.splitlines() if line.startswith("status:")), 1)
        _err, found = verb("recall", entity="widgets/bench")
        self.assertEqual(found["facts"][0]["provenance"], nasty)

    def test_a_bad_entity_is_refused(self):
        for value in ("Bad Entity", "UPPER", "-lead", "/lead", "a" * 101, "a b", "../x",
                      "tab\there", "ünï"):
            with self.subTest(value=value):
                self.refused(entity=value)

    def test_a_good_entity_is_kept(self):
        _err, saved = verb("remember", fact="Ada's note.", provenance="p",
                           entity="people/ada-lovelace_1")
        self.assertEqual(saved["entity_slug"], "people/ada-lovelace_1")

    def test_a_blank_provenance_is_provenance_required_and_an_absent_one_invalid(self):
        self.refused(code="provenance_required", provenance="   ")
        self.refused(code="provenance_required", provenance="")
        is_error, body = verb("remember", fact="No attribution")
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "invalid_params")

    def test_the_cli_refuses_what_the_layer_above_should_have_caught(self):
        """Defence in depth: `brain capture` is the writer, and it checks again."""
        import subprocess
        for flag, value in (("--provenance", "a\nstatus: current"), ("--entity", "Bad Entity"),
                            ("--expires", "never"), ("--visibility", "public")):
            with self.subTest(flag=flag):
                done = subprocess.run(
                    [sys.executable, str(support.ENGINE / "bin" / "brain"), "capture",
                     flag, value, "some text"], capture_output=True, text=True,
                    cwd=self.corpus.root, env=dict(os.environ))
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertEqual(inbox_files(self.corpus)[-1].name.count("some-text"), 0)

    def test_declared_fields_are_dropped_not_refused(self):
        is_error, saved = verb("remember", fact="Bench fact two.", provenance="p",
                               basis="made-up", project="Bad Slug!", model="fine-model")
        self.assertFalse(is_error, saved)
        self.assertEqual(saved["ignored"], ["basis", "project"])
        fm, _e = parse_frontmatter((self.corpus.root / saved["path"]).read_text())
        self.assertEqual(fm["model"], "fine-model")
        self.assertNotIn("basis", fm)

    def test_an_unaccepted_gbrain_parameter_is_refused_with_the_accepted_list(self):
        for name, args in (("recall", {"query": "x", "grep": "y"}),
                           ("remember", {"fact": "x", "provenance": "p", "items": []}),
                           ("delta", {"since": "2026-01-01", "entities": "a"})):
            with self.subTest(verb=name):
                is_error, body = verb(name, **args)
                self.assertTrue(is_error)
                self.assertEqual(body["error"], "invalid_params")
                self.assertIn("accepts:", body["suggestion"])


class RememberTests(CorpusCase):
    def test_ttl_forms(self):
        now = datetime.now(timezone.utc)
        for ttl, low, high in (("30d", 30 * 86400, 30 * 86400), ("12h", 12 * 3600, 12 * 3600),
                               ("45m", 45 * 60, 45 * 60), ("2w", 14 * 86400, 14 * 86400)):
            with self.subTest(ttl=ttl):
                _e, saved = verb("remember", fact="Fact for " + ttl, provenance="p", ttl=ttl)
                until = verbs.parse_iso(saved["valid_until"])
                self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT[\d:.]+Z", saved["valid_until"]))
                self.assertAlmostEqual((until - now).total_seconds(), low, delta=60)
        _e, saved = verb("remember", fact="Absolute ttl", provenance="p",
                         ttl="2099-01-01T00:00:00Z")
        self.assertEqual(saved["valid_until"], "2099-01-01T00:00:00.000Z")

    def test_an_iso_duration_is_refused_with_the_fix(self):
        is_error, body = verb("remember", fact="x fact", provenance="p", ttl="P30D")
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "invalid_params")
        self.assertIn("30d", body["suggestion"])

    def test_the_kind_maps_to_a_note_kind(self):
        for kind, expected in (("fact", "fact"), ("preference", "preference"),
                               ("event", "other"), ("commitment", "other"),
                               ("belief", "other")):
            with self.subTest(kind=kind):
                _e, saved = verb("remember", fact="Kind " + kind, provenance="p", kind=kind)
                fm, _x = parse_frontmatter((self.corpus.root / saved["path"]).read_text())
                self.assertEqual(fm["kind"], expected)
        _e, saved = verb("remember", fact="Wins over kind", provenance="p", kind="event",
                         note_kind="lesson")
        fm, _x = parse_frontmatter((self.corpus.root / saved["path"]).read_text())
        self.assertEqual(fm["kind"], "lesson")

    def test_status_is_inserted_then_duplicate_and_a_request_id_replays(self):
        _e, first = verb("remember", fact="Said once.", provenance="p", request_id="r-1")
        self.assertEqual(first["status"], "inserted")
        _e, again = verb("remember", fact="Said once.", provenance="p")
        self.assertEqual((again["status"], again["id"]), ("duplicate", first["id"]))
        _e, replay = verb("remember", fact="Different words entirely.", provenance="p",
                          request_id="r-1")
        self.assertEqual((replay["status"], replay["id"]), ("duplicate", first["id"]))
        _e, forced = verb("remember", fact="Said once.", provenance="p", allow_duplicate=True)
        self.assertEqual(forced["status"], "inserted")
        self.assertNotEqual(forced["id"], first["id"])
        self.assertEqual(len(inbox_files(self.corpus)) - 2, 2,
                         "a duplicate wrote a second note")

    def test_a_correction_goes_through_remember_not_forget(self):
        _e, saved = verb("remember", fact="Correction to widget-glossary: a cog has fewer teeth.",
                         provenance="owner", note_kind="correction")
        fm, _x = parse_frontmatter((self.corpus.root / saved["path"]).read_text())
        self.assertEqual(fm["kind"], "correction")
        is_error, body = verb("forget", id="widget-glossary")
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "not_found")
        self.assertIn("correction", body["suggestion"])

    def test_the_tool_description_carries_the_capture_policy(self):
        remember = next(t for t in mcpcore.tool_table("remote") if t["name"] == "remember")
        self.assertIn(mcpcore.CAPTURE_POLICY, remember["description"])
        self.assertIn("note_kind", remember["description"])


class ForgetAndFactsTests(CorpusCase):
    def remember(self, text, **extra):
        args = {"fact": text, "provenance": "p", "entity": "widgets/bench"}
        args.update(extra)
        _e, saved = verb("remember", **args)
        return saved

    def test_forget_writes_a_retraction_naming_the_inbox_path(self):
        saved = self.remember("A fact to forget.")
        _e, gone = verb("forget", id=saved["id"], reason="not true after all")
        self.assertEqual((gone["expired"], gone["reason"]), (True, "not true after all"))
        notes = [f for f in inbox_files(self.corpus) if "retract" in f.name]
        self.assertEqual(len(notes), 1)
        fm, _x = parse_frontmatter(notes[0].read_text())
        self.assertEqual(fm["kind"], "correction")
        self.assertIn("Retract knowledge/inbox/", notes[0].read_text())
        self.assertIn("not true after all", notes[0].read_text())
        _e, again = verb("forget", id=saved["id"])
        self.assertEqual(again["expired"], False)
        self.assertEqual(len([f for f in inbox_files(self.corpus) if "retract" in f.name]), 1,
                         "forgetting twice wrote a second retraction")

    def test_the_forgotten_fact_leaves_every_view_and_cannot_be_said_again(self):
        saved = self.remember("A fact to forget.")
        verb("forget", id=saved["id"])
        _e, found = verb("recall", entity="widgets/bench")
        self.assertEqual(found["facts"], [])
        _e, found = verb("recall", scope="all", query="zzzz")
        self.assertNotIn(saved["id"], [f["fact_id"] for f in found["facts"]])
        _e, delta = verb("delta", since="1970-01-01")
        self.assertNotIn(saved["id"], [f["fact_id"] for f in delta["facts"]])
        is_error, body = verb("remember", fact="  A fact   to forget. ", provenance="p")
        self.assertTrue(is_error)
        self.assertTrue(body["message"].startswith("fact_withdrawn"), body)
        is_error, _b = verb("remember", fact="A different fact.", provenance="p")
        self.assertFalse(is_error)

    def test_unknown_ids_are_not_found(self):
        for value in ("999999999", "widget-glossary", "knowledge/inbox/x.md",
                      "inbox-no-such-capture"):
            with self.subTest(id=value):
                is_error, body = verb("forget", id=value)
                self.assertTrue(is_error)
                self.assertEqual(body["error"], "not_found")
                self.assertTrue(body["suggestion"])

    def test_an_expired_ttl_hides_a_fact(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        saved = self.remember("Already stale fact.", ttl=past)
        _e, found = verb("recall", entity="widgets/bench")
        self.assertNotIn(saved["id"], [f["fact_id"] for f in found["facts"]])

    def test_the_facts_arm_opens_only_on_request(self):
        self.remember("A visible fact about benches.")
        _e, plain = verb("recall", query="benches")
        self.assertEqual(plain["facts"], [])
        self.assertEqual(plain["total"], 0)
        _e, with_entity = verb("recall", query="benches", entity="widgets/bench")
        self.assertEqual(with_entity["total"], 1)
        _e, with_all = verb("recall", scope="all", query="benches")
        self.assertEqual(with_all["total"], len(with_all["facts"]))
        self.assertGreaterEqual(with_all["total"], 1)
        fact = with_entity["facts"][0]
        self.assertEqual(fact["trust"], "provisional")
        self.assertEqual(fact["markers"], ["[provisional — unconsolidated]"])
        self.assertEqual(with_entity["facts_order"], "newest")

    def test_private_facts_never_reach_a_remote_caller(self):
        self.remember("A private fact.", visibility="private")
        local = json.loads(mcpcore.call("recall", {"entity": "widgets/bench"},
                                        transport="local")["content"][0]["text"])
        remote = json.loads(mcpcore.call("recall", {"entity": "widgets/bench"},
                                         transport="remote")["content"][0]["text"])
        self.assertEqual([f["visibility"] for f in local["facts"]], ["private"])
        self.assertEqual(remote["facts"], [])
        delta = json.loads(mcpcore.call("delta", {"since": "1970-01-01"},
                                        transport="remote")["content"][0]["text"])
        self.assertNotIn("A private fact.", [f["fact"] for f in delta["facts"]])
        local_delta = json.loads(mcpcore.call("delta", {"since": "1970-01-01"},
                                              transport="local")["content"][0]["text"])
        self.assertIn("A private fact.", [f["fact"] for f in local_delta["facts"]])

    def test_facts_are_newest_first_and_budgeted_as_a_prefix(self):
        ids = [self.remember("Fact number %d about benches." % n)["id"] for n in range(3)]
        _e, found = verb("recall", entity="widgets/bench")
        self.assertEqual(found["total"], 3)
        self.assertEqual({f["fact_id"] for f in found["facts"]}, set(ids))
        times = [f["created_at"] for f in found["facts"]]
        self.assertEqual(times, sorted(times, reverse=True))
        _e, tight = verb("recall", entity="widgets/bench", budget_tokens=1)
        self.assertEqual((tight["total"], tight["dropped_count"]), (0, 3))
        _e, roomy = verb("recall", entity="widgets/bench", budget_tokens=10_000)
        self.assertEqual((roomy["total"], roomy["dropped_count"]), (3, 0))
        self.assertGreater(roomy["budget_used"], 0)


class RecallTests(CorpusCase):
    QUERIES = (("choose widget engine", {}), ("engine pick", {}), ("sprocket engine", {}),
               ("sprocket zebra", {}), ("sprock", {}), ("zzzqqq", {}), ("widget", {"limit": 1}),
               ("sprocket", {"scope": "all", "limit": 20}), ("", {"scope": "handoff"}),
               ("gadget", {"scope": "handoff"}), ("widget", {"limit": 3}))

    @staticmethod
    def paths_of_search(text):
        return re.findall(r"^\d+\. (\S+) — ", text, re.M)

    def test_keyword_mode_is_exactly_brain_searchs_ranking(self):
        for query, extra in self.QUERIES:
            with self.subTest(query=query, extra=extra):
                old = mcpcore.call("brain_search", dict(query=query, **extra))
                new = json.loads(mcpcore.call(
                    "recall", dict(query=query, mode="keyword", **extra))["content"][0]["text"])
                self.assertEqual([r["path"] for r in new["results"]],
                                 self.paths_of_search(old["content"][0]["text"]))
                more = re.search(r"\((\d+) more hits below the cap", old["content"][0]["text"])
                self.assertEqual(new.get("more", 0), int(more.group(1)) if more else 0)
                mode = re.search(r"\[match mode: ([\w-]+)\]", old["content"][0]["text"])
                if mode:
                    self.assertEqual(new["match_mode"], mode.group(1))

    def test_the_default_mode_is_keyword_and_hybrid_degrades_instead_of_failing(self):
        _e, default = verb("recall", query="sprocket")
        _e, keyword = verb("recall", query="sprocket", mode="keyword")
        _e, hybrid = verb("recall", query="sprocket", mode="hybrid")
        self.assertNotIn("search_degraded", default)
        self.assertNotIn("search_degraded", keyword)
        self.assertEqual(hybrid.pop("search_degraded"), "embed_unavailable")
        self.assertEqual(default, keyword)
        self.assertEqual(hybrid, keyword)

    def test_the_markers_are_fields_and_the_trust_class_is_derived_in_order(self):
        _e, found = verb("recall", query="sprocket", scope="all", limit=20)
        by_path = {r["path"]: r for r in found["results"]}
        glossary = by_path["knowledge/reference/widget-glossary.md"]
        self.assertEqual(glossary["trust"], "stale")
        self.assertEqual(glossary["markers"][0], "[STALE — review_by 2026-04-01 passed]")
        self.assertTrue(glossary["markers"][1].startswith("[correction pending — "))
        self.assertEqual(glossary["review_by"], "2026-04-01")
        self.assertEqual(by_path["knowledge/journal/2026/2026-03-05.md"]["trust"], "journal")
        idea = by_path["knowledge/inbox/2026-03-06-090000-widget-idea.md"]
        self.assertEqual((idea["trust"], idea["markers"]),
                         ("provisional", ["[provisional — unconsolidated]"]))
        decision = by_path["knowledge/decisions/2026-03-01-choose-widget-engine.md"]
        self.assertEqual((decision["trust"], decision["markers"], decision["slug"]),
                         ("current", [], "2026-03-01-choose-widget-engine"))
        _e, handoff = verb("recall", scope="handoff", query="")
        note = handoff["results"][0]
        self.assertEqual((note["trust"], note["handoff_to"], note["handoff_at"]),
                         ("not_current", "gadget", "2026-03-09"))
        self.assertEqual(note["markers"], ["[handoff — belongs in gadget]"])

    def test_evidence_and_create_safety(self):
        _e, title = verb("recall", query="Choose the widget engine")
        self.assertEqual((title["results"][0]["evidence"], title["results"][0]["create_safety"]),
                         ("exact_title_match", "exists"))
        _e, alias = verb("recall", query="engine pick")
        self.assertEqual((alias["results"][0]["evidence"], alias["results"][0]["create_safety"]),
                         ("alias_hit", "unknown"))
        _e, plain = verb("recall", query="sprocket")
        self.assertEqual(plain["results"][0]["evidence"], "keyword_exact")

    def test_something_to_look_for_is_required(self):
        is_error, body = verb("recall")
        self.assertTrue(is_error)
        self.assertEqual(body["error"], "invalid_params")
        is_error, body = verb("recall", query="   ")
        self.assertEqual(body["error"], "invalid_params")

    def test_source_id_since_and_ignored_parameters(self):
        is_error, body = verb("recall", query="sprocket", source_id="other")
        self.assertEqual((is_error, body["error"]), (True, "not_found"))
        is_error, body = verb("recall", entity="widgets/x", since="not a date")
        self.assertEqual((is_error, body["error"]), (True, "invalid_params"))
        is_error, body = verb("recall", query="sprocket", question="why?", session_id="s",
                              budget_policy="query_first", source_id="default")
        self.assertFalse(is_error)
        self.assertEqual(body["facts_order"], "newest")

    def test_a_pages_budget_is_a_prefix(self):
        _e, full = verb("recall", query="widget", limit=10)
        _e, tight = verb("recall", query="widget", limit=10, budget_tokens=1)
        self.assertEqual((tight["results"], tight["dropped_count"]), ([], len(full["results"])))


class DeltaTests(CorpusCase):
    def test_a_walk_in_one_call(self):
        _e, delta = verb("delta", since="1970-01-01T00:00:00Z")
        self.assertEqual(delta["since"], "1970-01-01T00:00:00.000Z")
        self.assertEqual(delta["threads"], [])
        self.assertFalse(delta["has_more"])
        stamps = [p["modified"] for p in delta["pages"]]
        self.assertEqual(stamps, sorted(stamps), "oldest first")
        folders = {p["folder"] for p in delta["pages"]}
        self.assertFalse({"archive", "vault"} & folders)
        self.assertIn("handoff", folders)
        self.assertEqual(len(delta["pages"]), 8)
        self.assertEqual({f["fact_id"] for f in delta["facts"]},
                         {"inbox-2026-03-06-090000-widget-idea",
                          "inbox-2026-03-07-090000-glossary-fix"})
        self.assertTrue(delta["next_cursor"]["cursor"])
        self.assertIn("handoff", delta["text"])

    def test_since_is_normalised(self):
        for given, expected in (("1970-01-01", "1970-01-01T00:00:00.000Z"),
                                ("2026-10-29T08:00:00+04:00", "2026-10-29T04:00:00.000Z"),
                                ("2026-10-29T08:00:00.1234Z", "2026-10-29T08:00:00.123Z"),
                                ("0001-01-01", "0001-01-01T00:00:00.000Z")):
            with self.subTest(since=given):
                _e, delta = verb("delta", since=given)
                self.assertEqual(delta["since"], expected)
        for bad in ("yesterday", "2026-13-01", "10000-01-01", ""):
            with self.subTest(bad=bad):
                is_error, body = verb("delta", since=bad)
                self.assertEqual((is_error, body["error"]), (True, "invalid_params"))

    def test_neither_since_nor_cursor_is_invalid_even_with_a_session_id(self):
        for args in ({}, {"session_id": "abc"}):
            is_error, body = verb("delta", **args)
            self.assertEqual((is_error, body["error"]), (True, "invalid_params"))
            self.assertTrue(body["suggestion"])

    def test_a_cursor_resumes_and_a_budget_pages_without_loss_or_repeat(self):
        _e, whole = verb("delta", since="1970-01-01")
        seen_pages, seen_facts, cursor, rounds = [], [], None, 0
        args = {"since": "1970-01-01", "budget_tokens": 60}
        while rounds < 40:
            rounds += 1
            _e, step = verb("delta", **args)
            seen_pages += [p["path"] for p in step["pages"]]
            seen_facts += [f["fact_id"] for f in step["facts"]]
            cursor = step["next_cursor"]["cursor"]
            args = {"cursor": cursor, "budget_tokens": 60}
            if not step["has_more"]:
                break
        self.assertGreater(rounds, 1, "the budget was meant to force several calls")
        self.assertEqual(seen_pages, [p["path"] for p in whole["pages"]])
        self.assertEqual(seen_facts, [f["fact_id"] for f in whole["facts"]])
        _e, after = verb("delta", cursor=cursor)
        self.assertEqual((after["pages"], after["facts"], after["has_more"]), ([], [], False))
        saved = verb("remember", fact="Something new.", provenance="p")[1]
        _e, news = verb("delta", cursor=after["next_cursor"]["cursor"])
        self.assertEqual([f["fact_id"] for f in news["facts"]], [saved["id"]])

    def test_a_budget_too_small_for_anything_loses_nothing(self):
        _e, delta = verb("delta", since="1970-01-01", budget_tokens=1)
        self.assertEqual((delta["pages"], delta["facts"], delta["has_more"]), ([], [], True))
        self.assertEqual(delta["degraded_reason"], "budget_too_small")
        _e, retry = verb("delta", cursor=delta["next_cursor"]["cursor"])
        self.assertEqual(len(retry["pages"]), 8)

    def test_a_cursor_that_is_not_ours_is_refused(self):
        for cursor in ("garbage", "e30", ""):
            is_error, body = verb("delta", cursor=cursor) if cursor else (True, {"error": "x"})
            self.assertTrue(is_error)
        is_error, body = verb("delta", cursor="garbage")
        self.assertEqual(body["error"], "invalid_params")

    def test_the_cursor_wins_over_since(self):
        _e, first = verb("delta", since="1970-01-01")
        _e, second = verb("delta", since="1970-01-01", cursor=first["next_cursor"]["cursor"])
        self.assertEqual(second["pages"], [])


class EnvelopeAndProfileTests(CorpusCase):
    def test_every_verb_reply_and_error_carries_protocol_version_1(self):
        calls = (("recall", {"query": "sprocket"}), ("recall", {}),
                 ("remember", {"fact": "Envelope fact.", "provenance": "p"}),
                 ("remember", {"fact": "x"}), ("forget", {"id": "nope"}),
                 ("delta", {"since": "1970-01-01"}), ("delta", {}),
                 ("entity", {"name": "x"}))
        for name, args in calls:
            with self.subTest(name=name, args=args):
                result = mcp.call_tool(name, args)
                body = json.loads(result["content"][0]["text"])
                self.assertEqual(body["protocol_version"], 1)
                if result["isError"]:
                    self.assertTrue(body["suggestion"].strip())
                    self.assertIn(body["error"], ("invalid_params", "provenance_required",
                                                  "not_found", "scope_denied", "unavailable",
                                                  "internal"))

    def test_the_unsupported_verbs_are_unlisted_and_unavailable_on_every_profile(self):
        for profile, served in (("capture", "recall, remember, forget and delta"),
                                ("read", "recall and delta")):
            listed = {t["name"] for t in mcpcore.tools_for(profile, "remote")}
            for name in verbs.UNSUPPORTED:
                with self.subTest(profile=profile, verb=name):
                    self.assertNotIn(name, listed)
                    result = mcpcore.call(name, {}, profile=profile, transport="remote")
                    body = json.loads(result["content"][0]["text"])
                    self.assertTrue(result["isError"])
                    self.assertEqual(body["error"], "unavailable")
                    self.assertEqual(body["detail"], "unsupported_verb:" + name)
                    self.assertEqual(body["protocol_version"], 1)
                    self.assertIn("Use recall", body["suggestion"])
                    self.assertIn("This brain serves %s." % served, body["suggestion"])
                    self.assertIn(name, body["message"])

    def test_the_read_profile_refuses_the_write_verbs_with_scope_denied(self):
        before = inbox_files(self.corpus)
        for name, args in (("remember", {"fact": "x fact", "provenance": "p"}),
                           ("forget", {"id": "inbox-x"})):
            result = mcpcore.call(name, args, profile="read", transport="remote")
            body = json.loads(result["content"][0]["text"])
            self.assertEqual((result["isError"], body["error"]), (True, "scope_denied"))
            self.assertTrue(body["suggestion"])
        self.assertEqual(inbox_files(self.corpus), before)

    def test_the_stdio_allow_set_is_the_boundary_too(self):
        result = mcp.call_tool("remember", {"fact": "x fact", "provenance": "p"},
                               allow=mcp.READ_ONLY_TOOLS)
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "scope_denied")
        result = mcp.call_tool("remember", {"fact": "x fact", "provenance": "p"},
                               allow=mcp.DROP_BOX_TOOLS)
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "scope_denied")
        self.assertEqual(inbox_files(self.corpus)[-1].name.count("x-fact"), 0)

    def test_the_profiles_list_what_the_contract_says(self):
        read = {t["name"] for t in mcpcore.tools_for("read", "remote")}
        full = {t["name"] for t in mcpcore.tools_for("capture", "remote")}
        old = {"brain_search", "brain_read", "brain_links", "brain_recent"}
        self.assertEqual(read, old | {"recall", "delta"})
        self.assertEqual(full, old | {"recall", "delta", "remember", "forget", "brain_capture"})

    def test_there_is_one_table(self):
        local = {t["name"]: t for t in mcpcore.tool_table("local")}
        self.assertEqual(mcp.TOOLS_BY_NAME, local)
        self.assertEqual([t["name"] for t in mcp.TOOLS],
                         [t["name"] for t in mcpcore.tool_table("remote")])

    def test_limits_differ_by_transport_as_the_contract_says(self):
        local = {t["name"]: t for t in mcpcore.tool_table("local")}
        remote = {t["name"]: t for t in mcpcore.tool_table("remote")}
        self.assertEqual(remote["recall"]["inputSchema"]["properties"]["limit"]["maximum"], 20)
        self.assertEqual(local["recall"]["inputSchema"]["properties"]["limit"]["maximum"], 100)
        self.assertEqual(remote["remember"]["inputSchema"]["properties"]["fact"]["maxLength"],
                         65_536)
        self.assertEqual(local["remember"]["inputSchema"]["properties"]["fact"]["maxLength"],
                         100_000)

    def test_stdio_initialize_advertises_the_contract_and_the_engine_version(self):
        for allow, served in ((None, ["recall", "remember", "forget", "delta"]),
                              (mcp.READ_ONLY_TOOLS, ["recall", "delta"]),
                              (mcp.DROP_BOX_TOOLS, [])):
            with self.subTest(served=served):
                reply = mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {}}, allow=allow)
                self.assertEqual(reply["result"]["serverInfo"],
                                 {"name": "brain", "version": version.VERSION})
                self.assertNotEqual(version.VERSION, "1.0.0")
                self.assertEqual(reply["result"]["_meta"]["brain-verbs"],
                                 {"contract": "brain-verbs.v1", "memory_verbs": 1,
                                  "verbs": served,
                                  "unsupported": ["entity", "context_pack", "synthesize"]})

    def test_the_old_tools_event_vocabulary_names_the_new_tools_as_themselves(self):
        from brainlib import eventlog
        for name in ("recall", "remember", "forget", "delta"):
            self.assertIn(name, eventlog.VALUES)


if __name__ == "__main__":
    unittest.main()
