"""Fail-closed maintenance mode for writes (audit A2).

`check(data_root, state_dir)` returns a short reason when the brain must not accept a write
right now, or None. It is computed on every write rather than set by hand, so it engages the
moment a condition appears and clears the moment it goes. Reads never consult it.

Conditions (the spec's failure table):
  - a dirty tree outside knowledge/inbox/ (an uncommitted inbox note is a capture in flight)
  - HEAD not on the default branch (a consolidation or a hand checkout left it elsewhere)
  - an index that cannot be read
  - a data volume that cannot be written
  - an update `brain sync` was installing when it was interrupted (until it is finished)
"""
import os
import sqlite3
import subprocess
from pathlib import Path

INBOX = "knowledge/inbox/"
INSTALL_MARK = "sync-installing.json"   # brainlib/sync.py's marker; the contract between them


def _git(data_root, *args):
    return subprocess.run(["git", *args], cwd=str(data_root), capture_output=True, text=True,
                          timeout=10)


def check(data_root, state_dir, branch="main"):
    data_root, state_dir = Path(data_root), Path(state_dir)
    if not (os.access(str(data_root), os.W_OK) and os.access(str(data_root / ".git"), os.W_OK)):
        return "data not writable"
    if (state_dir / INSTALL_MARK).exists():
        return "interrupted update"
    head = _git(data_root, "rev-parse", "--abbrev-ref", "HEAD")
    if head.returncode != 0 or head.stdout.strip() != branch:
        return "not on %s" % branch
    status = _git(data_root, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        return "dirty tree"
    for line in status.stdout.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        if not path.startswith(INBOX):
            return "dirty tree"
    index = state_dir / "index.db"
    if index.exists():
        try:
            db = sqlite3.connect("file:%s?mode=ro" % index, uri=True, timeout=2)
            try:
                db.execute("select count(*) from sqlite_master").fetchone()
            finally:
                db.close()
        except sqlite3.Error:
            return "index unreadable"
    return None
