# SETUP — the complete guide, in one file

Everything needed to go from nothing to a fully working second brain wired
into whichever AI agents you use. Follow top to bottom; each part ends with a
way to verify it worked. The fast path is Parts 1-3 + Part 8 (about 5 minutes);
everything else is optional power.

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
Copilot, Windsurf/Devin, Claude Desktop. Part 2 wires Claude Code in one
command; Part 2b prints the exact registration for every other client.
(Developed and smoke-tested end-to-end on Claude Code; the wire protocol the
others use is covered by the test suite, but do the Part 8 check from your own
client the first time.)

Requirements: **macOS** with Python 3.9+ and git, plus at least one MCP-capable
agent. Optional but recommended: `brew install gh ripgrep gitleaks age` (`gh`
is the GitHub CLI used in Parts 1 and 8 — run `gh auth login` once after
installing; every gh step also has a no-gh alternative).

**Linux:** the core works — markdown, git, the toolbelt and the MCP server are
all plain Python and portable. Two things are macOS-only and DIY on Linux: the
schedules (Part 7 uses `launchd`; use `systemd --user` timers or cron) and the
vault key store (Part 9 uses Keychain; use `pass`, `gnome-keyring`, or a
file-with-strict-permissions). Everything else is identical.

---

## Part 1 — Get the code

### First, the thing to understand about git and GitHub

Your brain is **a git repository**. That is not an implementation detail you can
ignore — it is the whole reason your notes are permanent, versioned, and yours.
Two consequences you need to accept before you start:

- **Every commit is pushed automatically.** A git hook pushes to your remote in
  the background after each commit. That is the backup, and it is what makes a
  lost laptop a non-event.
- **So the remote holds everything you ever record.** Which means it **must be
  private**. A brain in a public repo is every private thought you ever wrote
  down, world-readable, in a history that outlives deleting it.

GitHub is the default because it is where most people already are, but nothing
here requires it — any git remote works (GitLab, a self-hosted server, even a
drive you control). You can also run with **no remote at all**: everything works
except the backup, and `bin/brain doctor` will tell you, every time, that your
knowledge exists on exactly one machine.

`bin/brain doctor` actively checks this. If your remote is publicly readable it
fails loudly with the command to fix it — see Part 8.

### Option A (recommended): one command

```sh
curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh
```

It checks your prerequisites, installs into `~/brain` (ask for somewhere else
with `--dir`), gives the clone a **fresh git history that is yours**, offers to
create your **private** GitHub repo, wires this machine, and finishes by running
`doctor`. It refuses to install over a directory that already has anything in
it, and it never touches a file you did not name.

Not comfortable piping a script into a shell? Read it first — it is one file:
<https://github.com/Cazy00/brain/blob/main/install.sh>. Or use Option B.

### Option B: GitHub template button

On <https://github.com/Cazy00/brain> click **Use this template → Create a new
repository**, name it (e.g. `my-brain`), and — important — set visibility to
**Private**. Then:

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain
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

## Part 2 — One command wires the machine

```sh
bin/brain init
```

This does five things (each idempotent — safe to re-run):

1. Installs the git hooks (`core.hooksPath = .githooks`) — actually, *every*
   `bin/brain` command self-installs these, so hooks can never silently be missing.
2. Writes `.mcp.json` with this clone's absolute path — Claude Code sessions
   *inside* the repo get the brain tools.
3. Renders `setup/skills/brain/SKILL.md` for this clone's path.
4. Symlinks the `/brain` skill into `~/.claude/skills/`.
5. Registers the MCP server at **user scope** (`claude mcp add --scope user
   brain <repo>/bin/brain-mcp`) — Claude Code sessions in *any* directory get
   the brain tools. If the `claude` CLI isn't installed, it prints the exact
   command to run later.

The two files init generates (`.mcp.json` and `setup/skills/brain/SKILL.md`)
contain this machine's absolute paths and are gitignored — every machine
regenerates its own via `bin/brain init`; don't commit them.

**Manual equivalents** (only if you want to do it by hand or `init` failed —
all five steps, in order):

```sh
git config core.hooksPath .githooks
printf '{\n  "mcpServers": {\n    "brain": {"command": "%s/bin/brain-mcp", "args": []}\n  }\n}\n' "$(pwd)" > .mcp.json
sed "s|{{REPO}}|$(pwd)|g" setup/skills/brain/SKILL.md.template > setup/skills/brain/SKILL.md
ln -sfn "$(pwd)/setup/skills/brain" ~/.claude/skills/brain
claude mcp add --scope user brain "$(pwd)/bin/brain-mcp"
```

Verify:

```sh
claude mcp list          # → brain: .../bin/brain-mcp - ✔ Connected
bin/brain doctor         # → [ok ] git hooks installed
```

---

## Part 2b — Any other agent (optional)

Skip if you only use Claude Code. Otherwise, one command prints the exact
registration for any supported client, plus where that client reads a global
routing rule and what silently breaks:

```sh
bin/brain connect              # list the clients it knows
bin/brain connect codex        # exact snippet + gotchas for one of them
bin/brain connect --all        # every client at once
```

Supported: `codex` (OpenAI Codex CLI), `gemini` (Gemini CLI), `cursor`,
`vscode` (+ Copilot), `windsurf` (Windsurf/Devin), `claude-desktop`, and
`claude-code` (which Part 2 already did). The server is identical for all of
them — stdio, command `<repo>/bin/brain-mcp`, args `[]`. Only the config
spelling differs, and it differs in ways that fail *silently* (three different
top-level keys), which is why `connect` prints the exact one per client instead
of leaving you to guess.

The connect output tells you the per-client restart step and, for clients that
have one, the ignore file. Write those ignore files with:

```sh
bin/brain connect --write-ignores   # .cursorignore / .geminiignore / .devinignore
```

They keep a client's *own* index out of `archive/`, `inbox/`, `journal/` and
`vault/`. They are a backstop, not a guarantee — see the note in Part 3.

---

## Part 3 — Tell your agent the brain exists (global routing)

This is what makes an agent *reach for* the brain unprompted. Without it the
tools exist but nothing routes questions to them. Print the exact block,
already filled in with your repo path:

```sh
bin/brain connect --routing
```

Then paste it into wherever your agent reads global instructions:

| Agent | Global instruction file |
|---|---|
| Claude Code | `~/.claude/CLAUDE.md` |
| Codex CLI | `~/.codex/AGENTS.md` |
| Gemini CLI | `~/.gemini/GEMINI.md` |
| VS Code + Copilot | `~/.copilot/instructions/brain.instructions.md` (frontmatter `applyTo: "**"`) |
| Windsurf / Devin | `~/.codeium/windsurf/memories/global_rules.md` |
| Cursor | No file — Settings → Customize → Rules → User Rules (paste it there) |
| Claude Desktop | No file — it reads none; rely on the server's tool descriptions |

(`bin/brain connect <client>` names the exact path for that client too.)

**Both halves of the block matter**: the first paragraph makes the agent
RETRIEVE, the second makes it OFFER TO CAPTURE. Ship only the first and you get
a brain that answers but never grows — every session's reasoning is lost at the
moment it was worth keeping.

One line in that block is load-bearing and worth understanding: *retrieve
through the brain's tools, never through the agent's own file index.* Several
agents ship a semantic codebase search that does not honour this repo's ignore
rules — it will return `archive/` (superseded — wrong) and `inbox/`
(unconsolidated) as if current, and it strips the trust signals (`[provisional
— unconsolidated]`, the ARCHIVED banner) that are the only thing marking a
stale note as stale. The ignore files from Part 2b narrow that blast radius;
the rule in the routing block is what actually holds.

**Restart your agent now** — MCP servers and (for Claude Code) skills load at
session start.

---

## Part 4 — Claude Desktop chat (optional)

Normal desktop chat can use the brain too. Edit
`~/Library/Application Support/Claude/claude_desktop_config.json` and **merge**
this in (don't delete existing keys; create the file if absent):

```json
{
  "mcpServers": {
    "brain": {
      "command": "/Users/<you>/brain/bin/brain-mcp",
      "args": []
    }
  }
}
```

The path must be absolute. Restart the Claude Desktop app. Chat now has
`brain_search` / `brain_read` / `brain_recent` / `brain_capture` — and capture
still commits + pushes, because the server does the git work itself.

## Part 5 — Cowork (optional)

In Cowork, connect `~/brain` as a folder. Local Cowork sessions can then
read/write your notes directly (guided by the repo's `AGENTS.md`). Notes:
local sessions only — remote/cloud sessions can't reach local folders or
local MCP servers. Don't worry about Cowork breaking things: the commit gate
validates whatever any agent writes, and the nightly doctor flags anything
left uncommitted.

## Part 6 — claude.ai web and mobile

Not supported — they can't run local processes. (Closable later by hosting
`brain-mcp` behind HTTP as a custom connector; deliberately not part of v1.)

---

## Part 7 — Schedules (recommended)

```sh
bin/brain schedule install --with-consolidate   # nightly doctor + weekly tidy
bin/brain schedule install                      # doctor only, no consolidation
```

The nightly doctor writes `.cache/doctor-report.txt` and shows a macOS
notification only when something is red.

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

## Part 8 — First run: prove the whole loop

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

## Part 9 — Vault for sensitive notes (optional)

For notes too sensitive for plaintext on GitHub (health, money, IDs):

```sh
brew install age
mkdir -p ~/.config/brain
age-keygen -o ~/.config/brain/vault-key.txt
security add-generic-password -a "$USER" -s brain-vault-key \
  -w "$(cat ~/.config/brain/vault-key.txt)" -U
age-keygen -y ~/.config/brain/vault-key.txt > setup/vault-recipient.txt
git add setup/vault-recipient.txt && git commit -m "vault: add public recipient"
```

Also copy the private key into your password manager — **the key IS the
vault**; lose both copies and encrypted notes are gone forever. Encrypting
and reading: see `setup/runbooks/vault.md`. Lint enforces the boundary: no
plaintext in `vault/`, no `sensitivity: private` outside it, everywhere.

## Part 10 — Second machine

```sh
git clone https://github.com/<you>/my-brain.git ~/brain
cd ~/brain && bin/brain init && bin/brain schedule install --with-consolidate
# vault access, if used — write to a temp file FIRST, then move it into place.
# `... -w > key.txt` truncates key.txt to zero bytes before security runs, so
# if the keychain item is missing (the default on a new machine) the redirect
# destroys the very key you were restoring.
mkdir -p ~/.config/brain
security find-generic-password -a "$USER" -s brain-vault-key -w > /tmp/vault-key.$$ \
  && mv /tmp/vault-key.$$ ~/.config/brain/vault-key.txt \
  || { rm -f /tmp/vault-key.$$; echo "no vault key in this machine's Keychain"; }
```

Add the Part 3 routing block to that machine's agent (`bin/brain connect
--routing`), and Part 2b for any non-Claude client. Done.

## Part 11 — Keeping the toolbelt up to date

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

## Troubleshooting

| Symptom | Fix |
|---|---|
| Brain tools don't appear in a session | Sessions load MCP at start — open a new session. Check `claude mcp list`; re-run `bin/brain init`. |
| Desktop chat doesn't show the tools | Config path/JSON typo, or app not restarted. Path must be absolute. |
| `commit blocked` with lint errors | That's the system working. Read the errors — each says exactly what to fix. `bin/brain lint` re-checks. |
| `WARNING: the content gate is DOWN` | Lint itself crashed (not your content). Run `python3 bin/brain lint` to see why; commits still work meanwhile. |
| Push rejected: `workflow scope` | Push once from a terminal (`git push`), or `gh auth refresh -s workflow`. |
| Doctor: `no upstream tracking` | `git push -u origin main` once. |
| Doctor: `not pushed — backup is behind; run: git push` | You're offline or the remote rejects; `git push` when back online. |
| `claude: command not found` | Install Claude Code, then `claude mcp add --scope user brain ~/brain/bin/brain-mcp`. |
| Consolidation does nothing on schedule | The `claude` CLI must be logged in for headless runs; run `bin/brain consolidate` manually once to check. |

## Uninstall / undo

To unwire a single machine while keeping the brain:

```sh
bin/brain schedule uninstall
claude mcp remove --scope user brain           # if you wired Claude Code
rm ~/.claude/skills/brain                       # removes the symlink only
# remove the routing block from your agent's global instruction file, and any
# per-client MCP registration you added in Part 2b. Your notes remain: they're
# just files in git.
```

To retire a brain entirely and start clean — the protected fresh start:

```sh
bin/brain reset
```

`reset` is interactive and refuses without a terminal, so no agent can trigger
it. It will not run until every commit on every branch is confirmed pushed
(it fetches first), writes and verifies a `git bundle` of all history outside
the repo, and makes you type a phrase computed from your live note count and
remote. It then de-wires this machine and **moves** `~/brain` aside to
`~/brain.retired-<timestamp>` — it deletes nothing, and it never touches the
remote. When it finishes it prints exactly what it preserved (the remote, the
bundle, the vault key, the routing block). Reinstall fresh with Part 1; delete
the retired copy by hand once the new brain passes `doctor`.
