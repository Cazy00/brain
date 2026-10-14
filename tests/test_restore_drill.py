"""The restore drill's own guarantees that need no network or identity."""
import re
import unittest
from pathlib import Path

DRILL = Path(__file__).resolve().parent.parent / "deploy" / "backup" / "restore-drill.sh"


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


if __name__ == "__main__":
    unittest.main()
