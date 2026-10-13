"""Tests for cooperative worker timeout reporting."""

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
import sync_engine_engine
from sync_engine_model import new_plan
from sync_engine_worker import BackupWorker


class TestBackupWorkerTimeout(unittest.TestCase):
    """Keeps timeout outcomes distinct from user cancellation."""

    def test_timeout_during_scan_reports_timed_out(self):
        results = []

        def scan(plan, cancel_event, progress_callback, **unused):
            progress_callback({"phase": "scan"})
            if cancel_event.is_set():
                raise sync_engine_engine.SyncCancelled("cancelled")

        with tempfile.TemporaryDirectory() as directory:
            worker = BackupWorker(new_plan(), directory, deadline=time.monotonic() - 1)
            worker.result_ready.connect(results.append)
            with mock.patch("sync_engine_worker.build_preview", side_effect=scan):
                worker.run()

        self.assertEqual("timed_out", results[0]["status"])
        self.assertIn("timeout elapsed", results[0]["warnings"][0])

    def test_timeout_during_transfer_preserves_completed_counts(self):
        results = []

        def execute(preview, cancel_event, progress_callback, allow_delete=False):
            progress_callback({"phase": "copy"})
            return {
                "status": "cancelled", "copied": 2, "deleted": 0, "bytes": 512,
                "errors": [], "warnings": [],
            }

        with tempfile.TemporaryDirectory() as directory:
            worker = BackupWorker(
                new_plan(), directory, preview={"operations": []}, deadline=time.monotonic() - 1,
            )
            worker.result_ready.connect(results.append)
            with mock.patch("sync_engine_worker.execute_preview", side_effect=execute):
                worker.run()

        self.assertEqual("timed_out", results[0]["status"])
        self.assertEqual(2, results[0]["copied"])
        self.assertEqual(512, results[0]["bytes"])

    def test_worker_does_not_throttle_operation_completion_messages(self):
        """Emits short-file completion so preview rows can show executed status."""
        worker = BackupWorker(new_plan(), tempfile.gettempdir(), preview={"operations": []})
        progress = []
        worker.progress.connect(progress.append)
        worker._report_progress({
            "phase": "copy", "completed_operations": 0, "total_operations": 3,
            "message": "Copying short.txt",
        })
        worker._report_progress({
            "phase": "copy", "completed_operations": 1, "total_operations": 3,
            "message": "Completed copy: short.txt",
        })
        self.assertEqual(2, len(progress))
        self.assertEqual("Completed copy: short.txt", progress[-1]["message"])


if __name__ == "__main__":
    unittest.main()
