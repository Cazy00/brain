# `brain serve` hardening, and one honest test — Implementation Plan

**Goal:** Close the three backlog items that are actually closable, and leave
the other four correctly described rather than half-done. After this the server
can be run without handing over write access, a guessing loop against it costs
something, and the test suite is green on every machine rather than on most of
them.

**Predecessor:** [2026-07-29-serve.md](2026-07-29-serve.md), closed 2026-07-29.
That plan shipped the transport and listed what it deliberately did not do;
[BACKLOG.md](../BACKLOG.md) recorded those omissions as items 1–7. This plan
takes 1, 2 and 5.

**Not in this plan, and why:**

- **Item 3 (OAuth 2.1 for claude.ai)** — an authorization server, not a flag,
  and it cannot be verified from here: it needs a public tunnel and a claude.ai
  account to test against. The backlog also says to re-check whether
  `static_headers` has left beta before starting, because that would make the
  whole piece unnecessary. Unchanged, still item 3.
- **Item 4 (Windows verified by CI only)** — needs somebody at a Windows
  machine. Nothing in this plan can move it, and Task 3 is written so it does
  not make it worse.
- **Item 6 (the garbled commit message)** — decided 2026-07-29: leave it. The
  commit's *content* is byte-identical to the correct one; only the message
  reads oddly. Rewriting it means force-pushing published history of a repo
  that is the public template, which diverges every existing clone to buy one
  tidy sentence in the log. Recorded as a decision so it stops reading as
  outstanding work.
- **Item 7** — not a defect. Nothing to do, which is the entry's whole point.

## Global Constraints

Carried forward from the predecessor plan; violating any of them fails review.

- **Python 3.9 floor.** No `match`, no `X | Y` unions at runtime, no
  `dict1 | dict2`, no `str.removeprefix`.
- **Zero third-party dependencies.** `bin/brain-mcp` advertises "zero
  dependencies and no vendor SDK" and that claim is load-bearing.
- **The stdio path does not change.** It serves all five tools, including the
  write tool, and it always did — stdio is a subprocess a client spawned on the
  machine the user is sitting at. Read-only is a property of the exposed
  socket, not of the tool layer.
- **No credential ever reaches the repo.** Tests use a `FileKeystore` in a temp
  directory; the fixture bearer is named `BEARER`, not `TOKEN`, because the
  commit gate cannot tell a realistic fixture from a real credential and is
  right not to try.
- **No test may bind a public interface.** `127.0.0.1`, port 0, always.
- **No test may sleep to observe a timeout.** The limiter takes its clock as a
  parameter; a suite that sleeps for a backoff is a suite that gets deleted.
- **All dates in code, comments and docs are absolute** (`2026-07-29`).
- **Existing tests must keep passing.** 445 as of 2026-07-29, of which one
  fails — item 5, which Task 3 fixes rather than silences.
- **Match the surrounding prose style.** Comments explain *why*, especially why
  an obvious alternative was rejected.

---

### Task 1: `brain serve --read-only`

**Files:**
- Modify: `bin/brainlib/mcp.py` (annotate the tool table; `handle`/`call_tool`
  take an allowlist)
- Modify: `bin/brainlib/serve.py` (`--read-only`, threaded through
  `make_server` and the banner)
- Test: `tests/test_serve.py`

**The one rule:** filtering happens **server-side**. A tool list that omits
`brain_capture` while `tools/call` still runs it is not a read-only mode, it is
a suggestion — the client is the thing being defended against, so it cannot be
the thing enforcing the restriction. Both halves are tested separately for
exactly that reason.

**How the set is derived — fail closed.** Every entry in `TOOLS` declares an
explicit `annotations.readOnlyHint` (the MCP spec's own field, so this is
information a client gets anyway and not a private convention).
`READ_ONLY_TOOLS` is the tools whose hint is exactly `True`. A tool added later
with no annotation is therefore **excluded** from read-only serving rather than
included by default, and a test asserts every tool declares one — so the
omission fails the suite instead of quietly exposing a new write path over a
socket somebody believed was read-only.

**Interfaces:**
- `mcp.READ_ONLY_TOOLS` — tuple of names.
- `mcp.handle(msg, allow=None)` — `None` means every tool, the stdio default.
- `mcp.call_tool(name, args, allow=None)` — a name outside `allow` comes back
  as a tool error, not as "unknown tool". The mode is printed in the startup
  banner and is a property of the deployment, not a secret, so telling the
  model the truth is more useful than pretending the tool does not exist.
- `serve.make_server(..., allow_tools=None)`, `serve.startup_notes(host, port,
  read_only=False)`.

- [x] **Step 1: Write the failing tests.** `tools/list` returns four tools and
      not `brain_capture`; `tools/call` on `brain_capture` by name is refused
      even though it was never listed; a read tool still works in the same
      server; every tool declares `readOnlyHint`; the derivation is fail-closed;
      stdio and a default `serve` still expose all five.
- [x] **Step 2: Run tests to verify they fail**
- [x] **Step 3: Implement**
- [x] **Step 4: Run tests to verify they pass**, then the whole suite
- [x] **Step 5: Commit** — `serve: --read-only, enforced where the client cannot reach it`

---

### Task 2: Per-IP backoff on failed authentication

**Files:**
- Modify: `bin/brainlib/serve.py` (`_Limiter`, wired into `_allowed`)
- Test: `tests/test_serve.py`

The token is 32 random bytes, so guessing it is not a practical attack — but
"not practical" is an argument and a limiter is a control, and the difference
matters the first time somebody runs this on a public bind.

**Design decisions, each of which has a reason worth not re-deriving:**

- **Keyed on the TCP peer address, and `X-Forwarded-For` is not trusted.** A
  header the client sets is a header an attacker rotates, which would trade a
  real control for one that can be stepped around by editing a request. The
  consequence is stated rather than hidden: behind a tunnel every client
  presents as the tunnel, so a guessing run through it slows down everyone
  through it. That is the correct direction to fail.
- **The block is checked before the token is compared.** A blocked caller is
  refused without their guess being looked at, which is the entire slow-down.
  It also means a *correct* token from a blocked address waits — deliberate,
  and the honest cost of the control.
- **Only failed authentication counts.** An `Origin` refusal does not: those
  requests come from the operator's own browser, so counting them would let any
  web page lock the operator out of their own server by pointing a `fetch` at
  it — turning a defence into the attack.
- **Success clears the counter**, so a typo before a correct token costs
  nothing.
- **The table is bounded.** A dict keyed by remote address that only ever grows
  is a memory-exhaustion primitive; a limiter that becomes the denial of
  service it was added to prevent is worse than no limiter. Idle entries are
  pruned and the table has a hard cap.
- **The clock is a parameter.** Tests assert on the backoff without sleeping
  through it.

Shape: five free attempts, then `min(1 * 2**(n-6), 300)` seconds — 1s, 2s, 4s …
capped at five minutes. At the cap that is roughly twelve attempts an hour
against a 256-bit secret.

- [ ] **Step 1: Write the failing tests.** Unit, with an injected clock: free
      attempts pass; the next one blocks; the backoff grows and is capped; time
      passing clears it; success clears it; a second key is untouched; the table
      stays bounded. Integration: a run of bad tokens ends in `429` carrying
      `Retry-After`; a good token interleaved resets the count; a fresh server
      answers the same good token `200`, proving it was the limiter and not the
      server.
- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement**
- [ ] **Step 4: Run tests to verify they pass**, then the whole suite
- [ ] **Step 5: Commit** — `serve: make guessing the token cost something`

---

### Task 3: The no-claude PATH stops being a lie

**Files:**
- Modify: `tests/test_brain.py` (`InitTests.env_with`)

`test_init_defers_when_claude_cli_absent` builds a PATH with no `claude` and
asserts its own sandbox is clean. That PATH contains `/usr/bin`, which is where
some Linux packages put `claude` — so on those machines the premise is false and
the assertion refuses. **This machine is one of them**, which is what makes the
fix verifiable here rather than argued about.

The fix is the one the backlog names: a temp directory holding symlinks to just
the tools `init` reaches for through PATH, used for *both* branches so the two
differ by exactly one thing — whether `claude` is on it. Which tools those are
gets determined by running it, not by reading the source and hoping.

**The Windows caveat, handled rather than ignored.** A symlink there needs
Developer Mode or admin, and `git` is a shim with libraries beside it that does
not survive being copied somewhere else. The bug is POSIX-only — `/usr/bin` is
not a place Windows keeps anything — so the workaround is too, and the fallback
is today's construction with a comment saying why. Windows CI must stay exactly
as green (or as red) as it is now; this task is not the place to change it.

- [ ] **Step 1: Determine empirically what `init` needs on PATH** — run it with
      a minimal one and add what actually breaks, rather than guessing from the
      source
- [ ] **Step 2: Implement**
- [ ] **Step 3: Run the full suite and confirm it is green on this machine** —
      445 passing, zero failures, which it has not been at any point in this
      plan's predecessors
- [ ] **Step 4: Commit** — `tests: a no-claude PATH that is actually claude-free`

---

### Task 4: Documentation, and closing the entries

**Files:**
- Modify: `SETUP.md` (Part 8 — two "there is no…" bullets are now false)
- Modify: `README.md` ("What it deliberately does not do")
- Modify: `bin/brain` (the `serve` usage block)
- Modify: `bin/brainlib/serve.py` (module docstring, `USAGE`)
- Modify: `docs/superpowers/BACKLOG.md`

Docs first, because two of them currently state as fact things this plan makes
untrue — "There is no read-only mode yet", "There is no rate limiting" — and a
doc that oversells is recoverable while a doc that undersells a security control
gets somebody to build the control again.

The backlog is edited, not appended to: items 1, 2 and 5 become done with the
commit that did them, item 6 becomes a recorded decision, and 3, 4 and 7 stay
exactly as they are. An item that is done and still listed as open is the same
folklore problem the file was opened to solve.

- [ ] **Step 1: Write the docs**
- [ ] **Step 2: Verify every command in them runs** — on this machine, on
      loopback, not by eye
- [ ] **Step 3: Commit** — `serve: document the read-only mode and the limiter`

---

## Self-review

**Backlog coverage.** Item 1 → Task 1, with the client-cannot-enforce-it rule
tested on both halves. Item 2 → Task 2, keyed and bounded, with every decision
that has a cost stating what the cost is. Item 5 → Task 3, fixed on the machine
where it reproduces. Items 3, 4, 7 unchanged and still accurate. Item 6 recorded
as decided. Docs and backlog → Task 4.

**What this deliberately does not do.** No per-token scoping — read-only is a
property of the process, so serving both modes at once means two processes on
two ports, which is fine and is what the docs will say. No lockout notification.
No persistence of the limiter across restarts: a restart clears it, which is a
real gap and a bad trade to close, because persisting it means writing
attacker-controlled keys to disk on the one component that faces a network.

**The risk that is real.** `--read-only` makes the *exposed* surface smaller; it
does not make the brain safe to expose. Everything in it is still readable by
whoever holds the token, which for a second brain is most of what there is to
protect. The docs must not let the flag read as "now it is safe to publish", and
the limiter must not read as authentication.
