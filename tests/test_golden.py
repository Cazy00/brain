#!/usr/bin/env python3
"""The three tools the memory verbs sit beside must not move.

`brain_search`, `brain_recent` and `brain_capture` stay served, unchanged,
until every consumer has moved to the verbs. This holds their text, byte for
byte, against what engine 0.4.5 printed for the same synthetic corpus
(tests/fixtures/golden/tools-0.4.5.json, recorded once from 0.4.5 itself).

Only two things are normalised, and only for the capture: the data root's
temporary path and the HH:MM:SS stamp the file name carries, because both
change on every run by design.

Re-recording is a deliberate act (BRAIN_GOLDEN_WRITE=1) and a reviewer should
refuse a diff to the file.

Run:  python3 -m unittest tests.test_golden -v
"""
import json
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import memverbs_support as support  # noqa: E402
from brainlib import mcp, mcpcore  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "tools-0.4.5.json"

# (name, tool, arguments). Chosen to cover every output shape of the three tools
# and of the two reads that share their markers.
READS = (
    ("search_title", "brain_search", {"query": "choose widget engine"}),
    ("search_alias", "brain_search", {"query": "engine pick"}),
    ("search_and_terms", "brain_search", {"query": "sprocket engine"}),
    ("search_any_term", "brain_search", {"query": "sprocket zebra"}),
    ("search_substring", "brain_search", {"query": "sprock"}),
    ("search_none", "brain_search", {"query": "zzzqqq"}),
    ("search_more_below_cap", "brain_search", {"query": "widget", "limit": 1}),
    ("search_all", "brain_search", {"query": "sprocket", "scope": "all", "limit": 20}),
    ("search_handoff_listing", "brain_search", {"query": "", "scope": "handoff"}),
    ("search_handoff_project", "brain_search", {"query": "gadget", "scope": "handoff"}),
    ("search_handoff_none", "brain_search", {"query": "nobody", "scope": "handoff"}),
    ("recent_wide", "brain_recent", {"days": 3650}),
    ("recent_default", "brain_recent", {}),
    ("read_by_id", "brain_read", {"id_or_path": "widget-glossary"}),
    ("read_archived", "brain_read", {"id_or_path": "2026-02-01-old-engine"}),
    ("read_inbox_path", "brain_read",
     {"id_or_path": "knowledge/inbox/2026-03-06-090000-widget-idea.md"}),
    ("links", "brain_links", {"id_or_path": "gadget-overview"}),
)
CAPTURE_TEXT = "Golden probe: the widget line gets a second sprocket supplier."


def normalise(text: str, root: Path) -> str:
    text = text.replace(str(root), "<data>")
    return re.sub(r"\d{4}-\d{2}-\d{2}-\d{6}-", "<date>-<hhmmss>-", text)


def record(tool, arguments, transport, root):
    result = mcpcore.call(tool, arguments, profile="capture", transport=transport)
    return {"text": normalise(result["content"][0]["text"], root),
            "isError": result["isError"]}


class GoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"            # `brain recent` prints local dates
        cls.corpus = support.Corpus()
        cls.addClassCleanup(cls.corpus.close)
        cls.expected = ({} if os.environ.get("BRAIN_GOLDEN_WRITE")
                        else json.loads(GOLDEN.read_text(encoding="utf-8")))

    @classmethod
    def tearDownClass(cls):
        if cls._tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls._tz

    def test_zz_record(self):
        """With BRAIN_GOLDEN_WRITE=1, records the golden file from this tree."""
        if not os.environ.get("BRAIN_GOLDEN_WRITE"):
            self.skipTest("recording only on request")
        out = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
        out.update({name: record(tool, args, "local", self.corpus.root)
                    for name, tool, args in READS})
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(out, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                          encoding="utf-8")

    def check(self, name, got):
        self.assertEqual(self.expected[name], got, f"{name} differs from engine 0.4.5")

    def test_reads_over_the_shared_tool_layer(self):
        if not self.expected:
            self.skipTest("recording")
        for name, tool, args in READS:
            with self.subTest(name):
                self.check(name, record(tool, args, "local", self.corpus.root))

    def test_reads_are_the_same_on_the_remote_transport(self):
        if not self.expected:
            self.skipTest("recording")
        for name, tool, args in READS:
            if args.get("limit", 0) > 20 or args.get("days", 0) > 365:
                continue                      # beyond the remote caps, by design
            with self.subTest(name):
                self.check(name, record(tool, args, "remote", self.corpus.root))

    def test_reads_are_the_same_over_stdio(self):
        if not self.expected:
            self.skipTest("recording")
        for name, tool, args in READS:
            with self.subTest(name):
                result = mcp.call_tool(tool, args)
                self.check(name, {"text": normalise(result["content"][0]["text"],
                                                    self.corpus.root),
                                  "isError": result["isError"]})


class GoldenCaptureTests(unittest.TestCase):
    """A capture changes the corpus, so each one gets its own."""

    def setUp(self):
        self.corpus = support.Corpus()
        self.addCleanup(self.corpus.close)
        self.expected = ({} if os.environ.get("BRAIN_GOLDEN_WRITE")
                         else json.loads(GOLDEN.read_text(encoding="utf-8")))

    def test_capture_text_over_stdio(self):
        if not self.expected:
            self.skipTest("recording")
        result = mcp.call_tool("brain_capture", {"text": CAPTURE_TEXT})
        self.assertEqual(self.expected["capture"],
                         {"text": normalise(result["content"][0]["text"], self.corpus.root),
                          "isError": result["isError"]})

    def test_capture_text_over_the_shared_tool_layer(self):
        if not self.expected:
            self.skipTest("recording")
        self.assertEqual(self.expected["capture"],
                         record("brain_capture", {"text": CAPTURE_TEXT}, "local",
                                self.corpus.root))

    def test_zz_record(self):
        if not os.environ.get("BRAIN_GOLDEN_WRITE"):
            self.skipTest("recording only on request")
        recorded = json.loads(GOLDEN.read_text(encoding="utf-8")) if GOLDEN.exists() else {}
        recorded["capture"] = record("brain_capture", {"text": CAPTURE_TEXT}, "local",
                                     self.corpus.root)
        GOLDEN.write_text(json.dumps(recorded, indent=1, ensure_ascii=False,
                                     sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
