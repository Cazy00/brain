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
- **305 existing tests stay green**: `python3 -m unittest discover -s tests`.
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

1. `rs256.py` + vectors. **Done first, because everything else trusts it.**
2. `access.py` + a JWKS fixture and a forged-assertion suite.
3. `mcpcore.py` — extracted from `bin/brain-mcp` verbatim, then shared.
4. `httpmcp.py` + `bin/brain-http`, legacy era first, then modern.
5. `capture.py` — transaction, ledger, queue.
6. `eventlog.py`.
7. Docker image, compose, systemd units.
8. Cloudflare provisioning script (idempotent, declarative desired state).
9. Backup, restore drill, runbooks.
10. Deploy, probe, and the client qualification matrix.

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
