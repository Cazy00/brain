# Remote MCP Foundation — implementation status and evidence

Status as of 2026-08-23. Branch `remote-mcp-foundation`, head `fcb3ae6`,
released as **`v0.2.4`** and deployed. **The cutover is done**: both hostnames
are served by the container stack, and there is exactly one writer.

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

A gate that asks for a demonstration is not satisfied by a passing test.
Fourteen defects in this work were found by running things and would not have been found
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
| Head | `fcb3ae6` |
| Release | **`v0.2.4`**, and the VPS engine is checked out at that tag, detached — not a branch |
| Image | `brain:0.2.4`, `org.opencontainers.image.version=0.2.4`, base `python:3.12-slim-bookworm`, arm64 |
| Image id | `sha256:f4f41caa…` — a **local** daemon digest; nothing is pushed, so no registry manifest digest exists |
| SBOM | `/var/lib/brain/sbom-0.2.4.json` — 129 components, **0 third-party Python packages** |
| Vulnerability scan | 73 HIGH/CRITICAL, **0 with a fix available** — all `affected`, `fix_deferred` or `will_not_fix` |
| Tests | **510 pass**, `python3 -m unittest discover -s tests`, exit 0 |
| Lint | `python3 bin/brain lint` → 0 errors, 0 warnings |

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
| `559107c` | this document, and the plan's remaining work |
| `bfd2466` | alerting: the two things only Cloudflare can see, and `--check` |
| `ecb4d08` | the alert filter Cloudflare advertises and then refuses |
| `bbb2ef0` | three runbook commands that would have failed when used |
| `fd64b7c` | one version number instead of three; the SBOM generator |
| `5f6129c` | a locked object and a lost permission were the same nothing |
| `658a908` | release 0.2.1 |
| `a0ffe40` | the local digest, called what it actually is on this daemon |
| `3226626` | release 0.2.2 |
| `7e73be4` | rotate a service token in the order Cloudflare actually allows |
| `729eca1` | the headless path, proven over the network |
| `81aef7d` | refuse an argument the tool does not declare |
| `81bf647` | stop claiming a push that has not happened |
| `fcb3ae6` | kill is not retirement |
| `bb749af` | release 0.2.4 — **deployed** |

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
| `bin/brainlib/version.py` | 21 | the release number, in one place |
| `deploy/sbom.sh` | 109 | the bill of materials, and the no-wheels assertion |

Test modules: `test_rs256` 31, `test_remote` 127, `test_provision` 47, plus the
pre-existing `test_brain` 213, `test_osbackend` 51, `test_setup` 41 — **510**.

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

### Alerting, and the release

| Requirement | Evidence | Where |
|---|---|---|
| Something outside the box notices the tunnel dying | **live** | Cloudflare notification policy `tunnel_health_event`, enabled, filtered to the brain's tunnel id — created through the API and confirmed by re-reading the account. |
| Something notices a headless credential expiring | **live** | Cloudflare `expiring_service_token_alert`, enabled. It fires 7 days out and will have nothing to say until a service token exists. |
| The alerts are addressed to somebody | **live** | `alerting/v3/destinations/eligible` → `email: eligible true, ready true`; pagerduty and webhooks are not eligible on this account. **Delivery itself is not proven**: `/policies/{id}/test` answers `15000` for both policies, so the first real delivery is the first proof. Recorded in the runbook rather than implied. |
| Alerting is reconciled, not clicked in | code + test | `deploy/cloudflare/provision.py` `step_notifications` and `_desired_notification`; `tests/test_provision.py` `NotificationTests` — 9 tests including "the owner cannot be removed from their own alerting" and "two policies with one name are refused". |
| The credential-expiry check is scheduled | code | `deploy/systemd/brain-edge-check.{service,timer}`, daily, `OnFailure=brain-alert@%n.service`. Installed on the VPS and **deliberately not enabled** — see Remaining. |
| A scheduled check can actually fail | code + test | `provision.py --check`: writes nothing, exits **2** when a human owes an action and **4** on drift. A plain dry run exits 0 whatever it finds, so scheduling that would have produced a green timer on the morning a credential lapsed. `CheckModeTests` — 6 tests, including that blocked outranks drift. |
| Production deploys a tag, never a moving branch | **live** | `git describe` on the VPS engine → `v0.2.2`, detached HEAD. `BRAIN_VERSION=0.2.2` in `deploy/.env`; compose refuses to start without it. |
| One version number, not three | code + test | `bin/brainlib/version.py`; the git tag is `v` + it, the image tag **is** it, the Dockerfile stamps it into `org.opencontainers.image.version`, and `serverInfo.version` reports it. `ReleaseVersionTests` — 4 tests. |
| Software bill of materials | **live** | `deploy/sbom.sh brain:0.2.2` → CycloneDX 1.5, **129 components**, `python.third_party_packages = 0`. Stored at `/var/lib/brain/sbom-0.2.2.json`. The generator **exits 77** if a third-party wheel ever appears, so that invariant is asserted rather than reported. |
| Vulnerability scan | **live** | Trivy against a saved tarball (not by mounting the daemon socket into a scanner): **73 HIGH/CRITICAL, 0 with a fix available** — every one `affected`, `fix_deferred` or `will_not_fix`. Nothing to apply; rebuilding would change nothing. Stored at `/var/lib/brain/vulnscan-0.2.2.json`. |
| Egress is a decision, not an oversight | code | Runbook procedure 11: the DOCKER-USER allowlist, the script that owns it, the systemd unit, the negative test, the rollback — and the residual risk written out for the case where it is not applied. `compose.yaml` pins the bridge name and subnet so the rules have an anchor that survives a redeploy. |
| The firewall script is not just prose | test | `RunbookFirewallScriptTests` — 7 tests; the script is **extracted from the runbook** and run against a fake `iptables`. Apply is idempotent across three runs, clear is exact, a foreign rule survives both. |
| SSH is stated rather than assumed | code | Runbook procedure 12: the effective `sshd -T` values as read on 2026-08-23, what to change and what each buys, the drop-in ordering trap (first-wins, so `99-` does **not** win), the three accounts with shells, and rate limiting — with the standing rule that SSH is never closed until a second, tested way in works. |
| A retention lock is not a failure | code + test | `deploy/backup/lib-s3.sh` `s3_delete` returns 0/2/1; `S3DeleteTests` — 4 tests. |

### Headless credentials, and the read/capture boundary over the network

Every row here is **live** — a request that left the VPS, crossed the
Cloudflare edge, and came back through the tunnel to the container.

| Gate | Result |
|---|---|
| A headless client can authenticate without a browser | `tools/list` on `brain-read` with `CF-Access-Client-Id/Secret` → **HTTP 200** |
| The read endpoint advertises exactly four tools | `brain_links`, `brain_read`, `brain_recent`, `brain_search` — **no `brain_capture`** |
| `resultType` on every modern result | `resultType: "complete"`, and `serverInfo` reports `{"name":"brain","version":"0.2.2"}` — the release number, end to end |
| A real read works over the network | `brain_search` → 200, results returned |
| A hand-built `brain_capture` on the read endpoint is refused | 200 with **`isError: true`** and a message naming the other endpoint — the MCP-correct shape for a tool refusal, not a transport error |
| A credential authorized elsewhere is refused here | the capture token at `brain-read` → **401** from Access, because the policy names one token rather than `any_valid_service_token` |
| **One headless credential can be revoked without affecting another** | before: both 200 / 4 tools. after revoking one: revoked → **401**, the other → **200, 4 tools**. Both sat on the same endpoint with the same profile, so the per-token policy is the only thing that separated them |
| `service_auth_401_redirect` enabled only after a Service Auth policy exists | set on `brain-read` after its policy; the read credential still returns 200 and the unauthenticated `WWW-Authenticate` + `resource_metadata` challenge is unchanged |
| The origin enforces the profile boundary itself | `bin/brainlib/access.py:335` — a credential mapped to one profile is refused at another with `wrong_profile`, independent of Access policy (unit-covered; Access blocks it first in production, which is the point) |

Secrets never left the box: the tokens were minted by a script running **on**
the VPS that wrote `client_secret` straight to `/etc/brain/service-tokens/`
with `O_EXCL` and mode `0600`, and printed only name, client id and expiry.

### The client matrix

Filled from the **server's own event log**, not from anyone's recollection.
`client` and `client_version` come from the client's `clientInfo` — modern
clients in `_meta`, legacy ones in `params` — so they are self-reported, which
is worth stating because nothing verifies them.

| Client | Version | Endpoint | Revision | Auth | What was observed |
|---|---|---|---|---|---|
| `claude-code` | 2.1.241 | read | **2026-07-28** (`server/discover`) | ✔ OAuth | discovery ok |
| `claude-code` | 2.1.241 | read | **2025-11-25** (`initialize`) | ✔ OAuth | legacy handshake ok — *the same client, both eras* |
| `claude-code` | 2.1.241 | capture | **2026-07-28** (`server/discover`) | ✔ OAuth | discovery ok |
| `codex-mcp-client` | 0.149.0-alpha.4 | capture | **2025-06-18** (`initialize`) | ✔ OAuth | initialize ok, twice; no `tools/list` or `tools/call` seen yet |
| service token | — | read | 2026-07-28 | ✔ `CF-Access-Client-*` | 4 tools; `brain_search` ok; `brain_capture` **denied** |
| service token | — | capture | 2026-07-28 | ✔ `CF-Access-Client-*` | 5 tools; capture, idempotency, the outage drill |

**The dual-era decision stopped being hypothetical.** One client, one
afternoon, negotiated `2026-07-28` on one request and `2025-11-25` on another,
and a second client arrived speaking `2025-06-18` — the oldest revision this
server accepts. Three revisions across two clients in a single log. A server
that had implemented only the revision the spec named would have refused two of
those three.

**Honest gap:** every `tool_call` in the log so far is from a *service*
principal. Interactive clients have authenticated, discovered and initialized;
none has yet invoked a tool. The rows above say so rather than implying a
working end-to-end path that has not been exercised. Using each client once —
a single search — closes it.

Two traps already recorded for whoever finishes this. A client whose
User-Agent looks automated is refused by **Cloudflare error 1010** at the edge,
*before* Access is consulted; the tell is that the 403 body is a Cloudflare
error page rather than an Access challenge. And Cloudflare's own Access request
log leaves `user_agent` empty for these applications [verified 2026-08-23,
27 entries, all `allowed: true`], so it can confirm that a login succeeded but
not which client made it — the brain's event log is the only place that knows.

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

## 4. Fourteen defects found by running, not reading

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
6. **A filter Cloudflare advertises and then refuses.** The account's own
   `available_alerts` lists `new_status` for `tunnel_health_event`. The API
   rejects every documented status, every capitalisation, with
   `17108: invalid new_status input`. The provisioner defaulted it for exactly
   one commit — the fake account accepted it happily, so only the real one
   could have found this.
7. **Four runbook commands naming a compose service that does not exist.**
   `brain-maintenance` is the systemd unit and the container prefix; the
   service is `maintenance`. They sat in the upgrade, rollback and
   corrupt-data procedures, each correct-looking and each failing at the
   moment somebody reached for it. The test written after the systemd version
   of this bug only looked at the units.
8. **Two runbook probes against a host port that does not exist.**
   `curl localhost:8787/readyz` was the stated proof that an upgrade or a
   rollback had worked. Nothing is published on the host — that absence is the
   first control in the whole network design.
9. **A network change that breaks `compose run` until a full `down`.** Pinning
   the egress bridge name made compose want to recreate the network, which it
   cannot do while containers are attached. Every nightly maintenance run
   would have failed after the engine was updated. Found by the upgrade
   procedure's own preflight step, which is what that step is for.
10. **A capture claiming a push that had not happened.** The live outage drill
    reported `backup: pushed to the private remote` while the commit sat
    unpushed. `_pending_since` means "a push was TRIED and failed"; in the
    moment after a commit nothing has been tried, so the flag was clear and
    `state()` read that as success. Every capture this system ever served said
    "pushed" the instant it committed. That line is the caller's only signal
    about durability, and a wrong one is worse than none.
11. **An undeclared argument silently dropped.** A probe sent `request_id`
    where the tool declares `client_request_id`. The capture succeeded and the
    retry-safety it asked for was off, with nothing said — the exact failure
    that argument exists to prevent. It is not only capture: a dropped `scope`
    turns an explicit search of the inbox into one that never looked at it.
12. **`kill` is not retirement.** The old server was stopped, its port checked,
    and it was back in five seconds under a new pid: a `systemd --user` unit
    with `Restart=always` on an account with `Linger=yes`. Invisible to
    `systemctl list-units`, to `crontab -l`, and to a grep of
    `/etc/systemd/system` — all three of which had just been run and all three
    of which said there was nothing.
13. **Cloudflare refuses to delete a service token a policy still references**
    (`12139`). The rotation procedure said "delete the old token and its
    policy" with no order, and the order is the whole thing.
14. **A retention lock and a lost permission reported as the same nothing.**
    The prune wrote `s3_delete "$key" && note "pruned $key"`, so a 409 from the
    bucket's object lock (expected, nightly, forever) and a 403 from a
    credential that had lost its delete permission were both silent. Found by
    reading a real backup run's log rather than the code.

---

## 5. What is not done

The plan's remaining work is in
[`../plans/2026-08-23-remote-mcp-foundation.md`](../plans/2026-08-23-remote-mcp-foundation.md).
R0 through R8 are closed. What is left is genuinely small, and none of it is
blocked on anything except other people's software and the passage of time:

- **The client matrix (R3).** Three rows observed — Claude Code, and a headless
  service token against each endpoint. Codex, ChatGPT connectors, Gemini CLI and
  claude.ai web each need an interactive login, and the published defect
  reports say at least two of them will fail in ways that are not this server's
  doing. A plan or client restriction is to be reported as such, never
  disguised as a server failure.
- **Two timers have not yet fired on their own.** The nightly backup and the
  monthly restore drill are armed and have both been proven by hand; what is
  missing is one unattended run each, which is elapsed time rather than work.
- **The egress allowlist is written and not applied.** Procedure 11, with the
  residual risk stated rather than implied away. Applying it needs a
  `compose down`/`up` for the pinned bridge name, and it is the one change here
  whose failure mode is a dead tunnel, so it wants its own window.
- **The final handback (R9).** Owed once the matrix is filled in.

Two housekeeping items that are not gates but should not be forgotten: the R2
storage keys pasted into a chat transcript are burned and live in
`/etc/brain/backup.env`, and the API token now standing at
`/etc/brain/cf-api.env` should carry a client-IP restriction to the VPS.
