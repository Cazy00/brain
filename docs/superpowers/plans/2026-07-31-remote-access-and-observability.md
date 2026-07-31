# Remote access and observability — Implementation Plan

**Goal:** `brain serve` becomes an MCP server any hosted assistant can connect to
over the internet, through the MCP authorization spec rather than through a
vendor integration — and when it misbehaves there is something to look at.

After this plan, one `brain serve` process on one port answers three
populations at once: a local client holding an operator bearer token (works
today, must keep working), a hosted assistant that completed an OAuth flow, and
an unauthenticated client asking the discovery questions that start that flow.

**Source:** [handoffs/2026-07-31-remote-access-and-observability.md](../handoffs/2026-07-31-remote-access-and-observability.md).
Read it first. It records what the owner decided and what the previous session
checked, and this plan does not re-derive any of it.

**Predecessors:** [2026-07-29-serve.md](2026-07-29-serve.md) built the transport
and the token; [2026-07-29-serve-hardening.md](2026-07-29-serve-hardening.md)
added `--read-only` and the per-address backoff — `Limiter` is reused, not
replaced; [2026-07-30-business-partition.md](2026-07-30-business-partition.md)
added `--drop-box`, `source:` and `visibility:`.

**Closes backlog item 3**, and corrects it: it says dynamic client registration
is required. It is not — see below.

---

## Scope, fixed by the owner on 2026-07-31

Four answers, and they narrow this considerably:

1. **Code only, in this public template.** The real brain — the clone that holds
   the owner's actual notes — is **not** created here and nothing is deployed
   live. The owner will stand it up themselves afterwards and report back. So
   every task below ends at "tested and documented", never at "running".
2. **The public endpoint serves all five tools**, `brain_capture` included.
   Carrying the brain in a pocket is most of the point, and "remember this" from
   a phone is the half that needs a write tool.
3. **A named Cloudflare Tunnel on this host** is the deployment being written
   for. That decides one thing only — the server never learns its own public
   URL, and everything in OAuth is identified by that URL. See D2.
4. **Build the spec once; do not branch per provider.** The owner's words:
   *"it's all the same protocol … the same way of setting, if not similar."*
   That is also what the research says, so there is no tension to manage: no
   file below contains a vendor name outside a comment or a document.

## What this plan does NOT build, and why

- **Dynamic client registration (`/register`).** The MCP specification now marks
  DCR **deprecated**, retained only for authorization servers that cannot do
  Client ID Metadata Documents. Building it would mean shipping a deprecated
  mechanism *and* an endpoint that lets an unauthenticated caller create rows.
  Task 3 builds the two mechanisms that replaced it. The omission is documented
  with its exact failure signal, so somebody debugging a client that only
  speaks DCR recognises it in one line rather than in an afternoon.
- **JWT access tokens.** D3.
- **User accounts.** There is one user. D4.
- **`--drop-box --oauth`.** Refused, by name, like `--drop-box --read-only`
  before it. A drop box is an endpoint an untrusted bot holds a fixed token
  for; OAuth's consent step assumes a human at a browser, and there is none.
- **Creating or serving the owner's real brain.** Scope point 1.

---

## Global Constraints

Carried forward. Violating any of them fails review.

- **Python 3.9 floor.** No `match`, no `X | Y` at runtime, no `dict1 | dict2`,
  no `str.removeprefix`.
- **Zero third-party dependencies.** `bin/brain-mcp` advertises "zero
  dependencies and no vendor SDK" and that claim is load-bearing. An OAuth 2.1
  authorization server in the standard library is achievable and this plan
  commits to it deliberately rather than discovering it halfway — see D3.
- **The stdio path does not change.** Not one line.
- **The existing bearer path does not change.** A local client wired up today
  must keep working, byte for byte, with OAuth off *and* with OAuth on. This is
  the single most likely way this plan does damage.
- **No credential ever reaches the repo.** Tests use a `FileKeystore` in a temp
  directory; fixture bearers are named `BEARER`, never `TOKEN`.
- **No test may bind a public interface.** `127.0.0.1`, port 0, always.
- **No test may reach the network.** The CIMD fetcher takes its transport as a
  parameter, for the same reason every clock here is a parameter.
- **No test may sleep** to observe a timeout, a TTL or a date rollover.
- **All dates in code, comments and docs are absolute** (`2026-07-31`).
- **585 existing tests must stay green** (`python3 -m unittest discover -s tests`;
  unittest reports to stderr, so redirect to a file rather than piping to
  `tail`, or the summary interleaves ahead of test stdout and looks like a
  hang). Verified green at 585 before this plan started.
- **Match the surrounding prose style.** Comments explain *why*, especially why
  an obvious alternative was rejected.
- **This repo is the public template.** `bin/brain doctor` printing
  `[RED] YOUR BRAIN IS PUBLIC` here is correct — do not "fix" it.
- **Never run `bin/brain init` from this checkout.**

---

## The security contract, in one place

Everything below serves these seven sentences. If a change weakens one, it is
wrong no matter how convenient.

1. **A token is only ever accepted for the resource it was issued for.** The
   audience is recorded at authorization, checked at the token endpoint, and
   compared again on every single MCP request. A token minted for another
   brain, or for another URL of this brain, is refused.
2. **The server's identity is stated by the operator, never inferred from a
   request.** Deriving the issuer or the resource from the `Host` header would
   let anyone who can reach the socket mint tokens whose audience is a hostname
   they chose. `--public-url` is required and is the only source.
3. **Consent cannot be given by a stranger.** `/authorize` issues nothing until
   somebody proves they are the operator, with the credential the operator
   already has. Failures back off on the same table as every other failed
   authentication.
4. **Fetching a Client ID Metadata Document is a request an attacker chose the
   target of.** It is HTTPS-only, resolved and address-checked before connect,
   pinned to the checked address, never redirected, size-capped, time-capped and
   cached. A `client_id` that resolves inside this network is refused.
5. **Everything a fetched document says is untrusted text.** `client_name` is
   rendered on an HTML page. It is escaped, or this plan has shipped a
   cross-site scripting hole into a consent screen.
6. **Nothing that authenticates anything is ever written to the log.** Not the
   operator token, not an access or refresh token, not an authorization code, not
   an `Authorization` header. Achieved by construction, not by filtering — D11.
7. **A log of a brain can contain the brain.** No search query, no note body, no
   note id and no tool argument is ever recorded. Also D11.

---

## Design decisions this plan settles

Each is settled here so the implementer does not re-derive it, and each names
what it rejected.

**D1. The authorization server lives inside `brain serve`, on the same origin
and the same port.** Handoff question 2. The protected-resource metadata has to
be reachable from the MCP URL's origin and the authorization-server metadata
from the issuer's well-known path; one process serving one hostname satisfies
both by construction. *Rejected:* a separate `brain auth` daemon — a second
port, a second tunnel route, and two processes that must agree about the
resource URI or issue tokens nothing accepts.

**D2. `--oauth` requires `--public-url`, and that URL is the identity of
everything.** Behind a tunnel the server sees `127.0.0.1:8787`; the client typed
`https://brain.example.com/mcp`, and RFC 9728 requires the `resource` in the
metadata to match what the user typed **exactly**, including the path. The
operator states it once. The issuer is that URL's origin. *Rejected:* deriving
it from the `Host` or `X-Forwarded-Host` header — attacker-controlled, and
`X-Forwarded-For` is already distrusted three feet away in `Limiter` for exactly
this reason.

**D3. Opaque tokens, hashed at rest, in SQLite. No JWTs, no crypto.** Handoff
question 3. A JWT means implementing JWS signing and key rotation; an opaque
token needs `secrets.token_urlsafe` and `hashlib.sha256`, both stdlib, and
`sqlite3` is already a dependency of the search index. Opaque tokens are also
**revocable** — the property a self-contained JWT structurally cannot have, and
handoff question 4 is entirely about revocation. Only the SHA-256 is stored, so
a stolen database file yields no working token. *Rejected:* JWTs (crypto for no
benefit), and a JSON file (no atomicity across the concurrent handlers
`ThreadingHTTPServer` creates).

**D4. Consent is proved with the operator bearer token, and no account is
invented.** The consent screen has one field: the value `brain serve
--new-token` printed. Compared with `hmac.compare_digest`; failures feed the
existing `Limiter`. The credential already exists, is already 32 random bytes,
and is already the thing that authorises this brain. *Rejected:* a second
password (one more secret to store, rotate and lose), and an unauthenticated
consent screen (anybody who finds the URL mints themselves a token, which is
not a consent screen but a queue).

**D5. The two authentication paths sit side by side, and the old one is tried
first.** One `Authorization: Bearer` header, two possible meanings: the operator
token in the keystore, or an OAuth access token in the store. `_allowed()`
compares the operator token first with `compare_digest` and only then looks the
value up. The existing path is therefore unchanged in behaviour and in cost, and
with `--oauth` absent the second lookup does not exist. *Rejected:* a distinct
header or path for OAuth (no client would use it — the spec says `Authorization`)
and replacing the bearer path (breaks every client this project supports today).

**D6. The store is per-brain, outside the repo, and keyed by the brain it
belongs to.** `~/.local/state/brain/<digest-of-root>/`, `0700`, holding the
SQLite database and the event log. Outside the repo is a stronger guarantee than
a `.gitignore` line that it never reaches git — handoff constraint 2 — and
per-brain is the direct lesson of the shared-keystore trap the business
partition found, where one host's two endpoints turned out to share one
credential. The directory holds a `root` file naming its brain, so an operator
looking at it can tell whose it is. *Rejected:* `.cache/` inside the repo
(gitignored, but the handoff says the secret gate must not be the last line of
defence), and one shared path for all brains (repeats the known trap).

**D7. Refresh tokens rotate, and reuse kills the whole family.** OAuth 2.1
requires rotation for public clients and Claude's documentation confirms it
refreshes reactively on a 401. A refresh token presented after it was already
rotated means a copy leaked, so every token descended from that authorization
is revoked at once. Errors are RFC 6749 codes — `invalid_grant`, never a custom
string, because a client that cannot parse the error retries forever.

**D8. Two scopes, and they are enforced where the tools are.** `brain:read` and
`brain:write`, mapping onto the read/write split the tool table already
declares through `readOnlyHint`. A token without `brain:write` calling
`brain_capture` gets `403` with `error="insufficient_scope"`. This composes with
`--read-only` rather than duplicating it: the flag decides what the *process*
will serve and therefore what may be advertised or granted at all; the scope
decides what *this token* may do within that. `offline_access` is advertised in
the authorization-server metadata (so a client knows to ask for a refresh
token) and **not** in the protected-resource metadata, which the spec says
SHOULD NOT list it.

**D9. Discovery is unauthenticated, by an exact-path allowlist, only when
`--oauth` is on.** A client with no token must be able to read both metadata
documents and reach `/authorize` — that is the entire point of the handshake.
The exemption is an exact-match set of literal paths checked before
authentication, never a prefix and never a pattern, because `startswith` on a
path is how an exemption becomes a bypass. The rate limiter still runs first,
ahead of everything including the exemption.

**D10. Pre-registered clients exist as the fallback, and are generic.** `brain
serve --new-client "<name>" --redirect-uri <uri>` mints a client id the
operator can paste into any assistant that asks for one. This is not a
per-provider branch: it is the third mechanism the MCP spec names, it is one
code path, and it is the answer if some future client speaks neither CIMD nor a
mechanism this server has. *Rejected:* relying on CIMD alone (leaves no path at
all for a client that lacks it, which is the opposite of the owner's
constraint).

**D11. The log is redacted by construction, not by a filter.** Every entry is
built from a fixed vocabulary of event names plus scalars the server itself
computed — a status code, a duration, an error *class*, a tool name from the
table of five. **No caller-supplied string is ever passed to it.** There is
therefore nothing to redact and no filter to get wrong: a search query cannot
be logged because no code path can hand one over. *Rejected:* logging requests
and scrubbing them afterwards — the scrubber is one missed field from writing
the owner's notes to disk, and the field it misses is the one nobody thought of.

---

### Task 1: The event log, and `brain logs`

**Files:**
- Create: `bin/brainlib/eventlog.py`
- Modify: `bin/brainlib/osbackend.py` (`state_dir`), `bin/brain` (`cmd_logs`,
  usage, dispatch)
- Test: `tests/test_eventlog.py` (new)

Built **first**, deliberately. Everything after this is an authorization
handshake with many failure modes, and building the thing that records failures
last means debugging all of them blind. The handoff's own framing — *"we can
see those errors, we can fix them"* — is the acceptance criterion for the rest
of this plan, not an epilogue to it.

**Interfaces:**
- `osbackend.state_dir(root)` → `~/.local/state/brain/<digest>/`, created `0700`,
  honouring `XDG_STATE_HOME`. `<digest>` is a short SHA-256 of the resolved
  brain root (D6). Writes a `root` file naming that brain on creation.
- `eventlog.EventLog(path, clock=..., max_bytes=...)` with one method,
  `record(event, **fields)`. `event` is checked against a module-level frozenset
  of known event names and an unknown one **raises** — the vocabulary is the
  redaction control (D11), so it fails loudly in tests rather than silently
  admitting a free-text field in production.
- Values must be `str`, `int`, `float`, `bool` or `None`, and every `str` value
  must itself come from a frozenset of allowed values (statuses, error classes,
  tool names). A test asserts a caller cannot record an arbitrary string.
- One JSON object per line, `ts` first, ISO-8601 UTC.
- Size-bounded: at `max_bytes` (default 5 MB) the file rolls to `.1` and a new
  one starts. Exactly one generation is kept — this is a testing-phase log, and
  handoff constraint 3 asks for a bound decided now rather than after a disk
  fills.
- Every failure to write is swallowed. A log that can take the server down is a
  worse problem than a log with a gap in it.

**`brain logs`** — handoff question 8, the interface that decides the rest:
`brain logs` (last 50, newest last), `--errors` (failures only), `--since
YYYY-MM-DD`, `--json` (raw lines, for anything else), `--path` (print the file
location and exit — the answer to "where is it" without printing the contents).

- [ ] **Step 1: Write the failing tests.** `state_dir` is per-root and two roots
      differ; it is `0700` and names its brain. `record` writes one line of
      JSON; an unknown event name raises; an arbitrary string value raises; a
      log over `max_bytes` rolls and keeps exactly one generation; a write to an
      unwritable path is swallowed; `brain logs` renders, filters by `--errors`
      and `--since`, and `--path` prints without reading.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `logs: an event log that cannot contain the brain`

---

### Task 2: Wire the log into `brain serve`

**Files:**
- Modify: `bin/brainlib/serve.py`
- Test: `tests/test_serve.py`

The log exists; now the server writes to it. Nothing about authorization changes
in this task, which is what makes it a safe place to prove the redaction
property against real traffic before there is more traffic to redact.

**Events, and this is the whole vocabulary for now:** `request` (method, path
class, status, duration ms), `auth_failed` (reason class only), `rate_limited`
(retry-after), `origin_refused`, `tool_call` (tool name, ok/error, duration),
`tool_error` (tool name, error class), `capture_committed` / `capture_uncommitted`
(the drop box's known failure, which today only reaches a terminal), `body_refused`
(reason class), `protocol_refused`.

**Path is recorded as a class, never as the string.** `/mcp` is `mcp`, a
well-known path is `discovery`, anything else is `other`. A path is
caller-supplied text and D11 says caller-supplied text does not enter this log
— and `/mcp?q=<the owner's question>` is exactly how it would get there.

- [ ] **Step 1: Write the failing tests.** A successful request logs `request`
      with status 200 and no header value anywhere in the file; a wrong token
      logs `auth_failed` and the token's bytes appear nowhere in the file; a
      `brain_search` call logs `tool_call` with the tool name and **not** the
      query; a 429 logs `rate_limited`; an unusual path is logged as `other`
      rather than as itself; serving with no log configured still works.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `serve: record what happened, never what was asked`

---

### Task 3: Metadata, the 401, and `--oauth --public-url`

**Files:**
- Create: `bin/brainlib/oauth.py` (config + the two metadata documents)
- Modify: `bin/brainlib/serve.py` (flags, routing, the 401, D9's allowlist)
- Test: `tests/test_oauth.py` (new)

The discovery half, and on its own it is already the difference between a client
that starts an OAuth flow and one that reports "couldn't reach the MCP server".
Nothing is issued yet.

**Protected-resource metadata**, served at **both**
`/.well-known/oauth-protected-resource` and
`/.well-known/oauth-protected-resource/mcp`, because a client that finds no
`resource_metadata` pointer probes the path-inserted form first and the root
second, and serving both costs one dict:

```json
{"resource": "<public-url>",
 "authorization_servers": ["<origin of public-url>"],
 "scopes_supported": ["brain:read", "brain:write"],
 "bearer_methods_supported": ["header"],
 "resource_name": "brain"}
```

`scopes_supported` reflects **what this process actually serves** — a
`--read-only` server advertises `brain:read` alone (D8), and there is a test for
it. `offline_access` is absent here on purpose.

**Authorization-server metadata**, at `/.well-known/oauth-authorization-server`:

```json
{"issuer": "<origin>",
 "authorization_endpoint": "<origin>/authorize",
 "token_endpoint": "<origin>/token",
 "revocation_endpoint": "<origin>/revoke",
 "scopes_supported": ["brain:read", "brain:write", "offline_access"],
 "response_types_supported": ["code"],
 "grant_types_supported": ["authorization_code", "refresh_token"],
 "code_challenge_methods_supported": ["S256"],
 "token_endpoint_auth_methods_supported": ["none"],
 "client_id_metadata_document_supported": true,
 "authorization_response_iss_parameter_supported": true}
```

Those last two flags are the whole of "works with more than one vendor" and
each is one line. `issuer` **must** be byte-identical to the origin a client
used to build that well-known URL or a conforming client discards the document.

**The 401 changes shape, and only when `--oauth` is on:**

```
WWW-Authenticate: Bearer resource_metadata="<origin>/.well-known/oauth-protected-resource", scope="brain:read brain:write"
```

With `--oauth` off it stays `Bearer realm="brain"` exactly as today. A test
holds both.

**`--public-url` validation, refused at startup rather than at runtime:** HTTPS
required (loopback `http://` permitted, so the whole flow is testable without a
tunnel), no fragment, no query, no trailing slash, and a warning if it is
loopback while `--bind` is not — that combination means a tunnel is in front and
the two do not agree, which produces a resource mismatch nothing explains.

- [ ] **Step 1: Write the failing tests.** Both metadata paths return the
      documents unauthenticated; `resource` is byte-identical to `--public-url`
      including its path; `issuer` is the origin; a `--read-only` process
      advertises only `brain:read`; the 401 carries `resource_metadata` with
      `--oauth` and `realm="brain"` without it; `--oauth` without `--public-url`
      refuses to start; a URL with a fragment, a query or plain `http://` on a
      non-loopback host refuses; `--drop-box --oauth` refuses; the exemption is
      exact-match (`/.well-known/oauth-protected-resource/../mcp` and
      `/.well-known/oauth-protected-resourceX` are **not** exempt); with
      `--oauth` off the well-known paths 401 like anything else.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `oauth: the metadata that makes any client start the flow`

---

### Task 4: Client resolution — CIMD, hardened, and pre-registration

**Files:**
- Modify: `bin/brainlib/oauth.py`, `bin/brain` (`serve --new-client`)
- Test: `tests/test_oauth.py`

The security-critical task. `resolve_client(client_id)` returns a client or
raises, by one of two mechanisms:

**A URL-shaped `client_id` is a Client ID Metadata Document.** Validated per
the spec: `https` scheme, a path component, fetched, parsed as JSON, and the
document's own `client_id` **must** equal the URL exactly. `client_name` and
`redirect_uris` must be present. The `redirect_uri` in the authorization
request is matched against that list.

**The fetch is the attack surface** (contract 4). All of these, each with a
test:

- `https` only; no path component means refuse before any network call.
- The host is resolved first, and **every** returned address is checked. Any
  loopback, private, link-local, multicast, reserved or unspecified address
  refuses the whole fetch. `169.254.169.254` is the one to name in the comment:
  on a rented VM that is the cloud metadata service, and it is the reason this
  matters on the exact machine this is being written for.
- The connection is **pinned to a checked address** — a small
  `HTTPSConnection` subclass that connects to the vetted IP while keeping the
  hostname for SNI and `Host`. Resolving, checking and then connecting by name
  leaves a DNS-rebinding window between the two, and closing it is fifteen
  lines.
- No redirects are followed, ever. A redirect is a second target that was not
  checked.
- Response capped at 64 KB and read with an explicit `amt`, not `read()`.
- Total timeout 5s. Claude allows 10s for the whole `/authorize` response and
  this fetch is inside it.
- Results are **cached**, positive and negative, bounded in count and honouring
  `Cache-Control: max-age` within a floor and a ceiling. Without a negative
  cache, a loop against `/authorize` becomes an outbound-request amplifier.
- The transport is injected, so no test touches the network.

**Redirect-URI matching is exact string equality, with one exception:** loopback
redirect URIs (`http://localhost/...`, `http://127.0.0.1/...`) match
**port-agnostically**, because RFC 8252 native clients bind an ephemeral port.
Everything else is exact. This is a generic RFC 8252 rule, not a vendor
accommodation, and the comment should say so.

**Pre-registration (D10):** `brain serve --new-client "<name>" --redirect-uri
<uri>` inserts a row and prints the `client_id` once. Repeatable
`--redirect-uri`. Refuses a redirect URI that is neither HTTPS nor loopback,
which is the spec's communication-security requirement and the open-redirect
control in one check.

- [ ] **Step 1: Write the failing tests.** A well-formed document resolves;
      a document whose `client_id` differs from its URL is refused; a missing
      `redirect_uris` or `client_name` is refused; `http://` is refused; a URL
      with no path is refused; a host resolving to `127.0.0.1`, `10.x`,
      `169.254.169.254` or `::1` is refused **before** connecting; a host with
      one public and one private address is refused; a redirect response is not
      followed; an oversized body is truncated and refused; the positive and
      negative caches both hit; the cache is bounded; an exact redirect URI
      matches and a different one does not; a loopback redirect matches on any
      port; `--new-client` mints and prints once and refuses a plain-`http`
      non-loopback redirect URI.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `oauth: resolve a client without becoming its request forger`

---

### Task 5: `/authorize` — consent, and PKCE

**Files:**
- Modify: `bin/brainlib/oauth.py` (the store, `/authorize`), `bin/brainlib/serve.py`
- Test: `tests/test_oauth.py`

**The store** arrives here, since this is the first thing that persists:
SQLite at D6's path, `0600`, tables for clients, codes, grants and tokens.
Secrets are stored as SHA-256 and never in the clear. Schema version pinned in
a `meta` table so a later change is a migration rather than a surprise.

**`GET /authorize`** validates `client_id`, `redirect_uri`, `response_type=code`,
`code_challenge` + `code_challenge_method=S256`, `scope` and `resource`, then
renders the consent page. Two rules about failure, and they are opposite on
purpose: a bad `client_id` or a `redirect_uri` that does not match the client's
document is rendered **as an error page**, never redirected — redirecting to an
unvalidated URI is the open-redirect this is supposed to prevent. Everything
else redirects back with `error=` per RFC 6749.

**The consent page** shows, and these are requirements not decoration:
- `client_name` from the fetched document, **HTML-escaped** (contract 5). A test
  plants `<script>` in a name and asserts it is inert in the output.
- The **redirect URI's hostname**, which the spec makes a MUST, plus the extra
  warning the spec asks for when every registered redirect URI is loopback.
- Which scopes are being granted, in plain words — "read every note in this
  brain", "write new notes, which are committed and pushed" — because "this is
  the whole tool surface, not a subset" is already how `startup_notes` talks to
  the operator and the consent screen is the same conversation.
- One field: the operator token (D4). Compared with `compare_digest`; a wrong
  value re-renders the page with a refusal and **counts on the `Limiter`**,
  which is handoff question 7's answer for this endpoint.

**`POST /authorize`** issues a single-use code, TTL 60s, bound to the client,
the redirect URI, the resource, the granted scopes and the PKCE challenge, and
redirects with `code` and `iss` (RFC 9207 — advertised in Task 3's metadata, so
it must actually be sent).

**`resource` is required and recorded.** It is checked against this server's own
canonical resource URI at authorization time, so a token for somewhere else is
never even minted.

- [ ] **Step 1: Write the failing tests.** A valid request renders the page; a
      missing or non-`S256` `code_challenge` is refused; an unknown `client_id`
      renders an error page and does **not** redirect; a mismatched
      `redirect_uri` does not redirect; a `resource` that is not this server is
      refused; a script tag in `client_name` is escaped; a loopback-only client
      gets the extra warning; the redirect hostname is displayed; a wrong
      operator token refuses, issues nothing and increments the limiter; a right
      one issues a code that redirects with `code` and `iss`; the code is
      single-use; an expired code is invalid on a parameterised clock; the code
      is stored hashed and its plaintext is absent from the database file.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `oauth: a consent screen with one user and no accounts`

---

### Task 6: `/token` and `/revoke`

**Files:**
- Modify: `bin/brainlib/oauth.py`, `bin/brainlib/serve.py`
- Test: `tests/test_oauth.py`

**`POST /token`**, `application/x-www-form-urlencoded` — a JSON-only parser here
is a documented, common failure that returns `415` and breaks every client.

- `grant_type=authorization_code`: the code must exist, be unexpired, be unused,
  belong to this client, match the `redirect_uri` and the `resource`, and
  `S256(code_verifier)` must equal the stored challenge. Reusing a code
  **revokes every token issued from it** — OAuth 2.1's rule, and the signal that
  a code leaked.
- `grant_type=refresh_token`: rotates (D7). The old token is invalidated in the
  same response that returns the new one. Presenting an already-rotated token
  revokes the entire family.
- Access token TTL 1 hour, refresh 30 days. `Cache-Control: no-store`.
- Errors are RFC 6749 codes with the right HTTP status: `invalid_grant`,
  `invalid_client`, `invalid_request`, `unsupported_grant_type`. Never a custom
  code, never a bare 500.
- Client authentication is `none` — the metadata says so, and every client here
  is public. A `client_id` is still required and still checked against the
  grant.

**`POST /revoke`** (RFC 7009): revokes a presented token and, for a refresh
token, its family. Always `200`, even for an unknown token — the spec's rule,
and a differing answer is an oracle for whether a token exists. This is
half of handoff question 4; the other half is Task 8's `retire`.

- [ ] **Step 1: Write the failing tests.** A valid exchange returns
      `access_token`, `token_type`, `expires_in`, `refresh_token`, `scope`; a
      wrong `code_verifier` is `invalid_grant`; a reused code is `invalid_grant`
      **and** the previously issued token stops working; a mismatched
      `redirect_uri`, `client_id` or `resource` is refused; JSON body is
      refused as `invalid_request`; refresh returns a **new** refresh token and
      the old one stops working; reusing a rotated refresh token revokes the
      family; an expired token is refused on a parameterised clock;
      `no-store` is set; every error body is an RFC 6749 code; `/revoke`
      answers 200 for a known and an unknown token alike and the known one
      stops working.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `oauth: tokens that rotate, and die together when one leaks`

---

### Task 7: Both paths on `/mcp` — audience and scope

**Files:**
- Modify: `bin/brainlib/serve.py` (`_allowed`), `bin/brainlib/oauth.py`
  (`validate_bearer`), `bin/brainlib/mcp.py` if scope enforcement needs it
- Test: `tests/test_serve.py`, `tests/test_oauth.py`

Where OAuth becomes usable, and where this plan is most likely to break
something that works today. **The operator-token path is tried first and is
unchanged** (D5).

- `validate_bearer(presented)` → a grant, or `None`. Looks up the SHA-256,
  checks expiry, checks revocation, and **checks the audience against this
  server's own resource URI** (contract 1). A token issued for another resource
  is refused even though it is in this database — which is the case that
  matters if the owner ever runs two brains on one host.
- **Scope enforcement in the dispatcher, not the advertised list.** Same rule
  the read-only mode already follows and for the same reason: a client that
  never read `tools/list` and calls `brain_capture` by name must be refused. A
  token lacking `brain:write` gets `403` with
  `WWW-Authenticate: Bearer error="insufficient_scope", scope="brain:write", resource_metadata="…"`.
- `tools/list` reflects the *token's* scopes as well as the process's flags, so
  a read-scoped token does not see a tool it cannot call.
- Every outcome logs: `oauth_token_accepted`, `oauth_token_rejected` (reason
  class), `insufficient_scope`.

- [ ] **Step 1: Write the failing tests.** The operator token still works with
      `--oauth` on and with it off; an OAuth access token works; an expired one
      401s; a revoked one 401s; one whose audience is a different resource 401s
      **even though the row exists**; a read-scoped token is refused
      `brain_capture` by name with 403 and `insufficient_scope`; `tools/list` on
      a read-scoped token omits `brain_capture`; a garbage bearer 401s and feeds
      the limiter exactly once; the refusal still closes the connection
      (`TestConnectionReuse` must still hold — backlog item 9 was invisible to
      tests that opened one connection per request).
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `serve: one endpoint, two credentials, one audience`

---

### Task 8: `doctor`, knowledge health, and `retire`

**Files:**
- Modify: `bin/brain` (`cmd_doctor`, `cmd_retire`), `bin/brainlib/oauth.py`
- Test: `tests/test_brain.py`, `tests/test_setup.py`

The second half of the observability ask, and it is deliberately small because
**most of it already exists**. The handoff says to check before designing:
`brain stats` already measures findability, staleness, orphans, the link graph
and capture rate; `doctor` already reports hooks, backup freshness and lint. The
real gap is that nobody is looking at either on a server.

- **`doctor` gains two lines.** How many errors the event log recorded in the
  last 7 days, with the command to read them — a number nobody has to know to
  ask for. And a knowledge-health summary lifted from `stats_snapshot()`, RED on
  the same conditions `stats` already treats as RED. `doctor` is what the
  nightly schedule already runs and what already notifies on a non-zero exit, so
  this reaches the operator without a new mechanism.
- **`retire` deletes the OAuth store and the event log**, naming both, and says
  how many live tokens it is revoking by doing so. This is handoff question 4:
  a token store that survives `retire` is a credential outliving the brain it
  authorised.

- [ ] **Step 1: Write the failing tests.** `doctor` reports the error count and
      is RED above a threshold; it reports knowledge health and is RED on an
      overdue review; `retire` removes the state directory and says what it
      revoked; `retire` on a brain that never served does not fail.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `doctor: the errors and the graph, where somebody will see them`

---

### Task 9: An end-to-end flow, driven by a real client

**Files:**
- Test: `tests/test_oauth.py`

Every task above tests its own piece. This one drives the **whole handshake**
against a live loopback server the way a hosted assistant would: unauthenticated
request → 401 → read `resource_metadata` → fetch protected-resource metadata →
build the authorization-server metadata URL from `issuer` and fetch it → check
`code_challenge_methods_supported` → CIMD `client_id` → `/authorize` → consent →
code → `/token` → call `brain_search` with the access token → refresh → call
again.

It is a test, not a script, so it runs in CI forever. It is the only thing that
proves the *documents agree with each other* — every unit test above can pass
while the issuer in one document fails to match the URL a client would build
from another, and that mismatch is the single most common way these deployments
fail.

The deliberate omission gets a test too: DCR. Assert `registration_endpoint` is
absent, so a future change that adds one is a decision somebody made on purpose.

- [ ] **Step 1: Write the test.** The full flow, plus: the flow with a
      pre-registered client id instead of CIMD; the flow refused at consent
      issues nothing; the access token from a completed flow is refused by a
      second server with a different `--public-url`.
- [ ] **Step 2: Run it, fix what it finds** — expect it to find something the
      unit tests could not.
- [ ] **Step 3: Whole suite**
- [ ] **Step 4: Commit** — `oauth: the whole handshake, end to end, in one test`

---

### Task 10: Documentation and the runbook

**Files:**
- Create: `setup/runbooks/remote-oauth.md`
- Modify: `SETUP.md`, `README.md`, `bin/brainlib/serve.py` (module docstring —
  it currently says "No OAuth", in detail), `docs/superpowers/BACKLOG.md`
  (close item 3, correct the DCR claim), `AGENTS.md` if the toolbelt summary
  changes

The owner will stand the deployment up themselves, so **the runbook is the
deliverable that decides whether any of this is usable.** It must contain:

- The named-tunnel deployment, concretely: `cloudflared tunnel login`, `create`,
  `route dns`, a config file, `run`, and `brain serve --oauth --public-url
  https://<host>/mcp`. Named, not a quick tunnel — a hostname that changes every
  run invalidates every issued token, because the audience is the URL.
- The tunnel contract, unchanged from `tunnel-cloudflare.md` and now with one
  more clause: it must forward `/.well-known/*` and `/authorize` and `/token` as
  well as `/mcp`. A route mapped to `/mcp` alone produces a connector that
  cannot discover anything, and the symptom — the MCP server sees the first
  request and the authorization server sees nothing — is worth naming.
- **Which parts are verified and which are not.** Everything below the tunnel is
  covered by tests including Task 9's end-to-end flow; the connection to a
  hosted assistant is **not** verified in this repo, because the owner is
  standing up the real brain. Say that plainly in the runbook, the way the
  tunnel runbook says which of its claims came from a live tunnel.
- What to do when it fails, keyed to `brain logs`: the discovery failure, the
  redirect-URI mismatch, the audience mismatch, the clock skew.
- The exposure posture, stated as bluntly as `startup_notes` already does:
  whoever completes this flow can read every note **and write new ones**, which
  are committed and pushed. That is the owner's decision, recorded, not softened.
- The residual risks, named rather than implied: an operator token that is now
  also a consent credential, one host serving two brains sharing one keystore
  entry (the known trap, unchanged by this work), and a tunnel putting every
  client behind one limiter bucket.

- [ ] **Step 1: Write the runbook and the doc changes**
- [ ] **Step 2: Run every command in it that can be run without the owner's
      domain** — the server, the flags, the refusals, `brain logs`, `doctor` —
      and correct the document to match what actually happened, not what it
      should have done. The tunnel runbook was written this way and it is why
      backlog item 9 was found.
- [ ] **Step 3: `bin/brain lint`, then the whole suite**
- [ ] **Step 4: Commit** — `docs: the brain on the internet, and what is still unverified`

---

## Self-review — to answer when the plan is done

Answer from a run, not from reading the code.

1. **Does the bearer path still work exactly as it did?** With `--oauth` off and
   with it on. This is the regression that matters most.
2. **Can an unauthenticated caller make this server fetch a URL of their
   choosing?** Walk it from `/authorize` outward. Name what stops each of:
   `http://`, a private address, a redirect to one, a DNS rebind, a 10 GB body.
3. **Is there any string in the event log that a caller supplied?** Grep the log
   after driving real traffic through it, including a search whose query is a
   distinctive nonsense word. That word must not appear.
4. **Does a token issued for one resource work against another?** Two servers,
   two `--public-url` values, one database. It must not.
5. **What happens when the operator token is rotated?** Existing OAuth grants
   should survive — they were consented to, not derived from it — but no new
   consent should be possible with the old value. Confirm which, and that it is
   the intended answer.
6. **Did any new MCP tool appear?** The table must still be the same five names.
   Everything here is transport.
7. **Run the full suite** and record the count. 585 before this plan; every
   number added should be traceable to a task above.
8. **Would this survive the owner deploying it wrong?** The failure modes are a
   quick tunnel instead of a named one, a route that forwards only `/mcp`, and a
   `--public-url` that disagrees with the hostname. Each should produce a
   refusal or a log line that names the problem, not a silent failure.
