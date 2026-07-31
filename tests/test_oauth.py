"""Tests for the authorization server — the half that lets a hosted assistant in.

Built to the specification, not to a vendor: no test here names one, and none
should. What is asserted is what an RFC requires, because the whole claim of
this work is that any client speaking the spec works, and a test suite full of
one company's quirks would quietly turn that into a lie.

Three rules from the predecessor plans hold here too: no test binds a public
interface, no test reaches the network (the metadata fetcher takes its
transport as a parameter), and no test sleeps — every clock and TTL is a
parameter.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT / "bin"))
from brainlib import eventlog  # noqa: E402
from brainlib import mcp  # noqa: E402
from brainlib import oauth  # noqa: E402
from brainlib import serve  # noqa: E402

BEARER = "fixture-value-for-tests-only"
PUBLIC = "https://brain.example/mcp"


class ConfigTests(unittest.TestCase):
    def test_the_issuer_is_the_origin_with_no_path(self):
        """An issuer WITH a path sends conforming clients probing three
        different well-known URLs in a defined order. An origin has one."""
        cfg = oauth.Config(PUBLIC)
        self.assertEqual(cfg.issuer, "https://brain.example")

    def test_the_resource_is_the_url_byte_for_byte(self):
        cfg = oauth.Config(PUBLIC)
        self.assertEqual(cfg.protected_resource_metadata()["resource"], PUBLIC)

    def test_a_port_survives_into_the_issuer(self):
        cfg = oauth.Config("https://brain.example:8443/mcp")
        self.assertEqual(cfg.issuer, "https://brain.example:8443")

    def test_the_prm_url_uses_the_path_inserted_form(self):
        """RFC 9728's canonical location when the resource has a path."""
        self.assertEqual(oauth.Config(PUBLIC).prm_url,
                         "https://brain.example/.well-known/oauth-protected-resource/mcp")

    def test_the_prm_url_is_the_root_when_the_resource_has_no_path(self):
        self.assertEqual(oauth.Config("https://brain.example").prm_url,
                         "https://brain.example/.well-known/oauth-protected-resource")


class PublicUrlValidationTests(unittest.TestCase):
    """Every rule here is a real failure that is invisible from the operator's
    side — the connector just says it cannot reach the server."""

    def refuses(self, url):
        with self.assertRaises(oauth.ConfigError):
            oauth.parse_public_url(url)

    def test_an_https_url_is_accepted(self):
        self.assertEqual(oauth.parse_public_url(PUBLIC), PUBLIC)

    def test_empty_is_refused(self):
        self.refuses("")

    def test_a_query_string_is_refused(self):
        self.refuses("https://brain.example/mcp?token=x")

    def test_a_fragment_is_refused(self):
        self.refuses("https://brain.example/mcp#here")

    def test_a_trailing_slash_is_refused_rather_than_normalised(self):
        """Silently normalising would change the identifier the operator has to
        retype into a client, which is the one string that must match."""
        self.refuses("https://brain.example/mcp/")

    def test_plain_http_on_a_public_host_is_refused(self):
        self.refuses("http://brain.example/mcp")

    def test_plain_http_on_loopback_is_allowed_for_testing(self):
        self.assertEqual(oauth.parse_public_url("http://127.0.0.1:8787/mcp"),
                         "http://127.0.0.1:8787/mcp")
        self.assertEqual(oauth.parse_public_url("http://localhost:8787/mcp"),
                         "http://localhost:8787/mcp")

    def test_a_missing_host_is_refused(self):
        self.refuses("https:///mcp")

    def test_a_non_http_scheme_is_refused(self):
        self.refuses("ftp://brain.example/mcp")


class MetadataDocumentTests(unittest.TestCase):
    def setUp(self):
        self.cfg = oauth.Config(PUBLIC)
        self.doc = self.cfg.authorization_server_metadata()

    def test_pkce_s256_is_advertised(self):
        """A conforming client MUST refuse to start a flow if this is absent —
        it is the only way PKCE support can be discovered."""
        self.assertEqual(self.doc["code_challenge_methods_supported"], ["S256"])

    def test_cimd_is_advertised_as_a_pair(self):
        """Both, or a client falls back to registration. They are checked
        together because a CIMD client is a PUBLIC client at the token
        endpoint, so `none` is what makes the pair coherent."""
        self.assertIs(self.doc["client_id_metadata_document_supported"], True)
        self.assertIn("none", self.doc["token_endpoint_auth_methods_supported"])

    def test_no_registration_endpoint_is_advertised(self):
        """Deliberate. Dynamic client registration is DEPRECATED in the MCP
        specification, and an endpoint through which an unauthenticated caller
        creates rows is not something to ship by accident. If this assertion
        ever fails, somebody decided to add it — which is the point."""
        self.assertNotIn("registration_endpoint", self.doc)

    def test_the_issuer_matches_the_well_known_url_it_would_be_fetched_from(self):
        """A client discards metadata whose issuer differs from the origin it
        built the URL out of, and it is right to."""
        built = self.doc["issuer"] + oauth.WELL_KNOWN_AS
        self.assertEqual(built,
                         "https://brain.example/.well-known/oauth-authorization-server")

    def test_iss_is_advertised_because_it_is_sent(self):
        self.assertIs(self.doc["authorization_response_iss_parameter_supported"], True)

    def test_offline_access_is_on_the_authorization_server_only(self):
        """The MCP spec says a resource server SHOULD NOT list it: a refresh
        token is not something the RESOURCE requires. The authorization server
        must, or no client knows it can ask for one."""
        self.assertIn(oauth.SCOPE_OFFLINE, self.doc["scopes_supported"])
        self.assertNotIn(oauth.SCOPE_OFFLINE,
                         self.cfg.protected_resource_metadata()["scopes_supported"])

    def test_the_resource_names_exactly_one_authorization_server(self):
        prm = self.cfg.protected_resource_metadata()
        self.assertEqual(prm["authorization_servers"], ["https://brain.example"])


class ScopeDerivationTests(unittest.TestCase):
    def test_a_full_server_offers_both(self):
        self.assertEqual(oauth.scopes_for(None), (oauth.SCOPE_READ, oauth.SCOPE_WRITE))

    def test_a_read_only_server_offers_only_read(self):
        self.assertEqual(oauth.scopes_for(mcp.READ_ONLY_TOOLS), (oauth.SCOPE_READ,))

    def test_a_write_only_server_offers_only_write(self):
        self.assertEqual(oauth.scopes_for(mcp.WRITE_ONLY_TOOLS), (oauth.SCOPE_WRITE,))

    def test_scopes_map_back_onto_tools(self):
        self.assertEqual(oauth.tools_for_scopes((oauth.SCOPE_READ,)),
                         frozenset(mcp.READ_ONLY_TOOLS))
        self.assertEqual(
            oauth.tools_for_scopes((oauth.SCOPE_READ, oauth.SCOPE_WRITE)),
            frozenset(mcp.READ_ONLY_TOOLS) | frozenset(mcp.WRITE_ONLY_TOOLS))

    def test_the_process_still_wins(self):
        """A token granted write against a read-only process reaches no write
        tool. The flag bounds the scope, never the other way round."""
        self.assertEqual(
            oauth.tools_for_scopes((oauth.SCOPE_READ, oauth.SCOPE_WRITE),
                                   allow_tools=mcp.READ_ONLY_TOOLS),
            frozenset(mcp.READ_ONLY_TOOLS))

    def test_an_unannotated_tool_is_covered_by_no_scope(self):
        """Fails closed, like READ_ONLY_TOOLS. A tool added later with no
        readOnlyHint is grantable through neither scope."""
        self.assertEqual(oauth.tools_for_scopes(
            (oauth.SCOPE_READ, oauth.SCOPE_WRITE), allow_tools=("brain_invented",)),
            frozenset())


class OAuthServeCase(unittest.TestCase):
    """A live loopback server with the authorization server switched on."""
    ALLOW_TOOLS = None
    PUBLIC_URL = "http://127.0.0.1/mcp"      # rewritten with the real port

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.log = eventlog.EventLog(self.state / "events.jsonl")

        # Bind first, then build the config from the port that was actually
        # allocated: the public URL has to match the URL a client will use, and
        # with port 0 nobody knows that until the socket exists.
        self.server = serve.make_server(BEARER, "127.0.0.1", 0,
                                        allow_tools=self.ALLOW_TOOLS, log=self.log)
        self.port = self.server.server_address[1]
        self.public = "http://127.0.0.1:{}/mcp".format(self.port)
        self.server.oauth = oauth.Config(
            self.public, scopes=oauth.scopes_for(self.ALLOW_TOOLS))
        self.store = oauth.Store(self.state / "oauth.db")
        self.net = FakeNet()
        self.server.auth = oauth.AuthServer(
            self.server.oauth, self.store,
            fetch=oauth.MetadataFetcher(resolver=self.net.resolve,
                                        getter=self.net.get))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)

    def get(self, path, bearer=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read().decode("utf-8", "replace"))
        finally:
            conn.close()

    def post_to(self, path, body, content_type="application/x-www-form-urlencoded"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("POST", path, body=body.encode("utf-8"),
                         headers={"Content-Type": content_type})
            response = conn.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read().decode("utf-8", "replace"))
        finally:
            conn.close()

    def post_mcp(self, body, bearer=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            headers = {"Content-Type": "application/json"}
            if bearer is not None:
                headers["Authorization"] = f"Bearer {bearer}"
            conn.request("POST", "/mcp", body=json.dumps(body).encode("utf-8"),
                         headers=headers)
            response = conn.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read().decode("utf-8", "replace"))
        finally:
            conn.close()


class DiscoveryOverHttpTests(OAuthServeCase):
    def test_the_401_points_at_the_resource_metadata(self):
        """The handshake starts here. Without this header — and on a 401, not a
        200 — a client has nothing to go on and reports only that it could not
        reach the server."""
        status, headers, _body = self.post_mcp({"jsonrpc": "2.0", "id": 1,
                                                "method": "ping"})
        self.assertEqual(status, 401)
        challenge = headers["WWW-Authenticate"]
        self.assertIn("resource_metadata=", challenge)
        self.assertIn("/.well-known/oauth-protected-resource/mcp", challenge)
        self.assertIn('scope="brain:read brain:write"', challenge)

    def test_protected_resource_metadata_is_served_unauthenticated(self):
        status, _headers, body = self.get(
            "/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["resource"], self.public)

    def test_the_root_location_is_served_too(self):
        """A client that finds no pointer probes the sub-path, then the root."""
        status, _headers, body = self.get("/.well-known/oauth-protected-resource")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["resource"], self.public)

    def test_authorization_server_metadata_is_served_unauthenticated(self):
        status, _headers, body = self.get("/.well-known/oauth-authorization-server")
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["issuer"], "http://127.0.0.1:{}".format(self.port))
        self.assertEqual(doc["code_challenge_methods_supported"], ["S256"])

    def test_metadata_is_json_and_says_so(self):
        _status, headers, _body = self.get("/.well-known/oauth-authorization-server")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_the_exemption_is_exact_match_not_a_prefix(self):
        """`startswith` on a path is how a discovery exemption becomes an
        authentication bypass."""
        for path in ("/.well-known/oauth-protected-resourceX",
                     "/.well-known/oauth-protected-resource/mcp/../../mcp",
                     "/.well-known/oauth-authorization-server/extra",
                     "/.well-known/"):
            status, _headers, _body = self.get(path)
            self.assertIn(status, (401, 404),
                          f"{path} was served without a token")

    def test_the_mcp_endpoint_is_still_protected(self):
        status, _headers, _body = self.post_mcp({"jsonrpc": "2.0", "id": 1,
                                                 "method": "ping"})
        self.assertEqual(status, 401)

    def test_the_operator_token_still_works_with_oauth_on(self):
        """The regression that matters most in this whole plan."""
        status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, bearer=BEARER)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_serving_metadata_is_recorded(self):
        self.get("/.well-known/oauth-authorization-server")
        events = [e["event"] for e in self.log.read(limit=100)]
        self.assertIn("oauth_metadata_served", events)


class ReadOnlyDiscoveryTests(OAuthServeCase):
    ALLOW_TOOLS = mcp.READ_ONLY_TOOLS

    def test_a_read_only_process_advertises_only_read(self):
        """The scope cannot exceed what the process would serve, or a consent
        screen promises something that will be refused."""
        _status, _headers, body = self.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(json.loads(body)["scopes_supported"], ["brain:read"])

    def test_the_challenge_narrows_too(self):
        _status, headers, _body = self.post_mcp({"jsonrpc": "2.0", "id": 1,
                                                 "method": "ping"})
        self.assertIn('scope="brain:read"', headers["WWW-Authenticate"])


class WithoutOAuthTests(unittest.TestCase):
    """With the flag off, nothing about this server changes."""

    def setUp(self):
        self.server = serve.make_server(BEARER, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)

    def request(self, method, path, bearer=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            headers = {} if bearer is None else {"Authorization": f"Bearer {bearer}"}
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders())
        finally:
            conn.close()

    def test_the_challenge_is_unchanged(self):
        _status, headers = self.request("POST", "/mcp")
        self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="brain"')

    def test_the_well_known_paths_are_not_exempt(self):
        """No authorization server means no discovery, and a metadata document
        served by a server that cannot issue a token is worse than a 401."""
        status, _headers = self.request("GET", "/.well-known/oauth-protected-resource")
        self.assertEqual(status, 401)


class CliFlagTests(unittest.TestCase):
    """`brain serve --oauth` refuses, at startup, everything it cannot do."""

    def run_serve(self, argv):
        import io
        import contextlib
        from brainlib import osbackend

        class _Store(osbackend.FileKeystore):
            pass

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = osbackend.FileKeystore(Path(tmp.name))
        store.set(serve.TOKEN_NAME, BEARER)
        # Both otherwise resolve to this MACHINE's real state directory, so a
        # test without them writes an issued-token database and an event log
        # into the developer's own home.
        self.oauth_store = oauth.Store(Path(tmp.name) / "oauth.db")
        self.log = eventlog.EventLog(Path(tmp.name) / "events.jsonl")
        err = io.StringIO()
        started = {}

        def fake_run(server):
            started["public"] = getattr(server, "oauth", None)

        with contextlib.redirect_stderr(err):
            code = serve.run_serve(argv, store=store, run=fake_run,
                                   oauth_store=self.oauth_store,
                                   log=self.log)
        return code, err.getvalue(), started

    def test_oauth_without_public_url_refuses(self):
        code, err, started = self.run_serve(["--oauth"])
        self.assertEqual(code, 2)
        self.assertIn("--public-url", err)
        self.assertEqual(started, {})

    def test_a_bad_public_url_refuses_with_the_reason(self):
        code, err, _started = self.run_serve(
            ["--oauth", "--public-url", "http://brain.example/mcp"])
        self.assertEqual(code, 2)
        self.assertIn("https", err)

    def test_drop_box_and_oauth_are_two_deployments(self):
        """A drop box is an endpoint an untrusted bot holds a fixed token for.
        OAuth's consent step assumes a human at a browser, and there is none."""
        code, err, _started = self.run_serve(
            ["--drop-box", "--source", "support-bot", "--oauth",
             "--public-url", "https://brain.example/mcp"])
        self.assertEqual(code, 2)
        self.assertIn("--drop-box", err)

    def test_public_url_without_oauth_refuses(self):
        """Half a pairing is a surprise waiting to happen — the same rule
        --source and --drop-box already follow."""
        code, err, _started = self.run_serve(
            ["--public-url", "https://brain.example/mcp"])
        self.assertEqual(code, 2)
        self.assertIn("--oauth", err)

    def test_a_good_pairing_starts(self):
        code, _err, started = self.run_serve(
            ["--oauth", "--public-url", "https://brain.example/mcp"])
        self.assertEqual(code, 0)
        self.assertEqual(started["public"].resource, "https://brain.example/mcp")

    def test_the_banner_names_the_public_url(self):
        _code, err, _started = self.run_serve(
            ["--oauth", "--public-url", "https://brain.example/mcp"])
        self.assertIn("https://brain.example/mcp", err)

    def test_the_help_documents_the_flags(self):
        self.assertIn("--oauth", serve.USAGE)
        self.assertIn("--public-url", serve.USAGE)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Client resolution. The security-critical half: an authorization request names
# a client_id, and under CIMD that id is a URL this server FETCHES. Everything
# below is about the fact that an attacker chooses the target of that request.
# ---------------------------------------------------------------------------

CIMD_URL = "https://app.example/oauth/client.json"
CIMD_DOC = {
    "client_id": CIMD_URL,
    "client_name": "An Assistant",
    "redirect_uris": ["https://app.example/callback",
                      "http://127.0.0.1/callback",
                      "http://localhost/callback"],
    "token_endpoint_auth_method": "none",
}


class FakeNet:
    """A resolver and a getter, so no test here touches the network.

    The seam is deliberately BELOW the address check: `resolved` is what DNS
    would return and `pages` is what a socket would, so a test can put a
    private address behind a public-looking hostname — which is the whole
    attack this code exists to stop.
    """

    def __init__(self, resolved=None, pages=None):
        # `is None`, not `or`: an EMPTY mapping is a meaningful fixture — it is
        # how a test says "this host does not resolve" or "this URL 404s" — and
        # `or` would silently substitute the working defaults for both.
        self.resolved = ({"app.example": ["93.184.216.34"]}
                         if resolved is None else resolved)
        self.pages = ({CIMD_URL: (200, json.dumps(CIMD_DOC).encode())}
                      if pages is None else pages)
        self.connected_to = []

    def resolve(self, host):
        if host not in self.resolved:
            raise OSError("no such host")
        return list(self.resolved[host])

    def get(self, host, ip, port, path, timeout, max_bytes):
        self.connected_to.append(ip)
        url = "https://{}{}".format(host, path)
        if url not in self.pages:
            return 404, b""
        status, body = self.pages[url]
        return status, body[:max_bytes + 1]


def fetcher(net=None, **kwargs):
    net = net or FakeNet()
    made = oauth.MetadataFetcher(resolver=net.resolve, getter=net.get, **kwargs)
    made.net = net
    return made


class CimdShapeTests(unittest.TestCase):
    """What a client_id has to look like before anything is fetched at all."""

    def refuses(self, url, reason, **kwargs):
        with self.assertRaises(oauth.ClientRefused) as caught:
            fetcher(**kwargs)(url)
        self.assertEqual(caught.exception.reason, reason)

    def test_a_well_formed_document_resolves(self):
        client = fetcher()(CIMD_URL)
        self.assertEqual(client.client_id, CIMD_URL)
        self.assertEqual(client.name, "An Assistant")
        self.assertEqual(client.kind, "cimd")

    def test_plain_http_is_refused(self):
        self.refuses("http://app.example/client.json", "not_https")

    def test_a_url_with_no_path_is_refused(self):
        """The spec requires a path component, and a bare origin as a client_id
        is indistinguishable from a typo."""
        self.refuses("https://app.example", "no_path")
        self.refuses("https://app.example/", "no_path")

    def test_a_client_id_that_disagrees_with_its_url_is_refused(self):
        """Otherwise anybody can host a document claiming to be somebody
        else's client, and the consent screen shows THEIR name."""
        doc = dict(CIMD_DOC, client_id="https://someone.else/client.json")
        net = FakeNet(pages={CIMD_URL: (200, json.dumps(doc).encode())})
        self.refuses(CIMD_URL, "client_id_mismatch", net=net)

    def test_a_document_without_redirect_uris_is_refused(self):
        doc = {k: v for k, v in CIMD_DOC.items() if k != "redirect_uris"}
        net = FakeNet(pages={CIMD_URL: (200, json.dumps(doc).encode())})
        self.refuses(CIMD_URL, "bad_document", net=net)

    def test_a_document_without_a_name_is_refused(self):
        """The name is what a human reads on the consent screen. A client with
        no name is one the operator cannot make a decision about."""
        doc = {k: v for k, v in CIMD_DOC.items() if k != "client_name"}
        net = FakeNet(pages={CIMD_URL: (200, json.dumps(doc).encode())})
        self.refuses(CIMD_URL, "bad_document", net=net)

    def test_a_document_that_is_not_json_is_refused(self):
        net = FakeNet(pages={CIMD_URL: (200, b"<html>not json</html>")})
        self.refuses(CIMD_URL, "bad_document", net=net)

    def test_a_document_that_is_a_json_array_is_refused(self):
        net = FakeNet(pages={CIMD_URL: (200, b"[1,2,3]")})
        self.refuses(CIMD_URL, "bad_document", net=net)

    def test_a_non_200_is_refused(self):
        net = FakeNet(pages={})
        self.refuses(CIMD_URL, "fetch_failed", net=net)

    def test_a_redirect_is_not_followed(self):
        """A redirect is a second target, and it was never address-checked."""
        net = FakeNet(pages={CIMD_URL: (302, b"")})
        self.refuses(CIMD_URL, "redirected", net=net)

    def test_an_oversized_body_is_refused_rather_than_truncated(self):
        """Truncating and parsing what fits would let a 10 GB response decide
        what this server believes about a client."""
        big = json.dumps(dict(CIMD_DOC, padding="x" * 100_000)).encode()
        net = FakeNet(pages={CIMD_URL: (200, big)})
        self.refuses(CIMD_URL, "too_big", net=net, max_bytes=1024)


class SsrfTests(unittest.TestCase):
    """The authorization endpoint is unauthenticated, so anybody on the
    internet can make this server issue an outbound request to a host they
    named. On a rented VM the interesting targets are localhost and
    169.254.169.254 — the cloud metadata service, which hands out credentials.
    """

    def refuses_address(self, address):
        net = FakeNet(resolved={"app.example": [address]})
        with self.assertRaises(oauth.ClientRefused) as caught:
            fetcher(net=net)(CIMD_URL)
        self.assertEqual(caught.exception.reason, "blocked_address")
        self.assertEqual(net.connected_to, [],
                         f"{address} was CONNECTED TO before being refused")

    def test_loopback_is_refused(self):
        self.refuses_address("127.0.0.1")

    def test_ipv6_loopback_is_refused(self):
        self.refuses_address("::1")

    def test_private_ranges_are_refused(self):
        for address in ("10.1.2.3", "172.16.0.1", "192.168.1.1"):
            self.refuses_address(address)

    def test_the_cloud_metadata_address_is_refused(self):
        self.refuses_address("169.254.169.254")

    def test_ipv6_unique_local_is_refused(self):
        self.refuses_address("fd00::1")

    def test_an_ipv4_mapped_ipv6_loopback_is_refused(self):
        """`::ffff:127.0.0.1` is loopback, and Python's `is_loopback` says
        False for it. Unmapping first is the only thing that catches it."""
        self.refuses_address("::ffff:127.0.0.1")

    def test_the_unspecified_address_is_refused(self):
        self.refuses_address("0.0.0.0")

    def test_one_bad_address_among_good_ones_refuses_the_whole_fetch(self):
        """A host that resolves to both a public and a private address is a
        DNS-rebinding setup. Picking the good one and proceeding would mean the
        check passed on an address the connection might not use."""
        net = FakeNet(resolved={"app.example": ["93.184.216.34", "127.0.0.1"]})
        with self.assertRaises(oauth.ClientRefused) as caught:
            fetcher(net=net)(CIMD_URL)
        self.assertEqual(caught.exception.reason, "blocked_address")

    def test_the_connection_is_pinned_to_the_checked_address(self):
        """Resolving, checking, and then connecting BY NAME leaves a rebinding
        window between the two. The connection uses the address that was
        checked, and this asserts it."""
        net = FakeNet(resolved={"app.example": ["93.184.216.34"]})
        fetcher(net=net)(CIMD_URL)
        self.assertEqual(net.connected_to, ["93.184.216.34"])

    def test_a_host_that_does_not_resolve_is_refused(self):
        net = FakeNet(resolved={})
        with self.assertRaises(oauth.ClientRefused) as caught:
            fetcher(net=net)(CIMD_URL)
        self.assertEqual(caught.exception.reason, "dns_failure")


class FetchCacheTests(unittest.TestCase):
    """Without a cache, /authorize is an outbound-request amplifier: it is
    unauthenticated, and every call makes this server fetch a URL of the
    caller's choosing."""

    def setUp(self):
        self.now = [1000.0]
        self.net = FakeNet()
        self.fetch = oauth.MetadataFetcher(resolver=self.net.resolve,
                                           getter=self.net.get,
                                           clock=lambda: self.now[0])
        self.fetches = []
        original = self.net.get

        def counting(*args, **kwargs):
            self.fetches.append(args[0])
            return original(*args, **kwargs)
        self.fetch._get = counting

    def test_a_second_resolution_does_not_refetch(self):
        self.fetch(CIMD_URL)
        self.fetch(CIMD_URL)
        self.assertEqual(len(self.fetches), 1)

    def test_a_failure_is_cached_too(self):
        """A negative cache is what stops a loop against a broken client_id
        from multiplying into a loop of outbound requests."""
        net = FakeNet(pages={})
        fetch = oauth.MetadataFetcher(resolver=net.resolve, getter=net.get,
                                      clock=lambda: self.now[0])
        seen = []
        fetch._get = lambda *a, **k: (seen.append(1), net.get(*a, **k))[1]
        for _ in range(5):
            with self.assertRaises(oauth.ClientRefused):
                fetch(CIMD_URL)
        self.assertEqual(len(seen), 1)

    def test_the_cache_expires(self):
        self.fetch(CIMD_URL)
        self.now[0] += 100_000
        self.fetch(CIMD_URL)
        self.assertEqual(len(self.fetches), 2)

    def test_the_cache_is_bounded(self):
        """An unbounded cache keyed on a caller-supplied URL is a memory
        exhaustion primitive reachable without a token — the same reasoning
        Limiter's table already carries."""
        pages = {}
        for i in range(50):
            url = "https://app.example/c{}.json".format(i)
            pages[url] = (200, json.dumps(dict(CIMD_DOC, client_id=url)).encode())
        net = FakeNet(pages=pages)
        fetch = oauth.MetadataFetcher(resolver=net.resolve, getter=net.get,
                                      cache_size=8, clock=lambda: self.now[0])
        for url in pages:
            fetch(url)
        self.assertLessEqual(fetch.cached(), 8)


class RedirectMatchingTests(unittest.TestCase):
    def setUp(self):
        self.client = fetcher()(CIMD_URL)

    def test_an_exact_uri_matches(self):
        self.assertTrue(self.client.matches_redirect("https://app.example/callback"))

    def test_a_different_uri_does_not(self):
        self.assertFalse(self.client.matches_redirect("https://evil.example/callback"))

    def test_a_path_that_is_a_prefix_does_not_match(self):
        self.assertFalse(self.client.matches_redirect("https://app.example/callback2"))

    def test_a_loopback_uri_matches_on_any_port(self):
        """RFC 8252: a native client binds an ephemeral port, so the port is
        ignored for loopback. A generic rule from the RFC, not an
        accommodation for one client."""
        self.assertTrue(self.client.matches_redirect("http://127.0.0.1:53119/callback"))
        self.assertTrue(self.client.matches_redirect("http://localhost:3000/callback"))

    def test_a_loopback_uri_with_a_different_path_does_not_match(self):
        self.assertFalse(self.client.matches_redirect("http://127.0.0.1:53119/steal"))

    def test_loopback_hostnames_do_not_cross_match(self):
        """`localhost` and `127.0.0.1` are registered separately by clients
        that want both, so treating them as interchangeable would accept a
        redirect the client never declared."""
        narrow = oauth.Client(CIMD_URL, "n", ("http://127.0.0.1/callback",), "cimd")
        self.assertFalse(narrow.matches_redirect("http://localhost:3000/callback"))

    def test_a_non_loopback_http_uri_is_not_port_agnostic(self):
        wide = oauth.Client(CIMD_URL, "w", ("http://app.example/callback",), "cimd")
        self.assertFalse(wide.matches_redirect("http://app.example:9999/callback"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = oauth.Store(Path(tmp.name) / "oauth.db")

    def test_a_registered_client_round_trips(self):
        client_id = self.store.register_client("My Client",
                                               ("https://app.example/cb",))
        found = self.store.client(client_id)
        self.assertEqual(found.name, "My Client")
        self.assertEqual(found.kind, "preregistered")

    def test_an_unknown_client_is_none(self):
        self.assertIsNone(self.store.client("nope"))

    def test_client_ids_are_unguessable(self):
        one = self.store.register_client("a", ("https://a.example/cb",))
        two = self.store.register_client("b", ("https://b.example/cb",))
        self.assertNotEqual(one, two)
        self.assertGreaterEqual(len(one), 20)

    def test_the_database_is_not_world_readable(self):
        import os
        import stat
        if os.name == "nt":
            self.skipTest("POSIX mode bits")
        self.store.register_client("a", ("https://a.example/cb",))
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode) & 0o077, 0)

    def test_it_survives_being_reopened(self):
        client_id = self.store.register_client("a", ("https://a.example/cb",))
        again = oauth.Store(self.store.path)
        self.assertIsNotNone(again.client(client_id))

    def test_the_schema_version_is_recorded(self):
        """So a later change is a migration rather than a surprise."""
        self.assertEqual(self.store.schema_version(), oauth.SCHEMA_VERSION)


class ResolveClientTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = oauth.Store(Path(tmp.name) / "oauth.db")
        self.fetch = fetcher()

    def test_a_url_shaped_id_goes_to_cimd(self):
        client = oauth.resolve_client(CIMD_URL, store=self.store, fetch=self.fetch)
        self.assertEqual(client.kind, "cimd")

    def test_a_plain_id_goes_to_the_store(self):
        client_id = self.store.register_client("Pre", ("https://app.example/cb",))
        client = oauth.resolve_client(client_id, store=self.store, fetch=self.fetch)
        self.assertEqual(client.kind, "preregistered")

    def test_an_unknown_plain_id_is_refused(self):
        with self.assertRaises(oauth.ClientRefused) as caught:
            oauth.resolve_client("no-such-client", store=self.store, fetch=self.fetch)
        self.assertEqual(caught.exception.reason, "unknown_client")

    def test_an_empty_id_is_refused(self):
        with self.assertRaises(oauth.ClientRefused):
            oauth.resolve_client("", store=self.store, fetch=self.fetch)

    def test_every_refusal_reason_is_loggable(self):
        """`ClientRefused.reason` is written to the event log, which refuses
        any string it has not agreed to. A reason outside that vocabulary
        would be silently dropped at exactly the moment somebody needed it."""
        for reason in oauth.REFUSAL_REASONS:
            self.assertIn(reason, eventlog.REASONS)


class NewClientCliTests(unittest.TestCase):
    """`brain serve --new-client` — the fallback for anything that speaks
    neither CIMD nor a mechanism this server has. Generic, not per-vendor."""

    def run_serve(self, argv):
        import contextlib
        import io
        from brainlib import osbackend

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = osbackend.FileKeystore(Path(tmp.name))
        store.set(serve.TOKEN_NAME, BEARER)
        self.db = Path(tmp.name) / "oauth.db"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = serve.run_serve(argv, store=store, run=lambda s: None,
                                   oauth_store=oauth.Store(self.db),
                                   log=eventlog.EventLog(Path(tmp.name) / "e.jsonl"))
        return code, out.getvalue(), err.getvalue()

    def test_it_mints_and_prints_once(self):
        code, out, _err = self.run_serve(
            ["--new-client", "My Assistant",
             "--redirect-uri", "https://app.example/callback"])
        self.assertEqual(code, 0)
        client_id = [line.strip() for line in out.splitlines() if line.strip()][-1]
        self.assertTrue(oauth.Store(self.db).client(client_id))

    def test_it_needs_a_redirect_uri(self):
        code, _out, err = self.run_serve(["--new-client", "My Assistant"])
        self.assertEqual(code, 2)
        self.assertIn("--redirect-uri", err)

    def test_a_plain_http_redirect_uri_is_refused(self):
        """OAuth 2.1 communication security: every redirect URI is loopback or
        HTTPS. This is also the open-redirect control."""
        code, _out, err = self.run_serve(
            ["--new-client", "X", "--redirect-uri", "http://app.example/cb"])
        self.assertEqual(code, 2)
        self.assertIn("https", err)

    def test_a_loopback_redirect_uri_is_accepted(self):
        code, _out, _err = self.run_serve(
            ["--new-client", "X", "--redirect-uri", "http://127.0.0.1/callback"])
        self.assertEqual(code, 0)

    def test_several_redirect_uris_are_accepted(self):
        code, out, _err = self.run_serve(
            ["--new-client", "X",
             "--redirect-uri", "http://127.0.0.1/callback",
             "--redirect-uri", "http://localhost/callback"])
        self.assertEqual(code, 0)
        client_id = [line.strip() for line in out.splitlines() if line.strip()][-1]
        self.assertEqual(len(oauth.Store(self.db).client(client_id).redirect_uris), 2)


# ---------------------------------------------------------------------------
# /authorize — consent, and PKCE.
# ---------------------------------------------------------------------------

def pkce():
    """A verifier and its S256 challenge, the way a client would build them."""
    import base64
    import hashlib
    import secrets
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class AuthServerCase(unittest.TestCase):
    """The authorization server with every dependency injected: no network, no
    real clock, no state outside a temp directory."""

    SCOPES = (oauth.SCOPE_READ, oauth.SCOPE_WRITE)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.now = [1_000_000.0]
        self.store = oauth.Store(Path(tmp.name) / "oauth.db",
                                 clock=lambda: self.now[0])
        self.cfg = oauth.Config(PUBLIC, scopes=self.SCOPES)
        self.auth = oauth.AuthServer(self.cfg, self.store, fetch=fetcher(),
                                     clock=lambda: self.now[0])
        self.verifier, self.challenge = pkce()

    def params(self, **overrides):
        base = {
            "response_type": "code",
            "client_id": CIMD_URL,
            "redirect_uri": "https://app.example/callback",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "scope": "brain:read brain:write",
            "state": "opaque-client-state",
            "resource": PUBLIC,
        }
        base.update(overrides)
        return {k: v for k, v in base.items() if v is not None}

    def consent(self, **overrides):
        """A full consent, returning the redirect URL it produced."""
        outcome = self.auth.consent(self.params(**overrides), BEARER,
                                    verify=lambda v: v == BEARER)
        self.assertEqual(outcome.kind, "redirect", outcome.body[:400])
        return outcome.url

    def code_from(self, url):
        from urllib.parse import parse_qs, urlsplit as split
        return parse_qs(split(url).query)["code"][0]


class AuthorizePageTests(AuthServerCase):
    def test_a_valid_request_renders_a_consent_page(self):
        outcome = self.auth.authorize(self.params())
        self.assertEqual(outcome.kind, "page")
        self.assertEqual(outcome.status, 200)
        self.assertIn("An Assistant", outcome.body)

    def test_the_client_name_is_html_escaped(self):
        """It comes out of a document an ATTACKER hosts, and it is rendered on
        a page the operator types their token into. Escaping it is not
        tidiness; without it this is a cross-site scripting hole in a consent
        screen."""
        doc = dict(CIMD_DOC, client_name="<script>steal()</script>")
        net = FakeNet(pages={CIMD_URL: (200, json.dumps(doc).encode())})
        auth = oauth.AuthServer(self.cfg, self.store, fetch=fetcher(net=net),
                                clock=lambda: self.now[0])
        body = auth.authorize(self.params()).body
        self.assertNotIn("<script>steal()", body)
        self.assertIn("&lt;script&gt;", body)

    def test_the_state_is_escaped_too(self):
        body = self.auth.authorize(
            self.params(state='"><script>x</script>')).body
        self.assertNotIn("<script>x</script>", body)

    def test_the_redirect_hostname_is_displayed(self):
        """The MCP spec makes this a MUST: it is the only thing on the page
        that says where the credential is actually going."""
        self.assertIn("app.example", self.auth.authorize(self.params()).body)

    def test_a_loopback_only_client_gets_an_extra_warning(self):
        """A Client ID Metadata Document cannot prevent loopback
        impersonation — any local process can bind a port and claim to be the
        legitimate client — so the spec asks for a warning."""
        doc = dict(CIMD_DOC, redirect_uris=["http://127.0.0.1/callback"])
        net = FakeNet(pages={CIMD_URL: (200, json.dumps(doc).encode())})
        auth = oauth.AuthServer(self.cfg, self.store, fetch=fetcher(net=net),
                                clock=lambda: self.now[0])
        body = auth.authorize(self.params(
            redirect_uri="http://127.0.0.1:9999/callback")).body
        self.assertIn("local", body.lower())

    def test_the_scopes_are_described_in_words(self):
        body = self.auth.authorize(self.params()).body
        self.assertIn("read", body.lower())
        self.assertIn("write", body.lower())

    def test_the_page_asks_for_the_operator_token(self):
        body = self.auth.authorize(self.params()).body
        self.assertIn("password", body)

    def test_nothing_is_issued_by_merely_rendering(self):
        self.auth.authorize(self.params())
        self.assertEqual(self.store.count_codes(), 0)


class AuthorizeRefusalTests(AuthServerCase):
    def test_an_unknown_client_renders_an_error_and_does_not_redirect(self):
        """Redirecting to a URI that has not been validated against a client is
        the open redirect this whole check exists to prevent."""
        outcome = self.auth.authorize(self.params(client_id="no-such-client"))
        self.assertEqual(outcome.kind, "page")
        self.assertEqual(outcome.status, 400)

    def test_a_redirect_uri_the_client_did_not_register_does_not_redirect(self):
        outcome = self.auth.authorize(
            self.params(redirect_uri="https://evil.example/callback"))
        self.assertEqual(outcome.kind, "page")
        self.assertEqual(outcome.status, 400)

    def test_a_missing_redirect_uri_does_not_redirect(self):
        outcome = self.auth.authorize(self.params(redirect_uri=None))
        self.assertEqual(outcome.kind, "page")

    def test_a_missing_pkce_challenge_is_refused(self):
        """OAuth 2.1 requires PKCE. A flow without it is one an intercepted
        authorization code completes on its own."""
        outcome = self.auth.authorize(self.params(code_challenge=None))
        self.assertEqual(outcome.kind, "redirect")
        self.assertIn("error=invalid_request", outcome.url)

    def test_a_plain_pkce_method_is_refused(self):
        outcome = self.auth.authorize(self.params(code_challenge_method="plain"))
        self.assertEqual(outcome.kind, "redirect")
        self.assertIn("error=invalid_request", outcome.url)

    def test_an_unsupported_response_type_is_refused(self):
        outcome = self.auth.authorize(self.params(response_type="token"))
        self.assertIn("error=unsupported_response_type", outcome.url)

    def test_a_resource_that_is_not_this_server_is_refused(self):
        """RFC 8707. A token this server issues is only ever for this server,
        so a request asking for one bound to somewhere else is a mistake at
        best and a confused-deputy setup at worst."""
        outcome = self.auth.authorize(
            self.params(resource="https://someone.else/mcp"))
        self.assertIn("error=invalid_", outcome.url)

    def test_an_absent_resource_defaults_to_this_server(self):
        """The spec makes sending it a client MUST, but a token bound to this
        server is the only thing this server can issue, so a client that omits
        it gets the right answer rather than a refusal."""
        outcome = self.auth.authorize(self.params(resource=None))
        self.assertEqual(outcome.kind, "page")

    def test_an_unknown_scope_is_refused(self):
        outcome = self.auth.authorize(self.params(scope="brain:read admin:all"))
        self.assertIn("error=invalid_scope", outcome.url)

    def test_the_state_survives_into_an_error_redirect(self):
        outcome = self.auth.authorize(self.params(code_challenge=None))
        self.assertIn("state=opaque-client-state", outcome.url)


class ConsentTests(AuthServerCase):
    def test_a_correct_operator_token_issues_a_code(self):
        url = self.consent()
        self.assertTrue(url.startswith("https://app.example/callback?"))
        self.assertIn("code=", url)
        self.assertIn("state=opaque-client-state", url)

    def test_the_issuer_is_returned_with_the_code(self):
        """RFC 9207, and the metadata advertises that it is sent — so it has to
        actually be sent, or a conforming client rejects the response."""
        self.assertIn("iss=", self.consent())

    def test_a_wrong_operator_token_issues_nothing(self):
        outcome = self.auth.consent(self.params(), "not-the-value",
                                    verify=lambda v: v == BEARER)
        self.assertEqual(outcome.kind, "page")
        self.assertEqual(outcome.status, 401)
        self.assertEqual(self.store.count_codes(), 0)

    def test_a_wrong_operator_token_is_reported_for_the_limiter(self):
        """Consent is the one place a credential can be GUESSED at over the
        network besides /mcp, so it backs off on the same table."""
        outcome = self.auth.consent(self.params(), "nope",
                                    verify=lambda v: v == BEARER)
        self.assertTrue(outcome.credential_failed)

    def test_a_correct_token_is_not_reported_as_a_failure(self):
        self.assertFalse(self.auth.consent(
            self.params(), BEARER, verify=lambda v: v == BEARER).credential_failed)

    def test_consent_revalidates_rather_than_trusting_the_form(self):
        """The hidden fields in the consent form are just request parameters
        again, and a form can be re-posted with any of them changed. They get
        exactly the same validation the first time round."""
        outcome = self.auth.consent(
            self.params(redirect_uri="https://evil.example/cb"), BEARER,
            verify=lambda v: v == BEARER)
        self.assertEqual(outcome.kind, "page")
        self.assertEqual(self.store.count_codes(), 0)

    def test_the_code_is_stored_hashed(self):
        url = self.consent()
        code = self.code_from(url)
        blob = self.store.path.read_bytes()
        self.assertNotIn(code.encode(), blob,
                         "the authorization code is in the database in the clear")

    def test_the_granted_scope_is_recorded(self):
        self.consent(scope="brain:read")
        row = self.store.only_code()
        self.assertEqual(row["scope"], "brain:read")

    def test_offline_access_is_grantable(self):
        """A client asks for it to get a refresh token; the server has to be
        willing to record it, or every session ends in an hour."""
        self.consent(scope="brain:read offline_access")
        self.assertIn("offline_access", self.store.only_code()["scope"])


class ReadOnlyConsentTests(AuthServerCase):
    SCOPES = (oauth.SCOPE_READ,)

    def test_write_cannot_be_granted_by_a_read_only_process(self):
        """The flag bounds the scope. A consent screen that promised write here
        would be promising something the dispatcher will refuse."""
        outcome = self.auth.authorize(self.params(scope="brain:read brain:write"))
        self.assertEqual(outcome.kind, "redirect")
        self.assertIn("error=invalid_scope", outcome.url)


class CodeLifecycleTests(AuthServerCase):
    def test_a_code_is_single_use_and_reports_the_reuse(self):
        """Not simply refused the second time: OAuth 2.1 requires the tokens
        issued from a REPLAYED code to be revoked, so the second attempt has to
        come back distinguishable from an unknown code rather than as None.
        Re-use means somebody else has a copy."""
        code = self.code_from(self.consent())
        first = self.store.take_code(code, self.now[0])
        self.assertFalse(first["already_used"])
        second = self.store.take_code(code, self.now[0])
        self.assertTrue(second["already_used"])

    def test_a_code_expires(self):
        code = self.code_from(self.consent())
        self.now[0] += oauth.CODE_TTL_SECONDS + 1
        self.assertIsNone(self.store.take_code(code, self.now[0]))

    def test_a_code_is_bound_to_everything_it_was_issued_with(self):
        code = self.code_from(self.consent())
        row = self.store.take_code(code, self.now[0])
        self.assertEqual(row["client_id"], CIMD_URL)
        self.assertEqual(row["redirect_uri"], "https://app.example/callback")
        self.assertEqual(row["resource"], PUBLIC)
        self.assertEqual(row["challenge"], self.challenge)


class AuthorizeOverHttpTests(OAuthServeCase):
    """The consent screen as a browser actually reaches it."""

    def setUp(self):
        OAuthServeCase.setUp(self)
        self.verifier, self.challenge = pkce()

    def query(self, **overrides):
        from urllib.parse import urlencode
        base = {"response_type": "code", "client_id": CIMD_URL,
                "redirect_uri": "https://app.example/callback",
                "code_challenge": self.challenge, "code_challenge_method": "S256",
                "scope": "brain:read brain:write", "state": "st",
                "resource": self.public}
        base.update({k: v for k, v in overrides.items() if v is not None})
        return urlencode(base)

    def post_form(self, body, origin=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if origin:
                headers["Origin"] = origin
            conn.request("POST", "/authorize", body=body.encode("utf-8"),
                         headers=headers)
            response = conn.getresponse()
            return (response.status, dict(response.getheaders()),
                    response.read().decode("utf-8", "replace"))
        finally:
            conn.close()

    def test_the_page_is_served_unauthenticated(self):
        status, _headers, body = self.get("/authorize?" + self.query())
        self.assertEqual(status, 200)
        self.assertIn("An Assistant", body)

    def test_the_page_carries_a_restrictive_csp(self):
        """It is the one page in this project a human types a credential into.
        It loads nothing, so nothing may be loaded into it."""
        _status, headers, _body = self.get("/authorize?" + self.query())
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_same_origin_form_post_is_not_refused_as_a_browser_origin(self):
        """The consent form IS a browser POST, and it carries this server's own
        Origin. Refusing it would make the endpoint unusable — which is exactly
        what the /mcp Origin rule would have done if applied unchanged."""
        status, _headers, _body = self.post_form(
            self.query() + "&operator_token=" + BEARER, origin=self.server.oauth.issuer)
        self.assertEqual(status, 302)

    def test_a_cross_origin_form_post_is_still_refused(self):
        status, _headers, _body = self.post_form(
            self.query() + "&operator_token=" + BEARER, origin="https://evil.example")
        self.assertEqual(status, 403)

    def test_consent_issues_a_code_and_redirects(self):
        status, headers, _body = self.post_form(
            self.query() + "&operator_token=" + BEARER)
        self.assertEqual(status, 302)
        self.assertIn("code=", headers["Location"])
        self.assertIn("iss=", headers["Location"])
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_wrong_operator_token_is_401_and_issues_nothing(self):
        status, _headers, _body = self.post_form(
            self.query() + "&operator_token=wrong")
        self.assertEqual(status, 401)
        self.assertEqual(self.store.count_codes(), 0)

    def test_a_wrong_operator_token_feeds_the_limiter(self):
        """Handoff question 7: an OAuth flow adds endpoints that can be
        hammered, and the consent screen is the one with a credential on it."""
        before = self.server.limiter.tracked()
        self.post_form(self.query() + "&operator_token=wrong")
        self.assertGreater(self.server.limiter.tracked(), before)

    def test_a_wrong_operator_token_is_recorded(self):
        self.post_form(self.query() + "&operator_token=wrong")
        events = [e["event"] for e in self.log.read(limit=100)]
        self.assertIn("oauth_consent_failed", events)
        self.assertNotIn("wrong", self.log.read.__self__.path.read_text())

    def test_an_oversized_form_is_refused(self):
        status, _headers, _body = self.post_form(
            self.query() + "&operator_token=" + ("x" * 40_000))
        self.assertEqual(status, 413)

    def test_the_token_endpoint_refuses_a_get(self):
        status, _headers, _body = self.get("/token")
        self.assertEqual(status, 405)


# ---------------------------------------------------------------------------
# /token and /revoke
# ---------------------------------------------------------------------------

class TokenCase(AuthServerCase):
    def exchange(self, **overrides):
        code = self.code_from(self.consent())
        form = {"grant_type": "authorization_code", "code": code,
                "client_id": CIMD_URL,
                "redirect_uri": "https://app.example/callback",
                "code_verifier": self.verifier, "resource": PUBLIC}
        form.update(overrides)
        form = {k: v for k, v in form.items() if v is not None}
        return self.auth.token(form)


class TokenExchangeTests(TokenCase):
    def test_a_valid_exchange_returns_the_expected_fields(self):
        status, payload, _headers = self.exchange()
        self.assertEqual(status, 200)
        self.assertTrue(payload["access_token"])
        self.assertEqual(payload["token_type"], "Bearer")
        self.assertEqual(payload["expires_in"], int(oauth.ACCESS_TTL_SECONDS))
        self.assertTrue(payload["refresh_token"])
        self.assertEqual(payload["scope"], "brain:read brain:write")

    def test_the_response_is_never_cached(self):
        _status, _payload, headers = self.exchange()
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_a_wrong_code_verifier_is_invalid_grant(self):
        """PKCE is what makes an intercepted authorization code useless."""
        status, payload, _headers = self.exchange(code_verifier="not-the-verifier")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_missing_code_verifier_is_refused(self):
        status, payload, _headers = self.exchange(code_verifier=None)
        self.assertEqual(status, 400)
        self.assertIn(payload["error"], ("invalid_grant", "invalid_request"))

    def test_an_unknown_code_is_invalid_grant(self):
        status, payload, _headers = self.exchange(code="never-issued")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_mismatched_redirect_uri_is_refused(self):
        status, payload, _headers = self.exchange(
            redirect_uri="https://app.example/other")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_mismatched_client_id_is_refused(self):
        status, payload, _headers = self.exchange(client_id="somebody-else")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_mismatched_resource_is_refused(self):
        status, payload, _headers = self.exchange(resource="https://other/mcp")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_an_expired_code_is_refused(self):
        code = self.code_from(self.consent())
        self.now[0] += oauth.CODE_TTL_SECONDS + 1
        status, payload, _headers = self.auth.token(
            {"grant_type": "authorization_code", "code": code,
             "client_id": CIMD_URL, "redirect_uri": "https://app.example/callback",
             "code_verifier": self.verifier})
        self.assertEqual(payload["error"], "invalid_grant")

    def test_an_unsupported_grant_type_is_named_correctly(self):
        status, payload, _headers = self.auth.token({"grant_type": "password"})
        self.assertEqual(payload["error"], "unsupported_grant_type")

    def test_a_missing_grant_type_is_invalid_request(self):
        status, payload, _headers = self.auth.token({})
        self.assertEqual(payload["error"], "invalid_request")

    def test_the_access_token_is_stored_hashed(self):
        _status, payload, _headers = self.exchange()
        blob = self.store.path.read_bytes()
        self.assertNotIn(payload["access_token"].encode(), blob)
        self.assertNotIn(payload["refresh_token"].encode(), blob)


class CodeReplayTests(TokenCase):
    def test_replaying_a_code_revokes_what_it_already_issued(self):
        """OAuth 2.1's rule, and the reasoning behind it: a code presented
        twice means somebody else has a copy, so the tokens it produced are
        not trustworthy either."""
        code = self.code_from(self.consent())
        form = {"grant_type": "authorization_code", "code": code,
                "client_id": CIMD_URL,
                "redirect_uri": "https://app.example/callback",
                "code_verifier": self.verifier}
        _status, first, _headers = self.auth.token(form)
        access = first["access_token"]
        self.assertIsNotNone(self.auth.validate_bearer(access))

        status, second, _headers = self.auth.token(form)
        self.assertEqual(status, 400)
        self.assertEqual(second["error"], "invalid_grant")
        self.assertIsNone(self.auth.validate_bearer(access),
                          "the token from a replayed code still works")


class RefreshTests(TokenCase):
    def refresh(self, token, client_id=CIMD_URL):
        return self.auth.token({"grant_type": "refresh_token",
                                "refresh_token": token, "client_id": client_id})

    def test_a_refresh_returns_a_new_pair(self):
        _status, first, _headers = self.exchange()
        status, second, _headers = self.refresh(first["refresh_token"])
        self.assertEqual(status, 200)
        self.assertTrue(second["access_token"])
        self.assertNotEqual(second["refresh_token"], first["refresh_token"],
                            "refresh tokens must ROTATE for a public client")

    def test_the_old_refresh_token_stops_working(self):
        _status, first, _headers = self.exchange()
        self.refresh(first["refresh_token"])
        status, payload, _headers = self.refresh(first["refresh_token"])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "invalid_grant")

    def test_reusing_a_rotated_refresh_token_kills_the_family(self):
        """A rotated token presented again means a copy leaked. Everything
        descended from that authorization goes at once — the alternative is an
        attacker and the legitimate client taking turns refreshing forever."""
        _status, first, _headers = self.exchange()
        _status, second, _headers = self.refresh(first["refresh_token"])
        self.refresh(first["refresh_token"])          # the leaked one, replayed
        self.assertIsNone(self.auth.validate_bearer(second["access_token"]))
        status, payload, _headers = self.refresh(second["refresh_token"])
        self.assertEqual(payload["error"], "invalid_grant")

    def test_an_expired_refresh_token_is_refused(self):
        _status, first, _headers = self.exchange()
        self.now[0] += oauth.REFRESH_TTL_SECONDS + 1
        status, payload, _headers = self.refresh(first["refresh_token"])
        self.assertEqual(payload["error"], "invalid_grant")

    def test_a_refresh_from_another_client_is_refused(self):
        _status, first, _headers = self.exchange()
        status, payload, _headers = self.refresh(first["refresh_token"],
                                                 client_id="somebody-else")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_an_unknown_refresh_token_is_invalid_grant(self):
        status, payload, _headers = self.refresh("never-issued")
        self.assertEqual(payload["error"], "invalid_grant")

    def test_the_scope_is_carried_forward_and_not_widened(self):
        code = self.code_from(self.consent(scope="brain:read"))
        _status, first, _headers = self.auth.token(
            {"grant_type": "authorization_code", "code": code,
             "client_id": CIMD_URL, "redirect_uri": "https://app.example/callback",
             "code_verifier": self.verifier})
        _status, second, _headers = self.refresh(first["refresh_token"])
        self.assertEqual(second["scope"], "brain:read")


class RevocationTests(TokenCase):
    def test_revoking_an_access_token_stops_it(self):
        _status, issued, _headers = self.exchange()
        self.auth.revoke({"token": issued["access_token"]})
        self.assertIsNone(self.auth.validate_bearer(issued["access_token"]))

    def test_revoking_a_refresh_token_takes_the_whole_grant(self):
        _status, issued, _headers = self.exchange()
        self.auth.revoke({"token": issued["refresh_token"]})
        self.assertIsNone(self.auth.validate_bearer(issued["access_token"]))

    def test_an_unknown_token_is_still_200(self):
        """RFC 7009. An answer that differs is an oracle for whether a token
        exists, which is a way to confirm a guess without using it."""
        self.assertEqual(self.auth.revoke({"token": "never-issued"})[0], 200)
        self.assertEqual(self.auth.revoke({})[0], 200)

    def test_a_known_token_is_also_200(self):
        _status, issued, _headers = self.exchange()
        self.assertEqual(self.auth.revoke({"token": issued["access_token"]})[0], 200)


class TokenOverHttpTests(AuthorizeOverHttpTests):
    """The token endpoint as a client actually reaches it."""

    def obtain_code(self):
        from urllib.parse import parse_qs, urlsplit as split
        _status, headers, _body = self.post_form(
            self.query() + "&operator_token=" + BEARER)
        return parse_qs(split(headers["Location"]).query)["code"][0]

    def test_a_form_encoded_exchange_works(self):
        from urllib.parse import urlencode
        code = self.obtain_code()
        status, headers, body = self.post_to("/token", urlencode({
            "grant_type": "authorization_code", "code": code,
            "client_id": CIMD_URL, "redirect_uri": "https://app.example/callback",
            "code_verifier": self.verifier, "resource": self.public}))
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(json.loads(body)["access_token"])

    def test_a_json_body_is_refused_as_invalid_request(self):
        """RFC 6749 requires form encoding here. A server that quietly accepts
        JSON teaches one client a habit that breaks against every other."""
        status, _headers, body = self.post_to(
            "/token", json.dumps({"grant_type": "authorization_code"}),
            content_type="application/json")
        self.assertEqual(status, 400)
        self.assertEqual(json.loads(body)["error"], "invalid_request")

    def test_the_token_endpoint_needs_no_bearer(self):
        """It is how a client GETS a bearer. Requiring one would be circular."""
        from urllib.parse import urlencode
        status, _headers, _body = self.post_to(
            "/token", urlencode({"grant_type": "refresh_token",
                                 "refresh_token": "nope"}))
        self.assertEqual(status, 400)

    def test_revocation_over_http_is_200(self):
        from urllib.parse import urlencode
        status, _headers, _body = self.post_to(
            "/revoke", urlencode({"token": "never-issued"}))
        self.assertEqual(status, 200)


# ---------------------------------------------------------------------------
# Task 7: both credentials on one endpoint, with an audience and a scope.
# ---------------------------------------------------------------------------

class TwoCredentialsTests(AuthorizeOverHttpTests):
    """One `Authorization: Bearer` header, two possible meanings.

    The operator token is tried FIRST and is unchanged — every local client
    this project already supports depends on that, and it is the regression
    that would do the most damage.
    """

    def obtain_token(self, scope="brain:read brain:write"):
        from urllib.parse import parse_qs, urlencode, urlsplit as split
        _status, headers, _body = self.post_form(
            self.query(scope=scope) + "&operator_token=" + BEARER)
        code = parse_qs(split(headers["Location"]).query)["code"][0]
        _status, _headers, body = self.post_to("/token", urlencode({
            "grant_type": "authorization_code", "code": code,
            "client_id": CIMD_URL, "redirect_uri": "https://app.example/callback",
            "code_verifier": self.verifier, "resource": self.public}))
        return json.loads(body)

    def call(self, name, bearer, arguments=None):
        return self.post_mcp({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                              "params": {"name": name,
                                         "arguments": arguments or {}}},
                             bearer=bearer)

    def test_the_operator_token_still_works(self):
        status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, bearer=BEARER)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_an_oauth_access_token_works(self):
        issued = self.obtain_token()
        status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            bearer=issued["access_token"])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["result"], {})

    def test_a_garbage_bearer_is_still_401(self):
        status, headers, _body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, bearer="nonsense")
        self.assertEqual(status, 401)
        self.assertIn("resource_metadata", headers["WWW-Authenticate"])

    def test_a_revoked_token_stops_working_immediately(self):
        from urllib.parse import urlencode
        issued = self.obtain_token()
        self.post_to("/revoke", urlencode({"token": issued["refresh_token"]}))
        status, _headers, _body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            bearer=issued["access_token"])
        self.assertEqual(status, 401)

    def test_an_expired_token_is_401(self):
        issued = self.obtain_token()
        # The clock is a parameter, so this does not sleep for an hour.
        expired = oauth.AuthServer(self.server.oauth, self.store,
                                   clock=lambda: __import__("time").time()
                                   + oauth.ACCESS_TTL_SECONDS + 10)
        self.assertIsNone(expired.validate_bearer(issued["access_token"]))

    def test_a_token_for_another_resource_is_refused_though_the_row_exists(self):
        """The audience check, and the case that makes it worth having: two
        brains on one host, one database, one token. RFC 8707 requires the
        resource server to verify it is the intended recipient rather than
        assume it."""
        issued = self.obtain_token()
        elsewhere = oauth.AuthServer(
            oauth.Config("https://a-different-brain.example/mcp"), self.store)
        self.assertIsNotNone(self.server.auth.validate_bearer(issued["access_token"]))
        self.assertIsNone(elsewhere.validate_bearer(issued["access_token"]))

    def test_the_operator_token_is_not_looked_up_in_the_token_store(self):
        """Tried first, and it must not fall through. A keystore value that
        happened to collide with a hashed row would be a very strange bug."""
        status, _headers, _body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"}, bearer=BEARER)
        self.assertEqual(status, 200)
        self.assertEqual(self.store.count_tokens(), 0)


class ScopeEnforcementTests(TwoCredentialsTests):
    def test_a_read_scoped_token_is_refused_the_write_tool_by_name(self):
        """By NAME — a client that never read tools/list and calls the tool
        anyway is exactly what this has to stop, which is the same rule
        --read-only already follows."""
        issued = self.obtain_token(scope="brain:read")
        status, headers, _body = self.call("brain_capture",
                                           issued["access_token"],
                                           {"text": "should never be written"})
        self.assertEqual(status, 403)
        challenge = headers["WWW-Authenticate"]
        self.assertIn('error="insufficient_scope"', challenge)
        self.assertIn('scope="brain:write"', challenge)
        self.assertIn("resource_metadata=", challenge)

    def test_tools_list_reflects_the_tokens_scopes(self):
        issued = self.obtain_token(scope="brain:read")
        _status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            bearer=issued["access_token"])
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertIn("brain_search", names)
        self.assertNotIn("brain_capture", names)

    def test_a_full_scoped_token_sees_every_tool(self):
        issued = self.obtain_token()
        _status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            bearer=issued["access_token"])
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(sorted(names), sorted(mcp.TOOLS_BY_NAME))

    def test_the_operator_token_is_not_scope_limited(self):
        """It is not an OAuth credential and has no scopes. Inventing some for
        it would change what every existing local client can do."""
        _status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, bearer=BEARER)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(sorted(names), sorted(mcp.TOOLS_BY_NAME))

    def test_insufficient_scope_is_recorded(self):
        issued = self.obtain_token(scope="brain:read")
        self.call("brain_capture", issued["access_token"], {"text": "x"})
        events = [e["event"] for e in self.log.read(limit=200)]
        self.assertIn("insufficient_scope", events)

    def test_an_accepted_token_is_recorded_without_its_value(self):
        issued = self.obtain_token()
        self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      bearer=issued["access_token"])
        events = [e["event"] for e in self.log.read(limit=200)]
        self.assertIn("oauth_token_accepted", events)
        raw = (self.state / "events.jsonl").read_text()
        self.assertNotIn(issued["access_token"], raw)

    def test_a_rejected_token_is_recorded_with_a_reason_class(self):
        self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      bearer="a-token-that-was-never-issued")
        rejected = [e for e in self.log.read(limit=200)
                    if e["event"] == "oauth_token_rejected"]
        self.assertTrue(rejected)
        self.assertEqual(rejected[0]["reason"], "unknown_token")
        raw = (self.state / "events.jsonl").read_text()
        self.assertNotIn("a-token-that-was-never-issued", raw)


class ReadOnlyProcessWithATokenTests(OAuthServeCase):
    ALLOW_TOOLS = mcp.READ_ONLY_TOOLS

    def test_the_process_bounds_the_scope_not_the_other_way_round(self):
        """Even a token minted by hand with brain:write reaches no write tool
        on a --read-only process. The flag is the outer boundary."""
        grant_id = self.store.create_grant(CIMD_URL, self.public,
                                           "brain:read brain:write")
        import secrets
        minted = secrets.token_urlsafe(32)
        import time as _time
        self.store.create_token(minted, grant_id, "access", _time.time() + 600)
        _status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, bearer=minted)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertNotIn("brain_capture", names)


# ---------------------------------------------------------------------------
# Task 9: the whole handshake, driven the way a hosted assistant drives it.
# ---------------------------------------------------------------------------

class WholeHandshakeTests(OAuthServeCase):
    """Every test above proves one piece. This one proves the pieces AGREE.

    Each unit test can pass while the issuer in one document fails to match the
    URL a client would build out of another — and that mismatch is the single
    most common way these deployments fail, because from the outside it looks
    identical to the server being unreachable. The only thing that catches it
    is walking the documents the way a client walks them, which is what this
    does: nothing below reads a value this test knew in advance if a real
    client would have had to discover it.
    """

    def setUp(self):
        OAuthServeCase.setUp(self)
        self.verifier, self.challenge = pkce()

    # -- the four steps a client takes before it can ask for anything --------

    def step_1_unauthenticated_request(self):
        status, headers, _body = self.post_mcp({"jsonrpc": "2.0", "id": 1,
                                                "method": "ping"})
        self.assertEqual(status, 401, "the handshake starts at a 401")
        return headers["WWW-Authenticate"]

    def step_2_read_the_challenge(self, challenge):
        import re
        found = re.search(r'resource_metadata="([^"]+)"', challenge)
        self.assertIsNotNone(found, "no resource_metadata to follow")
        return found.group(1)

    def step_3_protected_resource_metadata(self, url):
        from urllib.parse import urlsplit as split
        status, _headers, body = self.get(split(url).path)
        self.assertEqual(status, 200)
        doc = json.loads(body)
        self.assertEqual(doc["resource"], self.public,
                         "the advertised resource is not the URL a client uses")
        return doc["authorization_servers"][0]

    def step_4_authorization_server_metadata(self, issuer):
        from urllib.parse import urlsplit as split
        # Built the way a client builds it: the issuer, plus the well-known
        # suffix. Not a path this test knew.
        url = issuer + "/.well-known/oauth-authorization-server"
        status, _headers, body = self.get(split(url).path)
        self.assertEqual(status, 200, f"no metadata at {url}")
        doc = json.loads(body)
        self.assertEqual(doc["issuer"], issuer,
                         "a conforming client DISCARDS metadata whose issuer "
                         "differs from the URL it was fetched from")
        self.assertIn("S256", doc["code_challenge_methods_supported"],
                      "a conforming client refuses to proceed without this")
        return doc

    def discover(self):
        challenge = self.step_1_unauthenticated_request()
        prm_url = self.step_2_read_the_challenge(challenge)
        issuer = self.step_3_protected_resource_metadata(prm_url)
        return self.step_4_authorization_server_metadata(issuer)

    # -- the rest of the flow ------------------------------------------------

    def authorize_and_consent(self, metadata, client_id=CIMD_URL,
                              redirect="https://app.example/callback",
                              scope="brain:read brain:write"):
        from urllib.parse import parse_qs, urlencode, urlsplit as split
        query = urlencode({"response_type": "code", "client_id": client_id,
                           "redirect_uri": redirect,
                           "code_challenge": self.challenge,
                           "code_challenge_method": "S256", "scope": scope,
                           "state": "client-state", "resource": self.public})
        endpoint = split(metadata["authorization_endpoint"]).path
        status, _headers, body = self.get(endpoint + "?" + query)
        self.assertEqual(status, 200, body[:300])

        status, headers, _body = self.post_to(
            endpoint, query + "&operator_token=" + BEARER)
        self.assertEqual(status, 302)
        location = headers["Location"]
        returned = parse_qs(split(location).query)
        self.assertEqual(returned["state"], ["client-state"])
        self.assertEqual(returned["iss"], [metadata["issuer"]],
                         "RFC 9207: the iss must match the issuer the client "
                         "recorded, or it rejects the response")
        return returned["code"][0]

    def redeem(self, metadata, code, client_id=CIMD_URL,
               redirect="https://app.example/callback"):
        from urllib.parse import urlencode, urlsplit as split
        status, _headers, body = self.post_to(
            split(metadata["token_endpoint"]).path,
            urlencode({"grant_type": "authorization_code", "code": code,
                       "client_id": client_id, "redirect_uri": redirect,
                       "code_verifier": self.verifier, "resource": self.public}))
        self.assertEqual(status, 200, body[:300])
        return json.loads(body)

    # -- the tests -----------------------------------------------------------

    def test_the_whole_flow_end_to_end(self):
        metadata = self.discover()
        code = self.authorize_and_consent(metadata)
        issued = self.redeem(metadata, code)

        status, _headers, body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
             "params": {"name": "brain_search",
                        "arguments": {"query": "anything at all"}}},
            bearer=issued["access_token"])
        self.assertEqual(status, 200, "a token from a completed flow was refused")
        self.assertFalse(json.loads(body)["result"]["isError"])

        # ...and it survives a refresh, which is what a long-lived connection
        # actually depends on.
        from urllib.parse import urlencode, urlsplit as split
        _status, _headers, refreshed = self.post_to(
            split(metadata["token_endpoint"]).path,
            urlencode({"grant_type": "refresh_token",
                       "refresh_token": issued["refresh_token"],
                       "client_id": CIMD_URL}))
        second = json.loads(refreshed)
        status, _headers, _body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 10, "method": "ping"},
            bearer=second["access_token"])
        self.assertEqual(status, 200)

    def test_the_same_flow_with_a_pre_registered_client(self):
        """The fallback path, walked end to end. It is not a lesser mode: a
        client that speaks no Client ID Metadata Document gets here."""
        client_id = self.store.register_client(
            "Registered Assistant", ("https://elsewhere.example/cb",))
        metadata = self.discover()
        code = self.authorize_and_consent(
            metadata, client_id=client_id, redirect="https://elsewhere.example/cb")
        issued = self.redeem(metadata, code, client_id=client_id,
                             redirect="https://elsewhere.example/cb")
        status, _headers, _body = self.post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            bearer=issued["access_token"])
        self.assertEqual(status, 200)

    def test_a_refused_consent_issues_nothing_and_the_flow_cannot_continue(self):
        metadata = self.discover()
        from urllib.parse import urlencode, urlsplit as split
        query = urlencode({"response_type": "code", "client_id": CIMD_URL,
                           "redirect_uri": "https://app.example/callback",
                           "code_challenge": self.challenge,
                           "code_challenge_method": "S256",
                           "scope": "brain:read", "state": "s",
                           "resource": self.public})
        status, _headers, _body = self.post_to(
            split(metadata["authorization_endpoint"]).path,
            query + "&operator_token=definitely-not-it")
        self.assertEqual(status, 401)
        self.assertEqual(self.store.count_codes(), 0)
        self.assertEqual(self.store.count_tokens(), 0)

    def test_a_token_from_this_flow_is_refused_by_a_brain_at_another_url(self):
        """The audience check, from the outside. Same database, same process,
        different --public-url: the token must not work."""
        metadata = self.discover()
        issued = self.redeem(metadata, self.authorize_and_consent(metadata))
        elsewhere = oauth.AuthServer(
            oauth.Config("https://a-different-brain.example/mcp"), self.store)
        self.assertIsNone(elsewhere.validate_bearer(issued["access_token"]))

    def test_no_registration_endpoint_is_discoverable(self):
        """The deliberate omission, asserted where a client would look for it.
        Dynamic client registration is deprecated in the MCP specification; if
        this ever fails, somebody added it on purpose."""
        self.assertNotIn("registration_endpoint", self.discover())

    def test_the_flow_leaves_no_credential_in_the_event_log(self):
        metadata = self.discover()
        issued = self.redeem(metadata, self.authorize_and_consent(metadata))
        raw = (self.state / "events.jsonl").read_text(encoding="utf-8")
        for secret in (BEARER, issued["access_token"], issued["refresh_token"]):
            self.assertNotIn(secret, raw)

    def test_the_flow_is_recorded_step_by_step(self):
        """The operator has to be able to see a handshake that half-happened,
        which is most of what goes wrong here."""
        metadata = self.discover()
        issued = self.redeem(metadata, self.authorize_and_consent(metadata))
        # And then USE it: `oauth_token_accepted` is recorded when a token is
        # presented at /mcp, not when it is minted, and a test that stopped at
        # the mint would be asserting a step it never took.
        self.post_mcp({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      bearer=issued["access_token"])
        events = [e["event"] for e in self.log.read(limit=500)]
        for expected in ("oauth_metadata_served", "oauth_code_issued",
                         "oauth_token_issued", "oauth_token_accepted"):
            self.assertIn(expected, events)
