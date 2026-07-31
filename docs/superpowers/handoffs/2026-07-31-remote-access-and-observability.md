# Handoff: brain on the internet, for any agent, with something to look at when it breaks

**For the planning session, not for an implementer.** Nothing here is designed
yet. This is what was decided, what was checked, and what still has to be
answered before a plan can be written. Written 2026-07-31 at the end of the
session that built the business partition, because that session was nearly out
of room and this deserves a fresh one.

First `handoffs/` file. A plan says *how*; a spec says *what the design is*.
This is neither: it is the raw material for both, so the next session starts
from decisions rather than from a conversation it cannot see.

---

## What the owner wants

Three things, in one deployment:

1. **A real brain on the machine this repo is developed on** — an always-on
   host — holding their actual notes, not a scratch copy. This supersedes the
   earlier "no brain on this machine" position: it was correct while the box
   was only a template workshop, and the owner has now decided otherwise.
2. **Reachable over the internet, on their own domain**, so they can talk to it
   from a phone or a laptop through a hosted assistant and have it actually use
   the brain — the thing `brain serve` cannot do today. Confirmed 2026-07-31:
   this is wanted **now**, it will be wired and connected, and it will serve
   claude.ai *and* ChatGPT *and* others — not one of them.
3. **Enough visibility to run it in a testing phase**: when something fails,
   they want to be able to SEE it afterwards and fix it, rather than infer it
   from an agent's bad answer. Errors, and the health of the knowledge itself.

### The constraint that shapes everything

**This must not be Claude-only.** The owner was explicit. claude.ai is the
first client to connect, not the target. That reframes the work:

> Build the MCP authorization spec properly. claude.ai is then one client of
> it, and so is anything else that speaks the spec.

Anything Claude-specific must be identifiable as such and small: as far as this
session could tell, that is the redirect URI and Anthropic's egress range, and
nothing else. Every other requirement below is an RFC.

**Checked 2026-07-31, and the two big providers converge.** ChatGPT's custom
MCP connectors want the same things Claude's do — OAuth with PKCE, protected
resource metadata, token validation on the MCP server, HTTPS — and OpenAI
*recommends* **CIMD** (Client ID Metadata Documents) for client registration,
supporting public-client token exchange (`none`). Claude selects CIMD when the
authorization server metadata advertises both
`"client_id_metadata_document_supported": true` and `"none"` in
`token_endpoint_auth_methods_supported`. **Those are the same server.** So CIMD
+ PKCE + PRM + AS metadata looks like the shape that serves both without DCR
and without a per-provider branch — the plan should test that hypothesis first,
because if it holds, "works with any provider" costs almost nothing extra.

### Two populations of client, and both must work at once

This is the design point most likely to be missed:

- **Hosted assistants** (claude.ai, ChatGPT, and whatever follows) cannot take
  a token from a config file. They need the OAuth flow above.
- **Local clients** (Claude Code, Codex CLI, Gemini CLI, Cursor, VS Code) take
  a bearer token in a header and **already work today**, through `brain serve`
  as it stands.

The endpoint therefore has to accept **both** an operator-minted bearer token
and an OAuth-issued access token, on the same URL, at the same time — without
the OAuth work regressing what already works. Anything that replaces the
existing token path rather than sitting beside it breaks every client the
project currently supports.

Per-provider variation, so far, is small and enumerable: the redirect URI, the
client-authentication method, and each vendor's egress range if anything is
allowlisted. Everything else is shared.

### Hosting is not settled

The owner has a VPS **and** raised hosting it on a different machine behind a
Cloudflare Tunnel. The design must not assume either. What it can assume: a
stable HTTPS hostname on a domain they control, and a host that is up.

---

## What this session checked, so the planner does not re-derive it

Re-checked 2026-07-31 against Anthropic's own documentation (backlog item 3
says to do exactly this before starting):

- **Individuals still get OAuth only.** Adding a custom connector by URL as a
  personal Pro/Max user offers *"an OAuth Client ID and OAuth Client Secret for
  your server"*. `static_headers` — a fixed bearer token — is **beta and
  entered by an organization administrator**, and the credential is *"shared by
  the organization rather than pasted per user."* That is a product shape, not
  a flag waiting to flip. **Waiting for it is not a strategy.**
- **Dynamic client registration is NOT required.** The backlog says it is; the
  current docs say the Client Secret field is optional and that supplying a
  pre-registered client ID *"avoids dynamic client registration entirely."* So
  `/register` can be left out of v1. This is the single biggest scope reduction
  available and the backlog entry should be corrected when the plan lands.
- **The required surface**, all of it standards rather than Anthropic's:
  - `401` carrying `WWW-Authenticate: Bearer resource_metadata="…"` — the 401
    status is required; the header is ignored on a `200`
  - protected-resource metadata (RFC 9728), whose `resource` must match the URL
    the user typed **exactly**, including path
  - authorization-server metadata (RFC 8414), advertising
    `code_challenge_methods_supported: ["S256"]`
  - `/authorize` with PKCE S256 **and a human consent step** — there is no
    machine-to-machine path, by design
  - `/token`, `application/x-www-form-urlencoded`, refresh-token rotation for
    public clients, RFC 6749 error codes (`invalid_grant`, not a custom one)
  - redirect URI `https://claude.ai/api/mcp/auth_callback` for the hosted
    surfaces. **Claude Code is different** — RFC 8252 loopback on an ephemeral
    port, so an authorization server must match `localhost`/`127.0.0.1`
    port-agnostically. Any other agent will have its own; the plan should treat
    the redirect list as configuration, not a constant.
  - 10s response budget for discovery/registration/token, 30s for refresh
- **One design lead worth keeping:** the consent screen has no user accounts to
  authenticate against, and does not need to invent any — the owner can prove
  it is them by pasting the value `brain serve --new-token` already printed.
  The credential exists; the consent step can reuse it.

Sources: `support.claude.com/en/articles/11175166-about-custom-connectors-remote-mcp`,
`claude.com/docs/connectors/building/authentication`.

---

## The observability half, which has no design at all yet

The owner's words: *"if there are any errors or something, would be saved
somewhere. We can see those errors. We can fix them. We can see also the
integrity of the graph."*

Two different things, and the plan should keep them apart:

- **Operational errors** — a request that failed, a tool that errored, an
  auth handshake that did not complete, a capture that did not commit. Today
  these go to a terminal nobody is watching once the server is a daemon.
- **Knowledge integrity** — is the graph healthy, are notes findable, what is
  stale, what is orphaned.

**Check what already exists before designing anything.** `bin/brain stats`
already measures findability, capture rate, staleness, curation and the link
graph, and `bin/brain doctor` already reports hooks, backup freshness and lint.
The gap may be smaller than it looks, and may be mostly *"these exist but
nobody is looking at them on a server"* rather than *"these do not exist"*.

Three constraints the plan must settle, because they are easy to get wrong:

1. **A log of a brain server can contain the brain.** Queries are the owner's
   own questions; results are note content; headers carry the token. Whatever
   is recorded needs an explicit statement of what is redacted, and the answer
   for tokens is "never written at all" (`serve.py` already argues this for its
   request log).
2. **It must never reach git.** It is machine state. `.gitignore`, and the
   secret gate does not become the last line of defence.
3. **It is for a testing phase.** Retention and size need a bound decided now,
   not after a disk fills.

---

## Open questions for the planning session

Nobody should start writing code until these have answers.

1. **Where does the real brain live on this host, and what is its remote?** It
   holds the owner's actual notes, on a rented VM. `brain setup` asks this;
   the answer also decides the backup story.
2. **Does the authorization server live inside `brain serve`, or beside it?**
   The metadata documents must be discoverable from the MCP URL's origin (or
   pointed at by `resource_metadata`), which constrains this more than it first
   appears.
3. **Zero third-party dependencies is a standing constraint of this repo.** An
   authorization server in the standard library alone is achievable — opaque
   tokens rather than JWTs avoids crypto entirely — but the plan must say so
   deliberately rather than discover it halfway.
4. **Where do issued tokens live**, how are they revoked, and what happens on
   `brain retire`?
5. **Which second provider proves "not Claude-only"?** The claim needs one
   non-Anthropic hosted assistant actually connected, or it is an assertion.
   **ChatGPT is the owner's stated second** and is the right one: it exercises
   the OAuth path rather than the header path, which is where the risk is. Two
   things to establish early — custom MCP connectors live behind ChatGPT's
   *developer mode* and need a Plus/Pro-tier account (which plan does the owner
   have?), and on Business/Enterprise workspaces an admin has to enable custom
   connectors at all. The header-only clients (Codex CLI, Gemini CLI, Cursor,
   VS Code) prove nothing new about auth — they already work.
6. **What is exposed, and for how long?** Full-time internet-facing is a
   different posture from today's loopback-plus-a-tunnel. The read-only mode,
   the drop box (which must NOT be public), and the auth server each need a
   stated position.
7. **The existing failed-auth backoff is per-address**, and behind a tunnel
   every client is one address (documented in `SETUP.md` Part 8). An OAuth flow
   adds more endpoints that can be hammered. Does the limiter cover them?
8. **How does the owner read the errors?** A file they `cat`, a `brain`
   subcommand, something in `doctor`. Deciding the interface decides most of
   the rest.

---

## Carried-forward constraints (unchanged, non-negotiable)

- Python 3.9 floor; **zero third-party dependencies**; stdlib only.
- No credential ever in the repo. Tests use a `FileKeystore` in a temp dir and
  name fixtures `BEARER`, never `TOKEN`.
- No test binds a public interface — `127.0.0.1`, port 0.
- No test sleeps to observe a timeout; clocks are parameters.
- Absolute dates everywhere.
- **585 existing tests must stay green** (`python3 -m unittest discover -s tests`;
  redirect to a file — unittest reports on stderr).
- This repo is the public template. The REAL brain is a separate clone;
  nothing personal is ever committed here.

## Where the last session left things

Everything below is done, pushed, and needs no revisiting:
`source:` provenance, `brain serve --drop-box`, `visibility:`, `brain publish`
and its review queue, `setup/runbooks/business-partition.md` — the M/P business
partition, verified against Hermes Agent's own MCP client. See
[BACKLOG.md](../BACKLOG.md) and
[plans/2026-07-30-business-partition.md](../plans/2026-07-30-business-partition.md).

Backlog items 3 (this work) and 4 (Windows, still needs hardware) are what
remain.
