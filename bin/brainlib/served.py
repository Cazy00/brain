"""Whether a data folder is being served (2.2).

`mark(data_root, server)` records that a long-running server (`brain serve`, `brain-http`)
serves this working tree. `check(data_root)` returns a short reason when the folder is served,
or None. `brain consolidate` asks before it cuts a branch: consolidation switches branches and
stages everything, so it runs in its own clone, never in the folder captures land in.

The marker lives in the repository's git directory (`git rev-parse --git-path`), so it is
never committed, never travels to a clone, and belongs to exactly one working tree.
`BRAIN_SERVED=1` in the environment marks a folder served without a marker. A marker outlives
its server on purpose: a folder that was served stays refused until someone deletes the file.
"""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

MARKER = "brain-served"
ENV = "BRAIN_SERVED"


def marker_path(data_root):
    """The marker's absolute path, or None when data_root is not a git working tree."""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-path", MARKER], cwd=str(data_root),
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    path = Path(out.stdout.strip())
    return path if path.is_absolute() else Path(data_root) / path


def mark(data_root, server: str) -> bool:
    """Record that `server` serves data_root. False when it could not be recorded."""
    path = marker_path(data_root)
    if path is None:
        return False
    record = {"server": server, "pid": os.getpid(),
              "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        fd, tmp = tempfile.mkstemp(prefix=".brain-served-", dir=str(path.parent))
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle)
        os.replace(tmp, str(path))
    except OSError:
        return False
    return True


def check(data_root, env=None):
    env = os.environ if env is None else env
    if env.get(ENV) == "1":
        return "%s=1 is set" % ENV
    path = marker_path(data_root)
    if path is None or not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        who, since = record.get("server", "a server"), record.get("since", "?")
    except (OSError, ValueError, AttributeError):
        who, since = "a server", "?"
    return "served by %s since %s (marker: %s)" % (who, since, path)
