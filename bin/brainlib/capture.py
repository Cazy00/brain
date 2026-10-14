# bin/brainlib/capture.py
"""The remote capture transaction: idempotency, the write, and the backup queue.

`bin/brain capture --commit --atomic --json` is the write itself, and it stays
there deliberately: one writer, in the file that already owns the credential
scan, the repository lock and the branch check. This module is what has to
exist around it because the caller is remote.

Two problems only appear at a distance.

**A remote caller cannot tell a lost reply from a lost request.** A client whose
connection drops after the commit but before the response has no way to know
whether the note exists, and its only safe move — retry — silently doubles the
note. So a capture is idempotent on the client's own `client_request_id` when
it sends one, and deduplicated on a fingerprint of the content when it does
not. `allow_duplicate` turns off the content check and never the request-id
check, because "I meant to say that twice" and "my connection dropped" are
different statements.

The ledger stores **HMACs, never text**. It is derived state on a disk that also
holds the brain, and a table of recent capture text would be a second copy of
the most sensitive thing here, outside the notes, outside git, outside every
rule that governs the notes. An HMAC answers "have I seen this exact text" and
answers nothing else.

**A push can fail without the capture having failed.** The spec is explicit: a
capture is accepted once the LOCAL commit exists, and a failed push must not
un-accept it. So the result carries a backup state, the queue retries with
bounded backoff, and an alert fires after fifteen minutes rather than the
request failing.

Git is the queue. There is no second list to keep in sync with it, because
`origin/main..HEAD` already IS the set of accepted-but-unbacked-up captures,
maintained by the thing doing the work. The only state this module adds is when
the oldest failure started, which is the one question git cannot answer.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

LEDGER_FILE = "capture-ledger.db"

# How long a request id and a content fingerprint stay deduplicable. A dropped
# connection is retried in seconds; a person deliberately re-saying the same
# sentence is doing it days later. An hour separates the two comfortably and
# keeps the table small enough to prune in one statement.
REQUEST_TTL_SECONDS = 24 * 3600
CONTENT_TTL_SECONDS = 3600

# The spec's alert threshold: a push pending longer than this is an incident,
# not a blip.
PUSH_ALERT_SECONDS = 15 * 60
PUSH_BACKOFF_SECONDS = (5, 15, 45, 120, 300, 600)
# How long a capture will wait for its push to land before answering. Not a
# retry budget — the queue keeps retrying long after this — just long enough
# that a healthy push is reported as what it is. See `settle`.
PUSH_SETTLE_SECONDS = 2.5
PUSH_TIMEOUT_SECONDS = 120


class Ledger(object):
    """Bounded, HMAC-only record of captures recently accepted."""

    def __init__(self, state_dir, key: bytes, clock=time.time):
        if not key:
            raise ValueError("the ledger needs an HMAC key: it must never store raw text")
        self.path = os.path.join(str(state_dir), LEDGER_FILE)
        self._key = key
        self._clock = clock
        self._lock = threading.Lock()
        os.makedirs(str(state_dir), exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS seen ("
                       "fingerprint TEXT PRIMARY KEY, kind TEXT, at REAL, result TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS seen_at ON seen(at)")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def fingerprint(self, kind: str, profile: str, value: str) -> str:
        """One-way, keyed, and scoped.

        Keyed so the file cannot be brute-forced back into capture text — an
        unkeyed hash of a short sentence is recoverable and this table would
        otherwise be a dictionary attack away from being a copy of the inbox.
        Scoped by profile so two endpoints never collide on each other's
        history."""
        return hmac.new(self._key, ("%s|%s|%s" % (kind, profile, value)).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def lookup(self, kind: str, profile: str, value: str):
        """The stored result for this fingerprint, if it is still within its TTL."""
        digest = self.fingerprint(kind, profile, value)
        ttl = REQUEST_TTL_SECONDS if kind == "request" else CONTENT_TTL_SECONDS
        cutoff = self._clock() - ttl
        with self._lock, self._connect() as db:
            row = db.execute("SELECT result, at FROM seen WHERE fingerprint = ? AND kind = ?",
                             (digest, kind)).fetchone()
        if not row or row[1] < cutoff:
            return None
        try:
            return json.loads(row[0])
        except ValueError:                    # pragma: no cover - written by us
            return None

    def remember(self, kind: str, profile: str, value: str, result: dict) -> None:
        digest = self.fingerprint(kind, profile, value)
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO seen (fingerprint, kind, at, result) "
                       "VALUES (?,?,?,?)",
                       (digest, kind, self._clock(), json.dumps(result)))

    def prune(self) -> int:
        now = self._clock()
        with self._lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM seen WHERE (kind = 'request' AND at < ?) "
                "OR (kind <> 'request' AND at < ?)",
                (now - REQUEST_TTL_SECONDS, now - CONTENT_TTL_SECONDS))
            return cursor.rowcount


class BackupQueue(object):
    """Pushes accepted captures to the private remote, and never blocks one.

    A single worker thread, because the repository has a single writer and two
    concurrent pushes to one branch would simply make one of them fail. It is
    woken by a capture and otherwise sleeps; there is no polling loop burning a
    timer on a box that is idle most of the day."""

    def __init__(self, data_root, log=None, clock=time.time, runner=None,
                 sleeper=None, enabled: bool = True):
        self.data_root = str(data_root)
        self._log = log
        self._clock = clock
        self._run = runner or self._git
        self._sleep = sleeper or time.sleep
        self.enabled = enabled
        self._wake = threading.Event()
        self._pending_since = None
        self._alerted = False
        self._state_lock = threading.Lock()
        self._thread = None

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="brain-backup", daemon=True)
        self._thread.start()

    def nudge(self) -> None:
        self._wake.set()

    def state(self) -> str:
        """What to tell the caller about durability, in the spec's vocabulary.

        `_pending_since` alone is not enough to answer this, and reading it as
        if it were is how a capture came to report "pushed to the private
        remote" during a live outage drill while the commit sat unpushed. That
        flag means "a push has been TRIED and failed"; immediately after a
        commit nothing has been tried yet, so the flag is clear and the old
        answer was "pushed" — a claim about durability made before any
        durability existed.

        So the local repository is asked as well. `unpushed()` is a
        `rev-list --count` against the tracking ref: no network, no lock, and
        it knows the one thing the flag cannot — whether the commit is actually
        on the remote."""
        if not self.enabled:
            return "disabled"
        with self._state_lock:
            pending_since = self._pending_since
        if pending_since is None:
            outstanding = self.unpushed()
            # -1 means no upstream is configured, which is not the same as a
            # failure and not something a capture should be alarmed about.
            return "pushed" if outstanding <= 0 else "backup_pending"
        overdue = self._clock() - pending_since >= PUSH_ALERT_SECONDS
        return "push_failed" if overdue else "backup_pending"

    def settle(self, timeout: float = PUSH_SETTLE_SECONDS) -> str:
        """Give the worker a moment to finish, then report the truth.

        Without this every capture would answer `backup_pending` — accurately,
        because the push genuinely has not happened in the millisecond after
        the commit — and a signal that fires on every single capture is one
        nobody reads. With it, `pushed` stays the common answer and `PENDING`
        goes back to meaning something is wrong.

        This is NOT the queue's retry budget. The worker keeps retrying with
        its own backoff for as long as it takes; this only bounds how long the
        CALLER waits before being told. The note is already committed before
        the wait starts, so nothing is at risk in it."""
        deadline = self._clock() + timeout
        while True:
            current = self.state()
            if current != "backup_pending":
                return current
            with self._state_lock:
                tried_and_failed = self._pending_since is not None
            if tried_and_failed or self._clock() >= deadline:
                return current
            self._sleep(0.1)

    def unpushed(self) -> int:
        """How many commits are accepted locally and not yet on the remote."""
        done = self._run(["rev-list", "--count", "@{u}..HEAD"])
        if done.returncode != 0:
            return -1                          # no upstream configured, or not a repo
        try:
            return int(done.stdout.strip() or "0")
        except ValueError:                     # pragma: no cover
            return -1

    def push_once(self) -> bool:
        outstanding = self.unpushed()
        if outstanding == 0:
            self._clear()
            return True
        done = self._run(["push", "--quiet", "origin", "HEAD"])
        if done.returncode == 0 and self.unpushed() == 0:
            self._clear()
            if self._log:
                self._log.record("push_succeeded", count=outstanding, backup="pushed")
            return True
        self._mark_pending()
        if self._log:
            self._log.record("push_failed", count=max(outstanding, 0),
                             backup=self.state(), outcome="error")
        return False

    def _loop(self) -> None:                   # pragma: no cover - exercised by hand
        attempt = 0
        while True:
            self._wake.wait()
            self._wake.clear()
            while not self.push_once():
                delay = PUSH_BACKOFF_SECONDS[min(attempt, len(PUSH_BACKOFF_SECONDS) - 1)]
                attempt += 1
                with self._state_lock:
                    overdue = (self._pending_since is not None
                               and self._clock() - self._pending_since >= PUSH_ALERT_SECONDS
                               and not self._alerted)
                    if overdue:
                        self._alerted = True
                if overdue and self._log:
                    self._log.record("push_pending", backup="push_failed",
                                     ms=int((self._clock() - self._pending_since) * 1000),
                                     outcome="error")
                self._sleep(delay)
            attempt = 0

    def _mark_pending(self) -> None:
        with self._state_lock:
            if self._pending_since is None:
                self._pending_since = self._clock()

    def _clear(self) -> None:
        with self._state_lock:
            self._pending_since = None
            self._alerted = False

    def _git(self, args: list):
        return subprocess.run(["git", *args], cwd=self.data_root, capture_output=True,
                              text=True, timeout=PUSH_TIMEOUT_SECONDS)


class Capturer(object):
    """One remote capture, end to end."""

    def __init__(self, brain_bin: str, data_root, ledger: Ledger, queue: BackupQueue,
                 log=None, timeout: float = 120.0, runner=None):
        self.brain_bin = brain_bin
        self.data_root = str(data_root)
        self.ledger = ledger
        self.queue = queue
        self._log = log
        self._timeout = timeout
        self._run = runner or self._invoke

    def capture(self, text: str, profile: str, principal: str, cid: str,
                client_request_id=None, allow_duplicate: bool = False) -> dict:
        if client_request_id:
            previous = self.ledger.lookup("request", profile, client_request_id)
            if previous is not None:
                # The SAME answer, not a fresh one. A retry that returns a new
                # note id has not been deduplicated; it has been renamed.
                if self._log:
                    self._log.record("capture_deduplicated", cid=cid, profile=profile,
                                     principal=principal, outcome="deduplicated",
                                     note=previous.get("note") or None)
                result = dict(previous)
                result["deduplicated"] = "request"
                return result
        if not allow_duplicate:
            previous = self.ledger.lookup("content", profile, text)
            if previous is not None:
                if self._log:
                    self._log.record("capture_deduplicated", cid=cid, profile=profile,
                                     principal=principal, outcome="deduplicated",
                                     note=previous.get("note") or None)
                result = dict(previous)
                result["deduplicated"] = "content"
                return result

        outcome = self._run(text)
        if outcome.get("status") != "committed":
            if self._log:
                event = ("capture_refused" if outcome.get("status") == "refused"
                         else "capture_rollback")
                self._log.record(event, cid=cid, profile=profile, principal=principal,
                                 outcome="refused" if event == "capture_refused" else "error",
                                 reason=str(outcome.get("status") or "failed"))
            return {"ok": False, "status": outcome.get("status") or "failed",
                    "detail": outcome.get("detail") or "", "note": "", "commit": "",
                    "provisional": True, "backup": self.queue.state()}

        self.queue.nudge()
        result = {"ok": True, "status": "committed", "note": outcome.get("note", ""),
                  "commit": outcome.get("commit", ""), "provisional": True,
                  "backup": self.queue.settle(), "detail": ""}
        # Remember AFTER the commit exists. Recording first would make a crash
        # between the two look, to the retry that follows, exactly like a
        # success — and the note would never be written at all.
        if client_request_id:
            self.ledger.remember("request", profile, client_request_id, result)
        self.ledger.remember("content", profile, text, result)
        if self._log:
            self._log.record("capture_accepted", cid=cid, profile=profile,
                             principal=principal, outcome="ok",
                             note=result["note"], commit=result["commit"][:12],
                             backup=result["backup"],
                             mutation=hashlib.sha256(
                                 result["commit"].encode("utf-8")).hexdigest()[:16])
        return result

    def _invoke(self, text: str) -> dict:
        done = subprocess.run(
            [sys.executable, self.brain_bin, "capture", "--commit", "--atomic", "--json", text],
            capture_output=True, text=True, cwd=self.data_root, timeout=self._timeout)
        for line in reversed(done.stdout.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    break
        # No parseable result is a failure, whatever the exit code said. A
        # capture that cannot prove it committed must not be reported as one.
        return {"status": "failed",
                "detail": "the capture command returned no result (exit %d)" % done.returncode}


def format_result(result: dict) -> str:
    """The plain text a remote client sees. Provisional status is not optional.

    Every branch says the word `PROVISIONAL`, because a note that reads as
    settled knowledge the moment it is written is exactly the failure the inbox
    exists to prevent."""
    if not result.get("ok"):
        if result.get("status") == "refused":
            return ("REFUSED — the text looks like it contains a credential, so nothing "
                    "was written. Put the value in a secret store and capture a note that "
                    "names it instead.\n" + (result.get("detail") or ""))
        if result.get("status") == "retry":
            return ("NOT SAVED — the brain is running maintenance and cannot accept a "
                    "write right now. Nothing was left behind. Try again shortly.")
        return ("NOT SAVED — the capture did not commit and the attempted note was "
                "removed, so nothing is half-written. Try again.\n"
                + (result.get("detail") or ""))
    lines = ["captured as PROVISIONAL — this is an inbox note, not consolidated knowledge, "
             "and it will not appear in a default search until the brain's own "
             "consolidation pass promotes it."]
    if result.get("deduplicated") == "request":
        lines.insert(0, "ALREADY SAVED — this request id was captured before; "
                        "returning the original note rather than writing a second one.")
    elif result.get("deduplicated") == "content":
        lines.insert(0, "ALREADY SAVED — an identical note was captured recently. "
                        "Pass allow_duplicate to save it again anyway.")
    lines.append("note:   %s" % result.get("note", ""))
    lines.append("commit: %s" % (result.get("commit", "")[:12] or "unknown"))
    backup = result.get("backup", "")
    lines.append({
        "pushed": "backup: pushed to the private remote.",
        "backup_pending": "backup: PENDING — the note is committed locally and safe, and "
                          "the push to the private remote is being retried.",
        "push_failed": "backup: FAILING — the note is committed locally, but the private "
                       "remote has been unreachable for over fifteen minutes. The brain's "
                       "operator has been alerted.",
        "disabled": "backup: off — this brain has no configured remote.",
    }.get(backup, "backup: %s" % backup))
    return "\n".join(lines)
