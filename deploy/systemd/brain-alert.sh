#!/bin/sh
# deploy/systemd/brain-alert.sh — what happens when a brain timer fails.
#
# Invoked by brain-alert@.service with the failing unit's name. It is a file
# rather than an inline ExecStart= for a mechanical reason worth writing down:
# systemd expands `$NAME` inside Exec lines itself, so a shell fragment that
# uses positional parameters or its own variables gets them replaced with empty
# strings before /bin/sh ever sees them. An alert that silently logs blanks is
# worse than no alert.
#
# It records, and it delegates. It does not deliver — see brain-alert@.service
# for why there is no transport here.
set -eu

unit="${1:-unknown.unit}"
log=/var/lib/brain/alerts.log

result="$(systemctl show -p Result --value "$unit" 2>/dev/null || echo unknown)"
code="$(systemctl show -p ExecMainStatus --value "$unit" 2>/dev/null || echo '?')"

# One line, one incident, in the order a 3am reader wants it: when, what,
# and the exit code that indexes the runbook. snapshot.sh and restore-drill.sh
# both document their codes in their own headers.
line="$(date -u +%Y-%m-%dT%H:%M:%SZ) $unit result=$result exit=$code"
mkdir -p /var/lib/brain
printf '%s\n' "$line" >> "$log"
printf '%s\n' "$line"        # and to the journal, via systemd's stdout capture

# The optional delivery hook. Root-owned, written by the operator, given the
# unit name and the exit code. Its failure is not this script's failure: the
# log line above is the durable record, and a broken webhook must not also
# destroy the evidence that something else broke.
hook="${BRAIN_ALERT_HOOK:-/usr/local/lib/brain/alert}"
if [ -x "$hook" ]; then
    "$hook" "$unit" "$code" "$result" || printf 'alert hook failed: %s\n' "$hook" >&2
fi
exit 0
