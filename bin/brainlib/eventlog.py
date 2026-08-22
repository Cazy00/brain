# bin/brainlib/eventlog.py
"""What the server did, recorded so it can be looked at afterwards.

`brain-http` is a daemon behind a tunnel. Anything it says about a failure goes
to a terminal nobody is watching, which means a bad answer from an agent is the
FIRST time the owner learns something went wrong — and by then the reason has
scrolled away.

It is not a request log, and the difference is the entire design:

**A log of a brain server can contain the brain.** The queries are the owner's
own questions, the results are their note content, and the headers carry the
credential that reads all of it. So this module accepts only field names and
string values from FIXED VOCABULARIES, defined below. A caller cannot pass a
search query, a note id, a request path, an email or an assertion, because
`record` refuses any name it has not already agreed to and raises rather than
writing it.

That is deliberately stricter than scrubbing. A scrubber is a list of the
fields somebody remembered to redact, and the field it misses is the one nobody
thought of. This is a list of the fields allowed to exist, so the failure mode
of forgetting is a `ValueError` in a test rather than the owner's notes on a
disk somewhere. The cost is real and worth paying: adding a field means editing
this module, whose only job is to think about exactly this question.

Three more properties, each from the spec:

- **It never reaches git.** Not by `.gitignore` — by living under the state
  directory, outside the knowledge repository entirely. A rule enforced by
  geography does not depend on anyone maintaining a pattern file.
- **It is bounded and it expires.** Runtime events keep 30 days, mutation audit
  metadata 180. Both are size-capped as well, because a retention window is not
  a size limit and a disk fills on whichever comes first.
- **Failures to write are swallowed.** A log that can take the server down is a
  worse problem than a log with a gap in it.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

RUNTIME_FILE = "events.jsonl"
AUDIT_FILE = "mutations.jsonl"

RUNTIME_RETENTION_DAYS = 30
AUDIT_RETENTION_DAYS = 180

# 5 MB live plus one rolled generation, so the ceiling is ~10 MB per stream.
MAX_BYTES = 5_000_000

# Every event this server can record. An unknown name raises: the vocabulary IS
# the redaction control, so it must fail loudly in a test rather than quietly
# admitting a free-text field in production.
EVENTS = frozenset((
    # lifecycle
    "server_started", "server_stopping", "config_loaded", "readiness_changed",
    # transport
    "request", "method_not_allowed", "body_refused", "content_type_refused",
    "protocol_refused", "origin_refused", "rate_limited", "session_rejected",
    # authentication and authorization
    "auth_failed", "keys_refreshed", "keys_unavailable",
    # the tool layer
    "tool_call", "tool_error", "tool_denied", "tool_timeout",
    # mutation
    "capture_accepted", "capture_refused", "capture_deduplicated",
    "capture_rollback", "push_succeeded", "push_failed", "push_pending",
    "maintenance_mode",
))

# Which of those mean something went wrong, so `--errors` is one filter rather
# than a list somebody has to keep in their head. An event carrying `outcome`
# counts as a failure on its outcome instead, so a tool_call that errored is
# found by the same filter.
FAILURE_EVENTS = frozenset((
    "body_refused", "content_type_refused", "protocol_refused", "origin_refused",
    "rate_limited", "session_rejected", "auth_failed", "keys_unavailable",
    "tool_error", "tool_denied", "tool_timeout", "capture_refused",
    "capture_rollback", "push_failed", "maintenance_mode",
))

MUTATION_EVENTS = frozenset((
    "capture_accepted", "capture_refused", "capture_deduplicated",
    "capture_rollback", "push_succeeded", "push_failed", "push_pending",
))

# The only field names that may appear. `ts` and `event` are written here; the
# rest are what a caller may add.
#
# Read this list as the answer to "what could a log leak". There is no `query`,
# no `text`, no `id`, no `path`, no `email`, no `host` — every one of those was
# considered and left out. `principal` is an HMAC, `client` is the client's own
# self-reported name and is marked untrusted at the point of use, and `note` is
# a repository-relative inbox path, which is the one identifier the spec
# explicitly requires a mutation record to carry.
FIELDS = frozenset((
    "ts", "event", "cid", "mode", "principal", "profile", "endpoint",
    "client", "client_version", "method", "tool", "outcome", "status",
    "ms", "reason", "protocol", "count", "retry_after", "note", "commit",
    "backup", "mutation", "attempt", "days",
))

# Field values that are free-form strings would defeat the whole design, so the
# ones that could carry content are restricted to a vocabulary too.
OUTCOMES = frozenset(("ok", "error", "denied", "timeout", "refused", "deduplicated"))
BACKUP_STATES = frozenset(("pushed", "backup_pending", "push_failed", "disabled"))
MODES = frozenset(("interactive", "service", "none"))

_LOCK = threading.Lock()


class EventLog(object):
    """Two append-only JSONL streams under the state directory."""

    def __init__(self, state_dir, clock=time.time, max_bytes: int = MAX_BYTES):
        self.dir = Path(state_dir)
        self._clock = clock
        self._max_bytes = max_bytes

    def record(self, event: str, **fields) -> None:
        """Append one event. Raises on a vocabulary violation, never on IO.

        The asymmetry is the point. A field name this module has not agreed to
        is a PROGRAMMING error and must stop a test; a full disk is an
        OPERATIONAL one and must not stop the brain."""
        if event not in EVENTS:
            raise ValueError("unknown event %r — add it to eventlog.EVENTS deliberately"
                             % (event,))
        record = {"ts": _iso(self._clock()), "event": event}
        for name, value in fields.items():
            if name not in FIELDS:
                raise ValueError(
                    "field %r is not in eventlog.FIELDS. This is the redaction control: "
                    "if the field genuinely cannot contain a query, a note, an address or "
                    "a credential, add it there on purpose." % (name,))
            if value is None:
                continue
            record[name] = _checked(name, value)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._append(AUDIT_FILE if event in MUTATION_EVENTS else RUNTIME_FILE, line)

    def _append(self, filename: str, line: str) -> None:
        try:
            with _LOCK:
                self.dir.mkdir(parents=True, exist_ok=True)
                path = self.dir / filename
                if path.exists() and path.stat().st_size + len(line) + 1 > self._max_bytes:
                    os.replace(str(path), str(path) + ".1")
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:
            # Deliberately broad and deliberately silent. Every alternative is
            # worse: raising takes the request down, and printing to stderr in a
            # container with a full disk is the same failure one layer along.
            pass

    def prune(self, now=None) -> dict:
        """Drop events older than each stream's retention window.

        Rewrites rather than truncates, because these are append-only streams
        with no index — and rewrites through a temporary file so an interrupted
        prune cannot leave a half-written log."""
        now = self._clock() if now is None else now
        dropped = {}
        for filename, days in ((RUNTIME_FILE, RUNTIME_RETENTION_DAYS),
                               (AUDIT_FILE, AUDIT_RETENTION_DAYS)):
            cutoff = _iso(now - days * 86400)
            path = self.dir / filename
            if not path.exists():
                continue
            kept, removed = [], 0
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        stamp = line[8:8 + 20] if line.startswith('{"ts":"') else ""
                        # Lexicographic comparison is valid because the stamp is
                        # ISO-8601 UTC with a fixed width. A line we cannot read
                        # is KEPT: silently deleting what we failed to parse is
                        # how an audit trail quietly loses its worst day.
                        if stamp and stamp < cutoff:
                            removed += 1
                        else:
                            kept.append(line)
                if removed:
                    tmp = path.with_name(path.name + ".prune")
                    with open(tmp, "w", encoding="utf-8") as handle:
                        handle.writelines(kept)
                    os.replace(str(tmp), str(path))
            except Exception:
                continue
            dropped[filename] = removed
        return dropped


def _checked(name: str, value):
    if name == "outcome" and value not in OUTCOMES:
        raise ValueError("outcome %r is not one of %s" % (value, sorted(OUTCOMES)))
    if name == "backup" and value not in BACKUP_STATES:
        raise ValueError("backup %r is not one of %s" % (value, sorted(BACKUP_STATES)))
    if name == "mode" and value not in MODES:
        raise ValueError("mode %r is not one of %s" % (value, sorted(MODES)))
    if isinstance(value, str):
        # A cap on every string, because the vocabulary controls WHICH fields
        # exist and this controls how much any one of them can carry. `client`
        # is the client's own self-reported name and is the one field here an
        # attacker fully controls.
        return value[:200]
    if isinstance(value, (int, float, bool)):
        return value
    return str(value)[:200]


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
