"""Offscreen tests for non-blocking Automatic runs task discovery."""

import os
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))

try:
    from sync_engine_model import new_plan
    from sync_engine_qt import QtCore, QtWidgets
    from sync_engine_scheduler import AutomaticRunsDialog
except ImportError:
    QtCore = None
    QtWidgets = None


class _FakeStore:
    """Provides only the plan list needed to build the scheduler dialog."""

    def list_plans(self):
        """Returns one generic plan summary."""
        return [{"id": "test-plan", "name": "Test plan"}]


@unittest.skipIf(QtWidgets is None, "An existing PySide6 or PySide2 runtime is required.")
class TestAutomaticRunsDialog(unittest.TestCase):
    """Checks that scheduler enumeration runs outside the UI thread."""

    @classmethod
    def setUpClass(cls):
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_refresh_keeps_the_dialog_responsive_while_loading(self):
        started = threading.Event()
        release = threading.Event()
        task_list = [{
            "name": "Nightly", "schedule": "daily", "next_run": "Tomorrow",
            "state": "Ready", "enabled": True, "last_result": "0",
        }]

        def delayed_list_tasks():
            started.set()
            release.wait(2)
            return task_list

        with mock.patch("sync_engine_scheduler.list_tasks", side_effect=delayed_list_tasks):
            dialog = AutomaticRunsDialog(_FakeStore(), new_plan("Test plan"))
            worker = dialog._task_worker
            self.assertTrue(started.wait(1))
            self.assertFalse(dialog.loading_bar.isHidden())
            self.assertFalse(dialog.table.isEnabled())
            release.set()
            loop = QtCore.QEventLoop()
            dialog._task_worker.finished.connect(loop.quit)
            QtCore.QTimer.singleShot(3000, loop.quit)
            loop_runner = getattr(loop, "exec", None) or loop.exec_
            loop_runner()
            worker.wait()
            self.application.processEvents()
            self.assertEqual("Nightly", dialog.table.item(0, 0).text())
            self.assertTrue(dialog.loading_bar.isHidden())
            self.assertTrue(dialog.table.isEnabled())
            dialog.close()


if __name__ == "__main__":
    unittest.main()
