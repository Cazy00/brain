# Setup, wiring and retirement — UX redesign

Status: approved 2026-07-25. Supersedes nothing; this is the first spec for the
install surface.

## Why this exists

A real first run on 2026-07-25 (macOS, `curl … | sh` from `main`) ended in a RED
state and produced contradictory output. The transcript is the requirements
document, so the failures are recorded here verbatim rather than paraphrased.

**1. The installer lied about the remote.** It printed:

```
[warn] gh could not create 'my-brain' (name taken, or scopes missing)
[-- ] no remote yet — your notes are LOCAL ONLY and are not backed up
[ok ] remote is not publicly readable
```

Three lines, two of which contradict each other. `install.sh:227` runs
`gh repo create … --source . --remote origin --push` and treats any non-zero
exit as total failure, but `gh` adds the remote *before* it pushes. The remote
existed; `REMOTE_SET` said it did not. The visibility check that follows reads
git directly, so it saw the truth and reported it — hence one block of output
disagreeing with the next.

**2. So the install ended RED.** Because the push never ran, `doctor` reported
`[RED] no upstream tracking — backup state unknown`. A fresh install must not
finish in a state its own health check calls red.

**3. The path prompt has no line editing.** The operator pressed `^R` expecting
history search; it was swallowed as literal text by a POSIX `read`. There is no
completion, no history, and no validation message that explains a rejection.

**4. Wiring is split across two commands and finishes with homework.**
`install.sh` does some of it, `bin/brain init` does the rest, and the final
instruction is to paste a text block into `~/.claude/CLAUDE.md` by hand. Nobody
owns the end state, which is why nobody caught (2).

**5. Most users will hand this to an agent, not run it.** The current output is
written for a human reading a terminal. An agent gets prose instructions with no
machine-readable result and no defined behavior when there is no TTY.

**6. `brain reset` is dense, unpreviewable and misnamed.** ~40 lines of prose,
internal vocabulary (`de-wiring`, `launchd`, `user scope`), a long confirm
phrase, no way to see what it would do, no discoverability, and it ends by
pointing at a manual instead of offering the obvious next step.

**7. macOS only, not stated plainly.** README says "macOS today; … runs on Linux
with two DIY pieces". Windows is not mentioned at all, and does not work.

## Decisions

Recorded with the alternative that was rejected, because the alternative is the
part that gets re-litigated later.

| Decision | Rejected alternative | Why |
|---|---|---|
| Windows, Linux and macOS all first-class | macOS + Linux native, Windows via WSL | Owner's call. Accepts a larger surface and CI-only Windows verification. |
| One `brain setup`, same code path for human and agent | A separate agent-facing document | Two paths means the agent path rots silently. One path with `--yes`/`--json` cannot drift from the path humans exercise daily. |
| Never install prerequisites; state the consequence and the command | Auto-install via detected package manager | A piped-curl script installing system packages unprompted is more trust than this should assume, and corporate machines forbid it. |
| Shortlist picker + typed path with completion | Full arrow-key directory browser | The browser needs raw terminal mode (`termios`/`msvcrt`), redraw handling, and a non-TTY fallback *anyway* — so it is the shortlist plus a browser. Deferred until the shortlist proves insufficient. |
| MCP wiring is its own command, run after setup | One flow that installs and wires | Owner's call. Each step ends in a working state; you can stop after any of them. |
| Remote access is opt-in and near-invisible | Offer local/remote as a mode choice in setup | Most users are local-only forever. They must never read about tunnels or tokens. |
| Auth enforced by the server, not the tunnel | Rely on Cloudflare Access alone | `cloudflared` will publish an unauthenticated origin to a public hostname. One mis-scoped route, one bypass policy, or any other path to the origin (LAN, localhost, a second tunnel) exposes the whole brain including a **write** tool. A check inside the server travels with the server. |
| `reset` → `retire`, old name kept as an alias | Rename outright | The old name is in shipped docs and possibly muscle memory; a redirect costs three lines. |

## Architecture

Four commands. Each ends in a working state, so stopping after any one of them
is a legitimate outcome rather than an abandoned install.

```
brain setup      you have a brain. Notes work. It is backed up.
brain connect    your agents can reach it.
brain serve      it is reachable from other devices.       (opt-in)
brain retire     all of the above, gracefully undone.
```

`bin/brain init` is absorbed into `brain setup` and removed as a public command.
It survives only as an internal re-wiring routine that `setup` and `connect`
both call, because "re-run init when wiring drifts" is a documented repair and
must keep working.

### `brain setup`

Python, not shell. That choice is what makes Windows possible and what makes the
picker possible, and it is why `install.sh` shrinks to a bootstrap.

Three modes, one implementation:

| Mode | Trigger | Behavior |
|---|---|---|
| Interactive | stdin is a TTY | prompts; shortlist picker |
| Non-interactive | `--yes`, or no TTY | every default; never blocks |
| Agent | `--json` | machine-readable result on stdout; human text on stderr |

The `--json` result reports, per phase: `status` (`ok` \| `skipped` \| `failed`),
a human `detail`, and for anything not `ok`, a `remedy` string that is a literal
command where one exists. This is the contract an agent acts on, so it is
tested like an API, not like output.

Phases, all idempotent, safe to re-run, and re-runnable individually via
`--only <phase>`:

1. **check** — hard prerequisites (`git`, `python` ≥ 3.9) stop the run with the
   exact install command for the detected OS and package manager. Optional
   prerequisites report the *consequence*, not the name:
   - no `gh` → no automatic private backup; you create the repo yourself
   - no `gitleaks` → the secret gate falls back to the built-in scanner only
   - no `age` → no encrypted vault
   - no `rg` → search still works; the grep tier is slower

   Nothing is ever installed.

2. **place** — where the brain lives. Skipped when `--dir` is given.

3. **create** — copy the template, fresh git history on `main`, hooks installed
   via `core.hooksPath`. Refuses a non-empty destination (existing behavior,
   kept) but the refusal names the offending entries.

4. **backup** — the private remote. **Success is determined by inspecting git
   state after the attempt, never by an exit code**, which is the direct fix for
   failure (1). Concretely: run the create, then read `git remote get-url origin`
   and the upstream ref, and report what is actually true. When a remote exists
   but has no upstream, push with `-u` so the phase cannot leave doctor red.
   Visibility is verified as it is today, and a public remote is a hard, loud
   stop.

5. **verify** — `doctor`. Setup's exit code reflects doctor's, so a red install
   is a failed install.

The closing output states what is done, what is not, and the single next step —
which is `connect`, not `serve`:

> Your brain is at ~/brain. It works and it is backed up.
>
> Next: let your agents reach it —  `brain connect`
>
> Only using this on this computer? That is everything.
> Reaching it from other devices is a separate, optional step: `brain serve --help`

Remote access gets exactly one line and is never expanded on here. `connect` is
the only instruction that reads as a next step.

### Path picker

```
Where should your brain live?

  1  ~/brain                        (recommended)
  2  ~/Documents/brain
  3  ~/Library/Mobile Documents/…   iCloud Drive — syncs across your Macs
  4  type a path

>
```

Rules:

- Cloud locations (iCloud, OneDrive, Dropbox) appear **only when detected**, and
  carry the one-line consequence of choosing them.
- A typed path gets `~` expansion, tab completion, and absolute-path resolution.
- A rejection says why: `that directory has 4 files in it`, not `invalid`.
- No raw terminal mode anywhere. Piped or TTY-less input takes the default.

### `brain connect`

```
brain connect                        what is installed here, and what is wired
brain connect <client>               print exactly what is needed        (today's behavior)
brain connect <client> --apply       write it
brain connect <client> --apply --dry-run   show the exact diff, write nothing
brain connect --routing --apply      write the routing block
brain connect --json                 machine-readable, for agents
```

`--apply` is the change that removes the manual paste step. Its rules exist
because this command edits files this system did not create:

- **Merge, never overwrite.** Read, insert or update only the `brain` entry,
  write back.
- **Back up first**, to `<file>.brain-backup-<timestamp>`, and name the backup
  in the output.
- **Refuse on an unrecognized shape.** If the file does not parse, or the
  expected container key is missing and cannot be created unambiguously, print
  the snippet and stop. A refusal is always available and always safe.
- **Idempotent.** Re-applying an already-correct entry reports "already correct"
  and writes nothing.

The routing block is written between explicit markers
(`<!-- brain:routing:start -->` … `end`) so it can be updated in place and
removed cleanly by `retire`. Without markers, updating means guessing where the
block ends, and `retire` cannot undo it at all.

Bare `brain connect` detects which supported clients are actually installed on
this machine and shows one line each: not wired / wired to this brain / **wired
to a different brain** — the last being the case that silently breaks things
today.

### `brain serve` — opt-in remote access

`bin/brain-mcp` is stdio-only. stdio has no URL, which is why "just give them
the connection link" is not possible today and why this and the tunnel request
are the same feature.

HTTP transport is added **alongside** stdio. The stdio path is the default and
does not change; nothing about local use is affected.

```
brain serve                  serve on 127.0.0.1:<port>
brain serve --new-token      mint a token, store it, print it once
brain serve --bind 0.0.0.0   explicit, warned, opt-in
```

Security contract:

- **Refuses to start without a token.** A refusal, not a warning.
- Every request requires `Authorization: Bearer <token>`; comparison is
  constant-time.
- Default bind is loopback. Any other bind requires the explicit flag and prints
  what it is exposing.
- The token lives in the OS keystore (§ Cross-platform), never in the repo —
  `brain lint` forbids credentials there and that rule is not being weakened for
  this.
- `brain_capture` is a **write** tool reachable over this transport. The docs
  must say so plainly; a read-only serving mode is noted as future work, not
  built now.

The tunnel is explicitly **not** this project's concern. Documentation states
the contract a tunnel must satisfy — terminate TLS, forward to this port,
preserve the `Authorization` header — and says nothing about any specific
provider.

### `brain retire`

`reset` remains as an alias that prints the new name and continues.

- `--dry-run` runs every safety check and prints the exact plan. Changes
  nothing. This is the discoverable, safe way to explore the command.
- Default output is three short blocks: **what happens** / **what is kept** /
  **type this**. The current six-paragraph preserved list moves behind
  `--explain`.
- Plain language, with the internal name in parentheses: "stopped the weekly
  background jobs (launchd)".
- The confirm phrase keeps its anti-paste property — still computed from live
  state, so a phrase copied from a chat log cannot match — but is materially
  shorter.
- Ends by **offering to run the install**, rather than pointing at SETUP.md.
- Listed in `--help` with a one-line description.

Everything that makes the current command safe is preserved unchanged: the
backup bundle is written and verified before anything moves, `reset_blockers()`
still has no override flag, the repo is moved rather than deleted, the remote is
never touched, and ownership is checked before any shared machine state is
altered.

## Cross-platform

Three OS-dependent concerns move out of `bin/brain` into small backends with one
interface each. Today `launchctl` and `security` are called inline, which is why
the file is macOS-shaped.

| Concern | macOS | Linux | Windows |
|---|---|---|---|
| Schedules | `launchd` | `systemd --user` timers, fallback `cron` | `schtasks` |
| Secret storage | Keychain (`security`) | `secret-tool`, fallback file mode 0600 | Credential Manager |
| `/brain` skill install | symlink | symlink | **directory junction** (`mklink /J`) |

The junction matters: a Windows symlink needs Developer Mode or admin, a
directory junction needs neither. Where neither is available, fall back to a
copy and have `doctor` flag the copy as stale when the source changes —
a silently stale copy is the failure mode to avoid.

Two additions that will otherwise break Windows quietly:

- **`.gitattributes`** forcing LF on `.githooks/*` and `bin/*`. A CRLF checkout
  corrupts the shebang line; the hook then fails to execute and the commit gate
  is down with no error anyone will read. Git for Windows ships bash, so the
  `sh` hooks themselves are fine — only the line endings are fatal.
- **`brain.cmd`** shim so `brain search …` works in PowerShell and `cmd`.

`install.ps1` mirrors `install.sh`: verify git and python, clone, hand off to
`python bin/brain setup`, passing flags through unchanged.

## Testing

The existing 213 tests in `tests/test_brain.py` stay. Added:

- **CI matrix** — `.github/workflows/gate.yml` currently runs `ubuntu-latest`
  only. It becomes `ubuntu-latest`, `macos-latest`, `windows-latest`.
- **Setup phase tests** — each phase in isolation, idempotency (run twice, same
  result), and the `--json` contract shape.
- **The remote-failure regression** — `gh` adds a remote then fails to push;
  assert setup reports a remote that exists and leaves doctor green. This is
  failure (1) and it must never return.
- **`connect --apply`** — merge preserves unrelated entries; backup is written;
  unrecognized shapes refuse; re-apply is a no-op.
- **`retire --dry-run`** — changes nothing on disk, and its plan matches what a
  real run does.
- **Backend selection** — the right scheduler/keystore is chosen per platform,
  with the platform faked so all three are exercised on any runner.

**Stated risk:** Windows is verified by CI only. Nobody on this project has a
Windows machine to test on. The owner accepted this on 2026-07-25. Anything CI
cannot reach — interactive picker behavior in a real Windows terminal, keystore
prompts — is unverified and must be described as such rather than claimed.

## README

Order, per the owner: **how it works → install → the interesting parts.** It is
a product page that argues its case with facts, not an install manual.

1. What it is and how it works. Existing mermaid diagrams retained.
2. Install: prerequisites as a table with consequences, one line per OS, the
   paste-to-your-agent line, and a pointer to `brain connect`. This section is
   short *and* complete — which is only possible because setup became one
   command. Shortness is not achieved by omitting steps.
3. The rest: how it stays true, `stats`, and "What it deliberately does not do".

"What it deliberately does not do" stays prominent. It is the no-sugarcoating
the owner asked for, and the most credible section on the page.

Deep material — schedules, vault, second machine, serving, updates — stays in
SETUP.md, which is restructured to match the four-command shape.

## Implementation sequence

This spec is larger than one sitting. It is deliberately ordered so each stage
ships something usable and nothing later invalidates something earlier.

1. **Platform backends + `.gitattributes` + `brain.cmd` + CI matrix.** First,
   because everything after it needs Windows and Linux to already be real. It
   changes no user-visible behavior on macOS, so it is the safest thing to land
   first and the easiest to verify against the existing 213 tests.
2. **`brain setup`** — phases, picker, `--json`, and the remote-failure fix.
   The largest single piece and the one the owner's failed run was about.
3. **`install.sh` shrink + `install.ps1`.** Trivial once (2) exists; both are
   bootstraps that hand off.
4. **`brain connect --apply`** + routing markers + client detection.
5. **`brain retire`.**
6. **README and SETUP.md restructure.** Last, so it documents what was actually
   built rather than what was planned.
7. **`brain serve`.** Genuinely separable — nothing above depends on it, and it
   carries its own security review. It can slip without blocking anything else.

Stages 1–3 are the minimum that fixes the failed install. Stages 4–6 are the
"easy and clear" work. Stage 7 is the owner's remote-access requirement.

## Out of scope

- Anything about a specific tunnel provider. The user provisions their own
  server and tunnel.
- A read-only serving mode. Noted as future work; `brain_capture` is exposed
  over HTTP and that is documented plainly instead.
- The full arrow-key directory browser. Revisit only if the shortlist proves
  insufficient in practice.
- Semantic search, and every other item already listed under "What it
  deliberately does not do".

## Open question, to resolve during implementation

Whether claude.ai custom connectors accept bearer-token auth or require full
OAuth 2.1. This determines whether Desktop and web clients can consume
`brain serve` directly or need a bridging proxy. To be verified against current
primary documentation before the `serve` client-registration docs are written —
not guessed at, and marked UNVERIFIED in output if it cannot be established,
consistent with how `CONNECT_CLIENTS` already handles doc-silent behavior.
