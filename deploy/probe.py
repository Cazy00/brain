#!/usr/bin/env python3
"""Ask a brain endpoint which tools it serves, through Cloudflare Access, with a
service token read from a file on this machine.

The outside view the stack otherwise lacks (remote-brain.md, "what watches
what"): a healthy tunnel in front of a dead origin answers 502, and only a
request that crosses the edge sees that. Prints tool NAMES only — never the
token — and exits 0 when the count equals --expect.

    python3 deploy/probe.py https://brain-read.<ZONE>/mcp \
        --token-file /etc/brain/service-tokens/brain-read-headless --expect 4
"""
import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

PAIRS = (("client_id", "client_secret"), ("cf_access_client_id", "cf_access_client_secret"))


def read_token_file(path: Path) -> tuple:
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("{"):
        data = json.loads(text)
        data = data.get("result", data)
        return str(data["client_id"]), str(data["client_secret"])
    pairs = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            pairs[key.strip().lower()] = value.strip().strip("'\"")
    for id_key, secret_key in PAIRS:
        if id_key in pairs and secret_key in pairs:
            return pairs[id_key], pairs[secret_key]
    raise ValueError("%s: no client id/secret pair; keys present: %s"
                     % (path, ", ".join(sorted(pairs)) or "none"))


def tool_names(raw: str) -> list:
    body = raw.strip()
    if not body.startswith("{"):
        body = next(line[5:].strip() for line in body.splitlines() if line.startswith("data:"))
    return sorted(tool["name"] for tool in json.loads(body)["result"]["tools"])


def tools_list(url: str, client_id: str, client_secret: str, timeout: float = 20) -> list:
    request = urllib.request.Request(
        url, method="POST",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "MCP-Protocol-Version": "2025-11-25",
                 # An automated-looking agent is refused by Cloudflare (error
                 # 1010) before Access is consulted: remote-brain.md, 1010 trap.
                 "User-Agent": "Mozilla/5.0 (compatible; brain-probe/1)",
                 "CF-Access-Client-Id": client_id,
                 "CF-Access-Client-Secret": client_secret})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return tool_names(response.read().decode("utf-8"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="probe")
    parser.add_argument("url")
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--expect", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        names = tools_list(args.url, *read_token_file(args.token_file))
    except urllib.error.HTTPError as exc:
        print("probe: HTTP %d from %s" % (exc.code, args.url))
        return 1
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        print("probe: %s" % exc)
        return 1
    print("%d tools: %s" % (len(names), ", ".join(names)))
    return 0 if len(names) == args.expect else 1


if __name__ == "__main__":
    sys.exit(main())
