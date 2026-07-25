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

    def delete(self, name: str) -> bool:
        if not self._module_available():
            return self.fallback.delete(name)
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
