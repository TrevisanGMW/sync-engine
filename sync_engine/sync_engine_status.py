"""Locally drawn status indicators and readable system-tray progress tiles."""

from sync_engine_qt import QtCore, QtGui, QtWidgets, qt_enum


STATUS_COLORS = {
    "ready": "#8a9aaa", "running": "#5fd08a", "done": "#5fcf86",
    "warning": "#e6b93a", "error": "#e8645e", "cancelled": "#e8645e",
}
STATUS_LABELS = {
    "ready": "Ready", "running": "Running", "done": "Completed",
    "warning": "Warnings", "error": "Failed", "cancelled": "Cancelled",
}


class StatusIndicator(QtWidgets.QWidget):
    """Paints a spinning arc, completion check, warning, or failure symbol."""

    def __init__(self, parent=None):
        """Initializes an idle indicator.

        Args:
            parent (QWidget, optional): Owning title row.
        """
        super().__init__(parent)
        self.state = "ready"
        self.angle = 0
        self.setFixedSize(20, 20)
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(70)
        self.timer.timeout.connect(self._advance)
        self.set_state("ready")

    def set_state(self, state):
        """Changes the icon and runs the animation only during active work.

        Args:
            state (str): Key in ``STATUS_COLORS``.
        """
        self.state = state if state in STATUS_COLORS else "ready"
        self.setVisible(self.state != "ready")
        label = STATUS_LABELS[self.state]
        self.setToolTip(label)
        self.setAccessibleName(label)
        if self.state == "running":
            if not self.timer.isActive():
                self.timer.start()
        else:
            self.timer.stop()
        self.update()

    def _advance(self):
        """Advances the spinner without repainting a hidden window."""
        self.angle = (self.angle + 30) % 360
        if self.isVisible():
            self.update()

    def paintEvent(self, event):
        """Draws resolution-independent status artwork.

        Args:
            event (QPaintEvent): Requested paint region.
        """
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.scale(self.width() / 20.0, self.height() / 20.0)
        pen = QtGui.QPen(QtGui.QColor(STATUS_COLORS[self.state]), 2)
        pen.setCapStyle(qt_enum(QtCore.Qt, "PenCapStyle", "RoundCap"))
        pen.setJoinStyle(qt_enum(QtCore.Qt, "PenJoinStyle", "RoundJoin"))
        painter.setPen(pen)
        rectangle = QtCore.QRectF(3, 3, 14, 14)
        if self.state == "running":
            track = QtGui.QColor(STATUS_COLORS[self.state])
            track.setAlpha(65)
            painter.setPen(QtGui.QPen(track, 2))
            painter.drawEllipse(rectangle)
            painter.setPen(pen)
            painter.drawArc(rectangle, self.angle * 16, 110 * 16)
        elif self.state == "done":
            path = QtGui.QPainterPath(QtCore.QPointF(4, 10))
            path.lineTo(8, 14)
            path.lineTo(16, 6)
            painter.drawPath(path)
        elif self.state == "warning":
            triangle = QtGui.QPolygonF([
                QtCore.QPointF(10, 2), QtCore.QPointF(18, 17), QtCore.QPointF(2, 17),
            ])
            painter.drawPolygon(triangle)
            painter.drawLine(QtCore.QPointF(10, 7), QtCore.QPointF(10, 11))
            painter.drawPoint(QtCore.QPointF(10, 14))
        else:
            painter.drawEllipse(rectangle)
            if self.state in ("error", "cancelled"):
                painter.drawLine(7, 7, 13, 13)
                painter.drawLine(13, 7, 7, 13)
        painter.end()


def progress_icon(progress, state):
    """Builds crisp tray tiles with large digits and a colored progress strip.

    Args:
        progress (int): Completion percentage, clamped to 0 through 100.
        state (str): Status color key.

    Returns:
        QIcon: Icon containing native-size pixmaps for common tray densities.
    """
    progress = max(0, min(100, int(progress)))
    accent = STATUS_COLORS.get(state, "#4a90d9")
    if state in ("running", "ready"):
        accent = "#4a90d9"
    icon = QtGui.QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128):
        pixmap = QtGui.QPixmap(size, size)
        pixmap.fill(qt_enum(QtCore.Qt, "GlobalColor", "transparent"))
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        border = max(1.0, size * 0.045)
        tile = QtCore.QRectF(border / 2, border / 2, size - border, size - border)
        painter.setPen(QtGui.QPen(QtGui.QColor(accent), border))
        painter.setBrush(QtGui.QColor("#1b1e22"))
        painter.drawRoundedRect(tile, size * 0.14, size * 0.14)
        inset = size * 0.08
        track = QtCore.QRectF(inset, size * 0.78, size - 2 * inset, max(2, size * 0.12))
        painter.setPen(qt_enum(QtCore.Qt, "PenStyle", "NoPen"))
        painter.setBrush(QtGui.QColor("#454a50"))
        painter.drawRoundedRect(track, 1, 1)
        if progress:
            painter.setBrush(QtGui.QColor(accent))
            fill = QtCore.QRectF(track)
            fill.setWidth(track.width() * progress / 100.0)
            painter.drawRoundedRect(fill, 1, 1)
        text = str(progress)
        text_area = QtCore.QRectF(inset, 0, size - 2 * inset, track.top())
        font = QtGui.QFont("Segoe UI")
        font.setBold(True)
        pixels = max(6, int(size * 0.8))
        while pixels > 5:
            font.setPixelSize(pixels)
            bounds = QtGui.QFontMetricsF(font).tightBoundingRect(text)
            if bounds.width() <= text_area.width() and bounds.height() <= text_area.height() * 0.88:
                break
            pixels -= 1
        painter.setFont(font)
        painter.setPen(QtGui.QColor("#ffffff"))
        painter.drawText(text_area, qt_enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"), text)
        painter.end()
        icon.addPixmap(pixmap)
    return icon
