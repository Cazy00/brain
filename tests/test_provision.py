"""Tests for the Cloudflare reconciler in deploy/cloudflare/provision.py.

The script's central claim is that a second `--apply` against a converged
account reports zero changes. That claim cannot be settled by reading the
summary line it prints — a reconciler that wrote the same object twice and then
reported "converged" would print exactly the same line. So these tests count
the write requests that actually reached the account.

The account is a fake one: an in-memory object that answers reads from mutable
state and records every request it receives, injected through the `opener` seam
`Api` already exposes. That seam is the right one to test at, because
everything between the step functions and the socket — the request builder, the
pagination, the response envelope — stays real, and what is asserted is the
exact sequence of writes an operator would see in the Cloudflare audit log.

Nothing here talks to a network, and nothing here contains a real account id,
audience, tunnel id, email or credential. The one credential-shaped value the
fake account hands back is assembled at run time, because the point of the test
it serves is that such a value must never appear in a tracked file OR in this
script's output.
"""
import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
import urllib.parse
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "deploy" / "cloudflare"))
import provision  # noqa: E402

ACCOUNT_ID = "account-id-for-tests"
ZONE_ID = "zone-id-for-tests"
TEAM = "example-team.cloudflareaccess.com"
OWNER = "owner@example.invalid"
CAPTURE_HOST = "brain.example.invalid"
READ_HOST = "brain-read.example.invalid"
CAPTURE_CLIENT_NAME = "brain-capture-headless"
READ_CLIENT_NAME = "brain-read-headless"

# The API credential the script sends. Named for what it is in the request and
# never "token": a test fixture must not read as a leaked one.
BEARER = "not-a-real-cloudflare-credential"

# Ninety days, the design's declared service-token lifetime, as the state file
# spells it and as a timedelta to build fixtures with.
DECLARED_DURATION = "2160h"
DECLARED_LIFETIME = timedelta(hours=2160)


def stamp(moment: datetime) -> str:
    """The timestamp shape Cloudflare returns."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def sample_state(config_src="cloudflare", create_if_missing=True,
                 service_tokens=None, ingress=None, applications=None,
                 notifications=None):
    """The shape of desired-state.example.json with test values filled in.

    Defaults to `config_src: cloudflare` — the example file uses `local`, but
    the local path deliberately makes no API call at all, and the ordering and
    idempotency properties under test are about requests."""
    state = {
        "account_id": ACCOUNT_ID,
        "zone_id": ZONE_ID,
        "team_domain": TEAM,
        "owner_email": OWNER,
        "tunnel": {
            "name": "brain",
            "config_src": config_src,
            "create_if_missing": create_if_missing,
            "credentials_file": "/etc/brain/cloudflared/credentials.json",
            "ingress": ingress if ingress is not None else [
                {"hostname": CAPTURE_HOST, "service": "http://brain:8787"},
                {"hostname": READ_HOST, "service": "http://brain:8787"},
            ],
        },
        "applications": applications if applications is not None else [
            application(CAPTURE_HOST, "capture", CAPTURE_CLIENT_NAME),
            application(READ_HOST, "read", READ_CLIENT_NAME),
        ],
        "service_tokens": service_tokens if service_tokens is not None else [
            declared_client(CAPTURE_CLIENT_NAME, "capture"),
            declared_client(READ_CLIENT_NAME, "read"),
        ],
        "notifications": notifications if notifications is not None else [
            {"name": "brain tunnel unhealthy", "alert_type": "tunnel_health_event",
             "email": [OWNER]},
            {"name": "brain service token expiring",
             "alert_type": "expiring_service_token_alert", "email": [OWNER]},
        ],
        "dns": {"comment": "brain remote MCP — provision.py"},
    }
    return state


def application(hostname, profile, client_name):
    return {
        "name": "brain — %s" % profile,
        "hostname": hostname,
        "profile": profile,
        "session_duration": "336h",
        "service_auth_401_redirect": True,
        "oauth_configuration": {
            "enabled": True,
            "dynamic_client_registration": {
                "enabled": True,
                "allowed_uris": ["https://client.example.invalid/callback"],
                "allow_any_on_localhost": True,
                "allow_any_on_loopback": True,
            },
            "grant": {"access_token_lifetime": "15m", "session_duration": "336h"},
        },
        "extra": {"app_launcher_visible": False, "auto_redirect_to_identity": False},
        "policies": [
            {"name": "brain owner", "decision": "allow", "precedence": 1,
             "include": [{"email": {"email": OWNER}}]},
            {"name": "brain %s service clients" % profile, "decision": "non_identity",
             "precedence": 2,
             "include": [{"service_token": {"name": client_name}}]},
        ],
    }


def declared_client(name, profile):
    return {"name": name, "profile": profile, "duration": DECLARED_DURATION,
            "secret_file": "/etc/brain/service-tokens/%s" % name,
            "expiry_warning_days": 7}


# ---------------------------------------------------------------------------
# The fake account
# ---------------------------------------------------------------------------

Recorded = namedtuple("Recorded", "method path params body authorization")

MISSING = object()


class FakeResponse(object):
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self, limit=None):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeCloudflare(object):
    """One Cloudflare account, in memory: reads answered from mutable state,
    every request recorded in the order it arrived.

    It is deliberately more generous than the real API in one place — its
    service-token list carries a client secret, which Cloudflare never returns
    after the create call. A fake that withheld it could not prove the script
    does not leak one; this one can."""

    WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")

    def __init__(self):
        self.tunnels = []
        self.configurations = {}
        self.apps = []
        self.policies = {}
        self.service_clients = []
        self.notification_policies = []
        self.dns_records = []
        self.requests = []
        self._counter = 0
        # The connector credential the create-tunnel response carries. The
        # script discards it on purpose; assembled here rather than written as a
        # literal so no credential-shaped string is tracked in this repository.
        self.connector_credential = "-".join(("connector", "credential", "sentinel"))

    # -- the seam ----------------------------------------------------------
    def open(self, request, timeout=None):
        method = request.get_method()
        url = request.full_url
        assert url.startswith(provision.API_BASE), url
        path, _sep, query = url[len(provision.API_BASE):].partition("?")
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.requests.append(Recorded(method, path, dict(urllib.parse.parse_qsl(query)),
                                      body, request.get_header("Authorization")))
        result = self._dispatch(method, [p for p in path.split("/") if p],
                                dict(urllib.parse.parse_qsl(query)), body)
        payload = json.dumps({"success": True, "errors": [], "messages": [],
                              "result": result}).encode("utf-8")
        return FakeResponse(payload)

    # -- assertions read these --------------------------------------------
    def writes(self):
        return [record for record in self.requests
                if record.method in self.WRITE_METHODS]

    def bodies(self):
        return json.dumps([record.body for record in self.requests])

    def identifier(self, prefix):
        self._counter += 1
        return "%s-%02d" % (prefix, self._counter)

    def add_service_client(self, name, created_at=None, expires_at=MISSING):
        """A service token as the account holds it — created by a human, as
        provision.py insists, so the tests can exercise the checking it does."""
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(days=10) if created_at is None else created_at
        record = {"id": self.identifier("service-client"), "name": name,
                  "client_id": "%s.access" % name, "created_at": stamp(created_at)}
        if expires_at is MISSING:
            expires_at = created_at + DECLARED_LIFETIME
        if isinstance(expires_at, datetime):
            record["expires_at"] = stamp(expires_at)
        elif expires_at is not None:
            record["expires_at"] = expires_at
        record["client_secret"] = "-".join(("must", "never", "be", "printed"))
        self.service_clients.append(record)
        return record

    # -- routing -----------------------------------------------------------
    def _dispatch(self, method, parts, params, body):
        if parts[:1] == ["accounts"]:
            return self._account(method, parts[2:], params, body)
        if parts[:1] == ["zones"]:
            return self._zone(method, parts[2:], params, body)
        raise AssertionError("the script asked for an unexpected path: %s" % parts)

    def _account(self, method, rest, params, body):
        if rest == ["cfd_tunnel"]:
            if method == "GET":
                return self._page(self.tunnels, params)
            if method == "POST":
                created = {"id": self.identifier("tunnel"), "name": body["name"],
                           "config_src": body.get("config_src", "local"),
                           "token": self.connector_credential}
                self.tunnels.append(created)
                return copy.deepcopy(created)
        if len(rest) == 3 and rest[0] == "cfd_tunnel" and rest[2] == "configurations":
            tunnel_id = rest[1]
            if method == "GET":
                return {"tunnel_id": tunnel_id,
                        "config": copy.deepcopy(self.configurations.get(tunnel_id, {}))}
            if method == "PUT":
                self.configurations[tunnel_id] = copy.deepcopy(body["config"])
                return {"tunnel_id": tunnel_id, "config": copy.deepcopy(body["config"])}
        if rest == ["access", "service_tokens"] and method == "GET":
            return self._page(self.service_clients, params)
        if rest == ["alerting", "v3", "policies"]:
            if method == "GET":
                return self._page(self.notification_policies, params)
            if method == "POST":
                created = copy.deepcopy(body)
                created["id"] = self.identifier("notification")
                self.notification_policies.append(created)
                return copy.deepcopy(created)
        if len(rest) == 4 and rest[:3] == ["alerting", "v3", "policies"] and method == "PUT":
            return copy.deepcopy(self._replace(self.notification_policies, rest[3],
                                               body, ("id",)))
        if rest == ["access", "apps"]:
            if method == "GET":
                return self._page(self.apps, params)
            if method == "POST":
                created = copy.deepcopy(body)
                created["id"] = self.identifier("app")
                created["aud"] = "audience-for-%s" % body["domain"]
                self.apps.append(created)
                return copy.deepcopy(created)
        if len(rest) == 3 and rest[:2] == ["access", "apps"] and method == "PUT":
            return copy.deepcopy(self._replace(self.apps, rest[2], body, ("id", "aud")))
        if len(rest) == 4 and rest[:2] == ["access", "apps"] and rest[3] == "policies":
            app_id = rest[2]
            bucket = self.policies.setdefault(app_id, [])
            if method == "GET":
                return self._page(bucket, params)
            if method == "POST":
                created = copy.deepcopy(body)
                created["id"] = self.identifier("policy")
                bucket.append(created)
                return copy.deepcopy(created)
        if len(rest) == 5 and rest[:2] == ["access", "apps"] and rest[3] == "policies":
            if method == "PUT":
                bucket = self.policies.setdefault(rest[2], [])
                return copy.deepcopy(self._replace(bucket, rest[4], body, ("id",)))
        raise AssertionError("unexpected %s /accounts/.../%s" % (method, "/".join(rest)))

    def _zone(self, method, rest, params, body):
        if rest == ["dns_records"]:
            if method == "GET":
                wanted = params.get("name")
                matching = [record for record in self.dns_records
                            if wanted is None or record["name"] == wanted]
                return self._page(matching, params)
            if method == "POST":
                created = copy.deepcopy(body)
                created["id"] = self.identifier("dns")
                created["zone_id"] = ZONE_ID
                self.dns_records.append(created)
                return copy.deepcopy(created)
        if len(rest) == 2 and rest[0] == "dns_records" and method == "PUT":
            return copy.deepcopy(self._replace(self.dns_records, rest[1], body,
                                               ("id", "zone_id")))
        raise AssertionError("unexpected %s /zones/.../%s" % (method, "/".join(rest)))

    def _replace(self, collection, identifier, body, keep):
        for position, record in enumerate(collection):
            if record["id"] == identifier:
                replacement = copy.deepcopy(body)
                for key in keep:
                    if key in record:
                        replacement[key] = record[key]
                collection[position] = replacement
                return replacement
        raise AssertionError("no such object %r" % identifier)

    def _page(self, items, params):
        """Real pagination, so paginate() is exercised rather than bypassed.

        Every item is deep-copied on the way out: the script must not be able to
        mutate the account by holding a reference to something it read, or the
        idempotency test would prove nothing."""
        page = int(params.get("page", 1))
        size = int(params.get("per_page", provision.PAGE_SIZE))
        start = (page - 1) * size
        return [copy.deepcopy(item) for item in items[start:start + size]]


def phase_of(path: str) -> str:
    """Which STEP a request belongs to, judged only by the resource it touches."""
    if "/dns_records" in path:
        return "dns records"
    if "/access/service_tokens" in path:
        return "service tokens"
    if "/alerting/" in path:
        # Before the generic /policies rule below, which this path also matches.
        return "notifications"
    if "/policies" in path:
        return "access policies"
    if "/access/apps" in path:
        return "access applications"
    if "/configurations" in path:
        return "tunnel ingress"
    if "/cfd_tunnel" in path:
        return "tunnel"
    raise AssertionError("unclassified path %r" % path)


Run = namedtuple("Run", "code report output account")


class ProvisionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def load(self, state):
        """Through the real loader, so the secret scan and validation run."""
        path = Path(self._tmp.name) / "cloudflare.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        return provision.load_state(str(path))

    def refuse(self, state) -> str:
        with self.assertRaises(provision.Refusal) as caught:
            self.load(state)
        return str(caught.exception)

    def context_for(self, state, account, apply_changes=False, check=False):
        stream = io.StringIO()
        report = provision.Report(apply_changes, False, stream=stream)
        api = provision.Api(BEARER, opener=account.open)
        return (provision.Context(api, state, report, apply_changes, check),
                report, stream)

    def reconcile(self, state, account, apply_changes=False, check=False) -> Run:
        context, report, stream = self.context_for(state, account, apply_changes, check)
        code = provision.reconcile(context)
        report.finish(code)              # what main() prints; part of the output
        return Run(code, report, stream.getvalue(), account)

    def items(self, report, status):
        return [item for step in report.steps for item in step["items"]
                if item["status"] == status]

    def detail_text(self, report, status) -> str:
        return "\n".join(item.get("detail", "") for item in self.items(report, status))


class StateRefusalTests(ProvisionTestCase):
    """Prevents: a state file that publishes a hostname with no authentication
    in front of it, or that carries a credential, ever reaching the API.

    These are refusals rather than warnings because every one of them is a
    condition an operator would only see AFTER the line saying the record was
    created."""

    def test_an_ingress_hostname_with_no_application_is_refused(self):
        """The bug: validate_state checked that every application had a route
        but not that every route had an application. One extra line in
        tunnel.ingress was therefore enough to have the tunnel carry a hostname
        to the origin and the DNS step publish a proxied CNAME for it, with no
        Access application in front — which is exactly what the tunnel -> Access
        -> DNS ordering exists to prevent."""
        state = sample_state()
        state["tunnel"]["ingress"].append(
            {"hostname": "brain-open.example.invalid", "service": "http://brain:8787"})
        message = self.refuse(state)
        self.assertIn("brain-open.example.invalid", message)
        self.assertIn("no Access application", message)

    def test_an_application_hostname_absent_from_ingress_is_refused(self):
        """The other direction, which was always enforced: an application on a
        hostname the tunnel does not route is an application in front of
        nothing, and its login page would never reach an origin."""
        state = sample_state()
        state["applications"][1]["hostname"] = "brain-typo.example.invalid"
        message = self.refuse(state)
        self.assertIn("brain-typo.example.invalid", message)
        self.assertIn("tunnel.ingress", message)

    def test_the_catch_all_rule_does_not_count_as_a_published_hostname(self):
        """The catch-all carries no hostname and fronts nothing, so demanding an
        Access application for it would refuse every state file copied from a
        working connector config."""
        state = sample_state()
        state["tunnel"]["ingress"].append({"service": provision.CATCH_ALL_SERVICE})
        loaded = self.load(state)
        self.assertEqual(provision._routed_hostnames(loaded), [CAPTURE_HOST, READ_HOST])

    def test_a_catch_all_that_is_not_last_is_refused(self):
        """cloudflared matches ingress rules in order, so a catch-all in the
        middle silently kills every rule after it."""
        state = sample_state()
        state["tunnel"]["ingress"].insert(0, {"service": provision.CATCH_ALL_SERVICE})
        self.assertIn("LAST rule", self.refuse(state))

    def test_a_hostname_less_rule_that_is_not_the_catch_all_is_refused(self):
        state = sample_state()
        state["tunnel"]["ingress"].append({"service": "http://brain:8787"})
        self.assertIn("catch-all", self.refuse(state))

    def test_a_credential_shaped_value_is_refused(self):
        """The state file is the artefact most likely to be pasted into a chat
        window while debugging, so anything credential-shaped stops the run
        before the first request."""
        state = sample_state()
        # Assembled, never written: a credential-shaped literal must not exist
        # in a tracked file even as a fixture.
        state["tunnel"]["tunnel_token"] = "e" * 64
        message = self.refuse(state)
        self.assertIn("/etc/brain/", message)

    def test_a_long_opaque_value_in_an_ordinary_key_is_refused(self):
        state = sample_state()
        state["tunnel"]["name"] = "b" * 48
        self.assertIn("long opaque value", self.refuse(state))

    def test_any_valid_service_token_is_refused(self):
        """It reads as "service tokens are allowed here" and means "every
        service token in this account, including ones minted years from now"."""
        state = sample_state()
        state["applications"][0]["policies"][1]["include"] = [
            {"any_valid_service_token": {}}]
        self.assertIn("admits every service token", self.refuse(state))

    def test_the_shipped_example_state_is_refused_until_it_is_filled_in(self):
        """Doubles as the guard on this repository: the tracked example must
        still be nothing but placeholders, so no real account id, audience,
        tunnel id or address can be sitting in it."""
        example = REPO / "deploy" / "cloudflare" / "desired-state.example.json"
        with self.assertRaises(provision.Refusal) as caught:
            provision.load_state(str(example))
        self.assertIn("placeholder", str(caught.exception))

        # The refusal alone proves nothing about THIS repository: it fires on
        # the first placeholder it meets, so a real account id sitting beside
        # one unfilled field would still raise and still pass. Check the
        # identifying fields by name instead.
        raw = json.loads(example.read_text(encoding="utf-8"))
        identifying = [
            ("account_id", raw.get("account_id")),
            ("zone_id", raw.get("zone_id")),
            ("team_domain", raw.get("team_domain")),
            ("owner_email", raw.get("owner_email")),
            ("tunnel.id", (raw.get("tunnel") or {}).get("id")),
        ]
        for app in raw.get("applications", []) or []:
            oauth = (app.get("oauth_configuration") or {})
            for uri in (oauth.get("dynamic_client_registration") or {}).get("allowed_uris", []) or []:
                identifying.append(("allowed_uris", uri))
        for name, value in identifying:
            self.assertIsNotNone(value, "%s vanished from the example" % name)
            self.assertTrue(
                provision.PLACEHOLDER_RE.search(str(value)),
                "%s in the tracked example is a real value (%r), not a placeholder"
                % (name, value))


class ConvergenceTests(ProvisionTestCase):
    """Prevents: a reconciler that cannot prove it is a no-op — the property
    nobody dares run without, and the one the module docstring claims."""

    def account_with_clients(self):
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        return account

    def test_a_fresh_account_converges_in_one_apply(self):
        """Nothing exists but the two service tokens a human had to create by
        hand. One --apply must produce the whole edge: tunnel, ingress, both
        applications, all four policies, both DNS records — and block on
        nothing, because a first run that ends blocked is a first run that
        needs a second one."""
        state = self.load(sample_state())
        account = self.account_with_clients()
        run = self.reconcile(state, account, apply_changes=True)

        self.assertEqual(run.code, provision.EXIT_OK)
        self.assertEqual(run.report.blocked, 0, run.output)
        self.assertEqual(len(account.tunnels), 1)
        self.assertEqual(sorted(app["domain"] for app in account.apps),
                         sorted([CAPTURE_HOST, READ_HOST]))
        self.assertEqual(sum(len(bucket) for bucket in account.policies.values()), 4)
        self.assertEqual(sorted(record["name"] for record in account.dns_records),
                         sorted([CAPTURE_HOST, READ_HOST]))
        for record in account.dns_records:
            self.assertEqual(record["type"], "CNAME")
            self.assertTrue(record["proxied"])
            self.assertEqual(record["content"],
                             "%s.cfargotunnel.com" % account.tunnels[0]["id"])
        # The 401 redirect is deferred until its Service Auth policy exists and
        # then enabled in the same run — not left for a second one.
        for app in account.apps:
            self.assertTrue(app["service_auth_401_redirect"])

    def test_a_second_apply_issues_no_write_requests_at_all(self):
        """The idempotency claim, proven against the account and not against the
        summary line: a reconciler that PUT every object back unchanged would
        print "converged" just as convincingly, and would rewrite the whole edge
        on every run."""
        state = self.load(sample_state())
        account = self.account_with_clients()
        self.reconcile(state, account, apply_changes=True)
        mark = len(account.requests)

        second = self.reconcile(state, account, apply_changes=True)

        self.assertEqual(second.code, provision.EXIT_OK)
        self.assertEqual(
            [(record.method, record.path) for record in account.requests[mark:]
             if record.method in FakeCloudflare.WRITE_METHODS],
            [], "the second --apply wrote to a converged account")
        self.assertEqual(second.report.changes, 0)
        self.assertEqual(second.report.blocked, 0)
        self.assertIn("converged: no changes.", second.output)

    def test_the_reconcile_order_is_tunnel_ingress_apps_policies_tokens_dns(self):
        """That order IS the safety property: DNS last because DNS completes the
        reachable path, policies straight after applications because an
        application with no policy fails closed rather than open. A refactor
        could reorder the calls in reconcile() and every other test here would
        still pass, so the order is asserted twice over.

        The request log alone is NOT enough, and the gap is not obvious: the
        service-tokens step issues no requests of its own — the token list is
        fetched lazily by the policies step, which needs a token id — so its
        position in the log is pinned inside the policies block and is
        independent of where reconcile() actually calls it. Swapping the tokens
        and DNS calls therefore leaves every request in the same place. The
        report observes each step as it begins, including the silent one, so
        the two assertions together cover all six."""
        state = self.load(sample_state())
        account = self.account_with_clients()
        run = self.reconcile(state, account, apply_changes=True)

        self.assertEqual([step["step"] for step in run.report.steps],
                         list(provision.STEPS),
                         "reconcile() ran the steps in a different order")

        first_touch = {}
        for index, record in enumerate(account.requests):
            first_touch.setdefault(phase_of(record.path), index)
        ordered = [phase for phase, _index in
                   sorted(first_touch.items(), key=lambda pair: pair[1])]
        self.assertEqual(ordered, list(provision.STEPS))

        # DNS is not merely last to start, it is a contiguous tail: no record is
        # published while any part of the account is still being built.
        dns = [index for index, record in enumerate(account.requests)
               if phase_of(record.path) == "dns records"]
        self.assertEqual(dns, list(range(len(account.requests) - len(dns),
                                         len(account.requests))))

    def test_a_missing_service_token_blocks_without_opening_anything(self):
        """A fresh account where the human has not yet created the tokens: the
        Service Auth policies cannot be written and the 401 redirect stays off,
        but the identity policy is still created, so nothing is briefly open."""
        state = self.load(sample_state())
        run = self.reconcile(state, FakeCloudflare(), apply_changes=True)

        self.assertEqual(run.code, provision.EXIT_BLOCKED)
        blocked = self.detail_text(run.report, "blocked")
        self.assertIn(CAPTURE_CLIENT_NAME, blocked)
        self.assertIn("service_auth_401_redirect", json.dumps(run.report.steps))
        for app in run.account.apps:
            self.assertFalse(app["service_auth_401_redirect"])


class DryRunTests(ProvisionTestCase):
    """Prevents: a plan that changes something, and a plan that shows a value it
    does not have. An operator reads a plan as what --apply is going to do."""

    def test_a_dry_run_against_an_empty_account_writes_nothing(self):
        state = self.load(sample_state())
        run = self.reconcile(state, FakeCloudflare(), apply_changes=False)
        self.assertEqual(run.account.writes(), [])
        self.assertGreater(run.report.changes, 0)
        self.assertEqual(run.code, provision.EXIT_OK)
        self.assertIn("Re-run with --apply.", run.output)

    def built_account(self):
        """A fully converged account: one --apply against a fresh fake."""
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        return account

    def test_a_dry_run_against_a_built_account_writes_nothing(self):
        """The other end of the range: an account with everything already in
        place must also come out of a dry run untouched."""
        state = self.load(sample_state())
        account = self.built_account()
        mark = len(account.requests)

        run = self.reconcile(state, account, apply_changes=False)

        self.assertEqual([record for record in account.requests[mark:]
                          if record.method in FakeCloudflare.WRITE_METHODS], [])
        self.assertEqual(run.report.changes, 0)

    def test_a_dry_run_that_would_create_dns_records_still_writes_nothing(self):
        """Prevents: a plan that PUBLISHES the hostname it is only supposed to
        describe.

        The other two dry-run cases never reach the record-create branch, and
        for opposite reasons: against an empty account the no-tunnel guard
        returns before the loop, and against a converged one every record
        already exists so the create arm is skipped. The only state that walks
        it is the middle one — tunnel present, records absent — which is
        exactly the state a half-finished deployment is in. Without this, a
        mutant that drops the `if not context.apply` guard in step_dns and
        POSTs the records survives the whole suite."""
        state = self.load(sample_state())
        account = self.built_account()
        # Tunnel, applications and policies stay; only the records go. DNS is
        # the last step and the only one that completes the reachable path.
        account.dns_records[:] = []
        mark = len(account.requests)

        run = self.reconcile(state, account, apply_changes=False)

        self.assertEqual(
            [(record.method, record.path) for record in account.requests[mark:]
             if record.method in FakeCloudflare.WRITE_METHODS],
            [], "a DRY RUN published DNS records")
        self.assertEqual(run.report.changes, 2)
        self.assertEqual(account.dns_records, [])

    def test_a_dry_run_with_no_tunnel_never_reports_a_none_target(self):
        """The bug: step_tunnel returns before setting the tunnel id when the
        tunnel does not exist and this is not an --apply, and step_dns
        interpolated it anyway — planning every record with the content
        "None.cfargotunnel.com". A plan that shows a wrong value is worse than
        one that shows nothing, because it is read as what --apply will write,
        and that value would blackhole every hostname."""
        state = self.load(sample_state())
        run = self.reconcile(state, FakeCloudflare(), apply_changes=False)

        self.assertNotIn("None", run.output)
        self.assertNotIn("None", json.dumps(run.report.steps))
        self.assertNotIn("None", json.dumps(run.report.private_outputs))
        # And it says so rather than saying nothing: unknown has to look unknown.
        self.assertIn("UNKNOWN", run.output)
        self.assertIn("<tunnel-id>.cfargotunnel.com", run.output)
        self.assertIn(CAPTURE_HOST, run.output)

    def test_a_dry_run_with_no_tunnel_previews_no_edge_ingress(self):
        """Same defect, same cause, other victim: the edge configuration lives
        under the tunnel id, so building that path with no id would send the
        string "None" to the API."""
        state = self.load(sample_state(config_src="cloudflare"))
        run = self.reconcile(state, FakeCloudflare(), apply_changes=False)
        for record in run.account.requests:
            self.assertNotIn("None", record.path)
        self.assertIn("not previewed", run.output)

    def test_a_dry_run_that_would_create_the_tunnel_still_plans_the_rest(self):
        """The DNS gap must not swallow the whole plan: the applications and
        policies a first --apply would create are still shown."""
        state = self.load(sample_state())
        run = self.reconcile(state, FakeCloudflare(), apply_changes=False)
        created = [item["target"] for item in self.items(run.report, "create")]
        self.assertIn("tunnel brain", created)
        self.assertIn(CAPTURE_HOST, created)


class ServiceTokenTests(ProvisionTestCase):
    """Prevents: a service token reported healthy right up to the morning it
    stops working. The seven-day alert is the only warning there is, so every
    state in which it cannot fire has to be loud."""

    def check(self, account, tokens=None):
        state = self.load(sample_state(
            service_tokens=tokens or [declared_client(CAPTURE_CLIENT_NAME, "capture")]))
        context, report, stream = self.context_for(state, account)
        provision.step_service_tokens(context)
        # The step is driven on its own, so there is no exit code to speak of;
        # what this step decides is reported, and reconcile() turns a blocked
        # item into EXIT_BLOCKED (covered in ConvergenceTests).
        return Run(provision.EXIT_OK, report, stream.getvalue(), account)

    def test_a_token_with_no_expiry_blocks(self):
        """The bug: _parse_timestamp returns None both for an absent expires_at
        and for an unreadable one, and the whole expiry block was skipped in
        that case — so the credential was reported "ok" and no rotation alert
        could ever fire for it. Unknown is not healthy."""
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME, expires_at=None)
        run = self.check(account)
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertEqual(self.items(run.report, "ok"), [])
        self.assertIn("expiry could not be determined", run.output)
        self.assertIn("no expires_at", run.output)

    def test_a_token_with_an_unparseable_expiry_blocks(self):
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME, expires_at="whenever")
        run = self.check(account)
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertIn("not a timestamp", run.output)
        self.assertIn("dashboard", run.output)

    def test_a_token_inside_seven_days_of_expiry_warns(self):
        """The design's alert. Seven days is enough to create a replacement,
        put the secret in place and re-run; the day of is not."""
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=3, hours=1)
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME,
                                   created_at=expires_at - DECLARED_LIFETIME,
                                   expires_at=expires_at)
        run = self.check(account)
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertIn("expires in 3 day(s)", run.output)

    def test_an_expired_token_blocks(self):
        now = datetime.now(timezone.utc)
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME,
                                   created_at=now - DECLARED_LIFETIME - timedelta(days=2),
                                   expires_at=now - timedelta(days=2))
        run = self.check(account)
        self.assertIn("expired on", run.output)

    def test_a_lifetime_that_does_not_match_the_declaration_blocks(self):
        """A token created for a year where the state declares ninety days is a
        long-lived credential nobody will look at again — and it is invisible
        everywhere else, because the create happens in a dashboard where the
        duration is a dropdown."""
        now = datetime.now(timezone.utc)
        created_at = now - timedelta(days=10)
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME, created_at=created_at,
                                   expires_at=created_at + timedelta(days=365))
        run = self.check(account)
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertIn("365 day(s)", run.output)
        self.assertIn(DECLARED_DURATION, run.output)

    def test_a_lifetime_longer_than_declared_blocks_without_a_created_at(self):
        """Without created_at only the remaining lifetime is knowable, and
        remaining is shorter than declared for every healthy token — so only
        the impossible direction is reported, and it still is."""
        now = datetime.now(timezone.utc)
        account = FakeCloudflare()
        record = account.add_service_client(CAPTURE_CLIENT_NAME,
                                            expires_at=now + timedelta(days=200))
        record.pop("created_at")
        run = self.check(account)
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertIn("longer than the whole", run.output)

    def test_a_healthy_token_reports_ok_with_its_expiry(self):
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        run = self.check(account)
        self.assertEqual(self.items(run.report, "blocked"), [])
        self.assertEqual(len(self.items(run.report, "ok")), 1)
        self.assertIn("expires ", run.output)

    def test_a_missing_token_names_what_to_create_and_where_the_secret_goes(self):
        run = self.check(FakeCloudflare())
        self.assertEqual(len(self.items(run.report, "blocked")), 1)
        self.assertIn("/etc/brain/service-tokens/%s" % CAPTURE_CLIENT_NAME, run.output)
        self.assertIn(DECLARED_DURATION, run.output)


class SecretHandlingTests(ProvisionTestCase):
    """Prevents: this script becoming a secret-handling tool. Its --json output
    is meant to be shareable without redaction, which is only true if no
    credential can pass through it."""

    def test_a_client_secret_is_never_printed_returned_or_written(self):
        """The account here hands back a client secret the real API would not
        return, and a tunnel create response carrying the connector credential —
        which is the one the script explicitly discards. Neither may appear in
        the human output, in the machine output, in the private outputs, or in
        anything sent back to the API."""
        state = self.load(sample_state())
        account = FakeCloudflare()
        capture_client = account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        sentinel = capture_client["client_secret"]

        run = self.reconcile(state, account, apply_changes=True)

        # Everything --json would emit, which is the output meant to be
        # shareable without redaction.
        rendered = json.dumps({"steps": run.report.steps,
                               "private": run.report.private_outputs})
        for value in (sentinel, account.connector_credential, BEARER):
            self.assertNotIn(value, run.output)
            self.assertNotIn(value, rendered)
            self.assertNotIn(value, account.bodies())
        self.assertNotIn(sentinel, str(run.report.private_outputs))

    def test_the_api_credential_travels_in_a_header_and_never_in_a_url(self):
        """A credential in a query string lands in every proxy log between here
        and Cloudflare."""
        state = self.load(sample_state())
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        self.reconcile(state, account, apply_changes=True)
        self.assertTrue(account.requests)
        for record in account.requests:
            self.assertEqual(record.authorization, "Bearer " + BEARER)
            self.assertNotIn(BEARER, record.path)
            self.assertNotIn(BEARER, json.dumps(record.params))

    def test_the_audience_is_reported_as_a_private_output_not_as_a_note(self):
        """The aud is not a secret but it is account-specific, so it belongs in
        the private block an operator copies into /etc/brain/, never in a note
        that might be pasted into a ticket."""
        state = self.load(sample_state())
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        run = self.reconcile(state, account, apply_changes=True)
        self.assertIn("aud[%s]" % CAPTURE_HOST, run.report.private_outputs)


if __name__ == "__main__":
    unittest.main()


class NotificationTests(ProvisionTestCase):
    """Prevents: alerting that exists on the dashboard and reaches nobody.

    Every failure this deployment can detect from the inside goes quiet in the
    one case where silence is worst — the VPS being gone — so these policies
    are the only signal that survives it. A policy addressed to the wrong
    person, bound to the wrong tunnel, or duplicated under one name looks
    identical to a working one until the morning it is needed."""

    def account(self):
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        return account

    def test_both_policies_are_created_and_the_second_run_writes_nothing(self):
        account = self.account()
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        created = [p["name"] for p in account.notification_policies]
        self.assertEqual(sorted(created),
                         ["brain service token expiring", "brain tunnel unhealthy"])
        before = len(account.writes())
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        self.assertEqual(len(account.writes()), before,
                         "the second run rewrote a notification policy")

    def test_the_tunnel_alert_is_bound_to_this_tunnel_and_to_real_statuses(self):
        """An unfiltered tunnel_health_event covers every tunnel in the
        account, which here would page the owner about an unrelated connector
        serving somebody's dev hostnames."""
        account = self.account()
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        policy = [p for p in account.notification_policies
                  if p["alert_type"] == "tunnel_health_event"][0]
        self.assertEqual(policy["filters"]["tunnel_id"],
                         [account.tunnels[0]["id"]])
        self.assertEqual(policy["filters"]["new_status"],
                         list(provision.DEFAULT_TUNNEL_STATUSES))
        self.assertNotIn("healthy", policy["filters"]["new_status"])

    def test_the_owner_is_a_recipient_in_the_body_that_is_sent(self):
        account = self.account()
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        for policy in account.notification_policies:
            self.assertIn({"id": OWNER}, policy["mechanisms"]["email"])

    def test_a_policy_the_owner_cannot_receive_is_refused(self):
        state = sample_state(notifications=[
            {"name": "brain tunnel unhealthy", "alert_type": "tunnel_health_event",
             "email": ["oncall@example.invalid"]}])
        self.assertIn("owner", self.refuse(state))

    def test_a_policy_addressed_to_nobody_is_refused(self):
        state = sample_state(notifications=[
            {"name": "n", "alert_type": "tunnel_health_event", "email": []}])
        self.assertIn("notify nobody", self.refuse(state))

    def test_a_hand_written_tunnel_id_is_refused(self):
        """Ids are per-account. One written down here makes the state file
        unable to rebuild this edge anywhere else, which is its only job."""
        state = sample_state(notifications=[
            {"name": "n", "alert_type": "tunnel_health_event", "email": [OWNER],
             "filters": {"tunnel_id": ["some-other-tunnel"]}}])
        self.assertIn("tunnel_id", self.refuse(state))

    def test_two_policies_with_one_name_are_refused(self):
        """They are matched by name on every run, so the second would overwrite
        the first forever while the run still reported convergence."""
        entry = {"name": "same", "alert_type": "tunnel_health_event", "email": [OWNER]}
        self.assertIn("duplicates", self.refuse(sample_state(notifications=[entry, dict(entry)])))

    def test_a_status_cloudflare_does_not_have_is_refused(self):
        state = sample_state(notifications=[
            {"name": "n", "alert_type": "tunnel_health_event", "email": [OWNER],
             "filters": {"new_status": ["unhealthy"]}}])
        message = self.refuse(state)
        self.assertIn("new_status", message)
        self.assertIn("degraded", message)

    def test_an_existing_policy_is_updated_rather_than_duplicated(self):
        account = self.account()
        account.notification_policies.append({
            "id": "pre-existing", "name": "brain tunnel unhealthy",
            "alert_type": "tunnel_health_event", "enabled": False,
            "mechanisms": {"email": [{"id": "someone-else@example.invalid"}]}})
        run = self.reconcile(self.load(sample_state()), account, apply_changes=True)
        self.assertEqual(len(account.notification_policies), 2)
        fixed = [p for p in account.notification_policies if p["id"] == "pre-existing"][0]
        self.assertTrue(fixed["enabled"])
        self.assertEqual(fixed["mechanisms"]["email"], [{"id": OWNER}])
        self.assertIn("enabled", run.output)

    def test_nothing_is_ever_deleted(self):
        account = self.account()
        account.notification_policies.append({
            "id": "someone-elses", "name": "billing", "alert_type": "billing_usage_alert",
            "enabled": True, "mechanisms": {"email": [{"id": OWNER}]}})
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        self.assertIn("someone-elses", [p["id"] for p in account.notification_policies])
        self.assertEqual([r for r in account.requests if r.method == "DELETE"], [])


class CheckModeTests(ProvisionTestCase):
    """Prevents: a scheduled check that exits 0 on the morning a credential
    lapses.

    A dry run reports a plan and says so by exiting 0 whatever it finds. That
    is right for a human reading a diff and useless for a timer, whose only
    vocabulary is the exit code — which is why the expiring-service-token check
    in this file could be scheduled, before --check existed, and would still
    never have raised anything."""

    def converged_account(self):
        account = FakeCloudflare()
        account.add_service_client(CAPTURE_CLIENT_NAME)
        account.add_service_client(READ_CLIENT_NAME)
        self.reconcile(self.load(sample_state()), account, apply_changes=True)
        return account

    def test_a_converged_account_passes(self):
        account = self.converged_account()
        run = self.reconcile(self.load(sample_state()), account, check=True)
        self.assertEqual(run.code, provision.EXIT_OK)

    def test_drift_fails_with_its_own_code(self):
        account = self.converged_account()
        account.dns_records[0]["content"] = "somewhere-else.cfargotunnel.com"
        run = self.reconcile(self.load(sample_state()), account, check=True)
        self.assertEqual(run.code, provision.EXIT_DRIFT)

    def test_an_expiring_credential_outranks_drift(self):
        """Both are true at once here. A deadline is not the same kind of
        problem as a diff and must not be reported as one."""
        account = self.converged_account()
        account.dns_records[0]["content"] = "somewhere-else.cfargotunnel.com"
        account.service_clients[0]["expires_at"] = stamp(
            datetime.now(timezone.utc) + timedelta(days=2))
        run = self.reconcile(self.load(sample_state()), account, check=True)
        self.assertEqual(run.code, provision.EXIT_BLOCKED)

    def test_a_plain_dry_run_still_reports_rather_than_judges(self):
        account = self.converged_account()
        account.dns_records[0]["content"] = "somewhere-else.cfargotunnel.com"
        run = self.reconcile(self.load(sample_state()), account)
        self.assertEqual(run.code, provision.EXIT_OK)

    def test_check_writes_nothing(self):
        account = self.converged_account()
        before = len(account.writes())
        self.reconcile(self.load(sample_state()), account, check=True)
        self.assertEqual(len(account.writes()), before)

    def test_apply_and_check_together_are_refused(self):
        """And refused for THAT reason. Both refusals share an exit code, so
        asserting the code alone would pass on the missing-token path and prove
        nothing."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = provision.main(["--state", "/nonexistent", "--apply", "--check"])
        self.assertEqual(code, provision.EXIT_REFUSED)
        self.assertIn("Pick one", stderr.getvalue())
