# tests/test_osbackend.py
"""Tests for the per-OS backends.

These must pass on all three platforms, so nothing here may execute a
platform tool. Where a backend shells out, the test asserts on the argv it
BUILDS. Executing `security` or `schtasks` for real would either fail on the
wrong OS or, worse, write to the developer's actual keychain.
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class TestSchedulerSelection(unittest.TestCase):
    def test_each_family_gets_its_own_backend(self):
        self.assertEqual(osbackend.scheduler_for("macos").kind, "launchd")
        self.assertEqual(osbackend.scheduler_for("linux").kind, "systemd")
        self.assertEqual(osbackend.scheduler_for("windows").kind, "schtasks")

    def test_unknown_family_gets_a_backend_that_says_it_cannot(self):
        backend = osbackend.scheduler_for("unknown")
        self.assertFalse(backend.available())
        # Unavailable must be a supported state, not an exception: schedules
        # are optional and a machine without one must still finish setup.
        self.assertIn("no scheduler", backend.install("doctor", ["x"],
                                                      {"hour": 9, "minute": 0}).lower())


class TestSchtasksArgv(unittest.TestCase):
    """Windows argv is built here and executed only on Windows, so this is the
    only place its correctness can be checked from any machine."""

    def test_weekly_schedule_names_the_day_and_time(self):
        argv = osbackend.SchtasksScheduler().install_argv(
            "brain-consolidate", ["python", "bin/brain", "consolidate"],
            {"weekday": 1, "hour": 9, "minute": 0})
        self.assertIn("/sc", argv)
        self.assertIn("WEEKLY", argv)
        self.assertIn("MON", argv)
        self.assertIn("09:00", argv)

    def test_daily_schedule_omits_the_day(self):
        argv = osbackend.SchtasksScheduler().install_argv(
            "brain-doctor", ["python", "bin/brain", "doctor"],
            {"hour": 7, "minute": 30})
        self.assertIn("DAILY", argv)
        self.assertNotIn("/d", argv)
        self.assertIn("07:30", argv)


class TestSystemdUnits(unittest.TestCase):
    def test_timer_unit_has_a_calendar_line(self):
        service, timer = osbackend.SystemdScheduler().render_units(
            "brain-doctor", ["python3", "/home/x/brain/bin/brain", "doctor"],
            {"hour": 7, "minute": 30})
        self.assertIn("OnCalendar=*-*-* 07:30:00", timer)
        self.assertIn("ExecStart=python3 /home/x/brain/bin/brain doctor", service)
        # Without this the timer never fires unless the user is logged in.
        self.assertIn("Persistent=true", timer)


class TestSchedulerServes(unittest.TestCase):
    """serves() is what doctor needs beyond plain install/uninstall/status: a
    schedule's own identifier (a launchd label, a systemd unit name, a Task
    Scheduler task name) is machine-global, so finding it installed proves a
    job exists, never that it is THIS repo's — cf. bin/brain's
    schedule_serves_this_repo(), which is the one caller of this method."""

    def test_base_scheduler_never_serves_anything(self):
        # Unavailable is a normal state, not a special case the caller must
        # remember to check first — serves() must be just as safe to call.
        self.assertFalse(osbackend.Scheduler().serves("doctor", "/anywhere"))

    def test_launchd_serves_only_when_the_plist_mentions_the_marker(self):
        backend = osbackend.LaunchdScheduler()
        with tempfile.TemporaryDirectory() as tmp:
            backend.agents = Path(tmp)
            self.assertFalse(backend.serves("doctor", "/repo/x"),
                             "nothing is installed yet")
            backend.plist_path("doctor").write_text(
                "<plist><string>/repo/x/bin/brain</string></plist>", encoding="utf-8")
            self.assertTrue(backend.serves("doctor", "/repo/x"))
            self.assertFalse(backend.serves("doctor", "/repo/other"),
                             "a different repo's path must not match")

    def test_systemd_serves_only_when_the_unit_mentions_the_marker(self):
        backend = osbackend.SystemdScheduler()
        with tempfile.TemporaryDirectory() as tmp:
            backend.units = Path(tmp)
            self.assertFalse(backend.serves("doctor", "/repo/x"),
                             "nothing is installed yet")
            (backend.units / "doctor.service").write_text(
                "[Service]\nExecStart=python3 /repo/x/bin/brain doctor\n",
                encoding="utf-8")
            self.assertTrue(backend.serves("doctor", "/repo/x"))
            self.assertFalse(backend.serves("doctor", "/repo/other"),
                             "a different repo's path must not match")

    def test_schtasks_query_argv_names_the_task(self):
        # Same reasoning as TestSchtasksArgv: this is the only part of
        # serves() a non-Windows machine can verify without running schtasks.
        argv = osbackend.SchtasksScheduler().query_argv("brain-doctor")
        self.assertIn("/query", argv)
        self.assertIn("brain-doctor", argv)
        self.assertIn("/v", argv)


class TestSchtasksUnavailable(unittest.TestCase):
    """serves()/status()/uninstall() must return the same graceful 'no
    scheduler' value install() already returns when schtasks is absent —
    true today on every machine this suite actually runs on (macOS dev boxes,
    ubuntu-latest CI), but forced here rather than relied upon: this file's
    own module docstring promises nothing in it depends on which platform
    tool is actually installed, so a real Windows runner added later must not
    flip these tests' premise out from under them. Before this fix,
    subprocess.run(["schtasks", ...]) raised an uncaught FileNotFoundError
    instead of returning gracefully."""

    def setUp(self):
        self.backend = osbackend.SchtasksScheduler()
        # Instance-attribute override, same trick as backend.agents/.units
        # above: shadows the bound method so available() is False regardless
        # of whether schtasks actually exists on whatever machine runs this.
        self.backend.available = lambda: False

    def test_serves_returns_false_without_raising(self):
        self.assertFalse(self.backend.serves("doctor", "/repo/x"))

    def test_status_reports_unavailable_without_raising(self):
        self.assertEqual(self.backend.status("doctor"),
                         "no scheduler available on this platform")

    def test_uninstall_reports_unavailable_without_raising(self):
        self.assertEqual(self.backend.uninstall("doctor"),
                         "no scheduler available on this platform — nothing to remove")


class TestLaunchdRenderExtras(unittest.TestCase):
    """cwd/env/log are optional — TestSystemdUnits' 3-arg render_units() call
    above must keep working unchanged — but a REAL brain job needs all three:
    a launchd agent starts with almost no environment, so without them the
    installed job cannot find Homebrew tools and leaves no log to debug that
    with. This is what setup/schedules/*.plist.template used to hold."""

    def test_minimal_call_omits_the_optional_keys(self):
        xml = osbackend.LaunchdScheduler().render(
            "doctor", ["python3", "/repo/bin/brain", "doctor"],
            {"hour": 9, "minute": 0})
        self.assertNotIn("WorkingDirectory", xml)
        self.assertNotIn("EnvironmentVariables", xml)
        self.assertNotIn("StandardOutPath", xml)

    def test_cwd_env_log_are_rendered_when_given(self):
        xml = osbackend.LaunchdScheduler().render(
            "doctor", ["python3", "/repo/bin/brain", "doctor"],
            {"hour": 9, "minute": 0}, cwd="/repo",
            env={"PATH": "/opt/homebrew/bin:/usr/bin"},
            log="/repo/.cache/doctor-launchd.log")
        self.assertIn("<key>WorkingDirectory</key><string>/repo</string>", xml)
        self.assertIn("/opt/homebrew/bin:/usr/bin", xml)
        self.assertIn("<key>StandardOutPath</key><string>"
                      "/repo/.cache/doctor-launchd.log</string>", xml)
        self.assertIn("<key>StandardErrorPath</key><string>"
                      "/repo/.cache/doctor-launchd.log</string>", xml)


class TestLaunchdUninstallUnavailable(unittest.TestCase):
    """Deferred from Task 2's review (carried into this task's brief): the
    same defect class TestSchtasksUnavailable covers above, in
    LaunchdScheduler.uninstall() instead of SchtasksScheduler's. Before the
    fix it called subprocess.run(["launchctl", ...]) with no available()
    guard, so a host lacking launchctl hit an uncaught FileNotFoundError on
    `brain schedule uninstall` instead of the graceful sentence install()
    already returned. status() is a plain path check with no subprocess call
    behind it, so it is not at risk and is untouched by this fix."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = osbackend.LaunchdScheduler()
        # Belt and braces with the available() override below: even if this
        # ever ran against the buggy pre-fix code, redirecting .agents means
        # the subprocess call it makes unloads a path inside a throwaway temp
        # dir, never anything under the real ~/Library/LaunchAgents.
        self.backend.agents = Path(self.tmp.name)
        self.backend.available = lambda: False

    def tearDown(self):
        self.tmp.cleanup()

    def test_uninstall_reports_unavailable_without_raising(self):
        self.assertEqual(self.backend.uninstall("doctor"),
                         "no scheduler available on this platform — nothing to remove")


class TestSystemdUninstallUnavailable(unittest.TestCase):
    """See TestLaunchdUninstallUnavailable above — SystemdScheduler.uninstall()
    had the identical unguarded subprocess.run(["systemctl", ...]) call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = osbackend.SystemdScheduler()
        # Same belt-and-braces reasoning as the launchd test above, redirected
        # to the systemd user-unit directory instead.
        self.backend.units = Path(self.tmp.name)
        self.backend.available = lambda: False

    def tearDown(self):
        self.tmp.cleanup()

    def test_uninstall_reports_unavailable_without_raising(self):
        self.assertEqual(self.backend.uninstall("doctor"),
                         "no scheduler available on this platform — nothing to remove")


class TestKeystoreSelection(unittest.TestCase):
    def test_each_family_gets_its_own_backend(self):
        self.assertEqual(osbackend.keystore_for("macos").kind, "keychain")
        self.assertEqual(osbackend.keystore_for("windows").kind, "credman")
        # Linux picks secret-tool when present and falls back to a 0600 file.
        self.assertIn(osbackend.keystore_for("linux").kind, {"secret-tool", "file"})

    def test_every_backend_describes_itself_for_the_user(self):
        for family in ("macos", "linux", "windows"):
            self.assertTrue(osbackend.keystore_for(family).describe())


class TestKeychainArgv(unittest.TestCase):
    """Never executed here. Running `security` for real would read or write the
    developer's actual login keychain, which a test must never do."""

    def test_get_argv_asks_for_the_password_value(self):
        argv = osbackend.KeychainKeystore().get_argv("brain-vault-key")
        self.assertIn("find-generic-password", argv)
        self.assertIn("brain-vault-key", argv)
        self.assertIn("-w", argv)


class TestCredmanArgv(unittest.TestCase):
    def test_set_argv_carries_name_and_value(self):
        argv = osbackend.CredmanKeystore().set_argv("brain-serve-token", "s3cr3t")
        joined = " ".join(argv)
        self.assertIn("brain-serve-token", joined)
        self.assertIn("s3cr3t", joined)


class TestFileKeystore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = osbackend.FileKeystore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        self.assertTrue(self.store.set("token", "abc123"))
        self.assertEqual(self.store.get("token"), "abc123")

    def test_missing_reads_as_empty_not_an_error(self):
        self.assertEqual(self.store.get("nothing-here"), "")

    def test_stored_file_is_not_group_or_world_readable(self):
        self.store.set("token", "abc123")
        mode = (Path(self.tmp.name) / "token").stat().st_mode & 0o077
        self.assertEqual(mode, 0, "secret file must be 0600")

    def test_delete_removes_it(self):
        self.store.set("token", "abc123")
        self.assertTrue(self.store.delete("token"))
        self.assertEqual(self.store.get("token"), "")


class TestCredmanFallback(unittest.TestCase):
    """CredmanKeystore.get() must never silently return "" for a secret that
    really IS stored just because the CredentialManager PowerShell module
    happens to be missing — see the class docstring for why that is data
    loss, not a shrug. This exercises the fallback path by forcing
    _module_available() directly rather than relying on this Mac's ambient
    (real, but incidental) lack of `powershell` — same "forced, not relied
    upon" reasoning as TestSchtasksUnavailable above."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fallback = osbackend.FileKeystore(Path(self.tmp.name))
        self.store = osbackend.CredmanKeystore(fallback=fallback)
        self.store._module_available = lambda: False

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_uses_the_fallback_when_the_module_is_missing(self):
        self.assertTrue(self.store.set("brain-vault-key", "abc123"))
        self.assertEqual(self.store.get("brain-vault-key"), "abc123")

    def test_missing_reads_as_empty_not_a_lie(self):
        self.assertEqual(self.store.get("nothing-here"), "")

    def test_delete_removes_it_from_the_fallback(self):
        self.store.set("brain-vault-key", "abc123")
        self.assertTrue(self.store.delete("brain-vault-key"))
        self.assertEqual(self.store.get("brain-vault-key"), "")

    def test_describe_says_the_module_is_missing(self):
        self.assertIn("CredentialManager", self.store.describe())


class TestCredmanFallbackDrift(unittest.TestCase):
    """Review finding on TestCredmanFallback: _module_available() is
    re-evaluated on every call with no record of where a name was actually
    written, so a set() while the module is missing followed by a get()
    after it becomes available (installing the module later is the ORDINARY
    way a stock Windows box ever gets it, not a corner case) went straight to
    the primary lookup, found nothing there, and returned "" for a secret
    that really was sitting in self.fallback — the exact "your vault key is
    gone" failure the class exists to prevent, reached via drift instead of
    permanent absence.

    _module_available is forced to a MUTABLE flag (not a fixed lambda, unlike
    TestCredmanFallback above) so it can flip mid-test. _primary_get is ALSO
    forced, to simulate "module available, Get-StoredCredential found
    nothing" — get()'s only path to a real subprocess call is through
    _primary_get, so overriding it, not just _module_available, is what keeps
    this test from ever executing real `powershell` regardless of which
    availability state it exercises: the never-touch-the-real-keystore
    property has to survive a test that deliberately flips available=True,
    not just one that holds it fixed at False."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fallback = osbackend.FileKeystore(Path(self.tmp.name))
        self.store = osbackend.CredmanKeystore(fallback=fallback)
        self.available = False
        self.store._module_available = lambda: self.available
        self.store._primary_get = lambda name: ""

    def tearDown(self):
        self.tmp.cleanup()

    def test_value_written_while_unavailable_is_still_readable_once_available(self):
        self.assertTrue(self.store.set("brain-vault-key", "abc123"))
        self.available = True
        self.assertEqual(self.store.get("brain-vault-key"), "abc123")


class TestCredmanDeleteDrift(unittest.TestCase):
    """Second review finding on the same drift: get()'s fallthrough above
    means a value surviving unnoticed in self.fallback is not just stale, it
    is RETURNED again on the next get() — so delete() picking only one
    backend (whichever cmdkey/module state looks live right now) is no
    longer an inert gap, it is "deleted, but still readable", a revocation
    bypass for a vault key or a `serve` token. Concretely: set() while the
    module is unavailable writes into self.fallback; the module gets
    installed later (ordinary, not a corner case — see
    TestCredmanFallbackDrift); delete() now takes the _module_available()
    branch and only clears cmdkey, which never had anything; the fallback
    copy survives; get() falls through to it and hands the "deleted" secret
    back.

    Same forcing technique as TestCredmanFallbackDrift, extended to
    delete()'s own subprocess call: _module_available flips mid-test via a
    mutable flag, and BOTH _primary_get and _primary_delete are forced so
    that neither of the two methods capable of reaching a real subprocess
    call (get()'s Get-StoredCredential, delete()'s cmdkey) can do so while
    this test deliberately exercises the available=True branch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fallback = osbackend.FileKeystore(Path(self.tmp.name))
        self.store = osbackend.CredmanKeystore(fallback=fallback)
        self.available = False
        self.store._module_available = lambda: self.available
        self.store._primary_get = lambda name: ""
        # cmdkey has nothing to remove in this scenario — the value was
        # never written through it — so stub the result rather than run the
        # real binary, same reasoning as _primary_get above.
        self.store._primary_delete = lambda name: False

    def tearDown(self):
        self.tmp.cleanup()

    def test_deleted_after_becoming_available_does_not_come_back(self):
        self.assertTrue(self.store.set("brain-vault-key", "abc123"))
        self.available = True
        self.store.delete("brain-vault-key")
        self.assertEqual(self.store.get("brain-vault-key"), "")


class TestLinkDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("skill", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_something_that_reads_through(self):
        method, message = osbackend.link_dir(self.base / "link", self.target)
        self.assertIn(method, {"symlink", "junction", "copy"})
        self.assertEqual((self.base / "link" / "SKILL.md").read_text(encoding="utf-8"),
                         "skill")
        self.assertTrue(message)

    def test_relinking_an_existing_correct_link_is_a_no_op(self):
        osbackend.link_dir(self.base / "link", self.target)
        method, message = osbackend.link_dir(self.base / "link", self.target)
        self.assertIn("already", message.lower())

    def test_refuses_a_real_directory_it_did_not_create(self):
        stranger = self.base / "link"
        stranger.mkdir()
        (stranger / "someone-elses.md").write_text("x", encoding="utf-8")
        method, message = osbackend.link_dir(stranger, self.target)
        # Never clobber a directory this system did not put there — on the
        # copy path that is somebody's files.
        self.assertEqual(method, "failed")
        self.assertTrue((stranger / "someone-elses.md").exists())


class TestLinkDirNeverRaises(unittest.TestCase):
    """Task 4 review, Finding 1: link_dir() must return ("failed", message)
    for every OSError it can hit, never propagate one — cmd_init has no
    try/except around its call to link_dir(), so an uncaught OSError there
    previously produced a raw traceback instead of a [4/5] FAILED line, and
    skipped step 5 and the final summary entirely. That matters most on
    exactly the cases this feature exists for: a locked file, a read-only
    filesystem, permission denied.

    Real chmod-based permission failures are flaky across platforms (root
    ignores them; Windows doesn't have the same model) and this suite must
    stay meaningful on all three, so each call site is forced to fail
    directly via unittest.mock instead — deterministic everywhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("skill", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unlink_failure_on_a_stale_symlink_is_reported_not_raised(self):
        link = self.base / "link"
        other = self.base / "other-target"
        other.mkdir()
        link.symlink_to(other, target_is_directory=True)
        with mock.patch.object(Path, "unlink", side_effect=OSError("locked")):
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "failed")
        self.assertIn("locked", message)

    def test_marker_read_failure_is_reported_not_raised(self):
        link = self.base / "link"
        link.mkdir()
        (link / osbackend._COPY_MARKER).write_text(str(self.target), encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "failed")
        self.assertIn("denied", message)

    def test_rmtree_failure_on_a_stale_copy_is_reported_not_raised(self):
        link = self.base / "link"
        link.mkdir()
        (link / osbackend._COPY_MARKER).write_text(str(self.base / "elsewhere"),
                                                    encoding="utf-8")
        with mock.patch("shutil.rmtree", side_effect=OSError("in use")):
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "failed")
        self.assertIn("in use", message)

    def test_mkdir_failure_is_reported_not_raised(self):
        link = self.base / "nested" / "link"
        with mock.patch.object(Path, "mkdir", side_effect=OSError("read-only fs")):
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "failed")
        self.assertIn("read-only fs", message)


class TestLinkDirRepointing(unittest.TestCase):
    """Task 4 review, Finding 2: a link silently re-pointed away from a
    DIFFERENT brain must say so distinctly and name what it replaced. This
    repo's own CLAUDE.md names the exact incident: `brain init` run from a
    scratch or template checkout re-points the global skill symlink at that
    checkout, "silently hijacking the skill for every session on this
    machine." The old inline cmd_init code had a distinct "skill relinked"
    label but never said what it was relinked FROM; this is stronger than
    that, not just a restore."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("skill", encoding="utf-8")
        self.other_target = self.base / "other-brain"
        self.other_target.mkdir()
        (self.other_target / "SKILL.md").write_text("someone else's skill",
                                                     encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_relinking_a_symlink_that_points_elsewhere_names_the_old_target(self):
        link = self.base / "link"
        link.symlink_to(self.other_target, target_is_directory=True)
        method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "symlink")
        self.assertIn("relinked", message.lower())
        self.assertIn(str(self.other_target), message)
        self.assertEqual((link / "SKILL.md").read_text(encoding="utf-8"), "skill")

    def test_relinking_a_stale_copy_names_the_old_target(self):
        link = self.base / "link"
        link.mkdir()
        (link / osbackend._COPY_MARKER).write_text(str(self.other_target),
                                                    encoding="utf-8")
        (link / "SKILL.md").write_text("someone else's skill", encoding="utf-8")
        method, message = osbackend.link_dir(link, self.target)
        self.assertIn(str(self.other_target), message)
        self.assertEqual((link / "SKILL.md").read_text(encoding="utf-8"), "skill")

    def test_fresh_link_does_not_claim_to_be_a_relink(self):
        # Regression guard on the distinction itself: "relinked" must appear
        # ONLY when something existing actually got replaced, never on a
        # plain first-time link — otherwise the signal Finding 2 restores is
        # meaningless noise on the common case.
        link = self.base / "link"
        method, message = osbackend.link_dir(link, self.target)
        self.assertNotIn("relinked", message.lower())


class TestLinkDirStaleMarkerMismatch(unittest.TestCase):
    """Task 4 review, Finding 4: the shutil.rmtree(link) branch (reached when
    an existing copy's marker names a DIFFERENT target than the one
    requested) was new in the original task-4 commit and had no covering
    test at all — a bare rmtree with no test is not something to ship,
    regardless of whether the code happens to be correct."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.old_target = self.base / "old-target"
        self.old_target.mkdir()
        (self.old_target / "SKILL.md").write_text("old skill", encoding="utf-8")
        self.new_target = self.base / "new-target"
        self.new_target.mkdir()
        (self.new_target / "SKILL.md").write_text("new skill", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_stale_copy_is_removed_and_replaced(self):
        link = self.base / "link"
        link.mkdir()
        (link / osbackend._COPY_MARKER).write_text(str(self.old_target),
                                                    encoding="utf-8")
        (link / "SKILL.md").write_text("old skill", encoding="utf-8")
        (link / "leftover-from-old-copy.txt").write_text("stale", encoding="utf-8")

        method, message = osbackend.link_dir(link, self.new_target)

        self.assertIn(method, {"symlink", "junction", "copy"})
        self.assertEqual((link / "SKILL.md").read_text(encoding="utf-8"), "new skill")
        # The stale file from the OLD copy must be gone, not merely
        # shadowed — this is the one thing a bare shutil.rmtree(link) has to
        # get right, and the reason this branch cannot ship untested.
        self.assertFalse((link / "leftover-from-old-copy.txt").exists())


class TestLinkDirCopyFallback(unittest.TestCase):
    """Task 4 review, Minor finding: the copy and junction fallback paths are
    the entire reason link_dir exists, but neither macOS nor Linux can reach
    them naturally in a temp dir — Path.symlink_to just succeeds there.
    test_creates_something_that_reads_through (TestLinkDir, above) accepts
    any of the three methods but in practice only ever observes "symlink" on
    this suite's actual platforms. Forcing symlink_to to fail is the only way
    to exercise the copy path, its marker write, and the "already correct
    (copy)" branch from any machine this suite runs on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.target = self.base / "target"
        self.target.mkdir()
        (self.target / "SKILL.md").write_text("skill", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_falls_back_to_a_marked_copy_when_symlink_is_unavailable(self):
        link = self.base / "link"
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("no dev mode")):
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "copy")
        self.assertEqual((link / "SKILL.md").read_text(encoding="utf-8"), "skill")
        self.assertEqual((link / osbackend._COPY_MARKER).read_text(encoding="utf-8"),
                         str(self.target))
        self.assertIn("go stale", message)

    def test_an_existing_correct_copy_is_a_no_op(self):
        link = self.base / "link"
        with mock.patch.object(Path, "symlink_to", side_effect=OSError("no dev mode")):
            osbackend.link_dir(link, self.target)
            method, message = osbackend.link_dir(link, self.target)
        self.assertEqual(method, "copy")
        self.assertIn("already correct", message.lower())


if sys.platform == "win32":
    # chmod is a no-op on Windows, so the 0600 assertion cannot hold there.
    TestFileKeystore.test_stored_file_is_not_group_or_world_readable = \
        unittest.skip("POSIX permissions do not apply on Windows")(
            TestFileKeystore.test_stored_file_is_not_group_or_world_readable)


if __name__ == "__main__":
    unittest.main()


class FakeRun:
    """A subprocess.run stand-in. No test here may install a real unit: this
    one restarts itself forever, so a leaked one outlives the test session."""

    def __init__(self, stdout="", returncode=0):
        self.calls = []
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        return type("Done", (), {"returncode": self.returncode,
                                 "stdout": self.stdout, "stderr": ""})()


class TestLingerDetection(unittest.TestCase):
    """The silent trap: with lingering off, every `systemd --user` unit stops
    at logout — the brain serve service AND every schedule — and nothing
    anywhere reports it."""

    def test_yes_is_on(self):
        run = FakeRun(stdout="Linger=yes\n")
        with mock.patch.object(osbackend, "os_family", lambda: "linux"), \
             mock.patch.object(osbackend.shutil, "which", lambda t: "/bin/loginctl"):
            self.assertEqual(osbackend.linger_state("u", runner=run), "on")

    def test_no_is_off(self):
        run = FakeRun(stdout="Linger=no\n")
        with mock.patch.object(osbackend, "os_family", lambda: "linux"), \
             mock.patch.object(osbackend.shutil, "which", lambda t: "/bin/loginctl"):
            self.assertEqual(osbackend.linger_state("u", runner=run), "off")

    def test_a_failed_call_is_unknown_not_off(self):
        """Reporting 'off' when we could not tell would send somebody chasing a
        setting that may not exist on their machine."""
        run = FakeRun(returncode=1)
        with mock.patch.object(osbackend, "os_family", lambda: "linux"), \
             mock.patch.object(osbackend.shutil, "which", lambda t: "/bin/loginctl"):
            self.assertEqual(osbackend.linger_state("u", runner=run), "unknown")

    def test_no_loginctl_is_unknown(self):
        with mock.patch.object(osbackend, "os_family", lambda: "linux"), \
             mock.patch.object(osbackend.shutil, "which", lambda t: None):
            self.assertEqual(osbackend.linger_state("u"), "unknown")

    def test_other_platforms_are_not_applicable(self):
        """LaunchAgents and scheduled tasks survive logout on their own, so a
        warning about lingering there is noise that teaches people to skip the
        health report."""
        for family in ("macos", "windows"):
            with mock.patch.object(osbackend, "os_family", lambda: family):
                self.assertEqual(osbackend.linger_state("u"), "n/a")


class TestServiceSelection(unittest.TestCase):
    def test_each_family_gets_its_own_backend(self):
        self.assertEqual(osbackend.service_for("macos").kind, "launchd")
        self.assertEqual(osbackend.service_for("linux").kind, "systemd")

    def test_windows_gets_the_base_class_deliberately(self):
        """A scheduled task starts something at logon and will NOT restart it
        when it dies, which is the entire property a service is for. Claiming
        support and delivering a process that vanishes is worse than saying so."""
        backend = osbackend.service_for("windows")
        self.assertEqual(backend.kind, "none")
        self.assertFalse(backend.available())
        self.assertIn("no service manager", backend.install([]))

    def test_the_unavailable_backend_never_raises(self):
        backend = osbackend.Service()
        for sentence in (backend.install([]), backend.uninstall(), backend.status()):
            self.assertTrue(sentence)
        self.assertFalse(backend.serves("/anywhere"))


class TestSystemdServiceUnit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = osbackend.SystemdService(runner=FakeRun())
        self.backend.units = Path(self.tmp.name)

    def test_it_restarts_which_is_the_whole_point(self):
        unit = self.backend.render_unit([sys.executable, "/b/bin/brain", "serve"])
        self.assertIn("Restart=always", unit)
        self.assertIn("Type=simple", unit)

    def test_it_waits_for_the_network(self):
        """A tunnel dialling out at boot needs routing actually up; Restart=
        would otherwise hide it as a crash loop nobody reads."""
        unit = self.backend.render_unit(["/b/bin/brain"])
        self.assertIn("network-online.target", unit)

    def test_it_is_a_user_unit_not_a_system_one(self):
        self.assertIn("WantedBy=default.target",
                      self.backend.render_unit(["/b/bin/brain"]))

    def test_arguments_are_quoted(self):
        """--public-url is operator input. An unquoted space would silently
        truncate the command into something that still starts and serves the
        wrong thing."""
        unit = self.backend.render_unit(
            ["/b/bin/brain", "serve", "--public-url", "https://x/mcp",
             "--source", "a b"])
        self.assertIn("'a b'", unit)

    def test_the_working_directory_names_the_brain(self):
        unit = self.backend.render_unit(["/b/bin/brain"], cwd="/home/u/brain")
        self.assertIn("WorkingDirectory=/home/u/brain", unit)
        self.assertTrue(self.backend.render_unit(["/b/bin/brain"], cwd="/home/u/brain"))

    def test_install_writes_and_enables(self):
        run = FakeRun()
        self.backend._run = run
        self.backend.available = lambda: True
        outcome = self.backend.install(["/b/bin/brain", "serve"], cwd="/b")
        self.assertTrue(outcome.startswith("installed"))
        self.assertTrue(self.backend.unit_path().exists())
        self.assertIn(["systemctl", "--user", "daemon-reload"], run.calls)
        self.assertIn(["systemctl", "--user", "enable", "--now",
                       "brain-serve.service"], run.calls)

    def test_a_refused_start_is_reported_not_claimed(self):
        self.backend._run = FakeRun(returncode=1)
        self.backend.available = lambda: True
        self.assertNotIn("installed and started",
                         self.backend.install(["/b/bin/brain", "serve"]))

    def test_status_and_serves(self):
        self.backend.available = lambda: True
        self.assertEqual(self.backend.status(), "not installed")
        self.backend._run = FakeRun(stdout="active\n")
        self.backend.install(["/b/bin/brain", "serve"], cwd="/home/u/mybrain")
        self.assertEqual(self.backend.status(), "running")
        self.assertTrue(self.backend.serves("/home/u/mybrain"))
        self.assertFalse(self.backend.serves("/home/u/somebody-elses-brain"))

    def test_uninstall_when_absent_says_so(self):
        self.backend.available = lambda: True
        self.assertEqual(self.backend.uninstall(), "was not installed")

    def test_unavailable_never_raises(self):
        self.backend.available = lambda: False
        self.assertIn("no service manager", self.backend.install(["x"]))
        self.assertIn("nothing to remove", self.backend.uninstall())


class TestLaunchdServicePlist(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backend = osbackend.LaunchdService(runner=FakeRun())
        self.backend.agents = Path(self.tmp.name)

    def test_keepalive_is_what_makes_it_a_service(self):
        plist = self.backend.render_plist(["/b/bin/brain", "serve"])
        self.assertIn("<key>KeepAlive</key><true/>", plist)
        self.assertIn("<key>RunAtLoad</key><true/>", plist)

    def test_arguments_are_xml_escaped(self):
        """A bare & in a --public-url would produce a plist launchd silently
        refuses to load."""
        plist = self.backend.render_plist(
            ["/b/bin/brain", "--public-url", "https://x/mcp?a&b"])
        self.assertIn("&amp;", plist)
        self.assertNotIn("?a&b", plist)

    def test_unavailable_never_raises(self):
        self.backend.available = lambda: False
        self.assertIn("no service manager", self.backend.install(["x"]))


class TestPathShim(unittest.TestCase):
    """`bin/brain` is not on PATH, deliberately — this project installs nothing
    globally without being asked. The defect was that setup's summary said
    "Next: brain connect" anyway, and on a real first install the shell
    answered `command not found`."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / ".local" / "bin").mkdir(parents=True)

    def test_offered_when_the_directory_is_already_on_path(self):
        env = {"PATH": f"/usr/bin:{self.home}/.local/bin"}
        self.assertEqual(osbackend.path_shim(env=env, home=self.home),
                         self.home / ".local" / "bin")

    def test_not_offered_when_it_is_not_on_path(self):
        """A symlink into a directory the shell does not search is a second way
        of saying the same wrong thing."""
        self.assertIsNone(osbackend.path_shim(env={"PATH": "/usr/bin"},
                                              home=self.home))

    def test_not_offered_on_windows(self):
        """A symlink there needs Developer Mode or admin, and brain.cmd already
        exists for that platform."""
        env = {"PATH": f"{self.home}/.local/bin"}
        with mock.patch.object(osbackend, "os_family", lambda: "windows"):
            self.assertIsNone(osbackend.path_shim(env=env, home=self.home))

    def test_ownership_is_resolvable(self):
        brain = self.home / "mybrain"
        (brain / "bin").mkdir(parents=True)
        (brain / "bin" / "brain").write_text("#!/usr/bin/env python3\n")
        shim = self.home / ".local" / "bin" / "brain"
        shim.symlink_to(brain / "bin" / "brain")
        self.assertEqual(osbackend.shim_owner(shim), (brain / "bin" / "brain").resolve())

    def test_a_plain_file_has_no_owner(self):
        """Something that is not a symlink was not put there by this project,
        and must never be removed by it."""
        shim = self.home / ".local" / "bin" / "brain"
        shim.write_text("#!/bin/sh\necho not ours\n")
        self.assertIsNone(osbackend.shim_owner(shim))

    def test_a_missing_shim_has_no_owner(self):
        self.assertIsNone(osbackend.shim_owner(self.home / ".local" / "bin" / "brain"))
        self.assertIsNone(osbackend.shim_owner(None))

    def test_a_dangling_symlink_does_not_raise(self):
        shim = self.home / ".local" / "bin" / "brain"
        shim.symlink_to(self.home / "gone" / "bin" / "brain")
        osbackend.shim_owner(shim)      # must not raise
