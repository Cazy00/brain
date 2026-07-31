# Backlog — what is known, deferred, and not done

Opened 2026-07-29, at the end of the session that closed all seven stages of
[the setup UX spec](specs/2026-07-25-setup-ux-redesign-design.md). Last worked
2026-07-29.

This file exists because the alternative is folklore. Every item below was a
deliberate decision, not an oversight, and each one is flagged in the code at
the point where somebody would trip over it — this is the index, not the only
record.

**Nothing here is blocking.** The four commands work and 474 tests pass, with no
known failures on any platform this project can run.

The numbering is stable: items keep their number once closed, because "item 3"
in a commit message should still mean item 3 in a year. Six of the nine are
closed and stay listed with what closed them. Items 8 and 9 were both opened AND
closed on 2026-07-29 by the work that closed the others — 8 by verifying the new
documentation, 9 by running a real tunnel in front of the server rather than
reasoning about one. Two of the three still open need hardware or an account
this project does not have.

---

## Where things stand

| Plan | Stages | Status |
|---|---|---|
| [2026-07-25-setup-foundation.md](plans/2026-07-25-setup-foundation.md) | 1–3: backends, `brain setup`, the bootstraps | closed, 69/69 |
| [2026-07-29-connect-retire-docs.md](plans/2026-07-29-connect-retire-docs.md) | 4–6: `connect --apply`, `retire`, the docs | closed, 41/41 |
| [2026-07-29-serve.md](plans/2026-07-29-serve.md) | 7: `brain serve` | closed, 18/18 |
| [2026-07-29-serve-hardening.md](plans/2026-07-29-serve-hardening.md) | backlog items 1, 2, 5 | closed, 18/18 |
| — | items 8 and 9, found while verifying the above | closed, no plan |
| [2026-07-30-business-partition.md](plans/2026-07-30-business-partition.md) | M/P partition, the drop box, `publish` | closed, 6/6 |

The four commands the spec set out to build all exist and all end in a working
state: `brain setup`, `brain connect`, `brain serve`, `brain retire`. A fifth,
`brain publish`, compiles the customer-facing copy.

The business partition was the first work here that is not about the toolbelt:
it makes a brain safe to put in front of customers. Built 2026-07-30, 585 tests
green.

Its prerequisite is **met**: Hermes Agent's own MCP client was pointed at both
live endpoints and registered exactly the partition — four read tools against
P, `brain_capture` against M's drop box, nothing else — and a capture it made
landed in M stamped `source: support-bot` despite the call claiming `local`. No
new code was needed, and no API key: the MCP client layer does not involve the
model. One trap came out of it — Hermes parks every HTTP MCP server under
`mcp` 2.0.0 and needs its own pinned `mcp==1.26.0`.

Four things it found that no test would have, because they only appear when
both endpoints are actually running (see the runbook):

- Two endpoints run by the same user on one host **share one bearer token** —
  `brain serve` reads a single keystore entry — so P's read token also opens
  M's drop box. Different hosts, or at minimum different OS users.
- A drop box on a host with no git identity wrote every note and failed every
  commit, and reported each one to the bot as a failure — inviting it to retry
  the same claim until the daily cap. A capture that reached disk is now
  acknowledged, with the identical response either way.
- `ensure_hooks` announced "installed git hooks" in a directory that is not a
  git repo, and git's "fatal: not in a git directory" went with it — into the
  MCP responses a customer-facing bot reads out. A compiled brain is exactly
  such a directory until somebody runs `git init`.
- The agent side has a version cliff: `mcp` 2.0.0 removes the import Hermes
  uses for HTTP transport, so every brain endpoint parks silently and the bot
  comes up with no tools and nothing in its answers to say why.

Two deliberate departures from the plan, both recorded in the commits: the tree
is built in a temp directory and swapped in only once clean (building in place
would take the LIVE artifact down on a refused audit), and the review queue
counts notes that can never be published in a footer instead of listing them
forever.

---

## Waiting on the owner, not on work

Recorded 2026-07-30. Everything still open needs a **person to answer something**
before any code is worth writing — none of it is blocked on effort, and picking
any of it up without asking first would be building on a guess.

Whoever works on this repo next should raise these rather than wait to be asked.
The owner said on 2026-07-30 that they will confirm or deny each one when it
comes up, so the questions matter more than the estimates.

| Ask | Why it decides the work | Item |
|---|---|---|
| ~~**Do you want the brain inside the claude.ai app**~~ | **ANSWERED 2026-07-31: yes.** The owner has a domain and a Max plan, and wants the brain reachable from a phone — but explicitly NOT Claude-only, so the target is the MCP authorization spec with claude.ai as its first client. Re-checked the same day: individuals still get OAuth only, and `static_headers` is org-admin by design rather than a beta flag waiting to flip. **Next step is a plan, from [handoffs/2026-07-31-remote-access-and-observability.md](handoffs/2026-07-31-remote-access-and-observability.md)** — which also corrects this file: dynamic client registration turns out NOT to be required. | 3 |
| **Do you have a Windows machine to hand yet?** | It is the only thing that can close item 4. Nothing on macOS or Linux moves it, and CI already covers everything CI can reach. | 4 |

One thing NOT to ask, because it is settled:

- **Do not re-raise item 7 as a defect.** `doctor` calling this repo public is
  correct and is supposed to stay.

**Reversed 2026-07-31:** a brain IS now wanted on the development machine. The
2026-07-30 position ("this box is the template workshop, not a place notes
live") was right while that was all it was; the owner has since decided to run
their real brain there and expose it, which is what item 3 is now for. The real
brain is a SEPARATE clone — this checkout stays the public template, and
nothing personal is ever committed here.

Before starting item 3 on a "yes", re-check `static_headers` first — it was
still OAuth-only for individuals on 2026-07-29, and if that changes the item
becomes a settings change instead of a codebase.

---

## 1. `brain serve` has no read-only mode — CLOSED 2026-07-29

**Closed by:** `70ffa1d`, `brain serve --read-only`.

Serves `brain_search`, `brain_read`, `brain_links` and `brain_recent`; refuses
`brain_capture`. The filtering happens in the dispatcher, so a client that
never read `tools/list` and calls the tool by name is refused too — that half
is the actual control, and it has its own test.

Which tools qualify is derived from the tool table's `readOnlyHint`
annotations, failing closed: a tool added later with no annotation is left OUT
of read-only serving, and a test refuses any tool that declares nothing.

What it does **not** do, stated here because the flag invites the opposite
reading: every note is still readable by whoever holds the token. Read-only is
also a property of the process rather than of the token, so serving both modes
at once means two processes on two ports.

## 2. `brain serve` has no rate limiting — CLOSED 2026-07-29

**Closed by:** `49c5295`, a per-address backoff on failed authentication.

Five failures from an address are free; each one after that costs 1s, 2s, 4s,
capped at five minutes, answered `429` with `Retry-After`. Always on, no flag
to disable it.

Three decisions in it have costs, all documented at the point they are made:
the key is the TCP peer and `X-Forwarded-For` is not trusted (so a tunnel's
clients share one bucket); `Origin` refusals are not counted (or any web page
could lock the operator out with a `fetch` loop); and the table is bounded (an
unbounded one is a memory exhaustion primitive reachable without a token).

## 3. claude.ai web, Desktop and mobile cannot use `brain serve` — OPEN

**Where:** `bin/brainlib/serve.py` (module docstring), `SETUP.md` Part 8,
`README.md` ("what it deliberately does not do").

Checked against Anthropic's connector documentation on **2026-07-29**: adding a
custom connector by URL offers OAuth Client ID and Client Secret; a fixed
bearer token is supported through `static_headers`, which is **beta** and is
entered by an **organization administrator**, not an individual. So a personal
bearer-token server is not consumable there.

**Re-checked 2026-07-29, later the same day**, because this entry says to do
that before starting and a cheap check that closes a large item is worth more
than the item. Unchanged: the individual "add a custom connector by URL" flow
still documents only *"an OAuth Client ID and OAuth Client Secret for your
server"*, with no bearer-token or static-header path offered to a person on a
personal plan. Re-check again before anyone starts building; the answer today
is that nothing has moved.

Claude Code works today and the exact command is in SETUP.md Part 8 — including
through a tunnel, which was verified end to end on 2026-07-29
(`setup/runbooks/tunnel-cloudflare.md`). Reachability is not what blocks this
item, and no amount of tunnelling will unblock it.

**Done looks like:** OAuth 2.1 with dynamic client registration or a Client ID
Metadata Document, plus protected-resource metadata and a `401` carrying
`WWW-Authenticate: Bearer resource_metadata=…`. This is a large piece — an
authorization server, not a flag — and it is the reason the spec split `serve`
into its own stage in the first place. Re-check the beta status before starting:
if `static_headers` leaves beta for individuals, this becomes unnecessary.

## 4. Windows is verified by CI only — OPEN

**Where:** `bin/brainlib/osbackend.py` (`SchtasksScheduler`, `CredmanKeystore`),
`SETUP.md` Part 0, `README.md`.

The full suite runs on `windows-latest` on every push, so the argv each Windows
backend builds is checked. What CI cannot reach is unverified and is described
that way rather than claimed: how the path picker behaves in a real terminal,
whether a scheduled task actually fires, whether Credential Manager prompts.

`CredmanKeystore` carries a specific open question in its own docstring —
whether the `CredentialManager` PowerShell module is present on a stock box —
which was to be confirmed on the Windows runner and has not been.

`InitTests.env_with` also keeps its pre-2026-07-29 PATH construction on Windows
only (see item 5), so what that class proves there is weaker than what it proves
on POSIX. Worth folding into the same session as the rest of this.

**Done looks like:** somebody with a Windows machine runs `brain setup`,
`brain connect --all --apply`, `brain schedule install`, and a vault round
trip, and writes down what actually happened.

## 5. One test fails on a machine with `claude` in `/usr/bin` — CLOSED 2026-07-29

**Closed by:** `6267293`, a PATH built from symlinks instead of borrowed from
the system.

`test_init_defers_when_claude_cli_absent` builds a PATH with no `claude` on it
and asserts its own sandbox is clean. That PATH used to contain `/usr/bin`,
where some Linux packages install `claude`, so on those machines the premise
was false and the assertion refused — the assertion working, and the suite red
for a reason unrelated to anything under test.

The assertion did not move. The PATH is now a temp directory of symlinks to
just the tools `init` needs, so the premise is constructed rather than assumed.
Verified by putting a `claude` back into the farm and watching the test refuse,
because a fix that quietly made the assertion unreachable would look identical
from the summary line.

Still the old construction on Windows, for the reasons in the docstring —
folded into item 4.

## 6. A commit message on `main` is garbled — CLOSED 2026-07-29, as a decision

**Where:** commit `05d1f8f`, "setup: wire brain setup into the CLI".

Backticks in a shell heredoc ate the words "brain connect", leaving the
sentence "…setup installs the brain and  — run from the brain itself — wires
it". The code is unaffected: an amended commit with the correct message exists
in the reflog and is byte-identical in content.

**Decided 2026-07-29: leave it.** Fixing it means rewriting published history
(`git push --force-with-lease`) of the repo that is the public template, which
diverges every existing clone to buy one tidy sentence in a log. The content is
right and the cost of the fix exceeds the cost of the defect.

This is recorded rather than deleted so it stops reading as outstanding work
and nobody reopens it.

## 7. `doctor` reports this repo as public, correctly — NOT A DEFECT

**Where:** not a defect. Recorded so nobody "fixes" it.

`bin/brain doctor` run inside **this** checkout prints `[RED] YOUR BRAIN IS
PUBLIC`. That is right: this is the public template, not somebody's brain, and
it is supposed to be public. The check has no way to tell the difference and
should not — a check that could be talked out of that warning is worse than one
that cries wolf on the one repo where the wolf is invited.

Anyone running `doctor` here should read past that line. Anyone running it in
their **own** brain should not.

## 8. `serve --help` and `setup --help` show the global help — CLOSED 2026-07-29

**Closed by:** `84e591c`.

Found while verifying the documentation for `--read-only` by running it:
`brain serve --help` printed the toolbelt's global page, so the flag was written
up in SETUP.md, README.md and serve's own USAGE while the one place somebody
looks — asking the command — did not mention it.

`main()` still answers `<command> --help` for everything else, because that
guard exists for a good reason: `init --help` once ignored argv and wired the
machine for somebody who only asked what it did. `setup` and `serve` are named
exceptions, never inferred, because "does this handler look like it checks
--help first?" is exactly the question the original incident answered wrongly.
Each has a test asserting its own usage comes back and that nothing was done.

## 9. `brain serve` broke connection reuse — CLOSED 2026-07-29

**Closed by:** `1dadd72`.

Not on the original list. Found on 2026-07-29 by running a live Cloudflare
tunnel in front of the server to check the new documentation was true.

Every refusal path answered before reading the request body — deliberately,
since reading a 10 MB body in order to reject it is the denial of service the
size cap exists to prevent — leaving those bytes in the socket. On a kept-alive
HTTP/1.1 connection they were parsed as the next request line, so the client's
next request, with a correct token, came back `400` or `501`. One wrong token
made the connection useless. Present since the transport was written.

It was invisible to the test suite because every test there opened one
connection per request and closed it — the transport was only ever exercised in
the pattern real traffic does not use. A proxy pools connections, which is what
a tunnel is, which is the deployment the command exists for.

Recorded because the lesson outlives the fix: **the local suite could not have
found this.** `TestConnectionReuse` now holds the property in both directions,
and `setup/runbooks/tunnel-cloudflare.md` records what a real tunnel was
observed to do.

---

## Deferred elsewhere, not by this work

- **The gap loop** — [specs/2026-07-29-agent-brain-gap-loop-design.md](specs/2026-07-29-agent-brain-gap-loop-design.md),
  deferred in commit `ece23ea` because Hermes Agent supplies the harness it
  assumed would need building. That note records its own revival trigger.

  **Still deferred as of 2026-07-30, but only half of it.** That spec holds two
  things: the gap loop (deferred, unchanged) and the **M/P partition** under
  "Settled architecture" (no longer deferred — its own revival trigger, *"a
  customer-facing agent makes the partition real"*, has been met, and
  [plans/2026-07-30-business-partition.md](plans/2026-07-30-business-partition.md)
  implements it). Anyone reading that spec's DEFERRED banner should know it
  applies to the loop, not to the architecture underneath it.
