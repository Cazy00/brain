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


def correction(stamp, *targets, size=400):
    item = note(stamp, size=size, kind="correction")
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
        batch = passsize.select_batch([note("01-000000", size=900_000), note("02-000000")])
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

    def test_a_single_correction_over_the_budget_is_still_taken_alone(self):
        items = [correction("01-000000", ("big-note", 500_000, True)), note("02-000000")]
        batch = passsize.select_batch(items)
        self.assertEqual(batch.corrections, ["knowledge/inbox/2026-11-01-000000.md"])
        self.assertEqual(batch.notes, [])

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
