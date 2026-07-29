# Reaching `brain serve` through a Cloudflare Tunnel

One worked example of the tunnel contract in SETUP.md Part 8. The project takes
no position on which tunnel you use — this one is written up because it was the
one actually tested against the server, and because `cloudflared` dials **out**
to Cloudflare, so you open no inbound port and need no static IP.

## The shape worth copying

Two servers, not one. The tunnel gets the read-only brain; writing stays on
loopback, where you already are.

```sh
brain serve --new-token                       # once, if you have no token yet
brain serve --read-only --port 8787 &         # what the tunnel will reach
cloudflared tunnel --url http://127.0.0.1:8787
```

Then register the printed `https://…` URL with any client that can set a header:

```sh
claude mcp add --transport http brain https://<your-hostname>/mcp \
  --header "Authorization: Bearer $BRAIN_TOKEN"
```

A `--url` quick tunnel is fine for trying it and useless for living with: the
hostname is random and changes every run. For anything ongoing, a **named
tunnel** on your own domain (`cloudflared tunnel login`, `create`, `route dns`,
`run`) gives you a stable hostname. That flow needs a Cloudflare account and a
domain, so it is **not** verified here — the quick tunnel below is.

## What was actually checked, 2026-07-29

Run against this repo through a live `trycloudflare.com` tunnel, not from
documentation. Each contract point in SETUP.md, and how it was proved:

| Contract point | Evidence |
|---|---|
| Terminates TLS | Edge serves `https://`, origin receives plain HTTP |
| Forwards path and all | `/mcp` arrived at the origin as `/mcp` |
| Preserves `Authorization` | Echo server saw `Bearer …` byte-identical; a valid token got `200`, a wrong one `401` |
| Adds no `Origin` | The server 403s on any `Origin`, so a `200` at all is the proof — and an injected `Origin` still got `403` through the tunnel |

Also confirmed through the tunnel: `--read-only` holds (a `brain_capture` call
by name came back as a tool error and nothing reached the working tree), `GET`
answers `405`, and `brain_search` returned real note content.

Headers Cloudflare **adds** on the way in: `CF-Connecting-IP`, `CF-Ray`,
`CF-IPCountry`, `CF-Visitor`, `CDN-Loop`, `X-Forwarded-For`,
`X-Forwarded-Proto`. None of them are consulted by this server, deliberately —
see below.

## Three things it does not do

**It is not authentication.** A quick tunnel publishes a public hostname with
nothing in front of it; the bearer token is the only barrier. That is a
defensible posture — 32 random bytes, constant-time comparison, backoff on
failure — but it means the token is the whole story. Treat it accordingly.

If you want a second credential, put **Cloudflare Access** in front with a
*service token*: it authenticates with `CF-Access-Client-Id` and
`CF-Access-Client-Secret`, which do not collide with `Authorization`. Not tested
here.

**It does not get you claude.ai.** Reachability was never the blocker. The
per-user custom connector flow on claude.ai web, Desktop and mobile wants OAuth
credentials, and a fixed bearer header is beta and org-admin-only (checked
2026-07-29). A public HTTPS URL does not change that. Claude Code works today.

**It merges your clients for rate-limiting purposes.** Every request now arrives
at the origin from `127.0.0.1` — the origin sees `cloudflared`, not the caller.
The failed-authentication backoff therefore treats all your devices as one
address, so a run of wrong tokens from anywhere slows everything through the
tunnel.

`X-Forwarded-For` and `CF-Connecting-IP` do carry the real client address, and
this server ignores both on purpose: a header the client sets is a header an
attacker changes, and a limiter keyed on one is a limiter that is stepped around
by editing a request. Trusting them would need the origin to accept those
headers *only* from Cloudflare's ranges, which is a second thing to keep correct
forever in exchange for a control that is currently correct by construction.

## Taking it down

```sh
kill %1        # or Ctrl-C in whichever terminal holds cloudflared
```

The tunnel is the only thing that was public. `brain serve` keeps listening on
loopback until you stop it too, and stopping both leaves nothing exposed —
there is no state to unwind and no registration on Cloudflare's side for a quick
tunnel. Rotate the token with `brain serve --new-token` if you think it leaked;
every client wired to the old one stops working, which is the point.
