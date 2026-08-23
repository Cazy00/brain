# Remote MCP Foundation — audit findings and remediation

An independent audit of
[`../specs/2026-08-23-remote-mcp-foundation-design.md`](../specs/2026-08-23-remote-mcp-foundation-design.md)
against the code and the running system, 2026-08-23. It found real defects in
work that had been recorded as done, including three in the one code path that
mutates knowledge.

**This document exists to be picked up cold.** Nothing in it assumes you were
in the session that produced it.

---

## How to use this

Findings are in three tiers, and the tier is the instruction:

| Tier | Meaning | What to do |
|---|---|---|
| **A — verified** | Someone read the code and confirmed it, and the evidence below is a `file:line` you can open right now | Fix it. The evidence is good. |
| **B — reported** | An auditing agent claimed it; nobody has independently checked | **Verify before you fix.** Some of these will be wrong. |
| **C — known incomplete** | Already tracked, no surprise | Schedule it; do not re-litigate it |

Three working rules, because this codebase has a habit of catching its own
mistakes only when something is actually run:

1. **Write the failing test first.** Every Tier A finding below names the test
   that should exist. If the test passes before your fix, you have not
   understood the finding.
2. **Mutation-check it.** Re-break the code deliberately and confirm the new
   test fails. Several tests in this repo passed for the wrong reason; one
   "proves" the ledger holds no text and passes only because SQLite's WAL had
   not been checkpointed (finding **A3**).
3. **A passing unit test does not close a requirement that asks for a
   demonstration.** The spec's acceptance gates say "demonstrated". Where a
   finding says *run it*, run it on the VPS and keep the output.

### How these findings were produced

Seven auditors, one per group of spec sections, each enumerating every
requirement in its range and grading it against the repository and a dossier of
live system state. Every `done` verdict was then handed to a separate skeptic
whose only job was to refute it. **404 requirements: 238 done, 119 partial, 19
missing, 24 deliberate deviations.** The adversarial pass overturned fourteen
previously-`done` verdicts.

The tally is not the point and should not be quoted as a score. Many "partial"
verdicts are known incomplete work (Tier C). The findings below are what
matters.

---

## Tier A — verified by hand

Seven findings, each confirmed by reading the code named.

### A1. The capture lock does not cover the file write — a note can be filed on the consolidation branch while the caller is told it was not saved

**Evidence.** `bin/brain:2125` writes the note (`atomic_write`). `bin/brain:2143`
takes the lock (`with repo_lock():`). Consolidation takes the same lock
**twice**, releasing it in between: once around `git add -A` (`bin/brain:3681`)
and again around its commit (`bin/brain:3698`).

**The sequence.**

1. consolidate: `with repo_lock(): git add -A` → **releases the lock**
2. capture: `atomic_write()` puts the note in `knowledge/inbox/` — unlocked
3. consolidate: `with repo_lock(): git commit` → the note is committed onto
   `consolidate/*`
4. capture: finally takes the lock, sees the wrong branch, calls
   `_unstage_and_remove(path)` and prints
   **"NOT SAVED: … Nothing was left behind. Retry shortly."** (`bin/brain:2152`)

The note *is* saved — on a branch nothing searches — and the caller has been
told the opposite. Removing the working file also leaves a deletion in the
tree, which blocks the checkout back to `main`.

**Why it is not on fire today.** Consolidation is not scheduled on this
deployment (a recorded deviation — the pinned runner is not in the image). The
race cannot fire until consolidation runs, which is the obvious next thing
anyone will want to enable.

**Fix.** Hoist the lock so one region covers branch check → filesystem write →
index mutation → commit. Note that the comment at `bin/brain:2137` already
explains that the *branch test* was moved inside the lock to close a TOCTOU;
the write was left outside. `supersede` reportedly has the same shape
(`bin/brain:3148` before `:3158`) — verify that separately, it is Tier B.

**Test.** A capture racing a simulated consolidation must end in exactly one of
two states: committed on `main`, or not written at all. Never "reported lost,
committed elsewhere". Drive it with an injected hook between the two lock
acquisitions rather than with sleeps.

### A2. Fail-closed maintenance mode never engages in production

**Evidence.** `bin/brainlib/httpmcp.py:165` — `self.maintenance = None` — is the
only assignment outside a test. The only other is
`tests/test_remote.py:637`, which sets the attribute it is testing. The 503
gate at `bin/brainlib/httpmcp.py:611` is therefore unreachable in production.

The spec names five conditions that must trip it (dirty tree, consolidation
branch, broken index, unwritable volume, failed invariant). None is wired.
`setup/runbooks/remote-brain.md` states in prose that the system is "already in
fail-closed maintenance mode for mutations". It is not.

**Fix.** Either wire at least a dirty-tree check and an index probe on the
write path, or amend the spec's failure table to say these conditions are
handled per-call and delete the runbook sentence. Do not leave the mechanism
present and unreachable — that is worse than not having it, because it reads as
covered.

**Test.** Each condition, set for real, produces a 503 with `Retry-After` and
leaves reads working.

### A3. The idempotency ledger stores capture text in legible form

**Evidence.** `bin/brainlib/capture.py:119-121` stores `json.dumps(result)`.
`result["note"]` is the note's path, and the filename is a slug of the capture
text (roughly its first 40 characters). So the ledger holds a readable fragment
of every capture.

The module's own docstring says the ledger stores "**HMACs, never text**".

Worse, the test that asserts this property passes for the wrong reason: it
greps `capture-ledger.db` while SQLite's WAL still holds the string in
`capture-ledger.db-wal`. **The test cannot fail.**

Combined with **A4**, this is an unbounded, unencrypted, un-backed-up file of
capture-text fragments on the VPS.

**Fix.** Store fingerprint, kind, timestamp and status only. Reconstruct
anything else from the commit. Then fix the test to checkpoint the WAL (or
close the connection) before reading, and mutation-check that it now fails when
text is reintroduced.

### A4. `EventLog.prune()` is dead code

**Evidence.** `bin/brainlib/eventlog.py:159` defines it. There are **zero**
callers in `bin/`, `tests/` or `deploy/`.

The spec asks for 180-day retention. What happens instead is size-based
rotation, so `mutations.jsonl` — the audit trail — is discarded at around 10 MB
regardless of age. It also lives in the state directory that
`deploy/backup/snapshot.sh` deliberately excludes from the archive, so the
audit trail is neither retained nor backed up. The capture ledger is likewise
never pruned.

**Fix.** Call it — the maintenance timer is the natural place. Decide
explicitly whether the mutation audit trail belongs in the backup set; if it
does, it must move out of the excluded directory.

### A5. A rate-limited `initialize` sends two HTTP responses on one connection

**Evidence.** `bin/brainlib/httpmcp.py:448` calls
`self._allow_or_refuse(principal, "protocol", cid)` and **discards the return
value**. `_allow_or_refuse` (`:654-658`) sends a 429 itself via `_rate_limited`
and returns `False`. Line 455 then does `return self._send(200, …)` regardless.

So a client over its protocol budget receives a 429 **and** a 200 on one
keep-alive connection: HTTP framing corruption. Line 448 is also the function's
**only call site**, so the `protocol` bucket never actually refuses anything.

**Fix.** Obey the return value. **Test:** exactly one HTTP response per
request, asserted on the wire, for every bucket.

### A6. The public export ships the owner's live hostnames

**Evidence.** Run `python3 bin/brain template /tmp/exp` and grep the result for
the production zone: **six tracked files** carry the live hostnames, including
`setup/runbooks/remote-brain.md`, `deploy/cloudflared/config.yml.example`,
`deploy/cloudflare/desired-state.example.json`, and the spec, plan and handback
under `docs/superpowers/`.

`brain template` publishes by **denylist**, and `.github/workflows/gate.yml`
has rules for account ids, audiences, team domains and API tokens — but none
for hostnames. A hostname names a live deployment as surely as a tunnel id
does; that is the gate's own stated reasoning.

**Fix.** Publish by allowlist rather than denylist, and add a gate rule. Do
**not** hardcode the zone into a tracked file to make the rule work — that
recreates the leak in the checker. Read the zone from the private state file,
or fail on any hostname that is not a `<PLACEHOLDER>` shape.

### A7. Three real email addresses are in the public engine repository's history

**Evidence.** `git log --format='%ae%n%ce' | sort -u` returns three distinct
personal addresses. This is the repository the design calls the public engine
repo, and its history is already pushed.

Contained *only* if publication goes through `brain template`'s fresh-history
export, which nothing currently enforces.

**Fix.** Decide and write down: either publication is *only ever* via the
allowlist export into fresh history — enforced, not assumed — or the history
needs rewriting before anything is published. Note the addresses are already on
the remote; a local rewrite alone does not undo that.

---

## Tier B — reported by the audit, not independently verified

**Verify each before acting.** Grouped by area; `file:line` references are the
auditor's and may be stale or wrong.

### Capture transaction
- `supersede` has the same lock-ordering flaw as A1 (`bin/brain:3148` before `:3158`).
- No clean/known-state verification on the capture path — no `git status --porcelain`, no index probe. `consolidate` has one.
- Branch verification only tests `startswith("consolidate/")`; a detached HEAD or any other branch is accepted and committed to.
- The idempotency record is written by the parent *after* the child released the lock (`capture.py:325-330`), inverting the spec's ordering. A crash between commit and record means a retry with the same `client_request_id` writes a second note.
- The unlocked collision loop (`bin/brain:2129-2132`) lets two same-second captures race the same path.
- Two independent pushers race on every remote capture: `BackupQueue` and the `post-commit` hook.

### Protocol and process handling
- A capture whose child times out returns `500 / -32603`, not the `isError: true` tool result the error contract requires; `capture.py` has no exception handling around `_invoke`.
- `subprocess.run`'s kill reaches only the direct child; the `git commit` grandchild can survive and land the commit *after* the caller was told it failed — with no ledger entry, so a retry duplicates. Kill the process group.
- Restart does not drain an in-flight capture: `daemon_threads = True` means `server_close()` joins nothing, so the 130 s `stop_grace_period` buys nothing.
- Legacy-era unsupported-protocol-version answers a bare non-JSON-RPC `400`; the module's own comment explains why that breaks dual-era clients.
- `brain_read` accepts absolute paths inside `knowledge/`, which the spec forbids and the docstring claims it refuses.
- Tool error text concatenates raw child stderr (`bin/brainlib/mcpcore.py:356`) — a traceback with absolute paths reportedly reached a remote client, and a *successful* read was prefixed with local hook output.

### Alerting
- `brain-alert.sh` appends to a log file **on the machine that has the problem**, then calls an optional hook this repo never installs. The queue's 15-minute escalation writes a line nothing reads. Meanwhile `capture.py:397-399` tells the user *"The brain's operator has been alerted."* — reportedly false. The Cloudflare notification policies are the one real transport.

### Smaller
- Service tokens were minted with a **365-day** lifetime; the spec and the shipped example say 90 days (`2160h`). The example still contradicts reality. Either restore 90 days or write the reasoning down — this one is a live security-posture change and should not be left implicit.
- The ledger's dedup namespace is the profile string, not the principal, so two credentials on one endpoint share a namespace.
- `/run/secrets/principal_key` (`deploy/env.example`) vs `/run/secrets/principal-key` (runbook) — only one can be right.
- JWKS cache ignores served `Cache-Control` and has **no** test coverage for caching.
- `bin/brainlib/eventlog.py:95` claims "no `host`" while `endpoint` carries the request hostname.
- `clientInfo` is recorded without being marked self-reported in the record.
- `brain_links` does not tag per-neighbour trust state, unlike search and recent.
- A corrupt-but-fresh index returns `{"mode":"none","hits":[]}` — indistinguishable from "the brain does not know that". For a memory system a confidently empty answer is the wrong failure.
- The stdio surface regressed: `limit` went from unbounded to 1..100, so a call the previous local server accepted now returns `isError`.
- Runbook contradictions: `brain-consolidate.timer` appears in "What runs when" and does not exist; cutover step 10c.5 points at a re-sync note in step 7 that was never written; the freeze omits `git remote remove origin`, which the recovery procedure calls mandatory.
- `deploy/Dockerfile:25` claims compose deploys by digest. It deploys by tag.
- The frozen old copy is mode `0555`, world-readable, owned by `ubuntu`, on the internet-facing VPS, **with `origin` still configured**.
- `bin/brain lint` has not been run against the migrated production data — the green result on record is from a laptop checkout, not the VPS repo. `cmd_doctor` returns `0 if (ok and lint_code == 0)`, so doctor's exit code depends on a lint run nobody has shown.
- Missing outright: Cloudflare-side abuse/rate-limit rules (spec 473); Access logging policy documentation (spec 511); an *encrypted* pre-migration backup (spec 569 — what was taken was an Oracle boot-volume snapshot, and a `git bundle` went over scp unencrypted).
- No test exists for: the atomic-rollback branch of remote capture, journal tagging under `scope: all`, either timeout ceiling, JWKS caching, concurrent same-`request_id` retries, or the absence of a `ports:` stanza.

---

## Tier C — known incomplete, already tracked

No surprises here; see the remaining-work section of
[`2026-08-23-remote-mcp-foundation.md`](2026-08-23-remote-mcp-foundation.md).

- **The client matrix.** Two client families observed of five. Critically, **no
  interactive OAuth principal has ever completed a `tools/call`** — every tool
  invocation on record is a service credential driven by a script. The flagship
  user story is unproven end to end. Declaring some clients out of scope for v1
  is a legitimate answer; leaving the rows blank is not.
- **Egress allowlist** (runbook procedure 11) authored, not applied. Container
  egress is unrestricted. The script and unit exist only as heredocs inside the
  runbook — nothing version-controls them except the extraction test.
- **SSH hardening** (procedure 12) authored, not applied.
- **No timer has fired unattended.** All four are armed with `LAST -`.
- **No external port scan.** "No brain port reachable from the internet" is
  inferred from nothing being bound locally, never demonstrated from outside.
- **No restore-drill artifact** under `/var/lib/brain/drills/`.
- **No public engine repository** exists yet.
- **R2 object lock** is claimed in the handback but absent from the
  provisioning procedure — a rebuild from the runbook produces an unlocked
  bucket.
- **Application-health alerting** needs a paid Cloudflare Health Check.
- **R9 final handback** unwritten.
- The object-storage keys pasted into a chat transcript are burned and still in
  `/etc/brain/backup.env`.

---

## Deviations to accept and close

Twenty-four are reasoned and recorded at the point of divergence. They should
be closed, not re-argued: the origin answering 403 while Access owns the
challenge; unauthenticated `/healthz` and `/readyz` on the internal network; a
sessionless server; zero third-party dependencies even where the spec permitted
remote-only ones; consolidation unscheduled; backup and drill running on the
host rather than in the profile they protect; `cloudflared` on two networks
with a pinned request path; no `compose pull` for a locally built image;
`provision.py` instead of Terraform; three secret files owned by the container
uid rather than root; rotation instead of secret recovery; a verify-only
automated drill because the recovery identity is deliberately offline; freezing
at cutover rather than before transfer; and nine extra low-sensitivity fields
in the event log under a closed allowlist.

**One that should not be accepted as-is:** the 90-day → 365-day service-token
lifetime (Tier B). It is a security-posture change with no written reason and a
shipped example that still says otherwise.

---

## Definition of done

**Code, not documentation:** A1–A7 fixed with tests that fail first; the Tier B
capture-transaction and protocol items triaged and either fixed or explicitly
accepted in writing; one real alert transport wired to backup age, disk, push
backlog and failing timers — or `capture.py` stops claiming the operator was
alerted.

**Run, not written:** `lint` and `doctor` on the VPS against the real data with
output kept; all four timers observed firing unattended; one restore drill
leaving an artifact; one upgrade-then-rollback drill on the current line; one
concurrency drill covering same-`client_request_id` retries and a branch switch
under load, ending in `git fsck`; and **at least one interactive OAuth
principal completing `tools/list` → `brain_search` → `brain_capture`**.

**Then:** fill the client matrix to whatever subset is actually intended to be
supported, write R9 with the full image digest, and get a reviewer's verdict.

The security boundary — assertion validation, the two-audience profile split,
the doubly-enforced read-only boundary, path containment, the credential scan —
was examined closely and is sound. The gap is in the write path and in
alerting, and it is smaller than this document's length suggests.
