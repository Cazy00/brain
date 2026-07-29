# Backlog — what is known, deferred, and not done

Opened 2026-07-29, at the end of the session that closed all seven stages of
[the setup UX spec](specs/2026-07-25-setup-ux-redesign-design.md).

This file exists because the alternative is folklore. Every item below was a
deliberate decision, not an oversight, and each one is flagged in the code at
the point where somebody would trip over it — this is the index, not the only
record.

**Nothing here is blocking.** The four commands work, 445 tests pass (with one
known environment failure, item 5), and the system is usable as it stands.

---

## Where things stand

| Plan | Stages | Status |
|---|---|---|
| [2026-07-25-setup-foundation.md](plans/2026-07-25-setup-foundation.md) | 1–3: backends, `brain setup`, the bootstraps | closed, 69/69 |
| [2026-07-29-connect-retire-docs.md](plans/2026-07-29-connect-retire-docs.md) | 4–6: `connect --apply`, `retire`, the docs | closed, 41/41 |
| [2026-07-29-serve.md](plans/2026-07-29-serve.md) | 7: `brain serve` | closed, 18/18 |

The four commands the spec set out to build all exist and all end in a working
state: `brain setup`, `brain connect`, `brain serve`, `brain retire`.

---

## 1. `brain serve` has no read-only mode

**Where:** `bin/brainlib/serve.py` (module docstring), `SETUP.md` Part 8.

`brain_capture` is reachable over the HTTP transport and it **writes** — notes
land in the brain, get committed, and get pushed. Whoever holds the token can
do that. The docs say so plainly rather than implying a subset, which is the
honest interim position, but it is an interim position.

The spec called this future work and it stayed future work. The obvious shape
is a `--read-only` flag that serves a filtered tool list; the thing to be
careful about is that filtering must happen server-side, not by trusting a
client to not call the tool.

**Done looks like:** `brain serve --read-only` exposes four tools instead of
five, with a test that asserts `brain_capture` is absent from `tools/list` AND
that calling it by name is refused.

## 2. `brain serve` has no rate limiting

**Where:** `bin/brainlib/serve.py` (module docstring).

Fine on loopback, which is the default and where almost everyone will leave it.
Not fine the moment anyone runs it on a public bind for real: there is nothing
in the server that would slow down a loop guessing tokens. The token is 32
random bytes, so guessing it is not a practical attack — but "not practical"
is an argument, and a limiter is a control.

**Done looks like:** a per-IP failure counter with a backoff, and a test that a
run of bad tokens starts getting refused without a valid one being affected.

## 3. claude.ai web, Desktop and mobile cannot use `brain serve`

**Where:** `bin/brainlib/serve.py` (module docstring), `SETUP.md` Part 8,
`README.md` ("what it deliberately does not do").

Checked against Anthropic's connector documentation on **2026-07-29**: adding a
custom connector by URL offers OAuth Client ID and Client Secret; a fixed
bearer token is supported through `static_headers`, which is **beta** and is
entered by an **organization administrator**, not an individual. So a personal
bearer-token server is not consumable there.

Claude Code works today and the exact command is in SETUP.md Part 8.

**Done looks like:** OAuth 2.1 with dynamic client registration or a Client ID
Metadata Document, plus protected-resource metadata and a `401` carrying
`WWW-Authenticate: Bearer resource_metadata=…`. This is a large piece — an
authorization server, not a flag — and it is the reason the spec split `serve`
into its own stage in the first place. Re-check the beta status before starting:
if `static_headers` leaves beta for individuals, this becomes unnecessary.

## 4. Windows is verified by CI only

**Where:** `bin/brainlib/osbackend.py` (`SchtasksScheduler`, `CredmanKeystore`),
`SETUP.md` Part 0, `README.md`.

The full suite runs on `windows-latest` on every push, so the argv each Windows
backend builds is checked. What CI cannot reach is unverified and is described
that way rather than claimed: how the path picker behaves in a real terminal,
whether a scheduled task actually fires, whether Credential Manager prompts.

`CredmanKeystore` carries a specific open question in its own docstring —
whether the `CredentialManager` PowerShell module is present on a stock box —
which was to be confirmed on the Windows runner and has not been.

**Done looks like:** somebody with a Windows machine runs `brain setup`,
`brain connect --all --apply`, `brain schedule install`, and a vault round
trip, and writes down what actually happened.

## 5. One test fails on a machine with `claude` in `/usr/bin`

**Where:** `tests/test_brain.py`, `InitTests.env_with` (docstring).

`test_init_defers_when_claude_cli_absent` builds a PATH with no `claude` on it
and asserts its own sandbox is clean. That PATH still contains `/usr/bin`, so
on a machine where Claude Code is installed there the premise is false and the
assertion refuses. **This is the assertion working.** It predates all three
plans and must not be "fixed" by weakening it.

**Done looks like:** the no-claude PATH is a temp directory of symlinks to just
the tools `init` needs, rather than `/usr/bin`. Fiddly to keep working on
Windows, which is why it was not done under time pressure.

## 6. A commit message on `main` is garbled

**Where:** commit `05d1f8f`, "setup: wire brain setup into the CLI".

Backticks in a shell heredoc ate the words "brain connect", leaving the
sentence "…setup installs the brain and  — run from the brain itself — wires
it". The code is unaffected: an amended commit with the correct message exists
in the reflog and is byte-identical in content.

Fixing it needs a history rewrite of one already-pushed commit
(`git push --force-with-lease`), which was declined by the permission layer in
the session that made it.

**Done looks like:** either the force-push, or a decision to leave it — the
content is fine and the cost is one confusing sentence in the log.

## 7. `doctor` reports this repo as public, correctly

**Where:** not a defect. Recorded so nobody "fixes" it.

`bin/brain doctor` run inside **this** checkout prints `[RED] YOUR BRAIN IS
PUBLIC`. That is right: this is the public template, not somebody's brain, and
it is supposed to be public. The check has no way to tell the difference and
should not — a check that could be talked out of that warning is worse than one
that cries wolf on the one repo where the wolf is invited.

Anyone running `doctor` here should read past that line. Anyone running it in
their **own** brain should not.

---

## Deferred elsewhere, not by this work

- **The gap loop** — [specs/2026-07-29-agent-brain-gap-loop-design.md](specs/2026-07-29-agent-brain-gap-loop-design.md),
  deferred in commit `ece23ea` because Hermes Agent supplies the harness it
  assumed would need building. That note records its own revival trigger.
