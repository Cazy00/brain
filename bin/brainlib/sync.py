"""`brain sync` and the single pusher (2.4).

`run(data_root, state_dir)` brings the served folder's `main` and `origin/main` together:

1. Fetch `main` (outside the repo lock: it moves only `refs/remotes/origin/main`).
2. Under the repo lock, classify local `main` against `origin/main`:
   - equal: nothing to do;
   - remote-only: fast-forward the served folder, readers excluded;
   - local-only: push;
   - both: allowed only when everything local brings is new files under knowledge/inbox/.
     Merge in a temporary worktree, lint the result, install it by fast-forwarding the served
     folder to the merge (readers excluded), then push;
   - anything else (a local modification or deletion, a conflict, a lint failure) is refused.
     Nothing is ever resolved automatically.
3. Push. A push the remote rejected because it moved runs the whole sync once more, then
   reports pending.

`push_main(data_root)` is the one way anything pushes: `refs/heads/main:refs/heads/main`, never
`HEAD`, never forced. The capture queue, the post-commit hook (`brain sync --push`) and
`brain sync` itself all come through it.

Results: in_sync, fast_forwarded, pushed, merged (exit 0); pending (1: try again later, nothing
lost); refused (2: a person decides).
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import locks

BRANCH = "main"
REFSPEC = "refs/heads/%s:refs/heads/%s" % (BRANCH, BRANCH)
INBOX = "knowledge/inbox/"
WORKTREES = "sync-worktrees"
STEPS = ("fetched", "classified", "worktree", "merged", "linted", "installing", "installed",
         "pushing")
CRASH_EXIT = 70
FETCH_TIMEOUT = 120
PUSH_TIMEOUT = 120
NO_HOOKS = ["-c", "core.hooksPath=/dev/null"]
ENGINE = Path(__file__).resolve().parents[2]
EXIT = {"in_sync": 0, "fast_forwarded": 0, "pushed": 0, "merged": 0, "pending": 1,
        "refused": 2}


class Result(object):
    def __init__(self, status: str, detail: str = ""):
        self.status = status
        self.detail = detail

    @property
    def exit_code(self) -> int:
        return EXIT[self.status]

    def __repr__(self):                        # pragma: no cover - debugging aid
        return "Result(%r, %r)" % (self.status, self.detail)


def push_argv() -> list:
    return ["push", "--porcelain", "origin", REFSPEC]


def push_main(data_root, run=None) -> tuple:
    """(ok, detail, rejected). `rejected` means the remote moved: sync, then push again."""
    def default(args):
        return subprocess.run(["git", *args], cwd=str(data_root), capture_output=True,
                              text=True, timeout=PUSH_TIMEOUT)
    try:
        done = (run or default)(push_argv())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, repr(exc), False
    if done.returncode == 0:
        return True, "", False
    out = "%s\n%s" % (done.stdout or "", done.stderr or "")
    rejected = any(sign in out for sign in ("[rejected]", "non-fast-forward", "fetch first"))
    return False, out.strip()[-300:] or "git push exited %d" % done.returncode, rejected


def default_lint(tree: Path) -> tuple:
    """Lint a merged tree with the engine's own linter, its state kept inside that tree."""
    done = subprocess.run([sys.executable, str(ENGINE / "bin" / "brain"), "lint", "--root",
                           str(tree)], capture_output=True, text=True, timeout=600)
    return done.returncode == 0, (done.stdout or done.stderr or "").strip()[-300:]


def run(data_root, state_dir, lint=None, fault=None, before_push=None, retry: bool = True):
    data_root, state_dir = Path(data_root), Path(state_dir)
    lint = lint or default_lint

    def step(name):
        if os.environ.get("BRAIN_TEST_HOOKS") == "1" \
                and os.environ.get("BRAIN_TEST_CRASH_AT") == name:
            os._exit(CRASH_EXIT)               # a crash, not an exception: no cleanup runs
        if fault is not None:
            fault(name)

    _clear_worktrees(data_root, state_dir)
    fetch = _git(data_root, "fetch", "--quiet", "origin",
                 "+refs/heads/%s:refs/remotes/origin/%s" % (BRANCH, BRANCH),
                 timeout=FETCH_TIMEOUT)
    if fetch.returncode != 0:
        return Result("pending", "fetch failed: %s" % _tail(fetch))
    step("fetched")

    with locks.repo_lock(state_dir):
        head = _git(data_root, "symbolic-ref", "--quiet", "HEAD").stdout.strip()
        if head != "refs/heads/" + BRANCH:
            return Result("refused", "the served folder is on %s, not %s"
                          % (head or "a detached HEAD", BRANCH))
        local = _rev(data_root, "refs/heads/" + BRANCH)
        remote = _rev(data_root, "refs/remotes/origin/" + BRANCH)
        base = _git(data_root, "merge-base", local, remote).stdout.strip()
        step("classified")
        if local == remote:
            return Result("in_sync")
        if base == local:
            problem = _install(data_root, state_dir, remote, step)
            if problem:
                return Result("refused", problem)
            step("installed")
            return Result("fast_forwarded", "now at %s" % remote[:12])
        merged = False
        if base != remote:
            problem = _local_changes_problem(data_root, base, local)
            if problem:
                return Result("refused", problem)
            target, problem = _merge_in_worktree(data_root, state_dir, local, remote,
                                                 lint, step)
            if problem:
                return Result("refused", problem)
            problem = _install(data_root, state_dir, target, step)
            if problem:
                return Result("refused", problem)
            step("installed")
            merged = True

    step("pushing")
    if before_push is not None:
        before_push()
    ok, detail, rejected = push_main(data_root)
    if ok:
        return Result("merged" if merged else "pushed")
    if rejected and retry:
        return run(data_root, state_dir, lint=lint, fault=fault, before_push=before_push,
                   retry=False)
    return Result("pending", "the push did not land: %s" % detail)


def _local_changes_problem(data_root: Path, base: str, local: str):
    """Everything local brings over the merge base must be a new file in the inbox, both
    commit by commit and in sum (a merge commit cannot smuggle a change past the first)."""
    per_commit = _git(data_root, "log", "--no-merges", "--no-renames", "--format=",
                      "--name-status", "%s..%s" % (base, local))
    in_sum = _git(data_root, "diff", "--no-renames", "--name-status", base, local)
    if per_commit.returncode != 0 or in_sum.returncode != 0:
        return "could not read the local changes"
    for line in (per_commit.stdout + in_sum.stdout).splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        if status != "A" or not path.startswith(INBOX):
            return ("local main carries %s %s, which is not a new inbox note; a person "
                    "decides how it meets the remote" % (_VERBS.get(status[:1], status), path))
    return None


_VERBS = {"M": "a modification of", "D": "a deletion of", "A": "an addition of",
          "T": "a type change of"}


def _merge_in_worktree(data_root: Path, state_dir: Path, local: str, remote: str, lint, step):
    parent = state_dir / WORKTREES
    holder = None
    try:
        parent.mkdir(parents=True, exist_ok=True)
        holder = Path(tempfile.mkdtemp(prefix="merge-", dir=str(parent)))
        tree = holder / "tree"
        add = _git(data_root, "worktree", "add", "--detach", "--quiet", str(tree), remote)
        if add.returncode != 0:
            return None, "could not create the merge worktree: %s" % _tail(add)
        step("worktree")
        merge = _git(tree, "merge", "--no-ff", "--no-edit", "-m",
                     "sync: local captures onto origin/main", local)
        if merge.returncode != 0:
            return None, ("the merge conflicts, and conflicts are never resolved "
                          "automatically: %s" % _tail(merge))
        step("merged")
        ok, detail = lint(tree)
        if not ok:
            return None, "lint rejected the merged tree: %s" % detail
        step("linted")
        return _rev(tree, "HEAD"), None
    except OSError as exc:
        return None, "the merge failed: %s" % exc
    finally:
        if holder is not None:
            shutil.rmtree(str(holder), ignore_errors=True)
        _git(data_root, "worktree", "prune")


def _install(data_root: Path, state_dir: Path, target: str, step):
    with locks.tree_lock(state_dir, exclusive=True):
        step("installing")
        done = _git(data_root, "merge", "--ff-only", "--quiet", target)
        if done.returncode != 0:
            return "could not fast-forward the served folder: %s" % _tail(done)
    return None


def _clear_worktrees(data_root: Path, state_dir: Path) -> None:
    """A crashed run leaves its worktree behind; the next run starts clean."""
    shutil.rmtree(str(state_dir / WORKTREES), ignore_errors=True)
    _git(data_root, "worktree", "prune")


def _git(cwd, *args, timeout: int = 120):
    return subprocess.run(["git", *NO_HOOKS, *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout)


def _rev(cwd, ref: str) -> str:
    return _git(cwd, "rev-parse", "--verify", "--quiet", ref).stdout.strip()


def _tail(done) -> str:
    return ((done.stderr or "") + (done.stdout or "")).strip()[-300:] \
        or "exit %d" % done.returncode
