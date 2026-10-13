"""Standalone Qt interface for backup plans, change previews, and live progress."""

import copy
import ntpath
import re

from sync_engine_qt import QtCore, QtGui, QtWidgets, qt_enum
from sync_engine_style import (
    DARK_STYLESHEET, icon_path, multiline_tooltip, padded_icon,
)
from sync_engine_widgets import PathLabel, format_bytes, format_duration
from sync_engine_pair_view import FolderPairEditor
from sync_engine_version import VERSION


class _VariableNameDelegate(QtWidgets.QStyledItemDelegate):
    """Keeps custom variable names in the required braced token format."""

    def createEditor(self, parent, option, index):
        """Creates a line edit that accepts only ``{variable-name}`` tokens."""
        editor = QtWidgets.QLineEdit(parent)
        expression = QtCore.QRegularExpression(r"\{[A-Za-z_][A-Za-z0-9_-]*\}")
        editor.setValidator(QtGui.QRegularExpressionValidator(expression, editor))
        return editor


class _SplitterGripHandle(QtWidgets.QSplitterHandle):
    """Paints a subtle three-dot grip on a draggable splitter handle."""

    def __init__(self, orientation, parent=None):
        """Creates a splitter handle with a resize cursor and hover state."""
        super().__init__(orientation, parent)
        self._hovered = False
        self.setMouseTracking(True)
        self.setCursor(qt_enum(QtCore.Qt, "CursorShape", "SplitHCursor"))
        self.setToolTip("Drag to resize the plan list")

    def enterEvent(self, event):
        """Brightens the grip slightly while the pointer is over it."""
        self._hovered = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Restores the resting grip color after the pointer leaves."""
        self._hovered = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        """Draws three restrained dots at the center of the handle."""
        color = "#92999f" if self._hovered else "#737a80"
        painter = QtGui.QPainter(self)
        painter.setPen(QtGui.QPen(QtGui.QColor(color), 2))
        center = self.rect().center()
        for offset in (-5, 0, 5):
            painter.drawPoint(center.x(), center.y() + offset)
        painter.end()


class _GripSplitter(QtWidgets.QSplitter):
    """Creates splitter handles with the application's centered grip."""

    def createHandle(self):
        """Creates the custom draggable handle."""
        return _SplitterGripHandle(self.orientation(), self)


class _ResizablePreviewTable(QtWidgets.QTableWidget):
    """Notifies the view when the preview viewport needs its columns refit."""

    viewport_resized = QtCore.Signal()

    def resizeEvent(self, event):
        """Refit the final preview column after the table changes size.

        Args:
            event (QResizeEvent): Qt resize event.
        """
        super().resizeEvent(event)
        self.viewport_resized.emit()


class _ResizablePreviewTree(QtWidgets.QTreeWidget):
    """Notifies the view when the hierarchy preview needs its columns refit."""

    viewport_resized = QtCore.Signal()

    def resizeEvent(self, event):
        """Refit the hierarchy columns after the tree changes size."""
        super().resizeEvent(event)
        self.viewport_resized.emit()


class _PreviewProgressDelegate(QtWidgets.QStyledItemDelegate):
    """Paints compact progress bars for active preview operations."""

    def paint(self, painter, option, index):
        """Paints a blue inline bar and centered percentage for active rows."""
        progress = index.data(qt_enum(QtCore.Qt, "ItemDataRole", "UserRole"))
        if progress is None:
            super().paint(painter, option, index)
            return
        value = max(0, min(1000, int(progress)))
        bar_rect = option.rect.adjusted(5, 5, -5, -5)
        if bar_rect.width() <= 0 or bar_rect.height() <= 0:
            return
        painter.save()
        painter.setRenderHint(
            qt_enum(QtGui.QPainter, "RenderHint", "Antialiasing"), True
        )
        painter.setPen(QtGui.QPen(QtGui.QColor("#566772"), 1))
        painter.setBrush(QtGui.QColor("#25292c"))
        painter.drawRoundedRect(bar_rect, 4, 4)
        if value:
            fill_rect = bar_rect.adjusted(1, 1, -1, -1)
            fill_rect.setWidth(round(fill_rect.width() * value / 1000))
            painter.save()
            painter.setClipRect(bar_rect.adjusted(1, 1, -1, -1))
            painter.setPen(qt_enum(QtCore.Qt, "PenStyle", "NoPen"))
            painter.setBrush(QtGui.QColor("#789aae"))
            painter.drawRoundedRect(fill_rect, 3, 3)
            painter.restore()
        painter.setPen(QtGui.QColor("#f2f5f7"))
        painter.drawText(
            bar_rect,
            qt_enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"),
            self._percentage_text(value),
        )
        painter.restore()

    @staticmethod
    def _percentage_text(progress):
        """Formats inline progress without rounding an incomplete bar to 100%."""
        value = max(0, min(1000, int(progress)))
        percentage = 100 if value == 1000 else min(99, round(value / 10))
        return f"{percentage}%"


_PREVIEW_LABELS = {
    "copy": "New File", "update": "Replace File", "delete": "Remove File",
    "mkdir": "New Folder", "rmdir": "Remove Folder", "conflict": "Resolve Conflict",
    "skipped": "Skipped (Filtered)", "unchanged": "Unchanged", "preserved": "Preserved",
}
_PREVIEW_REASONS = {
    "copy": "No matching file exists on the target side.",
    "update": "The source and target differ under the selected comparison.",
    "delete": "This extra file will be removed in Mirror mode.",
    "mkdir": "This folder does not exist on the target side.",
    "rmdir": "This extra folder will be removed in Mirror mode.",
    "conflict": "Both sides changed since the last successful two-way sync.",
    "unchanged": "The item already exists on both sides and needs no change.",
    "preserved": "This item is preserved by the selected sync mode.",
}
_EXECUTED_PREVIEW_LABELS = {
    "copy": "Copied", "update": "Updated", "delete": "Removed",
    "mkdir": "Created Folder", "rmdir": "Removed Folder",
}
_ENVIRONMENT_VARIABLE_DESCRIPTIONS = {
    "{date}": "Current date in YYYY-MM-DD format.",
    "{day}": "Current day of the month using two digits.",
    "{desktop-dir}": "Current user's Desktop folder.",
    "{documents-dir}": "Current user's Documents folder.",
    "{downloads-dir}": "Current user's Downloads folder.",
    "{home-dir}": "Current user's home folder.",
    "{hostname}": "Name of the computer running SyncEngine.",
    "{month}": "Current month using two digits.",
    "{plan-dir}": "Folder containing the active .syncplan or .syncrun file.",
    "{plan-file}": "Full path to the active .syncplan or .syncrun file.",
    "{plan-id}": "Stable ID of the active sync plan.",
    "{plan-name}": "Display name of the active sync plan.",
    "{plan-parent-dir}": "Folder one level above the active plan-file folder.",
    "{project-dir}": "Legacy alias for the active plan-file folder.",
    "{project-file-dir}": "Legacy alias for the active plan-file folder.",
    "{sync-file}": "Full path to the active .syncplan or .syncrun file.",
    "{sync-file-dir}": "Folder containing the active .syncplan or .syncrun file.",
    "{sync-file-parent}": "Parent folder of the active sync file.",
    "{temp-dir}": "Operating system temporary folder.",
    "{time}": "Current time in hours, minutes, and seconds.",
    "{user}": "Name of the current user.",
    "{user-name}": "Name of the current user.",
    "{year}": "Current year using four digits.",
}
_PREVIEW_ROW_COLORS = {
    "copy": ("#26372c", "#c3dbc8"), "mkdir": ("#26372c", "#c3dbc8"),
    "update": ("#283541", "#c4d8e8"), "delete": ("#3a3026", "#e1c79d"),
    "rmdir": ("#3a3026", "#e1c79d"), "conflict": ("#402b2b", "#edb1a6"),
    "skipped": ("#2d3033", "#aeb4b9"), "unchanged": ("#2b3033", "#bbc2c7"),
    "preserved": ("#30312c", "#d3d0ad"),
}


class SyncEngineView(QtWidgets.QMainWindow):
    """Edits serializable backup plans without invoking filesystem operations."""

    new_plan_requested = QtCore.Signal()
    duplicate_plan_requested = QtCore.Signal()
    remove_plan_requested = QtCore.Signal()
    save_requested = QtCore.Signal()
    import_requested = QtCore.Signal()
    export_requested = QtCore.Signal()
    export_action_requested = QtCore.Signal()
    preview_requested = QtCore.Signal()
    sync_requested = QtCore.Signal()
    cancel_requested = QtCore.Signal()
    register_actions_requested = QtCore.Signal()
    show_logs_requested = QtCore.Signal()
    preferences_requested = QtCore.Signal()
    automatic_runs_requested = QtCore.Signal()
    show_environment_variables_requested = QtCore.Signal()
    show_reports_requested = QtCore.Signal()
    plan_context_requested = QtCore.Signal(str, str)
    variable_test_requested = QtCore.Signal(str)
    path_feedback_requested = QtCore.Signal(str, str)
    conflict_resolution_requested = QtCore.Signal(str, str)
    plan_selected = QtCore.Signal(str)
    plan_edited = QtCore.Signal()
    closing = QtCore.Signal(object)

    def __init__(self, parent=None):
        """Builds a complete independent window with no external state required.

        Args:
            parent (QWidget, optional): Owning widget.
        """
        super().__init__(parent)
        self._plan = None
        self._loading = False
        self._busy = False
        self._automatic_run_mode = False
        self._dirty = False
        self._countdown_total = None
        self._preview_progress_items = {"table": {}, "tree": {}}
        self._preview_displayed = []
        self._preview_pairs = []
        self._preview_operations = []
        self._preview_completion_items = {}
        self._preview_execution_status = {}
        self._has_preview_summary = False
        self._preview_generation_active = False
        self._preview_scan_rows = {}
        self._preview_scan_row_limit = 500
        self._automatic_confirmation_dialog = None
        self.preview_show_full_target_path = False
        self.preview_target_path_depth = 2
        self._file_menu_actions = []
        self._data_role = qt_enum(QtCore.Qt, "ItemDataRole", "UserRole")
        self.setWindowTitle("SyncEngine")
        self.setWindowIcon(QtGui.QIcon(icon_path()))
        self.setStyleSheet(DARK_STYLESHEET)
        self.resize(1120, 850)
        self.setMinimumSize(820, 660)
        self._build_menu()
        self._build_window()
        self._connect_fields()
        self.set_plan(None)
        self.set_status("Create a sync plan or import an existing one.")

    def _build_menu(self):
        """Creates standard file and application menus with retained actions."""
        file_menu = self.menuBar().addMenu("File")
        self.file_menu = file_menu
        self.utilities_menu = self.menuBar().addMenu("Utilities")
        self.environment_variables_action = self.utilities_menu.addAction("Environment Variables…")
        self.environment_variables_action.setIcon(
            padded_icon("icons/variable_add.svg")
        )
        self.environment_variables_action.triggered.connect(self.show_environment_variables_requested.emit)
        self.reports_action = self.utilities_menu.addAction("Open Sync Reports Folder")
        self.reports_action.setIcon(padded_icon("icons/menu_folder.svg"))
        self.reports_action.triggered.connect(self.show_reports_requested.emit)
        definitions = (
            ("New Sync Plan", self.new_plan_requested, "Ctrl+N", "ui_add.svg"),
            ("Save Sync Plan", self.save_requested, "Ctrl+S", "ui_save.svg"),
            ("Import Plan…", self.import_requested, "Ctrl+O", "rigger_action_import_grayscale.svg"),
            ("Export Selected Plan…", self.export_requested, "", "rigger_action_export_grayscale.svg"),
            ("Export Automatic Action…", self.export_action_requested, "", "rigger_action_export_grayscale.svg"),
        )
        for label, signal, shortcut, icon_name in definitions:
            action = file_menu.addAction(
                padded_icon(f"icons/{icon_name}"), label
            )
            action.triggered.connect(signal.emit)
            if shortcut:
                action.setShortcut(QtGui.QKeySequence(shortcut))
            self._file_menu_actions.append(action)
        file_menu.addSeparator()
        self.close_action = file_menu.addAction(
            padded_icon("icons/close.svg"), "Close"
        )
        self.close_action.triggered.connect(self.close)
        tools_menu = self.menuBar().addMenu("Tools")
        self.tools_menu = tools_menu
        self.register_action = tools_menu.addAction("Register Action File Extension…")
        self.register_action.setIcon(padded_icon("icons/rigger_action_import_grayscale.svg"))
        self.register_action.triggered.connect(self.register_actions_requested.emit)
        self.logs_action = tools_menu.addAction("Open Log Folder")
        self.logs_action.setIcon(padded_icon("icons/menu_folder.svg"))
        self.logs_action.triggered.connect(self.show_logs_requested.emit)
        tools_menu.addSeparator()
        self.preferences_action = tools_menu.addAction("Preferences…")
        self.preferences_action.setIcon(padded_icon("icons/menu_settings.svg"))
        self.preferences_action.triggered.connect(self.preferences_requested.emit)
        self.automatic_runs_action = tools_menu.addAction("Automatic Runs…")
        self.automatic_runs_action.setIcon(padded_icon("icons/setup_run_only.svg"))
        self.automatic_runs_action.triggered.connect(self.automatic_runs_requested.emit)
        help_menu = self.menuBar().addMenu("Help")
        self.help_menu = help_menu
        self.about_action = help_menu.addAction(
            padded_icon("icons/menu_info.svg"), "About SyncEngine"
        )
        self.about_action.triggered.connect(self._show_about)

    def _build_window(self):
        """Constructs the sidebar, tabbed settings, and persistent progress area."""
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        layout = QtWidgets.QHBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)
        self.main_splitter = _GripSplitter(qt_enum(QtCore.Qt, "Orientation", "Horizontal"))
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.setHandleWidth(20)
        self.main_splitter.setStyleSheet("QSplitter::handle:horizontal { background: transparent; }")
        layout.addWidget(self.main_splitter)
        self.main_splitter.addWidget(self._build_sidebar())
        main = QtWidgets.QWidget()
        main_layout = QtWidgets.QVBoxLayout(main)
        main_layout.setContentsMargins(14, 0, 0, 0)
        main_layout.setSpacing(10)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("SyncEngine")
        title.setObjectName("appTitle")
        heading.addWidget(title)
        heading.addStretch()
        self.save_button = self._button("Save", self.save_requested.emit)
        heading.addWidget(self.save_button)
        main_layout.addLayout(heading)
        self.plan_heading = QtWidgets.QWidget()
        plan_layout = QtWidgets.QHBoxLayout(self.plan_heading)
        plan_layout.setContentsMargins(0, 0, 0, 0)
        plan_layout.addWidget(QtWidgets.QLabel("Sync Plan"))
        self.plan_name = QtWidgets.QLineEdit()
        self.plan_name.setPlaceholderText("Name your sync plan")
        plan_layout.addWidget(self.plan_name, 1)
        main_layout.addWidget(self.plan_heading)
        self.settings_tabs = QtWidgets.QTabWidget()
        self.folders_page = self._build_folders_page()
        self.exclusions_page = self.folders_page.exclusions_page
        self.folders_page.path_feedback_requested.connect(self.path_feedback_requested.emit)
        self.options_page = self._build_options_page()
        self.automation_page = self._build_automation_page()
        self.preview_page = self._build_preview_page()
        self.settings_tabs.addTab(self.folders_page, "Folders")
        self.settings_tabs.addTab(self.exclusions_page, "Filters")
        self.variables_page = self._build_variables_page()
        self.settings_tabs.addTab(self.variables_page, "Variables")
        self.settings_tabs.addTab(self.options_page, "Options")
        self.settings_tabs.addTab(self.automation_page, "Automation")
        self.settings_tabs.addTab(self.preview_page, "Preview")
        main_layout.addWidget(self.settings_tabs, 1)
        self.progress_panel = self._build_progress()
        main_layout.addWidget(self.progress_panel)
        self.log_panel = self._build_log()
        main_layout.addWidget(self.log_panel)
        self.main_splitter.addWidget(main)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([230, 866])
        self.status_bar = QtWidgets.QStatusBar()
        self.status_bar.setObjectName("feedbackBar")
        self.status_bar.setContentsMargins(12, 0, 12, 0)
        self.status_bar.setSizeGripEnabled(False)
        self.status_bar.setStyleSheet(
            "QStatusBar#feedbackBar { background: #292d31; border-top: 1px solid #444a50; "
            "padding: 0px; min-height: 52px; } "
            "QStatusBar::item { border: none; }"
        )
        self.setStatusBar(self.status_bar)
        self.feedback_footer = QtWidgets.QWidget()
        footer_layout = QtWidgets.QHBoxLayout(self.feedback_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(8)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(False)
        self.status_label.setMinimumWidth(0)
        self.status_label.setContentsMargins(4, 0, 10, 0)
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Ignored"),
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Fixed"),
        ))
        footer_layout.addWidget(self.status_label, 1)
        self.summary_label = QtWidgets.QLabel()
        self.summary_label.setObjectName("mutedLabel")
        self.summary_label.setContentsMargins(8, 0, 12, 0)
        self.summary_label.hide()
        footer_layout.addWidget(self.summary_label)
        self.unsaved_label = QtWidgets.QLabel("Unsaved changes to this sync plan")
        self.unsaved_label.setStyleSheet(
            "QLabel { color: #e7c75f; font-weight: 600; padding: 3px 10px; }"
        )
        self.unsaved_label.hide()
        footer_layout.addWidget(self.unsaved_label)
        footer_layout.addStretch(1)
        self.cancel_button = self._button("Cancel", self.cancel_requested.emit)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.setMinimumWidth(84)
        self.cancel_button.hide()
        self.open_full_app_button = self._button("Open Full App", self.show_full_app)
        self.open_full_app_button.setMinimumWidth(118)
        self.open_full_app_button.hide()
        self.preview_button = self._button("Preview", self.preview_requested.emit)
        self.preview_button.setMinimumWidth(96)
        self.sync_button = self._button("Sync", self.sync_requested.emit)
        self.sync_button.setObjectName("primaryButton")
        self.sync_button.setMinimumWidth(104)
        button_policy = QtWidgets.QSizePolicy(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Fixed"),
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Fixed"),
        )
        for button in (
            self.open_full_app_button, self.cancel_button, self.preview_button, self.sync_button,
        ):
            button.setSizePolicy(button_policy)
        footer_layout.addWidget(self.open_full_app_button)
        footer_layout.addWidget(self.cancel_button)
        footer_layout.addWidget(self.preview_button)
        footer_layout.addWidget(self.sync_button)
        self.status_bar.addWidget(self.feedback_footer, 1)
        self.settings_tabs.currentChanged.connect(self._update_progress_panel_visibility)
        self._update_progress_panel_visibility()
        self._update_footer_visibility()

    def resizeEvent(self, event):
        """Prioritizes action buttons over optional footer text at narrow widths."""
        super().resizeEvent(event)
        if hasattr(self, "feedback_footer"):
            self._update_footer_visibility()

    def showEvent(self, event):
        """Refreshes status-bar layout after QMainWindow assigns its first geometry."""
        super().showEvent(event)
        QtCore.QTimer.singleShot(0, self._refresh_footer_layout)

    def _refresh_footer_layout(self):
        """Activates the footer layout after it receives its status-bar dimensions."""
        if not hasattr(self, "feedback_footer"):
            return
        self.feedback_footer.layout().activate()
        self._update_footer_visibility()

    def _update_footer_visibility(self):
        """Hides secondary feedback before the main action buttons are compressed."""
        footer_width = self.feedback_footer.width()
        self.status_label.setVisible(footer_width >= 900)
        self.summary_label.setVisible(
            not self._automatic_run_mode and self._has_preview_summary and footer_width >= 1120
        )

    def _build_sidebar(self):
        """Builds the persistent backup-plan list and plan management controls.

        Returns:
            QFrame: Completed sidebar.
        """
        self.sidebar = QtWidgets.QFrame()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setMinimumWidth(168)
        self.sidebar.setMaximumWidth(350)
        layout = QtWidgets.QVBoxLayout(self.sidebar)
        layout.setContentsMargins(10, 10, 14, 10)
        title = QtWidgets.QLabel("Sync Plans")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.plans_list = QtWidgets.QListWidget()
        self.plans_list.setToolTip("Each sync plan contains one or more source and destination folder pairs.")
        self.plans_list.currentItemChanged.connect(self._select_plan)
        self.plans_list.setContextMenuPolicy(qt_enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.plans_list.customContextMenuRequested.connect(self._show_plan_context_menu)
        layout.addWidget(self.plans_list, 1)
        self.new_button = self._button("New Plan", self.new_plan_requested.emit)
        layout.addWidget(self.new_button)
        return self.sidebar

    def _build_plan_context_menu(self, item):
        """Creates the plan-management menu with centered, padded icons.

        Args:
            item (QListWidgetItem or None): Selected plan item, or None for the empty list.

        Returns:
            QMenu: Configured plan context menu.
        """
        menu = QtWidgets.QMenu(self)
        if item is None:
            menu.addAction(
                padded_icon("icons/ui_add.svg"), "New Sync Plan",
                self.new_plan_requested.emit,
            )
            menu.addAction(
                padded_icon("icons/rigger_action_import_grayscale.svg"), "Import Plan…",
                self.import_requested.emit,
            )
            return menu
        plan_id = item.data(self._data_role)
        definitions = (
            ("Duplicate Plan", "duplicate", "rigger_action_duplicate_grayscale.svg"),
            ("Export Plan…", "export", "rigger_action_export_grayscale.svg"),
            ("Export Automatic Action…", "export_action", "rigger_action_export_grayscale.svg"),
            ("Import Plan…", "import", "rigger_action_import_grayscale.svg"),
            ("Preview", "preview", "menu_preview.svg"),
            ("Sync Now", "sync", "setup_run_only.svg"),
        )
        for label, action_name, icon_name in definitions:
            action = menu.addAction(padded_icon(f"icons/{icon_name}"), label)
            action.triggered.connect(
                lambda checked=False, identifier=plan_id, name=action_name:
                self.plan_context_requested.emit(identifier, name)
            )
        menu.addSeparator()
        delete_action = menu.addAction(
            padded_icon("icons/rigger_action_delete_grayscale.svg"), "Delete Plan…"
        )
        delete_action.triggered.connect(
            lambda checked=False, identifier=plan_id:
            self.plan_context_requested.emit(identifier, "delete")
        )
        return menu

    def _show_plan_context_menu(self, position):
        """Shows plan actions beside the item under the pointer."""
        item = self.plans_list.itemAt(position)
        if item is not None:
            self.plans_list.setCurrentItem(item)
        menu = self._build_plan_context_menu(item)
        runner = getattr(menu, "exec", None) or menu.exec_
        runner(self.plans_list.mapToGlobal(position))

    def _build_folders_page(self):
        """Creates the independent ordered folder-pair editor.

        Returns:
            FolderPairEditor: Folder-pair editing tab.
        """
        return FolderPairEditor()

    def _build_options_page(self):
        """Creates lightweight comparison options and guarded mirror settings.

        Returns:
            QWidget: Comparison settings tab.
        """
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        comparison = QtWidgets.QGroupBox("Compare Files")
        form = QtWidgets.QFormLayout(comparison)
        self.compare_combo = QtWidgets.QComboBox()
        self.compare_combo.addItem("Size and modified time (fast)", "metadata")
        self.compare_combo.addItem("File contents (more disk reading)", "content")
        self.compare_combo.setToolTip(multiline_tooltip(
            "Size and modified time avoids rereading unchanged files. Content comparison reads file data "
            "to detect changes even when size and timestamp match."
        ))
        form.addRow("Comparison", self.compare_combo)
        self.timestamp_tolerance = QtWidgets.QDoubleSpinBox()
        self.timestamp_tolerance.setRange(0, 120)
        self.timestamp_tolerance.setDecimals(3)
        self.timestamp_tolerance.setSingleStep(1)
        self.timestamp_tolerance.setSuffix(" seconds")
        self.timestamp_tolerance.setToolTip(multiline_tooltip(
            "Treat matching file sizes and modified times within this difference as unchanged. "
            "Use a small tolerance for destinations with lower timestamp precision."
        ))
        form.addRow("Timestamp Tolerance", self.timestamp_tolerance)
        layout.addWidget(comparison)
        hint = QtWidgets.QLabel(
            "Update and Mirror copy changed files. Two-way merges both folders using saved history. "
            "Add only preserves every existing destination file."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)
        safety = QtWidgets.QGroupBox("Deletion Safeguards")
        safety_layout = QtWidgets.QVBoxLayout(safety)
        self.allow_empty_mirror = QtWidgets.QCheckBox(
            "Allow an Empty Source to Remove Destination Contents"
        )
        self.allow_empty_mirror.setToolTip(multiline_tooltip(
            "Normally, Mirror refuses removals when the source has no included files. "
            "Enable only if an intentionally empty source should clear its destination. "
            "A missing or unreadable source always stops the run."
        ))
        safety_layout.addWidget(self.allow_empty_mirror)
        warning = QtWidgets.QLabel(
            "Mirror removes destination extras. Two-way sync propagates file deletions from either side. "
            "Both modes ask before manual deletion; automatic actions require explicit permission."
        )
        warning.setWordWrap(True)
        warning.setObjectName("mutedLabel")
        safety_layout.addWidget(warning)
        layout.addWidget(safety)
        execution = QtWidgets.QGroupBox("Run Monitoring")
        execution_layout = QtWidgets.QFormLayout(execution)
        self.timeout_seconds = QtWidgets.QSpinBox()
        self.timeout_seconds.setRange(0, 604800)
        self.timeout_seconds.setSingleStep(60)
        self.timeout_seconds.setSuffix(" seconds")
        self.timeout_seconds.setToolTip(
            "Maximum elapsed time for a sync scan and transfer. 0 disables the timeout. "
            "The timeout is checked between filesystem operations and copy chunks."
        )
        execution_layout.addRow("Timeout", self.timeout_seconds)
        self.expand_log_while_syncing = QtWidgets.QCheckBox("Expand the Run Log While Syncing")
        self.expand_log_while_syncing.setToolTip(
            "Shows completed file operations in the run log while the progress panel tracks the current file."
        )
        execution_layout.addRow(self.expand_log_while_syncing)
        self.generate_report = QtWidgets.QCheckBox("Write a Detailed Report After Each Sync")
        self.generate_report.setToolTip(
            "Saves a text report under the SyncEngine logs/reports folder, including transfers, "
            "unchanged files, conflicts, exclusions, bytes, and errors."
        )
        execution_layout.addRow(self.generate_report)
        layout.addWidget(execution)
        layout.addStretch()
        return page

    def _build_variables_page(self):
        """Creates the per-plan custom environment-variable editor.

        Returns:
            QWidget: Variable definitions and built-in variable guidance.
        """
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        hint = QtWidgets.QLabel(
            "Use {variable-name} tokens in source and destination paths. Custom values can refer to other "
            "custom variables. Turn on Query to evaluate a restricted expression when the sync runs."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)
        built_in_hint = QtWidgets.QLabel(
            "Built-ins: {plan-dir}, {sync-file-dir}, and {sync-file-parent} point to the folder "
            "containing the active .syncplan or .syncrun file; {plan-parent-dir} points one level above. "
            "Also available: {plan-file}, {plan-name}, {plan-id}, {home-dir}, {desktop-dir}, "
            "{documents-dir}, {downloads-dir}, {temp-dir}, {user-name}, {hostname}, {date}, "
            "{year}, {month}, {day}, and {time}."
        )
        built_in_hint.setWordWrap(True)
        built_in_hint.setObjectName("mutedLabel")
        layout.addWidget(built_in_hint)
        self.variables_table = QtWidgets.QTableWidget(0, 4)
        self.variables_table.setHorizontalHeaderLabels(
            ["Variable Token", "Value / Query Expression", "Query", "Run"]
        )
        self.variables_table.verticalHeader().hide()
        self.variables_table.setAlternatingRowColors(True)
        self.variables_table.setItemDelegateForColumn(0, _VariableNameDelegate(self.variables_table))
        self.variables_table.setContextMenuPolicy(qt_enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.variables_table.customContextMenuRequested.connect(self._show_variable_context_menu)
        header = self.variables_table.horizontalHeader()
        header.setSectionResizeMode(qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Interactive"))
        header.setSectionResizeMode(1, qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Stretch"))
        header.setSectionResizeMode(2, qt_enum(QtWidgets.QHeaderView, "ResizeMode", "ResizeToContents"))
        header.setSectionResizeMode(3, qt_enum(QtWidgets.QHeaderView, "ResizeMode", "ResizeToContents"))
        self.variables_table.setColumnWidth(0, 220)
        self.variables_table.setColumnWidth(2, 70)
        self.variables_table.setColumnWidth(3, 62)
        self.variables_table.setToolTip(
            "Tokens must match {variable-name}. Query expressions can use env[\"NAME\"], getenv(), "
            "join(), parent(), basename(), exists(), today(), now(), and expand()."
        )
        layout.addWidget(self.variables_table, 1)
        controls = QtWidgets.QHBoxLayout()
        self.add_variable_button = QtWidgets.QPushButton()
        self.add_variable_button.setIcon(padded_icon("icons/variable_add.svg"))
        self.add_variable_button.setIconSize(QtCore.QSize(24, 24))
        self.add_variable_button.setStyleSheet("QPushButton { padding: 0px; }")
        self.add_variable_button.setFixedSize(36, 34)
        self.add_variable_button.setToolTip("Add custom environment variable")
        self.add_variable_button.clicked.connect(self._add_variable_row)
        self.remove_variable_button = QtWidgets.QPushButton()
        self.remove_variable_button.setIcon(padded_icon("icons/variable_remove.svg"))
        self.remove_variable_button.setIconSize(QtCore.QSize(24, 24))
        self.remove_variable_button.setStyleSheet("QPushButton { padding: 0px; }")
        self.remove_variable_button.setFixedSize(36, 34)
        self.remove_variable_button.setToolTip("Remove selected environment variable")
        self.remove_variable_button.clicked.connect(self._remove_variable_row)
        controls.addWidget(self.add_variable_button)
        controls.addWidget(self.remove_variable_button)
        controls.addStretch()
        layout.addLayout(controls)
        return page

    def _add_variable_row(self):
        """Add a uniquely named custom variable row."""
        if self._loading or self._plan is None:
            return
        existing = {
            self.variables_table.item(row, 0).text().strip().casefold()
            for row in range(self.variables_table.rowCount())
            if self.variables_table.item(row, 0)
        }
        base_name = "{variable-name}"
        name = base_name
        suffix = 2
        while name.casefold() in existing:
            name = f"{{variable-name-{suffix}}}"
            suffix += 1
        row = self.variables_table.rowCount()
        self.variables_table.insertRow(row)
        self.variables_table.setItem(row, 0, QtWidgets.QTableWidgetItem(name))
        self.variables_table.setItem(row, 1, QtWidgets.QTableWidgetItem(""))
        self._add_variable_query_controls(row)
        self.variables_table.setRowHeight(row, 32)
        self.variables_table.setCurrentCell(row, 0)
        self.variables_table.editItem(self.variables_table.item(row, 0))
        self._edited()

    def _add_variable_query_controls(self, row, is_query=False):
        """Adds centered query and row-level run controls.

        Args:
            row (int): Variable row receiving the controls.
            is_query (bool): Whether the variable value is a query expression.
        """
        query_check = QtWidgets.QCheckBox()
        query_check.setToolTip("Evaluate this value as a restricted query expression at sync time")
        query_check.setChecked(bool(is_query))
        cell = QtWidgets.QWidget()
        cell_layout = QtWidgets.QHBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setAlignment(qt_enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"))
        cell_layout.addWidget(query_check)
        self.variables_table.setCellWidget(row, 2, cell)
        run_button = QtWidgets.QPushButton()
        run_button.setAutoDefault(False)
        run_button.setEnabled(bool(is_query))
        run_button.setIcon(padded_icon("icons/setup_run_only.svg"))
        run_button.setIconSize(QtCore.QSize(24, 24))
        run_button.setSizePolicy(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
        )
        run_button.setStyleSheet("QPushButton { padding: 0px; border-radius: 3px; }")
        run_button.setAccessibleName("Run Query")
        run_button.setToolTip("Evaluate this query with the current plan values")
        run_button.clicked.connect(
            lambda checked=False, button=run_button: self._test_variable_button(button)
        )
        run_cell = QtWidgets.QWidget()
        run_layout = QtWidgets.QHBoxLayout(run_cell)
        run_layout.setContentsMargins(1, 1, 1, 1)
        run_layout.setSpacing(0)
        run_layout.addWidget(run_button, 1)
        self.variables_table.setCellWidget(row, 3, run_cell)
        query_check.stateChanged.connect(
            lambda state, button=run_button: self._query_checkbox_changed(state, button)
        )

    def _query_checkbox_changed(self, state, run_button):
        """Enables the row's Run control and marks the plan as edited.

        Args:
            state (int): Current checkbox state.
            run_button (QPushButton): Run control associated with the checkbox.
        """
        run_button.setEnabled(bool(state))
        self._edited()

    def _test_variable_button(self, run_button):
        """Runs the variable query whose row owns the clicked button.

        Args:
            run_button (QPushButton): Clicked row-level Run control.
        """
        row = next(
            (index for index in range(self.variables_table.rowCount())
             if self.variables_table.cellWidget(index, 3)
             and self.variables_table.cellWidget(index, 3).findChild(QtWidgets.QPushButton) is run_button),
            -1,
        )
        self._test_variable_row(row)

    def _test_variable_row(self, row):
        """Emits a request to evaluate one checked custom query."""
        if row < 0 or row >= self.variables_table.rowCount():
            return
        item = self.variables_table.item(row, 0)
        cell = self.variables_table.cellWidget(row, 2)
        query_check = cell.findChild(QtWidgets.QCheckBox) if cell else None
        if item is not None and query_check and query_check.isChecked():
            self.variable_test_requested.emit(item.text().strip())

    def _show_variable_context_menu(self, position):
        """Shows query and row-management actions for a custom variable."""
        row = self.variables_table.rowAt(position.y())
        if row < 0:
            return
        self.variables_table.setCurrentCell(row, 0)
        menu = QtWidgets.QMenu(self)
        test_action = menu.addAction(
            padded_icon("icons/setup_run_only.svg"), "Run Query"
        )
        cell = self.variables_table.cellWidget(row, 2)
        query_check = cell.findChild(QtWidgets.QCheckBox) if cell else None
        test_action.setEnabled(bool(query_check and query_check.isChecked()))
        test_action.triggered.connect(lambda unused=False, selected_row=row: self._test_variable_row(selected_row))
        menu.addAction(
            padded_icon("icons/rigger_action_copy_grayscale.svg"),
            "Copy Token", self._copy_selected_variable_token,
        )
        menu.addAction(
            padded_icon("icons/rigger_action_duplicate_grayscale.svg"),
            "Duplicate Variable", self._duplicate_selected_variable,
        )
        menu.addAction(
            padded_icon("icons/rigger_action_delete_grayscale.svg"),
            "Remove Variable", self._remove_variable_row,
        )
        runner = getattr(menu, "exec", None) or menu.exec_
        runner(self.variables_table.viewport().mapToGlobal(position))

    def _copy_selected_variable_token(self):
        """Copies the selected braced variable token to the clipboard."""
        row = self.variables_table.currentRow()
        item = self.variables_table.item(row, 0) if row >= 0 else None
        if item:
            QtWidgets.QApplication.clipboard().setText(item.text().strip())

    def _duplicate_selected_variable(self):
        """Duplicates the selected variable using a unique valid token name."""
        row = self.variables_table.currentRow()
        name_item = self.variables_table.item(row, 0) if row >= 0 else None
        if name_item is None:
            return
        name = name_item.text().strip()
        base_name = name[1:-1] if name.startswith("{") and name.endswith("}") else "variable-name"
        candidate = f"{{{base_name}-copy}}"
        existing = {
            self.variables_table.item(index, 0).text().strip().casefold()
            for index in range(self.variables_table.rowCount())
            if self.variables_table.item(index, 0)
        }
        suffix = 2
        while candidate.casefold() in existing:
            candidate = f"{{{base_name}-copy-{suffix}}}"
            suffix += 1
        new_row = self.variables_table.rowCount()
        self.variables_table.insertRow(new_row)
        self.variables_table.setItem(new_row, 0, QtWidgets.QTableWidgetItem(candidate))
        source_value = self.variables_table.item(row, 1)
        self.variables_table.setItem(
            new_row, 1, QtWidgets.QTableWidgetItem(source_value.text() if source_value else "")
        )
        source_cell = self.variables_table.cellWidget(row, 2)
        source_check = source_cell.findChild(QtWidgets.QCheckBox) if source_cell else None
        self._add_variable_query_controls(
            new_row, is_query=bool(source_check and source_check.isChecked())
        )
        self.variables_table.setRowHeight(new_row, 32)
        self.variables_table.setCurrentCell(new_row, 0)
        self._edited()

    def _remove_variable_row(self):
        """Remove the selected custom variable row."""
        row = self.variables_table.currentRow()
        if row < 0 or self._loading or self._plan is None:
            return
        self.variables_table.removeRow(row)
        self._edited()

    def _build_automation_page(self):
        """Creates automatic-action settings and discoverable export controls.

        Returns:
            QWidget: Automation settings tab.
        """
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        intro = QtWidgets.QLabel(
            "Export an automatic action to run this sync plan when opened. "
            "Use the action with Windows Task Scheduler for recurring backups."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        group = QtWidgets.QGroupBox("Before an Automatic Sync")
        form = QtWidgets.QFormLayout(group)
        self.automation_start_mode = QtWidgets.QComboBox()
        self.automation_start_mode.addItem("Run Immediately", "immediate")
        self.automation_start_mode.addItem("Start Countdown", "countdown")
        self.automation_start_mode.addItem("Require Confirmation", "confirm")
        self.automation_start_mode.setToolTip(
            "Choose what happens when an automatic action is opened."
        )
        form.addRow("When Opened", self.automation_start_mode)
        self.countdown_seconds = QtWidgets.QSpinBox()
        self.countdown_seconds.setRange(0, 86400)
        self.countdown_seconds.setValue(10)
        self.countdown_seconds.setSuffix(" seconds")
        self.countdown_seconds.setToolTip("The Cancel button stays available throughout the countdown and the run.")
        self.countdown_label = QtWidgets.QLabel("Countdown")
        form.addRow(self.countdown_label, self.countdown_seconds)
        self.start_minimized = QtWidgets.QCheckBox("Start the Window Minimized")
        form.addRow("", self.start_minimized)
        self.close_after_run = QtWidgets.QCheckBox("Close the Window After the Automatic Run")
        self.close_after_run.setToolTip(
            "Close SyncEngine after the automatic action finishes. Leave this off to review "
            "the final preview and report."
        )
        form.addRow("", self.close_after_run)
        self.automation_mode_hint = QtWidgets.QLabel()
        self.automation_mode_hint.setObjectName("mutedLabel")
        self.automation_mode_hint.setWordWrap(True)
        form.addRow("", self.automation_mode_hint)
        self.automation_mirror = QtWidgets.QCheckBox(
            "Allow File Deletions in Automatic Actions"
        )
        self.automation_mirror.setToolTip(multiline_tooltip(
            "Explicitly permits removals for Mirror and Two-way pairs in an automatic action. "
            "Review a successful preview before enabling this option."
        ))
        form.addRow("", self.automation_mirror)
        layout.addWidget(group)
        note = QtWidgets.QLabel(
            "Automatic actions use the plan settings captured when exported. "
            "Export the action again after changing this plan."
        )
        note.setObjectName("mutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QtWidgets.QHBoxLayout()
        self.export_action_button = self._button("Export Automatic Action…", self.export_action_requested.emit)
        self.export_action_button.setObjectName("primaryButton")
        buttons.addWidget(self.export_action_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        layout.addStretch()
        return page

    def _build_preview_page(self):
        """Creates table and hierarchy views for every previewed target item.

        Returns:
            QWidget: Preview tab.
        """
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        preview_header = QtWidgets.QHBoxLayout()
        self.preview_summary = QtWidgets.QLabel("Run Preview to review changes before syncing.")
        self.preview_summary.setWordWrap(True)
        preview_header.addWidget(self.preview_summary, 1)
        preview_header.addWidget(QtWidgets.QLabel("View:"))
        self.preview_mode_combo = QtWidgets.QComboBox()
        self.preview_mode_combo.addItem("Table", "table")
        self.preview_mode_combo.addItem("Folder Tree", "tree")
        self.preview_mode_combo.setToolTip(
            "Choose how to browse the preview results. This view choice is saved with the sync plan."
        )
        preview_header.addWidget(self.preview_mode_combo)
        layout.addLayout(preview_header)
        self.preview_stack = QtWidgets.QStackedWidget()
        self.preview_table = _ResizablePreviewTable(0, 6)
        self.preview_table.setHorizontalHeaderLabels(
            ["Change", "Folder Pair", "Relative Path", "Reason", "Size", "File Progress"]
        )
        self.preview_table.setEditTriggers(qt_enum(QtWidgets.QAbstractItemView, "EditTrigger", "NoEditTriggers"))
        self.preview_table.setSelectionBehavior(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"
        ))
        self.preview_table.setSelectionMode(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"
        ))
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setWordWrap(False)
        self.preview_table.verticalHeader().hide()
        header = self.preview_table.horizontalHeader()
        header.setSectionResizeMode(qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Interactive"))
        header.setStretchLastSection(False)
        for column, width in enumerate((130, 150, 230, 300, 70, 135)):
            self.preview_table.setColumnWidth(column, width)
        self.preview_table.setItemDelegateForColumn(5, _PreviewProgressDelegate(self.preview_table))
        header.setMinimumSectionSize(42)
        self.preview_table.viewport_resized.connect(self._fill_preview_table)
        header.sectionResized.connect(self._fill_preview_table)
        self.preview_stack.addWidget(self.preview_table)
        self.preview_tree = _ResizablePreviewTree()
        self.preview_tree.setHeaderLabels(
            ["Target Folder / Item", "Change", "Reason", "Size", "File Progress"]
        )
        self.preview_tree.setEditTriggers(qt_enum(
            QtWidgets.QAbstractItemView, "EditTrigger", "NoEditTriggers"
        ))
        self.preview_tree.setSelectionBehavior(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"
        ))
        self.preview_tree.setSelectionMode(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"
        ))
        self.preview_tree.setUniformRowHeights(True)
        tree_header = self.preview_tree.header()
        tree_header.setSectionResizeMode(qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Interactive"))
        tree_header.setStretchLastSection(False)
        for column, width in enumerate((310, 155, 360, 75, 140)):
            self.preview_tree.setColumnWidth(column, width)
        self.preview_tree.setColumnWidth(1, 150)
        self.preview_tree.setItemDelegateForColumn(4, _PreviewProgressDelegate(self.preview_tree))
        tree_header.setMinimumSectionSize(42)
        self.preview_tree.viewport_resized.connect(self._fill_preview_tree_columns)
        tree_header.sectionResized.connect(self._fill_preview_tree_columns)
        self.preview_tree.currentItemChanged.connect(self._preview_tree_selection_changed)
        self.preview_stack.addWidget(self.preview_tree)
        self.preview_mode_combo.currentIndexChanged.connect(self._preview_mode_changed)
        self._fill_preview_table()
        self._fill_preview_tree_columns()
        layout.addWidget(self.preview_stack, 1)
        self.preview_note = QtWidgets.QLabel(
            "Items filtered by Include or Exclude rules remain outside the sync scope."
        )
        self.preview_note.setObjectName("mutedLabel")
        self.preview_note.setWordWrap(True)
        layout.addWidget(self.preview_note)
        conflict_buttons = QtWidgets.QHBoxLayout()
        self.use_source_button = self._button(
            "Use Source Version", lambda: self._resolve_selected_conflict("source")
        )
        self.use_backup_button = self._button(
            "Use Backup Version", lambda: self._resolve_selected_conflict("destination")
        )
        self.use_source_button.setEnabled(False)
        self.use_backup_button.setEnabled(False)
        conflict_buttons.addWidget(self.use_source_button)
        conflict_buttons.addWidget(self.use_backup_button)
        conflict_buttons.addStretch()
        layout.addLayout(conflict_buttons)
        self.preview_table.itemSelectionChanged.connect(self._update_conflict_buttons)
        return page

    def _preview_mode_changed(self, index):
        """Switches between the table and destination hierarchy preview.

        Args:
            index (int): Selected preview mode index.
        """
        self.preview_stack.setCurrentIndex(max(0, index))
        self._update_conflict_buttons()
        self._edited()

    def _preview_tree_selection_changed(self, current, previous):
        """Refreshes conflict choices when the hierarchy selection changes.

        Args:
            current (QTreeWidgetItem or None): Newly selected tree row.
            previous (QTreeWidgetItem or None): Previously selected tree row.
        """
        self._update_conflict_buttons()

    def _fill_preview_table(self, resized_section=None, *unused):
        """Fills spare width while keeping Size compact and all columns adjustable.

        Args:
            resized_section (int or None): Header section changed by the user.
            *unused (tuple): Additional Qt signal values.
        """
        self._fit_preview_columns(
            self.preview_table,
            minimum_widths=(70, 80, 100, 115, 48, 95),
            shrink_order=(3, 2, 1, 0, 5, 4),
            expand_order=(3, 2, 1, 0, 5),
            resized_section=resized_section,
            guard_name="_filling_preview_table",
        )

    def _fill_preview_tree_columns(self, resized_section=None, *unused):
        """Fills the hierarchy preview width while retaining interactive columns."""
        self._fit_preview_columns(
            self.preview_tree,
            minimum_widths=(150, 70, 115, 48, 95),
            shrink_order=(2, 1, 4, 3, 0),
            expand_order=(0, 2, 1, 3, 4),
            resized_section=resized_section,
            guard_name="_filling_preview_tree",
        )

    def _fit_preview_columns(self, widget, minimum_widths, shrink_order, expand_order,
                             resized_section, guard_name):
        """Uses available viewport width without overriding intentional user sizes.

        Args:
            widget (QTableWidget or QTreeWidget): Preview widget to refit.
            minimum_widths (tuple[int]): Minimum width for each column.
            shrink_order (tuple[int]): Columns reduced first when the viewport is narrow.
            expand_order (tuple[int]): Columns expanded when the viewport has spare width.
            resized_section (int or None): Column most recently resized by the user.
            guard_name (str): Instance attribute preventing recursive resize signals.
        """
        if getattr(self, guard_name, False):
            return
        available_width = widget.viewport().width()
        if available_width <= 0:
            return
        self.__dict__[guard_name] = True
        try:
            widths = [widget.columnWidth(index) for index in range(widget.columnCount())]
            for index, minimum_width in enumerate(minimum_widths):
                widths[index] = max(minimum_width, widths[index])
            excess_width = sum(widths) - available_width
            if excess_width > 0:
                candidates = [index for index in shrink_order if index != resized_section]
                candidates.extend(
                    index for index in shrink_order
                    if index == resized_section and index not in candidates
                )
                for index in candidates:
                    reduction = min(widths[index] - minimum_widths[index], excess_width)
                    if reduction <= 0:
                        continue
                    widths[index] -= reduction
                    excess_width -= reduction
                    if excess_width <= 0:
                        break
            spare_width = available_width - sum(widths)
            if spare_width > 0:
                targets = [index for index in expand_order if index != resized_section]
                if not targets:
                    targets = list(expand_order)
                if targets:
                    widths[targets[0]] += spare_width
            for index, width in enumerate(widths):
                if widget.columnWidth(index) != width:
                    widget.setColumnWidth(index, width)
        finally:
            self.__dict__[guard_name] = False

    def _build_progress(self):
        """Builds a contextual progress panel with useful transfer metrics.

        Returns:
            QFrame: Progress panel.
        """
        panel = QtWidgets.QFrame()
        panel.setObjectName("progressPanel")
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(13, 11, 13, 11)
        layout.setSpacing(7)
        heading = QtWidgets.QHBoxLayout()
        self.progress_title = QtWidgets.QLabel("Ready")
        self.progress_title.setObjectName("progressTitle")
        heading.addWidget(self.progress_title)
        heading.addStretch()
        self.operation_count = QtWidgets.QLabel("0 / 0 changes")
        self.operation_count.setObjectName("mutedLabel")
        heading.addWidget(self.operation_count)
        layout.addLayout(heading)
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setRange(0, 1000)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)
        self.current_path = PathLabel()
        self.current_path.setObjectName("mutedLabel")
        self.current_path.set_path("Current file will appear here during a sync.")
        layout.addWidget(self.current_path)
        self.file_progress = QtWidgets.QProgressBar()
        self.file_progress.setObjectName("fileProgress")
        self.file_progress.setRange(0, 1000)
        self.file_progress.setValue(0)
        self.file_progress.setTextVisible(False)
        self.file_progress.setToolTip("Progress within the current file")
        layout.addWidget(self.file_progress)
        metrics = QtWidgets.QHBoxLayout()
        self.bytes_label = QtWidgets.QLabel("0 B / 0 B")
        self.speed_label = QtWidgets.QLabel("— /s")
        self.eta_label = QtWidgets.QLabel("Remaining —")
        self.elapsed_label = QtWidgets.QLabel("Elapsed 0s")
        for label in (self.bytes_label, self.speed_label, self.eta_label, self.elapsed_label):
            label.setObjectName("mutedLabel")
            metrics.addWidget(label)
            metrics.addStretch()
        layout.addLayout(metrics)
        return panel

    def _build_log(self):
        """Creates a compact log that expands only when requested.

        Returns:
            QWidget: Log toggle and read-only output.
        """
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        self.log_toggle = QtWidgets.QToolButton()
        self.log_toggle.setText("Run log")
        self.log_toggle.setCheckable(True)
        self.log_toggle.setToolButtonStyle(qt_enum(QtCore.Qt, "ToolButtonStyle", "ToolButtonTextBesideIcon"))
        self.log_toggle.setArrowType(qt_enum(QtCore.Qt, "ArrowType", "RightArrow"))
        self.log_toggle.toggled.connect(self._toggle_log)
        layout.addWidget(self.log_toggle)
        self.run_log = QtWidgets.QPlainTextEdit()
        self.run_log.setReadOnly(True)
        self.run_log.setMaximumBlockCount(3000)
        self.run_log.setFixedHeight(110)
        self.run_log.hide()
        layout.addWidget(self.run_log)
        return container

    def _button(self, text, callback):
        """Creates a standard button and binds one action.

        Args:
            text (str): Visible label.
            callback (callable): Action invoked by a click.

        Returns:
            QPushButton: Configured button.
        """
        button = QtWidgets.QPushButton(text)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        return button


    def _connect_fields(self):
        """Connects user edits to model-independent dirty notifications."""
        self.plan_name.textChanged.connect(self._edited)
        self.folders_page.changed.connect(self._edited)
        self.variables_table.cellChanged.connect(self._edited)
        self.compare_combo.currentIndexChanged.connect(self._edited)
        self.timestamp_tolerance.valueChanged.connect(self._edited)
        self.allow_empty_mirror.toggled.connect(self._edited)
        self.timeout_seconds.valueChanged.connect(self._edited)
        self.expand_log_while_syncing.toggled.connect(self._edited)
        self.generate_report.toggled.connect(self._edited)
        self.automation_start_mode.currentIndexChanged.connect(self._automation_mode_changed)
        self.automation_start_mode.currentIndexChanged.connect(self._edited)
        self.countdown_seconds.valueChanged.connect(self._edited)
        self.start_minimized.toggled.connect(self._edited)
        self.close_after_run.toggled.connect(self._edited)
        self.automation_mirror.toggled.connect(self._edited)

    def set_plans(self, summaries, selected_id=None):
        """Replaces sidebar summaries without sending selection notifications.

        Args:
            summaries (list[dict]): Plan IDs and display names.
            selected_id (str, optional): Plan ID to select.
        """
        prior_loading = self._loading
        self._loading = True
        self.plans_list.clear()
        for summary in summaries:
            item = QtWidgets.QListWidgetItem(str(summary.get("name") or "Untitled"))
            item.setData(self._data_role, str(summary.get("id", "")))
            self.plans_list.addItem(item)
            if str(summary.get("id", "")) == str(selected_id):
                self.plans_list.setCurrentItem(item)
        self._loading = prior_loading

    def set_plan(self, plan):
        """Loads a plan into stable controls without emitting edit signals.

        Args:
            plan (dict or None): Serializable plan, or no active selection.
        """
        self._loading = True
        self._plan = copy.deepcopy(plan) if plan else None
        self.set_unsaved_changes(False)
        values = self._plan or {}
        automation = values.get("automation", {})
        self.plan_name.setText(values.get("name", ""))
        self._set_combo(self.preview_mode_combo, values.get("preview_view", "table"))
        self._set_combo(self.compare_combo, values.get("compare", "metadata"))
        self.timestamp_tolerance.setValue(float(values.get("timestamp_tolerance", 0)))
        self.allow_empty_mirror.setChecked(bool(values.get("allow_empty_mirror", False)))
        execution = values.get("execution", {})
        self.timeout_seconds.setValue(int(execution.get("timeout_seconds", 0)))
        self.expand_log_while_syncing.setChecked(bool(execution.get("expand_log_while_syncing", False)))
        self.generate_report.setChecked(bool(execution.get("generate_report", False)))
        self._set_combo(self.automation_start_mode, automation.get("start_mode", "countdown"))
        self.countdown_seconds.setValue(int(automation.get("countdown_seconds", 10)))
        self.start_minimized.setChecked(bool(automation.get("start_minimized", False)))
        self.close_after_run.setChecked(bool(automation.get("close_after_run", False)))
        self.automation_mirror.setChecked(bool(
            automation.get("allow_deletions", automation.get("allow_mirror", False))
        ))
        self._refresh_automation_mode_controls()
        self.folders_page.set_pairs(values.get("pairs", []))
        self.variables_table.setRowCount(0)
        for name, definition in sorted(values.get("variables", {}).items(), key=lambda item: item[0].casefold()):
            row = self.variables_table.rowCount()
            self.variables_table.insertRow(row)
            token = str(name)
            if not token.startswith("{"):
                token = f"{{{token}}}"
            is_query = isinstance(definition, dict) and bool(definition.get("query"))
            value = definition.get("value", "") if isinstance(definition, dict) else definition
            self.variables_table.setItem(row, 0, QtWidgets.QTableWidgetItem(token))
            self.variables_table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))
            self._add_variable_query_controls(row, is_query=is_query)
            self.variables_table.setRowHeight(row, 32)
        self._loading = False
        self._refresh_enabled()

    def set_unsaved_changes(self, dirty):
        """Shows whether the active plan has edits that are not saved.

        Args:
            dirty (bool): Whether the active plan differs from saved data.
        """
        self._dirty = bool(dirty)
        self.unsaved_label.setVisible(
            not self._automatic_run_mode and self._dirty and self._plan is not None
        )

    def get_plan(self):
        """Returns a detached copy of the edited plan with pair order preserved.

        Returns:
            dict or None: Current serializable plan.
        """
        if self._plan is None:
            return None
        result = copy.deepcopy(self._plan)
        result["pairs"] = self.folders_page.get_pairs()
        result["variables"] = {}
        for row in range(self.variables_table.rowCount()):
            name_item = self.variables_table.item(row, 0)
            value_item = self.variables_table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            if name:
                value = value_item.text() if value_item else ""
                cell = self.variables_table.cellWidget(row, 2)
                query_check = cell.findChild(QtWidgets.QCheckBox) if cell else None
                if query_check and query_check.isChecked():
                    result["variables"][name] = {"value": value, "query": True}
                else:
                    result["variables"][name] = value
        result["name"] = self.plan_name.text().strip()
        result["preview_view"] = self.preview_mode_combo.currentData() or "table"
        result["compare"] = self.compare_combo.currentData()
        result["timestamp_tolerance"] = self.timestamp_tolerance.value()
        result["allow_empty_mirror"] = self.allow_empty_mirror.isChecked()
        result["execution"] = {
            "timeout_seconds": self.timeout_seconds.value(),
            "expand_log_while_syncing": self.expand_log_while_syncing.isChecked(),
            "generate_report": self.generate_report.isChecked(),
        }
        result["automation"] = {
            "start_mode": self.automation_start_mode.currentData(),
            "countdown_seconds": self.countdown_seconds.value(),
            "start_minimized": self.start_minimized.isChecked(),
            "allow_deletions": self.automation_mirror.isChecked(),
            "close_after_run": self.close_after_run.isChecked(),
        }
        return result

    def _automation_mode_changed(self, *unused):
        """Updates automation controls to match the selected startup behavior.

        Args:
            *unused (tuple): Combo-box signal arguments.
        """
        self._refresh_automation_mode_controls()

    def _refresh_automation_mode_controls(self):
        """Enables only the settings that apply to the selected start mode."""
        mode = self.automation_start_mode.currentData()
        is_countdown = mode == "countdown"
        requires_confirmation = mode == "confirm"
        self.countdown_label.setEnabled(is_countdown)
        self.countdown_seconds.setEnabled(is_countdown)
        self.start_minimized.setEnabled(not requires_confirmation)
        if requires_confirmation and self.start_minimized.isChecked():
            self.start_minimized.setChecked(False)
        hints = {
            "immediate": "The sync starts as soon as the automatic action opens.",
            "countdown": "The sync starts when the countdown ends. Cancel remains available while it waits.",
            "confirm": "A confirmation dialog appears before scanning or changing any files.",
        }
        self.automation_mode_hint.setText(hints.get(mode, ""))

    def _set_combo(self, combo, value):
        """Selects a serialized value using the combo's item data.

        Args:
            combo (QComboBox): Target input.
            value (str): Serialized value.
        """
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))

    def _select_plan(self, current, previous):
        """Notifies the controller when the selected backup plan changes.

        Args:
            current (QListWidgetItem or None): Newly selected row.
            previous (QListWidgetItem or None): Previous row.
        """
        if current and not self._loading:
            self.plan_selected.emit(str(current.data(self._data_role)))


    def _edited(self, *unused):
        """Emits one ordinary edit notification for controller-managed persistence.

        Args:
            *unused (tuple): Qt signal arguments.
        """
        if not self._loading and self._plan is not None:
            self.plan_edited.emit()


    def set_busy(self, busy):
        """Locks plan editing while leaving cancellation and logs available.

        Args:
            busy (bool): Whether a countdown or background operation is active.
        """
        self._busy = bool(busy)
        self._countdown_total = None
        self._refresh_enabled()
        self._update_conflict_buttons()
        self.cancel_button.setVisible(self._busy)
        self.cancel_button.setEnabled(self._busy)
        self.cancel_button.setText("Cancel")
        self._update_progress_panel_visibility()
        self._refresh_run_mode_visibility()

    def set_automatic_run_mode(self, enabled=True):
        """Shows the compact preview and progress layout for automatic actions.

        Args:
            enabled (bool): Whether the compact automatic-run layout is active.
        """
        self._automatic_run_mode = bool(enabled)
        if self._automatic_run_mode:
            self.settings_tabs.setCurrentWidget(self.preview_page)
            plan_name = (self._plan or {}).get("name", "Automatic Run")
            self.setWindowTitle(f"SyncEngine — {plan_name}")
        else:
            self.setWindowTitle("SyncEngine")
        self.menuBar().setVisible(not self._automatic_run_mode)
        self.sidebar.setVisible(not self._automatic_run_mode)
        self.plan_heading.setVisible(not self._automatic_run_mode)
        self.save_button.setVisible(not self._automatic_run_mode)
        self.settings_tabs.tabBar().setVisible(not self._automatic_run_mode)
        self._update_progress_panel_visibility()
        self._refresh_run_mode_visibility()

    def show_full_app(self):
        """Returns from the compact automatic-run presentation to the full editor."""
        self.set_automatic_run_mode(False)

    def _refresh_run_mode_visibility(self):
        """Shows only the controls relevant to the current run presentation."""
        compact = self._automatic_run_mode
        self.open_full_app_button.setVisible(compact)
        self.preview_button.setVisible(not compact)
        self.sync_button.setVisible(not compact)
        self.cancel_button.setVisible(self._busy)
        self.unsaved_label.setVisible(not compact and self._dirty and self._plan is not None)
        if hasattr(self, "log_panel"):
            self.log_panel.setVisible(not compact)
        self._update_footer_visibility()

    def _update_progress_panel_visibility(self, *unused):
        """Shows progress while work is active or while reviewing the Preview tab.

        Args:
            *unused (tuple): Optional tab-change signal arguments.
        """
        if not hasattr(self, "progress_panel"):
            return
        showing_preview = self.settings_tabs.currentWidget() is self.preview_page
        self.progress_panel.setVisible(
            self._busy or showing_preview or self._automatic_run_mode
        )

    def _refresh_enabled(self):
        """Updates widget availability for empty selection and running operations."""
        has_plan = self._plan is not None
        editable = has_plan and not self._busy
        self.sidebar.setEnabled(not self._busy)
        self.plan_heading.setEnabled(editable)
        self.folders_page.setEnabled(editable)
        self.exclusions_page.setEnabled(editable and bool(self._plan.get("pairs")))
        self.variables_page.setEnabled(editable)
        self.options_page.setEnabled(editable)
        self.automation_page.setEnabled(editable)
        self.folders_page.set_busy(self._busy)
        self.save_button.setEnabled(not self._busy)
        self.preview_button.setEnabled(editable)
        self.sync_button.setEnabled(editable)
        self.export_action_button.setEnabled(editable)
        for index, action in enumerate(self._file_menu_actions):
            action.setEnabled(not self._busy and (index < 3 or has_plan))
        self.register_action.setEnabled(not self._busy)
        self.environment_variables_action.setEnabled(not self._busy and has_plan)
        self.reports_action.setEnabled(not self._busy)

    def set_status(self, text, level="info"):
        """Displays routine feedback without opening a modal dialog.

        Args:
            text (str): Human-readable feedback.
            level (str, optional): Info, warning, error, or success severity.
        """
        colors = {"info": "#bcbcbc", "warning": "#d0b58a", "error": "#df9a9a", "success": "#b0c3ad"}
        self.status_label.setText(str(text))
        self.status_label.setToolTip(str(text))
        self.status_label.setStyleSheet(f"color: {colors.get(level, colors['info'])};")

    def clear_preview(self):
        """Clears stale preview rows after settings or plan selection changes."""
        self._preview_generation_active = False
        self._preview_scan_rows = {}
        self.preview_table.setRowCount(0)
        self.preview_tree.clear()
        self._preview_displayed = []
        self._preview_pairs = []
        self._preview_operations = []
        self._preview_completion_items = {}
        self._preview_execution_status = {}
        self._preview_progress_items = {"table": {}, "tree": {}}
        self.preview_summary.setText("Run Preview to review changes before syncing.")
        self.preview_note.setText("Items filtered by Include or Exclude rules appear as Skipped in the preview.")
        self.summary_label.clear()
        self._has_preview_summary = False
        self._update_footer_visibility()
        self.use_source_button.setEnabled(False)
        self.use_backup_button.setEnabled(False)

    def begin_preview_generation(self):
        """Shows a live scan table while the full preview is being assembled."""
        self._preview_generation_active = True
        self._preview_scan_rows = {}
        self.preview_table.setUpdatesEnabled(False)
        self.preview_table.setRowCount(0)
        self.preview_table.setUpdatesEnabled(True)
        self.preview_tree.clear()
        self._preview_displayed = []
        self._preview_pairs = []
        self._preview_operations = []
        self._preview_progress_items = {"table": {}, "tree": {}}
        self._preview_completion_items = {}
        self._preview_execution_status = {}
        self._preview_tree_items = {}
        self.preview_summary.setText("Generating preview…")
        self.summary_label.clear()
        self.preview_note.setText(
            "SyncEngine is scanning the folders. The table refreshes as items are discovered."
        )
        self._has_preview_summary = True
        self.settings_tabs.setCurrentWidget(self.preview_page)
        self.preview_stack.setCurrentIndex(0)
        self._update_footer_visibility()
        self._update_progress_panel_visibility()

    def update_preview_scan_item(self, data):
        """Refreshes one recently discovered entry in the temporary scan table.

        Args:
            data (dict): Pair, path, scan side, and optional filter status.
        """
        if not self._preview_generation_active:
            return
        pair_id = str(data.get("pair_id", ""))
        relative_path = str(data.get("relative_path", ""))
        if not relative_path:
            return
        key = (pair_id, relative_path.casefold())
        row = self._preview_scan_rows.get(key)
        kind = "skipped" if data.get("kind") == "skipped" else "scanning"
        if row is None:
            if self.preview_table.rowCount() >= self._preview_scan_row_limit:
                self.preview_table.removeRow(0)
                self._preview_scan_rows = {
                    row_key: old_row - 1
                    for row_key, old_row in self._preview_scan_rows.items()
                    if old_row > 0
                }
            row = self.preview_table.rowCount()
            self.preview_table.insertRow(row)
            self._preview_scan_rows[key] = row
        elif self.preview_table.item(row, 0) is not None:
            current_kind = self.preview_table.item(row, 0).data(self._data_role + 1)
            if current_kind == "skipped" and kind == "scanning":
                return
        side = data.get("scan_side", "folder")
        values = (
            "Skipped" if kind == "skipped" else "Scanning",
            data.get("pair_name", "Folder Pair"),
            relative_path,
            data.get("reason") or f"Scanning {side} contents…",
            "",
            "",
        )
        background, foreground = _PREVIEW_ROW_COLORS.get(
            "skipped" if kind == "skipped" else "unchanged", ("#2c3033", "#c5c9cc")
        )
        self.preview_table.setUpdatesEnabled(False)
        for column, value in enumerate(values):
            item = self.preview_table.item(row, column)
            if item is None:
                item = QtWidgets.QTableWidgetItem()
                item.setFlags(item.flags() & ~qt_enum(
                    QtCore.Qt, "ItemFlag", "ItemIsEditable"
                ))
                self.preview_table.setItem(row, column, item)
            item.setText(str(value))
            item.setToolTip(str(value))
            item.setData(self._data_role + 1, kind)
            item.setBackground(QtGui.QColor(background))
            item.setForeground(QtGui.QColor(foreground))
        self.preview_table.setUpdatesEnabled(True)
        if self.preview_table.rowCount() >= self._preview_scan_row_limit:
            self.preview_note.setText(
                f"Generating preview. Showing the most recent {self._preview_scan_row_limit:,} discovered items; "
                "the completed preview will include the full results."
            )
        self._fill_preview_table()

    def _update_conflict_buttons(self):
        """Enables conflict choices only when a conflict row is selected."""
        item, is_tree_item, status_column = self._current_preview_selection()
        is_conflict = (
            item is not None
            and self._preview_item_data(item, is_tree_item, status_column, self._data_role + 1) == "conflict"
        )
        self.use_source_button.setEnabled(is_conflict and not self._busy)
        self.use_backup_button.setEnabled(is_conflict and not self._busy)

    def _resolve_selected_conflict(self, direction):
        """Emits a resolution for the selected conflict row."""
        item, is_tree_item, status_column = self._current_preview_selection()
        if item is None or self._preview_item_data(
                item, is_tree_item, status_column, self._data_role + 1) != "conflict":
            return
        self.conflict_resolution_requested.emit(
            str(self._preview_item_data(item, is_tree_item, status_column, self._data_role + 2)),
            f"{self._preview_item_data(item, is_tree_item, status_column, self._data_role + 3)}\x1f{direction}",
        )

    def _current_preview_selection(self):
        """Returns the selected row and its action column for the active mode.

        Returns:
            tuple: Selected preview item, tree-item flag, and action column.
        """
        if self.preview_mode_combo.currentData() == "tree":
            return self.preview_tree.currentItem(), True, 1
        row = self.preview_table.currentRow()
        return self.preview_table.item(row, 0) if row >= 0 else None, False, 0

    @staticmethod
    def _preview_item_data(item, is_tree_item, column, role):
        """Reads role data from either kind of preview row.

        Args:
            item (QTableWidgetItem or QTreeWidgetItem): Selected preview item.
            is_tree_item (bool): Whether the item belongs to the hierarchy view.
            column (int): Column storing the operation metadata.
            role (int): Qt item data role.

        Returns:
            object: Data stored for the selected operation.
        """
        return item.data(column, role) if is_tree_item else item.data(role)

    @staticmethod
    def _preview_values(operation):
        """Builds consistent action, reason, and size text for preview rows.

        Args:
            operation (dict): Preview operation or display-only row.
        Returns:
            tuple: Action label, reason, and formatted size.
        """
        kind = operation.get("kind", operation.get("action", ""))
        target_label = "source" if operation.get("target_root") == "source" else "backup"
        change_label = _PREVIEW_LABELS.get(kind, str(kind).title())
        if kind in ("copy", "update"):
            change_label = f"{change_label} to {target_label.title()}"
        elif kind == "delete":
            change_label = f"Remove {target_label.title()} File"
        elif kind == "mkdir":
            change_label = f"New Folder on {target_label.title()}"
        elif kind == "rmdir":
            change_label = f"Remove Folder from {target_label.title()}"
        reason = operation.get("reason", _PREVIEW_REASONS.get(kind, ""))
        if kind == "skipped":
            side = operation.get("side", "source")
            if not reason:
                reason = "Excluded by rule: matched an Exclude filter."
            elif not reason.startswith("Not matched by any Include rule") and not reason.startswith(
                    "Not inside a folder selected"):
                reason = f"Excluded by rule: {reason}"
            reason = f"{reason} ({side})"
        size = (
            format_bytes(operation.get("size", 0))
            if kind not in ("mkdir", "rmdir", "skipped") and operation.get("entry_type") != "folder"
            else "—"
        )
        return change_label, reason, size

    def _fill_preview_tree(self, displayed, pairs):
        """Builds a target-root hierarchy colored by each planned action.

        Args:
            displayed (list[dict]): Operations, unchanged rows, and exclusions.
            pairs (list[dict]): Resolved folder-pair definitions from the preview.
        """
        self.preview_tree.setUpdatesEnabled(False)
        self.preview_tree.clear()
        pair_by_id = {str(pair.get("id", "")): pair for pair in pairs}
        pair_names = {
            pair_id: pair.get("name") or f"Pair {index + 1}"
            for index, (pair_id, pair) in enumerate(pair_by_id.items())
        }
        root_items = {}
        path_items = {}
        self._preview_tree_items = {}
        for operation in displayed:
            pair_id = str(operation.get("pair_id", ""))
            pair = pair_by_id.get(pair_id, {})
            kind = operation.get("kind", operation.get("action", ""))
            target_root = operation.get("target_root")
            if target_root not in ("source", "destination"):
                target_root = "source" if kind == "skipped" and operation.get("side") == "source" else "destination"
            root_key = (pair_id, target_root)
            root = root_items.get(root_key)
            if root is None:
                pair_name = pair_names.get(pair_id, operation.get("pair_name") or "Folder Pair")
                side_label = "Source" if target_root == "source" else "Backup"
                target_path = pair.get(target_root) or "(path not set)"
                root = QtWidgets.QTreeWidgetItem([
                    f"{pair_name} — {self._display_target_path(target_path)}", "", "", "", "",
                ])
                root.setIcon(0, padded_icon("icons/menu_folder.svg"))
                root.setToolTip(
                    0, f"{side_label} folder: {target_path}\nFolder pair: {pair_name}"
                )
                self.preview_tree.addTopLevelItem(root)
                root_items[root_key] = root

            relative_path = str(operation.get("relative_path", "")).replace("\\", "/").strip("/")
            if not relative_path:
                target_item = root
                prefix_parts = []
            else:
                components = [part for part in relative_path.split("/") if part]
                parent = root
                prefix_parts = []
                target_item = None
                for index, component in enumerate(components):
                    prefix_parts.append(component)
                    prefix = "/".join(prefix_parts).casefold()
                    is_directory = (
                        index < len(components) - 1
                        or kind in ("mkdir", "rmdir")
                        or operation.get("entry_type") == "folder"
                    )
                    node_key = (root_key, prefix)
                    target_item = path_items.get(node_key)
                    if target_item is None:
                        target_item = QtWidgets.QTreeWidgetItem([component, "", "", "", ""])
                        parent.addChild(target_item)
                        path_items[node_key] = target_item
                    parent = target_item
                    target_item.setData(0, self._data_role + 4, "folder" if is_directory else "file")
                    target_item.setIcon(
                        0,
                        padded_icon(
                            "icons/menu_folder.svg" if is_directory else "icons/menu_file.svg"
                        ),
                    )

            change_label, reason, size = self._preview_values(operation)
            target_item.setText(1, change_label)
            target_item.setText(2, reason)
            target_item.setText(3, size)
            for column, value in enumerate((target_item.text(0), change_label, reason, size, "")):
                target_item.setToolTip(column, str(value))
                background, foreground = _PREVIEW_ROW_COLORS.get(kind, ("#2c3033", "#c5c9cc"))
                target_item.setBackground(column, QtGui.QColor(background))
                target_item.setForeground(column, QtGui.QColor(foreground))
            operation_key = (
                pair_id, target_root, relative_path.casefold().replace("\\", "/")
            )
            self._preview_tree_items[operation_key] = target_item
            if kind == "conflict":
                target_item.setData(1, self._data_role + 1, "conflict")
                target_item.setData(1, self._data_role + 2, operation.get("pair_id", ""))
                target_item.setData(1, self._data_role + 3, operation.get("relative_path", ""))
        self.preview_tree.expandToDepth(1)
        self.preview_tree.setUpdatesEnabled(True)

    def _display_target_path(self, path):
        """Shortens a target path according to the saved preview preference."""
        path = str(path or "(path not set)")
        if self.preview_show_full_target_path:
            return path
        components = [part for part in re.split(r"[\\/]+", path.rstrip("\\/")) if part]
        if not components:
            return path
        depth = max(1, int(self.preview_target_path_depth))
        tail = "/".join(components[-depth:])
        return f".../{tail}" if len(components) > depth else tail

    def set_preview_preferences(self, show_full_target_path=False, path_depth=2):
        """Applies display preferences for the table and folder-tree preview."""
        self.preview_show_full_target_path = bool(show_full_target_path)
        try:
            self.preview_target_path_depth = max(1, min(99, int(path_depth)))
        except (TypeError, ValueError):
            self.preview_target_path_depth = 2
        if self._preview_displayed:
            progress_role = self._data_role
            progress_by_path = {}
            for path_key, items in self._preview_progress_items["tree"].items():
                values = [item.data(4, progress_role) for item in items]
                values = [value for value in values if value is not None]
                if values:
                    progress_by_path[path_key] = max(values)
            self._fill_preview_tree(self._preview_displayed, self._preview_pairs)
            self._fill_tree_progress_items(self._preview_operations, self._preview_pairs)
            for path_key, value in progress_by_path.items():
                for item in self._preview_progress_items["tree"].get(path_key, []):
                    item.setData(4, progress_role, value)

    def set_preview(self, preview):
        """Displays every planned, unchanged, preserved, and excluded item.

        Args:
            preview (dict): Operations, summary counts, warnings, and optional plan.
        """
        operations = preview.get("operations", [])
        unchanged = preview.get("unchanged", [])
        skipped = preview.get("skipped", [])
        summary = preview.get("summary", {})
        warnings = preview.get("warnings", [])
        if not operations and not unchanged and not skipped and not summary and not warnings:
            self.clear_preview()
            return
        removed = summary.get("delete", 0) + summary.get("rmdir", 0)
        label = (
            f"{summary.get('copy', 0):,} new · {summary.get('update', 0):,} changed · "
            f"{removed:,} removed · {summary.get('unchanged', 0):,} unchanged · "
            f"{summary.get('conflict', 0):,} conflicts · "
            f"{summary.get('skipped', len(skipped)):,} skipped · "
            f"{format_bytes(summary.get('bytes', 0))} to copy"
        )
        self.preview_summary.setText(label)
        self.summary_label.setText(
            f"{summary.get('copy', 0) + summary.get('update', 0):,} copies · "
            f"{removed:,} removals · {summary.get('skipped', len(skipped)):,} skipped · "
            f"{format_bytes(summary.get('bytes', 0))}"
        )
        self._has_preview_summary = True
        self._preview_generation_active = False
        self._preview_scan_rows = {}
        self._update_footer_visibility()
        displayed = list(operations) + list(unchanged) + list(skipped)
        self._preview_displayed = displayed
        self._preview_operations = operations
        self.preview_table.setUpdatesEnabled(False)
        self.preview_table.setRowCount(len(displayed))
        self._preview_progress_items = {"table": {}, "tree": {}}
        self._preview_completion_items = {}
        self._preview_execution_status = {}
        self._preview_tree_items = {}
        pairs = preview.get("plan", {}).get("pairs") or self.folders_page.get_pairs()
        pair_names = {str(pair.get("id", "")): pair.get("name") or f"Pair {index + 1}"
                      for index, pair in enumerate(pairs)}
        for row, operation in enumerate(displayed):
            kind = operation.get("kind", operation.get("action", ""))
            change_label, reason, size = self._preview_values(operation)
            values = (
                change_label,
                operation.get("pair_name") or pair_names.get(operation.get("pair_id"), ""),
                operation.get("relative_path", ""),
                reason,
                size,
                "",
            )
            background, foreground = _PREVIEW_ROW_COLORS.get(kind, ("#2c3033", "#c5c9cc"))
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                item.setBackground(QtGui.QColor(background))
                item.setForeground(QtGui.QColor(foreground))
                if kind == "conflict" and column == 0:
                    item.setData(self._data_role + 1, "conflict")
                    item.setData(self._data_role + 2, operation.get("pair_id", ""))
                    item.setData(self._data_role + 3, operation.get("relative_path", ""))
                self.preview_table.setItem(row, column, item)
            progress_item = self.preview_table.item(row, 5)
            if kind in ("copy", "update", "delete", "mkdir", "rmdir"):
                for operation_path in (operation.get("source"), operation.get("destination")):
                    self._register_preview_progress_item("table", operation_path, progress_item)
        self.preview_table.setUpdatesEnabled(True)
        self._preview_pairs = pairs
        self._fill_preview_tree(displayed, pairs)
        self._fill_tree_progress_items(operations, pairs)
        self._resize_preview_columns_to_contents(self.preview_table, compact_columns=(4,))
        self._resize_preview_columns_to_contents(self.preview_tree, compact_columns=(3,))
        self._fill_preview_table()
        self._fill_preview_tree_columns()
        notes = []
        if skipped:
            notes.append(
                f"{len(skipped):,} items were filtered by Include or Exclude rules and are listed as Skipped. "
                "Folders rejected by Exclude rules are not scanned for their contents."
            )
        if summary.get("conflict"):
            notes.insert(0, f"{summary['conflict']:,} conflict(s) need a choice before this sync can run.")
        if warnings:
            notes.append("\n".join(str(warning) for warning in warnings[:5]))
        self.preview_note.setText(
            "\n".join(notes)
            or "Every scanned item is listed with its planned outcome and reason."
        )
        self.use_source_button.setEnabled(False)
        self.use_backup_button.setEnabled(False)
        self.settings_tabs.setCurrentWidget(self.preview_page)
        self.preview_stack.setCurrentIndex(self.preview_mode_combo.currentIndex())
        self._update_progress_panel_visibility()
        self._update_conflict_buttons()

    def _resize_preview_columns_to_contents(self, widget, compact_columns=()):
        """Measures preview data before fitting every column to the viewport.

        Args:
            widget (QTableWidget or QTreeWidget): Preview presentation to measure.
            compact_columns (tuple): Columns that should remain content-sized.
        """
        get_header = getattr(widget, "horizontalHeader", None)
        header = get_header() if get_header else widget.header()
        set_precision = getattr(header, "setResizeContentsPrecision", None)
        if set_precision:
            set_precision(1000)
        was_blocked = header.blockSignals(True)
        try:
            for column in range(widget.columnCount()):
                widget.resizeColumnToContents(column)
        finally:
            header.blockSignals(was_blocked)
        for column in compact_columns:
            widget.setColumnWidth(column, max(48, widget.columnWidth(column)))

    def _fill_tree_progress_items(self, operations, pairs):
        """Maps operation paths onto visible rows in the hierarchy preview."""
        self._preview_progress_items["tree"] = {}
        self._preview_completion_items = {}
        pairs_by_id = {str(pair.get("id", "")): pair for pair in pairs}
        for row, operation in enumerate(operations):
            kind = operation.get("kind", operation.get("action", ""))
            if kind not in ("copy", "update", "delete", "mkdir", "rmdir"):
                continue
            pair = pairs_by_id.get(str(operation.get("pair_id", "")), {})
            target_root = operation.get("target_root", "destination")
            if target_root not in ("source", "destination"):
                target_root = "destination"
            relative_path = str(operation.get("relative_path", "")).replace("\\", "/").strip("/")
            tree_item = self._preview_tree_items.get((
                str(operation.get("pair_id", "")), target_root, relative_path.casefold()
            ))
            if tree_item is None:
                continue
            preview_key = (
                str(operation.get("pair_id", "")), target_root, relative_path.casefold()
            )
            destination = operation.get("destination")
            if destination:
                destination_key = ntpath.normpath(str(destination)).casefold()
                completion_items = self._preview_completion_items.setdefault(destination_key, [])
                completion_items.append({
                    "kind": kind, "row": row, "tree_item": tree_item, "preview_key": preview_key,
                })
            tree_item.setText(4, "")
            tree_item.setData(4, self._data_role, None)
            completed_label = self._preview_execution_status.get(preview_key)
            if completed_label:
                self._set_preview_item_completed(row, tree_item, completed_label)
            self._register_preview_progress_item("tree", operation.get("source"), tree_item)
            self._register_preview_progress_item("tree", operation.get("destination"), tree_item)
            target_base = pair.get(target_root, "")
            if relative_path:
                target_path = ntpath.join(target_base, relative_path)
            else:
                target_path = target_base
            self._register_preview_progress_item("tree", target_path, tree_item)

    def _register_preview_progress_item(self, mode, path, item):
        """Associates a source or target path with its preview progress cell."""
        if not path or item is None:
            return
        key = ntpath.normpath(str(path)).casefold()
        items = self._preview_progress_items[mode].setdefault(key, [])
        if item not in items:
            items.append(item)

    def _update_preview_item_progress(self, data):
        """Updates per-operation bars in both preview presentations."""
        phase = data.get("phase", data.get("stage", ""))
        if phase not in ("copy", "delete"):
            return
        current_path = data.get("current_path")
        if not current_path:
            return
        total = data.get("current_file_total", data.get("file_total", 0))
        completed = data.get("current_file_bytes", data.get("file_completed", 0))
        value = 1000 if not total else max(0, min(1000, round(completed / total * 1000)))
        key = ntpath.normpath(str(current_path)).casefold()
        for mode, mapping in self._preview_progress_items.items():
            for item in mapping.get(key, []):
                if mode == "table":
                    item.setData(self._data_role, value)
                else:
                    item.setData(4, self._data_role, value)
            if mode == "table":
                self.preview_table.viewport().update()
            else:
                self.preview_tree.viewport().update()

    def _mark_preview_operation_complete(self, data):
        """Changes a preview row from its planned action to its completed action."""
        phase = data.get("phase", data.get("stage", ""))
        message = str(data.get("message", ""))
        current_path = data.get("current_path")
        if phase == "copy":
            match = re.match(r"^Completed (copy|update|mkdir):", message)
            if match is None:
                return
            completed_kind = match.group(1)
        elif phase == "delete" and message.startswith("Removed "):
            completed_kind = None
        else:
            return
        if not current_path:
            return
        path_key = ntpath.normpath(str(current_path)).casefold()
        for entry in self._preview_completion_items.get(path_key, []):
            kind = entry["kind"]
            if phase == "copy" and kind != completed_kind:
                continue
            if phase == "delete" and kind not in ("delete", "rmdir"):
                continue
            label = _EXECUTED_PREVIEW_LABELS[kind]
            self._preview_execution_status[entry["preview_key"]] = label
            self._set_preview_item_completed(entry["row"], entry["tree_item"], label)
        self.preview_table.viewport().update()
        self.preview_tree.viewport().update()

    def _set_preview_item_completed(self, row, tree_item, label):
        """Shows the executed result and a completed per-item bar in both views."""
        background = QtGui.QColor("#2a3a33")
        foreground = QtGui.QColor("#d4e7dc")
        status_item = self.preview_table.item(row, 0)
        progress_item = self.preview_table.item(row, 5)
        if status_item is not None:
            status_item.setText(label)
            status_item.setToolTip(f"{label} during this sync.")
            status_item.setBackground(background)
            status_item.setForeground(foreground)
        if progress_item is not None:
            progress_item.setData(self._data_role, 1000)
        if tree_item is not None:
            tree_item.setText(1, label)
            tree_item.setToolTip(1, f"{label} during this sync.")
            tree_item.setBackground(1, background)
            tree_item.setForeground(1, foreground)
            tree_item.setData(4, self._data_role, 1000)

    def update_progress(self, data):
        """Updates overall progress, file activity, and transfer estimates.

        Args:
            data (dict): Stage, counts, byte totals, and optional timing metrics.
        """
        phase = data.get("phase", data.get("stage", ""))
        self._update_preview_item_progress(data)
        self._mark_preview_operation_complete(data)
        titles = {
            "scan": "Scanning folders", "compare": "Comparing files", "copy": "Copying files",
            "delete": "Removing destination extras", "finished": "Finished", "cancelled": "Cancelled",
            "failed": "Stopped", "timed_out": "Timed out", "succeeded": "Finished",
            "countdown": "Automatic sync countdown",
        }
        self.progress_title.setText(titles.get(phase, data.get("status") or "Working"))
        scanning = phase in ("scan", "compare") and "percent" not in data
        self.progress_bar.setRange(0, 0 if scanning else 1000)
        completed = data.get("completed_operations", data.get("completed_files", 0))
        total = data.get("total_operations", data.get("total_files", 0))
        completed_bytes = data.get("completed_bytes", data.get("transferred_bytes", 0))
        total_bytes = data.get("total_bytes", 0)
        percent = data.get("percent")
        if percent is None:
            if total_bytes and total:
                percent = 90 * completed_bytes / total_bytes + 10 * completed / total
            elif total_bytes:
                percent = completed_bytes / total_bytes * 100
            elif total:
                percent = completed / total * 100
            else:
                percent = 100 if phase == "finished" else 0
            if phase in ("copy", "delete") and total and completed < total:
                percent = min(99, percent)
        if not scanning:
            self.progress_bar.setFormat("%p%")
            self.progress_bar.setValue(max(0, min(1000, round(float(percent) * 10))))
        if scanning:
            scanned_entries = data.get("scanned_entries", data.get("scanned_files", 0))
            self.operation_count.setText(f"{int(scanned_entries):,} items scanned")
        else:
            self.operation_count.setText(f"{int(completed):,} / {int(total):,} changes")
        path = data.get("current_path")
        if path is not None:
            self.current_path.set_path(path or data.get("message", ""))
        self.bytes_label.setText(f"{format_bytes(completed_bytes)} / {format_bytes(total_bytes)}")
        speed = data.get("speed_bytes_per_second")
        self.speed_label.setText(f"{format_bytes(speed)} /s" if speed else "— /s")
        self.eta_label.setText(f"Remaining {format_duration(data.get('eta_seconds'))}")
        self.elapsed_label.setText(f"Elapsed {format_duration(data.get('elapsed_seconds', 0))}")
        file_total = data.get("current_file_total", data.get("file_total", 0))
        file_completed = data.get("current_file_bytes", data.get("file_completed", 0))
        if file_total:
            self.file_progress.setRange(0, 1000)
            self.file_progress.setValue(max(0, min(1000, round(file_completed / file_total * 1000))))
        else:
            self.file_progress.setRange(0, 1000)
            self.file_progress.setValue(1000 if phase == "finished" else 0)
        message = data.get("message")
        if message:
            level = {"succeeded": "success", "failed": "error", "cancelled": "warning"}.get(phase, "info")
            self.set_status(message, level)

    def set_countdown(self, seconds, preview_loading=False):
        """Displays a cancellable countdown with an accurate progress indication.

        Args:
            seconds (int): Remaining time before an automatic sync.
            preview_loading (bool): Whether the preview scan is still active.
        """
        remaining = max(0, int(seconds))
        if self._countdown_total is None:
            self._countdown_total = max(1, remaining)
        duration = self._countdown_total
        self.cancel_button.setText("Cancel automatic sync")
        if preview_loading:
            self.set_status(
                f"Generating preview · {self.operation_count.text()} · Sync starts in {remaining:,}s"
            )
        else:
            self.progress_title.setText(f"Automatic sync in {remaining:,}s")
            self.progress_bar.setRange(0, 1000)
            self.progress_bar.setValue(
                max(0, min(1000, round((duration - remaining) / duration * 1000)))
            )
            self.progress_bar.setFormat(f"Starts in {remaining:,}s")
            self.current_path.set_path("Cancel to stop this automatic action before it starts.")
            self.set_status("Automatic action is waiting. Cancel is available before and during the sync.")

    def reset_progress(self):
        """Resets transfer metrics before a new run begins."""
        self.progress_bar.setFormat("%p%")
        self.update_progress({"phase": "ready", "status": "Ready", "current_path": ""})

    def append_log(self, text):
        """Appends a bounded log entry and keeps the latest output visible.

        Args:
            text (str): Log line or multiline output.
        """
        self.run_log.appendPlainText(str(text))
        scrollbar = self.run_log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def show_environment_variables(self, values):
        """Shows searchable built-in tokens, values, descriptions, and row actions."""
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("SyncEngine Environment Variables")
        dialog.resize(720, 520)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            "Right-click a row to copy its token or value, or insert a selected token into the last-focused "
            "source or destination field. "
            "System environment variables are also available in paths and queries."
        ))
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText("Filter variables…")
        layout.addWidget(search)
        table = QtWidgets.QTableWidget(len(values), 3)
        table.setHorizontalHeaderLabels(["Token", "Current value", "Description"])
        table.verticalHeader().hide()
        table.setEditTriggers(qt_enum(QtWidgets.QAbstractItemView, "EditTrigger", "NoEditTriggers"))
        table.setSelectionBehavior(qt_enum(QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"))
        table.setSelectionMode(qt_enum(QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"))
        table.setContextMenuPolicy(qt_enum(
            QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"
        ))
        table.customContextMenuRequested.connect(
            lambda position: self._show_environment_context_menu(table, position)
        )
        variable_header = table.horizontalHeader()
        variable_header.setSectionResizeMode(
            qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Interactive")
        )
        variable_header.setStretchLastSection(True)
        table.setColumnWidth(0, 180)
        table.setColumnWidth(1, 250)
        for row, (token, value) in enumerate(sorted(values.items())):
            table.setItem(row, 0, QtWidgets.QTableWidgetItem(token))
            table.setItem(row, 1, QtWidgets.QTableWidgetItem(str(value)))
            description = _ENVIRONMENT_VARIABLE_DESCRIPTIONS.get(
                token, "System environment variable or SyncEngine context token."
            )
            table.setItem(row, 2, QtWidgets.QTableWidgetItem(description))
        layout.addWidget(table, 1)

        def filter_rows(text):
            needle = str(text).casefold()
            for row in range(table.rowCount()):
                token_item = table.item(row, 0)
                value_item = table.item(row, 1)
                description_item = table.item(row, 2)
                match = any(
                    needle in item.text().casefold()
                    for item in (token_item, value_item, description_item)
                    if item is not None
                )
                table.setRowHidden(row, not match)

        search.textChanged.connect(filter_rows)

        def selected_token():
            row = table.currentRow()
            item = table.item(row, 0) if row >= 0 else None
            return item.text() if item else ""

        buttons = QtWidgets.QHBoxLayout()
        insert_button = QtWidgets.QPushButton("Insert Into Path")
        close_button = QtWidgets.QPushButton("Close")

        def insert_token():
            token = selected_token()
            if token:
                self.folders_page.insert_variable_token(token)
                self.set_status(f"Inserted {token} into the active folder path.")
                dialog.accept()

        insert_button.clicked.connect(insert_token)
        close_button.clicked.connect(dialog.reject)
        table.cellDoubleClicked.connect(lambda row, column: insert_token())
        buttons.addWidget(insert_button)
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        dialog_runner = getattr(dialog, "exec", None) or dialog.exec_
        dialog_runner()

    def _show_environment_context_menu(self, table, position):
        """Shows row-level copy actions for a built-in variable."""
        row = table.rowAt(position.y())
        if row < 0:
            return
        table.selectRow(row)
        token_item = table.item(row, 0)
        value_item = table.item(row, 1)
        if token_item is None or value_item is None:
            return
        menu = QtWidgets.QMenu(table)
        copy_token = menu.addAction(padded_icon("icons/rigger_action_copy_grayscale.svg"), "Copy Token")
        copy_value = menu.addAction(padded_icon("icons/rigger_action_copy_grayscale.svg"), "Copy Value")
        runner = getattr(menu, "exec", None) or menu.exec_
        chosen = runner(table.viewport().mapToGlobal(position))
        if chosen == copy_token:
            QtWidgets.QApplication.clipboard().setText(token_item.text())
            self.set_status(f"Copied {token_item.text()}.")
        elif chosen == copy_value:
            QtWidgets.QApplication.clipboard().setText(value_item.text())
            self.set_status(f"Copied the value for {token_item.text()}.")

    def show_path_feedback(self, title, lines):
        """Shows resolved path details in a read-only output dialog.

        Args:
            title (str): Direction-specific dialog title.
            lines (list[str]): Resolved path and existence details.
        """
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(700, 340)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        output = QtWidgets.QPlainTextEdit()
        output.setReadOnly(True)
        output.setPlainText("\n".join(str(line) for line in lines))
        output.setLineWrapMode(qt_enum(QtWidgets.QPlainTextEdit, "LineWrapMode", "NoWrap"))
        output.setFont(QtGui.QFontDatabase.systemFont(
            qt_enum(QtGui.QFontDatabase, "SystemFont", "FixedFont")
        ))
        layout.addWidget(output, 1)
        close_button = QtWidgets.QPushButton("Close")
        close_button.clicked.connect(dialog.accept)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        dialog_runner = getattr(dialog, "exec", None) or dialog.exec_
        dialog_runner()

    def _toggle_log(self, expanded):
        """Shows detailed output without rebuilding active controls.

        Args:
            expanded (bool): Whether the log should be visible.
        """
        self.run_log.setVisible(expanded)
        direction = "DownArrow" if expanded else "RightArrow"
        self.log_toggle.setArrowType(qt_enum(QtCore.Qt, "ArrowType", direction))

    def set_run_log_expanded(self, expanded):
        """Expands or collapses the run log without deleting its current contents."""
        self.log_toggle.setChecked(bool(expanded))

    def confirm(self, text, title="Confirm"):
        """Requests an explicit answer for a destructive or blocking decision.

        Args:
            text (str): Decision details.
            title (str, optional): Dialog title.

        Returns:
            bool: Whether the user chose to continue.
        """
        yes = qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
        no = qt_enum(QtWidgets.QMessageBox, "StandardButton", "No")
        return QtWidgets.QMessageBox.question(self, title, str(text), yes | no, no) == yes

    def confirm_automatic_run(self, text, callback, title="Confirm Automatic Sync"):
        """Shows a non-modal decision prompt while leaving the preview usable.

        Args:
            text (str): Automatic-run confirmation details.
            callback (callable): Receives True when the user approves the run.
            title (str): Dialog title.
        """
        yes = qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
        no = qt_enum(QtWidgets.QMessageBox, "StandardButton", "No")
        dialog = QtWidgets.QMessageBox(self)
        dialog.setWindowTitle(title)
        dialog.setText(str(text))
        dialog.setStandardButtons(yes | no)
        dialog.setDefaultButton(no)
        dialog.setEscapeButton(no)
        dialog.setWindowModality(qt_enum(QtCore.Qt, "WindowModality", "NonModal"))
        self._automatic_confirmation_dialog = dialog

        def finish_confirmation(unused_result):
            """Reports a single decision and releases the dialog reference."""
            if self._automatic_confirmation_dialog is not dialog:
                return
            approved = dialog.clickedButton() is dialog.button(yes)
            self._automatic_confirmation_dialog = None
            callback(approved)

        dialog.finished.connect(finish_confirmation)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_error(self, text):
        """Shows a blocking error while preserving the detailed status message.

        Args:
            text (str): Error description.
        """
        self.set_status(text, "error")
        QtWidgets.QMessageBox.critical(self, "SyncEngine", str(text))

    def _show_about(self):
        """Displays a short explanation of the app and its backup modes."""
        QtWidgets.QMessageBox.about(
            self, "About SyncEngine",
            f"SyncEngine {VERSION}\n\nFile sync plans with multiple folder pairs "
            "and Include or Exclude rules, "
            "change previews, and scheduled actions.\n\n"
            "Add only preserves existing files. Update replaces changed files. Mirror removes destination extras. "
            "Two-way sync merges changes from both folders and asks before resolving conflicting edits."
        )

    def closeEvent(self, event):
        """Lets the controller save edits or defer closing until cancellation ends.

        Args:
            event (QCloseEvent): Incoming close request.
        """
        self.closing.emit(event)
        if event.isAccepted():
            super().closeEvent(event)
