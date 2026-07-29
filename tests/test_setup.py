"""Tests for the setup surface: bootstraps, phases, picker.

Anything that WRITES runs in a sandbox with HOME redirected. These tests must
never touch the developer's real brain, real HOME, or real keychain.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import osbackend  # noqa: E402
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

    def test_a_whitespace_only_remedy_is_still_no_remedy(self):
        # An agent cannot run whitespace. "   " must be rejected exactly like
        # "" — otherwise the guard above is trivially defeated.
        with self.assertRaises(ValueError):
            setupmod.Result("failed", "something broke", remedy="   ")

    def test_a_non_string_detail_is_coerced_for_the_contract(self):
        # A phase that reports failure via Result("failed", exc, ...) and
        # forgets str(exc) must still produce a readable contract — the
        # reporting boundary (as_dict/render_json) is the one place this
        # module cannot afford to raise.
        result = setupmod.Result("ok", ValueError("oops"))
        self.assertEqual(result.as_dict()["detail"], "oops")


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

    def test_an_unrecognized_key_raises(self):
        # A key outside PHASES is a typo or a forgotten PHASES entry in OUR
        # code. Silently dropping it from "phases" while still letting it
        # flip the top-level status to failed is worse than a loud crash: it
        # reports a failure the agent can never find and never fix.
        results = {"typo_phase": setupmod.Result("failed", "oops", remedy="fix it")}
        with self.assertRaises(ValueError):
            setupmod.overall_status(results)
        with self.assertRaises(ValueError):
            setupmod.render_json(results)

    def test_a_partial_results_dict_reports_correctly(self):
        # `brain setup --only check` (Task 11) runs exactly one phase and
        # produces a results dict with exactly one entry. Fewer keys than
        # PHASES is legitimate and must keep reporting normally — only an
        # EXTRA, unrecognized key is a bug.
        results = {"check": setupmod.Result("ok", "check done")}
        payload = json.loads(setupmod.render_json(results))
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(list(payload["phases"]), ["check"])

    def test_an_empty_results_dict_is_ok_with_no_phases(self):
        # Nothing having run yet is not the same as something having broken.
        payload = json.loads(setupmod.render_json({}))
        self.assertEqual(payload, {"status": "ok", "phases": {}})


class TestCheckPhase(unittest.TestCase):
    def test_missing_hard_dependency_fails_with_a_remedy(self):
        result = setupmod.phase_check(which=lambda tool: None)
        self.assertEqual(result.status, "failed")
        self.assertIn("git", result.detail)
        self.assertTrue(result.remedy)

    def test_missing_optional_dependency_does_not_fail_the_phase(self):
        # Everything present except gh.
        result = setupmod.phase_check(which=lambda tool: None if tool == "gh" else "/x")
        self.assertEqual(result.status, "ok")

    def test_optional_dependency_reports_the_consequence_not_the_name(self):
        result = setupmod.phase_check(which=lambda tool: None if tool == "age" else "/x")
        self.assertIn("vault", result.detail.lower())

    def test_nothing_is_ever_installed(self):
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("check must never execute a package manager")

        result = setupmod.phase_check(which=lambda tool: None, run=fake_run)
        self.assertEqual(calls, [])
        self.assertEqual(result.status, "failed")

    def test_remedy_survives_when_no_package_manager_is_present(self):
        # On every machine that actually has brew/apt/dnf/pacman/winget, the
        # `... or f"install: {...}"` fallback at the tail of the missing_hard
        # branch is dead code as far as any test can tell — install_hint()
        # always returns a real command first. The one machine class where it
        # is NOT dead code is exactly the one named in phase_check's own
        # docstring (corporate machines that forbid installers), and that is
        # also the machine class most likely to be missing prerequisites in
        # the first place. Forcing package_manager() to "" here — rather than
        # trusting whatever happens to be on the machine running this suite —
        # is what makes this deterministic everywhere, the same reasoning
        # TestLinkDirNeverRaises in test_osbackend.py already applies to
        # platform-dependent failures.
        with mock.patch.object(osbackend, "package_manager", return_value=""):
            result = setupmod.phase_check(which=lambda tool: None)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.remedy.strip())

    def test_remedy_never_drops_a_hard_tool_that_lacks_an_install_hint(self):
        # Real PREREQS has exactly one non-skipped hard tool (git), so this
        # substitutes two fake ones to reach the "some tools have a hint, some
        # don't" case: remedy must still name EVERY missing hard tool, not
        # just the ones install_hint() could resolve, because remedy — not
        # detail — is the field an agent acts on (Result's own docstring).
        # install_hint() is mocked directly, not just PREREQS, so the result
        # does not depend on which package manager, if any, is actually
        # installed on the machine running this suite.
        fake_prereqs = {
            "hashint": {"hard": True, "why": "test-only tool", "pkg": {}},
            "nohint": {"hard": True, "why": "test-only tool", "pkg": {}},
        }

        def fake_hint(tool):
            return "brew install hashint" if tool == "hashint" else ""

        with mock.patch.object(osbackend, "PREREQS", fake_prereqs), \
             mock.patch.object(osbackend, "install_hint", side_effect=fake_hint):
            result = setupmod.phase_check(which=lambda tool: None)

        self.assertIn("hashint", result.remedy)
        self.assertIn("nohint", result.remedy)


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)


def _make_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "f.txt").write_text("x", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "first")
    return path


class TestBackupPhase(unittest.TestCase):
    """The 2026-07-25 regression, pinned.

    `gh repo create --source . --remote origin --push` ADDS the remote and then
    pushes. A failure at the push step leaves the remote in place, so a
    non-zero exit says nothing about whether a remote exists. install.sh read
    the exit code, reported 'no remote yet — LOCAL ONLY', and then its own
    visibility check read git and printed the opposite. The install finished
    with doctor RED.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.repo = _make_repo(self.base / "brain")
        self.origin = self.base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)],
                       capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_remote_added_but_push_failed_is_recovered_not_reported_as_absent(self):
        # Exactly the half-finished state gh leaves behind.
        _git(self.repo, "remote", "add", "origin", str(self.origin))

        def fake_gh(argv, **kwargs):
            # gh already ran and failed; it must not run again.
            raise AssertionError("must not re-run gh when a remote already exists")

        result = setupmod.phase_backup(self.repo, "my-brain", want_remote=True,
                                       run=fake_gh, which=lambda t: "/usr/bin/gh")
        self.assertEqual(result.status, "ok", result.detail)
        upstream = _git(self.repo, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{u}").stdout.strip()
        self.assertEqual(upstream, "origin/main",
                         "the phase must push -u so doctor is not left red")

    def test_no_remote_is_a_skip_not_a_failure_and_says_how_to_fix_it(self):
        result = setupmod.phase_backup(self.repo, "my-brain", want_remote=False,
                                       run=lambda *a, **k: None,
                                       which=lambda t: None)
        self.assertEqual(result.status, "skipped")
        self.assertIn("gh repo create", result.remedy)

    def test_a_remote_that_cannot_be_pushed_to_fails_loudly(self):
        _git(self.repo, "remote", "add", "origin",
             str(self.base / "does-not-exist.git"))
        result = setupmod.phase_backup(self.repo, "my-brain", want_remote=True,
                                       run=lambda *a, **k: None,
                                       which=lambda t: None)
        self.assertEqual(result.status, "failed")
        self.assertTrue(result.remedy)

    def test_an_already_tracking_repo_is_left_alone(self):
        _git(self.repo, "remote", "add", "origin", str(self.origin))
        _git(self.repo, "push", "-u", "-q", "origin", "main")
        result = setupmod.phase_backup(self.repo, "my-brain", want_remote=True,
                                       run=lambda *a, **k: None,
                                       which=lambda t: None)
        self.assertEqual(result.status, "ok")


class TestPlacePhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_an_explicit_path_skips_the_prompt(self):
        result, path = setupmod.phase_place(self.home, self.home,
                                            requested=str(self.home / "chosen"))
        self.assertEqual(result.status, "ok")
        self.assertEqual(path, self.home / "chosen")

    def test_an_unusable_explicit_path_fails_with_the_reason(self):
        busy = self.home / "busy"
        busy.mkdir()
        (busy / "a.txt").write_text("x", encoding="utf-8")
        result, path = setupmod.phase_place(self.home, self.home,
                                            requested=str(busy))
        self.assertEqual(result.status, "failed")
        self.assertIn("1", result.detail)

    def test_without_a_tty_the_recommended_default_is_taken(self):
        # Piped into `sh`, or run by an agent, there is no terminal to ask. The
        # phase must still produce a destination rather than blocking — the
        # same guarantee picker.choose() makes, asserted at the phase boundary
        # because that is where run_setup() consumes it.
        result, path = setupmod.phase_place(self.home, self.home,
                                            stream=io.StringIO(""))
        self.assertEqual(result.status, "ok")
        self.assertEqual(path, self.home / "brain")


class TestCreatePhase(unittest.TestCase):
    """phase_create runs `git commit` itself, so the identity it commits under
    has to come from somewhere.

    Left to the machine's own config, these tests pass on a developer's laptop
    and fail on every CI runner, which configures no user.name — and on a
    laptop with commit.gpgsign on they fail there too, waiting for a passphrase
    nobody is at the keyboard to type. An empty global config plus an identity
    in the environment fixes both, and keeps the promise at the top of this
    file: nothing here reads or writes the developer's real state.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "template"
        (self.source / "knowledge").mkdir(parents=True)
        (self.source / "knowledge" / "index.md").write_text("# i", encoding="utf-8")
        (self.source / ".githooks").mkdir()
        (self.source / ".githooks" / "pre-commit").write_text("#!/bin/sh\n",
                                                              encoding="utf-8")
        empty_config = self.base / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        patched = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_SYSTEM": str(empty_config),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        patched.start()
        self.addCleanup(patched.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_a_repo_with_one_commit_on_main(self):
        dest = self.base / "brain"
        result = setupmod.phase_create(self.source, dest)
        self.assertEqual(result.status, "ok", result.detail)
        self.assertEqual(_git(dest, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
                         "main")
        self.assertEqual(_git(dest, "rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_hooks_are_pointed_at_the_repos_own_directory(self):
        dest = self.base / "brain"
        setupmod.phase_create(self.source, dest)
        self.assertEqual(
            _git(dest, "config", "core.hooksPath").stdout.strip(), ".githooks")

    def test_the_template_history_does_not_come_along(self):
        # A brain's history is ITS OWN. Inheriting the product's history is
        # what `gh repo create --template` avoids too.
        dest = self.base / "brain"
        setupmod.phase_create(self.source, dest)
        subject = _git(dest, "log", "-1", "--format=%s").stdout.strip()
        self.assertEqual(subject, "brain: start")

    def test_the_bootstraps_are_not_copied_into_the_brain(self):
        # install.sh installs a brain; it is not part of one. It also carries
        # the template repo's own name, so a copy left behind would reinstall
        # the PRODUCT over someone's notes if it were ever run from there.
        (self.source / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.source / ".claude-plugin").mkdir()
        dest = self.base / "brain"
        setupmod.phase_create(self.source, dest)
        self.assertFalse((dest / "install.sh").exists())
        self.assertFalse((dest / ".claude-plugin").exists())

    def test_a_git_with_no_identity_fails_with_the_command_that_fixes_it(self):
        # The one failure mode a first run actually hits: git installed, never
        # configured. It must name both settings, because `git commit` names
        # neither in a way an agent can act on.
        dest = self.base / "brain"
        with mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "",
                                          "GIT_AUTHOR_EMAIL": "",
                                          "GIT_COMMITTER_NAME": "",
                                          "GIT_COMMITTER_EMAIL": ""}):
            result = setupmod.phase_create(self.source, dest)
        self.assertEqual(result.status, "failed")
        self.assertIn("user.name", result.remedy)
        self.assertIn("user.email", result.remedy)


def _done(code, out="", err=""):
    """What subprocess.run() hands phase_verify back, without running anything."""
    return subprocess.CompletedProcess([], code, out, err)


class TestVerifyPhase(unittest.TestCase):
    """doctor's verdict IS setup's verdict, so this phase may not soften it.

    `run` is injected rather than executing the real doctor: doctor reads the
    git remote, the inbox and the note birthdays of whatever repo it is pointed
    at, and a unit test that depends on all of those is a test of doctor, not
    of this phase.
    """

    def test_a_green_doctor_passes(self):
        result = setupmod.phase_verify(Path("/nowhere"),
                                       run=lambda argv: _done(0, "  [ok ] all good"))
        self.assertEqual(result.status, "ok")

    def test_a_nonzero_exit_fails_and_carries_doctors_own_words(self):
        result = setupmod.phase_verify(
            Path("/nowhere"),
            run=lambda argv: _done(1, "  [RED] no git remote — not backed up"))
        self.assertEqual(result.status, "failed")
        self.assertIn("no git remote", result.detail)
        self.assertIn("doctor", result.remedy)

    def test_a_red_line_fails_even_when_the_exit_code_says_otherwise(self):
        # The 2026-07-25 regression in its general form: an exit code is a
        # summary somebody has to remember to keep true, and doctor's RED lines
        # are the thing actually being asserted about. Read both, trust the
        # worse one.
        result = setupmod.phase_verify(
            Path("/nowhere"),
            run=lambda argv: _done(0, "  [RED] YOUR BRAIN IS PUBLIC"))
        self.assertEqual(result.status, "failed")

    def test_doctors_stderr_is_read_too(self):
        # A traceback from a broken doctor lands on stderr and leaves stdout
        # empty. Reading only stdout would call that install healthy.
        result = setupmod.phase_verify(
            Path("/nowhere"),
            run=lambda argv: _done(1, "", "Traceback (most recent call last):"))
        self.assertEqual(result.status, "failed")
        self.assertIn("Traceback", result.detail)


BRAIN = ROOT / "bin" / "brain"


def run_brain_cmd(*args, cwd=None, env=None):
    environ = dict(os.environ)
    environ.update(env or {})
    return subprocess.run([sys.executable, str(BRAIN), *args],
                          cwd=str(cwd or ROOT), capture_output=True,
                          text=True, timeout=180, env=environ)


class TestSetupCli(unittest.TestCase):
    def test_setup_appears_in_help(self):
        done = run_brain_cmd("--help")
        self.assertIn("setup", done.stdout)

    def test_setup_help_does_not_perform_setup(self):
        # `init --help` once wired the machine for someone who only asked what
        # it does. That must never recur for setup.
        done = run_brain_cmd("setup", "--help")
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("installing to", done.stdout)

    def test_setup_help_shows_setups_own_usage(self):
        """And not the toolbelt's global help, which is what it used to show.

        main() answers `<command> --help` itself so that asking what a command
        does can never do it. setup and serve are named exceptions because both
        answer --help before touching anything — which is why the test above
        sits right here, holding the property that earned the exception.
        """
        done = run_brain_cmd("setup", "--help")
        self.assertIn("brain setup — install a brain here", done.stdout)
        self.assertIn("--only", done.stdout)

    def test_json_mode_emits_only_json_on_stdout(self):
        done = run_brain_cmd("setup", "--json", "--yes", "--only", "check")
        json.loads(done.stdout)      # must parse — human text belongs on stderr

    def test_check_only_never_writes_anything(self):
        before = sorted(p.name for p in ROOT.iterdir())
        run_brain_cmd("setup", "--yes", "--only", "check")
        self.assertEqual(before, sorted(p.name for p in ROOT.iterdir()))

    def test_an_unknown_phase_names_the_ones_that_exist(self):
        # A typo must not silently run nothing and report success — that reads,
        # to an agent, as a completed install.
        done = run_brain_cmd("setup", "--only", "instal", "--yes")
        self.assertEqual(done.returncode, 2)
        self.assertIn("place", done.stderr)

    def test_a_later_phase_on_its_own_demands_a_destination(self):
        # Re-running one phase is a legitimate repair, but create/backup/verify
        # all need to know WHICH brain. Asking beats crashing on the None that
        # the place phase would have filled in.
        done = run_brain_cmd("setup", "--only", "verify", "--yes")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--dir", done.stderr)

    def test_the_help_points_a_first_run_at_setup(self):
        # AGENTS.md and CLAUDE.md both document "re-run init" as the repair
        # when a machine's wiring drifts, so init keeps working (test_brain.py
        # covers that it still wires). What must change is where somebody
        # installing for the FIRST time is sent.
        done = run_brain_cmd("init", "--help")
        self.assertEqual(done.returncode, 0)
        self.assertIn("brain setup", done.stdout)


class TestSetupIsNeverInteractiveWhenTold(unittest.TestCase):
    """--yes and --json both mean "do not ask".

    Both branches are driven at a fake terminal, because without one the test
    runner's stdin is never a TTY and the picker takes the default regardless —
    which would make this pass whether or not --yes does anything at all.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_yes_leaves_the_terminal_unread(self):
        terminal = _FakeTTY("1\n")
        with mock.patch.object(sys, "stdin", terminal):
            code = setupmod.run_setup(["--only", "place", "--yes"],
                                      home=self.home, cwd=self.home)
        self.assertEqual(code, 0)
        self.assertEqual(terminal.read(), "1\n",
                         "--yes prompted anyway and ate the answer")

    def test_json_leaves_the_terminal_unread(self):
        # An agent parsing stdout has no way to answer a prompt, so --json must
        # imply --yes rather than deadlocking against a terminal it cannot see.
        terminal = _FakeTTY("1\n")
        with mock.patch.object(sys, "stdin", terminal):
            setupmod.run_setup(["--only", "place", "--json"],
                               home=self.home, cwd=self.home)
        self.assertEqual(terminal.read(), "1\n", "--json prompted")

    def test_without_either_flag_a_terminal_is_asked(self):
        # The control: the same call WITHOUT --yes must consume the answer.
        # Otherwise the two tests above prove nothing.
        terminal = _FakeTTY("1\n")
        with mock.patch.object(sys, "stdin", terminal):
            setupmod.run_setup(["--only", "place"], home=self.home, cwd=self.home)
        self.assertEqual(terminal.read(), "",
                         "the picker never asked at a real terminal")


class TestSetupEndToEnd(unittest.TestCase):
    """Every phase in one run, against a real copy of this repo.

    The only test that proves the phases compose: that the tree `create`
    produces is one `doctor` can actually run inside, and that setup's exit
    code is doctor's rather than its own opinion.

    `--no-repo` is not optional here. Without it phase_backup would run `gh
    repo create` against whatever GitHub account the developer is logged into,
    from a test suite.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        empty_config = self.base / "gitconfig"
        empty_config.write_text("", encoding="utf-8")
        patched = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_SYSTEM": str(empty_config),
            "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
        })
        patched.start()
        self.addCleanup(patched.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_local_only_install_builds_a_brain_and_calls_it_not_backed_up(self):
        """The install works; doctor still calls it red, and so does setup.

        `--no-repo` is a legitimate choice and the backup phase records it as a
        skip, not a failure — but a brain with no off-machine copy is exactly
        what doctor is built to call [RED], and this repo's own manual says a
        backup gap is "worth seeing, not an untidiness to hide". So the run
        ends non-zero with the reason attached.

        Worth revisiting in Plan 2: a user who explicitly asked for local-only
        is being told their install failed, when what is true is that it
        succeeded and they gave something up. The exit code is right; the word
        "failed" against a choice they made deliberately is the part that
        reads badly.
        """
        dest = self.base / "brain"
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            code = setupmod.run_setup(["--dir", str(dest), "--no-repo", "--yes",
                                       "--json"],
                                      home=self.home, cwd=self.base, source=ROOT)
        payload = json.loads(out.getvalue())
        phases = payload["phases"]

        self.assertEqual(phases["check"]["status"], "ok", phases["check"]["detail"])
        self.assertEqual(phases["create"]["status"], "ok", phases["create"]["detail"])
        self.assertEqual(phases["backup"]["status"], "skipped")
        self.assertEqual(phases["verify"]["status"], "failed")
        self.assertIn("no git remote", phases["verify"]["detail"])
        self.assertEqual(code, 1, "a doctor-red install must not exit 0")

        # ...and what it built is a real brain, not a half-copied directory.
        self.assertTrue((dest / "bin" / "brain").is_file())
        self.assertTrue((dest / "knowledge" / "index.md").is_file())
        self.assertEqual(_git(dest, "config", "core.hooksPath").stdout.strip(),
                         ".githooks")

    def test_a_deliberate_local_only_install_is_told_why_it_ends_red(self):
        """The exit code is right. The silence around it was not.

        --no-repo builds a working brain and then exits 1, because verify hands
        doctor's verdict straight out and doctor calls a brain with no
        off-machine copy [RED]. Neither half moves: an unbacked-up brain really
        is unhealthy, AGENTS.md calls a backup gap "worth seeing, not an
        untidiness to hide", and the scheduled watchdog only fires on a
        non-zero exit. What was missing is that the person who typed --no-repo
        was shown the word "failed" with nothing connecting it to the flag they
        chose.
        """
        dest = self.base / "brain"
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            code = setupmod.run_setup(["--dir", str(dest), "--no-repo", "--yes"],
                                      home=self.home, cwd=self.base, source=ROOT)
        self.assertEqual(code, 1)
        text = err.getvalue()
        self.assertIn("--no-repo", text, "the flag that caused this is not named")
        self.assertIn("gh repo create", text, "no command that fixes it")

    def test_a_red_nobody_asked_for_gets_no_such_reassurance(self):
        """The complement, and the reason the branch is guarded rather than
        printed on every failure: a red line the user did NOT choose is a
        surprise, and explaining it away as expected is the worst thing this
        output could do.

        Runs --only verify, which never reaches phase_backup — deliberate.
        Without --no-repo, phase_backup would run `gh repo create` against
        whatever GitHub account the developer is logged into, from a test.
        """
        dest = self.base / "brain"
        with mock.patch.object(sys, "stdout", io.StringIO()):
            setupmod.run_setup(["--dir", str(dest), "--no-repo", "--yes", "--json"],
                               home=self.home, cwd=self.base, source=ROOT)
        err = io.StringIO()
        with mock.patch.object(sys, "stderr", err):
            setupmod.run_setup(["--only", "verify", "--dir", str(dest)],
                               home=self.home, cwd=self.base, source=ROOT)
        self.assertNotIn("--no-repo", err.getvalue())

    def test_setup_wires_nothing_into_the_users_home(self):
        # The hazard this whole command inherits: `brain init` re-points the
        # global ~/.claude/skills/brain symlink at whatever checkout ran it, so
        # an install from a temp clone would hijack the /brain skill for every
        # session on the machine. setup does not wire — `brain connect` does,
        # deliberately, as a separate step you run from the brain itself.
        dest = self.base / "brain"
        with mock.patch.object(sys, "stdout", io.StringIO()):
            setupmod.run_setup(["--dir", str(dest), "--no-repo", "--yes", "--json"],
                               home=self.home, cwd=self.base, source=ROOT)
        self.assertFalse((self.home / ".claude").exists(),
                         "setup touched the user's global agent config")


class TestBootstraps(unittest.TestCase):
    def test_install_sh_is_a_bootstrap_not_a_second_implementation(self):
        text = (ROOT / "install.sh").read_text(encoding="utf-8")
        self.assertIn("bin/brain setup", text)
        # Everything interactive now lives in Python, where it is testable and
        # where it works on all three platforms.
        for gone in ("Repository name", "Install to", "gh repo create"):
            self.assertNotIn(gone, text,
                             f"{gone!r} belongs in brainlib/setup.py now")

    def test_install_sh_stayed_small(self):
        lines = (ROOT / "install.sh").read_text(encoding="utf-8").splitlines()
        self.assertLess(len(lines), 120, "the bootstrap has grown a second brain")

    def test_install_ps1_exists_and_hands_off_the_same_way(self):
        text = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn("bin\\brain", text.replace("/", "\\"))
        self.assertIn("setup", text)

    def test_neither_bootstrap_wires_the_machine_from_a_temp_clone(self):
        # `brain init` re-points the global ~/.claude/skills/brain link at
        # whatever checkout ran it. A bootstrap runs from a clone it is about
        # to delete, so calling init there would leave every Claude session on
        # the machine pointing the /brain skill at a directory that no longer
        # exists. Wiring is `brain connect`, run later, from the brain itself.
        #
        # Comments are stripped first. Both files SAY "brain init" while
        # explaining why they never run it, and a check that cannot tell prose
        # from code would push that explanation out of the files — which is
        # where somebody reading the installer will actually look for it.
        for name in ("install.sh", "install.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8")
            code = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertNotIn("brain init", code.replace("\\", "/"),
                             f"{name} wires the machine from a throwaway clone")

    def test_both_bootstraps_check_the_python_floor(self):
        # 3.9 is the floor the whole toolbelt is written to. Finding out from a
        # SyntaxError halfway through setup is a worse first run than being
        # told before anything is cloned.
        for name in ("install.sh", "install.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn("(3,9)", text.replace(" ", ""),
                          f"{name} does not check the Python version")


class TestCiMatrix(unittest.TestCase):
    def test_all_three_platforms_run_the_tests(self):
        text = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, text,
                          "Windows support is CI-verified or it is not verified")

    def test_the_matrix_is_actually_wired_to_the_job(self):
        # Naming three runners in a `matrix:` block that no job reads is the
        # failure this catches: the file mentions Windows, and Windows is never
        # run. Nobody on this project has a Windows machine, so CI is the only
        # thing standing behind that support claim.
        text = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: ${{ matrix.os }}", text)


if __name__ == "__main__":
    unittest.main()
