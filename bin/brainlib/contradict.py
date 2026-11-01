# bin/brainlib/contradict.py
"""Which pairs of current notes might contradict each other, and how the answer is read.

The pure half of `brain contradictions` (0.7.0): no model, no network, and no write to
the brain. It reads the notes, finds pairs worth a second look with SQLite FTS5 held in
memory (never the on-disk search index, which a read-only command must not rebuild),
picks at most a few of them, and parses the one answer a confined judge gives. The
command itself — the snapshot, the model call, the result line — is in bin/brain,
beside the auditor it is modelled on.

A candidate pair is two CURRENT canonical notes where one is among the other's top
three FTS neighbours (queried with its title and topics) and the two share a topic or
a project. Sharing one is what keeps a judge's attention on notes that could plausibly
disagree; it is a filter on cost, not a claim about meaning.
"""
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .notes import CANONICAL, parse_frontmatter, read_text

MAX_PAIRS = 20
NEIGHBOURS = 3
RECENT_DAYS = 7
VERDICTS = ("contradicts", "compatible", "unclear")
WHY_MAX = 200
STATE_VERSION = 1


def load_notes(knowledge: Path, root: Path) -> list:
    """The current canonical notes: one dict each, sorted by id.

    Current means in a canonical folder with `status: current` and no `superseded_by`.
    A symlink is refused rather than followed, and so is a repeated id (lint rejects it
    anyway). `hash` is the sha256 of the file's bytes, which is what makes a pair
    judged once stay judged until one of its two notes changes."""
    notes, seen = [], set()
    for folder in CANONICAL:
        for path in sorted((knowledge / folder).rglob("*.md")):
            if path.is_symlink() or not path.is_file() or path.name in ("index.md", "README.md"):
                continue
            raw = path.read_bytes()
            fm, _err = parse_frontmatter(read_text(path))
            if not fm or fm.get("status") != "current" or fm.get("superseded_by"):
                continue
            note_id = fm.get("id")
            if not isinstance(note_id, str) or not note_id or note_id in seen:
                continue
            seen.add(note_id)
            topics = fm.get("topics") if isinstance(fm.get("topics"), list) else []
            projects = set()
            if isinstance(fm.get("project"), str) and fm["project"]:
                projects.add(fm["project"].lower())
            if folder == "projects":
                rel = path.relative_to(knowledge / folder).parts
                if len(rel) > 1:
                    projects.add(rel[0].lower())
            notes.append({
                "id": note_id, "path": path, "rel": str(path.relative_to(root)),
                "title": str(fm.get("title") or path.stem),
                "aliases": fm.get("aliases") if isinstance(fm.get("aliases"), list) else [],
                "topics": {str(t).lower() for t in topics}, "projects": projects,
                "body": "\n".join(read_text(path).splitlines()[fm.get("_end_line", 0):]),
                "hash": hashlib.sha256(raw).hexdigest(),
            })
    return sorted(notes, key=lambda n: n["id"])


def recent_ids(root: Path, notes: list, days: int = RECENT_DAYS) -> set:
    """Ids of notes added or changed by a commit in the last `days`, from git's history.

    git is asked not to take its optional locks, so even `git log` cannot touch the
    index file of the repository this command must leave alone. Not a git checkout, or
    git failing, means no note counts as recent."""
    try:
        run = subprocess.run(
            ["git", "log", f"--since={days}.days.ago", "--relative", "--name-only", "-z",
             "--format=", "--diff-filter=ACMR", "--", "knowledge"],
            cwd=root, capture_output=True, env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
    except OSError:
        return set()
    if run.returncode != 0:
        return set()
    changed = {p.decode("utf-8", "replace") for p in run.stdout.split(b"\0") if p}
    return {n["id"] for n in notes if n["rel"] in changed}


def _query(note: dict) -> str:
    words = []
    for word in re.findall(r"[a-z0-9]+", (note["title"] + " " + " ".join(sorted(note["topics"]))).lower()):
        if len(word) >= 3 and word not in words:
            words.append(word)
    return " OR ".join(f'"{w}"' for w in words[:12])


def _shares(a: dict, b: dict) -> bool:
    return bool(a["topics"] & b["topics"] or a["projects"] & b["projects"])


def candidate_pairs(notes: list) -> list:
    """Every candidate pair, each once as (a, b) with a's id before b's, sorted by ids."""
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE fts USING fts5(title, topics, aliases, body, "
                   "tokenize='porter unicode61 remove_diacritics 2')")
        for i, n in enumerate(notes):
            db.execute("INSERT INTO fts (rowid, title, topics, aliases, body) VALUES (?,?,?,?,?)",
                       (i, n["title"], " ".join(sorted(n["topics"])), " ".join(map(str, n["aliases"])),
                        n["body"]))
        found = {}
        for i, n in enumerate(notes):
            query = _query(n)
            if not query:
                continue
            rows = db.execute("SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank, rowid LIMIT ?",
                              (query, NEIGHBOURS + 1)).fetchall()
            for (j,) in [r for r in rows if r[0] != i][:NEIGHBOURS]:
                a, b = sorted((notes[i], notes[j]), key=lambda x: x["id"])
                if _shares(a, b):
                    found[(a["id"], b["id"])] = (a, b)
    finally:
        db.close()
    return [found[k] for k in sorted(found)]


def pair_key(a: dict, b: dict) -> str:
    return a["id"] + "|" + b["id"]


def select_pairs(notes: list, changed: set, state: dict, limit: int = MAX_PAIRS) -> tuple:
    """(pairs, next_offset): at most `limit` (never more than MAX_PAIRS) pairs to judge.

    Pairs already judged at both notes' current hashes are left out. Pairs touching a note
    in `changed` come first; the room left goes to a slice of the rest that starts at the
    state's rotation offset, so repeated runs walk the whole corpus. next_offset is where
    the next run starts, and is only worth keeping once this run's judgement was kept."""
    limit = max(1, min(int(limit), MAX_PAIRS))
    judged = state.get("judged") or {}
    todo = [(a, b) for a, b in candidate_pairs(notes)
            if (judged.get(pair_key(a, b)) or {}).get("hashes") != [a["hash"], b["hash"]]]
    touched = [p[0]["id"] in changed or p[1]["id"] in changed for p in todo]
    fresh = [p for p, t in zip(todo, touched) if t]
    rest = [p for p, t in zip(todo, touched) if not t]
    picked = fresh[:limit]
    offset = state.get("offset") if isinstance(state.get("offset"), int) else 0
    taken = []
    if rest and limit > len(picked):
        start = offset % len(rest)
        taken = (rest[start:] + rest[:start])[:limit - len(picked)]
    return picked + taken, offset + len(taken)


def read_state(path: Path) -> dict:
    """The state file as a dict; no file is an empty state. Anything unreadable raises
    ValueError rather than being reset, so a damaged file is noticed, not forgotten."""
    if not path.exists():
        return {"v": STATE_VERSION, "offset": 0, "judged": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"cannot read {path}: {exc}")
    if (not isinstance(data, dict) or data.get("v") != STATE_VERSION
            or not isinstance(data.get("judged", {}), dict)):
        raise ValueError(f"{path} is not a contradictions state file (v{STATE_VERSION})")
    data.setdefault("judged", {})
    return data


def record_judged(state: dict, judged: list, notes: list, next_offset: int) -> dict:
    """The state after a successful call: `judged` is [(a, b, verdict)]. Entries for notes
    that no longer exist are dropped, so the file follows the corpus instead of growing."""
    ids = {n["id"] for n in notes}
    kept = {k: v for k, v in (state.get("judged") or {}).items()
            if all(part in ids for part in k.split("|"))}
    for a, b, verdict in judged:
        kept[pair_key(a, b)] = {"hashes": [a["hash"], b["hash"]], "verdict": verdict}
    return {"v": STATE_VERSION, "offset": next_offset, "judged": kept}


def write_state(path: Path, state: dict) -> None:
    """Atomically: a temporary file beside the target, then a rename over it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=1, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def parse_verdicts(text: str, count: int):
    """{pair number (1-based): (verdict, why)} from the judge's answer, or None when there is
    no usable answer. The answer ends on one JSON object {"pairs": [{"n", "verdict", "why"}]};
    the last one in the text counts. A verdict outside VERDICTS reads as unclear, a reason is
    one line of at most WHY_MAX characters, and a pair the answer does not mention is absent."""
    starts = [m.start() for m in re.finditer(r'\{\s*"pairs"', text or "")]
    for start in reversed(starts):
        try:
            data, _end = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            continue
        if not isinstance(data.get("pairs"), list):
            continue
        out = {}
        for item in data["pairs"]:
            n = item.get("n") if isinstance(item, dict) else None
            if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= count or n in out:
                continue
            verdict = str(item.get("verdict") or "").strip().lower()
            why = re.sub(r"[\x00-\x1f\x7f\x85  ]+", " ", str(item.get("why") or "")).strip()
            out[n] = (verdict if verdict in VERDICTS else "unclear", why[:WHY_MAX])
        if out:
            return out
    return None
