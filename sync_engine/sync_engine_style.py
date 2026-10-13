"""Muted presentation resources for the standalone backup application."""

import os
import sys
import textwrap


def icon_path():
    """Resolves the raster application icon in source and frozen builds.

    Returns:
        str: PNG icon path, with SVG as a source fallback.
    """
    resource_roots = []
    extraction_root = getattr(sys, "_MEIPASS", None)
    if extraction_root:
        resource_roots.append(os.path.join(extraction_root, "sync_engine", "resources"))
    resource_roots.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources"))

    resource_names = (
        ("sync_engine.ico", "sync_engine.png", "sync_engine.svg")
        if os.name == "nt" else ("sync_engine.png", "sync_engine.svg")
    )
    for resource_name in resource_names:
        for resource_root in resource_roots:
            candidate = os.path.join(resource_root, resource_name)
            if os.path.isfile(candidate):
                return candidate

    fallback_name = "sync_engine.ico" if os.name == "nt" else "sync_engine.png"
    return os.path.join(resource_roots[0], fallback_name)


def resource_path(resource_name):
    """Finds a bundled resource in source and frozen builds.

    Args:
        resource_name (str): Path relative to the resources directory.

    Returns:
        str: Resource path, preferring the active PyInstaller extraction folder.
    """
    resource_roots = []
    extraction_root = getattr(sys, "_MEIPASS", None)
    if extraction_root:
        resource_roots.append(os.path.join(extraction_root, "sync_engine", "resources"))
    resource_roots.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources"))
    for resource_root in resource_roots:
        candidate = os.path.join(resource_root, resource_name)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(resource_roots[0], resource_name)


def padded_icon(resource_name, canvas_size=20, icon_size=14):
    """Centers menu artwork in a transparent icon canvas.

    Args:
        resource_name (str): Path relative to the resources directory.
        canvas_size (int): Width and height of the transparent icon canvas.
        icon_size (int): Width and height available to the source artwork.

    Returns:
        QIcon: A centered icon with consistent transparent padding.
    """
    from sync_engine_qt import QtCore, QtGui

    source_icon = QtGui.QIcon(resource_path(resource_name))
    if source_icon.isNull():
        return source_icon
    canvas = QtGui.QPixmap(canvas_size, canvas_size)
    canvas.fill(QtGui.QColor(0, 0, 0, 0))
    painter = QtGui.QPainter(canvas)
    vertical_inset = max(0, (canvas_size - icon_size) // 2)
    horizontal_inset = max(0, (canvas_size - icon_size) // 2)
    artwork_size = min(icon_size, canvas_size - horizontal_inset * 2)
    source_icon.paint(
        painter,
        QtCore.QRect(horizontal_inset, vertical_inset, artwork_size, min(icon_size, canvas_size)),
    )
    painter.end()
    return QtGui.QIcon(canvas)


def multiline_tooltip(value):
    """Wraps tooltip paragraphs into short readable lines.

    Args:
        value (str): Tooltip text.

    Returns:
        str: Wrapped tooltip text.
    """
    return "\n".join(textwrap.fill(line, width=68) for line in str(value).splitlines())


DARK_STYLESHEET = """
QMainWindow, QDialog, QWidget {
    background: #353535; color: #eeeeee; font-size: 12px;
}
QLabel { background: transparent; }
QLabel#appTitle { font-size: 19px; font-weight: 600; }
QLabel#sectionTitle { font-size: 14px; font-weight: 600; }
QLabel#mutedLabel { color: #b6b6b6; }
QLabel#progressTitle { font-size: 15px; font-weight: 600; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QTableWidget, QListWidget {
    background: #252525; color: #eeeeee; border: 1px solid #555555;
    border-radius: 4px; selection-background-color: #527f9a;
    selection-color: #ffffff;
}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { padding: 5px; min-height: 21px; }
QPlainTextEdit { padding: 5px; }
QListWidget { padding: 4px; outline: none; }
QListWidget::item { padding: 9px 7px; border-radius: 3px; }
QListWidget::item:selected { background: #3c6177; color: #ffffff; }
QListWidget::item:hover:!selected { background: #404040; }
QPushButton, QToolButton {
    background: #484848; color: #eeeeee; border: 1px solid #626262;
    border-radius: 4px; padding: 7px 11px;
}
QPushButton:hover, QToolButton:hover { background: #565656; border-color: #777777; }
QPushButton:pressed, QToolButton:pressed { background: #3c6177; }
QPushButton#primaryButton { background: #3c6177; border-color: #527f9a; font-weight: 600; }
QPushButton#primaryButton:hover { background: #527f9a; }
QPushButton#cancelButton { border-color: #9b8363; }
QPushButton:disabled, QToolButton:disabled { background: #3c3c3c; border-color: #4b4b4b; }
QWidget:disabled { color: #929292; }
QHeaderView::section {
    background: #444444; border: none; border-right: 1px solid #555555;
    padding: 7px; color: #eeeeee;
}
QTableWidget { gridline-color: #444444; alternate-background-color: #303030; }
QComboBox QAbstractItemView { background: #252525; selection-background-color: #527f9a; }
QScrollArea { border: none; }
QScrollBar:vertical { background: #303030; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #626262; min-height: 24px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 12px; background: #303030; }
QScrollBar::handle:horizontal { background: #626262; min-width: 24px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QProgressBar {
    background: #252525; border: 1px solid #555555; border-radius: 5px;
    text-align: center; min-height: 27px; color: #ffffff;
}
QProgressBar::chunk { background: #527f9a; border-radius: 4px; }
QProgressBar#fileProgress { min-height: 5px; max-height: 7px; border: none; border-radius: 3px; }
QProgressBar#fileProgress::chunk { background: #789aae; border-radius: 3px; }
QToolTip { background: #252525; color: #eeeeee; border: 1px solid #777777; padding: 6px; }
QSplitter::handle { background: #454545; }
QCheckBox { spacing: 7px; }
QGroupBox { border: 1px solid #555555; border-radius: 5px; margin-top: 11px; padding-top: 9px; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left; left: 10px; padding: 0 5px; }
QTabWidget::pane { background: #353535; border: 1px solid #555555; border-radius: 4px; }
QTabBar::tab { background: #404040; color: #dddddd; padding: 9px 14px; margin-right: 3px; }
QTabBar::tab:selected { background: #3c6177; color: #ffffff; }
QTabBar::tab:hover { background: #505050; }
QFrame#progressPanel { background: #303030; border: 1px solid #4d4d4d; border-radius: 6px; }
QFrame#progressPanel QLabel { background: transparent; border: none; }
QFrame#sidebar { background: #303030; border: 1px solid #3d3d3d; border-radius: 8px; }
QFrame#sidebar QLabel { background: transparent; }
QMenuBar { background: #303030; }
QMenuBar::item:selected, QMenu::item:selected { background: #3c6177; }
QMenu { background: #353535; border: 1px solid #626262; }
QMenu::item { padding: 6px 8px 6px 2px; }
"""

STYLESHEET = DARK_STYLESHEET
