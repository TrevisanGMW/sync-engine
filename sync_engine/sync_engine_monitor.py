"""Compact progress presentation and optional minimize-to-tray lifecycle."""

from sync_engine_qt import QtCore, QtGui, QtWidgets, qt_enum
from sync_engine_status import STATUS_COLORS, STATUS_LABELS, progress_icon
from sync_engine_style import DARK_STYLESHEET, icon_path
from sync_engine_window import ResizeGrip, TitleBar, WindowFrame, native_event, prepare_native_window


class CompactWindow(QtWidgets.QWidget):
    """Small title row and progress bar sharing the full application's state."""

    def __init__(self, manager):
        """Builds the compact view without filesystem or preference access.

        Args:
            manager (WindowManager): Owner of shared presentation state.
        """
        super().__init__()
        self.manager = manager
        self.setWindowTitle("SyncEngine")
        self.setWindowIcon(QtGui.QIcon(icon_path()))
        self.setWindowFlag(qt_enum(QtCore.Qt, "WindowType", "FramelessWindowHint"), True)
        self.setObjectName("compactWindow")
        self.setAttribute(qt_enum(QtCore.Qt, "WidgetAttribute", "WA_StyledBackground"))
        self.setStyleSheet(
            DARK_STYLESHEET
            + "QWidget#compactWindow { background: #292929; border: 1px solid #4a4a4a; border-radius: 7px; }"
        )
        self.setMinimumWidth(260)
        self.setFixedHeight(66)
        self.resize(380, 66)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(5)
        self.title_bar = TitleBar(self, compact=True)
        self.title_bar.compact_button.hide()
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(22)
        layout.addWidget(self.title_bar)
        layout.addWidget(self.progress_bar)
        self.left_grip = ResizeGrip(self, ("LeftEdge",), "SizeHorCursor")
        self.right_grip = ResizeGrip(self, ("RightEdge",), "SizeHorCursor")
        self.title_bar.compact_requested.connect(lambda: manager.set_compact(False))
        self.title_bar.minimize_requested.connect(manager.minimize)

    def resizeEvent(self, event):
        """Keeps horizontal handles on the compact window edges.

        Args:
            event (QResizeEvent): New window dimensions.
        """
        super().resizeEvent(event)
        if hasattr(self, "left_grip"):
            self.left_grip.setGeometry(0, 0, 5, self.height())
            self.right_grip.setGeometry(self.width() - 5, 0, 5, self.height())
            self.left_grip.raise_()
            self.right_grip.raise_()

    def showEvent(self, event):
        """Enables native Windows actions after the handle exists.

        Args:
            event (QShowEvent): Window becoming visible.
        """
        super().showEvent(event)
        prepare_native_window(self)

    def nativeEvent(self, event_type, message):
        """Shares native frame handling with the main window.

        Args:
            event_type (bytes): Qt platform event identifier.
            message (object): Native message address.

        Returns:
            tuple: Native event disposition.
        """
        handled, result = native_event(event_type, message)
        return (handled, result) if handled else super().nativeEvent(event_type, message)

    def closeEvent(self, event):
        """Routes close requests through the full application's save/cancel path.

        Args:
            event (QCloseEvent): Close request.
        """
        if self.manager.shutting_down:
            event.accept()
        else:
            event.ignore()
            self.manager.view.close()
            if self.manager.shutting_down:
                event.accept()


class WindowManager(QtCore.QObject):
    """Owns full/compact visibility, common status, and the optional tray icon."""

    def __init__(self, view):
        """Connects presentation controls without changing application logic.

        Args:
            view (SyncEngineView): Full application window.
        """
        super().__init__(view)
        self.view = view
        self.compact_active = False
        self.start_compact = False
        self.shutting_down = False
        self.tray_requested = False
        self.tray_enabled = False
        self.tray_icon = None
        self.tray_menu = None
        self._badge_key = None
        self._full_geometry = None
        self._full_rectangle = None
        self._full_state = None
        self._compact_position = None
        self._busy = False
        self._warnings = False
        self.phase = "ready"
        self.progress = 0
        self.state = "ready"
        self.message = "Ready"
        self.frame = WindowFrame(view)
        self.compact_view = CompactWindow(self)
        self.frame.title_bar.compact_requested.connect(lambda: self.set_compact(True))
        self.frame.title_bar.minimize_requested.connect(self.minimize)
        self.view.windowTitleChanged.connect(self.compact_view.setWindowTitle)
        self.view.plan_name.textChanged.connect(self._update_plan_title)
        self.view.installEventFilter(self)
        self.compact_view.installEventFilter(self)
        QtWidgets.QApplication.instance().aboutToQuit.connect(self.shutdown)
        self._refresh_status()

    def show_initial(self, minimized=False):
        """Shows the selected launch mode and respects the tray preference.

        Args:
            minimized (bool): Whether an automatic action starts minimized.
        """
        if self.start_compact:
            self.set_compact(True, show=False)
        if minimized:
            self.minimize()
        else:
            self.restore()

    def active_window(self):
        """Returns the window representing the current presentation mode.

        Returns:
            QWidget: Full or compact window.
        """
        return self.compact_view if self.compact_active else self.view

    def set_compact(self, enabled, show=True):
        """Switches presentations while preserving full geometry and window state.

        Args:
            enabled (bool): Whether to show the compact window.
            show (bool): Whether to reveal the selected window immediately.
        """
        enabled = bool(enabled)
        if self.shutting_down or enabled == self.compact_active:
            return
        previous = self.active_window()
        visible = previous.isVisible()
        if enabled:
            self._full_geometry = self.view.saveGeometry()
            self._full_rectangle = self.view.geometry()
            self._full_state = self.view.windowState() & ~qt_enum(QtCore.Qt, "WindowState", "WindowMinimized")
            position = self._compact_position if self._compact_position is not None else self.view.pos()
            self.compact_view.move(position)
        else:
            self._compact_position = self.compact_view.pos()
            if self._full_geometry is not None:
                self.view.restoreGeometry(self._full_geometry)
            if self._full_state is not None:
                self.view.setWindowState(self._full_state)
                if not self._full_state & qt_enum(QtCore.Qt, "WindowState", "WindowMaximized"):
                    self.view.setGeometry(self._full_rectangle)
        previous.hide()
        self.compact_active = enabled
        self.view.compact_action.setChecked(enabled)
        if show:
            self.restore()
            if not visible and self.tray_enabled:
                self.active_window().hide()
        self._refresh_actions()

    def _update_plan_title(self, name):
        """Shows the current plan name in the compact caption.

        Args:
            name (str): Editable plan display name.
        """
        title = name.strip() or "SyncEngine"
        self.compact_view.setWindowTitle(f"SyncEngine - {title}" if name.strip() else title)
        self.compact_view.title_bar.set_title(title)

    def restore(self):
        """Restores and focuses the active presentation from taskbar or tray."""
        window = self.active_window()
        if window.isMinimized():
            window.setWindowState(window.windowState() & ~qt_enum(QtCore.Qt, "WindowState", "WindowMinimized"))
        window.show()
        window.raise_()
        window.activateWindow()
        self._fit_on_screen(window)
        self._refresh_actions()

    @staticmethod
    def _fit_on_screen(window):
        """Keeps a restored compact window reachable after monitor changes.

        Args:
            window (QWidget): Restored window.
        """
        screens = QtWidgets.QApplication.screens()
        if any(screen.availableGeometry().intersects(window.frameGeometry()) for screen in screens):
            return
        if screens:
            window.move(screens[0].availableGeometry().topLeft() + QtCore.QPoint(30, 30))

    def minimize(self):
        """Minimizes to the tray when available, otherwise to the taskbar."""
        if self.tray_enabled:
            self.active_window().hide()
        else:
            self.active_window().showMinimized()
        self._refresh_actions()

    def set_tray_enabled(self, enabled):
        """Applies a saved tray preference with a safe taskbar fallback.

        Args:
            enabled (bool): Requested minimize-to-tray preference.
        """
        self.tray_requested = bool(enabled)
        self.tray_enabled = self.tray_requested and QtWidgets.QSystemTrayIcon.isSystemTrayAvailable()
        if self.tray_enabled:
            if self.tray_icon is None:
                self._create_tray()
            self._refresh_status()
            self.tray_icon.show()
        elif self.tray_icon is not None:
            if not self.active_window().isVisible():
                self.restore()
            self.tray_icon.hide()

    def _create_tray(self):
        """Creates and retains tray objects and their application actions."""
        self.tray_icon = QtWidgets.QSystemTrayIcon(self)
        self.tray_menu = QtWidgets.QMenu(self.view)
        self.visibility_action = self.tray_menu.addAction("Show SyncEngine")
        self.visibility_action.triggered.connect(self._toggle_visibility)
        self.mode_action = self.tray_menu.addAction("Switch to Compact Mode")
        self.mode_action.triggered.connect(self._toggle_mode)
        self.tray_menu.addSeparator()
        self.cancel_action = self.tray_menu.addAction("Cancel Current Run")
        self.cancel_action.triggered.connect(self.view.cancel_requested.emit)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction("Close SyncEngine", self.view.close)
        self.tray_icon.setContextMenu(self.tray_menu)
        self.tray_icon.activated.connect(self._tray_activated)
        self._refresh_actions()

    def _toggle_visibility(self):
        """Toggles the active view from the tray menu."""
        if self.active_window().isVisible() and not self.active_window().isMinimized():
            self.minimize()
        else:
            self.restore()

    def _toggle_mode(self):
        """Switches mode and reveals the selected presentation."""
        self.set_compact(not self.compact_active)
        self.restore()

    def _tray_activated(self, reason):
        """Restores the tool when its tray icon is double-clicked.

        Args:
            reason (QSystemTrayIcon.ActivationReason): Tray interaction.
        """
        if reason == qt_enum(QtWidgets.QSystemTrayIcon, "ActivationReason", "DoubleClick"):
            self.restore()

    def _refresh_actions(self):
        """Updates tray action labels without rebuilding the menu."""
        if self.tray_menu is None:
            return
        visible = self.active_window().isVisible() and not self.active_window().isMinimized()
        self.visibility_action.setText("Hide SyncEngine" if visible else "Show SyncEngine")
        self.mode_action.setText("Expand SyncEngine" if self.compact_active else "Switch to Compact Mode")
        self.cancel_action.setEnabled(self._busy)

    def set_busy(self, busy):
        """Resets run status on a new operation and updates cancellation access.

        Args:
            busy (bool): Whether a countdown, preview, or sync is active.
        """
        if busy and not self._busy:
            self.progress = 0
            self._warnings = False
            self.set_status("running")
        self._busy = bool(busy)
        self._refresh_actions()

    def note_message(self, text, level):
        """Keeps status tooltips current and records warning/error feedback.

        Args:
            text (str): User-visible status message.
            level (str): Feedback severity.
        """
        self.message = str(text)
        if level == "warning":
            if self.state != "running":
                self.state = "warning"
        elif level == "error":
            self.state = "error"
        self._refresh_status()

    def set_status(self, state, progress=None):
        """Applies an explicit run outcome to all presentations.

        Args:
            state (str): Presentation status key.
            progress (float, optional): Completion percentage.
        """
        self.state = "warning" if state == "done" and self._warnings else state
        if progress is not None:
            self.progress = max(0, min(100, int(progress)))
        self._refresh_status()

    def update_progress(self, progress, phase, warnings=False):
        """Synchronizes the compact bar and tray tile with the full progress bar.

        Args:
            progress (float): Current completion percentage.
            phase (str): Worker stage or terminal outcome.
            warnings (bool): Whether the completed run contains warnings.
        """
        self._warnings = self._warnings or bool(warnings)
        self.phase = phase
        state = {
            "ready": "ready", "finished": "done", "succeeded": "done",
            "failed": "error", "timed_out": "error", "cancelled": "cancelled",
        }.get(phase, "running")
        self.set_status(state, progress)

    def _refresh_status(self):
        """Repaints shared status and changes tray artwork only when necessary."""
        for title in (self.frame.title_bar, self.compact_view.title_bar):
            title.status_indicator.set_state(self.state)
            title.status_indicator.setToolTip(self.message)
        color = STATUS_COLORS.get(self.state, STATUS_COLORS["ready"])
        bar = self.compact_view.progress_bar
        bar.setValue(self.progress * 10)
        bar.setFormat("%p%")
        if self.state == "running" and self.phase in ("scan", "compare"):
            bar.setFormat("Scanning folders" if self.phase == "scan" else "Comparing files")
        bar.setToolTip(self.message)
        bar.setStyleSheet(
            "QProgressBar { background: #454545; border: 1px solid #555555; border-radius: 3px;"
            " min-height: 0px; text-align: center; color: #eeeeee; font-weight: 600; }"
            f"QProgressBar::chunk {{ background: {color}; border-radius: 2px; }}"
        )
        if self.tray_icon is not None:
            key = (self.progress, self.state)
            if key != self._badge_key:
                self.tray_icon.setIcon(progress_icon(*key))
                self._badge_key = key
            label = STATUS_LABELS.get(self.state, "Ready")
            self.tray_icon.setToolTip(f"{self.view.windowTitle()}\n{self.progress}% - {label}\n{self.message}"[:127])

    def eventFilter(self, watched, event):
        """Handles native minimize actions and updates title controls.

        Args:
            watched (QObject): Full or compact window.
            event (QEvent): Incoming window event.

        Returns:
            bool: False so ordinary application event handling continues.
        """
        if event.type() == qt_enum(QtCore.QEvent, "Type", "WindowStateChange"):
            if watched is self.view:
                self.frame.refresh()
            if self.tray_enabled and watched is self.active_window() and watched.isMinimized():
                QtCore.QTimer.singleShot(0, self._hide_minimized)
        return False

    def _hide_minimized(self):
        """Hides a natively minimized window after Qt finishes changing its state."""
        if not self.shutting_down and self.tray_enabled and self.active_window().isMinimized():
            self.active_window().hide()
            self._refresh_actions()

    def shutdown(self):
        """Closes the companion window and removes the tray icon after approval."""
        if self.shutting_down:
            return
        self.shutting_down = True
        self.frame.title_bar.status_indicator.timer.stop()
        self.compact_view.title_bar.status_indicator.timer.stop()
        self.compact_view.close()
        self.compact_view.deleteLater()
        if self.tray_icon is not None:
            self.tray_icon.hide()

    def saved_geometry(self):
        """Returns full-window geometry even when the compact view is active.

        Returns:
            QByteArray: Full application geometry for existing preferences.
        """
        if self.compact_active and self._full_geometry is not None:
            return self._full_geometry
        return self.view.saveGeometry()
