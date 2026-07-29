# Setup Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the macOS-only shell installer with one cross-platform `brain setup` command that works identically for a human at a terminal and an agent running it headless, and that cannot finish in a state `doctor` calls red.

**Architecture:** `bin/brain` is a 5,034-line single file that calls `launchctl` and `security` inline, which is the whole reason it is macOS-only. This plan extracts three OS-dependent concerns into `bin/brainlib/` — a package importable because Python puts the running script's directory (`bin/`) on `sys.path[0]` — and adds `brain setup` as the single owner of first-run state. `install.sh` shrinks to a bootstrap; `install.ps1` is its Windows twin. Both do nothing but verify git+python, clone, and hand off.

**Tech Stack:** Python 3.9+ standard library only. POSIX `sh` and PowerShell 5.1 for the two bootstraps. `unittest` for tests. GitHub Actions for the platform matrix.

## Global Constraints

These apply to every task. Violating any of them fails review.

- **Python 3.9 floor.** No `match`, no `X | Y` unions at runtime, no `dict1 | dict2`, no `str.removeprefix`. Verified by `python3 -c 'import sys; sys.version_info[:2] >= (3,9)'` in [install.sh:131](install.sh:131).
- **Zero third-party dependencies.** Standard library only, everywhere. This is load-bearing: `bin/brain-mcp` advertises "zero dependencies and no vendor SDK" and the whole system must run on a machine with nothing installed.
- **Never install anything on the user's machine.** Print the exact command and the consequence. This was an explicit owner decision.
- **All dates in code, comments and docs are absolute** (`2026-07-25`), never "today" or "last week". Enforced by `bin/brain lint` for notes; follow it everywhere.
- **New files under `bin/` ship automatically.** `cmd_template` publishes what `git ls-files` tracks ([bin/brain:3763](bin/brain:3763)), so tracked modules need no registration. Do not add them to `TEMPLATE_DROP`.
- **The commit gate runs on every commit.** `.githooks/pre-commit` runs `python3 bin/brain lint --staged` plus `gitleaks`. Every task's commit step must pass it.
- **Existing tests must keep passing.** `python3 -m unittest discover -s tests` — 213 tests today. Never delete one to make a change pass; if a test is genuinely wrong, say so in the commit message.
- **Match the surrounding prose style.** This codebase's comments explain *why*, especially why an obvious alternative was rejected. Terse "what" comments are out of place here.

---

### Task 1: OS detection and the prerequisite table

The data every later task reads to decide what is missing and what to tell the user.

**Files:**
- Create: `bin/brainlib/__init__.py`
- Create: `bin/brainlib/osbackend.py`
- Test: `tests/test_osbackend.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `os_family() -> str` — one of `"macos"`, `"linux"`, `"windows"`, `"unknown"`
  - `PREREQS: dict` — tool name → `{"hard": bool, "why": str, "pkg": dict}`
  - `package_manager() -> str` — e.g. `"brew"`, `"apt"`, `"dnf"`, `"pacman"`, `"winget"`, or `""`
  - `install_hint(tool: str) -> str` — a literal shell command, or `""` when unknown

- [x] **Step 1: Write the failing test**

```python
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'brainlib'`

- [x] **Step 3: Write minimal implementation**

```python
# bin/brainlib/__init__.py
"""Per-concern modules for the brain toolbelt.

bin/brain stays the single entry point and the place behavior is documented.
This package exists for the parts that MUST vary by machine — OS backends,
the interactive picker, the setup phases — because keeping them inline is what
made the toolbelt macOS-only and untestable without a real terminal.

Importable because Python puts the running script's directory (bin/) at
sys.path[0], so `import brainlib.osbackend` resolves for `python3 bin/brain`,
for `bin/brain-mcp`, and for the git hooks, all without a package install.
"""
```

```python
# bin/brainlib/osbackend.py
"""Per-OS backends: platform detection and the prerequisite table.

bin/brain called `launchctl` and `security` inline, which is the entire reason
it ran on macOS only. Everything machine-specific belongs here, behind one
interface per concern, so the rest of the toolbelt can stay platform-blind.
"""
import os
import shutil
import sys

# What each tool BUYS you, phrased as what its absence costs. A prerequisite
# list that says "gh not installed" tells the reader nothing they can act on;
# "no automatic private backup" tells them whether they care.
PREREQS = {
    "git": {
        "hard": True,
        "why": "the brain IS a git repository — nothing works without it",
        "pkg": {"brew": "git", "apt": "git", "dnf": "git",
                "pacman": "git", "winget": "Git.Git"},
    },
    "python3": {
        "hard": True,
        "why": "the toolbelt and the MCP server are Python (3.9 or newer)",
        "pkg": {},
    },
    "gh": {
        "hard": False,
        "why": "no automatic private backup — you would create the GitHub repo yourself",
        "pkg": {"brew": "gh", "apt": "gh", "dnf": "gh",
                "pacman": "github-cli", "winget": "GitHub.cli"},
    },
    "gitleaks": {
        "hard": False,
        "why": "the secret gate falls back to the built-in scanner alone",
        "pkg": {"brew": "gitleaks", "apt": "gitleaks", "dnf": "gitleaks",
                "pacman": "gitleaks", "winget": "Gitleaks.Gitleaks"},
    },
    "age": {
        "hard": False,
        "why": "no encrypted vault for sensitive notes",
        "pkg": {"brew": "age", "apt": "age", "dnf": "age",
                "pacman": "age", "winget": "FiloSottile.age"},
    },
    "rg": {
        "hard": False,
        "why": "search still works; the plain-grep tier is slower",
        "pkg": {"brew": "ripgrep", "apt": "ripgrep", "dnf": "ripgrep",
                "pacman": "ripgrep", "winget": "BurntSushi.ripgrep.MSVC"},
    },
}

# How each manager spells "install this". Kept next to PREREQS so a new tool
# and a new manager cannot drift apart silently.
_MANAGER_COMMANDS = {
    "brew": "brew install {pkg}",
    "apt": "sudo apt install {pkg}",
    "dnf": "sudo dnf install {pkg}",
    "pacman": "sudo pacman -S {pkg}",
    "winget": "winget install --id {pkg}",
}


def os_family() -> str:
    """'macos' | 'linux' | 'windows' | 'unknown'.

    sys.platform rather than platform.system(): it is a plain string constant
    with no subprocess behind it, so this stays safe to call at import time.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if os.name == "nt":
        return "windows"
    return "unknown"


def package_manager() -> str:
    """The manager actually present on this machine, or '' if none is.

    Order matters on Linux: a machine can have more than one, and the first
    that exists is the one whose packages will be current.
    """
    family = os_family()
    if family == "macos":
        return "brew" if shutil.which("brew") else ""
    if family == "windows":
        return "winget" if shutil.which("winget") else ""
    for manager in ("apt", "dnf", "pacman"):
        if shutil.which(manager):
            return manager
    return ""


def install_hint(tool: str) -> str:
    """A literal command the user can paste, or '' when we cannot be sure.

    Returning '' is deliberate and better than guessing: a wrong install
    command wastes more of someone's time than no command at all.
    """
    spec = PREREQS.get(tool)
    if not spec:
        return ""
    manager = package_manager()
    package = spec["pkg"].get(manager)
    if not manager or not package:
        return ""
    return _MANAGER_COMMANDS[manager].format(pkg=package)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: PASS, 7 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/__init__.py bin/brainlib/osbackend.py tests/test_osbackend.py
git commit -m "osbackend: platform detection and the prerequisite table"
```

---

### Task 2: Scheduler backends

Moves `launchctl` out of `bin/brain` and gives Linux and Windows a real implementation.

**Files:**
- Modify: `bin/brainlib/osbackend.py`
- Modify: `bin/brain:4153-4192` (`cmd_schedule` delegates instead of calling `launchctl`)
- Test: `tests/test_osbackend.py`

**Interfaces:**
- Consumes: `os_family()` from Task 1.
- Produces:
  - `scheduler_for(family: str) -> Scheduler` — a backend instance
  - `Scheduler.available() -> bool`, `.install(name, argv, when) -> str`, `.uninstall(name) -> str`, `.status(name) -> str`
  - `when` is a dict: `{"weekday": 1, "hour": 9, "minute": 0}` (Monday=1), or `{"hour": 9, "minute": 0}` for daily.
  - `.install()` returns a human sentence; it never raises on an unavailable backend, it returns a sentence saying so.

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_osbackend.py
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: FAIL with `AttributeError: module 'brainlib.osbackend' has no attribute 'scheduler_for'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/osbackend.py`:

```python
import subprocess
from pathlib import Path

_DAY_NAMES = {1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT", 7: "SUN"}


class Scheduler:
    """One interface, three implementations.

    Every method returns a human sentence rather than raising. A machine with
    no usable scheduler is a NORMAL state — schedules are optional and setup
    must complete without one — so 'cannot' is a return value, not an error.
    """
    kind = "none"

    def available(self) -> bool:
        return False

    def install(self, name: str, argv: list, when: dict) -> str:
        return ("no scheduler available on this platform — run "
                "`brain consolidate` by hand, or wire your own cron entry")

    def uninstall(self, name: str) -> str:
        return "no scheduler available on this platform — nothing to remove"

    def status(self, name: str) -> str:
        return "no scheduler available on this platform"


class LaunchdScheduler(Scheduler):
    kind = "launchd"

    def __init__(self):
        self.agents = Path.home() / "Library" / "LaunchAgents"

    def available(self) -> bool:
        return bool(shutil.which("launchctl"))

    def plist_path(self, name: str) -> Path:
        return self.agents / f"com.secondbrain.{name}.plist"

    def render(self, name: str, argv: list, when: dict) -> str:
        args = "".join(f"      <string>{a}</string>\n" for a in argv)
        cal = f"      <key>Hour</key><integer>{when['hour']}</integer>\n"
        cal += f"      <key>Minute</key><integer>{when['minute']}</integer>\n"
        if "weekday" in when:
            # launchd's Weekday is 0-6 with Sunday=0; ours is 1-7 with Monday=1.
            cal += (f"      <key>Weekday</key><integer>"
                    f"{when['weekday'] % 7}</integer>\n")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<plist version="1.0">\n  <dict>\n'
                f'    <key>Label</key><string>com.secondbrain.{name}</string>\n'
                f'    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n'
                f'    <key>StartCalendarInterval</key>\n    <dict>\n{cal}    </dict>\n'
                '  </dict>\n</plist>\n')

    def install(self, name: str, argv: list, when: dict) -> str:
        if not self.available():
            return super().install(name, argv, when)
        path = self.plist_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(name, argv, when), encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        subprocess.run(["launchctl", "load", str(path)], capture_output=True)
        return f"installed ({path})"

    def uninstall(self, name: str) -> str:
        path = self.plist_path(name)
        subprocess.run(["launchctl", "unload", str(path)], capture_output=True)
        if path.exists():
            path.unlink()
            return "removed"
        return "was not installed"

    def status(self, name: str) -> str:
        return "installed" if self.plist_path(name).exists() else "not installed"


class SystemdScheduler(Scheduler):
    kind = "systemd"

    def __init__(self):
        self.units = Path.home() / ".config" / "systemd" / "user"

    def available(self) -> bool:
        return bool(shutil.which("systemctl"))

    def render_units(self, name: str, argv: list, when: dict) -> tuple:
        exec_line = " ".join(argv)
        service = ("[Unit]\n"
                   f"Description=brain {name}\n\n"
                   "[Service]\nType=oneshot\n"
                   f"ExecStart={exec_line}\n")
        stamp = f"{when['hour']:02d}:{when['minute']:02d}:00"
        if "weekday" in when:
            day = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][when["weekday"] - 1]
            calendar = f"{day} *-*-* {stamp}"
        else:
            calendar = f"*-*-* {stamp}"
        timer = ("[Unit]\n"
                 f"Description=brain {name} timer\n\n"
                 f"[Timer]\nOnCalendar={calendar}\n"
                 # Without Persistent, a machine asleep at the scheduled moment
                 # simply skips the run and nobody is told.
                 "Persistent=true\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        return service, timer

    def install(self, name: str, argv: list, when: dict) -> str:
        if not self.available():
            return super().install(name, argv, when)
        self.units.mkdir(parents=True, exist_ok=True)
        service, timer = self.render_units(name, argv, when)
        (self.units / f"{name}.service").write_text(service, encoding="utf-8")
        (self.units / f"{name}.timer").write_text(timer, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.timer"],
                       capture_output=True)
        return f"installed ({self.units / (name + '.timer')})"

    def uninstall(self, name: str) -> str:
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.timer"],
                       capture_output=True)
        removed = False
        for suffix in (".service", ".timer"):
            path = self.units / f"{name}{suffix}"
            if path.exists():
                path.unlink()
                removed = True
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "removed" if removed else "was not installed"

    def status(self, name: str) -> str:
        return "installed" if (self.units / f"{name}.timer").exists() else "not installed"


class SchtasksScheduler(Scheduler):
    kind = "schtasks"

    def available(self) -> bool:
        return bool(shutil.which("schtasks"))

    def install_argv(self, name: str, argv: list, when: dict) -> list:
        # schtasks takes the command as ONE quoted string, not as argv.
        command = subprocess.list2cmdline(argv)
        stamp = f"{when['hour']:02d}:{when['minute']:02d}"
        built = ["schtasks", "/create", "/f", "/tn", name, "/tr", command]
        if "weekday" in when:
            built += ["/sc", "WEEKLY", "/d", _DAY_NAMES[when["weekday"]], "/st", stamp]
        else:
            built += ["/sc", "DAILY", "/st", stamp]
        return built

    def install(self, name: str, argv: list, when: dict) -> str:
        if not self.available():
            return super().install(name, argv, when)
        done = subprocess.run(self.install_argv(name, argv, when),
                              capture_output=True, text=True)
        if done.returncode != 0:
            return f"could not install: {(done.stderr or '').strip()[:200]}"
        return f"installed (Task Scheduler: {name})"

    def uninstall(self, name: str) -> str:
        done = subprocess.run(["schtasks", "/delete", "/f", "/tn", name],
                              capture_output=True, text=True)
        return "removed" if done.returncode == 0 else "was not installed"

    def status(self, name: str) -> str:
        done = subprocess.run(["schtasks", "/query", "/tn", name],
                              capture_output=True, text=True)
        return "installed" if done.returncode == 0 else "not installed"


def scheduler_for(family: str) -> Scheduler:
    return {"macos": LaunchdScheduler, "linux": SystemdScheduler,
            "windows": SchtasksScheduler}.get(family, Scheduler)()


def scheduler() -> Scheduler:
    return scheduler_for(os_family())
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: PASS, 12 tests

- [x] **Step 5: Rewire `cmd_schedule` to delegate**

In `bin/brain`, replace the body of `cmd_schedule` ([bin/brain:4153](bin/brain:4153)) so it calls `osbackend.scheduler()` instead of writing plists and calling `launchctl` itself. Keep the command's existing CLI surface (`install [--with-consolidate] | uninstall | status`) and its existing output wording unchanged — this task is a move, not a redesign. Add near the top of `bin/brain`:

```python
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brainlib import osbackend
```

- [x] **Step 6: Run the full suite**

Run: `python3 -m unittest discover -s tests`
Expected: PASS — 213 existing + 12 new. `schedule_serves_this_repo` and the doctor checks that read `PLIST_DIR` must still work on macOS.

- [x] **Step 7: Commit**

```bash
git add bin/brainlib/osbackend.py bin/brain tests/test_osbackend.py
git commit -m "osbackend: scheduler backends for launchd, systemd and schtasks"
```

---

### Task 3: Keystore backends

Where the vault key lives today, and where the `serve` token will live in Plan 3.

**Files:**
- Modify: `bin/brainlib/osbackend.py`
- Test: `tests/test_osbackend.py`

**Interfaces:**
- Consumes: `os_family()`.
- Produces:
  - `keystore_for(family: str) -> Keystore`
  - `Keystore.get(name) -> str` (`""` when absent), `.set(name, value) -> bool`, `.delete(name) -> bool`, `.describe() -> str`
  - `KeychainKeystore.get_argv(name) -> list`, `CredmanKeystore.set_argv(name, value) -> list` — argv builders, so correctness is checkable off-platform
  - `FileKeystore(directory)` — the Linux fallback, fully testable

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_osbackend.py
import tempfile


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


if sys.platform == "win32":
    # chmod is a no-op on Windows, so the 0600 assertion cannot hold there.
    TestFileKeystore.test_stored_file_is_not_group_or_world_readable = \
        unittest.skip("POSIX permissions do not apply on Windows")(
            TestFileKeystore.test_stored_file_is_not_group_or_world_readable)
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: FAIL with `AttributeError: module 'brainlib.osbackend' has no attribute 'keystore_for'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/osbackend.py`:

```python
class Keystore:
    """Where a secret lives on this machine.

    Never the repo: `brain lint` refuses credentials in tracked files and that
    rule is not being weakened. Every method fails soft — a machine with no
    keystore must still be able to run everything that does not need one.
    """
    kind = "none"

    def describe(self) -> str:
        return "no OS keystore available — secrets must be supplied by hand"

    def get(self, name: str) -> str:
        return ""

    def set(self, name: str, value: str) -> bool:
        return False

    def delete(self, name: str) -> bool:
        return False


class KeychainKeystore(Keystore):
    kind = "keychain"

    def describe(self) -> str:
        return "macOS Keychain"

    def get_argv(self, name: str) -> list:
        return ["security", "find-generic-password", "-a", os.environ.get("USER", ""),
                "-s", name, "-w"]

    def set_argv(self, name: str, value: str) -> list:
        return ["security", "add-generic-password", "-U", "-a",
                os.environ.get("USER", ""), "-s", name, "-w", value]

    def get(self, name: str) -> str:
        done = subprocess.run(self.get_argv(name), capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    def set(self, name: str, value: str) -> bool:
        return subprocess.run(self.set_argv(name, value),
                              capture_output=True).returncode == 0

    def delete(self, name: str) -> bool:
        return subprocess.run(
            ["security", "delete-generic-password", "-a",
             os.environ.get("USER", ""), "-s", name],
            capture_output=True).returncode == 0


class SecretToolKeystore(Keystore):
    kind = "secret-tool"

    def describe(self) -> str:
        return "the freedesktop secret service (secret-tool)"

    def get(self, name: str) -> str:
        done = subprocess.run(["secret-tool", "lookup", "service", "brain",
                               "account", name], capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    def set(self, name: str, value: str) -> bool:
        done = subprocess.run(["secret-tool", "store", "--label", f"brain {name}",
                               "service", "brain", "account", name],
                              input=value, capture_output=True, text=True)
        return done.returncode == 0

    def delete(self, name: str) -> bool:
        return subprocess.run(["secret-tool", "clear", "service", "brain",
                               "account", name], capture_output=True).returncode == 0


class FileKeystore(Keystore):
    """The fallback: a 0600 file under ~/.config/brain/secrets/.

    Weaker than a real keystore and it says so. It exists because a headless
    Linux box frequently has no secret service at all, and refusing to store
    anything there would make the vault and `serve` simply unavailable.
    """
    kind = "file"

    def __init__(self, directory=None):
        self.dir = Path(directory) if directory else \
            Path.home() / ".config" / "brain" / "secrets"

    def describe(self) -> str:
        return f"a 0600 file under {self.dir} (no OS keystore found)"

    def get(self, name: str) -> str:
        path = self.dir / name
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def set(self, name: str, value: str) -> bool:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / name
            # Create with the right mode BEFORE writing. Writing first and
            # chmod-ing after leaves the secret world-readable for the window
            # between the two, which is exactly when a backup job runs.
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
            return True
        except OSError:
            return False

    def delete(self, name: str) -> bool:
        try:
            (self.dir / name).unlink()
            return True
        except OSError:
            return False


class CredmanKeystore(Keystore):
    kind = "credman"

    def describe(self) -> str:
        return "Windows Credential Manager"

    def set_argv(self, name: str, value: str) -> list:
        return ["cmdkey", f"/generic:brain:{name}", "/user:brain", f"/pass:{value}"]

    def get(self, name: str) -> str:
        # cmdkey stores but will not print a secret back. PowerShell's
        # CredentialManager surface is the documented way to read one.
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-StoredCredential -Target 'brain:{name}')"
             ".GetNetworkCredential().Password"],
            capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    def set(self, name: str, value: str) -> bool:
        return subprocess.run(self.set_argv(name, value),
                              capture_output=True).returncode == 0

    def delete(self, name: str) -> bool:
        return subprocess.run(["cmdkey", f"/delete:brain:{name}"],
                              capture_output=True).returncode == 0


def keystore_for(family: str) -> Keystore:
    if family == "macos":
        return KeychainKeystore()
    if family == "windows":
        return CredmanKeystore()
    if family == "linux":
        return SecretToolKeystore() if shutil.which("secret-tool") else FileKeystore()
    return Keystore()


def keystore() -> Keystore:
    return keystore_for(os_family())
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: PASS, 21 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/osbackend.py tests/test_osbackend.py
git commit -m "osbackend: keystore backends for Keychain, secret-tool, file and Credential Manager"
```

**Note for the implementer:** `CredmanKeystore.get` depends on the `CredentialManager` PowerShell module, which is not present by default. Verify this on the Windows CI runner in Task 12; if it is unavailable, fall back to `FileKeystore` on Windows and say so in `describe()`. Do not leave a `get` that silently returns `""` when a secret really is stored — that turns a missing module into "your vault key is gone".

---

### Task 4: Skill link backend

The `/brain` skill is installed as a symlink today, which needs Developer Mode on Windows.

**Files:**
- Modify: `bin/brainlib/osbackend.py`
- Modify: `bin/brain:1460-1486` (step 4 of `cmd_init` delegates)
- Test: `tests/test_osbackend.py`

**Interfaces:**
- Consumes: `os_family()`.
- Produces: `link_dir(link: Path, target: Path) -> tuple` returning `(method, message)` where `method` is `"symlink"`, `"junction"`, `"copy"` or `"failed"`.

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_osbackend.py
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: FAIL with `AttributeError: module 'brainlib.osbackend' has no attribute 'link_dir'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/osbackend.py`:

```python
# A marker file dropped beside a COPY so a later run can tell its own copy from
# a directory somebody else created. Without it, the copy path has no safe way
# to refuse, and "overwrite whatever is there" is how you delete a stranger's
# skill folder.
_COPY_MARKER = ".brain-managed-copy"


def link_dir(link, target) -> tuple:
    """Make `link` resolve to `target`. Returns (method, message).

    Three methods in descending order of goodness. A Windows SYMLINK needs
    Developer Mode or admin; a Windows JUNCTION needs neither, which is why it
    is preferred there over asking people to change a system setting. A copy is
    last because it goes stale, so `doctor` has to be able to notice one.
    """
    import shutil as _shutil
    link, target = Path(link), Path(target)

    if link.is_symlink():
        try:
            if link.resolve() == target.resolve():
                return "symlink", "already correct"
        except OSError:
            pass
        link.unlink()
    elif link.exists():
        if (link / _COPY_MARKER).exists():
            if (link / _COPY_MARKER).read_text(encoding="utf-8").strip() == str(target):
                return "copy", "already correct (copy)"
            _shutil.rmtree(link)
        else:
            return "failed", (f"{link} already exists and was not created by brain — "
                              "move it aside, then run this again")

    link.parent.mkdir(parents=True, exist_ok=True)

    try:
        link.symlink_to(target, target_is_directory=True)
        return "symlink", f"linked {link}"
    except (OSError, NotImplementedError):
        pass

    if os_family() == "windows":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if done.returncode == 0:
            return "junction", f"junction created at {link}"

    try:
        _shutil.copytree(target, link)
        (link / _COPY_MARKER).write_text(str(target), encoding="utf-8")
        return "copy", (f"copied to {link} — neither a symlink nor a junction was "
                        "possible, so this will go stale; `brain doctor` will say when")
    except OSError as exc:
        return "failed", f"could not link or copy {link}: {exc}"
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_osbackend -v`
Expected: PASS, 25 tests

- [x] **Step 5: Rewire `cmd_init` step 4 to delegate**

Replace the symlink block at [bin/brain:1460-1486](bin/brain:1460) with a call to `osbackend.link_dir(link, skill_dir)`, preserving the existing `[4/5]` output format and the existing failure text. Keep the ownership check that already lives in `reset_dewire` untouched — Plan 2 deals with `retire`.

- [x] **Step 6: Run the full suite**

Run: `python3 -m unittest discover -s tests`
Expected: PASS

- [x] **Step 7: Commit**

```bash
git add bin/brainlib/osbackend.py bin/brain tests/test_osbackend.py
git commit -m "osbackend: skill linking via symlink, junction or marked copy"
```

---

### Task 5: Windows file-level compatibility

Two small files without which Windows fails silently rather than loudly.

**Files:**
- Create: `.gitattributes`
- Create: `brain.cmd`
- Modify: `tests/test_brain.py:33-34` (`SANDBOX_IGNORE`)
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no Python symbols. `brain.cmd` gives Windows users `brain <command>`.

- [x] **Step 1: Write the failing test**

```python
# tests/test_setup.py
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `.gitattributes is missing`

- [x] **Step 3: Write minimal implementation**

```
# .gitattributes
# Line endings are load-bearing here, and the failure they cause is silent.
#
# A CRLF checkout of a `#!/bin/sh` file corrupts the shebang: the kernel looks
# for an interpreter named "/bin/sh\r", does not find it, and the hook simply
# does not run. The commit gate would be DOWN on every Windows machine with no
# error anyone would read. Git for Windows ships bash, so the hooks themselves
# are portable — only this is not.
.githooks/* text eol=lf
bin/*       text eol=lf

# The reverse, for the one file that must be CRLF: cmd.exe misparses a .cmd
# with LF endings.
*.cmd       text eol=crlf

# Never let Git guess about binaries.
*.png binary
*.jpg binary
*.age binary
```

```
@echo off
REM brain — Windows shim so `brain <command>` works in cmd.exe and PowerShell.
REM
REM bin\brain has no .exe/.cmd extension and relies on a shebang, which Windows
REM does not honour. This is the entry point PATH can actually find.
python "%~dp0bin\brain" %*
```

Then add `__pycache__` to `SANDBOX_IGNORE` in [tests/test_brain.py:33](tests/test_brain.py:33), since `bin/brainlib` now produces bytecode that would otherwise be copied into every sandbox:

```python
SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git", ".cache", "graphify-out", "node_modules", ".DS_Store", "__pycache__")
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 4 tests

- [x] **Step 5: Verify the attributes actually apply**

Run: `git check-attr text eol -- .githooks/pre-commit bin/brain brain.cmd`
Expected: `eol: lf` for the first two, `eol: crlf` for `brain.cmd`

- [x] **Step 6: Commit**

```bash
git add .gitattributes brain.cmd tests/test_setup.py tests/test_brain.py
git commit -m "windows: pin line endings and add the brain.cmd shim"
```

---

### Task 6: The path picker

**Files:**
- Create: `bin/brainlib/picker.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `os_family()` from Task 1.
- Produces:
  - `candidates(home: Path, cwd: Path) -> list` — list of `(path, label)` tuples, recommended first
  - `reject_reason(path: Path) -> str` — `""` when acceptable, otherwise a sentence saying why
  - `expand(raw: str, home: Path, cwd: Path) -> Path` — `~` expansion and absolute resolution
  - `choose(home, cwd, stream=None, default=None) -> Path` — the interactive loop; returns `default` when `stream` is not a TTY

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
import io
import tempfile

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import picker  # noqa: E402


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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'brainlib.picker'`

- [x] **Step 3: Write minimal implementation**

```python
# bin/brainlib/picker.py
"""Choosing where the brain lives.

A raw POSIX `read` has no line editing, no completion and no history: an
operator pressing ^R at the old prompt had it swallowed as literal text. This
does the small, safe version of better — a numbered shortlist plus a typed
path with completion — and deliberately not a full-screen browser, which would
need raw terminal mode and a non-TTY fallback anyway.
"""
import os
import sys
from pathlib import Path

from . import osbackend

# Cloud roots worth offering, with the consequence of choosing one. Offered
# ONLY when the directory actually exists — listing a Dropbox folder to
# somebody who has no Dropbox is noise that makes the real options harder to see.
_CLOUD = [
    ("Library/Mobile Documents/com~apple~CloudDocs",
     "iCloud Drive — syncs across your Macs"),
    ("OneDrive", "OneDrive — syncs across your Windows machines"),
    ("Dropbox", "Dropbox — syncs across your devices"),
]


def candidates(home, cwd) -> list:
    """(path, label) pairs, recommended first."""
    home, cwd = Path(home), Path(cwd)
    found = [(home / "brain", "recommended"),
             (home / "Documents" / "brain", "")]
    for suffix, label in _CLOUD:
        root = home / suffix
        if root.is_dir():
            found.append((root / "brain", label))
    if cwd != home:
        found.append((cwd / "brain", "here, in the current directory"))
    return found


def reject_reason(path) -> str:
    """'' when this path can be installed into, else a sentence saying why not.

    The old installer said only "already exists and is not empty". Naming the
    count is what turns a refusal into something the reader can act on.
    """
    path = Path(path)
    if path.is_file():
        return f"{path} is a file, not a directory"
    if not path.exists():
        return ""
    try:
        entries = list(path.iterdir())
    except OSError as exc:
        return f"cannot read {path}: {exc}"
    if entries:
        names = ", ".join(sorted(e.name for e in entries)[:3])
        return (f"that directory has {len(entries)} item(s) in it ({names}"
                f"{', …' if len(entries) > 3 else ''}) — installing over it would "
                "mix your brain into somebody else's files")
    return ""


def expand(raw: str, home, cwd) -> Path:
    """Expand ~ and resolve relative paths WITHOUT requiring them to exist.

    Path.expanduser() reads the environment's HOME, which the tests redirect
    and the caller may not control, so home is passed in explicitly.
    """
    home, cwd, raw = Path(home), Path(cwd), raw.strip()
    if raw == "~":
        return home
    if raw.startswith("~/") or raw.startswith("~\\"):
        return home / raw[2:]
    path = Path(raw)
    return path if path.is_absolute() else cwd / path


def _enable_completion() -> None:
    """Tab completion for typed paths, when the platform has readline.

    Windows has no readline in the standard library and pyreadline is a third
    party dependency this project does not take, so completion is simply
    absent there. Everything else still works.
    """
    try:
        import readline
    except ImportError:
        return

    def complete(text, state):
        stub = Path(text).expanduser()
        directory = stub.parent if text.endswith(os.sep) is False else stub
        try:
            options = [str(p) + (os.sep if p.is_dir() else "")
                       for p in directory.iterdir()
                       if str(p).startswith(str(stub))]
        except OSError:
            return None
        return options[state] if state < len(options) else None

    readline.set_completer_delims(" \t\n")
    readline.parse_and_bind("tab: complete")
    readline.set_completer(complete)


def choose(home, cwd, stream=None, default=None) -> Path:
    """Ask, and keep asking until the answer is usable.

    Returns `default` immediately when there is no terminal — piped into `sh`,
    stdin is the script rather than the user, and blocking there is the bug
    this exists to avoid.
    """
    home, cwd = Path(home), Path(cwd)
    options = candidates(home, cwd)
    default = Path(default) if default else options[0][0]
    stream = stream if stream is not None else sys.stdin

    if not (hasattr(stream, "isatty") and stream.isatty()):
        return default

    _enable_completion()
    while True:
        print("\nWhere should your brain live?\n")
        for i, (path, label) in enumerate(options, 1):
            shown = str(path).replace(str(home), "~", 1)
            print(f"  {i}  {shown:<34}{('(' + label + ')') if label else ''}")
        print(f"  {len(options) + 1}  type a path\n")

        raw = stream.readline().strip()
        if not raw:
            chosen = default
        elif raw.isdigit() and 1 <= int(raw) <= len(options):
            chosen = options[int(raw) - 1][0]
        elif raw.isdigit() and int(raw) == len(options) + 1:
            print("  Path: ", end="", flush=True)
            typed = stream.readline().strip()
            if not typed:
                continue
            chosen = expand(typed, home, cwd)
        else:
            chosen = expand(raw, home, cwd)

        reason = reject_reason(chosen)
        if not reason:
            return chosen
        print(f"\n  Cannot use that: {reason}")
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 14 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/picker.py tests/test_setup.py
git commit -m "picker: shortlist plus typed path with completion and reasoned rejections"
```

---

### Task 7: Phase framework and the `--json` contract

The shape every phase returns, and the machine-readable output an agent acts on.

**Files:**
- Create: `bin/brainlib/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Result` — a class with `.status` (`"ok"`/`"skipped"`/`"failed"`), `.detail: str`, `.remedy: str`, and `.as_dict() -> dict`
  - `PHASES: tuple` — `("check", "place", "create", "backup", "verify")`
  - `render_json(results: dict) -> str`
  - `overall_status(results: dict) -> str`

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
import json

from brainlib import setup as setupmod  # noqa: E402


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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'brainlib.setup'`

- [x] **Step 3: Write minimal implementation**

```python
# bin/brainlib/setup.py
"""First-run setup: the phases, and the contract an agent reads.

One implementation serves a human at a terminal and an agent running headless.
Two implementations would mean the agent path rots silently, because nobody
exercises it daily — and most people will hand this whole thing to an agent.
"""
import json

PHASES = ("check", "place", "create", "backup", "verify")

_STATUSES = ("ok", "skipped", "failed")


class Result:
    """One phase's outcome.

    `remedy` is mandatory on failure and must be a literal command wherever one
    exists. It is the field an agent acts on, so a failure without it leaves
    the agent with nothing to do and the user with nothing to read.
    """

    def __init__(self, status: str, detail: str, remedy: str = ""):
        if status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {status!r}")
        if status == "failed" and not remedy:
            raise ValueError("a failed Result must carry a remedy")
        self.status = status
        self.detail = detail
        self.remedy = remedy

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "remedy": self.remedy}


def overall_status(results: dict) -> str:
    """'failed' if anything failed, else 'ok'.

    A skip is not a failure: no remote and no optional tools are legitimate,
    fully working outcomes and must not be reported as a broken install.
    """
    return "failed" if any(r.status == "failed" for r in results.values()) else "ok"


def render_json(results: dict) -> str:
    return json.dumps({
        "status": overall_status(results),
        "phases": {name: results[name].as_dict()
                   for name in PHASES if name in results},
    }, indent=2)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 19 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/setup.py tests/test_setup.py
git commit -m "setup: phase result type and the --json contract"
```

---

### Task 8: The check phase

**Files:**
- Modify: `bin/brainlib/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `PREREQS`, `install_hint` (Task 1); `Result` (Task 7).
- Produces: `phase_check(which=None) -> Result` — `which` is an injectable `shutil.which`-alike so tests can simulate a bare machine.

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `AttributeError: module 'brainlib.setup' has no attribute 'phase_check'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/setup.py`:

```python
import shutil

from . import osbackend


def phase_check(which=None, run=None) -> Result:
    """Report what is missing and what it costs. Install NOTHING.

    `run` is accepted only so a test can prove it is never called: a
    piped-curl script that installs system packages unprompted assumes more
    trust than this should, and corporate machines forbid it outright.
    """
    which = which or shutil.which
    missing_hard, missing_soft = [], []
    for tool, spec in osbackend.PREREQS.items():
        if tool == "python3":
            continue                    # we are running on it
        if which(tool):
            continue
        (missing_hard if spec["hard"] else missing_soft).append(tool)

    if missing_hard:
        lines = []
        for tool in missing_hard:
            hint = osbackend.install_hint(tool)
            lines.append(f"{tool} — {osbackend.PREREQS[tool]['why']}"
                         + (f"\n    install it with:  {hint}" if hint else ""))
        remedy = "; ".join(filter(None, (osbackend.install_hint(t)
                                         for t in missing_hard))) \
            or f"install: {', '.join(missing_hard)}"
        return Result("failed", "missing required tool(s):\n  " + "\n  ".join(lines),
                      remedy=remedy)

    if missing_soft:
        lines = []
        for tool in missing_soft:
            hint = osbackend.install_hint(tool)
            lines.append(f"{tool} not installed — {osbackend.PREREQS[tool]['why']}"
                         + (f"\n    add it later with:  {hint}" if hint else ""))
        return Result("ok", "everything required is present.\n  "
                      + "\n  ".join(lines))

    return Result("ok", "every prerequisite is present")
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 23 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/setup.py tests/test_setup.py
git commit -m "setup: check phase reports consequences and never installs"
```

---

### Task 9: The backup phase — the regression that started this

The failure from 2026-07-25. Determine truth by inspecting git, never by an exit code.

**Files:**
- Modify: `bin/brainlib/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `Result` (Task 7).
- Produces: `phase_backup(dest: Path, repo_name: str, want_remote: bool, run=None, which=None) -> Result`

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
import subprocess


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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `AttributeError: module 'brainlib.setup' has no attribute 'phase_backup'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/setup.py`:

```python
import subprocess
from pathlib import Path


def _git_out(repo, *args) -> str:
    done = subprocess.run(["git", *args], cwd=str(repo),
                          capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else ""


def phase_backup(dest, repo_name: str, want_remote: bool,
                 run=None, which=None) -> Result:
    """Set up the private remote, and report what is ACTUALLY true afterwards.

    On 2026-07-25 this was read from `gh`'s exit code. `gh repo create
    --source . --remote origin --push` adds the remote BEFORE it pushes, so a
    push failure returns non-zero with a remote sitting right there. The
    installer announced 'no remote yet — LOCAL ONLY', its own visibility check
    then read git and said the opposite, and the run ended with doctor RED.

    Nothing below trusts an exit code for the question 'is there a remote'.
    Git is asked, every time.
    """
    dest = Path(dest)
    run = run or (lambda argv, **kw: subprocess.run(
        argv, cwd=str(dest), capture_output=True, text=True))
    which = which or shutil.which

    origin = _git_out(dest, "remote", "get-url", "origin")

    if want_remote and not origin and which("gh"):
        run(["gh", "repo", "create", repo_name, "--private",
             "--source", ".", "--remote", "origin", "--push"])
        # Ask git, not gh. This line is the fix.
        origin = _git_out(dest, "remote", "get-url", "origin")

    if not origin:
        return Result(
            "skipped",
            "no remote — your notes exist on this machine only and are not backed up",
            remedy=f"gh repo create {repo_name} --private --source . --push")

    upstream = _git_out(dest, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{u}")
    if not upstream:
        branch = _git_out(dest, "rev-parse", "--abbrev-ref", "HEAD") or "main"
        pushed = subprocess.run(["git", "push", "-u", "origin", branch],
                                cwd=str(dest), capture_output=True, text=True)
        if pushed.returncode != 0:
            lines = (pushed.stderr or "").strip().splitlines()
            reason = lines[-1] if lines else "git push failed"
            return Result(
                "failed",
                f"the remote {origin} exists but nothing has been pushed to it, so "
                f"nothing is backed up: {reason}",
                remedy=f"cd {dest} && git push -u origin {branch}")

    return Result("ok", f"backed up to {origin}")
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 27 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/setup.py tests/test_setup.py
git commit -m "setup: backup phase reads git state instead of trusting gh's exit code

Fixes the 2026-07-25 install that reported 'no remote yet — LOCAL ONLY' while
a remote existed, then left doctor RED because nothing was ever pushed."
```

---

### Task 10: The place, create and verify phases

**Files:**
- Modify: `bin/brainlib/setup.py`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `picker.choose`, `picker.reject_reason` (Task 6); `Result` (Task 7).
- Produces:
  - `phase_place(home, cwd, requested=None, stream=None) -> tuple` → `(Result, Path)`
  - `phase_create(source: Path, dest: Path) -> Result`
  - `phase_verify(dest: Path, run=None) -> Result`

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
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


class TestCreatePhase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.source = self.base / "template"
        (self.source / "knowledge").mkdir(parents=True)
        (self.source / "knowledge" / "index.md").write_text("# i", encoding="utf-8")
        (self.source / ".githooks").mkdir()
        (self.source / ".githooks" / "pre-commit").write_text("#!/bin/sh\n",
                                                              encoding="utf-8")

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
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL with `AttributeError: module 'brainlib.setup' has no attribute 'phase_place'`

- [x] **Step 3: Write minimal implementation**

Append to `bin/brainlib/setup.py`:

```python
from . import picker


def phase_place(home, cwd, requested=None, stream=None) -> tuple:
    """Decide where the brain goes. Returns (Result, path)."""
    home, cwd = Path(home), Path(cwd)
    if requested:
        chosen = picker.expand(str(requested), home, cwd)
        reason = picker.reject_reason(chosen)
        if reason:
            return Result("failed", reason,
                          remedy="choose a different path with --dir <path>"), chosen
        return Result("ok", f"installing to {chosen}"), chosen
    chosen = picker.choose(home, cwd, stream=stream)
    return Result("ok", f"installing to {chosen}"), chosen


def phase_create(source, dest) -> Result:
    """Copy the template and give it a git history that belongs to its owner.

    The template's history is the PRODUCT's history, not yours. Starting fresh
    is also what GitHub's 'Use this template' button does, and nothing is lost:
    toolbelt updates come across by adding the template as a second remote and
    checking out paths, which needs no shared ancestry.
    """
    source, dest = Path(source), Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        for item in source.iterdir():
            if item.name in {".git", ".cache", "__pycache__", "install.sh",
                             "install.ps1", ".claude-plugin", "plugins"}:
                continue
            target = dest / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True)
            else:
                shutil.copy2(item, target)
    except OSError as exc:
        return Result("failed", f"could not copy the template: {exc}",
                      remedy=f"check that {dest} is writable, then run setup again")

    made = subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(dest),
                          capture_output=True, text=True)
    if made.returncode != 0:
        # git < 2.28 has no -b. Fall back rather than demanding an upgrade.
        subprocess.run(["git", "init", "-q"], cwd=str(dest), capture_output=True)
        subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"],
                       cwd=str(dest), capture_output=True)

    subprocess.run(["git", "config", "core.hooksPath", ".githooks"],
                   cwd=str(dest), capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=str(dest), capture_output=True)
    # The gate has nothing to check on an empty history and its own tooling is
    # not wired yet, so this one commit bypasses it deliberately.
    first = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "commit",
                            "-q", "-m", "brain: start"],
                           cwd=str(dest), capture_output=True, text=True)
    if first.returncode != 0:
        return Result("failed",
                      "could not make the first commit — git needs a name and email",
                      remedy='git config --global user.name "You" && '
                             'git config --global user.email "you@example.com"')
    return Result("ok", f"created {dest} with a fresh history on main")


def phase_verify(dest, run=None) -> Result:
    """Run doctor and let its verdict be setup's verdict.

    A fresh install that finishes in a state its own health check calls red is
    the exact failure this whole plan exists to remove, so doctor's result is
    not advisory here.
    """
    dest = Path(dest)
    run = run or (lambda argv: subprocess.run(argv, cwd=str(dest),
                                              capture_output=True, text=True))
    done = run([sys.executable, str(dest / "bin" / "brain"), "doctor"])
    output = (getattr(done, "stdout", "") or "") + (getattr(done, "stderr", "") or "")
    if getattr(done, "returncode", 1) != 0 or "[RED]" in output:
        return Result("failed", "doctor reported a problem:\n" + output.strip(),
                      remedy=f"cd {dest} && bin/brain doctor")
    return Result("ok", "doctor is green")
```

Add `import sys` to the module's imports.

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 32 tests

- [x] **Step 5: Commit**

```bash
git add bin/brainlib/setup.py tests/test_setup.py
git commit -m "setup: place, create and verify phases"
```

---

### Task 11: Wire `brain setup` into the CLI

**Files:**
- Modify: `bin/brainlib/setup.py` (add `run_setup`)
- Modify: `bin/brain` (module docstring, `main()` dispatch, `cmd_init` becomes internal)
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: every phase from Tasks 8–10.
- Produces: `run_setup(argv: list, home=None, cwd=None, source=None) -> int`

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
BRAIN = ROOT / "bin" / "brain"


def run_brain_cmd(*args, cwd=None, env=None):
    import os as _os
    environ = dict(_os.environ)
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

    def test_json_mode_emits_only_json_on_stdout(self):
        done = run_brain_cmd("setup", "--json", "--yes", "--only", "check")
        json.loads(done.stdout)      # must parse — human text belongs on stderr

    def test_check_only_never_writes_anything(self):
        before = sorted(p.name for p in ROOT.iterdir())
        run_brain_cmd("setup", "--yes", "--only", "check")
        self.assertEqual(before, sorted(p.name for p in ROOT.iterdir()))
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL — `unknown command 'setup'`

- [x] **Step 3: Write minimal implementation**

Append `run_setup` to `bin/brainlib/setup.py`:

```python
def run_setup(argv: list, home=None, cwd=None, source=None) -> int:
    """The whole first run. Interactive with a terminal, silent without one.

    Human text goes to stderr and machine output to stdout, so `--json` can be
    parsed by an agent without stripping anything first.
    """
    home = Path(home) if home else Path.home()
    cwd = Path(cwd) if cwd else Path.cwd()
    source = Path(source) if source else Path(__file__).resolve().parent.parent.parent

    as_json = "--json" in argv
    only = None
    if "--only" in argv:
        only = argv[argv.index("--only") + 1]
    requested = None
    if "--dir" in argv:
        requested = argv[argv.index("--dir") + 1]
    repo_name = "my-brain"
    if "--repo" in argv:
        repo_name = argv[argv.index("--repo") + 1]
    want_remote = "--no-repo" not in argv

    def say(text=""):
        if not as_json:
            print(text, file=sys.stderr)

    results, dest = {}, None
    wanted = (only,) if only else PHASES
    if only and only not in PHASES:
        print(f"unknown phase {only!r} — one of: {', '.join(PHASES)}", file=sys.stderr)
        return 2
    # Every phase after `place` needs a destination. Running one of them alone
    # is a legitimate repair, so ask where the brain is rather than crashing on
    # a None that came from a phase that was never run.
    if only in ("create", "backup", "verify") and not requested:
        print("--only " + only + " needs --dir <path>", file=sys.stderr)
        return 2
    if requested:
        dest = picker.expand(str(requested), home, cwd)

    for name in PHASES:
        if name not in wanted:
            continue
        if name == "check":
            results[name] = phase_check()
        elif name == "place":
            results[name], dest = phase_place(home, cwd, requested=requested)
        elif name == "create":
            results[name] = phase_create(source, dest)
        elif name == "backup":
            results[name] = phase_backup(dest, repo_name, want_remote)
        elif name == "verify":
            results[name] = phase_verify(dest)

        result = results[name]
        say(f"  [{result.status:<7}] {name}: {result.detail}")
        if result.status == "failed":
            say(f"            fix: {result.remedy}")
            break

    if as_json:
        print(render_json(results))
    elif dest and overall_status(results) == "ok":
        say(f"\n  Your brain is at {dest}. It works and it is backed up.")
        say("\n  Next: let your agents reach it —  brain connect")
        say("\n  Only using this on this computer? That is everything.")
        say("  Reaching it from other devices is a separate, optional step: "
            "brain serve --help")

    return 0 if overall_status(results) == "ok" else 1
```

In `bin/brain`: add `setup` to the module docstring's command list, add the dispatch branch in `main()`, and change `init` from a public command to one that prints a one-line redirect to `setup` while still performing the wiring (it remains the documented repair in AGENTS.md and CLAUDE.md):

```python
    if cmd == "setup":
        from brainlib.setup import run_setup
        return run_setup(rest)
```

- [x] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 36 tests

- [x] **Step 5: Run the whole suite**

Run: `python3 -m unittest discover -s tests`
Expected: PASS — all 213 original plus the new ones

- [x] **Step 6: Commit**

```bash
git add bin/brainlib/setup.py bin/brain tests/test_setup.py
git commit -m "setup: wire brain setup into the CLI with --json, --only and --dir"
```

---

### Task 12: The two bootstraps and the CI matrix

**Files:**
- Modify: `install.sh` (321 lines → bootstrap only)
- Create: `install.ps1`
- Modify: `.github/workflows/gate.yml`
- Test: `tests/test_setup.py`

**Interfaces:**
- Consumes: `brain setup` from Task 11.
- Produces: no Python symbols.

- [x] **Step 1: Write the failing test**

```python
# append to tests/test_setup.py
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


class TestCiMatrix(unittest.TestCase):
    def test_all_three_platforms_run_the_tests(self):
        text = (ROOT / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
        for runner in ("ubuntu-latest", "macos-latest", "windows-latest"):
            self.assertIn(runner, text,
                          "Windows support is CI-verified or it is not verified")
```

- [x] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_setup -v`
Expected: FAIL — `install.ps1` does not exist, and `install.sh` still contains `Repository name`

- [x] **Step 3: Rewrite `install.sh` as a bootstrap**

Keep the header comment, the colour helpers and the flag parsing. Replace everything from "Checking prerequisites" onward with: verify `git` and `python3 >= 3.9` only, clone the template to a temp dir, and `exec python3 "$TMP/bin/brain" setup "$@"`. Delete the destination prompt, the `gh` block, the visibility check and the next-steps text — all of that now lives in `brainlib/setup.py`, where it is tested and cross-platform. Target: under 120 lines.

- [x] **Step 4: Write `install.ps1`**

```powershell
# brain — one-command install for Windows.
#
#   irm https://raw.githubusercontent.com/Cazy00/brain/main/install.ps1 | iex
#
# A bootstrap and nothing more: verify git and python, fetch the template, and
# hand off to `brain setup`, which is the same code path macOS and Linux use.
# Anything interactive lives there so all three platforms behave identically.

$ErrorActionPreference = "Stop"

function Need($name, $hint) {
    if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
        Write-Error "$name is required. Install it with:  $hint"
        exit 1
    }
}

Need git    "winget install --id Git.Git"
Need python "winget install --id Python.Python.3.12"

$version = & python -c "import sys; print(1 if sys.version_info[:2] >= (3,9) else 0)"
if ($version.Trim() -ne "1") {
    Write-Error "python 3.9 or newer is required."
    exit 1
}

$repo = if ($env:BRAIN_TEMPLATE_REPO) { $env:BRAIN_TEMPLATE_REPO } else { "Cazy00/brain" }
$temp = Join-Path $env:TEMP ("brain-install-" + [System.Guid]::NewGuid().ToString("N"))

git clone --quiet --depth 1 "https://github.com/$repo.git" $temp
if ($LASTEXITCODE -ne 0) { Write-Error "could not clone https://github.com/$repo.git"; exit 1 }

try {
    & python (Join-Path $temp "bin\brain") setup @args
    exit $LASTEXITCODE
} finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
```

- [x] **Step 5: Extend the CI matrix**

In `.github/workflows/gate.yml`, change the `tests` job:

```yaml
  tests:
    if: github.event_name == 'push'
    strategy:
      fail-fast: false
      matrix:
        os: [ubuntu-latest, macos-latest, windows-latest]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.9"
      - name: runtime tests
        run: python -m unittest discover -s tests -v
```

Leave the `gate` job on `ubuntu-latest` — lint and gitleaks are platform-independent and running them three times buys nothing.

- [x] **Step 6: Run test to verify it passes**

Run: `python3 -m unittest tests.test_setup -v`
Expected: PASS, 40 tests

- [x] **Step 7: Prove the bootstrap end to end**

```bash
sh install.sh --dir /tmp/brain-smoke --no-repo --yes
```
Expected: completes, `/tmp/brain-smoke/bin/brain doctor` exits 0 with no `[RED]`. Then `rm -rf /tmp/brain-smoke`.

- [x] **Step 8: Commit**

```bash
git add install.sh install.ps1 .github/workflows/gate.yml tests/test_setup.py
git commit -m "install: shrink install.sh to a bootstrap, add install.ps1, test on three platforms"
```

---

## Self-review

**Spec coverage for stages 1–3.** `brain setup` phases → Tasks 7–11. Prerequisites with consequences, never installing → Task 8. Path picker → Task 6. The backup regression → Task 9. `--json`/`--yes`/`--only` → Tasks 7 and 11. Scheduler, keystore and link backends → Tasks 2, 3, 4. `.gitattributes`, `brain.cmd` → Task 5. Bootstraps → Task 12. CI matrix → Task 12. Setup's closing text naming `connect` → Task 11.

Deliberately deferred, with the plan that covers each: `connect --apply` and routing markers → Plan 2. `retire` → Plan 2. README and SETUP.md → Plan 2. `serve` → Plan 3.

**Known gap — CLOSED in Task 2.** `cmd_doctor` was reading `PLIST_DIR` directly, so on Linux and Windows it would report schedules as absent rather than asking `osbackend.scheduler().status()`. It now asks the backend; the only remaining mention of `PLIST_DIR` in `bin/brain` is a comment.

## Completion — 2026-07-29

All twelve tasks landed. `python3 -m unittest discover -s tests` is 336 tests, green apart from one environment-specific failure unrelated to this plan (`test_init_defers_when_claude_cli_absent` builds a PATH with no `claude` on it and asserts its own sandbox; on a machine where `claude` is installed into `/usr/bin` that assertion correctly refuses to run).

Two deviations from what is written above, both deliberate:

- **CI pins 3.9 on ubuntu and windows, 3.11 on macOS.** Task 12 Step 5 pins 3.9 everywhere; `macos-latest` is arm64 and no 3.9 build exists for it, so that runner would fail to start rather than run the suite. The syntax floor is still guarded by the other two.
- **Task 12 Step 7's smoke ends non-zero, and that is correct.** `sh install.sh --dir … --no-repo --yes` builds a working brain — capture, search, lint and the commit gate all verified inside it — and then `verify` fails, because `--no-repo` means no remote and `cmd_doctor` calls a brain with no off-machine copy `[RED]`. It is the only RED in the run. The step's "doctor exits 0 with no [RED]" cannot hold for a local-only install and never could; the design requirement it conflicts with ("setup's exit code reflects doctor's, so a red install is a failed install") wins.

  Worth deciding in Plan 2: a user who explicitly passed `--no-repo` is being told their install FAILED, when what is true is that it succeeded and they gave up their backup. The exit code is right. The word is not.

**Type consistency.** `Result(status, detail, remedy)` is used identically in every phase. `phase_place` alone returns `(Result, Path)` — noted in its Interfaces block because it breaks the pattern. `link_dir` returns `(method, message)`, distinct from `Result` on purpose: it is an `osbackend` primitive with no notion of setup phases.
