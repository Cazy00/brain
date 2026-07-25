"""Tests for the setup surface: bootstraps, phases, picker.

Anything that WRITES runs in a sandbox with HOME redirected. These tests must
never touch the developer's real brain, real HOME, or real keychain.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import picker  # noqa: E402
from brainlib import setup as setupmod  # noqa: E402


class _FakeTTY(io.StringIO):
    """A StringIO that claims to be a real terminal, so choose() takes the
    interactive branch instead of returning the default immediately. There is
    no other way to drive that branch under test — a real terminal cannot be
    scripted, and the test runner's own stdin is never a TTY either."""
    def isatty(self):
        return True


class _RaisingIsATty(io.StringIO):
    """isatty() that raises instead of returning False — what a CLOSED file
    object actually does (ValueError: I/O operation on closed file). choose()
    must treat this the same as a plain False, not let the exception escape."""
    def isatty(self):
        raise ValueError("I/O operation on closed file")


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
        reason = picker.reject_reason(target)
        self.assertIn("is a file, not a directory", reason,
                      "the rejection must say WHY, not just that it failed")


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

    def test_a_raising_isatty_is_treated_as_not_a_tty(self):
        # A closed file's isatty() raises rather than returning False. Letting
        # that escape choose() would defeat its one job — never block without
        # a confirmed terminal — by crashing instead of falling back to the
        # default. Failing fast beats hanging, but it is still not the
        # graceful fallback this function's docstring promises.
        chosen = picker.choose(Path("/home/x"), Path("/work"),
                               stream=_RaisingIsATty(""), default=Path("/home/x/brain"))
        self.assertEqual(chosen, Path("/home/x/brain"))


class TestChooseInteractive(unittest.TestCase):
    """Drives the TTY branch of choose() with a scripted fake terminal.

    Menu rendering, digit selection, the type-a-path sub-prompt and
    reject-then-retry all live inside that branch, and it has zero coverage
    otherwise — a real terminal cannot be scripted, so _FakeTTY (isatty() ==
    True over a StringIO of scripted lines) is the only way to reach it.
    Options still come from candidates() against a temp directory, and
    nothing here reads or writes the real HOME.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cwd = self.home / "cwd"

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_valid_digit_selects_that_candidate(self):
        options = picker.candidates(self.home, self.cwd)
        chosen = picker.choose(self.home, self.cwd, stream=_FakeTTY("1\n"))
        self.assertEqual(chosen, options[0][0])

    def test_an_out_of_range_digit_is_rejected_not_treated_as_a_path(self):
        # Before the fix, the bare `else: chosen = expand(raw, home, cwd)`
        # meant a fat-fingered "99" silently became the literal path
        # `<cwd>/99` and was accepted outright, because reject_reason() lets
        # through any path that simply doesn't exist yet. An out-of-range
        # digit must instead be refused and the menu re-shown — so it takes a
        # SECOND, valid line to produce a result at all.
        options = picker.candidates(self.home, self.cwd)
        chosen = picker.choose(self.home, self.cwd, stream=_FakeTTY("99\n1\n"))
        self.assertEqual(chosen, options[0][0])
        self.assertNotEqual(chosen, self.cwd / "99")

    def test_the_type_a_path_option_accepts_a_typed_path(self):
        options = picker.candidates(self.home, self.cwd)
        type_a_path = len(options) + 1
        target = self.home / "elsewhere"
        chosen = picker.choose(self.home, self.cwd,
                               stream=_FakeTTY(f"{type_a_path}\n{target}\n"))
        self.assertEqual(chosen, target)

    def test_a_rejected_path_reprompts_and_a_valid_path_then_succeeds(self):
        busy = self.home / "busy"
        busy.mkdir()
        (busy / "f1.txt").write_text("x", encoding="utf-8")
        options = picker.candidates(self.home, self.cwd)
        type_a_path = len(options) + 1
        good = self.home / "elsewhere"
        stream = _FakeTTY(f"{type_a_path}\n{busy}\n{type_a_path}\n{good}\n")
        chosen = picker.choose(self.home, self.cwd, stream=stream)
        self.assertEqual(chosen, good)


class TestResult(unittest.TestCase):
    def test_status_must_be_one_of_three(self):
        with self.assertRaises(ValueError):
            setupmod.Result("weird", "detail")

    def test_a_failure_without_a_remedy_is_a_bug(self):
        # An agent acts on `remedy`. A failure it cannot act on is a dead end.
        with self.assertRaises(ValueError):
            setupmod.Result("failed", "something went wrong")

    def test_ok_needs_no_remedy(self):
        self.assertEqual(setupmod.Result("ok", "done").remedy, "")


class TestJsonContract(unittest.TestCase):
    def test_every_phase_appears_with_the_agreed_keys(self):
        results = {name: setupmod.Result("ok", f"{name} done")
                   for name in setupmod.PHASES}
        payload = json.loads(setupmod.render_json(results))
        self.assertEqual(list(payload["phases"]), list(setupmod.PHASES))
        for phase in setupmod.PHASES:
            self.assertEqual({"status", "detail", "remedy"},
                             set(payload["phases"][phase]))

    def test_overall_is_failed_when_any_phase_failed(self):
        results = {name: setupmod.Result("ok", "fine") for name in setupmod.PHASES}
        results["backup"] = setupmod.Result("failed", "no remote", remedy="git push")
        self.assertEqual(json.loads(setupmod.render_json(results))["status"], "failed")

    def test_overall_is_ok_when_phases_are_merely_skipped(self):
        results = {name: setupmod.Result("ok", "fine") for name in setupmod.PHASES}
        results["backup"] = setupmod.Result("skipped", "no remote wanted")
        self.assertEqual(json.loads(setupmod.render_json(results))["status"], "ok")

    def test_output_is_valid_json_even_with_quotes_in_details(self):
        results = {name: setupmod.Result("ok", 'he said "hi"\nand left')
                   for name in setupmod.PHASES}
        json.loads(setupmod.render_json(results))


if __name__ == "__main__":
    unittest.main()
