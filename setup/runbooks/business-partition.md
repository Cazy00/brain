# Two brains: the one you keep, and the one customers may see

A brain you can put a customer-facing bot in front of. There are **two** of
them and that is the whole design:

- **M** — your real brain. Everything you know, most of which no customer may
  ever see. It runs a **drop box**: an endpoint that accepts notes and cannot
  read one.
- **P** — a compiled artifact, containing only the notes you approved one at a
  time. It runs **read-only**. The bot talks to this.

The bot's model can be cheap, weak, and talked into anything, because none of
the isolation depends on it behaving. It holds one URL for P and one for M's
drop box, and no path, credential or tool that reaches anything else.

Written 2026-07-30 by standing both endpoints up and running every command
below. Where a check needs two hosts it says so, and says what was verified on
one.

## The publish cycle

```sh
brain publish review                  # what nobody has decided about yet
brain publish approve opening-hours   # may be seen by customers
brain publish deny margin-model       # never; and stop asking
brain publish ~/P                     # compile. Read the report.
cd ~/P && bin/brain serve --read-only --port 8801   # restart, or P still
                                                    # serves the old build
```

`review` lists only what you can actually decide. Notes under `people/` and
`life/`, and anything whose `sensitivity` is not `normal`, can never be
published — they are counted in a footer instead of listed, because no
keystroke clears them and a queue you cannot finish is a queue you stop
reading.

**Read the removals first.** The report ends with what LEFT the published
brain, and that is the line that matters:

```
  REMOVED 1 note(s) — this brain will stop answering anything they covered:
    - reference/price-list.md
```

A removal usually means you superseded something. Both halves of that are
silent on their own: the successor is a new note nobody has reviewed, so it is
not published, and the predecessor is archived, so it is dropped. Correct — a
changed price must not keep serving the old value — but the visible effect is
a bot that starts saying "I don't know" about something it answered yesterday.
`brain publish review` has the successor waiting.

## The two endpoints

Two processes. Two ports. Two tokens. `brain serve` refuses `--drop-box` and
`--read-only` together, because that combination is not a mode — it is two
deployments.

```sh
# On M's host — accepts notes, cannot read one:
brain serve --drop-box --source support-bot --port 8802 --bind 127.0.0.1

# On P's host, run from INSIDE P — answers questions, cannot be written to:
cd ~/P && bin/brain serve --read-only --port 8801 --bind 127.0.0.1
```

`--source` is not optional and is not a label the caller can set: it is stamped
by the endpoint on every note that arrives, and consolidation reads it to tell
a claim a customer fed to a bot from something you wrote yourself. An agent
that can lie about its content can lie about its label, so the label does not
come from the request.

P is served **from inside P**. The serving process then holds no configuration
naming M at all — not a flag, not a path. Pointing M's toolbelt at P's notes
with a flag would work and is one typo away from serving M.

Neither command opens a public port on its own. Put a tunnel in front of P's
(see `tunnel-cloudflare.md`) or bind it where the bot's host can reach it, and
leave M's drop box on loopback or behind a private network.

## Three things that will catch you out

**One host, one user, ONE token.** `brain serve` reads a single value called
`brain-serve-token` from the OS keystore. Run both endpoints as the same user
on the same machine and they share it — which means the token you gave the bot
to READ P also opens M's drop box, and the separation you think you have is a
port number. Verified 2026-07-30: minting a token for the second endpoint
overwrote the first one's. **Different hosts, or at minimum different OS
users.** The checklist below tests for exactly this.

**M's host needs a git identity.** The drop box writes a note and commits it.
With no `user.name`/`user.email` configured, every capture lands on disk and
fails to commit — notes pile up uncommitted, and an uncommitted tree blocks the
consolidation pass that is the inbox's only drain. Found the first time this
was run for real:

```sh
git -C ~/brain config user.name  "Your Name"
git -C ~/brain config user.email "you@example.com"
```

**`git init` your P.** It is not required to serve, but `publish` uses P's git
history to work out what changed since the last build, and the diff is the part
of the report you actually read.

## The isolation checklist

Run it after every deployment change, and again when somebody has moved a
server and half-remembered why the two were separate. `$BOT` is the drop-box
token, `$READER` is P's.

Every command here was run on 2026-07-30 against two live servers. The output
shown is what came back, not what should have.

**1. The drop box serves one tool.**

```sh
curl -s -X POST http://M-HOST:8802/mcp -H "Authorization: Bearer $BOT" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Expect exactly one tool, `brain_capture`. Four tools means you started the
wrong process.

**2. The drop box cannot read, even when asked by name.**

```sh
curl -s -X POST http://M-HOST:8802/mcp -H "Authorization: Bearer $BOT" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call",
       "params":{"name":"brain_search","arguments":{"query":"margin"}}}'
```

```
isError=True
brain_search is not served by this brain. This endpoint serves brain_capture
and nothing else — that is a property of how it was started, not a permission
to retry into.
```

The tool list is a courtesy; a client is free to call any name it likes. This
is the check that matters, because a client ignoring the list is precisely what
the drop box defends against.

**3. A capture arrives, stamped, and tells the caller nothing.**

```sh
curl -s -X POST http://M-HOST:8802/mcp -H "Authorization: Bearer $BOT" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call",
       "params":{"name":"brain_capture","arguments":{
         "text":"A customer asked whether we open on Saturdays.",
         "source":"local"}}}'
```

```
isError=False
captured 2026-07-30-173842-a-customer-asked-whether-we-open-on-satu
```

Note what came back: an id, and nothing else. No path, no "a similar note
already exists", no count. A duplicate hint would turn the drop box into a read
oracle — the bot captures guesses and reads M one question at a time by
watching which ones come back known.

Note also the `"source":"local"` in the request, and what landed in M:

```
---
created: 2026-07-30
source: support-bot
status: draft
---

A customer asked whether we open on Saturdays.
```

The caller claimed to be `local`. The endpoint stamped what it knows.

**4. P serves the four read tools and refuses the write one.**

```sh
curl -s -X POST http://P-HOST:8801/mcp -H "Authorization: Bearer $READER" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call",
       "params":{"name":"brain_capture","arguments":{"text":"planted"}}}'
```

```
isError=True
brain_capture is not served by this brain. This endpoint serves brain_links,
brain_read, brain_recent, brain_search and nothing else …
```

**5. P knows what it should and nothing else.**

```sh
# a question a customer might cause:
… "params":{"name":"brain_search","arguments":{"query":"opening hours"}}
    1. knowledge/reference/opening-hours.md — opening hours (reference, 2026-07-20)

# something only M knows:
… "params":{"name":"brain_search","arguments":{"query":"margin markup"}}
    no hits — rewrite the query into 2-3 lexical variants …
```

The second one is the point of the whole system. P does not contain the private
note, so no prompt, jailbreak or clever phrasing gets it out: there is nothing
to get.

**6. The tokens are different.**

```sh
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://M-HOST:8802/mcp \
  -H "Authorization: Bearer $READER" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":8,"method":"tools/call",
       "params":{"name":"brain_capture","arguments":{"text":"planted"}}}'
```

Expect `401`. A `200` means both endpoints are sharing one keystore — see "One
host, one user, ONE token" above. This is the check people skip and the one
that fails.

**7. P names M nowhere.**

```sh
grep -rIl "$HOME/brain" ~/P        # expect: no output, exit 1
```

`brain publish` audits for this and refuses to build a tree containing M's
path, so a failure here means somebody edited P by hand.

**8. From the bot's host — the two-host checks.** These cannot be verified on
one machine, and they are the ones that make the boundary something other than
a secret being kept:

```sh
test ! -e /path/to/M                          # M's repo is not on this host
curl -s -m 5 http://M-HOST:8802/mcp           # expect: connection refused/timeout
```

Egress rules so the bot's host cannot reach M's host at all — stated as a
requirement, not a suggestion. With them, the drop box is reached by the one
route you opened, and a stolen bot token is worth one drop box. Without them,
the whole boundary is a bearer token in a config file on a machine running a
model you do not control.

## What this does NOT protect

**Everything in P is readable by any customer who talks the bot into reciting
it.** There is no second tier inside P, no "internal" notes it will decline to
mention. Curate P as if it were a public web page, because functionally that is
exactly what it is.

**A cheap model will say wrong things.** The partition governs what the bot can
KNOW, not what it says. Nothing here stops a weak model from misreading a note
or inventing an answer. That is a product problem, and it is not solved by
adding notes to P.

**An inbox note is not knowledge.** Anything the drop box accepts is a
proposal: it lands in `inbox/`, outside default search, stamped with its
source, and only consolidation — the one pinned, expensive runner — decides
whether it becomes a note. The consolidation prompt is told to corroborate
anything not stamped `local` and never to supersede a canonical note on the
strength of one. Read what arrives before you trust it: `rg --no-ignore
support-bot knowledge/inbox/`.

**The daily cap is a budget, not an airlock.** 200 captures per source per day
by default (`--daily-cap`), counted off the inbox so a restart is not a reset.
It stops a runaway bot filling a disk. It does not stop 200 wrong claims.
