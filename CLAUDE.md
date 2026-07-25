# brain — Claude Code bridge

The operating manual is `AGENTS.md`, imported below. It is vendor-neutral and it
is the authority: read it as if it were written here, because it is. Everything
after the import is bridge — the handful of facts that are true of Claude Code
and of nothing else. If the two ever disagree, AGENTS.md wins.

@AGENTS.md

## Claude-specific wiring

None of this changes the protocol above. It is where Claude Code finds the same
tools every other agent gets.

- **MCP tools.** `.mcp.json` at the repo root registers `bin/brain-mcp` for
  sessions started *inside* this repo; `bin/brain init` also runs
  `claude mcp add --scope user`, which is what makes brain_search / brain_read /
  brain_links / brain_recent / brain_capture reachable from any directory. Both
  are machine-local and gitignored — they are re-created by `init` on each new
  machine, never committed.
- **The `/brain` skill.** Rendered from `setup/skills/brain/SKILL.md.template`
  into `setup/skills/brain/SKILL.md` and symlinked to `~/.claude/skills/brain`
  by `bin/brain init`. The rendered copy is machine-local (it holds this
  clone's absolute path); the template is what is tracked.
- **Restart after `init`.** Claude Code reads MCP servers and skills at session
  start. A session that was already open when `init` ran will not see either.
- **Never run `bin/brain init` from a scratch or template copy.** It re-points
  the global `~/.claude/skills/brain` symlink at whatever checkout it was run
  from, silently hijacking the skill for every session on this machine. Repair
  by re-running `init` from the real brain.
- **Consolidation** runs through whichever runner `setup/consolidator.conf`
  names — the Claude CLI by default, on a pinned model. See "Consolidation" in
  AGENTS.md; the runner is config, not code.
- **Session mining** (`bin/brain sessions`, and the digest consolidation reads)
  parses Claude Code's own transcripts under `~/.claude/projects`. It exists
  only for Claude Code today; every other agent simply gets no digest, which
  consolidation already handles as a normal, non-fatal case.

## Global routing

The instruction that makes a Claude session reach for the brain *unprompted*
lives outside this repo, in `~/.claude/CLAUDE.md`. It is deliberately not
tracked here: it is machine state, and it must survive this repo being deleted
and re-cloned. `SETUP.md` Part 3 has the exact block, and the per-client
equivalents for every other agent.
