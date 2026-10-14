# brain — the production image for the remote MCP service.
#
# WHAT IS IN HERE: the engine, and nothing else. bin/, setup/, .githooks/ and
# the three operating documents. No knowledge, no git metadata, no secret, no
# deployment configuration. The owner's notes arrive at runtime as a bind mount
# at BRAIN_DATA_ROOT and can therefore never be baked into a layer, pushed to a
# registry, or leak out of a `docker save`. That separation is the whole reason
# `bin/brain` grew three roots (ENGINE / ROOT / state_dir) — this file is the
# place where the distinction stops being theoretical.
#
# NO MULTI-STAGE BUILD. There is nothing to compile: the repo is Python stdlib
# only, deliberately (see plans/2026-08-23-remote-mcp-foundation.md, "Why no
# PyJWT"). A builder stage would exist purely to be discarded, and it would
# invite someone to install a wheel in it. The absence of a build stage is what
# makes "no build tools at runtime" true by construction rather than by policy.
#
# NO `# syntax=` DIRECTIVE. It would pull a moving frontend image from Docker
# Hub on every build, adding a network dependency and an unpinned third party
# to the build of the one component that guards the owner's brain. Nothing
# below needs a feature newer than the frontend BuildKit already ships with.

# Digest-friendly TAG, not a baked-in digest, and the reason is not laziness.
#
# The spec requires the DEPLOYED images to be "pinned to immutable release
# versions or digests" — that pin belongs on the artifact this build produces
# (compose deploys the brain image by digest) and on cloudflared, which is a
# third-party binary nobody here rebuilds. The BASE is different: Debian
# security fixes for OpenSSL, zlib and git are published by rebuilding
# `python:3.12-slim-bookworm` under the same name. A digest written into this
# tracked file freezes the image at the vulnerabilities of the day it was
# written, and it rots silently — the build keeps succeeding.
#
# So: the tag floats and `--pull` picks up the fixes, while a release build that
# wants byte-reproducibility passes the digest in without editing this file:
#
#   docker build --build-arg BASE_IMAGE=python:3.12-slim-bookworm@sha256:<...>
#
# The resolved base is recorded in an OCI label either way, so the handback can
# state exactly what was used.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG BASE_IMAGE
# Passed in by the release build rather than defaulted, so an image built
# without it is labelled "unreleased" and says so, instead of quietly claiming
# a version nobody tagged. The value that belongs here is bin/brainlib/version.py's
# VERSION, which is also what the server reports to a client as serverInfo and
# what the git tag is named after — see that file for why all three are one.
ARG BRAIN_VERSION=unreleased
LABEL org.opencontainers.image.title="brain" \
      org.opencontainers.image.description="Remote MCP second brain — engine only, contains no knowledge" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${BRAIN_VERSION}" \
      org.opencontainers.image.base.name="${BASE_IMAGE}"

# The three roots of the fixed interface contract, as defaults so a compose file
# that forgets one still lands on the documented layout instead of on the
# combined local layout (which, in a container, would mean writing notes into
# the image's own filesystem).
#
# HOME and TMPDIR point at /tmp because the root filesystem is read-only in
# production and git insists on somewhere to put lock and index temporaries.
# /tmp MUST therefore be a tmpfs — see deploy/entrypoint.sh, which fails loudly
# rather than discovering this at the first capture.
#
# PYTHONUNBUFFERED because logs are the only observability this service has and
# a buffered stdout hides the last minutes before a crash. PYTHONDONTWRITEBYTECODE
# because a read-only filesystem cannot accept a .pyc anyway; it does not stop
# Python READING the bytecode compiled below, which is the part worth keeping.
ENV BRAIN_DATA_ROOT=/srv/brain/data \
    BRAIN_STATE_DIR=/srv/brain/state \
    BRAIN_HTTP_CONFIG=/etc/brain/brain-http.json \
    HOME=/tmp \
    TMPDIR=/tmp \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# git and openssh-client are not conveniences: an accepted capture is defined by
# the spec as one that has been COMMITTED, and the durability chain continues
# over SSH to the private remote. ca-certificates is what lets the JWKS fetch in
# brainlib/access.py verify Cloudflare's certificate — without it the origin
# would either fail closed on every request or, far worse, be tempted into an
# unverified fetch of the keys it authenticates with.
#
# --no-install-recommends keeps git from dragging in perl-heavy extras, and the
# apt lists are removed in the same layer so they are not merely hidden.
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        openssh-client; \
    rm -rf /var/lib/apt/lists/*

# uid/gid 10001, fixed, because the data and state bind mounts are owned by that
# number on the host and a container-assigned uid would make ownership depend on
# package installation order. --no-create-home: HOME is /tmp, and a real home
# directory in the image would just be an unwritable decoy under a read-only
# rootfs. nologin because nothing here should ever be an interactive account.
RUN set -eu; \
    groupadd --gid 10001 brain; \
    useradd --uid 10001 --gid 10001 --home-dir /srv/brain --no-create-home \
            --shell /usr/sbin/nologin brain

# The engine, copied as an explicit ALLOWLIST rather than `COPY . .`.
#
# The repository-root .dockerignore denies everything and re-admits these same
# paths, but the two are not redundant: .dockerignore controls what reaches the
# daemon, and BuildKit's rules for FINDING that file depend on how the build is
# invoked (see its header — it was originally written under deploy/, where
# Docker never reads it). An enumerated COPY cannot be mis-wired: knowledge/ has
# no line here, so no lookup rule, no build flag and no future refactor can put
# a note in a layer.
WORKDIR /opt/brain
COPY bin/ /opt/brain/bin/
COPY setup/ /opt/brain/setup/
COPY .githooks/ /opt/brain/.githooks/
COPY AGENTS.md CLAUDE.md README.md /opt/brain/
COPY deploy/entrypoint.sh /usr/local/bin/brain-entrypoint

# The mount points, pre-created with the runtime uid.
#
# They are EMPTY on purpose. If a bind mount fails to attach, the container sees
# these empty directories, the entrypoint finds no git repository, and it
# refuses to start — which is the spec's required behaviour: an unavailable data
# volume must fail, never be papered over with a fresh empty brain.
RUN set -eu; \
    mkdir -p /srv/brain/data /srv/brain/state; \
    chown 10001:10001 /srv/brain/data /srv/brain/state; \
    chmod 0755 /usr/local/bin/brain-entrypoint; \
    find /opt/brain/bin -maxdepth 1 -type f -exec chmod 0755 {} +; \
    find /opt/brain/.githooks -maxdepth 1 -type f -exec chmod 0755 {} +; \
    ln -s brain-entrypoint /usr/local/bin/brain-http; \
    ln -s brain-entrypoint /usr/local/bin/brain

# /opt/brain/bin is deliberately NOT on PATH.
#
# deploy/compose.yaml states each service's entrypoint explicitly — `["brain-http"]`
# for the server, `["brain"]` for the one-shot maintenance role — and it is right
# to: an image whose default ENTRYPOINT silently changed would otherwise
# repurpose a running service. But a compose `entrypoint:` override REPLACES the
# image ENTRYPOINT, so a preflight that lived only in ENTRYPOINT would be skipped
# by precisely that wiring, and the container would come up serving whatever the
# data mount happened to be.
#
# The two symlinks above resolve that without either file having to know about
# the other: both names ARE the preflight, which dispatches on the name it was
# invoked as. Leaving the real bin/ off PATH is what closes the loop — there is
# no spelling of `entrypoint:` that reaches the toolbelt around the checks.

# Bytecode for brainlib, compiled once at build time.
#
# Every MCP tool call spawns `python3 bin/brain ...` as a child process, so the
# import cost of brainlib is paid per REQUEST, not per container. Under a
# read-only rootfs Python cannot cache it itself — it would silently recompile
# forever. compileall is also a syntax gate: a file that cannot be parsed fails
# the build here rather than the first tool call at 3am.
RUN set -eu; \
    python3 -m compileall -q /opt/brain/bin

# Build-time assertions. Every one of these is a failure that is SILENT at
# runtime, which is the only reason to spend a layer on it.
#
#  * bare `python3`: .githooks/pre-commit invokes the toolbelt as `python3
#    "$BRAIN" lint --staged` and treats a non-1 exit as "toolchain broken,
#    commit anyway". If python3 does not resolve, the content gate fails OPEN
#    and every commit this server makes is unlinted — no error, no log line,
#    just an unguarded brain.
#  * Python 3.9 floor: the repo's global constraint. A base bump that regressed
#    it would surface as an unrelated SyntaxError somewhere deep.
#  * sqlite3 with FTS5: `bin/brain search` IS the FTS5 index. A libsqlite3 built
#    without it degrades retrieval at the first query, on the machine that is
#    hardest to reach.
#  * git and ssh: see the durability chain above.
#  * brain-http and the pre-commit hook must actually be present and executable
#    — the entrypoint points core.hooksPath at that directory.
#  * the brainlib import graph must resolve with the stdlib alone. This is where
#    an accidental third-party dependency stops being possible to ship.
#  * no knowledge, no .git, no secret-shaped file anywhere in the image. This is
#    the backstop that turns a mis-wired build context into a failed build
#    instead of a published image full of the owner's notes.
RUN set -eu; \
    command -v python3 >/dev/null 2>&1 || { echo 'FATAL: no bare python3 on PATH — the pre-commit content gate would fail OPEN'; exit 1; }; \
    python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 9) else 1)'; \
    python3 -c 'import sqlite3; sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")'; \
    command -v git >/dev/null 2>&1 || { echo 'FATAL: git missing — a capture is only accepted once it is committed'; exit 1; }; \
    command -v ssh >/dev/null 2>&1 || { echo 'FATAL: ssh missing — the private backup remote is unreachable'; exit 1; }; \
    test -f /opt/brain/bin/brain-http || { echo 'FATAL: bin/brain-http missing from the build context'; exit 1; }; \
    test -x /opt/brain/.githooks/pre-commit || { echo 'FATAL: .githooks/pre-commit missing or not executable'; exit 1; }; \
    test -x /usr/local/bin/brain-http || { echo 'FATAL: the brain-http preflight shim is broken'; exit 1; }; \
    test -x /usr/local/bin/brain || { echo 'FATAL: the brain preflight shim is broken'; exit 1; }; \
    python3 -c 'import sys; sys.path.insert(0, "/opt/brain/bin"); import brainlib.rs256, brainlib.access, brainlib.mcpcore, brainlib.osbackend'; \
    test ! -e /opt/brain/knowledge || { echo 'FATAL: knowledge/ entered the image — check the root .dockerignore wiring'; exit 1; }; \
    test ! -e /opt/brain/.git || { echo 'FATAL: git metadata entered the image'; exit 1; }; \
    found=$(find /opt/brain \( -name '*.age' -o -name '.env' -o -name 'vault-recipient.txt' -o -name '.mcp.json' \) -print -quit); \
    test -z "$found" || { echo "FATAL: secret-shaped file in the image: $found"; exit 1; }

# The engine stays root-owned and world-readable. uid 10001 must be able to RUN
# it and must never be able to REWRITE it: a bug that gives a remote caller a
# file write would otherwise be a bug that lets them edit bin/brain. Only the
# two bind mounts and the tmpfs are writable by the runtime user, and that is
# the entire writable surface of this container.
USER 10001:10001

# Documentation only — EXPOSE publishes nothing. No host port is published for
# this service anywhere in the deployment; cloudflared reaches it over the
# private compose network, and Docker's published ports would bypass the host
# firewall by DNAT'ing before INPUT even if one were.
EXPOSE 8787

# There is no curl in this image and there will not be: a health check is not
# worth a URL-fetching binary with a CVE history in an image whose entire job is
# to guard private data. python3 is already here and is already trusted.
#
# /healthz is LIVENESS — "the HTTP process answers". Readiness (/readyz, which
# also proves the data mount, git, locks and the Access keys) is deliberately
# not wired to Docker's restart machinery: a JWKS outage or a wedged data volume
# is not something a container restart fixes, and restart-looping the service
# would destroy the diagnostic evidence and every live MCP session with it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python3", "-c", "import sys,urllib.request as u; r=u.urlopen('http://127.0.0.1:8787/healthz',timeout=4); sys.exit(0 if r.status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/brain-entrypoint"]
CMD ["serve"]
