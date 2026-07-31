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

`--drop-box` is the mirror, and the one this repo's business partition needs:
brain_capture and nothing else, for an agent that must be able to contribute
without being able to read. Its two extra properties travel with the process
too — `--source`, stamped on every note so consolidation can tell a customer's
claim from the owner's own, and a daily cap counted off the inbox. Its replies
carry an acknowledgement and an id, never the CLI's output: a response that
varies with the brain's contents is a way to read the brain one question at a
time.

`--oauth` adds the MCP authorization spec beside all of that, for the clients a
header cannot reach. Two populations, one URL, and they coexist rather than
replace each other:

- **Local clients** set `Authorization: Bearer <the operator token>` and always
  did. Nothing about that path changed, with the flag on or off.
- **Hosted assistants** run on somebody else's servers, never see a config
  file, and can only obtain a credential a person consented to in a browser.
  That is what the authorization server in oauth.py issues, and `_allowed`
  accepts on the same header.

Built to the SPECIFICATION, not to a vendor: this file contains no product
name outside a comment, and neither does oauth.py. See that module for what is
implemented and what is deliberately not — the short version is that dynamic
client registration is now deprecated in the MCP spec and is not built, and
`--new-client` is the fallback for anything that needs a client id.

Everything the server does is recorded through eventlog, which can hold no
query text and no note content by construction. `brain logs` reads it.

Transport: Streamable HTTP per the 2025-06-18 MCP specification, minus the
optional parts. One endpoint, POST only, a single JSON object per request. GET
answers 405, which the spec explicitly permits for a server that offers no SSE
stream — saying so is compliant, pretending to stream is not. No session IDs:
they are a MAY and this server is stateless.
"""
import hmac
import json
import math
import os
import re
import secrets
import sys
import threading
import time
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import eventlog
from . import mcp
from . import notes
from . import oauth as oauthlib
from . import osbackend

ENDPOINT = "/mcp"
# Re-exported so the startup banner can name it without importing the module
# into every f-string. One definition, in oauth.py, where the RFC lives.
WELL_KNOWN_AS_PATH = oauthlib.WELL_KNOWN_AS
TOKEN_NAME = "brain-serve-token"
DEFAULT_PORT = 8787

# What a `--source` slug may look like: the id rule, imported rather than
# copied. Two enforcement points — this one refuses a bad slug at startup, the
# CLI refuses one at write time — and refusing early is what puts the error in
# the operator's terminal instead of the bot's response.
SOURCE_RE = notes.ID_RE
# How many captures one source may leave in the inbox in a day. There is no
# flag to turn this off, for the same reason the limiter has none: "it probably
# will not send that many" is an argument, and this is a control.
DEFAULT_DAILY_CAP = 200

# The same ceiling the stdio loop uses: a single request larger than this is
# not real traffic.
MAX_BODY_BYTES = 10_000_000
# The authorization-server endpoints get a far smaller ceiling. An OAuth form
# is a few hundred bytes, and these are the endpoints that answer WITHOUT a
# token — the one place on this server where an anonymous caller can send a
# body at all.
MAX_FORM_BYTES = 16_384

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


class DailyCap:
    """How many captures one source may leave in the inbox in one day.

    Counted from the FILESYSTEM, never from memory. Inbox filenames already
    begin with YYYY-MM-DD and every capture now carries `source:`, so the
    number is derivable from what is already on disk: no state file, and no
    reset when the process dies. An in-memory counter is a bypass an unstable
    bot finds by accident — crash, reconnect, full budget again — and "the bot
    crashed a lot" is not a thing anybody investigates.

    Per source, not per server, so two drop boxes on one brain cannot spend
    each other's budget; a note with no `source:` is nobody's and counts
    against nothing.

    Two known limits, both deliberate. Consolidation drains the inbox, so a
    pass that lands mid-day hands back the budget it emptied — a cap that
    survived that would need state of its own, which is the thing this avoids,
    and consolidation is weekly and operator-driven. And the count is taken
    per request rather than held, so two captures arriving in the same
    millisecond can both see 199; the cap is a budget, not an airlock.

    The clock is a parameter for the same reason the limiter's is: no test may
    sleep, and none may wait for midnight.
    """

    def __init__(self, limit: int, source: str, inbox=None, today=None):
        self.limit = limit
        self.source = source
        self._inbox = Path(inbox) if inbox is not None else \
            mcp.ROOT / "knowledge" / "inbox"
        self._today = today or (lambda: date.today().isoformat())

    def count(self) -> int:
        """Today's captures from this source. Stops counting at the limit —
        the answer above it is never used, and a brain with 40,000 inbox files
        should not read all of them to refuse one request."""
        day = self._today()
        found = 0
        if not self._inbox.is_dir():
            return 0
        for path in sorted(self._inbox.glob(f"{day}-*.md")):
            if _source_of(path) == self.source:
                found += 1
                if found >= self.limit:
                    break
        return found

    def __call__(self) -> str:
        """"" when there is budget, else the refusal to hand the caller.

        Fixed text. How much this endpoint has written today is a fact about
        the endpoint and could be said out loud safely — but a number that
        moves is still a number the caller can watch, and there is no reason
        to hand one over.
        """
        if self.count() < self.limit:
            return ""
        sys.stderr.write(f"  drop box REFUSED a capture: {self.source} is at its "
                         f"daily cap of {self.limit}\n")
        sys.stderr.flush()
        return "this endpoint has taken all it will take today; nothing was written"


_SOURCE_LINE = re.compile(r"^source:\s*(.+?)\s*$")


def _source_of(path: Path) -> str:
    """The `source:` of one inbox note, read without a YAML parser.

    Only the frontmatter block is examined, and only the first 20 lines of it:
    a body line that happens to start with "source:" is not frontmatter, and
    this runs once per file per request.
    """
    try:
        with path.open(encoding="utf-8-sig", errors="replace") as handle:
            for i, line in enumerate(handle):
                if i > 20 or (i and line.strip() == "---"):
                    break
                found = _SOURCE_LINE.match(line)
                if found:
                    return found.group(1).strip("'\"")
    except OSError:
        # A note that vanished mid-count (consolidation drains this folder) is
        # simply not counted. Failing the request over it would mean a drop box
        # that stops working whenever the brain tidies itself.
        return ""
    return ""


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


# A request path is text the CLIENT wrote, so it is recorded as a CLASS and
# never as itself — `/mcp?q=<the owner's question>` is exactly how a query
# would otherwise reach the event log. Exact keys, never a prefix match:
# classification is not an authorization decision, but the two live close
# enough together that keeping one habit for both is cheaper than remembering
# which is which.
PATH_CLASSES = {
    ENDPOINT: "mcp",
    oauthlib.WELL_KNOWN_PRM: "discovery",
    oauthlib.WELL_KNOWN_AS: "discovery",
    "/authorize": "authorize",
    "/token": "token",
    "/revoke": "revoke",
}


def _first(form: dict, name: str) -> str:
    """One value from a parsed form. First, never last — see oauth._one."""
    values = form.get(name) or [""]
    return values[0] if isinstance(values, (list, tuple)) else str(values)


def _tool_name(msg) -> str:
    """The tool being called, IF it is one this brain actually has.

    A name arrives inside the message, so it is caller-supplied text until it
    has been matched against the table — and the event log refuses any string
    it has not already agreed to. This is the matching, and returning "" for
    anything unrecognised is what keeps `{"name": "../../etc/passwd"}` out of
    a file on disk.
    """
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return ""
    params = msg.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return name if name in mcp.TOOLS_BY_NAME else ""


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

    # ------------------------------------------------------------ event log

    def _log(self, event: str, **fields) -> None:
        """Record one event, or do nothing if this server has no log.

        Deliberately not gated on `quiet`: that flag is about a terminal
        nobody is reading, and this file exists precisely because nobody is
        reading that terminal.
        """
        log = getattr(self.server, "log", None)
        if log is not None:
            log.record(event, **fields)

    def send_response_only(self, code, message=None):
        """Remember the status so the request can be logged with it.

        The lowest of the three status-setting methods — send_response and
        send_error both come through here — so one override catches every
        answer this handler can give, including the ones BaseHTTPRequestHandler
        produces without asking (a 501 for an unimplemented verb, a 400 for a
        malformed request line).
        """
        self._status = code
        BaseHTTPRequestHandler.send_response_only(self, code, message)

    def _path_class(self) -> str:
        path = self.path.split("?")[0]
        kind = self._oauth_kind()
        if kind:
            return "discovery" if kind in ("prm", "as") else kind
        return PATH_CLASSES.get(path, "other")

    def _oauth_kind(self) -> str:
        """Which authorization-server endpoint this is, or "" for none.

        EXACT-MATCH lookup, and that is the security property rather than an
        implementation detail. These paths are answered WITHOUT a token, so the
        set of them is an authentication exemption — and `startswith` on a path
        is how an exemption becomes a bypass:
        `/.well-known/oauth-protected-resourceX` and
        `/.well-known/oauth-protected-resource/../../mcp` both pass a prefix
        test, and neither is discovery.

        Empty when `--oauth` is off, so nothing is exempt on a server that has
        no authorization server to hand a client on to.
        """
        cfg = getattr(self.server, "oauth", None)
        if cfg is None:
            return ""
        return cfg.public_paths().get(self.path.split("?")[0], "")

    def _served(self, started: float) -> None:
        """One `request` entry per answered request, whatever the outcome."""
        self._log("request",
                  method=self.command if self.command in eventlog.METHODS else "other",
                  path_class=self._path_class(),
                  status=getattr(self, "_status", 0),
                  ms=int((time.monotonic() - started) * 1000))

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

    def _blocked(self) -> bool:
        """The rate limiter, and it runs ahead of EVERYTHING.

        Including the discovery exemption: an unauthenticated path is still a
        path, and an address that has been guessing tokens does not get to
        spend the server's time on metadata instead.
        """
        wait = self.server.limiter.retry_after(self.client_address[0])
        if not wait:
            return False
        self._log("rate_limited", retry_after=wait)
        self._refuse(429, "too many failed authentication attempts from "
                          f"this address; retry in {wait}s",
                     {"Retry-After": str(wait)})
        return True

    def _origin_ok(self, extra=()) -> bool:
        """Refuse a browser Origin unless it is allowlisted.

        `extra` is how the authorization-server endpoints stay usable: the
        consent form is a browser POST from this server's OWN public origin, so
        that origin has to be acceptable there — and only there. Passing it in
        per call, rather than adding it to `allow_origin` at startup, keeps the
        widening scoped to the endpoints that need it instead of opening /mcp
        to a page served from the same host.
        """
        origin = self.headers.get("Origin")
        if origin and origin not in tuple(self.server.allow_origin) + tuple(extra):
            # Not counted against the limiter, on purpose — see Limiter. The
            # Origin VALUE is not logged either: it is a string the caller
            # chose, and a refused origin tells the operator nothing that its
            # own contents would improve on.
            self._log("origin_refused")
            self._refuse(403, "this server does not accept browser origins")
            return False
        return True

    def _allow_set(self):
        """Which tools THIS request may reach: the process, then the token.

        Two boundaries, and the order is the design. `--read-only` and
        `--drop-box` decide what the PROCESS will serve at all; a scope decides
        what one token may do inside that. So a token granted `brain:write`
        against a read-only process still reaches no write tool — the flag is
        the outer boundary and the scope can only narrow it.

        Returned as an allow-set rather than enforced here, so the refusal
        still happens in the dispatcher: a client that never read tools/list
        and calls a tool by name has to be refused too, and the client is the
        thing being defended against.
        """
        allow = self.server.allow_tools
        if self.grant is None:
            return allow
        return oauthlib.tools_for_scopes(
            str(self.grant.get("scope", "")).split(), allow)

    def _insufficient_scope(self, tool: str) -> str:
        """RFC 6750's 403 challenge, naming the scope that would have worked.

        A client that is told only "no" re-authorizes with the same scopes and
        fails identically; one told which scope it needs can step up in a
        single round trip.
        """
        needed = (oauthlib.SCOPE_WRITE if tool in mcp.WRITE_ONLY_TOOLS
                  else oauthlib.SCOPE_READ)
        cfg = getattr(self.server, "oauth", None)
        parts = ['Bearer error="insufficient_scope"', f'scope="{needed}"']
        if cfg is not None:
            parts.append(f'resource_metadata="{cfg.prm_url}"')
        return ", ".join(parts)

    def _challenge(self) -> str:
        """What a 401 tells a client to do about it.

        With no authorization server there is nothing to point at, so it stays
        exactly the string it has always been. With one, it carries
        `resource_metadata` — which is the entire difference between a client
        that starts an OAuth flow and one that reports only that it could not
        reach the server.
        """
        cfg = getattr(self.server, "oauth", None)
        return cfg.challenge() if cfg is not None else 'Bearer realm="brain"'

    def _json(self, status: int, payload: dict, headers=None) -> None:
        """A JSON answer that does NOT close the connection.

        Distinct from `_refuse` on purpose. `_refuse` closes because it answers
        before reading the request body, leaving those bytes in the socket to
        be misparsed as the next request line (backlog item 9, found with a
        real tunnel). Nothing here has an unread body: these are GETs, or POSTs
        this handler read in full. Closing anyway would make a client
        re-handshake for every step of a flow that has four of them.
        """
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
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
        if self._blocked() or not self._origin_ok():
            return False

        peer = self.client_address[0]
        supplied = self.headers.get("Authorization", "")
        scheme, _, value = supplied.partition(" ")
        # Per-REQUEST, never on the server object: ThreadingHTTPServer runs
        # handlers concurrently, and a grant parked where two requests can see
        # it is one request answering with another's permissions.
        self.grant = None

        # The operator token FIRST, and unchanged. compare_digest on the token:
        # a naive `==` returns on the first wrong byte and hands the value over
        # one byte at a time to anything that can measure a response.
        operator = (scheme.lower() == "bearer"
                    and hmac.compare_digest(value.strip(), self.server.token))
        if not operator:
            # Only then the issued-token store, and only if this deployment has
            # one. With --oauth off this branch does not exist, so the header
            # path costs exactly what it always did.
            auth = getattr(self.server, "auth", None)
            if auth is not None and scheme.lower() == "bearer" and value.strip():
                self.grant = auth.validate_bearer(value.strip())
                if self.grant is not None:
                    self._log("oauth_token_accepted")
                else:
                    # A reason CLASS. The presented value is a string the
                    # caller chose, and it is quite often the real token for a
                    # different server.
                    self._log("oauth_token_rejected", reason="unknown_token")

        if not operator and self.grant is None:
            self.server.limiter.failed(peer)
            # Which of the three, so a stale client is distinguishable from a
            # guessing run — but never the guess itself.
            self._log("auth_failed",
                      reason=("no_header" if not supplied.strip()
                              else "bad_scheme" if scheme.lower() != "bearer"
                              else "bad_token"))
            self._refuse(401, "a bearer token is required",
                         {"WWW-Authenticate": self._challenge()})
            return False
        self.server.limiter.succeeded(peer)

        version = self.headers.get("MCP-Protocol-Version")
        if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
            # The spec's MUST. Absent is fine — it says to assume 2025-03-26,
            # not to refuse.
            self._log("protocol_refused", reason="unsupported_version")
            self._refuse(400, "unsupported MCP-Protocol-Version "
                              f"{version!r}; this server speaks "
                              f"{', '.join(SUPPORTED_PROTOCOL_VERSIONS)}")
            return False
        return True

    # --------------------------------------------------------------- methods

    def do_POST(self):
        started = time.monotonic()
        self._status = 0
        try:
            self._post()
        finally:
            # In a finally, so a request that raised out of the handler is
            # still recorded. A crash that leaves no trace is the exact
            # failure this log exists to end.
            self._served(started)

    def _post(self):
        kind = self._oauth_kind()
        if kind:
            self._oauth_post(kind)
            return
        if not self._allowed():
            return
        if self.path.split("?")[0] != ENDPOINT:
            # No event of its own: the `request` entry already carries the
            # status and the path CLASS, which is everything that can safely
            # be said about a path somebody else wrote.
            self._refuse(404, "not found")
            return

        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._log("body_refused", reason="bad_content_length")
            self._refuse(400, "malformed Content-Length")
            return
        if declared > MAX_BODY_BYTES:
            # Judged on the header, before a byte is read. Reading first and
            # deciding after is how a size limit becomes the thing that
            # exhausts the memory it was added to protect.
            self._log("body_refused", reason="too_large")
            self._refuse(413, f"request body exceeds {MAX_BODY_BYTES} bytes")
            return

        raw = self.rfile.read(declared) if declared else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            self._log("body_refused", reason="not_json")
            self._refuse(400, "body is not valid JSON")
            return
        if not isinstance(msg, dict):
            self._log("body_refused", reason="not_object")
            self._refuse(400, "body must be a single JSON-RPC object")
            return

        tool = _tool_name(msg)
        allow = self._allow_set()
        if tool and allow is not None and tool not in allow and self.grant is not None:
            # The spec's answer for a token that is valid but not sufficient:
            # 403 with the scope needed, so a client can step up rather than
            # guess. It is checked here as well as in the dispatcher — this one
            # gets the STATUS right, and the dispatcher below is what still
            # refuses a caller who never read tools/list.
            self._log("insufficient_scope", tool=tool)
            self._refuse(403, f"{tool} needs a scope this token was not granted",
                         {"WWW-Authenticate": self._insufficient_scope(tool)})
            return

        began = time.monotonic()
        try:
            out = mcp.handle(msg, allow=allow,
                             source=self.server.source, cap=self.server.cap,
                             log=self._log)
        except Exception as exc:
            # Same containment as stdio: one malformed request must never take
            # down a server other sessions are sharing. The exception's text is
            # NOT logged: it is built from whatever the tool layer was holding,
            # which on this server is note content.
            if tool:
                self._log("tool_error", tool=tool)
            msg_id = msg.get("id")
            if msg_id is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            out = {"jsonrpc": "2.0", "id": msg_id,
                   "error": {"code": -32603, "message": f"internal error: {exc!r}"}}
        else:
            self._log_tool(msg, tool, out, began)

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

    def _log_tool(self, msg, tool: str, out, began: float) -> None:
        """One `tool_call` per tools/call, and nothing for any other method.

        The OUTCOME comes from the reply's own `isError`, not from the HTTP
        status: a tool that refused still answers 200, and "the request
        succeeded" is not the question an operator reading this log is asking.
        """
        if not isinstance(msg, dict) or msg.get("method") != "tools/call":
            return
        ms = int((time.monotonic() - began) * 1000)
        if not tool:
            # Named a tool this brain does not have. The name is not recorded:
            # it is text the caller wrote, and `_tool_name` returning "" is the
            # boundary that keeps it off the disk.
            self._log("tool_call", reason="not_found", ms=ms)
            return
        result = out.get("result") if isinstance(out, dict) else None
        failed = bool(result.get("isError")) if isinstance(result, dict) else True
        self._log("tool_call", tool=tool, outcome="error" if failed else "ok", ms=ms)

    def do_GET(self):
        started = time.monotonic()
        self._status = 0
        try:
            kind = self._oauth_kind()
            if kind:
                self._oauth_get(kind)
                return
            if not self._allowed():
                return
            # No SSE stream here. The spec permits saying so with a 405, and a
            # server that pretends to stream and then does not is worse than
            # one that is honest about it.
            self._refuse(405, "this endpoint does not offer an SSE stream")
        finally:
            self._served(started)

    # -------------------------------------------- the authorization server

    def _oauth_get(self, kind: str) -> None:
        """The unauthenticated half of the handshake.

        A client with no token MUST be able to read both metadata documents and
        reach the consent screen — that is the entire point of a 401 that
        carries `resource_metadata`. The rate limiter still runs first.
        """
        cfg = self.server.oauth
        if self._blocked() or not self._origin_ok(extra=(cfg.issuer,)):
            return
        if kind == "prm":
            self._log("oauth_metadata_served")
            self._json(200, cfg.protected_resource_metadata())
            return
        if kind == "as":
            self._log("oauth_metadata_served")
            self._json(200, cfg.authorization_server_metadata())
            return
        if kind == "authorize":
            from urllib.parse import parse_qs
            query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            outcome = self.server.auth.authorize(query)
            self._log_outcome(outcome)
            self._deliver(outcome)
            return
        # /token and /revoke are POST-only. Saying so beats a 404, which reads
        # to whoever is debugging as "this server has no such endpoint" when
        # the metadata it just served says it does.
        self._json(405, {"error": "invalid_request",
                         "error_description": "this endpoint accepts POST"})

    def _oauth_post(self, kind: str) -> None:
        cfg = self.server.oauth
        if self._blocked() or not self._origin_ok(extra=(cfg.issuer,)):
            return
        form = self._read_form()
        if form is None:
            return
        if kind == "authorize":
            outcome = self.server.auth.consent(
                form, _first(form, "operator_token"),
                # The comparison is constant time and lives HERE rather than in
                # the authorization server, because the operator token is the
                # transport's credential — oauth.py never sees it, and cannot
                # accidentally log or return it.
                verify=lambda supplied: hmac.compare_digest(
                    supplied, self.server.token))
            if outcome.credential_failed:
                # The consent screen is one of only two places on this server
                # where a credential can be guessed over the network. Both back
                # off on the same table.
                self.server.limiter.failed(self.client_address[0])
                self._log("oauth_consent_failed", reason="consent_refused")
            elif outcome.kind == "redirect":
                self._log("oauth_code_issued")
            self._log_outcome(outcome)
            self._deliver(outcome)
            return

        if kind == "token":
            status, payload, headers = self.server.auth.token(form)
            if status == 200:
                self._log("oauth_token_issued"
                          if _first(form, "grant_type") == "authorization_code"
                          else "oauth_token_refreshed")
            else:
                # The CODE, not the description: the description names the
                # specific grant that failed, and a grant is a credential.
                self._log("oauth_error", code=payload.get("error", "server_error"))
            self._json(status, payload, headers)
            return

        if kind == "revoke":
            status, payload, headers = self.server.auth.revoke(form)
            self._log("oauth_grant_revoked")
            self._json(status, payload, headers)
            return

        self._log("oauth_error", code="server_error")
        self._json(503, {"error": "server_error",
                         "error_description": "this endpoint is not built yet"})

    def _read_form(self):
        """A form-urlencoded body, parsed, or None if it was refused.

        `application/x-www-form-urlencoded` is what RFC 6749 requires and what
        every OAuth client sends. A server that accepts JSON only here answers
        415 and breaks the flow for everyone — a documented, common failure.
        """
        from urllib.parse import parse_qs
        try:
            declared = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._log("body_refused", reason="bad_content_length")
            self._refuse(400, "malformed Content-Length")
            return None
        if declared > MAX_FORM_BYTES:
            # A far smaller ceiling than /mcp's: an OAuth form is a few hundred
            # bytes, and these endpoints are the unauthenticated ones.
            self._log("body_refused", reason="too_large")
            self._refuse(413, f"request body exceeds {MAX_FORM_BYTES} bytes")
            return None
        raw = self.rfile.read(declared) if declared else b""
        try:
            return parse_qs(raw.decode("utf-8"))
        except Exception:
            self._log("body_refused", reason="bad_form")
            self._refuse(400, "body is not application/x-www-form-urlencoded")
            return None

    def _log_outcome(self, outcome) -> None:
        """Record WHY the authorization server refused something.

        Found by running the server rather than by reading it: an SSRF refusal
        produced `request status=400` and nothing else, which tells an operator
        that something was refused and nothing at all about what. Both values
        are vocabulary constants chosen by oauth.py, never text from the
        request.
        """
        if outcome.event:
            self._log(outcome.event, reason=outcome.reason or None)

    def _deliver(self, outcome) -> None:
        """Carry out what the authorization server decided.

        The transport has no opinions about OAuth: every rule lives in
        oauth.py, where it can be tested without a socket, and this turns the
        answer into bytes.
        """
        if outcome.kind == "redirect":
            self.send_response(302)
            self.send_header("Location", outcome.url)
            # A redirect carrying an authorization code must never be stored.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            for key, value in outcome.headers.items():
                self.send_header(key, value)
            self.end_headers()
            return
        body = outcome.body.encode("utf-8")
        self.send_response(outcome.status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # The consent page carries a credential field and nothing else. It
        # loads nothing, so nothing may be loaded into it either.
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in outcome.headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self):
        started = time.monotonic()
        self._status = 0
        try:
            if not self._allowed():
                return
            self._refuse(405, "this server does not use sessions")
        finally:
            self._served(started)


def make_server(token: str, host: str = "127.0.0.1", port: int = 0,
                allow_origin=(), quiet: bool = False,
                allow_tools=None, limiter=None,
                source=None, cap=None, log=None) -> ThreadingHTTPServer:
    """A configured, unstarted server. Refuses to exist without a token.

    Raising here rather than at the call site is deliberate: this is the
    function every caller — the CLI, the tests, anything later — has to go
    through, so the one rule that must never be bypassed lives at the choke
    point rather than in each caller's good intentions.

    `allow_tools` is None for every tool, or the names this server will serve.
    `source` and `cap` are the drop box's two other properties — what a write
    is stamped with, and how much may be written today. All three are read off
    the server object per request and never off the message.

    `log` is an eventlog.EventLog or None. None is not a degraded mode: it is
    what every test in this file that is not ABOUT the log runs with, and it is
    what a caller who never wanted one gets.
    """
    if not (token or "").strip():
        raise ValueError("brain serve requires a token; mint one with "
                         "`brain serve --new-token`")
    server = _Server((host, port), _Handler)
    server.token = token
    server.allow_origin = tuple(allow_origin)
    server.quiet = quiet
    server.allow_tools = None if allow_tools is None else frozenset(allow_tools)
    server.source = source
    server.cap = cap
    server.log = log
    # Always one, never optional: there is no flag to turn it off, because the
    # only argument for turning it off is that guessing was never going to work
    # anyway — which is an argument, and this is a control.
    server.limiter = limiter or Limiter()
    return server


USAGE = """brain serve — reach this brain from another device.

  brain serve [--read-only] [--bind <addr>] [--port <n>] [--allow-origin <origin>]
  brain serve --oauth --public-url <url> [--read-only] [--bind <addr>] ...
  brain serve --drop-box --source <slug> [--daily-cap <n>] [--bind <addr>] ...
  brain serve <any of the above> --install-service
  brain serve --service-status | --uninstall-service
  brain serve --new-token

  --new-token           mint a value, store it in the OS keystore, print it ONCE
  --oauth               also act as an OAuth 2.1 authorization server, so a
                        HOSTED assistant — one that cannot be given a header —
                        can connect after a person consents in a browser.
                        Requires --public-url. The header path is unaffected
  --public-url <url>    the address a client will actually type, exactly.
                        Behind a tunnel this process only sees 127.0.0.1, and
                        every token it issues is bound to the PUBLIC url, so it
                        has to be told. https:// (http:// on loopback only)
  --read-only           serve the four read tools; refuse brain_capture
  --drop-box            serve brain_capture and NOTHING else; refuse all four
                        read tools. Requires --source
  --source <slug>       what to stamp on every note this endpoint accepts.
                        Set by you, never by the caller; consolidation treats
                        anything other than `local` as an untrusted proposal
  --daily-cap <n>       captures per source per day, default {cap}. Counted off
                        the inbox, so a restart is not a reset. No way to
                        switch it off
  --bind <addr>         default 127.0.0.1. Anything else is exposed; it says so
  --port <n>            default {port}
  --allow-origin <o>    permit one browser Origin. Repeatable. Empty by default,
                        because no legitimate client of this server is a browser
  --install-service     hand this exact command to the OS (systemd --user or
                        launchd) so it restarts on failure and survives a
                        reboot. Add it to a serve command you have already got
                        right; every flag is validated before anything is
                        installed. On Linux it also checks whether user
                        services survive logout, which by default they DO NOT
  --service-status      is it installed, running, and serving THIS brain
  --uninstall-service   stop it and remove the unit

Serves the same tools as the stdio server, over HTTP, behind a bearer token.
By default that includes brain_capture, which WRITES to your brain and
auto-pushes it — whoever holds the token can add notes.

--read-only takes that tool away, server-side: it is not listed, and it is
refused if a client calls it regardless. It does not make the brain safe to
expose — every note is still readable by whoever holds the token. Read-only is
a property of the process, so serving both at once means two of them, on two
ports, with the writable one on loopback.

--drop-box is the mirror, for an agent you do NOT trust with your notes: it can
file a claim and learn nothing. It answers with an acknowledgement and an id —
never a duplicate hint, never an error from inside this brain, because a
response that varies with your notes is a way to read them one question at a
time. The two flags are refused together: that is two deployments.

No TLS here. Put a tunnel in front that terminates TLS, forwards to this port,
preserves the Authorization header, and adds no Origin header.
""".format(port=DEFAULT_PORT, cap=DEFAULT_DAILY_CAP)


def startup_notes(host: str, port: int, read_only: bool = False,
                  drop_box: bool = False, source: str = "",
                  daily_cap: int = 0, log_path=None, oauth=None) -> tuple:
    """(what to tell the operator, what to warn them about).

    Split from the serving so both halves can be read — and tested — without
    opening a socket.

    The mode is stated on every start, including the default one. Somebody who
    ran this read-only last week and forgets the flag this week is exposing a
    write tool, and this line is the only thing between them and not noticing.
    """
    url = f"http://{host}:{port}{ENDPOINT}"
    if drop_box:
        mode = [f"  DROP BOX — serving brain_capture and nothing else, stamped",
                f"  `source: {source}`, capped at {daily_cap} captures a day.",
                "  Nothing here can read this brain: the four read tools are not",
                "  served and are refused if a client calls them anyway.",
                "",
                "  What arrives is a PROPOSAL, not knowledge. It lands in inbox/,",
                "  outside default search, and only consolidation can promote it."]
    elif read_only:
        mode = ["  READ-ONLY — serving the four read tools. brain_capture is not",
                "  served, and is refused if a client calls it anyway."]
    else:
        mode = ["  Serving all five tools, which includes brain_capture: whoever holds",
                "  the token can write notes into this brain, and they are committed",
                "  and pushed automatically. `--read-only` serves the other four."]
    notes = [
        f"brain serve — listening on {url}",
        "",
    ] + mode + [
        "",
    ] + ([
        f"  OAUTH ON — this brain is also an authorization server for",
        f"    {oauth.resource}",
        "  which is the address a client must use, character for character.",
        "  A hosted assistant that has never seen a config file can connect by",
        "  pasting that URL and consenting in a browser. Discovery:",
        f"    {oauth.prm_url}",
        f"    {oauth.issuer}{WELL_KNOWN_AS_PATH}",
        "  Your tunnel must forward those two paths and /authorize and /token,",
        "  not only the MCP path. A route mapped to the MCP path alone produces",
        "  a client that can never discover anything and cannot say why.",
        "",
    ] if oauth is not None else []) + [
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
    ] + ([
        "",
        # Said at START, not only when something breaks. An operator who does
        # not know this file exists is one who infers a failure from an
        # agent's bad answer, which is the thing it was built to replace.
        "  Failures are recorded. Read them with `brain logs --errors`:",
        f"    {log_path}",
        "  It holds no query text and no note content — by construction, not",
        "  by filtering. See bin/brainlib/eventlog.py.",
    ] if log_path else [])
    warnings = []
    if not _is_loopback(host) and drop_box:
        # A drop box on a public interface is the NORMAL deployment — the bot
        # is on another host, and that separation is the whole design. So this
        # says what is actually at stake, which is not disclosure: it is what
        # gets written, by something the operator does not control.
        warnings = [
            "",
            f"  EXPOSED — bound to {host}, not loopback. That is expected for a",
            "  drop box: whatever holds the token can file up to "
            f"{daily_cap} claims a day",
            f"  into this brain's inbox, stamped `{source}`. It cannot read a note.",
            "  Consolidation decides what any of it is worth; nothing here does.",
        ]
    elif not _is_loopback(host) and read_only:
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


def run_serve(argv: list, store=None, run=None, oauth_store=None) -> int:
    """The `brain serve` command.

    `run` is injected so the wiring can be tested without a socket, the same
    way phase_backup takes its runner. The tests that genuinely bind one go
    through make_server directly. `oauth_store` is injected for the same
    reason: no test may write to the machine's real one.
    """
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0

    store = store or osbackend.keystore()
    run = run or (lambda server: server.serve_forever())

    if "--new-client" in argv:
        return _new_client(argv, oauth_store)
    if "--uninstall-service" in argv:
        print(osbackend.service().uninstall())
        return 0
    if "--service-status" in argv:
        return _service_status()

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
    drop_box = "--drop-box" in argv
    source = _flag(argv, "--source")
    use_oauth = "--oauth" in argv
    public_url = _flag(argv, "--public-url")

    if drop_box and use_oauth:
        # Refused by name, like --drop-box --read-only before it. A drop box is
        # an endpoint an untrusted bot holds a fixed token for; an OAuth flow
        # ends at a consent screen that assumes a human with a browser, and
        # there is nobody at the other end of a drop box to be that human.
        print("--drop-box and --oauth are two deployments, not two flags.\n\n"
              "  A drop box is for an agent you do NOT trust, holding a token you\n"
              "  issued it. OAuth exists for a hosted assistant a PERSON consents\n"
              "  to in a browser. Nothing is sitting at a drop box to give consent.",
              file=sys.stderr)
        return 2
    if public_url and not use_oauth:
        # Half a pairing is a surprise waiting to happen — the same rule
        # --source and --drop-box already follow.
        print("--public-url only means something with --oauth.\n\n"
              "  It is the address a client will type, and the audience every\n"
              "  issued token is bound to. Without --oauth nothing issues tokens\n"
              "  and nothing reads it.", file=sys.stderr)
        return 2
    if use_oauth:
        try:
            public_url = oauthlib.parse_public_url(public_url)
        except oauthlib.ConfigError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if drop_box and read_only:
        # The operator will reach for the combination, so it is refused by
        # name rather than resolved by precedence. It is not a mode: a brain
        # that both answers questions and accepts claims from the same socket
        # has no boundary in it. Two deployments, two processes, two ports.
        print("--drop-box and --read-only are two deployments, not two flags.\n\n"
              "  One serves the four read tools; the other serves brain_capture and\n"
              "  refuses to read. Run two processes on two ports, with two tokens —\n"
              "  and if they face different things, on two hosts.", file=sys.stderr)
        return 2
    if drop_box and not source:
        # An unattributed drop box is worse than none: it produces exactly the
        # inbox notes consolidation cannot weigh, while looking like it works.
        print("--drop-box requires --source <slug>.\n\n"
              "  It names what is on the other end of this socket, and it is stamped\n"
              "  on every note that arrives. Consolidation reads it to tell a claim\n"
              "  a customer fed to a bot from something you wrote yourself; without\n"
              "  it, it cannot, and it has to trust both or neither.", file=sys.stderr)
        return 2
    if source and not drop_box:
        print("--source only means something with --drop-box.\n\n"
              "  It marks an endpoint the operator is NOT sitting at, which is also\n"
              "  what makes that endpoint answer tersely. Half the pairing is a\n"
              "  surprise waiting to happen.", file=sys.stderr)
        return 2
    if source and not SOURCE_RE.match(source):
        print(f"--source {source!r} is not a valid slug — lowercase letters, digits\n"
              "and hyphens, 3-80 chars (the same rule as a note id).", file=sys.stderr)
        return 2
    try:
        daily_cap = int(_flag(argv, "--daily-cap") or DEFAULT_DAILY_CAP)
    except ValueError:
        print("--daily-cap needs a number", file=sys.stderr)
        return 2
    if daily_cap < 1:
        print("--daily-cap must be at least 1. There is no way to switch the cap\n"
              "off, the same way there is no way to switch the auth backoff off.",
              file=sys.stderr)
        return 2

    allow_tools = None
    if drop_box:
        allow_tools = mcp.WRITE_ONLY_TOOLS
    elif read_only:
        allow_tools = mcp.READ_ONLY_TOOLS

    if "--install-service" in argv:
        # Deliberately HERE, after every flag has been validated and before a
        # socket is opened. Installing a unit whose flags this process never
        # checked is how an operator ends up with a service that restarts
        # forever, fails identically each time, and reports nothing — which is
        # exactly the failure a service is supposed to prevent.
        return install_service(argv, host, port)

    # Machine-local, per brain, outside the repository. Built here rather than
    # in make_server so the tests — which must never write to the operator's
    # real log — get one only when they ask for one.
    log = eventlog.EventLog(osbackend.state_dir(mcp.ROOT) / eventlog.FILENAME)

    try:
        server = make_server(existing, host, port, allow_origin=allow_origin,
                             allow_tools=allow_tools,
                             source=source or None,
                             cap=DailyCap(daily_cap, source) if drop_box else None,
                             log=log)
    except OSError as exc:
        print(f"could not listen on {host}:{port} — {exc}", file=sys.stderr)
        return 1

    if use_oauth:
        # Set after make_server rather than through it: the authorization
        # server is a property of this DEPLOYMENT, and every test that is not
        # about OAuth builds a server without one.
        server.oauth = oauthlib.Config(public_url,
                                       scopes=oauthlib.scopes_for(allow_tools))
        server.auth = oauthlib.AuthServer(server.oauth,
                                          oauth_store or oauth_store_for())

    log.record("server_started",
               mode="drop_box" if drop_box else "read_only" if read_only else "default",
               oauth=use_oauth)

    notes, warnings = startup_notes(host, server.server_address[1], read_only=read_only,
                                    drop_box=drop_box, source=source,
                                    daily_cap=daily_cap, log_path=log.path,
                                    oauth=getattr(server, "oauth", None))
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


SERVICE_FLAGS = ("--install-service", "--uninstall-service", "--service-status")


def service_argv(argv: list, root=None) -> list:
    """The command a supervisor should run, built from the one just validated.

    The service flags are stripped and everything else is carried through
    verbatim, so what gets installed is the command the operator typed and this
    process already checked — rather than a second command line assembled from
    remembered flags, which is how the unit and the documentation drift apart.
    """
    root = root or mcp.ROOT
    rest = [arg for arg in argv if arg not in SERVICE_FLAGS]
    return [sys.executable, str(Path(root) / "bin" / "brain"), "serve"] + rest


def install_service(argv: list, host: str, port: int) -> int:
    """Hand `brain serve` to the OS so it survives a logout and a reboot.

    Everything before this point has already validated the flags and confirmed
    a token exists, so what is installed is known to at least start.

    What this does NOT do is fix lingering, because it cannot: `loginctl
    enable-linger` needs root. It reports it instead, loudly, because a Linux
    user unit installed without it is stopped the moment the operator logs
    out — and the failure is completely silent.
    """
    backend = osbackend.service()
    if not backend.available():
        print(backend.install([], None, None), file=sys.stderr)
        return 1

    job_env = {"PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:"
                       "/usr/local/bin:/usr/bin:/bin"}
    outcome = backend.install(service_argv(argv), cwd=str(mcp.ROOT), env=job_env)
    print(f"brain serve: {outcome}")
    if not outcome.startswith("installed"):
        return 1

    print(f"\n  It serves {mcp.ROOT}, restarts if it dies, and comes back after "
          "a reboot.")
    print(f"  Listening on {host}:{port}. Check it with:\n"
          "    brain serve --service-status")

    linger = osbackend.linger_state()
    if linger == "off":
        # The whole reason this command exists rather than a documentation
        # paragraph. Stated as a REQUIREMENT, not a suggestion: without it the
        # unit that was just installed and started stops at logout, and nothing
        # anywhere reports that it has.
        print("\n  [RED] LINGERING IS OFF, so this service — and every schedule "
              "you install —\n        stops the moment you log out, silently. "
              "Fix it now:\n\n"
              f"    sudo loginctl enable-linger {os.environ.get('USER', '$USER')}\n\n"
              "        Then re-check with `brain serve --service-status`.")
        return 1
    if linger == "unknown":
        print("\n  [-- ] Could not tell whether user services survive logout on "
              "this machine.\n        If it has systemd, run: "
              f"sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    return 0


def _service_status() -> int:
    backend = osbackend.service()
    state = backend.status()
    print(f"brain serve service ({backend.kind}): {state}")
    if state != "not installed" and not backend.serves(str(mcp.ROOT)):
        # The same warning `brain schedule status` gives, for the same reason:
        # these names are machine-global, so a second brain on one host claims
        # the service from the first and nothing says so.
        print(f"  [RED] but it serves a DIFFERENT brain, not {mcp.ROOT} — "
              "re-run --install-service from here to claim it")
        return 1
    linger = osbackend.linger_state()
    if linger == "off":
        print("  [RED] lingering is off: this stops when you log out, and so do "
              "your schedules\n        sudo loginctl enable-linger "
              f"{os.environ.get('USER', '$USER')}")
        return 1
    if linger == "on":
        print("  [ok ] lingering is on — it survives logout")
    return 0 if state == "running" else 1


def oauth_store_for(root=None):
    """The issued-credential database for THIS brain.

    Beside the event log, under `osbackend.state_dir`: machine-local, per
    brain, and outside the repository so it cannot reach git. Per brain
    matters here more than anywhere — two endpoints on one host sharing one
    token database would mean a token consented to for one brain opening the
    other, which is the shared-keystore trap the business partition already
    found once.
    """
    return oauthlib.Store(osbackend.state_dir(root or mcp.ROOT) / "oauth.db")


def _new_client(argv: list, store=None) -> int:
    """`brain serve --new-client "<name>" --redirect-uri <uri>`.

    The third registration mechanism the MCP specification names, and the
    answer for any client that speaks neither Client ID Metadata Documents nor
    something this server has. It is GENERIC — one code path, no vendor in it —
    which is the point: "works with any provider" cannot rest on every provider
    having implemented the same optional thing.
    """
    name = _flag(argv, "--new-client")
    uris = _flags(argv, "--redirect-uri")
    if not name.strip() or name.startswith("--"):
        print('--new-client needs a name: brain serve --new-client "My Assistant" '
              "--redirect-uri <uri>", file=sys.stderr)
        return 2
    if not uris:
        print("--new-client requires at least one --redirect-uri.\n\n"
              "  It is where the authorization server may send a user back to, and it\n"
              "  is matched EXACTLY. A client with no registered redirect URI cannot\n"
              "  complete a flow, and one with a loose match is an open redirect.\n\n"
              "  Your client's own documentation names its callback URL.",
              file=sys.stderr)
        return 2
    for uri in uris:
        problem = _bad_redirect_uri(uri)
        if problem:
            print(problem, file=sys.stderr)
            return 2

    store = store or oauth_store_for()
    client_id = store.register_client(name, uris)
    print("Registered a client. Paste this id into that client's configuration:\n")
    print(f"  {client_id}\n")
    print("There is no client secret: this authorization server authenticates\n"
          "clients as PUBLIC clients (`none`), which is what the MCP\n"
          "specification's registration mechanisms all produce, and PKCE is what\n"
          "protects the exchange instead.\n"
          f"Registered redirect URI(s): {', '.join(uris)}", file=sys.stderr)
    return 0


def _bad_redirect_uri(uri: str) -> str:
    """"" if this redirect URI may be registered, else why not.

    OAuth 2.1's communication-security rule — every redirect URI is loopback or
    HTTPS — and the open-redirect control, in one check. A plain-http redirect
    to a public host hands the authorization code to anything on the path.
    """
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.netloc:
        return ""
    if parts.scheme == "http" and (parts.hostname or "") in oauthlib.LOOPBACK_HOSTS:
        return ""
    return (f"--redirect-uri {uri!r} must be https:// or a loopback http:// address.\n\n"
            "  OAuth 2.1 requires it: a plain-http redirect to a public host hands\n"
            "  the authorization code to anything on the network path. Loopback is\n"
            "  the exception, for a client running on the user's own machine.")


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
