"""A warm `brain` process for `recall`, so a call does not pay for a cold start.

`recall` runs `brain verb recall` as a child of the MCP layer. Each run starts an
interpreter, compiles the 9,000-line `bin/brain` (a script is never cached as bytecode),
imports `brainlib`, reads `vectors.db` and only then ranks: about 0.4 s before any work,
on a host without a bytecode cache. The worker pays that once, then answers over a pipe.

  run()      the one call: a finished `brain verb recall -` as a CompletedProcess, or None
             when the worker cannot take it. None always means "run the one-shot command":
             the answer is the same, only slower, and any error comes out of that command.
  prewarm()  start it (and read the vectors in) before the first call, from a thread.

The worker is `brain verb-worker` (bin/brain). It is disposable like everything else here:
a timeout, a crash or a changed environment kills it and the next call starts a new one;
it exits on its own when this process ends. It never holds the tree lock between calls.
`BRAIN_VERB_WORKER=off` switches it off.

One worker answers one call at a time. A call that finds it busy for longer than a moment
does not queue behind it: it runs the one-shot command at once, as it did before the worker
existed. Every fall back is counted (`fallbacks()`) and reported to `observer` (a server wires
its event log there) or, with none set, to stderr, at most once a minute per reason.
"""
import json
import os
import select
import subprocess
import sys
import threading
import time
from pathlib import Path

BRAIN = str(Path(__file__).resolve().parent.parent / "brain")
OPS = ("recall",)
MAX_REPLY = 32 * 1024 * 1024
BUSY_WAIT = 0.2             # seconds a call waits for a busy worker before running the one-shot command
REPORT_EVERY = 60.0         # seconds between stderr lines for one reason
REASONS = ("busy", "error", "refused")     # why a call ran the one-shot command instead

_lock = threading.Lock()
_state = {"proc": None, "key": None, "buffer": b""}
_fallbacks = {}             # reason -> how many calls, since this process started
_reported = {}              # reason -> monotonic time of the last stderr line
observer = None             # callable(reason), set by a server that keeps an event log


def enabled() -> bool:
    return (os.environ.get("BRAIN_VERB_WORKER") or "").strip().lower() not in ("off", "0", "no")


def fallbacks() -> dict:
    """How many calls ran the one-shot command instead of the worker, by reason."""
    return dict(_fallbacks)


def _fell_back(reason: str) -> None:
    _fallbacks[reason] = _fallbacks.get(reason, 0) + 1
    if observer is not None:
        try:
            observer(reason)
        except Exception:
            pass                     # a log that fails must not fail the call
        return
    now = time.monotonic()
    if now - _reported.get(reason, -REPORT_EVERY) >= REPORT_EVERY:
        _reported[reason] = now
        try:
            sys.stderr.write("brain: recall ran the one-shot command (worker %s; %d so far)\n"
                             % (reason, _fallbacks[reason]))
            sys.stderr.flush()
        except (OSError, ValueError):
            pass


def _engine_stamp():
    """The engine files the worker has compiled, with their sizes and mtimes. It runs the code
    it started with, so an update of `bin/brain` or `brainlib/` (a sync installs one) must mean a
    new worker, or it would keep serving the old ranking and trust rules."""
    root = Path(BRAIN)
    stamp = []
    for path in [root, *sorted((root.parent / "brainlib").glob("*.py"))]:
        try:
            st = path.stat()
        except OSError:
            continue
        stamp.append((path.name, st.st_mtime_ns, st.st_size))
    return tuple(stamp)


def _key(cwd):
    """What the worker was started under. It reads its configuration and its code once, at
    start, so a change in any BRAIN_* setting, in the engine files, or in the folder it runs
    in means a new worker."""
    return (str(cwd), sys.executable, _engine_stamp(),
            tuple(sorted((k, v) for k, v in os.environ.items() if k.startswith("BRAIN_"))))


def stop():
    proc, _state["proc"], _state["buffer"] = _state["proc"], None, b""
    if proc is None:
        return
    for stream in (proc.stdin, proc.stdout):
        try:
            stream.close()
        except OSError:
            pass
    proc.kill()
    proc.wait()


def _ensure(cwd):
    key = _key(cwd)
    proc = _state["proc"]
    if proc is not None and proc.poll() is None and _state["key"] == key:
        return proc
    stop()
    _state["proc"] = proc = subprocess.Popen(
        [sys.executable, BRAIN, "verb-worker"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=cwd)
    _state["key"] = key
    return proc


def _readline(proc, deadline):
    """One line from the worker. TimeoutError at the deadline, EOFError if it died."""
    fd = proc.stdout.fileno()
    while b"\n" not in _state["buffer"]:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        if not select.select([fd], [], [], left)[0]:
            raise TimeoutError
        piece = os.read(fd, 65536)
        if not piece:
            raise EOFError
        _state["buffer"] += piece
        if len(_state["buffer"]) > MAX_REPLY:
            raise EOFError
    line, _state["buffer"] = _state["buffer"].split(b"\n", 1)
    return line


def _ask(message: dict, timeout: float, cwd):
    """(reply, why): the worker's reply to one message, or (None, "busy") when another call
    holds it, or (None, "error") when it is unavailable (it is stopped); the caller falls back.
    A reply that took longer than `timeout` raises subprocess.TimeoutExpired, as the one-shot
    command would."""
    if not _lock.acquire(timeout=min(BUSY_WAIT, timeout)):
        return None, "busy"
    try:
        deadline = time.monotonic() + timeout
        try:
            proc = _ensure(cwd)
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            proc.stdin.flush()
            reply = json.loads(_readline(proc, deadline).decode("utf-8"))
        except TimeoutError:
            stop()
            raise subprocess.TimeoutExpired(["brain", "verb-worker"], timeout) from None
        except (OSError, EOFError, ValueError):
            stop()
            return None, "error"
        return (reply, "") if isinstance(reply, dict) else (None, "error")
    finally:
        _lock.release()


def run(args, timeout, input=None, cwd=None):
    """`brain verb recall -` answered by the worker, as the CompletedProcess the one-shot
    command would have returned; None when it is not a call the worker takes."""
    if not enabled() or len(args) != 3 or args[0] != "verb" or args[1] not in OPS or args[2] != "-":
        return None
    try:
        request = json.loads(input)
    except (TypeError, ValueError):
        return None
    reply, why = _ask({"op": args[1], "request": request}, timeout, cwd or os.getcwd())
    if reply is None:
        _fell_back(why)
        return None
    if reply.get("ok") is not True or not isinstance(reply.get("stdout"), str):
        _fell_back("refused")
        return None
    return subprocess.CompletedProcess(["brain", *args], 0, reply["stdout"] + "\n", "")


def prewarm(cwd=None, timeout: float = 30.0):
    """Start the worker and have it read the vectors in, on a thread, so the first recall
    finds it ready. Quietly does nothing when it cannot."""
    if not enabled():
        return None

    def go():
        try:
            _ask({"op": "warm"}, timeout, cwd or os.getcwd())
        except Exception:
            pass
    thread = threading.Thread(target=go, name="verb-worker-prewarm", daemon=True)
    thread.start()
    return thread
