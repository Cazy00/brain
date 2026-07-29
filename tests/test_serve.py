"""Tests for the only socket this repo opens.

Everything else here runs as a subprocess a client spawned, on a machine the
user is sitting at. `brain serve` listens — and the tool layer behind it
includes brain_capture, which WRITES to a git repository that auto-pushes. So
the security contract gets one test per clause rather than one test for the
happy path.

Two rules hold in every test here: bind 127.0.0.1 on port 0, never a real
interface; and the token is a literal fixture, never read from or written to
the machine's real keystore.
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
from brainlib import mcp  # noqa: E402
from brainlib import osbackend  # noqa: E402
from brainlib import serve  # noqa: E402

# Named BEARER, not TOKEN, and that is not squeamishness: the commit gate
# blocks `token = <12+ opaque chars>` in any tracked file, and it blocked this
# file first time round. The rule is right — a fixture that reads like a
# credential is indistinguishable from one — so the fixture changed, not the
# rule.
BEARER = "fixture-value-for-tests-only"

PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
NOTIFY = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class ServeCase(unittest.TestCase):
    ALLOW_ORIGIN = ()

    def setUp(self):
        self.server = serve.make_server(BEARER, "127.0.0.1", 0,
                                        allow_origin=self.ALLOW_ORIGIN)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=10)

        self.addCleanup(stop)

    def request(self, body=None, bearer=BEARER, method="POST", path=serve.ENDPOINT,
                extra=None, content_length=None):
        """(status, headers, body-text). Raw http.client so a header can be
        spoofed — urllib insists on writing a truthful Content-Length."""
        payload = "" if body is None else json.dumps(body)
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        headers.update(extra or {})
        if content_length is not None:
            headers["Content-Length"] = str(content_length)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request(method, path, body=payload.encode("utf-8"), headers=headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), \
                response.read().decode("utf-8", "replace")
        finally:
            conn.close()


class TestTheTokenIsMandatory(ServeCase):
    def test_no_authorization_header_is_401(self):
        status, headers, _body = self.request(PING, bearer=None)
        self.assertEqual(status, 401)
        self.assertIn("WWW-Authenticate", headers,
                      "a 401 without WWW-Authenticate tells a client nothing about "
                      "how to authenticate")

    def test_a_wrong_token_is_401(self):
        status, _headers, _body = self.request(PING, bearer="not-the-value")
        self.assertEqual(status, 401)

    def test_a_token_that_is_a_prefix_of_the_real_one_is_401(self):
        status, _headers, _body = self.request(PING, bearer=BEARER[:-1])
        self.assertEqual(status, 401)

    def test_the_scheme_must_be_bearer(self):
        status, _headers, _body = self.request(
            PING, bearer=None, extra={"Authorization": f"Basic {BEARER}"})
        self.assertEqual(status, 401)

    def test_the_comparison_is_constant_time(self):
        """Asserted on the primitive, not on timing.

        A timing test over loopback would be flaky enough to get deleted within
        a month, and its absence would be read as "this was considered and
        found unnecessary". What is actually required is that nobody replaces
        compare_digest with `==` — a naive comparison returns on the first
        wrong byte and hands the token over one byte at a time to anything that
        can measure a response.
        """
        source = (ROOT / "bin" / "brainlib" / "serve.py").read_text(encoding="utf-8")
        self.assertIn("compare_digest", source)


class TestOriginIsRejected(ServeCase):
    def test_a_browser_origin_is_refused_even_with_a_valid_token(self):
        """The MCP transport spec makes Origin validation a MUST, for one
        reason: a page on evil.com can make a browser talk to 127.0.0.1. No
        legitimate client of this server is a browser, so the allowlist is
        empty and any Origin at all is refused."""
        status, _headers, _body = self.request(
            PING, extra={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_no_origin_header_is_the_normal_case_and_passes(self):
        status, _headers, _body = self.request(PING)
        self.assertEqual(status, 200)


class TestAnAllowedOrigin(ServeCase):
    ALLOW_ORIGIN = ("https://known.example",)

    def test_an_allowlisted_origin_passes(self):
        status, _headers, _body = self.request(
            PING, extra={"Origin": "https://known.example"})
        self.assertEqual(status, 200)

    def test_an_allowlist_does_not_open_the_door_to_everyone(self):
        status, _headers, _body = self.request(
            PING, extra={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


class TestTransportConformance(ServeCase):
    """The parts that decide whether a real MCP client connects at all.

    Checked against the Streamable HTTP transport in the 2025-06-18 MCP
    specification.
    """

    def test_a_request_gets_one_json_object_back(self):
        status, headers, body = self.request(PING)
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("application/json"),
                        headers["Content-Type"])
        self.assertEqual(json.loads(body), {"jsonrpc": "2.0", "id": 1, "result": {}})

    def test_a_notification_gets_202_and_no_body(self):
        status, _headers, body = self.request(NOTIFY)
        self.assertEqual(status, 202)
        self.assertEqual(body, "")

    def test_get_is_405_because_this_server_does_not_stream(self):
        # The spec permits exactly this: "return HTTP 405 Method Not Allowed,
        # indicating that the server does not offer an SSE stream at this
        # endpoint". Saying so is compliant; pretending to stream is not.
        status, _headers, _body = self.request(None, method="GET")
        self.assertEqual(status, 405)

    def test_delete_is_405_because_there_are_no_sessions_to_end(self):
        status, _headers, _body = self.request(None, method="DELETE")
        self.assertEqual(status, 405)

    def test_any_other_path_is_404(self):
        status, _headers, _body = self.request(PING, path="/")
        self.assertEqual(status, 404)

    def test_unparseable_json_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("POST", serve.ENDPOINT, body=b"{not json",
                         headers={"Authorization": f"Bearer {BEARER}",
                                  "Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 400)
        finally:
            conn.close()

    def test_a_body_that_is_not_an_object_is_400(self):
        status, _headers, _body = self.request([1, 2, 3])
        self.assertEqual(status, 400)

    def test_an_oversized_body_is_refused_on_the_header_alone(self):
        """Refused before a byte of it is read. Reading first and judging after
        is how a size limit becomes the thing that exhausts the memory it was
        added to protect."""
        status, _headers, _body = self.request(
            PING, content_length=serve.MAX_BODY_BYTES + 1)
        self.assertEqual(status, 413)

    def test_an_unsupported_protocol_version_is_400(self):
        # The spec's MUST. The supported list is a literal in the source, so
        # widening it is a deliberate edit rather than an accident.
        status, _headers, _body = self.request(
            PING, extra={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)

    def test_a_supported_protocol_version_passes(self):
        for version in serve.SUPPORTED_PROTOCOL_VERSIONS:
            with self.subTest(version=version):
                status, _headers, _body = self.request(
                    PING, extra={"MCP-Protocol-Version": version})
                self.assertEqual(status, 200)

    def test_a_missing_protocol_version_is_allowed(self):
        # The spec says to assume 2025-03-26 when the header is absent, not to
        # refuse. Every request in the rest of this file omits it.
        status, _headers, _body = self.request(PING)
        self.assertEqual(status, 200)


class TestItIsTheSameBrain(ServeCase):
    """One tool layer, two transports. The whole reason brainlib/mcp.py
    exists is that these two lists cannot be allowed to differ."""

    def test_the_tool_list_over_http_matches_the_one_over_stdio(self):
        status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        self.assertEqual(status, 200)
        over_http = {t["name"] for t in json.loads(body)["result"]["tools"]}
        self.assertEqual(over_http, set(mcp.TOOLS_BY_NAME))

    def test_the_write_tool_is_visible_over_http(self):
        """brain_capture writes to a repository that auto-pushes, and it is
        reachable over this transport. Asserted rather than assumed, because
        the documentation says so in those words and a doc claim nothing tests
        is a doc claim that goes stale."""
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        names = {t["name"] for t in json.loads(body)["result"]["tools"]}
        self.assertIn("brain_capture", names)


class TestTokenStorage(unittest.TestCase):
    """The token goes to the OS keystore and nowhere else — never the repo,
    where lint refuses credentials, and never a config file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # A FileKeystore in a temp directory: the real keystore on the machine
        # running this suite must never be written to, and on a headless box
        # there may not be one at all.
        self.store = osbackend.FileKeystore(directory=Path(self.tmp.name))

    def test_a_minted_token_is_long_and_unguessable(self):
        first, second = serve.mint_token(), serve.mint_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_store_then_read_round_trips(self):
        minted = serve.mint_token()
        self.assertTrue(serve.store_token(minted, store=self.store))
        self.assertEqual(serve.read_token(store=self.store), minted)

    def test_no_token_reads_as_empty_rather_than_raising(self):
        self.assertEqual(serve.read_token(store=self.store), "")

    def test_the_token_file_is_not_world_readable(self):
        import os
        import stat

        if os.name == "nt":
            self.skipTest("POSIX file modes only")
        serve.store_token(serve.mint_token(), store=self.store)
        path = Path(self.tmp.name) / serve.TOKEN_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
