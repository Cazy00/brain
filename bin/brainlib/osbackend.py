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
