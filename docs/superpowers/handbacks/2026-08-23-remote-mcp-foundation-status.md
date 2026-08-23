# Remote MCP Foundation — implementation status and evidence

Status as of 2026-08-23. Branch `remote-mcp-foundation`, head `a88d6e4`.

This is the spec's "Implementation handback", written while the work is still
in flight rather than at the end. It exists to answer one question for every
claim: **where does it show that this was done?**

The distinction it keeps throughout is the one the spec's acceptance gates
insist on, and it is not pedantry:

| Evidence | What it proves |
|---|---|
| **code** | the behaviour is written down |
| **test** | it does what it says under controlled conditions |
| **live** | it did it on the production VPS, against the real network |

A gate that asks for a demonstration is not satisfied by a passing test. Five
defects in this work were found by running things and would not have been found
by reading them — they are listed at the end, because they are the argument for
the distinction.

Nothing in this file contains a credential, an Access assertion, an owner
address, an account identifier, an audience, a tunnel id, or note content.
Live values live in `/etc/brain/` on the VPS and in the session's private
deployment notes, never here.

---

## 1. Where the work is

| | |
|---|---|
| Branch | `remote-mcp-foundation` (public engine repo) |
| Head | `a88d6e4` |
| Tests | **453 pass**, `python3 -m unittest discover -s tests`, exit 0 |
| Lint | `python3 bin/brain lint` → 0 errors, 0 warnings |
| Release tag | **none yet** — see Remaining, R7 |

Commits, oldest first:

| Commit | What it did |
|---|---|
| `2dc617e` | three roots: engine, data, state |
| `279f941` | Access assertion validation in the standard library |
| `afea4df` | the HTTP MCP server, dual-era |
| `33238eb` | the production stack: image, compose, tunnel, backup, runbook |
| `7c0d4c7` | review findings on backup, the drill and the timers |
| `3a101cd` | the runbook steps that were missing and the one that was wrong |
| `1ad30d2` | the Cloudflare provisioner, hardened and tested |
| `b855776` | `resultType` on every 2026-07-28 result |
| `f629a0d` | four more 2026-07-28 MUSTs, and the push that never happened |
| `0c3eba9` | the push outage, which had a mechanism and no coverage |
| `bc29bab` | the timers, and the two things that only fail once installed |
| `83edb5a` | doctor watches the backup age and the disk |
| `a88d6e4` | CI scans the public export |

New code, all Python standard library only:

| File | Lines | Holds |
|---|---|---|
| `bin/brainlib/rs256.py` | 214 | RSASSA-PKCS1-v1_5 verification over SHA-256 |
| `bin/brainlib/access.py` | 387 | JWKS cache, claim checks, the principal |
| `bin/brainlib/mcpcore.py` | 346 | one tool table, one validator, one dispatcher |
| `bin/brainlib/httpmcp.py` | 746 | Streamable HTTP, both protocol eras |
| `bin/brainlib/capture.py` | 357 | idempotency ledger and the backup queue |
| `bin/brainlib/eventlog.py` | 216 | the log that cannot contain the brain |
| `bin/brain-http` | 326 | the entrypoint |

Test modules: `test_rs256` 31, `test_remote` 87, `test_provision` 30, plus the
pre-existing `test_brain` 213, `test_osbackend` 51, `test_setup` 41.

---

## 2. What is done, and where it shows

### The engine

| Requirement | Evidence | Where |
|---|---|---|
| Configurable data root, backward-compatible local mode | code + test | `bin/brain:76` `ENGINE`, `:98` `state_dir()`, `:351` `hooks_path()`. All 453 tests pass in the combined layout unchanged. |
| Split mode never conflates engine and data | live | `/srv/brain/engine` (code) and `/srv/brain/data` (a separate git repo) are distinct trees on the VPS; a capture from the container committed into the data repo. |
| `core.hooksPath` correct in split mode | code | `bin/brain:351` — absolute in split mode, because the data repo holds no `.githooks/` and a relative path there turns the content gate off silently. |
| `brain template` refuses in split mode | code | `bin/brain` `cmd_template`, guarded on `ROOT != ENGINE`. |
| RS256 verification, no third-party dependency | test | `tests/test_rs256.py` — 31 tests, of which 7 are forgeries (zero-padding, short padding run with the digest pushed left, missing leading zero, wrong block type, SHA-1 DigestInfo, trailing bytes, absent separator). |
| Assertion validation: issuer, audience, expiry, type, identity | test | `tests/test_remote.py` `AssertionTests` — wrong audience, wrong issuer, `type: org`, expired, not-yet-valid, another person's address, unknown `kid`, tampered payload, `alg: none`, unknown Host. |
| Service-token identity is distinguished, not inferred | test | `AssertionTests.test_a_half_shaped_service_assertion_is_refused` — `common_name` with a non-empty `sub`, or with an email, is refused rather than guessed at. |
| Hostname↔audience pairing is the read/write boundary | test | `AssertionTests.test_the_wrong_audience_is_refused` — both directions. |
| Read-only endpoint advertises exactly four tools | test | `CapabilityBoundaryTests` (legacy and modern era). |
| A hand-built `brain_capture` on read-only is refused | test | `CapabilityBoundaryTests.test_a_hand_built_capture_call_on_the_read_endpoint_is_refused` — asserts the refusal **and** that the inbox is still empty on disk. |
| Trust signals reach the client verbatim | test | `RetrievalTests` asserts `[match mode:`, and `CaptureTests` asserts `[provisional — unconsolidated]` survives the transport. |
| Path containment | test | `RetrievalTests.test_a_path_outside_knowledge_is_refused` — traversal, absolute, `.git/config`, escape via `..`. |
| Transport limits and hostility | test | `TransportTests` — 405 on GET/DELETE, 413 over 1 MiB, 415 on wrong content type, parse error survives, 100k-deep nesting survives, batch refused, health leaks nothing, no Python version in `Server:`. |
| Dual-era MCP | test + **live** | `ModernEraTests` covers both eras. **Live: Claude Code negotiated `2026-07-28` against the production endpoint.** |
| The log cannot contain the brain | test | `EventLogVocabularyTests` — an unknown field raises, and a test asserts no field name exists that could hold a query, note body, address or assertion. |

### The capture transaction

| Requirement | Evidence | Where |
|---|---|---|
| Credential scan before any disk write | test + **live** | `CaptureTests.test_a_credential_is_refused_without_touching_disk` asserts the inbox is empty afterwards. |
| Atomic write: temp, fsync, rename, dir fsync | code | `bin/brain:2024` `atomic_write`. |
| Rollback scoped to one pathspec, never a broad reset | code + **live** | `bin/brain:2216` `_unstage_and_remove`. Demonstrated on the VPS by breaking the git index mid-capture: rc 1, `note` empty, inbox unchanged, tree clean. |
| No absolute path in a result | code | `bin/brain:2201` `_safe_detail`. |
| Idempotency on the client's request id | test | `CaptureTests.test_a_retry_with_the_same_request_id_returns_the_same_note` — asserts the **same** note line, not merely that a second note was avoided. |
| Content dedup, and `allow_duplicate` bypasses only that | test | `CaptureTests` — two tests. |
| The ledger stores no capture text | test | `CaptureTests.test_the_ledger_never_stores_the_capture_text` greps the SQLite file for the canary words. |
| Concurrency: no lost notes, no dirty tree | test | `CaptureTests.test_concurrent_captures_all_land_exactly_once` — six threads, distinct text so dedup cannot mask a loss. |
| Mutation audit carries no content | test | `CaptureTests.test_the_event_log_records_the_mutation_and_no_content`. |
| Writes fail closed in maintenance, reads continue | test | `MaintenanceTests`. |
| Rate limits, capture budget separate from reads | test | `RateLimitTests` — two tests. |
| Push outage → `backup_pending`, escalate, recover | test | `BackupQueueTests` — 8 tests with an injected git and a parameterised clock. |

### Deployment

| Requirement | Evidence | Where |
|---|---|---|
| Image contains engine only, no knowledge | code + **live** | `deploy/Dockerfile` enumerated `COPY` plus build-time assertions. **The build asserts `knowledge/` did not enter the image and passed.** |
| `.dockerignore` actually consulted | **live** | Moved to the repository root — BuildKit never read it at `deploy/`. Proven by the build assertion above now being meaningful. |
| Bare `python3`, sqlite3 FTS5, git, ssh present | **live** | Dockerfile assertions; the image built. |
| Non-root, read-only rootfs, caps dropped, no host port | **live** | `deploy/compose.yaml`; `docker compose ps` shows `8787/tcp` (EXPOSE metadata) and **no published port**; `ss -tulpn` on the host shows nothing on 8787 from the container. |
| Entrypoint refuses to start on a bad mount | code | `deploy/entrypoint.sh` — never `git init`s, never `mkdir`s the data root. |
| Dedicated tunnel, not the shared one | **live** | Tunnel `brain`, locally configured, **healthy with 4 edge connections**; the pre-existing `workhorse` tunnel untouched. |
| Tunnel secret never travelled | **live** | Generated from `/dev/urandom` on the VPS and written straight into `/etc/brain/tunnel-credentials.json`; never fetched, never printed. |
| Managed OAuth on the read-only endpoint | **live** | Access app configured: 15m access token, 336h grant, DCR with localhost+loopback, owner-email Allow. |
| Unauthenticated request gets Cloudflare's OAuth 401 | **live** | `POST https://brain-read.qodevia.com/mcp` → `401` with `WWW-Authenticate: Bearer … resource_metadata=…`. |
| The origin emits no 401 and no challenge | test + **live** | `AssertionTests.test_the_origin_never_emits_a_401_or_a_challenge`; live the origin returns 403 with no such header. |
| Discovery documents correct | **live** | PRM `resource` = `https://brain-read.qodevia.com/mcp` exactly; AS metadata advertises PKCE `S256` and a registration endpoint. |
| Owner OAuth works | **live** | **Claude Code authenticated end to end.** |
| Timers installed and each unit executed | **live** | `brain-backup.service` exits 0; `brain-maintenance` runs index and lint clean; `OnFailure=` reached `brain-alert@` (proven by a real failure). |

### Durability — all four layers

| Layer | Evidence |
|---|---|
| 1. Atomic write | code `bin/brain:2024`; **live** canary capture. |
| 2. Local commit | **live** — canary committed, tree clean afterwards, survived `docker compose restart`. |
| 3. Push to the private remote | **live** — deploy key authenticates as the data repo only; `2f16cb7..e4c8ace` pushed; **0 unpushed** immediately after a one-shot container exited. |
| 4. Encrypted archive | **live** — uploaded to R2 and verified; retention pruning works; **bucket lock: 3-day retention protection** so the credential that uploads cannot erase recent recovery points. |
| Restore | **live** — **full restore drill PASSED** on the Mac using the offline age identity: decrypted, `git fsck` clean, HEAD matches the manifest, lint clean, index rebuilt, temp copy destroyed. |
| Upgrade / rollback leaves newer knowledge intact | **live** — note under `0.1.0`, upgrade to `0.1.1`, note under `0.1.1`, roll back to the retained `0.1.0`: **both notes present**, history intact, tree clean, 0 unpushed. |

### Separation

| Requirement | Evidence |
|---|---|
| Public export carries no knowledge, secrets or deployment values | **live** — `brain template` run; export inventoried: deploy stack ships, knowledge is the 3-file skeleton, **zero tenant identifiers**, no `.git`, no archives. |
| CI scans the export from outside the command that makes it | code | `.github/workflows/gate.yml` job `export` — re-inspects the emitted tree with rules written in the workflow, plus gitleaks over the export. Verified both ways: clean today, and it **catches a planted account id**. |
| No private value in any tracked file | **live** | Repeated greps for the account id, zone id, team domain, both audiences, tunnel ids, owner address and IP across `HEAD` and the working tree: **zero hits**. |

---

## 3. Deliberate deviations

Each is recorded in the plan with the evidence that forced it.

1. **The origin answers 403, never 401, and serves no `/.well-known/*`.**
   Cloudflare Access owns the OAuth challenge for these hostnames and answers
   before the tunnel. A competing challenge would be shadowed, or would win and
   name an authorization server that cannot issue this client a token. Verified
   live: Access produces the 401.
2. **Dual-era MCP.** The spec cites revision `2025-11-25`; the current revision
   is `2026-07-28`, and the first real client negotiated it.
3. **No third-party dependency, including on the remote path.** The spec permits
   remote-only dependencies; the permission is left unused.
4. **DCR allows localhost and loopback redirects.** Required — the team domain
   rejects them otherwise, and Claude Code, `mcp-remote` and the MCP Inspector
   all use them. Account-wide, and recorded as such.
5. **App session duration raised to match the grant session.** They are separate
   clocks and the shorter wins.
6. **Consolidation is not scheduled.** The pinned runner is the `claude` CLI and
   the image does not contain it. `deploy/systemd/README.md` records the three
   options and why the gap is visible rather than silent.
7. **Stateless: no MCP session ids.** A MAY in the legacy revisions, removed in
   the modern one.

---

## 4. Five defects found by running, not reading

The argument for finishing the cutover properly rather than declaring it done.

1. **`resultType` missing on every modern result but one.** Found by Claude Code
   refusing `tools/list` on the first real connection. It had been added where
   the spec's *example* showed it, not where the *rule* required it.
2. **The push that never happened.** A capture through the one-shot maintenance
   container committed and never pushed: the hook backgrounds the push, and the
   container exit took the background job with it.
3. **`.dockerignore` in the wrong place.** BuildKit never read it, so every
   build would have uploaded `knowledge/` and `.git/` to the daemon.
4. **`217/USER` and a compose service-name typo.** Both only appear when a
   systemd unit is actually started.
5. **Inverted base64 padding** in the JWS decoder, which rejected most real
   input. Caught by the first test written against it.

---

## 5. What is not done

The remaining work is a numbered, ordered plan in
[`../plans/2026-08-23-remote-mcp-foundation.md`](../plans/2026-08-23-remote-mcp-foundation.md),
under "Remaining work". In summary, from an independent audit of all 194 spec
requirements — **69 done, 81 partial, 25 missing, 17 deliberate deviations**:

- `brain.qodevia.com` is **not cut over**, so `brain_capture` has never been
  exercised over the network. That single fact accounts for most of the
  "partial" column.
- No Access **service tokens**, so the headless fallback and its revocation gate
  are unmet.
- The **client compatibility matrix** has one row and it is half filled.
- No Cloudflare **tunnel/app health alerting**.
- No **release tag**; production runs a branch, which the spec forbids.
- The runbook lacks the **egress allowlist** and **SSH hardening** sections.
- `brain doctor` is RED on three **unreviewed consolidation branches** inherited
  from the old brain — an owner decision.

Cutover must not be declared complete until every applicable gate passes. It
does not yet.
