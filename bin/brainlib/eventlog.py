# bin/brainlib/eventlog.py
"""What happened on the server, recorded so it can be looked at afterwards.

`brain serve` is a daemon behind a tunnel. Everything it currently says about a
failure goes to a terminal nobody is watching, which means a bad answer from an
agent is the FIRST time an operator learns anything went wrong — and by then
the reason has scrolled away. This is the file that fixes that.

It is not a request log, and the difference is the whole design:

**A log of a brain server can contain the brain.** The queries are the owner's
own questions, the results are their note content, and the headers carry the
credential that reads all of it. So this module accepts only field names and
string values from FIXED VOCABULARIES, defined below. A caller cannot pass a
search query, a note id, a request path or a token, because `record` refuses
any string it has not already agreed to and raises rather than writing it.

That is deliberately stricter than scrubbing. A scrubber is a list of the
fields somebody remembered to redact, and the field it misses is the one nobody
thought of; this is a list of the fields that are allowed to exist, so the
failure mode of forgetting is a `ValueError` in a test rather than the owner's
notes on disk. The cost is real and is worth paying: adding a new event or a
new field is an edit here, in the module whose only job is to think about this.

Two more properties, both from the handoff that asked for this:

- **It never reaches git.** Not by .gitignore — by living outside the
  repository entirely, under `osbackend.state_dir`. A rule enforced by
  geography does not depend on anyone maintaining a pattern file.
- **It is bounded.** One rollover generation and a byte cap, decided now
  rather than after a disk fills. This exists for a testing phase.

Failures to write are swallowed, always. A log that can take the server down is
a worse problem than a log with a gap in it.
"""
import json
import os
from pathlib import Path

from . import mcp

FILENAME = "events.jsonl"

# 5 MB live, plus one rolled generation, so the ceiling is ~10 MB per brain.
DEFAULT_MAX_BYTES = 5_000_000

# Every event this server can record. An unknown name raises: the vocabulary IS
# the redaction control, so it fails loudly in a test rather than quietly
# admitting a free-text field in production.
EVENTS = frozenset((
    # transport
    "server_started", "request", "auth_failed", "rate_limited",
    "origin_refused", "body_refused", "protocol_refused",
    # the tool layer
    "tool_call", "tool_error", "capture_committed", "capture_uncommitted",
    # the authorization server
    "oauth_metadata_served", "oauth_authorize_shown", "oauth_consent_failed",
    "oauth_code_issued", "oauth_token_issued", "oauth_token_refreshed",
    "oauth_token_accepted", "oauth_token_rejected", "oauth_grant_revoked",
    "oauth_client_resolved", "oauth_client_refused", "oauth_error",
    "insufficient_scope",
))

# Which of those mean something went wrong, for `brain logs --errors`. An event
# carrying `outcome` counts as a failure on its outcome instead, so a
# `tool_call` that errored is found by the same filter.
FAILURE_EVENTS = frozenset((
    "auth_failed", "rate_limited", "origin_refused", "body_refused",
    "protocol_refused", "tool_error", "capture_uncommitted",
    "oauth_consent_failed", "oauth_token_rejected", "oauth_client_refused",
    "oauth_error", "insufficient_scope",
))

# The only field names that may appear. `ts` and `event` are written by this
# module; the rest are what a caller may add.
FIELDS = frozenset((
    "ts", "event", "method", "path_class", "status", "ms", "reason", "tool",
    "outcome", "retry_after", "oauth", "scope", "client_kind", "code",
    "count", "mode",
))

# A request path is caller-supplied text — `/mcp?q=<the owner's question>` is
# exactly how a query would reach this file — so the path is recorded as a
# CLASS and never as itself.
PATH_CLASSES = ("mcp", "discovery", "authorize", "token", "revoke", "other")

# Likewise the method: the request line is written by the client, so anything
# outside this list is recorded as "other".
METHODS = ("GET", "POST", "DELETE", "other")

# Why something was refused, as a class rather than as a sentence. These are
# the only reasons that exist; a new failure mode means a new entry here.
REASONS = (
    # authentication at /mcp
    "no_header", "bad_scheme", "bad_token", "unknown_token", "expired",
    "revoked", "wrong_audience", "missing_scope",
    # the request itself
    "not_json", "not_object", "too_large", "bad_content_length",
    "unsupported_version", "not_found", "no_stream", "no_sessions",
    # the authorization endpoint
    "no_client", "bad_redirect_uri", "bad_challenge", "bad_resource",
    "bad_response_type", "bad_scope", "consent_refused",
    # the token endpoint
    "code_used", "code_expired", "pkce_failed", "refresh_reused",
    "unsupported_grant_type", "wrong_client", "bad_form",
    # resolving a client id metadata document
    "not_https", "no_path", "blocked_address", "dns_failure", "redirected",
    "too_big", "timeout", "bad_document", "client_id_mismatch", "fetch_failed",
    "unknown_client",
)

# RFC 6749 error codes, so a `code` field cannot become a custom string. A
# client that cannot parse the error retries forever.
ERROR_CODES = ("invalid_request", "invalid_client", "invalid_grant",
               "unauthorized_client", "unsupported_grant_type",
               "invalid_scope", "access_denied", "server_error",
               "unsupported_response_type", "insufficient_scope")

OUTCOMES = ("ok", "error", "refused")
CLIENT_KINDS = ("cimd", "preregistered")
SCOPES = ("brain:read", "brain:write", "offline_access")
MODES = ("default", "read_only", "drop_box")

# Tool names are DERIVED from the tool table rather than copied into it. A
# sixth tool is loggable the day it exists, and a typo here cannot invent one
# that does not — the same derivation READ_ONLY_TOOLS already uses.
VALUES = frozenset(
    PATH_CLASSES + METHODS + REASONS + ERROR_CODES + OUTCOMES
    + CLIENT_KINDS + SCOPES + MODES + tuple(mcp.TOOLS_BY_NAME)
)


def _utc_now() -> str:
    """ISO-8601 in UTC, to the second. A parameter everywhere it is used, for
    the same reason Limiter's clock is one: no test may wait for a clock."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class EventLog:
    """Append-only JSONL, bounded, with a vocabulary instead of a scrubber."""

    def __init__(self, path, clock=None, max_bytes: int = DEFAULT_MAX_BYTES):
        self.path = Path(path)
        self._clock = clock or _utc_now
        self._max_bytes = max_bytes

    # ------------------------------------------------------------- writing

    def record(self, event: str, **fields) -> None:
        """Write one entry, or raise if it would say something it may not.

        Validation raises; I/O does not. The two failure modes are opposite on
        purpose — a vocabulary violation is a bug to fix in a test, and a full
        disk is not a reason to stop serving the brain.
        """
        line = self._render(event, fields)
        try:
            self._roll_if_needed(len(line))
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            return

    def _render(self, event: str, fields: dict) -> str:
        if event not in EVENTS:
            raise ValueError(
                f"unknown event {event!r} — add it to eventlog.EVENTS deliberately. "
                "The vocabulary is what stops caller-supplied text reaching this file.")
        entry = {"ts": self._clock(), "event": event}
        for key, value in fields.items():
            if key not in FIELDS:
                raise ValueError(
                    f"unknown field {key!r} — add it to eventlog.FIELDS deliberately.")
            if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
                # bool first: it is a subclass of int, and checking int first
                # would let True through as a number rather than as itself.
                entry[key] = value
            elif isinstance(value, str):
                if value not in VALUES:
                    raise ValueError(
                        f"{key}={value!r} is not a known value. Every string this "
                        "log can hold is enumerated in eventlog — a value that is "
                        "not there is either a new class to add, or the caller's "
                        "own text, which must never be written here.")
                entry[key] = value
            else:
                raise ValueError(f"{key} must be a scalar, not {type(value).__name__}")
        return json.dumps(entry, sort_keys=False) + "\n"

    def _roll_if_needed(self, incoming: int) -> None:
        """One generation, never more.

        os.replace rather than a rename-and-delete pair: it is atomic, so a
        reader mid-rotation sees one file or the other and never neither.
        """
        try:
            size = self.path.stat().st_size
        except OSError:
            return
        if size + incoming <= self._max_bytes:
            return
        os.replace(str(self.path), str(self.path) + ".1")

    # ------------------------------------------------------------- reading

    def read(self, limit: int = 50, since: str = "", errors_only: bool = False) -> list:
        """Oldest first, newest last — the order a terminal reads naturally.

        Both generations are read, because an error that rolled off the live
        file is still the error somebody came looking for.
        """
        entries = []
        for path in (Path(str(self.path) + ".1"), self.path):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    # A half-written final line after a kill. Skipping it is
                    # right; failing the whole read over it is not.
                    continue
                if not isinstance(entry, dict):
                    continue
                if since and str(entry.get("ts", ""))[:10] < since:
                    continue
                if errors_only and not is_failure(entry):
                    continue
                entries.append(entry)
        return entries[-limit:] if limit else entries


def is_failure(entry: dict) -> bool:
    return (entry.get("event") in FAILURE_EVENTS
            or entry.get("outcome") in ("error", "refused"))


def render(entry: dict) -> str:
    """One entry as one readable line, for `brain logs`."""
    ts = str(entry.get("ts", ""))
    event = str(entry.get("event", "?"))
    rest = " ".join(f"{k}={v}" for k, v in entry.items()
                    if k not in ("ts", "event") and v is not None)
    return f"  {ts}  {event:<22} {rest}".rstrip()
