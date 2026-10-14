# Remote MCP Foundation — Implementation Plan

Implements [`../specs/2026-08-23-remote-mcp-foundation-design.md`](../specs/2026-08-23-remote-mcp-foundation-design.md).

The spec says what the design is. This says how it is built, and — more
importantly — records every place the world turned out not to match the spec,
with the evidence that settled it. The spec's own instruction is that external
capabilities change and the implementation reports observed behaviour rather
than remembered behaviour; this file is where that reporting lives.

Everything below marked `[verified 2026-08-23]` was executed against the
owner's own Cloudflare account, the live MCP specification, or this repository
on that date. `[docs 2026-08-23]` was read in the vendor's documentation and
not executed.

## Global constraints

Carried forward from `plans/2026-07-25-setup-foundation.md` and still binding:

- **Python 3.9 floor.** No `match`, no runtime `X | Y`, no `str.removeprefix`.
- **Zero third-party dependencies, everywhere, including the remote path.**
  The spec permits "remote-only dependencies packaged as an optional install
  surface". They are not taken. See "Why no PyJWT" below — the exemption exists
  and is deliberately left unused.
- **Never a credential in the repo.** Test fixtures spell it `BEARER`, never
  `TOKEN`. Real values live in `/etc/brain/` on the VPS and in Keychain here.
- **Absolute dates** in code, comments and docs.
- **No test binds a public interface** — `127.0.0.1`, port 0.
- **No test sleeps to observe a timeout** — clocks are parameters.
- **The suite stays green**: `python3 -m unittest discover -s tests`. It was 305
  tests when this plan was written and is 453 now; the number is not the point,
  never deleting one to make a change pass is.
- New files under `bin/` ship automatically: `cmd_template` publishes what
  `git ls-files` tracks. Machine-local artifacts go in `TEMPLATE_DROP` **and**
  `.gitignore`.

## What the world actually looks like

### The MCP specification moved

`2025-11-25` is not the current revision. **`2026-07-28` is** — a breaking
redesign that replaces the `initialize` handshake with per-request metadata,
makes `server/discover` mandatory, and adds `UnsupportedProtocolVersionError`
(`-32022`). `[verified 2026-08-23, modelcontextprotocol.io/specification/versioning]`

The spec requires the supported set to "cover the current MCP specification and
every older version required by the qualified client matrix". So the server is
**dual-era**, which the 2026-07-28 revision specifies explicitly and permits on
one endpoint: a request carrying modern `_meta` is served statelessly under the
new rules; an `initialize` request selects legacy semantics.

Legacy is what every client in the qualification matrix speaks today, so legacy
is the path that must be perfect. Modern is what stops this being obsolete on
arrival, and it is small: one extra method, one extra error, one version check.

### Cloudflare Access owns the whole pre-auth surface

Live probes against the owner's tenant settled the single biggest open question
in the spec — who serves the OAuth challenge and the metadata.

- Access with Managed OAuth serves **four** well-known documents on the
  application hostname, including the path-aware
  `/.well-known/oauth-protected-resource/mcp`. The origin sees none of those
  requests. `[verified 2026-08-23]`
- Cloudflare's documentation states that enabling Managed OAuth **replaces the
  401 response behaviour** on the protected application, and warns against
  enabling it if you rely on your own `WWW-Authenticate`. `[docs 2026-08-23]`
- Managed OAuth **hard-requires** the RFC 8707 `resource` parameter on
  `/authorize`. Omitting it produces an error redirect, never a login page.
  `[verified 2026-08-23]`
- The origin never sees the client's OAuth token: it is opaque, and what
  arrives is the ordinary Access assertion in `Cf-Access-Jwt-Assertion`.
  `[docs 2026-08-23]`

**Deviation 1 — the origin emits no `401` and no `WWW-Authenticate`, and
serves no `/.well-known/*`.** The spec's error contract lists `401: missing or
invalid authentication, with the OAuth challenge when applicable`. It is not
applicable: Cloudflare owns that response and answers it before the tunnel.
An origin that also emitted a challenge would either be shadowed (dead code) or
would win and hand a client an `authorization_servers` value pointing at the
wrong issuer. The origin therefore answers its own authentication and
authorization failures with **`403`**, so its refusals can never be mistaken
for Access's challenge. Every other status in the spec's contract is
implemented as written.

**Deviation 2 — dynamic client registration allows localhost and loopback
redirects.** The spec says to enable them "only when required by qualified
desktop clients". They are required: with no such app configured, the team
domain's registration endpoint **rejects** `http://localhost:<port>/callback`
and `http://127.0.0.1:…`, which is what Claude Code, `mcp-remote` and the MCP
Inspector all use. `[verified 2026-08-23]` Note the setting is a union across
every Managed-OAuth app on the account, so this is an account-wide relaxation
and is recorded as such.

**Deviation 3 — the application session duration is raised to match the grant
session.** `oauth_configuration.grant.session_duration` (the refresh lifetime)
is the spec's 14 days, but the app-level `session_duration` is a separate,
shorter clock, and the shorter one wins: a refresh that outlives the identity
session forces an interactive re-login the client cannot explain. Both are set
to `336h`. `[docs 2026-08-23]`

### Read versus write cannot be a scope

The tenant's authorization-server metadata advertises **no** `scopes_supported`
and the token response returns `"scope": ""`. There are no OAuth scopes, so the
read/write boundary cannot be expressed as one. `[verified 2026-08-23]`

It is therefore exactly what the spec chose: **two applications with two
audiences**. The two hostnames must not be merged into one multi-domain
application — an OAuth token obtained through any one domain of an application
is valid for all of them, which would make a read-only token work against the
capture endpoint. `[docs 2026-08-23]`

## Architecture

```text
                 Cloudflare edge
                 ├── brain-read.qodevia.com   Access app RO   aud_ro
                 └── brain.qodevia.com        Access app RW   aud_rw
                        │  Managed OAuth (browser) │ Service Auth (headless)
                        │  Cf-Access-Jwt-Assertion │
                        ▼
        ┌──────────── VPS: docker compose project "brain" ────────────┐
        │  cloudflared (container)  ──compose DNS──▶  brain:8787      │
        │      dedicated tunnel, local config, no host port           │
        │                                                             │
        │  brain (container)                                          │
        │      bin/brain-http  ──subprocess──▶  bin/brain             │
        │                                                             │
        │  brain-maintenance (one-shot, systemd timers)               │
        └──────┬──────────────────────┬───────────────────────────────┘
               │                      │
        /srv/brain/data          /srv/brain/state        /etc/brain
        private knowledge repo   index, locks, ledger    config + secrets
```

`cloudflared` runs as a **second, dedicated tunnel inside the compose project**,
not as the existing host unit. All replicas of one tunnel share one ingress
list, so reusing the `workhorse` tunnel would put the brain's routes in the same
configuration as five dev servers. `[verified 2026-08-23]` A dedicated tunnel
also means the brain reaches the edge over a connector that can be stopped
without touching anything else — which is the first step of the spec's incident
containment procedure.

The alternative — keeping the host connector and publishing a loopback port —
was rejected: it publishes a host port, which the spec forbids, and Docker's
published ports bypass the host firewall by DNAT'ing before `INPUT`.
`[verified 2026-08-23]`

## The three roots

Done. `bin/brain` now resolves three roots instead of one:

| Root | Holds | Configured by |
|---|---|---|
| `ENGINE` | code, `setup/`, `plugins/`, `.githooks/` | never — always the checkout this file runs from |
| `ROOT` | `knowledge/`, and the git repo every `git()` call runs in | `BRAIN_DATA_ROOT` |
| `state_dir()` | index, locks, digest, reports | `BRAIN_STATE_DIR` |

Locally all three collapse to the existing combined layout and nothing changes.
`--root` still beats both env vars, because `lint --staged` depends on it.

Consequences already handled:

- `core.hooksPath` becomes **absolute** in split mode (`hooks_path()`), because
  the data repository holds no `.githooks/`, and a relative path there
  configures a directory that does not exist — silently turning the content
  gate off.
- Both hooks resolve `bin/brain` from **their own location**, not from git's
  top-level, for the same reason.
- `brain template` **refuses** in split mode. It reasons about one tree; in
  split mode that tree is the owner's notes, with an allowlist written for the
  engine.

## Why no PyJWT

The spec permits remote-only dependencies. Taking that permission would put a
compiled cryptography stack inside the one component that decides whether a
request may read the owner's brain, and would end the repo's zero-dependency
property at exactly the point where it is worth most — a container that must be
minimal and auditable.

The whole cryptographic requirement is **RS256 signature verification against a
published public key**. `[verified 2026-08-23: the tenant's JWKS publishes two
RSA keys, both alg RS256, e=AQAB]` That is `pow(sig, e, n)` followed by an
**exact byte comparison** against a reconstructed EMSA-PKCS1-v1_5 block. It
involves no secret, so it has no timing channel, and the historical RSA
verification breaks (Bleichenbacher e=3 forgery) all come from *lenient*
parsing of the padding. A full-block reconstruction and a single `==` is immune
to them by construction, and is testable against fixed vectors.

Nothing else in the auth path is cryptography: issuer, audience, expiry, type
and identity are ordinary claim checks, and writing them by hand is a feature —
Cloudflare's own Python sample omits issuer verification and loops over keys
instead of matching `kid`. `[verified 2026-08-23]`

## Origin authentication contract

Every request, before the MCP body is parsed:

1. `Cf-Access-Jwt-Assertion` present (header only — the `CF_Authorization`
   cookie is never set on a bearer-token request). `[docs 2026-08-23]`
2. Header `alg == RS256`, `kid` matched against the cached JWKS from
   `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs`. A `kid` miss
   forces one rate-limited refetch, then fails closed. Keys rotate every six
   weeks with a seven-day overlap and exactly two are published.
   `[verified 2026-08-23]`
3. Signature verifies.
4. `iss` equals the configured team-domain origin exactly — never derived from
   the request Host, which is the *application* domain.
5. `aud` — **an array** — contains the audience configured for the hostname the
   request arrived on. Hostname and audience are checked as a pair and a
   mismatch fails closed.
6. `exp`/`iat`/`nbf` within a 60-second skew allowance. `nbf` is **not**
   required: service-token assertions do not carry one.
7. `type == "app"`. An `org` token is a valid signature over a different scope.
8. Identity:
   - **interactive** — `email` present and equal, after normalisation, to the
     configured owner;
   - **service** — `common_name` present **and** `sub == ""` **and** `email`
     absent, with `common_name` in the configured allowlist.
     `[docs 2026-08-23]` All three are tested; branching on `email` alone
     silently admits or silently rejects every headless request.

Any failure is `403` with a correlation id and no detail.

## Modules

New, all under `bin/brainlib/` so `bin/`-on-`sys.path` makes them importable:

| Module | Holds |
|---|---|
| `mcpcore.py` | the tool table, `validate_args`, and the dispatcher — one table for stdio and HTTP, with an `allow` set for the two profiles |
| `access.py` | JWKS cache, RS256 verification, claim checks, the principal |
| `rs256.py` | the verifier: base64url, DER/JWK parsing, EMSA-PKCS1-v1_5 |
| `httpmcp.py` | Streamable HTTP transport, dual-era dispatch, sessions, limits |
| `capture.py` | the capture transaction: lock, idempotency ledger, push queue |
| `eventlog.py` | the log that cannot contain the brain — allowlisted field names |

`bin/brain-http` is the entrypoint. `bin/brain-mcp` keeps working unchanged and
gains nothing: production stdio is not configured with a data root.

## Order of work

Steps 1-9 are **complete**. Step 10 is partly done and is the whole of what
remains; see "Remaining work" below.

1. ~~`rs256.py` + vectors.~~ Done first, because everything else trusts it.
2. ~~`access.py` + a JWKS fixture and a forged-assertion suite.~~
3. ~~`mcpcore.py` — extracted from `bin/brain-mcp`, then shared.~~
4. ~~`httpmcp.py` + `bin/brain-http`, legacy era first, then modern.~~
5. ~~`capture.py` — transaction, ledger, queue.~~
6. ~~`eventlog.py`.~~
7. ~~Docker image, compose, systemd units.~~
8. ~~Cloudflare provisioning script.~~
9. ~~Backup, restore drill, runbooks.~~
10. ~~Deploy, probe, and the cutover.~~ The client qualification matrix is
    the one part still open — see R3.

What each step produced, and the evidence for it, is in
[`../handbacks/2026-08-23-remote-mcp-foundation-status.md`](../handbacks/2026-08-23-remote-mcp-foundation-status.md).

---

## Audit findings — read this before calling anything finished

An independent audit on 2026-08-23 graded all 404 spec requirements and found
real defects in work recorded here as done, including three in the code path
that mutates knowledge. They are written up separately, with evidence and a
confidence tier per finding:

**→ [`2026-08-23-audit-remediation.md`](2026-08-23-audit-remediation.md)**

The remaining-work list below is still accurate about what was never started.
It is **not** a complete picture of what is left, because it says nothing about
the things that were finished incorrectly.

## Remaining work

An independent audit of all 194 spec requirements on 2026-08-23 returned
**69 done, 81 partial, 25 missing, 17 deliberate deviations**. Almost the whole
"partial" column has one cause: `brain.qodevia.com` is not cut over, so the
capture endpoint has never been exercised over the network.

**Closed since that audit:** ~~R0 the two credentials~~, ~~R1 the cutover~~,
~~R2 Access service tokens and the revocation gate~~, ~~R4 Cloudflare
alerting~~, ~~R5 the egress allowlist and SSH hardening~~, ~~R6 the live push
outage~~, ~~R7 the release, its identity, bill of materials and vulnerability
scan~~, ~~R8 the inherited consolidation branches~~. What each produced, with
the evidence, is in
[`../handbacks/2026-08-23-remote-mcp-foundation-status.md`](../handbacks/2026-08-23-remote-mcp-foundation-status.md).

**R1 is done, so the hinge has turned.** Both hostnames are served by the
container stack, there is exactly one writer, and `doctor` passes on the
migrated data with no RED. What remains is R3 and two things waiting on the
clock.

### ~~R0. Two credentials the owner has to make~~ — done

An API token scoped to Access, Tunnel, Notifications and zone DNS is installed
at `/etc/brain/cf-api.env`; `provision.py` now runs against the live account and
`--check` returns 4 (drift) for exactly the cutover items. Both service tokens
are minted, one year, secrets in `/etc/brain/service-tokens/`.

`brain-edge-check.timer` is installed and **still disabled on purpose**: the
only drift it would report today is the un-done cutover, so it would alert
nightly for a planned reason. Enable it as the last step of R1.

### ~~R1. Cut `brain.qodevia.com` over to the new stack~~ — done

Done 2026-08-23 in the runbook's order: out of the old tunnel's ingress first,
then the bypass policy deleted, then `provision.py --apply`, then DNS. The five
other hostnames on that tunnel were untouched. Access now owns the challenge;
before the cutover the same request was answered by the old server advertising
`scope="brain:read brain:write"`.

Capture over the network works, is idempotent on `client_request_id`, comes
back under `scope: all` tagged provisional, and pushes. `--check` reports the
edge converged.

The old copy is retired and read-only — and retiring it took more than `kill`:
it was a `systemd --user` unit with `Restart=always` on a lingering account and
came back in five seconds. Runbook step 10c now says where to look.

### ~~R2. Access service tokens, and prove revocation isolates~~ — done

Both tokens minted on the VPS, secrets written with `O_EXCL` mode `0600` and
never printed. Each has its own Service Auth policy naming that one token.
The gate — *one headless credential can be revoked without affecting another* —
was demonstrated with a third, disposable credential: before, both returned 200
and four tools; after revoking one, it returned 401 and the other still
returned 200 and four tools, on the same endpoint with the same profile.

Two things the doing taught, both now in runbook procedure 4: Cloudflare
**refuses to delete a service token while a policy references it**
(`12139 service_token_in_use`), so the order is policy then token; and Access
answers **401**, not 403, for a revoked credential on a Managed OAuth app.

The capture endpoint's Service Auth policy is declared in
`/etc/brain/cloudflare.json` and lands with the cutover — there is no point
binding a policy to an application still on `bypass`/Everyone.

### R3. The client compatibility matrix — the one open item

Three rows observed: Claude Code (OAuth, `2026-07-28`, four tools on the read
endpoint), and a headless service token against each endpoint. The rest need an
interactive login each.

Expect, from published defect reports: Gemini CLI should work; ChatGPT
connectors need developer mode; **Codex has open RFC 8707 defects** and may
authenticate then fail at token expiry — its supported path is the service
token, which now exists; **claude.ai web/mobile has a long-running unresolved
failure specifically against Access Managed OAuth** while Claude Code succeeds
on the same URL. A plan or client restriction is reported as such, never
disguised as a server failure.

One trap is already recorded: a client whose User-Agent looks automated is
refused by **Cloudflare error 1010** at the edge, before Access is consulted.
The 403 body is a Cloudflare error page rather than an Access challenge, which
is how to tell them apart.

### ~~R4. Cloudflare-side alerting~~ — done

Two policies are live and reconciled by `provision.py`; `--check` gives a timer
something to fail on; `brain-edge-check.timer` is installed and waits on R0a.
The gap that remains is deliberate and recorded: **nothing outside the box
knows whether the brain is answering.** Cloudflare's tunnel alert is explicit
that a tunnel can be healthy while the origin behind it is dead, and the
account's only HTTP-level notifier needs a paid Health Check. Closing that
needs an external prober this deployment does not have.

### ~~R5. Runbook: the egress allowlist and SSH hardening~~ — done

Procedures 11 and 12. The egress rules are written, scripted, unit-wrapped,
tested against a fake `iptables`, and **not applied** — the residual risk of
not applying them is written out rather than implied away. The SSH section
states the effective configuration as read on the box rather than the intended
one, and includes the drop-in ordering trap that makes `99-hardening.conf`
lose.

### ~~R6. Finish the durability evidence~~ — done, bar the clock

The **live push outage** was demonstrated twice against the real remote: the
capture was held with `backup: PENDING`, and the queue drained itself 45
seconds after the remote came back. The first run is what found the defect that
made the report untrue; the second was against the fixed build.

What is left is elapsed time, not work: the nightly backup and the monthly
restore drill are armed and both have been proven by hand, but neither has yet
run **unattended**.

### ~~R7. Release tag and the deployment discipline~~ — done

`v0.2.2`, deployed, engine checked out at the tag rather than a branch. One
version number now spans the git tag, the image tag, the OCI label and
`serverInfo.version`. SBOM: 129 components, zero third-party Python, asserted
rather than reported. Scan: 73 HIGH/CRITICAL and **zero with a fix available**,
which is the number that decides anything.

### ~~R8. Resolve the inherited consolidation branches~~ — done

Deleted, local and remote, at the owner's decision: start fresh. Merging was
never an option — their merge-base predated the commit that stripped the engine
out of the data repository, so a merge would have pushed ~32,000 lines of
engine files back into `knowledge/`. Each branch held seven inbox notes and
promoted nothing; two of the three overlapped heavily.

The commits (`1c3df2c`, `90a5394`, `1b922d6`) remain inside every encrypted
backup taken before the deletion, because the archive tars the whole data root
including `.git`. Recoverable by restore for as long as those recovery points
are kept.

`doctor` now passes with no RED.

### R9. Declare cutover complete

Cutover step 12, and it is close. Everything applicable passes except the
client matrix. The final handback owes: branch/commit and tag, test commands
and outputs, image digest and SBOM, sanitized service evidence, redacted
Cloudflare evidence, the separation scan and export inventory,
capture/concurrency/failure-injection results, git push and backup/restore
reports, the completed client matrix, and the deviations. No credential,
assertion, owner address, or note content in any of it.

Two housekeeping items to clear first, neither a gate:

- the object-storage keys pasted into a chat transcript are burned and live in
  `/etc/brain/backup.env`;
- the API token at `/etc/brain/cf-api.env` should carry a client-IP restriction
  to the VPS.

### Not in scope, deliberately

Consolidation scheduling stays out until a runner decision is made — see
`deploy/systemd/README.md`. Everything in the spec's "Non-goals for v1" stays
out; the audit confirmed none of it has crept in.

## Known client risks, before anything is built

From published, reproducible defect reports. `[docs 2026-08-23]`

| Client | Expectation |
|---|---|
| Claude Code | Implements RFC 8707 and enforces that PRM `resource` equals the configured URL exactly. Configure `https://brain.qodevia.com/mcp` with no trailing slash. Best-supported client. |
| Gemini CLI | Implements RFC 8707 correctly, path preserved. Same trailing-slash caution. |
| ChatGPT connectors | Send resource indicators as full URLs including the path. Expected to work; developer mode required. |
| Codex CLI | Open defects: has omitted `resource` on authorize, token exchange and refresh. Expect first-login success then failure at token expiry. **Service token is its supported path.** |
| claude.ai web / mobile / Desktop | Long-running unresolved failures specifically against Access Managed OAuth while Claude Code succeeds on the same URL. Not promised; validated as a separate, explicitly risky milestone. |

The spec requires an observed matrix, not an assumption. These are the
hypotheses the matrix will confirm or refute.
