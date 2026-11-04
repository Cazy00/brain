#!/usr/bin/env python3
"""Runtime tests for brainlib/passsize.py (engine 0.8.0, I-070): the batch a consolidation
pass is given and the measurement of its change set. Pure functions, so no repository, no
git and no model: the integration with `brain consolidate` is in test_brain.py."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from brainlib import passsize  # noqa: E402


def note(stamp, size=500, kind="fact", source="local"):
    return {"path": f"knowledge/inbox/2026-11-{stamp}.md", "size": size, "kind": kind,
            "source": source, "targets": []}


def correction(stamp, *targets, size=400, source="local", basis="owner-said", retraction=False):
    item = note(stamp, size=size, kind="correction", source=source)
    item.update(basis=basis, retraction=retraction)
    item["targets"] = [{"name": name, "size": tsize, "canonical": canonical}
                       for name, tsize, canonical in targets]
    return item


class SelectBatchTests(unittest.TestCase):

    def test_an_empty_inbox_gives_an_empty_batch(self):
        batch = passsize.select_batch([])
        self.assertEqual((batch.items, batch.deferred, len(batch)), ([], [], 0))

    def test_oldest_first_by_capture_instant_whatever_the_given_order(self):
        items = [note("03-000000"), note("01-000000"), note("02-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.notes, [f"knowledge/inbox/2026-11-0{i}-000000.md" for i in (1, 2, 3)])

    def test_session_mining_proposals_are_never_in_the_batch(self):
        items = [note("01-000000", source="session-mining"), note("02-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-02-000000.md"])
        self.assertNotIn("knowledge/inbox/2026-11-01-000000.md", batch.allowed)

    def test_the_item_cap_stops_the_selection(self):
        # With the default budget the 7,000-char floor stops small notes at 14 first
        # (14 x 7,000 = 98,000): the 15-item cap is the backstop behind it.
        self.assertEqual(len(passsize.select_batch([note(f"01-{i:06d}") for i in range(40)])),
                         passsize.BUDGET_CHARS // passsize.MIN_ITEM_CHARS)
        batch = passsize.select_batch([note(f"01-{i:06d}") for i in range(40)],
                                      budget_chars=10 ** 9)
        self.assertEqual(len(batch), passsize.MAX_ITEMS)

    def test_the_character_budget_stops_the_selection(self):
        # 10,000 chars each costs 40,000: two fit in 100,000, the third does not.
        batch = passsize.select_batch([note(f"01-{i:06d}", size=10_000) for i in range(5)])
        self.assertEqual(len(batch), 2)
        self.assertLessEqual(batch.chars, passsize.BUDGET_CHARS)

    def test_the_file_estimate_stops_the_selection(self):
        budget = passsize.select_batch([note(f"01-{i:06d}") for i in range(40)], max_items=40,
                                       budget_chars=10 ** 9)
        self.assertEqual(len(budget), int(passsize.BUDGET_FILES / passsize.FILES_PER_NOTE))
        self.assertLessEqual(budget.files, passsize.BUDGET_FILES)

    def test_one_item_is_always_taken_however_large(self):
        batch = passsize.select_batch([note("01-000000", size=150_000), note("02-000000")])
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-01-000000.md"])

    def test_corrections_come_first_with_their_targets(self):
        target = "knowledge/inbox/2026-11-01-000000.md"
        items = [note("01-000000"), note("02-000000"),
                 correction("03-000000", (target, 500, False))]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-03-000000.md"])
        # Its target is drained with it, so it is allowed but not a note of its own.
        self.assertNotIn(target, batch.notes)
        self.assertIn(target, batch.allowed)
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-02-000000.md"])

    def test_corrections_over_the_budget_are_taken_oldest_first_and_the_rest_deferred(self):
        """Review finding 5: corrections alone over the budget must not block every pass."""
        held = [f"knowledge/inbox/2026-10-{d:02d}-000000.md" for d in range(1, 9)]
        items = [{"path": p, "size": 200, "kind": "fact", "source": "local", "targets": []}
                 for p in held]
        # Each correction names one inbox note and one canonical note of 20,000 chars:
        # max(7,000, 4 x 20,600) = 82,400 chars and 2 files, so only one fits the budget.
        for i, target in enumerate(held):
            items.append(correction(f"02-{i:06d}", (target, 200, False),
                                    ("some-canonical-note", 20_000, True), size=400))
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-02-000000.md"])
        self.assertEqual(len(batch.deferred), len(held) - 1)
        self.assertEqual(batch.deferred[0], "knowledge/inbox/2026-11-02-000001.md")
        # No note a deferred correction names is in the batch or allowed to go.
        for target in held[1:]:
            self.assertNotIn(target, batch.notes)
            self.assertNotIn(target, batch.allowed)
        self.assertIn(held[0], batch.allowed)

    def test_a_single_correction_over_the_budget_is_still_taken_and_a_note_goes_with_it(self):
        """0.8.0 review finding 2: a correction over the budget is taken alone among the
        corrections, and a waiting note still goes with it."""
        items = [correction("01-000000", ("big-note", 500_000, True)), note("02-000000"),
                 note("03-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-01-000000.md"])
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-02-000000.md"])

    # -- 0.8.0 review finding 2: a correction the pass leaves never starves the notes --

    def test_a_large_left_behind_drop_box_correction_lets_the_owners_notes_drain(self):
        bad = correction("01-000000", ("big-note", 30_000, True), source="drop-box")
        owners = [note(f"02-{i:06d}") for i in range(5)]
        batch = passsize.select_batch([bad] + owners)
        self.assertEqual(batch.notes, [n["path"] for n in owners])
        # It is weighed, not applied: its target's size is not its cost.
        self.assertLess(batch.chars, passsize.BUDGET_CHARS)

    def test_a_trusted_correction_left_behind_every_pass_still_leaves_notes_a_part(self):
        stuck = correction("01-000000", ("big-note", 25_000, True))
        owners = [note(f"02-{i:06d}") for i in range(5)]
        for _ in range(3):                               # the same inbox, pass after pass
            batch = passsize.select_batch([stuck] + owners)
            self.assertEqual(batch.corrections, [stuck["path"]])
            self.assertTrue(batch.notes, "the owner's notes got no part of the pass")

    def test_corrections_take_at_most_their_share_while_notes_wait(self):
        fixes = [correction(f"01-{i:06d}") for i in range(12)]
        owners = [note(f"02-{i:06d}") for i in range(12)]
        batch = passsize.select_batch(fixes + owners)
        self.assertLessEqual(len(batch.corrections) * passsize.MIN_ITEM_CHARS,
                             passsize.BUDGET_CHARS * passsize.CORRECTION_SHARE)
        self.assertGreaterEqual(len(batch.notes), len(batch.corrections))
        # With no note waiting they may take the whole budget.
        alone = passsize.select_batch(fixes)
        self.assertGreater(len(alone.corrections), len(batch.corrections))

    def test_corrections_the_pass_can_act_on_come_before_the_others(self):
        items = [correction("01-000000", source="drop-box"),
                 correction("02-000000", basis="agent-inferred"),
                 correction("03-000000"),
                 correction("04-000000", basis="agent-found"),
                 correction("05-000000", basis="agent-found", retraction=True),
                 correction("06-000000", retraction=True)]
        batch = passsize.select_batch(items)
        order = [p[len("knowledge/inbox/2026-11-"):-len("-000000.md")] for p in batch.corrections]
        self.assertEqual(order, ["03", "04", "06", "01", "02", "05"])

    def test_only_a_local_correction_holds_the_inbox_note_it_names(self):
        x, y = "knowledge/inbox/2026-10-01-000000.md", "knowledge/inbox/2026-10-02-000000.md"
        items = [{"path": p, "size": 100, "kind": "fact", "source": "local", "targets": []}
                 for p in (x, y)]
        items += [correction("01-000000", (x, 100, False), basis="agent-inferred"),
                  correction("02-000000", (y, 100, False), source="drop-box")]
        batch = passsize.select_batch(items)
        self.assertNotIn(x, batch.allowed)      # the engine's check would block draining it
        self.assertNotIn(x, batch.notes)
        self.assertIn(y, batch.notes)           # a drop box holds nothing back

    # -- 0.8.0 review finding 3: corrections that name the same inbox note go together --

    def shared(self, first_cost):
        x = "knowledge/inbox/2026-10-01-000000.md"
        items = [{"path": x, "size": 100, "kind": "fact", "source": "local", "targets": []},
                 correction("01-000000", (x, 100, False), ("big", 24_000, True)),
                 correction("01-000001", (x, 100, False))]
        if first_cost:
            items.append(correction("00-000000", ("other-big", first_cost, True)))
        return x, items

    def test_two_corrections_naming_one_note_are_taken_together(self):
        x, items = self.shared(0)
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-01-000000.md",
                                             "knowledge/inbox/2026-11-01-000001.md"])
        self.assertEqual(batch.deferred, [])
        self.assertIn(x, batch.allowed)

    def test_two_corrections_naming_one_note_are_deferred_together_and_it_is_not_allowed(self):
        x, items = self.shared(20_000)
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-00-000000.md"])
        self.assertEqual(batch.deferred, ["knowledge/inbox/2026-11-01-000000.md",
                                          "knowledge/inbox/2026-11-01-000001.md"])
        self.assertNotIn(x, batch.allowed)
        self.assertNotIn(x, batch.notes)

    def test_cut_defers_a_kept_correction_that_shares_a_note_with_one_cut_away(self):
        x, items = self.shared(0)
        batch = passsize.select_batch(items + [note("09-000000")])
        smaller = passsize.cut(batch, 2)          # room for one correction and one note
        self.assertEqual(smaller.corrections, [])
        self.assertEqual(smaller.deferred[:2], batch.corrections)
        self.assertNotIn(x, smaller.allowed)
        self.assertEqual(smaller.notes, ["knowledge/inbox/2026-11-09-000000.md"])

    def test_cut_keeps_the_deferred_corrections_oldest_first(self):
        """0.8.0 review finding 8: in the selection's order, not re-sorted as a set."""
        items = [correction("03-000000"), correction("02-000000", source="drop-box")]
        batch = passsize.select_batch(items, max_items=1)
        self.assertEqual(batch.deferred, ["knowledge/inbox/2026-11-02-000000.md"])
        smaller = passsize.cut(passsize.select_batch(items), 0)
        self.assertEqual(smaller.deferred, ["knowledge/inbox/2026-11-02-000000.md"])
        three = [correction("01-000000"), correction("02-000000"), correction("03-000000")]
        batch = passsize.select_batch(three, max_items=2)
        self.assertEqual(passsize.cut(batch, 1).deferred,
                         ["knowledge/inbox/2026-11-02-000000.md",
                          "knowledge/inbox/2026-11-03-000000.md"])

    # -- 0.8.0 review finding 4: an item no audited pass can drain is set aside --

    def test_an_item_alone_over_the_diff_cap_is_set_aside_and_the_rest_go_on(self):
        huge = note("01-000000", size=210_000)
        batch = passsize.select_batch([huge, note("02-000000"), note("03-000000")])
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-02-000000.md",
                                       "knowledge/inbox/2026-11-03-000000.md"])
        self.assertNotIn(huge["path"], batch.allowed)
        self.assertIn("alone over the audit's 200000 diff chars", batch.set_aside[huge["path"]])

    def test_a_path_the_engine_set_aside_is_skipped(self):
        first = note("01-000000")
        batch = passsize.select_batch([first, note("02-000000")],
                                      set_aside={first["path"]: "over caps alone"})
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-02-000000.md"])
        self.assertEqual(batch.set_aside, {first["path"]: "over caps alone"})
        self.assertEqual(passsize.cut(batch, 1).set_aside, batch.set_aside)

    def test_cut_keeps_the_first_items_and_defers_the_corrections_it_drops(self):
        target = "knowledge/inbox/2026-10-01-000000.md"
        items = [{"path": target, "size": 100, "kind": "fact", "source": "local", "targets": []},
                 correction("01-000000", (target, 100, False)),
                 note("02-000000"), note("03-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(len(batch), 3)
        smaller = passsize.cut(batch, 2)
        self.assertEqual(smaller.items, batch.items[:2])
        self.assertIn(target, smaller.allowed)
        none_left = passsize.cut(batch, 0)       # never below one item
        self.assertEqual(len(none_left), 1)


class NamedTargetTests(unittest.TestCase):
    """0.8.1: the inbox notes a batch's corrections name are part of the batch."""

    TARGET = "knowledge/inbox/2026-11-02-024027-decision.md"

    def test_a_taken_correction_brings_its_inbox_target(self):
        items = [note("02-024027-decision", size=600),
                 correction("02-024043-fix", (self.TARGET, 600, False))]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-02-024043-fix.md"])
        self.assertEqual(batch.named, [self.TARGET])
        self.assertIn(self.TARGET, batch.allowed)
        self.assertNotIn(self.TARGET, batch.notes)
        self.assertEqual(batch.chars, passsize._cost(400 + 600))
        kept = passsize.cut(batch, 1)
        self.assertEqual((kept.corrections, kept.named), (batch.corrections, [self.TARGET]))

    def test_a_deferred_correction_keeps_its_inbox_target_held(self):
        items = [correction("01-000000", ("knowledge/reference/big.md", 30_000, True)),
                 correction("02-024043-fix", (self.TARGET, 600, False)),
                 note("02-024027-decision", size=600), note("03-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.deferred, ["knowledge/inbox/2026-11-02-024043-fix.md"])
        self.assertEqual(batch.named, [])
        self.assertNotIn(self.TARGET, batch.allowed)
        self.assertNotIn(self.TARGET, batch.notes)

    def test_an_untrusted_correction_names_nothing_it_may_drain(self):
        items = [note("02-024027-decision"),
                 correction("02-024043-fix", (self.TARGET, 600, False), basis="agent-inferred")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.named, [])
        self.assertNotIn(self.TARGET, batch.allowed)


class SetAsideTargetTests(unittest.TestCase):
    """0.8.1 review finding 4: a set-aside note a trusted correction names gets one
    instruction. It stays set aside and the correction is deferred with it."""

    def check(self, batch, target, fix):
        self.assertIn(target, batch.set_aside)
        self.assertIn(fix, batch.deferred)
        self.assertNotIn(fix, batch.corrections)
        self.assertNotIn(target, batch.named)
        self.assertNotIn(target, batch.allowed)
        self.assertNotIn(target, batch.items)
        kept = passsize.cut(batch, 1)
        self.assertNotIn(target, kept.allowed)
        self.assertIn(fix, kept.deferred)

    def test_a_retraction_of_a_note_alone_over_the_diff_cap_waits_with_it(self):
        huge = note("01-000000", size=210_000)
        fix = correction("02-000000", (huge["path"], 210_000, False), retraction=True)
        batch = passsize.select_batch([huge, fix, note("03-000000")])
        self.check(batch, huge["path"], fix["path"])
        self.assertEqual(batch.notes, ["knowledge/inbox/2026-11-03-000000.md"])

    def test_a_correction_of_a_note_the_engine_set_aside_waits_with_it(self):
        first = note("01-000000")
        fix = correction("02-000000", (first["path"], 500, False))
        partner = correction("02-000001", (first["path"], 500, False), basis="agent-found")
        batch = passsize.select_batch([first, fix, partner, note("03-000000")],
                                      set_aside={first["path"]: "over caps alone"})
        self.check(batch, first["path"], fix["path"])
        self.assertIn(partner["path"], batch.deferred)

    def test_a_correction_whose_target_is_not_set_aside_is_taken(self):
        first = note("01-000000")
        fix = correction("02-000000", (first["path"], 500, False))
        batch = passsize.select_batch([first, fix])
        self.assertEqual((batch.corrections, batch.named), ([fix["path"]], [first["path"]]))


class RetrySizeTests(unittest.TestCase):

    def test_over_the_character_cap_with_no_canonical_file_is_sized_by_characters(self):
        """Review finding 3: 0 canonical files must not divide by zero."""
        self.assertEqual(passsize.retry_size(15, 0, 250_000), 6)

    def test_over_the_file_cap_only(self):
        self.assertEqual(passsize.retry_size(15, 31, 1_000), 7)     # min(15 // 2, 9)
        self.assertEqual(passsize.retry_size(10, 40, 1_000), 5)

    def test_never_above_half_or_below_one(self):
        self.assertEqual(passsize.retry_size(15, 31, 10), 7)
        self.assertEqual(passsize.retry_size(2, 400, 10_000_000), 1)
        self.assertEqual(passsize.retry_size(1, 400, 10_000_000), 1)
        self.assertEqual(passsize.retry_size(9, 0, 0), 4)


class MeasureTests(unittest.TestCase):

    def test_a_rename_counts_once_and_archive_counts(self):
        raw = (b"R087\0knowledge/decisions/a.md\0knowledge/archive/decisions/a.md\0"
               b"A\0knowledge/decisions/b.md\0"
               b"A\0knowledge/archive/reference/c.md\0")
        rows = passsize.parse_name_status_z(raw)
        self.assertEqual(passsize.measure(rows, "x" * 12)["canonical_files"], 3)
        self.assertEqual(passsize.measure(rows, "x" * 12)["diff_chars"], 12)

    def test_inbox_handoff_and_journal_paths_do_not_count(self):
        raw = (b"D\0knowledge/inbox/a.md\0A\0knowledge/handoff/acme/a.md\0"
               b"M\0knowledge/journal/2026/2026-11-04.md\0M\0knowledge/index.md\0")
        rows = passsize.parse_name_status_z(raw)
        self.assertEqual(passsize.measure(rows, "")["canonical_files"], 0)

    def test_an_inbox_capture_git_pairs_with_its_promoted_note_counts_once(self):
        rows = passsize.parse_name_status_z(
            b"R060\0knowledge/inbox/a.md\0knowledge/reference/a.md\0")
        self.assertEqual(passsize.measure(rows, "")["canonical_files"], 1)

    def test_an_unreadable_shape_is_none(self):
        self.assertIsNone(passsize.parse_name_status_z(b"R100\0knowledge/decisions/a.md\0"))

    def test_the_caps(self):
        self.assertFalse(passsize.over_caps({"canonical_files": 30, "diff_chars": 200_000}))
        self.assertTrue(passsize.over_caps({"canonical_files": 31, "diff_chars": 1}))
        self.assertTrue(passsize.over_caps({"canonical_files": 0, "diff_chars": 200_001}))


if __name__ == "__main__":
    unittest.main()
