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


if sys.platform == "win32":
    # chmod is a no-op on Windows, so the 0600 assertion cannot hold there.
    TestFileKeystore.test_stored_file_is_not_group_or_world_readable = \
        unittest.skip("POSIX permissions do not apply on Windows")(
            TestFileKeystore.test_stored_file_is_not_group_or_world_readable)


if __name__ == "__main__":
    unittest.main()
