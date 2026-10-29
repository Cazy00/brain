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
set, which is how `brain serve --read-only` exposes four tools instead of five,
and `--drop-box` exactly one, without a second table to drift out of sync with
this one. `allow=None` — every tool — stays the default, because that is what
stdio is: a subprocess a client spawned on the machine the user is already
sitting at.

More properties of the ENDPOINT travel the same way, never through the
message: `source` (what trust class a write is stamped with), `via` (the
transport it arrived through) and `cap` (how much this endpoint may write
today). A caller must not be able to set any of them, so none is a tool
argument. `client` is stamped from the client's own clientInfo, which is
self-reported and said to be.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from . import capture, mcpcore, osbackend, provenance, verbs, version

ROOT = Path(__file__).resolve().parent.parent.parent
BRAIN = str(ROOT / "bin" / "brain")

# The one table is mcpcore's. This module used to carry a copy of it for stdio, which is
# how two transports come to disagree about a tool; it now reads the same table the
# remote endpoint does, with the local transport's limits.
TOOLS = mcpcore.tool_table("local")
TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


def _named_by_hint(hint: bool, tools=None) -> tuple:
    """Tool names whose readOnlyHint is EXACTLY this boolean.

    Derived from the table rather than written out beside it, so there is one
    place a tool is declared. Identity against True/False, not truthiness, is
    what makes both restricted modes fail CLOSED: a tool added later with no
    annotation, or with a truthy-but-not-boolean one, lands in NEITHER set and
    is therefore served by neither mode. Getting that wrong in the other
    direction means a write tool on a socket somebody believed was read-only,
    or a read tool on a drop box — silent failures both; being left out is a
    loud one, and tests/test_serve.py also refuses a tool that declares
    nothing at all.

    readOnlyHint is the MCP specification's own annotation, so this is not a
    private convention — it is information clients already receive.
    """
    return tuple(tool["name"] for tool in (TOOLS if tools is None else tools)
                 if isinstance(tool.get("annotations"), dict)
                 and tool["annotations"].get("readOnlyHint") is hint)


def read_only_names(tools=None) -> tuple:
    """The tools safe to serve when the caller must not be able to write."""
    return _named_by_hint(True, tools)


def write_only_names(tools=None) -> tuple:
    """The mirror: the tools safe to serve when the caller must not be able to
    READ. A drop box is not a small brain — it is a slot, and the thing on the
    other side of it is not trusted with a single note."""
    return _named_by_hint(False, tools)


READ_ONLY_TOOLS = read_only_names()
# Every tool that writes: what the `write` OAuth scope opens.
WRITE_ONLY_TOOLS = write_only_names()
# What a drop box serves: `brain_capture` and nothing else. It is a slot, not a small
# brain; `remember` and `forget` are for an endpoint whose caller may also read.
DROP_BOX_TOOLS = ("brain_capture",)


def run_cli(args):
    return subprocess.run(
        [sys.executable, BRAIN, *args], capture_output=True, text=True, cwd=ROOT, timeout=120
    )


def _error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


def drop_box_reply(run):
    """What an endpoint that stamps a foreign source hands back: an
    acknowledgement, and nothing else.

    An endpoint that has to stamp provenance is by definition one the operator
    is not sitting at, so the CLI's own output must not be forwarded. It
    carries the note's ABSOLUTE PATH — where this brain lives on disk — and on
    a failed commit it carries git's and lint's stderr, which is text about
    OTHER notes. Each of those is a read channel out of a brain the caller is
    not supposed to be able to read at all, and clause 3 of the security
    contract is that this endpoint acknowledges a write and stops.

    So: success is the new note's id, which is derived from the caller's own
    text and tells them nothing they did not send. Failure is that it failed.
    The reason goes to the operator's terminal, which is the one place it is
    both useful and safe.

    Success is "the note reached disk", NOT "the CLI exited 0". `brain capture`
    exits 1 when the note is written but the commit fails — an unconfigured git
    identity, a consolidation branch — and the note is explicitly not lost.
    Reporting that as a failure invites the bot to retry the same claim until
    the daily cap stops it, which is a worse outcome than an uncommitted note
    and fills the inbox with duplicates of it. The answer is identical either
    way, so the caller still learns nothing about this brain's state; the
    operator sees the real reason on the terminal, which is where it can be
    acted on. Found 2026-07-30 by running a real drop box on a host with no
    git identity configured.
    """
    written = [line.strip() for line in (run.stdout or "").splitlines()
               if line.strip().endswith(".md")]
    if run.returncode != 0:
        detail = (run.stdout or "").strip() + (("\n" + run.stderr.strip())
                                               if (run.stderr or "").strip() else "")
        # Terse to the caller, not silent to the owner: a drop box whose
        # failures are invisible is one that stops working quietly.
        sys.stderr.write("  drop box: capture problem: " + (detail or "no detail") + "\n")
        sys.stderr.flush()
        if not written:
            return _error("this brain did not accept that capture")
    note_id = Path(written[-1]).stem if written else ""
    return {"content": [{"type": "text",
                         "text": f"captured {note_id}".strip()}], "isError": False}


_CAPTURERS = {}


def data_root() -> Path:
    """Where the notes live: BRAIN_DATA_ROOT, else the engine's own folder. The same
    rule `bin/brain` applies, because the verbs' writes run it with this as the cwd."""
    raw = os.environ.get("BRAIN_DATA_ROOT", "").strip()
    return Path(raw).expanduser().resolve() if raw else ROOT


def local_capturer(root=None):
    """The write path of `remember` and `forget` on stdio and `brain serve`.

    The same transaction the remote endpoint runs (brainlib/capture.py), so a
    `request_id` means the same thing on every transport: a retry within 24 hours
    returns the original instead of a second note. There is no push queue: the
    post-commit hook pushes, as it does for `brain_capture` on this machine. The
    ledger sits with the rest of the machine-local state, outside the repository."""
    root = Path(root or data_root())
    override = os.environ.get("BRAIN_STATE_DIR", "").strip()
    state = Path(override).expanduser() if override else osbackend.state_dir(root, create=True)
    key = (str(root), str(state))
    if key not in _CAPTURERS:
        _CAPTURERS[key] = capture.Capturer(
            BRAIN, root, capture.Ledger(state, capture.ledger_key(state)),
            capture.BackupQueue(root, enabled=False))
    return _CAPTURERS[key]


def _verb_writer(via, client, source):
    """The `write` callable `mcpcore.call_verb` takes, for this endpoint's stamps. An
    endpoint that stamps a foreign source writes only through `brain_capture`."""
    if source:
        return None

    def write(text, declared, flags, request_id, allow_duplicate):
        return local_capturer().capture(
            text, "local", via or "stdio", "-", client_request_id=request_id,
            allow_duplicate=allow_duplicate, via=via, client=client, declared=declared,
            extra_flags=flags)
    return write


def call_tool(name, args, allow=None, source=None, cap=None, log=None, via=None,
              client=None):
    if isinstance(name, str) and name in verbs.UNSUPPORTED:
        served = [v for v in verbs.SERVED if allow is None or v in allow]
        return verbs.result(verbs.unavailable(name, served))
    # isinstance first: a non-string name (dict/list) is unhashable and would
    # raise on the membership test instead of reporting an unknown tool.
    if not isinstance(name, str) or name not in TOOLS_BY_NAME:
        return {"content": [{"type": "text", "text": f"unknown tool {name!r}"}], "isError": True}
    if allow is not None and name not in allow and name in verbs.SERVED:
        return verbs.result(verbs.scope_denied(name))
    if allow is not None and name not in allow:
        # The enforcement point, and it is deliberately HERE rather than in the
        # transport: a restriction that lives beside the dispatcher cannot be
        # skipped by a caller who never read tools/list, and a client is exactly
        # what this mode is defending against.
        #
        # It says which tool and what IS served, instead of claiming the tool
        # does not exist. That is no more than tools/list already returns, and
        # a model told the truth stops retrying, where one told "unknown tool"
        # reasonably tries three spellings of the name first. Phrased from the
        # allow-set rather than from a mode name so it stays true for whatever
        # the third restricted mode turns out to be.
        return {"content": [{"type": "text", "text":
                             f"{name} is not served by this brain. This endpoint serves "
                             f"{', '.join(sorted(allow))} and nothing else — that is a "
                             "property of how it was started, not a permission to retry "
                             "into."}],
                "isError": True}
    if name in verbs.SERVED:
        # A caller reaching this brain over `brain serve` is remote; stdio is the owner
        # at their own keyboard. Private facts are never returned to a remote caller.
        return mcpcore.call_verb(name, args, "local", remote=via not in (None, "stdio"),
                                 runner=lambda argv, timeout: run_cli(argv), timeout=120.0,
                                 write=_verb_writer(via, client, source))
    declared, ignored = {}, []
    if name == "brain_capture":
        # The caller's declared fields are DROPPED when bad, never refused — a
        # note is worth more than its label — so they are taken out before the
        # strict validator, which would refuse the whole call over a bad enum.
        args, declared, ignored = provenance.take_declared(args)
    args, problem = mcpcore.validate_args(TOOLS_BY_NAME[name], args if args is not None else {})
    if problem:
        return {"content": [{"type": "text", "text": f"invalid arguments for {name}: {problem}"}],
                "isError": True}
    if name == "brain_search":
        cli = ["search", "--limit", str(args.get("limit", 8))]
        if args.get("scope") in ("all", "handoff"):
            cli += ["--scope", args["scope"]]
        if args.get("scope") == "handoff" and args.get("offset") is not None:
            cli += ["--offset", str(args["offset"])]
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
        cli = ["capture"]
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
        # `via` and `client` are stamped the same way: `via` from how this
        # server was started, `client` from the client's own self-reported
        # clientInfo. Neither is in the schema, so neither can come from the
        # request arguments. The declared fields follow, already cleaned, and
        # the text goes LAST, so a note whose whole text is "--kind" never
        # finds a value after it to swallow.
        cli += provenance.cli_flags({"via": via, "client": client, **declared})
        cli.append(args["text"])
        if cap is not None:
            # Checked BEFORE the CLI runs: nothing reaches disk either way, and
            # a refusal that still forked a subprocess would be a cheap way to
            # keep the machine busy. What this endpoint has already written
            # today is a fact about the ENDPOINT — anything counted out of the
            # inbox's contents would not be, and would not be said out loud.
            refusal = cap()
            if refusal:
                return _error(refusal)
    try:
        run = run_cli(cli)
    except Exception as exc:
        if name == "brain_capture" and source:
            sys.stderr.write(f"  drop box could not run the capture: {exc!r}\n")
            return _error("this brain did not accept that capture")
        return {"content": [{"type": "text", "text": f"brain tool failed: {exc!r}. "
                             f"Fallback: search the files directly with rg in {ROOT} "
                             "(archive/ and vault/ are excluded by .rgignore)."}],
                "isError": True}
    if name == "brain_capture" and log is not None:
        # Whether a write actually landed, for an operator who is not at the
        # terminal. `brain capture` exits non-zero when the NOTE reached disk
        # but the COMMIT failed — an unconfigured git identity, a consolidation
        # branch in the way — and that is the failure this exists to surface:
        # it is invisible to the caller by design (see drop_box_reply), so
        # without this line it is invisible to everybody. `count` is how many
        # notes reached disk, which separates "written but not committed" from
        # "nothing happened at all".
        landed = sum(1 for line in (run.stdout or "").splitlines()
                     if line.strip().endswith(".md"))
        log("capture_committed" if run.returncode == 0 else "capture_uncommitted",
            count=landed)
    # A stamped source means an untrusted caller — see drop_box_reply.
    if name == "brain_capture" and source:
        return drop_box_reply(run)
    text = (run.stdout + ("\n" + run.stderr if run.stderr.strip() else "")).strip() or "(no output)"
    if ignored:
        text = provenance.ignored_line(ignored) + "\n" + text
    return {"content": [{"type": "text", "text": text}], "isError": run.returncode != 0}


# Where a 2026-07-28 client names itself on every request (see httpmcp.py).
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"


def _instructions(allow) -> str:
    """What `initialize` tells a client, from what this endpoint serves.

    Everything, or reads plus capture: the full policy. Reads only: the
    read-only sentence. No reads at all — a drop box — gets nothing: telling
    a caller that cannot read to search first is an instruction it cannot
    follow, and the drop box answers a write and says nothing else."""
    served = TOOLS_BY_NAME if allow is None else allow
    if not any(name in served for name in READ_ONLY_TOOLS):
        return ""
    return mcpcore.instructions(
        "capture" if "brain_capture" in served or "remember" in served else "read")


def _client_of(info, announce: bool = False):
    """`name/version` from a clientInfo object, or None. Self-reported: it
    says which program asked, and nothing may treat it as who."""
    if not isinstance(info, dict):
        return None
    label = provenance.client_label(info.get("name"), info.get("version"))
    if label is None and announce and info.get("name"):
        # The client's own spelling is not repeated: it is text somebody else
        # chose, and the operator needs to know only that captures will carry
        # no client.
        sys.stderr.write("brain-mcp: the client's self-reported name is not a valid "
                         "label; its captures will carry no `client`\n")
        sys.stderr.flush()
    return label


def handle(msg, allow=None, source=None, cap=None, log=None, via=None, session=None):
    """One JSON-RPC message in, one reply out — or None for a notification.

    Returns rather than writes, which is the whole reason a second transport is
    possible. The old version printed to stdout, so it could only ever serve
    stdio; HTTP needs the answer as a value, and needs to be able to tell a
    request (answer it) from a notification (202, no body).

    `allow` is the set of tool names this caller may reach, or None for all of
    them. It filters tools/list and gates tools/call — both, because either one
    alone is a mode a client can step around.

    `source` is what this endpoint stamps on anything it writes, or None to
    leave the CLI's own default. `cap` is a callable returning a refusal
    string when this endpoint has written enough for today, or "" when it may
    write; None means uncapped. Both travel the same way `allow` does — from
    how the server was started, past the message entirely — for the same
    reason: a property of the deployment must not be settable by the caller.

    `via` is the same kind of property: what this endpoint stamps as the
    transport a capture arrived through (`stdio`, `serve`, `drop-box`), or None
    to stamp nothing. `session` is a dict that lives as long as one client's
    connection — stdio is exactly one — so the clientInfo a client gave at
    `initialize` can be stamped on its later captures. Without one (sessionless
    HTTP), a modern client's per-request clientInfo is used when it sends one.
    """
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params")
    if not isinstance(params, dict):
        params = {}
    if method == "initialize":
        if session is not None:
            session["client"] = _client_of(params.get("clientInfo"), announce=True)
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "brain", "version": version.VERSION},
            # What this endpoint serves of the memory-verbs contract, and what the
            # brain never will (brainlib/verbs.py).
            "_meta": {"brain-verbs": verbs.advertisement(
                TOOLS_BY_NAME if allow is None else allow)},
        }
        text = _instructions(allow)
        if text:
            result["instructions"] = text
        return _reply(msg_id, result)
    if method == "tools/list":
        served = TOOLS if allow is None else [t for t in TOOLS if t["name"] in allow]
        return _reply(msg_id, {"tools": served})
    if method == "tools/call":
        args = params.get("arguments")
        client = (session or {}).get("client")
        if client is None:
            meta = params.get("_meta")
            client = _client_of(meta.get(META_CLIENT_INFO)) if isinstance(meta, dict) else None
        return _reply(msg_id, call_tool(params.get("name", ""),
                                        args if args is not None else {}, allow=allow,
                                        source=source, cap=cap, log=log, via=via,
                                        client=client))
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
    """The stdio transport: a line in, a line out.

    One process is one client's session, so what it said at `initialize` is
    remembered for the captures it makes, and every capture is stamped
    `via: stdio`."""
    session = {}
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
            out = handle(msg, via="stdio", session=session)
        except Exception as exc:
            msg_id = msg.get("id")
            if msg_id is None:
                continue
            out = _reply(msg_id, error={"code": -32603, "message": f"internal error: {exc!r}"})
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
