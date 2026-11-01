# `brain contradictions` only reads, and its judge is held to the auditor's confinement

Status: decided 2026-11-01 (release 0.7.0).

The engine had no way to notice that two current notes disagree. `brain contradictions`
finds candidate pairs without a model (an in-memory FTS5 search from each note's title and
topics, kept only where the two notes share a topic or project, at most 20 a run), has one
confined call judge them, and prints one result line.

Why it writes nothing in the brain: the first thing anyone does with a contradiction report is
decide what to do about it, and that decision is a person's or a consolidation pass's. A check
that also edits, commits or rebuilds the index would need the same review as consolidation and
could be run from any account that can read the notes. So it uses its own in-memory index
instead of the on-disk one, skips the hook installation and the tree lock that every other
command takes, asks git for its history without optional locks, and keeps what it remembers
(which pairs it judged, against both notes' content hashes, and where its rotation stands) in a
`--state` file that must live outside the brain.

Why the judge has the audit line's confinement, checked by the same function: it reads note
text, and note text is data that may try to instruct it. Read as its only tool, a snapshot
holding only the pair notes, no MCP, nothing that prompts, nothing kept: the worst a hostile
note can do is skew a verdict. The `contradict` line is optional in `consolidator.conf`
because consolidation never reads it; the command refuses to run without one.

Rejected: reusing the on-disk search index (it rebuilds itself when stale, which is a write);
a model call per pair (cost grows with the pairs, and the pairs share notes); comparing every
note with every other (quadratic, and almost all of it noise); recording `contradicts` pairs
as unjudged so they keep coming back (they would fill the 20 slots until someone edited a
note, hiding every other pair; a changed note is what makes a pair be asked again).

Limit: the candidates are the pairs search surfaces, so two notes that disagree in different
words, with no shared topic or project, are not found; and the judge can be wrong either way.
