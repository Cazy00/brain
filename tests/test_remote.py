"""End-to-end tests for the remote brain: Access assertions, HTTP, and the boundary.

These drive a real HTTP server over a real socket against a real sandbox brain,
with real RS256 assertions signed by the throwaway key in test_rs256. Nothing is
mocked except the JWKS fetch, because everything interesting about this system
is what happens when a request is ALMOST authorized.

The read-only boundary gets the most attention. It is the one thing in this
project that, if wrong, hands a remote caller the ability to write to the
owner's knowledge repository — and it is deliberately enforced twice, so both
enforcement points are tested separately: `tools/list` must not ADVERTISE
brain_capture, and a hand-built `tools/call` for it must be REFUSED. A test
that only checked the advertised list would pass against a server that ran
whatever arrived.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from brainlib import access, capture, eventlog, httpmcp, mcpcore, rs256  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_rs256 import (MODULUS, FORGE_EXPONENT, PUBLIC_EXPONENT, K,  # noqa: E402
                        b64u, int_b64u)

ENGINE = Path(__file__).resolve().parent.parent
TEAM = "example-team.cloudflareaccess.com"
ISSUER = "https://" + TEAM
AUD_READ = "a" * 64
AUD_WRITE = "b" * 64
OWNER = "owner@example.invalid"
SERVICE_CLIENT = "0123456789abcdef.access"
PRINCIPAL_KEY = b"test-principal-key-that-is-long-enough-to-pass"
KID = "test-kid"


def jwks_document():
    return {"keys": [{"kty": "RSA", "alg": "RS256", "use": "sig", "kid": KID,
                      "n": int_b64u(MODULUS), "e": int_b64u(PUBLIC_EXPONENT)}]}


def sign_assertion(claims: dict, kid: str = KID, alg: str = "RS256") -> str:
    """Mint an assertion the way Cloudflare would, so tests can bend one claim."""
    import hashlib
    header = b64u(json.dumps({"alg": alg, "kid": kid}).encode("utf-8"))
    payload = b64u(json.dumps(claims).encode("utf-8"))
    signing_input = ("%s.%s" % (header, payload)).encode("ascii")
    digest = rs256.SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(signing_input).digest()
    block = b"\x00\x01" + b"\xff" * (K - len(digest) - 3) + b"\x00" + digest
    signature = pow(int.from_bytes(block, "big"), FORGE_EXPONENT, MODULUS).to_bytes(K, "big")
    return "%s.%s.%s" % (header, payload, b64u(signature))


def owner_claims(aud=AUD_WRITE, **over):
    now = int(time.time())
    claims = {"aud": [aud], "email": OWNER, "iss": ISSUER, "type": "app",
              "iat": now - 10, "nbf": now - 10, "exp": now + 900,
              "sub": "subject-1", "identity_nonce": "n", "country": "OM"}
    claims.update(over)
    return claims


def service_claims(aud=AUD_READ, **over):
    now = int(time.time())
    claims = {"aud": [aud], "iss": ISSUER, "type": "app", "iat": now - 10,
              "exp": now + 900, "sub": "", "common_name": SERVICE_CLIENT}
    claims.update(over)
    return claims


def make_data_root(parent: Path) -> Path:
    """A private data repository, the way production has one: notes and git, no engine."""
    root = parent / "data"
    (root / "knowledge" / "inbox").mkdir(parents=True)
    (root / "knowledge" / "reference").mkdir(parents=True)
    (root / ".gitignore").write_text(".cache/\n", encoding="utf-8")
    (root / "knowledge" / "index.md").write_text(
        "# index\n\nRoute map.\n", encoding="utf-8")
    (root / "knowledge" / "topics.yaml").write_text("infra: infrastructure\n",
                                                    encoding="utf-8")
    (root / "knowledge" / "reference" / "tunnel-facts.md").write_text(
        "---\nid: tunnel-facts\nkind: reference\ntitle: Tunnel facts\n"
        "topics: [infra]\naliases: [cloudflare tunnel, connector]\n"
        "created: 2026-08-23\nstatus: current\n---\n\n"
        "A cloudflared connector dials out and opens no inbound port.\n",
        encoding="utf-8")
    for args in (["init", "-q", "-b", "main"],
                 ["config", "user.email", "sandbox@example.invalid"],
                 ["config", "user.name", "Sandbox"],
                 ["config", "core.hooksPath", ""],
                 ["add", "-A"],
                 ["commit", "-q", "-m", "sandbox"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    return root


class RemoteBrain(object):
    """A running brain: sandbox data, a live socket, and a client that talks to it."""

    def __init__(self, origin_policy="observe", git_push=False, limits=None):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data_root = make_data_root(base)
        self.state_dir = base / "state"
        self.state_dir.mkdir()
        self._saved_env = {k: os.environ.get(k)
                           for k in ("BRAIN_DATA_ROOT", "BRAIN_STATE_DIR")}
        os.environ["BRAIN_DATA_ROOT"] = str(self.data_root)
        os.environ["BRAIN_STATE_DIR"] = str(self.state_dir)

        jwks = access.JwksCache(TEAM, opener=lambda url: (
            json.dumps(jwks_document()).encode("utf-8"), 300))
        self.verifier = access.Verifier(
            team_domain=TEAM,
            endpoints=[access.Endpoint("brain-read.test", AUD_READ, "read"),
                       access.Endpoint("brain.test", AUD_WRITE, "capture")],
            owner_email=OWNER,
            service_principals={SERVICE_CLIENT: "read"},
            principal_key=PRINCIPAL_KEY, jwks=jwks)
        self.log = eventlog.EventLog(self.state_dir)
        self.queue = capture.BackupQueue(self.data_root, log=self.log, enabled=git_push)
        self.capturer = capture.Capturer(
            brain_bin=str(ENGINE / "bin" / "brain"), data_root=self.data_root,
            ledger=capture.Ledger(self.state_dir, PRINCIPAL_KEY),
            queue=self.queue, log=self.log)
        self.service = httpmcp.Service(
            verifier=self.verifier, capturer=self.capturer, log=self.log,
            limiter=httpmcp.Limiter(limits) if limits else None,
            origin_policy=origin_policy, readiness=lambda: (True, ""))
        self.server = httpmcp.make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def post(self, body, host="brain.test", assertion=None, headers=None,
             raw=None, method="POST"):
        """One HTTP round trip. Returns (status, headers, parsed-or-raw body)."""
        url = "http://127.0.0.1:%d/mcp" % self.port
        payload = raw if raw is not None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method=method)
        request.add_header("Content-Type", "application/json")
        request.add_header("Host", host)
        if assertion:
            request.add_header("Cf-Access-Jwt-Assertion", assertion)
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = response.read()
                return response.status, dict(response.headers), _maybe_json(data)
        except urllib.error.HTTPError as err:
            data = err.read()
            return err.code, dict(err.headers), _maybe_json(data)

    def get(self, path):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.status, _maybe_json(response.read())
        except urllib.error.HTTPError as err:
            return err.code, _maybe_json(err.read())

    def rpc(self, method, params=None, host="brain.test", assertion=None, msg_id=1,
            headers=None):
        body = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            body["params"] = params
        extra = {"MCP-Protocol-Version": "2025-11-25"} if method != "initialize" else {}
        extra.update(headers or {})
        return self.post(body, host=host,
                         assertion=assertion or sign_assertion(
                             owner_claims(AUD_WRITE if host == "brain.test" else AUD_READ)),
                         headers=extra)

    def call(self, name, arguments, host="brain.test", assertion=None):
        status, _headers, body = self.rpc(
            "tools/call", {"name": name, "arguments": arguments},
            host=host, assertion=assertion)
        return status, body

    def text_of(self, body):
        return body["result"]["content"][0]["text"]


def _maybe_json(data: bytes):
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except ValueError:
        return data


class RemoteTestCase(unittest.TestCase):
    ORIGIN_POLICY = "observe"
    GIT_PUSH = False
    LIMITS = None

    def setUp(self):
        self.brain = RemoteBrain(origin_policy=self.ORIGIN_POLICY,
                                 git_push=self.GIT_PUSH, limits=self.LIMITS)

    def tearDown(self):
        self.brain.close()


class HandshakeTests(RemoteTestCase):
    """The legacy era — what every client in the qualification matrix speaks today."""

    def test_initialize_agrees_a_version_and_names_the_server(self):
        status, _h, body = self.brain.rpc("initialize", {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "TestClient", "version": "9.9"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(body["result"]["serverInfo"]["name"], "brain")
        self.assertIn("tools", body["result"]["capabilities"])

    def test_an_unsupported_version_is_countered_not_rejected(self):
        """The spec says answer with a version you DO support and let the client
        decide. An error here would strand a client that could have worked."""
        status, _h, body = self.brain.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(status, 200)
        self.assertIn(body["result"]["protocolVersion"], httpmcp.LEGACY_VERSIONS)

    def test_a_notification_gets_202_and_no_body(self):
        """A JSON-RPC response to a notification is a protocol violation, and
        some clients treat one as fatal."""
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 202)
        self.assertIsNone(body)

    def test_ping_answers_an_empty_result(self):
        status, _h, body = self.brain.rpc("ping")
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {})

    def test_an_unknown_method_is_a_jsonrpc_error_not_an_http_error(self):
        status, _h, body = self.brain.rpc("resources/list")
        self.assertEqual(status, 200)
        self.assertEqual(body["error"]["code"], httpmcp.METHOD_NOT_FOUND)

    def test_an_unsupported_protocol_header_is_refused(self):
        status, _h, _b = self.brain.rpc("tools/list",
                                        headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)


class CapabilityBoundaryTests(RemoteTestCase):
    """The read/capture split, which is the whole reason there are two hostnames."""

    def test_the_read_only_endpoint_advertises_exactly_four_tools(self):
        status, _h, body = self.brain.rpc("tools/list", host="brain-read.test")
        self.assertEqual(status, 200)
        names = sorted(tool["name"] for tool in body["result"]["tools"])
        self.assertEqual(names, ["brain_links", "brain_read", "brain_recent",
                                 "brain_search"])

    def test_the_capture_endpoint_advertises_exactly_five_tools(self):
        status, _h, body = self.brain.rpc("tools/list", host="brain.test")
        self.assertEqual(status, 200)
        names = sorted(tool["name"] for tool in body["result"]["tools"])
        self.assertEqual(names, ["brain_capture", "brain_links", "brain_read",
                                 "brain_recent", "brain_search"])

    def test_a_hand_built_capture_call_on_the_read_endpoint_is_refused(self):
        """The security half of the boundary. Hiding the tool from tools/list is
        usability — a client can still construct the call, and this is what
        stops it. A server that only filtered the list would pass the two tests
        above and still be writable by anyone who read this file."""
        status, body = self.brain.call("brain_capture", {"text": "should never land"},
                                       host="brain-read.test")
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("not available on this endpoint",
                      self.brain.text_of(body))
        inbox = list((self.brain.data_root / "knowledge" / "inbox").glob("*.md"))
        self.assertEqual(inbox, [], "the read-only endpoint wrote a note to disk")

    def test_the_read_only_tool_descriptions_never_mention_capturing(self):
        status, _h, body = self.brain.rpc("tools/list", host="brain-read.test")
        blob = json.dumps(body["result"]["tools"]).lower()
        for vendor in ("claude", "anthropic", "openai", "gemini", "cursor", "cloudflare"):
            self.assertNotIn(vendor, blob,
                             "a vendor name leaked into the tool contract")


class AssertionTests(RemoteTestCase):
    """Every way an assertion can be almost right."""

    def refused(self, assertion, host="brain.test"):
        status, _h, _b = self.brain.rpc("tools/list", host=host, assertion=assertion)
        self.assertEqual(status, 403)

    def test_a_valid_owner_assertion_is_accepted(self):
        status, _h, _b = self.brain.rpc("tools/list",
                                        assertion=sign_assertion(owner_claims()))
        self.assertEqual(status, 200)

    def test_no_assertion_is_refused(self):
        status, _h, _b = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, assertion=None)
        self.assertEqual(status, 403)

    def test_the_origin_never_emits_a_401_or_a_challenge(self):
        """Cloudflare Access owns the OAuth challenge for these hostnames. A 401
        from here would be a challenge with no WWW-Authenticate behind it, which
        RFC 6750 forbids and no client can act on."""
        status, headers, _b = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, assertion=None)
        self.assertEqual(status, 403)
        self.assertNotIn("www-authenticate", {k.lower() for k in headers})

    def test_the_wrong_audience_is_refused(self):
        """An assertion minted for the read-only application must not authorize
        the capture endpoint. This pairing IS the read/write boundary."""
        self.refused(sign_assertion(owner_claims(aud=AUD_READ)), host="brain.test")
        self.refused(sign_assertion(owner_claims(aud=AUD_WRITE)), host="brain-read.test")

    def test_the_wrong_issuer_is_refused(self):
        self.refused(sign_assertion(owner_claims(iss="https://evil.cloudflareaccess.com")))

    def test_an_org_token_is_refused(self):
        """A genuine signature by the same key over a different scope."""
        self.refused(sign_assertion(owner_claims(type="org")))

    def test_an_expired_assertion_is_refused(self):
        self.refused(sign_assertion(owner_claims(exp=int(time.time()) - 3600)))

    def test_an_assertion_valid_in_the_future_is_refused(self):
        now = int(time.time())
        self.refused(sign_assertion(owner_claims(nbf=now + 3600, iat=now + 3600)))

    def test_another_persons_email_is_refused(self):
        self.refused(sign_assertion(owner_claims(email="someone.else@example.invalid")))

    def test_an_unknown_signing_key_is_refused(self):
        self.refused(sign_assertion(owner_claims(), kid="a-key-we-never-published"))

    def test_a_tampered_payload_is_refused(self):
        good = sign_assertion(owner_claims())
        header, payload, signature = good.split(".")
        forged = b64u(json.dumps(owner_claims(email="attacker@example.invalid"))
                      .encode("utf-8"))
        self.refused("%s.%s.%s" % (header, forged, signature))

    def test_an_unsigned_alg_none_assertion_is_refused(self):
        self.refused(sign_assertion(owner_claims(), alg="none"))

    def test_an_unknown_host_is_refused(self):
        self.refused(sign_assertion(owner_claims()), host="not-configured.test")

    def test_a_service_token_is_accepted_on_its_own_profile(self):
        status, _h, body = self.brain.rpc(
            "tools/list", host="brain-read.test",
            assertion=sign_assertion(service_claims(AUD_READ)))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["result"]["tools"]), 4)

    def test_a_service_token_is_refused_on_a_profile_it_was_not_granted(self):
        """The origin refuses independently of Access policy. Two things have to
        be wrong at once for a read-only client to reach the write endpoint."""
        self.refused(sign_assertion(service_claims(AUD_WRITE)), host="brain.test")

    def test_an_unknown_service_client_is_refused(self):
        self.refused(sign_assertion(service_claims(AUD_READ,
                                                   common_name="stranger.access")),
                     host="brain-read.test")

    def test_a_half_shaped_service_assertion_is_refused(self):
        """common_name with a non-empty sub, or with an email, is not a shape
        Access documents — and an unrecognised shape is refused rather than
        guessed at."""
        self.refused(sign_assertion(service_claims(AUD_READ, sub="not-empty")),
                     host="brain-read.test")
        self.refused(sign_assertion(service_claims(AUD_READ, email=OWNER)),
                     host="brain-read.test")


class TransportTests(RemoteTestCase):
    def test_get_on_the_endpoint_is_405(self):
        status, _body = self.brain.get("/mcp")
        self.assertEqual(status, 405)

    def test_delete_on_the_endpoint_is_405(self):
        status, _h, _b = self.brain.post(None, raw=b"", method="DELETE",
                                         assertion=sign_assertion(owner_claims()))
        self.assertEqual(status, 405)

    def test_a_body_over_one_mebibyte_is_413(self):
        oversized = b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"'
        oversized += b"x" * (1024 * 1024 + 64) + b'"}'
        status, _h, _b = self.brain.post(None, raw=oversized,
                                         assertion=sign_assertion(owner_claims()))
        self.assertEqual(status, 413)

    def test_a_non_json_content_type_is_415(self):
        status, _h, _b = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            assertion=sign_assertion(owner_claims()),
            headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_unparseable_json_is_a_parse_error_and_the_server_survives(self):
        status, _h, body = self.brain.post(None, raw=b"{not json",
                                           assertion=sign_assertion(owner_claims()))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.PARSE_ERROR)
        status, _h, _b = self.brain.rpc("ping")
        self.assertEqual(status, 200, "one bad request killed the server")

    def test_deeply_nested_json_does_not_kill_the_server(self):
        """json.loads raises RecursionError, not JSONDecodeError, on this — which
        is precisely why the parse guard is deliberately broad."""
        payload = (b"[" * 100_000) + (b"]" * 100_000)
        status, _h, _b = self.brain.post(None, raw=payload,
                                         assertion=sign_assertion(owner_claims()))
        self.assertIn(status, (400, 413))
        status, _h, _b = self.brain.rpc("ping")
        self.assertEqual(status, 200)

    def test_a_batch_is_refused(self):
        status, _h, body = self.brain.post(
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.INVALID_REQUEST)

    def test_health_and_readiness_need_no_assertion_and_leak_nothing(self):
        for path in ("/healthz", "/readyz"):
            status, body = self.brain.get(path)
            self.assertEqual(status, 200)
            blob = json.dumps(body)
            self.assertNotIn(str(self.brain.data_root), blob)
            self.assertNotIn(OWNER, blob)

    def test_the_server_never_advertises_its_python_version(self):
        _status, headers, _b = self.brain.rpc("ping")
        self.assertNotIn("Python", headers.get("Server", ""))


class StrictOriginTests(RemoteTestCase):
    ORIGIN_POLICY = "strict"

    def test_an_unlisted_origin_is_403(self):
        status, _h, _b = self.brain.rpc("ping", headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_no_origin_header_is_fine(self):
        status, _h, _b = self.brain.rpc("ping")
        self.assertEqual(status, 200)


class RetrievalTests(RemoteTestCase):
    """The trust signals are the contract, and they must arrive verbatim."""

    def test_search_returns_the_note_and_the_match_mode(self):
        status, body = self.brain.call("brain_search", {"query": "connector"})
        self.assertEqual(status, 200)
        text = self.brain.text_of(body)
        self.assertIn("tunnel-facts", text)
        self.assertIn("[match mode:", text)

    def test_read_returns_the_note_body(self):
        status, body = self.brain.call("brain_read", {"id_or_path": "tunnel-facts"})
        self.assertEqual(status, 200)
        self.assertIn("opens no inbound port", self.brain.text_of(body))

    def test_a_path_outside_knowledge_is_refused(self):
        for attempt in ("../../etc/passwd", "/etc/passwd",
                        "knowledge/../../.git/config", "knowledge/../.gitignore"):
            status, body = self.brain.call("brain_read", {"id_or_path": attempt})
            self.assertEqual(status, 200)
            self.assertTrue(body["result"]["isError"],
                            "containment let %r through" % attempt)

    def test_recent_and_links_answer(self):
        status, body = self.brain.call("brain_recent", {"days": 30})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])
        status, body = self.brain.call("brain_links", {"id_or_path": "tunnel-facts"})
        self.assertEqual(status, 200)
        self.assertIn("links for tunnel-facts", self.brain.text_of(body))

    def test_a_query_over_the_remote_cap_is_refused_as_a_tool_error(self):
        status, body = self.brain.call("brain_search", {"query": "x" * 2049})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("the cap is 2048", self.brain.text_of(body))

    def test_an_out_of_range_limit_is_refused(self):
        status, body = self.brain.call("brain_search", {"query": "connector", "limit": 500})
        self.assertTrue(body["result"]["isError"])


class CaptureTests(RemoteTestCase):
    def inbox(self):
        return sorted((self.brain.data_root / "knowledge" / "inbox").glob("*.md"))

    def test_a_capture_commits_and_reports_provisional(self):
        status, body = self.brain.call(
            "brain_capture", {"text": "The tunnel connector dials out, verified 2026-08-23."})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])
        text = self.brain.text_of(body)
        self.assertIn("PROVISIONAL", text)
        self.assertIn("knowledge/inbox/", text)
        self.assertEqual(len(self.inbox()), 1)
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.brain.data_root,
                             capture_output=True, text=True)
        self.assertIn("capture:", log.stdout)

    def test_a_captured_note_is_findable_under_scope_all_and_tagged_provisional(self):
        self.brain.call("brain_capture",
                        {"text": "Aardvark reconciliation is scheduled for 2026-09-01."})
        status, body = self.brain.call("brain_search",
                                       {"query": "aardvark", "scope": "all"})
        text = self.brain.text_of(body)
        self.assertIn("aardvark", text.lower())
        self.assertIn("[provisional — unconsolidated]", text)

    def test_a_credential_is_refused_without_touching_disk(self):
        # Assembled at runtime, never written out. A credential-shaped literal
        # here would be caught by the very gate this test exists to prove —
        # first by `brain lint`, which offers a `lint:allow-secret` pragma for
        # exactly this case, and then by gitleaks, which does not honour it. The
        # deeper reason to build it rather than exempt it: a repository whose
        # rule is "no credentials, ever" should not contain a list of the places
        # that rule is switched off, because that list is where a real one
        # eventually gets added.
        looks_like_a_token = "ghp_" + "abcdefghijklmnopqrstuvwxyz0123456789"
        status, body = self.brain.call("brain_capture", {
            "text": "the deploy key is " + looks_like_a_token})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("REFUSED", self.brain.text_of(body))
        self.assertEqual(self.inbox(), [], "a credential reached the disk")

    def test_a_retry_with_the_same_request_id_returns_the_same_note(self):
        first = self.brain.call("brain_capture", {
            "text": "Idempotency matters when the connection drops.",
            "client_request_id": "req-1"})[1]
        second = self.brain.call("brain_capture", {
            "text": "Idempotency matters when the connection drops.",
            "client_request_id": "req-1"})[1]
        self.assertEqual(len(self.inbox()), 1)
        self.assertIn("ALREADY SAVED", self.brain.text_of(second))
        # Same note, not a renamed one: a retry that returns a different id has
        # not been deduplicated.
        self.assertEqual(_note_line(self.brain.text_of(first)),
                         _note_line(self.brain.text_of(second)))

    def test_identical_text_is_deduplicated_without_a_request_id(self):
        self.brain.call("brain_capture", {"text": "The same sentence, twice."})
        second = self.brain.call("brain_capture", {"text": "The same sentence, twice."})[1]
        self.assertEqual(len(self.inbox()), 1)
        self.assertIn("ALREADY SAVED", self.brain.text_of(second))

    def test_allow_duplicate_saves_it_anyway(self):
        self.brain.call("brain_capture", {"text": "Deliberately said twice."})
        self.brain.call("brain_capture", {"text": "Deliberately said twice.",
                                          "allow_duplicate": True})
        self.assertEqual(len(self.inbox()), 2)

    def test_the_ledger_never_stores_the_capture_text(self):
        secret_ish = "Zanzibar pineapple ledger canary 2026-08-23"
        self.brain.call("brain_capture", {"text": secret_ish})
        blob = (self.brain.state_dir / "capture-ledger.db").read_bytes()
        self.assertNotIn(b"Zanzibar", blob)
        self.assertNotIn(b"pineapple", blob)

    def test_the_event_log_records_the_mutation_and_no_content(self):
        self.brain.call("brain_capture", {"text": "Rutabaga is the log canary."})
        audit = (self.brain.state_dir / "mutations.jsonl").read_text(encoding="utf-8")
        self.assertIn("capture_accepted", audit)
        self.assertNotIn("Rutabaga", audit)
        self.assertNotIn(OWNER, audit)

    def test_capture_reports_that_backup_is_off_when_there_is_no_remote(self):
        body = self.brain.call("brain_capture", {"text": "No remote configured here."})[1]
        self.assertIn("backup:", self.brain.text_of(body))

    def test_concurrent_captures_all_land_exactly_once(self):
        """The repository lock is the only thing between these and a corrupt
        git index. Distinct text, so content dedup cannot mask a lost note."""
        results = []
        lock = threading.Lock()

        def one(n):
            body = self.brain.call(
                "brain_capture", {"text": "Concurrent capture number %d, 2026-08-23." % n})[1]
            with lock:
                results.append(self.brain.text_of(body))

        threads = [threading.Thread(target=one, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)
        self.assertEqual(len(results), 6)
        self.assertEqual(len(self.inbox()), 6, "a concurrent capture was lost")
        status = subprocess.run(["git", "status", "--porcelain"],
                                cwd=self.brain.data_root, capture_output=True, text=True)
        self.assertEqual(status.stdout.strip(), "",
                         "concurrent captures left the tree dirty")


class MaintenanceTests(RemoteTestCase):
    def test_writes_fail_closed_and_reads_keep_working(self):
        self.brain.service.maintenance = "restore drill"
        status, _h, _b = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "brain_capture", "arguments": {"text": "nope"}}},
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 503)
        status, body = self.brain.call("brain_search", {"query": "connector"})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])


class RateLimitTests(RemoteTestCase):
    LIMITS = {"read": (3, 60.0), "capture": (1, 60.0), "protocol": (100, 60.0)}

    def test_reads_are_limited_per_principal_with_a_retry_hint(self):
        for _ in range(3):
            self.assertEqual(self.brain.call("brain_search", {"query": "connector"})[0], 200)
        status, _h, _b = self.brain.rpc(
            "tools/call", {"name": "brain_search", "arguments": {"query": "connector"}})
        self.assertEqual(status, 429)

    def test_the_capture_budget_is_separate_from_the_read_budget(self):
        """Initialization and token-refresh chatter must not be able to consume
        the budget a capture needs."""
        for _ in range(20):
            self.brain.rpc("tools/list")
        status, body = self.brain.call("brain_capture", {"text": "One allowed capture."})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])


class ModernEraTests(RemoteTestCase):
    """The 2026-07-28 revision, served from the same endpoint as the old one."""

    def modern(self, method, params=None, name=None, version="2026-07-28",
               caps={}, headers=None):
        params = dict(params or {})
        meta = dict(params.get("_meta") or {})
        meta[httpmcp.META_VERSION] = version
        if caps is not None:
            meta[httpmcp.META_CLIENT_CAPS] = caps
        params["_meta"] = meta
        sent = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if name is not None:
            sent["Mcp-Name"] = name
        sent.update(headers or {})
        return self.brain.post({"jsonrpc": "2.0", "id": 1, "method": method,
                                "params": params},
                               assertion=sign_assertion(owner_claims()), headers=sent)

    def test_a_request_without_client_capabilities_is_invalid_params(self):
        """clientCapabilities is a REQUIRED per-request _meta field, and the
        revision is explicit that a missing required field is -32602, not the
        -32020 a header check would have produced."""
        status, _h, body = self.modern("tools/list", caps=None)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.INVALID_PARAMS)
        self.assertIn("clientCapabilities", body["error"]["message"])

    def test_an_empty_capabilities_object_is_valid(self):
        """A client that can do nothing extra still declares that. {} is a
        statement, not an omission."""
        status, _h, body = self.modern("tools/list", caps={})
        self.assertEqual(status, 200)
        self.assertEqual(len(body["result"]["tools"]), 5)

    def test_a_missing_protocol_version_in_meta_is_invalid_params(self):
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {"_meta": {httpmcp.META_CLIENT_CAPS: {}}}},
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"})
        # No modern _meta version and a modern header: still routed modern by
        # shape, and refused for the field it is actually missing.
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.INVALID_PARAMS)

    def test_a_future_version_gets_the_supported_list_not_a_legacy_fallback(self):
        """The bug this replaced was silent and expensive: era selection keyed on
        whether the version was SUPPORTED, so a client naming a future revision
        fell through to the legacy path and got a bare 400. The transport tells
        a dual-era client that a 400 without a recognised modern error means
        'fall back to initialize' — so a client ahead of this server would have
        downgraded instead of being told what to retry with."""
        status, _h, body = self.modern("tools/list", version="2027-01-01")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertIn("2026-07-28", body["error"]["data"]["supported"])
        self.assertEqual(body["error"]["data"]["requested"], "2027-01-01")

    def test_tools_call_without_an_mcp_name_header_is_refused(self):
        """Absence and mismatch are different failures. Coalescing a missing
        header to "" made a call with neither a header nor a body name compare
        equal and pass — the exact request an intermediary routing on the
        header cannot see."""
        status, _h, body = self.modern(
            "tools/call", {"name": "brain_search", "arguments": {"query": "x"}},
            name=None)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.HEADER_MISMATCH)
        self.assertIn("Mcp-Name", body["error"]["message"])

    def test_cacheable_results_are_scoped_private(self):
        """Caching hints are required on these two, and `private` is the
        security half: both results VARY BY PRINCIPAL — the read-only profile
        is not shown brain_capture — and a shared cache is documented as
        possibly serving one caller's result to another even on an
        authenticated endpoint. `public` here would hand a read-only caller the
        capture endpoint's tool list."""
        for method in ("server/discover", "tools/list"):
            status, _h, body = self.modern(method)
            self.assertEqual(status, 200, method)
            result = body["result"]
            self.assertIsInstance(result.get("ttlMs"), int, method)
            self.assertGreaterEqual(result["ttlMs"], 0, method)
            self.assertEqual(result.get("cacheScope"), "private", method)

    def test_tools_call_results_carry_no_caching_hints(self):
        """Only the listed operations are cacheable. A tool CALL is not one of
        them, and caching one would be caching an action."""
        status, _h, body = self.modern(
            "tools/call", {"name": "brain_search", "arguments": {"query": "connector"}},
            name="brain_search")
        self.assertEqual(status, 200)
        self.assertNotIn("ttlMs", body["result"])
        self.assertNotIn("cacheScope", body["result"])

    def test_every_modern_result_carries_resultType_and_serverInfo(self):
        """Prevents the bug a real client found on the first connection.

        Claude Code negotiated 2026-07-28, authenticated, and then refused
        tools/list with "missing required resultType". The revision makes
        resultType mandatory on EVERY result — it is the discriminator between a
        finished answer and an `input_required` one — and the absent-means-
        complete rule is a bridge for earlier-revision servers only, so a modern
        server that omits it is simply invalid. It had been added to
        server/discover and nowhere else, which is exactly the shape of mistake
        a per-method assertion misses and a sweep catches."""
        cases = [
            ("server/discover", None, None),
            ("ping", None, None),
            ("tools/list", None, None),
            ("tools/call", {"name": "brain_search", "arguments": {"query": "connector"}},
             "brain_search"),
        ]
        for method, params, name in cases:
            status, _h, body = self.modern(method, params, name=name)
            self.assertEqual(status, 200, method)
            result = body["result"]
            self.assertEqual(result.get("resultType"), "complete",
                             "%s result has no resultType" % method)
            self.assertEqual(
                result.get("_meta", {}).get(httpmcp.META_SERVER_INFO, {}).get("name"),
                "brain", "%s result does not identify the server" % method)

    def test_a_legacy_result_does_not_carry_resultType(self):
        """The mirror, and it matters as much: resultType did not exist before
        2026-07-28. Adding it to the handshake era would be inventing a field in
        a revision that never defined one, and the eras must not bleed."""
        for method in ("ping", "tools/list"):
            status, _h, body = self.brain.rpc(method)
            self.assertEqual(status, 200)
            self.assertNotIn("resultType", body["result"], method)
            self.assertNotIn("_meta", body["result"], method)

    def test_server_discover_reports_every_supported_version(self):
        status, _h, body = self.modern("server/discover")
        self.assertEqual(status, 200)
        result = body["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("2025-11-25", result["supportedVersions"])
        self.assertEqual(result["_meta"][httpmcp.META_SERVER_INFO]["name"], "brain")

    def test_tools_list_works_in_the_modern_era(self):
        status, _h, body = self.modern("tools/list")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["result"]["tools"]), 5)

    def test_a_tools_call_needs_a_matching_mcp_name_header(self):
        status, _h, body = self.modern(
            "tools/call", {"name": "brain_search", "arguments": {"query": "connector"}},
            name="brain_search")
        self.assertEqual(status, 200)
        self.assertIn("tunnel-facts", body["result"]["content"][0]["text"])

    def test_a_mismatched_mcp_name_header_is_a_header_mismatch(self):
        status, _h, body = self.modern(
            "tools/call", {"name": "brain_search", "arguments": {"query": "connector"}},
            name="brain_read")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.HEADER_MISMATCH)

    def test_a_missing_mcp_method_header_is_a_header_mismatch(self):
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {"_meta": {httpmcp.META_VERSION: "2026-07-28",
                                  httpmcp.META_CLIENT_CAPS: {}}}},
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2026-07-28"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.HEADER_MISMATCH)

    def test_a_header_that_disagrees_with_the_body_is_refused(self):
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list",
             "params": {"_meta": {httpmcp.META_VERSION: "2026-07-28",
                                  httpmcp.META_CLIENT_CAPS: {}}}},
            assertion=sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list"})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], httpmcp.HEADER_MISMATCH)

    def test_an_unknown_method_is_404_with_a_jsonrpc_error(self):
        """The status is what lets a dual-era client tell a modern server from a
        legacy one, so it is 404 here and 200 in the handshake era."""
        status, _h, body = self.modern("resources/read")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], httpmcp.METHOD_NOT_FOUND)

    def test_the_read_only_boundary_holds_in_the_modern_era_too(self):
        params = {"name": "brain_capture", "arguments": {"text": "nope"},
                  "_meta": {httpmcp.META_VERSION: "2026-07-28",
                            httpmcp.META_CLIENT_CAPS: {}}}
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params},
            host="brain-read.test",
            assertion=sign_assertion(owner_claims(aud=AUD_READ)),
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                     "Mcp-Name": "brain_capture"})
        self.assertEqual(status, 200)
        self.assertTrue(body["result"]["isError"])


class FakeGit(object):
    """Stands in for git so a push outage can be staged without one.

    Records every invocation, so a test can assert what was ATTEMPTED and not
    merely what was reported."""

    class Done(object):
        def __init__(self, returncode, stdout=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ""

    def __init__(self, unpushed=1, push_ok=True, upstream=True):
        self.unpushed = unpushed
        self.push_ok = push_ok
        self.upstream = upstream
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["rev-list", "--count"]:
            if not self.upstream:
                return self.Done(128)
            return self.Done(0, "%d\n" % self.unpushed)
        if args[0] == "push":
            if self.push_ok:
                self.unpushed = 0
                return self.Done(0)
            return self.Done(1)
        raise AssertionError("unexpected git call: %r" % (args,))


class BackupQueueTests(unittest.TestCase):
    """The third durability layer, and the contract that makes a capture
    ACCEPTED before it is BACKED UP.

    A push failure must never un-accept a committed capture: the spec says the
    result reports backup_pending, the queue retries, and an alert fires after
    fifteen minutes. None of that was covered — the mechanism existed and its
    failure path had never been executed, which is the one path that only runs
    on the day something is already wrong."""

    def setUp(self):
        self.now = 1_000_000.0
        self.tmp = tempfile.TemporaryDirectory()
        self.log = eventlog.EventLog(self.tmp.name, clock=lambda: self.now)

    def tearDown(self):
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def queue(self, git, enabled=True):
        return capture.BackupQueue(self.tmp.name, log=self.log, enabled=enabled,
                                   clock=lambda: self.now, runner=git,
                                   sleeper=lambda _s: None)

    def test_a_successful_push_reports_pushed(self):
        queue = self.queue(FakeGit(unpushed=2, push_ok=True))
        self.assertTrue(queue.push_once())
        self.assertEqual(queue.state(), "pushed")

    def test_nothing_to_push_is_success_and_does_not_call_push(self):
        git = FakeGit(unpushed=0)
        queue = self.queue(git)
        self.assertTrue(queue.push_once())
        self.assertEqual([c for c in git.calls if c[0] == "push"], [],
                         "pushed when there was nothing to push")

    def test_a_failed_push_becomes_backup_pending_not_a_failure(self):
        """The capture is already committed and is NOT at risk. Reporting this
        as a failure would tell a caller to retry a note that is safely on
        disk, which is how one dropped connection becomes two notes."""
        queue = self.queue(FakeGit(unpushed=1, push_ok=False))
        self.assertFalse(queue.push_once())
        self.assertEqual(queue.state(), "backup_pending")

    def test_it_escalates_to_push_failed_after_fifteen_minutes(self):
        queue = self.queue(FakeGit(unpushed=1, push_ok=False))
        queue.push_once()
        self.assertEqual(queue.state(), "backup_pending")
        self.now += capture.PUSH_ALERT_SECONDS + 1
        self.assertEqual(queue.state(), "push_failed",
                         "an outage past the alert threshold still read as merely pending")

    def test_recovery_clears_the_pending_state(self):
        git = FakeGit(unpushed=1, push_ok=False)
        queue = self.queue(git)
        queue.push_once()
        self.now += capture.PUSH_ALERT_SECONDS + 1
        self.assertEqual(queue.state(), "push_failed")
        git.push_ok = True
        self.assertTrue(queue.push_once())
        self.assertEqual(queue.state(), "pushed", "state stayed red after recovery")

    def test_a_repository_with_no_upstream_is_reported_not_guessed(self):
        queue = self.queue(FakeGit(upstream=False))
        self.assertEqual(queue.unpushed(), -1)

    def test_a_brain_with_no_remote_says_backup_is_off(self):
        """Honest, and not the same as 'pushed'. A brain with nowhere to push
        has no third durability layer, and the caller is told so."""
        queue = self.queue(FakeGit(), enabled=False)
        self.assertEqual(queue.state(), "disabled")

    def test_the_outage_is_recorded_without_any_note_content(self):
        queue = self.queue(FakeGit(unpushed=3, push_ok=False))
        queue.push_once()
        written = (Path(self.tmp.name) / "mutations.jsonl").read_text(encoding="utf-8")
        self.assertIn("push_failed", written)
        self.assertIn('"count":3', written.replace(" ", ""))


class EventLogVocabularyTests(unittest.TestCase):
    """The redaction control is a list of what MAY exist, not a list of what to hide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = eventlog.EventLog(self.tmp.name)

    def tearDown(self):
        shutil.rmtree(self.tmp.name, ignore_errors=True)

    def test_an_unknown_field_raises_rather_than_being_written(self):
        with self.assertRaises(ValueError):
            self.log.record("tool_call", query="what did I decide about pricing")

    def test_an_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            self.log.record("something_new")

    def test_a_value_outside_a_closed_vocabulary_raises(self):
        for field, bad in (("outcome", "maybe"), ("backup", "probably"), ("mode", "root")):
            with self.assertRaises(ValueError):
                self.log.record("tool_call", **{field: bad})

    def test_there_is_no_field_that_could_carry_a_query_or_a_note_body(self):
        for forbidden in ("query", "text", "body", "email", "path", "assertion",
                          "host", "authorization", "arguments"):
            self.assertNotIn(forbidden, eventlog.FIELDS)

    def test_writing_survives_an_unwritable_directory(self):
        """A log that can take the server down is worse than a log with a gap."""
        broken = eventlog.EventLog("/proc/nonexistent/brain")
        broken.record("server_started")        # must not raise


def _compose_services(path: Path):
    """The service names in compose.yaml, without a YAML parser.

    This repository has no third-party dependency and this is not the place to
    acquire one. The file is machine-written-shaped — two-space indentation, one
    key per line — so a section-aware line scan is exact, and a scan that ever
    stops being exact fails loudly here rather than quietly in production."""
    services, section = [], None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line[:1].isalpha():                       # a top-level key
            section = line.split(":", 1)[0]
            continue
        if section != "services":
            continue
        if (line.startswith("  ") and not line.startswith("   ")
                and line.strip() and not line.lstrip().startswith("#")
                and line.rstrip().endswith(":")):
            services.append(line.strip().rstrip(":"))
    return services


def _compose_run_service(command: str):
    """The compose SERVICE a `docker compose ... run` line targets, or None.

    Parsed the way compose parses it — flags first, then the service — because
    the whole point is to catch an ExecStart that names something compose will
    not recognise."""
    words = command.split()
    # The unit spells docker by absolute path, so match the basename. Getting
    # this wrong is silent: the parser returns None, the caller skips the line,
    # and the test passes on a unit it never actually read.
    if not any(word.rsplit("/", 1)[-1] == "docker" for word in words):
        return None
    if "compose" not in words or "run" not in words:
        return None
    rest = words[words.index("run") + 1:]
    while rest and rest[0].startswith("-"):
        rest.pop(0)                                  # --rm, --no-deps
    return rest[0] if rest else None


class DeploymentUnitTests(unittest.TestCase):
    """Prevents: a scheduled unit that looks installed and never runs.

    Both defects these units actually shipped with were of that shape. One
    named the CONTAINER (`brain-maintenance`) where compose wanted the SERVICE
    (`maintenance`), so every nightly run died with "no such service" — a
    failure that reads as the maintenance work failing rather than as a name.
    The other set `User=` to an account that did not exist and every start
    ended in 217/USER. Neither is visible by reading the unit on its own; both
    are visible by reading it against the file it refers to."""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.systemd = root / "deploy" / "systemd"
        cls.compose = root / "deploy" / "compose.yaml"
        cls.services = sorted(cls.systemd.glob("*.service"))
        cls.timers = sorted(cls.systemd.glob("*.timer"))

    def directives(self, path, key):
        return [line.split("=", 1)[1].strip()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith(key + "=")]

    def test_every_timer_has_the_service_it_activates(self):
        for timer in self.timers:
            self.assertTrue(timer.with_suffix(".service").exists(),
                            "%s activates a unit that is not in this directory"
                            % timer.name)

    def test_every_timer_survives_a_missed_window(self):
        """An A1 instance that came back at 04:00 has already missed 03:20, and
        a missed night is exactly the night the report mattered."""
        for timer in self.timers:
            self.assertIn("true", [v.lower() for v in self.directives(timer, "Persistent")],
                          "%s does not catch up after a reboot" % timer.name)

    def test_every_timer_is_installable(self):
        for timer in self.timers:
            self.assertIn("timers.target", self.directives(timer, "WantedBy"),
                          "%s has no [Install] and enable would do nothing" % timer.name)

    def test_every_calendar_is_pinned_to_utc(self):
        """The scripts these drive reason in UTC. A local-time calendar crosses
        the UTC date boundary twice a year and silently skips a run."""
        for timer in self.timers:
            for calendar in self.directives(timer, "OnCalendar"):
                self.assertTrue(calendar.endswith("UTC"),
                                "%s: %r is not pinned to UTC" % (timer.name, calendar))

    def test_every_scheduled_service_raises_when_it_fails(self):
        for service in self.services:
            if service.name.startswith("brain-alert@"):
                continue                     # it IS the handler
            self.assertIn("brain-alert@%n.service", self.directives(service, "OnFailure"),
                          "%s fails silently" % service.name)

    def test_every_compose_service_a_unit_runs_exists_in_the_compose_file(self):
        known = _compose_services(self.compose)
        self.assertIn("maintenance", known, "the compose scan itself has stopped working")
        checked = 0
        for service in self.services:
            for command in self.directives(service, "ExecStart"):
                target = _compose_run_service(command)
                if target is None:
                    continue
                checked += 1
                self.assertIn(target, known,
                              "%s runs compose service %r, which compose.yaml does "
                              "not define" % (service.name, target))
        # Without this the test passes just as happily when the parser has
        # stopped recognising the lines it is supposed to be checking, which is
        # how the first version of it missed the very bug it was written for.
        self.assertGreaterEqual(checked, 3, "no compose `run` line was examined")

    def test_the_edge_check_stops_rather_than_reporting_an_edge_it_never_read(self):
        """`EnvironmentFile=-` makes a missing file non-fatal. For the token
        that reaches Cloudflare that would mean a green daily check that never
        authenticated — the exact failure the check exists to prevent."""
        unit = self.systemd / "brain-edge-check.service"
        token_files = [value for value in self.directives(unit, "EnvironmentFile")
                       if "cf-api" in value]
        self.assertEqual(len(token_files), 1)
        self.assertFalse(token_files[0].startswith("-"),
                         "a missing API token would be tolerated silently")



class RunbookFirewallScriptTests(unittest.TestCase):
    """The egress allowlist in the runbook is a shell script somebody will
    paste onto a production host, so it is extracted from the runbook and run
    here rather than trusted.

    Two properties are worth a test and nothing else is. `apply` must be
    idempotent, because it runs on every boot through a systemd unit and a
    chain that grows a duplicate set of rules per reboot is a slow outage. And
    `clear` must remove exactly the deployment's own rules, because
    DOCKER-USER is a shared hook chain: a clear that took someone else's rule
    with it would be discovered as a firewall hole, not as a bug here.

    The first version of this script deleted rules by parsing `iptables -S`.
    It passed inspection and failed this test: `-S` re-quotes
    `--log-prefix "brain-egress-drop "` and the trailing space does not
    survive being split back into words, so the LOG rule was never removed and
    `apply` grew the chain every time."""

    RULE_COUNT = 6

    @classmethod
    def setUpClass(cls):
        runbook = (Path(__file__).resolve().parent.parent
                   / "setup" / "runbooks" / "remote-brain.md")
        text = runbook.read_text(encoding="utf-8")
        start = "sudo tee /usr/local/sbin/brain-firewall >/dev/null <<'EOF'\n"
        head = text.index(start) + len(start)
        cls.script = text[head:text.index("\nEOF\n", head)] + "\n"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="brain-fw-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = self.dir / "chain"
        self.store.write_text("", encoding="utf-8")
        script = self.dir / "brain-firewall"
        script.write_text(self.script, encoding="utf-8")
        script.chmod(0o755)
        self.script_path = script
        stub_dir = self.dir / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "iptables"
        # Models the only two behaviours the script relies on: -D removes the
        # FIRST match, and exits non-zero when nothing matches.
        stub.write_text(
            "#!/bin/sh\n"
            "STORE=${FAKE_IPT_STORE:?}\n"
            "op=$1; shift\n"
            'spec="$*"\n'
            "case \"$op\" in\n"
            "  -A) printf '%s\\n' \"$spec\" >> \"$STORE\" ;;\n"
            "  -D) awk -v s=\"$spec\" 'BEGIN{d=0}{if(!d && $0==s){d=1;next}print}"
            "END{exit d?0:1}' \"$STORE\" > \"$STORE.tmp\" || "
            "{ rm -f \"$STORE.tmp\"; exit 1; }\n"
            "      mv \"$STORE.tmp\" \"$STORE\" ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n",
            encoding="utf-8")
        stub.chmod(0o755)
        self.env = dict(os.environ)
        self.env["PATH"] = "%s:%s" % (stub_dir, self.env.get("PATH", ""))
        self.env["FAKE_IPT_STORE"] = str(self.store)

    def run_script(self, verb):
        return subprocess.run(["/bin/sh", str(self.script_path), verb],
                              env=self.env, capture_output=True, text=True)

    def rules(self):
        return [line for line in self.store.read_text(encoding="utf-8").splitlines()
                if line.strip()]

    def test_the_script_is_valid_shell(self):
        checked = subprocess.run(["/bin/sh", "-n", str(self.script_path)],
                                 capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_apply_is_idempotent(self):
        for attempt in range(3):
            result = self.run_script("apply")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(len(self.rules()), self.RULE_COUNT,
                             "apply #%d changed the rule count" % (attempt + 1))

    def test_the_drop_is_last_and_the_accepts_precede_it(self):
        """Order is the whole rule set. A DROP above the ACCEPTs blocks the
        tunnel; a DROP that never gets appended allows everything."""
        self.run_script("apply")
        rules = self.rules()
        self.assertTrue(rules[-1].endswith("-j DROP"), rules[-1])
        self.assertEqual(sum(1 for r in rules if r.endswith("-j ACCEPT")), 4)
        self.assertLess(max(i for i, r in enumerate(rules) if r.endswith("-j ACCEPT")),
                        len(rules) - 1)

    def test_clear_removes_everything_it_added(self):
        self.run_script("apply")
        result = self.run_script("clear")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rules(), [])

    def test_clear_on_an_empty_chain_succeeds(self):
        """It runs as a systemd ExecStop, so failing on an already-clean chain
        would leave the unit in a failed state after a normal stop."""
        result = self.run_script("clear")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_rule_belonging_to_somebody_else_survives_both(self):
        foreign = "DOCKER-USER -i eth0 -j ACCEPT"
        self.store.write_text(foreign + "\n", encoding="utf-8")
        self.run_script("apply")
        self.run_script("clear")
        self.assertEqual(self.rules(), [foreign])

    def test_an_unknown_verb_is_a_usage_error(self):
        result = self.run_script("flush-everything")
        self.assertEqual(result.returncode, 64)
        self.assertEqual(self.rules(), [])


def _note_line(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("note:"):
            return line.strip()
    return ""


if __name__ == "__main__":
    unittest.main()
