"""Tests for the setup surface: bootstraps, phases, picker.

Anything that WRITES runs in a sandbox with HOME redirected. These tests must
never touch the developer's real brain, real HOME, or real keychain.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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


if __name__ == "__main__":
    unittest.main()
