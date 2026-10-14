# Remote MCP Foundation

Status: approved 2026-08-23.

This specification supersedes only the remote-access design under
`brain serve` in `2026-07-25-setup-ux-redesign-design.md`. In particular, the
single bearer token, the single permission profile, and the statement that the
tunnel is outside the project's concern are replaced here. The older spec's
local setup, connection, and retirement decisions remain in force.

## Why this exists

The brain is intended to be one persistent memory that every AI client can use.
Provider-owned memories cannot fill that role: ChatGPT, Codex, Claude, Gemini,
and future clients do not share one memory store. A local stdio MCP server also
cannot serve cloud clients and encourages multiple writable copies when it is
configured independently on several machines.

The production brain will therefore live on one always-on VPS and be reachable
only as an authenticated remote MCP server. All production clients use that
one service. Local stdio remains a supported mode for development and for other
self-hosters, but it never reaches this production data.

The public project is an empty engine and template. The owner's knowledge,
deployment configuration, credentials, Git history, and backups remain private
and are never published with it.

## Goals

1. Give remote MCP-capable clients one deterministic view of current brain
   knowledge.
2. Permit remote reads and provisional inbox capture without permitting remote
   canonical editing or administration.
3. Use browser OAuth for interactive clients, restricted to one owner identity.
4. Provide separately revocable credentials for headless clients that cannot
   complete browser OAuth.
5. Make the VPS the only production writer and prevent split-brain operation.
6. Keep Markdown under `knowledge/` as the only knowledge truth; indexes and
   session state remain derived and disposable.
7. Enforce a hard public-code/private-data separation suitable for a clean
   open-source release.
8. Make deployment, backup, recovery, and verification reproducible enough for
   a different agent to implement and for an independent reviewer to audit.

## Non-goals for v1

- Calendar, Reminders, Notion, Todoist, or task synchronization.
- Remote canonical editing, deletion, superseding, consolidation, reset, or
  administration.
- Multiple users, teams, sharing, or a hosted Brain SaaS.
- Mobile or web management interfaces.
- Vector search, embeddings, or cloud-hosted knowledge indexing.
- Multiple writable VPS replicas or automatic failover.
- Autonomous actions in external systems.
- Importing provider-specific ChatGPT, Claude, or Gemini memory.

Those are later projects. They must not expand the Remote MCP Foundation during
implementation.

## Decisions

| Decision | Rejected alternative | Reason |
|---|---|---|
| One production brain on the VPS | A writable copy per machine or provider | Multiple authorities create inconsistent memory and Git conflicts. |
| Every production client uses remote MCP | Mix local stdio and remote HTTP against the same data | Two write channels recreate split brain. |
| Docker Compose is the production deployment | Ad hoc host processes | Compose gives a repeatable, inspectable service boundary without changing local use. |
| Cloudflare Tunnel is the only network path | Publish a VPS port | The origin needs no public inbound application port. |
| Cloudflare Access Managed OAuth performs interactive login | Implement and maintain an OAuth authorization server | Access now supplies MCP-compatible OAuth discovery and browser authorization; custom token machinery adds security risk. |
| Two Access applications and endpoints | One authenticated endpoint with all tools | Separate audiences give a concrete, server-verifiable read-only boundary. |
| `brain.qodevia.com/mcp` is read + capture | Put write capability on every connection implicitly | The endpoint is explicitly selected for clients allowed to capture. |
| `brain-read.qodevia.com/mcp` is read-only | Depend only on instructions telling an agent not to capture | Tool discovery and handler authorization both enforce the boundary. |
| Cloudflare service credentials are the headless fallback | Add Brain-specific personal tokens in v1 | Access already provides named, expiring, renewable, independently revocable machine credentials. |
| Remote capture always commits | Expose local `commit: false` remotely | An uncommitted remote capture is neither durable nor auditable and can block consolidation. |
| Private Git is server-pushed history and backup | Bidirectional Git synchronization with local clones | Only the VPS may change production state. |
| A fresh-history public repository is produced from an allowlist | Make the current private repository public after deleting knowledge | Deleted files and secrets can remain in Git history. |
| Single writer, no horizontal write scaling in v1 | Multiple active Brain containers | Filesystem and Git mutations require one serialized authority. |

## System boundary

```text
Codex / ChatGPT / Claude / Gemini / future MCP clients
                         |
                         | MCP over HTTPS
                         v
        +------------------------------------------+
        | Cloudflare                               |
        |                                          |
        | brain.qodevia.com      read + capture    |
        | brain-read.qodevia.com read-only         |
        |                                          |
        | Access Managed OAuth / Service Auth      |
        +--------------------+---------------------+
                             |
                             | named Cloudflare Tunnel
                             v
        +------------------------------------------+
        | VPS / Docker Compose                     |
        |                                          |
        | cloudflared -> Brain HTTP MCP service    |
        |                         |                |
        |                         v                |
        |                    bin/brain core        |
        |                         |                |
        |                         v                |
        |               private mounted data      |
        +------------------------------------------+
```

There is no Cloudflare Worker and no Brain OAuth authorization server in v1.
Cloudflare terminates the public OAuth flow. The origin still authenticates
every request by cryptographically validating the Access assertion; the tunnel
is connectivity, not proof of identity.

No Brain HTTP port is published from Docker or opened in the VPS firewall.
`cloudflared` reaches the Brain service over a private Compose network. A local
administrator may manage the VPS over its independently secured SSH path, but
SSH is not a production Brain data interface.

## Public endpoints and capability profiles

| Endpoint | Access application | MCP tools |
|---|---|---|
| `https://brain-read.qodevia.com/mcp` | distinct read-only audience | `brain_search`, `brain_read`, `brain_links`, `brain_recent` |
| `https://brain.qodevia.com/mcp` | distinct read/capture audience | the four read tools plus `brain_capture` |

Both hostnames resolve through the same named tunnel to the same Brain service
and private data. The hostnames do not represent replicas.

The Access audience, not a request parameter, chooses the capability profile.
The origin requires the expected hostname/audience pair and fails closed on a
mismatch. `tools/list` omits unavailable tools, and the corresponding handler
authorization independently rejects a manually constructed call. Hiding a tool
is usability; handler authorization is the security boundary.

Remote clients can never reach canonical editing, `new`, `supersede`, delete,
consolidate, reset/retire, template publication, plugin installation, shell
execution, arbitrary paths, or administration.

## Authentication

### Interactive clients

Each hostname is a Cloudflare self-hosted Access application with Managed OAuth
enabled. The policy allows exactly the configured owner email. The public
repository contains `<OWNER_EMAIL>` only; the real address lives in private
deployment configuration.

Default OAuth settings:

- access token lifetime: 15 minutes;
- grant/refresh session: 14 days;
- dynamic client registration enabled;
- redirect URIs allowlisted;
- localhost and loopback redirects enabled only when required by qualified
  desktop clients.

An unauthenticated MCP request receives the OAuth `401` challenge generated by
Access. A compatible client follows protected-resource and authorization-server
metadata, opens a browser, and completes authorization code plus PKCE. Access
resolves its opaque client token and forwards a signed assertion in
`Cf-Access-Jwt-Assertion`.

The Brain origin must validate the assertion on every request:

1. obtain keys from the configured Cloudflare Access JWKS endpoint;
2. verify signature and accepted algorithm;
3. verify the exact issuer;
4. verify the audience for the selected endpoint;
5. verify expiration and any applicable time claims with a small documented
   clock-skew allowance;
6. for interactive identity, verify the normalized email exactly matches the
   configured owner;
7. reject a missing, invalid, stale, unknown, or mismatched assertion before
   parsing the MCP body.

JWKS is cached according to HTTP cache metadata, refreshed on an unknown key
identifier, and fails closed after the cached keys are no longer valid. Key
rotation must not require a deployment.

### Headless clients

Headless clients use Cloudflare Access service credentials only when browser
OAuth is impossible. Each client receives its own named credential, expiry,
and Access policy assignment. A credential is authorized against exactly one
of the two capability profiles and is never shared.

The required request form is the standard two Access headers. A single-header
service-credential mode may be enabled only after a live qualification proves
that it coexists with Managed OAuth on the same application and does not
confuse an OAuth bearer token with service authentication. It remains a
Cloudflare service credential, not a Brain bearer token. A client that supports
neither OAuth nor the qualified Access service-header form is incompatible with
v1; the implementation must not weaken authentication for it.

The origin accepts service authentication only when Access has forwarded a
valid signed assertion containing the documented service identity and the
correct application audience. Email is required for interactive mode, not
fabricated for service mode.

Service credentials expire after 90 days by default and alert seven days before
expiry. Deleting one must immediately block that client without affecting any
other OAuth session or service credential.

### Secret handling

No credential, token, key, real owner email, Cloudflare identifier tied to a
secret, or recovery identity enters either Git repository, a Docker image, a
command-line argument, application output, or test fixture.

Production secrets are root-owned files below `/etc/brain/`, mode `0600` where
applicable, and mounted into containers under `/run/secrets`. `.env.example`
documents variable names and safe placeholders only.

The Git deploy credential is limited to the private data repository. Cloudflare
automation credentials use the minimum account permissions needed for the
specific resource. The VPS holds only the public `age` recipient used to create
encrypted disaster-recovery archives; the recovery identity remains offline.

## MCP transport contract

Remote Brain uses MCP Streamable HTTP at `/mcp`. The local stdio MCP adapter
continues to work and remains Docker-optional. Remote-only dependencies are
packaged as an optional install surface and in the production image so the
zero-dependency local path is not silently changed.

The HTTP server supports:

- `initialize`;
- `notifications/initialized`;
- `ping`;
- `tools/list`;
- `tools/call`.

The server negotiates from an explicit supported protocol-version set and never
echoes an arbitrary client version as if it were supported. The set must cover
the current MCP specification and every older version required by the qualified
client matrix at release time.

The service may issue a cryptographically random MCP session identifier after
initialization. Sessions are bounded, expire, and contain no knowledge. A
session identifier is correlation state, never authentication; the Access
assertion is validated again on every request. A restart may require a client
to initialize again without affecting data correctness.

The HTTP request-body cap is 1 MiB. Media upload, attachments, resources,
prompts, arbitrary files, and arbitrary commands are not exposed in v1. The
server safely rejects malformed and deeply nested JSON, unsupported content
types, notification misuse, and unknown methods without terminating other
sessions.

Default operation timeouts are 30 seconds for reads and 120 seconds for
capture. A timeout terminates the child operation, releases or recovers the
lock safely, and returns a tool error with a request identifier.

## Tool contract

The existing names, meanings, and trust-bearing plain-text output remain the
compatibility contract. In particular, provisional, journal, archived,
superseded, and passed-`review_by` warnings must reach the client verbatim.
Structured content may be added later, but it cannot replace or weaken that
text in v1.

### `brain_search`

Inputs:

- `query`: required non-empty string, remote maximum 2,048 characters;
- `scope`: optional `canonical | all`, default `canonical`;
- `limit`: optional integer `1..20`, default `8`.

`canonical` remains the normal retrieval surface. `all` may include journal and
inbox material, and every such result remains mechanically tagged provisional
or journal. Archive and vault stay excluded.

### `brain_read`

Input: required `id_or_path`, maximum 512 characters.

An ID resolves through the supersede chain to current knowledge. A path must be
repository-relative and resolve to a Markdown file within `knowledge/` after
normalization and symlink resolution. Absolute paths, traversal, non-Markdown
files, and symlink escapes are refused. Trust and backlink context remains in
the response.

### `brain_links`

Input: required `id_or_path`, with the same containment contract as
`brain_read`. Output preserves inbound/outbound link semantics and trust state.

### `brain_recent`

Input: optional `days`, integer `1..365`, default `7`. Results retain the
existing maximum and provisional/journal labels.

### `brain_capture`

This tool exists only on the read/capture endpoint.

Inputs:

- `text`: required UTF-8 text, `1..65,536` characters;
- `client_request_id`: optional opaque string, maximum 128 characters;
- `allow_duplicate`: optional boolean, default `false`.

There is no remote `commit` option. A successful remote capture is always
credential-scanned, atomically written into `knowledge/inbox/`, committed to
the private local Git history, and queued for remote push. The result contains
the repository-relative inbox path or identifier, explicit `provisional`
status, local commit ID, and backup state. It never returns an absolute path.

The tool description preserves the capture policy: capture the why, not the
recoverable what; never capture credentials; ask before capturing another
named person's sensitive private life, health, finances, or relationships.

## Error contract

HTTP-layer errors:

- `401`: missing or invalid authentication, with the OAuth challenge when
  applicable;
- `403`: authenticated identity or audience lacks the requested capability;
- `413`: HTTP request or capture exceeds its limit;
- `415`: unsupported content type;
- `429`: rate limit exceeded, with a bounded retry indication;
- `503`: maintenance mode, unavailable write authority, unhealthy data mount,
  or another fail-closed dependency.

Protocol errors use JSON-RPC errors. Tool validation and execution failures use
MCP tool results with `isError: true` so one malformed call cannot kill a shared
server. Responses contain a safe correlation identifier, never a stack trace,
credential, note body, raw query, full tool result, or absolute path.

## Data authority and concurrency

The VPS private data repository is the canonical runtime location and the only
production writer. Its private Git remote is server-pushed history, audit, and
offsite backup; it is not a bidirectional editing surface. Local clones do not
push production changes.

Only one write-capable Brain service instance runs in v1. Capture and internal
maintenance use one inter-process repository lock stored on the private state
volume. The lock covers branch checks, filesystem mutation, Git index mutation,
commit, consolidation branch switching, and any operation that can change
canonical data. Reads may run concurrently except for the short atomic
replacement of derived indexes.

### Capture transaction

1. Authenticate and authorize.
2. Validate schema and size.
3. Scan the complete rendered note for credential patterns before disk write.
4. Deduplicate a retried request using the authenticated profile, MCP
   session/request identity, optional `client_request_id`, and a bounded recent
   HMAC fingerprint ledger that never stores raw capture text.
   `allow_duplicate: true` explicitly bypasses content deduplication while
   preserving request-ID idempotency.
5. Acquire the repository write lock.
6. Verify the production branch and expected clean/known repository state.
7. Write and fsync a temporary file on the same filesystem, then rename it
   atomically into the inbox.
8. Stage only that path and create a local commit.
9. If validation or commit fails, remove/unstage only the attempted capture and
   return failure. Never use a broad destructive Git reset.
10. Record the idempotency result, release the lock, and acknowledge success.
11. Push through the one production backup queue.

A capture is accepted only after the local commit exists. A remote Git push
failure does not erase or reject that accepted capture: the result reports
`backup_pending`, the queue retries with bounded exponential backoff, and an
alert fires after 15 minutes.

Unexpected uncommitted or untracked repository changes, a consolidation branch
at the wrong time, a broken Git index, an unwritable data volume, or a failed
invariant places mutation in fail-closed maintenance mode. Reads remain
available where safe. The system never auto-merges or guesses at conflicts.

Inbox notes remain provisional and excluded from canonical search until the
one pinned consolidator promotes or deletes them. The existing independent
propose/audit boundary remains mandatory. Consolidation is internal VPS
maintenance and is not an MCP tool.

Indexes, session records, JWKS cache, retry ledger, and lock metadata are
derived state. They can be rebuilt or discarded without changing knowledge.

## Code/data separation

The engine and the owner's data are different artifacts:

- public engine repository: `bin/`, tests, empty `knowledge/` skeleton,
  templates, setup/runbooks, Docker files, and examples;
- private data repository: actual `knowledge/`, permitted private Git history,
  and no engine secrets;
- private deployment configuration: `/etc/brain/`, outside both repositories;
- encrypted disaster-recovery objects: private S3-compatible storage.

Brain gains a configurable data root. Backward-compatible local mode defaults
to the existing combined repository layout. Production split mode uses:

```text
/srv/brain/data/    private knowledge Git repository
/srv/brain/state/   indexes, locks, sessions, and retry state
/etc/brain/         private configuration and secret files
```

Templates and executable code resolve from the installed engine. Knowledge and
all knowledge Git commands resolve from the configured private data root.
Production stdio is not configured with that root.

The public image contains code and empty templates only. Private data and state
are bind-mounted at runtime and cannot enter an image layer or build context.

The current private repository must never be made public in place. Public
publication uses an allowlist export, extending `bin/brain template` or an
equivalent deterministic command, into a new directory with fresh Git history.
CI scans that export and fails on unexpected paths, real knowledge, credentials,
private deployment values, Git metadata, archives, or backups.

## Docker Compose deployment

Production has three service roles:

1. `brain`: long-running HTTP MCP service;
2. `cloudflared`: named outbound Tunnel connector;
3. `brain-maintenance`: one-shot profile invoked by systemd timers for
   consolidation, lint, index, doctor, backup, and restore verification.

The Brain container:

- runs as a fixed non-root UID/GID;
- drops all Linux capabilities;
- sets `no-new-privileges`;
- uses a read-only root filesystem and a small `tmpfs` where required;
- receives writable mounts only for private data and derived state;
- publishes no host port;
- has a local-only health check;
- uses bounded CPU/memory/process limits documented for the reference VPS;
- writes logs to stdout/stderr with Docker rotation and never logs payloads.

The `cloudflared` container shares only the network needed to reach Brain and
uses a mounted tunnel credential. Both images are pinned to immutable release
versions or digests. `restart: unless-stopped` is the reference restart policy.

The host firewall exposes no Brain port. Outbound access is limited as practical
to Cloudflare Tunnel, Cloudflare JWKS, the private Git provider, encrypted
backup storage, package/image registries during controlled updates, and the
configured consolidator provider. SSH hardening is documented but remains
separate from the Brain application protocol.

Production deploys tagged releases, never a moving branch. Upgrade flow:

1. confirm lint, doctor, Git push, and backup freshness;
2. create a pre-upgrade encrypted snapshot;
3. pull the pinned image;
4. run backward-compatibility/migration preflight;
5. restart and run live acceptance probes;
6. retain the previous image and snapshot for rollback.

Software rollback never rewrites or discards knowledge created by a newer
version. An irreversible data migration requires a separately approved design.

## Cloudflare configuration

The implementation creates:

- proxied DNS/tunnel routes for `brain.qodevia.com` and
  `brain-read.qodevia.com`;
- two self-hosted Access applications with distinct audiences;
- owner-email Allow policies for interactive OAuth;
- Managed OAuth and dynamic client registration settings;
- separate Service Auth policies for headless credentials;
- redirect-URI entries proven necessary by the client qualification matrix;
- tunnel and application health alerting;
- general abuse/rate-limit controls that do not inspect or log Brain content.

Desired state is captured as declarative infrastructure where the current
Cloudflare provider supports the required fields. A small idempotent API script
may cover a field not yet represented by the provider. Actual state files,
account IDs, application IDs, audiences, tunnel credentials, owner email, and
service credentials remain private.

Managed OAuth availability and RFC 8707 behavior must be verified in the
owner's Cloudflare account before implementation proceeds past the auth gate.
If the feature is unavailable or a required client cannot use it, the agent
must stop and report the incompatibility. It must not silently add a bypass,
shared bearer token, or custom OAuth server.

Application rate-limit defaults per verified principal are 120 read operations
and 10 captures per minute. Cloudflare may add a broader IP/network abuse limit.
Legitimate MCP initialization and token-refresh traffic must not consume the
capture budget.

## Logging and observability

Application logs record only:

- timestamp;
- correlation ID;
- authentication mode;
- HMAC-derived stable principal identifier, never raw email;
- capability profile;
- asserted MCP `clientInfo` name/version, marked untrusted metadata;
- tool name;
- success/error category;
- latency;
- opaque mutation event ID and local commit ID for a mutation;
- backup state.

They never record OAuth/service credentials, Access assertions, raw email,
capture text, search query, note body, full response, environment, or absolute
path. Runtime logs rotate with 30-day retention. Mutation audit metadata has a
180-day default retention. Cloudflare's own Access logging is governed by the
account policy and documented separately.

Local liveness proves that the HTTP/MCP process responds. Readiness proves that
configuration is valid, Access keys are usable, private data is present, Git
and locks work, and adequate disk remains. Health output contains no private
names or content.

Alerts cover:

- Brain or tunnel unavailable;
- disk space below 20%;
- private Git push pending more than 15 minutes;
- encrypted backup older than 24 hours;
- lint, consolidation, index, or restore-drill failure;
- service credential within seven days of expiry.

There is no product telemetry by default.

## Backup and recovery

Four durability layers are required:

1. atomically written files on the VPS;
2. a local Git commit for every accepted mutation;
3. automatic push to the private Git remote;
4. a nightly fully encrypted archive in S3-compatible object storage, with
   Cloudflare R2 recommended for the Qodevia deployment.

Encrypted object retention defaults to seven daily, four weekly, and twelve
monthly recovery points. Object versioning or retention protection is enabled
when the selected storage supports it. The archive includes knowledge, private
Git history, and the minimum configuration metadata needed to rebuild, but no
plaintext secret export. A separate encrypted secrets-recovery procedure is
documented.

Targets:

- recovery point objective: at most 15 minutes of accepted captures;
- recovery time objective: service restored on a replacement VPS within two
  hours;
- no high-availability promise in v1.

A monthly automated restore drill restores the latest encrypted archive into a
temporary isolated directory, runs lint, rebuilds indexes, verifies Git history,
and executes deterministic search/read canaries. It then destroys the temporary
copy and retains only result metadata. A runbook covers lost VPS, corrupt data,
lost private Git remote, compromised credentials, and bad software deployment.

Incident containment begins by disabling the tunnel, revoking OAuth grants and
service credentials, preserving an encrypted diagnostic snapshot, rotating
secrets, and restoring onto a clean host. No recovery step makes the old local
copy writable alongside the replacement VPS.

## Migration and cutover

1. Run full tests, `brain lint`, and `brain doctor` on the existing private
   brain.
2. Create and verify an encrypted pre-migration backup.
3. Freeze local production writes.
4. Create the private split-mode data repository while preserving private
   knowledge history as documented.
5. Transfer it to the VPS over an authenticated encrypted channel and compare
   an inventory plus cryptographic hashes.
6. Deploy the pinned public Brain image with private data/state/config mounts.
7. Configure and verify both `qodevia.com` MCP endpoints.
8. Exercise OAuth, read-only denial, capture, Git push, backup, restart, and
   isolated restore.
9. Replace every production client configuration with its chosen remote MCP
   endpoint.
10. Disable local stdio access to production data.
11. Retain the previous local copy only as an offline recovery copy.
12. Declare cutover complete only after every acceptance gate passes.

## Failure behavior

| Failure | Required behavior |
|---|---|
| Cloudflare or tunnel unavailable | No public access; data remains intact; alert. |
| Access assertion absent/invalid | Reject before MCP parsing. |
| Wrong endpoint audience | `403`; never downgrade or infer a profile. |
| JWKS temporarily unavailable with valid cache | Continue until cache validity boundary, then fail closed. |
| Private data volume unavailable | Readiness fails; no empty brain is created over the missing mount. |
| Capture validation/commit fails | Roll back only that capture and return `isError`. |
| Git remote unavailable | Local committed capture succeeds as `backup_pending`; queue retries and alerts. |
| Unexpected working-tree state | Mutations stop; safe reads continue. |
| Consolidation active | Shared locking prevents interleaving; capture waits or returns bounded retry. |
| Index missing/corrupt | Rebuild from Markdown or fall back to authoritative scan; never treat index as truth. |
| Container restart | Clients may reinitialize; committed knowledge is unaffected. |
| Rate limit exceeded | `429` with safe retry guidance; no partial mutation. |
| Encrypted backup fails | Git protection remains, alert immediately, retry; do not claim backup healthy. |

## Acceptance gates

The implementation is incomplete until all applicable gates pass.

### Repository and regression

- Existing `python3 -m unittest discover -s tests` passes.
- Existing stdio MCP behavior remains compatible.
- New unit/integration suites pass from a clean clone.
- `brain lint` and `brain doctor` pass on the migrated private data.
- Public export contains no private knowledge, secrets, deployment state,
  backups, or inherited private Git history.

### Authentication and authorization

- Unauthenticated HTTP receives the correct OAuth challenge.
- Valid owner OAuth works on both endpoints.
- Wrong issuer, audience, email, signature, expiry, and hostname/audience pair
  are rejected.
- Read-only `tools/list` contains exactly four tools.
- A manually constructed `brain_capture` call on read-only is rejected.
- Read/capture advertises exactly the five v1 tools.
- One headless credential can be revoked without affecting another.

### Tool and data integrity

- Search/read/links/recent preserve current trust warnings verbatim.
- `scope: all` tags inbox and journal material correctly.
- Traversal, absolute path, non-Markdown, and symlink-escape reads fail.
- Credential-like capture is refused without disk or Git mutation.
- A canary capture commits, returns provisional state, appears under
  `scope: all`, survives restart, pushes, and enters encrypted backup.
- Retried capture is deduplicated; `allow_duplicate` works deliberately.
- Concurrent capture testing produces no lost notes, duplicate request
  commits, branch leakage, or Git index corruption.
- A forced commit failure rolls back the attempted note only.

### Deployment and recovery

- No Brain port is reachable from the public Internet or published by Compose.
- Both tunnel routes reach the same intended service.
- Images run non-root with the specified hardening and immutable version.
- Push outage/recovery and `backup_pending` alerting are demonstrated.
- The encrypted R2/S3 archive restores successfully in isolation.
- Upgrade and image rollback leave newer knowledge intact.

### Client qualification

The delivery includes an observed compatibility matrix, not an assumption:

| Client | Read-only OAuth | Read/capture OAuth | Headless fallback |
|---|---|---|---|
| Codex | verified result required | verified result required | record if supported |
| ChatGPT | verified result or exact account limitation | same | not expected |
| Claude | verified result required | verified result required | record if supported |
| Gemini | verified result required | verified result required | record if supported |

Each observation records client/version, endpoint, authentication result,
advertised tools, a safe search/read probe, capture where permitted, revocation
where applicable, and the exact client-specific limitation. A plan or account
restriction is reported as such and is not disguised as a server failure.

## Implementation handback

The implementing agent returns:

1. implementation branch/commit and release tag;
2. exact test commands, exit codes, and summarized outputs;
3. container image digest, software bill of materials, and vulnerability scan;
4. sanitized `docker compose ps`, health, firewall, and service status evidence;
5. redacted Cloudflare DNS, Tunnel, Access application, audience, policy, and
   Managed OAuth evidence;
6. public/private separation scan and public-export inventory;
7. capture/concurrency/failure-injection results;
8. Git push and encrypted backup/restore reports;
9. completed client compatibility matrix;
10. deviations and known limitations.

No handback contains a credential, Access assertion, real owner email, recovery
identity, private note content, raw search query, or secret-bearing state file.

The architectural reviewer will inspect the implementation, rerun repository
tests, audit the security and data boundaries, compare every acceptance gate to
evidence, and return pass/fail with blocking findings. If direct live
verification is desired, the owner connects the reviewer through the normal
OAuth flow; credentials are never copied into the handback.

## Current primary references

These links describe the current external protocols and products that the
implementation must re-check at execution time:

- MCP authorization specification:
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization>
- MCP transport specification:
  <https://modelcontextprotocol.io/specification/2025-11-25/basic/transports>
- Cloudflare Access Managed OAuth:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/managed-oauth/>
- Cloudflare Access JWT validation:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/authorization-cookie/validating-json/>
- Cloudflare Access service tokens:
  <https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/>
- Cloudflare MCP authorization options:
  <https://developers.cloudflare.com/agents/model-context-protocol/protocol/authorization/>
- ChatGPT custom MCP developer mode:
  <https://help.openai.com/en/articles/12584461-developer-mode-and-full-mcp-connectors-in-chatgpt>
- Codex configuration reference:
  <https://developers.openai.com/codex/config-reference/>
- Claude Code MCP documentation:
  <https://code.claude.com/docs/en/mcp>
- Gemini CLI MCP documentation:
  <https://google-gemini.github.io/gemini-cli/docs/tools/mcp-server.html>

External capabilities change. The implementation report records the date and
observed behavior rather than presenting remembered provider behavior as fact.
