import tempfile
import unittest
from datetime import timezone
from pathlib import Path

import common


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_atomic_write_round_trips_and_leaves_no_temp_files(self):
        path = self.root / "a.json"
        common.atomic_write(path, {"x": 1})
        self.assertEqual(common.load_json(path, None), {"x": 1})
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.json"])

    def test_a_missing_file_gives_the_default(self):
        self.assertEqual(common.load_json(self.root / "no.json", {"d": 1}), {"d": 1})

    def test_a_corrupt_file_is_an_error_not_an_empty_file(self):
        path = self.root / "bad.json"
        path.write_text("{ nope")
        with self.assertRaises(common.StateError):
            common.load_json(path, {})

    def test_weekly_notes_start_empty_and_round_trip(self):
        paths = common.Paths(self.root)
        weekly = common.load_weekly(paths)
        self.assertEqual(weekly, {"suggested": {}})
        weekly["last_profile_date"] = "2026-12-01"
        common.save_weekly(paths, weekly)
        self.assertEqual(common.load_weekly(paths)["last_profile_date"], "2026-12-01")

    def test_an_unreadable_weekly_file_raises(self):
        paths = common.Paths(self.root)
        paths.weekly.write_text("{ nope")
        with self.assertRaises(common.StateError):
            common.load_weekly(paths)


class DateTests(unittest.TestCase):
    def test_days_between_and_minus(self):
        self.assertEqual(common.days_between("2026-10-01", "2026-10-31"), 30)
        self.assertEqual(common.date_minus_days("2026-03-01", 1), "2026-02-28")

    def test_parse_iso_handles_reader_timestamps(self):
        self.assertEqual(common.parse_iso("2026-10-27T03:45:47.782350+00:00").hour, 3)
        self.assertEqual(common.parse_iso("2026-10-27T03:45:47Z").tzinfo, timezone.utc)
        self.assertEqual(common.parse_iso("2026-10-27").tzinfo, timezone.utc)
        self.assertIsNone(common.parse_iso(None))
        self.assertIsNone(common.parse_iso("garbage"))


if __name__ == "__main__":
    unittest.main()
