"""Small independent widgets and display helpers for SyncEngine."""

import os

from sync_engine_qt import QtCore, QtWidgets, qt_enum


def format_bytes(value):
    """Formats a byte count for compact progress and preview labels.

    Args:
        value (int or float): Byte count.

    Returns:
        str: Readable binary size.
    """
    amount = max(0.0, float(value or 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:,.0f} {unit}" if unit == "B" else f"{amount:,.1f} {unit}"
        amount /= 1024


def format_duration(value):
    """Formats elapsed or estimated time without excessive precision.

    Args:
        value (int or float or None): Seconds or an unknown estimate.

    Returns:
        str: Compact human-readable duration.
    """
    if value is None:
        return "—"
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


class FolderPathEdit(QtWidgets.QLineEdit):
    """Provides an editable path field accepting a single local folder drop."""

    focus_gained = QtCore.Signal(object)

    def __init__(self, placeholder, parent=None):
        """Creates a folder input with a descriptive placeholder.

        Args:
            placeholder (str): Empty-field guidance.
            parent (QWidget, optional): Owning widget.
        """
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setPlaceholderText(placeholder)
        self.setToolTip("Type a folder path, browse for it, or drop a folder here.")

    def focusInEvent(self, event):
        """Remembers which path field should receive a Utilities token."""
        super().focusInEvent(event)
        self.focus_gained.emit(self)

    def _folder_from_event(self, event):
        """Extracts an existing local folder from a drag event.

        Args:
            event (QDropEvent): Incoming drag or drop.

        Returns:
            str or None: Existing directory path.
        """
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            folder = urls[0].toLocalFile()
            if os.path.isdir(folder):
                return os.path.normpath(folder)
        return None

    def dragEnterEvent(self, event):
        """Accepts drags containing one existing folder.

        Args:
            event (QDragEnterEvent): Incoming drag.
        """
        if self._folder_from_event(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """Maintains the drag acceptance state over the input.

        Args:
            event (QDragMoveEvent): Incoming drag movement.
        """
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        """Sets the input to the dropped folder without altering its contents.

        Args:
            event (QDropEvent): Incoming drop.
        """
        folder = self._folder_from_event(event)
        if folder:
            self.setText(folder)
            event.acceptProposedAction()
        else:
            event.ignore()


class PathLabel(QtWidgets.QLabel):
    """Keeps long file paths readable without expanding the window width."""

    def __init__(self, parent=None):
        """Creates a selectable label with middle-elided path display.

        Args:
            parent (QWidget, optional): Owning widget.
        """
        super().__init__(parent)
        self._full_text = ""
        self.setMinimumWidth(0)
        self.setSizePolicy(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Ignored"),
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Preferred"),
        )
        self.setTextInteractionFlags(qt_enum(QtCore.Qt, "TextInteractionFlag", "TextSelectableByMouse"))

    def set_path(self, text):
        """Sets the full path while showing a width-aware abbreviation.

        Args:
            text (str): Full path or explanatory text.
        """
        self._full_text = str(text or "")
        self.setToolTip(self._full_text)
        self._refresh()

    def _refresh(self):
        """Updates the visible abbreviation for the current width."""
        self.setText(self.fontMetrics().elidedText(
            self._full_text, qt_enum(QtCore.Qt, "TextElideMode", "ElideMiddle"), max(80, self.width())
        ))

    def resizeEvent(self, event):
        """Recalculates the abbreviation when the label is resized.

        Args:
            event (QResizeEvent): Resize notification.
        """
        super().resizeEvent(event)
        self._refresh()
