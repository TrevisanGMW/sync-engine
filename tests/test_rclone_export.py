"""Tests startup filter summaries in standalone rclone batch exports."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
from sync_engine_model import new_pair, new_plan
from sync_engine_rclone_export import build_rclone_batch


class TestRcloneExport(unittest.TestCase):
    """Checks displayed filters against the flags used by each transfer."""

    def setUp(self):
        """Create isolated export inputs without a usable rclone executable."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "rclone.exe"
        self.config = self.root / "rclone.conf"
        self.executable.touch()
        self.config.touch()
        self.plan = new_plan("Filter summary")
        self.plan["pairs"][0].update({
            "source": str(self.root / "source"),
            "destination": "rclone://test/Backup/first",
            "mode": "update",
        })

    def _build(self):
        """Return an export using only disposable configuration files."""
        return build_rclone_batch(
            self.plan, self.executable, self.config, ["test"],
        )

    def _startup(self, contents):
        """Return startup lines, stopping before log creation or filesystem work."""
        return contents.split('type nul > "%LOG_FILE%"', 1)[0]

    def _run_startup(self, contents):
        """Run only the summary in cmd, with no rclone commands or log writes."""
        path = self.root / "summary.bat"
        path.write_bytes((self._startup(contents) + "exit /b 0\r\n").encode("utf-8"))
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(path)],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertEqual("", result.stderr)
        return result.stdout

    def test_summary_lists_actual_exclusions_for_each_pair(self):
        """Keep pair-specific patterns and case settings visible before work starts."""
        self.plan["pairs"][0]["exclusions"] = [{
            "pattern": "02_tasks", "match": "starts_with", "applies_to": "folders",
        }]
        second = new_pair()
        second.update({
            "source": str(self.root / "second"),
            "destination": "rclone://test/Backup/second",
            "mode": "update",
            "exclusions": [{
                "pattern": "desktop.ini", "match": "exact", "applies_to": "files",
                "ignore_case": False,
            }],
        })
        self.plan["pairs"].append(second)
        contents = self._build()
        startup = self._startup(contents)
        self.assertIn("echo   Case matching: case-insensitive.", startup)
        self.assertIn("echo   Case matching: case-sensitive.", startup)
        for variable in (
            "PAIR_1_FILTER_1_1", "PAIR_1_FILTER_1_2", "PAIR_1_FILTER_1_3",
            "PAIR_1_FILTER_1_4", "PAIR_2_FILTER_1_1",
        ):
            self.assertIn(f'echo   --exclude "%{variable}%"', startup)
            self.assertIn(f'    --exclude "%{variable}%" ^', contents)
        if os.name == "nt":
            output = self._run_startup(contents)
            for pattern in (
                "02_tasks*/", "02_tasks*/**", "**/02_tasks*/", "**/02_tasks*/**", "desktop.ini",
            ):
                self.assertIn(f'--exclude "{pattern}"', output)
            self.assertLess(output.index("02_tasks*/"), output.index("Pair 2:"))
            self.assertGreater(output.index("desktop.ini"), output.index("Pair 2:"))

    def test_summary_explicitly_reports_no_filters(self):
        """Show an unfiltered pair clearly rather than leaving the user guessing."""
        contents = self._build()
        self.assertIn("echo Filters: none.", self._startup(contents))
        if os.name == "nt":
            self.assertIn("Filters: none.", self._run_startup(contents))

    @unittest.skipUnless(os.name == "nt", "Requires Windows cmd.exe")
    def test_summary_quotes_shell_characters_in_patterns(self):
        """Print valid literal patterns without interpreting them as shell commands."""
        pattern = "cache & echo UNEXPECTED | (draft) <old> !"
        self.plan["pairs"][0]["exclusions"] = [{
            "pattern": pattern, "match": "exact", "applies_to": "files",
        }]
        output = self._run_startup(self._build())
        self.assertIn(f'--exclude "{pattern}"', output)
        self.assertNotIn("\nUNEXPECTED", output)


if __name__ == "__main__":
    unittest.main()
