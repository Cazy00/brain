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
import contextlib
import http.client
import io
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

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
    ALLOW_TOOLS = None          # None means every tool, which is the default
    FREE_ATTEMPTS = None        # None means the server's own limiter settings

    def setUp(self):
        limiter = None if self.FREE_ATTEMPTS is None else \
            serve.Limiter(free=self.FREE_ATTEMPTS)
        self.server = serve.make_server(BEARER, "127.0.0.1", 0,
                                        allow_origin=self.ALLOW_ORIGIN,
                                        allow_tools=self.ALLOW_TOOLS,
                                        limiter=limiter)
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


class TestConnectionReuse(ServeCase):
    """HTTP/1.1 keep-alive — which every real client and every proxy does.

    Found 2026-07-29 by running a Cloudflare quick tunnel in front of this
    server rather than by reasoning about it. Every other test in this file
    opens one connection per request and closes it, so the whole class of bug
    below was structurally invisible here: the transport was only ever exercised
    in the one pattern real traffic does not use.
    """

    def reusing_one_connection(self, first, second):
        """(status of the first, status of the second) down ONE connection."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        self.addCleanup(conn.close)
        statuses = []
        for bearer, body in (first, second):
            headers = {"Content-Type": "application/json"}
            if bearer is not None:
                headers["Authorization"] = f"Bearer {bearer}"
            # http.client reconnects by itself if the server closed the socket,
            # which is exactly what a well-behaved client does.
            conn.request("POST", serve.ENDPOINT,
                         body=json.dumps(body).encode("utf-8"), headers=headers)
            response = conn.getresponse()
            response.read()
            statuses.append(response.status)
        return statuses

    def test_a_refused_request_does_not_poison_the_next_one(self):
        """The bug this class exists for.

        A POST refused on authentication returns before its body is read — on
        purpose, because reading a 10 MB body in order to reject it is the
        denial of service the size cap exists to prevent. Those bytes stay in
        the socket, and on a kept-alive connection they are parsed as the next
        request line: the client's next, entirely valid request comes back 400
        or 501. One wrong token and the connection is useless.
        """
        first, second = self.reusing_one_connection(
            ("wrong", PING), (BEARER, PING))
        self.assertEqual(first, 401)
        self.assertEqual(second, 200,
                         "a valid request was misparsed from the previous "
                         "request's unread body")

    def test_an_origin_refusal_does_not_poison_the_next_one(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        self.addCleanup(conn.close)
        conn.request("POST", serve.ENDPOINT, body=json.dumps(PING).encode("utf-8"),
                     headers={"Authorization": f"Bearer {BEARER}",
                              "Content-Type": "application/json",
                              "Origin": "https://evil.example"})
        first = conn.getresponse()
        first.read()
        self.assertEqual(first.status, 403)

        conn.request("POST", serve.ENDPOINT, body=json.dumps(PING).encode("utf-8"),
                     headers={"Authorization": f"Bearer {BEARER}",
                              "Content-Type": "application/json"})
        second = conn.getresponse()
        second.read()
        self.assertEqual(second.status, 200)

    def test_a_refusal_says_it_is_closing_the_connection(self):
        """Asserted on the header, so the fix cannot be quietly undone.

        Draining the body instead would work too, and is worse: it means
        reading whatever an unauthenticated caller chose to send, which is the
        thing the size cap refuses to do.
        """
        status, headers, _body = self.request(PING, bearer="wrong")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("Connection", "").lower(), "close")

    def test_a_successful_request_still_keeps_the_connection(self):
        # The fix must not turn every call into a new TCP connection — that is
        # a handshake per tool call, over a tunnel, from a phone.
        status, headers, _body = self.request(PING)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("Connection", "").lower(), "close")


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


class TestReadOnlyServing(ServeCase):
    """`--read-only`, which has to hold against a client that ignores it.

    The tool list is a courtesy: a client is free to call any name it likes,
    and the thing this mode defends against is precisely a client that does.
    So the two halves — what is advertised and what is executed — are asserted
    separately, and the second one is the one that matters.
    """
    ALLOW_TOOLS = mcp.READ_ONLY_TOOLS

    def tools(self):
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in json.loads(body)["result"]["tools"]}

    def test_the_write_tool_is_not_listed(self):
        listed = self.tools()
        self.assertNotIn("brain_capture", listed)
        self.assertEqual(listed, set(mcp.READ_ONLY_TOOLS))

    def test_the_read_tools_are_all_still_listed(self):
        # Read-only must mean "one tool fewer", not "a different brain". A mode
        # that quietly dropped brain_links as well would be discovered by
        # somebody far from their laptop, which is the one place this runs.
        self.assertEqual(self.tools(),
                         {"brain_search", "brain_read", "brain_links", "brain_recent"})

    def test_calling_the_write_tool_by_name_is_refused(self):
        """The whole mode, in one assertion.

        Filtering the advertised list and then running whatever arrives is not
        a read-only server, it is a suggestion. This calls the tool that was
        never listed, exactly as a client that ignored the list would.
        """
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "brain_capture",
                        "arguments": {"text": "this must never reach the inbox"}}})
        result = json.loads(body)["result"]
        self.assertTrue(result["isError"],
                        "brain_capture ran on a read-only server")
        self.assertIn("brain_capture", result["content"][0]["text"])

    def test_a_read_tool_still_works(self):
        """Proves the filter refuses rather than breaks. A read-only mode where
        nothing works passes every test above and is useless."""
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "brain_recent", "arguments": {"days": 1}}})
        result = json.loads(body)["result"]
        self.assertFalse(result.get("isError"), result["content"][0]["text"])


class TestTheReadOnlySetIsDerivedFailingClosed(unittest.TestCase):
    """How the four names are arrived at, which is more important than the four
    names — this is the part that has to keep being right after somebody adds a
    sixth tool in 2028."""

    def test_every_tool_says_whether_it_is_read_only(self):
        """The real guard. A tool added without the annotation fails here,
        rather than being silently left out of read-only serving (or, if the
        derivation were ever inverted, silently exposed by it)."""
        for tool in mcp.TOOLS:
            with self.subTest(tool=tool["name"]):
                annotations = tool.get("annotations")
                self.assertIsInstance(
                    annotations, dict,
                    f"{tool['name']} declares no annotations; "
                    "readOnlyHint is what --read-only filters on")
                self.assertIsInstance(
                    annotations.get("readOnlyHint"), bool,
                    f"{tool['name']} must say readOnlyHint true or false")

    def test_an_unannotated_tool_is_excluded_not_included(self):
        # Fail closed. The failure mode of the other direction is a write tool
        # served over a socket somebody believed was read-only.
        self.assertEqual(mcp.read_only_names([{"name": "brain_future"}]), ())
        self.assertEqual(mcp.read_only_names([{"name": "brain_future",
                                               "annotations": {}}]), ())
        self.assertEqual(
            mcp.read_only_names([{"name": "brain_future",
                                  "annotations": {"readOnlyHint": "yes"}}]), (),
            "a truthy non-boolean was treated as a promise of read-only")

    def test_the_write_tool_is_not_in_the_read_only_set(self):
        self.assertNotIn("brain_capture", mcp.READ_ONLY_TOOLS)
        self.assertEqual(len(mcp.READ_ONLY_TOOLS), len(mcp.TOOLS) - 1)


class TestDropBoxServing(ServeCase):
    """`--drop-box`, the mirror of `--read-only`, and the same rule holds.

    A client that never read tools/list and calls brain_search by name has to
    be refused, because the client is the thing being defended against and
    therefore cannot be the thing enforcing the restriction. Both halves get
    their own assertion, and the second one is the one that matters.

    No capture is made over this socket on purpose: run_cli shells out to THIS
    repo's bin/brain, so a successful capture here would write a note into the
    tester's own brain. What a capture hands back is asserted below, against
    the dispatcher, with the CLI mocked.
    """
    ALLOW_TOOLS = mcp.WRITE_ONLY_TOOLS

    def tools(self):
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in json.loads(body)["result"]["tools"]}

    def test_only_the_write_tool_is_listed(self):
        self.assertEqual(self.tools(), {"brain_capture"})

    def test_calling_a_read_tool_by_name_is_refused(self):
        _status, _headers, body = self.request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "brain_search", "arguments": {"query": "prices"}}})
        result = json.loads(body)["result"]
        self.assertTrue(result["isError"], "brain_search ran on a drop box")
        self.assertIn("brain_search", result["content"][0]["text"])

    def test_every_read_tool_is_refused_not_just_search(self):
        """A drop box that leaked one read tool is a drop box that leaks the
        brain — brain_read and brain_links each return note bodies too."""
        for tool, args in (("brain_read", {"id_or_path": "knowledge/index.md"}),
                           ("brain_links", {"id_or_path": "knowledge/index.md"}),
                           ("brain_recent", {"days": 7})):
            with self.subTest(tool=tool):
                _status, _headers, body = self.request(
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": tool, "arguments": args}})
                self.assertTrue(json.loads(body)["result"]["isError"],
                                f"{tool} ran on a drop box")


class TestTheWriteOnlySetIsDerivedFailingClosed(unittest.TestCase):
    """Derived from the same annotation as the read-only set, and failing in
    the same direction. A tool added in 2028 with no annotation belongs to
    NEITHER restricted mode — so it is never exposed by one of them by
    accident, in either direction."""

    def test_the_write_only_set_is_the_write_tool(self):
        self.assertEqual(set(mcp.WRITE_ONLY_TOOLS), {"brain_capture"})

    def test_an_unannotated_tool_joins_neither_set(self):
        for annotations in ({}, {"annotations": {}},
                            {"annotations": {"readOnlyHint": "no"}},
                            {"annotations": {"readOnlyHint": 0}}):
            table = [dict({"name": "brain_future"}, **annotations)]
            with self.subTest(annotations=annotations):
                self.assertEqual(mcp.read_only_names(table), ())
                self.assertEqual(mcp.write_only_names(table), (),
                                 "a tool nobody annotated was served by a drop box")

    def test_the_two_sets_never_overlap(self):
        self.assertEqual(set(mcp.READ_ONLY_TOOLS) & set(mcp.WRITE_ONLY_TOOLS), set())
        self.assertEqual(set(mcp.READ_ONLY_TOOLS) | set(mcp.WRITE_ONLY_TOOLS),
                         set(mcp.TOOLS_BY_NAME),
                         "a tool belongs to neither mode — it declares no readOnlyHint")


class TestTheDropBoxTellsTheCallerNothing(unittest.TestCase):
    """Clause 3 of the security contract: it acknowledges a write and stops.

    A duplicate hint, a "similar note exists", a count, or a forwarded error
    turns the drop box into a read oracle — the bot captures guesses and reads
    the brain one question at a time by watching which ones come back known.
    """

    def run_capture(self, arguments=None, returncode=0, stdout=None, stderr="", **kw):
        seen = []

        def fake_run_cli(args):
            seen.append(list(args))
            out = "/home/someone/brain/knowledge/inbox/2026-07-30-120000-hours.md\n" \
                if stdout is None else stdout
            return subprocess.CompletedProcess(args, returncode, stdout=out, stderr=stderr)

        with mock.patch.object(mcp, "run_cli", fake_run_cli):
            result = mcp.call_tool("brain_capture",
                                   arguments or {"text": "hours are 9 to 5"}, **kw)
        return seen, result, result["content"][0]["text"]

    def test_a_capture_never_reads_the_brain(self):
        """The structural guard behind the whole clause. Nothing on this path
        may look M up — not to dedup, not to enrich, not to be helpful."""
        seen, _result, _text = self.run_capture(source="support-bot")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "capture",
                         f"a drop-box capture ran another brain command: {seen}")

    def test_the_answer_is_an_acknowledgement_and_an_id(self):
        _seen, result, text = self.run_capture(source="support-bot")
        self.assertFalse(result.get("isError"), text)
        self.assertIn("2026-07-30-120000-hours", text)

    def test_the_answer_never_carries_the_path_of_the_brain(self):
        """The CLI prints an absolute path. Where this brain lives on disk is
        the first thing anybody attacking it would like to know."""
        _seen, _result, text = self.run_capture(source="support-bot")
        self.assertNotIn("/home/someone/brain", text)
        self.assertNotIn("knowledge/inbox", text)

    def test_a_failure_does_not_hand_back_the_reason(self):
        """A failed commit forwards git's and lint's stderr, and lint's output
        is text about OTHER notes. The caller learns that it failed."""
        _seen, result, text = self.run_capture(
            source="support-bot", returncode=1,
            stdout="SAVED BUT NOT COMMITTED: knowledge/people/dana.md missing sensitivity",
            stderr="On branch consolidate/2026-07-30")
        self.assertTrue(result["isError"])
        self.assertNotIn("dana", text.lower())
        self.assertNotIn("consolidate", text.lower())
        self.assertNotIn("knowledge/", text)

    def test_the_operator_still_sees_the_reason(self):
        """Terse to the caller, not silent to the owner: a drop box whose
        failures are invisible is one that stops working quietly."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.run_capture(source="support-bot", returncode=1,
                             stdout="SAVED BUT NOT COMMITTED: git said no")
        self.assertIn("git said no", err.getvalue())

    def test_an_endpoint_that_stamps_nothing_is_unchanged(self):
        """stdio is a subprocess on the operator's own machine. It gets the
        CLI's output verbatim, exactly as it always has."""
        _seen, result, text = self.run_capture()
        self.assertFalse(result.get("isError"))
        self.assertIn("/home/someone/brain/knowledge/inbox", text)


class TestTheDailyCap(unittest.TestCase):
    """A cap counted from the filesystem, so a restart is not a reset.

    An in-memory counter is a bypass an unstable bot finds by accident: crash,
    reconnect, full budget. Inbox filenames already begin with YYYY-MM-DD and
    every capture carries `source:`, so the count is derivable from what is
    already on disk and needs no state file of its own.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name)

    def write(self, day, index, source):
        (self.inbox / f"{day}-1200{index:02d}-note.md").write_text(
            f"---\ncreated: {day}\nsource: {source}\nstatus: draft\n---\n\nbody\n",
            encoding="utf-8")

    def cap(self, limit=3, source="support-bot", day="2026-07-30"):
        return serve.DailyCap(limit, source, inbox=self.inbox, today=lambda: day)

    def test_under_the_cap_says_nothing(self):
        self.write("2026-07-30", 1, "support-bot")
        self.assertEqual(self.cap()(), "")

    def test_at_the_cap_refuses(self):
        for i in range(3):
            self.write("2026-07-30", i, "support-bot")
        self.assertTrue(self.cap()(), "the boundary was not enforced")

    def test_the_count_survives_a_restart(self):
        for i in range(3):
            self.write("2026-07-30", i, "support-bot")
        # A second, freshly constructed cap — a process that just came back up
        # — sees the same three notes, because it counts the disk.
        self.assertTrue(self.cap()())

    def test_the_cap_is_per_source(self):
        """Two drop boxes on one brain must not spend each other's budget."""
        for i in range(3):
            self.write("2026-07-30", i, "other-bot")
        self.assertEqual(self.cap()(), "")

    def test_yesterdays_captures_do_not_count(self):
        for i in range(5):
            self.write("2026-07-29", i, "support-bot")
        self.assertEqual(self.cap()(), "")

    def test_an_unstamped_note_is_not_counted_against_a_source(self):
        """A note with no source did not come from this endpoint."""
        (self.inbox / "2026-07-30-120009-hand-written.md").write_text(
            "---\ncreated: 2026-07-30\nstatus: draft\n---\n\nbody\n", encoding="utf-8")
        self.assertEqual(self.cap(limit=1)(), "")

    def test_a_missing_inbox_is_not_a_crash(self):
        """Nothing has been captured yet. The first write must not 500."""
        cap = serve.DailyCap(3, "support-bot", inbox=self.inbox / "nope",
                             today=lambda: "2026-07-30")
        self.assertEqual(cap(), "")

    def test_the_refusal_reaches_the_caller_as_a_tool_error(self):
        for i in range(3):
            self.write("2026-07-30", i, "support-bot")
        called = []

        def fake_run_cli(args):
            called.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="x.md", stderr="")

        with mock.patch.object(mcp, "run_cli", fake_run_cli):
            result = mcp.call_tool("brain_capture", {"text": "one more"},
                                   source="support-bot", cap=self.cap())
        self.assertTrue(result["isError"])
        self.assertEqual(called, [], "the capture ran despite being over the cap")

    def test_the_refusal_says_nothing_about_the_brain(self):
        for i in range(3):
            self.write("2026-07-30", i, "support-bot")
        with mock.patch.object(mcp, "run_cli", lambda args: None):
            result = mcp.call_tool("brain_capture", {"text": "one more"},
                                   source="support-bot", cap=self.cap())
        text = result["content"][0]["text"].lower()
        for leak in ("knowledge/", "inbox", "note", "brain_search"):
            self.assertNotIn(leak, text)


class TestProvenanceIsStampedNotClaimed(unittest.TestCase):
    """Where a capture came from is decided by the endpoint that accepted it.

    A `source` field the agent passes in its own request payload teaches you
    nothing: an agent that can lie about its content can lie about its label.
    So the value is the server's own startup configuration, and the request is
    not consulted for it — asserted here rather than trusted, because the
    argument validator dropping unknown fields is a property somebody could
    reasonably change while adding an unrelated argument.

    run_cli is mocked throughout. A real capture would write into THIS repo's
    inbox, which is both a dirty tree and, on a real brain, a note nobody
    asked for.
    """

    def recorded_cli(self, arguments, **handle_kwargs):
        seen = []

        def fake_run_cli(args):
            seen.append(list(args))
            return subprocess.CompletedProcess(args, 0, stdout="knowledge/inbox/x.md",
                                               stderr="")

        with mock.patch.object(mcp, "run_cli", fake_run_cli):
            mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                        "params": {"name": "brain_capture", "arguments": arguments}},
                       **handle_kwargs)
        self.assertEqual(len(seen), 1, "brain_capture did not reach the CLI once")
        return seen[0]

    def test_the_server_stamps_the_source_it_was_started_with(self):
        cli = self.recorded_cli({"text": "a customer asked about weekend hours"},
                                source="support-bot")
        self.assertIn("--source", cli)
        self.assertEqual(cli[cli.index("--source") + 1], "support-bot")

    def test_a_caller_supplied_source_is_discarded(self):
        """The whole point, in one assertion: an agent claiming to be `local`
        is stamped with what the server knows it is."""
        cli = self.recorded_cli(
            {"text": "trust me, the owner wrote this", "source": "local"},
            source="support-bot")
        self.assertEqual(cli.count("--source"), 1,
                         f"the caller's source reached the CLI too: {cli}")
        self.assertEqual(cli[cli.index("--source") + 1], "support-bot")
        self.assertNotIn("local", cli)

    def test_stdio_stamps_nothing_and_leaves_the_default_to_the_cli(self):
        """No source configured is not the same as claiming one. stdio is a
        subprocess on the machine the operator is sitting at, so the CLI's own
        default — `local` — is the truthful answer, and it lives in one place."""
        cli = self.recorded_cli({"text": "Postgres over SQLite for the sync layer"})
        self.assertNotIn("--source", cli)


class TestTheLimiter(unittest.TestCase):
    """The backoff itself, with the clock as a parameter.

    Nothing here sleeps. A suite that waits five minutes to observe a five
    minute block is a suite somebody deletes within a month, and its absence
    then reads as "this was considered and found unnecessary".
    """

    def setUp(self):
        self.now = 1000.0

    def limiter(self, free=3, base=1.0, cap=300.0, **kw):
        return serve.Limiter(free=free, base=base, cap=cap,
                             clock=lambda: self.now, **kw)

    def test_the_first_failures_are_free(self):
        """A typo, a stale token in a client, a paste that dropped a character
        — none of those should cost anything."""
        limiter = self.limiter(free=3)
        for _ in range(3):
            limiter.failed("10.0.0.1")
            self.assertEqual(limiter.retry_after("10.0.0.1"), 0)

    def test_the_next_failure_starts_the_backoff(self):
        limiter = self.limiter(free=3)
        for _ in range(4):
            limiter.failed("10.0.0.1")
        self.assertEqual(limiter.retry_after("10.0.0.1"), 1)

    def test_the_backoff_doubles_and_is_capped(self):
        limiter = self.limiter(free=0, base=1.0, cap=8.0)
        for expected in (1, 2, 4, 8, 8, 8):
            limiter.failed("10.0.0.1")
            self.assertEqual(limiter.retry_after("10.0.0.1"), expected)

    def test_time_passing_clears_the_block(self):
        limiter = self.limiter(free=0, base=4.0)
        limiter.failed("10.0.0.1")
        self.assertEqual(limiter.retry_after("10.0.0.1"), 4)
        self.now += 4.0
        self.assertEqual(limiter.retry_after("10.0.0.1"), 0)

    def test_a_correct_token_clears_the_count(self):
        """Otherwise a client that reconnects all day accumulates its way to a
        block on the strength of one typo this morning."""
        limiter = self.limiter(free=2)
        for _ in range(2):
            limiter.failed("10.0.0.1")
        limiter.succeeded("10.0.0.1")
        # Four failures in total now. Without the reset that is two past the
        # allowance and a block; with it, the count starts again at zero.
        for _ in range(2):
            limiter.failed("10.0.0.1")
        self.assertEqual(limiter.retry_after("10.0.0.1"), 0,
                         "the count survived a successful authentication")

    def test_one_address_does_not_block_another(self):
        limiter = self.limiter(free=0)
        limiter.failed("10.0.0.1")
        self.assertTrue(limiter.retry_after("10.0.0.1"))
        self.assertEqual(limiter.retry_after("10.0.0.2"), 0)

    def test_the_table_does_not_grow_without_bound(self):
        """A dict keyed by remote address that only ever grows is a memory
        exhaustion primitive, reachable by anyone who can send an unauthorised
        request. A limiter that becomes the denial of service it was added to
        prevent is worse than no limiter."""
        limiter = self.limiter(free=0, max_keys=64)
        for n in range(1000):
            limiter.failed("10.0.%d.%d" % (n // 256, n % 256))
        self.assertLessEqual(limiter.tracked(), 64)

    def test_entries_that_have_gone_quiet_are_forgotten(self):
        limiter = self.limiter(free=0, idle=60.0)
        limiter.failed("10.0.0.1")
        self.now += 61.0
        limiter.failed("10.0.0.2")
        self.assertEqual(limiter.tracked(), 1)


class TestFailedAuthIsRateLimited(ServeCase):
    """The limiter through the socket, which is the only place it matters."""
    FREE_ATTEMPTS = 2

    def fail_until_blocked(self):
        for _ in range(self.FREE_ATTEMPTS + 1):
            status, _headers, _body = self.request(PING, bearer="wrong")
            self.assertEqual(status, 401, "a wrong token stopped being a 401")

    def test_a_run_of_bad_tokens_ends_in_429(self):
        self.fail_until_blocked()
        status, headers, _body = self.request(PING, bearer="wrong")
        self.assertEqual(status, 429)
        self.assertIn("Retry-After", headers,
                      "a 429 without Retry-After tells a client nothing about when "
                      "to come back, so it comes back immediately")
        self.assertGreater(int(headers["Retry-After"]), 0)

    def test_the_block_holds_against_the_correct_token_too(self):
        """Deliberate, and the honest cost of the control.

        The block is checked BEFORE the token is compared — that is the whole
        slow-down, because a guess that is never looked at cannot be a guess
        that succeeds. The price is that the operator's own next request waits,
        which is the same trade every SSH server on the internet makes.
        """
        self.fail_until_blocked()
        status, _headers, _body = self.request(PING, bearer=BEARER)
        self.assertEqual(status, 429)

    def test_a_good_token_resets_the_count(self):
        for _ in range(self.FREE_ATTEMPTS):
            self.request(PING, bearer="wrong")
        self.assertEqual(self.request(PING)[0], 200)
        for _ in range(self.FREE_ATTEMPTS):
            self.assertEqual(self.request(PING, bearer="wrong")[0], 401)
        self.assertEqual(self.request(PING)[0], 200,
                         "a successful authentication did not clear the count")

    def test_an_origin_refusal_is_not_counted(self):
        """Counting them would hand any web page a lockout.

        A page on evil.com can make the operator's own browser send requests to
        127.0.0.1 — refused on Origin, and from the operator's own address. If
        those counted, a `fetch` in a loop would lock the operator out of their
        own brain: the limiter would become the attack it exists to blunt.
        """
        for _ in range(self.FREE_ATTEMPTS + 5):
            status, _headers, _body = self.request(
                PING, extra={"Origin": "https://evil.example"})
            self.assertEqual(status, 403)
        self.assertEqual(self.request(PING)[0], 200,
                         "browser origins locked the operator out")


class TestTheLimiterIsNotTheServer(ServeCase):
    """A control that cannot be told apart from a broken server is not a
    control anybody can debug. A fresh server answers the same good token."""
    FREE_ATTEMPTS = 1

    def test_a_fresh_server_answers_the_token_the_blocked_one_refused(self):
        for _ in range(self.FREE_ATTEMPTS + 1):
            self.request(PING, bearer="wrong")
        self.assertEqual(self.request(PING, bearer=BEARER)[0], 429)

        other = serve.make_server(BEARER, "127.0.0.1", 0)
        self.addCleanup(other.server_close)
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(other.shutdown)

        conn = http.client.HTTPConnection("127.0.0.1", other.server_address[1],
                                          timeout=15)
        try:
            conn.request("POST", serve.ENDPOINT, body=json.dumps(PING).encode("utf-8"),
                         headers={"Authorization": f"Bearer {BEARER}",
                                  "Content-Type": "application/json"})
            self.assertEqual(conn.getresponse().status, 200)
        finally:
            conn.close()


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


class TestTheCli(unittest.TestCase):
    """`run_serve` without ever listening.

    `run` is injected the same way phase_backup's is: the tests that actually
    bind a socket are above, and what is left to check here is the wiring —
    that the value the server is handed is the stored one, and that the two
    paths which must never reach a socket at all do not.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = osbackend.FileKeystore(directory=Path(self.tmp.name))
        self.started = []
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_serve(self, *argv):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return serve.run_serve(list(argv), store=self.store,
                                   run=self.started.append)

    def test_it_refuses_to_start_without_one_and_names_the_fix(self):
        """A refusal, not a warning, and not a silent mint.

        Minting one here would be worse than refusing: a credential that
        appears without anybody seeing it is a credential nobody knows to
        protect, and it would authorise a write tool.
        """
        code = self.run_serve()
        self.assertEqual(code, 1)
        self.assertEqual(self.started, [], "it opened a socket anyway")
        self.assertIn("--new-token", self.err.getvalue())

    def test_new_token_prints_it_once_and_stores_it(self):
        code = self.run_serve("--new-token")
        self.assertEqual(code, 0)
        self.assertEqual(self.started, [], "--new-token started a server")
        stored = serve.read_token(store=self.store)
        self.assertTrue(stored)
        self.assertIn(stored, self.out.getvalue(),
                      "the value was stored but never shown — it is unrecoverable now")
        self.assertIn("only time", self.out.getvalue().lower(),
                      "nothing told the reader this is the only time they see it")

    def test_new_token_says_it_breaks_every_client_already_wired(self):
        # Rotation is the correct behaviour. Rotation nobody was told about is
        # a morning spent debugging four clients that all stopped at once.
        self.run_serve("--new-token")
        self.assertIn("stop working", self.err.getvalue() + self.out.getvalue())

    def test_a_stored_value_is_what_the_server_gets(self):
        self.run_serve("--new-token")
        stored = serve.read_token(store=self.store)
        self.out, self.err = io.StringIO(), io.StringIO()

        code = self.run_serve("--port", "0")

        self.assertEqual(code, 0)
        self.assertEqual(len(self.started), 1)
        server = self.started[0]
        self.addCleanup(server.server_close)
        self.assertEqual(server.token, stored)
        self.assertEqual(server.server_address[0], "127.0.0.1",
                         "the default bind is not loopback")

    def test_the_banner_never_prints_the_value_itself(self):
        """It prints the registration command with a placeholder.

        Printing the real one on every start writes it into scrollback, any
        terminal log, and any screen recording — repeatedly, long after the one
        moment the operator was paying attention to it.
        """
        self.run_serve("--new-token")
        stored = serve.read_token(store=self.store)
        self.out, self.err = io.StringIO(), io.StringIO()

        self.run_serve("--port", "0")

        printed = self.out.getvalue() + self.err.getvalue()
        self.assertNotIn(stored, printed)
        self.assertIn("BRAIN_TOKEN", printed)
        self.assertIn("claude mcp add", printed,
                      "the one client this is known to work with is not named")

    def test_a_public_bind_says_what_it_is_exposing(self):
        self.run_serve("--new-token")
        self.out, self.err = io.StringIO(), io.StringIO()

        code = self.run_serve("--bind", "0.0.0.0", "--port", "0")

        self.assertEqual(code, 0)
        self.addCleanup(self.started[0].server_close)
        warned = self.err.getvalue()
        self.assertIn("0.0.0.0", warned)
        # It must name the consequence, not just the address. What is on the
        # other end of this socket can write to the brain.
        self.assertIn("write", warned.lower())

    def test_loopback_is_not_warned_about(self):
        self.run_serve("--new-token")
        self.out, self.err = io.StringIO(), io.StringIO()
        self.run_serve("--port", "0")
        self.addCleanup(self.started[0].server_close)
        self.assertNotIn("EXPOSED", self.err.getvalue())

    def serve_and_get_server(self, *argv):
        self.run_serve("--new-token")
        self.out, self.err = io.StringIO(), io.StringIO()
        code = self.run_serve(*argv)
        self.assertEqual(code, 0)
        self.addCleanup(self.started[0].server_close)
        return self.started[0]

    def test_read_only_narrows_what_the_server_will_run(self):
        # A set, not a sequence: this is a membership test on every call, and
        # ordering it would be an invitation to depend on the order.
        server = self.serve_and_get_server("--read-only", "--port", "0")
        self.assertEqual(set(server.allow_tools), set(mcp.READ_ONLY_TOOLS))

    def test_without_the_flag_every_tool_is_served(self):
        # The default has to stay the default. `serve` with no flags is what
        # the docs, the banner and every existing registration assume.
        server = self.serve_and_get_server("--port", "0")
        self.assertIsNone(server.allow_tools)

    def test_the_banner_says_which_mode_it_is_in(self):
        """Both ways round, because the interesting one is the default.

        Somebody who ran it read-only last week and forgets the flag this week
        is exposing a write tool, and the only thing standing between them and
        not noticing is what the terminal said at startup.
        """
        self.serve_and_get_server("--read-only", "--port", "0")
        read_only = self.err.getvalue().lower()
        self.assertIn("read-only", read_only)
        self.assertNotIn("write", read_only.replace("read-only", ""))

        self.started, self.out, self.err = [], io.StringIO(), io.StringIO()
        self.serve_and_get_server("--port", "0")
        writable = self.err.getvalue().lower()
        self.assertIn("brain_capture", writable)
        self.assertIn("write", writable)


class TestTheDropBoxCli(TestTheCli):
    """The flags, and the four ways of asking for a drop box that are wrong.

    Inherits the harness above: same injected store, same injected runner, so
    none of these opens a socket either.
    """

    def test_it_narrows_what_the_server_will_run(self):
        server = self.serve_and_get_server("--drop-box", "--source", "support-bot",
                                           "--port", "0")
        self.assertEqual(set(server.allow_tools), set(mcp.WRITE_ONLY_TOOLS))

    def test_the_source_reaches_the_server(self):
        server = self.serve_and_get_server("--drop-box", "--source", "support-bot",
                                           "--port", "0")
        self.assertEqual(server.source, "support-bot")

    def test_a_cap_is_always_configured(self):
        """There is no flag to turn it off, for the same reason the limiter has
        none: 'they probably will not send that many' is an argument, and this
        is a control."""
        server = self.serve_and_get_server("--drop-box", "--source", "support-bot",
                                           "--port", "0")
        self.assertEqual(server.cap.limit, serve.DEFAULT_DAILY_CAP)
        self.assertEqual(server.cap.source, "support-bot")

    def test_a_cap_can_be_lowered(self):
        server = self.serve_and_get_server("--drop-box", "--source", "support-bot",
                                           "--daily-cap", "5", "--port", "0")
        self.assertEqual(server.cap.limit, 5)

    def refuses(self, *argv):
        self.run_serve("--new-token")
        self.out, self.err = io.StringIO(), io.StringIO()
        code = self.run_serve(*argv)
        self.assertNotEqual(code, 0, "it started anyway")
        self.assertEqual(self.started, [], "it opened a socket anyway")
        return self.err.getvalue() + self.out.getvalue()

    def test_a_drop_box_without_a_source_refuses_to_start(self):
        """An unattributed drop box is worse than none: it produces exactly the
        inbox notes the consolidator cannot weigh, while looking like it
        works."""
        said = self.refuses("--drop-box", "--port", "0")
        self.assertIn("--source", said)

    def test_a_source_without_a_drop_box_refuses_to_start(self):
        """The pairing is what makes 'this endpoint stamps a foreign source'
        mean 'this endpoint answers tersely'. Half of it is a surprise."""
        said = self.refuses("--source", "support-bot", "--port", "0")
        self.assertIn("--drop-box", said)

    def test_a_malformed_source_refuses_to_start(self):
        said = self.refuses("--drop-box", "--source", "Support Bot!", "--port", "0")
        self.assertIn("--source", said)

    def test_a_drop_box_that_is_also_read_only_refuses_to_start(self):
        """The operator will reach for the combination, and it is not a mode:
        it is two deployments, and therefore two processes on two ports."""
        said = self.refuses("--drop-box", "--source", "support-bot", "--read-only",
                            "--port", "0")
        self.assertIn("two", said.lower())
        self.assertIn("--read-only", said)

    def test_a_cap_of_zero_refuses_to_start(self):
        said = self.refuses("--drop-box", "--source", "support-bot",
                            "--daily-cap", "0", "--port", "0")
        self.assertIn("--daily-cap", said)

    def test_the_banner_says_what_this_endpoint_is(self):
        self.serve_and_get_server("--drop-box", "--source", "support-bot", "--port", "0")
        said = self.err.getvalue()
        self.assertIn("support-bot", said, "the banner does not name the source")
        self.assertIn(str(serve.DEFAULT_DAILY_CAP), said, "the cap is not stated")
        self.assertIn("brain_capture", said)
        self.assertNotIn("brain_search", said, "a drop box banner advertised a read tool")

    def test_the_banner_says_the_notes_are_proposals(self):
        """The operator has to know what arrives here is not knowledge yet, or
        the drop box reads as a way for a bot to teach the brain things."""
        self.serve_and_get_server("--drop-box", "--source", "support-bot", "--port", "0")
        self.assertIn("inbox", self.err.getvalue().lower())

    def test_an_exposed_drop_box_says_what_it_exposes(self):
        """Different from the read-only warning, and it has to be: what is at
        risk here is what gets WRITTEN, not what can be read."""
        self.run_serve("--new-token")
        self.out, self.err = io.StringIO(), io.StringIO()
        code = self.run_serve("--drop-box", "--source", "support-bot",
                              "--bind", "0.0.0.0", "--port", "0")
        self.assertEqual(code, 0)
        self.addCleanup(self.started[0].server_close)
        warned = self.err.getvalue()
        self.assertIn("EXPOSED", warned)
        self.assertNotIn("read EVERY note", warned)

    def test_the_slug_rule_is_the_toolbelts_rule(self):
        """Two enforcement points — serve refuses a bad slug at startup, the
        CLI refuses one at write time — and this is what keeps them one rule.
        They are allowed to be two copies; they are not allowed to differ."""
        brain = (ROOT / "bin" / "brain").read_text(encoding="utf-8")
        self.assertIn(serve.SOURCE_RE.pattern, brain)


class TestServeInTheHelp(unittest.TestCase):
    def test_serve_is_listed(self):
        import subprocess

        done = subprocess.run([sys.executable, str(ROOT / "bin" / "brain"), "--help"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertIn("brain serve", done.stdout)

    def serve_help(self):
        import subprocess

        return subprocess.run([sys.executable, str(ROOT / "bin" / "brain"),
                               "serve", "--help"],
                              cwd=str(ROOT), capture_output=True, text=True, timeout=120)

    def test_serve_help_does_not_start_a_server(self):
        done = self.serve_help()
        self.assertEqual(done.returncode, 0)
        self.assertNotIn("listening", done.stdout.lower())

    def test_serve_help_shows_serves_own_usage(self):
        """Not the toolbelt's global help, which is what it used to show.

        Found 2026-07-29 while verifying the documentation for --read-only: the
        flag was written up in SETUP.md, README.md and serve's own USAGE, and
        the one place somebody would actually look for it — asking the command
        — answered with a page that did not mention it. Documentation nobody
        can reach is documentation that is not there.
        """
        done = self.serve_help()
        self.assertIn("brain serve — reach this brain", done.stdout)
        self.assertIn("--read-only", done.stdout)
        self.assertIn("--drop-box", done.stdout)
        self.assertIn("--new-token", done.stdout)


if __name__ == "__main__":
    unittest.main()
