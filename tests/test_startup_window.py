"""Verify Windows console suppression preserves native Qt window visibility."""

import importlib.util
import json
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
import sync_engine_startup
from sync_engine_startup import (
    _hidden_process_options, _startup_environment, set_windows_app_user_model_id,
)


def available_qt_binding():
    """Find an existing Qt binding without initializing a Qt application.

    Returns:
        str or None: Available PySide package name, or None when unavailable.
    """
    for binding_name in ("PySide6", "PySide2"):
        if importlib.util.find_spec(binding_name) is not None:
            return binding_name
    return None


QT_BINDING = available_qt_binding()


class TestWindowsAppIdentity(unittest.TestCase):
    """Verify Windows receives the stable taskbar grouping identity."""

    def test_sets_explicit_taskbar_identity_before_qt_window_creation(self):
        """Passes the app ID through the Win32 shell API."""
        shell32 = mock.Mock()
        shell32.SetCurrentProcessExplicitAppUserModelID.return_value = 0
        fake_windows = types.SimpleNamespace(shell32=shell32)
        with mock.patch.object(sync_engine_startup.os, "name", "nt"), mock.patch.object(
            sync_engine_startup.ctypes, "windll", fake_windows, create=True,
        ):
            self.assertTrue(set_windows_app_user_model_id())
        shell32.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(
            "SyncEngine.Desktop"
        )


@unittest.skipUnless(os.name == "nt" and QT_BINDING, "Windows and an existing PySide runtime are required.")
class TestStartupWindow(unittest.TestCase):
    """Check the native window that an isolated startup process actually shows."""

    def test_console_suppression_keeps_gui_window_visible(self):
        """Ensure launch flags hide the console while preserving the app window."""
        probe = r'''
import ctypes
from ctypes import wintypes
import importlib
import json
import sys

qt_widgets = importlib.import_module(f"{sys.argv[1]}.QtWidgets")
qt_core = importlib.import_module(f"{sys.argv[1]}.QtCore")
application = qt_widgets.QApplication([])
window = qt_widgets.QWidget()
window.setWindowTitle("SyncEngine startup visibility test")
window.resize(200, 100)
window.show()


def report_visibility():
    """Report native visibility and close this disposable test window.

    Returns:
        None: The result is printed and the Qt application is stopped.
    """
    user32 = ctypes.windll.user32
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    visible = bool(user32.IsWindowVisible(int(window.winId())))
    print(json.dumps({"visible": visible}), flush=True)
    window.close()
    application.quit()


qt_core.QTimer.singleShot(100, report_visibility)
runner = getattr(application, "exec", None) or application.exec_
raise SystemExit(runner())
'''
        environment = _startup_environment(sys.executable)
        environment["QT_QPA_PLATFORM"] = "windows"
        completed = subprocess.run(
            [sys.executable, "-c", probe, QT_BINDING], env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=15,
            **_hidden_process_options(),
        )
        self.assertEqual(0, completed.returncode, completed.stderr + completed.stdout)
        result = json.loads(completed.stdout.strip())
        self.assertEqual(True, result["visible"])


if __name__ == "__main__":
    unittest.main()
