"""The advisory file locks every brain process shares: bin/brain, brain-http, `brain sync`.

- `repo_lock(state_dir)`: exclusive. Serializes git mutations of the served folder (capture
  commits, supersede, consolidation's branch switch, sync's install). Its file is
  `<state_dir>/git.lock`, which deploy/backup/snapshot.sh also takes by exact path.
- `tree_lock(state_dir, exclusive=False)`: readers take it shared, and sync's install takes it
  exclusive, so a reader sees the old revision or the new one, never a half-checked-out tree.
  Its file is `<state_dir>/tree.lock`. A read never fails over this lock: when the file cannot
  be opened, the read goes ahead unlocked.

Platforms without fcntl get no locking (best effort), as before.
"""
import contextlib
from pathlib import Path

try:
    import fcntl
except ImportError:                            # pragma: no cover - non-POSIX
    fcntl = None

REPO_LOCK = "git.lock"
TREE_LOCK = "tree.lock"


@contextlib.contextmanager
def _held(path: Path, exclusive: bool, required: bool):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a")
    except OSError:
        if required:
            raise
        yield
        return
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
    finally:
        handle.close()


def repo_lock(state_dir):
    return _held(Path(state_dir) / REPO_LOCK, exclusive=True, required=True)


def tree_lock(state_dir, exclusive: bool = False):
    return _held(Path(state_dir) / TREE_LOCK, exclusive=exclusive, required=exclusive)


def try_tree_read(state_dir) -> bool:
    """Could a reader take the tree right now? Non-blocking; releases at once."""
    if fcntl is None:                          # pragma: no cover - non-POSIX
        return True
    path = Path(state_dir) / TREE_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
