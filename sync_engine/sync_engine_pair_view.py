"""Independent editor for ordered source and destination folder pairs."""

import copy
import json
import os
import uuid

from sync_engine_model import new_pair, new_plan, normalize_exclusions, validate_plan
from sync_engine_qt import QtCore, QtWidgets, qt_enum
from sync_engine_style import padded_icon
from sync_engine_widgets import FolderPathEdit


class _ResizableExclusionsTable(QtWidgets.QTableWidget):
    """Editable-width exclusion table that reports changes to its viewport."""

    viewport_resized = QtCore.Signal()

    def resizeEvent(self, event):
        """Notify the owner after the table receives its new geometry."""
        super().resizeEvent(event)
        self.viewport_resized.emit()


class FolderPairEditor(QtWidgets.QWidget):
    """Edits folder-pair data with stable controls and persistent identities."""

    changed = QtCore.Signal()
    path_feedback_requested = QtCore.Signal(str, str)

    def __init__(self, parent=None):
        """Creates a reusable editor without an external model dependency.

        Args:
            parent (QWidget, optional): Owning widget.
        """
        super().__init__(parent)
        self._plan = {"pairs": []}
        self._loading = False
        self._busy = False
        self._current_pair_id = None
        self._active_path_field = None
        self._data_role = qt_enum(QtCore.Qt, "ItemDataRole", "UserRole")
        self._build()
        self._connect_fields()
        self.set_pairs([])

    def _connect_fields(self):
        """Binds pair and exclusion edits to ordinary change notifications."""
        for field in (self.pair_name, self.source_path, self.destination_path):
            field.textChanged.connect(self._pair_edited)
        self.mode_combo.currentIndexChanged.connect(self._pair_edited)
        self.exclusion_pair_combo.currentIndexChanged.connect(self._select_exclusion_pair)
        self.exclusions_table.itemSelectionChanged.connect(self._refresh_exclusion_buttons)
        self.source_path.focus_gained.connect(self._remember_active_path)
        self.destination_path.focus_gained.connect(self._remember_active_path)

    def _remember_active_path(self, field):
        """Stores the path editor most recently focused by the user."""
        self._active_path_field = field

    def insert_variable_token(self, token):
        """Inserts a selected environment token into the last-focused path editor."""
        field = self._active_path_field or self.source_path
        field.insert(str(token))
        field.setFocus()

    def set_pairs(self, pairs):
        """Loads detached pair data without emitting edit notifications.

        Args:
            pairs (list[dict]): Ordered serialized folder pairs.
        """
        self._loading = True
        self._plan = {"pairs": copy.deepcopy(pairs)}
        for pair in self._plan["pairs"]:
            pair["exclusions"] = normalize_exclusions(pair)
            pair.pop("ignore_folders", None)
            pair.pop("ignore_match", None)
            pair.pop("ignore_files", None)
        self._current_pair_id = None
        self._active_path_field = None
        self.pairs_list.clear()
        for pair in self._plan["pairs"]:
            self._insert_pair_item(pair)
        if self.pairs_list.count():
            self.pairs_list.setCurrentRow(0)
            self._current_pair_id = self.pairs_list.item(0).data(self._data_role)
        self._load_pair(self._find_pair(self._current_pair_id))
        self._refresh_exclusion_pair_selector()
        self._loading = False
        self._refresh_pair_buttons()

    def get_pairs(self):
        """Returns detached pair data in the explicit visible order.

        Returns:
            list[dict]: Ordered serialized folder pairs.
        """
        self._flush_pair()
        return copy.deepcopy(self._plan["pairs"])

    def set_busy(self, busy):
        """Locks editing actions during a background operation.

        Args:
            busy (bool): Whether an operation is active.
        """
        self._busy = bool(busy)
        self.pair_editor.setEnabled(not self._busy and self._current_pair_id is not None)
        self.exclusions_page.setEnabled(not self._busy and bool(self._plan.get("pairs")))
        self._refresh_pair_buttons()

    def _edited(self):
        """Notifies the owning view of a changed pair configuration."""
        if not self._loading:
            self.changed.emit()

    def _build(self):
        """Creates the pair list and editor for the Folders tab."""
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        hint = QtWidgets.QLabel("Add a folder pair for each source you want to sync.")
        hint.setObjectName("mutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        content = QtWidgets.QSplitter(qt_enum(QtCore.Qt, "Orientation", "Horizontal"))
        content.setChildrenCollapsible(False)
        list_container = QtWidgets.QWidget()
        list_layout = QtWidgets.QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 4, 0)
        self.pairs_list = QtWidgets.QListWidget()
        self.pairs_list.setMinimumWidth(155)
        self.pairs_list.setDragEnabled(True)
        self.pairs_list.setAcceptDrops(True)
        self.pairs_list.setDropIndicatorShown(True)
        self.pairs_list.setDragDropOverwriteMode(False)
        self.pairs_list.setDragDropMode(qt_enum(
            QtWidgets.QAbstractItemView, "DragDropMode", "InternalMove"
        ))
        self.pairs_list.setDefaultDropAction(qt_enum(QtCore.Qt, "DropAction", "MoveAction"))
        self.pairs_list.setContextMenuPolicy(qt_enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"))
        self.pairs_list.customContextMenuRequested.connect(self._show_pair_context_menu)
        self.pairs_list.currentItemChanged.connect(self._select_pair)
        self.pairs_list.model().rowsMoved.connect(self._pairs_reordered)
        list_layout.addWidget(self.pairs_list, 1)
        controls = QtWidgets.QHBoxLayout()
        self.add_pair_button = self._button("Add Folder Pair", self._add_pair)
        controls.addWidget(self.add_pair_button, 1)
        list_layout.addLayout(controls)
        content.addWidget(list_container)
        self.pair_editor = QtWidgets.QWidget()
        editor_layout = QtWidgets.QVBoxLayout(self.pair_editor)
        editor_layout.setContentsMargins(8, 0, 0, 0)
        editor_layout.setSpacing(7)
        paths = QtWidgets.QFormLayout()
        paths.setSpacing(7)
        paths.setFieldGrowthPolicy(qt_enum(QtWidgets.QFormLayout, "FieldGrowthPolicy", "ExpandingFieldsGrow"))
        paths.setRowWrapPolicy(qt_enum(QtWidgets.QFormLayout, "RowWrapPolicy", "WrapLongRows"))
        self.pair_name = QtWidgets.QLineEdit()
        self.pair_name.setPlaceholderText("Optional Folder Pair Name")
        paths.addRow("Pair Name", self.pair_name)
        self.source_path = FolderPathEdit("Choose a source folder")
        self.destination_path = FolderPathEdit("Choose a destination folder")
        variable_tip = (
            "Supports {variable} path tokens defined in the Variables tab, plus built-in path variables."
        )
        self.source_path.setToolTip(variable_tip)
        self.destination_path.setToolTip(variable_tip)
        paths.addRow("Source", self._path_row(self.source_path, self._browse_source, "Source"))
        paths.addRow(
            "Destination",
            self._path_row(self.destination_path, self._browse_destination, "Destination"),
        )
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.setSizePolicy(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Fixed"),
        )
        self.mode_combo.setMinimumContentsLength(18)
        self.mode_combo.setSizeAdjustPolicy(qt_enum(
            QtWidgets.QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"
        ))
        self.mode_combo.addItem("Add Only - Copy Missing Files", "add")
        self.mode_combo.addItem("Update - Add and Replace Changed Files", "update")
        self.mode_combo.addItem("Mirror - Also Remove Destination Extras", "mirror")
        self.mode_combo.addItem("Move - Transfer Files and Remove from Source", "move")
        self.mode_combo.addItem("Two-Way - Merge Changes From Both Folders", "two_way")
        paths.addRow("Sync Mode", self.mode_combo)
        editor_layout.addLayout(paths)
        self.mode_description = QtWidgets.QLabel()
        self.mode_description.setWordWrap(True)
        self.mode_description.setObjectName("mutedLabel")
        editor_layout.addWidget(self.mode_description)
        editor_layout.addStretch()
        editor_scroll = QtWidgets.QScrollArea()
        editor_scroll.setWidgetResizable(True)
        editor_scroll.setWidget(self.pair_editor)
        content.addWidget(editor_scroll)
        content.setStretchFactor(0, 0)
        content.setStretchFactor(1, 1)
        content.setSizes([190, 580])
        layout.addWidget(content, 1)
        self.exclusions_page = self._build_exclusions_page()

    def _build_exclusions_page(self):
        """Builds the separate, resizable exclusion-rules tab.

        Returns:
            QWidget: Exclusion editor page.
        """
        page = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(page)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        hint = QtWidgets.QLabel(
            "Include rules limit matching files or folders. Exclude rules always take priority. "
            "Folders are scanned to find matching descendants; excluded folders and their contents are skipped."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)
        pair_row = QtWidgets.QHBoxLayout()
        pair_row.addWidget(QtWidgets.QLabel("Folder Pair"))
        self.exclusion_pair_combo = QtWidgets.QComboBox()
        self.exclusion_pair_combo.setMinimumContentsLength(24)
        self.exclusion_pair_combo.setSizeAdjustPolicy(qt_enum(
            QtWidgets.QComboBox, "SizeAdjustPolicy", "AdjustToMinimumContentsLengthWithIcon"
        ))
        pair_row.addWidget(self.exclusion_pair_combo, 1)
        layout.addLayout(pair_row)
        self.exclusions_table = _ResizableExclusionsTable(0, 5)
        self.exclusions_table.setHorizontalHeaderLabels(
            ["Method", "Name or Pattern", "Match", "Affects", "Ignore Case"]
        )
        self.exclusions_table.setEditTriggers(qt_enum(
            QtWidgets.QAbstractItemView, "EditTrigger", "NoEditTriggers"
        ))
        self.exclusions_table.setSelectionBehavior(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"
        ))
        self.exclusions_table.setSelectionMode(qt_enum(
            QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"
        ))
        self.exclusions_table.setAlternatingRowColors(True)
        self.exclusions_table.verticalHeader().hide()
        header = self.exclusions_table.horizontalHeader()
        header.setSectionResizeMode(qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Interactive"))
        header.setStretchLastSection(False)
        for column, width in enumerate((120, 245, 145, 160, 115)):
            self.exclusions_table.setColumnWidth(column, width)
        self.exclusions_table.viewport_resized.connect(self._fill_exclusions_table)
        layout.addWidget(self.exclusions_table, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.exclusion_add_button = self._button("Add Rule", self._add_exclusion)
        self.exclusion_remove_button = self._button("Remove Rule", self._remove_exclusion)
        buttons.addWidget(self.exclusion_add_button)
        buttons.addWidget(self.exclusion_remove_button)
        buttons.addStretch()
        layout.addLayout(buttons)
        return page

    def _fill_exclusions_table(self):
        """Use remaining viewport width for the final column after window resizes."""
        table = self.exclusions_table
        if getattr(self, "_filling_exclusion_table", False):
            return
        available_width = table.viewport().width()
        if available_width <= 0:
            return
        preceding_width = sum(table.columnWidth(column) for column in range(4))
        target_width = max(115, available_width - preceding_width)
        if table.columnWidth(4) == target_width:
            return
        self._filling_exclusion_table = True
        try:
            table.setColumnWidth(4, target_width)
        finally:
            self._filling_exclusion_table = False

    def _add_exclusion_row(self, rule=None):
        """Adds one editable rule row.

        Args:
            rule (dict, optional): Existing normalized rule to display.

        Returns:
            int: New table row index.
        """
        rule = rule or {
            "effect": "include", "pattern": "", "match": "starts_with",
            "applies_to": "files", "ignore_case": True,
        }
        row = self.exclusions_table.rowCount()
        self.exclusions_table.insertRow(row)
        effect = QtWidgets.QComboBox()
        effect.addItem("Include", "include")
        effect.addItem("Exclude", "exclude")
        index = effect.findData(rule.get("effect", "exclude"))
        effect.setCurrentIndex(max(0, index))
        effect.setToolTip(
            "Include keeps matching items in scope. Exclude always skips matching items."
        )
        effect.currentIndexChanged.connect(self._pair_edited)
        self.exclusions_table.setCellWidget(row, 0, effect)
        pattern = QtWidgets.QLineEdit(str(rule.get("pattern", "")))
        pattern.setPlaceholderText("Name or text to match")
        pattern.setToolTip("Matches an entry name at any depth in the folder pair.")
        pattern.textChanged.connect(self._pair_edited)
        self.exclusions_table.setCellWidget(row, 1, pattern)
        match = QtWidgets.QComboBox()
        match.addItem("Starts With", "starts_with")
        match.addItem("Ends With", "ends_with")
        match.addItem("Contains", "contains")
        match.addItem("Exact Match", "exact")
        if rule.get("match") == "glob":
            match.addItem("Legacy Glob", "glob")
            match.setToolTip("Preserves a wildcard rule from an older plan.")
        index = match.findData(rule.get("match", "starts_with"))
        match.setCurrentIndex(max(0, index))
        match.currentIndexChanged.connect(self._pair_edited)
        self.exclusions_table.setCellWidget(row, 2, match)
        applies_to = QtWidgets.QComboBox()
        applies_to.addItem("Files", "files")
        applies_to.addItem("Folders", "folders")
        applies_to.addItem("Files and Folders", "both")
        index = applies_to.findData(rule.get("applies_to", "both"))
        applies_to.setCurrentIndex(max(0, index))
        applies_to.currentIndexChanged.connect(self._pair_edited)
        self.exclusions_table.setCellWidget(row, 3, applies_to)
        ignore_case = QtWidgets.QCheckBox("Ignore case")
        ignore_case.setChecked(bool(rule.get("ignore_case", True)))
        ignore_case.setToolTip("Treat upper and lower case letters as equal when matching this rule.")
        ignore_case.toggled.connect(self._pair_edited)
        ignore_case_cell = QtWidgets.QWidget()
        ignore_case_layout = QtWidgets.QHBoxLayout(ignore_case_cell)
        ignore_case_layout.setContentsMargins(0, 0, 0, 0)
        ignore_case_layout.addWidget(
            ignore_case, 0, qt_enum(QtCore.Qt, "AlignmentFlag", "AlignCenter")
        )
        self.exclusions_table.setCellWidget(row, 4, ignore_case_cell)
        self.exclusions_table.setRowHeight(row, 34)
        return row

    def _add_exclusion(self):
        """Adds a default rule and makes the newly created row visible."""
        if self._loading or self._busy or self._plan is None:
            return
        row = self._add_exclusion_row()
        self.exclusions_table.setCurrentCell(row, 1)
        self._refresh_exclusion_buttons()
        self._pair_edited()
        self.exclusions_table.cellWidget(row, 1).setFocus()


    def _remove_exclusion(self):
        """Removes the currently selected exclusion rule."""
        if self._loading or self._busy:
            return
        row = self.exclusions_table.currentRow()
        if row < 0:
            return
        self.exclusions_table.removeRow(row)
        self._refresh_exclusion_buttons()
        self._pair_edited()


    def _refresh_exclusion_buttons(self):
        """Enables rule removal only when a table row is selected."""
        self.exclusion_remove_button.setEnabled(
            not self._busy and self.exclusions_table.currentRow() >= 0
        )


    def _exclusion_rows(self):
        """Reads complete rule rows in visible table order.

        Returns:
            list[dict]: Nonempty exclusion rules.
        """
        rules = []
        for row in range(self.exclusions_table.rowCount()):
            pattern = self.exclusions_table.cellWidget(row, 1).text().strip()
            if not pattern:
                continue
            rules.append({
                "effect": self.exclusions_table.cellWidget(row, 0).currentData(),
                "pattern": pattern,
                "match": self.exclusions_table.cellWidget(row, 2).currentData(),
                "applies_to": self.exclusions_table.cellWidget(row, 3).currentData(),
                "ignore_case": self.exclusions_table.cellWidget(row, 4).findChild(
                    QtWidgets.QCheckBox
                ).isChecked(),
            })
        return rules


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


    def _path_row(self, field, callback, label):
        """Creates a path input with inspect and folder browse controls.

        Args:
            field (QLineEdit): Path editor.
            callback (callable): Folder browsing action.
            label (str): Human-readable path direction.

        Returns:
            QWidget: Compact path row.
        """
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        layout.addWidget(field, 1)
        feedback_button = self._button("$", lambda: self._show_path_feedback(label, field))
        feedback_button.setFixedSize(33, 33)
        feedback_button.setStyleSheet("QPushButton { font-size: 17px; font-weight: 600; }")
        feedback_button.setToolTip("Show the resolved path and whether it currently exists")
        feedback_button.setAccessibleName(f"Inspect {label.lower()} path")
        layout.addWidget(feedback_button)
        button = self._button("", callback)
        button.setIcon(padded_icon("icons/folder_open.svg"))
        if button.icon().isNull():
            button.setIcon(QtWidgets.QApplication.style().standardIcon(
                qt_enum(QtWidgets.QStyle, "StandardPixmap", "SP_DirOpenIcon")
            ))
        button.setIconSize(QtCore.QSize(24, 24))
        button.setStyleSheet("QPushButton { padding: 0px; }")
        button.setFixedSize(33, 33)
        button.setToolTip("Browse for a folder")
        layout.addWidget(button)
        return row


    def _show_path_feedback(self, label, field):
        """Requests diagnostics for the current path template.

        Args:
            label (str): Human-readable path direction.
            field (QLineEdit): Path editor to inspect.
        """
        self.path_feedback_requested.emit(label, field.text())


    def _set_combo(self, combo, value):
        """Selects a serialized value using the combo's item data.

        Args:
            combo (QComboBox): Target input.
            value (str): Serialized value.
        """
        index = combo.findData(value)
        combo.setCurrentIndex(max(0, index))


    def _find_pair(self, pair_id):
        """Finds a pair by its persistent identity.

        Args:
            pair_id (str): Stable folder-pair ID.

        Returns:
            dict or None: Matching pair in the editable state.
        """
        for pair in (self._plan or {}).get("pairs", []):
            if pair.get("id") == pair_id:
                return pair
        return None


    def _insert_pair_item(self, pair):
        """Adds a selectable folder-pair row.

        Args:
            pair (dict): Folder-pair data.
        """
        pair_index = self.pairs_list.count() + 1
        label = pair.get("name") or f"Folder Pair {pair_index}"
        item = QtWidgets.QListWidgetItem(label)
        item.setData(self._data_role, pair.get("id"))
        item.setToolTip(
            f"{pair.get('source', '')}\n-> {pair.get('destination', '')}\n"
            "Right-click for actions or drag to reorder."
        )
        self.pairs_list.addItem(item)

    def _show_pair_context_menu(self, position):
        """Shows configuration actions for a folder pair or the empty list area."""
        item = self.pairs_list.itemAt(position)
        menu = QtWidgets.QMenu(self)
        if item is None:
            self._add_context_action(menu, "Add Folder Pair", "ui_add.svg", self._add_pair)
            self._add_context_action(
                menu, "Import Folder Pair…", "rigger_action_import_grayscale.svg", self._import_pair
            )
        else:
            self.pairs_list.setCurrentItem(item)
            row = self.pairs_list.row(item)
            move_up = self._add_context_action(
                menu, "Move Up", "rigger_action_up_arrow.svg",
                lambda unused=False: self._move_pair(-1),
            )
            move_up.setEnabled(row > 0 and not self._busy)
            move_down = self._add_context_action(
                menu, "Move Down", "rigger_action_down_arrow.svg",
                lambda unused=False: self._move_pair(1),
            )
            move_down.setEnabled(row < self.pairs_list.count() - 1 and not self._busy)
            menu.addSeparator()
            self._add_context_action(
                menu, "Duplicate Folder Pair", "rigger_action_duplicate_grayscale.svg", self._duplicate_pair
            )
            self._add_context_action(
                menu, "Export Folder Pair…", "rigger_action_export_grayscale.svg", self._export_pair
            )
            self._add_context_action(
                menu, "Import Folder Pair…", "rigger_action_import_grayscale.svg", self._import_pair
            )
            menu.addSeparator()
            self._add_context_action(
                menu, "Remove Folder Pair", "rigger_action_delete_grayscale.svg", self._confirm_remove_pair
            )
        runner = getattr(menu, "exec", None) or menu.exec_
        runner(self.pairs_list.mapToGlobal(position))

    @staticmethod
    def _add_context_action(menu, label, icon_name, callback):
        """Adds a local greyscale icon and callback to a context menu.

        Args:
            menu (QMenu): Context menu receiving the action.
            label (str): Visible action label.
            icon_name (str): Icon filename under the bundled icon directory.
            callback (callable): Action invoked by the menu item.

        Returns:
            QAction: Added context-menu action.
        """
        action = menu.addAction(padded_icon(f"icons/{icon_name}"), label)
        action.triggered.connect(callback)
        return action

    def _confirm_remove_pair(self):
        """Confirms removal of the selected pair configuration."""
        pair = self._find_pair(self._current_pair_id)
        if pair is None:
            return
        response = QtWidgets.QMessageBox.question(
            self, "Remove folder pair?", "Remove this folder-pair configuration? Files on disk are untouched.",
            qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
            | qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
            qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
        )
        if response == qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes"):
            self._remove_pair()

    def _duplicate_pair(self):
        """Duplicates the selected pair with a fresh stable identifier."""
        if self._busy or self._plan is None:
            return
        row = self.pairs_list.currentRow()
        pair = self._find_pair(self._current_pair_id)
        if row < 0 or pair is None:
            return
        self._flush_pair()
        duplicate = copy.deepcopy(pair)
        duplicate["id"] = new_pair()["id"]
        duplicate["name"] = f"{pair.get('name') or f'Folder Pair {row + 1}'} Copy"
        self._plan["pairs"].insert(row + 1, duplicate)
        self._rebuild_pair_list(duplicate["id"])
        self._edited()

    def _import_pair(self):
        """Imports one validated folder pair from a portable JSON file."""
        if self._busy or self._plan is None:
            return
        path, unused_filter = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import folder pair", "", "SyncEngine folder pair (*.syncpair *.json);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as stream:
                payload = json.load(stream)
            pair_data = payload.get("pair", payload) if isinstance(payload, dict) else payload
            if not isinstance(pair_data, dict):
                raise ValueError("The selected file does not contain a folder pair.")
            imported = copy.deepcopy(pair_data)
            imported["id"] = new_pair()["id"]
            candidate = new_plan()
            candidate["pairs"] = [imported]
            pair = validate_plan(candidate)["pairs"][0]
            self._flush_pair()
            self._plan["pairs"].append(pair)
            self._rebuild_pair_list(pair["id"])
            self._edited()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            QtWidgets.QMessageBox.warning(self, "Unable to import folder pair", str(error))

    def _export_pair(self):
        """Exports the selected folder-pair settings as portable JSON."""
        self._flush_pair()
        pair = self._find_pair(self._current_pair_id)
        if pair is None:
            return
        path, unused_filter = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export folder pair", "folder-pair.syncpair", "SyncEngine folder pair (*.syncpair)",
        )
        if not path:
            return
        if not path.lower().endswith(".syncpair"):
            path += ".syncpair"
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as stream:
                json.dump({"schema_version": 1, "pair": pair}, stream, indent=2, ensure_ascii=False)
                stream.write("\n")
        except OSError as error:
            QtWidgets.QMessageBox.warning(self, "Unable to export folder pair", str(error))

    def _rebuild_pair_list(self, selected_id):
        """Rebuilds rows while preserving the selected stable pair identifier."""
        self._loading = True
        self.pairs_list.clear()
        for pair in self._plan["pairs"]:
            self._insert_pair_item(pair)
        selected_row = next(
            (row for row in range(self.pairs_list.count())
             if self.pairs_list.item(row).data(self._data_role) == selected_id),
            -1,
        )
        self.pairs_list.setCurrentRow(selected_row)
        self._loading = False
        current = self.pairs_list.currentItem()
        self._current_pair_id = current.data(self._data_role) if current else None
        self._load_pair(self._find_pair(self._current_pair_id))
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()

    def _select_pair(self, current, previous):
        """Saves the previous editor and loads the newly selected pair.

        Args:
            current (QListWidgetItem or None): Newly selected pair row.
            previous (QListWidgetItem or None): Previous pair row.
        """
        if self._loading:
            return
        self._flush_pair()
        self._current_pair_id = current.data(self._data_role) if current else None
        self._load_pair(self._find_pair(self._current_pair_id))
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()

    def _select_pair_id(self, pair_id):
        """Selects a pair by its stable ID from the Exclusions tab.

        Args:
            pair_id (str): Stable folder-pair identifier.
        """
        if self._loading or self._busy or not pair_id:
            return
        row = next(
            (index for index in range(self.pairs_list.count())
             if self.pairs_list.item(index).data(self._data_role) == pair_id),
            -1,
        )
        if row >= 0 and row != self.pairs_list.currentRow():
            self.pairs_list.setCurrentRow(row)

    def _load_pair(self, pair):
        """Loads a single pair into existing editor widgets.

        Args:
            pair (dict or None): Folder-pair data.
        """
        prior_loading = self._loading
        self._loading = True
        values = pair or {}
        self.pair_name.setText(values.get("name", ""))
        self.source_path.setText(values.get("source", ""))
        self.destination_path.setText(values.get("destination", ""))
        self._set_combo(self.mode_combo, values.get("mode", "update"))
        self.exclusions_table.setRowCount(0)
        for rule in values.get("exclusions", []):
            self._add_exclusion_row(rule)
        self._refresh_exclusion_buttons()
        self.pair_editor.setEnabled(bool(pair) and not self._busy)
        self._update_mode_description()
        self._loading = prior_loading

    def _flush_pair(self):
        """Copies the stable editor's values into its matching pair state."""
        pair = self._find_pair(self._current_pair_id)
        if pair is None:
            return
        pair.update({
            "name": self.pair_name.text().strip(),
            "source": self.source_path.text().strip(),
            "destination": self.destination_path.text().strip(),
            "mode": self.mode_combo.currentData(),
            "exclusions": self._exclusion_rows(),
        })


    def _pair_edited(self, *unused):
        """Persists pair edits and refreshes its visible label.

        Args:
            *unused (tuple): Qt signal arguments.
        """
        if self._loading or self._plan is None:
            return
        self._flush_pair()
        pair = self._find_pair(self._current_pair_id)
        if pair is not None:
            fallback = f"Folder Pair {self.pairs_list.currentRow() + 1}"
            item = self.pairs_list.currentItem()
            if item is not None:
                item.setText(pair.get("name") or fallback)
                item.setToolTip(
                    f"{pair.get('source', '')}\n-> {pair.get('destination', '')}\n"
                    "Right-click for actions or drag to reorder."
                )
            self._refresh_exclusion_pair_selector()
        self._update_mode_description()
        self._edited()

    def _update_mode_description(self):
        """Updates the selected mode's concise explanation and removal warning."""
        descriptions = {
            "add": "Copies files that do not exist at the destination. Existing files are preserved.",
            "update": "Copies missing and changed files. Destination files absent from the source are kept.",
            "mirror": (
                "Copies missing and changed files, then removes destination extras. Excluded items are preserved."
            ),
            "move": (
                "Transfers included source files to the destination and removes each source file after "
                "verification. Destination-only files and excluded items are preserved."
            ),
            "two_way": (
                "Merges changes on either side using saved sync history. First sync combines non-conflicting files; "
                "different edits to the same file require a choice. Deletions propagate after confirmation."
            ),
        }
        mode = self.mode_combo.currentData()
        self.mode_description.setText(descriptions.get(mode, ""))
        color = "#d0b58a" if mode in ("mirror", "move", "two_way") else "#b6b6b6"
        self.mode_description.setStyleSheet(f"color: {color};")


    def _add_pair(self):
        """Appends an empty folder pair with a fresh identity."""
        if self._plan is None or self._busy:
            return
        self._flush_pair()
        pair = {
            "id": uuid.uuid4().hex, "name": f"Folder Pair {len(self._plan['pairs']) + 1}",
            "source": "", "destination": "",
            "mode": "update", "exclusions": [],
        }
        self._plan.setdefault("pairs", []).append(pair)
        self._insert_pair_item(pair)
        self.pairs_list.setCurrentRow(self.pairs_list.count() - 1)
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()
        self._edited()
        self.source_path.setFocus()

    def _remove_pair(self):
        """Removes only the selected configuration row and keeps inputs untouched."""
        if self._plan is None or self._busy:
            return
        row = self.pairs_list.currentRow()
        if row < 0:
            return
        pair_id = self._current_pair_id
        self._plan["pairs"] = [pair for pair in self._plan["pairs"] if pair.get("id") != pair_id]
        self._current_pair_id = None
        self.pairs_list.takeItem(row)
        current = self.pairs_list.currentItem()
        self._current_pair_id = current.data(self._data_role) if current else None
        self._load_pair(self._find_pair(self._current_pair_id))
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()
        self._edited()

    def _move_pair_for_id(self, pair_id, offset):
        """Selects and reorders a pair from the Exclusions pair selector.

        Args:
            pair_id (str): Stable folder-pair identifier.
            offset (int): Relative destination index.
        """
        for row in range(self.pairs_list.count()):
            if self.pairs_list.item(row).data(self._data_role) == pair_id:
                self.pairs_list.setCurrentRow(row)
                self._move_pair(offset)
                return

    def _move_pair(self, offset):
        """Changes pair order while preserving the selected pair identity.

        Args:
            offset (int): Relative destination index.
        """
        if self._plan is None or self._busy:
            return
        row = self.pairs_list.currentRow()
        destination = row + offset
        if row < 0 or not 0 <= destination < self.pairs_list.count():
            return
        self._flush_pair()
        selected_id = self._current_pair_id
        pair = self._plan["pairs"].pop(row)
        self._plan["pairs"].insert(destination, pair)
        self._loading = True
        self.pairs_list.clear()
        for pair_data in self._plan["pairs"]:
            self._insert_pair_item(pair_data)
        self.pairs_list.setCurrentRow(destination)
        self._loading = False
        self._current_pair_id = selected_id
        self._load_pair(self._find_pair(selected_id))
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()
        self._edited()

    def _pairs_reordered(self, *unused):
        """Updates serialized order after a user drag-and-drops a pair.

        Args:
            *unused (tuple): Qt model signal arguments.
        """
        if self._loading or self._busy or self._plan is None:
            return
        self._flush_pair()
        pairs_by_id = {pair.get("id"): pair for pair in self._plan["pairs"]}
        ordered_ids = [
            self.pairs_list.item(row).data(self._data_role)
            for row in range(self.pairs_list.count())
        ]
        self._plan["pairs"] = [pairs_by_id[pair_id] for pair_id in ordered_ids if pair_id in pairs_by_id]
        current = self.pairs_list.currentItem()
        self._current_pair_id = current.data(self._data_role) if current else None
        self._load_pair(self._find_pair(self._current_pair_id))
        self._refresh_exclusion_pair_selector()
        self._refresh_pair_buttons()
        self._edited()

    def _refresh_pair_buttons(self):
        """Updates pair actions and enables list dragging when applicable."""
        row = self.pairs_list.currentRow()
        editable = self._plan is not None and not self._busy
        self.add_pair_button.setEnabled(editable)
        self.pairs_list.setDragEnabled(editable and self.pairs_list.count() > 1)

    def _refresh_exclusion_pair_selector(self):
        """Refreshes the rule-tab pair selector without changing the active pair."""
        selected_id = self._current_pair_id
        prior_loading = self._loading
        self._loading = True
        self.exclusion_pair_combo.clear()
        for index, pair in enumerate(self._plan.get("pairs", [])):
            name = pair.get("name") or f"Folder Pair {index + 1}"
            self.exclusion_pair_combo.addItem(name, pair.get("id"))
        index = self.exclusion_pair_combo.findData(selected_id)
        self.exclusion_pair_combo.setCurrentIndex(index)
        self._loading = prior_loading
        self.exclusions_page.setEnabled(not self._busy and bool(self._plan.get("pairs")))

    def _select_exclusion_pair(self, index):
        """Selects the corresponding folder pair from the Exclusions tab.

        Args:
            index (int): Selected combo-box row.
        """
        if self._loading or index < 0:
            return
        self._select_pair_id(self.exclusion_pair_combo.itemData(index))

    def _browse_source(self):
        """Offers an existing-folder chooser for the current source."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Choose source folder", self.source_path.text())
        if folder:
            self.source_path.setText(os.path.normpath(folder))


    def _browse_destination(self):
        """Offers a destination chooser while permitting typed new folder paths."""
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Choose destination folder", self.destination_path.text()
        )
        if folder:
            self.destination_path.setText(os.path.normpath(folder))

