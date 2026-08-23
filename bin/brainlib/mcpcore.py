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
- **Profile** decides WHICH TOOLS exist. `read` is the four retrieval tools;
  `capture` is those four plus `brain_capture`. Nothing else is ever added by
  a request — the profile comes from the authenticated audience, never from a
  parameter.

`tools_for` filtering the advertised list is usability. `call` re-checking the
profile is the security boundary, and it is checked again there precisely
because a client can construct a call for a tool that was never advertised.
"""
from __future__ import annotations

import difflib
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent.parent
BRAIN = str(ENGINE / "bin" / "brain")

READ_TOOLS = ("brain_search", "brain_read", "brain_links", "brain_recent")
CAPTURE_TOOLS = READ_TOOLS + ("brain_capture",)
PROFILES = {"read": READ_TOOLS, "capture": CAPTURE_TOOLS}

# The remote caps come from the design spec; the local ones are what stdio has
# always allowed. They differ because the threat differs: a local caller is the
# owner at their own keyboard, a remote one is anything that got through Access.
LIMITS = {
    "local": {"query": 100_000, "id_or_path": 100_000, "text": 100_000,
              "limit": (1, 100), "days": (1, 3650)},
    "remote": {"query": 2_048, "id_or_path": 512, "text": 65_536,
               "limit": (1, 20), "days": (1, 365)},
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
# The capture policy travels WITH the tool, because the model reading this
# description is the only thing that applies it. Shortening it to fit a UI is
# how "ask before capturing somebody else's private life" stops happening.
CAPTURE_POLICY = (
    "Capture the WHY, not the what — the what is recoverable from git, files and "
    "calendars; the reasoning evaporates within a week. Never capture credentials "
    "of any kind. ALWAYS ask first before capturing anything about a named "
    "person's private life, health, finances or relationships. Text should be "
    "self-contained and use absolute dates (2026-08-23), never 'today'."
)
LOCAL_CAPTURE_DESCRIPTION = (
    "Save a thought, fact, or decision into the brain's inbox, committed and "
    "auto-backed-up to GitHub immediately. Use when the user says 'remember this' "
    "or states something durable worth keeping. " + CAPTURE_POLICY
)
REMOTE_CAPTURE_DESCRIPTION = (
    "Save a thought, fact, or decision into the brain's inbox. The note is "
    "scanned for credentials, written atomically, committed, and queued for "
    "backup — always, with no option to hold it back. It lands as PROVISIONAL "
    "and is not canonical knowledge until the brain's own consolidation pass "
    "promotes it. Use when the user says 'remember this' or states something "
    "durable worth keeping. " + CAPTURE_POLICY
)


def tool_table(transport: str = "local") -> list:
    """Every tool this brain can expose, with the limits of one transport."""
    caps = LIMITS[transport]
    tools = [
        {
            "name": "brain_search",
            "description": SEARCH_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Lexical search query.",
                              "maxLength": caps["query"]},
                    "scope": {"type": "string", "enum": ["canonical", "all"],
                              "description": "'all' additionally searches the journal and the "
                                             "unprocessed inbox (results tagged provisional)."},
                    "limit": {"type": "integer", "description": "Max hits (default 8).",
                              "minimum": caps["limit"][0], "maximum": caps["limit"][1]},
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
    for field in schema.get("required", []):
        if field not in args or args[field] is None \
                or (isinstance(args[field], str) and not args[field].strip()):
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


def cli_for(name: str, args: dict, transport: str = "local") -> list:
    """The bin/brain argv one validated tool call becomes."""
    if name == "brain_search":
        cli = ["search", "--limit", str(args.get("limit", 8))]
        if args.get("scope") == "all":
            cli += ["--scope", "all"]
        cli += ["--", args["query"]]      # query text, never parsed as flags
        return cli
    if name == "brain_read":
        return ["read", args["id_or_path"]]
    if name == "brain_links":
        return ["links", args["id_or_path"]]
    if name == "brain_recent":
        return ["recent", "--days", str(args.get("days", 7))]
    if name == "brain_capture":
        # Default stays True on both transports, and remotely there is no way
        # to ask for anything else: committing is what backs the note up, and
        # an uncommitted note dirties the tree, which blocks the consolidation
        # pass that is the inbox's only drain.
        cli = ["capture", args["text"]]
        if transport == "remote" or args.get("commit", True):
            cli.append("--commit")
        return cli
    raise KeyError(name)


def error_result(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def call(name, args, profile: str = "capture", transport: str = "local",
         runner=None, timeout: float = 120.0):
    """Dispatch one tools/call. Never raises; every failure is an isError result.

    The profile is re-checked here even though `tools_for` already filtered the
    advertised list. Hiding a tool is usability — a client can still construct
    the call — so this is where the read-only boundary is actually enforced."""
    if not isinstance(name, str) or name not in PROFILES["capture"]:
        # isinstance first: a non-string name (dict/list) is unhashable and
        # would raise on the membership test instead of reporting an unknown tool.
        return error_result("unknown tool %r" % (name,))
    if name not in PROFILES[profile]:
        return error_result(
            "tool %r is not available on this endpoint (profile %r is read-only)"
            % (name, profile))
    table = {tool["name"]: tool for tool in tool_table(transport)}
    args, problem = validate_args(table[name], args if args is not None else {})
    if problem:
        return error_result("invalid arguments for %s: %s" % (name, problem))
    try:
        run = (runner or _run_cli)(cli_for(name, args, transport), timeout)
    except Exception as exc:
        return error_result(_failure_hint(exc, transport))
    text = (run.stdout + ("\n" + run.stderr if run.stderr.strip() else "")).strip()
    return {"content": [{"type": "text", "text": text or "(no output)"}],
            "isError": run.returncode != 0}


def _failure_hint(exc, transport: str) -> str:
    if transport == "remote":
        # Never name a filesystem path to a remote caller: it discloses the
        # container's layout and it is not actionable from the other end of a
        # tunnel anyway.
        return ("brain tool failed: %s. The brain service could not complete the "
                "operation; retry once, then report it." % type(exc).__name__)
    return ("brain tool failed: %r. Fallback: search the files directly with rg in %s "
            "(archive/ and vault/ are excluded by .rgignore)." % (exc, ENGINE))


def _run_cli(args: list, timeout: float):
    return subprocess.run([sys.executable, BRAIN, *args], capture_output=True,
                          text=True, cwd=ENGINE, timeout=timeout)
