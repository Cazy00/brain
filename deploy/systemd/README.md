# systemd units for the remote brain

One-shot units invoked by timers on the host, each running the maintenance
profile of the compose project. They exist here rather than as `docker compose`
restart policies because they are *scheduled work against the same repository
the server writes into*, and they are serialised against it by the repository
lock on the shared state mount — not by Docker.

Install and enable them per `setup/runbooks/remote-brain.md`. Nothing here is
enabled by copying it: `systemctl enable --now <timer>` is a deliberate act, and
the runbook says which ones to enable when.

## What each unit watches, and the one blind spot they share

| Unit | Sees | Cannot see |
|---|---|---|
| `brain-backup` | the archive built, encrypted, uploaded, pruned | whether it can be restored |
| `brain-restore-drill` | that it can be restored | anything between drills |
| `brain-maintenance` | index, content, backup age, disk, unpushed commits | anything outside the box |
| `brain-edge-check` | the Cloudflare side: expiring service tokens, drifted applications, policies, DNS | whether the brain itself is answering |

The blind spot is the same for the first three: they run ON the VPS, so they go
quiet in exactly the failure where silence is worst — the box being gone. That
is what `brain-edge-check` and the Cloudflare notification policies it
reconciles are for. Between them, an outage is either reported by the host or
reported by Cloudflare, and never by neither.

`brain-edge-check` runs `provision.py --check`, which is a dry run that is
allowed to have an opinion: it writes nothing and exits non-zero on what it
finds. A plain dry run exits 0 whatever it sees, so scheduling that would have
produced a green timer on the morning a headless credential lapsed.

## Why there is no consolidation timer

There was one, and it was removed on 2026-08-23 rather than shipped.

`setup/consolidator.conf` pins BOTH invocations of the consolidation pass to the
`claude` CLI — that pin is the whole reason the pass is trustworthy, because it
is the single operation in this system where a model decides what becomes
permanent knowledge. The production image installs `ca-certificates`, `git` and
`openssh-client` and nothing else. So a consolidate unit would have run
`bin/brain consolidate` inside a container with no runner, failed every week,
and — because a timer that fails quietly is indistinguishable from a timer that
has nothing to do — left the inbox draining never while appearing scheduled.

Shipping a unit that cannot work is worse than shipping none: it converts a
known gap into an unknown one. The three real options are written down here so
the next person does not have to re-derive them:

1. **Add the runner to the image.** It needs a provider credential inside the
   container, which changes the image's threat model — the component holding
   the brain would gain an outbound authenticated channel to a third party.
   That deserves its own decision, not a line in a Dockerfile.
2. **Run it on the host, outside the container**, against the same data root
   with `BRAIN_DATA_ROOT` set. The host already has the CLI authenticated. This
   is the smallest change and the most likely answer, but it means the
   repository lock is now shared across a container boundary and a host process,
   which is exactly the interleaving the lock exists to prevent — so it needs a
   live concurrency test before it is trusted.
3. **Leave consolidation manual** until the inbox is big enough to be worth
   automating. `bin/brain doctor` already goes RED on an inbox over 25 items or
   an oldest item past 21 days, so the gap is visible rather than silent.

Until one is chosen, consolidation is a manual step and `doctor` is the alarm.
