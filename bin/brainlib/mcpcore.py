# bin/brainlib/mcpcore.py
"""The MCP tool layer — what the brain exposes, independent of how it is reached.

Split out of `bin/brain-mcp` when a second transport arrived. The split is not
tidiness: two transports each carrying their own tool table would drift, and a
tool present over stdio and missing over HTTP is a bug nobody finds until
somebody far from their laptop needs it. There is one table, one validator, one
dispatcher, and the transports are thin.

It shells out to `bin/brain` for the actual work, which is what makes retrieval
behaviour identical on the CLI, over stdio, and over HTTP — the ranking and the
trust signals are deterministic code, not model judgement. It also means the
plain text the CLI prints reaches the client unaltered, which the remote
contract requires: `[provisional — unconsolidated]`, the ARCHIVED banner and a
passed `review_by` are the only thing standing between a stale note and an
answer given as true.

Two axes, deliberately separate:

- **Transport** decides the argument LIMITS and which arguments exist at all.
  A local capture may ask not to commit, because the note is on a machine its
  owner is sitting at. A remote capture may not: an uncommitted remote capture
  is neither durable nor auditable, and it dirties the tree that consolidation
  needs clean.
- **Profile** decides WHICH TOOLS exist. `read` is the retrieval tools (the
  four old ones, plus the verbs `recall` and `delta`); `capture` is those plus
  `brain_capture` and the verbs `remember` and `forget`. Nothing else is ever
  added by a request — the profile comes from the authenticated audience, never
  from a parameter.

The memory verbs (`recall`, `remember`, `forget`, `delta`) are the contract
`brain-verbs.v1`; brainlib/verbs.py is where its rules are code. The old tools
`brain_search`, `brain_capture` and `brain_recent` are served unchanged beside
them until the last client has moved.

`tools_for` filtering the advertised list is usability. `call` re-checking the
profile is the security boundary, and it is checked again there precisely
because a client can construct a call for a tool that was never advertised.
"""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import provenance, verbs

ENGINE = Path(__file__).resolve().parent.parent.parent
BRAIN = str(ENGINE / "bin" / "brain")

READ_TOOLS = ("brain_search", "brain_read", "brain_links", "brain_recent") \
    + verbs.READ_VERBS
CAPTURE_TOOLS = READ_TOOLS + ("brain_capture",) + verbs.WRITE_VERBS
PROFILES = {"read": READ_TOOLS, "capture": CAPTURE_TOOLS}

# The remote caps come from the design spec; the local ones are what stdio has
# always allowed. They differ because the threat differs: a local caller is the
# owner at their own keyboard, a remote one is anything that got through Access.
LIMITS = {
    "local": {"query": 100_000, "id_or_path": 100_000, "text": 100_000,
              "limit": (1, 100), "days": (1, 3650), "offset": 1_000_000},
    "remote": {"query": 2_048, "id_or_path": 512, "text": 65_536,
               "limit": (1, 20), "days": (1, 365), "offset": 100_000},
}

SEARCH_DESCRIPTION = (
    "Search the user's permanent second brain — their decisions, projects, people, "
    "reference notes, and life context. Use this BEFORE answering any question "
    "about the user's past decisions, preferences, projects, or personal history. "
    "Returns CURRENT knowledge only (superseded notes are structurally excluded). "
    "If a query misses, retry with 2-3 lexical variants before concluding the "
    "brain has nothing."
)
READ_DESCRIPTION = (
    "Read one note from the brain in full, by id or repo-relative path, with its "
    "supersede history resolved — if the note was replaced, the CURRENT version is "
    "returned along with the chain of what replaced what."
)
LINKS_DESCRIPTION = (
    "Show the [[wikilink]] graph around one note: which notes REFER TO it "
    "(backlinks) and which it points at. Use this to answer 'what else is "
    "connected to X' or to gather everything the brain knows around a subject — "
    "search finds notes by wording, this finds them by relationship, including "
    "notes that never repeat the search term."
)
RECENT_DESCRIPTION = (
    "List notes recently added or touched in the brain (default: last 7 days)."
)
# What every client is told about reading, whatever its profile.
READ_POLICY = (
    "This is the user's permanent second brain. Search it before answering about their "
    "decisions, preferences, projects, people or history. Its trust markers "
    "([provisional — unconsolidated], [journal], [correction pending], an ARCHIVED banner, "
    "a passed review_by) are part of the answer: never present such a note as settled fact. "
    "If a query misses, retry with 2-3 lexical variants."
)
# The capture policy, ONE text for every client of every vendor: the server's
# instructions and the brain_capture description are the only things every MCP
# client receives, so this is where the rule has to live. It travels with the
# tool as well as in `initialize`, because a client may drop one of the two.
#
# It is short on purpose, and a test holds it to a budget: clients truncate
# long instructions, and the sentence a truncation cuts is the last one —
# which is how "ask before capturing somebody else's private life" would stop
# happening. Vendor-neutral on purpose too: "your client's built-in memory"
# is every vendor's, and naming one would make the rule read as theirs.
CAPTURE_POLICY = (
    "Capture without asking: a decision with its why and the alternatives rejected; "
    "a preference or constraint; a fact about the user's world; a hard-won lesson. If a "
    "search found nothing and the user answers, capture it. Offer in one line and carry on; "
    "\"remember this\" captures at once. What other agents need about the user goes here, "
    "not in built-in memory. Never capture credentials, transient state, or what the repo, "
    "git or files record. Open to-dos go in the project's tracker, not here; capture only "
    "the decision behind one. Ask first about a named person's private life, health, "
    "finances or relationships. Never say you cannot fix or delete a note: capture a kind: "
    "correction naming its id or path and quoting the wrong line (describe, never repeat, a "
    "secret or private detail); it stays [correction pending] until a consolidation pass "
    "applies it. Write self-contained text with absolute dates (2026-08-23). Fill kind and "
    "basis (owner-said, agent-found, agent-inferred), and project, job and model when known."
)
# How a correction is written, in the form the engine reads its target from (search
# and read mark that target "[correction pending]" until a pass applies it). It
# travels with brain_capture only: a client needs it when it calls the tool, and the
# instructions have no room left under their budget.
CORRECTION_FORMAT = (
    "To correct a note, start \"Correction to <id or path>: <what is true now>\" or, "
    "for one that should not exist, \"Retract <id or path>: <the user's reason>\". Find its "
    "path with brain_search (scope all reaches inbox notes) or brain_recent. Close a handoff "
    "note (scope handoff) with a correction starting \"Filed <path>: <where now>\" or "
    "\"Already recorded: <path>: <where>\"."
)
# The verbs' own descriptions. `recall` and `delta` say what the contract says (their
# trust markers are fields, not text tags); `remember` carries the capture policy as
# `brain_capture` does, with the correction format in the verbs' own vocabulary.
RECALL_DESCRIPTION = (
    "Search the user's permanent second brain — their decisions, projects, people, "
    "reference notes and life context. Use this BEFORE answering any question about the "
    "user's past decisions, preferences, projects or personal history. `query` searches "
    "notes and returns CURRENT knowledge only (superseded notes are structurally excluded); "
    "its trust markers ([provisional — unconsolidated], [journal], [correction pending], a "
    "passed review_by) are part of the answer, so never present such a note as settled fact. "
    "`entity` returns saved facts (unconsolidated captures) for that label. If a query "
    "misses, retry with 2-3 lexical variants before concluding the brain has nothing."
)
REMEMBER_DESCRIPTION = (
    "Save ONE fact to the brain's inbox, with mandatory attribution. It is scanned for "
    "credentials, committed and backed up with no way to hold it back, and it stays "
    "PROVISIONAL until the brain's consolidation pass promotes it."
)
VERB_CORRECTION_FORMAT = (
    "To correct a note, call remember with note_kind \"correction\" and start the fact "
    "\"Correction to <id or path>: <what is true now>\" or, for one that should not exist, "
    "\"Retract <id or path>: <the user's reason>\". Find its path with recall (scope all "
    "reaches inbox notes) or delta. Close a handoff note (scope handoff) with a correction "
    "starting \"Filed <path>: <where now>\" or \"Already recorded: <path>: <where>\"."
)
FORGET_DESCRIPTION = (
    "Retract one saved fact by its id (from remember, or recall facts[].fact_id), never a "
    "note id or path. The fact leaves recall at once; git keeps the record and consolidation "
    "drains the retraction. Forgetting it again returns expired: false."
)
DELTA_DESCRIPTION = (
    "What changed in the brain since a moment: notes touched after it and facts captured "
    "after it, oldest first, with their trust markers. Pass `since` (ISO 8601), or `cursor` "
    "from the previous reply's next_cursor. Delivery is at-least-once; when has_more is "
    "true, call again with next_cursor.cursor. `pages` is authoritative; `facts` are "
    "ordered by capture time, so a capture synced in with an earlier time than your cursor "
    "shows in `pages` but may never appear in `facts`."
)
READ_ONLY_SENTENCE = "This endpoint is read-only; it cannot save anything."
LOCAL_CAPTURE_DESCRIPTION = (
    "Save a note to the brain's inbox, committed and backed up at once (pass commit: "
    "false to keep it on this machine). It stays PROVISIONAL until the brain's "
    "consolidation pass promotes it. " + CAPTURE_POLICY + " " + CORRECTION_FORMAT
)
REMOTE_CAPTURE_DESCRIPTION = (
    "Save a note to the brain's inbox. It is scanned for credentials, committed and "
    "backed up, with no way to hold it back, and it stays PROVISIONAL until the "
    "brain's consolidation pass promotes it. " + CAPTURE_POLICY + " " + CORRECTION_FORMAT
)


def instructions(profile: str) -> str:
    """The `initialize` instructions for one profile, on every transport."""
    if profile == "capture":
        return READ_POLICY + " " + CAPTURE_POLICY
    return READ_POLICY + " " + READ_ONLY_SENTENCE


def verb_tools(transport: str) -> list:
    """The four memory verbs, with the limits of one transport (contract: tools)."""
    caps = LIMITS[transport]
    entity = {"type": "string", "pattern": verbs.ENTITY_RE.pattern, "maxLength": 100,
              "description": "A label, not an entity page. Filters the facts arm; remember "
                             "writes it."}
    iso = "ISO 8601 date or datetime."
    recall = {
        "name": "recall", "description": RECALL_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {
            "query": {"type": "string", "maxLength": caps["query"],
                      "description": "Search query. Runs the pages arm. Empty only with "
                                     "scope handoff."},
            "entity": entity,
            "scope": {"type": "string", "enum": ["canonical", "all", "handoff"],
                      "description": "canonical (default). 'all' adds the journal and the "
                                     "unprocessed inbox (tagged) and opens the facts arm. "
                                     "'handoff' lists only notes waiting to be filed in a "
                                     "project's own records."},
            "mode": {"type": "string", "enum": ["keyword", "hybrid"],
                     "description": "keyword = FTS only. hybrid = FTS plus meaning, where "
                                    "the engine has it; where it has not, the answer is "
                                    "keyword-only and sets search_degraded. Default: the "
                                    "engine's setting."},
            "limit": {"type": "integer", "minimum": caps["limit"][0],
                      "maximum": caps["limit"][1], "description": "Per-arm cap. Default 8."},
            "offset": {"type": "integer", "minimum": 0, "maximum": caps["offset"],
                       "description": "Scope handoff only: skip this many hits."},
            "since": {"type": "string",
                      "description": iso + " Filters the facts arm only. Unparseable: "
                                     "invalid_params."},
            "budget_tokens": {"type": "integer", "minimum": 1,
                              "description": "Server-side packing, facts first. Cost is "
                                             "ceil(len/4) per fact, ceil(title/4)+"
                                             "ceil(chunk/4) per result."},
            "source_id": {"type": "string", "maxLength": 64,
                          "description": "Only 'default' exists. Any other value: not_found."},
            "question": {"type": "string", "maxLength": 2000,
                         "description": "Accepted and ignored in v1; facts_order stays "
                                        "'newest'."},
            "budget_policy": {"type": "string", "enum": ["facts_first", "query_first"],
                              "description": "Accepted and ignored in v1."},
            "session_id": {"type": "string", "maxLength": 128,
                           "description": "Accepted and ignored in v1."},
        }},
        "annotations": {"readOnlyHint": True},
    }
    kinds = list(provenance.KINDS)
    remember = {
        "name": "remember",
        "description": " ".join((REMEMBER_DESCRIPTION, CAPTURE_POLICY, VERB_CORRECTION_FORMAT)),
        "inputSchema": {"type": "object", "properties": {
            "fact": {"type": "string", "minLength": 1, "maxLength": caps["text"],
                     "description": "The content to save."},
            "provenance": {"type": "string", "maxLength": verbs.PROVENANCE_MAX,
                           "description": "Required. Where the fact came from, free text, one "
                                          "line. Blank: provenance_required."},
            "ttl": {"type": "string",
                    "description": "Duration shorthand ('30d', '12h', '45m') or an absolute "
                                   "ISO 8601 timestamp. ISO durations ('P30D') are refused "
                                   "with a fix. Omitted: never expires."},
            "entity": entity,
            "kind": {"type": "string",
                     "enum": ["event", "preference", "commitment", "belief", "fact"],
                     "description": "Default 'fact'. Maps to a note kind: fact to fact, "
                                    "preference to preference, the rest to other."},
            "visibility": {"type": "string", "enum": ["world", "private"],
                           "description": "Default world."},
            "valid_from": {"type": "string",
                           "description": iso + " When the fact became true. Omitted: write "
                                          "time."},
            "request_id": {"type": "string", "maxLength": 128,
                           "description": "Send the SAME value when retrying a write that may "
                                          "already have succeeded; within 24 hours the retry "
                                          "returns the original result."},
            "note_kind": {"type": "string", "enum": kinds,
                          "description": "Harbor extension. Wins over kind. 'correction' is "
                                         "how a canonical note is corrected or retracted."},
            **{name: dict(spec) for name, spec in provenance.declared_schema().items()
               if name != "kind"},
            "allow_duplicate": {"type": "boolean",
                                "description": "Save even if an identical fact was captured "
                                               "recently. Default false."},
        }, "required": ["fact", "provenance"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    }
    forget = {
        "name": "forget", "description": FORGET_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {
            "id": {"type": "string", "minLength": 1, "maxLength": 128},
            "reason": {"type": "string", "maxLength": 500},
            "request_id": {"type": "string", "maxLength": 128,
                           "description": "Accepted and not used: forget is idempotent."},
        }, "required": ["id"]},
        "annotations": {"readOnlyHint": False, "destructiveHint": False,
                        "idempotentHint": True},
    }
    delta = {
        "name": "delta", "description": DELTA_DESCRIPTION,
        "inputSchema": {"type": "object", "properties": {
            "since": {"type": "string",
                      "description": iso + " Unparseable, or outside 0001-01-01..9999-12-31: "
                                     "invalid_params."},
            "cursor": {"type": "string", "maxLength": 512,
                       "description": "A previous reply's next_cursor.cursor. Wins over "
                                      "since."},
            "budget_tokens": {"type": "integer", "minimum": 1,
                              "description": "Pages pack first, then facts; text fits the "
                                             "budget."},
            "session_id": {"type": "string", "maxLength": 128,
                           "description": "Accepted and ignored: the cursor is stateless. "
                                          "Alone it does not satisfy the since-or-cursor "
                                          "rule."},
        }},
        "annotations": {"readOnlyHint": True},
    }
    return [recall, remember, forget, delta]


def tool_table(transport: str = "local") -> list:
    """Every tool this brain can expose, with the limits of one transport. The one
    table: stdio, `brain serve` and the remote endpoint all read it."""
    caps = LIMITS[transport]
    tools = verb_tools(transport) + [
        {
            "name": "brain_search",
            "description": SEARCH_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Lexical search query.",
                              "maxLength": caps["query"]},
                    "scope": {"type": "string", "enum": ["canonical", "all", "handoff"],
                              "description": "'all' additionally searches the journal and the "
                                             "unprocessed inbox (results tagged provisional). "
                                             "'handoff' lists only notes waiting to be filed in a "
                                             "project's own records — not brain knowledge; the query "
                                             "may be empty or a project slug."},
                    "limit": {"type": "integer", "description": "Max hits (default 8).",
                              "minimum": caps["limit"][0], "maximum": caps["limit"][1]},
                    "offset": {"type": "integer", "minimum": 0, "maximum": caps["offset"],
                               "description": "Scope handoff only: skip this many hits, to "
                                              "page (stable order: handoff_at, then path)."},
                },
                "required": ["query"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "brain_read",
            "description": READ_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id_or_path": {"type": "string", "maxLength": caps["id_or_path"],
                                   "description": "Note id (frontmatter) or path like "
                                                  "knowledge/decisions/2026-07-22-x.md"},
                },
                "required": ["id_or_path"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "brain_links",
            "description": LINKS_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id_or_path": {"type": "string", "maxLength": caps["id_or_path"],
                                   "description": "Note id (frontmatter) or repo-relative path."},
                },
                "required": ["id_or_path"],
            },
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "brain_recent",
            "description": RECENT_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {"days": {"type": "integer", "minimum": caps["days"][0],
                                        "maximum": caps["days"][1],
                                        "description": "Look-back window in days."}},
            },
            "annotations": {"readOnlyHint": True},
        },
    ]
    capture_properties = {
        "text": {"type": "string", "description": "The content to save.",
                 "minLength": 1, "maxLength": caps["text"]},
    }
    # What the caller may declare about the note — the same on every front end
    # (provenance.py). What the ENDPOINT stamps (via, client, source) is never
    # offered: a caller that could set it could claim to be anyone.
    capture_properties.update(provenance.declared_schema())
    if transport == "local":
        capture_properties["commit"] = {
            "type": "boolean",
            "description": ("Commit and auto-push the capture (default true). Pass false to "
                            "write it to inbox/ only, leaving it on this machine for review "
                            "before it reaches the remote. The text is scanned for "
                            "credentials either way."),
        }
    else:
        capture_properties["client_request_id"] = {
            "type": "string", "maxLength": 128,
            "description": ("An opaque id of your own. Send the SAME value when retrying "
                            "a capture that may already have succeeded, and the retry "
                            "returns the original result instead of a second note."),
        }
        capture_properties["allow_duplicate"] = {
            "type": "boolean",
            "description": ("Save even if an identical note was captured recently "
                            "(default false)."),
        }
    tools.append({
        "name": "brain_capture",
        "description": (LOCAL_CAPTURE_DESCRIPTION if transport == "local"
                        else REMOTE_CAPTURE_DESCRIPTION),
        "inputSchema": {"type": "object", "properties": capture_properties,
                        "required": ["text"]},
        # destructiveHint is false because a capture only ever appends: it adds
        # a note to inbox/, it cannot edit or delete an existing one.
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    })
    return tools


def tools_for(profile: str = "capture", transport: str = "local") -> list:
    allowed = PROFILES[profile]
    return [tool for tool in tool_table(transport) if tool["name"] in allowed]


def handoff_listing(name, args: dict) -> dict:
    """`brain_search` with `scope: handoff` lists every handoff note when the query is
    left out, so a missing query there means an empty one."""
    if name == "brain_search" and args.get("scope") == "handoff" and args.get("query") is None:
        return {**args, "query": ""}
    return args


def blank_allowed(name, field: str, args: dict) -> bool:
    """A blank required argument is refused, except a handoff listing's query."""
    return name == "brain_search" and field == "query" and args.get("scope") == "handoff"


def validate_args(tool: dict, args):
    """Check args against the tool's declared inputSchema BEFORE using them.

    A malformed call must come back as a tool error the model can read and
    retry — never as an exception that kills the server for every other session
    sharing it. Returns (clean_args, None) or (None, message).

    The coercions are not sloppiness. Models routinely send "8" for a number
    and "false" for a boolean, and refusing a whole search over a formatting nit
    helps nobody. Each one accepts exactly the unambiguous spellings and refuses
    the rest — silently reading a truthy "false" as True would commit and push a
    capture the caller asked to hold back."""
    schema = tool.get("inputSchema", {})
    props = schema.get("properties", {})
    if not isinstance(args, dict):
        return None, "arguments must be a JSON object"
    required = set(schema.get("required", []))
    # An argument this tool does not declare is REFUSED, not ignored.
    #
    # JSON Schema without `additionalProperties: false` says to ignore extras,
    # and for a search that would be the friendlier reading. For a capture it
    # is a trap, and it sprang on the first live run of the cutover: a probe
    # sent `request_id` where the tool declares `client_request_id`. The
    # capture succeeded, the retry-safety it had asked for was silently off,
    # and the only reason a duplicate note did not land is that the content
    # happened to match the dedup fingerprint too. A client retrying after a
    # timeout — which is the entire reason that argument exists — would have
    # written the note twice and had no way to know.
    #
    # Keys beginning with an underscore are tolerated: `_meta` and friends are
    # a reserved namespace a client may decorate a call with, and refusing
    # those would break conformant clients to catch nothing.
    unknown = sorted(k for k in args if k not in props and not k.startswith("_"))
    if unknown:
        accepted = ", ".join(sorted(props)) or "no arguments"
        hint = ""
        near = difflib.get_close_matches(unknown[0], list(props), n=1, cutoff=0.6)
        if near:
            # The failure is nearly always a typo, and naming the intended
            # argument is the difference between a message that fixes it and a
            # message that starts a search.
            hint = " Did you mean %r?" % near[0]
        return None, ("unknown argument %r.%s %s accepts: %s"
                      % (unknown[0], hint, tool.get("name", "this tool"), accepted))
    if isinstance(args, dict):
        args = handoff_listing(tool.get("name"), args)
    for field in schema.get("required", []):
        if field not in args or args[field] is None \
                or (isinstance(args[field], str) and not args[field].strip()
                    and not blank_allowed(tool.get("name"), field, args)):
            return None, "missing required argument %r" % field
    clean = {}
    for field, spec in props.items():
        if field not in args:
            continue
        value = args[field]
        # An explicit null on an OPTIONAL argument means "not supplied" — fall
        # back to the default rather than failing a whole search over it.
        if value is None and field not in required:
            continue
        expected = spec.get("type")
        if expected == "string":
            if not isinstance(value, str):
                return None, "argument %r must be a string, got %s" % (field,
                                                                      type(value).__name__)
            cap = spec.get("maxLength")
            if cap is not None and len(value) > cap:
                return None, "argument %r is %d characters — the cap is %d" % (
                    field, len(value), cap)
            if len(value) < spec.get("minLength", 0):
                return None, "argument %r is empty" % field
            # fullmatch, never match-with-$: `$` also matches before a trailing newline,
            # which is exactly the character a label must not carry.
            if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
                return None, "argument %r does not match the required form %s" % (
                    field, spec["pattern"])
        elif expected == "integer":
            if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
                value = int(value)
            # bool is an int subclass in Python; a JSON true is not a count.
            if isinstance(value, bool) or not isinstance(value, int):
                return None, "argument %r must be an integer, got %s" % (field,
                                                                        type(value).__name__)
            low, high = spec.get("minimum"), spec.get("maximum")
            if low is not None and value < low:
                return None, "argument %r must be at least %d" % (field, low)
            if high is not None and value > high:
                return None, "argument %r must be at most %d" % (field, high)
        elif expected == "boolean":
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "false"):
                    value = lowered == "true"
            if not isinstance(value, bool):
                return None, "argument %r must be a boolean, got %s" % (field,
                                                                       type(value).__name__)
        if "enum" in spec and value not in spec["enum"]:
            return None, "argument %r must be one of %s" % (field, spec["enum"])
        clean[field] = value
    return clean, None


def cli_for(name: str, args: dict, transport: str = "local", declared=None) -> list:
    """The bin/brain argv one validated tool call becomes. `declared` is a
    capture's cleaned declared fields (provenance.take_declared).

    A remote caller's read tools carry `--hide-private`: a capture marked
    `visibility: private` does not exist for them (the same rule the facts arm of
    `recall` follows). It sits before any positional argument, so it can never be
    read as query text."""
    hide = ["--hide-private"] if transport == "remote" else []
    if name == "brain_search":
        cli = ["search", *hide, "--limit", str(args.get("limit", 8))]
        if args.get("scope") in ("all", "handoff"):
            cli += ["--scope", args["scope"]]
        if args.get("scope") == "handoff" and args.get("offset") is not None:
            cli += ["--offset", str(args["offset"])]
        cli += ["--", args["query"]]      # query text, never parsed as flags
        return cli
    if name == "brain_read":
        return ["read", *hide, args["id_or_path"]]
    if name == "brain_links":
        return ["links", *hide, args["id_or_path"]]
    if name == "brain_recent":
        return ["recent", *hide, "--days", str(args.get("days", 7))]
    if name == "brain_capture":
        # Default stays True on both transports, and remotely there is no way
        # to ask for anything else: committing is what backs the note up, and
        # an uncommitted note dirties the tree, which blocks the consolidation
        # pass that is the inbox's only drain.
        cli = ["capture"]
        if transport == "remote" or args.get("commit", True):
            cli.append("--commit")
        # The text goes LAST: capture lifts its flags out of argv by name, so a
        # note whose whole text is "--kind" must not find a value after it.
        return cli + provenance.cli_flags(declared or {}) + [args["text"]]
    raise KeyError(name)


def error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def served_verbs(profile: str) -> list:
    """The memory verbs one profile serves."""
    return [verb for verb in verbs.SERVED if verb in PROFILES[profile]]


def call(name, args, profile: str = "capture", transport: str = "local",
         runner=None, timeout: float = 120.0, write=None):
    """Dispatch one tools/call. Never raises; every failure is an isError result.

    The profile is re-checked here even though `tools_for` already filtered the
    advertised list. Hiding a tool is usability — a client can still construct
    the call — so this is where the read-only boundary is actually enforced.

    `write` is how a verb's write lands: a callable the transport supplies
    (see `call_verb`). Without one `remember` and `forget` refuse, because this
    layer has no write path of its own."""
    if isinstance(name, str) and name in verbs.UNSUPPORTED:
        return verbs.result(verbs.unavailable(name, served_verbs(profile)))
    if not isinstance(name, str) or name not in PROFILES["capture"]:
        # isinstance first: a non-string name (dict/list) is unhashable and
        # would raise on the membership test instead of reporting an unknown tool.
        return error_result("unknown tool %r" % (name,))
    if name not in PROFILES[profile]:
        if name in verbs.SERVED:
            return verbs.result(verbs.scope_denied(name, served_verbs(profile)))
        return error_result(
            "tool %r is not available on this endpoint (profile %r is read-only)"
            % (name, profile))
    if name in verbs.SERVED:
        return call_verb(name, args, transport, remote=(transport == "remote"),
                         runner=runner, timeout=timeout, write=write)
    table = {tool["name"]: tool for tool in tool_table(transport)}
    args = args if args is not None else {}
    declared, ignored = {}, []
    if name == "brain_capture":
        # Declared fields are dropped when bad, never refused: see provenance.
        args, declared, ignored = provenance.take_declared(args)
    args, problem = validate_args(table[name], args)
    if problem:
        return error_result("invalid arguments for %s: %s" % (name, problem))
    try:
        run = (runner or _run_cli)(cli_for(name, args, transport, declared), timeout)
    except Exception as exc:
        return error_result(_failure_hint(exc, transport))
    text = (run.stdout + ("\n" + run.stderr if run.stderr.strip() else "")).strip()
    if ignored:
        text = provenance.ignored_line(ignored) + "\n" + text
    return {"content": [{"type": "text", "text": text or "(no output)"}],
            "isError": run.returncode != 0}


def _verb_cli(run, op: str, request: dict, timeout: float) -> dict:
    """One `brain verb` op: the request as JSON out, on stdin (a fact may be far larger
    than one command-line argument can be), the answer's last JSON line back (hooks and
    git may print before it)."""
    done = run(["verb", op, "-"], timeout, input=json.dumps(request))
    for line in reversed((done.stdout or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise RuntimeError("brain verb %s returned no result (exit %s)" % (op, done.returncode))


def _invalid_arguments(tool: dict, problem: str) -> dict:
    accepted = ", ".join(sorted(tool["inputSchema"]["properties"]))
    return verbs.invalid("invalid arguments for %s: %s" % (tool["name"], problem),
                         "Fix the argument and call again. %s accepts: %s." % (
                             tool["name"], accepted))


def call_verb(name: str, args, transport: str = "local", remote: bool = False,
              runner=None, timeout: float = 120.0, write=None):
    """One memory-verb call, already past the profile check. Every outcome is a
    verb response or the verb error envelope; nothing here raises.

    `write(text=, declared=, flags=, request_id=, allow_duplicate=, dedupe_text=)` lands a capture
    and returns the capture outcome (`capture.Capturer.capture`'s shape). The
    transport owns it because the transports differ in who the principal is, what
    is stamped (`via`, `client`) and whether a push queue exists."""
    tool = next(t for t in tool_table(transport) if t["name"] == name)
    args = args if args is not None else {}
    declared, ignored = {}, []
    if name == "remember":
        args, declared, ignored, early = verbs.split_remember(args)
        if early:
            return verbs.result(early)
    clean, problem = validate_args(tool, args)
    if problem:
        return verbs.result(_invalid_arguments(tool, problem))
    now = datetime.now(timezone.utc)
    run = runner or _run_cli
    try:
        if name == "recall":
            request, refused = verbs.prepare_recall(clean, remote, now)
            return verbs.result(refused or _verb_cli(run, "recall", request, timeout))
        if name == "delta":
            request, refused = verbs.prepare_delta(clean, remote, now)
            return verbs.result(refused or _verb_cli(run, "delta", request, timeout))
        if write is None:
            return verbs.result(verbs.error(
                "unavailable", "This brain is configured without a write path.",
                "Use an endpoint that can save; this one only reads."))
        if name == "remember":
            request, refused = verbs.prepare_remember(clean, now)
            if refused:
                return verbs.result(refused)
            if _verb_cli(run, "withdrawn", {"text": request["text"]}, timeout)["withdrawn"]:
                return verbs.result(verbs.invalid(
                    "fact_withdrawn: this claim was forgotten and its retraction is still "
                    "pending, so it cannot be remembered again yet.",
                    "Forget is final until consolidation drains the retraction. State the "
                    "claim differently, or correct it with note_kind 'correction'."))
            outcome = write(text=request["text"],
                            declared={**declared, "kind": request["note_kind"]},
                            flags=request["flags"], request_id=request["request_id"],
                            allow_duplicate=request["allow_duplicate"],
                            dedupe_text=request["dedupe_text"])
            if not outcome.get("ok"):
                return verbs.result(verbs.write_error(outcome))
            # A declared field dropped by the capture itself is named as well.
            later = [n for n in outcome.get("ignored") or [] if n not in ignored]
            return verbs.result(verbs.remember_response(
                outcome, request, [n for n in ("basis", "project", "job", "model")
                                   if n in ignored or n in later]))
        planned, refused = verbs.prepare_forget(clean)         # name == "forget"
        if refused:
            return verbs.result(refused)
        stem, reason = planned
        plan = _verb_cli(run, "forget-plan", {"stem": stem}, timeout)
        state = plan["state"]
        if state == "missing":
            return verbs.result(verbs.error(
                "not_found", "No saved fact has the id 'inbox-%s'." % stem[:60],
                "Use a fact id from remember or from recall's facts[].fact_id. A fact that "
                "consolidation already drained is gone."))
        if state == "pending":
            return verbs.result(verbs.forget_response(stem, False, reason))
        outcome = write(text=verbs.retraction_text(stem, reason), declared={"kind": "correction"},
                        flags=verbs.retraction_flags(stem, bool(plan.get("private"))),
                        request_id=None, allow_duplicate=True)
        if not outcome.get("ok"):
            return verbs.result(verbs.write_error(outcome))
        return verbs.result(verbs.forget_response(stem, True, reason))
    except Exception as exc:
        return verbs.result(verbs.error(
            "internal", "The brain could not complete %s." % name,
            _failure_hint(exc, "remote" if remote else transport)))


def _failure_hint(exc, transport: str) -> str:
    if transport == "remote":
        # Never name a filesystem path to a remote caller: it discloses the
        # container's layout and it is not actionable from the other end of a
        # tunnel anyway.
        return ("brain tool failed: %s. The brain service could not complete the "
                "operation; retry once, then report it." % type(exc).__name__)
    return ("brain tool failed: %r. Fallback: search the files directly with rg in %s "
            "(archive/ and vault/ are excluded by .rgignore)." % (exc, ENGINE))


def _run_cli(args: list, timeout: float, input=None):
    return subprocess.run([sys.executable, BRAIN, *args], capture_output=True,
                          text=True, cwd=ENGINE, timeout=timeout, input=input)
