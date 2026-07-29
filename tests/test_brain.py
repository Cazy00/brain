#!/usr/bin/env python3
"""Runtime tests for the brain toolbelt — stdlib unittest, no dependencies.

These cover BEHAVIOR, which lint cannot: that the bootstrap command actually
runs on a clean clone, that brain_read refuses to leave knowledge/, and that
one malformed MCP request cannot take the server down.

Anything that WRITES runs against a throwaway copy of the repo with HOME
redirected (see make_sandbox) — a test must never be able to damage the real
brain, and must never leave it in a state that blocks a commit.

Run:  python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "bin" / "brain"
MCP = ROOT / "bin" / "brain-mcp"
K = ROOT / "knowledge"

SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git", ".cache", "graphify-out", "node_modules", ".DS_Store", "__pycache__")


def temp_dir():
    """A sandbox whose teardown tolerates the detached post-commit job.

    post-commit reindexes and pushes in a fully detached subshell (it must not
    hold the caller's stdout pipe open, or every capture would block on the
    network). That job can still be writing into the sandbox's .cache/ as the
    test tears it down, which is a harmless race but a fatal rmtree."""
    try:
        return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    except TypeError:                      # Python 3.9 has no such argument
        return tempfile.TemporaryDirectory()


def cleanup_temp(tmp):
    try:
        tmp.cleanup()
    except OSError:
        pass


def run_brain(*args, repo=None):
    root = repo or ROOT
    return subprocess.run([sys.executable, str(root / "bin" / "brain"), *args],
                          cwd=root, capture_output=True, text=True, timeout=180)


def make_sandbox(tmp):
    """A throwaway clone-alike. The two machine-local generated files are
    removed so this matches what a REAL fresh clone looks like: they are
    gitignored, so they never arrive with the clone."""
    repo = Path(tmp) / "repo"
    shutil.copytree(ROOT, repo, symlinks=True, ignore=SANDBOX_IGNORE)
    for generated in (repo / ".mcp.json", repo / "setup/skills/brain/SKILL.md"):
        if generated.exists():
            generated.unlink()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    # Pre-set the hooks path so ensure_hooks() stays quiet: otherwise it prints
    # an install line ahead of --json output on the first command.
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=repo,
                   check=True, capture_output=True)
    # Track the files. A real brain is a repo with contents in its index, and
    # code that asks git what belongs to the project (`brain template` does,
    # so that untracked leftovers cannot be published) sees nothing at all in a
    # freshly `git init`-ed tree. Hooks are bypassed and the identity is passed
    # inline: this is scaffolding, and it must never touch the tester's config.
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=", "-c", "user.name=Sandbox",
         "-c", "user.email=sandbox@example.invalid",
         "commit", "-q", "-m", "sandbox"],
        cwd=repo, check=True, capture_output=True)
    return repo


def json_payload(stdout):
    """Parse --json output, tolerating any advisory line printed before it."""
    start = stdout.find("{")
    if start == -1:
        raise AssertionError(f"no JSON in output:\n{stdout}")
    return json.loads(stdout[start:])


class ReadContainmentTests(unittest.TestCase):
    """brain_read is reachable by any Claude session over MCP — it must never
    return a file from outside knowledge/, however the path is spelled.
    Read-only: these run against the real repo."""

    def assert_refused(self, request, forbidden_marker, repo=None):
        result = run_brain("read", request, repo=repo)
        self.assertEqual(result.returncode, 1,
                         f"{request!r} should be refused, got rc={result.returncode}")
        self.assertNotIn(forbidden_marker, result.stdout,
                         f"{request!r} leaked content from outside knowledge/")

    def test_parent_traversal_refused(self):
        # README.md sits in the repo root — inside the repo, outside knowledge/.
        self.assert_refused("knowledge/../README.md", "permanent second brain")

    def test_traversal_through_real_subfolder_refused(self):
        self.assert_refused("knowledge/decisions/../../README.md", "permanent second brain")

    def test_absolute_path_outside_knowledge_refused(self):
        self.assert_refused(str(ROOT / "README.md"), "permanent second brain")

    def test_escape_to_an_existing_file_outside_the_repo_refused(self):
        """Depth-independent: the target is a file this test creates, so the
        refusal can only come from containment — never from 'no such file'.
        (A fixed ../../../../etc/hosts probe is vacuous wherever the repo
        happens to sit at a different depth, e.g. on a CI runner.)"""
        with tempfile.TemporaryDirectory() as td:
            secret = Path(td) / "secret.md"
            secret.write_text("TOPSECRET-CANARY\n", encoding="utf-8")
            hops = os.path.relpath(secret, K)          # e.g. ../../../tmp/x/secret.md
            self.assertTrue(hops.startswith(".."), "probe must point outside knowledge/")
            self.assert_refused(hops, "TOPSECRET-CANARY")
            self.assert_refused(f"knowledge/{hops}", "TOPSECRET-CANARY")

    def test_legitimate_note_still_readable(self):
        """The fix must not break the thing the tool is for."""
        result = run_brain("read", "knowledge/index.md")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(result.stdout.strip(), "index.md read returned nothing")

    def test_nonexistent_note_reports_missing(self):
        result = run_brain("read", "knowledge/decisions/does-not-exist.md")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stdout)

    def test_non_markdown_file_inside_knowledge_refused(self):
        """`brain read` serves NOTES — .md only — and that is deliberate, not a
        side effect of the containment fix. topics.yaml is the one non-.md file
        under knowledge/, it exists, and it is still refused; this test exists so
        that widening the suffix rule is a decision someone makes on purpose.

        Nothing is lost: topics.yaml is a normal file any session can open. Only
        the MCP-reachable surface is kept at exactly one file type."""
        target = K / "topics.yaml"
        self.assertTrue(target.is_file(),
                        "probe is vacuous unless topics.yaml actually exists")
        result = run_brain("read", "knowledge/topics.yaml")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Controlled topic vocabulary", result.stdout,
                         "brain read served a non-.md file")


class SandboxContainmentTests(unittest.TestCase):
    """Containment cases that need to PLANT files inside knowledge/. Planting a
    symlink in the real tree would make bin/brain lint fail and block commits
    if the run were interrupted, so these use a sandbox copy."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.outside = Path(self.tmp.name) / "outside.md"
        self.outside.write_text("TOPSECRET-CANARY\n", encoding="utf-8")

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_symlink_inside_knowledge_cannot_escape(self):
        link = self.repo / "knowledge" / "reference" / "probe.md"
        link.symlink_to(self.outside)
        result = run_brain("read", "knowledge/reference/probe.md", repo=self.repo)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("TOPSECRET-CANARY", result.stdout)

    def test_symlinked_note_cannot_escape_via_its_id(self):
        """The id lookup reads the file off disk too — a symlink with valid
        frontmatter must not turn `brain read <id>` into an exfil route."""
        self.outside.write_text(
            "---\nid: probe-note\nkind: reference\ntitle: Probe\n"
            "topics: [brain]\ncreated: 2026-07-22\nstatus: current\n---\n\n"
            "TOPSECRET-CANARY\n", encoding="utf-8")
        (self.repo / "knowledge" / "reference" / "probe.md").symlink_to(self.outside)
        result = run_brain("read", "probe-note", repo=self.repo)
        self.assertNotIn("TOPSECRET-CANARY", result.stdout)
        self.assertEqual(result.returncode, 1, result.stdout)


class McpServerTests(unittest.TestCase):
    """One bad request must not kill a server that other sessions are sharing."""

    def rpc(self, *requests, raw=None):
        payload = raw if raw is not None else "".join(
            json.dumps(r) + "\n" for r in requests)
        proc = subprocess.run([sys.executable, str(MCP)], input=payload, cwd=ROOT,
                              capture_output=True, text=True, timeout=180)
        replies = {}
        for line in proc.stdout.splitlines():
            if line.strip():
                replies[json.loads(line).get("id")] = json.loads(line)
        return proc, replies

    @staticmethod
    def call(msg_id, tool, arguments):
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}

    PING = {"jsonrpc": "2.0", "id": 99, "method": "ping"}

    def assert_survived_with_tool_error(self, bad_call):
        proc, replies = self.rpc(bad_call, self.PING)
        self.assertIn(99, replies, "server died — the follow-up ping was never answered\n"
                                   f"stderr: {proc.stderr[-600:]}")
        self.assertIn(1, replies, "bad call got no reply at all")
        self.assertTrue(replies[1].get("result", {}).get("isError"),
                        f"expected a tool error, got {replies[1]}")
        self.assertNotIn("Traceback", proc.stderr)

    def test_missing_required_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {}))

    def test_missing_required_arg_read(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_read", {}))

    def test_missing_required_arg_capture(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_capture", {}))

    def test_wrong_type_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {"query": 123}))

    def test_null_required_arg(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", {"query": None}))

    def test_arguments_not_an_object(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_search", ["query"]))

    def test_oversized_arg(self):
        self.assert_survived_with_tool_error(
            self.call(1, "brain_search", {"query": "x" * 200_001}))

    def test_bad_enum_value(self):
        self.assert_survived_with_tool_error(
            self.call(1, "brain_search", {"query": "test", "scope": "everything"}))

    def test_unknown_tool(self):
        self.assert_survived_with_tool_error(self.call(1, "brain_nonexistent", {}))

    def test_non_string_tool_name(self):
        self.assert_survived_with_tool_error(self.call(1, {"weird": True}, {}))

    def test_deeply_nested_json_does_not_kill_the_server(self):
        """json.loads raises RecursionError — NOT JSONDecodeError — on deeply
        nested input. A guard that catches only JSONDecodeError lets one
        request take down every session sharing this server."""
        proc, replies = self.rpc(
            raw="[" * 100_000 + "]" * 100_000 + "\n" + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies,
                      f"server died on nested JSON\nstderr: {proc.stderr[-400:]}")

    def test_absurdly_long_line_does_not_kill_the_server(self):
        proc, replies = self.rpc(raw='"' + "x" * 20_000_000 + '"\n'
                                 + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies, "server died on an oversized line")

    def test_params_not_an_object(self):
        proc, replies = self.rpc(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "nope"}, self.PING)
        self.assertIn(99, replies, "server died on non-object params")

    def test_optional_null_is_treated_as_absent(self):
        """An explicit null for an optional arg is a normal thing for a model
        to emit; it must not fail the search."""
        _proc, replies = self.rpc(
            self.call(1, "brain_search", {"query": "brain", "scope": None, "limit": None}))
        self.assertFalse(replies[1]["result"]["isError"],
                         f"null optionals should be ignored, got {replies[1]}")

    def test_integer_sent_as_digit_string_is_accepted(self):
        _proc, replies = self.rpc(
            self.call(1, "brain_search", {"query": "brain", "limit": "3"}))
        self.assertFalse(replies[1]["result"]["isError"],
                         f'limit "3" should be coerced, got {replies[1]}')

    def test_tools_list_contract(self):
        _proc, replies = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in replies[1]["result"]["tools"]}
        self.assertEqual(names, {"brain_search", "brain_read", "brain_links",
                                 "brain_recent", "brain_capture"})

    def test_malformed_json_line_is_skipped(self):
        _proc, replies = self.rpc(raw="{not json at all\n[1,2,3]\n"
                                  + json.dumps(self.PING) + "\n")
        self.assertIn(99, replies, "server did not survive malformed input lines")

    def test_read_over_mcp_cannot_traverse(self):
        """The containment fix must hold through the MCP surface too."""
        _proc, replies = self.rpc(
            self.call(1, "brain_read", {"id_or_path": "knowledge/../README.md"}))
        self.assertNotIn("permanent second brain",
                         replies[1]["result"]["content"][0]["text"])


class UniversalClientTests(unittest.TestCase):
    """The qualification fixture for "any MCP-capable agent works".

    Every other client test in this file is Claude-shaped by habit. This one
    speaks the wire protocol the way a GENERIC client does — full handshake,
    nothing vendor-specific offered, nothing vendor-specific required — and
    then checks the two things the universal claim actually rests on:

      1. The server needs no vendor extension to be usable.
      2. The plain-text trust signals reach the client VERBATIM. They are the
         only thing standing between a superseded note and an answer given as
         true, and they travel as text — so any transport that reformats or
         summarises them re-opens exactly the hole they were added to close.

    Run against a SANDBOX: capture writes and commits, and no test may ever do
    that to the real brain."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def session(self, *messages, protocol="2025-11-25"):
        """One stdio session, opened the way the spec says a client opens one:
        initialize, then the initialized notification, then real work."""
        handshake = [
            {"jsonrpc": "2.0", "id": 0, "method": "initialize",
             "params": {"protocolVersion": protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "generic-mcp-client", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]
        payload = "".join(json.dumps(m) + "\n" for m in [*handshake, *messages])
        proc = subprocess.run([sys.executable, str(self.repo / "bin" / "brain-mcp")],
                              input=payload, cwd=self.repo, capture_output=True,
                              text=True, timeout=180)
        replies = {}
        for line in proc.stdout.splitlines():
            if line.strip():
                msg = json.loads(line)
                replies[msg.get("id")] = msg
        return proc, replies

    @staticmethod
    def call(msg_id, tool, **arguments):
        return {"jsonrpc": "2.0", "id": msg_id, "method": "tools/call",
                "params": {"name": tool, "arguments": arguments}}

    @staticmethod
    def text(reply):
        return "".join(part.get("text", "") for part in reply["result"]["content"])

    def test_a_generic_client_completes_the_handshake(self):
        proc, replies = self.session()
        self.assertIn(0, replies, f"no initialize reply\nstderr: {proc.stderr[-500:]}")
        result = replies[0]["result"]
        self.assertEqual(result["protocolVersion"], "2025-11-25",
                         "the server did not echo the client's protocol version")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "brain")
        self.assertEqual(proc.returncode, 0)

    def test_the_initialized_notification_is_ignored_not_answered(self):
        """A notification has no id. Replying to one is a protocol violation
        that strict clients treat as a fatal error."""
        _proc, replies = self.session()
        self.assertEqual(set(replies), {0},
                         "the server answered a notification, which has no id")

    def test_nothing_vendor_specific_is_offered_or_required(self):
        """If a tool schema named a vendor, or a required field only one client
        sends, 'any MCP client' would be false however well it read."""
        _proc, replies = self.session(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = replies[1]["result"]["tools"]
        blob = json.dumps(tools).lower()
        for vendor in ("claude", "anthropic", "openai", "gemini", "cursor", "copilot"):
            self.assertNotIn(vendor, blob, f"the tool surface names {vendor}")
        for tool in tools:
            schema = tool.get("inputSchema", {})
            self.assertEqual(schema.get("type"), "object")
            for field in schema.get("required", []):
                self.assertIn(field, schema.get("properties", {}),
                              f"{tool['name']} requires an undeclared field {field}")

    def test_capture_search_read_round_trips_over_the_wire(self):
        """The whole tool layer, in one session, with no CLI in sight."""
        token = "kryptonite" + "warbler"
        _proc, replies = self.session(
            self.call(1, "brain_capture", text=f"A durable fact about {token}."),
            self.call(2, "brain_search", query=token, scope="all"),
        )
        self.assertFalse(replies[1]["result"].get("isError"), self.text(replies[1]))
        found = self.text(replies[2])
        self.assertIn(token, found, "a capture made over MCP was not findable over MCP")
        self.assertIn("knowledge/inbox/", found, "the hit was not cited by path")

    def test_a_provisional_hit_is_labelled_provisional_over_the_wire(self):
        """`--scope all` reaches unconsolidated captures. Without the tag the
        client cannot tell a draft thought from settled knowledge."""
        token = "unvetted" + "marmoset"
        _proc, replies = self.session(
            self.call(1, "brain_capture", text=f"Raw thought about {token}."),
            self.call(2, "brain_search", query=token, scope="all"),
        )
        self.assertIn("[provisional — unconsolidated]", self.text(replies[2]),
                      "the provisional marker did not survive the MCP transport")

    def test_reading_an_archived_note_carries_its_warning_over_the_wire(self):
        """The single most dangerous read in the system: history returned as if
        it were current."""
        archived = self.repo / "knowledge" / "archive" / "reference" / "old-fact.md"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_text(
            "---\nid: old-fact\nkind: reference\ntitle: Old fact\ntopics: [brain]\n"
            "aliases: [old fact]\ncreated: 2026-01-01\nstatus: superseded\n"
            "superseded_by: new-fact\n---\n\nThe rate limit was 100 req/min.\n",
            encoding="utf-8")
        _proc, replies = self.session(self.call(1, "brain_read", id_or_path="old-fact"))
        body = self.text(replies[1])
        self.assertIn("ARCHIVED/superseded", body,
                      "an archived note came back over MCP with no warning attached")
        self.assertIn("new-fact", body, "the successor was not named")

    def test_a_search_states_which_match_mode_produced_it(self):
        _proc, replies = self.session(self.call(1, "brain_search", query="brain"))
        self.assertIn("[match mode:", self.text(replies[1]),
                      "the match-mode line did not survive the MCP transport")

    def test_an_older_protocol_version_still_works(self):
        """Clients pin different versions; a brain that only answers the newest
        is not universal."""
        _proc, replies = self.session(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, protocol="2025-06-18")
        self.assertEqual(replies[0]["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(len(replies[1]["result"]["tools"]), 5)


class InitTests(unittest.TestCase):
    """Bootstrap has to work on a machine that has never run this repo before —
    that is the whole disaster-recovery story, so it gets an isolated clone,
    an isolated HOME, and a stubbed claude CLI (a test must never be able to
    rewrite the developer's own MCP registration)."""

    def setUp(self):
        self.tmp = temp_dir()
        tmp = Path(self.tmp.name)
        self.repo = make_sandbox(tmp)
        self.home = tmp / "home"
        self.stub_bin = tmp / "stubbin"
        self.home.mkdir()
        self.stub_bin.mkdir()
        self.calls = self.stub_bin / "calls.log"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def stub_claude(self, scope=None, command=None, add_exit=0, add_message="",
                    get_output=None):
        """Fake the `claude` CLI as a CLI — not as whatever subcommand cmd_init
        happens to call today.

        `mcp list` and `mcp get` BOTH answer, consistently, in the real CLI's
        formats. That matters: a stub that only knows the subcommand the current
        code calls silently passes against an implementation that asks the other
        one, which is exactly how a regression test turns vacuous.

        scope=None means nothing is registered anywhere. Every invocation is
        logged so a test can assert what init actually ran, not what it printed.
        """
        command = command or self.server_path()
        if scope == "user":
            get_body = ("brain:\n"
                        "  Scope: User config (available in all your projects)\n"
                        "  Status: ✔ Connected\n"
                        "  Type: stdio\n"
                        f"  Command: {command}\n"
                        "  Args:\n"
                        "  Environment:\n")
            list_body = f"  brain: {command}  - ✔ Connected"
        elif scope == "project":
            # What the CLI reports when the ONLY entry is the .mcp.json that
            # init's own step 2 wrote moments earlier.
            get_body = ("brain:\n"
                        "  Scope: Project config (shared via .mcp.json)\n"
                        "  Status: ⏸ Pending approval (run `claude` to approve)\n")
            list_body = f"  brain: {command}  - ⏸ Pending approval (run `claude` to approve)"
        else:
            get_body = 'No MCP server named "brain". Run `claude mcp add` to add one.'
            list_body = ""
        if get_output is not None:
            get_body = get_output
        stub = self.stub_bin / "claude"
        stub.write_text(
            "#!/bin/sh\n"
            f'echo "$@" >> "{self.calls}"\n'
            'if [ "$2" = "list" ]; then\n'
            f"cat <<'ENDLIST'\n{list_body}\nENDLIST\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$2" = "get" ]; then\n'
            f"cat <<'ENDGET'\n{get_body}\nENDGET\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$2" = "add" ]; then\n'
            f"  echo '{add_message}' >&2\n"
            f"  exit {add_exit}\n"
            "fi\n"
            "exit 0\n", encoding="utf-8")
        stub.chmod(0o755)

    def claude_calls(self):
        return self.calls.read_text(encoding="utf-8") if self.calls.exists() else ""

    def env_with(self, claude=True):
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        path = f"{self.stub_bin}:{git_dir}:/usr/bin:/bin" if claude \
            else f"{git_dir}:/usr/bin:/bin"
        if not claude:
            self.assertIsNone(shutil.which("claude", path=path),
                              "test setup: claude still reachable on PATH")
        return {**os.environ, "HOME": str(self.home), "PATH": path,
                "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
                "XDG_CONFIG_HOME": str(self.home / ".config")}

    def init(self, env=None):
        return subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "init"],
                              cwd=self.repo, capture_output=True, text=True,
                              env=env or self.env_with(), timeout=180)

    def server_path(self):
        return str(self.repo.resolve() / "bin" / "brain-mcp")

    def test_init_wires_a_clean_clone_and_is_idempotent(self):
        self.stub_claude()                       # nothing registered yet
        first = self.init()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        # /var is a symlink to /private/var on macOS, so compare resolved.
        repo = self.repo.resolve()
        mcp = json.loads((self.repo / ".mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp["mcpServers"]["brain"]["command"], self.server_path())

        skill = (self.repo / "setup/skills/brain/SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{REPO}}", skill, "template placeholder left unrendered")
        self.assertIn(str(repo), skill, "skill not pointed at this clone")

        link = self.home / ".claude" / "skills" / "brain"
        self.assertTrue(link.is_symlink(), "/brain skill was not linked into ~/.claude/skills")
        self.assertEqual(link.resolve(), (self.repo / "setup/skills/brain").resolve())

        hooks = subprocess.run(["git", "config", "core.hooksPath"], cwd=self.repo,
                               capture_output=True, text=True)
        self.assertEqual(hooks.stdout.strip(), ".githooks")

        # Idempotent: now the stub reports it as registered at THIS path.
        self.stub_claude(scope="user")
        second = self.init()
        self.assertEqual(second.returncode, 0,
                         "re-running init must be safe\n" + second.stdout + second.stderr)

    def test_stale_registration_is_reported_not_called_success(self):
        """`claude mcp add` refuses with 'already exists' whether or not the
        registered path is this clone, so trusting that message reports success
        while every session outside the repo is wired to a dead path."""
        self.stub_claude(
            scope="user", command="/somewhere/else/bin/brain-mcp",
            add_exit=1, add_message="MCP server brain already exists in user config")
        result = self.init()
        self.assertEqual(result.returncode, 1,
                         "a stale registration must not be reported as success\n"
                         + result.stdout)
        self.assertIn("claude mcp remove brain -s user", result.stdout,
                      "the repair command must be printed")

    def test_matching_registration_is_success(self):
        self.stub_claude(scope="user")
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("mcp add", self.claude_calls(),
                         "init re-registered a server that was already correct")

    def test_project_scoped_entry_does_not_count_as_registered(self):
        """Step 2 writes a project-scoped .mcp.json, so asking `claude mcp list`
        shows brain as present on a machine that has NEVER registered it at user
        scope — init's own side effect answering init's own question. Believing
        it leaves brain tools working inside this repo and nowhere else, which
        is the single thing step 5 exists to prevent."""
        self.stub_claude(scope="project")
        result = self.init()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("mcp add --scope user brain", self.claude_calls(),
                      "a project-scoped entry was mistaken for a user-scope one, so the "
                      "user-scope registration never happened")

    def test_unverifiable_user_scope_entry_is_not_called_success(self):
        """A user-scope entry whose command cannot be read cannot be confirmed
        to point at this clone. Unverifiable is not the same as fine."""
        self.stub_claude(scope="user", get_output=(
            "brain:\n"
            "  Scope: User config (available in all your projects)\n"
            "  Status: ✔ Connected\n"))
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("claude mcp remove brain -s user", result.stdout)

    def test_init_reports_failure_when_template_missing(self):
        """A silent partial bootstrap is what broke this command before —
        a missing template must surface as a nonzero exit, not a cheery one."""
        self.stub_claude()
        (self.repo / "setup/skills/brain/SKILL.md.template").unlink()
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("SKILL.md.template", result.stdout)

    def test_no_dangling_skill_link_when_there_is_no_skill(self):
        """Linking ~/.claude/skills/brain at a directory with no SKILL.md gives
        Claude a broken skill — worse than no link."""
        self.stub_claude()
        (self.repo / "setup/skills/brain/SKILL.md.template").unlink()
        self.init()
        self.assertFalse((self.home / ".claude" / "skills" / "brain").exists(),
                         "init planted a link to a skill that does not exist")

    def test_real_registration_failure_is_reported(self):
        self.stub_claude(add_exit=1, add_message="connection refused")
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("connection refused", result.stdout)

    def test_init_defers_when_claude_cli_absent(self):
        """No Claude CLI is not a failure — init prints the exact command."""
        result = self.init(env=self.env_with(claude=False))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("claude mcp add --scope user brain", result.stdout)
        self.assertTrue((self.repo / ".mcp.json").is_file(),
                        "a missing claude CLI must not block the rest of the wiring")

    def test_init_preserves_other_mcp_servers(self):
        """.mcp.json is gitignored now, so clobbering someone else's entry here
        destroys it with no way to get it back."""
        self.stub_claude()
        (self.repo / ".mcp.json").write_text(json.dumps({
            "mcpServers": {"other": {"command": "/usr/local/bin/other-mcp", "args": []}}
        }), encoding="utf-8")
        self.init()
        servers = json.loads((self.repo / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
        self.assertIn("other", servers, "init destroyed an unrelated MCP server")
        self.assertEqual(servers["other"]["command"], "/usr/local/bin/other-mcp")
        self.assertEqual(servers["brain"]["command"], self.server_path())

    def test_init_does_not_clobber_a_real_skills_directory(self):
        """If ~/.claude/skills/brain is a real directory, init must refuse to
        delete it rather than silently destroying whatever is there."""
        self.stub_claude()
        real_dir = self.home / ".claude" / "skills" / "brain"
        real_dir.mkdir(parents=True)
        (real_dir / "keepme.txt").write_text("precious", encoding="utf-8")
        result = self.init()
        # Wording owned by osbackend.link_dir() (task 4: skill linking now
        # goes through the shared symlink/junction/copy backend) rather than
        # cmd_init itself — "is not a symlink" was the old inline message;
        # the backend's is more general (a copy can be legitimately ours too,
        # via the marker file) but makes the same promise: say why, don't
        # just fail silently.
        self.assertIn("was not created by brain", result.stdout,
                      "init must say why it skipped, not just fail")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertTrue((real_dir / "keepme.txt").is_file(), "init destroyed existing content")

    def test_init_reports_failure_outside_a_git_repo(self):
        """Claiming the commit gate is installed when git rejected the config
        is exactly the kind of false success this command used to give."""
        self.stub_claude()
        shutil.rmtree(self.repo / ".git")
        result = self.init()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("core.hooksPath", result.stdout)

    def test_doctor_runs_on_a_fresh_clone(self):
        """doctor must produce a report, not a traceback, on a fresh clone."""
        self.stub_claude()
        self.assertEqual(self.init().returncode, 0)
        result = subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "doctor"],
                                cwd=self.repo, capture_output=True, text=True,
                                env=self.env_with(), timeout=300)
        self.assertIn("brain doctor", result.stdout)
        self.assertNotIn("Traceback", result.stderr)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_capture_writes_the_note_and_prints_its_path(self):
        result = run_brain("capture", "test capture probe", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # ensure_hooks() may print first on a fresh repo — the note path is
        # the last line capture prints without --commit.
        written = Path(result.stdout.strip().splitlines()[-1])
        self.assertTrue(written.is_file(), f"no file at {written}\n{result.stdout}")
        self.assertIn("test capture probe", written.read_text(encoding="utf-8"))

    def test_failed_commit_is_reported_as_failure(self):
        """--commit was asked for; if git refused, the note is NOT backed up.
        Returning 0 made the MCP layer report isError:false and every caller
        believe the note was safely in git."""
        hook = self.repo / ".githooks" / "pre-commit"
        hook.write_text("#!/bin/sh\necho 'gate says no' >&2\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
        result = run_brain("capture", "probe that cannot commit", "--commit", repo=self.repo)
        self.assertEqual(result.returncode, 1,
                         "a refused commit must not be reported as success\n" + result.stdout)
        self.assertIn("NOT COMMITTED", result.stdout)
        # ...and the note must still be on disk, so nobody re-captures it.
        notes = list((self.repo / "knowledge" / "inbox").glob("*probe-that-cannot-commit*.md"))
        self.assertTrue(notes, "the capture was lost entirely")


class SearchRankingTests(unittest.TestCase):
    """FTS5 returns matches in an arbitrary order without ORDER BY. Ranking in
    Python after `LIMIT 100` therefore threw away the best hit as soon as more
    than 100 notes matched."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write_note(self, name, title, body):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {title}\n"
            f"topics: [brain]\ncreated: 2026-07-22\nstatus: current\n---\n\n{body}\n",
            encoding="utf-8")

    def test_best_hit_survives_more_than_100_matches(self):
        # 150 notes mention the term only in the body...
        for i in range(150):
            self.write_note(f"filler-{i:03d}", f"Filler {i}", "passing mention of wumpus here")
        # ...one has it in the title, which is weighted 8x. It must rank first.
        self.write_note("the-wumpus-note", "Wumpus", "wumpus wumpus wumpus")
        result = run_brain("search", "wumpus", "--limit", "3", "--json", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hits = json_payload(result.stdout)["hits"]
        self.assertTrue(hits, "no hits at all")
        self.assertEqual(hits[0]["id"], "the-wumpus-note",
                         f"best hit lost among 151 matches; got {[h['id'] for h in hits]}")

    def test_deleting_a_note_removes_it_from_search(self):
        """Deletion changes no surviving file's mtime, so an mtime-only
        freshness check leaves the note answering searches forever."""
        self.write_note("doomed-note", "Doomed", "zzyzx unique marker")
        first = run_brain("search", "zzyzx", "--json", repo=self.repo)
        self.assertTrue(json_payload(first.stdout)["hits"], "setup: note was not indexed")
        (self.ref / "doomed-note.md").unlink()
        after = run_brain("search", "zzyzx", "--json", repo=self.repo)
        self.assertEqual(json_payload(after.stdout)["hits"], [],
                         "deleted note is still in the index")

    def test_query_starting_with_dashes_is_not_parsed_as_a_flag(self):
        """Options come before `--`; everything after it is query text."""
        self.write_note("flag-note", "Flag discussion", "a note about --zzyzx-flag handling")
        result = run_brain("search", "--json", "--", "--zzyzx-flag", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hits = json_payload(result.stdout)["hits"]
        self.assertTrue(hits, "a query beginning with -- was swallowed as a flag")
        self.assertEqual(hits[0]["id"], "flag-note")

    def test_concurrent_first_searches_all_succeed(self):
        """Every Claude session runs its own MCP server and each rebuilds on
        its first stale search — they used to race on one shared temp file."""
        import concurrent.futures
        self.write_note("race-note", "Race", "concurrency marker term")
        index = self.repo / ".cache" / "index.db"
        if index.exists():
            index.unlink()                      # force all 20 to find it stale
        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            runs = [pool.submit(run_brain, "search", "concurrency", "--json", repo=self.repo)
                    for _ in range(20)]
            results = [r.result() for r in runs]
        for r in results:
            self.assertEqual(r.returncode, 0, f"concurrent search failed:\n{r.stderr[-400:]}")
        payloads = {json.dumps(json_payload(r.stdout)["hits"]) for r in results}
        self.assertEqual(len(payloads), 1, "concurrent searches disagreed on results")


class ConsolidateTests(unittest.TestCase):
    """Consolidation ran its commit and push from a `finally:`, so a model that
    failed still got its work committed and pushed, and the closing message
    said it landed regardless."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.stub_bin = Path(self.tmp.name) / "stubbin"
        self.stub_bin.mkdir()
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def stub_claude(self, body):
        stub = self.stub_bin / "claude"
        stub.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        stub.chmod(0o755)

    def consolidate(self):
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        env = {**os.environ, "PATH": f"{self.stub_bin}:{git_dir}:/usr/bin:/bin"}
        return subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "consolidate"],
                              cwd=self.repo, capture_output=True, text=True,
                              env=env, timeout=300)

    def commits_on_branch(self):
        out = subprocess.run(["git", "log", "--oneline", "--all"], cwd=self.repo,
                             capture_output=True, text=True)
        return [l for l in out.stdout.splitlines() if "consolidate:" in l]

    def test_model_failure_does_not_commit_or_claim_success(self):
        # The model "runs", writes a change, then fails.
        self.stub_claude('echo "partial work" >> knowledge/inbox/half-done.md\nexit 1')
        result = self.consolidate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("DID NOT LAND", result.stdout)
        self.assertEqual(self.commits_on_branch(), [],
                         "a failed consolidation was committed anyway")

    def test_model_timeout_or_crash_is_reported(self):
        self.stub_claude("exit 3")
        result = self.consolidate()
        self.assertEqual(result.returncode, 1)
        self.assertIn("claude exited 3", result.stdout)

    def test_clean_run_with_no_changes_reports_no_changes(self):
        self.stub_claude("exit 0")
        result = self.consolidate()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("no changes", result.stdout)


class SessionDigestTests(unittest.TestCase):
    """Session mining reads transcripts from OUTSIDE the repo and hands them to
    the consolidation model, so three things have to hold: only the user's own
    words are extracted (mining my own output back in would launder my
    speculation into his recorded fact), the window follows when he actually
    talked rather than when a file was touched, and the digest never becomes
    committable."""

    def setUp(self):
        self.tmp = temp_dir()
        tmp = Path(self.tmp.name)
        self.repo = make_sandbox(tmp)
        self.home = tmp / "home"
        self.projects = self.home / ".claude" / "projects" / "-home-example-proj"
        self.projects.mkdir(parents=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def iso(self, days_ago):
        stamp = datetime.now(timezone.utc) - timedelta(days=days_ago)
        return stamp.strftime("%Y-%m-%dT%H:%M:%S")

    def user_turn(self, text, ts, **extra):
        return {"type": "user", "timestamp": ts, "cwd": "/home/example/proj",
                "message": {"role": "user", "content": text}, **extra}

    def write_transcript(self, name, records):
        path = self.projects / name
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return path

    def sessions(self, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "sessions", *args],
            cwd=self.repo, capture_output=True, text=True, timeout=180,
            env={**os.environ, "HOME": str(self.home)})

    def test_extracts_only_the_users_own_prompts(self):
        self.write_transcript("a.jsonl", [
            self.user_turn("KEEPME dropped Redis because the ops cost was real",
                           self.iso(1)),
            self.user_turn("SIDECHAINLEAK", self.iso(1), isSidechain=True),
            self.user_turn("<local-command-stdout>CMDLEAK</local-command-stdout>",
                           self.iso(1)),
            {"type": "user", "timestamp": self.iso(1),
             "message": {"role": "user",
                         "content": [{"type": "tool_result", "content": "TOOLLEAK"}]}},
            {"type": "assistant", "timestamp": self.iso(1),
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "ASSISTANTLEAK"}]}},
            self.user_turn("kept <system-reminder>REMINDERLEAK</system-reminder> text",
                           self.iso(1)),
        ])
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("KEEPME dropped Redis", out.stdout)
        for leak in ("SIDECHAINLEAK", "CMDLEAK", "TOOLLEAK",
                     "ASSISTANTLEAK", "REMINDERLEAK"):
            self.assertNotIn(leak, out.stdout, f"{leak} leaked into the digest")

    def test_window_follows_when_he_talked_not_file_mtime(self):
        # Both files are written now, so both have a fresh mtime. Only the
        # recent CONVERSATION may be mined: a months-old session in a file that
        # was merely reindexed or copied would otherwise resurface every week.
        self.write_transcript("old.jsonl", [self.user_turn("ANCIENTTHOUGHT", self.iso(90))])
        self.write_transcript("new.jsonl", [self.user_turn("RECENTTHOUGHT", self.iso(1))])
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("RECENTTHOUGHT", out.stdout)
        self.assertNotIn("ANCIENTTHOUGHT", out.stdout,
                         "an old session was mined because its file was touched recently")

    def test_embedded_headings_cannot_forge_a_session_boundary(self):
        self.write_transcript("a.jsonl", [
            self.user_turn("## 2026-01-01 — /fake/dir\nprose after a heading", self.iso(1))])
        out = self.sessions("--stdout")
        self.assertEqual(out.stdout.count("\n## "), 1,
                         "a prompt's own heading was counted as a session header")

    def test_digest_is_written_to_cache_and_never_committable(self):
        self.write_transcript("a.jsonl", [self.user_turn("PRIVATEMUSING", self.iso(1))])
        self.assertEqual(self.sessions().returncode, 0)
        digest = self.repo / ".cache" / "session-digest.md"
        self.assertTrue(digest.exists(), "no digest was written")
        self.assertIn("PRIVATEMUSING", digest.read_text(encoding="utf-8"))
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo,
                                capture_output=True, text=True).stdout
        self.assertNotIn("session-digest", status,
                         "the digest is git-visible — mined session text could be committed")

    def test_missing_transcript_root_is_not_an_error(self):
        shutil.rmtree(self.home / ".claude")
        out = self.sessions("--stdout")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("nothing to mine", out.stdout)


class SupersedeTests(unittest.TestCase):
    """The two note-creating commands had no coverage. supersede in particular
    could overwrite an existing note (blank template → data loss) and rewrite
    append-only archive history."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write_ref(self, name, title, body, **fm):
        extra = "".join(f"{k}: {v}\n" for k, v in fm.items())
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {title}\n"
            f"topics: [brain]\naliases: [{name}]\ncreated: 2026-07-01\n"
            f"status: current\nreview_by: null\n{extra}---\n\n{body}\n",
            encoding="utf-8")

    def seed(self, name="seed-note"):
        """A note this test owns, to supersede. Tests must never depend on a
        note that happens to exist in the repo — the published template ships
        an EMPTY knowledge/, and a test keyed to the author's notes either
        fails there or, worse, passes vacuously."""
        self.write_ref(name, "Seed note", "The original claim.")
        return name

    def test_supersede_refuses_to_overwrite_an_existing_file(self):
        """A note whose filename != its id (legal) must not be clobbered when a
        supersede title happens to slugify onto its path."""
        # file api-limits.md holds id vendor-api-limits and a real fact
        (self.ref / "api-limits.md").write_text(
            "---\nid: vendor-api-limits\nkind: reference\ntitle: Vendor API limits\n"
            "topics: [brain]\naliases: [rate, quota]\ncreated: 2026-07-01\n"
            "status: current\nreview_by: null\n---\n\nThe vendor rate limit is 500 req/min.\n",
            encoding="utf-8")
        # supersede a note of our own with a title slugifying to "api-limits"
        seed = self.seed()
        result = run_brain("supersede", seed, "API limits", repo=self.repo)
        self.assertEqual(result.returncode, 1, "supersede overwrote an existing note\n" + result.stdout)
        self.assertIn("500 req/min", (self.ref / "api-limits.md").read_text(encoding="utf-8"),
                      "the pre-existing note's body was destroyed")

    def finish_supersede(self, old, new_slug):
        """Do what AGENTS.md tells the author to do after `brain supersede`:
        write the successor's body and replace the banner's placeholder.

        `supersede` deliberately renders a SKELETON, so the tree is not meant to
        be committable until this happens — lint blocks the half-done state on
        purpose (see the two guard tests below)."""
        successor = self.repo / f"knowledge/reference/{new_slug}.md"
        successor.write_text(
            successor.read_text(encoding="utf-8").replace(
                "## What\n", "## What\n\nThe revised claim.\n"),
            encoding="utf-8")
        archived = self.repo / f"knowledge/archive/reference/{old}.md"
        archived.write_text(
            archived.read_text(encoding="utf-8").replace(
                "<one-line reason>", "the original claim stopped being true"),
            encoding="utf-8")

    def test_supersede_produces_a_lint_clean_tree(self):
        """The COMPLETED happy path must leave the repo committable."""
        seed = self.seed()
        result = run_brain("supersede", seed, "Seed note v2", repo=self.repo)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.finish_supersede(seed, "seed-note-v2")
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 0, "supersede left the tree un-lintable:\n" + lint.stdout)
        # old note left canonical folder; successor is current and points back
        self.assertFalse((self.repo / f"knowledge/reference/{seed}.md").exists())
        self.assertTrue((self.repo / f"knowledge/archive/reference/{seed}.md").exists())

    def test_unreplaced_banner_placeholder_blocks_the_commit(self):
        """The archived note must record WHY it was replaced. Left unfilled, it
        says only THAT it was — and the reason is unrecoverable once the session
        that knew it ends, so this is an error, not a warning."""
        seed = self.seed()
        run_brain("supersede", seed, "Seed note v2", repo=self.repo)
        # fill the successor only; leave the banner placeholder in place
        successor = self.repo / "knowledge/reference/seed-note-v2.md"
        successor.write_text(
            successor.read_text(encoding="utf-8").replace(
                "## What\n", "## What\n\nThe revised claim.\n"), encoding="utf-8")
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 1, "an unfilled supersede reason committed clean")
        self.assertIn("one-line reason", lint.stdout)

    def test_empty_successor_body_blocks_the_commit(self):
        """The predecessor leaves the default search scope in the same commit,
        so shipping an unwritten successor destroys the knowledge outright —
        the one path by which a decision's wording disappears silently."""
        seed = self.seed()
        run_brain("supersede", seed, "Seed note v2", repo=self.repo)
        # replace the banner placeholder but leave the successor a bare skeleton
        archived = self.repo / f"knowledge/archive/reference/{seed}.md"
        archived.write_text(
            archived.read_text(encoding="utf-8").replace(
                "<one-line reason>", "superseded for a good reason"), encoding="utf-8")
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 1, "an empty successor committed clean")
        self.assertIn("still the empty template", lint.stdout)


class LintIntegrityTests(unittest.TestCase):
    """Knowledge-integrity checks that keep superseded/contradictory content
    from being served as one current fact."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write(self, name, extra="", body="ok"):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {name}\ntopics: [brain]\n"
            f"aliases: [{name}]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"{extra}---\n\n{body}\n", encoding="utf-8")

    def test_superseded_by_on_a_current_note_is_rejected(self):
        self.write("zombie", extra="superseded_by: something\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("must not carry superseded_by", out.stdout)

    def test_merge_conflict_markers_are_rejected(self):
        self.write("conflicted",
                   body="## What\n<<<<<<< HEAD\n500 req/min\n=======\n1000 req/min\n>>>>>>> other\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("merge-conflict", out.stdout)

    def test_setext_heading_is_not_flagged_as_a_conflict(self):
        """A markdown H1 underline (a run of '=') must not false-positive."""
        self.write("heading", body="Real Heading\n=======\nbody text here\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0, "setext underline false-flagged:\n" + out.stdout)

    def test_forked_supersede_is_rejected(self):
        arch = self.repo / "knowledge/archive/reference"
        arch.mkdir(parents=True, exist_ok=True)
        (arch / "old.md").write_text(
            "---\nid: old-fact\nkind: reference\ntitle: Old\ntopics: [brain]\n"
            "aliases: [old]\ncreated: 2026-06-01\nstatus: superseded\n"
            "superseded_by: new-a\nreview_by: null\n---\n\n> SUPERSEDED 2026-07-01 by [[new-a]] — x\n",
            encoding="utf-8")
        for n in ("a", "b"):
            self.write(f"new-{n}", extra="supersedes: old-fact\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1, "two notes superseding one target passed lint")
        self.assertIn("claim to supersede", out.stdout)


class CaptureConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def _committed(self):
        log = subprocess.run(["git", "log", "--oneline"], cwd=self.repo,
                             capture_output=True, text=True).stdout
        return log.count("capture:")

    def _uncommitted(self):
        st = subprocess.run(["git", "status", "--porcelain", "knowledge/inbox"],
                            cwd=self.repo, capture_output=True, text=True).stdout
        return len([l for l in st.splitlines() if l.strip()])

    def test_eight_concurrent_captures_all_commit_and_none_are_lost(self):
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            runs = [pool.submit(run_brain, "capture", f"concurrent unique-token-{i}",
                                "--commit", repo=self.repo) for i in range(8)]
            results = [r.result() for r in runs]
        for r in results:
            self.assertEqual(r.returncode, 0, "a concurrent capture failed:\n" + r.stdout)
        self.assertEqual(self._committed(), 8, "not every capture committed")
        self.assertEqual(self._uncommitted(), 0, "a capture was left uncommitted (unbacked)")

    def test_capture_does_not_sweep_unrelated_staged_work(self):
        wip = self.repo / "knowledge/reference/wip.md"
        wip.write_text("---\nid: wip\nkind: reference\ntitle: wip\ntopics: [brain]\n"
                       "aliases: [wip]\ncreated: 2026-07-24\nstatus: current\nreview_by: null\n"
                       "---\n\nunrelated work in progress\n", encoding="utf-8")
        subprocess.run(["git", "add", str(wip)], cwd=self.repo, check=True, capture_output=True)
        run_brain("capture", "innocent capture", "--commit", repo=self.repo)
        show = subprocess.run(["git", "show", "--stat", "--oneline", "HEAD"],
                             cwd=self.repo, capture_output=True, text=True).stdout
        self.assertNotIn("wip.md", show, "capture swept unrelated staged WIP into its commit")

    def test_capture_refuses_to_commit_on_a_consolidate_branch(self):
        subprocess.run(["git", "checkout", "-q", "-b", "consolidate/2026-07-24"],
                       cwd=self.repo, check=True, capture_output=True)
        out = run_brain("capture", "would strand on branch", "--commit", repo=self.repo)
        self.assertEqual(out.returncode, 1, "capture committed onto the consolidate branch")
        self.assertIn("consolidation", out.stdout)
        notes = list((self.repo / "knowledge/inbox").glob("*would-strand*.md"))
        self.assertTrue(notes, "the note was lost rather than kept on disk")


class RepoLockCoverageTests(unittest.TestCase):
    """Every command that mutates the git index must take repo_lock.

    Asserted by holding the lock from OUTSIDE and watching the command block —
    a race-free way to prove which code paths are actually guarded. Only capture
    was; supersede staged a rename and consolidate switched branches, staged and
    committed, all unguarded, so a capture firing in the same instant could be
    swept into someone else's commit or land on a consolidation branch."""

    HELD = 1.5   # long enough that "did not block" is unambiguous, short enough
                 # that four of these stay cheap

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def assert_blocks_on_repo_lock(self, *args, env=None):
        """Run `brain <args>` while the repo lock is held elsewhere; it must wait."""
        try:
            import fcntl
        except ImportError:
            self.skipTest("no fcntl on this platform — repo_lock is a no-op by design")
        (self.repo / ".cache").mkdir(exist_ok=True)
        holder = open(self.repo / ".cache" / "git.lock", "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        proc = subprocess.Popen(
            [sys.executable, str(self.repo / "bin" / "brain"), *args],
            cwd=self.repo, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True)
        try:
            with self.assertRaises(subprocess.TimeoutExpired,
                                   msg=f"`brain {args[0]}` did not take repo_lock — "
                                       "it ran to completion while the lock was held"):
                proc.communicate(timeout=self.HELD)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        out, _ = proc.communicate(timeout=120)
        return out

    def test_capture_commit_takes_the_lock(self):
        self.assert_blocks_on_repo_lock("capture", "locked capture", "--commit")

    def test_supersede_takes_the_lock(self):
        (self.repo / "knowledge/reference/lock-seed.md").write_text(
            "---\nid: lock-seed\nkind: reference\ntitle: Lock seed\ntopics: [brain]\n"
            "aliases: [lock seed]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            "---\n\nThe original claim.\n", encoding="utf-8")
        self.assert_blocks_on_repo_lock("supersede", "lock-seed", "Lock seed revised")

    def test_consolidate_takes_the_lock_before_cutting_its_branch(self):
        """The branch switch is the dangerous half: a capture that read 'main'
        from HEAD a moment earlier would commit onto the consolidation branch
        and vanish from main's working tree."""
        stub_bin = Path(self.tmp.name) / "stubbin"
        stub_bin.mkdir()
        stub = stub_bin / "claude"          # never invoke the real runner here
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        git_dir = str(Path(shutil.which("git") or "/usr/bin/git").parent)
        env = {**os.environ, "PATH": f"{stub_bin}:{git_dir}:/usr/bin:/bin"}
        out = self.assert_blocks_on_repo_lock("consolidate", env=env)
        self.assertNotIn("working tree not clean", out,
                         "the clean-tree test must run INSIDE the lock, with the "
                         "switch it guards")

    def test_capture_without_commit_does_not_wait_on_the_lock(self):
        """The counterpart: a capture that never touches the index must not be
        stalled behind a consolidation. Writing to disk is always available."""
        try:
            import fcntl
        except ImportError:
            self.skipTest("no fcntl on this platform")
        (self.repo / ".cache").mkdir(exist_ok=True)
        holder = open(self.repo / ".cache" / "git.lock", "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            out = run_brain("capture", "no commit needed", repo=self.repo)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        self.assertEqual(out.returncode, 0, out.stdout)


class ReadStalenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_inbox_read_is_marked_provisional(self):
        cap = run_brain("capture", "provisional flavor marker", repo=self.repo)
        path = Path(cap.stdout.strip().splitlines()[-1])
        out = run_brain("read", str(path), repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PROVISIONAL", out.stdout)

    def test_expired_review_by_is_surfaced_on_read(self):
        ref = self.repo / "knowledge/reference"
        (ref / "perishable.md").write_text(
            "---\nid: perishable\nkind: reference\ntitle: Perishable\ntopics: [brain]\n"
            "aliases: [perishable]\ncreated: 2020-01-01\nstatus: current\n"
            "review_by: 2020-06-01\n---\n\nA fact that should have been re-checked long ago.\n",
            encoding="utf-8")
        out = run_brain("read", "perishable", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("review_by", out.stdout)
        self.assertIn("STALE", out.stdout)


class LinkGraphTests(unittest.TestCase):
    """[[wikilinks]] were previously decorative: nothing validated them and
    nothing could answer 'what refers to this note?'. Supersede then left every
    hub page pointing at an archived note with no signal anywhere."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        self.ref = self.repo / "knowledge" / "reference"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write(self, name, body, extra=""):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {name}\ntopics: [brain]\n"
            f"aliases: [{name}]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"{extra}---\n\n{body}\n", encoding="utf-8")

    def links_json(self, note_id):
        out = run_brain("links", note_id, "--json", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return json_payload(out.stdout)

    def test_backlinks_are_discovered(self):
        self.write("target-note", "The thing being referenced.")
        self.write("citing-a", "See [[target-note]] for detail.")
        self.write("citing-b", "Also relies on [[target-note|the target]].")
        graph = self.links_json("target-note")
        self.assertEqual(sorted(n["id"] for n in graph["inbound"]), ["citing-a", "citing-b"],
                         "backlinks missed a referring note")

    def test_outbound_links_are_recorded(self):
        self.write("hub", "Points at [[leaf-one]] and [[leaf-two]].")
        self.write("leaf-one", "one")
        self.write("leaf-two", "two")
        graph = self.links_json("hub")
        self.assertEqual(sorted(n["id"] for n in graph["outbound"]), ["leaf-one", "leaf-two"])

    def test_dangling_link_in_a_canonical_note_is_an_error(self):
        self.write("has-bad-link", "Refers to [[no-such-note-anywhere]].")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 1, "a dangling wikilink did not block the commit")
        self.assertIn("dangling wikilink", out.stdout)

    def test_link_to_a_superseded_note_warns_with_the_successor(self):
        """The exact rot supersede used to create silently."""
        self.write("original-rule", "The original rule.")
        out = run_brain("supersede", "original-rule", "Original rule v2", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        # a hub still pointing at the OLD id
        self.write("stale-hub", "Follow [[original-rule]] for the rules.")
        lint = run_brain("lint", repo=self.repo)
        self.assertIn("points at a SUPERSEDED note", lint.stdout)
        self.assertIn("repoint it at", lint.stdout)

    def test_successor_may_cite_its_own_predecessor_without_warning(self):
        """`Supersedes [[old-id]]` is what the protocol itself writes — that one
        back-reference must never be reported as rot."""
        self.write("original-rule", "The original rule.")
        out = run_brain("supersede", "original-rule", "Original rule v2", repo=self.repo)
        self.assertEqual(out.returncode, 0, "setup: supersede failed\n" + out.stdout)
        # Complete the supersede as the protocol requires — an unwritten
        # successor and an unreplaced banner reason are both lint errors, and
        # this test is about the wikilink warning, not about those.
        successor = self.ref / "original-rule-v2.md"
        successor.write_text(
            successor.read_text(encoding="utf-8").replace(
                "## What\n", "## What\n\nThe revised rule.\n"), encoding="utf-8")
        archived = self.repo / "knowledge/archive/reference/original-rule.md"
        archived.write_text(
            archived.read_text(encoding="utf-8").replace(
                "<one-line reason>", "the rule changed"), encoding="utf-8")
        lint = run_brain("lint", repo=self.repo)
        self.assertEqual(lint.returncode, 0, "a clean supersede left lint failing:\n" + lint.stdout)
        # Other notes citing the old id SHOULD warn — that is the feature. Only
        # the successor's own back-reference must be exempt.
        offending = [l for l in lint.stdout.splitlines()
                     if "points at a SUPERSEDED note" in l and "original-rule-v2.md" in l]
        self.assertEqual(offending, [],
                         "the successor was flagged for citing its own predecessor")

    def test_archived_notes_are_not_link_checked(self):
        """Archive is append-only history; a dangling link there would be an
        unfixable permanent lint error."""
        arch = self.repo / "knowledge/archive/reference"
        arch.mkdir(parents=True, exist_ok=True)
        self.write("successor", "current")
        (arch / "old.md").write_text(
            "---\nid: old-note\nkind: reference\ntitle: Old\ntopics: [brain]\n"
            "aliases: [old]\ncreated: 2026-06-01\nstatus: superseded\n"
            "superseded_by: successor\nreview_by: null\n---\n\n"
            "> SUPERSEDED 2026-07-01 by [[successor]] — x\n\nRefers to [[long-gone-note]].\n",
            encoding="utf-8")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0,
                         "a link inside archived history blocked the commit:\n" + out.stdout)

    def test_inbox_dangling_link_warns_but_does_not_block(self):
        (self.repo / "knowledge/inbox/thought.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nrelates to [[not-yet-written]]\n",
            encoding="utf-8")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0, "a provisional capture was blocked by a link check")
        self.assertIn("dangling wikilink", out.stdout)

    def test_read_surfaces_backlinks(self):
        self.write("popular", "body")
        self.write("refers-here", "cites [[popular]]")
        out = run_brain("read", "popular", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("linked from", out.stdout)
        self.assertIn("refers-here", out.stdout)

    def test_deleting_a_note_updates_the_graph(self):
        """The graph is derived — it must not outlive the notes it describes."""
        self.write("doomed", "body")
        self.write("pointer", "cites [[doomed]]")
        self.assertTrue(self.links_json("doomed")["inbound"])
        (self.ref / "pointer.md").unlink()
        self.assertEqual(self.links_json("doomed")["inbound"], [],
                         "graph still reports a backlink from a deleted note")


class SecretScannerTests(unittest.TestCase):
    """The built-in scanner is the LAST line of defence: SETUP documents a
    no-gitleaks path, so anything it misses can be committed and auto-pushed.

    A real paste of production-shaped env vars once sailed through it untouched
    (0 of 5 detected). Every sample below is assembled at runtime rather than
    written literally, so this test file never itself contains a secret-shaped
    string for gitleaks/CI to flag."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def samples(self):
        return {
            "Stripe secret key": "STRIPE_KEY=" + "sk_" + "live_" + "5tR1pE" * 5,
            "Google API key": "MAPS=" + "AIza" + "Bc9" * 13,
            "GitHub fine-grained PAT": "GH=" + "github_" + "pat_" + "aB3" * 12,
            "URL with embedded credentials":
                "DB=" + "postgres" + "://" + "admin" + ":" + "s3cretpassw0rd" + "@" + "db.x.io/y",
            "credential assignment": "aws_secret_" + "access_key=" + "wJalr" * 8,
        }

    def write_note(self, text):
        (self.repo / "knowledge" / "inbox" / "leak.md").write_text(
            f"---\ncreated: 2026-07-24\nstatus: draft\n---\n\n{text}\n", encoding="utf-8")

    def test_each_secret_class_is_detected(self):
        for label, sample in self.samples().items():
            with self.subTest(secret=label):
                self.write_note(sample)
                out = run_brain("lint", repo=self.repo)
                self.assertEqual(out.returncode, 1,
                                 f"{label} was NOT detected — it would commit and push:\n{sample}")
                self.assertIn(label, out.stdout, f"detected, but not reported as {label}")

    def test_a_clean_note_is_not_flagged(self):
        """Guard against the scanner becoming so eager it blocks ordinary prose."""
        self.write_note(
            "We agreed the password policy needs review, and the API key rotation\n"
            "is handled by the ops runbook. See the token lifetime discussion.\n"
            "Connection docs live at https://example.com/docs/postgres and nowhere else.\n")
        out = run_brain("lint", repo=self.repo)
        self.assertEqual(out.returncode, 0,
                         "ordinary prose about passwords/keys was flagged:\n" + out.stdout)

    def test_scanner_does_not_flag_its_own_pattern_definitions(self):
        """bin/brain scans every file including itself; permissive patterns must
        not match the source that defines them."""
        out = run_brain("lint", repo=self.repo)
        self.assertNotIn("bin/brain:", out.stdout.replace("bin/brain lint", ""),
                         "the scanner flagged its own source:\n" + out.stdout)


class CommitGateTests(unittest.TestCase):
    """The pre-commit gate is what protects the whole repo, and it had no
    end-to-end coverage: every prior test that touched it replaced it with a
    stub. These drive the REAL hook via a real `git commit`, so the scale work
    on `lint --staged` (which now sweeps only the staged paths) cannot quietly
    stop blocking things."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def commit(self, *paths):
        subprocess.run(["git", "add", *[str(p) for p in paths]], cwd=self.repo,
                       check=True, capture_output=True)
        return subprocess.run(["git", "commit", "-m", "probe"], cwd=self.repo,
                              capture_output=True, text=True)

    def note(self, rel, body, front=None):
        p = self.repo / "knowledge" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        head = front if front is not None else (
            "id: probe-note\nkind: reference\ntitle: Probe\ntopics: [brain]\n"
            "aliases: [probe]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n")
        p.write_text(f"---\n{head}---\n\n{body}\n", encoding="utf-8")
        return p

    def test_gate_blocks_a_secret_in_a_staged_note(self):
        p = self.repo / "knowledge/inbox/leak.md"
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nK="
                     + "sk_" + "live_" + "AbCdEfGhIjKlMnOpQrSt" + "\n", encoding="utf-8")
        self.assertNotEqual(self.commit(p).returncode, 0, "a secret was committed")

    def test_gate_blocks_malformed_frontmatter(self):
        p = self.repo / "knowledge/reference/bad.md"
        p.write_text("---\nBROKEN\n", encoding="utf-8")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_merge_conflict_markers(self):
        p = self.note("reference/conflict.md",
                      "## W\n<<<<<<< HEAD\na\n=======\nb\n>>>>>>> other\n")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_a_dangling_wikilink(self):
        p = self.note("reference/dangle.md", "refers to [[no-such-note-at-all]]")
        self.assertNotEqual(self.commit(p).returncode, 0)

    def test_gate_blocks_a_duplicate_id_against_the_whole_tree(self):
        """A global invariant: catching it needs every OTHER note, not just the
        staged one — the staged-only sweep must not have cost us this."""
        first = self.note("reference/original.md", "x", front=(
            "id: collide-target\nkind: reference\ntitle: First\ntopics: [brain]\n"
            "aliases: [first]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(first).returncode, 0, "setup note was rejected")
        p = self.note("reference/dup.md", "x", front=(
            "id: collide-target\nkind: reference\ntitle: Dup\ntopics: [brain]\n"
            "aliases: [dup]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        out = self.commit(p)
        self.assertNotEqual(out.returncode, 0, "a duplicate id was committed")
        self.assertIn("duplicate id", out.stdout + out.stderr)

    def test_gate_lets_a_valid_note_through(self):
        p = self.note("reference/fine.md", "## What\nA perfectly ordinary note.")
        out = self.commit(p)
        self.assertEqual(out.returncode, 0,
                         "the gate blocked a valid note:\n" + out.stdout + out.stderr)

    def test_gate_blocks_deleting_a_note_others_still_link_to(self):
        """The incremental gate only inspects what changed — so a deletion,
        whose own file is gone, could sail through while every note pointing at
        it silently rots. Caught by differentially testing the fast gate against
        the full one; this is that case nailed down."""
        target = self.note("reference/linked-to.md", "the referenced note", front=(
            "id: link-target\nkind: reference\ntitle: T\ntopics: [brain]\n"
            "aliases: [t]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        referrer = self.note("reference/refers.md", "see [[link-target]] for detail", front=(
            "id: referrer\nkind: reference\ntitle: R\ntopics: [brain]\n"
            "aliases: [r]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(target, referrer).returncode, 0, "setup was rejected")
        subprocess.run(["git", "rm", "-q", "knowledge/reference/linked-to.md"],
                       cwd=self.repo, check=True, capture_output=True)
        out = subprocess.run(["git", "commit", "-m", "delete"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0,
                            "deleted a note that others still link to, breaking them silently")
        self.assertIn("dangling wikilink", out.stdout + out.stderr)

    def test_gate_allows_deleting_a_note_nothing_links_to(self):
        """The mirror image: the check must not make ordinary deletion painful."""
        lonely = self.note("reference/lonely.md", "nobody references this", front=(
            "id: lonely-note\nkind: reference\ntitle: L\ntopics: [brain]\n"
            "aliases: [l]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"))
        self.assertEqual(self.commit(lonely).returncode, 0)
        subprocess.run(["git", "rm", "-q", "knowledge/reference/lonely.md"],
                       cwd=self.repo, check=True, capture_output=True)
        out = subprocess.run(["git", "commit", "-m", "delete"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0,
                         "blocked a harmless deletion:\n" + out.stdout + out.stderr)

    def test_gate_judges_the_staged_blob_not_the_worktree(self):
        """The whole point of --staged: staging a bad blob and then fixing the
        working copy must still be refused, or a secret reaches the remote."""
        p = self.repo / "knowledge/inbox/z.md"
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nK="
                     + "sk_" + "live_" + "ZzYyXxWwVvUuTtSsRrQq" + "\n", encoding="utf-8")
        subprocess.run(["git", "add", str(p)], cwd=self.repo, check=True, capture_output=True)
        p.write_text("---\ncreated: 2026-07-24\nstatus: draft\n---\n\nnow clean\n",
                     encoding="utf-8")            # fix the WORKTREE only
        out = subprocess.run(["git", "commit", "-m", "probe"], cwd=self.repo,
                             capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0,
                            "committed a bad STAGED blob because the worktree looked clean")


class LocalRemoteTests(unittest.TestCase):
    """`git clone ~/brain` points origin at the SOURCE repo, and post-commit
    auto-pushes after every commit — so a copy made to experiment in would push
    its throwaway branches straight back into the real brain. That actually
    happened during development. A path on this machine is not a backup and is
    never pushed to."""

    def setUp(self):
        self.tmp = temp_dir()
        self.source = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "s@example.com"], ["user.name", "Source"]):
            subprocess.run(["git", "config", *cfg], cwd=self.source, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.source, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.source, check=True, capture_output=True)
        self.clone = Path(self.tmp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(self.source), str(self.clone)],
                       check=True, capture_output=True)
        for cfg in (["user.email", "c@example.com"], ["user.name", "Clone"],
                    ["core.hooksPath", ".githooks"]):
            subprocess.run(["git", "config", *cfg], cwd=self.clone, check=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def source_branches(self):
        out = subprocess.run(["git", "branch", "--format=%(refname:short)"],
                             cwd=self.source, capture_output=True, text=True).stdout
        return {b.strip() for b in out.splitlines() if b.strip()}

    def test_a_scratch_clone_never_pushes_back_into_the_source_brain(self):
        subprocess.run(["git", "checkout", "-q", "-b", "experiment"],
                       cwd=self.clone, check=True, capture_output=True)
        out = run_brain("capture", "throwaway experiment", "--commit", repo=self.clone)
        self.assertEqual(out.returncode, 0, "setup: the capture did not commit\n" + out.stdout)
        time.sleep(3)                      # post-commit is detached; let it try
        self.assertNotIn("experiment", self.source_branches(),
                         "a scratch clone pushed its branch into the source brain")
        log = subprocess.run(["git", "log", "--all", "--oneline"], cwd=self.source,
                             capture_output=True, text=True).stdout
        self.assertNotIn("throwaway", log, "clone commits leaked into the source brain")

    def test_doctor_refuses_to_call_a_local_path_a_backup(self):
        out = run_brain("doctor", repo=self.clone)
        self.assertIn("remote is a local path", out.stdout)
        self.assertNotEqual(out.returncode, 0,
                            "doctor reported healthy for a brain with no real backup")

    def test_local_remote_detection(self):
        """Mirrors the same test in .githooks/post-commit — keep them in step."""
        import importlib.util
        spec = importlib.util.spec_from_loader("brainmod", loader=None)
        module = importlib.util.module_from_spec(spec)
        module.__file__ = str(BRAIN)
        exec(compile(BRAIN.read_text(encoding="utf-8"), "brain", "exec"), module.__dict__)
        is_local = module.__dict__["is_local_remote"]
        for url in ("https://github.com/u/r.git", "git@github.com:u/r.git",
                    "ssh://git@host/u/r.git", "git://host/r.git"):
            self.assertFalse(is_local(url), f"{url} is a real remote")
        for url in ("/Users/x/brain", "file:///Users/x/brain", "../other",
                    "./copy", "~/brain", "/tmp/gone"):
            self.assertTrue(is_local(url), f"{url} is a local path")


class MergeBackupTests(unittest.TestCase):
    """`git merge` never runs post-commit — it runs post-merge, for a merge
    commit and a fast-forward alike. Before that hook existed, merging moved
    HEAD and pushed nothing: the work sat on this machine until some later
    ordinary commit happened to carry it, with nothing to say so. Observed for
    real when a capture branch was merged into main."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cfg in (["user.email", "m@example.com"], ["user.name", "Merger"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def git_shim(self):
        """A `git` that records every call and stops short of a real push, so a
        test can prove the push was REACHED without touching the network."""
        shim_dir = Path(self.tmp.name) / "shim"
        shim_dir.mkdir(exist_ok=True)
        log = Path(self.tmp.name) / "git-calls.log"
        real_git = shutil.which("git")
        (shim_dir / "git").write_text(
            f'#!/bin/sh\necho "$@" >> {log}\n'
            f'case "$1" in push) exit 0 ;; esac\nexec {real_git} "$@"\n', encoding="utf-8")
        (shim_dir / "git").chmod(0o755)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/example/not-real.git"],
                       cwd=self.repo, check=True, capture_output=True)
        env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}",
                   GIT_TERMINAL_PROMPT="0")
        return log, env

    def run_hook_and_read_calls(self, hook, env, log):
        subprocess.run(["sh", hook], cwd=self.repo, env=env,
                       capture_output=True, timeout=60)
        # The hook works in a detached subshell, so the parent returns first;
        # wait for the shim to actually record something.
        deadline = time.time() + 30
        calls = ""
        while time.time() < deadline:
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            if "rev-parse" in calls:
                time.sleep(1)              # let any push attempt land in the log too
                calls = log.read_text(encoding="utf-8")
                break
            time.sleep(0.5)
        self.assertIn("rev-parse", calls, "the shim never ran — the test proves nothing")
        return calls

    def test_post_merge_hook_exists_and_is_executable(self):
        """git silently skips a hook without the exec bit, so the mode IS the
        wiring — `brain template` copies it with copy2 to preserve exactly this."""
        hook = ROOT / ".githooks" / "post-merge"
        self.assertTrue(hook.exists(), "no post-merge hook — merges are not backed up")
        self.assertTrue(os.access(hook, os.X_OK),
                        "post-merge is not executable, so git will never run it")

    def test_a_real_merge_commit_reaches_the_backup(self):
        """End-to-end wiring, with post-commit stubbed down to a marker so the
        test stays hermetic: prove git fires post-merge for a merge commit, and
        that our hook hands off to the backup rather than ending there."""
        # Stub the sandbox's OWN .githooks/post-commit rather than pointing
        # core.hooksPath somewhere else: the detached backup job from any earlier
        # commit runs `bin/brain index`, whose ensure_hooks() resets
        # core.hooksPath back to .githooks — which would silently undo the
        # redirect mid-test and leave this asserting against the real hook.
        marker = Path(self.tmp.name) / "backup-ran"
        (self.repo / ".githooks" / "post-commit").write_text(
            f'#!/bin/sh\necho ran >> "{marker}"\n', encoding="utf-8")
        (self.repo / ".githooks" / "post-commit").chmod(0o755)
        base = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=self.repo,
                       check=True, capture_output=True)
        (self.repo / "knowledge/inbox/merged-capture.md").write_text(
            "---\ncreated: 2026-07-25\nstatus: draft\n---\n\nA capture.\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "side"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", base], cwd=self.repo,
                       check=True, capture_output=True)
        # The side-branch commit legitimately fired post-commit (that path has
        # always worked). Clear the marker here so what it records afterwards can
        # only have come from the merge itself.
        marker.unlink(missing_ok=True)
        self.assertFalse(marker.exists(), "setup: the marker did not clear")
        subprocess.run(["git", "merge", "--no-ff", "--no-edit", "-q", "side"],
                       cwd=self.repo, check=True, capture_output=True)
        self.assertTrue(marker.exists(),
                        "a merge commit never reached the backup — post-merge is not wired")

    def test_post_merge_does_reach_the_push_on_a_normal_branch(self):
        """The mirror of the guard below: without this, a post-merge that had
        quietly become inert would still pass the 'never pushes' test."""
        log, env = self.git_shim()
        calls = self.run_hook_and_read_calls(".githooks/post-merge", env, log)
        self.assertIn("push", calls,
                      "post-merge never reached the push — the backup is inert:\n" + calls)

    def test_post_merge_never_pushes_a_consolidate_branch(self):
        """post-merge delegates to post-commit precisely so the audit gate keeps
        applying at the merge entry point too. If it ever grows its own copy of
        the push, this is what catches the gate going missing on that path."""
        subprocess.run(["git", "checkout", "-q", "-b", "consolidate/2026-07-25"],
                       cwd=self.repo, check=True, capture_output=True)
        log, env = self.git_shim()
        calls = self.run_hook_and_read_calls(".githooks/post-merge", env, log)
        self.assertNotIn("push", calls,
                         "post-merge pushed from a consolidate branch:\n" + calls)


class TemplateTests(unittest.TestCase):
    """`brain template` is what makes this repo publishable. If it ever ships a
    note, a key, or machine-local wiring, private knowledge goes public — so
    the guarantees are asserted, not trusted."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.dest = Path(self.tmp.name) / "template"
        # Plant content that must NOT ship: a note, and a vault recipient.
        # Built at runtime: a literal canary here would also live in this test
        # file, which the template legitimately ships, and self-trip the check.
        self.canary = "CONFIDENTIAL" + "-" + "CANARY"
        (self.repo / "knowledge/reference/private-thing.md").write_text(
            "---\nid: private-thing\nkind: reference\ntitle: Private\ntopics: [brain]\n"
            "aliases: [private]\ncreated: 2026-07-01\nstatus: current\nreview_by: null\n"
            f"---\n\nA {self.canary} fact about the owner.\n", encoding="utf-8")
        (self.repo / "setup/vault-recipient.txt").write_text(
            "age1exampleexamplerecipientkeyvalue\n", encoding="utf-8")

    def tearDown(self):
        cleanup_temp(self.tmp)

    def generate(self):
        out = run_brain("template", str(self.dest), repo=self.repo)
        self.assertEqual(out.returncode, 0, "template generation failed:\n" + out.stdout)
        return out

    # Deliberately hardcoded rather than read from TEMPLATE_KEEP_KNOWLEDGE:
    # widening what the template ships must require editing this list, or the
    # test would rubber-stamp whatever the code decided to publish.
    ALLOWED_IN_TEMPLATE = {"index.md", "vault/README.md", "reference/note-conventions.md"}

    def test_no_note_of_the_owners_ships(self):
        self.generate()
        shipped = {str(p.relative_to(self.dest / "knowledge"))
                   for p in (self.dest / "knowledge").rglob("*.md")}
        unexpected = shipped - self.ALLOWED_IN_TEMPLATE
        self.assertEqual(unexpected, set(), f"owner notes shipped: {sorted(unexpected)}")
        blob = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in self.dest.rglob("*") if p.is_file())
        self.assertNotIn(self.canary, blob, "note CONTENT leaked into the template")

    def test_vault_recipient_and_machine_local_wiring_do_not_ship(self):
        self.generate()
        self.assertFalse((self.dest / "setup/vault-recipient.txt").exists(),
                         "the author's age recipient shipped — strangers would "
                         "encrypt to a key they cannot read back")
        self.assertFalse((self.dest / ".mcp.json").exists())
        self.assertFalse((self.dest / "setup/skills/brain/SKILL.md").exists())

    def test_the_scaffolding_a_brain_needs_does_ship(self):
        self.generate()
        for required in ("bin/brain", "bin/brain-mcp", "AGENTS.md", "CLAUDE.md", "SETUP.md",
                         "knowledge/index.md", "knowledge/topics.yaml",
                         ".githooks/pre-commit", "tests/test_brain.py"):
            self.assertTrue((self.dest / required).exists(), f"missing {required}")
        for folder in ("decisions", "reference", "topics", "people", "life",
                       "projects", "inbox", "archive", "vault"):
            self.assertTrue((self.dest / "knowledge" / folder).is_dir(),
                            f"knowledge/{folder}/ missing from the skeleton")

    def test_generated_template_is_lint_clean_and_usable(self):
        """An empty brain must accept its owner's very first note."""
        self.generate()
        self.assertEqual(run_brain("lint", repo=self.dest).returncode, 0)
        made = run_brain("new", "decision", "First real decision",
                         "--topics", "brain", repo=self.dest)
        self.assertEqual(made.returncode, 0, "a fresh brain rejected its first note:\n"
                         + made.stdout + made.stderr)
        self.assertEqual(run_brain("lint", repo=self.dest).returncode, 0)

    def test_the_daily_pulse_is_stripped_from_the_templates_own_ci(self):
        """The pulse emails on 3 days of no commits — right for a live brain,
        pure noise on a template repo nobody commits to. The push-triggered
        gate must survive; only the schedule and its job go."""
        self.generate()
        gate = (self.dest / ".github" / "workflows" / "gate.yml").read_text(encoding="utf-8")
        self.assertNotIn("schedule:", gate, "the schedule trigger was not removed")
        self.assertNotIn("pulse:", gate, "the pulse job was not removed")
        self.assertNotIn("github.event_name == 'schedule'", gate)
        self.assertIn("on:", gate)
        self.assertIn("push:", gate, "the push trigger must survive")
        self.assertIn("brain lint", gate, "the content gate must survive")
        self.assertIn("runtime tests", gate, "the test job must survive")
        self.assertIn("Backup alarm", gate,
                      "the emitted CI must say how to re-enable the pulse")

    def test_generating_the_template_does_not_touch_the_source_gate(self):
        """The strip is emit-only — it rewrites the COPY, never the source. (An
        assertion on the source's own content cannot live here: this suite also
        runs inside the emitted template as the acceptance check, where the
        'source' is itself already stripped.)"""
        gate = self.repo / ".github" / "workflows" / "gate.yml"
        before = gate.read_text(encoding="utf-8")
        self.generate()
        self.assertEqual(gate.read_text(encoding="utf-8"), before,
                         "template generation mutated the source repo's gate.yml")

    def test_the_example_note_stands_alone_and_is_dated_to_the_emit(self):
        """The one shipped note proves what a good note looks like, so its own
        references must not dangle in the copy a stranger reads, and its
        review_by must not already be in the past on day one."""
        self.generate()
        note = (self.dest / "knowledge" / "reference" / "note-conventions.md")
        fm, _ = load_brain_module().parse_frontmatter(note.read_text(encoding="utf-8"))
        import datetime as _dt
        today = _dt.date.today().isoformat()
        self.assertEqual(fm["created"], today, "the example note kept the author's date")
        self.assertGreater(fm["review_by"], today,
                           "the example note ships already overdue for review")
        body = note.read_text(encoding="utf-8")
        self.assertNotIn("2026-07-22-tiered-retrieval", body,
                         "the example still cites a decision that does not ship")
        # No dangling wikilink survived into the shipped example.
        for target in load_brain_module().extract_links(body):
            self.fail(f"the example note ships a wikilink to {target!r}, which is not "
                      "in the empty template")

    def test_the_owners_topic_vocabulary_does_not_ship(self):
        """topics.yaml is knowledge wearing a config file's clothes: the topic
        names ARE the author's projects, clients and concerns. It escapes the
        note sweep because a brain with no vocabulary cannot lint."""
        (self.repo / "knowledge/topics.yaml").write_text(
            "brain: second-brain\nconventions: schema\nretrieval: search\n"
            "acmecorp: client work, invoices\n", encoding="utf-8")
        self.generate()
        shipped = (self.dest / "knowledge/topics.yaml").read_text(encoding="utf-8")
        self.assertNotIn("acmecorp", shipped, "the author's private topic list shipped")
        self.assertIn("brain:", shipped, "the template needs a starter vocabulary to lint")

    # Assembled at runtime for the same reason as self.canary above: a literal
    # here would live in tests/test_brain.py, which the template legitimately
    # ships, and every one of these tests would trip on its own fixture.
    HANDLE = "quimby" + "handle"

    def test_the_publishers_own_name_does_not_ship(self):
        """Identity leaks through code, not just notes: a launchd label, a
        comment, a hardcoded home path. The tokens are derived from git rather
        than listed, because a hard-coded list of the author's names would have
        to live in the file being published."""
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/{self.HANDLE}/brain.git"],
                       cwd=self.repo, check=True, capture_output=True)
        brain = self.repo / "bin" / "brain"
        brain.write_text(brain.read_text(encoding="utf-8")
                         + f"\n# left over from {self.HANDLE}'s own machine\n",
                         encoding="utf-8")
        out = run_brain("template", str(self.dest), repo=self.repo)
        self.assertEqual(out.returncode, 1, "the publisher's handle shipped:\n" + out.stdout)
        self.assertIn(f"bin/brain: contains '{self.HANDLE}'", out.stdout,
                      "the scan did not reach the toolbelt itself")

    def test_a_short_multiword_name_is_caught_by_the_collapsed_token(self):
        """'Li Wu' splits to 'li'/'wu' (both under the 4-char floor) and would
        slip through word-splitting alone. The collapsed whole value 'liwu'
        catches it without lowering the floor into false-positive territory."""
        subprocess.run(["git", "config", "user.name", "Li Wu"],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "li.wu@example.com"],
                       cwd=self.repo, check=True)
        module = load_brain_module()
        module.git = lambda *a, **k: {
            ("config", "user.name"): "Li Wu",
            ("config", "user.email"): "li.wu@example.com",
            ("remote", "get-url", "origin"): "https://github.com/liwuhandle/brain.git",
        }.get(a, "")
        tokens = module.publisher_identity_tokens()
        self.assertIn("liwu", tokens, "the collapsed short multi-word name was missed")
        self.assertIn("liwuhandle", tokens, "the remote owner handle was missed")

    def test_the_identity_scan_is_never_a_silent_no_op(self):
        """When nothing identifying can be derived, the emit must SAY the scan
        did not run rather than print a reassuring 'clean' that implies it did."""
        module = load_brain_module()
        module.git = lambda *a, **k: ""            # no name, email or remote
        real_home = module.Path.home
        try:
            module.Path.home = staticmethod(lambda: Path("/app"))   # generic, stopword-ish
            self.assertEqual(module.publisher_identity_tokens(), set())
        finally:
            module.Path.home = real_home
        # And the emit surfaces it. Strip identity from the sandbox's git config
        # and run with a HOME whose basename is a stopword, so the real
        # derivation comes back empty in the spawned process too (the default
        # run_brain would leak the tester's real home-dir name).
        subprocess.run(["git", "config", "--unset", "user.name"],
                       cwd=self.repo, capture_output=True)
        subprocess.run(["git", "config", "--unset", "user.email"],
                       cwd=self.repo, capture_output=True)
        anon_home = Path(self.tmp.name) / "none"     # basename 'none' is a stopword
        anon_home.mkdir(exist_ok=True)
        # GIT_CONFIG_GLOBAL/SYSTEM=/dev/null so the spawned process sees no
        # user.name/email from the tester's own config (never mutate real config
        # from a test); local config has both unset and the sandbox has no remote.
        env = dict(os.environ, HOME=str(anon_home), GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")
        out = subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "template", str(self.dest)],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=180)
        # It may still succeed (identity is defense-in-depth), but it must warn.
        self.assertIn("did NOT run", out.stdout,
                      "the emit implied the identity scan ran when it did not:\n" + out.stdout)

    # A token root that appears nowhere in the shipped tree, so these tests
    # control exactly where it turns up. Built by concatenation for the same
    # reason self.canary is: a literal would live in this file, which ships.
    ROOT_TOKEN = "zorb" + "ax"

    def _publish(self, *extra, home=None):
        """Run `template` with a controlled identity, the way CI would."""
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null",
                   GIT_CONFIG_SYSTEM="/dev/null")
        if home is not None:
            env["HOME"] = str(home)
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "template",
             str(self.dest), *extra],
            cwd=self.repo, env=env, capture_output=True, text=True, timeout=300)

    def test_a_machine_account_home_is_not_treated_as_the_publisher(self):
        """Regression, and it was breaking every CI run: on GitHub Actions HOME
        is /home/runner, so the home basename became an identity token — and
        this project's own docs say "runner" constantly ("the pinned runner"),
        so `template` refused its own emit and 11 tests failed. A home
        directory names a machine unless git agrees it names a person."""
        module = load_brain_module()
        real_home = module.Path.home
        real_git = module.git
        try:
            module.git = lambda *a, **k: ""          # no name, email or remote
            # Assembled rather than written out: this file is published, and a
            # literal "/home/<ci-account>" here is a machine path in shipped
            # source — which the path scan would flag when running ON that CI.
            for machine in ("/home/" + "runner", "/home/" + "ubuntu",
                            "/home/" + "ec2-user"):
                module.Path.home = staticmethod(lambda m=machine: Path(m))
                tokens = module.publisher_identity_tokens()
                self.assertEqual(
                    tokens, set(),
                    f"{machine} was mistaken for a person: {tokens}")
        finally:
            module.Path.home = real_home
            module.git = real_git

    def test_a_home_directory_git_corroborates_is_still_identity(self):
        """The rule above must not become a hole: when git agrees the home
        directory names the publisher, it counts."""
        module = load_brain_module()
        real_home, real_git = module.Path.home, module.git
        try:
            module.git = lambda *a, **k: {
                ("config", "user.name"): "Quimby Handle",
            }.get(a, "")
            module.Path.home = staticmethod(lambda: Path("/Users/quimby"))
            self.assertIn("quimby", module.publisher_identity_tokens(),
                          "a corroborated home directory was dropped")
        finally:
            module.Path.home, module.git = real_home, real_git

    def test_a_name_inside_a_longer_word_does_not_block_publishing(self):
        """Identity tokens are matched on word boundaries, never as substrings.
        Substring matching made `template` unusable for a large share of real
        people: m-a-r-k against "markdown", k-e-n against "token", p-a-t
        against "path", g-e-n-e against "generate". That is most of the name
        space, and it fails closed — so it has to be right."""
        subprocess.run(["git", "config", "user.name", self.ROOT_TOKEN],
                       cwd=self.repo, check=True)
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8")
                          + f"\nA sentence about {self.ROOT_TOKEN}ing and "
                            f"{self.ROOT_TOKEN}ology.\n", encoding="utf-8")
        out = self._publish()
        self.assertEqual(out.returncode, 0,
                         "a name embedded in longer words blocked the emit:\n" + out.stdout)

    def test_a_standalone_occurrence_of_the_name_still_refuses(self):
        """The positive control for the test above: relaxing to word boundaries
        must not relax what the scan actually catches."""
        subprocess.run(["git", "config", "user.name", self.ROOT_TOKEN],
                       cwd=self.repo, check=True)
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8")
                          + f"\nReviewed by {self.ROOT_TOKEN} in 2026.\n",
                          encoding="utf-8")
        out = self._publish()
        self.assertEqual(out.returncode, 1,
                         "the publisher's name shipped verbatim:\n" + out.stdout)
        self.assertIn(self.ROOT_TOKEN, out.stdout)

    def test_allow_identity_waives_a_token_and_says_so(self):
        """Fail-closed needs a documented way through, or a publisher whose name
        is an ordinary English word can never publish at all. Waiving must be
        explicit, per-token, and reported — never silent."""
        subprocess.run(["git", "config", "user.name", self.ROOT_TOKEN],
                       cwd=self.repo, check=True)
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8")
                          + f"\nReviewed by {self.ROOT_TOKEN} in 2026.\n",
                          encoding="utf-8")
        self.assertEqual(self._publish().returncode, 1, "precondition: it should refuse")
        out = self._publish("--allow-identity", self.ROOT_TOKEN)
        self.assertEqual(out.returncode, 0, "the waiver did not take:\n" + out.stdout)
        self.assertIn("WAIVED", out.stdout, "a waived check was not reported")
        self.assertIn(self.ROOT_TOKEN, out.stdout)

    def test_an_absolute_home_path_is_caught_however_odd_the_username(self):
        """The token scan cannot express this: `/Users/<you>/brain` baked into a
        generated file gives the username away even when it is not a word
        anywhere. Matched literally, so it cannot false-positive either."""
        home = Path(self.tmp.name) / "none" / f"{self.ROOT_TOKEN}user"
        home.mkdir(parents=True, exist_ok=True)
        agents = self.repo / "AGENTS.md"
        agents.write_text(agents.read_text(encoding="utf-8")
                          + f"\nwired from {home}/brain on this machine\n",
                          encoding="utf-8")
        out = self._publish(home=home)
        self.assertEqual(out.returncode, 1,
                         "an absolute home path shipped:\n" + out.stdout)
        self.assertIn("absolute path", out.stdout,
                      "the path leak was not named as such:\n" + out.stdout)

    def test_the_license_and_install_doc_may_keep_the_publishers_name(self):
        """A copyright line has to name a person and the install doc has to name
        the repo people install FROM — the check must not make those impossible."""
        subprocess.run(["git", "remote", "add", "origin",
                        f"https://github.com/{self.HANDLE}/brain.git"],
                       cwd=self.repo, check=True, capture_output=True)
        (self.repo / "LICENSE").write_text(
            f"MIT License\n\nCopyright (c) 2026 A Person ({self.HANDLE})\n", encoding="utf-8")
        self.generate()
        self.assertIn(self.HANDLE, (self.dest / "LICENSE").read_text(encoding="utf-8"))

    def test_this_machines_scheduler_lock_does_not_ship(self):
        """It carries this session's UUID and pid. .gitignore keeps it out of a
        commit, but the scrub must not depend on the emitted tree being a git
        repo — at emit time it is not one yet."""
        lock = self.repo / ".claude" / "scheduled_tasks.lock"
        lock.parent.mkdir(exist_ok=True)
        lock.write_text('{"session":"11111111-2222-3333-4444-555555555555","pid":4242}\n',
                        encoding="utf-8")
        self.generate()
        self.assertFalse((self.dest / ".claude" / "scheduled_tasks.lock").exists(),
                         "this machine's session UUID shipped in the template")

    def test_refuses_to_overwrite_a_non_empty_destination(self):
        self.dest.mkdir(parents=True)
        (self.dest / "something-important.txt").write_text("do not lose me\n", encoding="utf-8")
        out = run_brain("template", str(self.dest), repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertTrue((self.dest / "something-important.txt").exists(),
                        "template generation clobbered an existing directory")

    # --force reaches for rmtree, so every overlap refusal has to fire BEFORE it
    # does. These four spellings each used to delete a real tree and only then
    # print the refusal, or (the descendant cases) never refuse at all.
    def assert_refused_intact(self, target, canary):
        out = run_brain("template", str(target), "--force", repo=self.repo)
        self.assertEqual(out.returncode, 1,
                         f"template accepted an overlapping destination {target}:\n" + out.stdout)
        self.assertIn("refusing", out.stdout.lower())
        self.assertTrue(canary.exists(),
                        f"--force destroyed {canary} before refusing {target}")

    def test_force_refuses_this_repo_before_deleting_it(self):
        self.assert_refused_intact(self.repo, self.repo / "bin" / "brain")

    def test_force_refuses_a_parent_of_this_repo_before_deleting_it(self):
        self.assert_refused_intact(self.repo.parent, self.repo / "bin" / "brain")

    def test_force_refuses_a_folder_inside_this_repo(self):
        """The worst spelling: knowledge/ is not an ancestor of the repo, so the
        upward-only check never fired and --force wiped every note."""
        self.assert_refused_intact(self.repo / "knowledge",
                                   self.repo / "knowledge" / "index.md")

    def test_force_refuses_an_unresolved_path_that_lands_inside_this_repo(self):
        self.assert_refused_intact(self.repo / "bin" / ".." / "knowledge",
                                   self.repo / "knowledge" / "index.md")


class ConnectTests(unittest.TestCase):
    """`brain connect` is the universal half of the tool layer: one server, many
    spellings. The spellings disagree in ways that fail SILENTLY — a wrong
    top-level key produces a server that never appears and never says why — so
    what is asserted here is that each client gets its own, correct one."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def connect(self, *args):
        out = run_brain("connect", *args, repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def test_every_client_gets_the_same_server_command(self):
        """The whole claim of the universal tool layer: one unmodified stdio
        server, consumed by everything. If a client needed its own build, the
        claim would be false."""
        server = str(self.repo / "bin" / "brain-mcp")
        out = self.connect("--all")
        module = load_brain_module()
        for key, client in module.CONNECT_CLIENTS.items():
            with self.subTest(client=key):
                self.assertIn(client["name"], out)
                if client["format"]:
                    self.assertIn(server, out, f"{key} was not given the server path")

    def test_each_config_format_is_syntactically_valid(self):
        """A snippet that does not parse is worse than no snippet: the client
        usually discards the whole file rather than the bad entry."""
        import json as _json
        module = load_brain_module()
        module.ROOT = self.repo
        server = str(self.repo / "bin" / "brain-mcp")
        for name, template in module.CONNECT_FORMATS.items():
            with self.subTest(fmt=name):
                text = template.format(server=server)
                if name == "toml":
                    self.assertIn("[mcp_servers.brain]", text)
                    self.assertIn(f'command = "{server}"', text)
                else:
                    parsed = _json.loads(text)
                    key = "servers" if name == "json-servers" else "mcpServers"
                    self.assertIn(key, parsed, f"{name} used the wrong top-level key")
                    self.assertEqual(parsed[key]["brain"]["command"], server)
                    self.assertEqual(parsed[key]["brain"]["args"], [])

    def test_vscode_uses_servers_and_everyone_else_uses_mcpservers(self):
        """The single most common silent failure. VS Code's key is `servers`;
        pasting a `mcpServers` snippet there registers nothing, with no error."""
        module = load_brain_module()
        self.assertEqual(module.CONNECT_CLIENTS["vscode"]["format"], "json-servers")
        for key in ("gemini", "cursor", "windsurf", "claude-desktop"):
            self.assertTrue(module.CONNECT_CLIENTS[key]["format"].startswith("json-mcpServers"),
                            f"{key} must use the mcpServers key")
        self.assertEqual(module.CONNECT_CLIENTS["codex"]["format"], "toml")

    def test_every_writable_client_says_where_its_config_lives(self):
        """`--apply` cannot write to a sentence.

        The prose `config` field carries what a single path cannot — project
        vs global scope, "run this palette command", the Windows spelling — so
        it stays, and `path` is the machine-readable half beside it. A client
        with a config FORMAT is a client something can be written for, so it
        must have one.
        """
        module = load_brain_module()
        for key, client in module.CONNECT_CLIENTS.items():
            with self.subTest(client=key):
                if client["format"]:
                    self.assertIsNotNone(client.get("path"),
                                         f"{key} has a config format but no path")

    def test_every_client_with_a_routing_file_says_where_it_is(self):
        """Cursor and Claude Desktop have no global instruction file at all —
        their routing rule lives in a UI. Everyone else has a file, and
        `--routing --apply` needs to know which."""
        module = load_brain_module()
        for key, client in module.CONNECT_CLIENTS.items():
            with self.subTest(client=key):
                ui_only = client["routing"].startswith("NO FILE")
                self.assertEqual(ui_only, client.get("routing_path") is None,
                                 f"{key}: routing prose and routing_path disagree")

    def test_no_client_path_is_relative(self):
        """A relative path would resolve against the current directory, so
        `--apply` run from inside the brain would write another tool's config
        into the brain — tracked, committed and pushed."""
        module = load_brain_module()
        for key, client in module.CONNECT_CLIENTS.items():
            for field in ("path", "routing_path"):
                spec = client.get(field)
                specs = list(spec.values()) if isinstance(spec, dict) else [spec]
                for one in specs:
                    if one:
                        with self.subTest(client=key, field=field):
                            self.assertTrue(one.startswith(("~", "%")),
                                            f"{key}.{field} is not anchored to a home")

    def test_claude_desktop_has_no_path_on_linux(self):
        """It does not ship there. Inventing a Linux path would make detection
        report a client that cannot be present, and `--apply` create a file
        nothing will ever read."""
        module = load_brain_module()
        spec = module.CONNECT_CLIENTS["claude-desktop"]["path"]
        with mock.patch.object(module.osbackend, "os_family", return_value="linux"):
            self.assertIsNone(module.expand_client_path(spec))
        with mock.patch.object(module.osbackend, "os_family", return_value="macos"):
            resolved = module.expand_client_path(spec)
        self.assertIsNotNone(resolved)
        self.assertTrue(resolved.is_absolute())

    def test_what_apply_writes_is_what_the_snippet_says_to_paste(self):
        """Two renderings of one registration, and nothing held them together.

        A person who pastes the snippet and a person who runs `--apply` must
        end up with the same file. If they can differ, one of those two paths
        is instructions this project prints and never exercises — and the
        printed one is the fallback everything else here falls back TO.
        """
        module = load_brain_module()
        module.ROOT = self.repo
        server = str(self.repo / "bin" / "brain-mcp")
        for fmt, template in module.CONNECT_FORMATS.items():
            with self.subTest(fmt=fmt):
                kind, container, entry = module.server_entry(fmt)
                pasted = template.format(server=server)
                if kind == "json":
                    self.assertEqual(json.loads(pasted), {container: {"brain": entry}})
                else:
                    written = "\n".join(
                        [f"[{container}]"]
                        + [f"{k} = {json.dumps(v) if isinstance(v, str) else '[]'}"
                           for k, v in entry.items()])
                    self.assertEqual(written.strip(), pasted.strip())

    def test_an_unknown_client_is_refused_rather_than_guessed_at(self):
        out = run_brain("connect", "emacs", repo=self.repo)
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown client", out.stdout)

    def test_the_routing_block_carries_both_halves(self):
        """Retrieval AND capture. Ship only the first and you get a brain that
        answers but never grows — every session's reasoning lost at the moment
        it was worth keeping."""
        out = self.connect("--routing")
        self.assertIn("brain_search", out, "the retrieval half is missing")
        self.assertIn("worth saving to your brain?", out, "the capture half is missing")
        self.assertIn("Never capture a named person's private life", out,
                      "the ask-first privacy rule is missing")
        self.assertIn(str(self.repo), out, "the block was not rendered for this clone")
        self.assertNotIn("{{REPO}}", out)

    def test_ignore_files_cover_exactly_what_rgignore_covers(self):
        """Two lists of 'not current knowledge' that can drift apart is one list
        too many: the day they disagree, one client indexes archive/ as current
        and nothing anywhere reports it."""
        self.connect("--write-ignores")
        rgignore = {l.strip() for l in (self.repo / ".rgignore").read_text(encoding="utf-8")
                    .splitlines() if l.strip() and not l.startswith("#")}
        module = load_brain_module()
        for name in module.IGNORE_FILES:
            with self.subTest(ignore=name):
                path = self.repo / name
                self.assertTrue(path.exists(), f"{name} was not written")
                entries = {l.strip() for l in path.read_text(encoding="utf-8").splitlines()
                           if l.strip() and not l.startswith("#")}
                self.assertTrue(rgignore <= entries,
                                f"{name} is missing {sorted(rgignore - entries)}")

    def test_ignore_files_say_they_are_not_a_read_barrier(self):
        """They are not. Cursor's own docs: terminal and MCP tools 'cannot block
        access to code governed by .cursorignore'. A file that reads like a
        guarantee is worse than no file, because it stops the real rule from
        being followed."""
        self.connect("--write-ignores")
        body = (self.repo / ".cursorignore").read_text(encoding="utf-8")
        self.assertIn("BACKSTOP, not the control", body)
        self.assertIn("AGENTS.md", body)

    def test_writing_ignores_twice_changes_nothing(self):
        self.connect("--write-ignores")
        before = (self.repo / ".cursorignore").read_text(encoding="utf-8")
        second = self.connect("--write-ignores")
        self.assertIn("already up to date", second)
        self.assertEqual(before, (self.repo / ".cursorignore").read_text(encoding="utf-8"))


class ConnectApplyTests(unittest.TestCase):
    """`--apply` edits config files this system did not create.

    Every test here runs with HOME redirected into a temp directory. A test
    that touched the real ~/.cursor would be indistinguishable from the bug
    this command has to not have.
    """

    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.repo = make_sandbox(self.tmp.name)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.server = str(self.repo.resolve() / "bin" / "brain-mcp")

    def connect(self, *args):
        env = {**os.environ, "HOME": str(self.home), "USERPROFILE": str(self.home),
               "CLAUDE_CONFIG_DIR": str(self.home / ".claude"),
               "XDG_CONFIG_HOME": str(self.home / ".config")}
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "connect", *args],
            cwd=self.repo, capture_output=True, text=True, timeout=180, env=env)

    def cursor_config(self, text):
        (self.home / ".cursor").mkdir(parents=True, exist_ok=True)
        path = self.home / ".cursor" / "mcp.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_apply_merges_and_names_the_backup(self):
        path = self.cursor_config(json.dumps({
            "mcpServers": {"other": {"command": "/usr/local/bin/other", "args": []}}}))

        out = self.connect("cursor", "--apply")

        self.assertEqual(out.returncode, 0, out.stdout)
        parsed = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(parsed["mcpServers"]["brain"]["command"], self.server)
        self.assertEqual(parsed["mcpServers"]["other"]["command"],
                         "/usr/local/bin/other", "an unrelated server was destroyed")
        self.assertIn(".brain-backup-", out.stdout,
                      "the backup was made but never named, so nobody can find it")
        self.assertTrue(list((self.home / ".cursor").glob("*.brain-backup-*")))

    def test_re_applying_reports_unchanged_and_writes_nothing(self):
        self.cursor_config('{"mcpServers": {}}')
        self.connect("cursor", "--apply")
        before = (self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8")
        backups = len(list((self.home / ".cursor").glob("*.brain-backup-*")))

        out = self.connect("cursor", "--apply")

        self.assertIn("unchanged", out.stdout)
        self.assertEqual((self.home / ".cursor" / "mcp.json").read_text(encoding="utf-8"),
                         before)
        self.assertEqual(len(list((self.home / ".cursor").glob("*.brain-backup-*"))),
                         backups, "an unchanged run made another backup")

    def test_dry_run_shows_the_diff_and_writes_nothing(self):
        path = self.cursor_config('{"mcpServers": {}}')
        before = path.read_text(encoding="utf-8")

        out = self.connect("cursor", "--apply", "--dry-run")

        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(path.read_text(encoding="utf-8"), before)
        self.assertIn("brain-mcp", out.stdout, "the diff does not show the change")
        self.assertIn("+", out.stdout, "no diff was printed")

    def test_dry_run_without_apply_is_a_usage_error(self):
        # Without --apply, connect already only prints. A --dry-run that
        # silently did the same thing would read as confirmation that --apply
        # had been asked for and previewed.
        out = self.connect("cursor", "--dry-run")
        self.assertEqual(out.returncode, 2)

    def test_a_refusal_exits_nonzero_and_still_hands_over_the_snippet(self):
        path = self.cursor_config('{\n  // mine\n  "mcpServers": {}\n}\n')
        original = path.read_text(encoding="utf-8")

        out = self.connect("cursor", "--apply")

        self.assertEqual(out.returncode, 1,
                         "a refusal that exits 0 reads as a successful write")
        self.assertIn("refused", out.stdout)
        self.assertIn(self.server, out.stdout, "no snippet to paste instead")
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_all_apply_never_creates_config_for_a_client_that_is_absent(self):
        # Litter, and worse than litter: the next run would detect the client
        # as present because the directory it just made is there.
        self.cursor_config('{"mcpServers": {}}')

        out = self.connect("--all", "--apply")

        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertTrue((self.home / ".cursor" / "mcp.json").exists())
        self.assertFalse((self.home / ".codex").exists(),
                         "a config was created for a client that is not installed")
        self.assertFalse((self.home / ".codeium").exists())

    def test_naming_a_client_explicitly_is_consent_to_create_it(self):
        # The complement of the rule above. --all is a sweep and must not
        # invent clients; naming one is a person saying they want it.
        out = self.connect("codex", "--apply")
        self.assertEqual(out.returncode, 0, out.stdout)
        text = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.brain]", text)
        self.assertIn(self.server, text)


class ConnectRoutingApplyTests(ConnectApplyTests):
    """The routing block is the thing that makes an agent reach for the brain
    unprompted, and until now it was a paste job with no way back.

    Markers are what change that: the block can be updated in place as this
    system changes, and `brain retire` can take it out again. Without them,
    every machine that ever ran this keeps instructions pointing at a directory
    that is no longer there.
    """

    def test_the_printed_block_carries_the_markers(self):
        # Someone who pastes by hand must end up with the same thing --apply
        # writes, markers included, or retire can undo one and not the other.
        out = self.connect("--routing")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("<!-- brain:routing:start -->", out.stdout)
        self.assertIn("<!-- brain:routing:end -->", out.stdout)
        self.assertIn("brain_search", out.stdout)

    def test_apply_writes_the_block_and_keeps_the_users_own_rules(self):
        (self.home / ".claude").mkdir()
        rules = self.home / ".claude" / "CLAUDE.md"
        rules.write_text("# my rules\nalways use tabs\n", encoding="utf-8")

        out = self.connect("--routing", "--apply")

        self.assertEqual(out.returncode, 0, out.stdout)
        text = rules.read_text(encoding="utf-8")
        self.assertIn("always use tabs", text, "the user's own rules were destroyed")
        self.assertIn("brain_search", text)
        self.assertIn(str(self.repo.resolve()), text,
                      "the block was not rendered for this clone")

    def test_a_second_apply_leaves_exactly_one_block(self):
        (self.home / ".claude").mkdir()
        self.connect("--routing", "--apply")
        second = self.connect("--routing", "--apply")
        text = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- brain:routing:start -->"), 1)
        self.assertIn("unchanged", second.stdout)

    def test_a_client_with_no_routing_file_is_skipped_not_failed(self):
        # Cursor's routing rule lives in a settings UI. Nothing can write it,
        # and reporting that as a failure would make an exit code meaningless
        # on the machines where it is the normal case.
        (self.home / ".cursor").mkdir()
        out = self.connect("cursor", "--routing", "--apply")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("Settings", out.stdout, "the UI location was not printed")

    def test_the_copilot_file_is_created_with_its_frontmatter(self):
        # A .instructions.md with no applyTo matches nothing, so a block
        # written into a fresh one would sit there being ignored.
        (self.home / ".copilot").mkdir()
        out = self.connect("vscode", "--routing", "--apply")
        self.assertEqual(out.returncode, 0, out.stdout)
        text = (self.home / ".copilot" / "instructions"
                / "brain.instructions.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---"), text[:40])
        self.assertIn('applyTo: "**"', text)
        self.assertIn("<!-- brain:routing:start -->", text)

    def test_routing_apply_never_creates_files_for_an_absent_client(self):
        out = self.connect("--routing", "--apply")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".codex").exists())


class ConnectReportTests(ConnectApplyTests):
    """Bare `brain connect` was a catalogue of what this system supports. It
    becomes a report of what THIS machine is actually wired to, because the
    catalogue answers a question nobody has and the report answers the one
    everybody does."""

    def test_it_reports_what_is_here_not_what_exists(self):
        (self.home / ".cursor").mkdir()
        out = self.connect()
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("Cursor", out.stdout)
        self.assertIn("not wired", out.stdout)

    def test_a_client_wired_to_a_different_brain_is_called_out(self):
        """The case that silently breaks things today.

        A second clone, or a brain that moved, leaves an agent talking to a
        path that is not this one. Every tool call still succeeds — against
        somebody else's notes, or against nothing — and no health check
        anywhere has ever mentioned it.
        """
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps({
            "mcpServers": {"brain": {"command": "/somewhere/else/bin/brain-mcp",
                                     "args": []}}}), encoding="utf-8")

        out = self.connect()

        self.assertIn("DIFFERENT brain", out.stdout)
        self.assertIn("/somewhere/else/bin/brain-mcp", out.stdout,
                      "the report must name the path it is wired to instead")

    def test_json_is_the_contract_an_agent_reads_before_applying(self):
        (self.home / ".cursor").mkdir()
        self.connect("cursor", "--apply")

        out = self.connect("--json")

        payload = json_payload(out.stdout)
        self.assertEqual(payload["server"], self.server)
        cursor = payload["clients"]["cursor"]
        self.assertTrue(cursor["installed"])
        self.assertEqual(cursor["wired"], "this")
        self.assertIsNone(cursor["routing_path"], "cursor has no instruction file")
        codex = payload["clients"]["codex"]
        self.assertFalse(codex["installed"])
        self.assertEqual(codex["wired"], "no")

    def test_json_reports_whether_the_routing_block_is_in_place(self):
        (self.home / ".claude").mkdir()
        before = json_payload(self.connect("--json").stdout)
        self.assertFalse(before["clients"]["claude-code"]["routing_applied"])

        self.connect("--routing", "--apply")

        after = json_payload(self.connect("--json").stdout)
        self.assertTrue(after["clients"]["claude-code"]["routing_applied"])

    def test_an_unreadable_config_is_unknown_rather_than_not_wired(self):
        # JSONC parses for the client and not for us. Reporting it as "not
        # wired" would send somebody to fix wiring that is already correct.
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text(
            '{\n  // mine\n  "mcpServers": {}\n}\n', encoding="utf-8")
        payload = json_payload(self.connect("--json").stdout)
        self.assertEqual(payload["clients"]["cursor"]["wired"], "unknown")


class ProtocolBridgeTests(unittest.TestCase):
    """AGENTS.md is the single instruction source; CLAUDE.md imports it.

    The failure this guards against is silent and slow: someone edits the
    protocol in CLAUDE.md, AGENTS.md drifts, and every non-Claude agent starts
    working from a stale copy of the rules while the tests stay green."""

    def test_agents_md_is_the_protocol_and_claude_md_imports_it(self):
        agents = ROOT / "AGENTS.md"
        claude = ROOT / "CLAUDE.md"
        self.assertTrue(agents.exists(), "AGENTS.md is the canonical protocol file")
        self.assertTrue(claude.exists(), "CLAUDE.md is the Claude Code bridge")
        bridge = claude.read_text(encoding="utf-8")
        self.assertRegex(bridge, r"(?m)^@AGENTS\.md\s*$",
                         "CLAUDE.md must import AGENTS.md on its own line — without "
                         "the import, Claude Code sees only the addendum")
        body = agents.read_text(encoding="utf-8")
        for section in ("## Note contract", "## What earns a note",
                        "## Superseding", "## Searching", "## Hard rules"):
            self.assertIn(section, body, f"the protocol lost its {section!r} section")

    def test_the_bridge_holds_no_protocol_of_its_own(self):
        """CLAUDE.md carries wiring, never rules. A protocol section here is a
        second source of truth by definition."""
        bridge = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for owned_by_agents_md in ("## Note contract", "## What earns a note",
                                   "## Superseding", "## Searching",
                                   "## Hard rules"):
            self.assertNotIn(owned_by_agents_md, bridge,
                             f"{owned_by_agents_md!r} belongs in AGENTS.md only — "
                             "duplicating it here lets the two drift apart")

    # Everything an agent is handed AS INSTRUCTION. These are the files where a
    # "the protocol is in CLAUDE.md" pointer does damage: a non-Claude agent
    # follows it to a file that is mostly an import line and works from the
    # addendum alone.
    #
    # Deliberately NOT the whole repo. bin/brain names CLAUDE.md legitimately —
    # its connect registry records which file each client reads — and a sweep
    # that cannot tell a pointer from a path would either fail on that or have
    # to exempt the toolbelt wholesale.
    INSTRUCTION_SURFACES = (
        "setup/consolidate-prompt.md", "setup/audit-prompt.md",
        "setup/skills/brain/SKILL.md.template", "setup/templates/routing-block.md",
        "setup/templates/project.md", "README.md",
        "knowledge/index.md", "knowledge/reference/note-conventions.md",
    )

    def test_nothing_an_agent_reads_still_points_at_claude_md(self):
        """decisions/ and archive/ are exempt on purpose: they record what was
        true when written, and rewriting them would forge the record."""
        checked, offenders = 0, []
        for rel in self.INSTRUCTION_SURFACES:
            path = ROOT / rel
            self.assertTrue(path.exists(), f"{rel} moved — update this list, do not "
                            "let the check quietly stop covering it")
            checked += 1
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "CLAUDE.md" in line:
                    offenders.append(f"{rel}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "protocol pointers left on CLAUDE.md:\n"
                         + "\n".join(offenders))
        self.assertEqual(checked, len(self.INSTRUCTION_SURFACES))

    def test_live_knowledge_notes_point_at_the_protocol_file_that_exists(self):
        """Same rule for current notes, which archive/ and decisions/ escape."""
        offenders = []
        for path in (K / "index.md", K / "reference" / "note-conventions.md"):
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if "CLAUDE.md" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class ResetTests(unittest.TestCase):
    """`brain reset` is the only command here that can lose work, so every layer
    of its guard stack is asserted rather than trusted.

    All of it runs against sandboxes with sandbox remotes. Nothing in this class
    may ever reach the real brain, the real launchd jobs, or the real
    ~/.claude/skills symlink."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(self.remote)], check=True)
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote)],
                       cwd=self.repo, check=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "main"],
                       cwd=self.repo, check=True, capture_output=True)
        self.module = load_brain_module()
        self.module.ROOT = self.repo
        self.module.K = self.repo / "knowledge"
        self.real_is_local = self.module.is_local_remote

    def tearDown(self):
        self.module.is_local_remote = self.real_is_local
        cleanup_temp(self.tmp)

    def blockers(self):
        """Blockers with the locality test stood down.

        The only remote a test can genuinely fetch from is a path on this
        machine — which reset correctly refuses as "a copy, not a backup", and
        would refuse FIRST, masking every other blocker. That refusal has its
        own test below, where the real function runs."""
        self.module.is_local_remote = lambda _url: False
        try:
            return self.module.reset_blockers()
        finally:
            self.module.is_local_remote = self.real_is_local

    def test_a_clean_fully_pushed_brain_has_no_blockers(self):
        """The baseline. If this fails the other assertions prove nothing —
        they would all be passing for the wrong reason."""
        self.assertEqual(self.blockers(), [])

    def test_it_refuses_without_a_terminal(self):
        """The layer that stops an agent shelling out to it. bin/brain-mcp's
        tool list is a whitelist so omission already covers the MCP surface;
        this covers everything else."""
        out = subprocess.run([sys.executable, str(self.repo / "bin" / "brain"), "reset"],
                             cwd=self.repo, input="", capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 1)
        self.assertIn("refuses to run without a terminal", out.stdout)
        self.assertTrue((self.repo / "bin" / "brain").exists(),
                        "a non-interactive invocation touched the repo")

    def test_a_dirty_tree_blocks(self):
        (self.repo / "knowledge" / "inbox" / "wip.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nunsaved\n", encoding="utf-8")
        self.assertIn("working tree is not clean", " ".join(self.blockers()))

    def test_a_stash_blocks(self):
        (self.repo / "README.md").write_text("changed\n", encoding="utf-8")
        subprocess.run(["git", "stash", "-q"], cwd=self.repo, check=True, capture_output=True)
        self.assertIn("stash", " ".join(self.blockers()))

    def test_an_unpushed_commit_on_any_branch_blocks(self):
        """Not just the current branch. A side branch left on a laptop is
        exactly what a fresh start must not silently eat."""
        subprocess.run(["git", "checkout", "-q", "-b", "side"], cwd=self.repo, check=True)
        (self.repo / "knowledge" / "inbox" / "side.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nside work\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "side"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        found = " ".join(self.blockers())
        self.assertIn("side", found)
        self.assertIn("only here", found)

    def test_a_branch_whose_upstream_was_deleted_blocks(self):
        """rev-list against a pruned upstream ERRORS; the plain git() helper
        swallows that as '', which reads as zero-ahead — a false 'all pushed'
        all-clear for a branch whose backup can no longer be verified."""
        subprocess.run(["git", "checkout", "-q", "-b", "wip"], cwd=self.repo, check=True)
        (self.repo / "knowledge" / "inbox" / "w.md").write_text(
            "---\ncreated: 2026-07-25\nstatus: draft\n---\n\nwip\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "wip"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "wip"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        # Delete the branch on the remote; wip.merge still names refs/heads/wip.
        subprocess.run(["git", "push", "-q", "origin", "--delete", "wip"],
                       cwd=self.repo, check=True, capture_output=True)
        found = " ".join(self.blockers())     # blockers() fetches --prune first
        self.assertIn("wip", found)
        self.assertIn("no longer exists on the remote", found,
                      "a branch tracking a deleted upstream passed as all-pushed")

    def test_an_unreviewed_consolidation_branch_blocks(self):
        """Machine-written knowledge nobody has read. Retiring the brain around
        it would discard it under the banner of a clean start."""
        subprocess.run(["git", "checkout", "-q", "-b", "consolidate/2026-07-24"],
                       cwd=self.repo, check=True)
        (self.repo / "knowledge" / "inbox" / "prop.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nproposed\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "consolidate: test"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "push", "-q", "-u", "origin", "consolidate/2026-07-24"],
                       cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "checkout", "-q", "main"], cwd=self.repo, check=True)
        self.assertIn("unreviewed consolidation", " ".join(self.blockers()))

    def test_a_detached_head_blocks(self):
        """A commit reachable only from a detached HEAD sits on no branch ref,
        so the per-branch loop never inspects it and `git push` never sent it.
        Refuse rather than reason about whether it is backed up."""
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-q", head], cwd=self.repo, check=True)
        self.assertIn("HEAD is detached", " ".join(self.blockers()))

    def test_a_local_path_remote_blocks(self):
        """The real locality test, unpatched: origin already IS a path on this
        machine, so a copy must not be mistaken for a backup."""
        self.assertIn("not a backup", " ".join(self.module.reset_blockers()))

    def test_no_remote_at_all_blocks(self):
        subprocess.run(["git", "remote", "remove", "origin"], cwd=self.repo, check=True)
        found = " ".join(self.blockers())
        self.assertIn("no git remote", found)
        self.assertIn("only a loss", found)

    def test_an_unreachable_remote_blocks_rather_than_assuming_pushed(self):
        """The reason this fetches instead of reading doctor's cached state:
        'already pushed', read from a ref that is days old, is the one wrong
        answer that costs everything."""
        shutil.rmtree(self.remote)
        found = " ".join(self.blockers())
        self.assertIn("could not reach the remote", found)
        self.assertIn("Unverifiable is not the same as backed up", found)

    def test_the_phrase_is_computed_from_live_state(self):
        """A stale phrase — pasted from a runbook, a chat, or a previous
        attempt — must stop matching the moment the brain changes."""
        before = self.module.reset_phrase()
        self.assertIn("retire", before)
        self.assertIn("history stays at", before,
                      "the phrase must name where the history keeps living")
        self.assertIn("remote", before)
        (self.repo / "knowledge" / "reference" / "one-more.md").write_text(
            "---\nid: one-more\nkind: reference\ntitle: One more\ntopics: [brain]\n"
            "aliases: [one more]\ncreated: 2026-07-24\nstatus: current\nreview_by: null\n"
            "---\n\nAnother note.\n", encoding="utf-8")
        self.assertNotEqual(self.module.reset_phrase(), before,
                            "the phrase did not change when the note count did")

    def dewire_against(self, link_target, home_name):
        """Run reset_dewire() with HOME and PATH redirected into the sandbox.

        PATH is emptied rather than shutil.which patched: reset_dewire imports
        shutil locally, and an emptied PATH is also a truer stand-in for a
        machine with no `claude` CLI. cmd_schedule is stubbed outright — a test
        must never be able to unload the real launchd jobs."""
        fake_home = Path(self.tmp.name) / home_name
        (fake_home / ".claude" / "skills").mkdir(parents=True)
        link = fake_home / ".claude" / "skills" / "brain"
        link.symlink_to(link_target)
        real_home = self.module.Path.home
        real_schedule = self.module.cmd_schedule
        real_path = os.environ.get("PATH", "")
        try:
            self.module.Path.home = staticmethod(lambda: fake_home)
            self.module.cmd_schedule = lambda _argv: 1
            os.environ["PATH"] = str(Path(self.tmp.name) / "nothing-here")
            return link, self.module.reset_dewire()
        finally:
            os.environ["PATH"] = real_path
            self.module.cmd_schedule = real_schedule
            self.module.Path.home = real_home

    def test_a_foreign_skill_symlink_is_reported_not_unlinked(self):
        """The incident this check exists for actually happened: `brain init`
        run from a scratch checkout re-pointed the global skill symlink. A reset
        that unlinked whatever it found would silently disconnect a DIFFERENT
        brain from its tools."""
        other = Path(self.tmp.name) / "someone-elses-brain"
        other.mkdir()
        link, (_done, preserved) = self.dewire_against(other, "home")
        self.assertTrue(link.is_symlink(), "a foreign skill symlink was removed")
        self.assertIn("NOT this clone", " ".join(preserved))

    def test_our_own_skill_symlink_is_unlinked(self):
        link, (done, _preserved) = self.dewire_against(
            self.repo / "setup" / "skills" / "brain", "home2")
        self.assertFalse(link.is_symlink(), "this clone's own symlink was left behind")
        self.assertIn("unlinked", " ".join(done))

    def test_the_preserved_report_needs_nothing_that_still_exists(self):
        """It prints AFTER the repo has moved aside, when ROOT is gone and every
        git call from cwd=ROOT raises. The first real end-to-end run crashed
        here — a traceback at the exact moment the operator most needs to be
        told nothing was lost."""
        gone = Path(self.tmp.name) / "definitely-not-here"
        self.assertFalse(gone.exists())
        real_root, real_git = self.module.ROOT, self.module.git

        def exploding_git(*_args, **_kw):
            raise AssertionError("the preserved report called git after the move")

        try:
            self.module.ROOT = gone
            self.module.git = exploding_git
            lines = self.module.preserved_report("https://example.invalid/x/brain.git",
                                                 gone.with_name("brain.retired-x"),
                                                 gone.with_name("backup.bundle"))
        finally:
            self.module.ROOT, self.module.git = real_root, real_git
        joined = " ".join(lines)
        self.assertIn("example.invalid/x/brain.git", joined,
                      "the report must name where the history still lives")
        self.assertIn("vault-key", joined, "the vault key preservation went unsaid")
        self.assertIn("brain.retired-x", joined)
        self.assertIn("backup.bundle", joined)

    def test_reset_is_not_reachable_over_mcp(self):
        """The tool list is a whitelist, so omission is the control. Asserted
        anyway: a future edit that adds tools by iterating over commands would
        hand every agent a wipe."""
        out = subprocess.run([sys.executable, str(self.repo / "bin" / "brain-mcp")],
                             input=json.dumps({"jsonrpc": "2.0", "id": 1,
                                               "method": "tools/list"}) + "\n",
                             cwd=self.repo, capture_output=True, text=True, timeout=120)
        names = {t["name"] for t in json.loads(out.stdout.splitlines()[0])["result"]["tools"]}
        self.assertNotIn("brain_reset", names)
        for name in names:
            self.assertNotIn("reset", name)


class DoctorExitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_no_remote_makes_doctor_exit_nonzero(self):
        """doctor is the backup alarm: a repo with no remote is unbacked, so
        doctor must exit non-zero so the scheduled watchdog notifies. (A fresh
        sandbox has no origin.)"""
        out = run_brain("doctor", repo=self.repo)
        self.assertNotEqual(out.returncode, 0,
                            "an unbacked repo (no remote) reported healthy:\n" + out.stdout)
        self.assertIn("no git remote", out.stdout)


def load_brain_module():
    """bin/brain executed as a module, so pure functions can be tested directly."""
    import importlib.util
    spec = importlib.util.spec_from_loader("brainmod", loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__file__ = str(BRAIN)
    exec(compile(BRAIN.read_text(encoding="utf-8"), "brain", "exec"), module.__dict__)
    return module


class CaptureSecretTests(unittest.TestCase):
    """A capture without --commit (the CLI default) used to reach disk without
    ever meeting scan_secrets: the only scan was --commit -> pre-commit ->
    lint --staged. So the cheapest, most-used write path was the unguarded one."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.inbox = self.repo / "knowledge" / "inbox"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def inbox_files(self):
        return list(self.inbox.glob("*.md")) if self.inbox.exists() else []

    def test_credential_capture_is_refused_before_the_write(self):
        before = set(self.inbox_files())
        out = run_brain("capture", "deploy key is ghp_" + "A" * 36, repo=self.repo)
        self.assertEqual(out.returncode, 1, "a credential capture was accepted\n" + out.stdout)
        self.assertIn("REFUSED", out.stdout)
        self.assertEqual(set(self.inbox_files()), before,
                         "the credential reached disk despite the refusal")

    def test_ordinary_capture_still_works(self):
        before = set(self.inbox_files())
        out = run_brain("capture", "Postgres over SQLite for the sync layer", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        added = set(self.inbox_files()) - before
        self.assertEqual(len(added), 1, "the capture did not land")

    def test_refusal_applies_without_commit(self):
        """The scan must run on the plain write path, not only under --commit."""
        before = set(self.inbox_files())
        out = run_brain("capture", "aws key AKIA" + "B" * 16, repo=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertEqual(set(self.inbox_files()), before,
                         "unscanned credential written to inbox/")


class SearchCurrencyTests(unittest.TestCase):
    """`brain read` has always warned on a draft or a passed review_by; search
    never did — so the ranked list, which is what a caller actually answers
    from, showed a stale note identically to a settled one."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.ref = self.repo / "knowledge" / "reference"
        self.ref.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write(self, name, extra=""):
        (self.ref / f"{name}.md").write_text(
            f"---\nid: {name}\nkind: reference\ntitle: {name}\ntopics: [brain]\n"
            f"aliases: [{name}]\ncreated: 2026-07-01\nstatus: current\n{extra}---\n\n"
            "The vendor rate limit is 500 requests per minute.\n", encoding="utf-8")

    def test_passed_review_by_is_marked_stale_in_search(self):
        self.write("perishable", extra="review_by: 2020-01-01\n")
        out = run_brain("search", "vendor rate limit", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("perishable", out.stdout, "the note did not rank at all")
        self.assertIn("STALE", out.stdout,
                      "a note whose review_by passed was served as current:\n" + out.stdout)

    def test_future_review_by_is_not_marked_stale(self):
        self.write("fresh", extra="review_by: 2099-01-01\n")
        out = run_brain("search", "vendor rate limit", repo=self.repo)
        self.assertIn("fresh", out.stdout)
        self.assertNotIn("STALE", out.stdout, "a note still in date was flagged stale")

    def test_recent_tags_provisional_inbox_rows(self):
        """recent is the surface reached for by 'what was I just working on',
        and it listed a raw capture and a settled decision in one column."""
        run_brain("capture", "a provisional thought about vendor limits", repo=self.repo)
        out = run_brain("recent", "--days", "2", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        inbox_rows = [l for l in out.stdout.splitlines() if "knowledge/inbox/" in l]
        self.assertTrue(inbox_rows, "the capture did not show in recent")
        for row in inbox_rows:
            self.assertIn("provisional", row, "an inbox row was listed as settled: " + row)


class SensitivityDefaultTests(unittest.TestCase):
    """FOLDER_KIND maps life -> note, so keying the default off `kind` gave
    knowledge/life/ — the folder the protocol names as requiring classification
    — the LEAST protective default, silently and while linting clean."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)

    def tearDown(self):
        cleanup_temp(self.tmp)

    def test_life_notes_default_to_personal(self):
        out = run_brain("new", "note", "How I handle mornings", "--topics", "brain",
                        repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        written = list((self.repo / "knowledge" / "life").glob("*.md"))
        self.assertEqual(len(written), 1, "the note did not land in life/")
        self.assertIn("sensitivity: personal", written[0].read_text(encoding="utf-8"),
                      "a life/ note defaulted to the least protective sensitivity")

    def test_reference_notes_are_not_marked_personal(self):
        """Only people/ and life/ carry sensitivity at all — the reference
        template has no such field, and the new default must not add one."""
        out = run_brain("new", "reference", "How the indexer works", "--topics", "brain",
                        repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        written = [p for p in (self.repo / "knowledge" / "reference").glob("*.md")
                   if "indexer" in p.name]
        self.assertEqual(len(written), 1)
        self.assertNotIn("sensitivity: personal", written[0].read_text(encoding="utf-8"))


class SessionMiningTests(unittest.TestCase):
    """Mining feeds consolidation, which writes permanent knowledge. Anything
    that reaches the digest can become canonical, so what it EXCLUDES is a
    correctness property, not a formatting nicety."""

    def setUp(self):
        self.module = load_brain_module()
        self.tmp = temp_dir()

    def tearDown(self):
        cleanup_temp(self.tmp)

    def transcript(self, name, records):
        import json
        path = Path(self.tmp.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return path

    def user_turn(self, text, ts="2026-07-24T10:00:00Z"):
        return {"type": "user", "timestamp": ts, "cwd": "/x",
                "message": {"role": "user", "content": text}}

    def test_orchestrator_payloads_are_not_mined_as_user_prompts(self):
        """Workflow/subagent returns pasted back by an orchestrator arrive as
        TOP-LEVEL user turns, so isSidechain does not exclude them. Mined, they
        launder assistant prose into the user's recorded fact — under a header
        that promises assistant output is excluded."""
        path = self.transcript("a.jsonl", [
            self.user_turn("I decided to use Postgres over SQLite."),
            self.user_turn('<result>{"final":"The agent concluded X is true."}</result>'),
            self.user_turn('{"final":"another agent payload"}'),
            self.user_turn("[SYSTEM NOTIFICATION - NOT USER INPUT]\nbackground task done"),
        ])
        turns, _cwd = self.module.__dict__["_human_turns"](path)
        texts = [t for _ts, t in turns]
        self.assertEqual(texts, ["I decided to use Postgres over SQLite."],
                         "a machine payload was mined as a user prompt: " + repr(texts))

    def test_repeated_turns_are_deduplicated_across_sessions(self):
        """A resumed session replays its earlier turns into a new transcript,
        so the window check (which gates the FILE) emitted the same prompt
        every week. Dedupe by content, not by per-turn timestamp — filtering by
        timestamp would sever recent turns from the older ones they refer to."""
        module = self.module
        root = Path(self.tmp.name) / "projects"
        (root / "p").mkdir(parents=True, exist_ok=True)
        import json
        shared = "I want icons, never emojis, in every UI."
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600))
        for n in ("one.jsonl", "two.jsonl"):
            (root / "p" / n).write_text("\n".join(json.dumps(r) for r in [
                {"type": "user", "timestamp": now, "cwd": "/x",
                 "message": {"role": "user", "content": shared}},
                {"type": "user", "timestamp": now, "cwd": "/x",
                 "message": {"role": "user", "content": f"unique to {n}"}},
            ]), encoding="utf-8")
        module.__dict__["TRANSCRIPT_ROOT"] = root
        text, _kept = module.__dict__["session_digest"](days=8)
        self.assertEqual(text.count(shared), 1,
                         "a replayed turn was mined twice:\n" + text)
        self.assertIn("unique to one.jsonl", text, "dedupe discarded a distinct turn")
        self.assertIn("unique to two.jsonl", text, "dedupe discarded a distinct turn")


class ConsolidationAuditTests(unittest.TestCase):
    """The consolidation audit is the one place this system spends a second
    agent. It exists because the pass that reads untrusted session transcripts
    is the same pass that decides what becomes permanent — so what is asserted
    here is the BOUNDARY, not the auditor's taste: it must fail closed, and it
    must not be able to see the digest."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        for cmd in (["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "base", "--allow-empty"],
                    ["git", "branch", "-M", "main"],
                    ["git", "checkout", "-q", "-b", "consolidate/test"]):
            subprocess.run(cmd, cwd=self.repo, check=True, capture_output=True)
        self.module = load_brain_module()
        self.module.ROOT = self.repo
        self.module.K = self.repo / "knowledge"
        self.module.INDEX_DB = self.repo / ".cache" / "index.db"
        self.runner = self.module.load_consolidator()
        self.calls = []

    def tearDown(self):
        cleanup_temp(self.tmp)

    def stage_a_change(self):
        (self.repo / "knowledge" / "inbox" / "audit-fixture.md").write_text(
            "---\ncreated: 2026-07-24\nstatus: draft\n---\n\nA staged change.\n",
            encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)

    def fake_claude(self, stdout="", returncode=0, record_digest=False):
        """Intercept only the auditor spawn; let real git calls through."""
        real_run = subprocess.run
        test = self

        def runner(cmd, *a, **kw):
            if cmd and cmd[0] == "claude":
                test.calls.append(cmd)
                if record_digest:
                    test.digest_visible_during_audit = test.module.digest_path().exists()
                return subprocess.CompletedProcess(cmd, returncode, stdout, "")
            return real_run(cmd, *a, **kw)
        return runner

    def test_pass_verdict_allows_the_commit(self):
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude(
            "Checked both notes. Nothing private, no contradiction.\nVERDICT: PASS")
        verdict, _detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "pass")

    def test_block_verdict_stops_the_pass(self):
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude(
            "knowledge/people/x.md records a diagnosis.\n"
            "VERDICT: BLOCK — names a person's health condition in plaintext")
        verdict, detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "block")
        self.assertIn("health condition", detail)

    def test_missing_verdict_fails_closed(self):
        """An auditor that rambles without deciding must not be read as consent.
        The work survives on the branch; an unaudited push does not come back."""
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude("I looked at the diff. Seems fine!")
        verdict, detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "error", "a missing verdict was treated as a pass")
        self.assertIn("no VERDICT", detail)

    def test_auditor_crash_fails_closed(self):
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude("", returncode=1)
        verdict, _detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "error", "a crashed auditor was treated as a pass")

    def test_digest_is_absent_while_the_auditor_runs(self):
        """Blindness must be a fact of the filesystem, not a line in a prompt:
        the auditor runs with cwd=ROOT and could otherwise just read .cache/."""
        self.stage_a_change()
        digest = self.module.digest_path()
        digest.parent.mkdir(parents=True, exist_ok=True)
        digest.write_text("# Session digest\n\nraw untrusted transcript\n", encoding="utf-8")
        self.digest_visible_during_audit = None
        self.module.subprocess.run = self.fake_claude("VERDICT: PASS", record_digest=True)
        self.module.audit_changes(self.runner)
        self.assertIs(self.digest_visible_during_audit, False,
                      "the auditor could read the session digest it must be blind to")
        self.assertTrue(digest.exists(), "the digest was not restored after the audit")

    def test_no_staged_changes_passes_without_spawning(self):
        """A pass that changed nothing must not burn a model call."""
        verdict, _detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "pass")
        self.assertEqual(self.calls, [], "the auditor was spawned with nothing to audit")

    def test_unreadable_diff_fails_closed(self):
        """The plain git() helper returns '' on failure, which is indistinguishable
        from 'nothing changed'. Reading a broken diff as an empty one would skip
        the audit and report a pass — the exact inversion of a safety gate."""
        self.stage_a_change()
        subprocess.run(["git", "branch", "-D", "main"], cwd=self.repo,
                       check=True, capture_output=True)   # no main to diff against
        self.module.subprocess.run = self.fake_claude("VERDICT: PASS")
        verdict, detail = self.module.audit_changes(self.runner)
        self.assertEqual(verdict, "error", "an unreadable diff was reported as a pass")
        self.assertIn("staged diff", detail)
        self.assertEqual(self.calls, [], "the auditor was spawned without a diff")

    def test_post_commit_never_pushes_a_consolidate_branch(self):
        """The consolidation prompt tells the pass to commit its own output. If
        post-commit pushed that commit, unaudited machine-written knowledge would
        reach a public remote BEFORE the auditor ever ran — the gate bypassed by
        its own author, silently. Asserted against the hook itself, because the
        hole is invisible from the Python side."""
        hook = (self.repo / ".githooks" / "post-commit").read_text(encoding="utf-8")
        self.assertIn("consolidate/*", hook,
                      "post-commit has no guard against pushing a consolidate branch")
        # Behavioural, not just textual: run the hook with a `git` shim that
        # records every invocation, on a consolidate branch with a remote that
        # WOULD otherwise be pushed to, and prove `push` was never reached.
        shim_dir = Path(self.tmp.name) / "shim"
        shim_dir.mkdir(exist_ok=True)
        log = Path(self.tmp.name) / "git-calls.log"
        real_git = shutil.which("git")
        (shim_dir / "git").write_text(
            f'#!/bin/sh\necho "$@" >> {log}\nexec {real_git} "$@"\n', encoding="utf-8")
        (shim_dir / "git").chmod(0o755)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/example/not-real.git"],
                       cwd=self.repo, check=True, capture_output=True)
        env = dict(os.environ, PATH=f"{shim_dir}:{os.environ['PATH']}")
        subprocess.run(["sh", ".githooks/post-commit"], cwd=self.repo, env=env,
                       capture_output=True, timeout=60)
        # The hook does its work in a detached subshell, so the parent returns
        # first; wait for the shim to actually record something.
        deadline = time.time() + 30
        calls = ""
        while time.time() < deadline:
            calls = log.read_text(encoding="utf-8") if log.exists() else ""
            if "rev-parse" in calls:
                time.sleep(1)              # let any push attempt land in the log too
                calls = log.read_text(encoding="utf-8")
                break
            time.sleep(0.5)
        self.assertIn("rev-parse", calls, "the shim never ran — the test proves nothing")
        self.assertNotIn("push", calls,
                         "post-commit tried to push from a consolidate branch:\n" + calls)

    def test_auditor_gets_no_write_permission(self):
        """The proposer runs with --permission-mode acceptEdits. If the auditor
        did too, the separation would be prompt wording rather than a boundary."""
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude("VERDICT: PASS")
        self.module.audit_changes(self.runner)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("acceptEdits", self.calls[0],
                         "the auditor was granted the same write permission as the author")

    def test_the_diff_is_actually_handed_over(self):
        """The auditor judges the artifact; if the diff never reached the prompt
        it would be rubber-stamping an empty page."""
        self.stage_a_change()
        self.module.subprocess.run = self.fake_claude("VERDICT: PASS")
        self.module.audit_changes(self.runner)
        prompt = self.calls[0][-1]
        self.assertIn("audit-fixture.md", prompt, "the staged diff was not in the prompt")
        self.assertIn("A staged change.", prompt)


class ConsolidatorConfigTests(unittest.TestCase):
    """The runner is pinned in config so it can move off Claude without a code
    change. What must NOT move with it is the audit boundary: the auditor's
    inability to write is a property of the specific tool, not of the command
    string, so porting the string alone would silently drop it."""

    def setUp(self):
        self.tmp = temp_dir()
        self.repo = make_sandbox(self.tmp.name)
        self.module = load_brain_module()
        self.module.ROOT = self.repo
        self.conf = self.repo / "setup" / "consolidator.conf"

    def tearDown(self):
        cleanup_temp(self.tmp)

    def write_conf(self, **fields):
        base = {"model": "test-model",
                "propose": "runner --model {model} --write -p",
                "audit": "runner --model {model} -p",
                "audit_cannot_write": "runner grants no write tools unless --write is passed"}
        base.update(fields)
        self.conf.write_text(
            "".join(f"{k} = {v}\n" for k, v in base.items() if v is not None),
            encoding="utf-8")

    def refusal(self):
        with self.assertRaises(self.module.ConsolidatorConfigError) as caught:
            self.module.load_consolidator()
        return str(caught.exception)

    def test_the_shipped_config_reproduces_todays_behaviour_exactly(self):
        """The whole point of moving the pin into config is that nothing else
        changes. If this drifts, an unattended weekly job changed silently."""
        runner = self.module.load_consolidator()
        self.assertEqual(runner["propose"],
                         ["claude", "--model", "claude-opus-4-8",
                          "--permission-mode", "acceptEdits", "-p"])
        self.assertEqual(runner["audit"], ["claude", "--model", "claude-opus-4-8", "-p"])
        self.assertEqual(runner["propose_timeout"], 1800)
        self.assertEqual(runner["audit_timeout"], 900)
        self.assertIn("consolidator.conf", runner["source"])

    def test_a_missing_config_falls_back_to_the_shipped_default(self):
        shipped = self.module.load_consolidator()
        self.conf.unlink()
        fallback = self.module.load_consolidator()
        self.assertEqual(fallback["propose"], shipped["propose"])
        self.assertEqual(fallback["audit"], shipped["audit"])
        self.assertEqual(fallback["source"], "built-in defaults")

    def test_identical_invocations_are_refused(self):
        """The refusal that matters: an auditor running the proposer's exact
        command has the proposer's exact write access, and the gate is theatre."""
        self.write_conf(propose="runner --write -p", audit="runner --write -p")
        self.assertIn("same command", self.refusal())

    def test_a_model_only_difference_is_still_refused(self):
        """Substitution happens before the comparison, so two invocations that
        differ only by an already-substituted {model} are the same command."""
        self.write_conf(propose="runner --model {model} -p",
                        audit="runner --model {model} -p")
        self.assertIn("same command", self.refusal())

    def test_a_model_carrying_flags_cannot_inject_write_access(self):
        """The subtle boundary break: a model field with flags is substituted
        into BOTH invocations, so propose != audit still holds, but the auditor
        silently gains --permission-mode acceptEdits. Refuse the multi-token
        model outright."""
        self.write_conf(model="claude-opus-4-8 --permission-mode acceptEdits")
        self.assertIn("single token", self.refusal())

    def test_a_multi_token_model_is_refused_however_it_is_spelled(self):
        """The conf format is `model = <token>`; a space makes it two tokens,
        which is the injection vector. Refuse it whether the second token is a
        flag or just noise — the single-token rule is the whole guarantee."""
        for bad in ("claude-opus-4-8 --permission-mode acceptEdits",
                    "claude opus", "claude; rm -rf /"):
            with self.subTest(model=bad):
                self.write_conf(model=bad)
                self.assertIn("single token", self.refusal())

    def test_the_model_substitutes_into_exactly_one_argument(self):
        """Structural guarantee: {model} is substituted per already-split token,
        so a single-token model lands in exactly one argv element and can never
        become a separate flag."""
        self.write_conf(model="some-model-v2.5")
        runner = self.module.load_consolidator()
        self.assertEqual(runner["audit"].count("some-model-v2.5"), 1)
        self.assertEqual(runner["audit"], ["runner", "--model", "some-model-v2.5", "-p"])
        self.assertNotIn("--permission-mode", " ".join(runner["audit"]))

    def test_an_unexplained_read_only_claim_is_refused(self):
        self.write_conf(audit_cannot_write="it just is")
        self.assertIn("audit_cannot_write", self.refusal())

    def test_a_missing_field_is_refused_rather_than_defaulted(self):
        """A half-edited config must not silently inherit Claude's settings —
        that is how someone ends up auditing with a runner they never chose."""
        for field in ("model", "propose", "audit", "audit_cannot_write"):
            with self.subTest(field=field):
                self.write_conf(**{field: None})
                self.assertIn(field, self.refusal())

    def test_a_nonsense_timeout_is_refused(self):
        self.write_conf(propose_timeout="soon")
        self.assertIn("propose_timeout", self.refusal())
        self.write_conf(propose_timeout="0")
        self.assertIn("positive", self.refusal())

    def test_a_multiline_explanation_is_read_as_one_value(self):
        self.conf.write_text(
            "model = m\npropose = runner --write -p\naudit = runner -p\n"
            "audit_cannot_write = the first line of the reason\n"
            "  continues on the second line and the third\n", encoding="utf-8")
        runner = self.module.load_consolidator()
        self.assertIn("continues on the second line", runner["audit_cannot_write"])

    def test_the_prompt_is_never_interpolated_into_the_command(self):
        """The prompt is appended as a final argv element, never formatted into
        a shell string — a note body containing backticks is not an injection."""
        runner = self.module.load_consolidator()
        for template in (runner["propose"], runner["audit"]):
            self.assertNotIn("{prompt}", " ".join(template))
            self.assertEqual(template[-1], "-p", "the prompt flag must come last, "
                             "so the prompt itself is the next argument")

    def test_consolidate_refuses_before_cutting_a_branch(self):
        """A bad runner config must fail on the ground — not after a branch is
        cut and a model has spent half an hour writing into it."""
        for cfg in (["user.email", "t@example.com"], ["user.name", "Test"]):
            subprocess.run(["git", "config", *cfg], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-q", "--no-verify", "--allow-empty",
                        "-m", "base"],
                       cwd=self.repo, check=True, capture_output=True)
        self.write_conf(propose="runner -p", audit="runner -p")
        out = run_brain("consolidate", repo=self.repo)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("refused", out.stdout)
        branches = subprocess.run(["git", "branch", "--list", "consolidate/*"],
                                  cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(branches.strip(), "",
                         "a branch was cut before the runner config was validated")


class PluginTests(unittest.TestCase):
    """The Claude Code plugin is wiring, not a second copy of the system. Its
    skill is generated from the same template `init` renders, and its MCP entry
    points at the toolbelt inside the USER'S brain — so the server and the notes
    it serves can never be different versions of each other."""

    def test_the_plugin_skill_has_not_drifted_from_the_template(self):
        """Two copies of the same instructions drift, and the one you are not
        looking at is the one that is wrong. Regenerating is one command; this
        makes forgetting it a test failure instead of a slow divergence."""
        out = run_brain("plugin", "check")
        self.assertEqual(out.returncode, 0,
                         "run `bin/brain plugin sync`:\n" + out.stdout)

    def test_the_plugin_does_not_ship_its_own_copy_of_the_server(self):
        plugin = ROOT / "plugins" / "brain"
        if not plugin.exists():
            self.skipTest("plugin not present in this tree")
        self.assertFalse((plugin / "bin" / "brain-mcp").exists(),
                         "the plugin shipped its own brain-mcp — it must exec the "
                         "one in the user's brain, or the two versions drift apart")

    def test_the_manifests_are_valid_json_with_the_fields_that_matter(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json")
                            .read_text(encoding="utf-8"))
        self.assertTrue(market.get("name"))
        self.assertTrue(market.get("owner", {}).get("name"))
        entries = {p["name"]: p for p in market["plugins"]}
        self.assertIn("brain", entries)
        self.assertTrue(entries["brain"].get("source"))

        plugin = json.loads((ROOT / "plugins" / "brain" / ".claude-plugin"
                             / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "brain")
        server = plugin["mcpServers"]["brain"]
        self.assertIn("${CLAUDE_PLUGIN_ROOT}", server["command"],
                      "the launcher must be addressed inside the installed plugin")
        self.assertEqual(server["env"]["BRAIN_PATH"], "${user_config.brain_path}",
                         "the server must be told where the user's brain is")
        self.assertTrue(plugin["userConfig"]["brain_path"]["required"],
                        "brain_path must be required — there is no sane default "
                        "for where someone keeps their knowledge")

    def test_the_launcher_expands_a_tilde_and_explains_a_missing_brain(self):
        """`${user_config.brain_path}` is substituted LITERALLY, so the shipped
        default "~/brain" reaches the launcher with the tilde intact. Unexpanded,
        it names a directory that cannot exist."""
        launcher = ROOT / "plugins" / "brain" / "bin" / "brain-mcp-launch"
        self.assertTrue(os.access(launcher, os.X_OK), "launcher is not executable")
        env = dict(os.environ, BRAIN_PATH="~/no-brain-here-" + "zzz")
        out = subprocess.run(["sh", str(launcher)], env=env, capture_output=True,
                             text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(str(Path.home()), out.stderr,
                      "the tilde was not expanded, so the path it reports is wrong")
        self.assertIn("/plugin configure brain", out.stderr,
                      "the error does not say how to fix it")


class StatsTests(unittest.TestCase):
    """`doctor` answers "is the machinery wired up". `stats` answers the harder
    one: is the KNOWLEDGE still findable, still growing, still curated. Those
    fail slowly and silently, so the signals themselves have to be asserted."""

    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.repo = make_sandbox(self.tmp.name)
        self.module = load_brain_module()
        self.module.ROOT = self.repo
        self.module.K = self.repo / "knowledge"
        self.module.INDEX_DB = self.repo / ".cache" / "index.db"
        self.module._DEFAULT_BRANCH = None

    def test_age_comes_from_git_not_file_mtime(self):
        """Every clone and checkout stamps mtime to now, so a brain copied to a
        second machine would report every note as written today — and every
        signal built on age would quietly reset to 'all healthy'."""
        rel = "knowledge/reference/note-conventions.md"
        target = self.repo / rel
        self.assertTrue(target.exists())
        os.utime(target, (0, 0))                      # mtime: 1970
        birthdays = self.module.note_birthdays()
        self.assertIn(rel, birthdays, "a committed note had no git birthday")
        self.assertNotEqual(birthdays[rel][:4], "1970",
                            "note age was read from mtime instead of git history")

    def test_findability_reports_a_note_search_cannot_return(self):
        """The quiet failure: the note exists, is correct, and is never
        returned — so the brain 'has' the answer and still cannot produce it."""
        self.module.do_search = lambda *a, **k: {"hits": [], "more": 0, "mode": "none"}
        snap = self.module.stats_snapshot()
        self.assertGreater(snap["findability"]["checked"], 0,
                           "nothing was checked, so a pass proves nothing")
        self.assertEqual(len(snap["findability"]["unfindable"]),
                         snap["findability"]["checked"],
                         "a search that returns nothing must mark every note unfindable")

    def test_findability_passes_when_search_returns_the_note(self):
        """Positive control for the test above — the check must be capable of
        passing, or asserting that it fails proves nothing."""
        def fake_search(query, scope="canonical", limit=8):
            meta = self.module.MetaIndex
            return {"hits": [{"id": i} for i in self._all_ids()], "more": 0, "mode": "all"}
        self.module.do_search = fake_search
        snap = self.module.stats_snapshot()
        self.assertEqual(snap["findability"]["unfindable"], [])

    def _all_ids(self):
        ids = []
        for path in (self.repo / "knowledge").rglob("*.md"):
            fm, _err = self.module.parse_frontmatter(
                path.read_text(encoding="utf-8", errors="replace"))
            if fm and fm.get("id"):
                ids.append(fm["id"])
        return ids

    def test_json_output_is_actually_json(self):
        out = run_brain("stats", "--json", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout)
        payload = json.loads(out.stdout[out.stdout.find("{"):])
        for key in ("corpus", "capture", "findability", "freshness",
                    "consolidation", "integrity"):
            self.assertIn(key, payload)


class DefaultBranchTests(unittest.TestCase):
    """`main` was hardcoded where the code decides whether consolidation work
    has been reviewed. On a `master` repo those comparisons named a branch that
    does not exist, so the check reported 'all clear' because it never ran."""

    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.module = load_brain_module()

    def _repo_on(self, branch):
        repo = make_sandbox(self.tmp.name)
        subprocess.run(["git", "branch", "-M", branch], cwd=repo,
                       check=True, capture_output=True)
        self.module.ROOT = repo
        self.module._DEFAULT_BRANCH = None
        return repo

    def test_a_master_branch_repo_is_detected(self):
        self._repo_on("master")
        self.assertEqual(self.module.default_branch(), "master")

    def test_a_main_branch_repo_is_detected(self):
        self._repo_on("main")
        self.assertEqual(self.module.default_branch(), "main")

    def test_an_unusual_trunk_name_is_detected(self):
        self._repo_on("trunk")
        self.assertEqual(self.module.default_branch(), "trunk")


class RemoteVisibilityTests(unittest.TestCase):
    """A brain pushed to a PUBLIC repo is every private thought the owner ever
    recorded, world-readable, in a history that outlives the fix. It is the one
    failure here with no undo, so the alarm is asserted rather than trusted.

    No network: slug parsing is pure, and the doctor branch is driven by a
    stubbed probe. A test that reached GitHub would be a test that fails on a
    plane."""

    def setUp(self):
        self.module = load_brain_module()

    def test_the_slug_is_parsed_from_every_remote_spelling(self):
        cases = {
            "https://github.com/acme/brain.git": "acme/brain",
            "https://github.com/acme/brain": "acme/brain",
            "git@github.com:acme/brain.git": "acme/brain",
            "ssh://git@github.com/acme/brain.git": "acme/brain",
            "https://github.com/acme/brain/": "acme/brain",
            "https://gitlab.com/acme/brain.git": "",
            "/Users/someone/brain": "",
            "": "",
        }
        for remote, expected in cases.items():
            self.assertEqual(self.module.github_repo_slug(remote), expected,
                             f"wrong slug for {remote!r}")

    def test_a_non_github_remote_is_unknown_not_assumed_safe(self):
        """Silence must never read as safety: an unknown remote is reported as
        unchecked, not as private."""
        self.assertEqual(self.module.remote_visibility("https://gitlab.com/a/b.git"),
                         "unknown")
        self.assertEqual(self.module.remote_visibility(""), "unknown")

    def _doctor_with_visibility(self, verdict):
        module, repo = self.module, self.repo
        module.ROOT, module.K = repo, repo / "knowledge"
        module.INDEX_DB = repo / ".cache" / "index.db"
        module.remote_visibility = lambda _remote: verdict
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = module.cmd_doctor()
        return code, buf.getvalue()

    def test_a_public_remote_is_red_and_names_the_fix(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.repo = make_sandbox(self.tmp.name)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/brain.git"],
                       cwd=self.repo, check=True, capture_output=True)
        code, out = self._doctor_with_visibility("public")
        self.assertIn("YOUR BRAIN IS PUBLIC", out)
        self.assertIn("--visibility private", out,
                      "the alarm did not tell the reader how to fix it")
        self.assertNotEqual(code, 0, "a public brain must make doctor fail")

    def test_a_private_remote_passes_quietly(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.repo = make_sandbox(self.tmp.name)
        subprocess.run(["git", "remote", "add", "origin",
                        "https://github.com/acme/brain.git"],
                       cwd=self.repo, check=True, capture_output=True)
        _code, out = self._doctor_with_visibility("private")
        self.assertIn("not publicly readable", out)
        self.assertNotIn("YOUR BRAIN IS PUBLIC", out)


class ScheduleOwnershipTests(unittest.TestCase):
    """A schedule's identifier (a launchd label, a systemd unit name, a Task
    Scheduler task name) is machine-global. Finding it installed proves a job
    exists, never that it serves THIS brain — and consolidation is the only
    thing that drains the inbox, so a false 'scheduled' means it silently
    never drains."""

    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.module = load_brain_module()
        fake_home = Path(self.tmp.name) / "home"
        self.plists = fake_home / "Library" / "LaunchAgents"
        self.plists.mkdir(parents=True)
        # schedule_serves_this_repo goes through osbackend.scheduler(), which
        # picks a backend by asking osbackend.os_family() about the REAL host.
        # This fixture only ever writes a launchd-style plist, so it lines up
        # when os_family() says "macos" — true on the Mac this suite was
        # written on — but the identical code asking a Linux CI runner (what
        # .github/workflows/gate.yml actually runs on ubuntu-latest) truthfully
        # gets "linux", scheduler() hands back a SystemdScheduler, and serves()
        # looks for ~/.config/systemd/user/consolidate.service — a file this
        # fixture never writes — so the same assertion fails there. Pinning
        # os_family() here makes the backend choice depend only on the fixture
        # below, never on which OS happens to be running the test.
        self.real_os_family = self.module.osbackend.os_family
        self.module.osbackend.os_family = lambda: "macos"
        self.addCleanup(setattr, self.module.osbackend, "os_family",
                        self.real_os_family)
        # schedule_serves_this_repo now goes through osbackend.scheduler(),
        # whose LaunchdScheduler computes its plist directory from Path.home()
        # at construction time — there is no more bin/brain-level PLIST_DIR
        # constant to patch, so redirecting HOME lands the fixture in the same
        # place the real code looks. Same pattern dewire_against uses below.
        self.real_home = self.module.Path.home
        self.module.Path.home = staticmethod(lambda: fake_home)
        self.addCleanup(setattr, self.module.Path, "home", self.real_home)
        self.module.ROOT = Path(self.tmp.name) / "this-brain"
        self.module.ROOT.mkdir()

    def _write_plist(self, target):
        (self.plists / "com.secondbrain.consolidate.plist").write_text(
            f"<plist><string>{target}/bin/brain</string></plist>", encoding="utf-8")

    def test_a_job_for_another_brain_is_not_this_brains_job(self):
        self._write_plist(Path(self.tmp.name) / "some-other-brain")
        self.assertFalse(
            self.module.schedule_serves_this_repo("consolidate"),
            "another brain's schedule was claimed as this one's")

    def test_a_job_for_this_brain_counts(self):
        self._write_plist(self.module.ROOT)
        self.assertTrue(
            self.module.schedule_serves_this_repo("consolidate"))

    def test_no_plist_at_all_is_not_scheduled(self):
        self.assertFalse(
            self.module.schedule_serves_this_repo("consolidate"))


class HelpDoesNotActTests(unittest.TestCase):
    """`<command> --help` must never DO the command. `init --help` used to ignore
    argv entirely and wire the machine — re-pointing a global skill symlink and
    registering an MCP server — for someone who only asked what it does."""

    def setUp(self):
        self.tmp = temp_dir()
        self.addCleanup(cleanup_temp, self.tmp)
        self.repo = make_sandbox(self.tmp.name)

    def test_init_help_prints_usage_and_wires_nothing(self):
        before = (self.repo / ".mcp.json").exists()
        out = run_brain("init", "--help", repo=self.repo)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("Commands:", out.stdout)
        self.assertEqual((self.repo / ".mcp.json").exists(), before,
                         "`init --help` wired the machine")

    def test_bare_help_is_not_an_unknown_command(self):
        for flag in ("--help", "-h", "help"):
            out = run_brain(flag, repo=self.repo)
            self.assertEqual(out.returncode, 0, f"{flag}: {out.stdout}")
            self.assertNotIn("unknown command", out.stdout,
                             f"{flag} was treated as a command name")


if __name__ == "__main__":
    unittest.main(verbosity=2)
