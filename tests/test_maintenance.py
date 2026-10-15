"""Audit A2: fail-closed maintenance mode, checked on every write."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE / "bin"))
from brainlib import maintenance  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class MaintenanceCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        self.state = Path(self.tmp.name) / "state"
        (self.data / "knowledge" / "inbox").mkdir(parents=True)
        self.state.mkdir()
        git(self.data, "init", "-q", "-b", "main")
        for key, value in (("user.email", "t@example.com"), ("user.name", "T")):
            git(self.data, "config", key, value)
        (self.data / "knowledge" / "index.md").write_text("# index\n")
        git(self.data, "add", "-A")
        git(self.data, "commit", "-q", "-m", "base")
        db = sqlite3.connect(str(self.state / "index.db"))
        db.execute("create table notes (id text)")
        db.commit()
        db.close()

    def check(self):
        return maintenance.check(self.data, self.state)

    def test_a_healthy_brain_is_not_in_maintenance(self):
        self.assertIsNone(self.check())

    def test_a_dirty_tree_outside_the_inbox_engages_it(self):
        (self.data / "knowledge" / "index.md").write_text("# edited by hand\n")
        self.assertEqual(self.check(), "dirty tree")

    def test_an_uncommitted_inbox_note_does_not(self):
        (self.data / "knowledge" / "inbox" / "a.md").write_text("x\n")
        self.assertIsNone(self.check())

    def test_a_branch_other_than_main_engages_it(self):
        git(self.data, "checkout", "-q", "-b", "consolidate/2026-10-15")
        self.assertEqual(self.check(), "not on main")

    def test_a_broken_index_engages_it(self):
        (self.state / "index.db").write_bytes(b"not a database at all, just bytes" * 10)
        self.assertEqual(self.check(), "index unreadable")

    def test_an_unwritable_data_volume_engages_it(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anything")
        os.chmod(self.data, 0o555)
        self.addCleanup(os.chmod, self.data, 0o755)
        self.assertEqual(self.check(), "data not writable")

    def test_it_clears_by_itself_when_the_condition_does(self):
        git(self.data, "checkout", "-q", "-b", "consolidate/x")
        self.assertIsNotNone(self.check())
        git(self.data, "checkout", "-q", "main")
        self.assertIsNone(self.check())


if __name__ == "__main__":
    unittest.main()
