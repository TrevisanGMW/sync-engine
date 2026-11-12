"""Regression coverage for window modes, status, tray behavior, and preferences."""

import logging
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
    from sync_engine_controller import SyncEngineController
    from sync_engine_model import PlanStore, new_plan
    from sync_engine_qt import QtCore, QtGui, QtWidgets, qt_enum
    from sync_engine_status import progress_icon
    from sync_engine_style import DARK_STYLESHEET, resource_path
    from sync_engine_view import SyncEngineView
except ImportError:
    QtWidgets = None


@unittest.skipIf(QtWidgets is None, "An existing PySide6 or PySide2 runtime is required.")
class TestWindow(unittest.TestCase):
    """Exercises presentation changes without touching user data or the real tray."""

    @classmethod
    def setUpClass(cls):
        """Creates the test process's single Qt application."""
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        """Creates an independent application window."""
        self.view = SyncEngineView()
        self.manager = self.view.window_manager

    def tearDown(self):
        """Releases the companion window and all timers."""
        self.view.close()
        self.view.deleteLater()
        self.application.processEvents()

    def test_custom_title_tracks_application_title_and_keeps_menus(self):
        """Keeps native metadata, custom caption, and standard menus in agreement."""
        self.view.setWindowTitle("SyncEngine - Project backup")
        self.assertEqual("SyncEngine - Project backup", self.manager.frame.title_bar.title_label.text())
        self.assertEqual(self.view.windowTitle(), self.manager.compact_view.windowTitle())
        self.assertEqual(["File", "Utilities", "Tools", "Help"],
                         [action.text() for action in self.view.menuBar().actions()])

    def test_compact_roundtrip_preserves_full_geometry_and_plan(self):
        """Returns to the same editor size without rebuilding the plan or preview."""
        self.view.set_plan(new_plan("Compact test"))
        self.view.show()
        self.application.processEvents()
        geometry = self.view.geometry()
        plan = self.view.get_plan()
        self.view.compact_action.setChecked(True)
        self.assertTrue(self.manager.compact_view.isVisible())
        self.assertFalse(self.view.isVisible())
        self.assertEqual(66, self.manager.compact_view.height())
        self.assertEqual(380, self.manager.compact_view.width())
        self.manager.compact_view.title_bar.maximize_button.click()
        self.assertTrue(self.view.isVisible())
        self.assertFalse(self.manager.compact_view.isVisible())
        self.assertEqual(geometry, self.view.geometry())
        self.assertEqual(plan, self.view.get_plan())

    def test_compact_roundtrip_preserves_maximized_state(self):
        """Retains OS maximization while switching presentation."""
        self.view.showMaximized()
        self.application.processEvents()
        self.manager.set_compact(True)
        self.manager.set_compact(False)
        self.assertTrue(self.view.isMaximized())

    def test_progress_and_warning_outcomes_match_both_windows(self):
        """Keeps the spinner active during work and shows warnings on completion."""
        self.view.set_busy(True)
        self.view.update_progress({"phase": "copy", "percent": 42})
        self.assertEqual(420, self.manager.compact_view.progress_bar.value())
        for title in (self.manager.frame.title_bar, self.manager.compact_view.title_bar):
            self.assertEqual("running", title.status_indicator.state)
            self.assertTrue(title.status_indicator.timer.isActive())
        self.view.set_busy(False)
        self.view.update_progress({"phase": "succeeded", "percent": 100, "warnings": True})
        self.assertEqual("warning", self.manager.state)
        self.assertEqual(1000, self.manager.compact_view.progress_bar.value())
        self.view.set_busy(True)
        self.view.update_progress({"phase": "succeeded", "percent": 100})
        self.assertEqual("done", self.manager.state)
        self.assertFalse(self.manager.compact_view.title_bar.status_indicator.timer.isActive())

    def test_failure_and_cancellation_keep_current_progress(self):
        """Does not turn a partial or failed sync into 100 percent success."""
        for phase, state in (("failed", "error"), ("timed_out", "error"), ("cancelled", "cancelled")):
            self.view.update_progress({"phase": phase, "percent": 37})
            self.assertEqual(state, self.manager.state)
            self.assertEqual(370, self.manager.compact_view.progress_bar.value())

    def test_scan_feedback_survives_status_updates(self):
        """Shows a scan phase while totals are unknown."""
        self.view.update_progress({"phase": "scan", "scanned_entries": 20})
        self.view.set_status("Generating preview - 20 items scanned")
        self.assertEqual("Scanning folders", self.manager.compact_view.progress_bar.format())

    def test_countdown_keeps_the_compact_spinner_running(self):
        """Does not leave a completed preview icon during an automatic countdown."""
        self.view.update_progress({"phase": "finished", "percent": 100})
        self.view.set_countdown(10)
        self.view.set_countdown(5)
        self.assertEqual("running", self.manager.state)
        self.assertEqual(50, self.manager.progress)
        self.assertEqual("Starts in 5s", self.manager.compact_view.progress_bar.format())

    def test_compact_start_minimized_to_tray_never_shows_a_window(self):
        """Avoids briefly showing the editor or compact window on an automatic launch."""
        self.manager.start_compact = True
        with mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                mock.patch.object(QtWidgets.QSystemTrayIcon, "show"):
            self.manager.set_tray_enabled(True)
        with mock.patch.object(self.manager.compact_view, "show") as show_compact:
            self.manager.show_initial(minimized=True)
        show_compact.assert_not_called()
        self.assertTrue(self.manager.compact_active)
        self.assertFalse(self.view.isVisible())
        self.assertFalse(self.manager.compact_view.isVisible())

    def test_compact_launch_without_tray_uses_taskbar_minimization(self):
        """Keeps a minimized launch reachable when tray mode is disabled."""
        self.manager.start_compact = True
        self.manager.show_initial(minimized=True)
        self.assertTrue(self.manager.compact_view.isMinimized())
        self.assertFalse(self.view.isVisible())

    def test_tray_is_disabled_by_default_and_unavailable_tray_falls_back(self):
        """Retains taskbar minimization unless tray mode is explicitly available."""
        self.assertFalse(self.manager.tray_enabled)
        self.assertIsNone(self.manager.tray_icon)
        with mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.manager.set_tray_enabled(True)
        self.manager.minimize()
        self.assertTrue(self.view.isMinimized())
        self.assertFalse(self.manager.tray_enabled)

    def test_tray_minimize_restore_and_disable_keep_window_reachable(self):
        """Hides the selected window and restores it when tray mode is disabled."""
        with mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                mock.patch.object(QtWidgets.QSystemTrayIcon, "show"):
            self.manager.set_tray_enabled(True)
        self.view.show()
        self.manager.set_compact(True)
        self.manager.minimize()
        self.assertFalse(self.manager.compact_view.isVisible())
        self.assertFalse(self.view.isVisible())
        self.manager.restore()
        self.assertTrue(self.manager.compact_view.isVisible())
        self.manager.minimize()
        self.manager.set_tray_enabled(False)
        self.assertTrue(self.manager.compact_view.isVisible())
        self.assertFalse(self.view.isVisible())

    def test_native_minimize_is_intercepted_in_both_presentations(self):
        """Also honors minimization requested by Windows rather than a Qt button."""
        with mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=True), \
                mock.patch.object(QtWidgets.QSystemTrayIcon, "show"):
            self.manager.set_tray_enabled(True)
        for compact in (False, True):
            self.manager.set_compact(compact)
            self.manager.restore()
            self.manager.active_window().showMinimized()
            self.application.processEvents()
            self.assertFalse(self.manager.active_window().isVisible())
            self.manager.restore()
            self.assertFalse(self.manager.active_window().isMinimized())

    def test_compact_close_obeys_controller_veto(self):
        """Retains the compact view and tray while a worker is shutting down."""
        self.view.show()
        self.manager.set_compact(True)

        def defer_close(event):
            """Simulates the controller waiting for cancellation.

            Args:
                event (QCloseEvent): Deferred close request.
            """
            event.ignore()

        self.view.closing.connect(defer_close)
        self.manager.compact_view.close()
        self.assertTrue(self.manager.compact_view.isVisible())
        self.assertFalse(self.manager.shutting_down)
        self.view.closing.disconnect(defer_close)
        self.manager.compact_view.close()
        self.assertTrue(self.manager.shutting_down)
        self.assertFalse(self.manager.compact_view.isVisible())

    def test_percentage_icons_render_at_native_tray_sizes(self):
        """Provides explicit density variants with visible digits and transparent corners."""
        for state in ("running", "warning", "done", "error"):
            for percent in (0, 9, 42, 100):
                icon = progress_icon(percent, state)
                self.assertIn(QtCore.QSize(16, 16), icon.availableSizes())
                self.assertIn(QtCore.QSize(32, 32), icon.availableSizes())
                rendered = icon.pixmap(32, 32).toImage()
                self.assertEqual(0, rendered.pixelColor(0, 0).alpha())
                self.assertGreater(rendered.pixelColor(16, 16).alpha(), 0)

    def test_spinbox_arrows_are_local_renderable_vectors(self):
        """Uses actual Qt image resources rather than unsupported CSS triangles."""
        self.assertNotIn("@SPIN_", DARK_STYLESHEET)
        for name in ("spin_up.svg", "spin_down.svg"):
            self.assertIn(name, DARK_STYLESHEET)
            self.assertFalse(QtGui.QIcon(resource_path(f"icons/{name}")).pixmap(10, 6).isNull())


@unittest.skipIf(QtWidgets is None, "An existing PySide6 or PySide2 runtime is required.")
class TestWindowController(unittest.TestCase):
    """Tests preference persistence and real sync results with disposable files."""

    @classmethod
    def setUpClass(cls):
        """Creates or reuses the process's Qt application."""
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        """Creates isolated preferences and folder-pair data."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        source.mkdir()
        (source / "sample.txt").write_text("window regression", encoding="utf-8")
        self.plan = new_plan("Window integration")
        self.plan["pairs"][0].update({"source": str(source), "destination": str(self.root / "target")})
        self.store = PlanStore(self.root / "data")
        self.view = SyncEngineView()
        self.controller = SyncEngineController(self.view, self.store, initial_plan=self.plan)

    def tearDown(self):
        """Stops work before releasing disposable data and log handles."""
        self.controller.cancel()
        self.controller._run_after_preview = False
        if self.controller.worker is not None:
            deadline = time.monotonic() + 10
            while self.controller.worker is not None and time.monotonic() < deadline:
                self.application.processEvents()
                time.sleep(0.005)
        self.view.close()
        self.view.deleteLater()
        self.application.processEvents()
        for handler in list(logging.getLogger("sync_engine").handlers):
            handler.close()
            logging.getLogger("sync_engine").removeHandler(handler)
        self.temporary.cleanup()

    def test_window_preferences_persist_and_default_to_off(self):
        """Saves both window options and applies tray mode without changing plan data."""
        original_plan = self.view.get_plan()

        def accept_preferences(dialog):
            """Inspects defaults and selects both new options.

            Args:
                dialog (QDialog): Preferences dialog.

            Returns:
                int: Accepted dialog result.
            """
            tray = dialog.findChild(QtWidgets.QCheckBox, "minimizeToTrayCheckBox")
            compact = dialog.findChild(QtWidgets.QCheckBox, "launchCompactCheckBox")
            self.assertFalse(tray.isChecked())
            self.assertFalse(compact.isChecked())
            tray.setChecked(True)
            compact.setChecked(True)
            return qt_enum(QtWidgets.QDialog, "DialogCode", "Accepted")

        method = "exec" if hasattr(QtWidgets.QDialog, "exec") else "exec_"
        with mock.patch.object(QtWidgets.QDialog, method, accept_preferences), \
                mock.patch.object(QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", return_value=False):
            self.controller._show_preferences()
        preferences = self.store.read_preferences()
        self.assertTrue(preferences["minimize_to_tray"])
        self.assertTrue(preferences["launch_in_compact_mode"])
        self.assertTrue(self.view.window_manager.start_compact)
        self.assertEqual(original_plan, self.view.get_plan())

    def test_real_sync_completes_while_compact_and_reports_warning_outcome(self):
        """Keeps real filesystem work functional and maps warning results accurately."""
        self.view.show()
        self.view.window_manager.set_compact(True)
        self.controller._request_sync()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.application.processEvents()
            if self.controller._pending_result is not None and self.controller.worker is None:
                break
            time.sleep(0.005)
        self.assertIsNone(self.controller.worker)
        self.assertEqual("window regression", (self.root / "target" / "sample.txt").read_text(encoding="utf-8"))
        self.assertEqual("done", self.view.window_manager.state)
        self.assertEqual(100, self.view.window_manager.progress)
        self.controller._show_result({"status": "succeeded", "warnings": ["Skipped optional item"]})
        self.assertEqual("warning", self.view.window_manager.state)


if __name__ == "__main__":
    unittest.main()
