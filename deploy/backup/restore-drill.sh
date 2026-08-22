#!/usr/bin/env bash
# deploy/backup/restore-drill.sh — proving the backups are real.
#
# An untested backup is a belief, not a durability layer. This script is the
# monthly test: it takes the newest encrypted archive out of object storage,
# restores it somewhere the live system cannot see, and makes the restored copy
# answer the questions a brain has to answer — does it lint, does the index
# rebuild, is the git history whole, do a known note id and a known query still
# come back with their trust markers. Then it destroys the copy and keeps only
# the verdict.
#
# TWO MODES, because of where the decryption identity lives.
#
# The whole point of `age -R` in snapshot.sh is that nothing on the VPS can
# decrypt an archive the VPS produced. That property is worth more than an
# automated drill, so it is not weakened here: a drill identity kept on the VPS
# would hand any attacker who reaches the box a plaintext copy of every
# recovery point.
#
#   --verify-only  (the default, and what the VPS timer runs)
#       Downloads the newest archive, proves byte-for-byte that the bucket
#       holds what was uploaded, audits retention and freshness, and checks
#       how long it has been since a real restore. It cannot prove the
#       plaintext, and it says so in every line of its output.
#
#   --full         (run where the offline identity is — the operator's
#                   machine, or an isolated recovery host)
#       The real drill. Decrypt, restore, lint, index, git, canaries, destroy.
#       Its verdict is uploaded to the bucket, which is how --verify-only on
#       the VPS knows whether a full drill has happened recently and can go red
#       when one has not.
#
# ISOLATION FROM LIVE DATA is structural, not a promise:
#
#   1. The live roots are resolved into LIVE_DATA_ROOT / LIVE_STATE_DIR and
#      then used for exactly one purpose — asserting that the scratch directory
#      is not inside them and they are not inside it. They are passed to
#      nothing.
#   2. Every invocation of the toolbelt and of git goes through drill_exec(),
#      the only place in this file that runs either. In container mode the sole
#      bind mount is the scratch directory, so the live tree is not in the
#      process's filesystem at all. In local mode the environment is emptied
#      with `env -i` and rebuilt with BRAIN_DATA_ROOT / BRAIN_STATE_DIR
#      pointing into the scratch directory, so an ambient BRAIN_DATA_ROOT in
#      the operator's shell cannot leak in.
#   3. Where the kernel allows it, the whole script re-executes inside a mount
#      namespace in which the live tree is bind-mounted read-only. A bug that
#      got past 1 and 2 would then fail with EROFS instead of writing.
#   4. The only rm -rf in the file refuses any path that is not the scratch
#      directory it created.
#
# EXIT CODES:
#   0   drill passed (see the verdict line for full vs verify-only)
#   64  usage error
#   69  a required tool or path is missing
#   70  THE DRILL FAILED — the archive did not restore, or the restore was bad
#   75  no full drill in too long, or a transient storage failure
#   78  offsite storage is not configured yet

set -euo pipefail
umask 077

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib-s3.sh
. "$SELF_DIR/lib-s3.sh"

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

say()  { printf '%s\n' "$*"; }
note() { printf '  [-- ] %s\n' "$*"; }
ok()   { printf '  [ok ] %s\n' "$*"; }
bad()  { printf '  [RED] %s\n' "$*" >&2; FAILURES=$((FAILURES + 1)); }
die()  { local c="$1"; shift; printf 'restore-drill: %s\n' "$*" >&2; exit "$c"; }

FAILURES=0

CONF_DIR="${CREDENTIALS_DIRECTORY:-/etc/brain}"
BACKUP_CONF="${BRAIN_BACKUP_CONF:-$CONF_DIR/backup.env}"
REPORT_DIR="${BRAIN_DRILL_REPORT_DIR:-/var/lib/brain/drills}"
SCRATCH_BASE="${BRAIN_DRILL_SCRATCH:-/var/tmp}"

# Resolved to be asserted against. Nothing downstream receives them.
LIVE_DATA_ROOT="${BRAIN_DATA_ROOT:-/srv/brain/data}"
LIVE_STATE_DIR="${BRAIN_STATE_DIR:-/srv/brain/state}"

# How the restored brain is executed. The container form is preferred on the
# VPS because it is also the thing recovery actually does — if the pinned image
# cannot read a restored archive, that is the finding.
DRILL_IMAGE="${BRAIN_DRILL_IMAGE:-}"
DRILL_ENTRYPOINT="${BRAIN_DRILL_ENTRYPOINT:-/opt/brain/bin/brain}"
DRILL_ENGINE="${BRAIN_DRILL_ENGINE:-}"

# A full drill older than this makes verify-only go red. Two months: long
# enough that a missed month is not an incident, short enough that a broken
# archive is found while the previous good one is still inside retention.
MAX_FULL_DRILL_AGE_DAYS="${BRAIN_DRILL_MAX_AGE_DAYS:-60}"

MODE=verify-only
IDENTITY="${BRAIN_DRILL_IDENTITY:-}"

while [ $# -gt 0 ]; do
    case "$1" in
        --full) MODE=full ;;
        --verify-only) MODE=verify-only ;;
        --identity) shift; IDENTITY="${1:-}" ;;
        -h|--help) sed -n '2,50p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die 64 "unknown argument: $1" ;;
    esac
    shift
done

# ------------------------------------------------------------------ preflight

for tool in curl openssl tar date; do
    command -v "$tool" >/dev/null 2>&1 || die 69 "$tool is not installed"
done

if [ "$MODE" = full ]; then
    command -v age >/dev/null 2>&1 || die 69 "age is not installed"
    [ -n "$IDENTITY" ] || die 64 "--full needs --identity <age-identity-file> (or BRAIN_DRILL_IDENTITY)"
    [ -r "$IDENTITY" ] || die 69 "cannot read identity file: $IDENTITY"
    # The identity must not live where the service lives. This is not
    # tidiness: an identity under /etc/brain or /srv/brain is one a compromise
    # of the brain host would obtain, and at that moment every archive in the
    # bucket becomes readable. Refusing here is what keeps "the VPS cannot read
    # its own backups" true after somebody takes a shortcut at 3am.
    IDENTITY_ABS="$(cd "$(dirname "$IDENTITY")" && pwd)/$(basename "$IDENTITY")"
    case "$IDENTITY_ABS" in
        /etc/brain/*|/srv/brain/*|/run/secrets/*)
            die 64 "refusing an identity stored at $IDENTITY_ABS — the recovery identity must stay off the brain host" ;;
    esac
    if [ -z "$DRILL_IMAGE" ] && [ -z "$DRILL_ENGINE" ]; then
        die 64 "--full needs an engine: set BRAIN_DRILL_IMAGE=<pinned image> or BRAIN_DRILL_ENGINE=<engine checkout>"
    fi
    if [ -n "$DRILL_ENGINE" ]; then
        [ -x "$DRILL_ENGINE/bin/brain" ] || die 69 "no executable $DRILL_ENGINE/bin/brain"
        command -v git >/dev/null 2>&1 || die 69 "git is not installed"
    fi
fi

mkdir -p "$REPORT_DIR" "$SCRATCH_BASE"
# Resolve the base to its physical path BEFORE creating anything under it. On
# macOS /var/tmp is a symlink to /private/var/tmp, so a scratch path resolved
# afterwards would no longer be prefixed by the base — and destroy_scratch,
# which refuses to delete anything outside it, would refuse to clean up.
SCRATCH_BASE="$(cd "$SCRATCH_BASE" && pwd)"
WORK="$(mktemp -d "$SCRATCH_BASE/brain-drill.XXXXXXXX")"
WORK="$(cd "$WORK" && pwd)"

# Assertion 1: the scratch directory and the live tree are disjoint. Checked
# before anything is created inside either.
case "$WORK/" in "$LIVE_DATA_ROOT"/*|"$LIVE_STATE_DIR"/*) die 70 "scratch $WORK is inside the live tree" ;; esac
case "$LIVE_DATA_ROOT/" in "$WORK"/*) die 70 "live data root is inside the scratch directory" ;; esac

destroy_scratch() {
    # The only rm -rf in this file, and it refuses anything that is not the
    # directory mktemp handed us.
    case "$WORK" in
        "$SCRATCH_BASE"/brain-drill.*) rm -rf "$WORK" ;;
        *) printf 'refusing to remove %s\n' "$WORK" >&2 ;;
    esac
}
trap destroy_scratch EXIT

# Assertion 3: re-exec with the live tree mounted read-only, when the kernel
# permits it. Best effort by design — this hardens the other assertions, it
# does not replace them, and a drill that cannot run at all is worse than one
# running with three guards instead of four.
if [ "${BRAIN_DRILL_NS:-}" != "1" ] && [ -d "$LIVE_DATA_ROOT" ] \
   && command -v unshare >/dev/null 2>&1 && [ "$(id -u)" = "0" ]; then
    # `sh -c` here is dash on Ubuntu, so the body is POSIX: no ${@:3}.
    if BRAIN_DRILL_NS=1 unshare --mount --propagation private -- sh -c '
            live="$1"; shift
            mount --bind "$live" "$live" 2>/dev/null || exit 97
            mount -o remount,bind,ro "$live" 2>/dev/null || exit 97
            exec "$@"
        ' _ "$LIVE_DATA_ROOT" "$0" "$@"; then
        exit 0
    else
        rc=$?
        [ "$rc" = 97 ] || exit "$rc"
        note "mount namespace unavailable — continuing with environment isolation only"
    fi
fi

# ------------------------------------------------------------------ the boundary

# drill_exec <brain|git> <args...>
#
# THE ONLY place this script runs the toolbelt or git. Everything the drill
# learns about the restored copy comes through here, which is why isolation can
# be read off the structure: there is one door, and the live roots are not
# behind it.
drill_exec() {
    local what="$1"; shift
    if [ -n "$DRILL_IMAGE" ]; then
        local entry="$DRILL_ENTRYPOINT"
        if [ "$what" = git ]; then entry=/usr/bin/git; fi
        docker run --rm --network none --read-only \
            --user "$(id -u):$(id -g)" \
            --mount "type=bind,source=$WORK,target=/drill" \
            --tmpfs /tmp:rw,nosuid,nodev,size=64m \
            --env BRAIN_DATA_ROOT=/drill/data \
            --env BRAIN_STATE_DIR=/drill/state \
            --env HOME=/drill/home \
            --env GIT_CONFIG_GLOBAL=/dev/null \
            --env GIT_CONFIG_SYSTEM=/dev/null \
            --entrypoint "$entry" "$DRILL_IMAGE" "$@"
    else
        local prog="$DRILL_ENGINE/bin/brain"
        if [ "$what" = git ]; then prog="$(command -v git)"; fi
        env -i \
            PATH=/usr/local/bin:/usr/bin:/bin \
            HOME="$WORK/home" \
            BRAIN_DATA_ROOT="$WORK/data" \
            BRAIN_STATE_DIR="$WORK/state" \
            GIT_CONFIG_GLOBAL=/dev/null \
            GIT_CONFIG_SYSTEM=/dev/null \
            "$prog" "$@"
    fi
}

# The restored repository's path AS THE SANDBOX SEES IT. In container mode the
# scratch directory is bound at /drill and the host path does not exist inside;
# passing $WORK/data there would fail with a confusing "not a repository".
if [ -n "$DRILL_IMAGE" ]; then SANDBOX_DATA=/drill/data; else SANDBOX_DATA="$WORK/data"; fi
drill_git() { drill_exec git -C "$SANDBOX_DATA" "$@"; }

# `stat -c` is GNU-only and the full drill is expected to run on the operator's
# macOS machine, where the recovery identity lives. `wc -c` is POSIX everywhere.
file_bytes() { wc -c < "$1" | tr -d ' '; }

# ------------------------------------------------------------------ fetch

s3_load_config "$BACKUP_CONF" || {
    say ""
    say "Offsite storage is not configured, so there is nothing to drill."
    say "See setup/runbooks/remote-brain.md, procedure 1, step 9."
    exit 78
}

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
START_EPOCH="$(date -u +%s)"
say "brain restore drill ($MODE) $STARTED"

NEWEST="$(s3_list "$S3_PREFIX/daily/" | grep -v '\.meta\.json$' | tail -1 || true)"
[ -n "$NEWEST" ] || die 75 "no archive under $S3_PREFIX/daily/ — has snapshot.sh ever succeeded?"
note "newest archive: $NEWEST"

# Freshness. The spec's alert is "encrypted backup older than 24 hours"; 36
# hours here so a drill run shortly before the nightly timer does not report a
# backup gap that does not exist.
ARCHIVE_STAMP="$(printf '%s' "$NEWEST" | sed -n 's|.*/brain-\(.*\)\.tar\.gz\.age$|\1|p')"
ARCHIVE_EPOCH="$(iso_epoch "$ARCHIVE_STAMP")"
if [ "$ARCHIVE_EPOCH" -gt 0 ] && [ $((START_EPOCH - ARCHIVE_EPOCH)) -gt 129600 ]; then
    bad "newest archive is $(( (START_EPOCH - ARCHIVE_EPOCH) / 3600 ))h old — the nightly backup is not running"
else
    ok "newest archive is fresh"
fi

s3_get "$NEWEST" "$WORK/archive.age" || die 75 "could not download $NEWEST"
GOT_BYTES="$(file_bytes "$WORK/archive.age")"
GOT_SHA="$(_s3_sha256_hex "$WORK/archive.age")"

if s3_get "$NEWEST.meta.json" "$WORK/sidecar.json"; then
    # Tolerant of whitespace around the colons. snapshot.sh writes this file
    # compactly, but the check must not turn "somebody reformatted the JSON"
    # into a shouted integrity failure — the two have completely different
    # remedies, and only one of them is an emergency.
    json_field() { sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([0-9a-zA-Z]*\).*/\1/p" "$WORK/sidecar.json" | head -1; }
    WANT_SHA="$(json_field sha256)"
    WANT_BYTES="$(json_field bytes)"
    WANT_HEAD="$(json_field git_head)"
    if [ -z "$WANT_SHA" ] || [ -z "$WANT_BYTES" ]; then
        bad "the sidecar for $NEWEST is unreadable — integrity is unproven, but the archive itself may be fine"
    elif [ "$GOT_SHA" = "$WANT_SHA" ] && [ "$GOT_BYTES" = "$WANT_BYTES" ]; then
        ok "bytes in the bucket match what snapshot.sh uploaded ($GOT_BYTES bytes)"
    else
        bad "INTEGRITY MISMATCH: stored sha256 $GOT_SHA / $GOT_BYTES bytes, expected $WANT_SHA / $WANT_BYTES"
    fi
else
    WANT_HEAD=""
    bad "no sidecar for $NEWEST — integrity cannot be proven from the bucket alone"
fi

# The age header is the cheapest evidence that the object is an age file at all
# and not a truncated upload or an error page stored under the right name.
if head -c 32 "$WORK/archive.age" | grep -q 'age-encryption.org/v1'; then
    ok "archive carries an age v1 header"
else
    bad "archive does not begin with an age v1 header"
fi

# Retention audit. Counts, not contents — this is the check that catches a
# prune loop that has quietly stopped running or started running twice.
for pair in "daily 7" "weekly 4" "monthly 12"; do
    set -- $pair
    n="$(s3_list "$S3_PREFIX/$1/" | grep -vc '\.meta\.json$' || true)"
    if [ "$n" -gt "$2" ]; then
        bad "$1: $n recovery points, retention says $2 — pruning is not running"
    else
        ok "$1: $n/$2 recovery points"
    fi
done

# ------------------------------------------------------------------ verify-only exit

if [ "$MODE" != full ]; then
    LAST_FULL="$(s3_list "$S3_PREFIX/drills/" | tail -1 || true)"
    LAST_FULL_STAMP="$(printf '%s' "$LAST_FULL" | sed -n 's|.*/drill-\(.*\)\.json$|\1|p')"
    LAST_FULL_EPOCH="$(iso_epoch "$LAST_FULL_STAMP")"
    AGE_DAYS=$(( LAST_FULL_EPOCH > 0 ? (START_EPOCH - LAST_FULL_EPOCH) / 86400 : 99999 ))
    say ""
    if [ "$AGE_DAYS" -gt "$MAX_FULL_DRILL_AGE_DAYS" ]; then
        say "VERDICT: verify-only, and OVERDUE."
        # Three different reasons produce this verdict and they are not the same
        # problem: nobody has ever run one, nobody has run one lately, or the
        # verdict objects have stopped being readable. Saying which is the
        # difference between a five-minute fix and an hour of guessing.
        if [ -z "$LAST_FULL" ]; then
            say "No full restore drill has ever published a verdict to this bucket."
        elif [ "$LAST_FULL_EPOCH" = "0" ]; then
            say "The newest verdict object ($LAST_FULL) has an unreadable date."
        else
            say "The bytes are intact. Nobody has proven in $AGE_DAYS days that they restore."
        fi
        say "Run the full drill where the offline identity lives:"
        say "    setup/runbooks/remote-brain.md, procedure 10."
        exit 75
    fi
    [ "$FAILURES" -eq 0 ] || { say "VERDICT: verify-only, $FAILURES problem(s)."; exit 70; }
    say "VERDICT: verify-only PASSED. Last full restore drill: ${AGE_DAYS}d ago."
    say "This proves the bucket holds the bytes that were uploaded. It does NOT"
    say "prove they decrypt into a working brain — only procedure 10 does that."
    exit 0
fi

# ------------------------------------------------------------------ full drill

mkdir -p "$WORK/data" "$WORK/state" "$WORK/home"
age -d -i "$IDENTITY" "$WORK/archive.age" > "$WORK/archive.tar.gz" \
    || die 70 "DECRYPT FAILED — this identity cannot open the archive"
ok "decrypted"

tar -xzf "$WORK/archive.tar.gz" -C "$WORK/data" || die 70 "EXTRACT FAILED"
rm -f "$WORK/archive.tar.gz" "$WORK/archive.age"

# The metadata rides one level up, out of the restored data root, so that a
# restored tree is byte-identical to what /srv/brain/data held and can be moved
# straight into place during a real recovery.
if [ -d "$WORK/data/RESTORE-METADATA-DELETE-BEFORE-RSYNC" ]; then
    mv "$WORK/data/RESTORE-METADATA-DELETE-BEFORE-RSYNC" "$WORK/meta"
    ok "manifest: $(sed -n 's/^git_head: *//p' "$WORK/meta/manifest.txt")"
else
    bad "no RESTORE-METADATA-DELETE-BEFORE-RSYNC/ in the archive — the manifest is missing"
fi
[ -d "$WORK/data/.git" ] || die 70 "restored tree has no .git — this is not a brain repository"
[ -d "$WORK/data/knowledge" ] || die 70 "restored tree has no knowledge/ — this is not a brain repository"

MAN_HEAD="$(sed -n 's/^git_head: *//p' "$WORK/meta/manifest.txt" 2>/dev/null || echo '')"
MAN_COMMITS="$(sed -n 's/^git_commits: *//p' "$WORK/meta/manifest.txt" 2>/dev/null || echo 0)"
MAN_NOTES="$(sed -n 's/^notes: *//p' "$WORK/meta/manifest.txt" 2>/dev/null || echo 0)"

# ---- git history --------------------------------------------------------
if drill_git fsck --no-progress --no-dangling >/dev/null 2>&1; then
    ok "git fsck clean"
else
    bad "git fsck reported problems in the restored history"
fi
GOT_HEAD="$(drill_git rev-parse HEAD 2>/dev/null || echo none)"
GOT_COMMITS="$(drill_git rev-list --count HEAD 2>/dev/null || echo 0)"
if [ -n "$MAN_HEAD" ] && [ "$GOT_HEAD" = "$MAN_HEAD" ]; then
    ok "HEAD matches the manifest ($GOT_COMMITS commits)"
else
    bad "HEAD is $GOT_HEAD, manifest says ${MAN_HEAD:-unknown}"
fi
if [ -n "$WANT_HEAD" ] && [ "$GOT_HEAD" != "$WANT_HEAD" ]; then
    bad "HEAD does not match the sidecar's git_head ($WANT_HEAD)"
fi
if [ "$GOT_COMMITS" -lt "$MAN_COMMITS" ]; then
    bad "restored history has $GOT_COMMITS commits, manifest recorded $MAN_COMMITS"
fi
DIRTY="$( { drill_git status --porcelain 2>/dev/null || true; } | wc -l | tr -d ' ')"
[ "$DIRTY" = "0" ] || note "$DIRTY uncommitted path(s) in the restored tree (expected if a capture was mid-flight)"

# ---- lint ---------------------------------------------------------------
if drill_exec brain lint > "$WORK/lint.out" 2>&1; then
    ok "lint clean"
else
    rc=$?
    # Exit 3 is lint's documented "the toolchain is broken", not "the notes are
    # bad". Distinguishing them matters: one means restore a different archive,
    # the other means fix the drill host.
    if [ "$rc" = 3 ]; then
        bad "lint could not run (exit 3) — the drill environment is broken, the archive is unjudged"
    else
        bad "lint found content errors in the restored brain (exit $rc): $(head -3 "$WORK/lint.out" | tr '\n' ' ')"
    fi
fi

# ---- index rebuild ------------------------------------------------------
INDEX_START="$(date -u +%s)"
if drill_exec brain index --quiet >/dev/null 2>&1; then
    ok "index rebuilt in $(( $(date -u +%s) - INDEX_START ))s"
else
    bad "index rebuild failed — search would be unavailable after a real restore"
fi

# ---- canaries -----------------------------------------------------------
#
# Deterministic, and derived from the restored data rather than from a list
# somebody has to maintain: the lexicographically first decision note. Its id
# and title are whatever the archive holds, so this works on the first drill
# after a fresh migration and keeps working ten years later. An operator who
# wants specific notes exercised can put them in /etc/brain/drill-canaries.txt,
# one `<id>` per line — that file is private because a note id is knowledge.
CANARY_IDS=""
if [ -r "$CONF_DIR/drill-canaries.txt" ]; then
    CANARY_IDS="$(grep -vE '^\s*(#|$)' "$CONF_DIR/drill-canaries.txt" || true)"
fi
if [ -z "$CANARY_IDS" ]; then
    FIRST_NOTE="$(find "$WORK/data/knowledge/decisions" -name '*.md' -type f 2>/dev/null | sort | head -1 || true)"
    if [ -n "$FIRST_NOTE" ]; then CANARY_IDS="$(sed -n 's/^id: *//p' "$FIRST_NOTE" | head -1)"; fi
fi

CANARY_PASS=0
CANARY_TOTAL=0
if [ -z "$CANARY_IDS" ]; then
    note "no canary available — the restored brain holds no decision notes"
else
    while IFS= read -r cid; do
        [ -n "$cid" ] || continue
        CANARY_TOTAL=$((CANARY_TOTAL + 3))
        if drill_exec brain read "$cid" 2>/dev/null | grep -qF "$cid"; then
            CANARY_PASS=$((CANARY_PASS + 1))
        else
            bad "canary read failed for one note id"
        fi
        if drill_exec brain links "$cid" >/dev/null 2>&1; then
            CANARY_PASS=$((CANARY_PASS + 1))
        else
            bad "canary links failed for one note id"
        fi
        cfile="$( { grep -rl "^id: $cid$" "$WORK/data/knowledge" 2>/dev/null || true; } | head -1)"
        ctitle="$( { sed -n 's/^title: *//p' "$cfile" 2>/dev/null || true; } | head -1)"
        if [ -n "$ctitle" ] && drill_exec brain search "$ctitle" --limit 8 2>/dev/null | grep -qF "$cid"; then
            CANARY_PASS=$((CANARY_PASS + 1))
        else
            bad "canary search did not return its own note — ranking or the index is wrong"
        fi
        # The trust boundary is itself a canary. Default-scope search must not
        # surface archive/ or inbox/; if a restore quietly changed that, every
        # answer this brain gives afterwards is superseded or provisional
        # material presented as current.
        if [ -n "$ctitle" ]; then
            CANARY_TOTAL=$((CANARY_TOTAL + 1))
            if drill_exec brain search "$ctitle" --limit 20 2>/dev/null \
                    | grep -qE 'knowledge/(archive|vault)/'; then
                bad "default-scope search leaked archive/ or vault/ content"
            else
                CANARY_PASS=$((CANARY_PASS + 1))
            fi
        fi
    done <<< "$CANARY_IDS"
    if [ "$CANARY_PASS" = "$CANARY_TOTAL" ]; then ok "canaries $CANARY_PASS/$CANARY_TOTAL"; fi
fi

RESTORED_NOTES="$(find "$WORK/data/knowledge" -name '*.md' -type f | wc -l | tr -d ' ')"

# ------------------------------------------------------------------ verdict

DURATION=$(( $(date -u +%s) - START_EPOCH ))
VERDICT=pass
[ "$FAILURES" -eq 0 ] || VERDICT=fail
REPORT="$REPORT_DIR/drill-$(date -u +%Y-%m-%dT%H%M%SZ).json"

# Result metadata ONLY. No note id, no title, no query, no path from inside the
# restored brain — the same rule brainlib/eventlog.py enforces on the server
# log, for the same reason: this file outlives the drill and may end up in a
# handback.
cat > "$REPORT" <<JSON
{"schema":1,"mode":"full","started":"$STARTED","duration_s":$DURATION,
 "archive":"$NEWEST","archive_bytes":$GOT_BYTES,"archive_sha256":"$GOT_SHA",
 "git_head_matched":$([ "$GOT_HEAD" = "$MAN_HEAD" ] && echo true || echo false),
 "commits":$GOT_COMMITS,"notes_restored":$RESTORED_NOTES,"notes_expected":$MAN_NOTES,
 "canaries_passed":$CANARY_PASS,"canaries_total":$CANARY_TOTAL,
 "failures":$FAILURES,"verdict":"$VERDICT"}
JSON
ok "report $REPORT"

# Uploaded so the VPS — which cannot run a full drill — can see that one
# happened. This is the only write this script makes to the bucket.
if s3_put "$REPORT" "$S3_PREFIX/drills/drill-$(date -u +%Y-%m-%dT%H%M%SZ).json"; then
    ok "verdict published to the bucket"
else
    note "could not publish the verdict — verify-only drills will start calling this overdue"
fi

destroy_scratch
trap - EXIT
if [ ! -d "$WORK" ]; then ok "restored copy destroyed"; else bad "scratch directory survived: $WORK"; fi

say ""
if [ "$VERDICT" = pass ]; then
    say "VERDICT: full restore drill PASSED ($RESTORED_NOTES notes, $GOT_COMMITS commits, ${DURATION}s)."
    exit 0
fi
say "VERDICT: full restore drill FAILED — $FAILURES problem(s). The backups are"
say "not proven. Do not rely on them until this is resolved:"
say "    setup/runbooks/remote-brain.md, procedure 6."
exit 70
