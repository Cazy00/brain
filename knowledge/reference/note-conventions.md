---
id: note-conventions
kind: reference
title: How to write a note that stays findable
topics: [brain, conventions, retrieval]
aliases: [frontmatter schema, note format, writing rules, findability]
created: 2026-07-25
status: current
review_by: 2027-07-25
---

## What

The write-time conventions that make retrieval precise. The system enforces
them (`bin/brain lint`, pre-commit hook); this note explains them so a human
or agent understands WHY each exists.

## Details

- One fact or decision per file, with a stable descriptive filename. A search
  hit should BE the answer, not a haystack containing it.
- Fill `aliases` with 2-5 terms future-you might search. Vocabulary drift —
  asking in 2030 words for a note written in 2026 words — is the biggest
  long-term precision threat, and aliases are the cheapest counter.
- `topics` must come from `knowledge/topics.yaml`. Adding a topic is a
  deliberate one-line commit; free-form tags fragment and silently break
  scoped search.
- Absolute dates only in bodies. "2026-07-22" stays meaningful forever.
- When a fact changes, supersede — never edit meaning in place, never delete.
  The old note gets `status: superseded`, `superseded_by`, a banner line, and
  moves to `archive/`. History is preserved; default search stays clean.
- Quick thoughts go to `inbox/` via `bin/brain capture` — zero friction now,
  structured during consolidation later.

## Sources

- The full protocol lives in `AGENTS.md` at the repo root — this note is
  one worked example of what it asks for.
