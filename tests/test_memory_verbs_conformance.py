#!/usr/bin/env python3
"""The standing conformance cases for the memory verbs (gate G1).

`tests/fixtures/memory-verbs/cases.json` is gbrain's MEMORY_VERBS v1 suite at
commit fd22a75 (MIT; the notice is beside it). gbrain is never installed or run:
this is a small runner of our own. The cases WRITE facts, so the runner only ever
targets a temporary corpus, and refuses to start against anything else.

Case N is the Nth entry of the array, counting from 1 (the file has no id).
Sixteen cases apply to this brain and all of them must pass. Eight do not, because
this brain does not serve `entity`, `context_pack` or `synthesize`; in their place
the runner checks that each of those verbs is absent from `tools/list` and that
calling it answers the `unavailable` envelope.

The runner is run twice, over the two transports the verbs are served on: the
stdio dispatcher (`brain-mcp`, and `brain serve` which shares it) and the remote
endpoint (`brain-http`) on both of its profiles.

Run:  python3 -m unittest tests.test_memory_verbs_conformance -v
"""
import json
import os
import re
import secrets
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memverbs_support as support  # noqa: E402
import test_remote as remote  # noqa: E402
from brainlib import mcp, verbs  # noqa: E402

CASES_FILE = Path(__file__).resolve().parent / "fixtures" / "memory-verbs" / "cases.json"
SERVED = ("remember", "recall", "forget", "delta")
APPLICABLE = {"remember": [1, 2, 3, 4, 5, 6, 17], "recall": [7, 8, 9, 10],
              "forget": [15, 16, 18], "delta": [23, 24]}
REPLACED = {"entity": [11, 12, 13, 14], "context_pack": [21, 22], "synthesize": [19, 20]}

# What a response of each verb must look like (the shape part of the contract's
# schemas; the contract file itself lives in a private repository and is not copied).
FACT_KINDS = {"event", "preference", "commitment", "belief", "fact"}
NOTE_KINDS = {"decision", "preference", "fact", "lesson", "idea", "correction", "other"}
TRUST = {"current", "provisional", "journal", "stale", "not_current"}


def is_nullable_str(value):
    return value is None or isinstance(value, str)


def check_fact(test, fact):
    for key in ("fact_id", "fact", "provenance", "created_at"):
        test.assertIsInstance(fact[key], str, key)
    test.assertIn(fact["kind"], FACT_KINDS)
    test.assertIn(fact["visibility"], {"world", "private"})
    test.assertEqual(fact["trust"], "provisional")
    test.assertIsInstance(fact["markers"], list)
    test.assertTrue(fact["fact_id"].startswith("inbox-"))
    for key in ("entity", "valid_from", "valid_until"):
        test.assertTrue(is_nullable_str(fact.get(key)), key)
    if "note_kind" in fact:
        test.assertIn(fact["note_kind"], NOTE_KINDS)


def check_shape(test, verb, body):
    """The contract's response schema for `verb`, as far as a client depends on it."""
    test.assertEqual(body["protocol_version"], 1)
    if verb == "remember":
        test.assertIsInstance(body["id"], str)
        test.assertIn(body["status"], {"inserted", "duplicate"})
        test.assertIsInstance(body["status_text"], str)
        test.assertIn("entity_slug", body)
        test.assertIn("valid_until", body)
        test.assertTrue(is_nullable_str(body["entity_slug"]))
        test.assertTrue(is_nullable_str(body["valid_until"]))
    elif verb == "recall":
        test.assertIsInstance(body["facts"], list)
        test.assertEqual(body["total"], len(body["facts"]))
        test.assertIsInstance(body["results"], list)
        test.assertEqual(body["facts_order"], "newest")
        for fact in body["facts"]:
            check_fact(test, fact)
        for hit in body["results"]:
            for key in ("slug", "title", "chunk", "provenance", "path", "folder", "kind"):
                test.assertIsInstance(hit[key], str, key)
            test.assertIn(hit["evidence"], {"alias_hit", "exact_title_match",
                                            "high_vector_match", "keyword_exact",
                                            "weak_semantic"})
            test.assertIn(hit["create_safety"], {"exists", "probable", "unknown"})
            test.assertEqual(hit["source_id"], "default")
            test.assertIn(hit["trust"], TRUST)
            test.assertIsInstance(hit["markers"], list)
        if "search_degraded" in body:
            test.assertEqual(body["search_degraded"], "embed_unavailable")
    elif verb == "forget":
        test.assertIsInstance(body["id"], str)
        test.assertIsInstance(body["expired"], bool)
        test.assertIn("reason", body)
    elif verb == "delta":
        test.assertRegex(body["since"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        test.assertEqual(body["threads"], [])
        test.assertIsInstance(body["text"], str)
        test.assertIsInstance(body["has_more"], bool)
        test.assertIsInstance(body["next_cursor"]["cursor"], str)
        for page in body["pages"]:
            for key in ("path", "title", "folder", "modified"):
                test.assertIsInstance(page[key], str, key)
            test.assertTrue(is_nullable_str(page["id"]))
            test.assertIn(page["trust"], TRUST)
            test.assertIsInstance(page["markers"], list)
        for fact in body["facts"]:
            check_fact(test, fact)


def dig(value, path):
    """`facts.0.fact_id` against a decoded body; (found, value)."""
    for part in path.split("."):
        if isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return False, None
    return True, value


TYPES = {"string": str, "array": list, "boolean": bool, "object": dict,
         "number": (int, float), "integer": int}


def check_expectation(test, body, rule):
    found, value = dig(body, rule["path"])
    label = "%s %s" % (rule["path"], {k: v for k, v in rule.items() if k != "path"})
    if "absentOrNotContains" in rule:
        test.assertNotIn(rule["absentOrNotContains"], json.dumps(value) if found else "", label)
        return
    test.assertTrue(found, "missing " + label)
    if "type" in rule:
        test.assertIsInstance(value, TYPES[rule["type"]], label)
    if "equals" in rule:
        test.assertEqual(value, rule["equals"], label)
    if "oneOf" in rule:
        test.assertIn(value, rule["oneOf"], label)
    if "gte" in rule:
        test.assertGreaterEqual(value, rule["gte"], label)
    if "lte" in rule:
        test.assertLessEqual(value, rule["lte"], label)


class Stdio(object):
    """The stdio dispatcher, called as a client would: JSON-RPC in, JSON-RPC out."""
    name = "stdio"

    def __init__(self, corpus):
        self.corpus = corpus
        self.next_id = 0

    def rpc(self, method, params=None):
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        return mcp.handle(message, via="stdio", session={})["result"]

    def tool_names(self):
        return {tool["name"] for tool in self.rpc("tools/list")["tools"]}

    def call(self, verb, params):
        result = self.rpc("tools/call", {"name": verb, "arguments": params})
        return result["isError"], result["content"][0]["text"]

    def close(self):
        pass


class Http(object):
    """The remote endpoint, both profiles, reached through a real socket."""
    name = "http"

    def __init__(self, corpus):
        self.brain = remote.RemoteBrain(limits={"read": (10_000, 60.0), "capture": (10_000, 60.0),
                                                "protocol": (10_000, 60.0)})
        self.corpus = None

    def tool_names(self, host="brain.test"):
        _s, _h, body = self.brain.rpc("tools/list", host=host)
        return {tool["name"] for tool in body["result"]["tools"]}

    def call(self, verb, params, host="brain.test"):
        status, body = self.brain.call(verb, params, host=host)
        assert status == 200, (status, body)
        result = body["result"]
        return result["isError"], result["content"][0]["text"]

    def close(self):
        self.brain.close()


class ConformanceBase(object):
    """The runner. `make_client` is the only thing a transport supplies."""

    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
        cls.corpus = support.Corpus()
        cls.addClassCleanup(cls.corpus.close)
        # Never a live brain: the data root the cases write to must be a temporary one.
        tmp = os.path.realpath(tempfile.gettempdir())
        assert os.path.realpath(os.environ["BRAIN_DATA_ROOT"]).startswith(tmp + os.sep), \
            "refusing to run the write cases outside a temporary directory"
        cls.client = cls.make_client(cls.corpus)
        cls.addClassCleanup(cls.client.close)
        # The entity pattern is lowercase letters, digits and '-': so is the marker.
        cls.marker = "cf" + secrets.token_hex(4)
        cls.saved = {}

    @classmethod
    def make_client(cls, corpus):
        raise NotImplementedError

    def substitute(self, value):
        if isinstance(value, dict):
            return {key: self.substitute(item) for key, item in value.items()}
        if isinstance(value, str):
            whole = re.fullmatch(r"\{\{id:(\w+)\}\}", value)
            if whole:
                return self.saved[whole.group(1)]
            return value.replace("{{marker}}", self.marker)
        return value

    def run_case(self, number, case):
        params = self.substitute(case["params"])
        is_error, text = self.client.call(case["verb"], params)
        body = json.loads(text)
        label = "case %d: %s" % (number, case["name"])
        if "expectErrorCode" in case:
            self.assertTrue(is_error, label)
            self.assertEqual(body["error"], case["expectErrorCode"], label)
            self.assertEqual(body["protocol_version"], 1, label)
            if case.get("expectSuggestion"):
                self.assertTrue(str(body.get("suggestion", "")).strip(), label)
            return body
        self.assertFalse(is_error, "%s -> %s" % (label, text))
        if case.get("validateSchema"):
            check_shape(self, case["verb"], body)
        for rule in case.get("expect", []):
            check_expectation(self, body, rule)
        if "saveAs" in case:
            found, value = dig(body, case["saveAs"]["path"])
            self.assertTrue(found, label)
            self.saved[case["saveAs"]["key"]] = value
        return body

    def test_the_applicable_cases_are_the_ones_the_contract_names(self):
        self.assertEqual(len(self.cases), 24)
        by_verb = {}
        for number, case in enumerate(self.cases, start=1):
            by_verb.setdefault(case["verb"], []).append(number)
        for verb, numbers in APPLICABLE.items():
            self.assertEqual(by_verb[verb], numbers, verb)
        for verb, numbers in REPLACED.items():
            self.assertEqual(by_verb[verb], numbers, verb)
        self.assertEqual(sum(len(n) for n in APPLICABLE.values()), 16)
        self.assertEqual(sum(len(n) for n in REPLACED.values()), 8)

    def test_the_sixteen_applicable_cases_pass_in_order(self):
        """The cases are a story (remember, recall it, forget it, try to say it again), so
        they run in file order, in one test."""
        ran = []
        for number, case in enumerate(self.cases, start=1):
            if case["verb"] not in SERVED:
                continue
            with self.subTest(case=number, name=case["name"]):
                self.run_case(number, case)
            ran.append(number)
        self.assertEqual(ran, sorted(n for numbers in APPLICABLE.values() for n in numbers))
        self.assertEqual(len(ran), 16)

    def test_the_eight_other_cases_are_replaced_by_the_unlisted_and_unavailable_check(self):
        listed = self.client.tool_names()
        for verb, numbers in REPLACED.items():
            with self.subTest(verb=verb, cases=numbers):
                self.assertNotIn(verb, listed)
                for number in numbers:
                    self.assertEqual(self.cases[number - 1]["verb"], verb)
                is_error, text = self.client.call(verb, {})
                body = json.loads(text)
                self.assertTrue(is_error)
                self.assertEqual(body["error"], "unavailable")
                self.assertEqual(body["protocol_version"], 1)
                self.assertEqual(body["detail"], "unsupported_verb:" + verb)
                self.assertIn("recall", body["suggestion"])
                self.assertIn(verb, body["message"])
        self.assertTrue(set(SERVED) <= listed)


class StdioConformanceTests(ConformanceBase, unittest.TestCase):
    @classmethod
    def make_client(cls, corpus):
        return Stdio(corpus)


class HttpConformanceTests(ConformanceBase, unittest.TestCase):
    """The same cases over the remote endpoint. The remote caller is the one the facts
    arm's privacy fence is for, so this run also holds the private fact back."""

    @classmethod
    def make_client(cls, corpus):
        return Http(corpus)

    def test_the_private_fact_never_leaves_over_http(self):
        self.run_case(6, self.cases[5])
        _e, text = self.client.call("recall", {"entity": "people/conformance-" + self.marker})
        self.assertNotIn("PRIVATE-SENTINEL", text)
        _e, text = self.client.call("delta", {"since": "1970-01-01"})
        self.assertNotIn("PRIVATE-SENTINEL", text)

    def test_the_read_endpoint_serves_the_read_verbs_only(self):
        inbox_dir = self.client.brain.data_root / "knowledge" / "inbox"
        before = sorted(inbox_dir.glob("*.md"))
        read = self.client.tool_names(host="brain-read.test")
        self.assertEqual(read, {"recall", "delta", "brain_search", "brain_read",
                                "brain_links", "brain_recent"})
        for verb, params in (("remember", {"fact": "x fact", "provenance": "p"}),
                             ("forget", {"id": "inbox-x"})):
            is_error, text = self.client.call(verb, params, host="brain-read.test")
            body = json.loads(text)
            self.assertEqual((is_error, body["error"], body["protocol_version"]),
                             (True, "scope_denied", 1))
        for verb in verbs.UNSUPPORTED:
            is_error, text = self.client.call(verb, {}, host="brain-read.test")
            body = json.loads(text)
            self.assertEqual(body["error"], "unavailable")
            self.assertIn("This brain serves recall and delta.", body["suggestion"])
        is_error, text = self.client.call("recall", {"query": "tunnel"}, host="brain-read.test")
        self.assertFalse(is_error)
        self.assertEqual(json.loads(text)["results"][0]["slug"], "tunnel-facts")
        self.assertEqual(sorted(inbox_dir.glob("*.md")), before, "the read endpoint wrote a note")

    def test_initialize_and_discover_advertise_the_contract_and_the_engine_version(self):
        from brainlib import httpmcp, version
        for host, served in (("brain.test", ["recall", "remember", "forget", "delta"]),
                             ("brain-read.test", ["recall", "delta"])):
            with self.subTest(host=host):
                status, _h, body = self.client.brain.rpc(
                    "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}}, host=host)
                self.assertEqual(status, 200)
                result = body["result"]
                self.assertEqual(result["serverInfo"],
                                 {"name": "brain", "version": version.VERSION})
                self.assertEqual(result["_meta"]["brain-verbs"],
                                 {"contract": "brain-verbs.v1", "memory_verbs": 1,
                                  "verbs": served,
                                  "unsupported": ["entity", "context_pack", "synthesize"]})
        self.assertEqual(httpmcp.SERVER_VERSION, version.VERSION)

    def test_the_log_names_the_new_tools_as_themselves(self):
        self.client.call("recall", {"query": "tunnel"})
        self.client.call("delta", {"since": "1970-01-01"})
        self.client.call("remember", {"fact": "A logged fact.", "provenance": "log test"})
        state = self.client.brain.state_dir
        events = [json.loads(line) for line in
                  (state / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        calls = [e for e in events if e["event"] == "tool_call"]
        names = [e["tool"] for e in calls]
        for tool in ("recall", "delta", "remember"):
            self.assertIn(tool, names)
        recall = next(e for e in calls if e["tool"] == "recall")
        self.assertEqual(recall["results"], 1)
        text = "".join(path.read_text(encoding="utf-8") for path in state.glob("*.jsonl"))
        self.assertNotIn("A logged fact", text, "a fact's text reached the event log")
        self.assertNotIn("tunnel", text.replace("tunnel-facts", ""), "a query reached the log")

    def test_a_remembered_fact_is_stamped_by_the_endpoint(self):
        _e, text = self.client.call("remember", {"fact": "Stamped fact.", "provenance": "stamp",
                                                 "via": "forged"})
        self.assertEqual(json.loads(text)["error"], "invalid_params")
        _e, text = self.client.call("remember", {"fact": "Stamped fact.", "provenance": "stamp"})
        saved = json.loads(text)
        note = (self.client.brain.data_root / saved["path"]).read_text(encoding="utf-8")
        self.assertRegex(note, r"(?m)^via: ")
        self.assertRegex(note, r"(?m)^source: local$")
        self.assertRegex(note, r'(?m)^provenance: "stamp"$')

    def test_the_write_verbs_fail_closed_in_maintenance_and_the_reads_keep_working(self):
        brain = self.client.brain
        brain.service.maintenance = "restore drill"
        self.addCleanup(setattr, brain.service, "maintenance", "")
        for verb, params in (("remember", {"fact": "x fact", "provenance": "p"}),
                             ("forget", {"id": "inbox-x"})):
            status, _body = brain.call(verb, params)
            self.assertEqual(status, 503, verb)
        status, body = brain.call("recall", {"query": "tunnel"})
        self.assertEqual(status, 200)
        self.assertFalse(body["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
