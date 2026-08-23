# bin/brainlib/httpmcp.py
"""MCP over Streamable HTTP, for a brain that lives on a server.

One endpoint, `POST /mcp`, plus two health paths. Everything else is 405.

**Dual-era, on one endpoint.** The MCP specification changed shape on
2026-07-28: the `initialize` handshake is gone, every request carries its own
protocol version in `_meta` and in a header, `server/discover` is mandatory,
and sessions were removed. Every client that exists today still speaks the
earlier, handshake-based revisions. The 2026-07-28 revision anticipates exactly
this and specifies how to serve both from one endpoint: a request carrying
modern per-request metadata is served statelessly under the new rules, and an
`initialize` request selects the legacy semantics. So this server implements
both, and the era is decided per request rather than per deployment.

**Answering with JSON, never SSE.** Rule 6 of the transport says a server may
answer a request with `application/json` OR `text/event-stream`, and the client
must support both. This server never initiates a message, has no progress to
report and no subscriptions, so it always answers with one JSON object. Saying
so is compliant; pretending to stream is not. `GET /mcp` is therefore 405,
which the specification explicitly permits for a server that offers no stream —
and which the modern revision requires.

**Stateless.** Session identifiers are a MAY in the legacy revisions and were
removed outright in the modern one. This server issues none, ignores any it is
sent, and keeps no per-client state at all. That is not a shortcut: a session
is state that has to be bounded, expired, made thread-safe and reasoned about
during a restart, and it buys a brain nothing, because every request is
authorized from scratch anyway. A restart costs a client one re-initialize and
costs the knowledge nothing.

**Authentication is not here.** It is in access.py, and it runs before this
module parses a body. What IS here is the consequence: the origin never emits
a `401` and never emits a `WWW-Authenticate` header, because Cloudflare Access
owns the OAuth challenge for these hostnames and answers it before the request
reaches the tunnel. An origin that also emitted one would either be shadowed,
or would win and hand a client an authorization server that is not the one that
can issue it a token. The origin's own refusals are `403`, so they can never be
mistaken for the challenge.
"""
from __future__ import annotations

import base64
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import access
from . import mcpcore

ENDPOINT = "/mcp"
SERVER_NAME = "brain"
SERVER_VERSION = "1.0.0"

# Every released protocol version, newest first. Membership in an enumerated
# set, never a string comparison: these are dates, but "2026-07-28" > "2025-11-25"
# is an accident of formatting and not a fact about compatibility.
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS
PREFERRED_LEGACY = "2025-11-25"

# A client that predates the MCP-Protocol-Version header (2025-06-18 introduced
# it) sends none, and the specification says to assume this.
ASSUMED_LEGACY_VERSION = "2025-03-26"

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"

# Caching hints are REQUIRED on the two results this server produces that the
# revision calls cacheable. `private` is not a default — both results VARY BY
# PRINCIPAL: tools_for() hides brain_capture from the read-only profile and the
# instructions differ. A shared cache is documented as possibly serving one
# caller's result to another even on an authenticated endpoint, so `public`
# here would be a way to hand a read-only caller the capture endpoint's tool
# list. `private` is the correctness fix and the security one at once.
DISCOVER_TTL_MS = 3_600_000
TOOLS_TTL_MS = 300_000
CACHE_SCOPE = "private"

MAX_BODY_BYTES = 1024 * 1024              # the spec's 1 MiB cap
READ_TIMEOUT_SECONDS = 30.0
CAPTURE_TIMEOUT_SECONDS = 120.0

# Per verified principal, per minute. Protocol chatter — initialize, ping,
# tools/list, server/discover — is deliberately counted in its own generous
# bucket so that a client reconnecting after a token refresh cannot consume the
# budget a capture needs.
RATE_LIMITS = {"read": (120, 60.0), "capture": (10, 60.0), "protocol": (600, 60.0)}

PROTOCOL_METHODS = frozenset(("initialize", "notifications/initialized", "ping",
                              "tools/list", "server/discover"))

# JSON-RPC and MCP error codes, all from the specification.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022


class Limiter(object):
    """A token bucket per principal per class of work.

    Keyed on the HMAC principal id, not on the client address: behind a tunnel
    every client shares one address, so an address-keyed limiter would let any
    one client throttle every other. There is nothing to key on but identity,
    and identity is exactly what the assertion already established."""

    def __init__(self, limits=None, clock=time.time):
        self._limits = dict(limits or RATE_LIMITS)
        self._clock = clock
        self._buckets = {}
        self._lock = threading.Lock()

    def take(self, principal: str, bucket: str):
        """Return (allowed, retry_after_seconds)."""
        capacity, window = self._limits[bucket]
        now = self._clock()
        key = (principal, bucket)
        with self._lock:
            if len(self._buckets) > 4096:
                # A bounded map. Nothing here is worth an unbounded dictionary
                # that a stream of distinct principals could grow forever.
                cutoff = now - window
                self._buckets = {k: v for k, v in self._buckets.items()
                                 if v and v[-1] > cutoff}
            hits = [t for t in self._buckets.get(key, ()) if t > now - window]
            if len(hits) >= capacity:
                self._buckets[key] = hits
                return False, max(1, int(window - (now - hits[0])) + 1)
            hits.append(now)
            self._buckets[key] = hits
        return True, 0


class Service(object):
    """Everything a request needs, assembled once at startup.

    A plain object rather than module globals so a test can build one pointed
    at a sandbox, and so two of them can exist in one process without the
    second quietly reconfiguring the first."""

    def __init__(self, verifier, capturer=None, log=None, limiter=None,
                 allowed_origins=(), origin_policy: str = "strict",
                 readiness=None, clock=time.time):
        self.verifier = verifier
        self.capturer = capturer
        self.log = log
        self.limiter = limiter or Limiter(clock=clock)
        self.allowed_origins = frozenset(o.strip().lower() for o in allowed_origins if o)
        if origin_policy not in ("strict", "observe"):
            raise ValueError("origin_policy must be 'strict' or 'observe'")
        self.origin_policy = origin_policy
        self._readiness = readiness
        self.maintenance = None               # a reason string disables mutation
        self._clock = clock

    def ready(self):
        """(ok, reason). Readiness is more than liveness and must say why not."""
        if self._readiness is None:
            return True, ""
        try:
            return self._readiness()
        except Exception as exc:              # pragma: no cover - defensive
            return False, type(exc).__name__

    def record(self, event: str, **fields) -> None:
        if self.log is not None:
            try:
                self.log.record(event, **fields)
            except ValueError:
                raise                          # a vocabulary bug must not be silent
            except Exception:                  # pragma: no cover - IO already swallowed
                pass


def correlation_id() -> str:
    """A short, random, meaningless handle for one request.

    Meaningless is the requirement. It goes into the client's error message and
    into the operator's log, and it is the only thing that appears in both — so
    it must reveal nothing about who asked, what they asked, or when."""
    return secrets.token_hex(8)


def _decode_header_value(value: str) -> str:
    """Undo the modern transport's base64 sentinel, if present."""
    if isinstance(value, str) and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except Exception:
            return value
    return value


class Handler(BaseHTTPRequestHandler):
    """One HTTP request. Every path through here ends in exactly one response."""

    server_version = "brain/%s" % SERVER_VERSION
    sys_version = ""                          # never advertise the Python version
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    @property
    def service(self) -> Service:
        return self.server.service

    def log_message(self, fmt, *args):
        """Silence the stdlib's stderr access log.

        It prints the request line, which for this server is a URL that can
        carry nothing sensitive today but is one refactor away from carrying a
        note id. The structured event log is the only record, and it is
        allowlisted by construction."""
        return

    def _send(self, status: int, payload=None, headers=None, cid: str = "") -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cid:
            self.send_header("Mcp-Correlation-Id", cid)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _refuse(self, status: int, cid: str, message: str, event: str,
                headers=None, **fields) -> None:
        """An HTTP-level refusal: a status, a correlation id, and nothing else.

        No stack trace, no claim value, no path, no hint about which check
        failed. A caller that can tell 'wrong audience' from 'expired' from
        'not the owner' has been handed a map of what to try next."""
        self.service.record(event, cid=cid, status=status, **fields)
        self._send(status, {"error": message, "correlation_id": cid},
                   headers=headers, cid=cid)

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        if self.path in ("/healthz", "/readyz"):
            return self._health(self.path == "/readyz")
        if self.path.split("?", 1)[0] == ENDPOINT:
            # Permitted for a server that offers no SSE stream, and required by
            # the modern revision. 404 would be wrong: a client probing for the
            # deprecated HTTP+SSE transport reads 404 as "try the old way".
            return self._send(405, {"error": "this endpoint accepts POST only"},
                              headers={"Allow": "POST"})
        return self._send(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.split("?", 1)[0] == ENDPOINT:
            return self._send(405, {"error": "this server issues no sessions"},
                              headers={"Allow": "POST"})
        return self._send(404, {"error": "not found"})

    def do_HEAD(self):
        return self.do_GET()

    def _health(self, deep: bool) -> None:
        """Liveness and readiness, with nothing private in either.

        Neither path requires an assertion, and both are reachable only from
        inside the Compose network — Cloudflare Access covers every path on the
        public hostnames, so the outside world cannot read these at all. They
        exist for the container healthcheck and the tunnel, which have no way
        to obtain an assertion and must not be given one."""
        if not deep:
            return self._send(200, {"status": "ok", "service": SERVER_NAME})
        ok, reason = self.service.ready()
        if self.service.maintenance:
            ok, reason = False, "maintenance"
        return self._send(200 if ok else 503,
                          {"status": "ready" if ok else "not-ready",
                           "reason": reason if not ok else "",
                           "service": SERVER_NAME})

    def do_POST(self):
        cid = correlation_id()
        started = time.time()
        if self.path.split("?", 1)[0] != ENDPOINT:
            return self._send(404, {"error": "not found"}, cid=cid)

        origin = self.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            # A MUST in the transport spec, and the status is specified: 403.
            return self._refuse(403, cid, "origin not allowed", "origin_refused")

        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if content_type and content_type.lower() != "application/json":
            return self._refuse(415, cid, "content type must be application/json",
                                "content_type_refused")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._refuse(400, cid, "bad content length", "body_refused")
        if length > MAX_BODY_BYTES:
            return self._too_large(cid, length)
        # Read at most the cap plus one byte even when Content-Length lies: a
        # chunked or mis-declared body must not be able to allocate more than
        # the limit just because it claimed to be small.
        raw = self.rfile.read(min(length, MAX_BODY_BYTES + 1)) if length else b""
        if len(raw) > MAX_BODY_BYTES:
            return self._too_large(cid, length, already_read=len(raw))

        try:
            principal = self.service.verifier.verify(
                self.headers.get("Cf-Access-Jwt-Assertion"), self.headers.get("Host"))
        except access.AccessDenied as denied:
            # 403, never 401. Cloudflare Access owns the OAuth challenge for
            # these hostnames; a 401 from here would be a challenge with no
            # WWW-Authenticate behind it, which RFC 6750 forbids and which a
            # client cannot act on.
            return self._refuse(403, cid, "not authorized", "auth_failed",
                                reason=denied.category, mode="none")

        try:
            message = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:
            # Deliberately broad. json.loads raises more than JSONDecodeError —
            # deeply nested input raises RecursionError, which is precisely the
            # payload an attacker would send.
            self.service.record("body_refused", cid=cid, status=400,
                                principal=principal.stable_id, reason="unparseable")
            return self._send(400, _error(None, PARSE_ERROR, "parse error"), cid=cid)
        if isinstance(message, list):
            # Batching was removed in 2025-06-18 and has not returned.
            return self._send(400, _error(None, INVALID_REQUEST,
                                          "JSON-RPC batching is not supported"), cid=cid)
        if not isinstance(message, dict):
            return self._send(400, _error(None, INVALID_REQUEST,
                                          "body must be a JSON-RPC message"), cid=cid)

        try:
            self._dispatch(message, principal, cid, started)
        except Exception as exc:              # pragma: no cover - last resort
            self.service.record("tool_error", cid=cid, principal=principal.stable_id,
                                outcome="error", reason=type(exc).__name__)
            self._send(500, _error(message.get("id"), INTERNAL_ERROR,
                                   "internal error (%s)" % cid), cid=cid)

    # How much of an over-large body to read and throw away before answering.
    # Without this the server answers 413 while the client is still writing, the
    # client's socket fills, and it sees a broken pipe instead of the perfectly
    # clear status we just sent. Bounded, because draining is a service to a
    # well-behaved client and must not become free work for a hostile one.
    MAX_DRAIN_BYTES = 8 * 1024 * 1024

    def _too_large(self, cid: str, length: int, already_read: int = 0) -> None:
        remaining = min(max(length - already_read, 0), self.MAX_DRAIN_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        # Close regardless: if the body was larger than we were willing to
        # drain, whatever is left would be read as the next request on a
        # keep-alive connection, and a fragment of JSON is not a request.
        self.close_connection = True
        self._refuse(413, cid, "request body exceeds 1 MiB", "body_refused",
                     headers={"Connection": "close"})

    def _origin_allowed(self, origin: str) -> bool:
        if origin.strip().lower() in self.service.allowed_origins:
            return True
        if self.service.origin_policy == "observe":
            # Deliberate, temporary, and configuration rather than code. No MCP
            # client is known to send an Origin through the tunnel, and the
            # cost of guessing wrong in the strict direction is a 403 on the
            # owner's only working client with nothing in the client's UI to
            # explain it. Observe mode records what real clients actually send
            # so the allowlist can be written from evidence; the runbook's
            # client-qualification step turns it to strict.
            self.service.record("origin_refused", status=200, outcome="ok",
                                reason="observed")
            return True
        return False

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, message: dict, principal, cid: str, started: float) -> None:
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        header_version = self.headers.get("MCP-Protocol-Version")
        body_version = meta.get(META_VERSION)

        # Era selection keys on the SHAPE of the request, never on whether the
        # named version happens to be one we support. The distinction is not
        # academic: keying on supported-ness made the UnsupportedProtocolVersion
        # branch below unreachable, so a client naming a FUTURE revision fell
        # through to the legacy path and got a plain 400 with no JSON-RPC body —
        # and the transport tells a dual-era client that a 400 without a
        # recognised modern error means "fall back to initialize". A client
        # ahead of this server would therefore have silently downgraded instead
        # of being told which versions it could retry with.
        modern = (META_VERSION in meta
                  or (header_version in MODERN_VERSIONS and method != "initialize"))
        client = meta.get(META_CLIENT_INFO) if modern else params.get("clientInfo")
        client_name, client_version = _client_identity(client)

        if modern:
            return self._dispatch_modern(message, method, msg_id, params, meta,
                                         header_version, body_version, principal, cid,
                                         started, client_name, client_version)
        return self._dispatch_legacy(message, method, msg_id, params, header_version,
                                     principal, cid, started, client_name, client_version)

    # -- the handshake era: 2024-11-05 .. 2025-11-25 ----------------------

    def _dispatch_legacy(self, message, method, msg_id, params, header_version,
                         principal, cid, started, client_name, client_version) -> None:
        if header_version is not None and method != "initialize":
            if header_version not in SUPPORTED_VERSIONS:
                return self._refuse(400, cid, "unsupported protocol version",
                                    "protocol_refused", principal=principal.stable_id,
                                    protocol=str(header_version)[:32])

        if method == "initialize":
            asked = params.get("protocolVersion")
            # Counter-offer rather than error: the spec says a server that does
            # not support the requested version responds with one it does
            # support, and lets the client decide whether it can live with it.
            agreed = asked if asked in LEGACY_VERSIONS else PREFERRED_LEGACY
            self._allow_or_refuse(principal, "protocol", cid)
            self.service.record("request", cid=cid, method="initialize",
                                principal=principal.stable_id, profile=principal.profile,
                                endpoint=principal.endpoint, mode=principal.mode,
                                client=client_name, client_version=client_version,
                                protocol=str(agreed)[:32], outcome="ok",
                                ms=_ms(started))
            return self._send(200, _result(msg_id, {
                "protocolVersion": agreed,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": _instructions(principal.profile),
            }), cid=cid)

        if msg_id is None:
            # A notification. 202 with an empty body, and never a JSON-RPC
            # response — a response to a notification is a protocol violation
            # that some clients treat as fatal.
            return self._send(202, None, cid=cid)

        if method == "ping":
            return self._send(200, _result(msg_id, {}), cid=cid)

        if method == "tools/list":
            allowed, retry = self.service.limiter.take(principal.stable_id, "protocol")
            if not allowed:
                return self._rate_limited(cid, principal, retry)
            return self._send(200, _result(msg_id, {
                "tools": mcpcore.tools_for(principal.profile, "remote")}), cid=cid)

        if method == "tools/call":
            return self._tools_call(msg_id, params, principal, cid, started,
                                    client_name, client_version)

        return self._send(200, _error(msg_id, METHOD_NOT_FOUND,
                                      "method not found: %s" % method), cid=cid)

    # -- the modern era: 2026-07-28 ---------------------------------------

    def _dispatch_modern(self, message, method, msg_id, params, meta, header_version,
                         body_version, principal, cid, started, client_name,
                         client_version) -> None:
        # The required per-request _meta fields come FIRST, and the order is the
        # point: an absent body field and a mismatched one are different
        # failures with different codes. Checking the header comparison first
        # answered a request that simply omitted the version with -32020
        # HeaderMismatch, when the rule for a missing required field is -32602.
        #
        # clientCapabilities is required and an empty object is valid — the
        # revision is stateless, so a request that does not say what the client
        # can do leaves the server with no way to find out.
        if not isinstance(body_version, str) or not body_version:
            return self._send(400, _error(msg_id, INVALID_PARAMS,
                                          "missing required _meta field: %s"
                                          % META_VERSION), cid=cid)
        if not isinstance(meta.get(META_CLIENT_CAPS), dict):
            return self._send(400, _error(msg_id, INVALID_PARAMS,
                                          "missing required _meta field: %s"
                                          % META_CLIENT_CAPS), cid=cid)
        # An unsupported version is answered before the header comparison too:
        # a client naming a version we do not speak needs the list it can retry
        # with, not a complaint about header mirroring.
        if body_version not in SUPPORTED_VERSIONS:
            return self._send(400, _error(
                msg_id, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                data={"supported": list(SUPPORTED_VERSIONS), "requested": body_version}),
                cid=cid)
        # Header and body must agree. The reason is not pedantry: an
        # intermediary may route on the header while this server executes on
        # the body, and the whole point of mirroring is that the two cannot
        # disagree about what is being asked.
        if header_version is None or header_version != body_version:
            return self._send(400, _error(msg_id, HEADER_MISMATCH,
                                          "MCP-Protocol-Version header does not match "
                                          "the request body"), cid=cid)
        header_method = self.headers.get("Mcp-Method")
        if header_method is None or header_method != method:
            return self._send(400, _error(msg_id, HEADER_MISMATCH,
                                          "Mcp-Method header does not match the request "
                                          "body"), cid=cid)

        if msg_id is None:
            return self._send(202, None, cid=cid)

        if method == "server/discover":
            self.service.record("request", cid=cid, method=method,
                                principal=principal.stable_id, profile=principal.profile,
                                endpoint=principal.endpoint, mode=principal.mode,
                                client=client_name, client_version=client_version,
                                protocol=body_version, outcome="ok", ms=_ms(started))
            return self._send(200, _modern_result(msg_id, {
                "supportedVersions": list(SUPPORTED_VERSIONS),
                "ttlMs": DISCOVER_TTL_MS,
                "cacheScope": CACHE_SCOPE,
                "capabilities": {"tools": {}},
                "instructions": _instructions(principal.profile),
                "_meta": {META_SERVER_INFO: {"name": SERVER_NAME,
                                             "version": SERVER_VERSION}},
            }), cid=cid)

        if method == "ping":
            return self._send(200, _modern_result(msg_id, {}), cid=cid)

        if method == "tools/list":
            allowed, retry = self.service.limiter.take(principal.stable_id, "protocol")
            if not allowed:
                return self._rate_limited(cid, principal, retry)
            return self._send(200, _modern_result(msg_id, {
                "tools": mcpcore.tools_for(principal.profile, "remote"),
                "ttlMs": TOOLS_TTL_MS,
                "cacheScope": CACHE_SCOPE}), cid=cid)

        if method == "tools/call":
            # Mcp-Name is REQUIRED on tools/call, and absence is its own
            # failure. Coalescing a missing header to "" made a call with
            # neither a header nor a body name compare equal and pass — the
            # exact request an intermediary routing on the header cannot see.
            raw_name = self.headers.get("Mcp-Name")
            if raw_name is None:
                return self._send(400, _error(msg_id, HEADER_MISMATCH,
                                              "missing required header: Mcp-Name"),
                                  cid=cid)
            if _decode_header_value(raw_name) != params.get("name"):
                return self._send(400, _error(msg_id, HEADER_MISMATCH,
                                              "Mcp-Name header does not match the "
                                              "request body"), cid=cid)
            return self._tools_call(msg_id, params, principal, cid, started,
                                    client_name, client_version, modern=True)

        # An unimplemented method is 404 in the modern era, not 200. The status
        # is what lets a dual-era CLIENT tell a modern server from a legacy one.
        return self._send(404, _error(msg_id, METHOD_NOT_FOUND,
                                      "method not found: %s" % method), cid=cid)

    # -- the tools ---------------------------------------------------------

    def _tools_call(self, msg_id, params, principal, cid, started,
                    client_name, client_version, modern: bool = False) -> None:
        # Shared by both eras, so the result shape has to be chosen here rather
        # than assumed. A 2026-07-28 result carries resultType and serverInfo; a
        # legacy one must NOT, because those revisions never defined them.
        reply = _modern_result if modern else _result
        name = params.get("name")
        args = params.get("arguments")
        writes = name == "brain_capture"

        if writes and name not in mcpcore.PROFILES[principal.profile]:
            # The read-only boundary, enforced where a hand-built call arrives
            # rather than only where the list is advertised. This is the
            # security check; hiding the tool was the usability one.
            self.service.record("tool_denied", cid=cid, tool=str(name)[:64],
                                principal=principal.stable_id, profile=principal.profile,
                                endpoint=principal.endpoint, outcome="denied",
                                ms=_ms(started))
            return self._send(200, reply(msg_id, mcpcore.error_result(
                "brain_capture is not available on this endpoint. This is the read-only "
                "brain; use the read/capture endpoint to save a note.")), cid=cid)

        allowed, retry = self.service.limiter.take(
            principal.stable_id, "capture" if writes else "read")
        if not allowed:
            return self._rate_limited(cid, principal, retry, tool=str(name)[:64])

        if writes and self.service.maintenance:
            # Fail closed on mutation while reads stay available, exactly as the
            # spec's failure table requires.
            self.service.record("maintenance_mode", cid=cid, principal=principal.stable_id,
                                outcome="error", reason=self.service.maintenance[:64])
            return self._refuse(503, cid, "the brain is not accepting writes right now",
                                "maintenance_mode", headers={"Retry-After": "60"})

        if writes:
            result = self._capture(args, principal, cid)
        else:
            result = mcpcore.call(name, args, profile=principal.profile,
                                  transport="remote", timeout=READ_TIMEOUT_SECONDS)
        self.service.record("tool_call", cid=cid, tool=str(name)[:64],
                            principal=principal.stable_id, profile=principal.profile,
                            endpoint=principal.endpoint, mode=principal.mode,
                            client=client_name, client_version=client_version,
                            outcome="error" if result.get("isError") else "ok",
                            ms=_ms(started))
        return self._send(200, reply(msg_id, result), cid=cid)

    def _capture(self, args, principal, cid) -> dict:
        table = {tool["name"]: tool for tool in mcpcore.tool_table("remote")}
        clean, problem = mcpcore.validate_args(table["brain_capture"],
                                               args if args is not None else {})
        if problem:
            return mcpcore.error_result("invalid arguments for brain_capture: %s" % problem)
        if self.service.capturer is None:      # pragma: no cover - misconfiguration
            return mcpcore.error_result("this brain is configured without a write path")
        outcome = self.service.capturer.capture(
            clean["text"], principal.profile, principal.stable_id, cid,
            client_request_id=clean.get("client_request_id"),
            allow_duplicate=bool(clean.get("allow_duplicate", False)))
        from . import capture as capturelib
        return {"content": [{"type": "text", "text": capturelib.format_result(outcome)}],
                "isError": not outcome.get("ok", False)}

    def _rate_limited(self, cid, principal, retry, tool=None) -> None:
        self._refuse(429, cid, "rate limit exceeded; retry in %ds" % retry,
                     "rate_limited", principal=principal.stable_id,
                     profile=principal.profile, tool=tool, retry_after=retry,
                     headers={"Retry-After": str(retry)})

    def _allow_or_refuse(self, principal, bucket, cid):
        allowed, retry = self.service.limiter.take(principal.stable_id, bucket)
        if not allowed:
            self._rate_limited(cid, principal, retry)
        return allowed


def _instructions(profile: str) -> str:
    common = ("This is the owner's permanent second brain. Search it BEFORE answering "
              "anything about their decisions, preferences, projects, people or history. "
              "Results carry trust markers — [provisional — unconsolidated], [journal], "
              "an ARCHIVED banner, a passed review_by — and those markers are part of the "
              "answer: never present a provisional or archived note as settled fact. "
              "If a query misses, try 2-3 lexical variants before concluding there is "
              "nothing.")
    if profile == "capture":
        return common + (" You may also save a note. Capture the WHY, never the "
                         "recoverable what, never a credential, and always ask first "
                         "before recording anything about a named person's private life.")
    return common + " This endpoint is read-only; it cannot save anything."


def _client_identity(client):
    if not isinstance(client, dict):
        return None, None
    # Self-reported and unverified by the protocol — recorded for display and
    # debugging only, and never used to decide anything.
    name = client.get("name")
    version = client.get("version")
    return (str(name)[:64] if isinstance(name, str) else None,
            str(version)[:32] if isinstance(version, str) else None)


def _ms(started: float) -> int:
    return int((time.time() - started) * 1000)


def _result(msg_id, result: dict) -> dict:
    """A legacy-era result: the payload, exactly as the handshake revisions expect."""
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _modern_result(msg_id, payload: dict) -> dict:
    """A 2026-07-28 result, which is not the same shape as a legacy one.

    Two additions the revision requires of EVERY result, and one choke point so
    that adding a method later cannot forget them:

    `resultType` is mandatory — "The `result` MUST include a `resultType` field
    to indicate the type of the result." It is the discriminator that lets a
    client tell a finished answer from an `input_required` one under
    multi-round-trip requests. This server never asks the client for input, so
    every result it produces is `complete`; that is a fact about this server,
    not a default to lean on. The spec's absent-means-complete rule is a bridge
    for EARLIER-revision servers only, so a modern server that omits it is
    simply invalid — which is exactly how the first real client failed: it
    authenticated, negotiated 2026-07-28, and then refused `tools/list`.

    `serverInfo` in `_meta` is a SHOULD, and it is included because the
    revision is stateless: there is no handshake left in which to say who is
    answering, so a response that omits it leaves a client with no way to
    identify the server it is talking to. It is self-reported and unverified,
    which is why nothing here depends on it."""
    result = dict(payload)
    result["resultType"] = "complete"
    meta = dict(result.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, {"name": SERVER_NAME, "version": SERVER_VERSION})
    result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code: int, message: str, data=None) -> dict:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: Service):
        ThreadingHTTPServer.__init__(self, address, Handler)
        self.service = service


def make_server(service: Service, host: str = "127.0.0.1", port: int = 0) -> Server:
    """Build the server. The default bind is loopback and the caller says otherwise.

    In production it binds 0.0.0.0 inside a container that publishes no port, so
    the only route in is the Compose network the tunnel shares with it. That is
    a deliberate exception to the transport spec's 'bind localhost when running
    locally' guidance and not a contradiction of it: nothing else on the host
    can route to the container's network."""
    return Server((host, port), service)
