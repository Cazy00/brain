# Connect, Retire and the Docs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Finish the setup UX redesign. `brain connect` stops printing snippets for a human to paste and starts writing them, safely, into files this system did not create. `brain reset` becomes `brain retire` and stops reading like a warning label. README and SETUP.md are rewritten to document what was built rather than what was planned.

**Predecessor:** [2026-07-25-setup-foundation.md](2026-07-25-setup-foundation.md) — stages 1–3 of the spec, closed 2026-07-29. This plan is stages 4–6 of [the same spec](../specs/2026-07-25-setup-ux-redesign-design.md); stage 7 (`brain serve`) is Plan 3 and depends on nothing here.

**Architecture:** Three concerns, in dependency order.

1. `CONNECT_CLIENTS` currently describes each client in prose — `"config": "~/.cursor/mcp.json  (global)  or  <repo>/.cursor/mcp.json  (project)"`. Prose cannot be written to. Every client gains machine-readable `path` and `routing_path` fields, and the prose stays for the human. That is the enabling change; `--apply`, detection and `--json` all read those two fields.
2. Editing another tool's config is the only genuinely dangerous thing in this repo that is not already gated. It gets its own module, `bin/brainlib/configedit.py`, with four rules — merge, back up, refuse, idempotent — and its own tests, so the danger is in one place with one test file pointed at it.
3. `retire` is mostly renaming and rewriting output around machinery that already works. The one new capability is removing the routing block, which is only possible once `connect --apply` writes it between markers. That ordering is why retire comes after connect and not before.

**Tech Stack:** Python 3.9+ standard library only. `unittest`. No TOML library exists below 3.11, so the TOML path is deliberate text surgery with a loud refusal on anything it cannot recognise.

## Global Constraints

These apply to every task. Violating any of them fails review.

- **Python 3.9 floor.** No `match`, no `X | Y` unions at runtime, no `dict1 | dict2`, no `str.removeprefix`. No `tomllib` — it arrived in 3.11.
- **Zero third-party dependencies.** Standard library only, everywhere.
- **Never install anything on the user's machine.** Print the exact command and the consequence.
- **Never write a file this system does not own without backing it up first.** The backup path is `<file>.brain-backup-<YYYY-MM-DD-HHMMSS>` and it is named in the output. This is the whole reason `--apply` is allowed to exist.
- **A refusal is always available and always safe.** Anything `--apply` cannot do confidently, it declines to do, prints the snippet, and exits non-zero. Printing is v1's behaviour and it still works; `--apply` is strictly additive.
- **All dates in code, comments and docs are absolute** (`2026-07-29`), never "today".
- **The commit gate runs on every commit.** `.githooks/pre-commit` runs `python3 bin/brain lint --staged` plus `gitleaks`.
- **Existing tests must keep passing.** `python3 -m unittest discover -s tests` — 336 tests as of 2026-07-29. Never delete one to make a change pass; if a test is genuinely wrong, say so in the commit message.

  One known environment-specific failure predates this plan: `test_init_defers_when_claude_cli_absent` builds a PATH with no `claude` on it and asserts its own sandbox is clean. On a machine with `claude` installed into `/usr/bin` that assertion correctly refuses. It is not caused by this work and must not be "fixed" by weakening the assertion.
- **Match the surrounding prose style.** Comments explain *why*, especially why an obvious alternative was rejected.

---

### Task 1: The `--no-repo` verdict reads as a failure the user chose

Deferred out of the previous plan's self-review. Small, independent, and it clears an open question before anything larger lands on top of it.

`brain setup --no-repo` builds a working brain and then exits 1, because `verify` runs doctor and doctor calls a brain with no off-machine copy `[RED]`. Both halves are correct and neither should change: an unbacked-up brain IS unhealthy, `AGENTS.md` says a backup gap is "worth seeing, not an untidiness to hide", and the scheduled watchdog only notifies on a non-zero exit. What is wrong is that a user who typed `--no-repo` is shown the word "failed" with no statement that this is the thing they asked for.

**Decision:** the exit code and the word both stay. The output gains one closing block, printed only when the user opted out of a remote and verify then failed, that says what happened, why, and the one command that fixes it. Softening the verdict would be the dishonest option; explaining it is the honest one.

**Files:**
- Modify: `bin/brainlib/setup.py` (`run_setup`)
- Test: `tests/test_setup.py`

- [x] **Step 1: Write the failing test**

In `TestSetupEndToEnd`, assert that a `--no-repo` run's stderr names the flag as the cause and carries the `gh repo create` remedy — and that a run which did NOT opt out is not given that block, because there the red is a genuine surprise.

- [x] **Step 2: Run test to verify it fails**
- [x] **Step 3: Implement** — a `want_remote is False and overall_status(results) == "failed"` branch after the phase loop, before the return.
- [x] **Step 4: Run test to verify it passes**
- [x] **Step 5: Commit** — `setup: say why a --no-repo install ends red`

---

### Task 2: Machine-readable client paths

The enabling change. Nothing can be written until each client says where it lives in a form other than a sentence.

**Files:**
- Modify: `bin/brain` (`CONNECT_CLIENTS`, `print_client`)
- Test: `tests/test_brain.py` (`ConnectTests`)

**Interfaces:**
- Produces, per client entry:
  - `path: str` — the config file `--apply` writes, `~`-relative, or `None` where the client has no writable config (claude-code, which `init` owns).
  - `routing_path: str` — the global instruction file the routing block goes in, or `None` where the client has none (cursor, claude-desktop — both UI-only).
  - `routing_preamble: str` — text that must precede the block in a file created from scratch. Only vscode has one (`applyTo` frontmatter); everywhere else it is `""`.

The existing prose fields stay exactly as they are. They carry the caveats — project vs global scope, the Windows path, "UI only" — that a single path cannot, and `print_client` is still what a human reads.

- [x] **Step 1: Write the failing test**

Assert that every client with a `format` also has a `path`; that every client whose `routing` prose does not begin `NO FILE` has a `routing_path`; and that no `path` is a bare relative name (a config written to the current directory instead of the user's home is the failure mode).

- [x] **Step 2: Run test to verify it fails**
- [x] **Step 3: Implement**

Add the three fields. `claude-desktop`'s path is OS-dependent (`~/Library/Application Support/Claude/` on macOS, `%APPDATA%/Claude/` on Windows, and it does not ship on Linux) — resolve it through a helper that asks `osbackend.os_family()`, and return `None` on Linux rather than inventing a path no file will ever be at.

- [x] **Step 4: Run test to verify it passes**
- [x] **Step 5: Commit** — `connect: give every client a machine-readable config path`

---

### Task 3: The config-editing primitive

**Files:**
- Create: `bin/brainlib/configedit.py`
- Test: `tests/test_configedit.py`

**Interfaces:**
- Consumes: nothing but the standard library.
- Produces:
  - `apply_json(path, container, name, entry) -> Outcome` — merge one server into a JSON config under top-level key `container` (`mcpServers` or `servers`).
  - `apply_toml(path, table, entry) -> Outcome` — the same for `[mcp_servers.brain]`.
  - `apply_markers(path, block, preamble="") -> Outcome` — insert or update text between `<!-- brain:routing:start -->` and `<!-- brain:routing:end -->`.
  - `remove_markers(path) -> Outcome` — delete the block and its markers, leaving everything else untouched.
  - `Outcome` — `action` (`"created"` | `"updated"` | `"unchanged"` | `"refused"`), `detail`, `backup` (path or `""`), `snippet` (what to paste, when refused).

Rules, all four tested directly:

- **Merge, never overwrite.** Unrelated servers and unrelated top-level keys survive byte-for-byte in the JSON case, and every other table survives in the TOML case.
- **Back up first**, to `<file>.brain-backup-<stamp>`, and name it in `Outcome.backup`. No backup when nothing changes — a directory full of identical backups is how people learn to ignore them.
- **Refuse on an unrecognized shape.** JSON that does not parse (VS Code and Cursor both tolerate comments, which `json.loads` does not), a container key holding something that is not an object, a TOML file containing a multi-line string (`"""` / `'''`) where naive append cannot be proven safe. Refusal returns the snippet and writes nothing.
- **Idempotent.** An already-correct entry returns `"unchanged"`, writes nothing, and makes no backup.

Also required, and easy to forget: writes are atomic (write a sibling temp file, then `os.replace`), because a half-written config is worse than an unwritten one, and the backup exists precisely so this is recoverable.

- [x] **Step 1: Write the failing test** — one test per rule per format, plus the atomicity and permissions cases.
- [x] **Step 2: Run test to verify it fails**
- [x] **Step 3: Implement**
- [x] **Step 4: Run test to verify it passes**
- [x] **Step 5: Commit** — `configedit: merge, back up, refuse, idempotent`

---

### Task 4: `brain connect <client> --apply`

**Files:**
- Modify: `bin/brain` (`cmd_connect`)
- Test: `tests/test_brain.py`

**Interfaces:**
- Consumes: `configedit`, the `path` fields from Task 2.
- Produces: `--apply` and `--dry-run` on `cmd_connect`.

Behaviour:

- `brain connect <client>` — unchanged. Prints. This stays the default forever; `--apply` is opt-in because it edits someone else's file.
- `brain connect <client> --apply` — writes, reports the action and the backup path.
- `brain connect <client> --apply --dry-run` — prints the exact diff and writes nothing. Reported as such; `--dry-run` without `--apply` is a usage error rather than a silent no-op.
- `brain connect claude-code --apply` — calls the existing `cmd_init` wiring, which is what already owns that client's two files. Do NOT duplicate it.
- `brain connect --all --apply` — every client that is actually installed. Never one that is not: creating `~/.codex/config.toml` on a machine with no Codex is litter, and it makes the next `connect` report the client as present.
- Exit code is non-zero if any client refused, so a scripted run cannot mistake a refusal for a write.

- [ ] **Step 1: Write the failing test** — a fake HOME with pre-seeded config files; assert unrelated entries survive, backups are named, re-apply is unchanged, `--dry-run` writes nothing, and an uninstalled client is skipped rather than created.
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit** — `connect: --apply writes the registration, --dry-run shows it first`

---

### Task 5: Routing markers, and `--routing --apply`

**Files:**
- Modify: `bin/brain` (`routing_block`, `cmd_connect`)
- Test: `tests/test_brain.py`

The block is written between `<!-- brain:routing:start -->` and `<!-- brain:routing:end -->` so it can be updated in place and removed cleanly. Without markers, updating means guessing where the block ends and `retire` cannot undo it at all — which is exactly the state the current instructions leave every machine in.

- `brain connect --routing` — prints the block, now with markers. Unchanged otherwise.
- `brain connect --routing --apply` — writes it into the routing file of every installed client that has one, creating the file (and its parents, and any `routing_preamble`) if needed.
- `brain connect <client> --routing --apply` — one client.
- A client with `routing_path: None` prints where the UI setting lives and is counted as skipped, not failed. Cursor and Claude Desktop have no file; nothing can write one for them.
- Re-running updates the block in place. This matters more than it looks: the block changes as this system changes, and before markers there was no way to update it except by hand.

- [ ] **Step 1: Write the failing test** — including that surrounding content in a pre-existing `~/.claude/CLAUDE.md` is preserved above and below the markers, and that a second apply with a changed block replaces it rather than appending a second copy.
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit** — `connect: routing block gets markers and can be applied`

---

### Task 6: Client detection and `--json`

**Files:**
- Modify: `bin/brain` (`cmd_connect`)
- Test: `tests/test_brain.py`

Bare `brain connect` currently lists every client this system knows about, which is a catalogue, not a status. It becomes a report of this machine: for each client whose config file or config directory exists, one line saying **not wired** / **wired to this brain** / **wired to a different brain**.

The third is the case that silently breaks things today: a second clone, or a brain that moved, leaves an agent talking to a path that is not this one and nothing anywhere says so.

`--json` emits the same information as data: per client, `installed`, `wired` (`"no"` | `"this"` | `"other"`), `path`, `routing_path`, `routing_applied`. This is the contract an agent reads before deciding whether to run `--apply`, so it is tested like an API.

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit** — `connect: report what this machine is actually wired to`

---

### Task 7: `brain retire`

**Files:**
- Modify: `bin/brain` (`cmd_reset` → `cmd_retire`, `main`, module docstring)
- Test: `tests/test_brain.py` (`ResetTests`)

Everything that makes the current command safe is preserved unchanged: the bundle is written and verified before anything moves, `reset_blockers()` still has no override flag, the repo is moved rather than deleted, the remote is never touched, ownership is checked before any shared machine state is altered, and it still refuses without a TTY and is still absent from the MCP tool list.

What changes is the name and the reading experience:

- `brain retire` is the name. `brain reset` prints one line naming the new name and continues — it is in shipped docs and possibly muscle memory.
- Listed in `--help` with a one-line description. It is currently listed; the description gets the new name.
- `--dry-run` runs every safety check and prints the exact plan, changing nothing. This is the discoverable, safe way to explore the command, and it is also how anyone reviewing this can see what it would do without owning a spare brain.
- Default output is three short blocks: **what happens** / **what is kept** / **type this**. The six-paragraph preserved list moves behind `--explain`.
- Plain language with the internal name in parentheses: "stopped the weekly background jobs (launchd)".
- The confirm phrase keeps its anti-paste property — still computed from live state, so a phrase from a chat log cannot match — but gets materially shorter.
- The routing block is removed via `configedit.remove_markers` from every routing file that has one, now that Task 5 puts markers there. A block left behind points every future session at a directory that no longer exists. A block without markers (written by hand, before this existed) is REPORTED, not guessed at.
- Ends by offering to run the install rather than pointing at SETUP.md.

- [ ] **Step 1: Write the failing test** — `--dry-run` changes nothing and its plan matches a real run's; `reset` still works and says the new name; the phrase is shorter but still live-state-derived; a marked routing block is removed and an unmarked one is reported.
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run test to verify it passes**
- [ ] **Step 5: Commit** — `retire: rename reset, add --dry-run, and undo the routing block`

---

### Task 8: README

**Files:**
- Modify: `README.md`

Order, per the owner: **how it works → install → the interesting parts.** It is a product page that argues its case with facts, not an install manual.

1. What it is and how it works. Existing mermaid diagrams retained.
2. Install: prerequisites as a table with consequences, one line per OS, the paste-to-your-agent line, and a pointer to `brain connect`. Short *and* complete — which is only possible because setup became one command. Shortness is not achieved by omitting steps.
3. The rest: how it stays true, `stats`, and "What it deliberately does not do", which stays prominent.

The current README opens with the problem and reaches Install at line 135, after the git primer. The primer moves to SETUP.md; the install section moves up and shrinks to the four commands that now exist.

- [ ] **Step 1: Rewrite**
- [ ] **Step 2: Check every command in it actually runs** — including on this machine, not just by eye
- [ ] **Step 3: Commit** — `README: how it works, then install, then the interesting parts`

---

### Task 9: SETUP.md

**Files:**
- Modify: `SETUP.md`

Restructured to the four-command shape: `setup` / `connect` / `retire`, with `serve` named as not-yet-built rather than omitted. Parts 1 and 2 collapse into `brain setup`; Parts 2b and 3 collapse into `brain connect`; the Uninstall section becomes `brain retire`. Deep material — schedules, vault, second machine, updates, troubleshooting — stays, because that is what this file is for.

Anything the previous plan changed and this file still describes the old way of is a lie a new user will follow: `bin/brain init` as a first-run step, the interactive `install.sh`, the manual paste of the routing block.

- [ ] **Step 1: Rewrite**
- [ ] **Step 2: Verify every command** — every command block in the file, run or explicitly marked as unverifiable here (macOS/Windows paths)
- [ ] **Step 3: Commit** — `SETUP: restructure around setup, connect and retire`

---

## Self-review

**Spec coverage for stages 4–6.** `connect --apply` with merge/backup/refuse/idempotent → Tasks 3, 4. Routing markers → Task 5. Client detection with the wired-to-a-different-brain case → Task 6. `connect --json` → Task 6. `retire` with `--dry-run`, three blocks, `--explain`, shorter phrase, install offer → Task 7. README → Task 8. SETUP.md → Task 9. Task 1 is not from the spec; it is the open question the previous plan's completion note left behind, resolved before anything is built on top of it.

**Not in this plan.** `brain serve` → Plan 3, which depends on nothing here and carries its own security review. The spec's open question about whether claude.ai custom connectors accept bearer-token auth belongs with it.

**Ordering risk.** Task 7 depends on Task 5 for marker removal, and Tasks 4–6 all depend on Task 2. Task 1, 8 and 9 are independent. If this plan is stopped part-way, stop after any completed task: each one ends with a working toolbelt, which is the same property the four commands themselves have.

**The dangerous task is 3, and it is dangerous in a way tests can reach.** Editing another tool's config file is the only operation here that can destroy something the user cannot get back. That is why it is a separate module with a separate test file, why the backup is mandatory rather than a flag, why the writes are atomic, and why refusing is always an available answer. `--apply` is opt-in; every path it takes still works when the answer is "print it and let a human paste it", because that is what v1 did and it is the fallback that cannot break.
