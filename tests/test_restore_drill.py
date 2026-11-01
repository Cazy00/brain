"""The restore drill's own guarantees that need no network or identity."""
import re
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKUP = Path(__file__).resolve().parent.parent / "deploy" / "backup"
DRILL = BACKUP / "restore-drill.sh"
RETENTION_LIB = BACKUP / "lib-retention.sh"
NOW = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)


def key(age_hours: float) -> str:
    """A daily object key made `age_hours` before NOW, named the way snapshot.sh names it."""
    return "brain/daily/brain-%sZ.tar.gz.age" % (NOW - timedelta(hours=age_hours)).strftime("%Y%m%dT%H%M%S")


def audit(ages_hours, keep=7, lock_days=3, cls="daily"):
    """Run retention_audit on keys of the given ages (newest last, as the listing sorts them)."""
    keys = sorted(key(a) for a in ages_hours)
    out = subprocess.run(
        ["bash", "-c", '. "$1"; retention_audit "$2" "$3" "$4" "$5"', "x",
         str(RETENTION_LIB), cls, str(keep), str(lock_days), str(int(NOW.timestamp()))],
        input="\n".join(keys) + ("\n" if keys else ""), capture_output=True, text=True, check=False)
    return out.returncode, out.stdout.strip()


class RestoreDrillTests(unittest.TestCase):
    def test_the_archive_is_extracted_as_the_user_running_the_drill(self):
        """Run as root (a recovery host's monthly timer), tar would keep the brain
        container's uid on every file, and git then refuses the restored repository
        ("dubious ownership"): HEAD reads as none and the drill fails a good backup."""
        extract = [line for line in DRILL.read_text().splitlines()
                   if re.search(r"\btar\b.*-x", line)]
        self.assertTrue(extract, "no extraction line found")
        for line in extract:
            self.assertIn("--no-same-owner", line)

    def test_exactly_the_retention_count_passes(self):
        rc, line = audit([24 * d for d in range(7)])
        self.assertEqual((rc, line), (0, "ok daily: 7/7 recovery points"))

    def test_fewer_than_the_retention_count_passes(self):
        rc, line = audit([24 * d for d in range(3)])
        self.assertEqual((rc, line), (0, "ok daily: 3/7 recovery points"))

    def test_a_fresh_surplus_is_the_lock_not_a_failure(self):
        """Nine points: seven nights plus two upgrade-day extras. The two oldest are
        the surplus; both are inside the 3-day lock, so the bucket cannot delete them."""
        ages = [2, 8, 20, 30, 44, 50, 60, 66, 70]
        rc, line = audit(ages)
        self.assertEqual((rc, line),
                         (0, "ok daily: 9 recovery points, 2 still under the 3-day lock"))

    def test_an_old_surplus_fails(self):
        """The oldest of nine is 6 days old: past the lock, so pruning is not running."""
        ages = [2, 8, 20, 30, 44, 50, 60, 66, 144]
        rc, line = audit(ages)
        self.assertEqual(rc, 1)
        self.assertTrue(line.startswith("bad daily: 9 recovery points, retention says 7"), line)
        self.assertIn("pruning is not running", line)

    def test_one_old_surplus_point_among_fresh_ones_fails(self):
        ages = [2, 8, 20, 30, 44, 50, 60, 70, 150]
        rc, _ = audit(ages)
        self.assertEqual(rc, 1)

    def test_a_point_whose_lock_lapsed_since_the_last_prune_passes(self):
        """3 days plus 6 hours: unprunable at last night's run, prunable at the next."""
        ages = [2, 8, 20, 30, 44, 50, 60, 66, 24 * 3 + 6]
        self.assertEqual(audit(ages)[0], 0)

    def test_only_the_surplus_is_dated_not_the_kept_points(self):
        """A kept point may be any age (a monthly one is a year old); only the oldest
        count-minus-keep points are the surplus."""
        ages = [24 * 300, 24 * 200, 24 * 100, 24 * 40, 24 * 5, 24 * 2, 24, 2]
        rc, line = audit(ages, keep=12, cls="monthly")
        self.assertEqual((rc, line), (0, "ok monthly: 8/12 recovery points"))

    def test_the_lock_window_is_the_setting(self):
        ages = [2, 8, 20, 30, 44, 50, 60, 66, 24 * 5]
        self.assertEqual(audit(ages, lock_days=3)[0], 1)
        self.assertEqual(audit(ages, lock_days=5)[0], 0)

    def test_an_unreadable_timestamp_is_not_excused(self):
        out = subprocess.run(
            ["bash", "-c", '. "$1"; retention_audit daily 1 3 "$2"', "x", str(RETENTION_LIB),
             str(int(NOW.timestamp()))],
            input="brain/daily/brain-garbage.tar.gz.age\n" + key(1) + "\n",
            capture_output=True, text=True, check=False)
        self.assertEqual(out.returncode, 1, out.stdout)

    def test_the_drill_uses_the_audit_and_a_documented_default_window(self):
        text = DRILL.read_text()
        self.assertIn("retention_audit", text)
        self.assertIn("BRAIN_BACKUP_LOCK_DAYS:-3", text)
        self.assertNotIn("pruning is not running", text.replace("retention_audit", ""))


if __name__ == "__main__":
    unittest.main()
