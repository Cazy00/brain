# Windows CI is informational until its failures are fixed

Status: decided 2026-10-14 (executor ruling during Phase 0, owner informed).
**Superseded the same day:** the owner does not need Windows, so the Windows leg was
removed from CI rather than kept informational. The failure list below is where to
start if Windows support is wanted again.

On `main` at 29bffcf the Windows job ran 1107 tests: 55 failures, 26 errors,
9 skipped. Release 0.2.5 fixed the connect-snippet class (`ConnectTests`); the
rest are platform assumptions in the tests and the toolbelt, not regressions:

- paths asserted with `/` (`knowledge/inbox/...`), and C:/D: cross-drive `relpath`
- Windows file locks on `.git/objects` during cleanup (`PermissionError`)
- bash-only scripts (`RunbookFirewallScriptTests`, SBOM)
- `os.statvfs` absent on Windows (doctor's disk check)
- the deep-JSON test hitting `RecursionError` at Windows' stack size
- the Codex TOML read-back not unescaping a quoted Windows path, so
  `connect --check` / `doctor` report a correct registration as different

Decision: the Windows leg of `tests` runs with `continue-on-error`, so it still
reports, and the README says Windows is not verified. A red Windows job that
blocks every merge would be switched off or ignored; an informational one keeps
the evidence visible. It goes back to blocking when these are fixed.
