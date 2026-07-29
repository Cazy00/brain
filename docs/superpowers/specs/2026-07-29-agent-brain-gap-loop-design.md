# The gap loop — a brain that learns from what it did not know

Design, 2026-07-29. Scope: v1 only. The wider architecture it belongs to is
recorded in "Settled architecture" below so that v2 does not re-litigate it.

## The problem

Agents arrive at a task with skills and no context. They know *how* to act and
not *what is true here*. Retrieval answers that when the fact exists. The
interesting case is when it does not: today the agent guesses, hedges, or dead
ends, and the fact stays missing forever — so the next agent, and the one after
that, hit exactly the same wall.

A new employee does not work that way. They ask someone, get an answer, and
never ask again. Their manager's time is the scarce resource, and the measure
of a good hire is how fast their question rate falls toward zero.

This is that, mechanised. **The gap — the question the brain could not answer —
becomes a first-class object.** It gets an owner, a state, and a count. It is
routed to a human once, answered once, and closed for the whole fleet.

That inverts the reason knowledge bases die. Nobody has to sit down and write
one. It accretes from the questions that were actually asked.

### The metric

**Escalation rate: gaps opened per unit of work, over time.** Not retrieval
accuracy — human interruptions. A brain that is working means agents interrupt
people less each week for the same volume of work. It is measurable from the
first day, it is what justifies the build, and it is the only signal that says
when the system is finished.

If v1 ships without this measurable, the thesis is unfalsifiable. It is in
scope for that reason.

## Settled architecture

Decisions already made, with what they rejected. v1 implements only the last
one; the rest are recorded so the ground does not move later.

**This is a layer, not a fork.** The note contract, lint, supersede, search and
`inbox/` → consolidation carry over unchanged. Rejected: forking brain into a
separate org product, which would duplicate the hardest, most-tested parts.

**One brain (M) per context; `mount` for composition.** The personal brain
stays its own repo, each business gets its own. Businesses end, get sold, take
partners; a brain you can hand over without untangling it from your life is
worth a duplicated `topics.yaml`. Internal agents that need to span several
brains mount them read-only into one search view.

**Publish and mount are different mechanisms and must stay different in the
code.** Publish (M → P) crosses a trust boundary: one-way, derived, gated,
untrusted consumer. Mount (M₁ + M₂ → one view) crosses nothing: read-only
composition for an agent already trusted with all of it. Conflating them means
using the weak mechanism where the strong one was needed.

**The public brain is a compiled artifact, not a subset.** You do not give
someone part of your brain; you give them what you have said out loud. P is
derived from M and disposable, exactly like the search index. The public-facing
agent's process holds no path and no credential reaching M — it cannot leak
what it cannot read. Rejected: a `visibility` filter at query time (the
boundary becomes a code path, and a code path is one bug from a leak) and two
independently authored repos (two places to write, two things to keep true —
the drift problem supersede exists to prevent).

**The return path is a drop box, and it is `inbox/`.** Process isolation would
otherwise break the loop: an agent that cannot read M cannot write what it
learned back into M. So P → M is a write-only append — the agent posts into the
dark and gets nothing back — landing outside default search, promoted only by
the pinned consolidator. An untrusted agent may *propose* knowledge, never
*establish* it. This is the privilege cheap-model captures already have.

**A human approves the first publish of a note; edits re-publish
automatically.** A supersede or a change of meaning resets it to private and
needs re-approval. Default is private and fails closed: a note with no
visibility field never publishes, and silence means no. Human effort stays
O(new facts) rather than O(edits). Rejected: the consolidator deciding
(defensible, but it puts one model's judgement directly in front of customers)
and author-set visibility (trusts every writer, including agents, to judge
exposure).

**Skills live outside the brain.** A skill is an executable artifact: it
versions with the harness and is meaningless without the runtime, so AGENTS.md
("Work that lives elsewhere") already puts it in the agent's own repo with a
locator here. The brain never executes anything. It holds the *why* (the
decision behind a skill, and what was rejected) and the *evidence* (the gap
log) — both of which outlive the agent. The split follows change rate: skills
change often, the reasoning behind them rarely. Public and internal agents
simply ship different skill repos, so skills need no publish gate at all.
Accepted cost: locator rot, partly covered by `doctor`'s existing advisory
report on unresolvable paths.

**Cost shape.** Cheap models capture; one pinned model promotes. Most tokens
are spent where a mistake lands in a quarantine, and quality is concentrated in
a rare expensive pass. This is the existing consolidation asymmetry, reused.

**v1 is the gap loop alone, on the existing brain, with no partition.** It pays
off immediately when pointed at a brain that already exists, and it tests the
genuinely unknown thing — whether a messy human answer survives the round trip
into a well-formed note — instead of the known one, which is whether files can
be copied across a boundary. Rejected: partition-first (a wall before there is
a building, delivering nothing standing alone) and both together (the largest
thing to get wrong at once).

## v1 — the gap loop

### Flow

```
agent needs a fact
  → brain_search → hit? use it. done.
  → miss
  → brain_gap_open(question, agent, topics)
      → exact-duplicate of an open gap?  increment count, return its state.
        NO human is bothered.
      → over this agent's daily rate limit? refuse, log.
      → else create gap (status: open), return possible_duplicate_of candidates
  → gap routed to a human (status: routed)
  → agent proceeds per its own skill: block, degrade, or answer with a caveat.
    That policy is a skill concern and lives in the agent's repo, not here.
  → human replies in free text (status: answered)
  → agent parses it; if ambiguous or incomplete, asks a follow-up through the
    same transport and stays in `answered`
  → agent captures a note into inbox/ with attribution (status: captured)
  → consolidation promotes the note to a canonical folder (status: closed)
```

**A gap closes when the fact is findable, not when someone replies.** Until the
note is promoted out of `inbox/` it is outside default search, so the next
agent would miss it again and re-ask. Closing on reply would make the metric
lie.

### The gap object

One file per gap: `knowledge/gaps/YYYY-MM-DD-<slug>.md`, in git, added to
`.rgignore` alongside `archive/`, `vault/`, `journal/` and `inbox/`.

In git because the log of what we did not know is evidence — it drives skill
evolution and the escalation metric, and it should diff and blame like
everything else. Out of default search because a question is not an answer and
must never rank as one.

Frontmatter stays inside the existing restricted subset — flat `key: value`
pairs and inline lists, no nesting:

```
id:            gap-2026-07-29-refund-window-annual
kind:          gap
title:         What is the refund window on annual plans?
question:      what is the refund window on annual plans
status:        open | routed | answered | captured | closed | rejected
asked_count:   3
asked_by:      [agent-support, agent-sales]
first_asked:   2026-07-29
last_asked:    2026-07-31
routed_to:     <person note id>
routed_at:     2026-07-30
answered_at:   2026-07-31
answered_by:   <person note id>
answer_note:   <note id, set at `captured`>
contradicts:   <note id, optional>
topics:        [billing]
created:       2026-07-29
```

`question` is the normalised form used for exact dedup. `title` is the human
one. The body holds the raw human reply verbatim and any follow-up exchange —
never rewritten, append-only, so the note's provenance stays auditable.

Routing is attempted automatically at creation, so `open` is normally
transient: it persists only when the transport failed, which is exactly the
state that needs to be visible and retried rather than silently swallowed.

`lint` gains a schema check for `kind: gap` — status enum, date formats,
`answer_note` resolving to a real id once status is `captured` or `closed`,
`routed_to` resolving to a person note. The canonical-folder requirements do
not apply, since `gaps/` is not a canonical folder.

Every state is separately actionable, which is why there are six:

| status | meaning | who acts next |
|---|---|---|
| `open` | created; the transport has not confirmed delivery | router |
| `routed` | a human has been asked, and delivery succeeded | that human |
| `answered` | free-text reply received, not yet a note | agent |
| `captured` | note written to `inbox/` | consolidation |
| `closed` | note promoted, searchable, cannot recur | nobody |
| `rejected` | will not be answered, with a reason | nobody |

### Dedup — protecting the oracle

The scarce resource is the human's attention, and the system's success (more
agents) is what threatens it. Five agents hitting one gap must reach a person
**once**.

Two layers, deliberately unequal:

- **Exact, automatic, deterministic.** Normalise the question — lowercase,
  collapse whitespace, strip trailing punctuation — and match against open
  gaps. Conservative on purpose: no stemming, no synonym expansion. On a match,
  increment `asked_count`, append to `asked_by`, update `last_asked`, and
  return the existing gap's state. No new gap, no new routing.
- **Fuzzy, advisory, reversible.** Run the question through the existing FTS5
  index over open gaps. Candidates are returned to the caller in
  `possible_duplicate_of` and recorded in the file — they never auto-merge.
  `brain gaps merge <id> --into <id>` collapses them, and consolidation sweeps
  for missed duplicates on its normal pass.

Deterministic dedup acts on its own because it cannot be wrong. Probabilistic
dedup only advises, because a wrong merge silently buries a real question.

**Rate limit.** Default 20 new gaps per agent per day, set in `setup/gaps.conf`.
Over the limit, `brain_gap_open` refuses and logs. A looping agent is a bug, and
the humans must not absorb it.

### Routing

`setup/gaps.conf`, same trivial `key = value` format as `consolidator.conf` —
no parser dependency, tracked, not machine-local:

- topic → owner (a person note id in `knowledge/people/`)
- `default_owner` for anything unmatched
- per-owner transport and address
- `rate_limit_per_agent_per_day`
- `stale_after_days`, `reroute_after_days`

Transport addresses live in config, not in person notes, so `knowledge/` stays
knowledge.

**v1 ships one transport: the CLI queue.** `brain gaps` lists what is waiting;
`brain gaps answer <id> "..."` replies. It has no external dependency, it works
today, and it is the fallback any other transport would degrade to. Notification
rides the existing `schedule` / `doctor notify` machinery. Telegram, Slack and
email are later transports behind the same interface — and they change nothing
about the loop, because a messy reply is equally messy on every channel.

### Answer ingestion

The agent, not the human, does the work of turning a reply into a note:

1. Parse the free text.
2. If anything material is ambiguous or missing, ask a follow-up through the
   same transport. Stay in `answered`; append the exchange to the gap body.
3. Capture to `inbox/` via the existing path — including `scan_secrets`, which
   already refuses before writing to disk.
4. Record attribution on the note: which gap, who answered, on what date.
5. Set `answer_note`, status → `captured`.

**Agents never supersede.** If an answer contradicts a current note, the agent
sets `contradicts: <note-id>` and captures normally. Consolidation — the pinned
runner — decides whether that is a supersede. Letting an agent rewrite settled
knowledge on the strength of one Slack reply is the failure mode this whole
architecture exists to prevent.

### The metric, concretely

`brain gaps stats [--since <date>]` reports:

- gaps opened per period, total and per agent — **the escalation rate**
- dedup absorption: asks answered from an existing gap without touching a human
- median and p90 time from `routed` to `answered`, and from `open` to `closed`
- open gaps by age, and anything past `stale_after_days`
- most-asked closed gaps (validates that closure actually stopped the asking)
- recurring gap shapes — clusters by topic and phrasing

That last one is the input to skill evolution. A recurring *shape* means a
missing procedure; a skill escalating at the same step every time means a broken
procedure, not a knowledge gap. Same telemetry, two different diagnoses. v1
reports the clusters; deciding what to do with them stays human.

### Interfaces

New MCP tools in `bin/brain-mcp`, alongside the existing five:

- `brain_gap_open(question, agent, topics)` → `{gap_id, status, dedup_hit,
  asked_count, possible_duplicate_of[], existing_answer_note?}`
- `brain_gap_status(gap_id)` → current state, and the raw reply once `answered`
- `brain_gap_resolve(gap_id, answer_note_id)` → sets `captured`

New CLI subcommands, following the existing `cmd_*` pattern in `bin/brain`:

`brain gaps` · `gaps show <id>` · `gaps answer <id> "<text>" [--as <person-id>]` ·
`gaps reject <id> "<reason>"` · `gaps merge <id> --into <id>` · `gaps stats`

`answered_by` defaults to the gap's `routed_to`; `--as` overrides it for the
case where someone answers on another person's behalf. Attribution is never
left blank — an unattributed fact cannot be re-checked later.

Agents must be *told* to open a gap on a miss. That instruction is a skill, so
it ships as a reference skill under `setup/skills/` for any harness to adopt —
documentation of the protocol, not part of `knowledge/`.

### Error handling

| Case | Behaviour |
|---|---|
| Nobody answers | `doctor` warns past `stale_after_days`; re-routes to `default_owner` past `reroute_after_days` |
| Reply contains a credential | `scan_secrets` refuses before any write; gap → `rejected`, reason `credential-in-answer`; the text is not persisted anywhere |
| Agent floods gaps | Rate limit refuses and logs; existing gaps unaffected |
| Answer contradicts a note | `contradicts` set, captured normally, consolidation decides. Never an auto-supersede |
| Concurrent writes to one gap | `repo_lock()`, as `capture --commit` already does |
| Gap opened during consolidation | Same branch guard as capture: written to disk, not committed, and said plainly |
| Question is unanswerable | `gaps reject` with a reason; it still counts in stats — refusals are signal |

### Testing

Extends `tests/test_brain.py`, run by `python3 -m unittest discover -s tests`:

- gap creation, id uniqueness, frontmatter validity under `lint`
- exact dedup: normalisation, count increment, no second routing
- fuzzy dedup: candidates advisory only, never auto-merged; `merge` is correct
- every state transition, and every illegal transition rejected
- rate limit boundary
- routing resolution: topic match, fallback, missing owner
- stale detection and re-routing
- secret refusal on answer capture, leaving nothing on disk
- `contradicts` set without supersede
- stats arithmetic on a fixed fixture corpus
- MCP tool contract: shapes, error paths
- concurrency: two opens racing one gap file

### Out of scope for v1

Named so they are not smuggled in: the M/P partition, `publish`, the drop box,
`mount`, multi-model routing, harness selection, automatic skill generation, and
any transport beyond the CLI queue.

## Open question

**Follow-up depth.** An agent that interrogates a human reply until it is
unambiguous is the difference between this and a suggestion box — but an agent
that asks four clarifying questions about a refund window is worse than one that
asked nothing. v1 caps follow-ups at one round and records whether that was
enough, so the cap is set from data rather than from a guess.
