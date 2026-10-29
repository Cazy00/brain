# bin/brainlib/verbs.py
"""The memory verbs: `recall`, `remember`, `forget` and `delta`.

The written interface is the contract `brain-verbs.v1`, which lives in the
owner's workspace repository (docs/contracts/brain-verbs.v1.md, with a
machine-readable twin). It is MEMORY_VERBS v1 of garrytan/gbrain at commit
fd22a75 (MIT), minus the verbs this brain does not serve, plus the additive
parameters and fields the contract names. This module is where those rules are
code, and it holds nothing else: no subprocess, no transport, no ranking.

What it does NOT do is as deliberate as what it does:

- It never searches. The pages arm of `recall` is the ranking `brain search`
  already computes, handed in as data, so a verb and the old tool cannot
  disagree about which note is first.
- It never writes a note. `remember` is `brain capture` with a few more
  frontmatter keys, and `forget` is a `kind: correction` capture; this module
  only validates what may be written and shapes what comes back.
- It never reads outside `knowledge/inbox/`, and only the inbox's files the
  caller hands it. The facts arm is the inbox: an unconsolidated capture is a
  fact, and a reader meets one only by asking for it (`entity`, or
  `scope: all`), so the rule that keeps provisional material out of a default
  search keeps holding.

Everything a caller can influence is checked here, before the CLI is run:
`provenance`, `entity` and `reason` are free text from any write client, and
each is scanned for credentials, refused if it carries a line break or a
control character, and written through `frontmatter_line` as a quoted scalar,
so no value can add or change another key (`status`, `visibility`, ...).
"""
from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import notes

CONTRACT = "brain-verbs.v1"
MEMORY_VERBS = 1
PROTOCOL_VERSION = 1

READ_VERBS = ("recall", "delta")
WRITE_VERBS = ("remember", "forget")
SERVED = ("recall", "remember", "forget", "delta")
# Verbs of MEMORY_VERBS this brain never serves. They are not listed, and a call
# answers `unavailable` rather than "unknown tool", so a client can tell the
# difference between a typo and a verb this brain does not have.
UNSUPPORTED = ("entity", "context_pack", "synthesize")

ENTITY_RE = re.compile(r"[a-z0-9][a-z0-9/_-]{0,99}")
FACT_ID_RE = re.compile(r"inbox-([a-z0-9][a-z0-9._-]*)")
PROVENANCE_MAX = 500
DELTA_PAGE_MAX = 30
DELTA_FACT_MAX = 30
MAX_INBOX_BYTES = 256 * 1024

# Every control character (C0, DEL, C1) and the Unicode line breaks that
# str.splitlines() also honours, tab included: a value is ONE line.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
_TTL_SHORTHAND = re.compile(r"(\d{1,6})\s*([mhdw])", re.I)
_TTL_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
_ISO_DURATION = re.compile(r"P(?=\d|T\d)", re.I)
_RETRACT_LEAD = re.compile(r"(?i)^\s*retract\s+(\S+)")
_PUNCT = "`'\"()[]<>*,;:."
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

PROVISIONAL_MARKER = "[provisional — unconsolidated]"


# -- envelopes -------------------------------------------------------------

def error(code: str, message: str, suggestion: str, detail: str = "") -> dict:
    """MEMORY_VERBS' error envelope. The suggestion is never empty: it is how a
    caller learns the fix, and an empty one is the failure it exists to prevent."""
    out = {"error": code, "message": message, "suggestion": suggestion or "Retry the call.",
           "protocol_version": PROTOCOL_VERSION}
    if detail:
        out["detail"] = detail
    return out


def invalid(message: str, suggestion: str) -> dict:
    return error("invalid_params", message, suggestion)


def result(payload: dict) -> dict:
    """An MCP tool result carrying a verb response, `isError` from its shape."""
    body = dict(payload)
    body.setdefault("protocol_version", PROTOCOL_VERSION)
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "isError": isinstance(body.get("error"), str)}


def _names(verbs) -> str:
    verbs = list(verbs)
    return verbs[0] if len(verbs) == 1 else ", ".join(verbs[:-1]) + " and " + verbs[-1]


def unavailable(verb: str, served=SERVED) -> dict:
    """The answer to a verb this brain does not serve (contract, error_envelope)."""
    return error(
        "unavailable", "This brain does not serve the verb '%s'." % verb,
        "Use recall: pass query for notes, or entity for saved facts. This brain serves %s."
        % _names([v for v in SERVED if v in served]), detail="unsupported_verb:%s" % verb)


def scope_denied(verb: str) -> dict:
    return error("scope_denied", "This endpoint is read-only: it does not serve '%s'." % verb,
                 "Use the read/capture endpoint to save or forget a fact; this one serves "
                 "recall and delta.")


def advertisement(served) -> dict:
    """`_meta["brain-verbs"]` of the `initialize` result: what THIS endpoint serves."""
    return {"contract": CONTRACT, "memory_verbs": MEMORY_VERBS,
            "verbs": [v for v in SERVED if v in served], "unsupported": list(UNSUPPORTED)}


# -- time ------------------------------------------------------------------

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
                  r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?)?")


def parse_iso(value):
    """An ISO 8601 date or datetime as an aware UTC datetime, or None.

    Hand-parsed so it behaves the same on every Python the engine supports (the
    stdlib's own parser learnt `Z` and odd fractions only in 3.11). A naive time
    is UTC. The range is 0001-01-01 to 9999-12-31, as the contract says."""
    if not isinstance(value, str):
        return None
    m = _ISO.fullmatch(value.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, fraction, zone = m.groups()
    try:
        micro = int((fraction or "0")[:6].ljust(6, "0"))
        stamp = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0),
                         int(second or 0), micro, tzinfo=timezone.utc)
        if zone and zone != "Z":
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            if offset >= timedelta(hours=24):
                return None
            stamp = stamp - offset if zone[0] == "+" else stamp + offset
    except (ValueError, OverflowError):
        return None
    return stamp


def to_ms(stamp: datetime) -> int:
    return (stamp - _EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    return _EPOCH + timedelta(milliseconds=ms)


def iso_ms(stamp: datetime) -> str:
    """ISO 8601 UTC with milliseconds: 1970-01-01T00:00:00.000Z (year padded)."""
    stamp = stamp.astimezone(timezone.utc)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        stamp.year, stamp.month, stamp.day, stamp.hour, stamp.minute, stamp.second,
        stamp.microsecond // 1000)


def iso_seconds(stamp: datetime) -> str:
    return iso_ms(stamp)[:19] + "Z"


# -- input hygiene -----------------------------------------------------------

def line_problem(field: str, value):
    """Why `value` may not be written as one line of frontmatter, or None."""
    if not isinstance(value, str):
        return "%s must be a string" % field
    if _CONTROL.search(value):
        return "%s may not contain a line break or any control character" % field
    return None


def has_secret(field: str, value: str) -> bool:
    """True when the credential scan that guards every capture matches `value`."""
    hits = []
    notes.scan_secrets(Path(field), hits, raw=("%s: %s" % (field, value)).encode("utf-8"))
    return bool(hits)


def quote_scalar(value: str) -> str:
    """`value` as a YAML double-quoted scalar. JSON's string syntax is a subset of
    it, which makes the quoting something the standard library already does
    correctly: quotes and backslashes escaped, nothing left that could end the
    value early or start another key."""
    return json.dumps(value, ensure_ascii=False)


def frontmatter_line(name: str, value: str) -> str:
    return "%s: %s\n" % (name, quote_scalar(value))


def read_scalar(raw: str) -> str:
    """The inverse of `quote_scalar`; an unquoted value is returned as it is."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return raw.strip("'\"")


def free_text_problem(field: str, value):
    """The refusal for a free-text field (provenance, entity, reason), or None.
    The message never repeats the value: it may be the credential."""
    problem = line_problem(field, value)
    if problem:
        return invalid(problem, "Send %s as one line of plain text." % field)
    if has_secret(field, value):
        return invalid("%s looks like it contains a credential, so nothing was written." % field,
                       "Name where the secret is kept instead of pasting it into %s." % field)
    return None


def parse_ttl(value, now: datetime):
    """(valid_until datetime, None) or (None, error). `30d`, `12h`, `45m`, `2w` or an
    absolute ISO 8601 timestamp; an ISO duration (P30D) is refused with the fix."""
    text = value.strip() if isinstance(value, str) else ""
    m = _TTL_SHORTHAND.fullmatch(text)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        try:
            return now + timedelta(**{_TTL_UNITS[unit]: amount}), None
        except OverflowError:
            return None, invalid("ttl is out of range", "Use a shorter duration such as 30d.")
    if _ISO_DURATION.match(text):
        return None, invalid(
            "ttl %r is an ISO 8601 duration, which this brain does not read." % text[:40],
            "Use the shorthand instead: 30d for P30D, 12h for PT12H, 45m for PT45M; "
            "or an absolute timestamp such as 2026-12-31T00:00:00Z.")
    stamp = parse_iso(text)
    if stamp is None:
        return None, invalid("ttl %r is neither a duration nor a timestamp." % text[:40],
                             "Use 30d, 12h or 45m, or an ISO 8601 timestamp.")
    return stamp, None


# -- request preparation -----------------------------------------------------

def _clean_declared(args: dict):
    """Split basis/project/job/model out, as `brain_capture` does: a bad value is
    dropped and named, never a refusal. `kind` is NOT one of them here: for these
    verbs it is v1's own enum, and the note kind is `note_kind`."""
    from . import provenance
    rest, values, ignored = {}, {}, []
    for name, value in args.items():
        if name not in ("basis", "project", "job", "model"):
            rest[name] = value
            continue
        if value is None:
            continue
        cleaned = provenance.clean(name, value)
        if cleaned is None:
            ignored.append(name)
        else:
            values[name] = cleaned
    return rest, values, [n for n in ("basis", "project", "job", "model") if n in ignored]


def split_remember(args):
    """What `validate_args` must not see for `remember`: the declared Harbor fields,
    and a blank provenance (`provenance_required`, not a missing argument).
    Returns (rest, declared, ignored, early_error)."""
    if isinstance(args, dict):
        prov = args.get("provenance")
        if isinstance(prov, str) and not prov.strip():
            return args, {}, [], error(
                "provenance_required", "provenance is blank: a fact must say where it came from.",
                "Pass provenance as one line naming the source, e.g. 'owner said it in chat' "
                "or a document name.")
        rest, declared, ignored = _clean_declared(args)
        return rest, declared, ignored, None
    return args, {}, [], None


def prepare_remember(args: dict, now: datetime):
    """The validated `remember` request, or (None, error). `args` has been through
    the tool's schema; this is the part of the contract a schema cannot say."""
    for field in ("provenance", "entity"):
        if args.get(field) is not None:
            refused = free_text_problem(field, args[field])
            if refused:
                return None, refused
    if args.get("entity") is not None and not ENTITY_RE.fullmatch(args["entity"]):
        return None, invalid(
            "entity %r is not a valid label." % args["entity"][:40],
            "Use lowercase letters, digits, '-', '_' and '/', starting with a letter or "
            "digit, at most 100 characters, e.g. people/ada-lovelace.")
    valid_until = None
    if args.get("ttl") is not None:
        valid_until, refused = parse_ttl(args["ttl"], now)
        if refused:
            return None, refused
    valid_from = None
    if args.get("valid_from") is not None:
        valid_from = parse_iso(args["valid_from"])
        if valid_from is None:
            return None, invalid("valid_from %r is not an ISO 8601 date or datetime."
                                 % args["valid_from"][:40],
                                 "Use e.g. 2026-10-29 or 2026-10-29T08:00:00Z.")
    note_kind = args.get("note_kind") or {"fact": "fact", "preference": "preference"}.get(
        args.get("kind") or "fact", "other")
    flags = ["--provenance", args["provenance"].strip()]
    if args.get("entity"):
        flags += ["--entity", args["entity"]]
    if valid_until is not None:
        flags += ["--expires", iso_ms(valid_until)]
    if valid_from is not None:
        # A date stays a date: that is the form the note contract gives `valid_from`
        # on a canonical note, and a capture is what gets promoted into one.
        given = args["valid_from"].strip()
        flags += ["--valid-from", given if re.fullmatch(r"\d{4}-\d{2}-\d{2}", given)
                  else iso_ms(valid_from)]
    if args.get("visibility") == "private":
        flags += ["--visibility", "private"]
    return {"text": args["fact"], "note_kind": note_kind, "flags": flags,
            "entity": args.get("entity"),
            # The same sentence filed under another entity is another fact.
            "dedupe_text": args["fact"] + "\x1f" + (args.get("entity") or ""),
            "valid_until": iso_ms(valid_until) if valid_until is not None else None,
            "request_id": args.get("request_id"),
            "allow_duplicate": bool(args.get("allow_duplicate", False))}, None


def write_error(outcome: dict) -> dict:
    """The error envelope for a capture that did not land."""
    status = outcome.get("status")
    if status == "refused":
        return error("invalid_params",
                     "The text was refused, so nothing was written (%s)."
                     % (str(outcome.get("detail") or "it looks like a credential")[:200]),
                     "Put a secret in a secret store and write a fact that names it instead.")
    if status == "retry":
        return error("unavailable", "The brain cannot take a write right now.",
                     "Nothing was left behind. Retry shortly, with the same request_id.")
    return error("internal", "Not saved: the capture did not commit.",
                 "Nothing was left behind. Retry with the same request_id.")


def remember_response(outcome: dict, request: dict, ignored) -> dict:
    """The `remember` reply for a capture that landed."""
    note = str(outcome.get("note") or "")
    stem = Path(note.replace("\\", "/")).stem if note else ""
    duplicate = bool(outcome.get("deduplicated"))
    out = {"protocol_version": PROTOCOL_VERSION, "id": "inbox-" + stem if stem else "",
           "status": "duplicate" if duplicate else "inserted",
           "status_text": ("An identical fact was already saved; this is its id."
                           if outcome.get("deduplicated") == "content" else
                           "This request id was already saved; this is the original."
                           if duplicate else
                           "Saved as a PROVISIONAL fact: it is an unconsolidated capture "
                           "until the brain's consolidation pass promotes it."),
           "entity_slug": request.get("entity"), "valid_until": request.get("valid_until"),
           "path": note.replace("\\", "/")}
    if ignored:
        out["ignored"] = list(ignored)
    return out


def prepare_forget(args: dict):
    """(stem, reason) for a valid `forget`, or (None, error)."""
    reason = args.get("reason")
    if reason is not None:
        refused = free_text_problem("reason", reason)
        if refused:
            return None, refused
    m = FACT_ID_RE.fullmatch(args["id"].strip())
    if not m:
        return None, error(
            "not_found", "No saved fact has the id %r." % args["id"][:40],
            "forget takes a fact id ('inbox-...') from remember or from recall's "
            "facts[].fact_id, never a note id or a path. To correct a note, remember with "
            "note_kind 'correction' and the text 'Correction to <id>: ...'.")
    return (m.group(1), reason.strip() if isinstance(reason, str) and reason.strip() else None), None


def retraction_text(stem: str, reason) -> str:
    """The capture `forget` writes. An inbox note has only a path, so the path is
    what the correction names (AGENTS.md, "Writing notes"); consolidation drains
    the target and this retraction together."""
    return "Retract knowledge/inbox/%s.md: %s" % (stem, reason or "forgotten through forget")


def forget_response(stem: str, expired: bool, reason) -> dict:
    return {"protocol_version": PROTOCOL_VERSION, "id": "inbox-" + stem, "expired": expired,
            "reason": reason}


def prepare_recall(args: dict, remote: bool, now: datetime):
    """The validated `recall` request, or (None, error)."""
    query = (args.get("query") or "").strip()
    entity = args.get("entity")
    scope = args.get("scope") or "canonical"
    if not (query or entity or scope in ("all", "handoff")):
        return None, invalid("recall needs something to look for.",
                             "Pass query for notes, entity for saved facts, or scope 'all'.")
    if args.get("source_id") not in (None, "default"):
        return None, error("not_found", "No source %r exists." % str(args["source_id"])[:40],
                           "Only the source 'default' exists on this brain; leave source_id out.")
    since = None
    if args.get("since") is not None:
        since = parse_iso(args["since"])
        if since is None:
            return None, invalid("since %r is not an ISO 8601 date or datetime."
                                 % str(args["since"])[:40],
                                 "Use e.g. 2026-10-29 or 2026-10-29T08:00:00Z.")
    return {"query": query, "entity": entity, "scope": scope,
            "mode": args.get("mode") or "keyword", "limit": args.get("limit", 8),
            "offset": args.get("offset", 0), "remote": remote,
            "since_ms": to_ms(since) if since is not None else None,
            "budget_tokens": args.get("budget_tokens"),
            "now_ms": to_ms(now)}, None


# -- cursors ---------------------------------------------------------------

def encode_cursor(since_iso: str, page_pos, fact_pos) -> str:
    raw = json.dumps({"v": 1, "s": since_iso, "p": list(page_pos), "f": list(fact_pos)},
                     separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(value):
    """(since_iso, page_pos, fact_pos) or None. Strict: it came from a client."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        obj = json.loads(raw.decode("utf-8"))
        if obj.get("v") != 1 or not isinstance(obj.get("s"), str) \
                or parse_iso(obj["s"]) is None:
            return None
        pos = []
        for key in ("p", "f"):
            ms, name = obj[key]
            if isinstance(ms, bool) or not isinstance(ms, int) or not isinstance(name, str) \
                    or len(name) > 600:
                return None
            pos.append((ms, name))
        return obj["s"], pos[0], pos[1]
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def prepare_delta(args: dict, remote: bool, now: datetime):
    cursor = args.get("cursor")
    since_iso, page_pos, fact_pos = None, None, None
    if cursor:
        decoded = decode_cursor(cursor)
        if decoded is None:
            return None, invalid("cursor is not one this brain issued.",
                                 "Pass next_cursor.cursor from the previous reply unchanged, "
                                 "or start again with since.")
        since_iso, page_pos, fact_pos = decoded
    elif args.get("since") is not None:
        since = parse_iso(args["since"])
        if since is None:
            return None, invalid("since %r is not an ISO 8601 date or datetime, or is outside "
                                 "0001-01-01..9999-12-31." % str(args["since"])[:40],
                                 "Use e.g. 2026-10-29 or 1970-01-01T00:00:00Z.")
        since_iso = iso_ms(since)
        page_pos = fact_pos = (to_ms(since), "")
    else:
        return None, invalid("delta needs since or cursor.",
                             "Pass since (an ISO 8601 date or datetime) to start, then "
                             "next_cursor.cursor from each reply. session_id alone is not "
                             "enough: this brain keeps no session state.")
    return {"since_iso": since_iso, "page_pos": page_pos, "fact_pos": fact_pos,
            "remote": remote, "budget_tokens": args.get("budget_tokens"),
            "now_ms": to_ms(now)}, None


# -- the inbox: where the facts are ----------------------------------------

def split_note(text: str):
    """(raw frontmatter dict, body) of one note; the values are unparsed."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    head = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return head, "\n".join(lines[i + 1:]).strip()
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if m and m.group(1) not in head:
            head[m.group(1)] = m.group(2)
    return {}, text


class Capture(object):
    """One inbox note, read for the facts arm."""

    def __init__(self, name: str, mtime: float, head: dict, body: str):
        self.name = name
        self.stem = name[:-3] if name.endswith(".md") else name
        self.path = "knowledge/inbox/" + name
        self.head = head
        self.body = body
        stamp = parse_iso(read_scalar(head.get("captured_at", ""))) if head.get("captured_at") \
            else None
        if stamp is None:
            stamp = datetime.fromtimestamp(mtime, tz=timezone.utc)
        self.at_ms = to_ms(stamp)

    def get(self, key: str):
        raw = self.head.get(key)
        if raw is None:
            return None
        value = read_scalar(raw)
        return value if value not in ("", "null", "~") else None

    @property
    def fact_id(self) -> str:
        return "inbox-" + self.stem

    def expired(self, now_ms: int) -> bool:
        raw = self.get("expires")
        if not raw:
            return False
        stamp = parse_iso(raw)
        if stamp is None:
            return False
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            stamp += timedelta(days=1)          # a date holds through that day
        return to_ms(stamp) <= now_ms


def read_inbox(inbox: Path) -> list:
    """Every readable capture in `inbox`, by file name. Symlinks and oversized files
    are skipped, and an unreadable one is not an error: the facts arm is a view."""
    import os
    captures = []
    try:
        entries = sorted(os.scandir(inbox), key=lambda e: e.name)
    except OSError:
        return captures
    for entry in entries:
        try:
            if not entry.name.endswith(".md") or not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            if info.st_size > MAX_INBOX_BYTES:
                continue
            with open(entry.path, "r", encoding="utf-8-sig", errors="replace") as handle:
                head, body = split_note(handle.read())
        except OSError:
            continue
        captures.append(Capture(entry.name, info.st_mtime, head, body))
    return captures


def normalise_fact(text: str) -> str:
    return " ".join(str(text).split())


def pending_retractions(captures) -> set:
    """Stems of the inbox captures a pending retraction names. Only a retraction
    from an owner endpoint (`source: local`) counts, as for every correction: a
    drop box's caller must not be able to withdraw what it cannot read."""
    gone = set()
    for cap in captures:
        if cap.get("kind") != "correction" or cap.get("source") != "local":
            continue
        lead = _RETRACT_LEAD.match(cap.body)
        if not lead:
            continue
        target = lead.group(1).strip(_PUNCT)
        m = re.fullmatch(r"(?:knowledge/)?inbox/([^/]+?)\.md", target)
        if m:
            gone.add(m.group(1))
        elif target.startswith("inbox-"):
            gone.add(target[len("inbox-"):])
    return gone


def withdrawn(captures, text: str) -> bool:
    """True when `text` is, once whitespace is normalised, the text of a fact that has a
    pending retraction: the claim was withdrawn and may not be remembered again."""
    wanted = normalise_fact(text)
    gone = pending_retractions(captures)
    return any(normalise_fact(c.body) == wanted for c in captures if c.stem in gone)


def forget_state(captures, stem: str) -> str:
    """`missing`, `pending` (already forgotten) or `ready`."""
    if not any(c.stem == stem for c in captures):
        return "missing"
    return "pending" if stem in pending_retractions(captures) else "ready"


def visible_facts(captures, now_ms: int, remote: bool, entity=None):
    """The captures the facts arm may show, oldest first: not expired, not under a
    pending retraction, and not private to a remote caller."""
    gone = pending_retractions(captures)
    out = []
    for cap in captures:
        if cap.stem in gone or cap.expired(now_ms):
            continue
        if remote and cap.get("visibility") == "private":
            continue
        if entity is not None and cap.get("entity") != entity:
            continue
        out.append(cap)
    out.sort(key=lambda c: (c.at_ms, c.name))
    return out


def _instant(value):
    """A stored date or instant as ISO 8601 UTC with milliseconds, or None."""
    stamp = parse_iso(value) if value else None
    return iso_ms(stamp) if stamp is not None else None


def fact_record(cap: Capture, pending: dict) -> dict:
    note_kind = cap.get("kind")
    created = from_ms(cap.at_ms)
    markers = [PROVISIONAL_MARKER]
    waiting = list(dict.fromkeys(pending.get(cap.path, [])))
    if waiting:
        markers.append("[correction pending — %s]" % ", ".join(waiting))
    out = {"fact_id": cap.fact_id, "fact": cap.body,
           "kind": "preference" if note_kind == "preference" else "fact",
           "entity": cap.get("entity"),
           "provenance": cap.get("provenance") or "unrecorded",
           "visibility": "private" if cap.get("visibility") == "private" else "world",
           "valid_from": _instant(cap.get("valid_from")) or iso_ms(created),
           "valid_until": cap.get("expires"),
           "created_at": iso_ms(created), "path": cap.path,
           "trust": "provisional", "markers": markers}
    if note_kind in ("decision", "preference", "fact", "lesson", "idea", "correction", "other"):
        out["note_kind"] = note_kind
    return out


# -- recall ----------------------------------------------------------------

def _cost(text: str) -> int:
    return int(math.ceil(len(text) / 4.0))


def trust_of(hit: dict) -> str:
    """Coarse class, checked in this order (contract, `trust`)."""
    if hit.get("folder") == "inbox":
        return "provisional"
    if hit.get("folder") == "journal":
        return "journal"
    if hit.get("folder") == "handoff" or (hit.get("status") not in (None, "", "current")):
        return "not_current"
    if hit.get("stale"):
        return "stale"
    return "current"


def _norm(text) -> str:
    return " ".join(re.findall(r"\w+", str(text).lower()))


def page_record(hit: dict, query: str) -> dict:
    """One pages-arm result for a hit of the ranking `brain search` computes."""
    wanted = _norm(query)
    title_match = bool(wanted) and wanted == _norm(hit.get("title"))
    alias_match = bool(wanted) and wanted in {_norm(a) for a in hit.get("aliases") or []}
    folder = hit.get("folder")
    # A note with no frontmatter id is known by its path.
    synthetic = hit.get("id") == "%s-%s" % (folder, Path(str(hit.get("path"))).stem)
    slug = hit["path"] if folder in ("inbox", "handoff") or synthetic else hit["id"]
    out = {"slug": slug, "title": hit.get("title") or "", "chunk": hit.get("snippet") or "",
           "evidence": ("exact_title_match" if title_match else
                        "alias_hit" if alias_match else "keyword_exact"),
           "create_safety": "exists" if title_match else "unknown",
           "provenance": hit["path"], "source_id": "default", "path": hit["path"],
           "folder": folder or "", "kind": hit.get("kind") or "",
           "created": hit.get("created") or None, "status": hit.get("status"),
           "review_by": hit.get("review_by"), "trust": trust_of(hit),
           "markers": list(hit.get("markers") or [])}
    for key in ("handoff_to", "handoff_at", "handoff_reason"):
        if key in hit:
            out[key] = hit[key]
    return out


def recall(request: dict, search, captures, pending: dict) -> dict:
    """The `recall` reply. `search` is the result of the ranking (or None when no
    pages arm runs); its hits carry `markers` and `aliases` already."""
    results, more, match_mode = [], 0, None
    if search is not None:
        results = [page_record(h, request["query"]) for h in search["hits"]]
        more, match_mode = int(search.get("more") or 0), search.get("mode")
    facts = []
    if request["entity"] is not None or request["scope"] == "all":
        shown = visible_facts(captures, request["now_ms"], request["remote"], request["entity"])
        if request["since_ms"] is not None:
            shown = [c for c in shown if c.at_ms >= request["since_ms"]]
        facts = [fact_record(c, pending) for c in reversed(shown)][:request["limit"]]
    out = {"protocol_version": PROTOCOL_VERSION}
    budget = request["budget_tokens"]
    dropped = 0
    if budget is not None:
        # Facts first. The first item that does not fit ends the packing: what is
        # kept is a prefix, so a reader never sees a gap it cannot account for.
        used, kept_facts, kept_results, full = 0, [], [], False
        for item, bucket, text in (
                [(f, kept_facts, f["fact"]) for f in facts]
                + [(r, kept_results, None) for r in results]):
            cost = _cost(text) if text is not None else _cost(item["title"]) + _cost(item["chunk"])
            if full or used + cost > budget:
                full = True
                dropped += 1
                continue
            used += cost
            bucket.append(item)
        facts, results = kept_facts, kept_results
        out.update({"budget_tokens": budget, "budget_used": used, "dropped_count": dropped})
    out.update({"facts": facts, "total": len(facts), "results": results,
                "facts_order": "newest"})
    if match_mode is not None:
        out["more"] = more
        out["match_mode"] = match_mode
    if request["mode"] == "hybrid":
        # This release has no meaning arm. A request for one is answered
        # keyword-only and says so, which is what the contract has an embed
        # failure do: degrade, never error.
        out["search_degraded"] = "embed_unavailable"
    return out


# -- delta -----------------------------------------------------------------

def delta(request: dict, pages: list, captures, pending: dict) -> dict:
    """The `delta` reply. `pages` is the walk (path, id, title, folder, mtime, status,
    stale, markers), in any order."""
    # JSON hands the positions back as lists; they are compared as (ms, name) tuples.
    page_pos, fact_pos = tuple(request["page_pos"]), tuple(request["fact_pos"])
    walk = sorted(((int(p["mtime"] * 1000), p["path"]), p) for p in pages)
    walk = [(key, p) for key, p in walk if key > page_pos]
    more_pages = len(walk) > DELTA_PAGE_MAX
    walk = walk[:DELTA_PAGE_MAX]
    shown = [(( c.at_ms, c.name), c) for c in visible_facts(
        captures, request["now_ms"], request["remote"])]
    shown = [(key, c) for key, c in shown if key > fact_pos]
    more_facts = len(shown) > DELTA_FACT_MAX
    shown = shown[:DELTA_FACT_MAX]

    budget = request["budget_tokens"]
    used, dropped, full = 0, 0, False
    kept_pages, kept_facts = [], []
    for (key, p) in walk:
        record = {"path": p["path"], "id": p.get("id"), "title": p["title"],
                  "folder": p["folder"], "modified": iso_seconds(from_ms(key[0])),
                  "trust": trust_of(p), "markers": list(p["markers"])}
        cost = _cost(record["title"])
        if budget is not None and (full or used + cost > budget):
            full = True
            dropped += 1
            continue
        used += cost
        kept_pages.append((key, record))
    for (key, c) in shown:
        record = fact_record(c, pending)
        cost = _cost(record["fact"])
        if budget is not None and (full or used + cost > budget):
            full = True
            dropped += 1
            continue
        used += cost
        kept_facts.append((key, record))
    if kept_pages:
        page_pos = kept_pages[-1][0]
    if kept_facts:
        fact_pos = kept_facts[-1][0]
    lines = ["%s  %s%s" % (r["modified"], r["path"], "".join(" " + m for m in r["markers"]))
             for _k, r in kept_pages]
    lines += ["%s  %s  %s" % (r["created_at"], r["fact_id"], " ".join(r["fact"].split())[:120])
              for _k, r in kept_facts]
    out = {"protocol_version": PROTOCOL_VERSION, "since": request["since_iso"],
           "pages": [r for _k, r in kept_pages], "facts": [r for _k, r in kept_facts],
           "threads": [], "text": "\n".join(lines) or "(nothing changed)",
           "has_more": bool(more_pages or more_facts or dropped),
           "next_cursor": {"cursor": encode_cursor(request["since_iso"], page_pos, fact_pos)}}
    if budget is not None:
        out.update({"budget_tokens": budget, "budget_used": used, "dropped_count": dropped})
        if dropped and not kept_pages and not kept_facts:
            out["degraded_reason"] = "budget_too_small"
    return out
