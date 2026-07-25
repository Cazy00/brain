# bin/brainlib/osbackend.py
"""Per-OS backends: platform detection and the prerequisite table.

bin/brain called `launchctl` and `security` inline, which is the entire reason
it ran on macOS only. Everything machine-specific belongs here, behind one
interface per concern, so the rest of the toolbelt can stay platform-blind.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

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

    def install(self, name: str, argv: list, when: dict, cwd: str = None,
                env: dict = None, log: str = None) -> str:
        return ("no scheduler available on this platform — run "
                "`brain consolidate` by hand, or wire your own cron entry")

    def uninstall(self, name: str) -> str:
        return "no scheduler available on this platform — nothing to remove"

    def status(self, name: str) -> str:
        return "no scheduler available on this platform"

    def serves(self, name: str, marker: str) -> bool:
        # Nothing is ever installed on an unavailable backend, so it cannot
        # serve any repo. A graceful False here — mirroring available() and
        # status() — is what lets a caller ask "is this scheduled HERE"
        # without a platform check guarding every call site.
        return False


class LaunchdScheduler(Scheduler):
    kind = "launchd"

    def __init__(self):
        self.agents = Path.home() / "Library" / "LaunchAgents"

    def available(self) -> bool:
        return bool(shutil.which("launchctl"))

    def plist_path(self, name: str) -> Path:
        return self.agents / f"com.secondbrain.{name}.plist"

    def render(self, name: str, argv: list, when: dict, cwd: str = None,
               env: dict = None, log: str = None) -> str:
        args = "".join(f"      <string>{a}</string>\n" for a in argv)
        cal = f"      <key>Hour</key><integer>{when['hour']}</integer>\n"
        cal += f"      <key>Minute</key><integer>{when['minute']}</integer>\n"
        if "weekday" in when:
            # launchd's Weekday is 0-6 with Sunday=0; ours is 1-7 with Monday=1.
            cal += (f"      <key>Weekday</key><integer>"
                    f"{when['weekday'] % 7}</integer>\n")
        # cwd/env/log are optional so a bare-bones caller can still get a
        # minimal plist — but a launchd agent starts with almost nothing (a
        # stripped PATH, no working directory, output to /dev/null), so any
        # REAL job needs at least the PATH shim below or it silently cannot
        # find Homebrew-installed tools like gitleaks/age. This block is what
        # setup/schedules/*.plist.template used to hold; folding it in here
        # means there is exactly one place that knows what a brain plist
        # contains, rather than a template AND a renderer that can drift.
        extra = ""
        if cwd:
            extra += f'    <key>WorkingDirectory</key><string>{cwd}</string>\n'
        if env:
            pairs = "".join(f"      <key>{k}</key><string>{v}</string>\n"
                            for k, v in env.items())
            extra += ('    <key>EnvironmentVariables</key>\n    <dict>\n'
                      f'{pairs}    </dict>\n')
        if log:
            extra += (f'    <key>StandardOutPath</key><string>{log}</string>\n'
                       f'    <key>StandardErrorPath</key><string>{log}</string>\n')
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<plist version="1.0">\n  <dict>\n'
                f'    <key>Label</key><string>com.secondbrain.{name}</string>\n'
                f'    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n'
                f'{extra}'
                f'    <key>StartCalendarInterval</key>\n    <dict>\n{cal}    </dict>\n'
                '  </dict>\n</plist>\n')

    def install(self, name: str, argv: list, when: dict, cwd: str = None,
                env: dict = None, log: str = None) -> str:
        if not self.available():
            return super().install(name, argv, when, cwd, env, log)
        path = self.plist_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(name, argv, when, cwd, env, log), encoding="utf-8")
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

    def serves(self, name: str, marker: str) -> bool:
        """Does the installed plist mention `marker` (a repo path)?

        The label is machine-global, so merely finding the plist proves
        nothing — a brain that has been moved, renamed, or is simply the
        second one on this machine would see another brain's job and report
        itself scheduled as its own.
        """
        try:
            text = self.plist_path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return marker in text


class SystemdScheduler(Scheduler):
    kind = "systemd"

    def __init__(self):
        self.units = Path.home() / ".config" / "systemd" / "user"

    def available(self) -> bool:
        return bool(shutil.which("systemctl"))

    def render_units(self, name: str, argv: list, when: dict, cwd: str = None,
                      env: dict = None) -> tuple:
        exec_line = " ".join(argv)
        # Same bare-environment problem as launchd (a --user unit starts with
        # almost nothing) and the same fix — but WorkingDirectory=/Environment=
        # are ordinary [Service] keys, so unlike launchd there is no separate
        # template this duplicates; it was always going to live in code.
        extra = f"WorkingDirectory={cwd}\n" if cwd else ""
        if env:
            extra += "".join(f"Environment={k}={v}\n" for k, v in env.items())
        service = ("[Unit]\n"
                   f"Description=brain {name}\n\n"
                   "[Service]\nType=oneshot\n"
                   f"{extra}"
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

    def install(self, name: str, argv: list, when: dict, cwd: str = None,
                env: dict = None, log: str = None) -> str:
        if not self.available():
            return super().install(name, argv, when, cwd, env, log)
        self.units.mkdir(parents=True, exist_ok=True)
        service, timer = self.render_units(name, argv, when, cwd, env)
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

    def serves(self, name: str, marker: str) -> bool:
        """Does the installed unit mention `marker` (a repo path)? See
        LaunchdScheduler.serves — same reasoning, same machine-global name."""
        try:
            text = (self.units / f"{name}.service").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return False
        return marker in text


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

    def query_argv(self, name: str) -> list:
        # Split out from serves() for the same reason install_argv is split
        # from install(): it is the only part a non-Windows machine can check.
        return ["schtasks", "/query", "/tn", name, "/v", "/fo", "list"]

    def install(self, name: str, argv: list, when: dict, cwd: str = None,
                env: dict = None, log: str = None) -> str:
        # cwd/env/log are accepted for interface parity but unused: a
        # Scheduled Task runs with the invoking user's normal profile and PATH
        # already loaded, so there is no bare environment to compensate for —
        # and schtasks' simple /create form has no flag to set either even if
        # there were.
        if not self.available():
            return super().install(name, argv, when, cwd, env, log)
        done = subprocess.run(self.install_argv(name, argv, when),
                              capture_output=True, text=True)
        if done.returncode != 0:
            return f"could not install: {(done.stderr or '').strip()[:200]}"
        return f"installed (Task Scheduler: {name})"

    def uninstall(self, name: str) -> str:
        # Same guard as install() above: subprocess.run raises an uncaught
        # FileNotFoundError when `schtasks` is not on PATH, which is every
        # non-Windows machine — including the macOS/Linux boxes doctor and
        # this test suite run on. available() is the same shutil.which check
        # install() already trusts, so a missing binary is the ordinary
        # "no scheduler here" case, not a crash.
        if not self.available():
            return super().uninstall(name)
        done = subprocess.run(["schtasks", "/delete", "/f", "/tn", name],
                              capture_output=True, text=True)
        return "removed" if done.returncode == 0 else "was not installed"

    def status(self, name: str) -> str:
        if not self.available():          # see uninstall() above
            return super().status(name)
        done = subprocess.run(["schtasks", "/query", "/tn", name],
                              capture_output=True, text=True)
        return "installed" if done.returncode == 0 else "not installed"

    def serves(self, name: str, marker: str) -> bool:
        if not self.available():          # see uninstall() above
            return super().serves(name, marker)
        done = subprocess.run(self.query_argv(name), capture_output=True, text=True)
        if done.returncode != 0:
            return False
        return marker in (done.stdout or "")


def scheduler_for(family: str) -> Scheduler:
    return {"macos": LaunchdScheduler, "linux": SystemdScheduler,
            "windows": SchtasksScheduler}.get(family, Scheduler)()


def scheduler() -> Scheduler:
    return scheduler_for(os_family())
