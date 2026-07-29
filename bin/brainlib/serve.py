# bin/brainlib/serve.py
"""`brain serve` — the brain over HTTP, for reaching it from another device.

The only socket this repo opens, and the tool layer behind it includes
brain_capture, which WRITES to a git repository that auto-pushes. So the
security contract is short, absolute, and each clause has a test:

1. It refuses to start without a token. A refusal, not a warning.
2. Every request carries `Authorization: Bearer <token>`, compared with
   hmac.compare_digest. A naive `==` returns on the first wrong byte and hands
   the token over one byte at a time to anything that can measure a response.
3. The default bind is 127.0.0.1. Anything else is an explicit flag that says
   out loud what it is exposing.
4. An `Origin` header is refused outright unless allowlisted. The MCP transport
   spec makes this a MUST for one reason: a page on evil.com can make a browser
   talk to 127.0.0.1. No legitimate client here is a browser, so the allowlist
   is empty by default. It is the second barrier, not the first — a
   cross-origin request cannot set Authorization without a preflight either —
   and two independent barriers is the point.
5. The token lives in the OS keystore. Never the repo (lint refuses credentials
   in tracked files and that rule is not being weakened for this), never a
   config file, never a URL.
6. Failed authentication backs off, per address, and there is no flag to turn
   it off. It is checked BEFORE the token is compared, so a blocked caller's
   guess is never looked at. See Limiter for what that costs and who pays it.

No TLS here, deliberately. The tunnel terminates TLS; this serves plaintext to
loopback, which is correct, or to whatever the operator explicitly asked for,
which is their call and is printed back to them.

`--read-only` serves the four read tools and refuses brain_capture, in the
dispatcher rather than here — a transport that filters the advertised list and
then runs whatever arrives has implemented a suggestion. It shrinks what can be
DONE to the brain, not what can be read out of it, and for a second brain the
reading is most of the exposure. Read-only is a property of the process, so
serving both at once means two of them on two ports.

NOT BUILT, and tracked in docs/superpowers/BACKLOG.md rather than left as
folklore — a decision somebody may need to revisit:

- **No OAuth.** Bearer only, which is what Claude Code and anything else that
  can set a header takes. claude.ai on the web, Desktop and mobile cannot use
  it: checked 2026-07-29, their per-user custom connector flow accepts OAuth
  client credentials, and the fixed-header path is beta and org-admin-only.
  Closing that gap means OAuth 2.1 with dynamic client registration.

Transport: Streamable HTTP per the 2025-06-18 MCP specification, minus the
optional parts. One endpoint, POST only, a single JSON object per request. GET
answers 405, which the spec explicitly permits for a server that offers no SSE
stream — saying so is compliant, pretending to stream is not. No session IDs:
they are a MAY and this server is stateless.
"""
import hmac
import json
import math
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import mcp
from . import osbackend

ENDPOINT = "/mcp"
TOKEN_NAME = "brain-serve-token"
DEFAULT_PORT = 8787

# The same ceiling the stdio loop uses: a single request larger than this is
# not real traffic.
MAX_BODY_BYTES = 10_000_000

# Versions this server has actually been checked against. The spec requires a
# 400 for anything else, and keeping the list a literal means widening it is a
# deliberate edit rather than something that happens by accident.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18",
                               "2025-11-25")


# Failed-authentication backoff. Five attempts are free — a stale token in a
# client, a paste that dropped a character, a colleague trying the wrong one —
# and every failure after that costs 1s, 2s, 4s … up to five minutes. Against
# 32 random bytes the arithmetic was never the problem; this is what turns
# "guessing it is not practical" from an argument into a control.
AUTH_FREE_ATTEMPTS = 5
AUTH_BACKOFF_BASE_SECONDS = 1.0
AUTH_BACKOFF_CAP_SECONDS = 300.0
# Forget an address that has been quiet for an hour, and never track more than
# this many at once. Both are about the limiter's own footprint, not the attack.
AUTH_IDLE_SECONDS = 3600.0
AUTH_MAX_KEYS = 4096


class Limiter:
    """Per-address backoff on failed authentication.

    Keyed on the TCP peer address, and `X-Forwarded-For` is deliberately NOT
    consulted: a header the client sets is a header an attacker rotates, so
    trusting it would swap a real control for one that is stepped around by
    editing a request. The cost of that choice is stated rather than hidden —
    behind a tunnel every client arrives as the tunnel, so a guessing run
    through it slows down everything else through it too. That is the direction
    this should fail in.

    Only failed authentication is counted. An Origin refusal is not, and that
    is not an oversight: those requests come from the operator's own browser,
    driven by whatever page asked for them, so counting them would let any web
    page lock the operator out of their own brain with a `fetch` in a loop.

    The backoff escalates on failed AUTHENTICATION, not on refused requests: a
    caller who keeps hammering while blocked is turned away without their count
    moving, so the wait they were told about is the wait they get. Escalating on
    blocked requests too would be a stronger control against one attacker and a
    permanent lockout for everyone sharing a tunnel with them — Retry-After
    should mean what it says.

    The clock is a parameter so the tests can assert on a five-minute backoff
    without waiting five minutes for it.
    """

    def __init__(self, free: int = AUTH_FREE_ATTEMPTS,
                 base: float = AUTH_BACKOFF_BASE_SECONDS,
                 cap: float = AUTH_BACKOFF_CAP_SECONDS,
                 idle: float = AUTH_IDLE_SECONDS,
                 max_keys: int = AUTH_MAX_KEYS,
                 clock=time.monotonic):
        self._free = free
        self._base = base
        self._cap = cap
        self._idle = idle
        self._max_keys = max_keys
        self._clock = clock
        # ThreadingHTTPServer means concurrent handlers share this table.
        self._lock = threading.Lock()
        self._state = {}          # address -> [failures, blocked_until, last_seen]

    def retry_after(self, key) -> int:
        """Whole seconds this address must wait, or 0. Whole, because that is
        what a Retry-After header carries."""
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return 0
            remaining = entry[1] - self._clock()
            return int(math.ceil(remaining)) if remaining > 0 else 0

    def failed(self, key) -> None:
        now = self._clock()
        with self._lock:
            self._prune(now)
            entry = self._state.get(key) or [0, 0.0, now]
            entry[0] += 1
            entry[2] = now
            over = entry[0] - self._free
            if over > 0:
                entry[1] = now + min(self._base * (2 ** (over - 1)), self._cap)
            self._state[key] = entry

    def succeeded(self, key) -> None:
        """Forget this address. A count that survived a correct token would
        have a client which reconnects all day blocked by this morning's typo."""
        with self._lock:
            self._state.pop(key, None)

    def tracked(self) -> int:
        with self._lock:
            return len(self._state)

    def _prune(self, now: float) -> None:
        """Bound the table. The caller holds the lock.

        A dict keyed by remote address that only ever grows is a memory
        exhaustion primitive reachable by anyone who can send one unauthorised
        request — a limiter that becomes the denial of service it was added to
        prevent is worse than no limiter at all.

        Evicting the least recently seen when full does mean somebody able to
        arrive from many addresses can push a real entry out. That costs them
        the backoff they had already earned and costs the operator nothing,
        which is the right way round.
        """
        for key in [k for k, entry in self._state.items()
                    if now - entry[2] > self._idle]:
            del self._state[key]
        if len(self._state) >= self._max_keys:
            ordered = sorted(self._state.items(), key=lambda kv: kv[1][2])
            for key, _entry in ordered[:len(self._state) - self._max_keys + 1]:
                del self._state[key]


def mint_token() -> str:
    """A new token. 32 bytes of os.urandom, URL-safe, ~43 characters."""
    return secrets.token_urlsafe(32)


def store_token(value: str, store=None) -> bool:
    return (store or osbackend.keystore()).set(TOKEN_NAME, value)


def read_token(store=None) -> str:
    return (store or osbackend.keystore()).get(TOKEN_NAME)


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost")


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A client going away is not a server error.

        Refusing an oversized request means answering and closing without
        reading the body, which the sending client sees as a reset — the
        correct outcome, and one the default handler prints a traceback for.
        Anything else still surfaces.
        """
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


class _Handler(BaseHTTPRequestHandler):
    # Set by make_server on the server object; read through self.server.
    protocol_version = "HTTP/1.1"     # keep-alive, so a client is not reconnecting per call
    # No Python version in the Server header. It is free reconnaissance for
    # anything scanning, and this is the one thing here that faces a network.
    server_version = "brain"
    sys_version = ""

    def log_message(self, fmt, *args):
        """Method, path and status only.

        BaseHTTPRequestHandler's default already logs no headers, but relying
        on that is one upstream change away from writing an Authorization
        header into a terminal log. Stated explicitly instead.
        """
        if self.server.quiet:
            return
        sys.stderr.write("  %s %s\n" % (self.address_string(), fmt % args))

    # ---------------------------------------------------------------- guards

    def _refuse(self, status: int, message: str, headers=None) -> None:
        """Answer and CLOSE. Every path through here is a refusal.

        The close is load-bearing, not tidiness. Refusals happen before the
        request body is read — deliberately, because reading a 10 MB body in
        order to reject it is the denial of service the size cap exists to
        prevent — so those bytes are still sitting in the socket. On a
        kept-alive HTTP/1.1 connection they are then parsed as the next request
        line, and the client's next, entirely valid request comes back 400 or
        501. One wrong token poisons the connection.

        Found 2026-07-29 by putting a real tunnel in front of this server. It
        was invisible to every test here because they open one connection per
        request; a proxy pools them, which is precisely the deployment `serve`
        exists for.

        Draining the body instead would also work, and is worse: it means
        reading whatever an unauthenticated caller chose to send. Closing costs
        an attacker a fresh handshake per guess, which stacks with the limiter.
        """
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # send_header already sets close_connection for this value; setting it
        # too says the intent out loud rather than relying on that side effect.
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        """Backoff, then Origin, then token. Everything else follows all three.

        The order is the design. A blocked address is refused before its guess
        is looked at, which is the entire slow-down — a guess that is never
        compared cannot be a guess that succeeds. The price is that a CORRECT
        token from a blocked address waits too, which is deliberate and is the
        same trade every SSH server on the internet makes.
        """
        peer = self.client_address[0]
        wait = self.server.limiter.retry_after(peer)
        if wait:
            self._refuse(429, "too many failed authentication attempts from "
                              f"this address; retry in {wait}s",
                         {"Retry-After": str(wait)})
            return False

        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allow_origin:
            # Not counted against the limiter, on purpose — see Limiter.
            self._refuse(403, "this server does not accept browser origins")
            return False

        supplied = self.headers.get("Authorization", "")
        scheme, _, value = supplied.partition(" ")
        # compare_digest on BOTH the scheme and the token: an early return on
        # the scheme is harmless (it is not a secret), but the token comparison
        # must not short-circuit.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                value.strip(), self.server.token):
            self.server.limiter.failed(peer)
            self._refuse(401, "a bearer token is required",
                         {"WWW-Authenticate": 'Bearer realm="brain"'})
            return False
        self.server.limiter.succeeded(peer)

        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            # The spec's MUST. Absent is fine — it says to assume 2025-03-26,
            # not to refuse.
            self._refuse(400, "unsupported MCP-Protocol-Version "
                              f"{version!r}; this server speaks "
                              f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}")
            return False
        return True

    # --------------------------------------------------------------- methods

    def do_POST(self):
        if not self._allowed():
            return
        if self.path.split("?")[0] != ENDPOINT:
            self._refuse(404, "not found")
            return

        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._refuse(400, "malformed Content-Length")
            return
        if declared > MAX_BODY_BYTES:
            # Judged on the header, before a byte is read. Reading first and
            # deciding after is how a size limit becomes the thing that
            # exhausts the memory it was added to protect.
            self._refuse(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
            return

        raw = self.rfile.read(declared) if declared else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self._refuse(400, "body is not valid JSON")
            return
        if not isinstance(msg, dict):
            self._refuse(400, "body must be a single JSON-RPC object")
            return

        try:
            out = mcp.handle(msg, allow=self.server.allow_tools)
        except Exception as exc:
            # Same containment as stdio: one malformed request must never take
            # down a server other sessions are sharing.
            msg_id = msg.get("id")
            if msg_id is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            out = {"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32603, "message": f"internal error: {exc!r}"}}

        if out is None:
            # A notification or a response: the spec says 202 with no body.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        body = json.dumps(out).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._allowed():
            return
        # No SSE stream here. The spec permits saying so with a 405, and a
        # server that pretends to stream and then does not is worse than one
        # that is honest about it.
        self._refuse(405, "this endpoint does not offer an SSE stream")

    def do_DELETE(self):
        if not self._allowed():
            return
        self._refuse(405, "this server does not use sessions")


def make_server(token: str, host: str = "127.0.0.1", port: int = 0,
                allow_origin=(), quiet: bool = False,
                allow_tools=None, limiter=None) -> ThreadingHTTPServer:
    """A configured, unstarted server. Refuses to exist without a token.

    Raising here rather than at the call site is deliberate: this is the
    function every caller — the CLI, the tests, anything later — has to go
    through, so the one rule that must never be bypassed lives at the choke
    point rather than in each caller's good intentions.

    `allow_tools` is None for every tool, or the names this server will serve.
    """
    if not (token or "").strip():
        raise ValueError("brain serve requires a token; mint one with "
                         "`brain serve --new-token`")
    server = _Server((host, port), _Handler)
    server.token = token
    server.allow_origin = tuple(allow_origin)
    server.quiet = quiet
    server.allow_tools = None if allow_tools is None else frozenset(allow_tools)
    # Always one, never optional: there is no flag to turn it off, because the
    # only argument for turning it off is that guessing was never going to work
    # anyway — which is an argument, and this is a control.
    server.limiter = limiter or Limiter()
    return server


USAGE = """brain serve — reach this brain from another device.

  brain serve [--read-only] [--bind <addr>] [--port <n>] [--allow-origin <origin>]
  brain serve --new-token

  --new-token           mint a value, store it in the OS keystore, print it ONCE
  --read-only           serve the four read tools; refuse brain_capture
  --bind <addr>         default 127.0.0.1. Anything else is exposed; it says so
  --port <n>            default {port}
  --allow-origin <o>    permit one browser Origin. Repeatable. Empty by default,
                        because no legitimate client of this server is a browser

Serves the same tools as the stdio server, over HTTP, behind a bearer token.
By default that includes brain_capture, which WRITES to your brain and
auto-pushes it — whoever holds the token can add notes.

--read-only takes that tool away, server-side: it is not listed, and it is
refused if a client calls it regardless. It does not make the brain safe to
expose — every note is still readable by whoever holds the token. Read-only is
a property of the process, so serving both at once means two of them, on two
ports, with the writable one on loopback.

No TLS here. Put a tunnel in front that terminates TLS, forwards to this port,
preserves the Authorization header, and adds no Origin header.
""".format(port=DEFAULT_PORT)


def startup_notes(host: str, port: int, read_only: bool = False) -> tuple:
    """(what to tell the operator, what to warn them about).

    Split from the serving so both halves can be read — and tested — without
    opening a socket.

    The mode is stated on every start, including the default one. Somebody who
    ran this read-only last week and forgets the flag this week is exposing a
    write tool, and this line is the only thing between them and not noticing.
    """
    url = f"http://{host}:{port}{ENDPOINT}"
    mode = ["  READ-ONLY — serving the four read tools. brain_capture is not",
            "  served, and is refused if a client calls it anyway."] if read_only else \
           ["  Serving all five tools, which includes brain_capture: whoever holds",
            "  the token can write notes into this brain, and they are committed",
            "  and pushed automatically. `--read-only` serves the other four."]
    notes = [
        f"brain serve — listening on {url}",
        "",
    ] + mode + [
        "",
        "  Register it with a client that can set a header:",
        f'    claude mcp add --transport http brain {url} \\',
        '      --header "Authorization: Bearer $BRAIN_TOKEN"',
        "",
        # The value itself is deliberately absent. Printing it on every start
        # writes it into scrollback, any terminal log and any screen recording
        # — repeatedly, long after the one moment anybody was watching for it.
        "  $BRAIN_TOKEN is the value `brain serve --new-token` printed. It is not",
        "  repeated here on purpose; mint a new one if it is lost.",
        "",
        "  Behind a tunnel, swap the host for your hostname. The tunnel must",
        "  terminate TLS, forward to this port, preserve the Authorization",
        "  header, and add no Origin header.",
    ]
    warnings = []
    if not _is_loopback(host) and read_only:
        # Still the whole brain, and that is the part worth saying out loud.
        # --read-only shrinks what can be DONE to it, not what can be read out
        # of it, and for a second brain the reading is most of the exposure.
        warnings = [
            "",
            f"  EXPOSED — bound to {host}, not loopback. Anything that can reach",
            f"  {host}:{port} and holds the token can read EVERY note in this brain.",
            "  Read-only limits what it can change, not what it can see.",
        ]
    elif not _is_loopback(host):
        warnings = [
            "",
            f"  EXPOSED — bound to {host}, not loopback. Anything that can reach",
            f"  {host}:{port} and holds the token can read every note in this brain",
            "  and WRITE new ones, which are committed and pushed automatically.",
            "  That is the whole tool surface, not a subset.",
        ]
    return notes, warnings


def run_serve(argv: list, store=None, run=None) -> int:
    """The `brain serve` command.

    `run` is injected so the wiring can be tested without a socket, the same
    way phase_backup takes its runner. The tests that genuinely bind one go
    through make_server directly.
    """
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0

    store = store or osbackend.keystore()
    run = run or (lambda server: server.serve_forever())

    if "--new-token" in argv:
        minted = mint_token()
        if not store_token(minted, store=store):
            print(f"could not store the value in {store.describe()}", file=sys.stderr)
            return 1
        print("This is the ONLY time this is shown. Copy it now:\n")
        print(f"  {minted}\n")
        print(f"Stored in {store.describe()}.", file=sys.stderr)
        print("Any client already wired to a previous value will stop working "
              "until you\nre-register it with this one.", file=sys.stderr)
        return 0

    existing = read_token(store=store)
    if not existing:
        # A refusal, not a warning, and emphatically not a silent mint: a
        # credential that appears without anybody seeing it is a credential
        # nobody knows to protect — and this one authorises a write tool.
        print("brain serve refuses to start without a token.\n\n"
              "  brain serve --new-token\n\n"
              "mints one, stores it in this machine's keystore, and prints it once.",
              file=sys.stderr)
        return 1

    host = _flag(argv, "--bind") or "127.0.0.1"
    try:
        port = int(_flag(argv, "--port") or DEFAULT_PORT)
    except ValueError:
        print("--port needs a number", file=sys.stderr)
        return 2
    allow_origin = _flags(argv, "--allow-origin")
    read_only = "--read-only" in argv

    try:
        server = make_server(existing, host, port, allow_origin=allow_origin,
                             allow_tools=mcp.READ_ONLY_TOOLS if read_only else None)
    except OSError as exc:
        print(f"could not listen on {host}:{port} — {exc}", file=sys.stderr)
        return 1

    notes, warnings = startup_notes(host, server.server_address[1], read_only=read_only)
    for line in notes:
        print(line, file=sys.stderr)
    for line in warnings:
        print(line, file=sys.stderr)
    print("", file=sys.stderr)
    try:
        run(server)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
    finally:
        server.server_close()
    return 0


def _flag(argv: list, name: str) -> str:
    if name not in argv:
        return ""
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) else ""


def _flags(argv: list, name: str) -> tuple:
    found = []
    for i, arg in enumerate(argv):
        if arg == name and i + 1 < len(argv):
            found.append(argv[i + 1])
    return tuple(found)
