# bin/brainlib/provenance.py
"""Where an inbox note came from, and what its writer said about it.

One module, because the rule has three users that must agree exactly:
`brain capture`, which writes the fields, and the two MCP front ends, which
stamp some of them and offer the rest to callers. A rule with three copies is
three rules, and here the drift would be a value one layer accepts and another
writes into frontmatter unchecked.

Two kinds of field, kept apart by WHO may set them:

- **Endpoint fields** (`via`, `client`) are stamped by the server that accepted
  the capture, from what it knows: the label its operator gave the verified
  credential, or the transport it is; and the client program's own
  self-reported name and version. They are never tool arguments, so a caller
  cannot set them. `client` says which program asked, not who — it is
  self-reported, and nothing may treat it as an identity.
- **Declared fields** (`kind`, `basis`, `project`, `job`, `model`) are the
  caller's own claims about the note. They are recorded as claims: a declared
  field never opens a gate by itself, and consolidation weighs it against
  everything else it knows.

Values are checked strictly, and a value that fails is DROPPED, never repaired
— a frontmatter line is one `key: value` pair, so a value carrying a newline,
a `#`, a quote, a bracket or `: ` could otherwise write a second key. Losing
the note over a bad label would be the worse failure, so the note is still
saved and the dropped field is named in the reply.
"""
import copy
import re
import time

KINDS = ("decision", "preference", "fact", "lesson", "idea", "correction", "other")
BASES = ("owner-said", "agent-found", "agent-inferred")

ENDPOINT_FIELDS = ("via", "client")
DECLARED_FIELDS = ("kind", "basis", "project", "job", "model")
FIELDS = ENDPOINT_FIELDS + DECLARED_FIELDS

# The order an inbox note's frontmatter is written in. `created`, `source` and
# `status` were there before provenance and keep their places around it.
ORDER = ("created", "captured_at", "source") + FIELDS + ("status",)

SLUG_MAX = 80
CLIENT_MAX = 96

# fullmatch, never match-with-$: `$` also matches before a trailing newline,
# which is precisely the character this exists to keep out.
_SLUG = re.compile(r"[a-z0-9][a-z0-9._:-]{0,%d}" % (SLUG_MAX - 1))
_CLIENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/ -]{0,%d}" % (CLIENT_MAX - 1))
# Refused whatever the field's own pattern says. The patterns already exclude
# all of it; this is the rule said out loud, so a pattern widened later for a
# good reason cannot quietly let a line break or a second key through.
_NEVER = re.compile(r"[\x00-\x1f\x7f#'\"\[\]{}]|: ")


def clean(field: str, value):
    """The value as it will be written, or None when it may not be written."""
    if field not in FIELDS:
        raise KeyError(field)
    # Checked BEFORE anything is trimmed: "fact\n" is refused, not tidied into
    # "fact". A value that arrives carrying a line break was built by
    # something that should be fixed, and quietly accepting it hides that.
    if not isinstance(value, str) or _NEVER.search(value):
        return None
    raw = value.strip(" ")
    if not raw:
        return None
    if field == "client":
        # `name/version`, as the client spells itself, folded to one token.
        return raw.lower().replace(" ", "-") if _CLIENT.fullmatch(raw) else None
    lowered = raw.lower()
    if field == "kind":
        return lowered if lowered in KINDS else None
    if field == "basis":
        return lowered if lowered in BASES else None
    return lowered if _SLUG.fullmatch(lowered) else None


def is_slug(value) -> bool:
    """True when `value` may be written as `via` exactly as it is spelled."""
    return isinstance(value, str) and clean("via", value) == value


def client_label(name, version=None):
    """`name/version` from a client's self-reported clientInfo, or None."""
    if not isinstance(name, str) or not name.strip(" "):
        return None
    label = name.strip(" ")
    if isinstance(version, str) and version.strip(" "):
        label = "%s/%s" % (label, version.strip(" "))
    return clean("client", label[:CLIENT_MAX])


def captured_at(now=None) -> str:
    """The capture instant in UTC, to the second: 2026-10-18T05:12:09Z.

    UTC whatever the deployment's time zone, so two notes from servers in two
    zones still sort into the order they were written."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))


def take_declared(args):
    """Split a tool call's declared fields out of its arguments.

    Returns (rest, values, ignored): the other arguments, for the strict
    validator; the declared values that passed `clean`; and the names of those
    that did not. The strict validator never sees these fields, because it
    REFUSES a bad value and the rule here is to drop it and keep the note. An
    explicit null means "not supplied", as it does for every optional argument.
    """
    if not isinstance(args, dict):
        return args, {}, []
    rest, values, ignored = {}, {}, []
    for name, value in args.items():
        if name not in DECLARED_FIELDS:
            rest[name] = value
            continue
        if value is None:
            continue
        cleaned = clean(name, value)
        if cleaned is None:
            ignored.append(name)
        else:
            values[name] = cleaned
    return rest, values, [name for name in DECLARED_FIELDS if name in ignored]


def cli_flags(values: dict) -> list:
    """The `brain capture` flags that carry these values, in field order."""
    flags = []
    for name in FIELDS:
        value = (values or {}).get(name)
        if value:
            flags += ["--" + name, value]
    return flags


def ignored_line(ignored) -> str:
    """The one sentence a caller is told about declared fields not recorded."""
    return ("ignored: %s — not a valid value, so it is not recorded on the note (the "
            "tool's schema lists what each field accepts)" % ", ".join(ignored))


_DECLARED_SCHEMA = {
    "kind": {"type": "string", "enum": list(KINDS),
             "description": ("What this note is. A correction names, in its text, the id "
                             "of the note it corrects (an inbox note's path, which is all "
                             "an inbox note has).")},
    "basis": {"type": "string", "enum": list(BASES),
              "description": ("Why it is believed: owner-said when the user said it, "
                              "agent-found when you verified it, agent-inferred when it is "
                              "your own conclusion.")},
    "project": {"type": "string", "maxLength": SLUG_MAX,
                "description": "Slug of the project this concerns, when there is one."},
    "job": {"type": "string", "maxLength": SLUG_MAX,
            "description": "Id of the task or job this came from, when known."},
    "model": {"type": "string", "maxLength": SLUG_MAX,
              "description": "Id of the model writing this capture, when known."},
}


def declared_schema() -> dict:
    """The brain_capture properties every front end offers for declared fields."""
    return copy.deepcopy(_DECLARED_SCHEMA)
