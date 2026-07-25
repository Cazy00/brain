#!/bin/sh
# brain — one-command install.
#
#   curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh
#
# Flags (pass them after `-s --` when piping, e.g.
#   curl -fsSL … | sh -s -- --dir ~/knowledge --repo my-brain --yes):
#
#   --dir <path>    where the brain lives            (default: ~/brain)
#   --repo <name>   private GitHub repo to create    (default: ask, or skip)
#   --no-repo       do not create a remote at all    (LOCAL ONLY — no backup)
#   --no-wire       install the files, skip `bin/brain init` (wire it later)
#   --yes           never ask; take every default
#   --ref <ref>     branch/tag of the template to install (default: main)
#   --help
#
# What it does, in order: check prerequisites, clone the template, give it a
# FRESH git history that is yours, create your private remote, wire this machine
# (`bin/brain init`), and prove it with `bin/brain doctor`.
#
# It never deletes anything you did not name, and it refuses to install over a
# directory that already has something in it.

set -eu

TEMPLATE_REPO="${BRAIN_TEMPLATE_REPO:-Cazy00/brain}"
TEMPLATE_REF="main"
DEST=""
REPO_NAME=""
MAKE_REPO="ask"
ASSUME_YES="no"
WIRE="yes"

# ---------------------------------------------------------------- presentation

# Colour only when stdout is a terminal AND the terminal admits to having any.
if [ -t 1 ] && [ -n "${TERM:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    B=$(printf '\033[1m'); DIM=$(printf '\033[2m'); R=$(printf '\033[0m')
    OK=$(printf '\033[32m'); WARN=$(printf '\033[33m'); ERR=$(printf '\033[31m')
else
    B=""; DIM=""; R=""; OK=""; WARN=""; ERR=""
fi

say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s%s\n' "$B" "$R" "$B" "$*" "$R"; }
ok()   { printf '  %s[ok ]%s %s\n' "$OK" "$R" "$*"; }
note() { printf '  %s[-- ]%s %s\n' "$DIM" "$R" "$*"; }
warn() { printf '  %s[warn]%s %s\n' "$WARN" "$R" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$ERR" "$R" "$*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Piped into `sh`, stdin is the SCRIPT — not the user. Prompts have to come from
# the controlling terminal, and if there is not one we must never block.
if [ -r /dev/tty ] && [ -c /dev/tty ]; then HAS_TTY="yes"; else HAS_TTY="no"; fi

ask() {  # ask <prompt> <default>  -> echoes the answer
    _p="$1"; _d="$2"
    if [ "$ASSUME_YES" = "yes" ] || [ "$HAS_TTY" = "no" ]; then
        printf '%s' "$_d"; return 0
    fi
    printf '%s%s%s [%s]: ' "$B" "$_p" "$R" "$_d" > /dev/tty
    IFS= read -r _a < /dev/tty || _a=""
    [ -n "$_a" ] || _a="$_d"
    printf '%s' "$_a"
}

confirm() {  # confirm <prompt>  -> 0 for yes
    _p="$1"
    if [ "$ASSUME_YES" = "yes" ] || [ "$HAS_TTY" = "no" ]; then return 0; fi
    printf '%s%s%s [Y/n]: ' "$B" "$_p" "$R" > /dev/tty
    IFS= read -r _a < /dev/tty || _a=""
    case "$_a" in [nN]*) return 1 ;; *) return 0 ;; esac
}

# Piped through `sh`, $0 is not a readable file — so usage cannot be scraped
# from this script's own header. It is spelled out instead.
usage() {
    cat <<'USAGE'
brain — one-command install.

  curl -fsSL https://raw.githubusercontent.com/Cazy00/brain/main/install.sh | sh

Flags (pass them after `-s --` when piping):

  curl -fsSL … | sh -s -- --dir ~/knowledge --repo my-brain --yes

  --dir <path>    where the brain lives                  (default: ~/brain)
  --repo <name>   private GitHub repo to create          (default: ask, or skip)
  --no-repo       do not create a remote at all          (LOCAL ONLY — no backup)
  --no-wire       install the files, skip `bin/brain init`
  --yes, -y       never ask; take every default
  --ref <ref>     branch/tag of the template to install  (default: main)
  --help, -h      this

It checks prerequisites, clones the template, gives it a FRESH git history that
is yours, creates your private remote, wires this machine, and proves it with
`bin/brain doctor`. It never deletes anything you did not name, and it refuses
to install over a directory that already has something in it.
USAGE
    exit 0
}

# --------------------------------------------------------------------- arguments

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)     DEST="${2:-}"; shift 2 ;;
        --repo)    REPO_NAME="${2:-}"; MAKE_REPO="yes"; shift 2 ;;
        --ref)     TEMPLATE_REF="${2:-}"; shift 2 ;;
        --no-repo) MAKE_REPO="no"; shift ;;
        --no-wire) WIRE="no"; shift ;;
        --yes|-y)  ASSUME_YES="yes"; shift ;;
        --help|-h) usage ;;
        *) die "unknown option: $1  (try --help)" ;;
    esac
done

say ""
say "${B}brain${R} — a second brain that stays true"
say "${DIM}installing from ${TEMPLATE_REPO}@${TEMPLATE_REF}${R}"

# ------------------------------------------------------------------ prerequisites

step "Checking prerequisites"

have git || die "git is required. Install it and run this again."
ok "git $(git --version | awk '{print $3}')"

have python3 || die "python3 3.9+ is required. Install it and run this again."
PY_OK=$(python3 -c 'import sys; print("yes" if sys.version_info[:2] >= (3,9) else "no")' 2>/dev/null || echo no)
[ "$PY_OK" = "yes" ] || die "python3 3.9+ is required (found $(python3 -V 2>&1))."
ok "$(python3 -V 2>&1)"

case "$(uname -s)" in
    Darwin) ok "macOS — everything is supported" ;;
    Linux)  note "Linux — the core works; scheduling and the vault keystore are DIY (see SETUP.md)" ;;
    *)      warn "$(uname -s) is untested; the core is portable Python, so it will probably work" ;;
esac

for tool in gh rg gitleaks age; do
    if have "$tool"; then ok "$tool"; else note "$tool not installed (optional)"; fi
done

# ------------------------------------------------------------------- destination

step "Choosing where your brain lives"

if [ -z "$DEST" ]; then
    DEST=$(ask "Install to" "$HOME/brain")
fi
# Expand a leading ~ ourselves: it arrives literal from a flag or a prompt.
case "$DEST" in
    "~")   DEST="$HOME" ;;
    "~/"*) DEST="$HOME/${DEST#"~/"}" ;;
esac
# Absolute, without requiring the directory to exist yet.
case "$DEST" in /*) ;; *) DEST="$(pwd)/$DEST" ;; esac

if [ -e "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null || true)" ]; then
    die "$DEST already exists and is not empty.
       Refusing to install over it — if that is an old brain, retire it with
       'bin/brain reset' from inside it, or pass --dir to pick somewhere else."
fi
ok "$DEST"

# -------------------------------------------------------------------- get the code

step "Fetching the template"

TMP="${TMPDIR:-/tmp}/brain-install.$$"
rm -rf "$TMP"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT INT TERM

git clone --quiet --depth 1 --branch "$TEMPLATE_REF" \
    "https://github.com/${TEMPLATE_REPO}.git" "$TMP" \
    || die "could not clone https://github.com/${TEMPLATE_REPO}.git (ref: $TEMPLATE_REF)"
ok "cloned $TEMPLATE_REPO"

# The template's history is the PRODUCT's history, not yours. Your brain starts
# its own — which is also what GitHub's "Use this template" button does. Nothing
# is lost: SETUP.md Part 11 pulls toolbelt updates by adding the template as a
# second remote and checking out paths, which needs no shared ancestry.
rm -rf "$TMP/.git"

# Distribution scaffolding is not part of anyone's knowledge repo.
rm -rf "$TMP/.claude-plugin" "$TMP/plugins"
rm -f  "$TMP/install.sh"

mkdir -p "$DEST"
# Copy contents including dotfiles, without relying on non-POSIX cp flags.
(cd "$TMP" && tar cf - .) | (cd "$DEST" && tar xf -)
ok "installed into $DEST"

cd "$DEST"

if git init --quiet -b main 2>/dev/null; then :; else
    git init --quiet
    git symbolic-ref HEAD refs/heads/main
fi
git add -A
git -c core.hooksPath=/dev/null commit --quiet -m "brain: start" \
    || die "could not make the first commit — is git user.name/user.email set?"
ok "fresh git history, branch main"

# ------------------------------------------------------------------- your remote

step "Setting up your private backup"

if [ "$MAKE_REPO" = "ask" ]; then
    if [ "$HAS_TTY" = "yes" ] && [ "$ASSUME_YES" = "no" ]; then
        if confirm "Create a PRIVATE GitHub repo to back this up?"; then
            MAKE_REPO="yes"
        else
            MAKE_REPO="no"
        fi
    else
        MAKE_REPO="no"
    fi
fi

REMOTE_SET="no"
if [ "$MAKE_REPO" = "yes" ]; then
    if have gh && gh auth status >/dev/null 2>&1; then
        [ -n "$REPO_NAME" ] || REPO_NAME=$(ask "Repository name" "my-brain")
        if gh repo create "$REPO_NAME" --private --source . --remote origin --push >/dev/null 2>&1; then
            ok "created private repo and pushed: $(git remote get-url origin 2>/dev/null || echo "$REPO_NAME")"
            REMOTE_SET="yes"
        else
            warn "gh could not create '$REPO_NAME' (name taken, or scopes missing)"
        fi
    else
        warn "gh is not installed or not logged in"
    fi
fi

if [ "$REMOTE_SET" = "no" ]; then
    note "no remote yet — your notes are LOCAL ONLY and are not backed up"
fi

# Verify rather than assume. We ask for --private above, but the remote may
# already have existed, or an org policy may have overridden it. A brain in a
# public repo is the only mistake here with no undo, so it is checked out loud
# at the moment it is cheapest to fix — not left for the first doctor run.
ORIGIN=$(git remote get-url origin 2>/dev/null || true)
if [ -n "$ORIGIN" ]; then
    SLUG=$(printf '%s' "$ORIGIN" | sed -n 's|.*github\.com[/:]\([^/]*\)/\(.*\)|\1/\2|p' | sed 's|\.git$||; s|/$||')
    if [ -n "$SLUG" ]; then
        VIS=""
        if have gh; then
            VIS=$(gh repo view "$SLUG" --json visibility -q .visibility 2>/dev/null | tr '[:upper:]' '[:lower:]')
        fi
        if [ -z "$VIS" ] && have curl; then
            # 200 without credentials proves anyone can read it.
            if curl -fsS -o /dev/null "https://api.github.com/repos/$SLUG" 2>/dev/null; then
                VIS="public"
            else
                VIS="private"
            fi
        fi
        case "$VIS" in
            public)
                printf '\n  %s================= STOP =================%s\n' "$ERR" "$R"
                printf '  %sYOUR BRAIN REPO IS PUBLIC.%s Anyone on the internet can read\n' "$ERR" "$R"
                printf '  every note you will ever put in it.\n\n'
                printf '  Fix it now:\n'
                printf '    %sgh repo edit %s --visibility private --accept-visibility-change-consequences%s\n\n' "$DIM" "$SLUG" "$R"
                printf '  Or on the web: Settings -> General -> Danger Zone -> Change visibility.\n'
                printf '  %s=======================================%s\n\n' "$ERR" "$R"
                ;;
            private) ok "remote is not publicly readable" ;;
            *)       note "could not verify remote visibility — check it is PRIVATE yourself" ;;
        esac
    fi
fi

# ------------------------------------------------------------------------ wiring

if [ "$WIRE" = "yes" ]; then
    step "Wiring this machine"
    python3 bin/brain init || die "bin/brain init failed — see the output above"
else
    step "Wiring this machine"
    note "skipped (--no-wire) — run 'cd $DEST && bin/brain init' when you are ready"
    git config core.hooksPath .githooks
    ok "git hooks installed (the commit gate works even unwired)"
fi

# ------------------------------------------------------------------------- proof

step "Checking it works"

python3 bin/brain doctor || true

# -------------------------------------------------------------------- next steps

step "Done"

say ""
say "  Your brain is at ${B}${DEST}${R}"
say ""
say "  ${B}1.${R} Make your agent reach for it without being asked:"
say "     ${DIM}cd $DEST && bin/brain connect --routing${R}"
say "     Paste that block into your agent's global instruction file"
say "     (Claude Code: ~/.claude/CLAUDE.md). Both halves matter — the first"
say "     makes it retrieve, the second makes it offer to capture."
say ""
if [ "$REMOTE_SET" = "no" ]; then
say "  ${B}2.${R} ${WARN}Set up a private backup${R} — nothing is backed up yet:"
say "     ${DIM}cd $DEST && gh repo create my-brain --private --source . --push${R}"
say "     Your notes must never live in a public repo."
say ""
fi
say "  ${B}3.${R} Restart your agent (MCP servers and skills load at session start),"
say "     then ask it: ${DIM}\"what's in my brain?\"${R}"
say ""
say "  Read next: ${DIM}${DEST}/SETUP.md${R} (the full guide, including schedules,"
say "  other agents, and the encrypted vault) and ${DIM}${DEST}/AGENTS.md${R} (the rules"
say "  your notes are held to)."
say ""
