# bin/brainlib/access.py
"""Validating a Cloudflare Access assertion, which is the whole security boundary.

The remote brain sits behind Cloudflare Access. Access terminates the browser
OAuth flow, resolves the client's opaque token, and forwards a signed assertion
in `Cf-Access-Jwt-Assertion`. This module is the origin deciding, independently,
whether to believe it.

**The tunnel is connectivity, not proof of identity.** It is tempting to argue
that nothing can reach the origin except through Cloudflare, so validation is
belt-and-braces. Two things make that wrong, and they are worth stating because
somebody will propose removing this file to make a latency graph nicer:

1. The audience check is the ONLY mechanism separating the read-only endpoint
   from the one that can write. Both hostnames resolve through the same tunnel
   to the same process; nothing about the network path distinguishes them.
   Cloudflare's own metadata says there are no OAuth scopes to lean on, so
   `aud` is not a secondary check — it *is* the read/write boundary.
2. A tunnel is a piece of configuration. Configuration changes.

Two populations arrive here and they look different on the wire. A person who
logged in through the browser brings `email`; a headless client using an Access
service token brings `common_name` and an EMPTY `sub`, and carries no `email`
at all. Authorization written against `email` alone silently rejects every
headless request — or, if written the other way round, silently admits them.
Both shapes are handled explicitly and neither is inferred from the absence of
the other.

Everything fails closed. There is no path through this module that returns a
principal without a verified signature, a matching issuer, a matching audience
for the hostname the request arrived on, an unexpired token, and a recognised
identity.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request

from . import rs256

# Cloudflare publishes the signing keys here, on the TEAM domain, not on the
# application hostname. Observed 2026-08-23 on the owner's tenant: two RSA keys,
# both RS256, served with a four-hour cache lifetime and no Access in front of
# them. There is no /.well-known/jwks.json and no OIDC discovery worth doing —
# discovery would cost a round-trip and return this same URL.
CERTS_PATH = "/cdn-cgi/access/certs"

# Never trust a cached key set for longer than this, whatever Cache-Control
# says. Keys rotate every six weeks by default with a seven-day overlap, so an
# hour of staleness is invisible; a day of it would not be.
MAX_CACHE_SECONDS = 3600.0
MIN_CACHE_SECONDS = 60.0

# A token naming a key we have never seen is the normal, benign consequence of
# a rotation. Refetching immediately is right; refetching on EVERY such token is
# how an attacker turns a stream of garbage assertions into a denial-of-service
# against Cloudflare on our behalf.
FORCED_REFETCH_INTERVAL = 60.0

# Clocks disagree. This much and no more — the token lifetime is fifteen
# minutes, so a minute of slack is generous without being meaningful.
CLOCK_SKEW_SECONDS = 60.0

JWKS_TIMEOUT_SECONDS = 10.0
MAX_JWKS_BYTES = 256 * 1024
MAX_ASSERTION_BYTES = 8192


class AccessDenied(Exception):
    """This request is not authorized. The message is for the OPERATOR's log.

    It never reaches the client: a caller that leaks the reason tells an
    attacker which of issuer, audience, expiry or identity to fix next. The
    HTTP layer answers with a status and a correlation id, and the reason lands
    only in the event log's fixed vocabulary."""

    def __init__(self, reason: str, category: str = "invalid_assertion"):
        Exception.__init__(self, reason)
        self.category = category


class Endpoint(object):
    """One public hostname, its Access audience, and what it may do.

    Hostname and audience travel together and are checked together. Verifying
    the audience alone would let an assertion minted for the read-only
    application authorize a capture, because the process serving both is the
    same one."""

    __slots__ = ("hostname", "aud", "profile")

    def __init__(self, hostname: str, aud: str, profile: str):
        if not hostname or not aud or not profile:
            raise ValueError("an endpoint needs a hostname, an audience and a profile")
        self.hostname = hostname.strip().lower()
        self.aud = aud.strip()
        self.profile = profile


class Principal(object):
    """Who is making this request, in the only terms that may be written down.

    `stable_id` is an HMAC, not an email and not a client id. The spec's logging
    contract allows a stable principal identifier and forbids the raw address;
    an HMAC gives both at once, and because the key lives only in the
    deployment's secrets a leaked log cannot be reversed into an identity."""

    __slots__ = ("mode", "stable_id", "profile", "endpoint")

    def __init__(self, mode: str, stable_id: str, profile: str, endpoint: str):
        self.mode = mode                 # "interactive" | "service"
        self.stable_id = stable_id
        self.profile = profile
        self.endpoint = endpoint


class JwksCache(object):
    """The Access signing keys, fetched over HTTPS and cached with a bounded life.

    Fails closed. When the cache has expired and the fetch fails, this raises
    rather than serving the last-known keys forever: the spec's failure table
    says to continue until the cache validity boundary and then stop, and
    "forever" is not a boundary."""

    def __init__(self, team_domain: str, opener=None, clock=time.time):
        self.url = "https://%s%s" % (team_domain.strip().strip("/"), CERTS_PATH)
        self._opener = opener or _fetch
        self._clock = clock
        self._lock = threading.Lock()
        self._keys = {}                  # kid -> rs256.PublicKey
        self._expires_at = 0.0
        self._last_forced = 0.0

    def key_for(self, kid: str):
        """Return the key with this id, refetching once if it is unknown."""
        with self._lock:
            now = self._clock()
            if now >= self._expires_at:
                self._refresh(now)
            key = self._keys.get(kid)
            if key is not None:
                return key
            # Unknown kid on a still-valid cache: almost certainly a rotation
            # that happened inside the cache window. One rate-limited refetch,
            # then refuse.
            if now - self._last_forced >= FORCED_REFETCH_INTERVAL:
                self._last_forced = now
                self._refresh(now)
                key = self._keys.get(kid)
                if key is not None:
                    return key
        raise AccessDenied("no signing key with that id", "unknown_key")

    def _refresh(self, now: float) -> None:
        try:
            body, max_age = self._opener(self.url)
        except Exception as exc:
            # Only fatal once the cache has actually expired. Inside the window
            # a transient failure changes nothing, and saying so in the log is
            # more useful than failing a request that was going to succeed.
            if now >= self._expires_at:
                raise AccessDenied("signing keys unavailable: %r" % (exc,), "keys_unavailable")
            return
        try:
            document = json.loads(body.decode("utf-8"))
            entries = document["keys"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("empty key set")
        except Exception as exc:
            if now >= self._expires_at:
                raise AccessDenied("unreadable key set: %r" % (exc,), "keys_unavailable")
            return
        loaded = {}
        for entry in entries:
            try:
                key = rs256.PublicKey.from_jwk(entry)
            except rs256.BadSignature:
                # One unusable entry must not discard the others. A JWKS that
                # gains an EC key one day should degrade to "the RSA ones still
                # work", not to "nothing verifies".
                continue
            loaded[key.kid] = key
        if not loaded:
            if now >= self._expires_at:
                raise AccessDenied("key set contained no usable RS256 key", "keys_unavailable")
            return
        self._keys = loaded
        self._expires_at = now + max(MIN_CACHE_SECONDS, min(MAX_CACHE_SECONDS, max_age))


def _fetch(url: str):
    """Fetch a JWKS and return (body, max_age_seconds). Standard library only."""
    request = urllib.request.Request(url, headers={"User-Agent": "brain-http"})
    with urllib.request.urlopen(request, timeout=JWKS_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_JWKS_BYTES + 1)
        if len(body) > MAX_JWKS_BYTES:
            raise ValueError("key set larger than %d bytes" % MAX_JWKS_BYTES)
        return body, _max_age(response.headers.get("Cache-Control", ""))


def _max_age(header: str) -> float:
    for part in str(header).split(","):
        part = part.strip().lower()
        if part.startswith("max-age="):
            try:
                return float(part.split("=", 1)[1])
            except ValueError:
                return MIN_CACHE_SECONDS
    return MIN_CACHE_SECONDS


class Verifier(object):
    """The origin's independent check on one Access assertion."""

    def __init__(self, team_domain: str, endpoints, owner_email: str,
                 service_principals=(), principal_key=b"", jwks=None,
                 clock=time.time):
        team_domain = team_domain.strip().strip("/")
        if not team_domain or "/" in team_domain:
            raise ValueError("team domain must be a bare hostname")
        # The issuer is the TEAM domain, never the application hostname and
        # never derived from the request's Host header — deriving it from the
        # request would let the request choose who is allowed to have signed it.
        self.issuer = "https://%s" % team_domain
        self.endpoints = {}
        for endpoint in endpoints:
            self.endpoints[endpoint.hostname] = endpoint
        if not self.endpoints:
            raise ValueError("no endpoints configured")
        self.owner_email = _normalize_email(owner_email)
        if not self.owner_email:
            raise ValueError("an owner email is required")
        self.service_principals = {}
        for name, profile in dict(service_principals).items():
            self.service_principals[str(name).strip()] = profile
        if not principal_key:
            raise ValueError("a principal HMAC key is required")
        self.principal_key = principal_key
        self.jwks = jwks or JwksCache(team_domain, clock=clock)
        self._clock = clock

    def endpoint_for(self, host: str):
        """Resolve the Host header to a configured endpoint, or refuse.

        Any port is stripped; anything unrecognised is refused rather than
        defaulted. Defaulting here would mean a request arriving with an
        unexpected Host got SOME profile, and the safe-looking choice —
        read-only — would still be a profile nobody configured."""
        host = str(host or "").strip().lower()
        if not host:
            raise AccessDenied("no host", "unknown_host")
        if host.startswith("["):                      # IPv6 literal
            host = host.split("]", 1)[0] + "]"
        elif ":" in host:
            host = host.split(":", 1)[0]
        endpoint = self.endpoints.get(host)
        if endpoint is None:
            raise AccessDenied("no endpoint for that host", "unknown_host")
        return endpoint

    def verify(self, assertion, host: str) -> Principal:
        endpoint = self.endpoint_for(host)
        if not assertion:
            raise AccessDenied("no Access assertion", "no_assertion")
        if len(assertion) > MAX_ASSERTION_BYTES:
            raise AccessDenied("assertion larger than %d bytes" % MAX_ASSERTION_BYTES)
        try:
            signing_input, header_b64, payload_b64, signature = rs256.split_jws(assertion)
            header = _json_part(header_b64, "header")
            if header.get("alg") != "RS256":
                raise AccessDenied("algorithm %r is not RS256" % (header.get("alg"),))
            kid = header.get("kid")
            if not isinstance(kid, str) or not kid:
                raise AccessDenied("assertion names no key")
            key = self.jwks.key_for(kid)
            rs256.verify(key, signing_input, signature)
            claims = _json_part(payload_b64, "payload")
        except rs256.BadSignature as exc:
            raise AccessDenied("signature: %s" % (exc,))
        return self._authorize(claims, endpoint)

    def _authorize(self, claims: dict, endpoint: Endpoint) -> Principal:
        if claims.get("iss") != self.issuer:
            raise AccessDenied("issuer mismatch", "wrong_issuer")
        # `type` distinguishes an APPLICATION token from an ORG token. An org
        # token is a genuine signature by the same key over a different scope,
        # so omitting this check accepts a credential that was never issued for
        # this application at all.
        if claims.get("type") != "app":
            raise AccessDenied("token type %r is not an app token" % (claims.get("type"),))
        # `aud` is an ARRAY in an Access assertion, not a string.
        audiences = claims.get("aud")
        if isinstance(audiences, str):
            audiences = [audiences]
        if not isinstance(audiences, list) or endpoint.aud not in audiences:
            raise AccessDenied("audience does not match this endpoint", "wrong_audience")
        self._check_times(claims)
        return self._identify(claims, endpoint)

    def _check_times(self, claims: dict) -> None:
        now = self._clock()
        exp = _numeric(claims.get("exp"), "exp")
        if now > exp + CLOCK_SKEW_SECONDS:
            raise AccessDenied("assertion expired", "expired")
        # `nbf` is present on an identity assertion and absent on a service
        # one. Requiring it would 403 every headless request; ignoring it when
        # present would accept a token before it was valid.
        for name in ("nbf", "iat"):
            if name in claims:
                value = _numeric(claims.get(name), name)
                if value > now + CLOCK_SKEW_SECONDS:
                    raise AccessDenied("assertion %s is in the future" % name, "not_yet_valid")

    def _identify(self, claims: dict, endpoint: Endpoint) -> Principal:
        email = claims.get("email")
        common_name = claims.get("common_name")
        subject = claims.get("sub")
        # The discriminator, tested in full rather than inferred from one
        # missing field: a service assertion has a common_name, an EMPTY sub,
        # and no email. Anything that satisfies only part of that is not a
        # shape Access documents, and an unknown shape is refused.
        if common_name is not None or subject == "":
            if not isinstance(common_name, str) or not common_name.strip():
                raise AccessDenied("service assertion without a client id")
            if subject != "":
                raise AccessDenied("service assertion with a subject")
            if email:
                raise AccessDenied("service assertion carrying an email")
            name = common_name.strip()
            allowed = self.service_principals.get(name)
            if allowed is None:
                raise AccessDenied("unknown service client", "unknown_principal")
            if allowed != endpoint.profile:
                # A credential is authorized against exactly one profile. Access
                # policy should already have stopped this; the origin refusing
                # it too is what makes the boundary independent of that policy.
                raise AccessDenied("service client not authorized here", "wrong_profile")
            return Principal("service", self._stable_id("svc", name),
                             endpoint.profile, endpoint.hostname)
        normalized = _normalize_email(email)
        if not normalized:
            raise AccessDenied("assertion carries no identity")
        if not hmac.compare_digest(normalized, self.owner_email):
            raise AccessDenied("not the owner", "unknown_principal")
        return Principal("interactive", self._stable_id("usr", normalized),
                         endpoint.profile, endpoint.hostname)

    def _stable_id(self, kind: str, value: str) -> str:
        digest = hmac.new(self.principal_key,
                          ("%s:%s" % (kind, value)).encode("utf-8"),
                          hashlib.sha256).hexdigest()
        return "%s_%s" % (kind, digest[:16])


def _json_part(part_b64: bytes, what: str) -> dict:
    try:
        parsed = json.loads(rs256.b64url_decode(part_b64).decode("utf-8"))
    except rs256.BadSignature:
        raise
    except Exception:
        raise AccessDenied("unreadable %s" % what)
    if not isinstance(parsed, dict):
        raise AccessDenied("%s is not an object" % what)
    return parsed


def _numeric(value, what: str) -> float:
    # bool is an int subclass and `True` would otherwise read as the epoch.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccessDenied("claim %s is not a number" % what)
    return float(value)


def _normalize_email(value) -> str:
    """Lower-case and trim, and nothing cleverer.

    No Unicode folding, no plus-address stripping, no dot removal: every one of
    those makes two different addresses compare equal, and this comparison is
    the entire identity check."""
    if not isinstance(value, str):
        return ""
    return value.strip().lower()
