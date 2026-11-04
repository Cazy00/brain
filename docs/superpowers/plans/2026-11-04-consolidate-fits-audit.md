# Plan: consolidation passes that fit the audit

B45, I-070. Engine `github.com/Cazy00/brain`, from v0.7.0 to v0.8.0. Written 2026-11-04. Plan only:
nothing here is built until the plan review has run.

## The problem, in numbers

- The home curator ran `consolidate` on 2026-11-02, 11-03 and 11-04, and none landed. On 11-04 the
  audit blocked with "diff truncated at 200,000 characters; the visible part already reaches the
  30-canonical-file cap and the rest can't be audited". `consolidate-result`: `blocked`, $6.82,
  16.1M tokens in, `inbox_before` 79. The two earlier passes (about $0.50 each) blocked on a
  contradiction that has since been corrected.
- Only the prompts state the caps. `setup/consolidate-prompt.md` says "max 40" notes and "at most
  30 canonical files". `setup/audit-prompt.md` blocks a change set of more than 30 files.
  `bin/brain` `audit_changes` cuts the diff at `AUDIT_MAX_DIFF_CHARS = 200_000` and then asks the
  model to block, so the engine pays for an audit whose answer is already known.
- The proposer went over the caps, and the whole paid pass was thrown away. Nothing in the engine
  makes the next pass any smaller, and every day of captures makes it bigger, so the passes will
  never catch up on their own.
- Sizing data. None of this is in brain-data's history or the kept logs: neither is readable by
  `agent`, so the live pass (point 4) measures it properly.
  - 2026-10-21: the 15-file cap stopped the pass after 18 notes, about **0.83 canonical files per
    note**.
  - 2026-10-23: a 40-note pass landed as brain-data PR #12, with 87 changed files (inbox
    deletions and handoffs included). The engine side cost $4.77–4.82, against $2.35 for a
    20-note pass: about **$0.12 per note**, roughly linear (brain note
    `2026-10-23-the-brain-s-curator-pass-runs-daily`).
  - 2026-11-04: the first 200K characters already held 30 canonical files, and the diff ran past
    200K with at most 40 notes in the batch. That is more than **5,000 diff characters per note**.
    This pass's captures are longer than October's, at 1–2K characters each. Every drained note
    also shows up in full as a deletion.

## 1. Mechanical caps before the audit

**Recommended:** the engine measures the staged change set before any audit. When it is over a
hard cap, the engine runs no audit, resets the branch and runs the pass **once more, with a smaller
batch sized from what it just measured**. If the second try is also over, the pass ends `blocked`
and still no audit is paid for.

- **What is measured** (`brainlib/passsize.py`, a new module with one job: choose a batch and
  measure a change set):
  - `canonical_files`: the number of distinct paths under `knowledge/{decisions,topics,projects,
    people,life,reference,archive}/` in `git diff --cached -M --name-status <trunk>`. A rename
    counts once, so a supersede counts its successor and the archived note. Inbox, handoff and
    journal paths do not count.
  - `diff_chars`: `len()` of exactly the text `audit_changes` would hand to the auditor.
- **Hard caps:** at most 30 canonical files and at most `AUDIT_MAX_DIFF_CHARS` (200,000) diff
  characters. The 3-proposal cap is already code (`proposal_problems`).
- **Where the check runs:** in `_consolidate`, after lint, the proposal, handoff and correction
  checks and before `seal_pass`. Nothing is committed before the check. The batch check below
  runs at the same point.
- **What happens when a pass is over a cap:**
  1. The engine prints `  over caps: <n> canonical files, <m> diff chars (caps 30 / 200000)`.
  2. It discards the attempt: it checks out the trunk, deletes the branch and cuts it again under
     the same name, all under `repo_lock`. The git control files are restored the same way they
     are today.
  3. It reruns the proposer with batch `N2 = max(1, min(N // 2, floor(N * min(20 / files,
     100000 / chars))))`.
  4. It allows at most **two attempts** per run. The result's cost sums both attempts.
- **The second attempt is also over:** status `blocked`, the reason line starts `over caps:`, the
  branch is left for inspection and no audit runs. This is also the result when the first batch
  was already 1.
- **A drain outside the batch:** the pass removed or handed off an inbox note that is not in the
  batch, not a correction, not named by a correction and not a session-mining proposal. That pass
  ends `blocked` (`batch check: …`) with no retry. It ignored its instructions, and a rerun would
  not change that.
- **`audit_changes` no longer truncates.** A diff over the limit returns `("error", "diff of N
  chars is over the audit limit (200000); not audited")` and starts no model. `brain
  audit-branch`, which curator-audit uses, gets the same refusal for free. The reason it is
  `error` and not `block`: the curator records an audit block only on a `block` verdict ("an
  error is no judgement of the audit"), so an over-size diff can never park the curator for a
  week.
- **The audit prompt** drops its "more than 30 modified canonical files" clause, so the engine is
  the only place that rule lives. The engine adds a NOTE giving the measured counts. The
  3-proposal clause stays as a backstop to the code check.

**Rejected:**
- *Commit nothing and stop cheaply.* The money is saved, but tomorrow's inbox produces the same
  batch and the same overshoot, so the passes never catch up. That is the failure this plan exists
  to fix.
- *Ask the same session to trim its work down to the oldest notes that fit.* The trim would undo
  merges, supersedes and handoffs, which is the riskiest edit a model can make on this tree. The
  audit would then judge a tangled diff. The session's context is at its largest by then, so it
  is the dearest turn. And this same model has already ignored the cap once.
- *Audit the diff in chunks.* A contradiction can sit across two chunks. The audit must see the
  whole change set it judges.

## 2. Batch size from the backlog

**Recommended: the engine chooses the batch and names it to the proposer path by path.** Today the
prompt says "the oldest 40".

- `passsize.select_batch(inbox)` handles every inbox note that is not a correction and not a
  session-mining proposal, oldest first by file name, which is the capture instant. It gives each
  note an estimated diff cost of `max(7_000, 4 × its size in chars)`. It takes notes while the
  running total, corrections included (they are always processed in full), stays within
  **100,000 characters (half the hard cap)**, the estimated canonical files (0.85 per note) stay
  within **20 (two thirds of 30)**, and the count stays within **15**. It always takes at least
  one note.
- Why these numbers: 5,000+ characters per note was measured on 11-04, and 7,000 rounds it up for
  the deletion, the promoted body and its Sources lines. Notes 1–2K long therefore come to 12–15
  per pass, and long captures to fewer. A fixed count alone was rejected because captures vary in
  length about five times over.
- The prompt changes from "oldest first, max 40" to the batch the engine lists. `propose_prompt`
  appends a `THIS PASS'S BATCH` block holding the paths. The prompt also says project overviews
  are promoted only from inside the batch, so an overview does not slip past the cap.
  Corrections still cover the whole inbox, because an original must never be promoted while its
  correction waits.
- The engine chooses because the limit is the engine's own (its auditor's input). The replace
  test, "to replace this, what changes?": only `passsize.py` and its two calls in `bin/brain`,
  with no curator change. The curator never learns the size. A `--batch` flag on the curator was
  rejected: it would put engine knowledge into the curator and split one rule across two
  repositories.
- The result line gains the optional keys `batch`, `attempts`, `canonical_files` and
  `diff_chars`. `status` keeps its five values, because the curator's `consolidate_result` drops
  the whole line, cost included, when a status is unknown.
- **This lowers the owner's 2026-10-22 pass size of 40.** The owner said yes to 40 "provided it
  improves the brain and avoids future problems", and it did not avoid them. Once this lands, the
  job captures a correction to `2026-10-23-the-brain-s-curator-pass-runs-daily`: up to 15 notes,
  chosen by the engine against the audit's limit.

## 3. Draining the 79-note backlog

**Recommended: run several sequential curator runs in one sitting right after the deploy, then go
back to daily.** Each run goes through the whole normal path: engine, curator-audit and the
no-tap merge. Each pass builds on the merged result of the one before.

- **Cost per pass:** about $0.12 per note × 12–15 notes, so $1.50–1.80 on the engine side. The
  curator-audit run and the post-pass contradiction check come on top. Estimate **$2–2.50 a
  pass**. A smaller context should, if anything, lower the cost per note.
- **Backlog:** the 79 include corrections and unexpired proposals. Call it 65–75 notes to drain,
  so **5–6 passes, about $11–15 in total**, and one evening of runs at about 30–45 minutes each.
  The 11-04 pass alone burned $6.82 for nothing.
- **At one pass a day it never drains.** Captures arrived at about 14 a day in October, close to a
  15-note batch. After the drain, I recommend **two timer slots a day (02:00 and 14:00 UTC)**.
  Cost scales with notes, not with passes, and an idle pass makes no model call, so a second slot
  adds capacity without adding spend. This is a one-line change to the workspace's
  `curator/deploy/harbor-curator.timer`, in its own workspace PR. Turning the timer back on is
  the owner's choice, because the owner paused it.
- **Rejected:**
  - *Several passes inside one engine run.* The curator accepts exactly one commit on
    `origin/main` per run (`one_audited_commit`), and pass k+1 must build on pass k after k has
    been audited and merged. Doing that inside one run would mean changing the curator's
    contract.
  - *One bigger pass.* That is the failure itself.
- Each extra run is a manual `systemctl start harbor-curator.service`, done by the admin as a
  `root-command` (logged, no tap). They run one after another, never in parallel: the curator
  refuses a busy run with `EXIT_BUSY`, and the memory budget is shared.
- API credits are nearly used up. The manager tells the owner that the drain costs about $11–15
  before it starts, and stops after the live pass (point 4) if that pass costs more than $4.

## 4. Tests (no model) and the one live check

New `ConsolidateTests` and `ConsolidationAuditTests` cases use the existing `stub_claude` (a
`claude` shell stub on PATH). The stub tells propose from audit by its argv, logs every call to a
file, and reads the batch from the prompt it is given.

1. `select_batch`: oldest first; corrections and proposals are excluded and counted against the
   budget; the char budget, the 15-note cap and the file estimate each stop the selection; at
   least one note is always taken; an empty inbox gives an empty batch.
2. `measure`: a rename counts once; inbox, handoff and journal paths are not counted; archive
   paths are; `diff_chars` equals the text `audit_changes` would send.
3. Propose writes 31 canonical files on the first call and 5 on the second. The test checks two
   propose calls (the second prompt lists `N2` paths), exactly one audit call, status
   `committed`, `attempts: 2`, **one** `consolidate/` branch holding **one** commit on the trunk
   (the curator's invariant), and cost summed over all three calls.
4. Propose goes over the caps both times: status `blocked`, zero audit calls, the reason starts
   `over caps:`, both attempts' cost is reported.
5. A diff over 200K with fewer than 30 files follows the same path as 3.
6. A batch of 1 that is over the caps: no retry, `blocked`.
7. A drain outside the batch: `blocked` with `batch check:`, no retry, no audit.
8. A correction whose target sits outside the batch is drained with it, and that is allowed.
9. `audit_changes` and `audit-branch` given a diff over the limit: verdict `error`, the runner is
   never started, and `audit-result` says `error`.
10. The prompt rules: `ConsolidationPromptRulesTests` drop the "oldest 40" phrases and pin the
    batch block. The audit prompt has no file-count clause.
11. The result line: the new keys are present, and `status` is one of the five.

`python3 -m unittest discover -s tests` must be green before the PR leaves draft.

**The one live check**, after the engine PR merges and v0.8.0 is installed in `/opt/brain-engine`:
the admin starts `harbor-curator.service` once, with the timer still paused. Its kept log in
`/var/log/harbor-curator` (admin-readable) must show:

- `consolidate-result` with `status: committed`, a `batch` of 12–15, `canonical_files` ≤ 30,
  `diff_chars` ≤ 200,000 and `attempts: 1`;
- an engine audit pass and a curator-audit pass;
- the brain-data PR merged.

Rule (e) of the curator's block rules, a new engine version, clears the 11-04 block record, so
that record does not skip this run. If the measured characters or files per note are more than 30%
off the estimates, a small follow-up PR adjusts the constants in `passsize.py` before the drain
runs. Then come the drain runs (point 3).

## 5. Contract and version

- **Engine 0.7.0 → 0.8.0** in `bin/brainlib/version.py`, tagged `v0.8.0`. It is a minor bump: the
  behaviour changes, the result line gains keys, and no call changes.
- **The curator (`Cazy00/workspace`, `curator/bin/curator-engine`) needs no change.**
  - It still calls `consolidate --digest-from … --digest-since …` with no new flag.
  - It still receives one branch with one commit.
  - It still gets the same five statuses, and it ignores the new keys
    (`engine.consolidate_result` reads named keys only).
  - curator-audit's `brain audit-branch` keeps its interface, and the result is now `error`
    where it used to be a truncated `block`.
  - An optional later change is to show `batch` and `attempts` in the curator's event. That is
    not needed for this fix.
- **Docs to update in the same PR:**
  - AGENTS.md "Consolidation": "draining the oldest 40" becomes "the batch the engine chooses
    (at most 15, within the audit's limit)", and the result line's new keys are listed;
  - the README's result-line contract.
- **Review:** the PR changes the consolidation and audit path (`bin/brain`, `setup/*-prompt.md`),
  which is review code and the brain's write path. That makes it a risky change: one deep review
  at the PR's final head, before it merges (`harbor-review`).

## Order of work

1. This plan gets the manager's one plan review. The findings are fixed in this file.
2. One engine PR, holding `passsize.py`, the `_consolidate`/`audit_changes` changes, the prompts,
   the tests, the docs and v0.8.0, is built test-first and committed in small steps.
3. It gets the deep review and its fix check, then is merged and tagged; the admin installs v0.8.0.
4. The live pass (point 4), and the constants tuned if needed.
5. The drain runs, then the workspace timer PR, and the correction capture to the brain decision.
