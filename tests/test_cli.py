"""Exercise the actual pure-Python app entry point without Qt."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
from sync_engine_model import new_plan, save_action, save_plan


class TestCli(unittest.TestCase):
    """Run previews and automatic actions exclusively on temporary folders."""

    def setUp(self):
        """Create disposable plan paths and isolate user-local storage."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.plan = new_plan("CLI plan")
        self.plan["pairs"][0].update({"source": str(self.source), "destination": str(self.destination)})
        self.plan_path = self.root / "test.syncplan"
        self.action_path = self.root / "test.syncrun"
        self.environment = dict(os.environ)
        self.environment["LOCALAPPDATA"] = str(self.root / "user")
        self.environment["XDG_DATA_HOME"] = str(self.root / "user")
        self.application_path = Path(__file__).resolve().parents[1] / "sync_engine" / "sync_engine.py"

    def tearDown(self):
        """Clean up all files created by the test process."""
        self.temporary.cleanup()

    def _command(self, *arguments):
        """Invoke the real entry point with a bounded wait.

        Args:
            *arguments (str): Command line arguments to pass to the app.

        Returns:
            subprocess.CompletedProcess: Exit status and captured UTF-8 output.
        """
        return subprocess.run(
            [sys.executable, str(self.application_path), *map(str, arguments)],
            env=self.environment, capture_output=True, text=True,
            encoding="utf-8", timeout=20,
        )

    def test_json_preview_does_not_create_destination(self):
        """Verify CLI preview is read-only and returns structured counts."""
        (self.source / "file.txt").write_text("preview", encoding="utf-8")
        self.plan["pairs"][0].update({
            "source": "{plan-dir}/source",
            "destination": "{plan-dir}/destination",
        })
        save_plan(self.plan, self.plan_path)
        completed = self._command("--preview", self.plan_path, "--json")
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        self.assertEqual(1, json.loads(completed.stdout)["summary"]["copy"])
        self.assertFalse(self.destination.exists())

    def test_headless_action_copies_with_no_qt(self):
        """Verify a scheduled-style action works using pure Python."""
        (self.source / "file.txt").write_text("scheduled", encoding="utf-8")
        self.plan["variables"] = {"backup_root": str(self.destination.parent)}
        self.plan["pairs"][0].update({
            "source": "{sync-file-dir}/source",
            "destination": "{backup_root}/destination",
        })
        save_action(self.plan, self.action_path)
        completed = self._command("--run", self.action_path, "--headless", "--countdown", "0", "--json")
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        self.assertEqual("succeeded", json.loads(completed.stdout)["status"])
        self.assertEqual("scheduled", (self.destination / "file.txt").read_text(encoding="utf-8"))

    def test_headless_action_requires_confirmation_in_the_gui(self):
        """Never bypasses a plan's confirmation mode when Qt prompts are unavailable."""
        self.plan["automation"]["start_mode"] = "confirm"
        save_action(self.plan, self.action_path)
        completed = self._command("--run", self.action_path, "--headless", "--json")
        self.assertEqual(2, completed.returncode, completed.stderr + completed.stdout)
        self.assertIn("requires confirmation", completed.stderr)
        self.assertFalse(self.destination.exists())

    def test_plain_plan_cannot_authorize_headless_deletions(self):
        """Verify a mirror plan alone cannot delete destination extras."""
        (self.source / "file.txt").write_text("source", encoding="utf-8")
        self.destination.mkdir()
        (self.destination / "extra.txt").write_text("retain", encoding="utf-8")
        self.plan["pairs"][0]["mode"] = "mirror"
        self.plan["automation"]["allow_deletions"] = True
        save_plan(self.plan, self.plan_path)
        completed = self._command("--run", self.plan_path, "--headless", "--countdown", "0", "--json")
        self.assertEqual(1, completed.returncode)
        self.assertEqual("failed", json.loads(completed.stdout)["status"])
        self.assertEqual("retain", (self.destination / "extra.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.destination / "file.txt").exists())

    def test_invalid_countdown_returns_argument_error(self):
        """Verify negative delays are rejected before touching storage."""
        completed = self._command("--run", self.action_path, "--headless", "--countdown", "-1")
        self.assertEqual(2, completed.returncode)
        self.assertIn("--countdown", completed.stderr)

    @unittest.skipIf(os.name == "nt", "Windows ACLs do not follow chmod permissions")
    def test_unwritable_data_directory_has_structured_failure(self):
        """Verify local storage failures use the normal JSON error result."""
        blocked_path = self.root / "blocked"
        blocked_path.write_text("a file cannot be a storage directory", encoding="utf-8")
        self.environment["LOCALAPPDATA"] = str(blocked_path)
        self.environment["XDG_DATA_HOME"] = str(blocked_path)
        save_plan(self.plan, self.plan_path)
        completed = self._command("--preview", self.plan_path, "--json")
        self.assertEqual(1, completed.returncode)
        self.assertEqual("failed", json.loads(completed.stdout)["status"])
        self.assertNotIn("Traceback", completed.stdout)


if __name__ == "__main__":
    unittest.main()
