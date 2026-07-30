#!/usr/bin/env python3
"""Tests for `brain publish` — the trust boundary.

Everything else in this repo can be wrong and cost the owner time. This one
can be wrong and cost somebody else their privacy: it decides which notes leave
a brain and reach a compiled artifact that a customer-facing bot reads out
loud. So the tests are written the way the compiler is — they assert on the
OUTPUT TREE, never on the intention of the code that produced it.

Two rules hold throughout: M is always a throwaway copy with its own knowledge/
(never this repo's), and no fixture contains a real person, business or
credential.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SANDBOX_IGNORE = shutil.ignore_patterns(
    ".git", ".cache", "graphify-out", "node_modules", ".DS_Store", "__pycache__")

TOPICS = """# the vocabulary
hours: opening, closing, when-open
pricing: prices, rates
staffing: rota, who-works-when
"""


def git(repo, *args, check=True):
    return subprocess.run(["git", "-c", "core.hooksPath=", "-c", "user.name=Sandbox",
                           "-c", "user.email=sandbox@example.invalid", *args],
                          cwd=repo, capture_output=True, text=True, check=check)


def make_brain(tmp) -> Path:
    """A throwaway M: this repo's toolbelt, an EMPTY knowledge/, its own git.

    The notes have to be the test's own. A test keyed to whatever happens to
    sit in this checkout either fails on the published template (which ships an
    empty knowledge/) or, worse, passes vacuously.
    """
    repo = Path(tmp) / "M"
    shutil.copytree(ROOT, repo, symlinks=True, ignore=SANDBOX_IGNORE)
    for generated in (repo / ".mcp.json", repo / "setup/skills/brain/SKILL.md"):
        if generated.exists():
            generated.unlink()
    knowledge = repo / "knowledge"
    for path in knowledge.rglob("*.md"):
        if path.name != "README.md":
            path.unlink()
    (knowledge / "index.md").write_text(
        "# Route map\n\nWhere things live.\n", encoding="utf-8")
    (knowledge / "topics.yaml").write_text(TOPICS, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "M")
    return repo


class PublishCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.cleanup)
        self.repo = make_brain(self.tmp.name)
        self.dest = Path(self.tmp.name) / "P"

    def cleanup(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    # ------------------------------------------------------------ fixtures

    def note(self, folder, kind, note_id, body="A fact a customer may need.",
             **fm):
        fields = {"id": note_id, "kind": kind, "title": note_id.replace("-", " "),
                  "topics": "[hours]", "aliases": f"[{note_id}]",
                  "created": "2026-07-01", "status": "current"}
        fields.update({k: v for k, v in fm.items() if v is not None})
        path = self.repo / "knowledge" / folder / f"{note_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\n" + "".join(f"{k}: {v}\n" for k, v in fields.items())
                        + "---\n\n" + body + "\n", encoding="utf-8")
        return path

    def publish(self, *flags, dest=None):
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), "publish",
             str(dest or self.dest), *flags],
            cwd=self.repo, capture_output=True, text=True, timeout=300)

    def published_ids(self):
        ids = set()
        for path in (self.dest / "knowledge").rglob("*.md"):
            if path.name in ("index.md", "README.md"):
                continue
            ids.add(path.stem)
        return ids

    def text_of(self, note_id):
        found = [p for p in (self.dest / "knowledge").rglob(f"{note_id}.md")]
        self.assertEqual(len(found), 1, f"{note_id} is not in the published tree")
        return found[0].read_text(encoding="utf-8")


class TestWhatShips(PublishCase):
    """Selection: compiled from what a human approved, never filtered at
    query time. A boundary that is a code path is one bug from a leak."""

    def test_a_public_note_ships(self):
        self.note("reference", "reference", "opening-hours", visibility="public")
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("opening-hours", self.published_ids())

    def test_a_private_note_does_not(self):
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.note("reference", "reference", "margin-model", visibility="private")
        self.assertEqual(self.publish().returncode, 0)
        self.assertNotIn("margin-model", self.published_ids())

    def test_an_unreviewed_note_does_not(self):
        """Absent visibility means nobody has looked at it. Fail closed: the
        default for every note that existed before this feature, and for every
        note `brain new` will ever write, is 'not published'."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.note("reference", "reference", "never-reviewed")
        self.assertEqual(self.publish().returncode, 0)
        self.assertNotIn("never-reviewed", self.published_ids())

    def test_provisional_folders_never_ship_whatever_they_claim(self):
        """inbox/, journal/ and archive/ are outside the canonical set, and a
        `visibility: public` sitting in one of them changes nothing. An inbox
        note is what an untrusted bot can write, so this is the path from the
        drop box straight to a customer if it were ever wrong."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        for folder, name in (("inbox", "2026-07-30-120000-planted"),
                             ("journal/2026", "2026-07-30"),
                             ("archive/reference", "old-hours")):
            path = self.repo / "knowledge" / folder / f"{name}.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"---\nid: {name}\nkind: reference\ntitle: planted\ntopics: [hours]\n"
                "created: 2026-07-01\nstatus: current\nvisibility: public\n---\n\n"
                "Something nobody approved.\n", encoding="utf-8")
        self.assertEqual(self.publish().returncode, 0)
        for name in ("2026-07-30-120000-planted", "2026-07-30", "old-hours"):
            self.assertNotIn(name, self.published_ids())

    def test_a_people_note_marked_public_is_refused_not_shipped(self):
        """lint would never let this be committed — and the compiler does not
        get to assume lint ran. The rule that decides what a customer sees is
        applied here too, over the notes themselves."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.note("people", "person", "a-colleague", sensitivity="normal",
                  visibility="public")
        out = self.publish()
        self.assertEqual(out.returncode, 1, "a people/ note reached the compiler\n"
                         + out.stdout)
        self.assertIn("a-colleague", out.stdout)

    def test_a_note_that_claims_public_but_does_not_parse_is_refused(self):
        """The worst outcome is the silent one: the operator approved it and
        believes it shipped. Nothing here can read what it approves, so it
        refuses out loud instead of quietly leaving it out."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        broken = self.repo / "knowledge" / "reference" / "broken.md"
        broken.write_text("---\nid: broken\nvisibility: public\nthis line is not "
                          "frontmatter\n---\n\nSomething.\n", encoding="utf-8")
        out = self.publish()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("broken", out.stdout)

    def test_the_published_tree_is_a_working_brain(self):
        """P is served by `brain serve --read-only` run from INSIDE it, so it
        has to be a brain, not a folder of markdown: its own toolbelt, its own
        index, and clean under its own lint."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertTrue((self.dest / "bin" / "brain").is_file(), "no toolbelt in P")
        lint = subprocess.run([sys.executable, str(self.dest / "bin" / "brain"), "lint"],
                              cwd=self.dest, capture_output=True, text=True, timeout=300)
        self.assertEqual(lint.returncode, 0, "P does not lint:\n" + lint.stdout)
        found = subprocess.run([sys.executable, str(self.dest / "bin" / "brain"),
                                "search", "opening hours"],
                               cwd=self.dest, capture_output=True, text=True, timeout=300)
        self.assertIn("opening-hours", found.stdout, "P cannot find its own note")

    def test_nothing_in_p_names_where_m_lives(self):
        """The path of the brain P was compiled from is the first thing anybody
        attacking it would like to know, and it has no business in an artifact
        a bot reads."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        for path in self.dest.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            self.assertNotIn(str(self.repo), body, f"{path} names M's location")


class TestWhatIsStripped(PublishCase):
    def test_only_allowlisted_frontmatter_survives(self):
        """An allowlist, not a denylist: a denylist publishes every field
        somebody adds later and forgets to exclude."""
        self.note("reference", "reference", "opening-hours", visibility="public",
                  review_by="2027-01-01", source="support-bot",
                  valid_from="2026-01-01")
        self.assertEqual(self.publish().returncode, 0)
        shipped = self.text_of("opening-hours")
        for dropped in ("review_by", "source", "valid_from"):
            self.assertNotIn(dropped, shipped, f"{dropped} reached the published copy")
        for kept in ("id:", "kind:", "title:", "topics:", "created:", "status:",
                     "visibility:"):
            self.assertIn(kept, shipped)

    def test_a_link_to_an_unpublished_note_becomes_plain_text(self):
        """A dangling [[2026-03-01-acquisition-talks]] leaks the existence, the
        date and the subject of a private note to anyone the bot talks to."""
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="We open at nine. See [[2026-03-01-acquisition-talks]] for why.")
        self.note("decisions", "decision", "2026-03-01-acquisition-talks",
                  visibility="private")
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stdout)
        shipped = self.text_of("opening-hours")
        self.assertNotIn("[[", shipped)
        self.assertNotIn("acquisition", shipped,
                         "the private note's subject shipped inside a broken link")

    def test_stripped_links_are_counted_out_loud(self):
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="See [[2026-03-01-acquisition-talks]].")
        self.note("decisions", "decision", "2026-03-01-acquisition-talks",
                  visibility="private")
        out = self.publish()
        self.assertRegex(out.stdout, r"1 .*link", "the strip happened silently")

    def test_a_link_between_two_published_notes_survives(self):
        """Stripping every link would also work and would be wrong: P is a
        brain, and brain_links is one of the four tools it serves."""
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="Prices are separate: [[price-list]].")
        self.note("reference", "reference", "price-list", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        self.assertIn("[[price-list]]", self.text_of("opening-hours"))

    def test_topics_yaml_holds_only_what_shipped(self):
        """The topic names ARE the business's projects, clients and
        preoccupations, in a file nobody thinks of as a note."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        vocabulary = (self.dest / "knowledge" / "topics.yaml").read_text(encoding="utf-8")
        self.assertIn("hours", vocabulary)
        for unused in ("pricing", "staffing", "rota", "who-works-when"):
            self.assertNotIn(unused, vocabulary,
                             "an unused topic name shipped with the artifact")

    def test_the_route_map_is_regenerated_not_copied(self):
        """M's index.md describes M — its hubs, its projects — and every link
        in it would dangle in P."""
        (self.repo / "knowledge" / "index.md").write_text(
            "# Route map\n\nSee the acquisition file and the staffing rota.\n",
            encoding="utf-8")
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        route = (self.dest / "knowledge" / "index.md").read_text(encoding="utf-8")
        self.assertNotIn("acquisition", route)
        self.assertIn("opening-hours", route)


class TestTheAudit(PublishCase):
    """The output is audited, not the input. Everything above could be right
    and one bug in the copy could still ship a note — so the last step reads
    the tree that was actually built."""

    def refusal(self, *flags):
        out = self.publish(*flags)
        self.assertEqual(out.returncode, 1, "the build was not refused\n" + out.stdout)
        return out.stdout

    def test_a_planted_secret_refuses_the_build(self):
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="the deploy key is ghp_" + "A" * 36)
        said = self.refusal()
        self.assertIn("REFUS", said.upper())

    def test_a_refused_build_leaves_nothing_behind(self):
        """Leaving an unclean tree on disk is how a refused build still gets
        published: the tree looks finished, the refusal has scrolled off, and
        nobody re-checks before restarting the server that serves it."""
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="the deploy key is ghp_" + "A" * 36)
        self.refusal()
        self.assertFalse((self.dest / "knowledge").exists(),
                         "the unclean tree was left on disk")

    def test_a_failed_rebuild_leaves_the_previous_artifact_serving(self):
        """The destination is what a bot is answering from right now. Building
        in place would mean a refused audit takes the live artifact down; it is
        built somewhere else and swapped in only once it is clean."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        before = self.text_of("opening-hours")
        self.note("reference", "reference", "price-list", visibility="public",
                  body="the deploy key is ghp_" + "A" * 36)
        self.refusal()
        self.assertEqual(self.text_of("opening-hours"), before,
                         "a refused build damaged the artifact being served")
        self.assertNotIn("price-list", self.published_ids())

    def test_a_refusal_names_every_problem_not_the_first(self):
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="key one ghp_" + "A" * 36)
        self.note("reference", "reference", "price-list", visibility="public",
                  body="key two AKIA" + "B" * 16)
        said = self.refusal()
        self.assertIn("opening-hours", said)
        self.assertIn("price-list", said)

    def test_a_sensitivity_field_in_the_output_refuses_the_build(self):
        """The allowlist should already have dropped it. The audit is what
        makes that a guarantee rather than a hope."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        broken = self.repo / "bin" / "brainlib" / "publish.py"
        source = broken.read_text(encoding="utf-8")
        self.assertIn("sensitivity", source,
                      "the compiler no longer mentions sensitivity — check this test "
                      "still exercises the audit rather than passing vacuously")
        # Simulate a compiler that forgot to strip it: widen the allowlist in M's
        # own copy of the tool, then publish with it.
        broken.write_text(source.replace('"visibility")', '"visibility", "sensitivity")'),
                          encoding="utf-8")
        self.note("reference", "reference", "price-list", visibility="public",
                  sensitivity="normal")
        said = self.refusal()
        self.assertIn("sensitivity", said)

    def test_there_is_no_way_to_publish_a_tree_that_failed(self):
        """No --force. There is no legitimate reason to ship a tree that failed
        its own audit, and a flag that exists gets used at 6pm on a Friday.

        The refusal names the flag rather than pretending not to understand it:
        somebody typing it has a problem, and "unknown flag" sends them looking
        for the right spelling instead of at the audit.
        """
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="the deploy key is ghp_" + "A" * 36)
        out = self.publish("--force")
        self.assertEqual(out.returncode, 2, out.stdout)
        self.assertFalse(self.dest.exists(), "--force wrote something")
        self.assertIn("no --force", out.stdout)


class TestTheReport(PublishCase):
    """A rebuild that quietly drops a note is the failure mode: the bot starts
    answering 'I don't know' about something it knew yesterday."""

    def first_build(self):
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.note("reference", "reference", "price-list", visibility="public")
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def test_a_first_build_reports_what_it_added(self):
        said = self.first_build()
        self.assertIn("opening-hours", said)

    def test_revoking_approval_removes_the_note_on_rebuild(self):
        """Rebuilt from zero every time, never patched. A note whose approval
        was withdrawn has to vanish, and the only way to guarantee that is to
        carry nothing forward."""
        self.first_build()
        note = self.repo / "knowledge" / "reference" / "price-list.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "visibility: public", "visibility: private"), encoding="utf-8")
        out = self.publish()
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("price-list", self.published_ids())

    def test_a_removal_is_reported_loudly(self):
        self.first_build()
        note = self.repo / "knowledge" / "reference" / "price-list.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "visibility: public", "visibility: private"), encoding="utf-8")
        said = self.publish().stdout
        self.assertIn("REMOVED", said.upper(),
                      "a note left the published brain without saying so")
        self.assertIn("price-list", said)

    def test_an_edit_is_reported_as_an_update(self):
        self.first_build()
        note = self.repo / "knowledge" / "reference" / "price-list.md"
        note.write_text(note.read_text(encoding="utf-8").replace(
            "A fact a customer may need.", "Prices went up on 2026-07-30."),
            encoding="utf-8")
        said = self.publish().stdout.lower()
        self.assertIn("updated", said)
        self.assertIn("price-list", said)

    def test_superseding_a_published_note_is_visible_in_the_report(self):
        """The dangerous case, because both halves are silent on their own: the
        successor is unreviewed so it does not ship, and the predecessor is
        archived so it is dropped. Correct — a changed price must not keep
        serving the old value — but the operator has to SEE it."""
        self.first_build()
        done = subprocess.run([sys.executable, str(self.repo / "bin" / "brain"),
                               "supersede", "price-list", "Price list 2026"],
                              cwd=self.repo, capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        said = self.publish().stdout
        self.assertIn("REMOVED", said.upper())
        self.assertIn("price-list", said)
        self.assertNotIn("price-list-2026", self.published_ids(),
                         "an unreviewed successor was published")


class TestDryRun(PublishCase):
    def test_it_writes_nothing(self):
        self.note("reference", "reference", "opening-hours", visibility="public")
        out = self.publish("--dry-run")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(self.dest.exists(), "--dry-run wrote to the destination")

    def test_it_still_reports_and_still_audits(self):
        """A dry run that skipped the audit would be a dry run of something
        else — the audit is most of what the operator is asking about."""
        self.note("reference", "reference", "opening-hours", visibility="public",
                  body="the deploy key is ghp_" + "A" * 36)
        out = self.publish("--dry-run")
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertFalse(self.dest.exists())

    def test_it_does_not_disturb_an_existing_build(self):
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        before = sorted(p.name for p in (self.dest / "knowledge").rglob("*.md"))
        self.note("reference", "reference", "price-list", visibility="public")
        self.assertEqual(self.publish("--dry-run").returncode, 0)
        after = sorted(p.name for p in (self.dest / "knowledge").rglob("*.md"))
        self.assertEqual(before, after, "--dry-run modified the live artifact")


class TestTheDestination(PublishCase):
    def test_it_refuses_to_publish_over_the_brain_itself(self):
        out = self.publish(dest=self.repo)
        self.assertEqual(out.returncode, 1)
        self.assertIn("knowledge", (self.repo / "knowledge").as_posix())
        self.assertTrue((self.repo / "bin" / "brain").is_file(), "M was damaged")

    def test_it_refuses_a_directory_it_does_not_own(self):
        """P is a compiled artifact, so publish deletes and rewrites it. A
        directory with somebody's own files in it is not that, and finding out
        afterwards is too late."""
        self.dest.mkdir()
        (self.dest / "notes-i-wrote.md").write_text("mine", encoding="utf-8")
        out = self.publish()
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertTrue((self.dest / "notes-i-wrote.md").exists(),
                        "publish deleted a file it did not create")

    def test_it_keeps_the_artifacts_own_git_history(self):
        """P is deployed from a repo. Wiping .git on every rebuild would throw
        away the record of what was served last week."""
        self.note("reference", "reference", "opening-hours", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        subprocess.run(["git", "init", "-q"], cwd=self.dest, check=True,
                       capture_output=True)
        git(self.dest, "add", "-A")
        git(self.dest, "commit", "-q", "-m", "first publish")
        head = git(self.dest, "rev-parse", "HEAD").stdout.strip()
        self.note("reference", "reference", "price-list", visibility="public")
        self.assertEqual(self.publish().returncode, 0)
        self.assertEqual(git(self.dest, "rev-parse", "HEAD").stdout.strip(), head,
                         "the rebuild lost P's history")


class TestTheReviewQueue(PublishCase):
    """The human gate, and the reason it is a CLI command and not a tool.

    Approval is an act by a person. A tool that lists what is pending is a tool
    that enumerates a brain's private notes to whoever holds a token, and
    putting the human gate on the wire defeats the human gate.
    """

    def brain(self, *args):
        return subprocess.run(
            [sys.executable, str(self.repo / "bin" / "brain"), *args],
            cwd=self.repo, capture_output=True, text=True, timeout=300)

    def test_it_lists_notes_nobody_has_reviewed(self):
        self.note("reference", "reference", "never-reviewed")
        out = self.brain("publish", "review")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("never-reviewed", out.stdout)

    def test_it_omits_notes_already_decided(self):
        """`private` means a human looked and said no. Asking again every week
        is how a review queue becomes a queue nobody finishes."""
        self.note("reference", "reference", "already-refused", visibility="private")
        self.note("reference", "reference", "already-approved", visibility="public")
        self.note("reference", "reference", "never-reviewed")
        out = self.brain("publish", "review")
        self.assertIn("never-reviewed", out.stdout)
        self.assertNotIn("already-refused", out.stdout)
        self.assertNotIn("already-approved", out.stdout)

    def test_it_shows_enough_to_decide_without_opening_the_file(self):
        self.note("reference", "reference", "weekend-cover",
                  body="Saturdays are covered by whoever opened on Friday.")
        out = self.brain("publish", "review")
        self.assertIn("weekend-cover", out.stdout)
        self.assertIn("Saturdays are covered", out.stdout)

    def test_it_is_newest_first(self):
        self.note("reference", "reference", "older-note", created="2026-01-01")
        self.note("reference", "reference", "newer-note", created="2026-07-01")
        out = self.brain("publish", "review")
        self.assertLess(out.stdout.index("newer-note"), out.stdout.index("older-note"),
                        "the oldest unreviewed note was listed first")

    def test_approve_sets_the_field_and_touches_nothing_else(self):
        path = self.note("reference", "reference", "opening-hours")
        before = path.read_text(encoding="utf-8")
        out = self.brain("publish", "approve", "opening-hours")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        after = path.read_text(encoding="utf-8")
        self.assertIn("visibility: public", after)
        self.assertEqual([l for l in before.splitlines() if l.strip()],
                         [l for l in after.splitlines()
                          if l.strip() and l != "visibility: public"],
                         "approve rewrote something other than the one field")

    def test_deny_records_the_refusal_rather_than_leaving_it_absent(self):
        self.note("reference", "reference", "margin-model")
        self.assertEqual(self.brain("publish", "deny", "margin-model").returncode, 0)
        text = (self.repo / "knowledge/reference/margin-model.md").read_text(
            encoding="utf-8")
        self.assertIn("visibility: private", text)
        self.assertNotIn("margin-model", self.brain("publish", "review").stdout,
                         "a refused note came back to the queue")

    def test_notes_that_could_never_be_published_are_counted_not_listed(self):
        """On a brain with a hundred people in it, people/ and life/ would be
        most of the queue, permanently, and no keystroke clears them. Counted
        rather than dropped: a queue that quietly hides notes is its own lie."""
        self.note("people", "person", "a-colleague", sensitivity="normal")
        self.note("reference", "reference", "opening-hours")
        out = self.brain("publish", "review")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("opening-hours", out.stdout)
        self.assertNotIn("a-colleague", out.stdout)
        self.assertIn("never be", out.stdout)
        self.assertIn("people/", out.stdout)

    def test_an_unknown_id_is_an_error(self):
        out = self.brain("publish", "approve", "no-such-note")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("no-such-note", out.stdout)

    def test_approve_refuses_a_note_that_could_never_be_published(self):
        """The check is the one lint applies, called here rather than copied.
        Writing the field and letting the next publish fail would put the
        refusal in a place nobody is looking."""
        self.note("people", "person", "a-colleague", sensitivity="normal")
        out = self.brain("publish", "approve", "a-colleague")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("people/", out.stdout, "the refusal does not name the rule")
        self.assertNotIn("visibility",
                         (self.repo / "knowledge/people/a-colleague.md").read_text(
                             encoding="utf-8"),
                         "a note that cannot be published was marked public anyway")

    def test_approve_refuses_a_personal_note(self):
        self.note("reference", "reference", "an-arrangement", sensitivity="personal")
        out = self.brain("publish", "approve", "an-arrangement")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("sensitivity", out.stdout)

    def test_approve_then_publish_ships_it(self):
        """The whole cycle, because each half passing on its own proves
        nothing about the join."""
        self.note("reference", "reference", "opening-hours")
        self.assertEqual(self.brain("publish", "approve", "opening-hours").returncode, 0)
        self.assertEqual(self.publish().returncode, 0)
        self.assertIn("opening-hours", self.published_ids())

    def test_the_tool_surface_did_not_grow(self):
        """No MCP tool was added here, and none ever will be. Approval over the
        wire is approval by whoever holds a token."""
        sys.path.insert(0, str(ROOT / "bin"))
        from brainlib import mcp

        self.assertEqual({t["name"] for t in mcp.TOOLS},
                         {"brain_search", "brain_read", "brain_links", "brain_recent",
                          "brain_capture"})
        for tool in mcp.TOOLS:
            self.assertNotIn("visibility", json.dumps(tool).lower())
            self.assertNotIn("publish", json.dumps(tool).lower())


if __name__ == "__main__":
    unittest.main()
