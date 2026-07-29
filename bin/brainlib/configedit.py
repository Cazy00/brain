# bin/brainlib/configedit.py
"""Editing config files this system does not own.

Everything else in this repo writes inside the brain, where git is the undo.
This module writes into ~/.cursor, ~/.codex, ~/.claude and their neighbours,
where there is no undo at all — so it is the one place that needs rules rather
than care, and it is kept separate so those rules have somewhere to live and
one test file pointed at them.

Four rules, in the order they matter:

1. **Merge, never overwrite.** Another server's entry, an unrelated top-level
   key, a sibling TOML table: all survive byte-for-byte. The official Claude
   Desktop instructions say to "replace the contents" of the config file, which
   destroys trusted-folder settings and every other server anyone had.
2. **Back up first**, to <file>.brain-backup-<stamp>, and name it in the
   Outcome. Never when nothing changed — a directory of identical backups is
   how people learn to ignore backups.
3. **Refuse anything it cannot recognise.** Refusing is always available and
   always safe, because printing a snippet for a human to paste is what this
   command did before `--apply` existed. That fallback cannot break, so nothing
   here has to guess.
4. **Do nothing when already correct.** Re-running is the documented repair
   when wiring drifts, so it has to be free.

Writes go through a sibling temp file and os.replace, because a half-written
config is worse than an unwritten one, and the mode of the original is carried
across, because mkstemp creates 0600 and inheriting that would quietly change
who can read a file somebody deliberately shared.
"""
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

MARKER_START = "<!-- brain:routing:start -->"
MARKER_END = "<!-- brain:routing:end -->"

_ACTIONS = ("created", "updated", "unchanged", "refused")


class Outcome:
    """What one edit did.

    `snippet` is mandatory on a refusal for the same reason `remedy` is
    mandatory on a failed setup Result: a refusal that leaves the caller with
    nothing to paste has turned a working manual path into a dead end.
    """

    def __init__(self, action: str, detail: str, backup: str = "", snippet: str = "",
                 preview: str = ""):
        if action not in _ACTIONS:
            raise ValueError(f"action must be one of {_ACTIONS}, got {action!r}")
        if action == "refused" and not (snippet or "").strip():
            raise ValueError("a refusal must carry the snippet to paste instead")
        self.action = action
        self.detail = detail
        self.backup = backup
        self.snippet = snippet
        # The whole file as it would be after the edit. Set on every write and
        # on every dry run, so --dry-run diffs the real proposed content rather
        # than a second, hand-built approximation of it that could disagree
        # with what --apply actually does.
        self.preview = preview

    @property
    def changed(self) -> bool:
        return self.action in ("created", "updated")

    def as_dict(self) -> dict:
        return {"action": self.action, "detail": str(self.detail),
                "backup": str(self.backup), "snippet": str(self.snippet)}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _backup(path: Path) -> str:
    """Copy the file aside before it is touched. Returns the backup's path."""
    stamp = time.strftime("%Y-%m-%d-%H%M%S")
    target = Path(str(path) + f".brain-backup-{stamp}")
    # A second edit in the same second would otherwise overwrite the first
    # backup — which is exactly the run where somebody is iterating and most
    # likely to need it.
    n = 2
    while target.exists():
        target = Path(str(path) + f".brain-backup-{stamp}-{n}")
        n += 1
    shutil.copy2(path, target)
    return str(target)


def _write(path: Path, text: str) -> None:
    """Atomically replace `path` with `text`, keeping its mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".brain-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        if path.exists():
            shutil.copymode(str(path), temp)
        os.replace(temp, str(path))
    except BaseException:
        # Leaving a .brain-*.tmp behind next to somebody's config would be
        # litter they have no way to explain.
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def _commit(path: Path, new_text: str, dry_run: bool, action: str, verb: str) -> Outcome:
    """The single place a write happens, so backup-then-write cannot be skipped.

    Every caller routes through here, which is what keeps `--dry-run` honest:
    it reports the same proposed text the real run would write, rather than a
    second rendering of it that could quietly drift out of step.

    `verb` is the stem — "add the brain server to X" — so that one string
    serves both "would add ..." and "added ...". Two hand-written tenses per
    call site is two chances for them to describe different things.
    """
    if dry_run:
        return Outcome(action, "would " + verb, preview=new_text)
    backup = _backup(path) if path.exists() else ""
    _write(path, new_text)
    stem, _, rest = verb.partition(" ")
    past = stem + ("d " if stem.endswith("e") else "ed ") + rest
    return Outcome(action, past, backup=backup, preview=new_text)


def apply_json(path, container: str, name: str, entry: dict, dry_run: bool = False) -> Outcome:
    """Merge one server entry into a JSON config under `container`.

    `container` differs per client and the difference is silent: VS Code reads
    `servers`, everybody else reads `mcpServers`, and a snippet under the wrong
    key produces a server that never appears and never says why.
    """
    path = Path(path)
    snippet = json.dumps({container: {name: entry}}, indent=2)

    if not path.exists():
        return _commit(path, snippet + "\n", dry_run, "created",
                       f"create {path} with the brain server")

    text = _read(path)
    try:
        parsed = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        # VS Code and Cursor both accept JSONC. json.loads does not, and
        # re-serialising would delete every comment in the file — silently, and
        # from a file this system did not write.
        return Outcome("refused",
                       f"{path} is not plain JSON ({exc}). It may use comments or "
                       "trailing commas, which cannot be re-serialised without "
                       "throwing them away.",
                       snippet=snippet)
    if not isinstance(parsed, dict):
        return Outcome("refused",
                       f"{path} holds {type(parsed).__name__}, not an object",
                       snippet=snippet)

    servers = parsed.get(container, {})
    if not isinstance(servers, dict):
        return Outcome("refused",
                       f"{path} has a {container!r} key that is not an object",
                       snippet=snippet)
    if servers.get(name) == entry:
        return Outcome("unchanged", f"{path} already points at this brain")

    had = name in servers
    servers = dict(servers)
    servers[name] = entry
    parsed[container] = servers
    # Re-serialised at indent 2 rather than patched in place: without a parser
    # that preserves formatting there is no way to do better, and the backup is
    # what makes reformatting recoverable.
    return _commit(path, json.dumps(parsed, indent=2) + "\n", dry_run, "updated",
                   ("repoint the brain server in " if had
                    else "add the brain server to ") + str(path))


def _toml_value(value) -> str:
    if isinstance(value, str):
        return json.dumps(value)          # TOML basic strings are JSON strings
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"no TOML spelling for {type(value).__name__}")


def apply_toml(path, table: str, entry: dict, dry_run: bool = False) -> Outcome:
    """Insert or replace a `[table]` in a TOML file, by text.

    There is no TOML parser in the standard library below Python 3.11 and this
    project takes no dependencies, so this is deliberate text surgery. Text
    surgery cannot tell a `[table]` line from the same characters inside a
    multi-line string, so a file containing one is refused rather than guessed
    at — the snippet still gets the user where they were going.
    """
    path = Path(path)
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in entry.items())
    snippet = f"[{table}]\n{body}"

    if not path.exists():
        return _commit(path, snippet + "\n", dry_run, "created",
                       f"create {path} with [{table}]")

    text = _read(path)
    if '"""' in text or "'''" in text:
        return Outcome("refused",
                       f"{path} contains a multi-line string, and this edits TOML as "
                       "text (no parser ships with Python 3.9). A table header inside "
                       "a string is indistinguishable from a real one.",
                       snippet=snippet)

    lines = text.splitlines()
    header = f"[{table}]"
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break

    if start is None:
        joined = text if text.endswith("\n") or not text else text + "\n"
        return _commit(path, joined + ("\n" if joined.strip() else "") + snippet + "\n",
                       dry_run, "updated", f"add [{table}] to {path}")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("["):
            end = i
            break
    current = "\n".join(lines[start:end]).strip()
    if current == snippet.strip():
        return Outcome("unchanged", f"{path} already points at this brain")

    rebuilt = lines[:start] + snippet.splitlines() + [""] + lines[end:]
    return _commit(path, "\n".join(rebuilt).rstrip("\n") + "\n", dry_run, "updated",
                   f"repoint [{table}] in {path}")


def apply_markers(path, block: str, preamble: str = "", dry_run: bool = False) -> Outcome:
    """Write `block` between the routing markers, creating the file if needed.

    The markers are the whole point. Without them, updating the block means a
    human finding where it ends by eye, and `brain retire` cannot remove it at
    all — so every machine that ever ran this keeps instructions pointing at a
    directory that no longer exists.
    """
    path = Path(path)
    marked = f"{MARKER_START}\n{block.strip()}\n{MARKER_END}\n"

    if not path.exists():
        return _commit(path, (preamble.rstrip("\n") + "\n\n" if preamble else "") + marked,
                       dry_run, "created", f"create {path} with the routing block")

    text = _read(path)
    has_start, has_end = MARKER_START in text, MARKER_END in text
    if has_start != has_end:
        return Outcome("refused",
                       f"{path} has one routing marker and not the other — somebody "
                       "edited it by hand and left it half-open. Guessing where the "
                       "block ends risks deleting their text.",
                       snippet=marked)

    if has_start:
        head, _, rest = text.partition(MARKER_START)
        _, _, tail = rest.partition(MARKER_END)
        rebuilt = head + marked.rstrip("\n") + tail
        if rebuilt == text:
            return Outcome("unchanged", f"{path} already has the current block")
        return _commit(path, rebuilt, dry_run, "updated",
                       f"update the routing block in {path}")

    joined = text if text.endswith("\n") or not text else text + "\n"
    return _commit(path, joined + ("\n" if joined.strip() else "") + marked,
                   dry_run, "updated", f"add the routing block to {path}")


def remove_markers(path, dry_run: bool = False) -> Outcome:
    """Take the block and its markers out, leaving everything else alone.

    A file with no markers is reported as unchanged rather than searched: a
    block written by hand, before markers existed, is the caller's to notice
    and say so about. Deleting text this module cannot prove it wrote is the
    one thing worse than leaving it behind.
    """
    path = Path(path)
    if not path.exists():
        return Outcome("unchanged", f"{path} does not exist")

    text = _read(path)
    if MARKER_START not in text or MARKER_END not in text:
        return Outcome("unchanged", f"no marked routing block in {path}")

    head, _, rest = text.partition(MARKER_START)
    _, _, tail = rest.partition(MARKER_END)
    rebuilt = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    return _commit(path, rebuilt, dry_run, "updated",
                   f"remove the routing block from {path}")
