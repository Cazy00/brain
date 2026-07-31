# bin/brainlib/oauth.py
"""The MCP authorization spec, so any hosted assistant can reach this brain.

`brain serve` has always taken a bearer token in a header, which is what a
LOCAL client does — Claude Code, Codex CLI, Cursor, VS Code all set one, and
they still do; nothing here changes that path. A HOSTED assistant cannot: it
runs on somebody else's servers, the user never touches a config file, and the
only credential it can obtain is one a user consented to in a browser. That is
what this module is for.

It is built to the SPECIFICATION rather than to a vendor, and that is the whole
design constraint. The owner's instruction on 2026-07-31 was blunt about it —
*"it's all the same protocol … the same way of setting, if not similar"* — and
the research agrees: the two largest hosted assistants converge on OAuth 2.1 +
PKCE + protected-resource metadata + Client ID Metadata Documents, with
public-client (`none`) token exchange. So there is no vendor branch anywhere in
this file, and no vendor name outside a comment.

The pieces, all RFCs:

- RFC 9728 protected-resource metadata — what this server is, and where its
  authorization server lives.
- RFC 8414 authorization-server metadata — what the authorization server can
  do. `code_challenge_methods_supported: ["S256"]` is a MUST: a conforming
  client refuses to start a flow without it.
- RFC 7636 PKCE, S256 only.
- RFC 8707 resource indicators — every token is bound to the URL it was issued
  for, and checked against it on every request.
- RFC 9207 `iss` in the authorization response, against mix-up attacks.
- Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document)
  — a client identifies itself with an HTTPS URL that serves its own metadata,
  which this server fetches and validates.

**Dynamic client registration is deliberately absent.** The MCP specification
now marks RFC 7591 DCR *deprecated*, retained only for authorization servers
that cannot do Client ID Metadata Documents. Building `/register` would mean
shipping a deprecated mechanism AND an endpoint through which an
unauthenticated caller creates database rows. A client that speaks only DCR
will fail to find `registration_endpoint` in the metadata below; that is the
signal, it is documented in the runbook, and `brain serve --new-client` is the
answer — a pre-registered client id, which is the third mechanism the
specification names and works for anything.

**No JWTs.** Tokens here are opaque random values, stored as SHA-256 digests.
That keeps this file inside the standard library (this repo has zero
third-party dependencies and that claim is load-bearing), and it buys the
property a self-contained token structurally cannot have: revocation.
"""
import http.client
import ipaddress
import json
import os
import socket
import sqlite3
import ssl
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import mcp

SCHEMA_VERSION = 1

# The two scopes, mapped onto the read/write split the tool table already
# declares through `readOnlyHint`. Not invented here: the distinction already
# exists, `--read-only` already enforces it process-wide, and a scope is the
# same question asked of one token instead of one process.
SCOPE_READ = "brain:read"
SCOPE_WRITE = "brain:write"

# Asked for by a client that wants a refresh token. Advertised in the
# AUTHORIZATION SERVER metadata and deliberately NOT in the protected-resource
# metadata: the MCP spec says a resource server SHOULD NOT list it, because a
# refresh token is not a thing the resource requires.
SCOPE_OFFLINE = "offline_access"

WELL_KNOWN_PRM = "/.well-known/oauth-protected-resource"
WELL_KNOWN_AS = "/.well-known/oauth-authorization-server"


class ConfigError(ValueError):
    """A misconfiguration the operator can fix, phrased so they can fix it."""


class Config:
    """Everything this authorization server knows about its own identity.

    All of it derives from ONE operator-supplied string, and that is the
    security property rather than a convenience. Behind a tunnel this process
    sees `127.0.0.1:8787`; the client typed `https://brain.example.com/mcp`,
    and RFC 9728 requires the advertised `resource` to match what the user
    typed EXACTLY, including the path. Deriving any of it from a request header
    instead would let anyone who can reach the socket mint tokens whose
    audience is a hostname they chose — the same reasoning that makes `Limiter`
    refuse to trust `X-Forwarded-For` a few hundred lines away.
    """

    def __init__(self, public_url: str, scopes=(SCOPE_READ, SCOPE_WRITE)):
        self.resource = public_url
        parts = urlsplit(public_url)
        # The issuer is the ORIGIN, with no path. That is what makes the
        # authorization-server metadata live at exactly
        # `<origin>/.well-known/oauth-authorization-server` — an issuer WITH a
        # path sends conforming clients probing three different well-known
        # URLs in a defined order, and every one of them is a chance to
        # disagree with what this server serves.
        self.issuer = "{}://{}".format(parts.scheme, parts.netloc)
        self.resource_path = parts.path or ""
        self.scopes = tuple(scopes)

    # ------------------------------------------------------------- endpoints

    @property
    def authorization_endpoint(self) -> str:
        return self.issuer + "/authorize"

    @property
    def token_endpoint(self) -> str:
        return self.issuer + "/token"

    @property
    def revocation_endpoint(self) -> str:
        return self.issuer + "/revoke"

    @property
    def prm_url(self) -> str:
        """What the 401 points a client at.

        The path-inserted form when the resource has a path, which is RFC
        9728's canonical location for it; the root otherwise.
        """
        return self.issuer + WELL_KNOWN_PRM + self.resource_path

    def public_paths(self) -> dict:
        """Exact paths this server answers WITHOUT a token, and what each is.

        Exact keys, matched by equality, never by prefix. `startswith` on a
        path is how an unauthenticated-discovery exemption becomes an
        authentication bypass: `/.well-known/oauth-protected-resource/../mcp`
        and `/.well-known/oauth-protected-resourceX` both pass a prefix test
        and neither is discovery.

        Both the path-inserted and the root protected-resource locations are
        served, with the same document. A client that finds no
        `resource_metadata` pointer probes the sub-path first and the root
        second, and answering both costs one dict entry — where answering only
        one produces the failure that is hardest to diagnose from the outside,
        because this server sees the first request and the authorization
        server sees nothing at all.
        """
        paths = {
            WELL_KNOWN_PRM: "prm",
            WELL_KNOWN_AS: "as",
            "/authorize": "authorize",
            "/token": "token",
            "/revoke": "revoke",
        }
        if self.resource_path:
            paths[WELL_KNOWN_PRM + self.resource_path] = "prm"
        return paths

    # ------------------------------------------------------------- documents

    def protected_resource_metadata(self) -> dict:
        """RFC 9728. What this resource is, and who authorizes access to it."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(self.scopes),
            "bearer_methods_supported": ["header"],
            "resource_name": "brain",
        }

    def authorization_server_metadata(self) -> dict:
        """RFC 8414. Two of these fields are the whole of "not one vendor".

        `client_id_metadata_document_supported` and a
        `token_endpoint_auth_methods_supported` containing `none` are what make
        a conforming client choose Client ID Metadata Documents over
        registration — and they are checked as a PAIR by more than one
        implementation, because a CIMD client authenticates as a public client
        at the token endpoint. Advertising one without the other is how a
        server ends up being asked to register clients it did not want to
        register.

        `issuer` must be byte-identical to the origin the client used to build
        this document's URL. A conforming client discards metadata whose
        issuer differs, and it is right to: that check is the mitigation for a
        whole class of impersonation.
        """
        return {
            "issuer": self.issuer,
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "revocation_endpoint": self.revocation_endpoint,
            "scopes_supported": list(self.scopes) + [SCOPE_OFFLINE],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": True,
            "authorization_response_iss_parameter_supported": True,
            "revocation_endpoint_auth_methods_supported": ["none"],
        }

    def challenge(self) -> str:
        """The `WWW-Authenticate` value for a 401.

        The 401 status is what matters — the header is ignored on a 200 — and
        `resource_metadata` is what turns a refusal into the first step of a
        handshake rather than a dead end. `scope` is advertised too, so a
        client asks for what this process actually serves instead of guessing
        from the metadata and over-asking.
        """
        return 'Bearer resource_metadata="{}", scope="{}"'.format(
            self.prm_url, " ".join(self.scopes))


def parse_public_url(raw: str, allow_insecure_loopback: bool = True) -> str:
    """Validate `--public-url`, or raise ConfigError saying what is wrong.

    Refused at startup rather than discovered at runtime. Every rule here
    corresponds to a real failure that is invisible from the operator's side —
    the connector simply says it cannot reach the server — so the only place
    they can be caught cheaply is here, before anything is serving.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ConfigError("--oauth needs --public-url: the address a client will "
                          "actually type.\n\n"
                          "  Behind a tunnel this process only ever sees 127.0.0.1, and "
                          "every\n  token it issues is bound to the PUBLIC url. It cannot "
                          "guess it, and\n  guessing wrong issues tokens nothing accepts.\n\n"
                          "    brain serve --oauth --public-url https://brain.example.com/mcp")
    parts = urlsplit(raw)
    if parts.scheme not in ("https", "http"):
        raise ConfigError(f"--public-url must be an http(s) URL, not {raw!r}")
    if not parts.netloc:
        raise ConfigError(f"--public-url has no host: {raw!r}")
    if parts.query or parts.fragment:
        # RFC 8707 says a resource identifier carries no fragment, and a query
        # string in the audience is a token bound to a URL nobody will retype
        # the same way twice.
        raise ConfigError("--public-url must have no query string and no #fragment — "
                          "it is an identity, not a request")
    if parts.path.endswith("/"):
        # Normalising silently would be worse than refusing. The advertised
        # `resource` has to match what the user types into their assistant
        # EXACTLY, so the one string that decides it should be the one the
        # operator wrote, not one this code improved.
        raise ConfigError("--public-url must not end in '/' — the trailing slash "
                          "changes the resource identifier, and it has to match what "
                          "you type into the client exactly")
    if parts.scheme == "http":
        host = parts.hostname or ""
        if not (allow_insecure_loopback and host in ("127.0.0.1", "::1", "localhost")):
            # OAuth 2.1 requires HTTPS for every authorization server endpoint.
            # Loopback http stays legal so the whole flow can be exercised on a
            # laptop without a tunnel, which is what the end-to-end test does.
            raise ConfigError("--public-url must be https:// — an OAuth flow over "
                              "plain http exposes the authorization code and the "
                              "access token to anything on the path.\n\n"
                              "  http:// is accepted for 127.0.0.1 and localhost only, "
                              "for local testing.")
    return raw


def scopes_for(allow_tools) -> tuple:
    """Which scopes a process serving `allow_tools` may advertise or grant.

    Derived from the tool table's own `readOnlyHint` annotations, exactly as
    READ_ONLY_TOOLS is, and fails closed for the same reason: a tool added
    later with no annotation belongs to neither set, so it is covered by
    neither scope and is grantable through neither.

    This is what makes scopes COMPOSE with `--read-only` rather than duplicate
    it. The flag decides what the process will serve at all; the scope decides
    what one token may do inside that. A read-only process cannot advertise
    `brain:write`, cannot grant it, and would refuse it anyway.
    """
    served = tuple(mcp.TOOLS_BY_NAME) if allow_tools is None else tuple(allow_tools)
    out = []
    if any(name in mcp.READ_ONLY_TOOLS for name in served):
        out.append(SCOPE_READ)
    if any(name in mcp.WRITE_ONLY_TOOLS for name in served):
        out.append(SCOPE_WRITE)
    return tuple(out)


def tools_for_scopes(granted, allow_tools=None) -> frozenset:
    """Which tools a token holding `granted` may call.

    The intersection of what the PROCESS serves and what the TOKEN was granted.
    Used by the dispatcher, not by the advertised list — a client that never
    read tools/list and calls a tool by name has to be refused too, which is
    the same rule `--read-only` already follows and for the same reason.
    """
    served = set(mcp.TOOLS_BY_NAME) if allow_tools is None else set(allow_tools)
    allowed = set()
    if SCOPE_READ in granted:
        allowed |= set(mcp.READ_ONLY_TOOLS)
    if SCOPE_WRITE in granted:
        allowed |= set(mcp.WRITE_ONLY_TOOLS)
    return frozenset(served & allowed)


# ---------------------------------------------------------------------------
# Clients
#
# An authorization request names a `client_id`, and under Client ID Metadata
# Documents that id is an HTTPS URL this server FETCHES. Everything below
# exists because /authorize is unauthenticated, so the target of that outbound
# request is chosen by whoever sent the request — which is the definition of
# server-side request forgery, and on a rented VM the interesting targets are
# localhost and 169.254.169.254, the cloud metadata service that hands out
# credentials.
# ---------------------------------------------------------------------------

# Every way resolving a client can fail. They are written into the event log,
# which refuses any string it has not already agreed to, so this tuple has to
# stay a subset of eventlog.REASONS — a test asserts exactly that. A reason
# outside the vocabulary would be dropped at the moment somebody needed it.
REFUSAL_REASONS = (
    "not_https", "no_path", "blocked_address", "dns_failure", "redirected",
    "too_big", "timeout", "bad_document", "client_id_mismatch", "fetch_failed",
    "unknown_client",
)

# A metadata document is a few hundred bytes. 64 KB is generous by two orders
# of magnitude and still small enough that a hostile response cannot matter.
MAX_DOCUMENT_BYTES = 65_536
# Claude allows 10s for the whole /authorize response and this fetch happens
# inside it, so it has to be well under that. Other clients are stricter.
FETCH_TIMEOUT_SECONDS = 5.0
CACHE_SIZE = 64
CACHE_TTL_SECONDS = 900.0
# Shorter, on purpose: a client whose document is briefly broken should recover
# quickly, while a loop against a bad client_id still costs one fetch a minute.
NEGATIVE_CACHE_TTL_SECONDS = 60.0

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


class ClientRefused(Exception):
    """A client that will not be honoured, and the class of reason why.

    The reason is a VOCABULARY value, never a sentence, because it is written
    to the event log and to nothing else. What a client's document contained is
    not the operator's business and is not written anywhere.
    """

    def __init__(self, reason: str, detail: str = ""):
        Exception.__init__(self, reason)
        self.reason = reason
        self.detail = detail


class Client:
    """A resolved client: who it says it is, and where it may be sent back to."""

    def __init__(self, client_id: str, name: str, redirect_uris, kind: str):
        self.client_id = client_id
        self.name = name
        self.redirect_uris = tuple(redirect_uris)
        self.kind = kind

    def matches_redirect(self, presented: str) -> bool:
        """Exact string equality, with RFC 8252's loopback exception.

        Exact matching is what the specification requires and what prevents an
        open redirect. The exception is that a NATIVE client binds an ephemeral
        loopback port it cannot know in advance, so `http://127.0.0.1/callback`
        has to match `http://127.0.0.1:53119/callback`. That is a rule from RFC
        8252 section 7.3, applied to every client, and not an accommodation for
        any particular one.

        `localhost` and `127.0.0.1` deliberately do NOT cross-match: a client
        that wants both registers both, and treating them as interchangeable
        would accept a redirect the client never declared.
        """
        for registered in self.redirect_uris:
            if registered == presented:
                return True
            if _loopback_match(registered, presented):
                return True
        return False


def _loopback_match(registered: str, presented: str) -> bool:
    one, two = urlsplit(registered), urlsplit(presented)
    if one.scheme != "http" or two.scheme != "http":
        return False
    if (one.hostname or "") not in LOOPBACK_HOSTS:
        return False
    if one.hostname != two.hostname:
        return False
    # Everything except the port must still match exactly. A loopback
    # redirect whose PATH is free would let any local process claim the code.
    return one.path == two.path and one.query == two.query


def _unmapped(ip):
    """`::ffff:127.0.0.1` is loopback and Python says `is_loopback` is False
    for it. Unmapping first is the only thing that catches it."""
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def is_public_address(raw: str) -> bool:
    """Whether this server is willing to make a request to that address.

    Fails closed: anything unparseable is not public. `is_global` does most of
    the work, and the explicit list in front of it is not redundant — it names
    the cases that matter so a reader can see them, and it does not depend on
    one property of the standard library staying exactly as it is.
    """
    try:
        ip = _unmapped(ipaddress.ip_address(raw))
    except ValueError:
        return False
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return bool(ip.is_global)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to a specific ADDRESS, with the hostname kept for SNI and Host.

    Resolving a name, checking the addresses, and then connecting by name
    leaves a window in which DNS can answer differently the second time — the
    rebinding attack, and it turns every address check into a suggestion. This
    connects to the address that was actually checked. Fifteen lines, and
    without them the whole of `is_public_address` is decorative.
    """

    def __init__(self, host, address, **kwargs):
        http.client.HTTPSConnection.__init__(self, host, **kwargs)
        self._address = address

    def connect(self):
        self.sock = socket.create_connection((self._address, self.port),
                                             self.timeout)
        # server_hostname is the NAME, so certificate validation and SNI still
        # check what the client_id claimed rather than what it resolved to.
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _https_get(host, address, port, path, timeout, max_bytes):
    """One request, no redirects, capped. Returns (status, body bytes).

    Reads one byte MORE than the cap so an oversized body can be REFUSED
    rather than truncated — parsing what fits would let a hostile response
    decide what this server believes about a client.
    """
    context = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(host, address, port=port, timeout=timeout,
                                  context=context)
    try:
        conn.request("GET", path, headers={"Accept": "application/json",
                                           "User-Agent": "brain"})
        response = conn.getresponse()
        return response.status, response.read(max_bytes + 1)
    finally:
        conn.close()


class MetadataFetcher:
    """Fetch and validate a Client ID Metadata Document.

    The resolver and the getter are injected so no test reaches the network,
    and the seam sits BELOW the address check on purpose: a test can put a
    private address behind a public-looking hostname, which is the attack this
    class exists to stop and would otherwise be untestable.
    """

    def __init__(self, resolver=None, getter=None, clock=None,
                 timeout: float = FETCH_TIMEOUT_SECONDS,
                 max_bytes: int = MAX_DOCUMENT_BYTES,
                 cache_size: int = CACHE_SIZE,
                 ttl: float = CACHE_TTL_SECONDS,
                 negative_ttl: float = NEGATIVE_CACHE_TTL_SECONDS):
        self._resolve = resolver or self._default_resolver
        self._get = getter or _https_get
        self._clock = clock or time.monotonic
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._cache_size = cache_size
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._cache = {}          # url -> (expires, Client or ClientRefused)

    @staticmethod
    def _default_resolver(host):
        return [info[4][0] for info in
                socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]

    def cached(self) -> int:
        return len(self._cache)

    def __call__(self, url: str) -> Client:
        now = self._clock()
        hit = self._cache.get(url)
        if hit is not None and hit[0] > now:
            if isinstance(hit[1], ClientRefused):
                # A negative cache, and it is not an optimisation: /authorize
                # is unauthenticated, so without one a loop against a broken
                # client_id turns this server into an outbound request
                # amplifier pointed at whatever host that id names.
                raise hit[1]
            return hit[1]
        try:
            client = self._fetch(url)
        except ClientRefused as refused:
            self._remember(url, refused, now + self._negative_ttl)
            raise
        self._remember(url, client, now + self._ttl)
        return client

    def _remember(self, url, value, expires):
        # Bounded. A cache keyed on a caller-supplied URL that only grows is a
        # memory exhaustion primitive reachable without a token — the same
        # reasoning Limiter's table already carries, and the same resolution.
        if len(self._cache) >= self._cache_size:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])
            for key, _value in oldest[:len(self._cache) - self._cache_size + 1]:
                del self._cache[key]
        self._cache[url] = (expires, value)

    def _fetch(self, url: str) -> Client:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ClientRefused("not_https")
        if not parts.hostname:
            raise ClientRefused("not_https")
        if parts.path in ("", "/"):
            # The specification requires a path component. A bare origin as a
            # client_id is also indistinguishable from a typo.
            raise ClientRefused("no_path")

        try:
            addresses = self._resolve(parts.hostname)
        except OSError:
            raise ClientRefused("dns_failure")
        if not addresses:
            raise ClientRefused("dns_failure")
        if not all(is_public_address(address) for address in addresses):
            # EVERY address, not the first acceptable one. A host answering
            # with both a public and a private address is a rebinding setup,
            # and choosing the good one would mean the check passed on an
            # address the connection might not have used.
            raise ClientRefused("blocked_address")

        path = parts.path + (("?" + parts.query) if parts.query else "")
        try:
            status, body = self._get(parts.hostname, addresses[0],
                                     parts.port or 443, path,
                                     self._timeout, self._max_bytes)
        except (OSError, ssl.SSLError):
            raise ClientRefused("fetch_failed")
        if status in (301, 302, 303, 307, 308):
            # Never followed. A redirect names a second target, and that target
            # was never address-checked — following one hands back every
            # protection above.
            raise ClientRefused("redirected")
        if status != 200:
            raise ClientRefused("fetch_failed")
        if len(body) > self._max_bytes:
            raise ClientRefused("too_big")
        return _validate_document(url, body)


def _validate_document(url: str, body: bytes) -> Client:
    try:
        doc = json.loads(body.decode("utf-8"))
    except Exception:
        raise ClientRefused("bad_document")
    if not isinstance(doc, dict):
        raise ClientRefused("bad_document")
    if doc.get("client_id") != url:
        # The specification's central requirement, and the reason a URL can be
        # an identity at all: without it, anybody can host a document claiming
        # to be somebody else's client, and the consent screen shows THEIR
        # name over a redirect to the attacker's callback.
        raise ClientRefused("client_id_mismatch")
    name = doc.get("client_name")
    uris = doc.get("redirect_uris")
    if not isinstance(name, str) or not name.strip():
        # The name is what a human reads on the consent screen. A client with
        # no name is one the operator cannot make a decision about.
        raise ClientRefused("bad_document")
    if (not isinstance(uris, list) or not uris
            or not all(isinstance(uri, str) and uri for uri in uris)):
        raise ClientRefused("bad_document")
    return Client(url, name.strip(), tuple(uris), "cimd")


def resolve_client(client_id: str, store=None, fetch=None) -> Client:
    """A client_id to a Client, by whichever of the two mechanisms applies.

    A URL is a Client ID Metadata Document. Anything else is a pre-registered
    id, which the operator minted with `brain serve --new-client`. Those are
    the two mechanisms this server supports; the third, dynamic registration,
    is deprecated in the MCP specification and is not built (see the module
    docstring).
    """
    client_id = (client_id or "").strip()
    if not client_id:
        raise ClientRefused("unknown_client")
    if client_id.lower().startswith(("http://", "https://")):
        if fetch is None:
            raise ClientRefused("fetch_failed")
        return fetch(client_id)
    found = store.client(client_id) if store is not None else None
    if found is None:
        raise ClientRefused("unknown_client")
    return found


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY, name TEXT NOT NULL,
    redirect_uris TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY, client_id TEXT NOT NULL, resource TEXT NOT NULL,
    scope TEXT NOT NULL, created REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
    resource TEXT NOT NULL, scope TEXT NOT NULL, challenge TEXT NOT NULL,
    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0, grant_id TEXT);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY, grant_id TEXT NOT NULL, kind TEXT NOT NULL,
    expires REAL NOT NULL, used INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS tokens_by_grant ON tokens (grant_id);
"""


class Store:
    """Clients, codes, grants and tokens, in SQLite.

    Machine-local and per brain — `osbackend.state_dir` decides where, and it
    is outside the repository so it can never reach git.

    **Nothing is stored in the clear that could be replayed.** Codes and tokens
    are kept as SHA-256 digests, so a stolen database file yields nothing that
    opens this brain. That is also why the tokens are opaque rather than JWTs:
    a self-contained token cannot be revoked, and revocation is most of what
    this table is for.

    A connection is opened per operation rather than held. ThreadingHTTPServer
    runs handlers concurrently and a sqlite3 connection is not safe to share
    across threads; opening per call is the version of that which cannot be got
    wrong later by somebody adding a second caller.
    """

    def __init__(self, path, clock=None):
        self.path = Path(path)
        self._clock = clock or time.time
        self._prepare()

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _prepare(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        if fresh:
            # Created with the mode already set rather than chmod-ed after.
            # The window between the two is exactly when a backup job runs —
            # the same reasoning FileKeystore carries, and this file holds
            # every credential this authorization server has issued.
            os.close(os.open(str(self.path), os.O_CREAT | os.O_WRONLY, 0o600))
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema', ?)",
                         (str(SCHEMA_VERSION),))
            conn.commit()
        finally:
            conn.close()

    def schema_version(self) -> int:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0

    # ----------------------------------------------------------- clients

    def register_client(self, name: str, redirect_uris) -> str:
        import secrets
        client_id = secrets.token_urlsafe(24)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO clients (client_id, name, redirect_uris, created) "
                "VALUES (?, ?, ?, ?)",
                (client_id, name, json.dumps(list(redirect_uris)),
                 time.strftime("%Y-%m-%d", time.gmtime(self._clock()))))
            conn.commit()
        finally:
            conn.close()
        return client_id

    def client(self, client_id: str):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT client_id, name, redirect_uris FROM clients WHERE client_id = ?",
                (client_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Client(row[0], row[1], tuple(json.loads(row[2])), "preregistered")

    # ------------------------------------------------- codes, grants, tokens

    def create_grant(self, client_id: str, resource: str, scope: str) -> str:
        import secrets
        grant_id = secrets.token_urlsafe(18)
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO grants (grant_id, client_id, resource, scope, created) "
                "VALUES (?, ?, ?, ?, ?)",
                (grant_id, client_id, resource, scope, self._clock()))
            conn.commit()
        finally:
            conn.close()
        return grant_id

    def grant(self, grant_id: str):
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT grant_id, client_id, resource, scope, revoked "
                "FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"grant_id": row[0], "client_id": row[1], "resource": row[2],
                "scope": row[3], "revoked": bool(row[4])}

    def revoke_grant(self, grant_id: str) -> int:
        """Kill a grant and everything descended from it.

        One statement per table rather than a cascade, because "revoked" has to
        mean the access token stops working NOW — a foreign key would only stop
        new rows appearing.
        """
        conn = self._connect()
        try:
            conn.execute("UPDATE grants SET revoked = 1 WHERE grant_id = ?", (grant_id,))
            done = conn.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))
            conn.execute("UPDATE codes SET used = 1 WHERE grant_id = ?", (grant_id,))
            conn.commit()
            return done.rowcount
        finally:
            conn.close()

    def create_code(self, code: str, client_id: str, redirect_uri: str,
                    resource: str, scope: str, challenge: str, expires: float) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO codes (code_hash, client_id, redirect_uri, resource, "
                "scope, challenge, expires) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(code), client_id, redirect_uri, resource, scope,
                 challenge, expires))
            conn.commit()
        finally:
            conn.close()

    def take_code(self, code: str, now: float):
        """Consume a code, or None. Single use, enforced in ONE statement.

        The `used = 0` in the UPDATE's WHERE clause is what makes this atomic:
        two requests arriving with the same code race, and exactly one of them
        gets a rowcount of 1. Reading, checking, and then writing would let both
        through — and a replayed code is how an intercepted one gets used.

        A code that was already used is not simply refused: the caller is told
        so through `already_used`, because OAuth 2.1 requires the tokens issued
        from a replayed code to be revoked. Re-use means somebody else has it.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT client_id, redirect_uri, resource, scope, challenge, "
                "expires, used, grant_id FROM codes WHERE code_hash = ?",
                (digest(code),)).fetchone()
            if row is None:
                return None
            found = {"client_id": row[0], "redirect_uri": row[1], "resource": row[2],
                     "scope": row[3], "challenge": row[4], "expires": row[5],
                     "already_used": bool(row[6]), "grant_id": row[7]}
            if found["already_used"] or found["expires"] <= now:
                return found if found["already_used"] else None
            changed = conn.execute(
                "UPDATE codes SET used = 1 WHERE code_hash = ? AND used = 0",
                (digest(code),))
            conn.commit()
            if changed.rowcount != 1:
                found["already_used"] = True
            return found
        finally:
            conn.close()

    def attach_grant_to_code(self, code: str, grant_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("UPDATE codes SET grant_id = ? WHERE code_hash = ?",
                         (grant_id, digest(code)))
            conn.commit()
        finally:
            conn.close()

    def count_codes(self) -> int:
        conn = self._connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
        finally:
            conn.close()

    def only_code(self):
        """The single stored code, for tests that issued exactly one."""
        conn = self._connect()
        try:
            row = conn.execute("SELECT client_id, redirect_uri, resource, scope, "
                               "challenge, expires FROM codes").fetchone()
        finally:
            conn.close()
        return {"client_id": row[0], "redirect_uri": row[1], "resource": row[2],
                "scope": row[3], "challenge": row[4], "expires": row[5]}

    def prune(self, now: float) -> None:
        """Drop what can never be used again.

        Not housekeeping: a table of expired codes and dead tokens on a server
        that has been running for a year is a bigger disclosure than the same
        table with a week in it, and it grows without limit otherwise.
        """
        conn = self._connect()
        try:
            conn.execute("DELETE FROM codes WHERE expires < ?", (now - 3600,))
            conn.execute("DELETE FROM tokens WHERE expires < ?", (now - 3600,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The authorization server itself
# ---------------------------------------------------------------------------

# Short, because a code is a one-shot value redeemed by a machine within
# milliseconds of being issued. Anything longer is only ever a window for a
# stolen one.
CODE_TTL_SECONDS = 60.0
ACCESS_TTL_SECONDS = 3600.0
REFRESH_TTL_SECONDS = 30 * 86400.0


def digest(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64url_sha256(value: str) -> str:
    import base64
    import hashlib
    raw = hashlib.sha256(value.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class Outcome:
    """What the transport should do about a request.

    The authorization server decides; `serve.py` only carries it out. That
    split is what lets every rule below be tested without a socket, and it
    keeps the HTTP handler from acquiring opinions about OAuth.

    `credential_failed` is the one thing that travels back the other way: the
    rate limiter lives in the transport, and a wrong operator token at the
    consent screen has to count on the same table as a wrong bearer at /mcp.
    """

    def __init__(self, kind, status=200, body="", url="", headers=None,
                 credential_failed=False, event="", reason=""):
        self.kind = kind                  # "page" | "redirect"
        self.status = status
        self.body = body
        self.url = url
        self.headers = headers or {}
        self.credential_failed = credential_failed
        # What the transport should RECORD about this, as vocabulary values.
        # Carried rather than logged here because oauth.py holds no log: the
        # alternative was a refusal that produced only `request status=400`,
        # which tells an operator that something was refused and nothing about
        # why — found by running the server rather than by reading it.
        self.event = event
        self.reason = reason


class AuthError(Exception):
    """An authorization request that cannot proceed.

    `redirect` says whether the error may be sent BACK to the client. It is
    False for exactly two cases — an unresolvable client, and a redirect URI
    the client did not register — and that distinction is a security control,
    not a nicety: redirecting to an unvalidated URI is the open redirect the
    validation exists to prevent.
    """

    def __init__(self, code: str, description: str, redirect: bool = True,
                 reason: str = "bad_document"):
        Exception.__init__(self, code)
        self.code = code
        self.description = description
        self.redirect = redirect
        self.reason = reason


class AuthServer:
    """OAuth 2.1 authorization code flow with PKCE, over one brain.

    There is exactly ONE user — the person who owns the brain — and no accounts
    are invented for them. Consent is proved by pasting the value `brain serve
    --new-token` printed, compared in constant time. That credential already
    exists, is already 32 random bytes, and is already the thing that authorises
    this brain; inventing a second password would be one more secret to store,
    rotate and lose, and an unauthenticated consent screen is not a consent
    screen but a queue.
    """

    def __init__(self, config: Config, store: Store, fetch=None, clock=None,
                 code_ttl: float = CODE_TTL_SECONDS,
                 access_ttl: float = ACCESS_TTL_SECONDS,
                 refresh_ttl: float = REFRESH_TTL_SECONDS):
        self.config = config
        self.store = store
        self.fetch = fetch if fetch is not None else MetadataFetcher()
        self._clock = clock or time.time
        self.code_ttl = code_ttl
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl

    # ------------------------------------------------------------- /authorize

    def authorize(self, params: dict) -> Outcome:
        """Render the consent page, or refuse. Nothing is issued here."""
        try:
            request = self._validate(params)
        except AuthError as error:
            return self._error_outcome(error, params)
        return Outcome("page", 200, consent_page(self.config, request))

    def consent(self, params: dict, operator_token: str, verify) -> Outcome:
        """The form came back. Issue a code if the operator proved it is them.

        `params` is REVALIDATED from scratch. The hidden fields in the consent
        form are request parameters like any others and a form can be re-posted
        with any of them changed — trusting them because "we already checked"
        is how a validated redirect URI becomes an attacker's.
        """
        try:
            request = self._validate(params)
        except AuthError as error:
            return self._error_outcome(error, params)

        if not verify(operator_token or ""):
            # Reported back so the transport can count it: the consent screen
            # is one of only two places on this server where a credential can
            # be guessed over the network, and both back off on one table.
            return Outcome("page", 401,
                           consent_page(self.config, request,
                                        refused="That is not this brain's token."),
                           credential_failed=True)

        import secrets
        code = secrets.token_urlsafe(32)
        self.store.create_code(code, request["client"].client_id,
                               request["redirect_uri"], request["resource"],
                               " ".join(request["scope"]), request["challenge"],
                               self._clock() + self.code_ttl)
        self.store.prune(self._clock())
        return Outcome("redirect", 302, url=_with_query(
            request["redirect_uri"],
            {"code": code, "state": request["state"],
             # RFC 9207, and the metadata advertises that it is sent — so it
             # has to actually be sent, or a conforming client rejects the
             # whole response as a possible mix-up.
             "iss": self.config.issuer}))

    # -------------------------------------------------------------- internals

    def _validate(self, params: dict) -> dict:
        """Every rule an authorization request has to satisfy, in one place.

        Called by BOTH the page render and the consent POST, deliberately: two
        copies of this would drift, and the copy that drifted would be the one
        that issues credentials.
        """
        client_id = _one(params, "client_id")
        try:
            client = resolve_client(client_id, store=self.store, fetch=self.fetch)
        except ClientRefused as refused:
            raise AuthError("invalid_client",
                            "this client could not be identified",
                            redirect=False, reason=refused.reason)

        redirect_uri = _one(params, "redirect_uri")
        if not redirect_uri or not client.matches_redirect(redirect_uri):
            # NEVER redirected. Sending an error to a URI the client did not
            # register is the open redirect this check exists to prevent.
            raise AuthError("invalid_request",
                            "that redirect URI is not registered for this client",
                            redirect=False, reason="bad_redirect_uri")

        state = _one(params, "state")
        if _one(params, "response_type") != "code":
            raise AuthError("unsupported_response_type",
                            "this server issues authorization codes only",
                            reason="bad_response_type")

        challenge = _one(params, "code_challenge")
        method = _one(params, "code_challenge_method")
        if not challenge or method != "S256":
            # OAuth 2.1 requires PKCE and the metadata advertises S256 only. A
            # flow without it is one that an intercepted authorization code
            # completes on its own.
            raise AuthError("invalid_request",
                            "PKCE is required: code_challenge with "
                            "code_challenge_method=S256",
                            reason="bad_challenge")

        resource = _one(params, "resource") or self.config.resource
        if resource != self.config.resource:
            # RFC 8707. This server can only issue tokens for itself, so a
            # request for one bound elsewhere is a mistake at best and a
            # confused-deputy setup at worst.
            raise AuthError("invalid_target",
                            "this authorization server issues tokens only for "
                            + self.config.resource,
                            reason="bad_resource")

        asked = (_one(params, "scope") or " ".join(self.config.scopes)).split()
        grantable = set(self.config.scopes) | {SCOPE_OFFLINE}
        unknown = [s for s in asked if s not in grantable]
        if unknown:
            # Including a scope this PROCESS does not serve. A consent screen
            # that promised write on a --read-only server would be promising
            # something the dispatcher then refuses.
            raise AuthError("invalid_scope",
                            "this endpoint grants " + ", ".join(sorted(grantable)),
                            reason="bad_scope")
        return {"client": client, "redirect_uri": redirect_uri, "state": state,
                "challenge": challenge, "resource": resource,
                "scope": tuple(asked) or self.config.scopes,
                "params": params}

    def _error_outcome(self, error: AuthError, params: dict) -> Outcome:
        # `oauth_client_refused` when the client itself could not be trusted —
        # which is also every SSRF refusal, and the one an operator most needs
        # to be able to see. `oauth_error` for everything else.
        event = ("oauth_client_refused" if error.code == "invalid_client"
                 else "oauth_error")
        if not error.redirect:
            return Outcome("page", 400, error_page(error),
                           event=event, reason=error.reason)
        query = {"error": error.code, "error_description": error.description,
                 "iss": self.config.issuer}
        state = _one(params, "state")
        if state:
            query["state"] = state
        return Outcome("redirect", 302,
                       url=_with_query(_one(params, "redirect_uri"), query),
                       event=event, reason=error.reason)


def _one(params: dict, name: str) -> str:
    """One value for a parameter, from a parsed query or form.

    Takes the FIRST when a parameter is repeated rather than the last or a
    join. A repeated parameter is a classic way to make two parsers disagree
    about a request — the validator reading one `redirect_uri` and the issuer
    reading the other — so this file has exactly one rule and applies it
    everywhere.
    """
    value = params.get(name)
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return (value or "").strip() if isinstance(value, str) else ""


def _with_query(url: str, extra: dict) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = parts.query + ("&" if parts.query else "") + urlencode(
        {k: v for k, v in extra.items() if v})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


# ---------------------------------------------------------------------------
# The two pages
#
# Plain HTML, inline CSS, no scripts and nothing fetched from anywhere. A
# consent screen is the one page in this project a human reads in a browser,
# and it is the page they type a credential into — so it loads nothing it did
# not serve itself, and it renders nothing it did not escape.
# ---------------------------------------------------------------------------

# What each scope MEANS, in the words startup_notes already uses for the same
# capabilities. A consent screen that says "brain:write" has not told anybody
# anything they can make a decision about.
SCOPE_WORDS = {
    SCOPE_READ: "Read every note in this brain — decisions, projects, people, "
                "journal entries, everything except the encrypted vault.",
    SCOPE_WRITE: "Write new notes into this brain. They are committed and "
                 "pushed to your private remote automatically.",
    SCOPE_OFFLINE: "Stay connected without asking you again, until you revoke it.",
}

_STYLE = """
 body{font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
      max-width:34rem;margin:3rem auto;padding:0 1.25rem;color:#1a1a1a;background:#fff}
 h1{font-size:1.35rem;margin:0 0 .4rem}
 .who{font-size:1.05rem;margin:0 0 1.25rem;color:#444}
 .name{font-weight:600}
 ul{padding-left:1.1rem;margin:.5rem 0 1.25rem}
 li{margin:.35rem 0}
 .box{border:1px solid #d9d9d9;border-radius:8px;padding:.9rem 1.1rem;margin:1.25rem 0}
 .warn{border-color:#c98a00;background:#fff9ec}
 .bad{border-color:#c0392b;background:#fdf0ee}
 code{background:#f2f2f2;padding:.1rem .3rem;border-radius:3px;font-size:.9em}
 label{display:block;font-weight:600;margin:0 0 .35rem}
 input[type=password]{width:100%;padding:.55rem;font-size:1rem;
      border:1px solid #bbb;border-radius:6px;box-sizing:border-box}
 button{margin-top:.9rem;padding:.6rem 1.2rem;font-size:1rem;border:0;
      border-radius:6px;background:#1a1a1a;color:#fff;cursor:pointer}
 .foot{color:#666;font-size:.85rem;margin-top:2rem}
 @media (prefers-color-scheme:dark){
   body{background:#131313;color:#eee}.who{color:#bbb}
   .box{border-color:#3a3a3a}.warn{background:#2a2109;border-color:#7a5c00}
   .bad{background:#2c1512;border-color:#8c3226}
   code{background:#242424}input[type=password]{background:#1d1d1d;color:#eee;
      border-color:#444}button{background:#eee;color:#131313}.foot{color:#999}}
"""


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>{}</title><style>{}</style></head><body>{}</body></html>"
            ).format(title, _STYLE, body)


def consent_page(config: Config, request: dict, refused: str = "") -> str:
    """The one page a human reads. Three things on it are requirements.

    **Everything from the client's document is escaped.** `client_name` comes
    out of a document somebody else hosts, and it is rendered on the page the
    operator types their token into. Without escaping this is a cross-site
    scripting hole in a consent screen, which is about the worst place to have
    one.

    **The redirect URI's hostname is shown.** The MCP specification makes it a
    MUST, and it is the only thing on the page that says where the credential
    is actually going. A name is a claim; a hostname is a fact.

    **A loopback-only client gets a warning.** A Client ID Metadata Document
    cannot prevent loopback impersonation — any local process can bind a port
    and say it is the legitimate client — so the spec asks for one, and this
    says it in words rather than in a symbol.
    """
    import html
    client = request["client"]
    redirect = urlsplit(request["redirect_uri"])
    name = html.escape(client.name)
    host = html.escape(redirect.hostname or redirect.netloc or "unknown")

    granted = "".join(
        "<li>{}</li>".format(html.escape(SCOPE_WORDS.get(scope, scope)))
        for scope in request["scope"])

    warn = ""
    if all((urlsplit(uri).hostname or "") in LOOPBACK_HOSTS
           for uri in client.redirect_uris):
        warn = ("<div class=\"box warn\"><strong>This client runs on a local "
                "machine.</strong><br>It will be sent back to <code>{}</code>. "
                "Any program on that machine can claim to be it, so only "
                "continue if you started this yourself, just now.</div>"
                ).format(html.escape(request["redirect_uri"]))
    if client.kind == "preregistered":
        warn += ("<div class=\"box\">This is a client you registered yourself "
                 "with <code>brain serve --new-client</code>.</div>")

    problem = ('<div class="box bad">{}</div>'.format(html.escape(refused))
               if refused else "")

    hidden = "".join(
        '<input type="hidden" name="{}" value="{}">'.format(
            key, html.escape(value, quote=True))
        for key, value in (
            ("response_type", "code"),
            ("client_id", client.client_id),
            ("redirect_uri", request["redirect_uri"]),
            ("code_challenge", request["challenge"]),
            ("code_challenge_method", "S256"),
            ("scope", " ".join(request["scope"])),
            ("state", request["state"]),
            ("resource", request["resource"]),
        ) if value)

    body = """
<h1>Connect this brain?</h1>
<p class="who"><span class="name">{name}</span> is asking to connect to your
second brain, and will be sent back to <code>{host}</code>.</p>
<p>If you continue, it will be able to:</p>
<ul>{granted}</ul>
{warn}
{problem}
<form method="post" action="/authorize">
{hidden}
<div class="box">
<label for="t">Paste this brain's token to confirm it is you</label>
<input id="t" type="password" name="operator_token" autocomplete="off"
       autofocus spellcheck="false">
<button type="submit">Connect</button>
</div>
</form>
<p class="foot">This is the value <code>brain serve --new-token</code> printed.
There are no accounts here — the brain has one owner, and this is how it knows
you are them. Close this tab to refuse; nothing is issued until you submit.</p>
""".format(name=name, host=host, granted=granted, warn=warn, problem=problem,
           hidden=hidden)
    return _page("Connect this brain?", body)


def error_page(error: AuthError) -> str:
    """Shown instead of a redirect when the redirect itself cannot be trusted.

    Deliberately says little. The operator is not the audience for a client's
    misconfiguration and cannot fix it from here; the event log carries the
    class of failure, which is where somebody debugging should look.
    """
    import html
    body = """
<h1>This request was refused</h1>
<div class="box bad"><strong>{code}</strong><br>{description}</div>
<p class="foot">Nothing was issued and you have not been redirected — the
address this client asked to be returned to could not be verified, and sending
you there anyway is exactly the attack this check exists to stop.</p>
<p class="foot">The server recorded what happened.
Run <code>brain logs --errors</code> to see it.</p>
""".format(code=html.escape(error.code), description=html.escape(error.description))
    return _page("Refused", body)


# ---------------------------------------------------------------------------
# Token issuance, appended to AuthServer.
#
# Kept as functions bound onto the class below rather than inlined above only
# so this file reads in the order the flow happens: identity, consent, then
# credentials.
# ---------------------------------------------------------------------------

def _token_error(code: str, description: str = ""):
    """An RFC 6749 error, with the status the RFC gives it.

    Never a custom code and never a bare 500: a client that cannot parse the
    error cannot recover from it, and the common failure — a refresh token
    that is no longer valid — has to come back as `invalid_grant` or clients
    retry it forever instead of starting a new flow.
    """
    status = 401 if code == "invalid_client" else 400
    body = {"error": code}
    if description:
        body["error_description"] = description
    return status, body, {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _token_response(access: str, refresh: str, scope: str, expires_in: int):
    body = {"access_token": access, "token_type": "Bearer",
            "expires_in": expires_in, "scope": scope}
    if refresh:
        body["refresh_token"] = refresh
    # A token response must never be cached — by the client, by a proxy, or by
    # anything a tunnel puts in the path.
    return 200, body, {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _token(self, form: dict):
    """POST /token. `application/x-www-form-urlencoded`, per RFC 6749.

    Client authentication is `none`: every client here is a PUBLIC client, the
    metadata says so, and PKCE is what protects the exchange instead of a
    secret the client could not keep anyway. The `client_id` is still required
    and still checked against the grant — it is an identity claim, not a
    credential.
    """
    grant_type = _one(form, "grant_type")
    if not grant_type:
        return _token_error("invalid_request", "grant_type is required")
    if grant_type == "authorization_code":
        return self._exchange_code(form)
    if grant_type == "refresh_token":
        return self._refresh(form)
    return _token_error("unsupported_grant_type",
                        "this server supports authorization_code and refresh_token")


def _exchange_code(self, form: dict):
    import hmac
    code = _one(form, "code")
    if not code:
        return _token_error("invalid_request", "code is required")
    row = self.store.take_code(code, self._clock())
    if row is None:
        return _token_error("invalid_grant", "unknown or expired authorization code")
    if row["already_used"]:
        # OAuth 2.1: a replayed code means somebody else has a copy, so what it
        # already produced is not trustworthy either. Killing the grant is a
        # stronger response than refusing the second exchange, and it is the
        # right one — the legitimate client can start a new flow, and an
        # attacker holding the first token cannot.
        if row["grant_id"]:
            self.store.revoke_grant(row["grant_id"])
        return _token_error("invalid_grant", "this authorization code was already used")

    client_id = _one(form, "client_id")
    if client_id != row["client_id"]:
        return _token_error("invalid_grant", "this code was issued to another client")
    if _one(form, "redirect_uri") != row["redirect_uri"]:
        return _token_error("invalid_grant",
                            "redirect_uri does not match the authorization request")
    resource = _one(form, "resource")
    if resource and resource != row["resource"]:
        return _token_error("invalid_grant", "resource does not match the "
                                             "authorization request")

    verifier = _one(form, "code_verifier")
    if not verifier or not hmac.compare_digest(_b64url_sha256(verifier),
                                               row["challenge"]):
        # compare_digest rather than ==: the challenge is not quite a secret,
        # but a comparison that returns on the first wrong byte is a habit this
        # file does not want anywhere near it.
        return _token_error("invalid_grant", "code_verifier does not match the "
                                             "code_challenge")

    grant_id = self.store.create_grant(row["client_id"], row["resource"], row["scope"])
    self.store.attach_grant_to_code(code, grant_id)
    return self._issue(grant_id, row["scope"])


def _refresh(self, form: dict):
    presented = _one(form, "refresh_token")
    if not presented:
        return _token_error("invalid_request", "refresh_token is required")
    found = self.store.find_token(presented)
    if found is None or found["kind"] != "refresh":
        return _token_error("invalid_grant", "unknown refresh token")
    grant = self.store.grant(found["grant_id"])
    if grant is None or grant["revoked"]:
        return _token_error("invalid_grant", "this authorization was revoked")
    if found["used"]:
        # Rotation makes this detectable, and detecting it is the whole point:
        # a token that was already exchanged, presented again, means a copy
        # leaked. Revoking the FAMILY is what stops an attacker and the real
        # client taking turns refreshing each other's tokens indefinitely.
        self.store.revoke_grant(grant["grant_id"])
        return _token_error("invalid_grant", "this refresh token was already used")
    if found["expires"] <= self._clock():
        return _token_error("invalid_grant", "this refresh token has expired")
    client_id = _one(form, "client_id")
    if client_id and client_id != grant["client_id"]:
        return _token_error("invalid_grant", "this grant belongs to another client")

    self.store.spend_token(presented)
    # The scope comes off the GRANT, never off the form: a refresh request that
    # could widen its own scope would make the consent screen advisory.
    return self._issue(grant["grant_id"], grant["scope"])


def _issue(self, grant_id: str, scope: str):
    import secrets
    now = self._clock()
    access = secrets.token_urlsafe(32)
    self.store.create_token(access, grant_id, "access", now + self.access_ttl)
    # A refresh token is issued whether or not `offline_access` was asked for.
    # The specification leaves it to the authorization server, and the
    # alternative here is a person re-consenting in a browser every hour from a
    # phone, which is the same as the feature not existing. The risk that buys
    # is bounded by the three things around it: these rotate, reuse kills the
    # family, and `brain retire` deletes the lot.
    refresh = secrets.token_urlsafe(32)
    self.store.create_token(refresh, grant_id, "refresh", now + self.refresh_ttl)
    self.store.prune(now)
    return _token_response(access, refresh, scope, int(self.access_ttl))


def _revoke(self, form: dict):
    """RFC 7009. Always 200, even for a token that never existed.

    An answer that differed would be an oracle for whether a token exists,
    which is a way to confirm a guess without spending it. Revoking either
    token takes the whole grant: the two halves authorise the same access, so
    revoking one and leaving the other is a control that does not control
    anything.
    """
    presented = _one(form, "token")
    if presented:
        found = self.store.find_token(presented)
        if found is not None:
            self.store.revoke_grant(found["grant_id"])
    return 200, {}, {"Cache-Control": "no-store"}


def _validate_bearer(self, presented: str):
    """An access token to the grant behind it, or None.

    Four checks, and the AUDIENCE one is the least obvious and the most
    important: a token in this database that was issued for a different
    resource is refused anyway. That is what stops a token consented to for one
    brain opening another that happens to share a host — and it is the RFC 8707
    requirement that MCP servers validate the audience rather than assume it.
    """
    if not presented:
        return None
    found = self.store.find_token(presented)
    if found is None or found["kind"] != "access":
        return None
    if found["expires"] <= self._clock():
        return None
    grant = self.store.grant(found["grant_id"])
    if grant is None or grant["revoked"]:
        return None
    if grant["resource"] != self.config.resource:
        return None
    return grant


AuthServer.token = _token
AuthServer._exchange_code = _exchange_code
AuthServer._refresh = _refresh
AuthServer._issue = _issue
AuthServer.revoke = _revoke
AuthServer.validate_bearer = _validate_bearer


def _store_create_token(self, token: str, grant_id: str, kind: str,
                        expires: float) -> None:
    conn = self._connect()
    try:
        conn.execute("INSERT INTO tokens (token_hash, grant_id, kind, expires) "
                     "VALUES (?, ?, ?, ?)", (digest(token), grant_id, kind, expires))
        conn.commit()
    finally:
        conn.close()


def _store_find_token(self, presented: str):
    conn = self._connect()
    try:
        row = conn.execute(
            "SELECT grant_id, kind, expires, used FROM tokens WHERE token_hash = ?",
            (digest(presented),)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {"grant_id": row[0], "kind": row[1], "expires": row[2],
            "used": bool(row[3])}


def _store_spend_token(self, token: str) -> None:
    """Mark a refresh token rotated. The row STAYS, deliberately — deleting it
    would make a replay indistinguishable from an unknown token, and detecting
    the replay is the entire reason rotation is worth doing."""
    conn = self._connect()
    try:
        conn.execute("UPDATE tokens SET used = 1 WHERE token_hash = ?",
                     (digest(token),))
        conn.commit()
    finally:
        conn.close()


def _store_count_tokens(self) -> int:
    conn = self._connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM tokens t JOIN grants g ON g.grant_id = t.grant_id "
            "WHERE t.kind = 'access' AND g.revoked = 0").fetchone()[0]
    finally:
        conn.close()


Store.create_token = _store_create_token
Store.find_token = _store_find_token
Store.spend_token = _store_spend_token
Store.count_tokens = _store_count_tokens
