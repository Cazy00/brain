# shellcheck shell=bash
# Recovery-point timestamps and the retention audit. Sourced by restore-drill.sh;
# no network, no globals but the ones named below, so tests can source it alone.

# YYYY-MM-DDTHHMMSSZ -> epoch seconds, or 0 when it cannot be read.
#
# Two implementations because this script runs in two places: the VPS (GNU
# coreutils) and the operator's macOS machine, where the recovery identity
# lives and the full drill therefore has to run. `date -d` does not exist on
# BSD, and the failure is silent — it would report every archive as undated and
# every full drill as never having happened, which is an alarm that cries wolf
# forever.
iso_epoch() {
    [ -n "${1:-}" ] || { printf '0\n'; return; }
    local out
    out="$(date -u -d "$(printf '%s' "$1" | sed 's/T\(..\)\(..\)\(..\)Z/ \1:\2:\3/')" +%s 2>/dev/null || true)"
    [ -n "$out" ] || out="$(date -u -j -f '%Y-%m-%dT%H%M%SZ' "$1" +%s 2>/dev/null || true)"
    printf '%s\n' "${out:-0}"
}

# How long past the bucket's object lock a surplus point may sit before it is a
# finding. snapshot.sh prunes once a night, so a point whose lock lapsed just
# after the last prune is legitimately still there until the next one.
RETENTION_PRUNE_INTERVAL_SECS=86400

# retention_audit <class> <keep> <lock-days> <now-epoch>  < keys, oldest first
#
# Prints one line, `ok|bad <text>`, and returns 0 for ok, 1 for bad.
#
# Keys are the object keys of a class, sorted, `.meta.json` sidecars already
# removed. Their names embed a UTC timestamp, so sorted order is age order and
# the surplus is the OLDEST (count - keep) of them — exactly what snapshot.sh's
# prune_class tries to delete.
#
# More than `keep` points is not a failure by itself: the bucket's object lock
# refuses to delete a point younger than the lock window (snapshot.sh logs
# "still under its retention lock — not prunable yet"), so a burst of extra
# backups, such as brain-upgrade's pre-upgrade one, outlives the count for a few
# days. It is a failure only when a SURPLUS point is older than the lock plus one
# prune interval, because then the lock cannot be why it is still there. An
# unreadable or future timestamp counts as old: nothing may excuse a point we cannot date.
retention_audit() {
    local class="$1" keep="$2" lock_days="$3" now="$4"
    local keys n surplus=0 old=0 key stamp epoch limit
    keys="$(cat)"
    n="$(printf '%s\n' "$keys" | grep -c . || true)"
    if [ "$n" -le "$keep" ]; then
        printf 'ok %s: %s/%s recovery points\n' "$class" "$n" "$keep"
        return 0
    fi
    limit=$(( 10#$lock_days * 86400 + RETENTION_PRUNE_INTERVAL_SECS ))
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        [ "$surplus" -lt $((n - keep)) ] || break
        surplus=$((surplus + 1))
        stamp="$(printf '%s' "$key" | sed -n 's|.*/brain-\(.*\)\.tar\.gz\.age$|\1|p')"
        epoch="$(iso_epoch "$stamp")"
        # A stamp in the future (this clock behind the one that made it) cannot
        # be shown to be young, so it is treated like an unreadable one.
        if [ "$epoch" -le 0 ] || [ "$epoch" -gt $((now + 3600)) ] || [ $((now - epoch)) -gt "$limit" ]; then
            old=$((old + 1))
        fi
    done <<< "$keys"
    if [ "$old" -gt 0 ]; then
        printf 'bad %s: %s recovery points, retention says %s, and %s of the %s surplus are older than the %s-day lock plus one prune night — pruning is not running\n' \
            "$class" "$n" "$keep" "$old" "$surplus" "$lock_days"
        return 1
    fi
    printf 'ok %s: %s recovery points, %s still under the %s-day lock\n' "$class" "$n" "$surplus" "$lock_days"
}
