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


def _enable_completion():
    """Tab completion for typed paths, when the platform has readline.

    Windows has no readline in the standard library and pyreadline is a third
    party dependency this project does not take, so completion is simply
    absent there. Everything else still works.

    readline.set_completer()/set_completer_delims() are PROCESS-GLOBAL, not
    scoped to this function or even to this module — on a platform that has
    them, calling this leaves a path-completer installed for the rest of the
    process. bin/brain is normally one subcommand per process (the CLI exits
    right after), so in ordinary use that state dies with the process anyway.
    But "normally" is not "always": a test run loads every test into ONE
    process, and a later interactive prompt in the same run (this toolbelt
    already has one — cmd_reset's `input("> ")` confirmation) has no business
    inheriting a path completer this function installed. So this returns a
    zero-arg callable that puts the previous completer and delimiters back;
    the caller (choose(), below) restores in a `finally` unconditionally. What
    this does NOT restore: the "tab: complete" key binding itself — the
    readline API exposes no way to read what Tab was bound to beforehand, only
    to set it. Rebinding Tab to complete is the common default in most
    environments, so that residual gap is accepted rather than chased further.
    """
    try:
        import readline
    except ImportError:
        return None

    previous_completer = readline.get_completer()
    previous_delims = readline.get_completer_delims()

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

    def restore():
        readline.set_completer(previous_completer)
        readline.set_completer_delims(previous_delims)

    return restore


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

    restore = _enable_completion()
    try:
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
    finally:
        if restore is not None:
            restore()
