#!/bin/sh
# brain container entrypoint — the preflight that decides whether this container
# is allowed to touch the owner's brain at all.
#
# The spec's failure table says an unavailable data volume must fail readiness
# and must NEVER result in an empty brain created over the missing mount. That
# rule is enforced here, first, before anything else can run: this script does
# not `git init`, does not `mkdir -p` the data root, and does not repair a
# repository it does not recognise. If the mount is absent or wrong, the right
# outcome is a container that refuses to start with a message a human can act
# on — not a service that comes up healthy and empty and starts accepting
# captures into a filesystem that will vanish on the next restart.
#
# Why here and not in the image: BRAIN_DATA_ROOT is a RUNTIME mount. Its
# ownership, its writability and whether it holds a brain at all are facts about
# this host on this boot, and none of them can be established at build time.
#
# Accepted commands (the container's whole interface — a deliberately closed
# set, so a compose typo cannot turn this image into a general shell on the
# brain's data):
#
#   serve            (default)  run the HTTP MCP service, bin/brain-http
#   brain <args...>             run one toolbelt subcommand and exit — this is
#                               what the one-shot brain-maintenance service uses
#                               for lint, index, doctor, consolidate, backup
#
# It is ALSO installed under two other names, and dispatches on the one it was
# invoked as: /usr/local/bin/brain-http and /usr/local/bin/brain. That is not
# convenience. deploy/compose.yaml states each service's entrypoint explicitly
# ("so that a change to the image's default ENTRYPOINT cannot silently
# repurpose a service"), which is right — but a compose `entrypoint:` override
# REPLACES the image's ENTRYPOINT, and an image whose preflight lives only in
# the default ENTRYPOINT would have all of the checks below silently skipped by
# exactly that wiring. Installing the preflight under the names compose already
# calls means both wirings run it, and neither has to know about the other.
# Nothing else is on PATH: /opt/brain/bin is deliberately NOT added, so there
# is no spelling of `entrypoint:` that reaches the toolbelt around this file.
set -eu

ENGINE=/opt/brain

# Hardcoded, not an environment variable, and that is on purpose: bin/brain
# resolves its own ENGINE from __file__ and refuses to take it from the
# environment, because resolving executable code through configuration is how a
# config change becomes arbitrary code execution. Introducing BRAIN_ENGINE here
# would quietly re-open exactly that door.

die() {
    echo "brain: FATAL: $*" >&2
    echo "brain: refusing to start. The brain has NOT been modified." >&2
    exit 1
}

warn() {
    echo "brain: WARNING: $*" >&2
}

# A real write, not `test -w`. On a read-only bind mount the permission bits can
# say yes while the filesystem says EROFS, and the difference only shows up at
# the first capture — which is the worst possible moment to discover it.
assert_writable() {
    _dir="$1"
    _what="$2"
    _probe="$_dir/.brain-write-probe.$$"
    # The subshell is load-bearing: redirections are applied left to right, so
    # `: > "$_probe" 2>/dev/null` prints the shell's own "Permission denied"
    # before the silencing redirection is in place, and the operator sees a raw
    # shell error instead of the message below saying which mount is at fault.
    if ! ( : > "$_probe" ) 2>/dev/null; then
        die "$_what is not writable by uid $(id -u): $_dir"
    fi
    rm -f "$_probe"
}

# ---------------------------------------------------------------------------
# 1. The toolchain the git hooks depend on.
#
# The build already asserted both of these, but PATH is settable at runtime and
# the failure mode is silent in the one direction that matters: .githooks/pre-commit
# runs `python3 "$BRAIN" lint --staged` and treats any exit code other than 1 as
# "the toolchain is broken, commit anyway". A missing python3 therefore does not
# block anything — it turns the content gate OFF, with no error and no log line.
# One `command -v` here is cheap insurance against a container started with a
# hand-edited environment.
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 \
    || die "no bare 'python3' on PATH — .githooks/pre-commit would fail OPEN and every commit would be unlinted"
command -v git >/dev/null 2>&1 \
    || die "no 'git' on PATH — a capture is only accepted once it is committed"

# ---------------------------------------------------------------------------
# 2. Git configuration, in a container-scoped file on the tmpfs.
#
# GIT_CONFIG_GLOBAL rather than ~/.gitconfig: it needs no writable HOME, it is
# unambiguously "global" scope, and it disappears with the container. That scope
# matters for one setting in particular — git ignores `safe.directory` unless it
# comes from protected configuration, precisely because an attacker who can
# write a repository can write that repository's own .git/config.
#
# safe.directory is needed even though the mount is owned by 10001: if the host
# ownership is ever wrong, git's refusal is "dubious ownership", which reads
# like a permissions bug and sends people looking in the wrong place. With this
# set, a genuinely unwritable mount fails at the writability probe below with a
# message that says so.
# ---------------------------------------------------------------------------
GIT_CONFIG_GLOBAL="${TMPDIR:-/tmp}/brain-gitconfig"
export GIT_CONFIG_GLOBAL
( : > "$GIT_CONFIG_GLOBAL" ) 2>/dev/null \
    || die "cannot write $GIT_CONFIG_GLOBAL — mount a tmpfs at ${TMPDIR:-/tmp}; the root filesystem is read-only by design"

data="${BRAIN_DATA_ROOT:-}"
[ -n "$data" ] || die "BRAIN_DATA_ROOT is unset — refusing to guess where the brain lives"

git config --global safe.directory "$data"

# A committer identity that can never be mistaken for a person and can never be
# mailed. RFC 2606 reserves .invalid precisely so an address like this cannot be
# delivered anywhere, which is what keeps a synthetic identity out of the
# owner's real correspondence — and keeps a real address out of a public image.
# Override per deployment if the private git host wants something specific.
git config --global user.name "${BRAIN_GIT_NAME:-brain}"
git config --global user.email "${BRAIN_GIT_EMAIL:-brain@brain.invalid}"

# Nothing here holds a signing key. An inherited /etc/gitconfig with signing
# turned on would fail every capture commit at the very end of the transaction,
# after the note is already on disk.
git config --global commit.gpgsign false

# core.hooksPath goes in the CONTAINER's global config, not in the data
# repository's own .git/config, and the difference is not cosmetic. /opt/brain
# exists only inside this image. Persisting that absolute path into the data
# repository would follow the notes onto the host, into a restored backup and
# into a recovery clone, where git would find no hooks directory and silently
# run no hooks at all — the content gate down, everywhere, forever, with no
# error. Setting it here also makes bin/brain's ensure_hooks() a no-op, so the
# service never writes to the data repository's config either.
test -x "$ENGINE/.githooks/pre-commit" \
    || die "$ENGINE/.githooks/pre-commit is missing or not executable — the content gate cannot run"
git config --global core.hooksPath "$ENGINE/.githooks"

# ---------------------------------------------------------------------------
# 3. The data root must already BE a brain. Never create one.
# ---------------------------------------------------------------------------
[ -d "$data" ] || die "BRAIN_DATA_ROOT does not exist: $data (is the bind mount attached?)"

if ! git -C "$data" rev-parse --git-dir >/dev/null 2>&1; then
    die "BRAIN_DATA_ROOT is not a git repository: $data — the mount is missing or wrong. Not creating one: an empty brain over a missing mount is worse than no service."
fi

# The repository must be rooted AT the mount, not inherited from a parent. A
# nested or mis-specified mount would otherwise commit the owner's notes into
# whatever repository happens to enclose them.
top=$(git -C "$data" rev-parse --show-toplevel 2>/dev/null || echo "")
real=$(cd "$data" && pwd -P)
[ "$top" = "$real" ] \
    || die "BRAIN_DATA_ROOT is inside a different git repository (toplevel: ${top:-unknown}) — refusing to write into it"

# Two independent "this is really a brain" tests, because an empty mount and a
# freshly `git init`ed one are exactly what this whole check exists to reject.
[ -d "$data/knowledge" ] \
    || die "no knowledge/ directory under $data — this is a git repository, but it is not the brain"
git -C "$data" rev-parse --verify --quiet HEAD >/dev/null \
    || die "$data has no commits — that is an empty brain, not the migrated one. Restore the data volume."

assert_writable "$data" "BRAIN_DATA_ROOT"

# Ask git where its directory actually is rather than assuming "$data/.git" is a
# directory. It is one here, but a gitfile pointing elsewhere would make the
# probe below fail with a message about permissions when the real problem is
# layout — and the object store is the half of the repository that has to be
# writable for a commit to exist at all.
gitdir=$(git -C "$data" rev-parse --absolute-git-dir 2>/dev/null || echo "")
[ -n "$gitdir" ] || die "cannot resolve the git directory for $data"
assert_writable "$gitdir" "the git object store for BRAIN_DATA_ROOT"

# ---------------------------------------------------------------------------
# 4. State: derived and disposable, so it may be created — data never is.
#
# Creating it is safe in a way that creating the data root is not: an index, a
# lock file and a ledger can all be rebuilt from the Markdown, and losing them
# costs a reindex. Under the production read-only rootfs a MISSING state mount
# cannot be silently invented either — mkdir lands on the read-only filesystem
# and fails right here.
# ---------------------------------------------------------------------------
state="${BRAIN_STATE_DIR:-}"
[ -n "$state" ] || die "BRAIN_STATE_DIR is unset — refusing to scatter index and lock files into the data mount"
mkdir -p "$state" 2>/dev/null || true
[ -d "$state" ] || die "BRAIN_STATE_DIR does not exist and cannot be created: $state (is the bind mount attached?)"
assert_writable "$state" "BRAIN_STATE_DIR"

# ---------------------------------------------------------------------------
# 5. Configuration. Existence and readability only.
#
# The SCHEMA is validated by bin/brain-http, which is the component that has to
# understand it; two validators would drift and the second one would be the one
# nobody updates. What is worth catching here is the boring failure — a config
# file that is root-owned 0600 on the host and therefore invisible to uid 10001
# — because the server's own error for that is indistinguishable from a
# misconfigured path.
# ---------------------------------------------------------------------------
conf="${BRAIN_HTTP_CONFIG:-}"
[ -n "$conf" ] || die "BRAIN_HTTP_CONFIG is unset"
[ -f "$conf" ] || die "BRAIN_HTTP_CONFIG does not exist: $conf (mount /etc/brain read-only into the container)"
[ -r "$conf" ] || die "BRAIN_HTTP_CONFIG is not readable by uid $(id -u): $conf — it must be owned by the container uid, mode 0600"

# Advisory, not fatal: a mode check can be meaningless on some filesystems, and
# refusing to serve the brain over a stat quirk is a worse outcome than saying
# so out loud. Never print the file's contents — it names the owner and the
# Access audiences.
mode=$(stat -c '%a' "$conf" 2>/dev/null || echo "")
case "$mode" in
    ""|600|400) : ;;
    *) warn "$conf is mode $mode; the deployment contract says 0600" ;;
esac

# ---------------------------------------------------------------------------
# 6. Hand over. exec, so the server is PID 1 and receives SIGTERM directly —
# `docker compose stop` must reach the process that holds the repository lock,
# not a shell that would be killed while a capture is mid-transaction.
# ---------------------------------------------------------------------------
# Which of the three names this process was invoked as decides what it runs;
# only the neutral name takes the command as an argument.
self=${0##*/}
case "$self" in
    brain-http)
        cmd="serve"
        ;;
    brain)
        cmd="brain"
        ;;
    *)
        if [ "$#" -gt 0 ]; then
            cmd="$1"
            shift
        else
            cmd="serve"
        fi
        ;;
esac

# Tolerate the image CMD leaking through. Compose clears CMD when a service
# overrides `entrypoint:`, and Docker does the same for `--entrypoint`, so this
# should never fire — but if some runner ever passes the default `serve` along
# to the brain-http shim, an unrecognised argument reaching the server is a
# confusing startup failure for a purely cosmetic reason.
if [ "$cmd" = "serve" ] && [ "${1:-}" = "serve" ]; then
    shift
fi

case "$cmd" in
    serve)
        # --bind is supplied HERE rather than left to bin/brain-http's default,
        # and the default is the reason. brain-http binds 127.0.0.1 unless told
        # otherwise, which is the right default for a developer running it on
        # their laptop and exactly wrong in a container: the tunnel connector
        # reaches this process over the compose network, and a loopback bind
        # would leave it answering only itself. The failure is quiet — the
        # container comes up, the healthcheck passes (it probes 127.0.0.1 from
        # inside), and only the edge sees 502.
        #
        # Binding all interfaces is safe HERE and nowhere else: this container
        # publishes no port, and the network carrying MCP traffic is declared
        # `internal: true`, so it has no gateway and nothing off it can route
        # in. The variable exists so a different deployment can narrow it
        # without editing the image.
        exec python3 "$ENGINE/bin/brain-http" \
            --bind "${BRAIN_HTTP_BIND:-0.0.0.0}" \
            --port "${BRAIN_HTTP_PORT:-8787}" "$@"
        ;;
    brain)
        exec python3 "$ENGINE/bin/brain" "$@"
        ;;
    *)
        die "unknown command '$cmd'. This container runs 'serve' or 'brain <subcommand>' and nothing else — that closed set is intentional. For a shell, override the entrypoint explicitly."
        ;;
esac
