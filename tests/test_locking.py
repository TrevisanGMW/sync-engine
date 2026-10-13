"""Regression tests for operating-system backup locking across processes."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
from sync_engine_locking import AlreadyRunningError, RunLock


class TestRunLock(unittest.TestCase):
    """Tests backup exclusion and automatic kernel cleanup without running a sync."""

    def setUp(self):
        """Creates an isolated lock directory and locates the app import root."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.application = Path(__file__).resolve().parents[1] / "sync_engine"

    def test_distinct_instances_cannot_acquire_together(self):
        """Prevents concurrent backup owners within the same process."""
        first_lock = RunLock(self.directory)
        second_lock = RunLock(self.directory)
        with first_lock:
            with self.assertRaises(AlreadyRunningError):
                second_lock.acquire()
            self.assertEqual(None, second_lock.handle)
        with second_lock:
            self.assertIsNotNone(second_lock.handle)

    def test_repeat_acquire_keeps_the_original_lock(self):
        """Ensures accidental repeated acquisition does not release the live lock."""
        first_lock = RunLock(self.directory)
        with first_lock:
            original_handle = first_lock.handle
            first_lock.acquire()
            self.assertIs(original_handle, first_lock.handle)
            with self.assertRaises(AlreadyRunningError):
                RunLock(self.directory).acquire()

    def test_release_without_acquire_and_repeated_release(self):
        """Makes cancellation and cleanup safe before or after acquisition."""
        run_lock = RunLock(self.directory)
        run_lock.release()
        run_lock.acquire()
        run_lock.release()
        run_lock.release()
        self.assertEqual(None, run_lock.handle)
        with RunLock(self.directory):
            pass

    def test_exception_releases_context_lock(self):
        """Releases the lock when a backup operation raises an error."""
        with self.assertRaisesRegex(RuntimeError, "test failure"):
            with RunLock(self.directory):
                raise RuntimeError("test failure")
        with RunLock(self.directory):
            pass

    def test_subprocess_contention_and_release(self):
        """Prevents another scheduled process from writing while a GUI owns the lock."""
        script = (
            "import sys\n"
            "from sync_engine_locking import AlreadyRunningError, RunLock\n"
            "try:\n"
            "    with RunLock(sys.argv[1]):\n"
            "        pass\n"
            "except AlreadyRunningError:\n"
            "    raise SystemExit(11)\n"
        )
        command = [sys.executable, "-c", script, str(self.directory)]
        with RunLock(self.directory):
            busy = subprocess.run(command, cwd=str(self.application), capture_output=True, timeout=10)
        available = subprocess.run(command, cwd=str(self.application), capture_output=True, timeout=10)
        self.assertEqual(11, busy.returncode, busy.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(0, available.returncode, available.stderr.decode("utf-8", errors="replace"))

    def test_kernel_releases_lock_after_abrupt_exit(self):
        """Recovers from a crashed worker without deleting a stale lock file."""
        script = (
            "import os, sys\n"
            "from sync_engine_locking import RunLock\n"
            "run_lock = RunLock(sys.argv[1]).acquire()\n"
            "os._exit(0)\n"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", script, str(self.directory)], cwd=str(self.application),
            capture_output=True, timeout=10,
        )
        self.assertEqual(0, crashed.returncode, crashed.stderr.decode("utf-8", errors="replace"))
        with RunLock(self.directory):
            pass


if __name__ == "__main__":
    unittest.main()
