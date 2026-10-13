"""Integration checks for real background sync and countdown behavior."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
try:
    from sync_engine_controller import SyncEngineController, _suggest_export_filename
    from sync_engine_model import PlanStore, new_plan
    from sync_engine_qt import QtCore, QtWidgets, qt_enum
    from sync_engine_view import SyncEngineView
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "An existing PySide6 or PySide2 runtime is required.")
class TestController(unittest.TestCase):
    """Exercise user actions using disposable filesystem trees and Qt workers."""

    @classmethod
    def setUpClass(cls):
        """Create the test process's one Qt application."""
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_export_filename_uses_sanitized_plan_name(self):
        """Suggests a safe lowercase filename based on the plan display name."""
        self.assertEqual("test_plan.syncrun", _suggest_export_filename("Test Plan", ".syncrun"))
        self.assertEqual("sync_plan.syncplan", _suggest_export_filename("!!!", ".syncplan"))

    def test_export_opens_resizable_non_native_dialog_with_plan_name(self):
        """Avoids the system's full-screen native dialog and suggests the plan slug."""
        controller = SyncEngineController(SyncEngineView(), PlanStore(self.root / "dialog-data"))
        inspected = []

        def accept_dialog():
            dialog = self.application.activeModalWidget()
            self.assertIsInstance(dialog, QtWidgets.QFileDialog)
            inspected.append(dialog)
            self.assertTrue(dialog.testOption(
                qt_enum(QtWidgets.QFileDialog, "Option", "DontUseNativeDialog")
            ))
            self.assertGreaterEqual(dialog.minimumWidth(), 640)
            dialog.selectFile(str(self.root / "test_plan.syncrun"))
            dialog.accept()

        QtCore.QTimer.singleShot(0, accept_dialog)
        selected_path = controller._choose_export("Export automatic action", ".syncrun", "Test Plan")
        expected_path = os.path.normcase(os.path.normpath(str(self.root / "test_plan.syncrun")))
        self.assertEqual(expected_path, os.path.normcase(os.path.normpath(selected_path)))
        self.assertEqual(1, len(inspected))
        controller.view.close()

    def setUp(self):
        """Construct one independent plan and controller for each case."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.plan = new_plan("Integration plan")
        self.plan["pairs"][0].update({"source": str(self.source), "destination": str(self.destination)})
        self.store = PlanStore(self.root / "data")
        self.view = SyncEngineView()
        self.controller = SyncEngineController(self.view, self.store, initial_plan=self.plan)

    def tearDown(self):
        """Stop active work and close the window before cleaning up files."""
        self.controller.cancel()
        self._wait_idle()
        self.view.close()
        self.application.processEvents()
        import logging

        for handler in list(logging.getLogger("sync_engine").handlers):
            handler.close()
            logging.getLogger("sync_engine").removeHandler(handler)
        self.temporary.cleanup()

    def _wait_idle(self):
        """Process Qt events until the worker stops or the test deadline expires."""
        deadline = time.monotonic() + 15
        while self.controller.worker is not None and time.monotonic() < deadline:
            self.application.processEvents()
            time.sleep(0.005)
        self.application.processEvents()
        self.assertIsNone(self.controller.worker, "Background worker did not finish.")

    def test_sync_runs_real_engine_and_retains_metadata(self):
        """Verify a user sync creates the destination and copies real bytes."""
        source_file = self.source / "résumé.txt"
        source_file.write_text("backup content", encoding="utf-8")
        self.controller._request_sync()
        self._wait_idle()
        target_file = self.destination / source_file.name
        self.assertEqual("backup content", target_file.read_text(encoding="utf-8"))
        self.assertEqual(source_file.stat().st_mtime_ns, target_file.stat().st_mtime_ns)
        self.assertEqual("idle", self.controller.state)
        self.assertTrue(Path(self.controller.log_path).is_file())

    def test_preview_leaves_destination_absent(self):
        """Verify the Preview action never creates a target directory."""
        (self.source / "file.txt").write_text("preview only", encoding="utf-8")
        self.controller._request_preview()
        self._wait_idle()
        self.assertFalse(self.destination.exists())
        self.assertEqual(1, self.controller.preview["summary"]["copy"])

    def test_plan_keeps_preview_choice_and_preferences_keep_path_display(self):
        """Stores preview mode on its plan and keeps path display in preferences."""
        def accept_preferences(dialog):
            dialog.findChild(QtWidgets.QCheckBox).setChecked(True)
            dialog.findChild(QtWidgets.QSpinBox).setValue(4)
            return QtWidgets.QDialog.Accepted

        self.view.preview_mode_combo.setCurrentIndex(1)
        self.assertTrue(self.controller.dirty)
        self.assertTrue(self.controller._save_plan(quiet=True))
        self.assertEqual("tree", self.store.load(self.plan["id"])["preview_view"])

        exec_name = "exec" if hasattr(QtWidgets.QDialog, "exec") else "exec_"
        with mock.patch.object(QtWidgets.QDialog, exec_name, accept_preferences):
            with mock.patch.object(
                self.store, "change_projects_directory", return_value=self.root
            ):
                self.controller._show_preferences()

        preferences = self.store.read_preferences()
        self.assertNotIn("preview_default_mode", preferences)
        self.assertTrue(preferences["preview_show_full_target_path"])
        self.assertEqual(4, preferences["preview_target_path_depth"])
        self.assertEqual("tree", self.view.preview_mode_combo.currentData())
        self.assertTrue(self.view.preview_show_full_target_path)

    def test_countdown_can_cancel_without_touching_files(self):
        """Starts a preview immediately, then lets cancellation prevent copying."""
        self.controller._automatic = True
        self.controller.start_countdown(30)
        self.assertTrue(self.controller._countdown_active)
        self.assertIsNotNone(self.controller.worker)
        self.controller.cancel()
        self._wait_idle()
        self.assertEqual("idle", self.controller.state)
        self.assertFalse(self.controller.countdown_timer.isActive())
        self.assertFalse(self.destination.exists())

    def test_automatic_start_modes_dispatch_immediate_countdown_and_confirmation(self):
        """Applies each saved mode when an automatic action is opened."""
        for start_mode in ("immediate", "countdown", "confirm"):
            with self.subTest(start_mode=start_mode):
                plan = new_plan(f"{start_mode} action")
                plan["automation"]["start_mode"] = start_mode
                plan["automation"]["countdown_seconds"] = 30
                view = SyncEngineView()
                store = PlanStore(self.root / f"{start_mode}_data")
                controller = SyncEngineController(
                    view, store, initial_plan=plan, auto_run=True,
                )
                scans = []
                prompts = []
                def start_test_scan(run_after, **kwargs):
                    """Records scan intent and simulates the active confirmation scan."""
                    scans.append(run_after)
                    if start_mode == "confirm":
                        controller.worker = object()

                controller._start_scan = start_test_scan
                decisions = []

                def capture_prompt(message, callback, title="Confirm"):
                    """Records that the modeless prompt opens while the scan is active."""
                    prompts.append(message)
                    decisions.append(callback)

                view.confirm_automatic_run = capture_prompt
                self.application.processEvents()
                if start_mode == "countdown":
                    self.assertEqual("countdown", controller.state)
                    self.assertEqual([False], scans)
                    controller.cancel()
                else:
                    self.assertEqual([start_mode == "immediate"], scans)
                    self.assertEqual(start_mode == "confirm", bool(prompts))
                    if decisions:
                        decisions[0](False)
                        controller.worker = None
                controller._automatic = False
                view.close()
                self.application.processEvents()

    def test_confirmation_scans_before_showing_modeless_prompt(self):
        """Keeps the preview available while an automatic-run decision is pending."""
        (self.source / "visible.txt").write_text("preview this", encoding="utf-8")
        callbacks = []
        observed = []

        def show_prompt(message, callback, title="Confirm"):
            """Captures whether preview work already started when asked."""
            observed.append(self.controller.worker is not None)
            callbacks.append(callback)

        self.view.confirm_automatic_run = show_prompt
        self.controller._confirm_automatic_run()
        self.assertEqual([True], observed)
        callbacks[0](False)
        self._wait_idle()
        self.assertIsNotNone(self.controller.preview)
        self.assertEqual(1, self.controller.preview["summary"]["copy"])
        self.assertFalse(self.destination.exists())

    def test_countdown_expiring_during_scan_runs_after_preview_is_ready(self):
        """Starts syncing as soon as a still-running preview finishes after timeout."""
        (self.source / "countdown.txt").write_text("run after scan", encoding="utf-8")
        from sync_engine_engine import build_preview

        def delayed_preview(*args, **kwargs):
            """Keeps the worker active long enough to exercise countdown expiry."""
            time.sleep(0.15)
            return build_preview(*args, **kwargs)

        with mock.patch("sync_engine_worker.build_preview", side_effect=delayed_preview):
            self.controller._automatic = True
            self.controller.start_countdown(30)
            self.controller._deadline = time.monotonic() - 1
            self.controller._tick_countdown()
            self.assertTrue(self.controller._run_after_preview)
            self._wait_idle()

        self.assertEqual("run after scan", (self.destination / "countdown.txt").read_text())

    def test_confirmation_accepted_during_scan_runs_after_preview_is_ready(self):
        """Continues immediately after the confirmation and outstanding scan complete."""
        (self.source / "confirmed.txt").write_text("approved", encoding="utf-8")
        from sync_engine_engine import build_preview
        callbacks = []
        self.view.confirm_automatic_run = lambda message, callback, title="Confirm": callbacks.append(callback)

        def delayed_preview(*args, **kwargs):
            """Keeps the worker active while the decision is delivered."""
            time.sleep(0.15)
            return build_preview(*args, **kwargs)

        with mock.patch("sync_engine_worker.build_preview", side_effect=delayed_preview):
            self.controller._confirm_automatic_run()
            self.assertIsNotNone(self.controller.worker)
            callbacks[0](True)
            self.assertTrue(self.controller._run_after_preview)
            self._wait_idle()

        self.assertEqual("approved", (self.destination / "confirmed.txt").read_text())

    def test_manual_mirror_requires_confirmation_before_writes(self):
        """Verify declining the deletion prompt also prevents pending copies."""
        (self.source / "new.txt").write_text("new", encoding="utf-8")
        self.destination.mkdir()
        (self.destination / "extra.txt").write_text("preserve", encoding="utf-8")
        self.plan["pairs"][0]["mode"] = "mirror"
        self.view.set_plan(self.plan)
        prompts = []

        def decline(message, title="Confirm"):
            """Record and decline a destructive confirmation.

            Args:
                message (str): Prompt text.
                title (str): Prompt title.

            Returns:
                bool: False to decline the action.
            """
            prompts.append(message)
            return False

        self.view.confirm = decline
        self.controller._request_sync()
        self._wait_idle()
        self.assertEqual(1, len(prompts))
        self.assertEqual("preserve", (self.destination / "extra.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.destination / "new.txt").exists())

    def test_authorized_mirror_copies_before_removing_extras(self):
        """Verify an accepted mirror performs the reviewed changes."""
        (self.source / "new.txt").write_text("new", encoding="utf-8")
        self.destination.mkdir()
        (self.destination / "extra.txt").write_text("remove", encoding="utf-8")
        self.plan["pairs"][0]["mode"] = "mirror"
        self.view.set_plan(self.plan)

        def accept(message, title="Confirm"):
            """Accept this test's deletion confirmation.

            Args:
                message (str): Prompt text.
                title (str): Prompt title.

            Returns:
                bool: True to authorize the tested operation.
            """
            return True

        self.view.confirm = accept
        self.controller._request_sync()
        self._wait_idle()
        self.assertEqual("new", (self.destination / "new.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.destination / "extra.txt").exists())

    def test_automatic_mirror_without_authorization_preserves_everything(self):
        """Verify automatic deletion cannot silently bypass authorization."""
        (self.source / "new.txt").write_text("new", encoding="utf-8")
        self.destination.mkdir()
        (self.destination / "extra.txt").write_text("preserve", encoding="utf-8")
        self.plan["pairs"][0]["mode"] = "mirror"
        self.view.set_plan(self.plan)
        self.controller._automatic = True
        self.controller._start_scan(run_after=True)
        self._wait_idle()
        self.assertTrue((self.destination / "extra.txt").exists())
        self.assertFalse((self.destination / "new.txt").exists())

    def test_first_start_lists_the_new_default_plan(self):
        """Verify a first-launch plan appears in the sidebar immediately."""
        empty_store = PlanStore(self.root / "empty_user")
        empty_view = SyncEngineView()
        empty_controller = SyncEngineController(empty_view, empty_store)
        self.assertEqual(1, empty_view.plans_list.count())
        self.assertEqual(empty_controller.plan["id"], empty_store.list_plans()[0]["id"])
        empty_view.close()


if __name__ == "__main__":
    unittest.main()
