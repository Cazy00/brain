# Plan: consolidation passes that fit the audit

B45, I-070. Engine `github.com/Cazy00/brain`, from v0.7.0 to v0.8.0. Written 2026-11-04. Plan only:
nothing here is built until the plan review has run. The review ran on 2026-11-04 (verdict PASS,
6 should-fix, 2 notes; kept beside this file as `2026-11-04-consolidate-fits-audit.review.md`),
and every finding is fixed below. "Review findings" at the end maps each one to its fix.

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
    20-note pass: about **$0.12 per note**, linear with no visible fixed part (2 × $2.35 = $4.70;
    brain note `2026-10-23-the-brain-s-curator-pass-runs-daily`).
  - 2026-11-04: the first 200K characters already held 30 canonical files, and the diff ran past
    200K with at most 40 notes in the batch. That is more than **5,000 diff characters per note**,
    and **at least $0.17 per note** ($6.82 over at most 40 notes, engine audit included). This
    pass's captures are longer than October's, at 1–2K characters each. Every drained note also
    shows up in full as a deletion.

## 1. Mechanical caps before the audit

**Recommended:** the engine measures the change set before anything else looks at it. When it is
over a hard cap, the engine runs no audit, discards the attempt completely and runs the pass **once
more, with a smaller batch sized from what it just measured**. If the second try is also over, the
pass ends `blocked`, saves the attempt as a patch for inspection, leaves a clean checkout and pays
for no audit.

- **What is measured** (`brainlib/passsize.py`, a new module with one job: choose a batch and
  measure a change set; pure functions over data `bin/brain` hands it, no git of its own):
  - `canonical_files`: the number of distinct paths under `knowledge/{decisions,topics,projects,
    people,life,reference,archive}/` in `git diff --cached -M --name-status <trunk>`. A rename
    counts once, so a supersede counts its successor and the archived note. Inbox, handoff and
    journal paths do not count.
  - `diff_chars`: `len()` of `staged_diff_vs_main()`, the exact text `audit_changes` hands to the
    auditor.
- **The measured change set is the audited one** (finding 2). Before measuring, the engine stages
  everything, unconditionally: `git add -A` under `repo_lock`, so nothing the proposer left
  unstaged or untracked is missed (today it stages only when the tree is dirty, and only after
  lint). `seal_pass` adds no file: it commits exactly the staged index, or amends the proposer's
  last commit's message, or adds an empty commit; the tree does not change. `.cache/` (the digest)
  is gitignored and never staged; journal entries and handoff notes are written by the proposer
  and are already in the staged set. So the index measured equals the tree of the sealed commit,
  which is what the engine audit (same index) and curator-audit's `audit-branch` (the branch's
  tree read into a private index, then the same `staged_diff_vs_main`) receive. Two guards make
  that a checked fact, not an argument:
  - after `seal_pass` the engine measures again, on the sealed commit against the trunk, with no
    model call; if that is over a cap (it should never be), it takes the same over-cap path below,
    which discards the sealed commit with the rest;
  - a test seals a branch, runs `audit-branch` on it and asserts that the diff the stub auditor
    receives is exactly `diff_chars` long.
- **Hard caps:** at most 30 canonical files and at most `AUDIT_MAX_DIFF_CHARS` (200,000) diff
  characters. The 3-proposal cap is already code (`proposal_problems`).
- **Where the check runs** (note 7): first, straight after the proposer returns and the git control
  files are restored, before lint and before the proposal, handoff, correction and batch checks.
  So an oversized attempt always takes the retry path, even when it would also fail lint. Two
  outcomes skip the measurement and never retry, for a reason each:
  - the runner failed (non-zero exit, timeout, could not start): status `failed` as today. A retry
    would spend another proposer run on a fault that size does not explain;
  - the proposer changed git control files: status `failed` as today. That is a security refusal,
    and a second run would hand the same proposer another go at it.
- **What happens when a pass is over a cap:**
  1. The engine prints `  over caps: <n> canonical files, <m> diff chars (caps 30 / 200000)`.
  2. It discards the attempt (finding 1), all under `repo_lock`: it checks that HEAD is still the
     pass's branch (otherwise `failed`, no retry), then `git reset --hard <trunk>` (which moves the
     branch back to the trunk, so any commit the proposer or the seal made is gone, and resets
     the index and tracked files), then `git clean -fd` (which removes every untracked file the
     proposer created; ignored files such as `.cache/session-digest.md` stay, and the tree was
     verified clean before the branch was cut, so nothing else is untracked). The branch now
     points at the trunk under the same name: that is the recut. The git control files are
     restored the same way they are today, before any git command.
  3. It reruns the proposer, same digest, with the batch cut to the first `N2` items of the same
     ordering (section 2), where `N` is the number of items (corrections and notes) in the first
     batch and, for `files` = `canonical_files` and `chars` = `diff_chars`:

     ```
     file_ratio = 20 / files      if files > 0 else infinity     (finding 3)
     char_ratio = 100000 / chars  if chars > 0 else infinity
     N2 = max(1, min(N // 2, floor(N * min(file_ratio, char_ratio))))
     ```

     A pass over the character cap only through inbox deletions and handoffs has 0 canonical files
     and is sized by characters alone. Both caps cannot be 0 at once: an empty change set is never
     over a cap.
  4. It allows at most **two attempts** per run. The result's cost sums every call of the run.
- **The second attempt is also over** (finding 6), or the first batch was already 1 item: status
  `blocked`, the reason line starts `over caps:`, and no audit runs. The engine then:
  1. writes the attempt as a patch, `git diff --binary --cached <trunk>`, to
     `<state dir>/over-caps/<branch>.patch` (mode 0600; the state dir is `.cache/` or
     `BRAIN_STATE_DIR`, outside git; the newest 5 are kept), and prints its path;
  2. discards the attempt exactly as in step 2 above, checks out the trunk and deletes the branch.

  The next run therefore starts on the trunk with a clean tree, inherits nothing, and is not
  refused as "working tree not clean". A saved patch was chosen over a local-only commit on the
  branch: a kept `consolidate/` branch is what `doctor` reports as an unreviewed pass, and a
  stale one next to tomorrow's would be a second new branch, which the curator refuses (its clone
  is fresh each run, so on home it never sees one; a manual run would). The patch is the same
  content the clone already holds, kept where only its owner reads it.
- **A drain outside the batch:** the pass removed or handed off an inbox note that is not in the
  batch (section 2), not a correction in the batch, not an inbox target of one, and not a
  session-mining proposal. That pass ends `blocked` (`batch check: …`) with no retry. It ignored
  its instructions, and a rerun would not change that. This check runs after the cap check, with
  the proposal, handoff and correction checks.
- **`audit_changes` refuses what is over a cap, and no longer truncates** (finding 4). It measures
  with `passsize.measure` before it builds a prompt. More than 30 canonical files, or a diff over
  200,000 characters, returns `("error", "<n> canonical files / <m> diff chars, over the audit
  limit (30 / 200000); not audited")` and starts no model. `brain audit-branch`, which
  curator-audit uses, goes through `audit_changes`, so the independent gate refuses an oversized
  branch even if the engine's own check had a bug. It is `error` and not `block` because the
  curator records an audit block only on a `block` verdict ("an error is no judgement of the
  audit"), so an over-size diff can never park the curator for a week.
- **The audit prompt keeps** its "more than 30 modified canonical files" clause as a second
  backstop, as it keeps the 3-proposal clause (finding 4). The engine adds a NOTE to the prompt
  giving the measured counts, so the auditor need not count.

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
- *Discard by `git checkout <trunk>` and recutting the branch.* Checkout carries staged and
  untracked changes across, so attempt 2 would start on attempt 1's edits (finding 1).

## 2. Batch size from the backlog

**Recommended: the engine chooses the batch and names it to the proposer path by path.** Today the
prompt says "the oldest 40".

- `passsize.select_batch(items, max_items=15)` takes the inbox as data: per note its file name,
  its size in characters, its `kind`, `source` and `expires`, and, for a correction, its targets
  and their sizes and folders (`bin/brain` reads them with `correction_targets` and
  `forget_target`). It returns the corrections and the notes of the batch, oldest first by file
  name (the capture instant), the estimate, and how many corrections it deferred.
- **Estimates.** A note costs `max(7_000, 4 × its size)` diff characters and 0.85 canonical files.
  A correction costs `max(7_000, 4 × (its size + its targets' sizes))` characters and 2 canonical
  files per canonical target (a supersede writes the successor and the archived note; a
  retraction the archived note and its successor-less move), 0 for an inbox or handoff target.
- **Budget:** **100,000 characters (half the hard cap)**, **20 canonical files (two thirds of
  30)**, and **15 items**, corrections and notes together. It always takes at least one item.
- **Corrections first, within the budget** (finding 5). Corrections are taken oldest first, each
  with its targets, while the budget holds. The rest are **deferred**: they stay in the inbox for a
  later pass, and every inbox note a deferred correction names is left out of this batch, so an
  original is never promoted while its correction waits (`correction_problems` already blocks a
  drain of such a note, so this is enforced as code, not only asked). Then notes fill what is
  left of the budget, oldest first, skipping corrections, session-mining proposals and notes a
  deferred correction names. If one correction alone is over the budget it is still taken (at
  least one item), and the retry and the over-cap path apply to it like any other batch.
  - Recommended over the alternative, *end with a distinct reason when the corrections exceed the
    budget*: that would stop every pass while the corrections stand, which is the same
    never-catches-up failure, only better labelled. Oldest-first always makes progress, and a
    single correction too big for the audit still ends visibly, as `over caps:` naming its batch.
- Why these numbers: 5,000+ characters per note was measured on 11-04, and 7,000 rounds it up for
  the deletion, the promoted body and its Sources lines. Notes 1–2K long therefore come to 12–15
  per pass, and long captures to fewer. A fixed count alone was rejected because captures vary in
  length about five times over.
- **The prompt** changes from "oldest first, max 40" and "read the WHOLE inbox for corrections" to
  the batch the engine lists. `propose_prompt` appends a `THIS PASS'S BATCH` block: the
  corrections to apply, in order, then the notes, by path, and a line saying how many corrections
  were deferred and that their targets must stay in the inbox. The prompt also says project
  overviews and handoffs come only from inside the batch, so neither slips past the cap. Session-
  mining proposals keep their own rules (promote on corroboration, delete once expired) and are
  outside the batch.
- The engine chooses because the limit is the engine's own (its auditor's input). The replace
  test, "to replace this, what changes?": only `passsize.py` and its calls in `bin/brain`
  (`_consolidate`, `audit_changes`, `propose_prompt`), with no curator change. The curator never
  learns the size. A `--batch` flag on the curator was rejected: it would put engine knowledge
  into the curator and split one rule across two repositories.
- The result line gains the optional keys `batch` (items in the attempt that ended the run),
  `deferred_corrections`, `attempts`, `canonical_files` and `diff_chars` (the last measurement).
  `status` keeps its five values, because the curator's `consolidate_result` drops the whole
  line, cost included, when a status is unknown; it reads named keys only, so the new ones pass
  through unnoticed.
- **This lowers the owner's 2026-10-22 pass size of 40.** The owner said yes to 40 "provided it
  improves the brain and avoids future problems", and it did not avoid them. Once this lands, the
  job captures a correction to `2026-10-23-the-brain-s-curator-pass-runs-daily`: up to 15 items,
  chosen by the engine against the audit's limit.

## 3. Draining the 79-note backlog

**Recommended: run several sequential curator runs in one sitting right after the deploy, then go
back to daily.** Each run goes through the whole normal path: engine, curator-audit, the
contradiction check and the no-tap merge. Each pass builds on the merged result of the one before.
Nothing in this work runs a drain or changes the timer: both are spends the owner decides.

- **Cost per note (engine side, proposer plus the engine's audit): $0.17**, the 11-04 floor
  (note 8), not October's $0.12. October's two passes showed no fixed engine cost per pass.
- **Fixed cost per non-idle pass, outside the engine:**
  - curator-audit (`brain audit-branch`, a second audit of the same diff): **about $1.00**. Not yet
    measured on its own; the figure is an allowance for one confined Opus read of a diff of up to
    100K characters plus the notes it opens.
  - the contradiction check (`brain contradictions`, one call, at most 20 pairs): **about $0.30**.
    Measured at $0.09 on the synthetic corpus (2026-10-29, workspace brain-upgrade log); real
    notes are longer.
  - Together **about $1.30 a pass**. The live pass (point 4) measures both from their result
    lines and replaces these figures.
- **Backlog:** the 79 include corrections and unexpired proposals. Call it 65–75 notes to drain
  in 5–7 passes of 12–15. Upper end: 75 × $0.17 = $12.75, plus 7 × $1.30 = $9.10, so
  **$22 for the whole drain**, the live pass included. That is the one figure the owner is
  quoted. One evening of runs at about 30–45 minutes each. The 11-04 pass alone burned $6.82 for
  nothing.
- **Stop rule, tied to the quote** (note 8): after the live pass, and again after every drain
  pass, the manager computes `spent so far + remaining notes × measured cost per note + remaining
  passes × measured fixed cost per pass`, from the `consolidate-result`, `audit-result` and
  `contradiction-result` lines. If that is over $22, the drain stops and the owner gets the new
  figure to decide on. A blocked or failed pass stops it too.
- **At one pass a day it never drains.** Captures arrived at about 14 a day in October, close to a
  15-item batch. After the drain, I recommend **two timer slots a day (02:00 and 14:00 UTC)**.
  The notes cost the same whichever slot drains them; what a second slot adds is the fixed cost of
  a second non-idle pass: **up to $1.30 a day, about $40 a month**, and less on days when one slot
  finds nothing (an idle pass makes no model call, and curator-audit and the contradiction check
  run only after a pass that was not idle). This is a one-line change to the workspace's
  `curator/deploy/harbor-curator.timer`, in its own workspace PR. Turning the timer back on, and
  the second slot, are the owner's choices: the owner paused the timer, and both are spends.
- **Rejected:**
  - *Several passes inside one engine run.* The curator accepts exactly one commit on
    `origin/main` per run (`one_audited_commit`), and pass k+1 must build on pass k after k has
    been audited and merged. Doing that inside one run would mean changing the curator's
    contract.
  - *One bigger pass.* That is the failure itself.
- Each extra run is a manual `systemctl start harbor-curator.service`, done by the admin as a
  `root-command` (logged, no tap). They run one after another, never in parallel: the curator
  refuses a busy run with `EXIT_BUSY`, and the memory budget is shared.
- API credits are nearly used up. The manager tells the owner that the drain costs about $22
  and a second slot up to $40 a month before either starts.

## 4. Tests (no model) and the one live check

New `ConsolidateTests` and `ConsolidationAuditTests` cases use the existing `stub_claude` (a
`claude` shell stub on PATH). The stub tells propose from audit by its argv, logs every call to a
file, and reads the batch from the prompt it is given.

1. `select_batch`: oldest first; proposals are excluded; the char budget, the file estimate and
   the 15-item cap each stop the selection; at least one item is always taken; an empty inbox
   gives an empty batch.
2. `select_batch` with corrections over the budget (finding 5): the oldest corrections that fit
   are taken with their targets, the rest are deferred and counted, and no note a deferred
   correction names is in the batch; a single correction over the budget is still taken alone.
3. `measure`: a rename counts once; inbox, handoff and journal paths are not counted; archive
   paths are; `diff_chars` equals `len(staged_diff_vs_main())`.
4. The N2 formula: over the character cap with 0 canonical files gives a smaller batch and no
   error (finding 3); over the file cap only; `N2` is never above `N // 2` or below 1.
5. Propose writes 31 canonical files (and an untracked scratch file, and a commit of its own) on
   the first call and 5 on the second. The stub's second propose call records, before it writes,
   that none of attempt 1's files exist and that `git status --porcelain` is empty and HEAD
   equals the trunk (finding 1). The test checks two propose calls (the second prompt lists `N2`
   items), exactly one audit call, status `committed`, `attempts: 2`, **one** `consolidate/`
   branch holding **one** commit on the trunk (the curator's invariant), and cost summed over all
   three calls.
6. Propose goes over the caps both times: status `blocked`, zero audit calls, the reason starts
   `over caps:`, both attempts' cost is reported, the patch file exists and holds attempt 2's
   files, HEAD is the trunk, the tree is clean and no `consolidate/` branch is left (finding 6).
   A second `consolidate` run then starts normally: it is not refused as unclean and its
   propose call is made.
7. A diff over 200K with fewer than 30 files follows the same path as 5.
8. A batch of 1 that is over the caps: no retry, `blocked`, the same clean end as 6.
9. Over the caps and failing lint on attempt 1: it retries (note 7), and attempt 2's lint result
   decides.
10. A drain outside the batch: `blocked` with `batch check:`, no retry, no audit.
11. A correction in the batch whose inbox target is outside the notes is drained with it, and that
    is allowed; draining a note a deferred correction names is blocked.
12. `audit_changes` and `audit-branch` given more than 30 canonical files, and given a diff over
    200K characters: verdict `error`, the runner is never started, and `audit-result` says
    `error` (finding 4).
13. The sealed branch's `audit-branch` diff equals the engine's measured `diff_chars`
    (finding 2), including when the proposer committed part of its work itself and left the rest
    untracked.
14. The prompt rules: `ConsolidationPromptRulesTests` drop the "oldest 40" and "WHOLE inbox"
    phrases and pin the batch block. The audit prompt keeps its file-count clause.
15. The result line: the new keys are present, and `status` is one of the five.

`python3 -m unittest discover -s tests` must be green before the PR leaves draft.

**The one live check**, after the engine PR merges and v0.8.0 is installed in `/opt/brain-engine`:
the admin starts `harbor-curator.service` once, with the timer still paused. Its kept log in
`/var/log/harbor-curator` (admin-readable) must show:

- `consolidate-result` with `status: committed`, a `batch` of 12–15, `canonical_files` ≤ 30,
  `diff_chars` ≤ 200,000 and `attempts: 1`;
- an engine audit pass and a curator-audit pass, with their costs;
- the brain-data PR merged.

Rule (e) of the curator's block rules, a new engine version, clears the 11-04 block record, so
that record does not skip this run. If the measured characters or files per note are more than 30%
off the estimates, a small follow-up PR adjusts the constants in `passsize.py` before the drain
runs. The stop rule (point 3) is then applied to the live pass's measured costs before the drain.

## 5. Contract and version

- **Engine 0.7.0 → 0.8.0** in `bin/brainlib/version.py`, tagged `v0.8.0`. It is a minor bump: the
  behaviour changes, the result line gains keys, and no call changes.
- **The curator (`Cazy00/workspace`, `curator/bin/curator-engine`) needs no change.**
  - It still calls `consolidate --digest-from … --digest-since …` with no new flag.
  - It still receives one branch with one commit: the retry reuses the branch name, reset to the
    trunk, and a blocked over-cap run leaves no branch.
  - It still gets the same five statuses, and it ignores the new keys
    (`engine.consolidate_result` reads named keys only).
  - curator-audit's `brain audit-branch` keeps its interface, and the result is now `error`
    where it used to be a truncated `block`.
  - An optional later change is to show `batch` and `attempts` in the curator's event. That is
    not needed for this fix.
- **Docs to update in the same PR:**
  - AGENTS.md "Consolidation": "draining the oldest 40" becomes "the batch the engine chooses
    (at most 15 items, corrections first, within the audit's limit)", the corrections paragraph
    says corrections over the budget wait for a later pass with their targets, the over-cap retry
    and its patch file are described, and the result line's new keys are listed;
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
5. With the owner's yes to the quoted $22: the drain runs under the stop rule, then (with a
   separate yes) the workspace timer PR, and the correction capture to the brain decision.

## Review findings

| # | Finding | Fix in this plan |
|---|---|---|
| 1 | Discard by checkout carries work across | Section 1, step 2: `git reset --hard <trunk>` + `git clean -fd` under the lock; test 5 checks attempt 2 starts clean |
| 2 | Measured diff may differ from the audited one | Section 1, "The measured change set is the audited one": stage all first, re-measure after the seal; test 13 |
| 3 | `20 / files` divides by zero | Section 1, step 3: guarded ratios; test 4 |
| 4 | 30-file backstop lost in the independent gate | `audit_changes` (and so `audit-branch`) refuses > 30 files as `error`; the prompt clause stays; test 12 |
| 5 | Corrections over the budget block every pass | Section 2: corrections oldest first within the budget, the rest deferred with their targets; test 2, test 11 |
| 6 | Over-cap second attempt leaves a dirty tree | Section 1: patch file in the state dir, clean reset, branch deleted; test 6 |
| 7 | (note) Lint before the cap check skips the retry | Section 1, "Where the check runs": caps measured first; runner and git-control failures excepted, with reasons; test 9 |
| 8 | (note) Cost per note, fixed costs, stop rule | Section 3: $0.17 a note, $1.30 fixed a pass, $22 drain, $40 a month for a second slot, stop rule tied to $22 |
