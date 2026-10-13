"""Tests for SyncEngine's readable text reports."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
from sync_engine_report import build_sync_report, write_sync_report


class TestSyncEngineReport(unittest.TestCase):
    """Checks that reports distinguish planned and completed work."""

    def setUp(self):
        self.plan = {"name": "Test plan"}
        self.preview = {
            "summary": {
                "copy": 2, "update": 1, "unchanged": 5, "delete": 1,
                "rmdir": 0, "conflict": 1, "skipped": 3, "bytes": 2048,
            },
            "operations": [{
                "kind": "copy", "pair_name": "Assets", "relative_path": "image.png", "size": 1024,
            }],
            "skipped": [{"kind": "skipped", "relative_path": "cache", "reason": "Excluded by rule"}],
            "warnings": ["Preview warning"],
        }
        self.result = {
            "status": "succeeded", "copied": 1, "deleted": 0, "bytes": 1024,
            "errors": [], "warnings": [],
        }

    def test_report_contains_plan_totals_conflicts_and_operations(self):
        report = build_sync_report(self.plan, self.preview, self.result, 3.5, "session.log")

        self.assertIn("SyncEngine Report", report)
        self.assertIn("Files transferred: 1", report)
        self.assertIn("Already up to date: 5", report)
        self.assertIn("Conflicts encountered: 1 (0 resolved)", report)
        self.assertIn("Excluded items skipped: 3", report)
        self.assertIn("Assets / image.png", report)
        self.assertIn("Preview warning", report)

    def test_report_writer_creates_a_timestamped_text_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_sync_report(self.plan, self.preview, self.result, directory)

            self.assertTrue(path.exists())
            self.assertEqual(".txt", path.suffix)
            self.assertIn("SyncEngine Report", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
