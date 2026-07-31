"""Tests for the event log — the thing that must never contain the brain.

Two properties get almost all the tests here, and neither is about logging:

1. **Nothing that authenticates anything is written.** No token, no header.
2. **Nothing the caller supplied is written.** No search query, no note body,
   no note id, no path string.

Both are held by CONSTRUCTION rather than by a filter: `record` accepts only
field names and string values from fixed vocabularies, so a caller-supplied
string cannot be passed to it at all. The tests below therefore assert on the
refusal, not on a scrubbed output — a scrubber that missed a field would still
pass a test that only checked the fields it knew about.
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import eventlog  # noqa: E402
from brainlib import osbackend  # noqa: E402


class FakeClock:
    """Time as a parameter, never as a sleep. Same rule as Limiter's."""

    def __init__(self, start="2026-07-31T09:00:00Z"):
        self.value = start

    def __call__(self):
        return self.value


class StateDirTests(unittest.TestCase):
    """Where machine-local state lives, and why it is per-brain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "state"
        self.env = {"XDG_STATE_HOME": str(self.home)}

    def test_lives_outside_the_brain(self):
        """The strongest guarantee that a log never reaches git is that it is
        not in the repository at all — stronger than a .gitignore line."""
        brain = Path(self.tmp.name) / "brain"
        brain.mkdir()
        where = osbackend.state_dir(brain, env=self.env)
        self.assertNotIn(str(brain.resolve()), str(where.resolve()))
        self.assertTrue(str(where).startswith(str(self.home)))

    def test_two_brains_do_not_share_one_directory(self):
        """The business partition found two endpoints on one host sharing one
        keystore entry, so P's read token also opened M's drop box. This is the
        same trap, and it is closed here rather than documented."""
        one = Path(self.tmp.name) / "brain-one"
        two = Path(self.tmp.name) / "brain-two"
        one.mkdir()
        two.mkdir()
        self.assertNotEqual(osbackend.state_dir(one, env=self.env),
                            osbackend.state_dir(two, env=self.env))

    def test_same_brain_is_stable_across_calls(self):
        brain = Path(self.tmp.name) / "brain"
        brain.mkdir()
        self.assertEqual(osbackend.state_dir(brain, env=self.env),
                         osbackend.state_dir(brain, env=self.env))

    def test_names_the_brain_it_belongs_to(self):
        """An operator who finds this directory can tell whose it is."""
        brain = Path(self.tmp.name) / "brain"
        brain.mkdir()
        where = osbackend.state_dir(brain, env=self.env)
        self.assertEqual((where / "root").read_text(encoding="utf-8").strip(),
                         str(brain.resolve()))
        self.assertIn("brain", where.name)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_is_not_world_readable(self):
        """It holds hashed credentials. 0700, created that way, never chmod-ed
        afterwards — the window between the two is when a backup job runs."""
        brain = Path(self.tmp.name) / "brain"
        brain.mkdir()
        where = osbackend.state_dir(brain, env=self.env)
        self.assertEqual(stat.S_IMODE(where.stat().st_mode) & 0o077, 0)

    def test_falls_back_without_xdg(self):
        brain = Path(self.tmp.name) / "brain"
        brain.mkdir()
        fake_home = Path(self.tmp.name) / "home"
        where = osbackend.state_dir(brain, env={}, home=fake_home)
        self.assertTrue(str(where).startswith(str(fake_home / ".local" / "state")))


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.clock = FakeClock()
        self.log = eventlog.EventLog(self.path, clock=self.clock)

    def lines(self):
        return [json.loads(line) for line in
                self.path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_writes_one_json_object_per_line(self):
        self.log.record("request", method="POST", path_class="mcp", status=200, ms=3)
        entry, = self.lines()
        self.assertEqual(entry["event"], "request")
        self.assertEqual(entry["status"], 200)
        self.assertEqual(entry["ts"], "2026-07-31T09:00:00Z")

    def test_an_unknown_event_raises(self):
        """The vocabulary IS the redaction control. It fails loudly in a test
        rather than quietly admitting a free-text field in production."""
        with self.assertRaises(ValueError):
            self.log.record("something_new", status=200)
        self.assertFalse(self.path.exists())

    def test_an_unknown_field_raises(self):
        with self.assertRaises(ValueError):
            self.log.record("request", query="what did I decide about pricing")

    def test_an_arbitrary_string_value_raises(self):
        """The whole point. A caller cannot pass a string this module has not
        already agreed to, so a search query cannot arrive by any route."""
        with self.assertRaises(ValueError):
            self.log.record("tool_call", tool="brain_search",
                            outcome="pricing model for the enterprise tier")

    def test_a_known_string_value_is_accepted(self):
        self.log.record("tool_call", tool="brain_search", outcome="ok", ms=12)
        entry, = self.lines()
        self.assertEqual(entry["tool"], "brain_search")

    def test_tool_names_come_from_the_tool_table(self):
        """Derived, not copied: a sixth tool added to mcp.TOOLS is loggable the
        day it exists, and a typo here cannot invent a tool that does not."""
        from brainlib import mcp
        for name in mcp.TOOLS_BY_NAME:
            self.log.record("tool_call", tool=name, outcome="ok")
        self.assertEqual(len(self.lines()), len(mcp.TOOLS_BY_NAME))

    def test_numbers_and_booleans_pass_through(self):
        self.log.record("request", status=401, ms=0, oauth=True)
        entry, = self.lines()
        self.assertEqual(entry["status"], 401)
        self.assertIs(entry["oauth"], True)

    def test_a_non_scalar_raises(self):
        with self.assertRaises(ValueError):
            self.log.record("request", status={"nested": "object"})

    def test_a_write_failure_is_swallowed(self):
        """A log that can take the server down is a worse problem than a log
        with a gap in it."""
        log = eventlog.EventLog(Path(self.tmp.name) / "no" / "such" / "dir" / "e.jsonl")
        log.record("request", status=200)     # must not raise

    def test_appends_rather_than_truncating(self):
        self.log.record("request", status=200)
        eventlog.EventLog(self.path, clock=self.clock).record("request", status=201)
        self.assertEqual(len(self.lines()), 2)


class RotationTests(unittest.TestCase):
    """A bound decided now, not after a disk fills."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.log = eventlog.EventLog(self.path, clock=FakeClock(), max_bytes=400)

    def test_rolls_over_at_the_cap(self):
        for _ in range(40):
            self.log.record("request", method="POST", path_class="mcp", status=200)
        self.assertTrue(self.path.exists())
        self.assertTrue(self.path.with_suffix(".jsonl.1").exists())
        self.assertLessEqual(self.path.stat().st_size, 400 + 200)

    def test_keeps_exactly_one_generation(self):
        for _ in range(200):
            self.log.record("request", method="POST", path_class="mcp", status=200)
        siblings = sorted(p.name for p in Path(self.tmp.name).iterdir())
        self.assertEqual(siblings, ["events.jsonl", "events.jsonl.1"])


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.clock = FakeClock()
        self.log = eventlog.EventLog(self.path, clock=self.clock)

    def test_reads_newest_last(self):
        for status in (200, 401, 500):
            self.log.record("request", status=status)
        got = self.log.read()
        self.assertEqual([e["status"] for e in got], [200, 401, 500])

    def test_limit_keeps_the_newest(self):
        for status in (200, 401, 500):
            self.log.record("request", status=status)
        self.assertEqual([e["status"] for e in self.log.read(limit=2)], [401, 500])

    def test_errors_only(self):
        self.log.record("request", status=200)
        self.log.record("auth_failed", reason="bad_token")
        self.log.record("tool_call", tool="brain_search", outcome="error")
        events = [e["event"] for e in self.log.read(errors_only=True)]
        self.assertEqual(events, ["auth_failed", "tool_call"])

    def test_since_filters_by_date(self):
        self.log.record("request", status=200)
        self.clock.value = "2026-08-02T09:00:00Z"
        self.log.record("request", status=201)
        got = self.log.read(since="2026-08-01")
        self.assertEqual([e["status"] for e in got], [201])

    def test_reads_the_rolled_generation_too(self):
        """An error that rolled off the live file is still the error somebody
        is looking for — until the SECOND rollover drops it, which is the
        bound this log deliberately has."""
        log = eventlog.EventLog(self.path, clock=self.clock, max_bytes=300)
        log.record("auth_failed", reason="bad_token")
        while not Path(str(self.path) + ".1").exists():
            log.record("request", method="POST", path_class="mcp", status=200)
        events = [e["event"] for e in log.read(limit=1000)]
        self.assertIn("auth_failed", events)
        self.assertEqual(events[0], "auth_failed",
                         "the rolled generation must be read BEFORE the live one")

    def test_a_missing_file_reads_as_empty(self):
        self.assertEqual(eventlog.EventLog(self.path).read(), [])

    def test_a_corrupt_line_is_skipped_not_fatal(self):
        self.log.record("request", status=200)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self.log.record("request", status=201)
        self.assertEqual([e["status"] for e in self.log.read()], [200, 201])


class LogsCommandTests(unittest.TestCase):
    """`brain logs` — handoff question 8, the interface that decides the rest."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.env = dict(os.environ, XDG_STATE_HOME=str(self.state))

    def run_brain(self, *args):
        import subprocess
        return subprocess.run([sys.executable, str(ROOT / "bin" / "brain")] + list(args),
                              capture_output=True, text=True, cwd=str(ROOT), env=self.env)

    def seed(self):
        where = osbackend.state_dir(ROOT, env=self.env)
        log = eventlog.EventLog(where / eventlog.FILENAME, clock=FakeClock())
        log.record("request", method="POST", path_class="mcp", status=200)
        log.record("auth_failed", reason="bad_token")
        return log

    def test_path_prints_the_location_without_reading_it(self):
        done = self.run_brain("logs", "--path")
        self.assertEqual(done.returncode, 0)
        self.assertIn(str(self.state), done.stdout)

    def test_renders_recent_entries(self):
        self.seed()
        done = self.run_brain("logs")
        self.assertEqual(done.returncode, 0)
        self.assertIn("request", done.stdout)
        self.assertIn("auth_failed", done.stdout)

    def test_errors_filters(self):
        self.seed()
        done = self.run_brain("logs", "--errors")
        self.assertIn("auth_failed", done.stdout)
        self.assertNotIn("path_class", done.stdout)

    def test_json_emits_the_raw_lines(self):
        self.seed()
        done = self.run_brain("logs", "--json")
        parsed = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
        self.assertEqual([e["event"] for e in parsed], ["request", "auth_failed"])

    def test_an_empty_log_says_so_rather_than_printing_nothing(self):
        done = self.run_brain("logs")
        self.assertEqual(done.returncode, 0)
        self.assertTrue(done.stdout.strip())


if __name__ == "__main__":
    unittest.main()
