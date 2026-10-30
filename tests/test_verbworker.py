#!/usr/bin/env python3
"""The warm recall worker (brainlib/verbworker.py, `brain verb-worker`) and the vector cache.

What is proved: a warm answer is the one-shot answer, byte for byte; one process serves many
calls; it is replaced when it dies, when a BRAIN_* setting changes and when a call outlives
its timeout; it follows vectors.db as the file changes; it holds no lock between calls; it
can be switched off. And the vector cache never serves a file that may have changed under it.

Run:  python3 -m unittest tests.test_verbworker -v
"""
import fcntl
import json
import operator
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from array import array
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memverbs_support  # noqa: E402,F401  (puts the engine's bin/ on sys.path)
from brainlib import mcp, vectors, verbs, verbworker  # noqa: E402
from test_hybrid import HybridCase, paths, recall  # noqa: E402


def pid():
    proc = verbworker._state["proc"]
    return proc.pid if proc else None


class WorkerTests(HybridCase):
    def setUp(self):
        super().setUp()
        self.index()
        self.env(BRAIN_VECTORS="on", BRAIN_VERB_WORKER=None)
        self.addCleanup(verbworker.stop)
        self.addCleanup(self.env, BRAIN_VERB_WORKER=None)
        verbworker.stop()

    def cold(self, **args):
        """The same call answered by the one-shot command."""
        self.env(BRAIN_VERB_WORKER="off")
        try:
            return recall(**args)
        finally:
            self.env(BRAIN_VERB_WORKER=None)

    def test_a_warm_answer_is_the_one_shot_answer(self):
        for args in ({"query": "gizmo", "mode": "hybrid"}, {"query": "sprocket engine"},
                     {"query": "gizmo", "mode": "keyword"}, {"query": "gizmo", "scope": "all"}):
            warm = recall(**args)
            self.assertIsNotNone(pid(), args)
            self.assertEqual(warm, self.cold(**args), args)

    def test_one_process_serves_many_calls(self):
        recall(query="gizmo")
        first = pid()
        for query in ("sprocket engine", "gizmo", "car"):
            recall(query=query)
        self.assertEqual(pid(), first)

    def test_a_dead_worker_is_replaced_and_the_call_still_answers(self):
        want = recall(query="gizmo")
        first = pid()
        os.kill(first, signal.SIGKILL)
        time.sleep(0.2)
        self.assertEqual(recall(query="gizmo"), want)
        self.assertNotEqual(pid(), first)

    def test_a_changed_setting_starts_a_new_worker(self):
        recall(query="gizmo")
        first = pid()
        self.env(BRAIN_VECTOR_FLOOR="0.99")
        got = recall(query="gizmo")
        self.assertNotEqual(pid(), first)
        self.assertEqual(got, self.cold(query="gizmo"))
        self.assertNotIn(self.GLOSSARY, paths(got))

    GLOSSARY = "knowledge/reference/widget-glossary.md"

    def test_it_follows_vectors_db_as_the_file_changes(self):
        self.assertIn(self.GLOSSARY, paths(recall(query="gizmo")))
        first = pid()
        (self.corpus.state / "vectors.db").unlink()
        self.assertNotIn(self.GLOSSARY, paths(recall(query="gizmo")))
        self.assertEqual(recall(query="gizmo")["search_degraded"], "embed_unavailable")
        time.sleep(2.2)                      # the rebuilt file is older than the cache's settle time
        self.index()
        self.assertIn(self.GLOSSARY, paths(recall(query="gizmo")))
        self.assertEqual(pid(), first)

    def test_a_call_past_its_timeout_kills_the_worker_and_the_next_one_answers(self):
        recall(query="gizmo")
        first = pid()
        request, _ = verbs.prepare_recall({"query": "gizmo", "mode": "hybrid"}, False,
                                          datetime.now(timezone.utc))
        self.fake.delay = 1.5
        with self.assertRaises(subprocess.TimeoutExpired):
            verbworker.run(["verb", "recall", "-"], 0.4, json.dumps(request))
        self.assertIsNone(pid())
        self.fake.delay = 0.0
        self.assertIn(self.GLOSSARY, paths(recall(query="gizmo")))
        self.assertNotEqual(pid(), first)

    def test_it_holds_no_lock_between_calls(self):
        recall(query="gizmo")
        self.assertIsNotNone(pid())
        with open(self.corpus.state / "tree.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)      # raises if the worker holds it

    def test_it_can_be_switched_off(self):
        self.env(BRAIN_VERB_WORKER="off")
        recall(query="gizmo")
        self.assertIsNone(pid())

    def test_other_ops_and_odd_calls_are_left_to_the_one_shot_command(self):
        self.assertIsNone(verbworker.run(["verb", "delta", "-"], 5, "{}"))
        self.assertIsNone(verbworker.run(["verb", "recall", "{}"], 5, None))
        self.assertIsNone(verbworker.run(["verb", "recall", "-"], 5, "not json"))
        self.assertIsNone(verbworker.run(["verb", "recall", "-"], 5, "{}"))     # the worker says no
        self.assertEqual(recall(query="gizmo"), self.cold(query="gizmo"))

    def test_concurrent_recalls_do_not_queue_behind_one_worker(self):
        """The worker takes one call at a time; the others run the one-shot command at once
        instead of waiting their turn (and then their own full timeout)."""
        self.fake.delay = 1.5
        want = self.cold(query="gizmo")
        results, errors = [], []

        def call():
            try:
                results.append(recall(query="gizmo"))
            except BaseException as exc:         # noqa: BLE001
                errors.append(exc)
        threads = [threading.Thread(target=call) for _ in range(5)]
        began = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        elapsed = time.monotonic() - began
        self.assertEqual(errors, [])
        self.assertEqual(results, [want] * 5)
        self.assertGreaterEqual(verbworker.fallbacks().get("busy", 0), 1)
        self.assertLess(elapsed, 5 * 1.5 * 0.7, "the calls were serialized behind the worker")

    def test_a_busy_worker_is_not_waited_for(self):
        recall(query="gizmo")
        request, _ = verbs.prepare_recall({"query": "gizmo"}, False, datetime.now(timezone.utc))
        began = time.monotonic()
        with verbworker._lock:                   # another call is in the worker
            self.assertIsNone(verbworker.run(["verb", "recall", "-"], 30, json.dumps(request)))
        self.assertLess(time.monotonic() - began, 1.0)
        self.assertIsNotNone(pid(), "a busy worker is left alone, not killed")

    def test_the_prewarmed_worker_has_the_vectors_before_the_first_call(self):
        verbworker.prewarm(cwd=mcp.ROOT).join(30)
        first = pid()
        self.assertIsNotNone(first)
        recall(query="gizmo")
        self.assertEqual(pid(), first)


class FallbackReportTests(unittest.TestCase):
    def setUp(self):
        self.seen = []
        self.addCleanup(setattr, verbworker, "observer", None)
        self.addCleanup(verbworker._fallbacks.clear)
        self.addCleanup(verbworker._reported.clear)
        verbworker._fallbacks.clear()
        verbworker._reported.clear()
        verbworker.observer = self.seen.append

    def test_a_busy_worker_and_a_refusal_are_reported_and_counted(self):
        with verbworker._lock:
            self.assertIsNone(verbworker.run(["verb", "recall", "-"], 5, "{}"))
        self.assertEqual(self.seen, ["busy"])
        self.addCleanup(verbworker.stop)
        self.addCleanup(os.environ.pop, "BRAIN_VECTORS", None)
        self.assertIsNone(verbworker.run(["verb", "recall", "-"], 20, "{}"))      # the worker says no
        self.assertEqual(self.seen, ["busy", "refused"])
        self.assertEqual(verbworker.fallbacks(), {"busy": 1, "refused": 1})

    def test_without_an_observer_it_goes_to_stderr_once_a_minute_per_reason(self):
        import contextlib
        import io
        verbworker.observer = None
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            for _ in range(3):
                with verbworker._lock:
                    verbworker.run(["verb", "recall", "-"], 5, "{}")
        self.assertEqual(verbworker.fallbacks(), {"busy": 3})
        self.assertEqual(err.getvalue().count("one-shot"), 1)
        self.assertIn("busy", err.getvalue())

    def test_both_servers_write_a_fall_back_to_their_event_log(self):
        from brainlib import eventlog, httplog, httpmcp, serve
        with tempfile.TemporaryDirectory() as tmp:
            for make, watch, name in (
                    (lambda d: eventlog.EventLog(Path(d) / "events.jsonl"), serve.watch_worker, "events.jsonl"),
                    (lambda d: httplog.EventLog(d), httpmcp.watch_worker, "events.jsonl")):
                folder = Path(tmp) / str(watch.__module__)
                folder.mkdir()
                log = make(folder)
                watch(log)
                for reason in verbworker.REASONS:            # every reason the worker can give is loggable
                    verbworker._fell_back(reason)
                lines = [json.loads(line) for line in (folder / name).read_text().splitlines()]
                self.assertEqual([(e["event"], e["reason"]) for e in lines],
                                 [("worker_fallback", r) for r in verbworker.REASONS])

    def test_a_call_the_worker_does_not_take_is_not_a_fall_back(self):
        self.assertIsNone(verbworker.run(["verb", "delta", "-"], 5, "{}"))
        self.assertIsNone(verbworker.run(["verb", "recall", "-"], 5, "not json"))
        self.assertEqual(self.seen, [])


class EngineUpdateTests(HybridCase):
    def setUp(self):
        super().setUp()
        self.index()
        self.env(BRAIN_VECTORS="on", BRAIN_VERB_WORKER=None)
        self.addCleanup(verbworker.stop)
        verbworker.stop()

    def test_the_stamp_follows_bin_brain_and_every_brainlib_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "brainlib").mkdir()
            (root / "brain").write_text("a")
            (root / "brainlib" / "x.py").write_text("a")
            real, verbworker.BRAIN = verbworker.BRAIN, str(root / "brain")
            self.addCleanup(setattr, verbworker, "BRAIN", real)
            first = verbworker._engine_stamp()
            self.assertEqual(verbworker._engine_stamp(), first)
            (root / "brainlib" / "x.py").write_text("bb")
            second = verbworker._engine_stamp()
            self.assertNotEqual(second, first)
            os.utime(root / "brain", (1, 1))
            self.assertNotEqual(verbworker._engine_stamp(), second)
            (root / "brainlib" / "y.py").write_text("")
            self.assertEqual(len(verbworker._engine_stamp()), 3)

    def test_an_engine_update_replaces_the_warm_worker(self):
        want = recall(query="gizmo")
        first = pid()
        self.assertIsNotNone(first)
        self.assertEqual(recall(query="gizmo"), want)
        self.assertEqual(pid(), first)
        real = verbworker._engine_stamp
        verbworker._engine_stamp = lambda: real() + (("brain", 1, 1),)     # `brain sync` installed new code
        self.addCleanup(setattr, verbworker, "_engine_stamp", real)
        self.assertEqual(recall(query="gizmo"), want)
        self.assertNotEqual(pid(), first)


def write_db(path, rows):
    db = sqlite3.connect(str(path))
    db.execute(vectors.SCHEMA)
    with db:
        db.executemany("INSERT INTO vectors VALUES (?,?,?,?,?)",
                       [(n, 0, "h", "m", array("f", v).tobytes()) for n, v in rows])
    db.close()


class VectorCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / vectors.DB_NAME
        vectors._LOADED.clear()

    def age(self, seconds=3600):
        old = time.time() - seconds
        os.utime(self.path, (old, old))

    def test_an_unchanged_file_is_read_once(self):
        write_db(self.path, [("a", (1.0, 0.0))])
        self.age()
        self.assertIs(vectors.load(self.tmp.name, "m"), vectors.load(self.tmp.name, "m"))

    def test_a_changed_file_is_read_again(self):
        write_db(self.path, [("a", (1.0, 0.0))])
        self.age()
        before = vectors.load(self.tmp.name, "m")
        write_db(self.path, [("b", (0.0, 1.0))])
        self.age(1800)
        after = vectors.load(self.tmp.name, "m")
        self.assertEqual([r[0] for r in before], ["a"])
        self.assertEqual([r[0] for r in after], ["a", "b"])

    def test_a_file_written_a_moment_ago_is_never_kept(self):
        write_db(self.path, [("a", (1.0, 0.0))])
        first = vectors.load(self.tmp.name, "m")
        self.assertIsNot(first, vectors.load(self.tmp.name, "m"))
        self.assertEqual(first, vectors.load(self.tmp.name, "m"))

    def test_a_vanished_file_is_empty_not_remembered(self):
        write_db(self.path, [("a", (1.0, 0.0))])
        self.age()
        self.assertEqual(len(vectors.load(self.tmp.name, "m")), 1)
        self.path.unlink()
        self.assertEqual(vectors.load(self.tmp.name, "m"), [])

    def test_the_two_dot_products_agree(self):
        a = array("f", (0.1 * i for i in range(768)))
        b = array("f", (1.0 - 0.001 * i for i in range(768)))
        slow = sum(map(operator.mul, a, b))
        self.assertAlmostEqual(vectors.cosine(tuple(a), b), slow, places=6)
        self.assertEqual(vectors.cosine(tuple(a), b[:5]), 0.0)


if __name__ == "__main__":
    unittest.main()
