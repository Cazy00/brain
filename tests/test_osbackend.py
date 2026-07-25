# tests/test_osbackend.py
"""Tests for the per-OS backends.

These must pass on all three platforms, so nothing here may execute a
platform tool. Where a backend shells out, the test asserts on the argv it
BUILDS. Executing `security` or `schtasks` for real would either fail on the
wrong OS or, worse, write to the developer's actual keychain.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from brainlib import osbackend  # noqa: E402


class TestOSFamily(unittest.TestCase):
    def test_returns_a_known_family(self):
        self.assertIn(osbackend.os_family(),
                      {"macos", "linux", "windows", "unknown"})

    def test_matches_this_interpreter(self):
        expected = {"darwin": "macos", "win32": "windows"}.get(
            sys.platform, "linux" if sys.platform.startswith("linux") else "unknown")
        self.assertEqual(osbackend.os_family(), expected)


class TestPrereqs(unittest.TestCase):
    def test_git_and_python_are_hard(self):
        self.assertTrue(osbackend.PREREQS["git"]["hard"])
        self.assertTrue(osbackend.PREREQS["python3"]["hard"])

    def test_optional_tools_are_soft(self):
        for tool in ("gh", "gitleaks", "age", "rg"):
            self.assertFalse(osbackend.PREREQS[tool]["hard"], tool)

    def test_every_entry_states_a_consequence_not_a_restatement(self):
        # The 'why' is what the user LOSES, so it must not merely name the tool.
        for tool, spec in osbackend.PREREQS.items():
            self.assertTrue(spec["why"], tool)
            self.assertNotEqual(spec["why"].strip().lower(), tool.lower())

    def test_every_entry_has_a_package_for_every_manager(self):
        managers = {"brew", "apt", "dnf", "pacman", "winget"}
        for tool, spec in osbackend.PREREQS.items():
            if tool == "python3":
                continue          # bootstrapped before this code can run
            self.assertEqual(managers, set(spec["pkg"]), tool)


class TestInstallHint(unittest.TestCase):
    def test_hint_names_the_package_when_a_manager_is_present(self):
        hint = osbackend.install_hint("gh")
        if osbackend.package_manager():
            self.assertIn(osbackend.package_manager(), hint)
        else:
            self.assertEqual(hint, "")

    def test_unknown_tool_gives_no_hint_rather_than_a_wrong_one(self):
        self.assertEqual(osbackend.install_hint("nosuchtool"), "")


if __name__ == "__main__":
    unittest.main()
