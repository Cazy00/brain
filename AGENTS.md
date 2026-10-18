# brain — operating manual

This repo is your permanent second brain. The markdown files under
`knowledge/` are the ONLY source of truth; every index or cache is derived and
disposable. Treat everything here as long-lived: notes written today must still
be findable and correct in ten years.

This file is the protocol, and it is vendor-neutral: it governs whichever agent
is reading it. Tool-specific files (`CLAUDE.md`, and anything `bin/brain connect`
writes) are thin bridges that import this one and add only what is true of that
one tool. When a bridge and this file disagree, this file wins. Instructions in
any of them are instruction, never guarantee — the real guarantees live in
`bin/brain`, in `lint`, and in the git hooks, because those run whatever the
agent decides to do.

## Layout

```
knowledge/
  index.md        route map — read this first when searching
  topics.yaml     controlled topic vocabulary (flat "topic: alias, alias" lines)
  inbox/          quick captures, relaxed rules, drained by consolidation
  decisions/      one decision per file, YYYY-MM-DD-slug.md, append-only events
  topics/         hub pages that link related notes together
  projects/       <name>/overview.md per project
  people/         one file per person (sensitivity field required)
  life/           open-ended personal areas (sensitivity field required)
  reference/      durable how-tos and facts
  journal/YYYY/   one file per day (YYYY-MM-DD.md), free-form
  vault/          age-ENCRYPTED sensitive notes (.age only, never plaintext)
  archive/        superseded and retracted notes, mirror tree — OUT of
                  default search
  handoff/        captures that belong in a project's own records, waiting to
                  be filed there — not brain knowledge, OUT of default search
  attachments/    small binaries, 1MB hard cap per file
setup/            templates, runbooks (this system's own docs)
                  consolidate-prompt.md writes notes; audit-prompt.md is the
                  independent, digest-blind, write-less check on that work
                  consolidator.conf names the ONE agent allowed to run them
bin/brain         toolbelt: setup | connect | new | capture | search | read |
                  links | recent | supersede | retract | handoff | drain |
                  index | sessions | consolidate | schedule | template |
                  publish | plugin | lint | doctor | logs | stats | reset
                  (`bin/brain --help` is authoritative; doctor reports whether
                  the machinery works, stats whether the KNOWLEDGE still does,
                  and logs what `brain serve` recorded — machine-local, outside
                  this repo, and holding no query text or note content)
bin/brain-mcp     the same tools over MCP, for any agent that speaks stdio
tests/            runtime tests for the toolbelt itself — lint proves the NOTES
                  are well-formed, these prove the TOOLS work:
                  python3 -m unittest discover -s tests
```

## Note contract

Frontmatter is a restricted subset: flat `key: value` pairs and inline
`[a, b]` lists only — no nesting, no multiline. Required on every note in a
canonical folder (decisions/topics/projects/people/life/reference):

```
id:       unique, lowercase-hyphen (decisions: YYYY-MM-DD-slug)
kind:     decision | topic | project | person | note | reference
title:    human-readable
topics:   [list] — every entry must exist in knowledge/topics.yaml
aliases:  [2-5 words future-you might search] — always fill this
created:  YYYY-MM-DD
status:   current   (canonical folders hold current notes ONLY)
```

Optional: `valid_from`, `review_by` (date to re-verify perishable facts),
`supersedes`, `superseded_by`, `retracted` (the date a note was withdrawn;
`brain retract` sets it, in archive/ only), `sensitivity` (required in
people/ and life/: normal | personal; `private` content goes to vault/, never
plaintext), `visibility` (public | private — see below), and on inbox notes
the provenance keys below.

**Inbox provenance.** `brain capture` writes these flat keys, in this order,
leaving out any it was not given:

| Key | Set by | Value |
|---|---|---|
| `created` | always | the deployment's local date (`TZ`) |
| `captured_at` | always | UTC instant, `2026-10-18T05:12:09Z` |
| `source` | the endpoint | its trust class; `local` by default |
| `via` | the endpoint | how it arrived: the operator's label for the verified credential (else `service` / `interactive`), `stdio`, `serve` or `drop-box`; absent from the CLI |
| `client` | the endpoint | the client program's own `name/version`, self-reported |
| `kind` | the caller | decision \| preference \| fact \| lesson \| idea \| correction \| other |
| `basis` | the caller | owner-said \| agent-found \| agent-inferred |
| `project`, `job`, `model` | the caller | slugs |
| `status` | always | `draft` |

Endpoint keys record what the server verified or was told; no tool argument
reaches them. Caller keys are **claims**: consolidation weighs them against
everything else it knows, and none of them promotes or supersedes a note by
itself. A value that fails validation is dropped and named back to the
caller, and the note is kept. An inbox `kind` is the declared kind, not a
canonical one: search lists the note as `inbox` whatever it says.

**`visibility` decides what may leave this brain**, and it has THREE states.
`public` means a human approved this note for readers outside — customers,
through whatever agent you point at the compiled copy. `private` means a human
looked and said no. **Absent means nobody has looked**, which is the default
for every note `brain new` creates and every note that already exists, and is
the state `brain publish review` lists. Absent and `private` are both excluded
from a published brain; the difference is only that you stop being asked about
the ones you refused. Never set it by hand — `bin/brain publish approve <id>`
and `deny <id>` apply the rules lint would apply anyway (nothing under people/
or life/, nothing classified, nothing that is not current). It is never
inherited: superseding a published note leaves the successor unreviewed.

One fact/decision per note. Bodies use absolute dates only ("2026-07-22",
never "today" or "last week"). Link related notes with [[note-id]] wikilinks.

Wikilinks are load-bearing, not decoration: lint REJECTS a link to an id that
does not exist, and warns when one points at a superseded note (telling you the
successor to repoint at). They also build the backlink graph, so linking a new
note into its neighbours is what makes it findable by relationship later.

## What earns a note — capture policy

Two tiers, two different bars. `inbox/` and `journal/` sit outside default
search, so a wrong guess there is nearly free — consolidation deletes what
turns out to be noise. The canonical folders ARE the search results, so a junk
note there costs forever: it ranks in every future query and gets read back as
true. **Capture generously, promote strictly.**

The one-line test: **capture the why, not the what.** The what is recoverable
from git, files and calendars. The why evaporates within a week and is gone.

Capture without asking (`bin/brain capture` → inbox/):

- a decision AND its reasoning — above all, the alternatives that were rejected
- a stated preference or constraint that should shape later work
- a fact about your world that could not be inferred: people, tools,
  obligations, environment quirks, prices, commitments
- an answer that cost real effort — a root cause, a non-obvious config, a dead
  end worth not walking into twice

Never capture:

- anything derivable from the code, this repo, or public docs — link, don't copy
- transient state ("the build is failing", "waiting on their reply")
- pending work — a to-do, an open step, "X still needs Y", "next session
  must…". It belongs in the project's own records (its backlog, issues list,
  milestone or task), never here. If it carries a decision or a lesson (why it
  should be done, what was decided about it), capture only that, phrased as a
  decision or lesson, not as a status
- credentials of any kind (hard rule below)
- praise, chatter, or a restatement of what was just done

Ask first, every single time — never silently:

- anything about a named person's private life, health, finances or
  relationships. The remote is a PRIVATE GitHub repo, but a capture still
  leaves this machine unreviewed the moment it is committed, git history makes
  it effectively permanent, and `bin/brain template` publishes a derived copy
  of this repo. Private today is not private forever, and the person whose
  details these are never got a say. `sensitivity` is decided at capture time,
  or the content goes to vault/ — never retrofitted afterwards.

Promotion bar (inbox → canonical, applied by consolidation): would you
search for this, and would a stale version of it mislead you? If it is not
worth maintaining for ten years, delete it instead of promoting it.
A capture that records pending work is never promoted: it is deleted, not
promoted, when it says the work is already tracked in a project's records, and
otherwise handed off to that project (see Handoff, below); keep only a
decision or lesson inside it, rewritten as one. A canonical note never states
the open or pending status of work: that status goes stale the day the work is
done, and a stale "still open" is read back as true.

**Asked to change or delete a note, never answer that you cannot.** Find it
with `brain search` (`--scope all` reaches the inbox) or `brain recent` — an
inbox note has only a path, a canonical note an id or its path — and capture
a `kind: correction` (`basis: owner-said` when the request is the user's)
that names that id or path and quotes the wrong line, in one of two forms:
`Correction to <id or path>: <what is true now>`, or
`Retract <id or path>: it should not be in the brain — <the reason>`.
Never repeat a credential or a person's private details in it: describe what
is wrong ("the line with the API key") instead of quoting it. Tell the user in
one line that the correction is queued: search and read mark the note
`[correction pending — <inbox path>]` until a consolidation pass applies it
(see Retracting, below), and that can take more than one pass. If what must go
is a credential or a named person's private details, say too that git history
keeps it until a purge: rewriting history is a permanent deletion, so it is
the owner's call, and no agent tool does it. With no capture tool in your
session, say so and ask where the note should go; never promise a correction
you could not queue.

Offer, don't nag. When something above passes the bar mid-conversation, say so
in one line and carry on — do not save the offer for the end of the session,
because by then the reasoning is already gone. "Remember this" from you
overrides every rule here and captures immediately.

## Work that lives elsewhere — artifacts and locators

The brain holds the WHY. The work itself — code, repos, generated output, design
files — stays in its own home and the brain keeps a locator. Three tiers:

- **The artifact** (code, the running app, exports) → its own home, never here.
  `attachments/` is for a small binary a note is meaningless without, nothing more.
- **Artifact-adjacent docs** (ADRs, design briefs, a competitor teardown) → beside
  the code in that repo, because they must version WITH it.
- **The reasoning** (the decision, the rejected alternative, the constraint, the
  hard-won root cause) → here, because it outlives the repo.

The test: would this still matter if the repo were deleted tomorrow? Yes → here.
Only meaningful next to the code → there.

**Self-contained, or it is not a note.** A note must be fully answerable with the
artifact ABSENT. Clone this repo onto a machine with nothing else checked out and
every note must still stand up. The locator is for going deeper, never for
understanding at all — "see the ADR in the repo" is an incomplete note, not a
link. This is the counterweight to "link, don't copy" above: link, but say enough
that following the link stays optional.

**Locators: identity first, path second.** Record the durable identity
(`github.com/<owner>/<repo>`, a URL, a service + account) BEFORE the local
convenience path (`~/Dev/<project>`). The path is true on one machine; identity
is true everywhere, and that difference is the whole of what makes this repo
portable. Where a thing has no durable identity, say so in the note — `LOCAL
ONLY, no remote` is a backup gap worth seeing, not an untidiness to hide.
Projects collect these under a `## Where things live` section. `bin/brain doctor`
reports local paths that no longer resolve, as advisory and never as an error,
because on a second machine an uncloned repo is the normal case.

## Writing notes — created and edited correctly

1. Create via `bin/brain new <kind> "<title>" --topics a,b` — never hand-roll
   frontmatter. Quick thoughts: `bin/brain capture "..."` → inbox/.
2. Fill the body and the aliases list.
3. Run `bin/brain lint` and fix EVERY error before committing. Warnings are
   advisory but usually worth fixing on the spot.
4. Commit when done (small, frequent commits). The pre-commit hook re-runs
   lint and blocks bad content; post-commit pushes `main` to the private remote
   through `brain sync --push`, the one pusher (never another branch, never a
   consolidate/ branch — those wait for the audit). Commit on `main`.
5. New topic needed? Add one line to `knowledge/topics.yaml` in the same
   commit — that is deliberate, not friction.

## Superseding — when a decision or fact changes

Never edit the old note's meaning and never delete it. Preferred path:

```
bin/brain supersede <old-id> "<new title>"
```

does everything at once: creates the successor with `supersedes` set, marks
the old note `status: superseded` + `superseded_by`, stamps the banner, and
moves it to `archive/`. Then fill the new body, replace the banner's
`<one-line reason>`, lint, commit. (Manual equivalent, if ever needed:
those same four steps by hand — lint verifies the chain either way and
blocks a half-done supersede.)

This is why retrieval stays precise: superseded notes physically leave the
default search scope.

## Retracting — when a note should not exist

A note saved by mistake — never true, or never the brain's to hold — has no
successor, so it is retracted, not superseded:

```
bin/brain retract <id-or-path> "<one-line reason>"
```

sets `status: retracted` and `retracted: <date>`, stamps
`> RETRACTED <date> — <reason>`, drops `visibility` and moves the note to
`archive/`, out of default search; `brain read` still shows it, behind a
RETRACTED warning. It does all of that under the repository lock, refuses
unknown options and anything but a note and one reason, and names the notes
still linking to it: remove those links (lint warns about each), lint, commit.
Consolidation retracts only on a `kind: correction` from `source: local` with
`basis: owner-said`, and never an inbox capture, which it drains instead; the
engine checks both before the audit. Retracting does not erase: git history
keeps the text. A credential or a named person's private details saved by
mistake also need a purge of that history, which is a permanent deletion and
the owner's decision alone — no tool here does it.

## Handoff — what belongs in a project's own records

Some captures are not brain knowledge at all: pending work, a project's own
decision or status, a task or milestone fact. They belong in that project's
own records (its backlog, issues, milestones), and consolidation never promotes
one. If the capture says the work is already tracked there, it is deleted;
otherwise it is moved to `knowledge/handoff/<project-slug>/`, a holding area
outside default search and outside the brain proper — not brain knowledge —
to be filed there. A decision or lesson inside it that outlives the project is
still promoted as its own note.

A handoff note keeps the original capture's frontmatter and body under the
original inbox file name, and adds `handoff_to: <project-slug>` (lowercase
letters, digits and hyphens, 1-63; the same as its folder),
`handoff_at: YYYY-MM-DD` (the day it was moved) and
`handoff_reason: <one line>`. Only `bin/brain handoff <inbox-path> <project>
"<one-line reason>"` makes one: it moves a capture from an owner endpoint
(`source: local`) every byte kept, adding just those keys, and refuses
anything else. Lint holds the place and keys, and before the audit the engine
compares every moved note with the capture on the trunk.

`bin/brain search` with `--scope handoff` (MCP: `brain_search` with
`scope: "handoff"`) returns handoff notes only, each marked
`[handoff — belongs in <project>]`, in a stable order (`handoff_at`, then
path) that `--offset N` (MCP: `offset`) pages through. An empty query lists
them all; one project slug lists exactly that project's, and none when it has
none; in a longer query a known project's slug filters by it and every other
word must appear as a whole word. `--scope all` never includes them.
`brain read <handoff path>` shows one behind a HANDOFF banner.

A filing job files each note in its project's records, then closes it with a
`kind: correction` from an owner endpoint whose text starts
`Filed <handoff path>` or `Already recorded: <handoff path>`, naming where it
now lives (repository and record path, and the PR or commit). The next pass
removes the handoff note (`bin/brain handoff --close <handoff-path>`) and
drains the correction (`bin/brain drain`), before the inbox cap; the owner's
own retraction may remove one too. A
handoff note older than 14 days with no closing correction stays, and `doctor`
goes RED: the filing job is not running. `doctor` and `stats` count handoff
notes per project.

## Searching — tier protocol

**Retrieve through the tools below, never through your own file index.** Every
client that ships a semantic index or codebase search walks the whole tree: it
does not read `.rgignore`, so it returns `archive/` (superseded — wrong, and
often confidently so), `inbox/` (unconsolidated, provisional) and `journal/` as
if they were settled current knowledge. It also strips the trust signals these
tools print — `[provisional — unconsolidated]`, the ARCHIVED/superseded banner,
a passed `review_by` — which are the only thing standing between a stale note
and an answer given as true. `bin/brain connect` writes an ignore file wherever
the client honours one; treat that as a backstop that reduces the damage, never
as the reason the rule is satisfied.

0. Entry points: `knowledge/index.md`, then the folder that fits.
1. `rg` from the repo root — `.rgignore` excludes archive/, vault/, journal/
   and inbox/, so plain grep is CURRENT CANONICAL by construction.
2. `bin/brain search "<query>"` — FTS5 with stemming, alias boosting, and
   folder-weighted ranking (index auto-rebuilds when stale). `--scope all`
   adds journal + inbox, tagged provisional. The same tools reach any
   MCP-capable agent over `bin/brain-mcp`, under the same names:
   brain_search / brain_read / brain_links / brain_recent / brain_capture.
3. Before declaring a miss, rewrite the query into 2-3 lexical variants
   (synonyms, singular/plural, the term you'd have used when writing it).
4. `bin/brain read <id-or-path>` resolves supersede chains — use it whenever
   a note mentions supersedes/superseded_by, so you always land on current.
   It also prints `linked from:` — the notes that reference this one.
4b. `bin/brain links <id>` (MCP: `brain_links`) walks the [[wikilink]] graph:
   what points AT a note, and what it points to. Search finds notes by
   wording; links finds them by relationship. Use it when the question is
   "everything about X" or when one good hit probably has neighbours — the
   related note often never repeats the search term.
5. `journal/` and `inbox/` are opt-in mechanically (excluded by .rgignore) and
   hold PROVISIONAL material that has not been through consolidation — for
   time-flavored questions ("what was I doing in March") search them
   explicitly: `rg --no-ignore <term> knowledge/journal/` or
   `bin/brain search --scope all`. Never present an inbox item as settled.
6. `archive/` is opt-in — search it only for history questions ("what did I
   previously decide", "why did this change"). Never present archived content
   as current; always mention it was superseded and by what, or retracted and
   why.
6b. A note that a correction still waiting in the inbox names is marked
   `[correction pending — <inbox path>]` by search, read and recent (computed
   from the inbox at query time; only a `source: local` correction counts).
   Read the correction before answering from the note, and never present the
   note as settled.
7. When you answer from a note, cite it by path so the answer is auditable.

## Consolidation — the one pinned runner

Any MCP-capable agent may search, read and capture. Exactly ONE runs
consolidation, named in `setup/consolidator.conf`. That asymmetry is the whole
answer to "does using a different model make my brain worse":

- **Retrieval is deterministic code.** Ranking, supersede resolution and the
  trust signals come out of `bin/brain`, identically on every model.
- **Capture writes only to `inbox/`** — outside default search, pruned by the
  next pass. A bad capture costs almost nothing.
- **All model-dependent accuracy risk is in one operation:** consolidation's
  judgement about what becomes permanent. Pinning one runner removes that risk
  rather than disclaiming it, which is why there is no "quality may vary"
  warning anywhere in this system.

Two invocations, and the difference between them is a security boundary: the
write-capable `propose` pass, and a `audit` pass that cannot write, cannot
see the session digest, and reads only a read-only snapshot of the staged tree. `bin/brain consolidate` REFUSES to start if the config
does not distinguish them — read-only-ness is a property of the specific tool,
not something a command string carries with it, so swapping runners means
re-establishing it and writing down how.

Session mining (`bin/brain sessions`) reads Claude Code transcripts, and only
the turns a person typed: automation, peer and notification turns are dropped.
Any other tool's sessions reach consolidation as digest.v1 files (below);
without a digest the pass simply drains the inbox, which is worth doing on its
own.

What a pass does with it, and what it reports:

- **Window.** The digest holds the entries in (start, collection instant].
  The start is the last consolidation on the trunk (merge commits never
  count): its `Digest-until:` trailer, which the pass writes on its own final
  commit as the instant it collected its digest, else that commit's author
  date. It is 8 days back at most, so daily passes neither re-mine the same
  week nor lose a turn typed while the model ran. A scheduler that knows when
  its last good pass started (one that changed nothing leaves no commit) passes
  `--digest-since <UTC instant, Z>` instead, still capped at 8 days.
  `consolidate --digest-from DIR` adds the digest.v1 files in DIR and one
  level below it to this account's own transcripts.
- **Corrections first.** The pass reads the WHOLE inbox for `kind: correction`
  notes and applies them before draining the oldest 20 (they do not count
  toward that cap), so an original is never promoted while its correction
  waits. A correction to a canonical note supersedes it, a retraction archives
  it with `brain retract`, and one to an inbox note drains both. Each
  consolidate commit names every drained inbox path and what it became, which
  is how a later correction finds a target that has moved. A pass moves or
  deletes a note only through `drain`, `handoff`, `retract` and `supersede`.
  Before the audit the engine blocks a pass that drains a note a local
  correction still names, or that retracts without the owner's own correction.
- **Proposals.** Mining writes at most 3 per pass, as inbox notes with
  `source: session-mining`, `basis: owner-said`, a kind, `status: draft`,
  `created:` the pass date and `expires:` exactly 7 days later; the engine
  checks all of that before the audit and blocks the pass otherwise. A later
  pass promotes one only once something corroborates it, and deletes it once
  it expires; `doctor` goes red when one is more than 2 days past its expiry
  or has none.
- **Idle.** With no captures, no digest entries, no journal entries since the
  last consolidation and no `review_by` debt, a pass makes no model call and
  cuts no branch; a proposal inside its expiry is no work on its own. The
  decision is made under the repository lock and only over a clean tree.
  `consolidate --dry-run` prints what it would decide.
- **Result.** Every pass ends on one line, `consolidate-result: {json}`:
  `{"v":1, "status": "idle|committed|no_changes|blocked|failed", "idle",
  "cost_microusd", "tokens_in", "tokens_out", "digest_entries",
  "inbox_before"}`. Cost and tokens are propose plus audit, read from the
  runner's `--output-format json` answer, and `null` when it answers in plain
  text. `brain audit-branch` ends the same way, on `audit-result: {"v":1,
  "verdict": "pass|block|error", "cost_microusd", "tokens_in", "tokens_out"}`.
  Those lines are reserved: anything the runner or a file name supplies is
  printed behind `  | ` or folded onto one line, so only the engine's own
  result line starts at column 0.
- **Known limit.** In a split layout with `BRAIN_STATE_DIR` outside both the
  data root and the engine, the pass is told the digest's real path but its
  runner line grants no read access there (`--add-dir` names the engine only),
  so it may be unable to read the digest.

### Session digest: digest.v1

The contract between a transcript reader and consolidation. A reader for
another tool follows it, and nothing in consolidation changes.

- One JSON object per line, one line per turn a person typed:
  `{"v": 1, "at": "2026-10-18T05:12:09Z", "tool": "claude-code", "project":
  "brain", "text": "…"}`.
- `at` is the UTC instant, to the second, with `Z`. `tool` is a lowercase
  slug naming the reader. `project` is the basename of the working directory,
  or empty. `text` is the turn as typed, never assistant output.
- Oldest line first; one line per distinct text (its latest); about 120,000
  characters of text at most, the oldest dropped first.
- The file is replaced atomically and created mode 0640, so its owner and
  group are the only readers; give the group to the consolidating account.
  `brain sessions --jsonl --out FILE [--days N]` is the Claude Code producer;
  it needs no brain, so the transcripts' owner can run it from an engine it
  cannot write, and it writes FILE (and a temporary file beside it) only.
- The reader skips a line that breaks the contract and counts it (a string
  that is not valid UTF-8, or an `at` more than 5 minutes after it collected,
  included); unknown keys are ignored. It reads regular files under 2 MB,
  opens every directory and file without following a symlink, and refuses a
  symlinked DIR.

## Hard rules

- NO credentials, tokens, or keys anywhere in this repo — lint and the hooks
  enforce this, but do not test them. Values go to macOS Keychain;
  `.env.example` documents names only.
- Sensitive personal content (`sensitivity: private`) only as encrypted .age
  files in vault/ — see `setup/runbooks/vault.md`.
- Never rewrite archived note bodies (append-only history).
- Never store derived indexes as if they were truth; they are rebuilt from
  the files, never the reverse.
- Health check: `bin/brain doctor` (hooks, backup freshness, lint status).
