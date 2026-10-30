"""What recall and brain_search answer for a fixed set of keyword queries, as one JSON
document. Engine-version neutral on purpose: it only uses the MCP dispatcher, so the same
file records the answers of 0.5.0 (tests/fixtures/golden/recall-0.5.0.json) and checks
them on a later engine. Re-recording is a deliberate act; refuse a diff to the fixture."""
import json

from brainlib import mcp

QUERIES = (("choose widget engine", {}), ("engine pick", {}), ("sprocket engine", {}),
           ("sprocket zebra", {}), ("sprock", {}), ("zzzqqq", {}), ("widget", {"limit": 1}),
           ("sprocket", {"scope": "all", "limit": 20}), ("", {"scope": "handoff"}),
           ("gadget", {"scope": "handoff"}), ("widget", {"limit": 3}), ("cog", {}),
           ("glossary", {"scope": "all"}))


def snapshot() -> dict:
    out = {}
    for query, extra in QUERIES:
        key = json.dumps([query, extra], sort_keys=True)
        args = dict(query=query, **extra)
        out[key] = {
            "brain_search": mcp.call_tool("brain_search", args)["content"][0]["text"],
            "recall_keyword": mcp.call_tool(
                "recall", dict(args, mode="keyword"))["content"][0]["text"],
            "recall_default": mcp.call_tool("recall", args)["content"][0]["text"],
        }
    return out
