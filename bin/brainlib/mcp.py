# bin/brainlib/mcp.py
"""The MCP tool layer — what the brain exposes, independent of how it is reached.

Split out of bin/brain-mcp on 2026-07-29, when a second transport arrived. The
split is not tidiness: two transports that each carried their own tool table
would drift, and a tool present over stdio and missing over HTTP is a bug
nobody finds until somebody far from their laptop needs it. There is one table,
one validator, one dispatcher, and the transports are thin.

It shells out to bin/brain for the actual work, which is what makes retrieval
behaviour identical on the CLI, over stdio, and over HTTP — the ranking and the
trust signals are deterministic code, not model judgement.

One table, but not always all of it: `handle` and `call_tool` take an `allow`
set, which is how `brain serve --read-only` exposes four tools instead of five
without a second table to drift out of sync with this one. `allow=None` — every
tool — stays the default, because that is what stdio is: a subprocess a client
spawned on the machine the user is already sitting at.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BRAIN = str(ROOT / "bin" / "brain")

TOOLS = [
    {
        "name": "brain_search",
        "description": (
            "Search the user's permanent second brain — their decisions, projects, people, "
            "reference notes, and life context. Use this BEFORE answering any question "
            "about the user's past decisions, preferences, projects, or personal history. "
            "Returns CURRENT knowledge only (superseded notes are structurally excluded). "
            "If a query misses, retry with 2-3 lexical variants before concluding the "
            "brain has nothing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Lexical search query."},
                "scope": {"type": "string", "enum": ["canonical", "all"],
                          "description": "'all' additionally searches the journal and the "
                                         "unprocessed inbox (results tagged provisional)."},
                "limit": {"type": "integer", "description": "Max hits (default 8)."},
            },
            "required": ["query"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "brain_read",
        "description": (
            "Read one note from the brain in full, by id or repo-relative path, with its "
            "supersede history resolved — if the note was replaced, the CURRENT version is "
            "returned along with the chain of what replaced what."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_or_path": {"type": "string",
                               "description": "Note id (frontmatter) or path like "
                                              "knowledge/decisions/2026-07-22-x.md"},
            },
            "required": ["id_or_path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "brain_links",
        "description": (
            "Show the [[wikilink]] graph around one note: which notes REFER TO it "
            "(backlinks) and which it points at. Use this to answer 'what else is "
            "connected to X' or to gather everything the brain knows around a subject — "
            "search finds notes by wording, this finds them by relationship, including "
            "notes that never repeat the search term."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_or_path": {"type": "string",
                               "description": "Note id (frontmatter) or repo-relative path."},
            },
            "required": ["id_or_path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "brain_recent",
        "description": "List notes recently added or touched in the brain (default: last 7 days).",
        "inputSchema": {
            "type": "object",
            "properties": {"days": {"type": "integer", "description": "Look-back window in days."}},
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "brain_capture",
        "description": (
            "Save a thought, fact, or decision into the brain's inbox, committed and "
            "auto-backed-up to GitHub immediately. Use when the user says 'remember this' "
            "or states something durable worth keeping. Text should be self-contained "
            "with absolute dates."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The content to save."},
                "commit": {
                    "type": "boolean",
                    "description": (
                        "Commit and auto-push the capture (default true). Pass false to "
                        "write it to inbox/ only, leaving it on this machine for review "
                        "before it reaches the remote. The text is scanned for "
                        "credentials either way."
                    ),
                },
            },
            "required": ["text"],
        },
        # The one tool that is not read-only, and the reason --read-only exists.
        # destructiveHint is false because a capture only ever appends: it adds
        # a note to inbox/, it cannot edit or delete an existing one.
        "annotations": {"readOnlyHint": False, "destructiveHint": False},
    },
]


TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
MAX_ARG_CHARS = 100_000  # a note or query longer than this is a bug or an abuse


def read_only_names(tools=None) -> tuple:
    """The tools safe to serve when the caller must not be able to write.

    Derived from the table rather than written out beside it, so there is one
    place a tool is declared. It fails CLOSED, and that direction is the whole
    point: a tool added later with no annotation, or with a truthy-but-not-True
    one, is left OUT of read-only serving. Getting that wrong the other way
    means a write tool on a socket somebody believed was read-only, which is a
    silent failure; being left out is a loud one, and tests/test_serve.py also
    refuses a tool that declares nothing at all.

    readOnlyHint is the MCP specification's own annotation, so this is not a
    private convention — it is information clients already receive.
    """
    return tuple(tool["name"] for tool in (TOOLS if tools is None else tools)
                 if isinstance(tool.get("annotations"), dict)
                 and tool["annotations"].get("readOnlyHint") is True)


READ_ONLY_TOOLS = read_only_names()


def run_cli(args):
    return subprocess.run(
        [sys.executable, BRAIN, *args], capture_output=True, text=True, cwd=ROOT, timeout=120
    )


def validate_args(name, args):
    """Check args against the tool's declared inputSchema BEFORE using them.

    A malformed call must come back as a tool error the model can read and
    retry — never as an exception that kills the server for every other
    session sharing it. Returns (clean_args, None) or (None, message).
    """
    schema = TOOLS_BY_NAME[name].get("inputSchema", {})
    props = schema.get("properties", {})
    if not isinstance(args, dict):
        return None, "arguments must be a JSON object"
    for field in schema.get("required", []):
        if field not in args or args[field] is None \
                or (isinstance(args[field], str) and not args[field].strip()):
            return None, f"missing required argument {field!r}"
    clean = {}
    required = set(schema.get("required", []))
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
                return None, f"argument {field!r} must be a string, got {type(value).__name__}"
            if len(value) > MAX_ARG_CHARS:
                return None, (f"argument {field!r} is {len(value)} chars — the cap is "
                              f"{MAX_ARG_CHARS}")
        elif expected == "integer":
            # Models routinely send "8" for a number; accept the digit string
            # rather than refusing to search over a formatting nit.
            if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
                value = int(value)
            # bool is an int subclass in Python; a JSON true is not a count.
            if isinstance(value, bool) or not isinstance(value, int):
                return None, f"argument {field!r} must be an integer, got {type(value).__name__}"
        elif expected == "boolean":
            # Same tolerance as the integer branch, and for the same reason:
            # models send "false" as a string. Coerce the unambiguous spellings
            # and refuse the rest — silently accepting a truthy "false" would
            # commit and push a capture the caller asked to hold back.
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in ("true", "false"):
                    value = lowered == "true"
            if not isinstance(value, bool):
                return None, f"argument {field!r} must be a boolean, got {type(value).__name__}"
        if "enum" in spec and value not in spec["enum"]:
            return None, f"argument {field!r} must be one of {spec['enum']}"
        clean[field] = value
    return clean, None


def call_tool(name, args, allow=None, source=None):
    # isinstance first: a non-string name (dict/list) is unhashable and would
    # raise on the membership test instead of reporting an unknown tool.
    if not isinstance(name, str) or name not in TOOLS_BY_NAME:
        return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
    if allow is not None and name not in allow:
        # The enforcement point, and it is deliberately HERE rather than in the
        # transport: a restriction that lives beside the dispatcher cannot be
        # skipped by a caller who never read tools/list, and a client is exactly
        # what this mode is defending against.
        #
        # It says which tool and why, instead of claiming the tool does not
        # exist. Whether this server is read-only is printed in its own startup
        # banner and is a property of how it was launched, not a secret — and a
        # model told the truth stops retrying, where one told "unknown tool"
        # reasonably tries three spellings of the name first.
        return {"content": [{"type": "text", "text":
                             f"{name} is not served by this brain: it is running read-only, "
                             "which serves the four read tools and refuses the write one. "
                             "Retrieval is unaffected."}],
                "isError": True}
    args, problem = validate_args(name, args)
    if problem:
        return {"content": [{"type": "text", "text": f"invalid arguments for {name}: {problem}"}],
                "isError": True}
    if name == "brain_search":
        cli = ["search", "--limit", str(args.get("limit", 8))]
        if args.get("scope") == "all":
            cli += ["--scope", "all"]
        cli += ["--", args["query"]]      # query text, never parsed as flags
    elif name == "brain_read":
        cli = ["read", args["id_or_path"]]
    elif name == "brain_links":
        cli = ["links", args["id_or_path"]]
    elif name == "brain_recent":
        cli = ["recent", "--days", str(args.get("days", 7))]
    elif name == "brain_capture":
        # Default stays True: committing is what backs the note up, and an
        # uncommitted note dirties the tree, which blocks the consolidation pass
        # that is the inbox's only drain. The flag exists so a caller CAN hold a
        # capture back from the remote deliberately.
        cli = ["capture", args["text"]]
        if args.get("commit", True):
            cli.append("--commit")
        if source:
            # Provenance is stamped by the ENDPOINT, never claimed by the
            # caller. `source` comes from how this server was started; a
            # `source` in the request arguments never reaches this line,
            # because validate_args rebuilds the argument dict from the
            # declared schema and the schema does not offer the field. An
            # agent that can lie about its content can lie about its label, so
            # the label has to come from somewhere the agent cannot reach.
            #
            # None — stdio — passes nothing and leaves the default to the CLI,
            # which calls it `local`. Two spellings of the default would be
            # two things to keep in step.
            cli += ["--source", source]
    try:
        run = run_cli(cli)
    except Exception as exc:
        return {"content": [{"type": "text", "text": f"brain tool failed: {exc!r}. "
                             f"Fallback: search the files directly with rg in {ROOT} "
                             "(archive/ and vault/ are excluded by .rgignore)."}],
                "isError": True}
    text = (run.stdout + ("\n" + run.stderr if run.stderr.strip() else "")).strip() or "(no output)"
    return {"content": [{"type": "text", "text": text}], "isError": run.returncode != 0}


def handle(msg, allow=None, source=None):
    """One JSON-RPC message in, one reply out — or None for a notification.

    Returns rather than writes, which is the whole reason a second transport is
    possible. The old version printed to stdout, so it could only ever serve
    stdio; HTTP needs the answer as a value, and needs to be able to tell a
    request (answer it) from a notification (202, no body).

    `allow` is the set of tool names this caller may reach, or None for all of
    them. It filters tools/list and gates tools/call — both, because either one
    alone is a mode a client can step around.

    `source` is what this endpoint stamps on anything it writes, or None to
    leave the CLI's own default. It travels the same way `allow` does — from
    how the server was started, past the message entirely — for the same
    reason: a property of the deployment must not be settable by the caller.
    """
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        return _reply(msg_id, {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "brain", "version": "1.0.0"},
        })
    if method == "tools/list":
        served = TOOLS if allow is None else [t for t in TOOLS if t["name"] in allow]
        return _reply(msg_id, {"tools": served})
    if method == "tools/call":
        args = params.get("arguments")
        return _reply(msg_id, call_tool(params.get("name", ""),
                                        args if args is not None else {}, allow=allow,
                                        source=source))
    if method == "ping":
        return _reply(msg_id, {})
    if msg_id is not None:
        return _reply(msg_id, error={"code": -32601, "message": f"method not found: {method}"})
    # A notification (no id) is answered by saying nothing, per JSON-RPC.
    return None


def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    return out


MAX_LINE_CHARS = 10_000_000  # a single request larger than this is not real traffic


def serve_stdio():
    """The stdio transport: a line in, a line out. Unchanged in behaviour."""
    for line in sys.stdin:
        line = line.strip()
        if not line or len(line) > MAX_LINE_CHARS:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            # Deliberately broad: an unparseable line is skipped per JSON-RPC
            # (there is no id to answer). json.loads raises more than
            # JSONDecodeError — deeply nested input raises RecursionError,
            # which used to take the whole server down with it.
            continue
        if not isinstance(msg, dict):
            continue
        # Top-level containment: one malformed request must never take the
        # server down with it — every other session is sharing it.
        try:
            out = handle(msg)
        except Exception as exc:
            msg_id = msg.get("id")
            if msg_id is None:
                continue
            out = _reply(msg_id, error={"code": -32603, "message": f"internal error: {exc!r}"})
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
