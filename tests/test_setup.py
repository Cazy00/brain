"""Tests for the setup surface: bootstraps, phases, picker.

Anything that WRITES runs in a sandbox with HOME redirected. These tests must
never touch the developer's real brain, real HOME, or real keychain.
"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import picker  # noqa: E402


class TestLineEndingPolicy(unittest.TestCase):
    """A CRLF checkout corrupts a `#!/bin/sh` shebang. Git for Windows ships
    bash, so the hooks themselves are fine — only the line endings are fatal,
    and the failure is silent: the hook does not run and the commit gate is
    simply down."""

    def setUp(self):
        path = ROOT / ".gitattributes"
        self.assertTrue(path.exists(), ".gitattributes is missing")
        self.text = path.read_text(encoding="utf-8")

    def test_hooks_are_pinned_to_lf(self):
        self.assertIn(".githooks/* text eol=lf", self.text)

    def test_toolbelt_is_pinned_to_lf(self):
        self.assertIn("bin/* text eol=lf", self.text)

    def test_the_windows_shim_is_pinned_to_crlf(self):
        # A .cmd file with LF endings misparses in cmd.exe.
        self.assertIn("*.cmd text eol=crlf", self.text)


class TestWindowsShim(unittest.TestCase):
    def test_shim_exists_and_forwards_every_argument(self):
        path = ROOT / "brain.cmd"
        self.assertTrue(path.exists(), "brain.cmd is missing")
        text = path.read_text(encoding="utf-8")
        self.assertIn("%*", text, "the shim must forward all arguments")
        self.assertIn("bin\\brain", text)


class TestCandidates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_home_brain_is_first_and_recommended(self):
        found = picker.candidates(self.home, self.home / "cwd")
        self.assertEqual(found[0][0], self.home / "brain")
        self.assertIn("recommended", found[0][1].lower())

    def test_cloud_folders_appear_only_when_they_exist(self):
        plain = [str(p) for p, _ in picker.candidates(self.home, self.home / "cwd")]
        self.assertFalse([p for p in plain if "Dropbox" in p])

        (self.home / "Dropbox").mkdir()
        withcloud = picker.candidates(self.home, self.home / "cwd")
        hits = [label for path, label in withcloud if "Dropbox" in str(path)]
        self.assertEqual(len(hits), 1)
        # A cloud choice has a consequence; the label must state it.
        self.assertTrue(hits[0].strip())


class TestRejectReason(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_path_that_does_not_exist_is_fine(self):
        self.assertEqual(picker.reject_reason(self.base / "new"), "")

    def test_an_empty_directory_is_fine(self):
        (self.base / "empty").mkdir()
        self.assertEqual(picker.reject_reason(self.base / "empty"), "")

    def test_a_non_empty_directory_is_rejected_and_says_how_many(self):
        busy = self.base / "busy"
        busy.mkdir()
        for i in range(4):
            (busy / f"f{i}.txt").write_text("x", encoding="utf-8")
        reason = picker.reject_reason(busy)
        self.assertIn("4", reason, "the rejection must say WHY, with the count")

    def test_a_file_is_rejected(self):
        target = self.base / "afile"
        target.write_text("x", encoding="utf-8")
        self.assertTrue(picker.reject_reason(target))


class TestExpand(unittest.TestCase):
    def test_tilde_expands(self):
        home = Path("/home/someone")
        self.assertEqual(picker.expand("~/brain", home, Path("/tmp")),
                         home / "brain")

    def test_bare_tilde_is_home(self):
        home = Path("/home/someone")
        self.assertEqual(picker.expand("~", home, Path("/tmp")), home)

    def test_relative_paths_resolve_against_cwd(self):
        self.assertEqual(picker.expand("sub/dir", Path("/home/x"), Path("/work")),
                         Path("/work/sub/dir"))


class TestChooseWithoutATty(unittest.TestCase):
    def test_no_tty_takes_the_default_and_never_blocks(self):
        # Piped into `sh`, stdin is the SCRIPT, not the user. Blocking here is
        # the failure mode this guards against.
        chosen = picker.choose(Path("/home/x"), Path("/work"),
                               stream=io.StringIO(""), default=Path("/home/x/brain"))
        self.assertEqual(chosen, Path("/home/x/brain"))


if __name__ == "__main__":
    unittest.main()
