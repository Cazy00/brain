# The HTTP server keeps 180 days and 25 MB of log history per stream

Status: decided 2026-10-31 (release 0.6.2).

The HTTP server writes two logs, a runtime event stream and a mutation audit
stream. Runtime event retention went from 30 days to 180, and each log's size cap
from 5 MB to 25 MB. A log keeps one rolled generation beside the live file, so a
log occupies about 50 MB at most (about 100 MB for the two). The audit stream
already kept 180 days and keeps them.

Why: with 30 days an operator could not look back over months when
troubleshooting or tuning a limit, because the evidence had expired before the
question came up. The larger cap keeps the window from being cut short by volume
before the days run out.

What a runtime event holds: the event name and time, a correlation id, the
principal (an HMAC, not an address or email), the profile, endpoint and mode,
the method and tool name, the outcome, status and timing, and the caller's
self-reported client name and version. The fields come from a fixed list in
`httplog.py`, and a field outside it raises rather than being written, so search
queries, note text, request paths and credentials have no field to land in and
are not logged. The audit stream additionally records the repository-relative
inbox path of a capture and its commit.

Cost: disk space, and a longer-lived record of who called what and when. The
principal is a keyed hash and the client name is the client's own claim, so the
record identifies a credential and a program, not a person, but it is kept six
times as long. The logs stay under the state directory, outside the knowledge
repository, and never reach git.

Rejected: keeping 30 days and raising the cap only (the age limit was the part
that cut history short); keeping an unbounded log (a retention window is not a
size limit, and a disk fills on whichever comes first).
