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
import re
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
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from brainlib import access, capture, httplog, httpmcp, mcpcore, rs256  # noqa: E402
from brainlib import version as brainversion  # noqa: E402

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
CAPTURE_CLIENT = "fedcba9876543210.access"
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

    def __init__(self, origin_policy="observe", git_push=False, limits=None,
                 verifier_options=None):
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
            service_principals={SERVICE_CLIENT: "read", CAPTURE_CLIENT: "capture"},
            principal_key=PRINCIPAL_KEY, jwks=jwks, **(verifier_options or {}))
        self.log = httplog.EventLog(self.state_dir)
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



def vendor_names_in(text: str) -> list:
    """The vendor names a tool contract must not carry. `cursor` and `next_cursor` as whole
    words are brain-verbs.v1's pagination terms (tests/test_brain.py strips the same two)."""
    # Case-sensitive on purpose: the pagination words are lowercase, the editor is "Cursor".
    blob = re.sub(r"\b(next_)?cursor\b", "", text).lower()
    return [v for v in ("claude", "anthropic", "openai", "gemini", "cursor", "cloudflare")
            if v in blob]


class RemoteTestCase(unittest.TestCase):
    ORIGIN_POLICY = "observe"
    GIT_PUSH = False
    LIMITS = None
    VERIFIER_OPTIONS = None

    def setUp(self):
        self.brain = RemoteBrain(origin_policy=self.ORIGIN_POLICY,
                                 git_push=self.GIT_PUSH, limits=self.LIMITS,
                                 verifier_options=self.VERIFIER_OPTIONS)

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

    def test_the_capture_profile_is_told_the_capture_policy(self):
        """Every client of every vendor receives `initialize` instructions, so
        this is where the capture policy reaches a client with no other
        configuration at all."""
        _status, _h, body = self.brain.rpc("initialize", {"protocolVersion": "2025-11-25"})
        self.assertEqual(body["result"]["instructions"], mcpcore.instructions("capture"))
        self.assertIn(mcpcore.CAPTURE_POLICY, body["result"]["instructions"])

    def test_the_read_profile_is_told_it_is_read_only(self):
        _status, _h, body = self.brain.rpc("initialize", {"protocolVersion": "2025-11-25"},
                                           host="brain-read.test")
        self.assertEqual(body["result"]["instructions"], mcpcore.instructions("read"))
        self.assertNotIn(mcpcore.CAPTURE_POLICY, body["result"]["instructions"])


class CapabilityBoundaryTests(RemoteTestCase):
    """The read/capture split, which is the whole reason there are two hostnames."""

    def test_the_read_only_endpoint_advertises_exactly_its_six_tools(self):
        status, _h, body = self.brain.rpc("tools/list", host="brain-read.test")
        self.assertEqual(status, 200)
        names = sorted(tool["name"] for tool in body["result"]["tools"])
        self.assertEqual(names, ["brain_links", "brain_read", "brain_recent",
                                 "brain_search", "delta", "recall"])

    def test_the_capture_endpoint_advertises_exactly_its_nine_tools(self):
        status, _h, body = self.brain.rpc("tools/list", host="brain.test")
        self.assertEqual(status, 200)
        names = sorted(tool["name"] for tool in body["result"]["tools"])
        self.assertEqual(names, ["brain_capture", "brain_links", "brain_read",
                                 "brain_recent", "brain_search", "delta", "forget",
                                 "recall", "remember"])

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
        self.assertEqual(vendor_names_in(json.dumps(body["result"]["tools"])), [],
                         "a vendor name leaked into the tool contract")

    def test_the_vendor_check_still_catches_the_editor_called_cursor(self):
        """`cursor` and `next_cursor` are delta's pagination words and are let through;
        the editor's name is not, in any case or position."""
        self.assertEqual(vendor_names_in('{"cursor": "x", "next_cursor": {"cursor": "y"}}'), [])
        self.assertEqual(vendor_names_in("Works with Cursor and next_cursor."), ["cursor"])
        self.assertEqual(vendor_names_in("a cursorial note"), ["cursor"])


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
        self.assertEqual(len(body["result"]["tools"]), 6)

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

    def test_handoff_notes_are_listed_under_scope_handoff_only(self):
        """Handoff notes are not brain knowledge: the remote front end lists them under
        `scope: handoff` (an empty query lists them all) and never under `all`."""
        rel = "knowledge/handoff/acme/2026-10-15-120000-aardvark.md"
        path = self.brain.data_root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\ncreated: 2026-10-15\nsource: local\nstatus: draft\n"
                        "handoff_to: acme\nhandoff_at: 2026-10-18\n"
                        "handoff_reason: pending work for acme\n---\n\n"
                        "Aardvark migration still needs its rollback drill.\n",
                        encoding="utf-8")
        for args in (["add", "-A"], ["commit", "-q", "-m", "handoff"]):
            subprocess.run(["git", *args], cwd=self.brain.data_root, check=True,
                           capture_output=True)
        _status, body = self.brain.call("brain_search", {"query": "aardvark", "scope": "all"})
        self.assertNotIn("knowledge/handoff/", self.brain.text_of(body))
        _status, body = self.brain.call("brain_search", {"query": "", "scope": "handoff"})
        text = self.brain.text_of(body)
        self.assertFalse(body["result"]["isError"], text)
        self.assertIn(rel, text)
        self.assertIn("[handoff — belongs in acme]", text)
        _status, body = self.brain.call("brain_read", {"id_or_path": rel})
        self.assertIn("HANDOFF", self.brain.text_of(body))

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

    def _state_text(self, pattern):
        """Every file matching `pattern` under the state dir, lowercased: the
        SQLite WAL and SHM hold rows the main file does not have yet, and note
        filenames are lowercase slugs of the text."""
        blob = b""
        for path in sorted(self.brain.state_dir.rglob(pattern)):
            blob += path.read_bytes()
        return blob.lower()

    def test_the_ledger_never_stores_the_capture_text(self):
        secret_ish = "Zanzibar pineapple ledger canary 2026-08-23"
        self.brain.call("brain_capture", {"text": secret_ish})
        blob = self._state_text("capture-ledger.db*")
        for word in (b"zanzibar", b"pineapple", b"canary"):
            self.assertNotIn(word, blob)

    def test_the_event_log_records_the_mutation_and_no_content(self):
        self.brain.call("brain_capture", {"text": "Rutabaga is the log canary."})
        audit = (self.brain.state_dir / "mutations.jsonl").read_text(encoding="utf-8")
        self.assertIn("capture_accepted", audit)
        self.assertNotIn(b"rutabaga", self._state_text("*.jsonl"))
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


def _frontmatter(text: str) -> dict:
    lines = text.splitlines()
    end = lines.index("---", 1)
    return dict(line.split(": ", 1) for line in lines[1:end])


class _StampCases(object):
    """What the server knows, stamped; what the caller says, recorded as a claim.

    The server knows which verified credential sent a capture and what the
    client calls itself, and used to write both only to its log, so every note
    read as `source: local` — the owner, on their own machine — whoever wrote
    it. `via` and `client` are now stamped from that knowledge, and nothing in
    a request can set them."""

    def notes(self):
        return sorted((self.brain.data_root / "knowledge" / "inbox").glob("*.md"))

    def only_note(self):
        found = self.notes()
        self.assertEqual(len(found), 1, found)
        return _frontmatter(found[0].read_text(encoding="utf-8"))

    def modern_capture(self, arguments, client_info=None, assertion=None):
        meta = {httpmcp.META_VERSION: "2026-07-28", httpmcp.META_CLIENT_CAPS: {}}
        if client_info is not None:
            meta[httpmcp.META_CLIENT_INFO] = client_info
        status, _h, body = self.brain.post(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "brain_capture", "arguments": arguments, "_meta": meta}},
            assertion=assertion or sign_assertion(owner_claims()),
            headers={"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call",
                     "Mcp-Name": "brain_capture"})
        self.assertEqual(status, 200, body)
        return body

    def test_a_modern_client_is_stamped_with_its_own_name(self):
        body = self.modern_capture({"text": "Named by its client, 2026-10-18."},
                                   {"name": "Generic Client", "version": "2.1"})
        self.assertFalse(body["result"]["isError"], body)
        self.assertEqual(self.only_note()["client"], "generic-client/2.1")

    def test_a_client_name_that_is_not_a_label_is_dropped_and_logged(self):
        body = self.modern_capture({"text": "A client with a strange name."},
                                   {"name": "evil\nstatus: current", "version": "1"})
        self.assertFalse(body["result"]["isError"], body)
        fields = self.only_note()
        self.assertNotIn("client", fields)
        self.assertEqual(fields["status"], "draft")
        events = [json.loads(line) for line in
                  (self.brain.state_dir / "events.jsonl").read_text(encoding="utf-8")
                  .splitlines()]
        dropped = [e for e in events if e["event"] == "provenance_dropped"]
        self.assertEqual(len(dropped), 1, events)
        self.assertEqual(dropped[0]["reason"], "client")
        self.assertNotIn("evil", json.dumps(dropped[0]))

    def test_declared_fields_reach_the_note(self):
        _status, body = self.brain.call("brain_capture", {
            "text": "We picked the daily curator over weekly, 2026-10-18.",
            "kind": "decision", "basis": "owner-said", "project": "brain",
            "job": "j-0042", "model": "some-model-1"})
        self.assertFalse(body["result"]["isError"], body)
        fields = self.only_note()
        self.assertEqual((fields["kind"], fields["basis"], fields["project"], fields["job"],
                          fields["model"]),
                         ("decision", "owner-said", "brain", "j-0042", "some-model-1"))

    def test_a_bad_declared_value_is_named_and_the_note_still_saved(self):
        _status, body = self.brain.call("brain_capture", {
            "text": "A note worth more than its label.", "kind": "musing",
            "basis": "owner-said", "project": 7})
        self.assertFalse(body["result"]["isError"], body)
        text = self.brain.text_of(body)
        self.assertIn("ignored: kind, project", text)
        self.assertIn("PROVISIONAL", text)
        fields = self.only_note()
        self.assertNotIn("kind", fields)
        self.assertNotIn("project", fields)
        self.assertEqual(fields["basis"], "owner-said")

    def test_a_caller_cannot_set_what_the_endpoint_stamps(self):
        for field in ("via", "client", "source", "captured_at", "created", "status"):
            _status, body = self.brain.call("brain_capture",
                                            {"text": "claiming %s" % field, field: "local"})
            self.assertTrue(body["result"]["isError"], field)
            self.assertIn("unknown argument", self.brain.text_of(body))
        self.assertEqual(self.notes(), [], "a refused call reached the disk")

    def test_content_dedup_keys_on_the_text_not_the_labels(self):
        self.brain.call("brain_capture", {"text": "Said once, labelled twice.",
                                          "kind": "fact"})
        _status, second = self.brain.call("brain_capture", {"text": "Said once, labelled twice.",
                                                            "kind": "decision"})
        self.assertIn("ALREADY SAVED", self.brain.text_of(second))
        self.assertEqual(len(self.notes()), 1)


class CaptureStampTests(_StampCases, RemoteTestCase):
    """No labels configured: a capture is stamped with the principal's mode."""

    def test_the_owner_is_stamped_with_the_principal_mode(self):
        _status, body = self.brain.call("brain_capture", {"text": "Stamped by mode, 2026-10-18."})
        self.assertFalse(body["result"]["isError"], body)
        fields = self.only_note()
        self.assertEqual(fields["via"], "interactive")
        self.assertEqual(fields["source"], "local")
        # A legacy tools/call carries no clientInfo; absent is the honest answer.
        self.assertNotIn("client", fields)

    def test_a_legacy_call_may_name_its_client_in_meta(self):
        """A stateless caller that never initializes names itself the modern way."""
        _status, _h, body = self.brain.rpc("tools/call", {
            "name": "brain_capture", "arguments": {"text": "Named in _meta, 2026-10-30."},
            "_meta": {httpmcp.META_CLIENT_INFO: {"name": "harbor-relay", "version": "1"}}})
        self.assertFalse(body["result"]["isError"], body)
        self.assertEqual(self.only_note()["client"], "harbor-relay/1")

    def test_a_service_credential_is_stamped_service(self):
        assertion = sign_assertion(service_claims(aud=AUD_WRITE, common_name=CAPTURE_CLIENT))
        _status, body = self.brain.call("brain_capture", {"text": "From a service, 2026-10-18."},
                                        assertion=assertion)
        self.assertFalse(body["result"]["isError"], body)
        self.assertEqual(self.only_note()["via"], "service")


class LabelledPrincipalStampTests(_StampCases, RemoteTestCase):
    """The operator's names for the credentials it verifies."""
    VERIFIER_OPTIONS = {"principal_labels": {CAPTURE_CLIENT: "relay"},
                        "owner_label": "owner-sign-in"}

    def test_the_owner_is_stamped_with_the_operators_label(self):
        self.brain.call("brain_capture", {"text": "The owner, by label, 2026-10-18."})
        self.assertEqual(self.only_note()["via"], "owner-sign-in")

    def test_a_service_credential_is_stamped_with_its_label(self):
        assertion = sign_assertion(service_claims(aud=AUD_WRITE, common_name=CAPTURE_CLIENT))
        self.brain.call("brain_capture", {"text": "The relay, by label, 2026-10-18."},
                        assertion=assertion)
        self.assertEqual(self.only_note()["via"], "relay")

    def test_the_stable_id_is_unchanged_by_a_label(self):
        """The label is for the NOTE; the log's principal id stays the HMAC."""
        verifier = self.brain.verifier
        plain = access.Verifier(team_domain=TEAM, endpoints=list(verifier.endpoints.values()),
                                owner_email=OWNER, principal_key=PRINCIPAL_KEY,
                                jwks=verifier.jwks)
        claims = owner_claims()
        self.assertEqual(verifier._identify(claims, verifier.endpoint_for("brain.test")).stable_id,
                         plain._identify(claims, plain.endpoint_for("brain.test")).stable_id)


class PrincipalLabelConfigTests(unittest.TestCase):
    """A label is written into notes, so a bad one refuses to start the server."""

    @classmethod
    def setUpClass(cls):
        from importlib.machinery import SourceFileLoader
        import importlib.util
        loader = SourceFileLoader("brain_http_under_test", str(ENGINE / "bin" / "brain-http"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.module = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.module)

    def load(self, **extra):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = {"team_domain": TEAM, "owner_email": OWNER, "principal_key_file": "/k",
                  "endpoints": [{"hostname": "brain.test", "aud": AUD_WRITE,
                                 "profile": "capture"}],
                  "service_principals": {CAPTURE_CLIENT: "capture"}}
        config.update(extra)
        path = Path(tmp.name) / "brain-http.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return self.module.load_config(str(path))

    def test_good_labels_load(self):
        config = self.load(principal_labels={CAPTURE_CLIENT: "relay"}, owner_label="owner")
        self.assertEqual(config["principal_labels"], {CAPTURE_CLIENT: "relay"})

    def test_a_label_that_is_not_a_slug_refuses_to_start(self):
        for bad in ("Relay Bot", "relay\nstatus: current", "", 7, "x" * 81):
            with self.assertRaises(self.module.ConfigError, msg=repr(bad)):
                self.load(principal_labels={CAPTURE_CLIENT: bad})
            with self.assertRaises(self.module.ConfigError, msg=repr(bad)):
                self.load(owner_label=bad)

    def test_a_label_for_a_credential_that_is_not_configured_refuses_to_start(self):
        with self.assertRaises(self.module.ConfigError):
            self.load(principal_labels={"nobody.access": "relay"})

    def test_principal_labels_must_be_an_object(self):
        with self.assertRaises(self.module.ConfigError):
            self.load(principal_labels=["relay"])

    def test_the_labels_reach_the_verifier(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        key = base / "principal.key"
        key.write_bytes(PRINCIPAL_KEY)
        key.chmod(0o600)
        config = self.load(principal_labels={CAPTURE_CLIENT: "relay"}, owner_label="owner")
        config["principal_key_file"] = str(key)
        (base / "state").mkdir()
        service, _queue, _log = self.module.build(config, make_data_root(base), base / "state")
        self.assertEqual(service.verifier.principal_labels, {CAPTURE_CLIENT: "relay"})
        self.assertEqual(service.verifier.owner_label, "owner")


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


class RateLimitEventNamesClientTests(RemoteTestCase):
    LIMITS = {"read": (1, 60.0), "capture": (1, 60.0), "protocol": (100, 60.0)}

    def limited_events(self):
        path = self.brain.state_dir / "events.jsonl"
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [e for e in events if e["event"] == "rate_limited"]

    def search(self, meta=None):
        params = {"name": "brain_search", "arguments": {"query": "connector"}}
        if meta:
            params["_meta"] = meta
        return self.brain.rpc("tools/call", params)[0]

    def test_a_client_that_identified_itself_is_named(self):
        self.assertEqual(self.search(), 200)
        self.assertEqual(self.search({httpmcp.META_CLIENT_INFO:
                                      {"name": "harbor-relay", "version": "2"}}), 429)
        (event,) = self.limited_events()
        self.assertEqual(event["client"], "harbor-relay")
        self.assertEqual(event["client_version"], "2")

    def test_a_client_that_did_not_is_not_invented(self):
        self.assertEqual(self.search(), 200)
        self.assertEqual(self.search(), 429)
        (event,) = self.limited_events()
        self.assertNotIn("client", event)
        self.assertNotIn("client_version", event)


class RateLimitDefaultsTests(unittest.TestCase):
    def test_the_default_limits_are_pinned(self):
        self.assertEqual(httpmcp.RATE_LIMITS, {"read": (1200, 60.0), "capture": (10, 60.0),
                                               "protocol": (600, 60.0)})


class EventLogHistoryDefaultsTests(unittest.TestCase):
    def test_the_server_logs_keep_a_troubleshooting_history(self):
        self.assertEqual(httplog.RUNTIME_RETENTION_DAYS, 180)
        self.assertEqual(httplog.AUDIT_RETENTION_DAYS, 180)
        self.assertEqual(httplog.MAX_BYTES, 25_000_000)


class OneResponsePerRequestTests(RemoteTestCase):
    """Audit A5: a rate-limited initialize sent a 429 AND a 200 on one connection, which
    corrupts HTTP framing for every request after it on that keep-alive connection."""
    LIMITS = {"read": (1, 60.0), "capture": (1, 60.0), "protocol": (1, 60.0)}

    def raw(self, body):
        import socket
        payload = json.dumps(body).encode("utf-8")
        head = ("POST /mcp HTTP/1.1\r\nHost: brain.test\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nConnection: close\r\nCf-Access-Jwt-Assertion: %s\r\n%s\r\n"
                % (len(payload), sign_assertion(owner_claims(AUD_WRITE)),
                   "" if body.get("method") == "initialize" else "MCP-Protocol-Version: 2025-11-25\r\n"))
        conn = socket.create_connection(("127.0.0.1", self.brain.port), timeout=10)
        conn.sendall(head.encode("ascii") + payload)
        data = b""
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        conn.close()
        return data

    def test_an_over_budget_initialize_gets_exactly_one_response(self):
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                           "clientInfo": {"name": "t", "version": "1"}}}
        first = self.raw(init)
        self.assertEqual(len(re.findall(rb"HTTP/1\.[01] \d{3} ", first)), 1)
        second = self.raw(init)
        statuses = re.findall(rb"HTTP/1\.[01] (\d{3}) ", second)
        self.assertEqual(statuses, [b"429"], second[:300])

    def test_every_tool_bucket_sends_exactly_one_response_when_over_budget(self):
        for name, arguments in (("brain_search", {"query": "x"}), ("brain_capture", {"text": "One note."})):
            call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}}
            self.raw(call)
            statuses = re.findall(rb"HTTP/1\.[01] (\d{3}) ", self.raw(call))
            self.assertEqual(len(statuses), 1, (name, statuses))


class MaintenanceModeTests(RemoteTestCase):
    """Audit A2: the 503 gate is reached in production, because the write path asks a live
    check on every capture; reads never consult it."""

    def test_writes_are_refused_reads_keep_working_and_it_clears_by_itself(self):
        reasons = ["dirty tree"]
        self.brain.service.maintenance_check = lambda: reasons[0]
        status, headers, _body = self.brain.rpc(
            "tools/call", {"name": "brain_capture", "arguments": {"text": "Held back."}})
        self.assertEqual(status, 503)
        self.assertEqual(headers.get("Retry-After"), "60")
        self.assertEqual(self.brain.call("brain_search", {"query": "connector"})[0], 200)
        reasons[0] = None
        status, body = self.brain.call("brain_capture", {"text": "Accepted again."})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])


class SearchResultCountTests(RemoteTestCase):
    """Metric 7 needs zero-result searches counted: each brain_search event carries the
    number of hits it found (a number, never the query or a title)."""

    def search_events(self):
        rows = [json.loads(line) for line in
                (self.brain.state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if r.get("event") == "tool_call" and r.get("tool") == "brain_search"]

    def test_a_search_event_carries_its_result_count(self):
        self.assertEqual(self.brain.call("brain_search", {"query": "connector"})[0], 200)
        self.assertEqual(self.brain.call("brain_search", {"query": "zzqqxxnothinghere"})[0], 200)
        first, second = self.search_events()[-2:]
        self.assertGreaterEqual(first["results"], 1)
        self.assertEqual(second["results"], 0)
        self.assertNotIn("zzqq", json.dumps(second))


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
        self.assertEqual(len(body["result"]["tools"]), 9)

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
        self.assertEqual(len(body["result"]["tools"]), 9)

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

    def __init__(self, unpushed=1, push_ok=True, upstream=True, push_rejected=False):
        self.unpushed = unpushed
        self.push_ok = push_ok
        self.upstream = upstream
        self.push_rejected = push_rejected
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["rev-list", "--count"]:
            if not self.upstream:
                return self.Done(128)
            return self.Done(0, "%d\n" % self.unpushed)
        if args[0] == "push":
            if self.push_rejected:
                return self.Done(1, "To origin\n!\trefs/heads/main:refs/heads/main\t"
                                    "[rejected] (fetch first)\nDone\n")
            if self.push_ok:
                self.unpushed = 0
                return self.Done(0)
            return self.Done(1)
        raise AssertionError("unexpected git call: %r" % (args,))


class LedgerKeyTests(unittest.TestCase):
    """Review of #15, row 8: two servers starting together on a fresh state directory must
    end with ONE key, or request ids and deduplication stop working between them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def test_a_second_starter_arriving_mid_creation_gets_the_same_key(self):
        from unittest import mock
        real, nested, inner = os.fdopen, [], []

        def fdopen(fd, *args, **kwargs):
            # Between the key file being created and its key being written: the moment
            # a second process (here, a nested call) can look at the file.
            if not nested:
                nested.append(True)
                inner.append(capture.ledger_key(self.state))
            return real(fd, *args, **kwargs)
        with mock.patch.object(os, "fdopen", fdopen):
            outer = capture.ledger_key(self.state)
        self.assertEqual(len(outer), 32)
        self.assertEqual(inner, [outer])
        self.assertEqual((self.state / "capture-ledger.key").read_bytes(), outer)

    def test_the_key_is_private_and_stable(self):
        key = capture.ledger_key(self.state)
        self.assertEqual(capture.ledger_key(self.state), key)
        path = self.state / "capture-ledger.key"
        self.assertEqual(path.stat().st_mode & 0o077, 0, "the key is readable by others")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()), ["capture-ledger.key"])

    def test_an_empty_key_file_from_an_older_crash_is_replaced_once(self):
        self.state.mkdir()
        (self.state / "capture-ledger.key").write_bytes(b"")
        key = capture.ledger_key(self.state)
        self.assertEqual(len(key), 32)
        self.assertEqual(capture.ledger_key(self.state), key)


class LedgerUpgradeTests(unittest.TestCase):
    """Prevents: a server upgraded past audit A3 keeping, forever, the rows an
    older version wrote — their note path is a slug of the capture text — and
    rows past their TTL that nothing ever deleted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def _write_legacy_row(self, digest):
        import sqlite3
        db = sqlite3.connect(str(self.state / capture.LEDGER_FILE))
        db.execute("CREATE TABLE IF NOT EXISTS seen ("
                   "fingerprint TEXT PRIMARY KEY, kind TEXT, at REAL, result TEXT)")
        db.execute("INSERT OR REPLACE INTO seen VALUES (?,?,?,?)", (digest, "content", time.time(), json.dumps(
            {"ok": True, "status": "committed",
             "note": "knowledge/inbox/2026-08-23-zanzibar-pineapple-canary.md",
             "commit": "abc1234", "provisional": True, "backup": "pushed", "detail": ""})))
        db.commit()
        db.close()

    def _disk(self):
        return b"".join(p.read_bytes()
                        for p in sorted(self.state.glob(capture.LEDGER_FILE + "*"))).lower()

    def test_opening_an_old_ledger_scrubs_the_text_from_its_rows(self):
        self._write_legacy_row("f" * 64)
        capture.Ledger(self.state, PRINCIPAL_KEY)
        self.assertNotIn(b"pineapple", self._disk())

    def test_a_scrubbed_row_still_answers_its_lookup(self):
        digest = capture.Ledger(self.state, PRINCIPAL_KEY).fingerprint(
            "content", "capture", "Zanzibar pineapple canary")
        self._write_legacy_row(digest)
        ledger = capture.Ledger(self.state, PRINCIPAL_KEY)
        self.assertEqual(ledger.lookup("content", "capture", "Zanzibar pineapple canary"),
                         {"status": "committed", "commit": "abc1234"})

    def test_rows_past_their_ttl_are_deleted_when_a_new_one_is_written(self):
        import sqlite3
        clock = [1000.0]
        ledger = capture.Ledger(self.state, PRINCIPAL_KEY, clock=lambda: clock[0])
        ledger.remember("content", "capture", "old", {"status": "committed", "commit": "abc1234"})
        clock[0] += capture.REQUEST_TTL_SECONDS + capture.CONTENT_TTL_SECONDS + 1
        ledger.remember("content", "capture", "new", {"status": "committed", "commit": "def5678"})
        db = sqlite3.connect(str(self.state / capture.LEDGER_FILE))
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM seen").fetchone()[0], 1)


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
        self.log = httplog.EventLog(self.tmp.name, clock=lambda: self.now)

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

    def test_a_push_that_lands_four_seconds_later_is_reported_pushed(self):
        """Measured on Oracle (2026-10-15): a no-op push to GitHub takes 2.1-2.3 s, so a real one
        lands just past a 2.5 s window and every capture answered PENDING."""
        clock = [0.0]

        class SlowGit(FakeGit):
            def __call__(inner, args):
                if args[:2] == ["rev-list", "--count"]:
                    return inner.Done(0, "0\n" if clock[0] >= 4.0 else "1\n")
                return FakeGit.__call__(inner, args)

        def sleep(seconds):
            clock[0] += seconds
        queue = capture.BackupQueue(self.tmp.name, log=self.log, clock=lambda: clock[0],
                                    runner=SlowGit(unpushed=1), sleeper=sleep)
        self.assertEqual(queue.settle(), "pushed")

    def test_pending_says_the_push_may_still_be_in_flight(self):
        """PENDING also covers a push still on its way, not only one being retried."""
        text = capture.format_result({"ok": True, "note": "knowledge/inbox/x.md",
                                      "commit": "abc1234", "backup": "backup_pending"})
        self.assertIn("in flight", text)

    def test_the_queue_pushes_exactly_main_through_the_single_pusher(self):
        """2.4: one code path pushes, and it names main — never HEAD."""
        git = FakeGit(unpushed=1, push_ok=True)
        self.queue(git).push_once()
        pushes = [c for c in git.calls if c[0] == "push"]
        self.assertEqual(pushes, [["push", "--porcelain", "origin",
                                   "refs/heads/main:refs/heads/main"]])

    def test_a_rejected_push_triggers_one_sync_not_a_retry_loop(self):
        git = FakeGit(unpushed=1, push_rejected=True)
        synced = []
        queue = capture.BackupQueue(self.tmp.name, log=self.log, clock=lambda: self.now,
                                    runner=git, sleeper=lambda _s: None,
                                    syncer=lambda: synced.append(1))
        self.assertFalse(queue.push_once())
        self.assertEqual(synced, [1], "a rejected push did not hand over to sync")
        self.assertEqual(len([c for c in git.calls if c[0] == "push"]), 1,
                         "the queue retried the push itself")

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


class DurabilityReportTests(unittest.TestCase):
    """Prevents: a capture claiming durability it does not have.

    Found in a live push-outage drill, not in a test. The remote was
    unroutable, the note was committed and unpushed, and the capture answered
    "backup: pushed to the private remote." The bug was one word of meaning:
    `_pending_since` records that a push has been TRIED and failed, and in the
    millisecond after a commit nothing has been tried, so the flag is clear —
    and the old `state()` read a clear flag as success.

    That line is the caller's ONLY signal about durability. A wrong one is
    worse than none: it is the failure this whole system is built to avoid,
    which is being confidently told something untrue."""

    def queue(self, git, sleeper=None):
        return capture.BackupQueue("/nonexistent", clock=lambda: 1000.0,
                                   runner=git, sleeper=sleeper or (lambda _s: None))

    def test_an_unpushed_commit_is_never_reported_as_pushed(self):
        """The exact live defect, in one assertion."""
        queue = self.queue(FakeGit(unpushed=1))
        self.assertEqual(queue.state(), "backup_pending")

    def test_a_pushed_commit_is_reported_as_pushed(self):
        self.assertEqual(self.queue(FakeGit(unpushed=0)).state(), "pushed")

    def test_no_upstream_is_not_an_alarm(self):
        """`rev-list` against a missing tracking ref fails. That is a brain with
        no remote configured, which `disabled` and `doctor` already speak about;
        a capture must not shout PENDING at it forever."""
        self.assertEqual(self.queue(FakeGit(upstream=False)).state(), "pushed")

    def test_a_disabled_queue_still_says_disabled(self):
        queue = capture.BackupQueue("/nonexistent", runner=FakeGit(unpushed=5),
                                    enabled=False)
        self.assertEqual(queue.state(), "disabled")

    def test_settle_reports_pushed_once_the_push_lands(self):
        git = FakeGit(unpushed=1)
        queue = self.queue(git)
        # The worker is what drains it; drive one round by hand, as it would.
        queue.push_once()
        self.assertEqual(queue.settle(timeout=0.0), "pushed")

    def test_settle_does_not_wait_once_a_push_has_actually_failed(self):
        """A failed push is a decided answer. Waiting out the timeout for one
        would put the caller's latency in the hands of a broken remote."""
        git = FakeGit(unpushed=1, push_ok=False)
        slept = []
        queue = self.queue(git, sleeper=slept.append)
        queue.push_once()                       # marks it pending-since
        self.assertEqual(queue.settle(timeout=60.0), "backup_pending")
        self.assertEqual(slept, [], "settle waited on a question already answered")

    def test_settle_is_bounded_when_nothing_has_been_tried_yet(self):
        """Nothing has failed and nothing has succeeded — the worker simply has
        not run. That is the case settle exists for, and it must end."""
        ticks = iter([1000.0 + i * 0.5 for i in range(40)])
        queue = capture.BackupQueue("/nonexistent", clock=lambda: next(ticks),
                                    runner=FakeGit(unpushed=1), sleeper=lambda _s: None)
        self.assertEqual(queue.settle(timeout=1.0), "backup_pending")

    def test_the_message_for_each_state_says_the_right_thing(self):
        """`pushed` must claim the remote; `backup_pending` must reassure that
        the note is safe, because it is."""
        pushed = capture.format_result({"ok": True, "status": "committed", "note": "n",
                                        "commit": "abc", "backup": "pushed"})
        pending = capture.format_result({"ok": True, "status": "committed", "note": "n",
                                         "commit": "abc", "backup": "backup_pending"})
        self.assertIn("pushed to the private remote", pushed)
        self.assertIn("committed locally and safe", pending)
        self.assertNotIn("pushed to the private remote", pending)


class EventLogVocabularyTests(unittest.TestCase):
    """The redaction control is a list of what MAY exist, not a list of what to hide."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = httplog.EventLog(self.tmp.name)

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
            self.assertNotIn(forbidden, httplog.FIELDS)

    def test_writing_survives_an_unwritable_directory(self):
        """A log that can take the server down is worse than a log with a gap."""
        broken = httplog.EventLog("/proc/nonexistent/brain")
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
    # `compose run` flags come in two kinds and getting the difference wrong is
    # how this returns `python3` for `--entrypoint python3 maintenance` — a
    # confident wrong answer rather than a miss.
    takes_a_value = {"--entrypoint", "--env", "-e", "--label", "-l", "--name",
                     "--publish", "-p", "--user", "-u", "--volume", "-v",
                     "--workdir", "-w"}
    while rest and rest[0].startswith("-"):
        flag = rest.pop(0)
        if flag in takes_a_value and rest:
            rest.pop(0)
    return rest[0] if rest else None


def _shell_commands(markdown: str):
    r"""Every command line in a fenced block, with `\` continuations joined."""
    joined, buffer = [], ""
    for line in markdown.splitlines():
        line = line.rstrip()
        if buffer:
            line = buffer + " " + line.lstrip()
            buffer = ""
        if line.endswith("\\"):
            buffer = line[:-1].rstrip()
            continue
        joined.append(line)
    return joined


class UnknownArgumentTests(unittest.TestCase):
    """Prevents: a capture that quietly forgets the retry-safety it was asked for.

    JSON Schema without `additionalProperties: false` says to ignore an
    undeclared argument. For a search that is the friendlier reading; for a
    capture it is a trap, and it sprang on the first live run of the cutover.
    A probe sent `request_id` where the tool declares `client_request_id`. The
    capture succeeded and the idempotency was silently off — a client retrying
    after a timeout, which is the ONLY reason that argument exists, would have
    written the note twice with nothing to tell it so."""

    def tool(self, name, transport="remote"):
        return [t for t in mcpcore.tool_table(transport) if t["name"] == name][0]

    def test_the_typo_that_started_this_is_refused(self):
        clean, error = mcpcore.validate_args(
            self.tool("brain_capture"), {"text": "x", "request_id": "a"})
        self.assertIsNone(clean)
        self.assertIn("request_id", error)

    def test_the_message_names_the_argument_that_was_meant(self):
        """A near miss is nearly always a typo, and naming the intended
        argument is the difference between a message that fixes it and a
        message that starts a search."""
        _, error = mcpcore.validate_args(
            self.tool("brain_capture"), {"text": "x", "request_id": "a"})
        self.assertIn("Did you mean 'client_request_id'?", error)
        _, error = mcpcore.validate_args(
            self.tool("brain_search"), {"query": "x", "scop": "all"})
        self.assertIn("Did you mean 'scope'?", error)

    def test_the_message_lists_what_the_tool_does_accept(self):
        _, error = mcpcore.validate_args(
            self.tool("brain_capture"), {"text": "x", "colour": "blue"})
        for accepted in ("text", "client_request_id", "allow_duplicate", "kind", "basis",
                         "project", "job", "model"):
            self.assertIn(accepted, error)

    def test_the_correct_spelling_still_works(self):
        clean, error = mcpcore.validate_args(
            self.tool("brain_capture"), {"text": "x", "client_request_id": "a"})
        self.assertIsNone(error)
        self.assertEqual(clean["client_request_id"], "a")

    def test_a_reserved_underscore_key_is_tolerated(self):
        """`_meta` and friends are a namespace a conformant client may decorate
        a call with. Refusing those would break real clients to catch nothing."""
        clean, error = mcpcore.validate_args(
            self.tool("brain_capture"), {"text": "x", "_meta": {"trace": 1}})
        self.assertIsNone(error)
        self.assertNotIn("_meta", clean)

    def test_every_tool_refuses_an_undeclared_argument(self):
        """Not just capture — a silently dropped `scope` turns an explicit
        search of the inbox into a search that never looked at it."""
        for transport in ("local", "remote"):
            for tool in mcpcore.tool_table(transport):
                args = {field: "x" for field in
                        tool["inputSchema"].get("required", [])}
                args["definitely_not_an_argument"] = "x"
                clean, error = mcpcore.validate_args(tool, args)
                self.assertIsNone(clean, "%s/%s accepted an unknown argument"
                                  % (transport, tool["name"]))
                self.assertIn("definitely_not_an_argument", error)


class ReleaseVersionTests(unittest.TestCase):
    """Prevents: three version numbers that are each believable alone.

    The server used to report `1.0.0` while the box ran `brain:0.1.0` built
    from a branch with no tag on it at all. Every one of those is plausible in
    isolation, which is what makes the disagreement expensive: a client's bug
    report names a version that never existed, and nobody can tell which code
    it was."""

    def test_the_server_reports_the_release_version(self):
        self.assertEqual(httpmcp.SERVER_VERSION, brainversion.VERSION)

    def test_the_version_is_a_release_number_and_not_a_branch_name(self):
        self.assertRegex(brainversion.VERSION, r"^\d+\.\d+\.\d+$")

    def test_serverinfo_carries_it_to_the_client(self):
        """The modern revision is stateless — there is no handshake left in
        which to say who is answering — so this is the only place a client can
        learn it."""
        result = httpmcp._modern_result(1, {})
        info = result["result"]["_meta"][httpmcp.META_SERVER_INFO]
        self.assertEqual(info, {"name": httpmcp.SERVER_NAME,
                                "version": brainversion.VERSION})

    def test_the_dockerfile_labels_an_unreleased_build_as_one(self):
        """A build that forgets to pass the version must not inherit a number
        that looks like a release."""
        dockerfile = (Path(__file__).resolve().parent.parent
                      / "deploy" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("ARG BRAIN_VERSION=unreleased", dockerfile)
        self.assertIn('org.opencontainers.image.version="${BRAIN_VERSION}"', dockerfile)


class S3DeleteTests(unittest.TestCase):
    """Prevents: an operator learning to ignore the DELETE line.

    The bucket has a three-day object lock, and it is load-bearing: the
    credential that uploads a snapshot can also delete one, so without the lock
    a single stolen key erases every recovery point it just made. The lock
    necessarily also refuses a legitimate prune of anything younger than the
    window — with a 409, every night, forever.

    Filing that under the same heading as a real failure is how the message
    that WOULD matter (a 403 from a credential that lost its permissions)
    becomes invisible. So `s3_delete` has three returns rather than two, and
    this exercises all three through the real function with only the HTTP layer
    replaced."""

    LIB = None

    @classmethod
    def setUpClass(cls):
        cls.LIB = (Path(__file__).resolve().parent.parent
                   / "deploy" / "backup" / "lib-s3.sh")

    def delete_returning(self, status):
        """Source the real library, stub only `_s3_request`, call s3_delete."""
        script = (
            'set -uo pipefail\n'
            'S3_BUCKET=b; S3_ENDPOINT=https://example.invalid; S3_REGION=auto\n'
            'source "%s"\n'
            '_s3_request() { printf "%s"; }\n'
            's3_delete some/key.tar.gz.age\n'
            'echo "rc=$?"\n' % (self.LIB, status))
        return subprocess.run(["/bin/bash", "-c", script],
                              capture_output=True, text=True)

    def test_a_successful_delete_returns_zero(self):
        result = self.delete_returning("204")
        self.assertIn("rc=0", result.stdout)
        self.assertEqual(result.stderr.strip(), "")

    def test_a_retention_lock_is_its_own_return_and_is_silent(self):
        """Silent on stderr because it is not a failure; distinct because the
        caller has to say something different about it."""
        result = self.delete_returning("409")
        self.assertIn("rc=2", result.stdout)
        self.assertNotIn("DELETE", result.stderr)

    def test_a_permission_failure_is_loud_and_is_not_the_lock(self):
        result = self.delete_returning("403")
        self.assertIn("rc=1", result.stdout)
        self.assertIn("DELETE", result.stderr)
        self.assertIn("403", result.stderr)

    def test_the_prune_caller_handles_all_three(self):
        """The library can be as careful as it likes if the caller writes
        `s3_delete "$key" && note ...`, which is what it used to do: a 409 and
        a 403 were both simply nothing."""
        snapshot = (Path(__file__).resolve().parent.parent
                    / "deploy" / "backup" / "snapshot.sh").read_text(encoding="utf-8")
        prune = snapshot[snapshot.index("prune_class() {"):snapshot.index("prune_class daily")]
        self.assertIn("retention lock", prune)
        self.assertIn("could not prune", prune)
        self.assertNotIn('s3_delete "$key" && note', prune)


class SbomTests(unittest.TestCase):
    """Prevents: a bill of materials that is a correct document about a broken
    promise.

    The image's dependency set is 127 Debian packages and NO third-party Python
    at all — that emptiness is a design decision restated in half a dozen
    places, and it is the kind of decision a single `pip install` in a
    Dockerfile undoes without anyone noticing. So the generator asserts it
    rather than reporting it: a wheel makes the build fail, not the document
    longer."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="brain-sbom-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.script = (Path(__file__).resolve().parent.parent / "deploy" / "sbom.sh")
        self.fake = self.dir / "docker"

    def write_fake_docker(self, pip_output):
        self.fake.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  inspect)\n"
            "    case \"$3\" in\n"
            "      *'.Id'*) echo 'sha256:deadbeef' ;;\n"
            "      *Created*) echo '2026-08-23T00:00:00Z' ;;\n"
            "      *Architecture*) echo arm64 ;;\n"
            "      *base.name*) echo 'python:3.12-slim-bookworm' ;;\n"
            "      *image.version*) echo '0.2.0' ;;\n"
            "      *) echo '' ;;\n"
            "    esac ;;\n"
            "  run)\n"
            "    case \"$*\" in\n"
            "      *platform*) echo '3.12.14' ;;\n"
            "      *pip*) printf '%s' \"" + pip_output + "\" ;;\n"
            "      *dpkg-query*) printf 'libc6\\t2.36-9\\tarm64\\ngit\\t1:2.39.5\\tarm64\\n' ;;\n"
            "    esac ;;\n"
            "esac\n",
            encoding="utf-8")
        self.fake.chmod(0o755)

    def run_sbom(self):
        env = dict(os.environ)
        env["DOCKER"] = str(self.fake)
        return subprocess.run(["/bin/sh", str(self.script), "brain:0.2.0"],
                              env=env, capture_output=True, text=True)

    def test_it_emits_a_bill_of_materials_for_a_clean_image(self):
        self.write_fake_docker("pip==25.0.1\n")
        result = self.run_sbom()
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(result.stdout)
        self.assertEqual(document["bomFormat"], "CycloneDX")
        names = [c["name"] for c in document["components"]]
        self.assertIn("brain", names)
        self.assertIn("git", names)
        properties = {p["name"]: p["value"] for p in document["metadata"]["properties"]}
        self.assertEqual(properties["python.third_party_packages"], "0")

    def test_a_third_party_wheel_refuses_the_build(self):
        self.write_fake_docker("pip==25.0.1\nPyJWT==2.9.0\n")
        result = self.run_sbom()
        self.assertEqual(result.returncode, 77)
        self.assertIn("PyJWT", result.stderr)
        self.assertEqual(result.stdout.strip(), "",
                         "a refused run must not also emit a document")

    def test_the_local_digest_is_not_passed_off_as_a_registry_one(self):
        """Nothing here is pushed, so no registry manifest digest exists. The
        first version of this called `.Id` the config id, which is what it is
        under the classic image store and NOT what it is under containerd —
        this build's `.Id` was the OCI index digest. Both are local; neither is
        a registry digest; the document says so rather than picking a name that
        implies one."""
        self.write_fake_docker("pip==25.0.1\n")
        document = json.loads(self.run_sbom().stdout)
        properties = {p["name"]: p["value"] for p in document["metadata"]["properties"]}
        self.assertIn("image.id", properties)
        self.assertNotIn("image.digest", properties)
        self.assertIn("NOT a registry manifest digest", properties["image.id_kind"])


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

    def compose_block(self, opener, indent):
        """The lines of one compose block, from its opening line to the next
        key at the same indentation. No YAML parser, like _compose_services."""
        lines = self.compose.read_text(encoding="utf-8").splitlines()
        start = lines.index(opener)
        block = []
        for line in lines[start + 1:]:
            if line.strip() and not line.lstrip().startswith("#") \
                    and len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line.strip())
        return block

    def test_the_brain_and_maintenance_follow_the_deployments_time_zone(self):
        """E7: `created`, the inbox file name and the drop box's daily cap are
        the container's local date, so the container must be told the zone the
        brain's owner lives in. UTC stays the default; `captured_at` is UTC
        whatever this says."""
        self.assertIn("TZ: ${BRAIN_TZ:-UTC}", self.compose_block("x-brain-env: &brain-env", 0))
        self.assertIn("environment: *brain-env", self.compose_block("  brain:", 2))
        self.assertIn("<<: *brain-env", self.compose_block("  maintenance:", 2))
        example = (self.compose.parent / "env.example").read_text(encoding="utf-8")
        self.assertIn("BRAIN_TZ", example)

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

    def test_the_runbook_names_compose_services_that_exist(self):
        """The same check, against the other place these names are written.

        The unit files had this bug and so did the runbook, in four commands
        across three procedures: they all said `brain-maintenance`, which is
        the systemd unit and the container prefix, where compose wanted the
        SERVICE `maintenance`. Every one of them would have failed with "no
        such service" at the moment somebody reached for it — during an
        upgrade, or while recovering corrupt data."""
        known = _compose_services(self.compose)
        runbook = (Path(__file__).resolve().parent.parent
                   / "setup" / "runbooks" / "remote-brain.md")
        checked = 0
        for command in _shell_commands(runbook.read_text(encoding="utf-8")):
            target = _compose_run_service(command)
            if target is None:
                continue
            checked += 1
            self.assertIn(target, known,
                          "the runbook runs compose service %r, which "
                          "compose.yaml does not define:\n  %s"
                          % (target, command.strip()))
        self.assertGreaterEqual(checked, 4, "no compose `run` command was examined")

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



def _compose_networks(path: Path):
    """The top-level networks of compose.yaml as {name: {key: value}}, flattened.

    Same section-aware line scan as `_compose_services`. Each network's keys are
    its own (`internal`) and those one level down (`driver_opts`' entries), which
    is all these tests need."""
    networks, current, in_section = {}, None, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line[:1].isalpha():
            in_section = line.rstrip() == "networks:"
            continue
        stripped = line.strip()
        if not in_section or not stripped or stripped.startswith("#"):
            continue
        if line.startswith("  ") and not line.startswith("   "):
            current = networks.setdefault(stripped.rstrip(":"), {})
        elif current is not None and ":" in stripped:
            key, _, value = stripped.partition(":")
            current[key.strip()] = value.strip()
    return networks


class ComposeNetworkIsolationTests(unittest.TestCase):
    """Prevents: an internal network that still gives the host an address.

    `internal: true` stops routing off the segment, but Docker still puts an
    address on the bridge, and a container can reach any host service listening
    there. Gateway mode `isolated` is what removes it. Losing that line from a
    network, or adding an internal network without it, is silent."""

    GATEWAY_MODE = "com.docker.network.bridge.gateway_mode_ipv4"
    GATEWAY_MODE_V6 = "com.docker.network.bridge.gateway_mode_ipv6"

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.networks = _compose_networks(root / "deploy" / "compose.yaml")

    def test_the_scan_finds_the_networks_it_is_meant_to_check(self):
        self.assertEqual(sorted(self.networks), ["egress", "embed", "mcp"])

    def test_every_internal_network_is_isolated_from_the_host(self):
        internal = [n for n, v in self.networks.items() if v.get("internal") == "true"]
        self.assertEqual(sorted(internal), ["embed", "mcp"])
        for name in internal:
            for option in (self.GATEWAY_MODE, self.GATEWAY_MODE_V6):
                self.assertEqual(self.networks[name].get(option), "isolated",
                                 "internal network %r leaves the host an address on its "
                                 "bridge: set %s: isolated" % (name, option))

    def test_egress_is_not_internal(self):
        """It is the only way out; making it internal would cut git, backups and the tunnel."""
        self.assertNotEqual(self.networks["egress"].get("internal"), "true")
        self.assertNotIn(self.GATEWAY_MODE, self.networks["egress"])
        self.assertNotIn(self.GATEWAY_MODE_V6, self.networks["egress"])


class EmbedderDeploymentTests(unittest.TestCase):
    """Prevents: search by meaning that is wired in one place and not another.

    The pins live in deploy/embedder.env (read by a root script with a strict
    parser) and are repeated in compose.yaml, which cannot interpolate that file;
    the units that feed it, and the network that holds it, are separate files.
    Each of them is wrong quietly: a stale digest runs the old image, an embedder
    on the wrong network is reachable or unreachable without any error."""

    LINE = re.compile(r"^[A-Z][A-Z0-9_]*=[A-Za-z0-9._:/@+-]+$")

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.deploy = root / "deploy"
        cls.compose = (cls.deploy / "compose.yaml").read_text(encoding="utf-8")
        cls.pins = {}
        for line in (cls.deploy / "embedder.env").read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#"):
                key, _, value = line.partition("=")
                cls.pins[key] = value

    def block(self, opener, indent):
        lines = self.compose.splitlines()
        start = lines.index(opener)
        out = []
        for line in lines[start + 1:]:
            if line.strip() and not line.lstrip().startswith("#") \
                    and len(line) - len(line.lstrip()) <= indent:
                break
            out.append(line.strip())
        return out

    def test_the_pins_file_is_key_value_lines_a_strict_parser_can_read(self):
        text = (self.deploy / "embedder.env").read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertRegex(line, self.LINE, "not plain KEY=VALUE: %r" % line)
        self.assertEqual(sorted(self.pins), sorted(
            ["EMBED_IMAGE", "MODEL_REPO", "MODEL_REV", "MODEL_FILE", "MODEL_SHA256",
             "MODEL_BYTES", "MODEL_ALIAS"]))
        self.assertRegex(self.pins["EMBED_IMAGE"], r"@sha256:[0-9a-f]{64}$")
        self.assertRegex(self.pins["MODEL_REV"], r"^[0-9a-f]{40}$")
        self.assertRegex(self.pins["MODEL_SHA256"], r"^[0-9a-f]{64}$")
        self.assertTrue(self.pins["MODEL_BYTES"].isdigit())

    def test_compose_repeats_the_pins_exactly(self):
        lines = self.block("  embedder:", 2)
        self.assertIn("image: " + self.pins["EMBED_IMAGE"], lines)
        self.assertIn('- "/models/%s"' % self.pins["MODEL_FILE"], lines)
        self.assertIn('- "%s"' % self.pins["MODEL_ALIAS"], lines)
        self.assertIn('- "last"', lines)      # Qwen3-Embedding pools by its last token, not the mean

    def test_the_embedder_is_hardened_unpublished_and_on_the_internal_network_alone(self):
        lines = self.block("  embedder:", 2)
        self.assertIn("<<: *hardening", lines)
        self.assertIn("logging: *logging", lines)
        self.assertFalse([l for l in lines if l.startswith("ports:")], "the embedder publishes a port")
        self.assertIn("memswap_limit: 1g", lines)
        self.assertIn("memory: 1G", lines)
        self.assertIn('cpus: "2.0"', lines)
        self.assertIn("- ${BRAIN_HOST_MODELS:-/srv/brain/models}:/models:ro", lines)
        self.assertIn("- embed", lines)
        self.assertNotIn("- egress", lines)
        self.assertNotIn("egress:", lines)
        self.assertTrue([l for l in lines if "/health" in l], "no healthcheck on /health")

    def test_the_embed_network_is_internal_and_holds_exactly_three_services(self):
        self.assertIn("internal: true", self.block("  embed:", 2))
        holders = []
        for name in ("brain", "cloudflared", "maintenance", "embedder"):
            lines = self.block("  %s:" % name, 2)
            if "- embed" in lines or "embed:" in lines:
                holders.append(name)
        self.assertEqual(sorted(holders), ["brain", "embedder", "maintenance"])

    def test_the_brain_does_not_wait_for_the_embedder(self):
        """Keyword search, capture and the rest must work with the embedder down."""
        self.assertNotIn("depends_on:", self.block("  brain:", 2))
        self.assertNotIn("depends_on:", self.block("  maintenance:", 2))

    def test_brain_and_maintenance_are_pointed_at_the_embedder_and_the_mode_comes_from_env(self):
        env = self.block("x-brain-env: &brain-env", 0)
        url = next(l.split(": ", 1)[1].strip() for l in env if l.strip().startswith("BRAIN_EMBEDDER_URL:"))
        self.assertEqual(url, "http://embedder:8080")
        self.assertEqual(urlparse(url.rstrip("/") + "/v1/embeddings").path, "/v1/embeddings")
        self.assertIn("BRAIN_VECTORS: ${BRAIN_VECTORS:-shadow}", env)
        self.assertIn("environment: *brain-env", self.block("  brain:", 2))
        self.assertIn("<<: *brain-env", self.block("  maintenance:", 2))
        example = (self.deploy / "env.example").read_text(encoding="utf-8")
        self.assertIn("\nBRAIN_VECTORS=shadow\n", example)
        self.assertIn("BRAIN_HOST_MODELS=", example)

    def test_sync_and_nightly_maintenance_build_the_vectors_after_their_own_step(self):
        for unit, after in (("brain-sync.service", " maintenance sync"),
                            ("brain-maintenance.service", " maintenance index --quiet")):
            text = (self.deploy / "systemd" / unit).read_text(encoding="utf-8")
            execs = [l for l in text.splitlines() if l.startswith("ExecStart=")]
            index = next(i for i, l in enumerate(execs) if l.endswith(after))
            self.assertTrue(execs[index + 1].endswith(" maintenance index --vectors --quiet"),
                            "%s does not embed right after its own step" % unit)
            self.assertFalse(execs[index + 1].startswith("ExecStart=-"),
                             "tolerated by the unit, not by the command: the engine must exit 0")


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


class ServedMarkerWiringTests(unittest.TestCase):
    """2.2: brain-http marks the folder it serves, so consolidation refuses it."""

    def test_brain_http_marks_the_data_folder_it_serves(self):
        sys.path.insert(0, str(ENGINE / "bin"))
        from brainlib import served
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        data_root = make_data_root(base)
        state_dir = base / "state"
        state_dir.mkdir()
        key = base / "principal.key"
        key.write_bytes(PRINCIPAL_KEY)
        key.chmod(0o600)
        config = base / "brain-http.json"
        config.write_text(json.dumps({
            "team_domain": TEAM, "owner_email": OWNER, "principal_key_file": str(key),
            "endpoints": [{"hostname": "brain.test", "aud": AUD_WRITE,
                           "profile": "capture"}]}), encoding="utf-8")
        self.assertIsNone(served.check(data_root, env={}))
        env = dict(os.environ, BRAIN_DATA_ROOT=str(data_root),
                   BRAIN_STATE_DIR=str(state_dir))
        env.pop("BRAIN_SERVED", None)
        proc = subprocess.Popen([sys.executable, str(ENGINE / "bin" / "brain-http"),
                                 "--config", str(config), "--bind", "127.0.0.1",
                                 "--port", "0"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 30
            while time.time() < deadline and proc.poll() is None:
                if served.check(data_root, env={}):
                    break
                time.sleep(0.2)
            reason = served.check(data_root, env={})
        finally:
            proc.terminate()
            try:
                _out, err = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                _out, err = proc.communicate()
        self.assertIsNotNone(reason, "brain-http served the folder without marking it:\n"
                             + err.decode("utf-8", "replace")[-1500:])
        self.assertIn("brain-http", reason)

    def test_brain_http_finishes_an_interrupted_install_before_serving(self):
        """After a reboot mid-install, the server must not serve a half-checked-out tree
        until the five-minute timer happens to come round."""
        sys.path.insert(0, str(ENGINE / "bin"))
        from brainlib import served, sync
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        data_root = make_data_root(base)
        state_dir = base / "state"
        state_dir.mkdir()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=data_root,
                              capture_output=True, text=True).stdout.strip()
        (state_dir / sync.INSTALL_MARK).write_text(json.dumps({"from": head, "to": head}))
        facts = data_root / "knowledge" / "reference" / "tunnel-facts.md"
        facts.write_text("half of an interrupted checkout\n", encoding="utf-8")
        key = base / "principal.key"
        key.write_bytes(PRINCIPAL_KEY)
        key.chmod(0o600)
        config = base / "brain-http.json"
        config.write_text(json.dumps({
            "team_domain": TEAM, "owner_email": OWNER, "principal_key_file": str(key),
            "endpoints": [{"hostname": "brain.test", "aud": AUD_WRITE,
                           "profile": "capture"}]}), encoding="utf-8")
        env = dict(os.environ, BRAIN_DATA_ROOT=str(data_root), BRAIN_STATE_DIR=str(state_dir))
        env.pop("BRAIN_SERVED", None)
        proc = subprocess.Popen([sys.executable, str(ENGINE / "bin" / "brain-http"),
                                 "--config", str(config), "--bind", "127.0.0.1",
                                 "--port", "0"],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.time() + 30
            while time.time() < deadline and proc.poll() is None:
                if served.check(data_root, env={}):
                    break
                time.sleep(0.2)
        finally:
            proc.terminate()
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
        self.assertFalse((state_dir / sync.INSTALL_MARK).exists(), "the install was not finished")
        self.assertNotIn("interrupted", facts.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
