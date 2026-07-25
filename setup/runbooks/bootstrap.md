# New machine bootstrap

1. `git clone https://github.com/<you>/my-brain.git ~/brain` — YOUR private
   brain repo, not the public template. (Starting from scratch instead? Follow
   `SETUP.md` Part 1 first; this runbook is for a machine joining an existing
   brain.)
2. `cd ~/brain && bin/brain init` — wires this machine: git hooks, `.mcp.json`,
   the rendered `/brain` skill, its symlink into `~/.claude/skills/`, and the
   user-scope MCP registration. Required, not optional: `.mcp.json` and the
   rendered `SKILL.md` hold absolute paths, so they are gitignored and do NOT
   arrive with the clone. Without this step Claude has no brain tools.
3. `bin/brain schedule install --with-consolidate` — nightly doctor + the
   weekly consolidation pass. Not optional in spirit: capture is deliberately
   generous, and consolidation is the only thing that drains `inbox/` into real
   notes or deletes it.
4. `bin/brain doctor` — confirms hooks, schedules, backup state and lint.
5. Install the optional tools doctor mentions:
   `brew install ripgrep gitleaks age`
6. Vault access: restore the key from Keychain — see `runbooks/vault.md`.

That's the whole bootstrap: one clone, two commands, one key.
(Full walkthrough with verification at each step: `SETUP.md`.)
