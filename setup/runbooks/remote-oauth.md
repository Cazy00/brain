# Reaching this brain from a hosted assistant

`brain serve` has always taken a bearer token in a header. That is what a
**local** client does — Claude Code, Codex CLI, Cursor, VS Code all set one, and
they still do. A **hosted** assistant cannot: it runs on somebody else's
servers, you never touch a config file, and the only credential it can obtain is
one you consented to in a browser.

`brain serve --oauth` is the other half. This runbook is how to stand it up.

**Written 2026-07-31.** Everything below the tunnel was run against a live
server on a real socket, and what was observed is quoted rather than described.
The two things that were **not** verified are named in
[What is not verified here](#what-is-not-verified-here) — read that section
before you trust the rest.

---

## What you need

- A **host that is up**, with `bin/brain` on it and a brain in it.
- A **stable HTTPS hostname** you control. Not a quick tunnel: the hostname is
  the *audience* of every token this server issues, so a hostname that changes
  invalidates every connection you have made.
- The token `brain serve --new-token` printed. It is now doing two jobs — the
  header credential for local clients, and the thing you paste to prove consent.

---

## The short version

```sh
brain serve --new-token                                    # once, if you have none
brain serve --oauth --public-url https://brain.example.com/mcp --port 8787
cloudflared tunnel run brain                               # in another shell
```

Once that works, stop running it by hand — add `--install-service` to the same
command and, on Linux, `sudo loginctl enable-linger $USER`. See
[Keeping it running](#keeping-it-running); skipping it is how the brain is
offline the next time you look.

Then in the assistant, add a custom connector by URL and paste
**`https://brain.example.com/mcp`** — character for character, the same string
you gave `--public-url`. You will be sent to a consent page, you paste your
token, and it connects.

That is the whole flow. The rest of this document is the parts that go wrong.

---

## `--public-url` is the one thing to get right

Behind a tunnel this process only ever sees `127.0.0.1:8787`. The client typed
`https://brain.example.com/mcp`. RFC 9728 requires the advertised `resource` to
match what the user typed **exactly, including the path** — so the server has to
be told, and it cannot guess.

It is refused at startup, not at runtime, if it is:

| Refused | Why |
|---|---|
| absent | there is nothing to bind tokens to |
| `http://` on a public host | OAuth 2.1 requires HTTPS; loopback `http://` is allowed for local testing only |
| ending in `/` | the trailing slash changes the identifier, and it must match what you type into the client |
| carrying `?query` or `#fragment` | an identity, not a request |

**Deriving it from the `Host` header was considered and rejected**, and it is
worth knowing why: anyone who can reach the socket could then set that header
and be issued tokens whose audience is a hostname they chose. It is the same
reasoning that makes the rate limiter refuse to trust `X-Forwarded-For`.

---

## The tunnel contract, with one new clause

Everything in [tunnel-cloudflare.md](tunnel-cloudflare.md) still applies:
terminate TLS, forward to the port, preserve `Authorization`, add no `Origin`.

**The new clause: forward `/.well-known/*`, `/authorize`, `/token` and
`/revoke`, not only `/mcp`.**

A route mapped to `/mcp` alone produces the failure that is hardest to diagnose
from the outside, because it looks exactly like the server being down. The
symptom is specific and worth memorising:

> **Your MCP server sees the first request, and your authorization server sees
> no traffic at all.** The client got a 401, could not fetch the metadata it
> pointed at, and gave up with something like "couldn't reach the server".

A named tunnel that forwards the whole hostname is the simple answer:

```yaml
# ~/.cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /home/you/.cloudflared/<tunnel-id>.json
ingress:
  - hostname: brain.example.com
    service: http://127.0.0.1:8787     # the WHOLE host, not one path
  - service: http_status:404
```

Set it up once:

```sh
cloudflared tunnel login
cloudflared tunnel create brain
cloudflared tunnel route dns brain brain.example.com
cloudflared tunnel run brain
```

A `--url` quick tunnel is fine for a first look and useless for living with —
the hostname is random and changes every run, and here that means every token
you have issued stops being valid.

---

## Keeping it running

Everything above shows `brain serve` as a shell command. That is right for
trying it and **wrong for living with it**: it is a foreground process, so it
dies with the terminal, and it does not come back after a reboot.

Once the flags are right, hand the same command to the OS:

```sh
brain serve --oauth --public-url https://brain.example.com/mcp --install-service
```

It validates every flag first, then installs a `systemd --user` unit (or a
launchd agent on macOS) that runs **that exact command**, restarts it if it
dies, and starts it at boot.

```sh
brain serve --service-status      # installed? running? serving THIS brain?
brain serve --uninstall-service
```

### On Linux, do this too — or none of it survives

```sh
sudo loginctl enable-linger $USER
```

Without it, `systemd --user` stops **every** unit you own the moment your last
session ends. You SSH in, install the service, log out, and it is gone — and so
are the nightly `doctor` and the weekly consolidation from `brain schedule
install`, which have the same problem and always did.

Nothing warns you at the OS level. `brain serve --install-service` exits
non-zero and says so, `--service-status` says so, and `brain doctor` says so
whenever you have a service or a schedule that lingering would break. It cannot
fix it for you: `enable-linger` needs root.

macOS and Windows have no equivalent step — LaunchAgents and scheduled tasks
survive logout on their own, so nothing is printed about it there.

### The tunnel needs the same treatment

`cloudflared tunnel run brain` is a foreground process too. `cloudflared`
installs its own service:

```sh
sudo cloudflared service install
```

That one is a system service, so lingering does not apply to it — which is
worth knowing, because it means the tunnel can be up while the brain behind it
is down, and the symptom is a connector that reaches your hostname and gets a
502.

**Windows:** `--install-service` deliberately refuses. `schtasks` can start
something at logon but will not restart it when it dies, which is the entire
property this is for; claiming support and handing back a process that vanishes
on its first exception would be worse than saying so. Use NSSM or a real
Windows service.

---

## What the flow actually looks like

Run against a live server on 2026-07-31. Every response below is real output.

**1. An unauthenticated request gets a 401 that says where to go next.** The
401 status is the part that matters; the header is ignored on a 200.

```
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="http://127.0.0.1:8791/.well-known/oauth-protected-resource/mcp", scope="brain:read brain:write"
```

**2. Protected-resource metadata (RFC 9728), served without a token.**

```json
{"resource": "http://127.0.0.1:8791/mcp",
 "authorization_servers": ["http://127.0.0.1:8791"],
 "scopes_supported": ["brain:read", "brain:write"],
 "bearer_methods_supported": ["header"], "resource_name": "brain"}
```

**3. Authorization-server metadata (RFC 8414), served without a token.** Two
fields here are the whole of "works with more than one assistant" —
`client_id_metadata_document_supported` and a `none` in
`token_endpoint_auth_methods_supported`. Clients check them as a pair.

```json
{"issuer": "http://127.0.0.1:8791",
 "authorization_endpoint": "http://127.0.0.1:8791/authorize",
 "token_endpoint": "http://127.0.0.1:8791/token",
 "revocation_endpoint": "http://127.0.0.1:8791/revoke",
 "scopes_supported": ["brain:read", "brain:write", "offline_access"],
 "response_types_supported": ["code"],
 "grant_types_supported": ["authorization_code", "refresh_token"],
 "code_challenge_methods_supported": ["S256"],
 "token_endpoint_auth_methods_supported": ["none"],
 "client_id_metadata_document_supported": true,
 "authorization_response_iss_parameter_supported": true,
 "revocation_endpoint_auth_methods_supported": ["none"]}
```

**4. The consent page.** One page, no scripts, nothing loaded from anywhere. It
shows the client's name, **the hostname it will be sent back to**, what it will
be able to do in plain words, and one field for your token. There are no
accounts: the brain has one owner, and pasting the token is how it knows you are
them.

**5. Consent issues a code**, and the response carries `iss` (RFC 9207, against
mix-up attacks):

```
Location: https://example.org/cb?code=…&state=demo&iss=http%3A%2F%2F127.0.0.1%3A8791
```

**6. The code is exchanged at `/token`** — `application/x-www-form-urlencoded`,
with the PKCE verifier — and comes back as:

```
{'access_token': '<43 chars>', 'token_type': 'Bearer', 'expires_in': 3600,
 'scope': 'brain:read', 'refresh_token': '<43 chars>'}
```

**7. The token reads the brain**, and — because this one asked for `brain:read`
only — `tools/list` returns four tools, and `brain_capture` called **by name**
is refused:

```
HTTP/1.1 403 Forbidden
WWW-Authenticate: Bearer error="insufficient_scope", scope="brain:write", resource_metadata="…"
```

---

## Two scopes, and what they mean

| Scope | What consenting to it allows |
|---|---|
| `brain:read` | read every note in this brain — decisions, projects, people, journal, everything except the encrypted vault |
| `brain:write` | write new notes, which are committed and pushed to your private remote automatically |
| `offline_access` | stay connected without asking again, until you revoke it |

Scopes **compose with** `--read-only` rather than replace it. The flag decides
what the process will serve at all; the scope decides what one token may do
inside that. A token granted `brain:write` against a `--read-only` server
reaches no write tool.

---

## Clients that need a client id

Most assistants identify themselves with a **Client ID Metadata Document** — an
HTTPS URL serving their own metadata, which this server fetches and validates.
Nothing to configure; it just works.

For anything that does not, register one yourself:

```sh
brain serve --new-client "My Assistant" --redirect-uri https://their.callback/url
```

It prints a client id once. Paste it wherever the assistant asks for one. There
is no client secret — every client here is a **public** client and PKCE is what
protects the exchange instead.

**Dynamic client registration (`/register`) is deliberately not implemented.**
The MCP specification marks it deprecated. If a client only speaks DCR it will
fail to find `registration_endpoint` in the metadata above; `--new-client` is
the answer.

---

## When it does not work

Start here, every time:

```sh
brain logs --errors
```

It is machine-local, lives outside the repository, and holds **no query text and
no note content** — by construction, not by filtering (see
`bin/brainlib/eventlog.py`). What you get is a class of failure and a timestamp:

```
2026-07-31T08:32:47Z  auth_failed            reason=no_header
2026-07-31T08:32:47Z  oauth_client_refused   reason=blocked_address
2026-07-31T08:32:47Z  oauth_client_refused   reason=not_https
2026-07-31T08:29:33Z  oauth_consent_failed   reason=consent_refused
2026-07-31T08:29:49Z  insufficient_scope     tool=brain_capture
```

| What you see | What it is |
|---|---|
| the client says it cannot reach the server, and `brain logs` shows one `request` and nothing else | the tunnel is forwarding `/mcp` only. Forward the whole hostname |
| `oauth_client_refused reason=bad_redirect_uri` | the client's callback URL is not one it registered. Nothing is redirected in this case, on purpose — sending you to an unverified address is the attack the check exists to stop |
| `oauth_client_refused reason=blocked_address` | a `client_id` URL resolving somewhere inside your network. Refused before any connection |
| `oauth_error code=invalid_grant` right after connecting | usually `--public-url` disagreeing with the hostname the client used. The token's audience is checked on every request |
| it worked yesterday and every client is now refused | check whether the tunnel hostname changed |
| `rate_limited` for everyone at once | behind a tunnel every client shares one address, so one guessing run slows everything down. Known, deliberate, documented in SETUP.md Part 8 |

`brain doctor` also reports the count of failures in the last 7 days, so a
problem you were not watching for still reaches you through the nightly run.

---

## What you are exposing

Stated as bluntly as the startup banner does, because this is a decision, not a
default:

**Anyone who completes this flow can read every note in this brain and write new
ones, which are committed and pushed automatically.** That is the whole tool
surface. The consent page says so in words before you type anything.

If that is more than you want, `--read-only` serves the four read tools and
refuses `brain_capture` — server-side, so a client that calls it by name is
refused too. Run the writable one on loopback where you already are.

### Residual risks, named rather than implied

- **The operator token is now doing two jobs**: the header credential for local
  clients, and the consent credential. Whoever holds it can both read the brain
  directly and authorise new connections. Rotating it with `--new-token` breaks
  local clients until you re-register them; existing OAuth grants survive,
  because they were consented to rather than derived from it. Revoke those with
  `/revoke`, or with `brain retire` for all of them at once.
- **Two brains on one host still share one keystore entry.** Unchanged by this
  work, and documented in [business-partition.md](business-partition.md). The
  OAuth token store does *not* share — it is keyed per brain — and a token
  issued for one `--public-url` is refused by a server on another, but the
  bearer token is still one value per OS user. Different hosts, or at minimum
  different OS users.
- **Behind a tunnel every client is one address** to the rate limiter, which
  deliberately does not trust `X-Forwarded-For`. A guessing run through the
  tunnel slows down everything else through it.
- **This is not a hardened public service.** It is one person's brain on one
  host. The controls are real — PKCE, audience-bound opaque tokens, rotation
  with family revocation, an SSRF-hardened fetcher, a rate limiter with no
  off switch — but the honest framing is a personal server on the internet, not
  a product.

---

## Revoking

```sh
curl -s -X POST https://brain.example.com/revoke -d "token=<any token from that client>"
```

Either half of a pair revokes the whole grant: they authorise the same access,
so revoking one and leaving the other is a control that does not control
anything. It answers `200` for a token that never existed too — an answer that
differed would confirm a guess without spending it.

`brain retire` destroys the entire token database and says how many connected
clients it just disconnected. A token store that outlives the brain it
authorised is a live credential nobody is watching any more.

---

## What is not verified here

Written down rather than glossed, in the same spirit as the tunnel runbook's
"three things it does not do":

1. **No hosted assistant has been connected to this code.** The specification is
   implemented and tested — including an end-to-end test that walks the whole
   handshake the way a client walks it, discovering each document from the last
   rather than being told where they are — and the flow above was driven against
   a live server with `curl`. But the connection from a real hosted assistant is
   the owner's next step, not something this repository has done.
2. **The named-tunnel setup was not run.** It needs a Cloudflare account and a
   domain, which this repository does not have. The commands are the documented
   ones; the *contract* they have to satisfy was verified through a live quick
   tunnel on 2026-07-29, and the one new clause (forward `/.well-known/*` and
   the OAuth endpoints) follows from what the flow above actually requests.

When you do connect one, **write down what happened** — including anything that
did not match this page — the way `tunnel-cloudflare.md` records what a real
tunnel was observed to do. That is what turns this from a plan into a runbook.
