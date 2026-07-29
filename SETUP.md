# SETUP — the complete guide, in one file

Everything needed to go from nothing to a fully working second brain wired into
whichever AI agents you use.

It is four commands, and each one ends in a working state — so stopping after
any of them is a legitimate place to stop, not an abandoned install.

```
brain setup      you have a brain. Notes work. It is backed up.
brain connect    your agents can reach it.
brain serve      it is reachable from other devices.       (opt-in, Part 8)
brain retire     all of the above, gracefully undone.
```

Parts 1 and 2 are the whole install, about five minutes. Everything after them
is optional power: schedules, the encrypted vault, a second machine.

---

## Part 0 — What you are setting up

| Piece | What it gives you |
|---|---|
| The repo (`~/brain`) | Your knowledge as markdown files in git — the only source of truth |
| Git hooks | Every commit is validated (schema + secrets) and auto-pushed to your private backup |
| `bin/brain` | The toolbelt: create/search/supersede notes, lint, health checks |
| `bin/brain-mcp` | A local MCP server — how any agent, *anywhere*, reads and writes your brain |
| Agent wiring | MCP registration + a global routing rule, so your agent uses the brain without being told (and, for Claude Code, a `/brain` skill) |
| Schedules | Nightly health report; optional weekly AI tidy-up |
| Vault | Optional encrypted storage for sensitive notes |

**The tool layer is vendor-neutral.** `bin/brain-mcp` is a zero-dependency
stdio MCP server with no vendor SDK, so any MCP-capable agent consumes it
unmodified: Claude Code, OpenAI Codex CLI, Gemini CLI, Cursor, VS Code +
Copilot, Windsurf/Devin, Claude Desktop.

### Platforms

macOS, Linux and Windows. The three OS-dependent pieces each have a backend:

| | macOS | Linux | Windows |
|---|---|---|---|
| Schedules | `launchd` | `systemd --user` | `schtasks` |
| Vault key storage | Keychain | file, mode 0600 | Credential Manager |
| The `/brain` skill link | symlink | symlink | directory junction |

**Windows is verified by CI only.** The full test suite runs on all three
platforms on every push, but nobody on this project owns a Windows machine, so
anything CI cannot reach there — how the path picker feels in a real terminal,
Credential Manager prompts — is unverified rather than known to work.

### Prerequisites

Nothing is ever installed for you, on any platform. `brain setup` prints the
exact command for your OS and package manager, and what you lose by skipping it.

| | | Without it |
|---|---|---|
| `git` | **required** | Nothing works — the brain *is* a git repository |
| `python3` ≥ 3.9 | **required** | The toolbelt and the MCP server are Python |
| `gh` | optional | No automatic private backup; you create the GitHub repo yourself |
| `gitleaks` | optional | The secret gate falls back to the built-in scanner alone |
| `age` | optional | No encrypted vault for sensitive notes |
| `rg` | optional | Search still works; the plain-grep tier is slower |

If you use `gh`, run `gh auth login` once before Part 1 — that is what lets
setup create your private repo for you. Every `gh` step has a no-`gh`
alternative.

### Before you start: your brain is a git repository

That is not an implementation detail you can ignore — it is the whole reason
your notes are permanent, versioned, and yours. Two consequences to accept:

- **Every commit is pushed automatically.** A git hook pushes to your remote in
  the background after each commit. That is the backup, and it is what makes a
  lost laptop a non-event.
- **So the remote holds everything you ever record.** Which means it **must be
  private**. A brain in a public repo is every private thought you ever wrote
  down, world-readable, in a history that outlives deleting it.

GitHub is the default because it is where most people already are, but nothing
here requires it — any git remote works. You can also run with **no remote at
all**: everything works except the backup. That is supported, and it is
deliberately not treated as finished — `doctor` reports it red every time, and
so does `brain setup --no-repo`, which tells you plainly that the red line is
the one you asked for.

---

## Part 1 — `brain setup`

### Option A (recommended): one command

**macOS and Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/Cazy00/brain/main/install.ps1 | iex
```

Both scripts are bootstraps and nothing more: they check git and Python, fetch
the template into a temp directory, and hand off to `brain setup`. Everything
interactive lives there, in Python, so all three platforms behave identically.

Flags go after `-s --` when piping:

```sh
curl -fsSL … | sh -s -- --dir ~/knowledge --repo my-brain --yes
```

| Flag | |
|---|---|
| `--dir <path>` | Where the brain lives; skips the picker |
| `--repo <name>` | Name for the private GitHub repo (default: `my-brain`) |
| `--no-repo` | No remote at all — LOCAL ONLY, no backup |
| `--yes`, `-y` | Never ask; take every default |
| `--json` | Machine-readable result on stdout, human text on stderr |
| `--only <phase>` | Re-run one phase: `check`, `place`, `create`, `backup`, `verify` |

### What setup actually does

Five phases, each idempotent and each re-runnable on its own with `--only`:

1. **check** — prerequisites. Missing required tools stop the run with the exact
   install command. Missing optional ones report the *consequence*, not the name.
   Nothing is ever installed.
2. **place** — where the brain lives. A shortlist (`~/brain`, `~/Documents/brain`,
   any cloud folder you actually have) plus a typed path with tab completion.
   Skipped when you pass `--dir`. With no terminal — piped into `sh`, or run by
   an agent — it takes the recommended default rather than blocking.
3. **create** — copies the template and gives it a **fresh git history that is
   yours**. The template's history is the product's, not yours. It refuses a
   destination that already has anything in it, and the refusal names what is in
   the way.
4. **backup** — your private GitHub repo, via `gh` if you have it. Success is
   determined by **inspecting git state afterwards**, never by an exit code:
   `gh repo create --push` adds the remote *before* it pushes, so a push failure
   leaves a remote sitting right there and a non-zero exit that says nothing
   about whether one exists. If a remote exists with no upstream, it pushes with
   `-u` so the run cannot leave `doctor` red.
5. **verify** — `doctor`. Setup's exit code **is** doctor's, so an install this
   calls done is one the health check agrees with.

Setup does **not** wire your agents. That is Part 2, deliberately: wiring
re-points a global link at whatever checkout ran it, and a first install runs
from a throwaway clone.

### Option B: GitHub template button

On <https://github.com/Cazy00/brain> click **Use this template → Create a new
repository**, name it (e.g. `my-brain`), and — important — set visibility to
**Private**. Then:

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain && bin/brain setup --only verify --dir .
```

### Option C: plain clone, remote later

```sh
git clone https://github.com/Cazy00/brain.git ~/brain
cd ~/brain
git remote remove origin      # drop the product remote FIRST — it must not stay 'origin'
rm -rf .git && git init -b main && git add -A && git commit -m "brain: start"
gh repo create my-brain --private --source . --push   # creates YOUR private repo as origin
```

No `gh`? Create a **private** repository in the GitHub web UI, then:

```sh
git remote add origin https://github.com/<you>/my-brain.git
git push -u origin main
```

**Verify it is private before you write anything real into it** (or just look at
the repo page — it must say Private):

```sh
gh repo view --json visibility -q .visibility    # must print: PRIVATE
```

Got it wrong? Fix it immediately, and treat anything already pushed as seen:

```sh
gh repo edit <you>/my-brain --visibility private --accept-visibility-change-consequences
```

---

## Part 2 — `brain connect`

Two things have to be true for an agent to use your brain: it must be able to
*reach* it (the MCP server), and it must know to *reach for* it (the routing
rule). `connect` does both.

Start by asking what this machine looks like:

```sh
bin/brain connect
```

One line per client, and three answers that matter:

| | |
|---|---|
| `not installed` | The client is not on this machine. Nothing to do. |
| `not wired` | Installed, no brain registered. |
| `wired to this brain` | Done — and it says whether the routing block is in place. |
| **`WIRED TO A DIFFERENT brain`** | An agent is talking to a path that is not this one. Every tool call still succeeds, against someone else's notes or against nothing. This is the failure that used to be invisible. |

Then wire it:

```sh
bin/brain connect --all --apply           # register the server with every client found here
bin/brain connect --routing --apply       # add the rule that makes agents reach for it
```

Restart your agents afterwards — MCP servers and global instructions are read at
session start.

### What `--apply` will and will not do

It edits config files this system did not create, so it has rules:

- **Merge, never overwrite.** Your other MCP servers and unrelated settings
  survive untouched. (The official Claude Desktop instruction is to "replace the
  contents" of its config file, which destroys them.)
- **Back up first**, to `<file>.brain-backup-<timestamp>`, named in the output.
- **Refuse anything it cannot recognise** — a config with comments in it, a
  shape it does not know — and print the snippet for you to paste instead.
  Refusing is always available and always safe.
- **Do nothing when already correct.** Re-running is the repair when wiring
  drifts, so it costs nothing.
- **`--all` only writes to clients that are actually installed.** Creating
  `~/.codex/config.toml` on a machine with no Codex is litter. Naming a client
  explicitly is different — that is you saying you want it.

See exactly what would change, first:

```sh
bin/brain connect cursor --apply --dry-run
```

### Doing it by hand

`--apply` is opt-in. Everything it does, you can do yourself — that is what this
command did before `--apply` existed, and it still works:

```sh
bin/brain connect              # what is here, and what it is wired to
bin/brain connect codex        # the exact snippet + the gotchas, for one client
bin/brain connect --all        # every client at once
bin/brain connect --routing    # the routing block itself
```

Supported: `claude-code`, `codex` (OpenAI Codex CLI), `gemini` (Gemini CLI),
`cursor`, `vscode` (+ Copilot), `windsurf` (Windsurf/Devin), `claude-desktop`.
The server is identical for all of them — stdio, command `<repo>/bin/brain-mcp`,
args `[]`. Only the config spelling differs, and it differs in ways that fail
*silently*: three different top-level keys, two file formats, and one client
with no global instruction file at all.

Where each client reads its global routing rule:

| Agent | Global instruction file |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` |
| Gemini CLI | `~/.gemini/GEMINI.md` |
| VS Code + Copilot | `~/.copilot/instructions/brain.instructions.md` (frontmatter `applyTo: "**"`) |
| Windsurf / Devin | `~/.codeium/windsurf/memories/global_rules.md` |
| Cursor | No file — Settings → Customize → Rules → User Rules (paste it there) |
| Claude Desktop | No file — it reads none; rely on the server's tool descriptions |

The last two are why `--routing --apply` reports "skipped" for some clients: no
file exists to write.

### Why the routing block matters

Without it the tools exist but nothing routes questions to them. **Both halves
of the block are load-bearing**: the first paragraph makes the agent RETRIEVE,
the second makes it OFFER TO CAPTURE. Ship only the first and you get a brain
that answers but never grows — every session's reasoning lost at the moment it
was worth keeping.

The block is written between markers:

```
<!-- brain:routing:start -->  …  <!-- brain:routing:end -->
```

which is what lets `connect --routing --apply` update it in place as this system
changes, and lets `brain retire` take it back out again. If you paste it by
hand, keep the markers.

One line inside it is worth understanding: *retrieve through the brain's tools,
never through the agent's own file index.* Several agents ship a semantic
codebase search that does not honour this repo's ignore rules — it will return
`archive/` (superseded — wrong) and `inbox/` (unconsolidated) as if current, and
it strips the trust signals (`[provisional — unconsolidated]`, the ARCHIVED
banner) that are the only thing marking a stale note as stale. Ignore files
narrow that blast radius:

```sh
bin/brain connect --write-ignores   # .cursorignore / .geminiignore / .devinignore
```

They are a backstop, not a guarantee. The rule in the routing block is what
actually holds.

### Claude Code specifics

`brain connect claude-code --apply` runs the repo's own wiring: git hooks,
`.mcp.json` for sessions started *inside* the repo, the rendered `/brain` skill,
a link into `~/.claude/skills/`, and `claude mcp add --scope user` so sessions in
*any* directory get the brain tools. The same routine is available directly as
`bin/brain init`, which is the documented repair when a machine's wiring drifts.

`.mcp.json` and `setup/skills/brain/SKILL.md` hold this machine's absolute paths
and are gitignored. Every machine regenerates its own; do not commit them.

**Never run `bin/brain init` from a scratch or template copy** — it re-points the
global `~/.claude/skills/brain` link at whatever checkout it ran in, hijacking
the `/brain` skill for every session on the machine. Repair by re-running it from
the real brain.

Verify:

```sh
claude mcp list          # → brain: .../bin/brain-mcp - ✔ Connected
bin/brain connect        # → claude-code … wired to this brain, routing block in place
```

### Cowork

Connect `~/brain` as a folder. Local Cowork sessions can then read/write your
notes directly (guided by the repo's `AGENTS.md`). Local sessions only —
remote/cloud sessions cannot reach local folders or local MCP servers. The
commit gate validates whatever any agent writes, and the nightly doctor flags
anything left uncommitted.

### claude.ai web and mobile

Not supported. They cannot run a local process, and the remote transport
(`brain serve`, Part 8) authenticates with a bearer token, which claude.ai's
per-user custom connector flow does not accept — see that part for the detail
and the date it was checked.

---

## Part 3 — Schedules (recommended)

```sh
bin/brain schedule install --with-consolidate   # nightly doctor + weekly tidy
bin/brain schedule install                      # doctor only, no consolidation
```

The scheduler is per-platform (`launchd` / `systemd --user` / `schtasks`); the
command is the same everywhere. The nightly doctor writes
`.cache/doctor-report.txt` and notifies only when something is red.

Install the consolidation job too — it is not decoration. The capture policy in
`AGENTS.md` deliberately captures generously into `inbox/`, and consolidation
is the only thing that turns those into real notes or deletes them. Without it
the inbox grows forever and nothing is ever promoted. It runs the pinned
consolidator (`setup/consolidator.conf` — Claude Code by default) headless
weekly, mines the past week's sessions for things you meant to record, and
always lands on a `consolidate/` branch for you to review and merge — never on
main. `bin/brain doctor` flags it when it is missing.

Any agent can search, read and capture; exactly one runs consolidation. That
is deliberate, not a limitation: retrieval is deterministic code and identical
on every model, so the only model-dependent judgement in the whole system is
what gets promoted to permanent knowledge — and that is pinned to one named
runner rather than left to whatever happens to be open. To change it, edit
`setup/consolidator.conf`; `bin/brain consolidate` refuses a config that does
not keep the auditor read-only (see below). Session mining currently reads
Claude Code transcripts only — other runners simply get no digest and still do
the inbox drain, which is the bulk of the value.

The pass runs as two agents, not one. The first drains the inbox and writes
notes. A second then audits the staged diff before anything is committed or
pushed — it runs with no write permission, and the session digest is physically
moved out of the repo while it works, so it judges the artifact rather than
re-running the reasoning that produced it. It checks two things only: whether
any content exposes a named person's private life, and whether a new note
contradicts one that is already current. It is explicitly barred from judging
whether something was *worth* keeping — that needs the conversation it cannot
see. No verdict, a crash, or an unreadable diff all count as a block: the work
stays on the branch, and `bin/brain doctor` reports unreviewed branches. The
findings land in `.cache/audit-report.md`.

Running it by hand instead is fine: `bin/brain consolidate`. Doing neither is
the one option that quietly breaks the system.

### Backup alarm (re-enabling the CI pulse)

If you installed from the public template, its CI (`.github/workflows/gate.yml`)
runs lint + tests on every push, but the *daily backup alarm* was removed — on
a repo nobody commits to it would just email a red build every day. Now that
this is your live brain, that alarm is worth having: it emails you if no commit
reaches GitHub in three days, the off-machine signal that auto-push has silently
stopped. Add it back by pasting these two blocks into `gate.yml` — the
`schedule:` trigger under `on:`, and the `pulse:` job under `jobs:`:

```yaml
on:
  push:
    branches: ["**"]
  schedule:
    - cron: "0 9 * * *"      # add this
```

```yaml
  pulse:                     # add this whole job
    if: github.event_name == 'schedule'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: fail if no push in 3 days
        run: |
          last=$(git log -1 --format=%ct)
          age=$(( $(date +%s) - last ))
          if [ "$age" -gt 259200 ]; then
            echo "::error::No push in over 3 days — backup may have stopped."
            exit 1
          fi
```

The nightly local `doctor` is the primary alarm regardless; this is the
belt-and-braces that still fires when the laptop is off. (GitHub also pauses
scheduled workflows after ~60 days of no repo activity.)

---

## Part 4 — First run: prove the whole loop

```sh
bin/brain capture "The brain is alive as of $(date +%F)" --commit
bin/brain search "brain alive" --scope all
bin/brain doctor
```

Expected: capture prints the file path and "captured and committed — push
runs in background". Search (the `--scope all` matters — fresh captures live
in the inbox, which default search deliberately excludes until consolidation
promotes them) returns your capture tagged `[provisional — unconsolidated]`.

Doctor is the one to read carefully. Three kinds of line:

| Line | Means |
|---|---|
| `[ok ]` | Healthy. |
| `[-- ]` | Informational, and normal on a fresh install — an optional tool you have not installed, an index not built until the first search, consolidation not scheduled yet, the workspace not trusted yet. Nothing to do. |
| `[RED]` | Actually wrong. Doctor exits non-zero and the line names the fix. |

On a brand-new install you should see **no `[RED]` lines** once you have a
private remote and have pushed once. The ones you might legitimately hit first
time:

- `no git remote — knowledge is not backed up` — you skipped the remote. Part 1.
- `no upstream tracking` — you have a remote but never pushed: `git push -u origin main`.
- **`YOUR BRAIN IS PUBLIC`** — stop and fix this before writing anything real.
  The line gives you the exact `gh repo edit … --visibility private` command.
  Doctor checks this on every run, via `gh` if you have it and an anonymous API
  probe if you do not, because it is the only mistake here with no undo.

Then start a NEW session in your agent (any of the clients you wired) and ask:
*"what's in my brain from today?"* — it should call `brain_recent`/`brain_search`
on its own. Do this from your own client the first time: it is the one check the
test suite cannot do for you.

---

## Part 5 — Vault for sensitive notes (optional)

For notes too sensitive for plaintext on GitHub (health, money, IDs). The key
storage differs per platform — Keychain on macOS, Credential Manager on Windows,
a file at mode 0600 on Linux — and `bin/brain` uses whichever is present.

macOS:

```sh
brew install age
mkdir -p ~/.config/brain
age-keygen -o ~/.config/brain/vault-key.txt
security add-generic-password -a "$USER" -s brain-vault-key \
  -w "$(cat ~/.config/brain/vault-key.txt)" -U
age-keygen -y ~/.config/brain/vault-key.txt > setup/vault-recipient.txt
git add setup/vault-recipient.txt && git commit -m "vault: add public recipient"
```

Linux: the same, minus the `security` line — the key file at
`~/.config/brain/vault-key.txt` with mode 0600 *is* the store. Install `age` with
your package manager.

Also copy the private key into your password manager — **the key IS the
vault**; lose both copies and encrypted notes are gone forever. Encrypting
and reading: see `setup/runbooks/vault.md`. Lint enforces the boundary: no
plaintext in `vault/`, no `sensitivity: private` outside it, everywhere.

## Part 6 — Second machine

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain
bin/brain connect --all --apply
bin/brain connect --routing --apply
bin/brain schedule install --with-consolidate
```

No `setup` — the brain already exists; this machine only needs wiring. Vault
access, if used (macOS) — write to a temp file FIRST, then move it into place.
`... -w > key.txt` truncates `key.txt` to zero bytes before `security` runs, so
if the keychain item is missing (the default on a new machine) the redirect
destroys the very key you were restoring:

```sh
mkdir -p ~/.config/brain
security find-generic-password -a "$USER" -s brain-vault-key -w > /tmp/vault-key.$$ \
  && mv /tmp/vault-key.$$ ~/.config/brain/vault-key.txt \
  || { rm -f /tmp/vault-key.$$; echo "no vault key in this machine's Keychain"; }
```

## Part 7 — Keeping the toolbelt up to date

There is no automatic upgrade path, on purpose: your brain's `origin` is *your*
private repo, not the template it came from, so a fix to `bin/brain` in the
template does not flow to you. This is the same single-source-of-truth
discipline the rest of the system runs on — your notes are never entangled with
someone else's remote — but it means toolbelt updates are a manual pull.

When you want the latest tooling (never the notes — there are none in the
template):

```sh
git remote add template https://github.com/Cazy00/brain.git
git fetch template
# review what changed in the machinery only:
git diff main template/main -- bin/ setup/ tests/ .githooks/ .github/
# bring across what you want — e.g. the whole toolbelt:
git checkout template/main -- bin/ setup/ tests/ .githooks/ .github/
bin/brain lint && python3 -m unittest discover -s tests   # prove it still holds
git commit -am "toolbelt: pull upstream fixes"
```

Never `merge` the template into your brain — that would drag its empty skeleton
and starter vocabulary over your real notes. Cherry-pick paths, as above.

---

## Part 8 — `brain serve` (optional, and most people never need it)

Everything above runs on one machine. `serve` is for when you want the brain
from a second one — a phone, a laptop that is not this one, a machine you are
sitting at somewhere else. If that is not a thing you want, skip this part
entirely; nothing else in the system depends on it.

```sh
bin/brain serve --new-token     # mint a token, store it, print it ONCE
bin/brain serve                 # listen on 127.0.0.1:8787
```

It serves the **same tools** as the local stdio server, over HTTP, behind a
bearer token. Same tool layer, one code path — a remote brain that answered
differently from the local one would be worse than no remote brain.

### Read this before you expose it

- **`brain_capture` is reachable over this transport, and it writes.** Whoever
  holds the token can add notes to your brain, which are committed and pushed
  automatically. That is the whole tool surface, not a subset. There is no
  read-only mode yet.
- **It refuses to start without a token.** A refusal, not a warning. It will
  not mint one silently: a credential nobody saw is a credential nobody knows
  to protect.
- **The token lives in this machine's keystore** — Keychain, Credential
  Manager, the secret service, or a 0600 file — never in the repo, never in a
  config file, never in a URL.
- **The default bind is loopback.** `--bind 0.0.0.0` works and prints exactly
  what it is exposing before it serves.
- **Browser origins are refused.** Any request carrying an `Origin` header is
  rejected, because no legitimate client of this server is a browser and a web
  page can otherwise make your browser talk to `127.0.0.1`.
- **There is no TLS here, on purpose.** See the tunnel contract below.
- **There is no rate limiting.** Worth knowing before a public bind.

### Connecting a client

Claude Code, verified against its current documentation on 2026-07-29:

```sh
claude mcp add --transport http brain http://127.0.0.1:8787/mcp \
  --header "Authorization: Bearer $BRAIN_TOKEN"
```

Any client that lets you set a request header works the same way. Clients that
do not — including **claude.ai on the web, Claude Desktop and mobile** — cannot
use this. Checked against Anthropic's connector documentation on 2026-07-29:
adding a custom connector by URL offers OAuth Client ID and Client Secret only;
a fixed bearer token is supported through `static_headers`, which is in beta and
is entered by an **organization administrator**, not by an individual. Closing
that gap means implementing OAuth 2.1 with dynamic client registration, which is
a much larger piece and is not built.

### The tunnel contract

Reaching the server from outside your machine needs something in front of it.
Which tunnel is your choice and this project takes no position on it. What it
must do:

1. **Terminate TLS.** The brain speaks plain HTTP; anything crossing a network
   must be wrapped by the thing in front.
2. **Forward to this port**, path and all — the endpoint is `/mcp`.
3. **Preserve the `Authorization` header.** A tunnel that strips or rewrites it
   makes every request a 401.
4. **Add no `Origin` header.** The server refuses requests that carry one.

And one thing it must not be relied upon for: **authentication.** A tunnel that
publishes an unauthenticated origin to a public hostname is one mis-scoped
route, one bypass policy, or one other path to the origin away from exposing
the whole brain. The check inside the server travels with the server; a policy
in front of it does not.

## Part 9 — `brain retire`

To unwire a single machine while keeping the brain:

```sh
bin/brain schedule uninstall
claude mcp remove --scope user brain           # if you wired Claude Code
rm ~/.claude/skills/brain                       # removes the symlink only
# and remove the routing block between its markers from each instruction file.
# Your notes remain: they're just files in git.
```

To retire a brain entirely and start clean — the protected fresh start:

```sh
bin/brain retire --dry-run    # every safety check, the whole plan, changes nothing
bin/brain retire              # the real thing
```

(`bin/brain reset` still works and says where it moved to.)

`--dry-run` is the safe way to explore it, and it runs the checks for real
rather than describing them. The command itself is interactive and refuses
without a terminal, so no agent can trigger it. It will not run until every
commit on every branch is confirmed pushed (it fetches first), writes and
verifies a `git bundle` of all history outside the repo, and makes you type a
phrase computed from your live note count and remote — so a phrase copied from
a chat log will not match.

It then de-wires this machine — schedules, the skill link, the MCP registration,
and the routing block from every instruction file that has one between markers —
and **moves** `~/brain` aside to `~/brain.retired-<timestamp>`. It deletes
nothing, and it never touches the remote. A routing block written by hand,
without markers, is named and left alone rather than guessed at.

`bin/brain retire --explain` lists everything it preserves, in full. Reinstall
fresh with Part 1; delete the retired copy by hand once the new brain passes
`doctor`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Brain tools don't appear in a session | Sessions load MCP at start — open a new session. `bin/brain connect` says what this machine is wired to; `--apply` re-wires it. |
| An agent answers from the wrong notes | `bin/brain connect` — look for `WIRED TO A DIFFERENT brain`. A second clone or a moved brain leaves the old path registered. |
| `--apply` refused | It printed why, and the snippet to paste instead. A config with comments in it is the usual cause: JSON with comments parses for the client and not for us, and rewriting it would delete them. |
| Desktop chat doesn't show the tools | Config path/JSON typo, or app not restarted (Cmd-Q, not just closing the window). The command must be an absolute path. |
| `commit blocked` with lint errors | That's the system working. Read the errors — each says exactly what to fix. `bin/brain lint` re-checks. |
| `WARNING: the content gate is DOWN` | Lint itself crashed (not your content). Run `python3 bin/brain lint` to see why; commits still work meanwhile. |
| Push rejected: `workflow scope` | Push once from a terminal (`git push`), or `gh auth refresh -s workflow`. |
| Doctor: `no upstream tracking` | `git push -u origin main` once. |
| Doctor: `not pushed — backup is behind; run: git push` | You're offline or the remote rejects; `git push` when back online. |
| `claude: command not found` | Install Claude Code, then `bin/brain connect claude-code --apply`. |
| Consolidation does nothing on schedule | The `claude` CLI must be logged in for headless runs; run `bin/brain consolidate` manually once to check. |
| `brain serve` refuses to start | It has no token. `bin/brain serve --new-token` mints one and prints it once. |
| Every request to `brain serve` is a 401 | The header is missing, rewritten, or the token is stale. A tunnel that does not preserve `Authorization` looks exactly like a wrong token. Mint a new one and re-register the client if you are unsure which. |
| Every request to `brain serve` is a 403 | Something is adding an `Origin` header — a browser, or a proxy that inserts one. This server refuses browser origins by design; `--allow-origin` takes one explicitly. |
