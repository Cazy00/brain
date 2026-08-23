#!/usr/bin/env python3
"""Make one Cloudflare account match the brain's declared edge configuration.

The brain's entire public surface is Cloudflare: two hostnames, one tunnel that
carries them to a container that publishes no port, and two Access applications
that decide who may speak to it. None of the *values* live in this repository —
the account id, the zone, the audiences and the owner's address are private and
belong in `/etc/brain/`. What lives here is the SHAPE of that configuration and
the code that makes an account match it, so the edge can be rebuilt from a
private state file after a lost machine, a fat-fingered dashboard edit, or a
migration to another account.

Why a script and not Terraform. The design allows exactly this: declarative
infrastructure where a provider supports the required fields, "a small
idempotent API script" where it does not. `oauth_configuration` — Managed
OAuth, dynamic client registration, the two grant lifetimes — is that gap, and
it is precisely the part that must not be clicked in by hand, because it is the
part that decides how a browser login is issued and how long it lasts.

Three properties, each of which could have gone the other way:

**Dry run is the default.** `--apply` is the only thing that mutates. The
alternative — apply by default with a `--dry-run` escape — is the same script
with the failure mode reversed: the mistake is then made *before* you see the
diff rather than after. This account also carries the owner's other hostnames,
so the cost of a wrong `--state` file pointed at the wrong account is not
theoretical.

**It never deletes.** Nothing here removes a DNS record, an application, a
policy, a tunnel or a service token. Dropping a hostname from the state file
does not unpublish it; the drift is REPORTED and a human decides. A reconciler
that prunes to match its input is one bad state file away from taking down the
five dev hostnames that share this zone on a completely different tunnel
[verified 2026-08-23], and "it was declarative" is not a comfort at that point.

**It holds no secret and mints none.** The state file is refused outright if it
contains anything shaped like a credential (`_scan_for_secrets`). The one
credential this system needs that Cloudflare will only ever show once — an
Access service token's client secret — is deliberately NOT created here. See
`step_service_tokens` for the reasoning; it is the sharpest trade-off in the
file.

The reconciliation order is a safety property, not a convenience:

    tunnel -> tunnel ingress -> Access applications -> Access policies
           -> service tokens -> DNS records

DNS is LAST because DNS is the step that completes the reachable path. Access
takes roughly 85 seconds to begin enforcing on a newly created application
[verified 2026-08-23]; an application created *after* its hostname already
resolved would spend that window fronting an unauthenticated origin. Creating
the application first spends that window on a name that does not resolve yet.
The same reasoning puts policies immediately after applications: an Access
application with no policy fails CLOSED [verified 2026-08-23], so the ordering
is never briefly permissive, only briefly unreachable.

Everything the API does here is either a read or an idempotent write, and a
second `--apply` against a converged account reports zero changes. A reconciler
that cannot prove it is a no-op is a reconciler nobody dares run, so it is
proven rather than asserted: `tests/test_provision.py` runs it twice against a
fake account that records every request and counts the writes the second run
made. Counting is the only proof available — a script that PUT every object
back unchanged would print exactly the same summary line.

Usage:

    python3 deploy/cloudflare/provision.py --state /etc/brain/cloudflare.json
    python3 deploy/cloudflare/provision.py --state /etc/brain/cloudflare.json --apply

The token is read from the environment (`--token-env`, default
`CLOUDFLARE_API_TOKEN`) and never from an argument: a command-line token lands
in shell history and in `ps` output for every user on the host.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_BASE = "https://api.cloudflare.com/client/v4"
HTTP_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 50

# The six steps, in the order they must run. The list is the source of truth for
# the progress banner AND for the "which step failed" message the exit contract
# owes the operator, so there is exactly one place to change if a step is added.
STEPS = (
    "tunnel",
    "tunnel ingress",
    "access applications",
    "access policies",
    "service tokens",
    "notifications",
    "dns records",
)

EXIT_OK = 0
EXIT_FAILED = 1          # a reconciliation step failed outright
EXIT_BLOCKED = 2         # everything the script may do is done; a human owes an action
EXIT_REFUSED = 3         # refused to start: bad state file, missing token, secret found
EXIT_DRIFT = 4           # --check only: the account no longer matches the declared state

PROFILES = ("read", "capture")

# Cloudflare's four tunnel statuses [docs 2026-08-23]. Alerting on the three
# that are not "healthy" is the whole point; "healthy" is included in the set
# only so that a state file may ask for the recovery notification too.
TUNNEL_STATUSES = ("healthy", "degraded", "down", "inactive")
DEFAULT_TUNNEL_STATUSES = ("degraded", "down", "inactive")

# Cloudflare returns the two grant lifetimes as Go duration strings ("15m",
# "336h") and rejects anything else. Validating the shape here turns a silent
# API 400 in the middle of an apply into a refusal before the first request.
DURATION_RE = re.compile(r"^[0-9]+(ns|us|ms|s|m|h)$")

# The same units, in seconds, so a declared duration can be COMPARED and not
# only shape-checked. The two tables are read together: a unit admitted by one
# and missing from the other is a bug, which is why they sit side by side.
DURATION_SECONDS = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0, "m": 60.0, "h": 3600.0}

# cloudflared requires the LAST ingress rule to carry no hostname. This script
# appends that rule itself; the service is named here because validate_state
# must also RECOGNISE one an operator copied in from the connector's own
# config.yml, and because a rule with no hostname fronts nothing and must never
# be counted as a published route.
CATCH_ALL_SERVICE = "http_status:404"

# How far a live service token's minted lifetime may drift from the declared
# duration before it is reported. Cloudflare computes expires_at exactly, so an
# hour is generous: it covers clock skew and the seconds between "create" and
# "created", not a token issued with a different duration.
LIFETIME_TOLERANCE = timedelta(hours=1)

# An unfilled example. The example state file ships with these on purpose, and a
# run against it must stop with an explanation rather than send "<ACCOUNT_ID>"
# to the API and report a confusing 400.
PLACEHOLDER_RE = re.compile(r"<[A-Z0-9_]+>")


class Refusal(Exception):
    """The run must not start. Exit 3, before a single API request is made."""


class StepFailed(Exception):
    """A reconciliation step failed. Carries the step name for the exit message."""

    def __init__(self, step: str, message: str):
        Exception.__init__(self, message)
        self.step = step


class ApiError(Exception):
    """Cloudflare said no. The message is the API's own, never a request body."""


# ---------------------------------------------------------------------------
# Secret and placeholder scanning
# ---------------------------------------------------------------------------

# Key names that must never carry a value in a state file. This is a tripwire,
# not a proof: it exists to catch the operator who pastes a tunnel token or a
# service-token secret into the file "just to get it working", because that file
# is the one artefact of this deployment most likely to be copied into a chat
# window or a ticket while debugging.
SECRET_KEY_HINTS = ("secret", "password", "passwd", "credential", "private", "apikey", "api_key", "token")

# Keys whose NAME trips the hints above but which are structural. `token_id` is
# a public identifier, `*_file` names a path, and the OAuth lifetime keys simply
# contain the word "token".
SECRET_KEY_ALLOW = frozenset((
    "token_id", "service_tokens", "access_token_lifetime", "service_auth_401_redirect",
))

# Keys whose values are legitimately long opaque hex identifiers. An Access
# audience is 64 hex characters and so is a service-token client secret, so the
# value alone cannot tell them apart — the key name has to.
IDENTIFIER_KEYS = frozenset(("account_id", "zone_id", "id", "tunnel_id", "token_id", "aud", "record_id"))

OPAQUE_BLOB_RE = re.compile(r"^[A-Za-z0-9+/=_-]{40,}$")
JWT_SHAPED_RE = re.compile(r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$")


def _walk_strings(node, path="", key=""):
    """Yield (json_path, key, value) for every string in the document.

    A list inherits the key of the object field that holds it, so
    `{"tunnel_token": ["..."]}` is judged by the name `tunnel_token` and not by
    the empty name of an array element. Wrapping a secret in a list is the
    obvious way past a key-name check, and it should not work."""
    if isinstance(node, dict):
        for child_key, value in node.items():
            here = "%s.%s" % (path, child_key) if path else str(child_key)
            if isinstance(value, str):
                yield here, str(child_key), value
            else:
                for item in _walk_strings(value, here, str(child_key)):
                    yield item
    elif isinstance(node, list):
        for position, value in enumerate(node):
            here = "%s[%d]" % (path, position)
            if isinstance(value, str):
                yield here, key, value
            else:
                for item in _walk_strings(value, here, key):
                    yield item


def _looks_like_path_or_url(value: str) -> bool:
    """A filesystem path or a URL is long and opaque-looking and is neither.

    `/etc/brain/service-tokens/brain-capture-headless` is 48 characters of
    exactly the alphabet base64 uses, so without this the blob heuristic would
    reject every secret_file in the state — and an operator who learns to work
    around the scanner has been taught the wrong lesson."""
    return (value.startswith("/") or value.startswith("~")
            or "://" in value or " " in value)


def _scan_for_secrets(document) -> None:
    """Refuse a state file that carries anything shaped like a credential."""
    for path, key, value in _walk_strings(document):
        lowered = key.lower()
        # A `_`-prefixed key is documentation and is stripped before anything
        # reads it, so its NAME says nothing about its content. Its VALUE is
        # still scanned below: a secret pasted into a note is still a secret.
        documentation = lowered.startswith("_")
        if not documentation and lowered not in SECRET_KEY_ALLOW and not lowered.endswith("_file"):
            for hint in SECRET_KEY_HINTS:
                if hint in lowered and value.strip():
                    raise Refusal(
                        "%s looks like it holds a credential. Secrets belong in "
                        "/etc/brain/, never in the desired state." % path)
        if "PRIVATE KEY" in value:
            raise Refusal("%s contains a private key block." % path)
        if JWT_SHAPED_RE.match(value):
            raise Refusal("%s looks like a signed token." % path)
        if (lowered not in IDENTIFIER_KEYS and not _looks_like_path_or_url(value)
                and OPAQUE_BLOB_RE.match(value)):
            raise Refusal(
                "%s is a long opaque value in a key that should not hold one. "
                "If it is genuinely configuration, name the key so it is "
                "obviously not a secret; if it is a secret, it belongs in "
                "/etc/brain/." % path)


def _strip_documentation(node):
    """Drop keys beginning with `_`, which exist because JSON has no comments.

    Stripped AFTER the secret scan and BEFORE the placeholder check, so a note
    is still searched for credentials but is allowed to spell out the
    placeholders it is explaining."""
    if isinstance(node, dict):
        return dict((k, _strip_documentation(v)) for k, v in node.items()
                    if not str(k).startswith("_"))
    if isinstance(node, list):
        return [_strip_documentation(v) for v in node]
    return node


def _reject_placeholders(document) -> None:
    for path, _key, value in _walk_strings(document):
        match = PLACEHOLDER_RE.search(value)
        if match:
            raise Refusal(
                "%s still contains the placeholder %s. desired-state.example.json "
                "is a shape, not a configuration: copy it somewhere private "
                "(/etc/brain/cloudflare.json) and fill it in." % (path, match.group(0)))


# ---------------------------------------------------------------------------
# State loading and validation
# ---------------------------------------------------------------------------

def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError) as error:
        raise Refusal("cannot read desired state %s: %s" % (path, error))
    if not isinstance(raw, dict):
        raise Refusal("desired state must be a JSON object")
    _scan_for_secrets(raw)
    state = _strip_documentation(raw)
    _reject_placeholders(state)
    validate_state(state)
    return state


def _require(node: dict, key: str, kind, where: str):
    if key not in node:
        raise Refusal("%s is missing %r" % (where, key))
    value = node[key]
    if not isinstance(value, kind) or (kind is str and not value.strip()):
        raise Refusal("%s.%s has the wrong type" % (where, key))
    return value


def _check_duration(value: str, where: str) -> None:
    if not DURATION_RE.match(value):
        raise Refusal("%s must be a Go duration such as \"15m\" or \"336h\", not %r" % (where, value))


def validate_state(state: dict) -> None:
    """Refuse anything the API would accept but the design forbids.

    Everything checked here is a rule that Cloudflare itself will happily let
    you break. The API is content to merge two hostnames into one application,
    or to admit every service token in the account; this system is not."""
    _require(state, "account_id", str, "state")
    _require(state, "zone_id", str, "state")
    _require(state, "team_domain", str, "state")
    owner_email = _require(state, "owner_email", str, "state").strip().lower()

    unknown = sorted(set(state) - {"account_id", "zone_id", "team_domain", "owner_email",
                                   "tunnel", "applications", "service_tokens",
                                   "notifications", "dns"})
    if unknown:
        raise Refusal("state has unknown keys: %s" % ", ".join(unknown))

    tunnel = _require(state, "tunnel", dict, "state")
    unknown = sorted(set(tunnel) - {"name", "id", "config_src", "create_if_missing",
                                    "credentials_file", "local_config", "prune_ingress",
                                    "ingress"})
    if unknown:
        raise Refusal("tunnel has unknown keys: %s" % ", ".join(unknown))
    _require(tunnel, "name", str, "tunnel")
    config_src = tunnel.get("config_src", "local")
    if config_src not in ("local", "cloudflare"):
        raise Refusal("tunnel.config_src must be \"local\" or \"cloudflare\"")
    ingress = _require(tunnel, "ingress", list, "tunnel")
    if not ingress:
        raise Refusal("tunnel.ingress is empty; there would be nothing to route")
    hostnames = []
    for position, rule in enumerate(ingress):
        where = "tunnel.ingress[%d]" % position
        if not isinstance(rule, dict):
            raise Refusal("%s is not an object" % where)
        service = _require(rule, "service", str, where)
        if "hostname" not in rule:
            # The catch-all. cloudflared refuses a configuration whose final
            # rule has a hostname, so an operator copying the connector's own
            # config.yml into the state file brings one along; refusing it as
            # "missing hostname" would teach them to delete the one line they
            # were right about. It is accepted and then ignored — step_ingress
            # appends its own — and it is deliberately NOT added to `hostnames`,
            # because it publishes nothing, so the check below must not go
            # looking for an Access application to put in front of it.
            if service != CATCH_ALL_SERVICE:
                raise Refusal("%s has no hostname, so it is the catch-all and "
                              "its service must be %r, not %r"
                              % (where, CATCH_ALL_SERVICE, service))
            if position != len(ingress) - 1:
                raise Refusal("%s is the catch-all and must be the LAST rule; "
                              "cloudflared matches in order, so every rule "
                              "after it is dead" % where)
            continue
        hostname = _require(rule, "hostname", str, where).strip().lower()
        if hostname in hostnames:
            raise Refusal("%s repeats hostname %s" % (where, hostname))
        hostnames.append(hostname)
    if not hostnames:
        raise Refusal("tunnel.ingress declares no hostname; there would be "
                      "nothing to route")

    applications = _require(state, "applications", list, "state")
    if not applications:
        raise Refusal("state.applications is empty")
    seen_hosts = set()
    for position, app in enumerate(applications):
        where = "applications[%d]" % position
        if not isinstance(app, dict):
            raise Refusal("%s is not an object" % where)
        # Refuse an unknown key rather than ignore it. A misspelt
        # `service_auth_401_redirekt` that is silently dropped leaves an
        # operator certain they configured something they did not.
        unknown = sorted(set(app) - {"name", "hostname", "profile", "session_duration",
                                     "service_auth_401_redirect", "oauth_configuration",
                                     "policies", "extra"})
        if unknown:
            raise Refusal("%s has unknown keys: %s" % (where, ", ".join(unknown)))
        _require(app, "name", str, where)
        hostname = _require(app, "hostname", str, where).strip().lower()
        if hostname in seen_hosts:
            # Two applications on one hostname is a configuration Cloudflare
            # will accept and then resolve unpredictably.
            raise Refusal("%s: %s already has an application" % (where, hostname))
        seen_hosts.add(hostname)
        if hostname not in hostnames:
            raise Refusal("%s: %s is not in tunnel.ingress, so it would never "
                          "reach the origin" % (where, hostname))
        _require(app, "profile", str, where)
        profile = app["profile"]
        if profile not in PROFILES:
            raise Refusal("%s.profile must be one of %s" % (where, ", ".join(PROFILES)))
        session = _require(app, "session_duration", str, where)
        _check_duration(session, "%s.session_duration" % where)
        oauth = _require(app, "oauth_configuration", dict, where)
        _validate_oauth(oauth, "%s.oauth_configuration" % where)
        grant_session = oauth["grant"]["session_duration"]
        if grant_session != session:
            # The refresh grant and the Access identity session are two separate
            # clocks and the SHORTER one wins. A grant that outlives the session
            # produces a silent interactive re-login the client cannot explain,
            # which is the failure mode that cost a day on 2026-08-23. Making
            # them differ is therefore a refusal, not a warning.
            raise Refusal(
                "%s.session_duration (%s) must equal oauth_configuration.grant."
                "session_duration (%s): the shorter clock wins and a refresh "
                "that outlives the identity session forces a silent re-login"
                % (where, session, grant_session))
        _validate_policies(app.get("policies"), owner_email, profile, where)

    # The other direction, and the one that matters more. The loop above proves
    # every application has a route; this proves every route has an
    # application. Without it, adding one line to tunnel.ingress is enough to
    # publish a hostname the tunnel carries to the origin and DNS gives a
    # proxied public CNAME — with NOTHING in front of it, because Access only
    # protects hostnames that have an application. That is precisely the
    # exposure the whole tunnel -> ingress -> Access -> DNS ordering exists to
    # prevent, so it is a refusal and not a warning: a warning would be printed
    # once, above the line that says the record was created.
    #
    # The catch-all cannot trip this — it carries no hostname and so was never
    # added to `hostnames` above.
    unfronted = [host for host in hostnames if host not in seen_hosts]
    if unfronted:
        raise Refusal(
            "tunnel.ingress routes %s with no Access application in front of "
            "it. The tunnel would carry it to the origin and the DNS step would "
            "publish it, leaving it reachable with no authentication at all. "
            "Either declare an application for it, or remove it from "
            "tunnel.ingress." % ", ".join(unfronted))

    dns = state.get("dns") or {}
    if not isinstance(dns, dict):
        raise Refusal("state.dns must be an object")
    unknown = sorted(set(dns) - {"comment"})
    if unknown:
        # Nothing else about these records is configurable on purpose: type,
        # content, proxied and ttl are all determined by the fact that this is a
        # tunnel hostname, and making any of them an option invites turning off
        # the proxy, which would take the request out of Access entirely.
        raise Refusal("state.dns has unknown keys: %s" % ", ".join(unknown))

    for position, token in enumerate(state.get("service_tokens", []) or []):
        where = "service_tokens[%d]" % position
        if not isinstance(token, dict):
            raise Refusal("%s is not an object" % where)
        unknown = sorted(set(token) - {"name", "profile", "duration", "secret_file",
                                       "expiry_warning_days"})
        if unknown:
            raise Refusal("%s has unknown keys: %s" % (where, ", ".join(unknown)))
        _require(token, "name", str, where)
        if "duration" in token:
            # Checked here so the lifetime comparison in step_service_tokens is
            # never silently skipped: an unreadable duration would make that
            # check pass by doing nothing, which is the failure mode this file
            # is trying to stop having.
            _check_duration(_require(token, "duration", str, where),
                            "%s.duration" % where)
        if "expiry_warning_days" in token:
            # Same reasoning as duration above, and the same failure mode:
            # step_service_tokens does int(spec.get("expiry_warning_days", 7)),
            # so a string here raises ValueError deep inside the reconcile and
            # a float silently truncates the window the operator thought they
            # set. The seven-day alert is the only warning before a headless
            # client stops working; it must not be configurable into nothing.
            days = token["expiry_warning_days"]
            if isinstance(days, bool) or not isinstance(days, int) or days < 1:
                raise Refusal("%s.expiry_warning_days must be a positive whole "
                              "number of days, not %r" % (where, days))
        token_profile = _require(token, "profile", str, where)
        if token_profile not in PROFILES:
            raise Refusal("%s.profile must be one of %s" % (where, ", ".join(PROFILES)))

    _validate_notifications(state.get("notifications", []) or [], owner_email)


def _validate_notifications(declared, owner_email: str) -> None:
    """Alerting nobody reads is worse than none, so two things are enforced.

    The owner has to be a recipient. Cloudflare will happily accept a policy
    addressed only to an alias, and an alias is a thing that gets forwarded,
    filtered and eventually abandoned; the person who owns the brain is then
    the last to know it is down. Adding other recipients is fine — removing
    the owner is not.

    And `tunnel_id` may not be written here. It is per-account, so a state file
    carrying one cannot rebuild this edge in a different account, which is the
    one job the file exists for; `_desired_notification` injects the id of the
    tunnel it just reconciled instead."""
    seen = set()
    for position, policy in enumerate(declared):
        where = "notifications[%d]" % position
        if not isinstance(policy, dict):
            raise Refusal("%s is not an object" % where)
        unknown = sorted(set(policy) - {"name", "alert_type", "email", "enabled",
                                        "description", "filters"})
        if unknown:
            raise Refusal("%s has unknown keys: %s" % (where, ", ".join(unknown)))
        name = _require(policy, "name", str, where)
        if name in seen:
            # Policies are matched by name on every run. Two with one name and
            # the second would overwrite the first every time, forever, and the
            # run would still report "converged".
            raise Refusal("%s duplicates the name %r" % (where, name))
        seen.add(name)
        _require(policy, "alert_type", str, where)
        if "enabled" in policy and not isinstance(policy["enabled"], bool):
            raise Refusal("%s.enabled must be true or false" % where)
        recipients = _require(policy, "email", list, where)
        if not recipients:
            raise Refusal("%s.email is empty; the policy would notify nobody" % where)
        for address in recipients:
            if not isinstance(address, str) or "@" not in address:
                raise Refusal("%s.email contains %r, which is not an address"
                              % (where, address))
        if owner_email not in [str(a).strip().lower() for a in recipients]:
            raise Refusal("%s.email does not include the owner (state.owner_email). "
                          "Add other recipients freely; the owner is not optional."
                          % where)
        filters = policy.get("filters")
        if filters is not None:
            if not isinstance(filters, dict):
                raise Refusal("%s.filters must be an object" % where)
            if "tunnel_id" in filters:
                raise Refusal("%s.filters must not set tunnel_id — it is per-account "
                              "and is filled in from the reconciled tunnel" % where)
            for key, values in filters.items():
                if not isinstance(values, list) or not values:
                    raise Refusal("%s.filters.%s must be a non-empty list" % (where, key))
                if any(not isinstance(v, str) or not v.strip() for v in values):
                    raise Refusal("%s.filters.%s must hold strings" % (where, key))
            for status in filters.get("new_status", []):
                if status not in TUNNEL_STATUSES:
                    raise Refusal("%s.filters.new_status has %r; Cloudflare's tunnel "
                                  "statuses are %s" % (where, status,
                                                       ", ".join(TUNNEL_STATUSES)))


def _validate_oauth(oauth: dict, where: str) -> None:
    """Managed OAuth has exactly three keys and no others [docs 2026-08-23].

    Refusing an unknown key rather than passing it through is deliberate: a
    misspelling in this object is not rejected by the API, it is IGNORED, and
    the operator is left believing dynamic client registration is on when it is
    not."""
    unknown = sorted(set(oauth) - {"enabled", "dynamic_client_registration", "grant"})
    if unknown:
        raise Refusal("%s has unknown keys: %s" % (where, ", ".join(unknown)))
    if oauth.get("enabled") is not True:
        raise Refusal("%s.enabled must be true; the brain has no other login path" % where)

    dcr = _require(oauth, "dynamic_client_registration", dict, where)
    dcr_unknown = sorted(set(dcr) - {"enabled", "allowed_uris", "allow_any_on_localhost",
                                     "allow_any_on_loopback"})
    if dcr_unknown:
        raise Refusal("%s.dynamic_client_registration has unknown keys: %s"
                      % (where, ", ".join(dcr_unknown)))
    if not isinstance(dcr.get("allowed_uris", []), list):
        raise Refusal("%s.dynamic_client_registration.allowed_uris must be a list" % where)

    grant = _require(oauth, "grant", dict, where)
    grant_unknown = sorted(set(grant) - {"access_token_lifetime", "session_duration"})
    if grant_unknown:
        raise Refusal("%s.grant has unknown keys: %s" % (where, ", ".join(grant_unknown)))
    _check_duration(_require(grant, "access_token_lifetime", str, where),
                    "%s.grant.access_token_lifetime" % where)
    _check_duration(_require(grant, "session_duration", str, where),
                    "%s.grant.session_duration" % where)


def _validate_policies(policies, owner_email: str, profile: str, where: str) -> None:
    if not isinstance(policies, list) or not policies:
        # An application with no policies fails closed [verified 2026-08-23], so
        # this is not a security hole — it is an outage waiting for the first
        # login attempt, and it is better found here.
        raise Refusal("%s.policies is empty; the application would deny everyone" % where)
    for position, policy in enumerate(policies):
        pwhere = "%s.policies[%d]" % (where, position)
        if not isinstance(policy, dict):
            raise Refusal("%s is not an object" % pwhere)
        _require(policy, "name", str, pwhere)
        decision = _require(policy, "decision", str, pwhere)
        if decision not in ("allow", "non_identity"):
            raise Refusal("%s.decision must be \"allow\" or \"non_identity\"" % pwhere)
        if "precedence" in policy:
            # Precedence ORDERS the policies, and order decides which one
            # matches first. step_policies does int(spec.get("precedence", ...)),
            # so a string raises deep inside the reconcile and a float
            # truncates — quietly moving a Service Auth policy above or below
            # the identity Allow it was meant to sit beside.
            order = policy["precedence"]
            if isinstance(order, bool) or not isinstance(order, int) or order < 1:
                raise Refusal("%s.precedence must be a positive whole number, "
                              "not %r" % (pwhere, order))
        includes = _require(policy, "include", list, pwhere)
        if not includes:
            raise Refusal("%s.include is empty" % pwhere)
        for rule in includes:
            _validate_include(rule, owner_email, decision, profile, pwhere)


def _validate_include(rule, owner_email: str, decision: str, profile: str, where: str) -> None:
    if not isinstance(rule, dict) or len(rule) != 1:
        raise Refusal("%s: each include rule is a single-key object" % where)
    kind = list(rule)[0]
    if kind == "any_valid_service_token":
        # This is the whole reason include rules are validated at all. It reads
        # like "service tokens are allowed here"; it means "every service token
        # in this entire Cloudflare account is allowed here", including tokens
        # minted years from now for something unrelated. A Service Auth policy
        # names ONE token.
        raise Refusal(
            "%s uses any_valid_service_token, which admits every service token "
            "in the account. Name the one token instead: "
            "{\"service_token\": {\"name\": \"...\"}}" % where)
    if kind == "email":
        address = (rule[kind] or {}).get("email", "")
        if str(address).strip().lower() != owner_email:
            raise Refusal("%s allows %r, which is not the configured owner_email"
                          % (where, address))
        if decision != "allow":
            raise Refusal("%s: an email include belongs on an allow policy" % where)
    elif kind == "service_token":
        if decision != "non_identity":
            # A service token on an `allow` policy is checked as an identity and
            # will never match; the headless client gets a login page it cannot
            # complete, which looks exactly like a broken origin.
            raise Refusal("%s: a service token include requires decision "
                          "\"non_identity\" (Service Auth)" % where)
        named = (rule[kind] or {}).get("name", "")
        if not str(named).strip():
            raise Refusal("%s: name the service token; ids are per-account and "
                          "are resolved at run time" % where)
    else:
        raise Refusal("%s: unsupported include rule %r. This deployment allows "
                      "exactly one owner identity and named service tokens."
                      % (where, kind))


# ---------------------------------------------------------------------------
# The Cloudflare client
# ---------------------------------------------------------------------------

class Api(object):
    """The narrowest possible Cloudflare v4 client: JSON in, `result` out."""

    def __init__(self, token: str, opener=None):
        self._token = token
        self._opener = opener or urllib.request.urlopen

    def request(self, method: str, path: str, body=None, params=None):
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method=method)
        request.add_header("Authorization", "Bearer " + self._token)
        request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "brain-provision/1")
        try:
            with self._opener(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as error:
            raw = error.read(MAX_RESPONSE_BYTES)
            raise ApiError("%s %s -> HTTP %s: %s"
                           % (method, path, error.code, _api_messages(raw)))
        except urllib.error.URLError as error:
            raise ApiError("%s %s -> %s" % (method, path, error.reason))
        try:
            document = json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError("%s %s -> unreadable response" % (method, path))
        if not document.get("success", False):
            raise ApiError("%s %s -> %s" % (method, path, _api_messages(raw)))
        return document.get("result")

    def paginate(self, path: str, params=None):
        """Every list endpoint here is paginated and every one of them has more
        entries than one page in a real account — the zone already carries seven
        hostnames [verified 2026-08-23]."""
        collected = []
        page = 1
        while True:
            query = dict(params or {})
            query.update({"page": page, "per_page": PAGE_SIZE})
            batch = self.request("GET", path, params=query) or []
            collected.extend(batch)
            if len(batch) < PAGE_SIZE:
                return collected
            page += 1
            if page > 100:
                raise ApiError("GET %s did not terminate" % path)


def _api_messages(raw: bytes) -> str:
    try:
        document = json.loads(raw.decode("utf-8"))
        errors = document.get("errors") or []
        parts = ["%s %s" % (item.get("code", "?"), item.get("message", "")) for item in errors]
        return "; ".join(parts) if parts else "no error detail"
    except Exception:
        return "unparseable error body"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_MISSING = object()


def _compact(value) -> str:
    if value is _MISSING:
        return "(unset)"
    return json.dumps(value, sort_keys=True)


def _diff(before, after, prefix=""):
    """Field-by-field difference, recursing into objects and comparing lists whole.

    Lists are compared atomically on purpose: `allowed_uris` and `include` are
    ordered sets whose meaning is the whole list, and a per-element diff of them
    reads as noise."""
    if isinstance(before, dict) and isinstance(after, dict):
        lines = []
        for key in sorted(set(before) | set(after)):
            lines.extend(_diff(before.get(key, _MISSING), after.get(key, _MISSING),
                               prefix + key + "."))
        return lines
    if before == after:
        return []
    return ["%s: %s -> %s" % (prefix.rstrip("."), _compact(before), _compact(after))]


class Report(object):
    """What happened, in a form a human reads and a machine can parse.

    Human lines are printed as they happen rather than buffered to the end: an
    `--apply` that dies halfway must leave behind an accurate record of what it
    already changed, and a buffer that is never flushed is the opposite of that."""

    def __init__(self, apply_changes: bool, as_json: bool, stream=sys.stdout):
        self.apply = apply_changes
        self.as_json = as_json
        self.stream = stream
        self.steps = []
        self._current = None
        self.changes = 0
        self.blocked = 0
        self.private_outputs = {}

    def _say(self, text: str) -> None:
        if not self.as_json:
            self.stream.write(text + "\n")
            self.stream.flush()

    def begin(self, name: str) -> None:
        self._current = {"step": name, "items": []}
        self.steps.append(self._current)
        self._say("[%d/%d] %s" % (STEPS.index(name) + 1, len(STEPS), name))

    def _item(self, status: str, target: str, detail=None, diff=()):
        entry = {"status": status, "target": target}
        if detail:
            entry["detail"] = detail
        if diff:
            entry["diff"] = list(diff)
        self._current["items"].append(entry)
        return entry

    def ok(self, target: str, detail: str = "") -> None:
        self._item("ok", target, detail)
        self._say("      ok        %s%s" % (target, ("  " + detail) if detail else ""))

    def change(self, action: str, target: str, diff=(), detail: str = "") -> None:
        self.changes += 1
        self._item(action, target, detail, diff)
        verb = action if self.apply else action.upper()
        self._say("      %-9s %s%s" % (verb, target, ("  " + detail) if detail else ""))
        for line in diff:
            self._say("                  %s" % line)

    def block(self, target: str, message: str) -> None:
        self.blocked += 1
        self._item("blocked", target, message)
        self._say("      blocked   %s" % target)
        for line in message.splitlines():
            self._say("                  %s" % line)

    def note(self, text: str) -> None:
        self._item("note", "", text)
        for line in text.splitlines():
            self._say("                  %s" % line)

    def private(self, key: str, value: str) -> None:
        """A value the operator needs and this repository must never hold."""
        self.private_outputs[key] = value

    def finish(self, code: int) -> None:
        if self.as_json:
            json.dump({"apply": self.apply, "exit": code, "changes": self.changes,
                       "blocked": self.blocked, "steps": self.steps,
                       "private_outputs": self.private_outputs},
                      self.stream, indent=2, sort_keys=False)
            self.stream.write("\n")
            return
        self._say("")
        if self.private_outputs:
            self._say("PRIVATE — belongs in /etc/brain/brain-http.json, never in git:")
            for key in sorted(self.private_outputs):
                self._say("  %-28s %s" % (key, self.private_outputs[key]))
            self._say("")
        if self.changes == 0 and self.blocked == 0:
            self._say("converged: no changes.")
        elif not self.apply:
            self._say("%d change(s) would be made. Re-run with --apply." % self.changes)
        else:
            self._say("%d change(s) applied." % self.changes)
        if self.blocked:
            self._say("%d item(s) need an operator action before the next run "
                      "can converge." % self.blocked)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

class Context(object):
    def __init__(self, api: Api, state: dict, report: Report, apply_changes: bool,
                 check: bool = False):
        self.api = api
        self.state = state
        self.report = report
        self.apply = apply_changes
        self.check = check
        self.account = "/accounts/" + state["account_id"]
        self.zone = "/zones/" + state["zone_id"]
        self.tunnel_id = None
        self.apps = {}                # hostname -> live application object
        self.deferred_401 = {}        # hostname -> True while 401 redirect is held back
        # hostname -> whether a Service Auth policy exists, or would exist after
        # this run. Tracked rather than re-read, because the application object
        # cached above was fetched BEFORE the policies step touched it.
        self.service_auth = {}
        self._service_tokens = None

    def service_tokens(self):
        """Listing tokens is a READ, and reads are not part of the step ordering.

        Only mutations are ordered. The policies step needs a token's id to bind
        it, and that id can only come from the account — waiting for the service
        token step to look it up would make every Service Auth policy take two
        runs to converge, which breaks the idempotency contract for no gain."""
        if self._service_tokens is None:
            self._service_tokens = self.api.paginate(self.account + "/access/service_tokens")
        return self._service_tokens


def reconcile(context: Context) -> int:
    step_tunnel(context)
    step_ingress(context)
    step_applications(context)
    step_policies(context)
    step_service_tokens(context)
    step_notifications(context)
    step_dns(context)
    # A dry run makes no claim about convergence — it reports a plan — so it
    # exits 0 whatever it found, with the blocked items printed all the same.
    # Only an --apply that could not finish the job says so in its exit code,
    # because only an --apply was supposed to finish it.
    if context.apply and context.report.blocked:
        return EXIT_BLOCKED
    # --check is the third mode and it exists for one reason: a timer needs a
    # verdict, and a plain dry run deliberately does not give one. Without it
    # the expiring-service-token check in this file could be scheduled and
    # would still never alert, because it would exit 0 on the morning the
    # credential lapsed. Blocked outranks drift: an expiring token is a
    # deadline, a changed application is a diff.
    if context.check:
        if context.report.blocked:
            return EXIT_BLOCKED
        if context.report.changes:
            return EXIT_DRIFT
    return EXIT_OK


def step_tunnel(context: Context) -> None:
    report = context.report
    report.begin("tunnel")
    spec = context.state["tunnel"]
    name = spec["name"]
    wanted_src = spec.get("config_src", "local")

    live = None
    for candidate in context.api.paginate(context.account + "/cfd_tunnel",
                                          {"is_deleted": "false"}):
        if candidate.get("name") == name:
            live = candidate
            break

    if live is None:
        if not spec.get("create_if_missing", False):
            raise StepFailed("tunnel", "no tunnel named %r in this account and "
                                       "tunnel.create_if_missing is false" % name)
        if not context.apply:
            report.change("create", "tunnel %s" % name,
                          detail="config_src=%s" % wanted_src)
            return
        created = context.api.request("POST", context.account + "/cfd_tunnel",
                                      {"name": name, "config_src": wanted_src})
        # The create response carries the connector credential. It is dropped
        # here — unprinted, unlogged, absent from --json. That costs nothing,
        # because unlike a service token's client secret this one can be
        # retrieved again from the account at any time, so there is no reason
        # for it to pass through an operator's scrollback or a CI log.
        context.tunnel_id = created["id"]
        report.change("create", "tunnel %s" % name, detail="id %s" % created["id"])
        report.note(
            "The connector credential returned by the API was deliberately "
            "discarded. Retrieve it from the account with the cloudflared CLI or "
            "the dashboard and write it to %s (root-owned, mode 0600); "
            "compose mounts that file read-only into the connector."
            % spec.get("credentials_file", "/etc/brain/cloudflared/credentials.json"))
        return

    context.tunnel_id = live["id"]
    declared = spec.get("id")
    if declared and declared != live["id"]:
        raise StepFailed("tunnel",
                         "tunnel %r exists with id %s but the state declares %s. "
                         "Two tunnels of the same name in one account is the one "
                         "case this script will not guess its way through."
                         % (name, live["id"], declared))
    live_src = live.get("config_src")
    if live_src != wanted_src:
        # config_src is not something this script flips. Moving a tunnel from
        # local to remote configuration silently blackholes every route the
        # mounted config file provides, and moving it the other way abandons the
        # ingress list stored at the edge. Both are decisions, not drift.
        raise StepFailed("tunnel",
                         "tunnel %r has config_src=%r but the state wants %r. "
                         "Change it deliberately; a flip here silently discards "
                         "one of the two ingress lists." % (name, live_src, wanted_src))
    report.ok("tunnel %s" % name, "id %s, config_src %s" % (live["id"], live_src))
    report.private("tunnel_id", live["id"])


def _routed_hostnames(state: dict):
    """The hostnames tunnel.ingress publishes — the catch-all excluded.

    One place, because "what does this configuration publish" is asked by the
    ingress step, by the DNS step and by validation, and an answer that differed
    between them would mean a hostname routed but never given an application, or
    given one and never a record."""
    return [rule["hostname"].strip().lower()
            for rule in state["tunnel"]["ingress"] if rule.get("hostname")]


def step_ingress(context: Context) -> None:
    """Route the hostnames to the origin — at the edge, or in the mounted file.

    The brain runs its own dedicated tunnel rather than joining the host's
    existing one, and this step is the reason. All replicas of one tunnel share
    ONE ingress list [verified 2026-08-23], and the configuration PUT REPLACES
    that list entirely: a run of this script against a shared tunnel would
    delete the five dev hostnames it knows nothing about."""
    report = context.report
    report.begin("tunnel ingress")
    spec = context.state["tunnel"]
    wanted = [{"hostname": rule["hostname"].strip().lower(), "service": rule["service"]}
              for rule in spec["ingress"] if rule.get("hostname")]

    if spec.get("config_src", "local") == "local":
        # Locally-configured tunnels keep their ingress in the file mounted into
        # the connector container; the API holds nothing to reconcile. What CAN
        # be checked is that the file and the state agree, because a hostname
        # present here and missing there is a 404 that looks like an outage.
        path = spec.get("local_config")
        if not path:
            report.ok("ingress", "managed by the connector's local config file")
            return
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            report.block("ingress file %s" % path,
                         "cannot be read from here (%s). On the VPS this file is "
                         "the ingress; verify it lists exactly: %s"
                         % (error, ", ".join(rule["hostname"] for rule in wanted)))
            return
        found = set(re.findall(r"^\s*-?\s*hostname:\s*\"?([A-Za-z0-9._-]+)\"?\s*$",
                               text, re.M))
        expected = set(rule["hostname"] for rule in wanted)
        if found != expected:
            raise StepFailed("tunnel ingress",
                             "%s routes %s but the state declares %s"
                             % (path, sorted(found) or "nothing", sorted(expected)))
        report.ok("ingress file %s" % path, "%d hostname(s) match" % len(expected))
        return

    if context.tunnel_id is None:
        # Only reachable in a dry run: --apply created the tunnel in the step
        # before this one and always has an id by here. The edge configuration
        # lives UNDER that id, so there is nothing to fetch and nothing to
        # diff — and interpolating the id anyway would send the literal string
        # "None" to the API and report whatever 404 came back as a failure.
        report.note("not previewed: the tunnel does not exist yet, so it has no "
                    "edge configuration to compare against. --apply creates the "
                    "tunnel and writes this ingress in the same run.")
        return

    path = "%s/cfd_tunnel/%s/configurations" % (context.account, context.tunnel_id)
    live = context.api.request("GET", path) or {}
    live_config = (live.get("config") or {})
    live_ingress = live_config.get("ingress") or []
    live_hosts = [rule.get("hostname") for rule in live_ingress if rule.get("hostname")]
    unknown = [host for host in live_hosts
               if host not in set(rule["hostname"] for rule in wanted)]
    if unknown and not spec.get("prune_ingress", False):
        raise StepFailed("tunnel ingress",
                         "this tunnel already routes %s, which the state does not "
                         "declare. The configuration PUT replaces the whole list, "
                         "so applying would unpublish them. Either add them to the "
                         "state or set tunnel.prune_ingress." % ", ".join(unknown))

    # The catch-all MUST be last and MUST exist: cloudflared refuses a
    # configuration whose final rule has a hostname, and without it an unmatched
    # Host header falls through to whatever the previous rule was.
    desired_ingress = list(wanted) + [{"service": CATCH_ALL_SERVICE}]
    desired_config = dict(live_config)
    desired_config["ingress"] = desired_ingress
    if live_ingress == desired_ingress:
        report.ok("ingress", "%d hostname(s) plus catch-all" % len(wanted))
        return
    diff = _diff({"ingress": live_ingress}, {"ingress": desired_ingress})
    report.change("update", "ingress", diff)
    if context.apply:
        context.api.request("PUT", path, {"config": desired_config})


def step_applications(context: Context) -> None:
    report = context.report
    report.begin("access applications")
    live_apps = context.api.paginate(context.account + "/access/apps")
    by_domain = {}
    for app in live_apps:
        domain = str(app.get("domain", "")).strip().lower()
        if domain:
            by_domain[domain] = app

    for spec in context.state["applications"]:
        hostname = spec["hostname"].strip().lower()
        live = by_domain.get(hostname)
        desired = _desired_application(spec, hostname)

        # service_auth_401_redirect cannot be turned on before a Service Auth
        # policy exists [verified 2026-08-23], and policies are the NEXT step.
        # So it is held back here and applied by the tail of step_policies. The
        # alternative — reordering policies before applications — would create
        # the policy against an application that does not exist yet.
        wants_401 = bool(desired.get("service_auth_401_redirect"))
        if wants_401 and not _has_service_auth_policy(context, live):
            desired["service_auth_401_redirect"] = False
            context.deferred_401[hostname] = True

        if live is None:
            if not context.apply:
                report.change("create", hostname, _diff({}, desired))
                continue
            created = context.api.request("POST", context.account + "/access/apps", desired)
            context.apps[hostname] = created
            report.change("create", hostname, detail="aud captured below")
            report.note("Access begins enforcing roughly 85 seconds after an "
                        "application is created. DNS is reconciled last for "
                        "exactly this reason; do not probe the hostname sooner.")
            report.private("aud[%s]" % hostname, created.get("aud", ""))
            continue

        context.apps[hostname] = live
        report.private("aud[%s]" % hostname, live.get("aud", ""))
        owned_live = dict((key, live.get(key, _MISSING)) for key in desired)
        diff = _diff(owned_live, desired)
        if not diff:
            report.ok(hostname, "id %s" % live.get("id", "?"))
            continue
        report.change("update", hostname, diff)
        if context.apply:
            body = _merge_application(live, desired)
            # PUT, never PATCH: PATCH on an Access application is rejected under
            # API-token authentication [verified 2026-08-23]. PUT replaces the
            # whole object, which is why the body starts from the live one.
            context.apps[hostname] = context.api.request(
                "PUT", "%s/access/apps/%s" % (context.account, live["id"]), body)


def _desired_application(spec: dict, hostname: str) -> dict:
    """The fields this script owns on an Access application. Nothing else."""
    desired = {
        "name": spec["name"],
        "type": "self_hosted",
        # `domain`, singular. NEVER self_hosted_domains together with
        # destinations: the two are counted additively against a five-
        # destination cap [verified 2026-08-23], and sending both is how a
        # two-hostname account discovers the cap at five. Each brain hostname is
        # its own application anyway — merging them into one multi-domain
        # application would make a read-only token valid on the capture domain,
        # because an OAuth token issued through any domain of an application is
        # valid for all of them.
        "domain": hostname,
        "session_duration": spec["session_duration"],
        "service_auth_401_redirect": bool(spec.get("service_auth_401_redirect", False)),
        "oauth_configuration": spec["oauth_configuration"],
    }
    for key, value in (spec.get("extra") or {}).items():
        desired[key] = value
    return desired


def _merge_application(live: dict, desired: dict) -> dict:
    """Build a full PUT body: the live object, minus what must not be sent."""
    body = dict(live)
    for key in ("id", "aud", "created_at", "updated_at", "deleted_at",
                # Omitting `policies` from a PUT leaves the application's
                # policies intact [verified 2026-08-23]. Sending them would make
                # this step quietly own the next one.
                "policies",
                # See _desired_application: these two must never travel with
                # `domain`.
                "self_hosted_domains", "destinations"):
        body.pop(key, None)
    body.update(desired)
    return body


def _has_service_auth_policy(context: Context, live_app) -> bool:
    if not live_app:
        return False
    policies = live_app.get("policies")
    if policies is None:
        policies = context.api.paginate(
            "%s/access/apps/%s/policies" % (context.account, live_app["id"]))
    return any(policy.get("decision") == "non_identity" for policy in policies or [])


def step_policies(context: Context) -> None:
    report = context.report
    report.begin("access policies")
    for spec in context.state["applications"]:
        hostname = spec["hostname"].strip().lower()
        live_app = context.apps.get(hostname)
        if live_app is None:
            # Only reachable in a dry run: --apply created the application in
            # the previous step. There is nothing to diff the policies against,
            # and that is a gap in the preview rather than something an operator
            # must go and fix — hence a note, not a block.
            report.note("%s: policies will be created with the application; they "
                        "cannot be previewed until it exists" % hostname)
            continue
        app_id = live_app["id"]
        path = "%s/access/apps/%s/policies" % (context.account, app_id)
        live_policies = context.api.paginate(path)
        by_name = dict((policy.get("name"), policy) for policy in live_policies)

        for position, policy_spec in enumerate(spec["policies"]):
            desired = _desired_policy(context, policy_spec, position, hostname, spec["profile"])
            if desired is None:
                continue                       # blocked; already reported
            if desired["decision"] == "non_identity":
                # Records the state at the END of this run, which is what the
                # deferred 401 handoff needs — including in a dry run, where
                # "would exist" is exactly what the plan should reflect.
                context.service_auth[hostname] = True
            name = desired["name"]
            live = by_name.get(name)
            target = "%s / %s" % (hostname, name)
            if live is None:
                if not context.apply:
                    report.change("create", target, _diff({}, desired))
                    continue
                context.api.request("POST", path, desired)
                report.change("create", target)
                continue
            owned_live = dict((key, live.get(key, _MISSING)) for key in desired)
            diff = _diff(owned_live, desired)
            if not diff:
                report.ok(target)
                continue
            report.change("update", target, diff)
            if context.apply:
                body = dict(live)
                for key in ("id", "created_at", "updated_at", "uid"):
                    body.pop(key, None)
                body.update(desired)
                context.api.request("PUT", "%s/%s" % (path, live["id"]), body)

    _enable_deferred_401(context)


def _desired_policy(context: Context, spec: dict, position: int, hostname: str,
                    profile: str):
    """Resolve a declared policy into the body the API wants, or None if blocked."""
    includes = []
    for rule in spec["include"]:
        kind = list(rule)[0]
        if kind != "service_token":
            includes.append(rule)
            continue
        name = rule[kind]["name"].strip()
        existing = _find_service_token(context, name)
        if existing is None:
            context.report.block(
                "%s / %s" % (hostname, spec["name"]),
                "service token %r does not exist yet, so this Service Auth policy "
                "cannot name it. Create the token (see the service tokens step), "
                "then re-run. Nothing is opened in the meantime: the application's "
                "identity policy still applies and an unmatched request is denied."
                % name)
            return None
        declared = _declared_token(context, name)
        if declared and declared.get("profile") != profile:
            raise StepFailed(
                "access policies",
                "service token %r is declared for the %r profile but is bound to "
                "the %r application. A credential is authorized against exactly "
                "one profile." % (name, declared.get("profile"), profile))
        includes.append({"service_token": {"token_id": existing["id"]}})

    desired = {
        "name": spec["name"],
        "decision": spec["decision"],
        "include": includes,
        "precedence": int(spec.get("precedence", position + 1)),
    }
    for key in ("exclude", "require"):
        if key in spec:
            desired[key] = spec[key]
    return desired


def _find_service_token(context: Context, name: str):
    for token in context.service_tokens():
        if token.get("name") == name:
            return token
    return None


def _declared_token(context: Context, name: str):
    for token in context.state.get("service_tokens", []) or []:
        if token.get("name") == name:
            return token
    return None


def _enable_deferred_401(context: Context) -> None:
    """Turn on service_auth_401_redirect now that a Service Auth policy exists.

    This is the second half of the ordering problem noted in step_applications.
    It runs at the tail of the policies step rather than as a seventh step so
    that the declared order stays true: applications, then policies, and DNS
    still last."""
    report = context.report
    for spec in context.state["applications"]:
        hostname = spec["hostname"].strip().lower()
        if hostname not in context.deferred_401:
            continue
        live = context.apps.get(hostname)
        if live is None:
            # Dry run against an application that does not exist yet. Whether
            # the 401 redirect can be enabled depends on a policy that will be
            # created in the same --apply, so this is unpreviewable rather than
            # blocked.
            report.note("%s: service_auth_401_redirect is enabled on the run "
                        "after its Service Auth policy exists" % hostname)
            continue
        if not context.service_auth.get(hostname, False):
            report.block("%s service_auth_401_redirect" % hostname,
                         "still off: it cannot be enabled until a Service Auth "
                         "policy exists on this application. Re-run once the "
                         "service token is created.")
            continue
        report.change("update", "%s service_auth_401_redirect" % hostname,
                      ["service_auth_401_redirect: false -> true"])
        if context.apply:
            body = _merge_application(live, {"service_auth_401_redirect": True})
            context.apps[hostname] = context.api.request(
                "PUT", "%s/access/apps/%s" % (context.account, live["id"]), body)


def step_service_tokens(context: Context) -> None:
    """Report on service tokens. This step deliberately creates nothing.

    Cloudflare shows a service token's client secret EXACTLY ONCE, in the create
    response, and never again [verified 2026-08-23]. That collides head-on with
    this script's contract, which forbids printing or persisting a secret. The
    three ways out were weighed:

      - create and print it — puts a long-lived credential in scrollback, in CI
        logs, and in whatever the operator pastes when asking for help;
      - create and write it to a file — better, but it makes this script a
        secret-handling tool, which is the property that lets it stay reviewable
        and lets its --json output be shared without redaction;
      - create and discard it — mints a credential nobody can ever use.

    So the token is created by a human, in a session where the secret is shown
    to them and goes straight into /etc/brain/. This step tells them exactly
    what to create and where the secret has to land, then verifies the result on
    the next run. Everything else about a token — that it exists, that it is
    bound to the right policy, that it is not about to expire — is checked here,
    which is the part a human is bad at."""
    report = context.report
    report.begin("service tokens")
    declared = context.state.get("service_tokens", []) or []
    if not declared:
        report.ok("service tokens", "none declared")
        return
    now = datetime.now(timezone.utc)
    for spec in declared:
        name = spec["name"]
        live = _find_service_token(context, name)
        if live is None:
            report.block("service token %s" % name, (
                "does not exist. Create it in the Cloudflare dashboard (Access > "
                "Service Auth > Service Tokens) with duration %s, copy the Client "
                "Secret that is shown ONCE — it can never be retrieved again — "
                "into %s (mode 0600, root-owned), and add the Client ID to "
                "service_principals in /etc/brain/brain-http.json under the %r "
                "profile. Then re-run this script to bind it to the Service Auth "
                "policy."
                % (spec.get("duration", "8760h"),
                   spec.get("secret_file", "/etc/brain/service-tokens/%s" % name),
                   spec["profile"])))
            continue
        detail = "client id %s" % live.get("client_id", "?")
        stamped = live.get("expires_at")
        expires_at = _parse_timestamp(stamped)
        if expires_at is None:
            # Reporting "ok" here was the sharpest thing this step could get
            # wrong. The seven-day alert below is the ONLY warning between a
            # 90-day credential and the morning it stops working, and the token
            # whose expiry cannot be read is exactly the token that alert will
            # never fire for — so silence would be indistinguishable from
            # health right up until the outage. Unknown is not ok.
            report.block("service token %s" % name,
                         "exists (%s) but its expiry could not be determined: "
                         "%s. Read the expiry in the dashboard (Access > "
                         "Service Auth > Service Tokens); until it is known, "
                         "this credential has no rotation alert at all."
                         % (detail,
                            "the API returned no expires_at" if stamped is None
                            else "expires_at %r is not a timestamp this script "
                                 "can read" % (stamped,)))
            continue
        warn_days = int(spec.get("expiry_warning_days", 7))
        if expires_at <= now:
            report.block("service token %s" % name,
                         "expired on %s. Rotate it: create a replacement, put "
                         "the new secret in place, then delete this one."
                         % expires_at.date().isoformat())
            continue
        days = (expires_at - now).days
        if days <= warn_days:
            report.block("service token %s" % name,
                         "expires in %d day(s), on %s. Rotate it before then; "
                         "the client stops working the moment it lapses."
                         % (days, expires_at.date().isoformat()))
            continue
        mismatch = _lifetime_mismatch(spec, live, expires_at, now)
        if mismatch is not None:
            report.block("service token %s" % name, mismatch)
            continue
        detail += ", expires %s" % expires_at.date().isoformat()
        report.ok("service token %s" % name, detail)


def _lifetime_mismatch(spec: dict, live: dict, expires_at, now):
    """Does the live token's lifetime match the declared duration? None if it does.

    A token that exists and is not about to lapse can still be the WRONG token.
    One minted for a year where the state declares 90 days is a long-lived
    credential nobody will look at again; one minted for a week is an outage
    with a date on it. Neither shows up as drift anywhere else in this script,
    because the token is created by a human in a dashboard where the duration is
    a dropdown — which is exactly the kind of mistake a machine should catch.

    Two measurements, deliberately of different strength:

    - With `created_at`, the minted lifetime is known exactly, so drift in
      EITHER direction is reported.
    - Without it, only the remaining lifetime is known, and remaining is shorter
      than declared for every healthy token that has been alive a day — so only
      the impossible direction (remaining LONGER than the whole declared
      duration) is reported. Reporting the other direction here would flag every
      correct token in the account, which is how a check gets ignored."""
    declared = _parse_go_duration(spec.get("duration"))
    if declared is None:
        return None
    created_at = _parse_timestamp(live.get("created_at"))
    if created_at is not None:
        minted = expires_at - created_at
        if abs(minted - declared) <= LIFETIME_TOLERANCE:
            return None
        return ("was minted with a lifetime of %s but the state declares %s "
                "(created %s, expires %s). Recreate it with the declared "
                "duration, or change the declaration to the duration that was "
                "actually issued — the two disagreeing means neither is the "
                "rotation schedule anyone is following."
                % (_describe_delta(minted), spec["duration"],
                   created_at.date().isoformat(), expires_at.date().isoformat()))
    remaining = expires_at - now
    if remaining <= declared + LIFETIME_TOLERANCE:
        return None
    return ("has %s left, which is longer than the whole %s lifetime the state "
            "declares, so it was not created with that duration. Recreate it "
            "with the declared duration, or change the declaration to match "
            "what was actually issued." % (_describe_delta(remaining), spec["duration"]))


def _describe_delta(delta) -> str:
    """A span in the units a human thinks in. Rounded on purpose: this goes into
    a sentence an operator reads, never into a comparison."""
    hours = delta.total_seconds() / 3600.0
    if abs(hours) >= 48:
        return "%.0f day(s)" % (hours / 24.0)
    return "%.1f hour(s)" % hours


def _parse_go_duration(value):
    """Turn a Go duration such as "336h" into a timedelta, or None.

    None for anything DURATION_RE does not admit, so this and the loader agree
    on what a duration is.

    Shape-checking a duration at load time is not enough to compare one, and
    comparing is the point: see _lifetime_mismatch."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    match = DURATION_RE.match(text)
    if not match:
        return None
    unit = match.group(1)
    return timedelta(seconds=int(text[:-len(unit)]) * DURATION_SECONDS[unit])


def _parse_timestamp(value):
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})", value)
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    # Cloudflare stamps these in UTC. Treating a missing zone as UTC is right
    # here and would be wrong almost anywhere else, so it is stated rather than
    # assumed.
    return parsed.replace(tzinfo=timezone.utc)


def step_notifications(context: Context) -> None:
    """Cloudflare's half of the alerting — and the half it cannot do.

    Everything else this system watches, it watches from the inside: `doctor`
    reads the backup stamp and the disk, the timers raise through
    `brain-alert@`, the capture queue escalates its own backlog. All of it runs
    ON the VPS, which means all of it goes quiet in exactly the failure where
    silence is indistinguishable from health — the box being gone. These
    policies are the only alerting that survives that, because Cloudflare is
    the one observer that is not the thing being observed.

    Two are worth having and the account offers little else [verified
    2026-08-23 against `available_alerts`]:

      `tunnel_health_event`      the connector stopped talking to the edge
      `expiring_service_token_alert`  a headless credential lapses in 7 days

    Read the first one for exactly what it says. Cloudflare's own
    documentation is blunt about it: tunnel status "only reflects the
    connection between cloudflared and the Cloudflare network... A tunnel can
    appear Healthy while users are unable to connect to an application"
    [docs 2026-08-23]. So this alert fires when the connector dies and stays
    silent when the brain behind it dies — which is why it does not replace the
    endpoint probe on the host, and why `brain-edge-check.timer` runs this
    file with `--check` rather than treating a green tunnel as an answer.

    There is deliberately no application-health policy. The account's only
    HTTP-level notifier is `health_check_status_notification`, which needs a
    standalone Health Check — a paid add-on this deployment does not have. An
    empty policy pointed at nothing would look like coverage on the dashboard
    and alert on nothing at all, which is worse than the gap it papers over.

    Like every other step here: it creates and updates, and it never deletes.
    A policy dropped from the state file is reported as drift for a human."""
    report = context.report
    report.begin("notifications")
    declared = context.state.get("notifications", []) or []
    if not declared:
        report.ok("notifications", "none declared")
        return
    path = context.account + "/alerting/v3/policies"
    by_name = dict((policy.get("name"), policy) for policy in context.api.paginate(path))

    for spec in declared:
        desired = _desired_notification(context, spec)
        if desired is None:
            continue                        # blocked; already reported
        name = desired["name"]
        live = by_name.get(name)
        target = "notification %s" % name
        if live is None:
            if not context.apply:
                report.change("create", target, _diff({}, desired))
                continue
            context.api.request("POST", path, desired)
            report.change("create", target)
            continue
        owned_live = dict((key, live.get(key, _MISSING)) for key in desired)
        diff = _diff(owned_live, desired)
        if not diff:
            report.ok(target)
            continue
        report.change("update", target, diff)
        if context.apply:
            body = dict(live)
            for key in ("id", "created", "modified"):
                body.pop(key, None)
            body.update(desired)
            context.api.request("PUT", "%s/%s" % (path, live["id"]), body)


def _desired_notification(context: Context, spec: dict):
    """Resolve a declared policy into the body the API wants, or None if blocked."""
    alert_type = spec["alert_type"]
    desired = {
        "name": spec["name"],
        "alert_type": alert_type,
        "enabled": bool(spec.get("enabled", True)),
        # The API's own wording: "IDs for email type will be the email address."
        "mechanisms": {"email": [{"id": address} for address in spec["email"]]},
    }
    if spec.get("description"):
        desired["description"] = spec["description"]

    filters = dict(spec.get("filters") or {})
    if alert_type == "tunnel_health_event":
        if context.tunnel_id is None:
            # Only reachable in a dry run against an account where the tunnel
            # does not exist yet: --apply created it in the first step. Same
            # shape as the policies step's unpreviewable case.
            context.report.note(
                "%s: cannot be previewed until the tunnel exists — its filter "
                "binds to the tunnel's id" % spec["name"])
            return None
        # Bound to THIS tunnel, and the id is injected rather than declared.
        # An unfiltered tunnel_health_event covers every tunnel in the account,
        # which here would mean paging the owner about the unrelated host
        # connector that serves their dev hostnames. Ids are per-account, so
        # writing one into the state file would also make that file unusable
        # for rebuilding the edge anywhere else.
        filters["tunnel_id"] = [context.tunnel_id]
        filters.setdefault("new_status", list(DEFAULT_TUNNEL_STATUSES))
    if filters:
        desired["filters"] = filters
    return desired


def step_dns(context: Context) -> None:
    """Last, because this is what makes the hostname reachable.

    Every record is a proxied CNAME to <tunnel-id>.cfargotunnel.com. Proxied is
    not a preference: an unproxied record for a tunnel hostname does not resolve
    to anything that can serve it, and it would also take the request out of
    Access, which is the entire authentication boundary."""
    report = context.report
    report.begin("dns records")
    settings = context.state.get("dns") or {}
    comment = settings.get("comment", "brain remote MCP — deploy/cloudflare/provision.py")
    hostnames = _routed_hostnames(context.state)

    if context.tunnel_id is None:
        # Only reachable in a dry run against an account where the tunnel does
        # not exist yet; --apply creates it in the first step. The content of
        # every record here is <tunnel-id>.cfargotunnel.com, so with no id there
        # is no target — and a plan that prints a target it does not have is
        # worse than a plan that prints none. An operator reads a plan as what
        # --apply is going to do, so an interpolated None reads as a VALUE
        # rather than as an absence, and it is a value that would blackhole
        # every hostname if it were ever written. Nothing is diffed; what the
        # run cannot know is said plainly instead.
        report.note("not planned: each of these hostnames gets a proxied CNAME "
                    "to <tunnel-id>.cfargotunnel.com, and the tunnel does not "
                    "exist yet — so the id, which is the entire content of the "
                    "record, is UNKNOWN and no target can be shown. Affected: "
                    "%s. Run --apply, which creates the tunnel first and then "
                    "has the real id, or re-run this plan once the tunnel "
                    "exists to see each record diffed against it."
                    % ", ".join(hostnames))
        return

    target = "%s.cfargotunnel.com" % context.tunnel_id
    for hostname in hostnames:
        matches = context.api.paginate(context.zone + "/dns_records", {"name": hostname})
        existing = [record for record in matches
                    if str(record.get("name", "")).lower() == hostname]
        conflicting = [record for record in existing if record.get("type") != "CNAME"]
        if conflicting:
            # Replacing an A record with a CNAME is a deletion wearing a
            # different hat, and this script does not delete. A human looks at
            # what that record was for.
            raise StepFailed("dns records",
                             "%s already has a %s record. This script will not "
                             "replace a record of another type; remove it "
                             "deliberately if the hostname is meant for the brain."
                             % (hostname, conflicting[0].get("type")))
        desired = {
            "type": "CNAME",
            "name": hostname,
            "content": target,
            "proxied": True,
            "ttl": 1,                 # 1 means "automatic", the only legal ttl when proxied
            "comment": comment,
        }
        if not existing:
            if not context.apply:
                report.change("create", hostname, _diff({}, desired))
                continue
            context.api.request("POST", context.zone + "/dns_records", desired)
            report.change("create", hostname, detail="-> %s (proxied)" % target)
            continue
        live = existing[0]
        owned_live = dict((key, live.get(key, _MISSING)) for key in desired)
        diff = _diff(owned_live, desired)
        if not diff:
            report.ok(hostname, "-> %s (proxied)" % target)
            continue
        report.change("update", hostname, diff)
        if context.apply:
            body = dict(live)
            for key in ("id", "zone_id", "zone_name", "created_on", "modified_on",
                        "proxiable", "locked", "meta", "tags_modified_on"):
                body.pop(key, None)
            body.update(desired)
            context.api.request("PUT", "%s/dns_records/%s" % (context.zone, live["id"]),
                                body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision.py",
        description="Reconcile a Cloudflare account to the brain's declared edge "
                    "configuration. Dry run unless --apply is given.")
    parser.add_argument("--state", required=True,
                        help="path to the private desired-state JSON file")
    parser.add_argument("--token-env", default="CLOUDFLARE_API_TOKEN",
                        help="environment variable holding the Cloudflare API token "
                             "(default: CLOUDFLARE_API_TOKEN). The token is never "
                             "accepted as an argument.")
    parser.add_argument("--apply", action="store_true",
                        help="actually make the changes. Without this, nothing "
                             "is written and only the diff is printed.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="emit the plan or result as JSON instead of text")
    parser.add_argument("--check", action="store_true",
                        help="a dry run that FAILS on what it finds, for a timer: "
                             "exit 2 if an operator action is owed (an expiring "
                             "service token, a missing one), exit 4 if the account "
                             "has drifted from the declared state. A plain dry run "
                             "exits 0 either way, because a plan is not a verdict.")
    return parser


def _read_token(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise Refusal("%s is empty or unset. Export a Cloudflare API token scoped "
                      "to this account's Access, Tunnel and DNS resources." % name)
    return value.strip()


def _warn_on_loose_permissions(path: str, report_stream) -> None:
    """The state file holds the account id, the zone and the owner's address.

    None of that is a credential, so a loose mode is a warning and not a
    refusal — but it is exactly the file an attacker with a shell would read to
    learn which hostnames are worth attacking."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        report_stream.write(
            "warning: %s is readable beyond its owner; chmod 600 it.\n" % path)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.apply and args.check:
        # Checked before anything is read, because it is a contradiction in the
        # command line rather than something the account or the state file did.
        sys.stderr.write("refused: --check reports, --apply changes. Pick one.\n")
        return EXIT_REFUSED
    try:
        credential = _read_token(args.token_env)
        state = load_state(args.state)
    except Refusal as refusal:
        sys.stderr.write("refused: %s\n" % refusal)
        return EXIT_REFUSED
    _warn_on_loose_permissions(args.state, sys.stderr)

    report = Report(args.apply, args.as_json)
    if not args.as_json:
        sys.stdout.write("brain cloudflare reconcile — %s\n\n"
                         % ("APPLY" if args.apply else
                            "CHECK (nothing will change; drift and deadlines fail)"
                            if args.check else "DRY RUN (nothing will change)"))
    context = Context(Api(credential), state, report, args.apply, args.check)
    try:
        code = reconcile(context)
    except StepFailed as failure:
        report.finish(EXIT_FAILED)
        sys.stderr.write("FAILED at step %r: %s\n" % (failure.step, failure))
        return EXIT_FAILED
    except ApiError as error:
        step = report.steps[-1]["step"] if report.steps else "startup"
        report.finish(EXIT_FAILED)
        sys.stderr.write("FAILED at step %r: %s\n" % (step, error))
        return EXIT_FAILED
    report.finish(code)
    return code


if __name__ == "__main__":
    sys.exit(main())
