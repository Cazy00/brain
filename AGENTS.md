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
  archive/        superseded notes, mirror tree — OUT of default search
  attachments/    small binaries, 1MB hard cap per file
setup/            templates, runbooks (this system's own docs)
                  consolidate-prompt.md writes notes; audit-prompt.md is the
                  independent, digest-blind, write-less check on that work
                  consolidator.conf names the ONE agent allowed to run them
bin/brain         toolbelt: setup | connect | new | capture | search | read |
                  links | recent | supersede | index | sessions | consolidate |
                  schedule | template | publish | plugin | lint | doctor |
                  stats | reset
                  (`bin/brain --help` is authoritative; doctor reports whether
                  the machinery works, stats whether the KNOWLEDGE still does)
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
`supersedes`, `superseded_by`, `sensitivity` (required in people/ and life/:
normal | personal; `private` content goes to vault/, never plaintext),
`visibility` (public | private — see below), `source` (which endpoint accepted
a capture; on inbox notes).

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
   lint and blocks bad content; post-commit auto-pushes to the private remote
   (never a consolidate/ branch — those wait for the audit).
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
   as current; always mention it was superseded and by what.
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
write-capable `propose` pass, and a `audit` pass that cannot write and cannot
see the session digest. `bin/brain consolidate` REFUSES to start if the config
does not distinguish them — read-only-ness is a property of the specific tool,
not something a command string carries with it, so swapping runners means
re-establishing it and writing down how.

Session mining (`bin/brain sessions`) reads Claude Code transcripts only. Every
other runner simply gets no digest, which the consolidation prompt already
handles as a normal case — the inbox drain is worth doing on its own.

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
