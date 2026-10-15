"""2.2: a served data folder says so, and consolidation can ask."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ENGINE / "bin"))
from brainlib import served  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


class ServedMarkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        (self.data / "knowledge").mkdir(parents=True)
        git(self.data, "init", "-q", "-b", "main")

    def test_an_unserved_folder_is_not_served(self):
        self.assertIsNone(served.check(self.data, env={}))

    def test_a_marked_folder_is_served_and_names_its_server(self):
        self.assertTrue(served.mark(self.data, "brain-http"))
        reason = served.check(self.data, env={})
        self.assertIsNotNone(reason)
        self.assertIn("brain-http", reason)

    def test_the_marker_lives_in_the_git_directory_never_the_tree(self):
        """A marker in the working tree would be committed by the next capture and
        travel to every clone, which would then all refuse to consolidate."""
        served.mark(self.data, "brain serve")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=self.data, capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "", "the marker landed in the working tree")
        path = served.marker_path(self.data)
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text())["server"], "brain serve")

    def test_a_clone_of_a_served_folder_is_not_served(self):
        """The curator's own clone is the whole point: it must come out clean."""
        (self.data / "knowledge" / "index.md").write_text("# index\n")
        git(self.data, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
        git(self.data, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        served.mark(self.data, "brain-http")
        clone = Path(self.tmp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.data), str(clone)], check=True,
                       capture_output=True)
        self.assertIsNone(served.check(clone, env={}))

    def test_the_environment_marks_a_folder_served(self):
        self.assertIn("BRAIN_SERVED", served.check(self.data, env={"BRAIN_SERVED": "1"}))

    def test_a_folder_that_is_not_a_repository_is_not_marked(self):
        bare_dir = Path(self.tmp.name) / "plain"
        bare_dir.mkdir()
        self.assertFalse(served.mark(bare_dir, "brain serve"))
        self.assertIsNone(served.check(bare_dir, env={}))


if __name__ == "__main__":
    unittest.main()
