#!/usr/bin/env bash
# deploy/backup/snapshot.sh — the fourth durability layer.
#
# Layers one to three all live inside the running system: atomically written
# files, a local git commit for every accepted mutation, and an automatic push
# to the private git remote. They protect against the software being wrong.
# They do not protect against the ACCOUNT being lost — a deleted repository, a
# stolen deploy token used to force-push, a VPS that is compromised and pushes
# whatever the attacker wants. Every one of those failures propagates through
# layers one to three at machine speed.
#
# This layer survives them because its output is encrypted to an identity that
# has never been on this machine and lands with a different vendor. Nothing
# running on the VPS can read a single one of these archives, and nothing
# running on the VPS can produce a plaintext one.
#
# Normally run by deploy/systemd/brain-backup.service, nightly. Safe to run by
# hand at any time; it takes the same repository lock the brain takes, so it
# can never photograph a half-written commit.
#
# EXIT CODES are the interface — brain-alert@.service reports them and the
# runbook is indexed by them:
#
#   0   archive built, uploaded, and verified by an independent read-back
#   64  usage error
#   69  a required tool or path is missing
#   70  archive built and kept locally, but the upload or its verification failed
#   73  the archive could not be built
#   75  the repository lock was held too long — transient, the timer retries
#   77  REFUSED: something credential-shaped was about to enter the archive
#   78  offsite storage is not configured yet (archive built and kept locally)
#
# Note what 70 and 78 have in common: the local encrypted archive is written
# and kept either way, and the success stamp is NOT written either way. Losing
# the offsite copy must never also lose tonight's data, and a failed upload
# must never be able to look like a healthy backup.

set -euo pipefail
umask 077

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=deploy/backup/lib-s3.sh
. "$SELF_DIR/lib-s3.sh"

# ------------------------------------------------------------------ presentation

say()  { printf '%s\n' "$*"; }
note() { printf '  [-- ] %s\n' "$*"; }
ok()   { printf '  [ok ] %s\n' "$*"; }
warn() { printf '  [warn] %s\n' "$*" >&2; }
die()  { local c="$1"; shift; printf 'snapshot: %s\n' "$*" >&2; exit "$c"; }

# ------------------------------------------------------------------ configuration
#
# Under systemd the two root-owned config files arrive through LoadCredential=,
# which copies them into a per-invocation directory readable by the service
# user and nothing else. That is what lets this script run as uid 10001 — the
# same uid that owns the data — with an empty capability set, instead of as
# root with DAC override. Run by hand as root, $CREDENTIALS_DIRECTORY is unset
# and /etc/brain is read directly.
CONF_DIR="${CREDENTIALS_DIRECTORY:-/etc/brain}"
BACKUP_CONF="${BRAIN_BACKUP_CONF:-$CONF_DIR/backup.env}"
HTTP_CONF="${BRAIN_HTTP_CONFIG:-$CONF_DIR/brain-http.json}"

DATA_ROOT="${BRAIN_DATA_ROOT:-/srv/brain/data}"
STATE_DIR="${BRAIN_STATE_DIR:-/srv/brain/state}"
LOCAL_DIR="${BRAIN_BACKUP_LOCAL_DIR:-/srv/brain/backups}"
RECIPIENTS="${BRAIN_BACKUP_RECIPIENTS:-/etc/brain/backup-recipients.txt}"

# Retention, from the spec. Applied per class, and only ever after tonight's
# object has been verified to exist.
KEEP_DAILY=7
KEEP_WEEKLY=4
KEEP_MONTHLY=12

# 4 GiB. A single-shot PUT tops out at 5 GiB and this file has no multipart
# implementation on purpose; refusing with 3.5 hours of headroom is better than
# discovering the ceiling on the night the archive crosses it.
MAX_ARCHIVE_BYTES=$((4 * 1024 * 1024 * 1024))

LOCK_WAIT="${BRAIN_BACKUP_LOCK_WAIT:-900}"
DRY_RUN=no

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=yes ;;
        -h|--help) sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die 64 "unknown argument: $1" ;;
    esac
    shift
done

# ------------------------------------------------------------------ preflight

for tool in tar age openssl curl git flock date; do
    command -v "$tool" >/dev/null 2>&1 || die 69 "$tool is not installed"
done
[ -d "$DATA_ROOT/.git" ] || die 69 "$DATA_ROOT is not a git repository (is the data mount present?)"
[ -r "$RECIPIENTS" ] || die 69 "no age recipients file at $RECIPIENTS"
# An empty recipients file makes `age -R` encrypt to nobody, and the failure is
# silent enough that it would be discovered during a restore.
grep -qE '^[[:space:]]*age1' "$RECIPIENTS" || die 69 "$RECIPIENTS contains no age recipient"
mkdir -p "$LOCAL_DIR"

STAMP="$(date -u +%Y-%m-%dT%H%M%SZ)"
DOW="$(date -u +%u)"     # 1..7, 7 = Sunday
DOM="$(date -u +%d)"
ARCHIVE="$LOCAL_DIR/brain-$STAMP.tar.gz.age"
STAGE="$(mktemp -d "$LOCAL_DIR/.staging.XXXXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

say "brain snapshot $STAMP"

# ------------------------------------------------------- configuration metadata
#
# "The minimum configuration metadata needed to rebuild, but no plaintext
# secret." The distinction drawn here is between a value that IDENTIFIES the
# deployment and a value that AUTHENTICATES to it. Audiences, hostnames, the
# owner address, mount points and image digests are the first kind: without
# them a replacement VPS cannot be rebuilt to match, and every one of them is
# already inside an archive that only an offline identity can open. Tunnel
# credentials, the S3 secret, the git deploy key and anything under
# /run/secrets are the second kind, and they are rotated during recovery
# anyway — carrying them would add risk and buy nothing.
# The directory name IS the warning, and it is long on purpose. This holds
# brain-http.json -- the owner address, both Access audiences, the team domain --
# and it sits at the archive root beside knowledge/ and .git/. The restore path a
# tired operator reaches for is `rsync -a extracted/ /srv/brain/data/`, which
# would commit the deployment configuration into the knowledge repository and
# push it to the private remote. A short name like "meta" relies on remembering
# an --exclude; this one cannot be typed past without reading it.
META="$STAGE/RESTORE-METADATA-DELETE-BEFORE-RSYNC"
mkdir -p "$META"

if [ -r "$HTTP_CONF" ]; then
    cp "$HTTP_CONF" "$META/brain-http.json"
else
    warn "no $HTTP_CONF — the archive will not describe the endpoint configuration"
fi

HEAD_COMMIT="$(git -C "$DATA_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
COMMIT_COUNT="$(git -C "$DATA_ROOT" rev-list --count HEAD 2>/dev/null || echo 0)"
NOTE_COUNT="$(git -C "$DATA_ROOT" ls-files -- 'knowledge/*.md' 2>/dev/null | wc -l | tr -d ' ')"
GIT_REMOTE="$(git -C "$DATA_ROOT" remote get-url origin 2>/dev/null || echo 'LOCAL ONLY, no remote')"
BRANCH="$(git -C "$DATA_ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

{
    echo "# brain disaster-recovery archive"
    echo "created:        $STAMP"
    echo "schema:         1"
    echo "data_root:      $DATA_ROOT"
    echo "state_dir:      $STATE_DIR   (derived, NOT in this archive)"
    echo "git_branch:     $BRANCH"
    echo "git_head:       $HEAD_COMMIT"
    echo "git_commits:    $COMMIT_COUNT"
    echo "git_remote:     $GIT_REMOTE"
    echo "notes:          $NOTE_COUNT"
    echo "data_uid_gid:   $(stat -c '%u:%g' "$DATA_ROOT" 2>/dev/null || echo unknown)"
    echo "recipients_sha: $(_s3_sha256_hex "$RECIPIENTS")"
    echo
    echo "# Deliberately absent: every secret. Recovery rotates them all —"
    echo "# see setup/runbooks/remote-brain.md, procedure 5 and procedure 8."
    echo "absent:         tunnel credentials, S3 keys, git deploy key, /run/secrets/*"
    echo
    echo "# To restore:"
    echo "#   age -d -i <offline-identity> brain-$STAMP.tar.gz.age | tar -xz -C <empty-dir>"
    echo "#   rm -rf <empty-dir>/RESTORE-METADATA-DELETE-BEFORE-RSYNC"
    echo "#"
    echo "# That second line is not optional: this directory holds the deployment"
    echo "# configuration, not knowledge, and copying it back into the data root"
    echo "# would commit the endpoint audiences and the owner address into the brain."
    echo "# Then follow setup/runbooks/remote-brain.md procedure 5."
} > "$META/manifest.txt"

# Fail closed before anything is encrypted. This scan is the last gate between
# a mis-edited /etc/brain and a permanent, replicated copy of a credential; it
# looks for a long opaque value assigned to a secret-shaped name, and exempts
# filesystem paths because brain-http.json legitimately POINTS at secret files.
if grep -RInE '(secret|token|password|private[_-]?key|BEGIN [A-Z ]*PRIVATE KEY)"?[[:space:]]*[:=][[:space:]]*"?[A-Za-z0-9+/=_-]{16,}' "$META" \
        | grep -vE '[:=][[:space:]]*"?/' >/dev/null; then
    die 77 "a credential-shaped value is present in the staged metadata under $META — nothing was written"
fi

# ------------------------------------------------------------------ build

# The SAME lock bin/brain's repo_lock takes (state_dir()/git.lock), by exact
# path and not by a lock of our own: two lock files protecting one repository
# is the same as no lock at all. A timeout is transient by definition — a long
# consolidation run, or a capture waiting on the push queue — so it exits 75
# and lets the timer come back rather than failing the night's backup.
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/git.lock"
flock -w "$LOCK_WAIT" -x 9 || die 75 "repository lock still held after ${LOCK_WAIT}s — retrying next timer"

set +e
tar --numeric-owner -C "$STAGE" -cz "RESTORE-METADATA-DELETE-BEFORE-RSYNC" -C "$DATA_ROOT" . 2>"$STAGE/tar.err" \
    | age -R "$RECIPIENTS" -o "$STAGE/archive.age"
status=("${PIPESTATUS[@]}")
set -e
flock -u 9 || true

# Both halves, individually. `set -o pipefail` alone would tell us the pipeline
# failed; it would not tell a 3am reader whether the data could not be read or
# the recipients file was wrong, and those have completely different remedies.
[ "${status[0]}" -eq 0 ] || die 73 "tar failed (rc=${status[0]}): $(head -3 "$STAGE/tar.err" 2>/dev/null)"
[ "${status[1]}" -eq 0 ] || die 73 "age failed (rc=${status[1]}) — check $RECIPIENTS"

BYTES="$(stat -c %s "$STAGE/archive.age")"
[ "$BYTES" -gt 0 ] || die 73 "the encrypted archive is empty"
[ "$BYTES" -lt "$MAX_ARCHIVE_BYTES" ] || die 73 \
    "archive is $BYTES bytes, above the ${MAX_ARCHIVE_BYTES}-byte single-PUT ceiling — prune knowledge/attachments/ or add multipart upload"

# Rename within the same filesystem: a reader can never see a partial archive
# under the real name, so "newest file in $LOCAL_DIR" is always a complete one.
mv "$STAGE/archive.age" "$ARCHIVE"
SHA="$(_s3_sha256_hex "$ARCHIVE")"
ok "archive $ARCHIVE ($BYTES bytes, $NOTE_COUNT notes, $COMMIT_COUNT commits)"

cat > "$STAGE/sidecar.json" <<JSON
{"schema":1,"created":"$STAMP","bytes":$BYTES,"sha256":"$SHA","format":"tar.gz.age","git_head":"$HEAD_COMMIT"}
JSON
# The sidecar is the only unencrypted object, and it holds integrity fields
# ONLY — no counts, no ids, no hostnames. It exists so the VPS, which by design
# cannot decrypt anything it uploads, can still prove monthly that the bytes in
# the bucket are the bytes it wrote.

if [ "$DRY_RUN" = yes ]; then
    note "--dry-run: local archive kept, nothing uploaded, retention untouched"
    exit 0
fi

# ------------------------------------------------------------------ upload

if ! s3_load_config "$BACKUP_CONF"; then
    say ""
    say "OFFSITE BACKUP IS NOT CONFIGURED."
    say "Tonight's encrypted archive is on this machine only:"
    say "    $ARCHIVE"
    say "That is one disk. It is not a backup."
    say ""
    say "Fix it: create the R2 (or any S3-compatible) bucket, then write"
    say "/etc/brain/backup.env, mode 0600, root-owned, with:"
    say "    BRAIN_BACKUP_ENDPOINT=https://<ACCOUNT_ID>.r2.cloudflarestorage.com"
    say "    BRAIN_BACKUP_BUCKET=<BUCKET>"
    say "    BRAIN_BACKUP_REGION=auto"
    say "    BRAIN_BACKUP_PREFIX=brain"
    say "    BRAIN_BACKUP_ACCESS_KEY_ID=<KEY_ID>"
    say "    BRAIN_BACKUP_SECRET_ACCESS_KEY=<SECRET>"
    say "Then: sudo systemctl start brain-backup.service"
    say "Full procedure: setup/runbooks/remote-brain.md, procedure 1, step 9."
    exit 78
fi

# `if`, not `[ ... ] && ...`: under `set -e` a top-level AND-list whose test
# fails takes the whole script down, and six days a week that test fails.
CLASSES="daily"
if [ "$DOW" = "7" ]; then CLASSES="$CLASSES weekly"; fi
if [ "$DOM" = "01" ]; then CLASSES="$CLASSES monthly"; fi

upload_verified() {  # upload_verified <class>
    local class="$1"
    local key="$S3_PREFIX/$class/brain-$STAMP.tar.gz.age"
    s3_put "$ARCHIVE" "$key" || return 1
    # Read the object back rather than trusting the 200. A truncated upload
    # that the gateway still acknowledges is exactly the "partial upload
    # reported as success" this script is required never to produce, and the
    # only way to rule it out is to ask the store what it now holds.
    local head len etag
    head="$(s3_head "$key")" || return 1
    len="${head%% *}"; etag="${head#* }"
    [ "$len" = "$BYTES" ] || {
        printf 'VERIFY FAILED %s: stored %s bytes, sent %s\n' "$key" "$len" "$BYTES" >&2
        return 1
    }
    # A single-shot PUT stores the MD5 as the ETag. Checked only when it looks
    # like one: a gateway that returns some other opaque token is not evidence
    # of corruption, and treating it as such would make backups fail on a
    # provider change rather than on a real problem.
    case "$etag" in
        [0-9a-f][0-9a-f]*)
            if [ "${#etag}" -eq 32 ]; then
                local md5; md5="$(openssl dgst -md5 "$ARCHIVE" | awk '{print $NF}')"
                [ "$etag" = "$md5" ] || {
                    printf 'VERIFY FAILED %s: ETag %s != md5 %s\n' "$key" "$etag" "$md5" >&2
                    return 1
                }
            fi ;;
    esac
    s3_put "$STAGE/sidecar.json" "$key.meta.json" || return 1
    ok "uploaded and verified $key"
    return 0
}

UPLOAD_OK=yes
for class in $CLASSES; do
    upload_verified "$class" || { UPLOAD_OK=no; break; }
done

if [ "$UPLOAD_OK" != yes ]; then
    say ""
    say "The archive exists and is intact locally:  $ARCHIVE"
    say "The offsite copy DID NOT complete. Retention was not touched, and the"
    say "success stamp was not written, so doctor and the alert timer both stay"
    say "red until an upload verifies."
    say "Diagnose: setup/runbooks/remote-brain.md, procedure 1, step 9."
    exit 70
fi

# ------------------------------------------------------------------ retention
#
# Pruning happens only here — after a verified upload — so a bad night can
# never both fail to add a recovery point and delete an old one.

prune_class() {  # prune_class <class> <keep>
    local class="$1" keep="$2" keys n i key
    keys="$(s3_list "$S3_PREFIX/$class/" | grep -v '\.meta\.json$' || true)"
    n="$(printf '%s\n' "$keys" | grep -c . || true)"
    [ "$n" -gt "$keep" ] || { note "$class: $n/$keep recovery points"; return 0; }
    i=0
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        i=$((i + 1))
        [ "$i" -le $((n - keep)) ] || break
        # Belt and braces on a delete: only a key this script's own naming
        # scheme could have produced is ever removed, so a stray object in the
        # bucket is left alone rather than silently destroyed.
        case "$key" in
            "$S3_PREFIX/$class/brain-"*"Z.tar.gz.age") ;;
            *) warn "skipping unrecognised object: $key"; continue ;;
        esac
        s3_delete "$key" && note "pruned $key"
        s3_delete "$key.meta.json" >/dev/null 2>&1 || true
    done <<< "$keys"
}

prune_class daily "$KEEP_DAILY"
prune_class weekly "$KEEP_WEEKLY"
prune_class monthly "$KEEP_MONTHLY"

# Local copies are a convenience for a fast restore, not a backup layer. Three
# is enough to survive noticing a problem a day or two late.
# `|| true` on the pipeline, not decoration: with pipefail set, an `ls` that
# matches nothing would take the whole script down one line before the success
# stamp is written — turning a healthy backup into a red alert.
( ls -1t "$LOCAL_DIR"/brain-*.tar.gz.age 2>/dev/null || true ) | tail -n +4 | while IFS= read -r old; do
    rm -f "$old" && note "removed local $old"
done

# ------------------------------------------------------------------ stamp
#
# Written last, and only on the success path. bin/brain doctor and the alerting
# rule ("encrypted backup older than 24 hours") both read this file, so writing
# it early — or writing it on a partial run — would turn the loudest alarm in
# the system into a lie.
mkdir -p "$STATE_DIR"
cat > "$STATE_DIR/backup-last-success.json" <<JSON
{"schema":1,"completed":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","stamp":"$STAMP","bytes":$BYTES,"sha256":"$SHA","classes":"$CLASSES","git_head":"$HEAD_COMMIT"}
JSON
ok "backup complete: $CLASSES"
