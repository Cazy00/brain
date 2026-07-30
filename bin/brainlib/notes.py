# bin/brainlib/notes.py
"""The note contract, and the secret scanner — the rules that must be identical
everywhere.

Split out of bin/brain on 2026-07-30, when `brain publish` arrived. The reason
is the same one that split the MCP tool table out of the stdio server: a rule
with two copies is two rules, and here the drift would be silent and
expensive. `publish` decides what a customer may see, and lint decides what may
be committed; if their idea of "a note marked public" ever differed by one
condition, the difference would be a note shipped to strangers that lint
believed was unpublishable.

What belongs here: anything pure that answers "what IS a well-formed note" —
the frontmatter parser, the vocabularies, the id rule, the wikilink shape, the
publishability rules, and the credential patterns. What does not: anything that
knows where THIS brain lives on disk, or what any particular command does with
the answer. bin/brain imports these names and its call sites are unchanged.
"""
import re
from pathlib import Path

CANONICAL = ("decisions", "topics", "projects", "people", "life", "reference")
FOLDER_KIND = {
    "decisions": "decision",
    "topics": "topic",
    "projects": "project",
    "people": "person",
    "life": "note",
    "reference": "reference",
}
# May a note leave this brain? Three states, and the third one is ABSENT:
# never reviewed. `private` means a human looked and said no, which is why it
# is worth spelling — both are excluded from a published brain, but only the
# absent ones belong in the review queue, and a queue that keeps re-asking
# about refusals is a queue nobody finishes.
VISIBILITIES = {"public", "private"}
# Folders whose notes are never publishable, whatever their frontmatter says.
# Spelled separately from SENSITIVITY_REQUIRED even though the two match
# today: one asks "must this be classified", the other "may this be seen by a
# customer", and merging them would silently answer the second question with
# the first the day either list changes.
NEVER_PUBLIC = {"people", "life"}

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
WIKILINK_RE = re.compile(r"\[\[\s*([^\]\[|#\n]+?)\s*(?:[|#][^\]\n]*)?\]\]")

ALLOW_PRAGMA = "lint:allow-secret"

SECRET_PATTERNS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("Slack token", re.compile(r"\bxox[bpars]-[A-Za-z0-9-]{10,}\b")),
    # Anthropic/OpenAI-style keys: sk-... AND Stripe/other sk_live_/sk_test_...
    ("API-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Stripe secret key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Stripe restricted key", re.compile(r"\brk_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b")),
    ("Google OAuth id", re.compile(r"\b[0-9]+-[0-9a-z]{32}\.apps\.googleusercontent\.com\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    # A connection URL carrying an inline credential (the user-colon-secret pair
    # before the host). Covers postgres, mongodb, redis, amqp, https, etc. The
    # example forms are kept out of this comment on purpose — this scanner scans
    # its own source, and a literal sample here would self-flag.
    ("URL with embedded credentials",
     re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:[^\s/@]{4,}@")),
    # An assignment of a secret-ish name to a long opaque value. Unlike the old
    # rule this does NOT require the value to be quoted — the real leak that got
    # through was `STRIPE_SECRET_KEY=sk_live_…` and `aws_secret_access_key=…`
    # with bare values. Prose ("the password: see the runbook") is safe: the
    # value run must be 12+ non-space chars with no spaces.
    (
        "credential assignment",
        re.compile(r"(?i)\b(?:aws_secret_access_key|secret[_-]?access[_-]?key|"
                   r"client[_-]?secret|access[_-]?token|auth[_-]?token|refresh[_-]?token|"
                   r"api[_-]?key|secret[_-]?key|private[_-]?key|secret|token|passwd|password)\b"
                   r"\s*[:=]\s*['\"]?[^'\"\s]{12,}"),
    ),
]

# Literal screen: a substring EVERY secret pattern must contain to match.
# Each regex above begins with \b, which forces the engine to try at every
# position — ~165ms per pattern over a 30MB corpus, so 13 of them cost ~2.2s on
# every lint. str.__contains__ uses a C substring search and is ~10x cheaper, so
# a file is only handed to the regexes when a required literal is actually
# present. These lists must stay a strict SUPERSET of the patterns: if you add a
# pattern above, add the literal it cannot match without.
SECRET_LITERALS = (
    "PRIVATE KEY",                                    # private key block
    "AKIA",                                           # AWS access key id
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_",           # GitHub classic
    "github_pat_",                                    # GitHub fine-grained
    "xox",                                            # Slack
    "sk-", "sk_", "rk_",                              # OpenAI/Anthropic, Stripe
    "AIza", ".apps.googleusercontent.com",            # Google
    "eyJ",                                            # JWT
    "://",                                            # URL with inline credential
)
# The credential-assignment rule is case-insensitive; these are matched against
# a lowercased copy. "secret" also covers client_secret/secret_key/
# aws_secret_access_key; "token" covers access_/auth_/refresh_token.
SECRET_KEYWORDS = (
    "secret", "token", "passwd", "password",
    "apikey", "api_key", "api-key",
    "privatekey", "private_key", "private-key",
)


def secret_screen(text: str) -> bool:
    """True if any secret pattern could possibly match — cheap superset test."""
    for literal in SECRET_LITERALS:
        if literal in text:
            return True
    lowered = text.lower()
    for keyword in SECRET_KEYWORDS:
        if keyword in lowered:
            return True
    return False


def scan_secrets(path: Path, errors: list, raw: bytes = None) -> None:
    """Scan a file for secret patterns. Bytes-aware: UTF-16 content (NUL-interleaved
    when read as UTF-8) is decoded and scanned too, so encoding is not an evasion."""
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            errors.append((path, f"unreadable file: {exc}"))
            return
    texts = [raw.decode("utf-8-sig", errors="replace")]
    if b"\x00" in raw[:4096]:
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                texts.append(raw.decode(enc))
                break
            except (UnicodeDecodeError, ValueError):
                continue
    for text in texts:
        if not secret_screen(text):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if ALLOW_PRAGMA in line:
                continue
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    errors.append((path, f"line {lineno}: possible {label} — secrets never "
                                   "go in this repo"))


def read_text(path: Path) -> str:
    # utf-8-sig: transparently strips a Windows BOM so a BOM'd note still
    # starts with "---" as far as the parser is concerned.
    return path.read_text(encoding="utf-8-sig", errors="replace")


def _split_list(inner: str) -> list:
    """Split an inline [a, b] list on commas, respecting quotes — so
    ["brain, ai-tooling"] is ONE item, matching what a real YAML reader sees."""
    items, cur, quote = [], "", None
    for ch in inner:
        if quote:
            if ch == quote:
                quote = None
            else:
                cur += ch
        elif ch in "'\"":
            quote = ch
        elif ch == ",":
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    items.append(cur.strip())
    return [i for i in items if i]


def parse_frontmatter(text: str):
    """Parse the restricted frontmatter subset: flat 'key: value', inline [lists].

    Returns (dict, None) on success, (None, error_message) on failure.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "missing frontmatter (file must start with ---)"
    fm = {}
    for i, line in enumerate(lines[1:], start=2):
        if line.strip() == "---":
            fm["_end_line"] = i
            return fm, None
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m:
            return None, f"line {i}: cannot parse {line.strip()!r} (only flat 'key: value' allowed)"
        key, raw = m.group(1), m.group(2).strip()
        if key in fm:
            return None, f"line {i}: duplicate frontmatter key {key!r}"
        if raw.startswith("[") and raw.endswith("]"):
            fm[key] = _split_list(raw[1:-1].strip())
        elif raw in ("", "null", "~"):
            fm[key] = None
        else:
            fm[key] = raw.strip("'\"")
    return None, "frontmatter never closed with a second ---"


def extract_links(body: str) -> list:
    """Wikilink targets in a note body, in order, de-duplicated.

    Accepts [[note-id]], [[note-id|display text]] and [[note-id#section]] —
    the target is everything before the first | or #."""
    seen, out = set(), []
    for m in WIKILINK_RE.finditer(body or ""):
        target = m.group(1).strip()
        if target and target not in seen:
            seen.add(target)
            out.append(target)
    return out


def fm_update(path: Path, updates: dict, banner: str = None) -> None:
    """Set flat frontmatter fields in place (add if missing); optionally insert a
    banner line right after the frontmatter block.

    A value of None REMOVES the field. Removing is not the same as writing an
    empty value: `visibility:` with nothing after it reads as a field somebody
    set, and the difference between "never reviewed" and "reviewed" is the
    whole of what that field records.
    """
    lines = read_text(path).splitlines()
    end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    opener, block, tail = lines[:1], lines[1:end], lines[end:]
    kept, done = [], set()
    for line in block:
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if m and m.group(1) in updates:
            done.add(m.group(1))
            if updates[m.group(1)] is None:
                continue
            line = f"{m.group(1)}: {updates[m.group(1)]}"
        kept.append(line)
    for key, value in updates.items():
        if key not in done and value is not None:
            kept.append(f"{key}: {value}")
    if banner:
        # After the closing --- (tail[0]), with a blank line between.
        tail = tail[:1] + ["", banner] + tail[1:]
    path.write_text("\n".join(opener + kept + tail) + "\n", encoding="utf-8")


def publish_blockers(folder: str, fm: dict) -> list:
    """Every reason this note must NOT be marked `visibility: public`.

    One function, three callers — lint refuses a note that already carries the
    field wrongly, `brain publish approve` refuses to write it in the first
    place, and the compiler's audit re-checks the tree it just built — because
    copies of the rule that decides what a customer can see would not stay one
    rule.

    Returns human-readable reasons, plural on purpose: an operator fixing one
    problem should be told about the second one now, not after the next run.
    """
    reasons = []
    sensitivity = (fm or {}).get("sensitivity")
    if sensitivity and sensitivity != "normal":
        reasons.append(f"its sensitivity is {sensitivity!r} — the two fields disagree, "
                       "and a note classified as not-normal is not customer-facing "
                       "material whatever the other field says")
    if folder in NEVER_PUBLIC:
        reasons.append(f"notes in {folder}/ are about a named person's life; nothing "
                       "there is publishable, and the person whose details they are "
                       "never got a say")
    if (fm or {}).get("status") != "current":
        reasons.append("only a current note can be published — an archived or draft "
                       "one is history or work in progress, and serving it to a "
                       "customer would serve a fact this brain has already replaced")
    return reasons
