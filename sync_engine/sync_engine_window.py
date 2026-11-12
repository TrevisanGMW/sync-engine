"""Application title rows with native Windows snapping and resizing."""

import ctypes
import logging
import sys

from sync_engine_qt import QtCore, QtGui, QtWidgets, qt_enum
from sync_engine_status import StatusIndicator


logger = logging.getLogger(__name__)


def prepare_native_window(window):
    """Retains native resize and keyboard behavior beneath a custom title row.

    Args:
        window (QWidget): A realized top-level window.
    """
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        handle = ctypes.c_void_p(int(window.winId()))
        user32.GetWindowLongW.restype = ctypes.c_long
        user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint,
        ]
        style = user32.GetWindowLongW(handle, -16)
        # Thick frame, system menu, minimize and maximize boxes enable OS window actions.
        wanted = style | 0x00040000 | 0x00080000 | 0x00020000 | 0x00010000
        if wanted != style:
            user32.SetWindowLongW(handle, -16, ctypes.c_long(wanted))
            user32.SetWindowPos(handle, None, 0, 0, 0, 0, 0x0037)
        margins = (ctypes.c_int * 4)(1, 1, 1, 1)
        dwm = ctypes.windll.dwmapi
        dwm.DwmExtendFrameIntoClientArea(handle, ctypes.byref(margins))
        corner = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(handle, 33, ctypes.byref(corner), ctypes.sizeof(corner))
        border = ctypes.c_uint(0x00121212)
        dwm.DwmSetWindowAttribute(handle, 34, ctypes.byref(border), ctypes.sizeof(border))
    except (AttributeError, OSError, ValueError):
        logger.debug("Native window effects are unavailable", exc_info=True)


def native_event(event_type, message):
    """Removes the OS caption while keeping maximization inside the work area.

    Args:
        event_type (bytes): Qt platform event identifier.
        message (object): Native MSG address.

    Returns:
        tuple: Whether the event was handled and its native result.
    """
    if sys.platform != "win32" or bytes(event_type) != b"windows_generic_MSG":
        return False, 0
    from ctypes import wintypes

    class FrameParameters(ctypes.Structure):
        """Win32 NCCALCSIZE_PARAMS layout."""

        _fields_ = [("rectangles", wintypes.RECT * 3), ("position", ctypes.c_void_p)]

    class MonitorInformation(ctypes.Structure):
        """Win32 MONITORINFO layout."""

        _fields_ = [
            ("size", wintypes.DWORD), ("monitor", wintypes.RECT),
            ("work", wintypes.RECT), ("flags", wintypes.DWORD),
        ]

    native_message = wintypes.MSG.from_address(int(message))
    user32 = ctypes.windll.user32
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.IsZoomed.argtypes = [wintypes.HWND]
    if native_message.message == 0x0086:  # WM_NCACTIVATE
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        return True, user32.DefWindowProcW(
            native_message.hWnd, native_message.message, native_message.wParam, -1,
        )
    if native_message.message != 0x0083 or not native_message.wParam:  # WM_NCCALCSIZE
        return False, 0
    if not user32.GetWindowLongW(native_message.hWnd, -16) & 0x00040000:
        return False, 0
    if user32.IsZoomed(native_message.hWnd):
        user32.MonitorFromWindow.restype = wintypes.HANDLE
        user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        monitor = user32.MonitorFromWindow(native_message.hWnd, 2)
        information = MonitorInformation()
        information.size = ctypes.sizeof(information)
        if user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(information)):
            parameters = FrameParameters.from_address(native_message.lParam)
            parameters.rectangles[0] = information.work
    return True, 0


class ElidedTitle(QtWidgets.QLabel):
    """Keeps long plan names from widening a compact window."""

    def __init__(self, parent=None):
        """Creates a title that can shrink freely.

        Args:
            parent (QWidget, optional): Owning title row.
        """
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        self.setAttribute(qt_enum(QtCore.Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"))

    def paintEvent(self, event):
        """Paints the available portion of the title.

        Args:
            event (QPaintEvent): Requested paint region.
        """
        painter = QtGui.QPainter(self)
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        text = self.fontMetrics().elidedText(
            self.text(), qt_enum(QtCore.Qt, "TextElideMode", "ElideRight"), self.width(),
        )
        painter.drawText(self.rect(), qt_enum(QtCore.Qt, "AlignmentFlag", "AlignVCenter"), text)


class TitleBar(QtWidgets.QWidget):
    """Draggable application title, status indicator, and window controls."""

    compact_requested = QtCore.Signal()
    minimize_requested = QtCore.Signal()

    def __init__(self, window, compact=False):
        """Builds a full or compact title row.

        Args:
            window (QWidget): Window controlled by this row.
            compact (bool): Whether to offer expansion instead of maximization.
        """
        super().__init__(window)
        self.target = window
        self.compact = compact
        self.press_point = None
        self.setObjectName("windowTitleBar")
        self.setAttribute(qt_enum(QtCore.Qt, "WidgetAttribute", "WA_StyledBackground"))
        self.setFixedHeight(24 if compact else 32)
        self.setStyleSheet(
            "QWidget#windowTitleBar { background: #292929; color: #eeeeee; }"
            "QToolButton { background: transparent; border: none; padding: 0; border-radius: 3px; }"
            "QToolButton:hover { background: #4a4a4a; }"
            "QToolButton#windowClose:hover { background: #a63d3d; }"
        )
        layout = QtWidgets.QHBoxLayout(self)
        margins = (0, 0, 0, 0) if compact else (7, 2, 4, 2)
        layout.setContentsMargins(*margins)
        layout.setSpacing(6 if compact else 5)
        self.icon_label = QtWidgets.QLabel()
        self.icon_label.setFixedSize(18, 18)
        self.icon_label.setAttribute(qt_enum(QtCore.Qt, "WidgetAttribute", "WA_TransparentForMouseEvents"))
        self.title_label = ElidedTitle()
        self.status_indicator = StatusIndicator()
        if compact:
            self.status_indicator.setFixedSize(18, 18)
        self.compact_button = self._button("compact", "Switch to compact mode")
        self.minimize_button = self._button("minimize", "Minimize")
        self.maximize_button = self._button("expand" if compact else "maximize", "Expand" if compact else "Maximize")
        self.close_button = self._button("close", "Close")
        self.close_button.setObjectName("windowClose")
        layout.addWidget(self.icon_label)
        layout.addWidget(self.title_label, 1)
        layout.addWidget(self.status_indicator)
        if not compact:
            layout.addWidget(self.compact_button)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        self.compact_button.clicked.connect(self.compact_requested)
        self.minimize_button.clicked.connect(self.minimize_requested)
        self.maximize_button.clicked.connect(self._maximize)
        self.close_button.clicked.connect(window.close)
        window.windowTitleChanged.connect(self.set_title)
        window.windowIconChanged.connect(self._set_icon)
        self.set_title(window.windowTitle())
        self._set_icon(window.windowIcon())

    def _button(self, name, tooltip):
        """Creates a title button from a local vector resource.

        Args:
            name (str): Artwork name.
            tooltip (str): Accessible description.

        Returns:
            QToolButton: Configured control.
        """
        from sync_engine_style import resource_path

        button = QtWidgets.QToolButton(self)
        button.setIcon(QtGui.QIcon(resource_path(f"icons/window_{name}.svg")))
        icon_size = 13 if self.compact else 14
        button.setIconSize(QtCore.QSize(icon_size, icon_size))
        button.setFixedSize(24, 24 if self.compact else 26)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        return button

    def set_title(self, text):
        """Updates the visible and hover titles.

        Args:
            text (str): Application-defined title.
        """
        self.title_label.setText(text)
        self.title_label.setToolTip(text)

    def _set_icon(self, icon):
        """Updates the application icon.

        Args:
            icon (QIcon): Window icon.
        """
        self.icon_label.setPixmap(icon.pixmap(18, 18))

    def _maximize(self):
        """Expands the compact view or toggles native maximization."""
        if self.compact:
            self.compact_requested.emit()
        elif self.target.isMaximized():
            self.target.showNormal()
        else:
            self.target.showMaximized()

    def refresh_maximize_button(self):
        """Updates the maximize control after native window-state changes."""
        if self.compact:
            return
        from sync_engine_style import resource_path

        maximized = self.target.isMaximized()
        name = "restore" if maximized else "maximize"
        self.maximize_button.setIcon(QtGui.QIcon(resource_path(f"icons/window_{name}.svg")))
        self.maximize_button.setToolTip("Restore" if maximized else "Maximize")

    def contextMenuEvent(self, event):
        """Offers standard window commands on the custom title row.

        Args:
            event (QContextMenuEvent): Requested menu position.
        """
        menu = QtWidgets.QMenu(self)
        restore = menu.addAction("Restore", self.target.showNormal)
        restore.setEnabled(self.target.isMaximized() or self.target.isMinimized())
        menu.addAction("Minimize", self.minimize_requested.emit)
        if not self.compact:
            maximize = menu.addAction("Maximize", self.target.showMaximized)
            maximize.setEnabled(not self.target.isMaximized())
        menu.addSeparator()
        menu.addAction("Close", self.target.close)
        runner = getattr(menu, "exec", None) or menu.exec_
        runner(event.globalPos())

    def mousePressEvent(self, event):
        """Remembers a possible native title drag.

        Args:
            event (QMouseEvent): Mouse press.
        """
        if event.button() == qt_enum(QtCore.Qt, "MouseButton", "LeftButton"):
            self.press_point = QtGui.QCursor.pos()

    def mouseMoveEvent(self, event):
        """Hands dragging to the OS so snapping and monitor transitions work.

        Args:
            event (QMouseEvent): Mouse movement.
        """
        if self.press_point is None or not event.buttons() & qt_enum(QtCore.Qt, "MouseButton", "LeftButton"):
            return
        point = QtGui.QCursor.pos()
        if (point - self.press_point).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return
        self.press_point = None
        if self.target.isMaximized():
            fraction = (point.x() - self.target.x()) / max(1, self.target.width())
            self.target.showNormal()
            self.target.move(point.x() - int(self.target.width() * fraction), point.y() - self.height() // 2)
        handle = self.target.windowHandle()
        if handle and hasattr(handle, "startSystemMove"):
            handle.startSystemMove()

    def mouseReleaseEvent(self, event):
        """Clears a pending drag.

        Args:
            event (QMouseEvent): Mouse release.
        """
        self.press_point = None

    def mouseDoubleClickEvent(self, event):
        """Mirrors native title-bar double clicking.

        Args:
            event (QMouseEvent): Mouse double click.
        """
        if event.button() == qt_enum(QtCore.Qt, "MouseButton", "LeftButton"):
            self.press_point = None
            self._maximize()


class ResizeGrip(QtWidgets.QWidget):
    """Invisible edge handle that starts an operating-system resize."""

    def __init__(self, window, edges, cursor):
        """Creates a window-edge handle.

        Args:
            window (QWidget): Target window.
            edges (tuple): Qt edge names.
            cursor (str): Qt cursor shape name.
        """
        super().__init__(window)
        self.edges = edges
        self.setCursor(qt_enum(QtCore.Qt, "CursorShape", cursor))

    def mousePressEvent(self, event):
        """Begins native resizing on a left-button press.

        Args:
            event (QMouseEvent): Mouse press.
        """
        if event.button() != qt_enum(QtCore.Qt, "MouseButton", "LeftButton"):
            return
        handle = self.window().windowHandle()
        if handle and hasattr(handle, "startSystemResize"):
            edges = qt_enum(QtCore.Qt, "Edge", self.edges[0])
            for name in self.edges[1:]:
                edges |= qt_enum(QtCore.Qt, "Edge", name)
            handle.startSystemResize(edges)


class WindowFrame:
    """Places a title row above an existing QMainWindow without moving its menus."""

    def __init__(self, window):
        """Installs the frame and native resize handles.

        Args:
            window (QMainWindow): Existing full application window.
        """
        self.window = window
        window.setWindowFlag(qt_enum(QtCore.Qt, "WindowType", "FramelessWindowHint"), True)
        window.setContentsMargins(1, 33, 1, 1)
        self.title_bar = TitleBar(window)
        self.grips = []
        for edges, cursor in (
            (("LeftEdge",), "SizeHorCursor"), (("RightEdge",), "SizeHorCursor"),
            (("TopEdge",), "SizeVerCursor"), (("BottomEdge",), "SizeVerCursor"),
            (("TopEdge", "LeftEdge"), "SizeFDiagCursor"),
            (("TopEdge", "RightEdge"), "SizeBDiagCursor"),
            (("BottomEdge", "LeftEdge"), "SizeBDiagCursor"),
            (("BottomEdge", "RightEdge"), "SizeFDiagCursor"),
        ):
            self.grips.append(ResizeGrip(window, edges, cursor))
        self.refresh()

    def refresh(self):
        """Positions the caption and resize handles after a size or state change."""
        width, height = self.window.width(), self.window.height()
        self.title_bar.setGeometry(1, 1, max(0, width - 2), 32)
        self.title_bar.raise_()
        edge, corner = 5, 9
        rectangles = (
            (0, corner, edge, height - 2 * corner),
            (width - edge, corner, edge, height - 2 * corner),
            (corner, 0, width - 2 * corner, edge),
            (corner, height - edge, width - 2 * corner, edge),
            (0, 0, corner, corner), (width - corner, 0, corner, corner),
            (0, height - corner, corner, corner),
            (width - corner, height - corner, corner, corner),
        )
        for grip, rectangle in zip(self.grips, rectangles):
            grip.setGeometry(*rectangle)
            grip.setVisible(not self.window.isMaximized())
            grip.raise_()
        self.title_bar.refresh_maximize_button()
