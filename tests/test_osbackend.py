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


if __name__ == "__main__":
    unittest.main()
