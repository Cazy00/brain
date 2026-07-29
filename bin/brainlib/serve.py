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

No TLS here, deliberately. The tunnel terminates TLS; this serves plaintext to
loopback, which is correct, or to whatever the operator explicitly asked for,
which is their call and is printed back to them.

Transport: Streamable HTTP per the 2025-06-18 MCP specification, minus the
optional parts. One endpoint, POST only, a single JSON object per request. GET
answers 405, which the spec explicitly permits for a server that offers no SSE
stream — saying so is compliant, pretending to stream is not. No session IDs:
they are a MAY and this server is stateless.
"""
import hmac
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import mcp
from . import osbackend

ENDPOINT = "/mcp"
TOKEN_NAME = "brain-serve-token"

# The same ceiling the stdio loop uses: a single request larger than this is
# not real traffic.
MAX_BODY_BYTES = 10_000_000

# Versions this server has actually been checked against. The spec requires a
# 400 for anything else, and keeping the list a literal means widening it is a
# deliberate edit rather than something that happens by accident.
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18",
                               "2025-11-25")


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
        body = json.dumps({"error": message}).encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        """Origin, then token. Everything else happens after both."""
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allow_origin:
            self._refuse(403, "this server does not accept browser origins")
            return False

        supplied = self.headers.get("Authorization", "")
        scheme, _, value = supplied.partition(" ")
        # compare_digest on BOTH the scheme and the token: an early return on
        # the scheme is harmless (it is not a secret), but the token comparison
        # must not short-circuit.
        if scheme.lower() != "bearer" or not hmac.compare_digest(
                value.strip(), self.server.token):
            self._refuse(401, "a bearer token is required",
                         {"WWW-Authenticate": 'Bearer realm="brain"'})
            return False

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
            out = mcp.handle(msg)
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
                allow_origin=(), quiet: bool = False) -> ThreadingHTTPServer:
    """A configured, unstarted server. Refuses to exist without a token.

    Raising here rather than at the call site is deliberate: this is the
    function every caller — the CLI, the tests, anything later — has to go
    through, so the one rule that must never be bypassed lives at the choke
    point rather than in each caller's good intentions.
    """
    if not (token or "").strip():
        raise ValueError("brain serve requires a token; mint one with "
                         "`brain serve --new-token`")
    server = _Server((host, port), _Handler)
    server.token = token
    server.allow_origin = tuple(allow_origin)
    server.quiet = quiet
    return server
