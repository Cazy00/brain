#!/bin/sh
# brain — one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh
#
# A bootstrap and nothing more: check that git and python 3.9+ are present,
# fetch the template into a temp directory, and hand off to `brain setup`.
#
# Everything that used to be here — choosing where the brain lives, the private
# remote, the health check — now lives in bin/brainlib/setup.py, because this
# script could only ever run on macOS and Linux and because a shell installer
# is untestable in practice. install.ps1 is this file's Windows twin and hands
# off to exactly the same code, so all three platforms behave identically.
#
# It does NOT wire your agents. `brain init` re-points the global
# ~/.claude/skills/brain link at whatever checkout ran it, and this script runs
# from a clone it is about to delete. Wiring is `brain connect`, run afterwards
# from the brain itself.

set -eu

TEMPLATE_REPO="${BRAIN_TEMPLATE_REPO:-Cazy00/brain}"
TEMPLATE_REF="main"

die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# Piped through `sh`, $0 is not a readable file — so usage cannot be scraped
# from this script's own header. It is spelled out instead.
usage() {
    cat <<'USAGE'
brain — one-command install.

  curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh

Flags (pass them after `-s --` when piping):

  curl -fsSL … | sh -s -- --dir ~/knowledge --repo my-brain --yes

This script owns exactly one of them:

  --ref <ref>     branch/tag of the TEMPLATE to install from  (default: main)
  --help, -h      this

Everything else goes straight to `brain setup`, which owns the install:

  --dir <path>    where the brain lives; skips the picker
  --repo <name>   name for the private GitHub repo   (default: my-brain)
  --no-repo       do not create a remote at all — LOCAL ONLY, no backup
  --yes, -y       never ask; take every default
  --json          machine-readable result on stdout, human text on stderr
  --only <phase>  re-run one phase of the install

Afterwards, `brain connect` wires your agents to it. That is a separate step
on purpose: each one ends in a working state, so stopping after either is a
legitimate outcome rather than an abandoned install.
USAGE
    exit 0
}

# Rotate through the arguments, consuming the one flag this script owns and
# re-appending the rest, in order, for `brain setup`. `set -- "$@" "$1"` rather
# than accumulating into a string, because a path with a space in it has to
# survive the trip. `remaining` counts the ORIGINAL arguments, which is also
# what makes a trailing `--ref` detectable: by then $2 is an argument this loop
# has already rotated past, not the value somebody forgot to type.
remaining=$#
while [ "$remaining" -gt 0 ]; do
    case "$1" in
        --ref)     [ "$remaining" -ge 2 ] || die "--ref needs a value"
                   TEMPLATE_REF="$2"; shift 2; remaining=$((remaining - 2)) ;;
        --help|-h) usage ;;
        *)         set -- "$@" "$1"; shift; remaining=$((remaining - 1)) ;;
    esac
done

printf '\nbrain — a second brain that stays true\n'
printf 'installing from %s@%s\n\n' "$TEMPLATE_REPO" "$TEMPLATE_REF"

# The two hard prerequisites, checked before anything is downloaded. Everything
# else — gh, gitleaks, age, rg — is reported with its consequence by the check
# phase of `brain setup`, which never installs anything either.
have git || die "git is required. Install it and run this again."
have python3 || die "python3 3.9+ is required. Install it and run this again."
python3 -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' \
    || die "python3 3.9+ is required (found $(python3 -V 2>&1))."

TMP="${TMPDIR:-/tmp}/brain-install.$$"
rm -rf "$TMP"
trap 'rm -rf "$TMP"' EXIT INT TERM

git clone --quiet --depth 1 --branch "$TEMPLATE_REF" \
    "https://github.com/${TEMPLATE_REPO}.git" "$TMP" \
    || die "could not clone https://github.com/${TEMPLATE_REPO}.git (ref: $TEMPLATE_REF)"

# Hand off. Not `exec`: the trap above still has a temp clone to delete, and
# exec would replace this shell before it could run. `set -e` carries setup's
# exit code out of here unchanged, which matters — it is doctor's verdict, so a
# non-zero exit means the brain was installed but is not yet healthy.
python3 "$TMP"/bin/brain setup "$@"
