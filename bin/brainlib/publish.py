# bin/brainlib/publish.py
"""`brain publish` — compile P, a customer-facing brain, out of M, the real one.

This is the trust boundary of the whole system, and it has exactly one idea in
it: **P is compiled, not filtered.** It is rebuilt from nothing on every run
and contains only notes a human marked `visibility: public`. There is no
query-time exclusion anywhere, because a boundary that is a code path is one
bug away from a leak — and the bug would be found by a customer.

Everything else follows from that:

- **Destroy and rebuild.** A note whose approval was revoked has to vanish, and
  the only way to guarantee that is to carry nothing forward. Never patch,
  never merge.
- **Audit the OUTPUT, not the input.** The selection above can be perfectly
  right and one bug in the copy can still ship a note, so the last step reads
  the tree that was actually built and re-asks every question of it.
- **Refuse loudly and take the tree with you.** Leaving an unclean copy on disk
  is how a refused build still gets published: the tree looks finished, the
  refusal has scrolled off, and nobody re-checks before restarting the server.
- **P is a whole brain, not a folder of markdown.** It ships the toolbelt, so
  `brain serve --read-only` is run from INSIDE it and the serving process holds
  no configuration naming M at all. The alternative — pointing M's toolbelt at
  P's knowledge directory with a flag — is one typo from serving M.
- **Say what LEFT.** A superseded fact is silently correct: the successor is
  unreviewed so it does not ship, the predecessor is archived so it is dropped.
  Both halves are invisible on their own, and the operator only finds out when
  the bot starts answering "I don't know" about something it knew yesterday.

What this does NOT do: commit, push, or restart anything. The operator does
that, having read the report — particularly the removals.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

from . import notes

# The only frontmatter that reaches P. An allowlist, not a denylist: a denylist
# publishes every field somebody adds in 2028 and forgets to exclude, and the
# field they add will be the one that carries a review note or a source.
PUBLISHED_FIELDS = ("id", "kind", "title", "topics", "aliases", "created",
                    "status", "visibility")

# What publish owns inside the destination and will therefore delete. Anything
# else in there means the operator is keeping something in a directory this
# command rebuilds from zero — which it refuses to do rather than discover
# afterwards. `.git` is exempt: P is deployed from a repo, and its history is
# the record of what was served last week.
MANAGED = ("knowledge", "bin", ".githooks", ".rgignore", ".gitignore", "README.md")

# What a link to an unpublished note becomes. Not the id: a dangling
# [[2026-03-01-acquisition-talks]] hands a stranger the existence, the date and
# the subject of a private note, which is the leak this strip exists to close.
UNPUBLISHED_MARK = "(unpublished note)"

USAGE = """brain publish — compile a customer-facing brain from this one.

  brain publish <destination> [--dry-run]
  brain publish review
  brain publish approve <id>
  brain publish deny <id>

  --dry-run     build, audit and report, then throw the tree away. Writes
                nothing to the destination

`review` lists the notes nobody has decided about yet — newest first, with
enough of each to decide without opening it. `approve` and `deny` set the one
field, in place, and nothing else. Notes already denied stay out of the queue:
being re-asked forever about refusals is how a review queue stops being read.

None of these is an MCP tool, and none ever will be. Approval is an act by a
person at a keyboard; a tool that listed what is pending would enumerate this
brain's PRIVATE notes to whoever holds a token, and putting the human gate on
the wire defeats the human gate.

Selects the notes a human marked `visibility: public`, drops every other
frontmatter field, strips links to notes that stayed behind, and writes a
complete brain — toolbelt included — to the destination. Serve it with
`brain serve --read-only` run from INSIDE that directory, so the serving
process holds no path that reaches this brain.

The destination is rebuilt from zero every time. That is what makes revoking
an approval work, and it is why publish refuses a directory holding anything
it did not create.

There is no --force: a tree that failed its own audit has no legitimate
publisher. Read the refusal, fix the note, run it again.
"""


def run_publish(argv: list, root: Path) -> int:
    """The `brain publish` command. `root` is M."""
    if "--help" in argv or "-h" in argv:
        print(USAGE)
        return 0
    if argv[:1] == ["review"]:
        return review(Path(root).resolve())
    if argv[:1] in (["approve"], ["deny"]):
        if len(argv) < 2:
            print(f"usage: brain publish {argv[0]} <id>")
            return 2
        return decide(Path(root).resolve(), argv[1],
                      "public" if argv[0] == "approve" else "private")
    unknown = [a for a in argv if a.startswith("-") and a != "--dry-run"]
    if unknown:
        print(f"unknown flag {unknown[0]!r}\n\n{USAGE}")
        if unknown[0] == "--force":
            print("There is deliberately no --force. A tree that failed its audit is\n"
                  "not a tree to publish anyway; the refusal names every problem.")
        return 2
    args = [a for a in argv if not a.startswith("-")]
    if not args:
        print(USAGE)
        return 2

    dest = Path(args[0]).expanduser().resolve()
    root = Path(root).resolve()
    # Refuse BEFORE anything is destroyed, and in both directions: an ancestor
    # is what the rebuild would delete, and a destination inside M would have
    # the copy walking into its own output.
    if dest == root or dest in root.parents or root in dest.parents:
        print(f"refusing to publish over this brain: {dest} overlaps {root} — "
              "choose a destination outside it")
        return 1
    if dest.exists():
        if not dest.is_dir():
            print(f"{dest} is not a directory")
            return 1
        strangers = sorted(p.name for p in dest.iterdir()
                           if p.name not in MANAGED and p.name != ".git")
        if strangers:
            print(f"refusing to publish into {dest}: it holds files publish did not "
                  "create\n  " + ", ".join(strangers[:10]) +
                  "\n\nThis directory is rebuilt from zero on every publish, so anything "
                  "kept\nhere would be deleted. Publish into a directory that is only "
                  "ever\nthe compiled artifact.")
            return 1

    previous = manifest(dest)
    selected, refused = select(root / "knowledge")
    if refused:
        print("REFUSING TO PUBLISH — these notes are marked public but are not "
              "publishable:")
        for rel, reasons in refused:
            for reason in reasons:
                print(f"        - {rel}: {reason}")
        print("\nNothing was written. Fix the notes (or `brain publish deny` them) "
              "and run again.")
        return 1
    print(f"brain publish — compiling from {len(selected)} approved note(s)")
    print(f"  [1/5] selected {len(selected)} of "
          f"{count_candidates(root / 'knowledge')} note(s) in canonical folders")

    # ALWAYS built somewhere else first, then swapped in. Building in place
    # would mean a failed audit leaves the destination empty — and the
    # destination is what a customer-facing bot is serving right now. This way
    # a refused build costs nothing: the previous artifact keeps answering
    # until a clean one is ready to replace it, and the unclean tree never
    # reaches the place anybody would deploy from.
    #
    # It is still a rebuild from zero, which is the property that matters:
    # nothing is carried forward, so a revoked approval cannot survive.
    staging = Path(tempfile.mkdtemp(prefix="brain-publish-"))
    dry_run = "--dry-run" in argv
    try:
        stripped, vocabulary = write_tree(staging, root, selected)
        print(f"  [2/5] wrote knowledge/ — {len(selected)} note(s), "
              f"{len(vocabulary)} topic(s), {stripped} link(s) stripped")
        copied = copy_toolbelt(staging, root)
        print(f"  [3/5] copied the toolbelt — {copied} file(s); P is a brain, "
              "not a folder of notes")

        problems = audit(staging, root, {n["id"] for n in selected}, vocabulary)
        if problems:
            print("  [4/5] REFUSING TO PUBLISH — the compiled brain is not clean:")
            for problem in problems:
                print(f"        - {problem}")
            print(f"\n        Nothing was written to {dest}. The unclean tree was "
                  "deleted rather\n        than left on disk, because a tree that "
                  "looks finished is a tree\n        somebody deploys after the "
                  "refusal has scrolled off.")
            return 1
        print("  [4/5] audited the output ........................ clean")

        check = subprocess.run([sys.executable, str(staging / "bin" / "brain"), "lint"],
                               cwd=staging, capture_output=True, text=True)
        if check.returncode != 0:
            print("  [5/5] LINT FAILED inside the compiled brain:")
            print("\n".join("        " + line
                            for line in check.stdout.splitlines()[-12:]))
            print(f"\n        Nothing was written to {dest}.")
            return 1
        print("  [5/5] lint inside P ............................. clean")

        if not dry_run:
            swap(staging, dest)
        report(previous, manifest(staging if dry_run else dest), dest,
               dry_run=dry_run)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    if dry_run:
        print(f"\n--dry-run: {dest} was not touched.")
        return 0
    print(f"\npublished: {dest}")
    print("  serve it from INSIDE that directory, never from this one:")
    print(f"    cd {dest} && bin/brain serve --read-only --port 8788")
    print("  the running server is still serving the PREVIOUS build until you "
          "restart it.")
    return 0


# ------------------------------------------------------------------ selection


def candidates(k: Path):
    """Every note in a canonical folder, with its folder and parsed frontmatter.

    Canonical only. inbox/, journal/, archive/ and vault/ are not eligible
    whatever their frontmatter says — an inbox note is what an untrusted drop
    box can write, and archive/ is the previous answer to a question P is
    already answering.
    """
    for folder in notes.CANONICAL:
        base = k / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.md")):
            if path.name in ("index.md", "README.md"):
                continue
            text = notes.read_text(path)
            fm, err = notes.parse_frontmatter(text)
            yield folder, path, fm or {}, text, err


def count_candidates(k: Path) -> int:
    return sum(1 for _ in candidates(k))


def select(k: Path):
    """(published, refused). Refused is loud on purpose.

    A note marked public that cannot be published is not silently skipped: the
    operator approved it, so they believe it shipped, and the gap between what
    they believe and what a customer can see is the whole thing this command
    exists to keep closed.
    """
    published, refused = [], []
    for folder, path, fm, text, err in candidates(k):
        rel = path.relative_to(k.parent)
        if err:
            # A note whose frontmatter does not parse has no visibility as far
            # as this command can tell — and if it MEANT to be public, silently
            # leaving it out is the worst outcome: the operator approved it and
            # believes it shipped. lint would have caught this in M, so its
            # presence means something else is already wrong.
            if "visibility: public" in text:
                refused.append((rel, [f"its frontmatter does not parse ({err}), so "
                                      "nothing here can tell what it approves"]))
            continue
        if fm.get("visibility") != "public":
            continue
        blockers = notes.publish_blockers(folder, fm)
        if blockers:
            refused.append((rel, blockers))
            continue
        if not fm.get("id"):
            refused.append((rel, ["it has no id — every published note needs one, "
                                  "because ids are what links and search resolve"]))
            continue
        published.append({"folder": folder, "rel": path.relative_to(k),
                          "id": str(fm["id"]), "fm": fm, "text": text})
    return published, refused


# --------------------------------------------------------------- the human gate


def first_line(text: str, fm: dict) -> str:
    """A line of the note worth reading in a list. Headings are the template's
    words, not the author's, so they are skipped."""
    for line in text.splitlines()[fm.get("_end_line", 0):]:
        stripped = line.strip()
        if stripped and not stripped.startswith(("#", ">", "<!--", "-")):
            return stripped[:78]
    return ""


def pending(root: Path):
    """(decidable, never-publishable): notes with ABSENT visibility.

    `private` notes are decided and stay out. That distinction is the entire
    reason absent and private are different states: a queue that keeps
    re-asking about refusals is a queue nobody finishes, and a review queue
    nobody finishes is a review queue nobody reads.

    The same argument takes out the notes that COULD never be approved —
    everything under people/ and life/, and anything classified. On a brain
    with a hundred people in it those would be most of the queue, permanently,
    and there is no keystroke that clears them. They are counted rather than
    dropped: a queue that quietly hides notes is its own kind of lie.
    """
    decidable, blocked = [], []
    for folder, path, fm, text, err in candidates(root / "knowledge"):
        if err or fm.get("visibility") is not None:
            continue
        note = {"folder": folder, "path": path, "fm": fm,
                "id": str(fm.get("id") or path.stem),
                "created": str(fm.get("created") or ""),
                "line": first_line(text, fm)}
        (blocked if notes.publish_blockers(folder, fm) else decidable).append(note)
    order = lambda n: (n["created"], n["id"])          # noqa: E731 — one use
    return sorted(decidable, key=order, reverse=True), blocked


def review(root: Path) -> int:
    waiting, blocked = pending(root)
    if not waiting:
        print("Nothing waiting: every note that could be published has been "
              "approved or denied.")
        if blocked:
            print(f"({len(blocked)} note(s) can never be published — see below.)")
    else:
        print(f"{len(waiting)} note(s) nobody has decided about yet, newest first.\n")
    for note in waiting:
        print(f"  {note['created']}  {note['id']}")
        print(f"      {note['fm'].get('title', '')}")
        if note["line"]:
            print(f"      {note['line']}")
        print("")
    if blocked:
        folders = sorted({note["folder"] for note in blocked})
        print(f"  {len(blocked)} unreviewed note(s) are not listed because they can "
              "never be\n  published (" + ", ".join(f"{f}/" for f in folders) +
              ", or a sensitivity that is not `normal`).\n  Nothing you type here "
              "changes that, which is why they are not in the queue.\n")
    if waiting:
        print("  brain publish approve <id>     may be seen by customers")
        print("  brain publish deny <id>        never; and stop asking")
    return 0


def decide(root: Path, note_id: str, value: str) -> int:
    """Set `visibility` on one note, in place. Non-interactive on purpose.

    An interactive loop is welcome on top of this and is not this: a command
    that can only be driven by a human at a prompt is a command no test ever
    exercises, and this one decides what customers see.
    """
    for folder, path, fm, _text, err in candidates(root / "knowledge"):
        if err or str(fm.get("id") or "") != note_id:
            continue
        current = fm.get("visibility")
        if current == value:
            print(f"{note_id} is already {value} — nothing to do")
            return 0
        if value == "public":
            blockers = notes.publish_blockers(folder, fm)
            if blockers:
                # Refused here rather than written and left for the next
                # publish to reject: that puts the refusal in a place nobody is
                # looking, hours later, in a command about something else.
                print(f"REFUSED — {note_id} cannot be published:")
                for reason in blockers:
                    print(f"  - {reason}")
                return 1
        notes.fm_update(path, {"visibility": value})
        was = "unreviewed" if current is None else current
        print(f"{note_id}: {was} -> {value}  ({path.relative_to(root)})")
        if value == "public":
            print("It reaches customers at the next `brain publish`, not now.")
        return 0
    print(f"no current note in a canonical folder has the id {note_id!r} — "
          "`brain publish review` lists what is waiting")
    return 1


# -------------------------------------------------------------------- writing


def project(fm: dict) -> str:
    """The frontmatter block P gets: allowlisted fields, original order."""
    lines = ["---"]
    for field in PUBLISHED_FIELDS:
        if field not in fm or fm[field] in (None, ""):
            continue
        value = fm[field]
        if isinstance(value, list):
            value = "[" + ", ".join(str(v) for v in value) + "]"
        lines.append(f"{field}: {value}")
    lines.append("---")
    return "\n".join(lines)


def strip_links(body: str, published: set):
    """Demote every [[link]] whose target stayed behind. Returns (body, count).

    Safe because AGENTS.md already requires a note to stand up with the
    artifact absent — "the locator is for going deeper, never for
    understanding at all". Rejected: refusing to publish a note that links to a
    private one, which lets one link block an otherwise-approved note and
    tempts the operator into deleting the link from M to satisfy P.
    """
    stripped = 0

    def replace(match):
        nonlocal stripped
        inner = match.group(0)[2:-2]
        target = match.group(1).strip()
        if target in published:
            return match.group(0)
        stripped += 1
        # [[id|what the author called it]] keeps the author's words, which are
        # approved content. A bare [[id]] has nothing but the id, and the id is
        # the leak.
        _, sep, display = inner.partition("|")
        return display.strip() if sep and display.strip() else UNPUBLISHED_MARK

    return notes.WIKILINK_RE.sub(replace, body), stripped


def write_tree(target: Path, root: Path, selected: list):
    """Write P's knowledge/. Returns (links stripped, topics shipped)."""
    k = target / "knowledge"
    published = {note["id"] for note in selected}
    stripped_total, used_topics = 0, set()
    for note in selected:
        text = note["text"]
        fm = note["fm"]
        body = "\n".join(text.splitlines()[fm.get("_end_line", 0):]).lstrip("\n")
        body, stripped = strip_links(body, published)
        stripped_total += stripped
        topics = fm.get("topics") or []
        used_topics |= {str(t) for t in (topics if isinstance(topics, list) else [topics])}
        path = k / note["rel"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(project(fm) + "\n\n" + body.rstrip() + "\n", encoding="utf-8")

    # Every canonical folder exists in P even when empty, so the tree is a
    # brain rather than a shape that happens to match one today.
    for folder in list(notes.CANONICAL) + ["inbox", "journal", "archive", "vault",
                                           "attachments"]:
        directory = k / folder
        directory.mkdir(parents=True, exist_ok=True)
        if not any(directory.iterdir()):
            (directory / ".gitkeep").write_text("", encoding="utf-8")

    write_topics(k, root / "knowledge" / "topics.yaml", used_topics)
    write_index(k, selected)
    write_readme(target, len(selected))
    return stripped_total, used_topics


def write_topics(k: Path, source: Path, used: set) -> None:
    """P's vocabulary: the topics its own notes use, and nothing else.

    The topic names ARE the business's projects, clients and preoccupations,
    listed in a file nobody thinks of as a note. Comments go too — a comment is
    prose nobody reviewed for a customer's eyes.
    """
    kept = []
    if source.exists():
        for line in notes.read_text(source).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name = stripped.split(":", 1)[0].strip()
            if name in used:
                kept.append(stripped)
    for name in sorted(used):
        if not any(line.split(":", 1)[0].strip() == name for line in kept):
            # A topic a note uses but topics.yaml never declared. lint inside P
            # would reject the note; shipping the name keeps P valid and the
            # problem visible in M, where it belongs.
            kept.append(f"{name}:")
    (k / "topics.yaml").write_text(
        "# Compiled by `brain publish` — the topics used by published notes only.\n"
        + "\n".join(sorted(kept)) + "\n", encoding="utf-8")


def write_index(k: Path, selected: list) -> None:
    """A route map generated from what shipped.

    M's index.md describes M: its hubs, its projects, and links that would
    dangle here. It cannot travel, and a published brain with no route map is
    a brain whose first search has nowhere to start.
    """
    by_folder = {}
    for note in selected:
        by_folder.setdefault(note["folder"], []).append(note)
    lines = ["# Route map",
             "",
             "Compiled by `brain publish`. Every note here was approved, one at a",
             "time, for readers outside the brain it came from.",
             ""]
    for folder in notes.CANONICAL:
        found = sorted(by_folder.get(folder, []), key=lambda n: n["id"])
        if not found:
            continue
        lines += [f"## {folder}", ""]
        # 200 lines is lint's ceiling for a route map, and an oversized one
        # stops fitting in an agent's context long before that.
        for note in found[:40]:
            lines.append(f"- [[{note['id']}]] — {note['fm'].get('title', note['id'])}")
        if len(found) > 40:
            lines.append(f"- ...and {len(found) - 40} more in {folder}/")
        lines.append("")
    (k / "index.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_readme(target: Path, count: int) -> None:
    (target / "README.md").write_text(
        "# A compiled brain\n\n"
        f"{count} note(s), compiled by `brain publish` from a brain that is not "
        "here.\n\n"
        "**Do not edit anything in this directory.** It is rebuilt from zero on "
        "every\npublish, so an edit made here is an edit that disappears — and the "
        "note it\nwas meant for lives somewhere else.\n\n"
        "Serve it from inside this directory:\n\n"
        "    bin/brain serve --read-only --port 8788\n\n"
        "Everything here was approved for readers outside the brain it came from. "
        "Treat\nit as a public web page, because functionally that is what it is.\n",
        encoding="utf-8")


def copy_toolbelt(target: Path, root: Path) -> int:
    """P ships the tools, so it can be served without M being anywhere near.

    Tracked files only, exactly as `brain template` does it and for the same
    reason: walking the filesystem ships whatever is lying around, and
    .gitignore is where people ALREADY record what is machine-local.
    """
    prefixes = ("bin/", ".githooks/")
    exact = {".rgignore", ".gitignore"}
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=root,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip("\0").strip():
        wanted = [rel for rel in listed.stdout.split("\0")
                  if rel and (rel.startswith(prefixes) or rel in exact)]
    else:
        # Not a git checkout (a tarball, a copy). The blast radius is small —
        # these paths hold no notes — but say so rather than pretend.
        print("        [!] not a git repo; copying bin/ and hooks by directory walk")
        wanted = [str(p.relative_to(root)) for p in sorted(root.rglob("*"))
                  if p.is_file() and (str(p.relative_to(root)).startswith(prefixes)
                                      or p.name in exact)]
    copied = 0
    for rel in wanted:
        src = root / rel
        if not src.is_file() or src.is_symlink() or "__pycache__" in rel:
            continue
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        copied += 1
    return copied


# ---------------------------------------------------------------------- audit


def audit(target: Path, root: Path, published: set, vocabulary: set) -> list:
    """Re-ask every question of the tree that was actually built.

    Nothing here trusts the selection above. If the two ever disagree the
    answer is to refuse, because one of them is wrong and this is the one
    holding the thing a customer would read.
    """
    problems = []
    home = str(root)
    for path in sorted(target.rglob("*")):
        if not path.is_file() or ".git" in path.relative_to(target).parts:
            continue
        rel = path.relative_to(target)
        found = []
        notes.scan_secrets(path, found)
        problems += [f"{rel}: {message}" for _p, message in found]
        try:
            text = notes.read_text(path)
        except OSError as exc:
            problems.append(f"{rel}: unreadable in the output tree ({exc})")
            continue
        if home in text:
            problems.append(f"{rel}: names the brain it was compiled from ({home}) — "
                            "P must hold no path that reaches M")

    k = target / "knowledge"
    for folder, path, fm, text, err in candidates(k):
        rel = path.relative_to(target)
        if err:
            problems.append(f"{rel}: frontmatter does not parse in the output ({err})")
            continue
        if fm.get("visibility") != "public":
            problems.append(f"{rel}: shipped without `visibility: public` — only "
                            "notes a human approved may be here")
        if "sensitivity" in fm:
            problems.append(f"{rel}: carries a `sensitivity` field — no classification "
                            "of M's material belongs in a compiled brain")
        for reason in notes.publish_blockers(folder, fm):
            problems.append(f"{rel}: {reason}")
        for field in fm:
            if field != "_end_line" and field not in PUBLISHED_FIELDS:
                problems.append(f"{rel}: frontmatter field {field!r} is not on the "
                                "published allowlist")
        body = "\n".join(text.splitlines()[fm.get("_end_line", 0):])
        for link in notes.extract_links(body):
            if link not in published:
                problems.append(f"{rel}: links to [[{link}]], which is not published — "
                                "a dangling link names a note that stayed behind")
        topics = fm.get("topics") or []
        for topic in (topics if isinstance(topics, list) else [topics]):
            if str(topic) not in vocabulary:
                problems.append(f"{rel}: topic {topic!r} is outside the vocabulary "
                                "shipped with this brain")
    for folder in ("inbox", "journal", "archive", "vault"):
        stray = [p for p in (k / folder).rglob("*.md")] if (k / folder).is_dir() else []
        for path in stray:
            problems.append(f"{path.relative_to(target)}: {folder}/ must be empty in a "
                            "compiled brain — nothing there was ever approved")
    return problems


def swap(staging: Path, dest: Path) -> None:
    """Replace the published artifact with the tree that just passed its audit.

    Only what publish manages is removed, and never `.git`: P is deployed from
    a repo, and its history is the record of what was being served last week.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for name in MANAGED:
        path = dest / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    for item in sorted(staging.iterdir()):
        shutil.move(str(item), str(dest / item.name))


# --------------------------------------------------------------------- report


def manifest(dest: Path) -> dict:
    """{path: content hash} for the notes published at dest, or {} if none.

    Taken from git when the destination is a repo — P is deployed from one, so
    "what was last committed" is the closest thing to "what is being served" —
    and from the directory otherwise. No state file either way: a manifest kept
    beside the artifact is a manifest that can disagree with it.
    """
    k = dest / "knowledge"
    if not k.is_dir():
        return {}
    tracked = None
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=dest,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip("\0").strip():
        tracked = {rel for rel in listed.stdout.split("\0") if rel}
    out = {}
    for path in sorted(k.rglob("*.md")):
        rel = str(path.relative_to(dest))
        if tracked is not None and rel not in tracked:
            continue
        if path.name in ("index.md", "README.md"):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def report(previous: dict, current: dict, dest: Path, dry_run: bool = False) -> None:
    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    updated = sorted(rel for rel in set(current) & set(previous)
                     if current[rel] != previous[rel])
    where = f"{dest}" + (" (as last committed)" if (dest / ".git").exists() else "")
    print(f"\nchanges against what is published at {where}:")
    for label, group in (("added  ", added), ("updated", updated)):
        print(f"  {label} {len(group):>3}" + ("" if not group else
              "   " + ", ".join(short(rel) for rel in group[:8])
              + (f" and {len(group) - 8} more" if len(group) > 8 else "")))
    if removed:
        # Last and loudest. A removal is the one change that makes the bot
        # WORSE at its job: it will now say "I don't know" about something it
        # answered yesterday, and nothing else in this output says so.
        print("")
        print(f"  REMOVED {len(removed)} note(s) — this brain will stop answering "
              "anything they covered:")
        for rel in removed:
            print(f"    - {short(rel)}")
        print("  A superseded note leaves here until its successor is approved. If "
              "that\n  is what happened, `brain publish review` has the successor "
              "waiting.")
    else:
        print("  removed   0")
    if dry_run:
        print("  (nothing was written — this is what a real run would do)")


def short(rel: str) -> str:
    return rel[len("knowledge/"):] if rel.startswith("knowledge/") else rel
