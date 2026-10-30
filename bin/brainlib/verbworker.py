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

_lock = threading.Lock()
_state = {"proc": None, "key": None, "buffer": b""}


def enabled() -> bool:
    return (os.environ.get("BRAIN_VERB_WORKER") or "").strip().lower() not in ("off", "0", "no")


def _key(cwd):
    """What the worker was started under. It reads its configuration once, at start, so a
    change in any BRAIN_* setting (or the folder it runs in) means a new worker."""
    return (str(cwd), sys.executable,
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
    """The worker's reply to one message, or None when it is unavailable (it is stopped,
    and the caller falls back). A reply that took longer than `timeout` raises
    subprocess.TimeoutExpired, as the one-shot command would."""
    if not _lock.acquire(timeout=timeout):
        return None
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
            return None
        return reply if isinstance(reply, dict) else None
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
    reply = _ask({"op": args[1], "request": request}, timeout, cwd or os.getcwd())
    if not reply or reply.get("ok") is not True or not isinstance(reply.get("stdout"), str):
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
