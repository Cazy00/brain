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
        # Same guard as install() above, and the same fix already applied to
        # SchtasksScheduler below: subprocess.run raises an uncaught
        # FileNotFoundError when the platform tool is missing. launchctl
        # ships with every real Mac, so this mainly guards a stripped-down or
        # sandboxed environment reporting itself as macOS — but install()
        # already refuses to assume that can't happen, and this class's own
        # base (Scheduler) promises unavailable is a normal state everywhere,
        # not just in install().
        if not self.available():
            return super().uninstall(name)
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
        # Same guard as install() above — see LaunchdScheduler.uninstall()
        # for the full reasoning. A Linux box without systemd (minimal
        # containers, some embedded distros, WSL1) would otherwise hit an
        # uncaught FileNotFoundError here instead of the graceful sentence
        # every other unavailable path on this class already returns.
        if not self.available():
            return super().uninstall(name)
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
    """UNVERIFIED ON A REAL MACHINE, as of 2026-07-29.

    Nobody on this project owns Windows. CI runs the whole suite on
    windows-latest, so the argv this builds is checked — but a scheduled task
    that CI never installs, fires, or reads back is not the same as one known
    to work. Anything here that only shows up when a task actually runs at 3am
    is untested. Tracked in docs/superpowers/BACKLOG.md; the same caveat
    applies to CredmanKeystore below.
    """

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


def linger_state(user: str = None, runner=None) -> str:
    """'on' | 'off' | 'unknown' | 'n/a' — does this user's systemd survive logout?

    The trap this exists to catch is silent and specific to `systemd --user`,
    which is what Linux boxes get. With lingering OFF, every user unit is
    stopped the moment your last session ends. On a rented VM that means:
    you SSH in, `brain schedule install`, log out — and the nightly doctor and
    the weekly consolidation never run again, with nothing anywhere saying so.
    A long-running `brain serve` dies the same way.

    `loginctl enable-linger <user>` fixes it, and it needs root, which is why
    this reports rather than repairs.

    'n/a' on platforms with no such concept (macOS LaunchAgents and Windows
    scheduled tasks both survive logout on their own), so a caller can print
    nothing at all there rather than an irrelevant reassurance.
    """
    if os_family() != "linux":
        return "n/a"
    if not shutil.which("loginctl"):
        # A Linux box with no logind — a container, WSL1 — has no lingering to
        # enable and usually no systemd --user either. Unknown, not off: saying
        # "off" would send somebody chasing a setting that does not exist.
        return "unknown"
    runner = runner or subprocess.run
    user = user or os.environ.get("USER") or ""
    try:
        done = runner(["loginctl", "show-user", user, "-p", "Linger"],
                      capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if done.returncode != 0:
        return "unknown"
    value = (done.stdout or "").strip().lower()
    if value.endswith("=yes"):
        return "on"
    if value.endswith("=no"):
        return "off"
    return "unknown"


class Service:
    """A brain process the OS keeps ALIVE, as opposed to one it starts on a clock.

    `Scheduler` runs a job at a time and lets it finish. `brain serve` is the
    other shape: it must be running continuously, restart if it crashes, and
    come back after a reboot. Those are different enough that sharing one
    interface would mean a `when` parameter that a service ignores and a
    `Restart=` that a schedule cannot express.

    Same contract as Scheduler in every other respect: one interface, one
    implementation per platform, and every method returns a human sentence
    rather than raising, because a machine with no service manager is a normal
    state that everything else must still work on.
    """
    kind = "none"
    name = "brain-serve"

    def available(self) -> bool:
        return False

    def install(self, argv: list, cwd: str = None, env: dict = None) -> str:
        return ("no service manager available on this platform — run "
                "`brain serve` under whatever supervisor you already use "
                "(a terminal multiplexer will do; it will not survive a reboot)")

    def uninstall(self) -> str:
        return "no service manager available on this platform — nothing to remove"

    def status(self) -> str:
        return "no service manager available on this platform"

    def serves(self, marker: str) -> bool:
        return False


class SystemdService(Service):
    """A `systemd --user` unit. Read `linger_state` before trusting one."""
    kind = "systemd"

    def __init__(self, runner=None):
        self.units = Path.home() / ".config" / "systemd" / "user"
        # Injected so tests can assert on what would be run without running it.
        # Installing a real unit from a test would leave a service behind on
        # the developer's machine — and this one restarts itself forever.
        self._run = runner or subprocess.run

    def available(self) -> bool:
        return bool(shutil.which("systemctl"))

    def unit_path(self) -> Path:
        return self.units / f"{self.name}.service"

    def render_unit(self, argv: list, cwd: str = None, env: dict = None) -> str:
        import shlex
        # Quoted per argument. The scheduler's " ".join is fine for the fixed
        # argv it builds; this one carries operator input (--public-url, a
        # --source slug), and an unquoted space would silently truncate the
        # command into something that still starts and serves the wrong thing.
        # systemd accepts POSIX single-quoting, which is what shlex produces.
        exec_line = " ".join(shlex.quote(str(a)) for a in argv)
        extra = f"WorkingDirectory={cwd}\n" if cwd else ""
        if env:
            extra += "".join(f"Environment={k}={v}\n" for k, v in env.items())
        return ("[Unit]\n"
                "Description=brain serve — this brain over HTTP\n"
                # Not just network.target: a tunnel dialling out on boot needs
                # routing to actually be up, and Restart= would otherwise paper
                # over it with a crash loop nobody reads.
                "After=network-online.target\nWants=network-online.target\n\n"
                "[Service]\nType=simple\n"
                f"{extra}"
                f"ExecStart={exec_line}\n"
                # The whole point of a service rather than a shell command.
                "Restart=always\nRestartSec=5\n"
                "NoNewPrivileges=true\n\n"
                # default.target, not multi-user.target: this is a --user unit.
                "[Install]\nWantedBy=default.target\n")

    def install(self, argv: list, cwd: str = None, env: dict = None) -> str:
        if not self.available():
            return Service.install(self, argv, cwd, env)
        try:
            self.units.mkdir(parents=True, exist_ok=True)
            self.unit_path().write_text(self.render_unit(argv, cwd, env),
                                        encoding="utf-8")
        except OSError as exc:
            return f"could not write {self.unit_path()} — {exc}"
        self._run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        done = self._run(["systemctl", "--user", "enable", "--now",
                          f"{self.name}.service"], capture_output=True, text=True)
        if getattr(done, "returncode", 0) != 0:
            return (f"unit written to {self.unit_path()} but systemd refused to "
                    f"start it — {(getattr(done, 'stderr', '') or '').strip()[:200]}")
        return f"installed and started ({self.unit_path()})"

    def uninstall(self) -> str:
        if not self.available():
            return Service.uninstall(self)
        self._run(["systemctl", "--user", "disable", "--now",
                   f"{self.name}.service"], capture_output=True)
        existed = self.unit_path().exists()
        if existed:
            try:
                self.unit_path().unlink()
            except OSError as exc:
                return f"could not remove {self.unit_path()} — {exc}"
        self._run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "removed" if existed else "was not installed"

    def status(self) -> str:
        if not self.unit_path().exists():
            return "not installed"
        if not self.available():
            return "installed"
        done = self._run(["systemctl", "--user", "is-active",
                          f"{self.name}.service"], capture_output=True, text=True)
        return ("running" if (getattr(done, "stdout", "") or "").strip() == "active"
                else "installed, not running")

    def serves(self, marker: str) -> bool:
        try:
            text = self.unit_path().read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return marker in text


class LaunchdService(Service):
    """A LaunchAgent with KeepAlive. Survives logout without any linger step."""
    kind = "launchd"

    def __init__(self, runner=None):
        self.agents = Path.home() / "Library" / "LaunchAgents"
        self._run = runner or subprocess.run

    def available(self) -> bool:
        return bool(shutil.which("launchctl"))

    def plist_path(self) -> Path:
        return self.agents / f"com.secondbrain.{self.name}.plist"

    def render_plist(self, argv: list, cwd: str = None, env: dict = None,
                     log: str = None) -> str:
        from xml.sax.saxutils import escape
        # Escaped, unlike the scheduler's fixed argv: --public-url is operator
        # input and a bare & would produce a plist launchd silently refuses.
        args = "".join(f"      <string>{escape(str(a))}</string>\n" for a in argv)
        extra = ""
        if cwd:
            extra += f"    <key>WorkingDirectory</key><string>{escape(cwd)}</string>\n"
        if env:
            pairs = "".join(f"      <key>{escape(k)}</key><string>{escape(str(v))}</string>\n"
                            for k, v in env.items())
            extra += ("    <key>EnvironmentVariables</key>\n    <dict>\n"
                      f"{pairs}    </dict>\n")
        if log:
            extra += (f"    <key>StandardOutPath</key><string>{escape(log)}</string>\n"
                      f"    <key>StandardErrorPath</key><string>{escape(log)}</string>\n")
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<plist version="1.0">\n  <dict>\n'
                f'    <key>Label</key><string>com.secondbrain.{self.name}</string>\n'
                f'    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n'
                f'{extra}'
                '    <key>RunAtLoad</key><true/>\n'
                '    <key>KeepAlive</key><true/>\n'
                '  </dict>\n</plist>\n')

    def install(self, argv: list, cwd: str = None, env: dict = None) -> str:
        if not self.available():
            return Service.install(self, argv, cwd, env)
        try:
            self.agents.mkdir(parents=True, exist_ok=True)
            self.plist_path().write_text(self.render_plist(argv, cwd, env),
                                         encoding="utf-8")
        except OSError as exc:
            return f"could not write {self.plist_path()} — {exc}"
        self._run(["launchctl", "unload", str(self.plist_path())],
                  capture_output=True)
        done = self._run(["launchctl", "load", "-w", str(self.plist_path())],
                         capture_output=True, text=True)
        if getattr(done, "returncode", 0) != 0:
            return (f"plist written to {self.plist_path()} but launchctl refused "
                    f"it — {(getattr(done, 'stderr', '') or '').strip()[:200]}")
        return f"installed and started ({self.plist_path()})"

    def uninstall(self) -> str:
        if not self.available():
            return Service.uninstall(self)
        self._run(["launchctl", "unload", "-w", str(self.plist_path())],
                  capture_output=True)
        if self.plist_path().exists():
            try:
                self.plist_path().unlink()
            except OSError as exc:
                return f"could not remove {self.plist_path()} — {exc}"
            return "removed"
        return "was not installed"

    def status(self) -> str:
        if not self.plist_path().exists():
            return "not installed"
        if not self.available():
            return "installed"
        done = self._run(["launchctl", "list"], capture_output=True, text=True)
        return ("running" if f"com.secondbrain.{self.name}" in
                (getattr(done, "stdout", "") or "") else "installed, not running")

    def serves(self, marker: str) -> bool:
        try:
            return marker in self.plist_path().read_text(encoding="utf-8",
                                                         errors="replace")
        except OSError:
            return False


def service_for(family: str) -> Service:
    """Windows deliberately gets the base class.

    A scheduled task is not a service manager: `schtasks` can start something
    at logon, but it will not restart it when it dies, which is the property
    this exists for. Claiming support and delivering a process that vanishes on
    its first exception is worse than saying plainly that this platform needs
    NSSM or a real Windows service, which the returned sentence does.
    """
    return {"macos": LaunchdService, "linux": SystemdService}.get(
        family, Service)()


def service() -> Service:
    return service_for(os_family())


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
    """Windows Credential Manager.

    cmdkey can WRITE a credential but famously cannot print one back out —
    reading needs `Get-StoredCredential` from the `CredentialManager`
    PowerShell module, which is NOT installed on a stock Windows box (nothing
    ships it, nothing installs it automatically; confirm this on the Windows
    CI runner in a later task — it cannot be checked from here). A get() that
    returns "" whenever that module simply happens to be missing would be
    indistinguishable from "no secret was ever stored" — for a vault key or a
    `serve` token, that misreading is not a shrug, it is silent data loss.

    So get/set/delete all consult the SAME probe (_module_available) before
    touching cmdkey or PowerShell: set()/delete() use it to choose where a
    value goes, and fall back to self.fallback (a FileKeystore) when it says
    no. If that were the whole story, a value written through cmdkey while
    only get() ever fell back to the file would still read back as "" later
    — same bug, one layer down.

    It is not the whole story, because _module_available() is re-evaluated on
    every call with no record of where a name was actually written. The
    module getting installed later is the ORDINARY way a stock Windows box
    ever acquires it — not a corner case — so "written while unavailable,
    read after becoming available" is a realistic sequence. get() therefore
    also falls through to self.fallback whenever the primary
    (Get-StoredCredential) lookup comes back empty, on the chance a set()
    that ran before the module appeared put it there instead.

    This is a ONE-WAY guarantee, not a symmetric one, and deliberately so:
    the opposite drift — written via cmdkey while the module WAS present,
    read after it is later removed — cannot be recovered by the same trick
    or any other available here. That set() had no reason to also write
    self.fallback (cmdkey succeeded), so there is no copy to fall through
    to, and a cmdkey-stored secret cannot be read back at all without the
    module. That direction stays a real, structural gap: named here, not
    fixed.

    delete() closes the matching gap on the writing side: once the module is
    available it clears BOTH self.fallback and cmdkey unconditionally,
    rather than picking whichever one looks live right now, because get()'s
    fallthrough above is exactly what would hand a stale fallback copy back
    out AFTER delete() reported success — "deleted, but still readable" is a
    revocation bypass, not a shrug, for a vault key or a `serve` token.
    Clearing both costs nothing when only one was ever written to: deleting
    against an absent name is a free no-op on either backend.

    delete() has its OWN one-way limit, mirroring get()'s: a value written
    via cmdkey while the module WAS available, deleted after the module is
    later removed, is not cleared from Credential Manager — the unavailable
    branch above only ever touches self.fallback, because that is the only
    place delete() COULD have written it if it had run earlier in that same
    unavailable state. This does not reopen the revocation-bypass risk the
    fix above closes: get() in that same unavailable state also only reads
    self.fallback, so the un-cleared cmdkey entry is unreadable through this
    class either, not silently returned. It is a real gap all the same — an
    orphaned OS-level credential this class can no longer see or remove —
    named here, not fixed, for the same reason the get() gap above is not.
    """
    kind = "credman"

    def __init__(self, fallback=None):
        # Injectable so tests can point the fallback at a temp directory
        # instead of this machine's real ~/.config/brain/secrets.
        self.fallback = fallback if fallback is not None else FileKeystore()

    def describe(self) -> str:
        if self._module_available():
            return "Windows Credential Manager"
        return ("Windows Credential Manager, but the CredentialManager "
                "PowerShell module is not installed, so reads and writes use "
                f"{self.fallback.describe()} instead")

    def _module_available(self) -> bool:
        # Get-Module -ListAvailable only inspects what is installed locally;
        # it never touches Credential Manager itself, so running it to
        # CHOOSE a backend is safe to call unconditionally. The `powershell`
        # PATH check comes first so a non-Windows machine (every dev box and
        # this test suite) answers False without spawning a process at all.
        if not shutil.which("powershell"):
            return False
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "if (Get-Module -ListAvailable -Name CredentialManager) "
             "{ exit 0 } else { exit 1 }"],
            capture_output=True)
        return done.returncode == 0

    def set_argv(self, name: str, value: str) -> list:
        return ["cmdkey", f"/generic:brain:{name}", "/user:brain", f"/pass:{value}"]

    def _primary_get(self, name: str) -> str:
        # cmdkey stores but will not print a secret back. PowerShell's
        # CredentialManager surface is the documented way to read one. Split
        # out from get() so a drift test can simulate "module available, but
        # nothing found there" by overriding this one method, without ever
        # invoking a real subprocess (see TestCredmanFallbackDrift).
        done = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-StoredCredential -Target 'brain:{name}')"
             ".GetNetworkCredential().Password"],
            capture_output=True, text=True)
        return done.stdout.strip() if done.returncode == 0 else ""

    def get(self, name: str) -> str:
        if not self._module_available():
            return self.fallback.get(name)
        primary = self._primary_get(name)
        if primary:
            return primary
        # The module is available NOW, but the value may have been written
        # by set() at a time when it was NOT (see the class docstring: this
        # drift is ordinary, not a corner case). A miss on the primary
        # lookup falls through to self.fallback on the chance set() put it
        # there, rather than being reported as absence. One-way: see the
        # class docstring for the direction this cannot help.
        return self.fallback.get(name)

    def set(self, name: str, value: str) -> bool:
        if not self._module_available():
            return self.fallback.set(name, value)
        return subprocess.run(self.set_argv(name, value),
                              capture_output=True).returncode == 0

    def _primary_delete(self, name: str) -> bool:
        # Split out from delete() for the same reason _primary_get is split
        # out of get(): a drift test needs to exercise the
        # _module_available()==True branch without ever invoking a real
        # subprocess (see TestCredmanDeleteDrift).
        return subprocess.run(["cmdkey", f"/delete:brain:{name}"],
                              capture_output=True).returncode == 0

    def delete(self, name: str) -> bool:
        if not self._module_available():
            return self.fallback.delete(name)
        # Clear BOTH backends once the module is available, not just cmdkey:
        # a value may have been written to self.fallback by a set() that ran
        # before the module appeared (see the class docstring), and get()'s
        # own fallthrough is exactly what would hand that stale copy back
        # AFTER this delete reported success — "deleted, but still
        # readable" is a revocation bypass for a vault key or a `serve`
        # token, not a shrug. Unlike set()'s single-path choice, this is not
        # a permanent trade-off: deleting against an absent fallback file is
        # free (FileKeystore.delete just returns False), so it costs nothing
        # on a machine that never touched the fallback at all.
        removed_native = self._primary_delete(name)
        removed_fallback = self.fallback.delete(name)
        return removed_native or removed_fallback


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


def state_dir(root, env=None, home=None, create: bool = False) -> Path:
    """Where THIS brain keeps machine-local state: `~/.local/state/brain/<name>`.

    Two properties, and both are the point rather than the convention:

    **Outside the repository.** The event log and the issued-token database go
    here, and the rule they have to satisfy is "never reaches git". A
    .gitignore line satisfies it only for as long as somebody maintains the
    pattern file and nobody runs `git add -f`; a path that is not inside the
    working tree satisfies it by geography. The secret gate stays as the
    backstop it was, rather than becoming the last line of defence.

    **Per brain, not per machine.** The business partition found two `brain
    serve` endpoints on one host sharing ONE keystore entry, so the read-only
    brain's token also opened the drop box — a real finding, from a real
    deployment, and the direct ancestor of this function. State keyed by the
    brain it belongs to cannot repeat it. The directory name carries a readable
    stem AND a digest of the resolved path: the stem so an operator can tell at
    a glance whose it is, the digest so two brains that are both called
    `brain` do not collide.

    `env` and `home` are parameters so tests never touch the real one. Created
    0700 at creation time rather than chmod-ed afterwards, for the same reason
    FileKeystore opens with the mode already set: the window between the two is
    exactly when a backup job runs.

    **`create` defaults to False, and that default is load-bearing.** Asking
    where state WOULD live is not the same as wanting it to exist: `doctor`,
    `logs` and `retire` all need the path in order to look, and creating a
    directory as a side effect of looking meant every `brain doctor` against
    every throwaway repo left one behind. The test suite noticed first — it ran
    `bin/brain` over temp roots and littered the developer's real
    `~/.local/state/brain` with one empty directory per fixture — but a real
    user running `doctor` on a brain that never served would have got the same.
    Only the two callers that are about to WRITE pass create=True.
    """
    import hashlib
    import re

    env = os.environ if env is None else env
    root = Path(root).resolve()
    base = env.get("XDG_STATE_HOME")
    base = Path(base) if base else Path(home or Path.home()) / ".local" / "state"

    digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    # The stem is for a human reading `ls`; the digest is what makes it unique.
    # Sanitised, because a directory name is not the place to find out that a
    # path component contained something the filesystem dislikes.
    stem = re.sub(r"[^a-z0-9-]+", "-", root.name.lower()).strip("-") or "brain"
    where = base / "brain" / f"{stem}-{digest}"
    if not create:
        return where
    try:
        where.mkdir(parents=True, exist_ok=True)
        os.chmod(str(where), 0o700)
        marker = where / "root"
        if not marker.exists():
            marker.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        # Same fail-soft contract as everything else in this module: a machine
        # that cannot create it still runs everything that does not need it.
        pass
    return where


def forget_state(root, env=None, home=None) -> tuple:
    """Delete this brain's machine-local state. Returns (removed, live tokens).

    Called by `brain retire`, and it is not tidiness. This directory holds the
    issued-token database: every credential a hosted assistant was ever
    consented to hold. A token store that survives the brain it authorised is a
    credential nobody is watching any more, still valid, still able to open a
    repository that has moved — the exact shape of a secret that turns up in an
    incident two years later.

    The count is read BEFORE the delete so `retire` can say how many live
    grants it just ended. Saying "and 3 connected clients stopped working" is
    the difference between an operator understanding what happened and one
    filing a bug about their phone.
    """
    where = state_dir(root, env=env, home=home)
    live = 0
    database = where / "oauth.db"
    if database.exists():
        try:
            from . import oauth
            live = oauth.Store(database).count_tokens()
        except Exception:
            # A store this version cannot read is still a store to delete.
            # Refusing to clean up because the count failed would leave the
            # credentials behind for the sake of a number.
            live = 0
    try:
        shutil.rmtree(str(where))
        return True, live
    except OSError:
        return False, live


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

    Never raises. Every filesystem call below that can fail with OSError
    (unlink, the marker read, rmtree, mkdir — the symlink/copytree attempts
    already caught their own) is wrapped and turned into a ("failed", message)
    return instead — the same "a sentence, never an exception" contract
    Scheduler and Keystore already keep in this module. cmd_init has no
    try/except around its call into this function, so an uncaught OSError
    here used to produce a raw traceback instead of a [4/5] FAILED line, and
    skip step 5 and the final summary entirely — on exactly the
    permission-denied or locked-file cases this function exists to handle.
    """
    link, target = Path(link), Path(target)
    # None means link did not already exist — a fresh, first-time link. Set
    # to a string below whenever something WAS already there and is about to
    # be replaced, so the success messages further down can name what
    # changed. Naming it is the one on-screen signal that a link just got
    # silently re-pointed away from a DIFFERENT brain — the incident this
    # repo's own CLAUDE.md warns about (`init` run from a scratch checkout
    # re-points the global skill symlink, "silently hijacking the skill for
    # every session on this machine") — so it is worth carrying even for the
    # rarer copy-replaces-copy case, not just the symlink one that incident
    # actually involved. Plan 2's `retire` is expected to read this same
    # distinction back out of the message.
    old_target = None

    if link.is_symlink():
        try:
            if link.resolve() == target.resolve():
                return "symlink", "already correct"
        except OSError:
            pass
        # os.readlink, not a second resolve(): resolve() above may already
        # have failed (a broken symlink whose old target no longer exists),
        # and readlink still reports the raw stored path even then.
        try:
            old_target = os.readlink(str(link))
        except OSError:
            old_target = "an unreadable location"
        try:
            link.unlink()
        except OSError as exc:
            return "failed", f"could not remove the existing link at {link}: {exc}"
    elif link.exists():
        try:
            has_marker = (link / _COPY_MARKER).exists()
        except OSError:
            has_marker = False
        if not has_marker:
            return "failed", (f"{link} already exists and was not created by brain — "
                              "move it aside, then run this again")
        try:
            old_target = (link / _COPY_MARKER).read_text(encoding="utf-8").strip()
        except OSError as exc:
            return "failed", f"could not read {link / _COPY_MARKER}: {exc}"
        if old_target == str(target):
            return "copy", "already correct (copy)"
        try:
            shutil.rmtree(link)
        except OSError as exc:
            return "failed", f"could not remove the existing copy at {link}: {exc}"

    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return "failed", f"could not create {link.parent}: {exc}"

    # Appended to every success message below whenever this replaced
    # something rather than creating it fresh — empty string otherwise, so a
    # first-time link's wording is byte-for-byte what it was before this
    # distinction existed.
    was = f" — was pointing at {old_target}" if old_target is not None else ""

    try:
        link.symlink_to(target, target_is_directory=True)
        verb = "relinked" if old_target is not None else "linked"
        return "symlink", f"{verb} {link}{was}"
    except (OSError, NotImplementedError):
        pass

    if os_family() == "windows":
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True, text=True)
        if done.returncode == 0:
            return "junction", f"junction created at {link}{was}"

    try:
        shutil.copytree(target, link)
        (link / _COPY_MARKER).write_text(str(target), encoding="utf-8")
        return "copy", (f"copied to {link}{was} — neither a symlink nor a junction was "
                        "possible, so this will go stale; `brain doctor` will say when")
    except OSError as exc:
        return "failed", f"could not link or copy {link}: {exc}"
