# The business partition — M, P and the drop box — Implementation Plan

**Goal:** Make a brain safe to put in front of customers. After this plan there
are two brains: **M**, the business's real brain, and **P**, a compiled artifact
containing only what a human approved for customers to see. A customer-facing
bot reads P and can propose new knowledge back into M's `inbox/`, and it holds
no path and no credential that reaches M. The bot's model may be cheap, weak,
and jailbroken — the isolation does not depend on it behaving.

**Design authority:** [specs/2026-07-29-agent-brain-gap-loop-design.md](../specs/2026-07-29-agent-brain-gap-loop-design.md),
section **"Settled architecture"** (lines 80–135). Read it before writing code.
Every decision there — publish-vs-mount, compiled-artifact-not-subset, the
`inbox/` drop box, human-approves-first-publish — is settled and is NOT to be
re-litigated in this plan. That spec is stamped DEFERRED; the deferral was of
*the gap loop*, not of the partition, and the partition's own revival trigger
("a customer-facing agent makes the partition real") is now met.

**Predecessors:** [2026-07-29-serve.md](2026-07-29-serve.md) built the transport
and the token; [2026-07-29-serve-hardening.md](2026-07-29-serve-hardening.md)
added `--read-only` and the per-address backoff. This plan reuses both
mechanisms and their tests. `cmd_template` (`bin/brain:4115-4440`) is the
working precedent for the compiler in Task 4 — same shape, different payload.

## What this plan does NOT build, and why

- **The gap loop.** It has its own fully-written spec (frontmatter, six-state
  machine, dedup, routing, `gaps stats`, test list). It is the *largest* piece
  and the *least* urgent: without it the bot still contributes, because Task 2
  gives it a drop box and a human reads `inbox/` by hand. Build this plan
  first, run it against a real business, then build the gap loop knowing what
  a real customer answer looks like.
- **`mount`** (M₁ + M₂ into one read-only search view for a trusted internal
  agent). Composition, not isolation. Nothing here needs it.
- **OAuth 2.1 / claude.ai reachability** — backlog item 3, unrelated.
- **Message transports.** Hermes supplies Telegram/Discord/Slack/WhatsApp/
  Signal. The brain never executes anything and never speaks to a customer.

## Before this plan is worth starting

**Verify `bin/brain-mcp` actually runs under Hermes Agent.** The claim that
brain plugs in "with zero new code" is a design assertion that nobody has
tested. Connect it, call `brain_search` and `brain_capture`, and write down what
happened. Everything below assumes that works. If it does not, fix that first —
it changes the shape of Tasks 2 and 6.

---

## Global Constraints

Carried forward from the predecessor plans. Violating any of them fails review.

- **Python 3.9 floor.** No `match`, no `X | Y` unions at runtime, no
  `dict1 | dict2`, no `str.removeprefix`.
- **Zero third-party dependencies.** `bin/brain-mcp` advertises "zero
  dependencies and no vendor SDK" and that claim is load-bearing.
- **The stdio path does not change.** It serves all five tools including the
  write tool, and it always did — stdio is a subprocess spawned on the machine
  the operator is sitting at. Every restriction here is a property of an
  exposed socket, never of the tool layer.
- **No credential ever reaches the repo.** Tests use a `FileKeystore` in a temp
  directory; fixture bearers are named `BEARER`, not `TOKEN`.
- **No test may bind a public interface.** `127.0.0.1`, port 0, always.
- **No test may sleep to observe a timeout or a date rollover.** Anything
  time-dependent takes its clock as a parameter.
- **All dates in code, comments and docs are absolute** (`2026-07-30`).
- **Existing tests must keep passing.** 474 as of 2026-07-30, all green
  (`python3 -m unittest discover -s tests`; unittest reports to stderr, so
  redirect to a file rather than piping to `tail`, or the summary interleaves
  ahead of test stdout and looks like a hang).
- **Match the surrounding prose style.** Comments explain *why*, especially why
  an obvious alternative was rejected.
- **This repo is the public template.** No fixture may contain real business
  data, a real customer message, or anybody's name. `bin/brain doctor` printing
  `[RED] YOUR BRAIN IS PUBLIC` here is correct — do not "fix" it.
- **Never run `bin/brain init` from this checkout or from a scratch copy.** It
  re-points the global `~/.claude/skills/brain` symlink at whatever it was run
  from.

---

## The security contract, in one place

Everything below serves these five sentences. If a change weakens one of them,
it is wrong no matter how convenient.

1. **The bot's process holds no path and no credential reaching M.** It reaches
   P through one endpoint and M's drop box through another. Nothing else. No
   shell, no filesystem, no git.
2. **P is compiled, not filtered.** It is rebuilt from zero and contains only
   notes a human marked `visibility: public`. There is no query-time exclusion
   anywhere, because a boundary that is a code path is one bug from a leak.
3. **The drop box returns no information about M.** It acknowledges a write and
   nothing more — no dedup hint, no "similar note exists", no error text
   derived from M's contents. A duplicate signal is a read channel.
4. **An untrusted agent may propose knowledge, never establish it.** Captures
   land in `inbox/`, outside default search, promoted only by the pinned
   consolidator. No agent ever supersedes.
5. **Visibility is set by a human at the CLI, never over the wire.** No MCP
   tool sets it, reads it, or lists what is pending. Putting the human gate on
   the wire defeats the human gate.

## Design decisions this plan settles

New since the spec. Each is settled here so the implementer does not re-derive
it, and each names what it rejected.

**Provenance is stamped by the endpoint, never claimed by the caller.** A
`source:` field the agent passes in its own request payload teaches you nothing
— an agent that can lie about its content can lie about its label. The server
stamps it from its own startup configuration, and a `source` in the request is
ignored. Rejected: trusting the caller (worthless), and inferring it from the
bearer token (works, but couples provenance to credential rotation).

**Absent visibility and `visibility: private` are different states.** Absent
means *never reviewed* and appears in the review queue. `private` means
*reviewed and refused* and stays quiet. Both are excluded from P, so the
fail-closed property is unchanged — but without the distinction the operator is
re-asked forever about notes they already rejected, and a review queue nobody
finishes is a review queue nobody reads. Rejected: a two-value enum with a
separate `reviewed:` flag (two fields to keep consistent, one of them
redundant).

**Wikilinks to unpublished notes are stripped, and this is a security
requirement.** A dangling `[[2026-03-01-acquisition-talks-with-partner]]` in P
leaks the existence, the date and the subject of a private note to anyone the
bot talks to. The compiler replaces the link with its plain text and reports
the count. This is safe because AGENTS.md already requires every note to be
self-contained — "the locator is for going deeper, never for understanding at
all". Rejected: refusing to publish a note that links to a private one (one
link would block an otherwise-approved note, and the operator's escape is to
delete the link from M, damaging M to satisfy P).

**A superseded fact leaves P until the successor is re-approved, and `publish`
must say so out loud.** The successor is a new note with absent visibility, so
it is not published; the predecessor is archived, so it is dropped. That is
correct — a changed price must not keep serving the old value — but it is
silent, and silence here means the bot starts answering "I don't know" about
something it knew yesterday. `publish` reports removals as prominently as
additions.

**P ships the whole toolbelt and is a complete, valid brain.** `brain serve
--read-only` is run from *inside* P, so the serving process has no
configuration naming M at all. Rejected: pointing M's toolbelt at P's knowledge
directory with a flag (`ROOT` is derived from the script's own location at
`bin/brain:60`, and a flag that redirects it is one typo from serving M).

**Frontmatter ships by allowlist.** Only `id`, `kind`, `title`, `topics`,
`aliases`, `created`, `status` and `visibility` reach P. Everything else —
`review_by`, `source`, `supersedes`, `sensitivity`, and any field added in
future — is dropped. Rejected: a denylist, which publishes every field somebody
adds later and forgets to exclude.

---

### Task 1: `source:` — provenance stamped by the endpoint

**Files:**
- Modify: `bin/brain` (`cmd_capture` gains `--source`; lint learns the field)
- Modify: `bin/brainlib/mcp.py` (`call_tool` takes a `source`; `brain_capture`
  ignores any `source` in the request arguments)
- Test: `tests/test_brain.py`, `tests/test_serve.py`

Today `cmd_capture` writes exactly two frontmatter fields — `created` and
`status: draft` (`bin/brain:2364`). Nothing records *who* wrote a capture, so
the consolidator cannot tell a claim a customer fed to a bot from something the
owner wrote. That is the knowledge-poisoning hole, and it is open right now for
every remote capture, before any of this plan's other tasks exist.

**Interfaces:**
- `cmd_capture` accepts `--source <slug>`; omitted means `local`.
- Slug validated as lowercase-hyphen (same rule as `id`); an invalid slug is
  refused before the write, alongside the existing `scan_secrets` refusal.
- `mcp.call_tool(name, args, allow=None, source=None)` — `source` comes from
  the *server*, and `args["source"]` is discarded if present. A test asserts a
  request that tries to set its own source is stamped with the server's value
  anyway.
- Written as `source: <slug>` into inbox frontmatter, after `created`.
- `lint` accepts `source` as a known optional field and validates the slug.
  Inbox notes stay under relaxed rules otherwise.

**Also update the consolidation prompt** (`setup/consolidate-prompt.md`): a note
whose `source` is anything other than `local` is an untrusted *proposal*.
Corroborate before promoting; never supersede a canonical note on the strength
of one, regardless of how confident it reads. State it in the prompt in those
terms — the prompt is the only place this rule can live, because the
consolidator is the only thing that acts on it.

- [ ] **Step 1: Write the failing tests.** `--source` writes the field; omitted
      gives `local`; an invalid slug is refused and nothing is written; a
      caller-supplied `source` in MCP arguments is overridden by the server's;
      lint accepts the field and rejects a malformed value.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `capture: record who wrote it, stamped by the endpoint`

---

### Task 2: `brain serve --drop-box`

**Files:**
- Modify: `bin/brainlib/mcp.py` (`WRITE_ONLY_TOOLS`, derived like
  `READ_ONLY_TOOLS`)
- Modify: `bin/brainlib/serve.py` (`--drop-box`, `--source`, the daily cap, the
  banner)
- Test: `tests/test_serve.py`

The mirror of `--read-only`, and the same rule applies: **filtering happens
server-side, in the dispatcher.** A client that never read `tools/list` and
calls `brain_search` by name must be refused, because the client is the thing
being defended against and therefore cannot be the thing enforcing the
restriction. Both halves get their own test, exactly as Task 1 of the hardening
plan did.

**Fail closed, symmetrically.** `READ_ONLY_TOOLS` is tools whose
`readOnlyHint` is exactly `True` (`bin/brainlib/mcp.py:145-153`).
`WRITE_ONLY_TOOLS` is tools whose `readOnlyHint` is exactly `False`. A tool
added later with no annotation lands in **neither** set, so it is exposed by
neither restricted mode. The existing test asserting every tool declares an
annotation covers both.

**`--drop-box` requires `--source`** and refuses to start without it. An
unattributed drop box is worse than none: it produces exactly the inbox notes
the consolidator cannot weigh, while looking like it is working.

**Mutually exclusive with `--read-only`** — refuse both, with a message saying
they are two deployments and therefore two processes on two ports. This is the
same fact the hardening plan recorded about read-only, and the operator will
reach for the combination.

**The response carries nothing from M.** On success: an acknowledgement and the
new note's id. Never a dedup hint, never "a similar note already exists", never
a count, never search results. Rejected explicitly and worth the comment: a
duplicate signal turns the drop box into a read oracle — the bot captures
guesses and watches which ones come back "already known", and reads M one
question at a time.

**Daily cap, counted from the filesystem.** Default 200 captures per source per
day, in the flag. The count is *derived*: inbox filenames already begin with
`YYYY-MM-DD` and the frontmatter now carries `source`, so counting today's
matching files needs no new state file and survives a restart — an in-memory
counter would reset every crash, which is a bypass an unstable bot finds by
accident. Over the cap, `brain_capture` returns a tool error and the refusal is
logged. Existing `scan_secrets` refusal is unchanged and runs first.

- [ ] **Step 1: Write the failing tests.** `tools/list` returns only
      `brain_capture`; `tools/call` on `brain_search` by name is refused; the
      derivation is fail-closed; `--drop-box` without `--source` refuses to
      start; `--drop-box --read-only` refuses; a successful capture returns no
      information about M; the daily cap refuses at the boundary and survives a
      simulated restart; the cap is per-source.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `serve: --drop-box, a write-only endpoint that tells the caller nothing`

---

### Task 3: `visibility:` in the note contract

**Files:**
- Modify: `bin/brain` (known-field list at `:481`; validation beside
  `sensitivity` at `:582-589`; `cmd_supersede`)
- Test: `tests/test_brain.py`

`sensitivity` is the template to copy, field for field: a small enum, validated
by lint, with a rule that refuses a dangerous combination.

**Values:** `public` | `private`. **Absent is a third state** — never reviewed —
and is the default for every note that exists today and every note `brain new`
creates. Absent and `private` are both excluded from P; only their behaviour in
the review queue differs.

**Cross-checks lint must enforce:**
- `visibility: public` with `sensitivity: personal` is an **error**. The two
  fields disagreeing is a mistake with a bad failure mode, and lint is the only
  thing that sees both.
- `visibility: public` on a note under `people/` or `life/` is an **error**.
- `visibility: public` on a note that is not `status: current` is an **error** —
  archived and superseded notes are never publishable.

**`cmd_supersede` never carries visibility forward.** The successor is created
without the field, so a changed fact is private until a human looks at it
again. Add the assertion to the existing supersede tests rather than a new
file: the chain test already exists and this is a property of the chain.

- [ ] **Step 1: Write the failing tests.** Lint accepts `public`/`private` and
      rejects anything else; absent is valid everywhere; each of the three
      cross-checks errors; `brain new` sets no visibility; `brain supersede`
      leaves the successor without one even when the predecessor was public.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `notes: visibility, absent by default and never inherited`

---

### Task 4: `brain publish` — compile P from M

**Files:**
- Create: `bin/brainlib/publish.py` (the compiler and its audit)
- Modify: `bin/brain` (`cmd_publish`, the usage block, `main()` dispatch)
- Test: `tests/test_publish.py` (new)

This is the trust boundary. Read `cmd_template` (`bin/brain:4115-4440`) first —
it already implements this shape correctly for a different payload, including
the part most implementations get wrong.

**The build, in order:**

1. **Destroy and rebuild.** P's `knowledge/` is regenerated from nothing on
   every run. Never patch, never merge. A note whose approval was revoked must
   vanish, and the only way to guarantee that is to never carry anything
   forward.
2. **Select** notes with `visibility: public` and `status: current` from
   canonical folders only. `inbox/`, `journal/`, `archive/`, `vault/` and
   `gaps/` are never eligible, whatever their frontmatter says.
3. **Project frontmatter by allowlist** — `id`, `kind`, `title`, `topics`,
   `aliases`, `created`, `status`, `visibility`. Drop everything else.
4. **Strip wikilinks** whose target is not in the published set, leaving the
   link text as plain prose. Count them and report.
5. **Filter `topics.yaml`** to the topics actually used by published notes.
   `TEMPLATE_TOPICS` exists because "the topic names ARE the author's projects,
   clients and preoccupations, listed in a file nobody thinks of as a note" —
   the same is true here, and more so for a business.
6. **Regenerate `index.md`.** M's route map describes M, and its links would
   dangle. Generate a minimal one from what shipped.
7. **Copy the toolbelt** so P is a complete, runnable brain — `bin/`, hooks,
   the folder skeleton. P must be servable with no reference to M.
8. **Audit the output, not the input.** Over the generated tree: `scan_secrets`
   on every file; no note lacking `visibility: public`; no `sensitivity` value
   at all; no `[[wikilink]]` resolving outside P; no topic outside P's filtered
   vocabulary; no occurrence of M's absolute path. Then run `bin/brain lint`
   *inside P* and require it clean.
9. **On any failure: refuse, name every problem, and delete the output tree.**
   This is `cmd_template`'s behaviour and its comment says why — leaving an
   unclean copy on disk is how a refused build still gets published: the tree
   looks finished, the refusal has scrolled off, and nobody re-checks. Copy the
   reasoning, not just the `rmtree`.
10. **Report the diff against the previous build**: added, updated, and
    **removed**, with removals printed last and loudest. Take the previous
    manifest from P's own git history — P is a repo, so `git ls-files` before
    the rebuild is the manifest, and no separate state file is needed.

**Flags:** `brain publish <dest>` builds; `--dry-run` reports the diff and the
audit without writing; `--force` is **not** offered — there is no legitimate
reason to publish a tree that failed its audit.

- [ ] **Step 1: Write the failing tests.** A public note ships and a private one
      does not; absent visibility does not ship; disallowed frontmatter fields
      are absent from the output; a wikilink to an unpublished note is stripped
      to plain text and counted; `topics.yaml` contains only used topics; a
      planted secret fails the audit and the tree is deleted; a planted
      `sensitivity` fails; the output passes its own lint; a revoked note
      disappears on rebuild; the diff reports removals; `--dry-run` writes
      nothing.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `publish: compile P from M, and refuse to ship a tree that failed its audit`

---

### Task 5: `brain publish review` — the human gate

**Files:**
- Modify: `bin/brainlib/publish.py`, `bin/brain` (subcommands, usage)
- Test: `tests/test_publish.py`

**No MCP tool is added in this task, and none ever will be.** Approval is a CLI
act by a person. A tool that lists what is pending is a tool that enumerates M's
private notes to whoever holds a token.

**Interfaces:**
- `brain publish review` — lists notes with **absent** visibility, newest first,
  with id, title and first line. Notes already marked `private` do not appear.
- `brain publish approve <id>` / `brain publish deny <id>` — set the field,
  non-interactive, exit non-zero on an unknown id.
- Both write the frontmatter field in place and leave everything else untouched.

The non-interactive form is required, not optional: a plan that specifies only
an interactive loop specifies something that cannot be tested. An interactive
wrapper over these two commands is welcome afterwards and is not part of this
task.

**One thing to get right:** `approve` on a note that lint would reject as public
(personal sensitivity, `people/`, not current) must refuse and say which rule,
rather than writing a field that makes the next `publish` fail. The check
already exists from Task 3 — call it here rather than duplicating it.

- [ ] **Step 1: Write the failing tests.** `review` lists absent-visibility
      notes and omits `private` and `public` ones; `approve` and `deny` set the
      field and nothing else; an unknown id exits non-zero; `approve` refuses a
      note that violates a Task 3 cross-check and names the rule.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `publish: the review queue, and why it is not an MCP tool`

---

### Task 6: Documentation and the deployment runbook

**Files:**
- Create: `setup/runbooks/business-partition.md`
- Modify: `SETUP.md` (a new Part covering the two-brain deployment), `README.md`
  (what this does and does not protect), `AGENTS.md` (`visibility` and `source`
  in the note contract), `docs/superpowers/BACKLOG.md` (link this plan)

The runbook is the deliverable that decides whether any of the above is real,
because **the most likely failure of this whole design is operational**: someone
runs both brains on one host with one config to save a few dollars, and nothing
warns them, because everything still works.

It must contain:

- The two endpoints, concretely: `brain serve --read-only` run *from inside P*,
  and `brain serve --drop-box --source <bot> ` run against M, on separate ports
  with separate tokens.
- Separate hosts, separate credentials, and — stated as a requirement, not a
  suggestion — **network egress rules so the bot's host cannot reach M's host
  at all**, so the boundary is not merely a token being kept secret.
- The bot has no shell, no filesystem access to either repo, and no git.
- **An isolation checklist the operator can actually run**, from the bot's host:
  M's port refuses; M's repo path does not exist; the drop-box token cannot
  read; the read-only token cannot write. Each one a command with its expected
  output. A checklist is what makes this verifiable later, when somebody has
  moved a server and half-remembered why it was separate.
- The publish cycle: `review` → `approve` → `publish` → restart P's server, and
  the fact that **removals** in the publish report are the line to read first.
- What is NOT protected: everything in P is readable by any customer who talks
  the bot into reciting it. Curate P as if it were a public web page, because
  functionally it is one.

- [ ] **Step 1: Write the runbook and the doc changes**
- [ ] **Step 2: Run every command in the isolation checklist and correct the
      document to match what actually happened** — not what it should have
      done. The tunnel runbook was written this way and it is why item 9 was
      found.
- [ ] **Step 3: `bin/brain lint`, then the whole suite**
- [ ] **Step 4: Commit** — `docs: the two-brain deployment, checked against a running one`

---

## Self-review

Before declaring this done, answer each in writing:

1. **Can the bot's process reach M?** Not "is it configured not to" — is there
   any path, credential, mount or tool through which it could. Walk it from the
   bot's config outward.
2. **Does any drop-box response differ depending on M's contents?** Success,
   failure, timing, error text. Any difference is a read channel.
3. **Publish a brain containing one private note whose id is embarrassing.**
   Grep the entire output tree for that id. It must not appear — not in a
   wikilink, not in `topics.yaml`, not in `index.md`, not in a backlink.
4. **Revoke a note's approval and rebuild.** Confirm it is gone from P, and
   that the run said so.
5. **Supersede a published note.** Confirm the fact leaves P, the successor is
   unpublished, and the report made both visible.
6. **Did any new MCP tool appear?** Only `brain_capture` behaviour changed. If
   `tools/list` grew, something is wrong.
7. **Run the full suite** and record the count. 474 before this plan; every
   number added should be traceable to a task above.
8. **Would this survive a weak model?** Re-read each mitigation and ask whether
   it depends on the bot choosing correctly. If any does, it is not a control.
