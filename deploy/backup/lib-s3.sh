# deploy/backup/lib-s3.sh — S3-compatible object storage in POSIX-ish bash.
#
# Sourced by snapshot.sh and restore-drill.sh. Never executed directly.
#
# WHY THIS EXISTS INSTEAD OF THE AWS CLI
#
# The brief allowed either. The AWS CLI was rejected, and the reason is not
# taste:
#
#   * It is not on the box. Installing it means either a snap (which
#     auto-refreshes on Canonical's schedule — an unattended, unpinned code
#     change inside the one process that holds the offsite backup credential)
#     or pip, which puts a Python dependency tree next to a service whose whole
#     claim is that it has none.
#   * Its credential resolution chain has many inputs — environment,
#     ~/.aws/*, container metadata, and instance metadata. This is an Oracle
#     Cloud instance with a live metadata endpoint; a tool that silently probes
#     for credentials we did not give it is the wrong tool for a script whose
#     failure mode must be "loud", not "used something unexpected and
#     succeeded".
#   * A backup script must keep working ten years from now with no maintenance.
#     `curl` and `openssl` are part of the base system and their interfaces have
#     been stable for a decade. The AWS CLI has already had one breaking major
#     version.
#
# The cost is this file: about a hundred lines of SigV4. That is a real cost,
# but it is a bounded and auditable one, and its failure mode is a signature
# mismatch — a 403 with a message, never a silent partial success.
#
# SCOPE, deliberately narrow: single-shot PUT, HEAD, GET, LIST and DELETE.
# There is no multipart upload. A brain that needs multipart is a brain whose
# archive has grown past 5 GiB, and snapshot.sh refuses at 4 GiB with an
# actionable error rather than growing this file into an S3 client.

# Empty-body SHA-256. Constant, and spelled out rather than recomputed, because
# every non-PUT request signs it.
S3_EMPTY_SHA256=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

_s3_hex_of_stdin() { od -An -tx1 -v | tr -d ' \n'; }

# Both take the LAST field of openssl's output. `openssl dgst` has printed
# "SHA2-256(stdin)= <hex>" and "SHA256(stdin)= <hex>" across versions; the hex
# has always been last. Parsing by position rather than by prefix is what makes
# this survive the next rename.
_s3_sha256_hex() { openssl dgst -sha256 "$@" | awk '{print $NF}'; }
_s3_hmac_hex() { openssl dgst -sha256 -mac HMAC -macopt "hexkey:$1" | awk '{print $NF}'; }

# s3_load_config <config-file>
#
# Populates S3_ENDPOINT / S3_BUCKET / S3_REGION / S3_PREFIX and the credential.
# Returns 78 (EX_CONFIG) — never 0, never 1 — when offsite storage is not
# configured. The caller distinguishes "not set up yet" from "broken" on that
# code alone, which is the whole reason R2 not being enabled on the account can
# be reported as an actionable sentence instead of a stack trace.
s3_load_config() {
    local conf="$1"
    if [ ! -r "$conf" ]; then
        printf 'offsite backup is not configured: %s is missing or unreadable\n' "$conf" >&2
        return 78
    fi
    # `set -a` so the file may be plain KEY=value with no export keywords —
    # which is also what systemd EnvironmentFile= accepts, so one file works
    # for both and nobody has to remember which syntax this one wants.
    set -a
    # shellcheck disable=SC1090
    . "$conf"
    set +a

    S3_ENDPOINT="${BRAIN_BACKUP_ENDPOINT:-}"
    S3_BUCKET="${BRAIN_BACKUP_BUCKET:-}"
    S3_REGION="${BRAIN_BACKUP_REGION:-auto}"
    S3_PREFIX="${BRAIN_BACKUP_PREFIX:-brain}"
    S3_ACCESS_KEY="${BRAIN_BACKUP_ACCESS_KEY_ID:-}"
    S3_SECRET_KEY="${BRAIN_BACKUP_SECRET_ACCESS_KEY:-}"

    local missing=""
    [ -n "$S3_ENDPOINT" ] || missing="$missing BRAIN_BACKUP_ENDPOINT"
    [ -n "$S3_BUCKET" ] || missing="$missing BRAIN_BACKUP_BUCKET"
    [ -n "$S3_ACCESS_KEY" ] || missing="$missing BRAIN_BACKUP_ACCESS_KEY_ID"
    [ -n "$S3_SECRET_KEY" ] || missing="$missing BRAIN_BACKUP_SECRET_ACCESS_KEY"
    # A placeholder left in place is the expected state until the bucket is
    # created, so it is detected explicitly and reported as "not configured"
    # rather than being sent to the endpoint and coming back as a 403 the
    # operator then has to diagnose at 3am.
    case "$S3_ENDPOINT$S3_BUCKET$S3_ACCESS_KEY$S3_SECRET_KEY" in
        *"<"*">"*) missing="$missing (placeholders still present)" ;;
    esac
    if [ -n "$missing" ]; then
        printf 'offsite backup is not configured in %s — missing:%s\n' "$conf" "$missing" >&2
        return 78
    fi

    # Keys are constructed by this repo, never by a user, so they are always
    # drawn from a safe alphabet. Asserting that is what lets every request be
    # signed without a URI encoder: an un-encoded byte in a canonical URI is a
    # silent signature mismatch, and the assertion turns it into a refusal.
    case "$S3_PREFIX" in
        *[!A-Za-z0-9/._-]*) printf 'unsafe BRAIN_BACKUP_PREFIX: %s\n' "$S3_PREFIX" >&2; return 78 ;;
    esac
    S3_ENDPOINT="${S3_ENDPOINT%/}"
    return 0
}

# _s3_request <method> <canonical-uri> <canonical-query> <body-file|-> <out-file>
# Echoes the HTTP status code. Returns non-zero only when curl itself failed.
_s3_request() {
    local method="$1" uri="$2" query="$3" body="$4" out="$5"
    local amzdate datestamp host scope payload_hash creq sts
    amzdate="$(date -u +%Y%m%dT%H%M%SZ)"
    datestamp="${amzdate%%T*}"
    host="${S3_ENDPOINT#*://}"; host="${host%%/*}"
    scope="$datestamp/$S3_REGION/s3/aws4_request"

    if [ "$body" = "-" ]; then
        payload_hash="$S3_EMPTY_SHA256"
    else
        payload_hash="$(_s3_sha256_hex "$body")"
    fi

    creq="$(printf '%s\n%s\n%s\nhost:%s\nx-amz-content-sha256:%s\nx-amz-date:%s\n\nhost;x-amz-content-sha256;x-amz-date\n%s' \
        "$method" "$uri" "$query" "$host" "$payload_hash" "$amzdate" "$payload_hash" \
        | _s3_sha256_hex)"
    sts="AWS4-HMAC-SHA256
$amzdate
$scope
$creq"

    local k
    k="$(printf 'AWS4%s' "$S3_SECRET_KEY" | _s3_hex_of_stdin)"
    k="$(printf '%s' "$datestamp"  | _s3_hmac_hex "$k")"
    k="$(printf '%s' "$S3_REGION"  | _s3_hmac_hex "$k")"
    k="$(printf '%s' "s3"          | _s3_hmac_hex "$k")"
    k="$(printf '%s' "aws4_request"| _s3_hmac_hex "$k")"
    local sig
    sig="$(printf '%s' "$sts" | _s3_hmac_hex "$k")"

    local url="$S3_ENDPOINT$uri"
    [ -n "$query" ] && url="$url?$query"

    # -H 'Expect:' kills curl's 100-continue handshake. Some S3 gateways never
    # answer it and curl then stalls for a full second on every upload; worse,
    # a proxy that answers it with an error produces a "failure" with no body.
    # --retry is safe here because every method this file issues is idempotent.
    local -a args=(
        --silent --show-error
        --connect-timeout 15 --max-time "${S3_MAX_TIME:-900}"
        --retry 2 --retry-delay 5 --retry-connrefused
        -H "Expect:"
        -H "x-amz-date: $amzdate"
        -H "x-amz-content-sha256: $payload_hash"
        -H "Authorization: AWS4-HMAC-SHA256 Credential=$S3_ACCESS_KEY/$scope, SignedHeaders=host;x-amz-content-sha256;x-amz-date, Signature=$sig"
        -o "$out" -w '%{http_code}'
    )
    case "$method" in
        PUT)    args+=(--upload-file "$body") ;;
        HEAD)   args+=(--head) ;;
        DELETE) args+=(-X DELETE) ;;
        GET)    ;;
        *) printf 'unsupported method %s\n' "$method" >&2; return 64 ;;
    esac
    curl "${args[@]}" "$url"
}

_s3_key_uri() { printf '/%s/%s' "$S3_BUCKET" "$1"; }

# s3_put <local-file> <key>   — 0 only on 2xx.
s3_put() {
    local out code
    out="$(mktemp)"
    code="$(_s3_request PUT "$(_s3_key_uri "$2")" "" "$1" "$out")" || { rm -f "$out"; return 1; }
    case "$code" in
        2*) rm -f "$out"; return 0 ;;
        *) printf 'PUT %s -> HTTP %s\n%s\n' "$2" "$code" "$(head -c 512 "$out")" >&2
           rm -f "$out"; return 1 ;;
    esac
}

# s3_head <key> — echoes "<content-length> <etag>" on success.
s3_head() {
    local out code len etag
    out="$(mktemp)"
    code="$(_s3_request HEAD "$(_s3_key_uri "$1")" "" - "$out")" || { rm -f "$out"; return 1; }
    case "$code" in
        2*) ;;
        *) printf 'HEAD %s -> HTTP %s\n' "$1" "$code" >&2; rm -f "$out"; return 1 ;;
    esac
    # Lowercased first, because the default awk on Ubuntu is mawk and mawk has
    # no IGNORECASE. Header case is not ours to predict: HTTP/2 lowercases every
    # name, HTTP/1.1 gateways do not, and a case-sensitive match that happens to
    # work today is a verification step that silently stops verifying tomorrow.
    # Both values we read are case-insensitive anyway — a decimal and lowercase
    # hex — so folding the whole response is safe.
    tr '[:upper:]' '[:lower:]' < "$out" > "$out.lc"
    len="$(awk '/^content-length:/ {gsub(/\r/,""); print $2}' "$out.lc" | tail -1)"
    etag="$(awk '/^etag:/ {gsub(/[\r"]/,""); print $2}' "$out.lc" | tail -1)"
    rm -f "$out.lc"
    rm -f "$out"
    printf '%s %s\n' "${len:-0}" "${etag:-}"
}

# s3_get <key> <dest-file>
s3_get() {
    local code
    code="$(_s3_request GET "$(_s3_key_uri "$1")" "" - "$2")" || return 1
    case "$code" in
        2*) return 0 ;;
        *) printf 'GET %s -> HTTP %s\n' "$1" "$code" >&2; rm -f "$2"; return 1 ;;
    esac
}

# s3_delete <key>
#
# Three outcomes, not two, and the third is the point:
#
#   0  deleted
#   2  REFUSED by the bucket's object lock, and that is expected. The
#      credential this script holds can both write and delete, so a single
#      stolen key could otherwise erase every recovery point it just created.
#      The lock is what stops that, and it necessarily also stops a legitimate
#      prune of anything younger than the retention window. Reporting that as
#      a failure teaches an operator to ignore the one message that would
#      matter if the lock were ever removed.
#   1  anything else — a 403 from a credential that lost its permissions, a
#      500, a signature mismatch. Those are real and must not be filed under
#      the same heading as "working as designed".
s3_delete() {
    local out code
    out="$(mktemp)"
    code="$(_s3_request DELETE "$(_s3_key_uri "$1")" "" - "$out")" || { rm -f "$out"; return 1; }
    rm -f "$out"
    case "$code" in
        2*) return 0 ;;
        409) return 2 ;;
        *) printf 'DELETE %s -> HTTP %s\n' "$1" "$code" >&2; return 1 ;;
    esac
}

# s3_list <prefix> — one key per line, sorted. Sorted output is the whole
# retention mechanism: keys embed a UTC ISO-8601 timestamp, so lexical order IS
# chronological order and no date parsing is needed to find the newest or the
# oldest. Any renaming of the key format has to preserve that property.
s3_list() {
    local prefix="$1" out code query enc
    out="$(mktemp)"
    enc="$(printf '%s' "$prefix" | sed 's|/|%2F|g')"
    # Query parameters must be sorted by name for SigV4: list-type, max-keys,
    # prefix.
    query="list-type=2&max-keys=1000&prefix=$enc"
    code="$(_s3_request GET "/$S3_BUCKET" "$query" - "$out")" || { rm -f "$out"; return 1; }
    case "$code" in
        2*) ;;
        *) printf 'LIST %s -> HTTP %s\n' "$prefix" "$code" >&2; rm -f "$out"; return 1 ;;
    esac
    # A truncated listing means more than 1000 objects share a prefix, which
    # cannot happen under a 12-object retention policy — so it means something
    # else is writing into the bucket. Refuse rather than prune a partial view.
    if grep -q '<IsTruncated>true</IsTruncated>' "$out"; then
        printf 'LIST %s is truncated (>1000 objects) — refusing to reason about a partial listing\n' "$prefix" >&2
        rm -f "$out"; return 1
    fi
    tr '<' '\n' < "$out" | sed -n 's|^Key>||p' | sort
    rm -f "$out"
}
