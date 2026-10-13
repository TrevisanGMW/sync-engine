"""Offscreen tests for plan editing and meaningful progress presentation."""

import copy
import os
import sys
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sync_engine"))

try:
    from sync_engine_qt import QtCore, QtWidgets, qt_enum
    from sync_engine_style import icon_path
    from sync_engine_view import SyncEngineView
except ImportError:
    QtWidgets = None


def sample_plan():
    """Builds a blank test plan without exposing real source or target paths.

    Returns:
        dict: Test-only serializable plan.
    """
    return {
        "schema_version": 1, "id": "test-plan", "name": "Test plan", "compare": "metadata",
        "preview_view": "table",
        "timestamp_tolerance": 0.0, "allow_empty_mirror": False, "variables": {},
        "execution": {"timeout_seconds": 0, "expand_log_while_syncing": False, "generate_report": False},
        "automation": {
            "start_mode": "countdown", "countdown_seconds": 10,
            "start_minimized": False, "allow_deletions": False, "close_after_run": False,
        },
        "pairs": [{
            "id": "test-pair", "name": "First pair", "source": "", "destination": "", "mode": "update",
            "exclusions": [],
        }],
    }


@unittest.skipIf(QtWidgets is None, "An existing PySide6 or PySide2 runtime is required.")
class TestSyncEngineView(unittest.TestCase):
    """Validates stateful UI editing without opening dialogs or scanning disks."""

    @classmethod
    def setUpClass(cls):
        """Creates one shared Qt application for the test process."""
        cls.application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        """Constructs a hidden window and loads an independent test plan."""
        self.view = SyncEngineView()
        self.plan = sample_plan()
        self.view.set_plan(self.plan)

    def tearDown(self):
        """Disposes of the hidden window and drains deferred Qt cleanup."""
        self.view.close()
        self.view.deleteLater()
        self.application.processEvents()

    def test_construct_roundtrip_without_mutating_input(self):
        """Preserves stable identities and detaches returned plan data."""
        expected = copy.deepcopy(self.plan)
        self.assertEqual(expected, self.view.get_plan())
        result = self.view.get_plan()
        result["pairs"][0]["name"] = "Changed outside the view"
        self.assertEqual(expected, self.plan)
        self.assertEqual("First pair", self.view.get_plan()["pairs"][0]["name"])

    def test_utilities_is_a_top_level_menu_next_to_tools(self):
        """Keeps utility actions out of File and exposes their own menu."""
        menu_names = [action.text() for action in self.view.menuBar().actions()]
        self.assertIn("Utilities", menu_names)
        self.assertIn("Tools", menu_names)
        self.assertEqual(1, abs(menu_names.index("Utilities") - menu_names.index("Tools")))
        file_menu_names = [action.text() for action in self.view.file_menu.actions()]
        self.assertNotIn("Utilities", file_menu_names)

    def test_file_and_utility_menu_actions_have_icons(self):
        """Gives all user-facing File and Utilities menu commands a visible icon."""
        file_actions = [action for action in self.view.file_menu.actions() if not action.isSeparator()]
        utility_actions = [action for action in self.view.utilities_menu.actions() if not action.isSeparator()]
        self.assertTrue(file_actions)
        self.assertTrue(utility_actions)
        self.assertTrue(all(not action.icon().isNull() for action in file_actions))
        self.assertTrue(all(not action.icon().isNull() for action in utility_actions))

    def test_tools_help_and_sync_plan_menu_actions_have_icons(self):
        """Keeps the less common menus discoverable with consistent icons."""
        tools_actions = [action for action in self.view.tools_menu.actions() if not action.isSeparator()]
        help_actions = [action for action in self.view.help_menu.actions() if not action.isSeparator()]
        self.assertTrue(all(not action.icon().isNull() for action in tools_actions))
        self.assertTrue(all(not action.icon().isNull() for action in help_actions))
        top_level_actions = self.view.menuBar().actions()
        self.assertTrue(all(action.icon().isNull() for action in top_level_actions))
        self.assertEqual(["File", "Utilities", "Tools", "Help"],
                         [action.text() for action in top_level_actions])

        plan_item = QtWidgets.QListWidgetItem("Test Plan")
        plan_item.setData(self.view._data_role, "test-plan")
        menu = self.view._build_plan_context_menu(plan_item)
        plan_actions = [action for action in menu.actions() if not action.isSeparator()]
        self.assertTrue(plan_actions)
        self.assertTrue(all(not action.icon().isNull() for action in plan_actions))

    def test_menu_icons_have_transparent_centered_padding(self):
        """Leaves an even transparent border around menu artwork."""
        pixmap = self.view.logs_action.icon().pixmap(QtCore.QSize(20, 20))
        image = pixmap.toImage()
        self.assertEqual(0, image.pixelColor(0, 0).alpha())
        self.assertEqual(0, image.pixelColor(image.width() - 1, image.height() - 1).alpha())
        filled_columns = [
            x for x in range(image.width())
            if any(image.pixelColor(x, y).alpha() for y in range(image.height()))
        ]
        left_space = filled_columns[0]
        right_space = image.width() - 1 - filled_columns[-1]
        self.assertLessEqual(abs(left_space - right_space), 1)

    def test_action_buttons_are_anchored_in_feedback_footer(self):
        """Keeps Preview and Sync aligned with the lower feedback bar."""
        self.assertIs(self.view.feedback_footer, self.view.preview_button.parentWidget())
        self.assertIs(self.view.feedback_footer, self.view.sync_button.parentWidget())

    def test_window_uses_the_bundled_taskbar_icon(self):
        """Loads the multi-resolution ICO on Windows for the native taskbar."""
        selected_icon = icon_path()
        self.assertTrue(os.path.isfile(selected_icon))
        self.assertFalse(self.view.windowIcon().isNull())
        if os.name == "nt":
            self.assertEqual(".ico", os.path.splitext(selected_icon)[1].lower())

    def test_footer_uses_one_background_and_no_spacer_before_actions(self):
        """Shares one footer surface and gives status text the freed width."""
        self.assertIn("#292d31", self.view.status_bar.styleSheet())
        self.assertIn("#292d31", self.view.feedback_footer.styleSheet())
        layout = self.view.feedback_footer.layout()
        widgets = [layout.itemAt(index).widget() for index in range(layout.count())]
        self.assertEqual(self.view.open_full_app_button, widgets[-4])
        self.assertEqual(self.view.cancel_button, widgets[-3])
        self.assertEqual(self.view.preview_button, widgets[-2])
        self.assertEqual(self.view.sync_button, widgets[-1])
        self.assertTrue(all(layout.itemAt(index).spacerItem() is None for index in range(layout.count())))

    def test_sync_button_has_balanced_window_edge_padding(self):
        """Balances the Sync button gap at the right and bottom window edges."""
        self.view.resize(1120, 850)
        self.view.show()
        self.application.processEvents()
        top_left = self.view.sync_button.mapTo(self.view, QtCore.QPoint(0, 0))
        right_gap = self.view.width() - top_left.x() - self.view.sync_button.width()
        bottom_gap = self.view.height() - top_left.y() - self.view.sync_button.height()
        self.assertLessEqual(abs(right_gap - bottom_gap), 2)

    def test_automatic_run_mode_shows_preview_progress_and_full_app_action(self):
        """Hides editing surfaces until the user explicitly opens the full app."""
        self.view.set_automatic_run_mode(True)
        self.assertTrue(self.view.sidebar.isHidden())
        self.assertTrue(self.view.plan_heading.isHidden())
        self.assertTrue(self.view.save_button.isHidden())
        self.assertTrue(self.view.menuBar().isHidden())
        self.assertTrue(self.view.settings_tabs.tabBar().isHidden())
        self.assertFalse(self.view.progress_panel.isHidden())
        self.assertTrue(self.view.log_panel.isHidden())
        self.assertFalse(self.view.open_full_app_button.isHidden())
        self.assertTrue(self.view.preview_button.isHidden())
        self.assertTrue(self.view.sync_button.isHidden())
        self.view.show_full_app()
        self.assertFalse(self.view.sidebar.isHidden())
        self.assertFalse(self.view.settings_tabs.tabBar().isHidden())
        self.view.settings_tabs.setCurrentWidget(self.view.folders_page)
        self.assertTrue(self.view.progress_panel.isHidden())

    def test_automation_close_option_round_trips(self):
        """Persists the automatic-run close preference with its sync plan."""
        self.view.close_after_run.setChecked(True)
        self.assertTrue(self.view.get_plan()["automation"]["close_after_run"])

    def test_about_dialog_displays_application_version(self):
        """Shows the version kept in the standalone version module."""
        with mock.patch.object(QtWidgets.QMessageBox, "about") as about_dialog:
            self.view._show_about()
        self.assertIn("0.0.1", about_dialog.call_args[0][2])

    def test_narrow_footer_keeps_main_actions_visible(self):
        """Hides optional status text before reducing the Preview and Sync buttons."""
        self.view.resize(820, 660)
        self.view.show()
        self.application.processEvents()
        self.assertTrue(self.view.status_label.isHidden())
        self.assertTrue(self.view.summary_label.isHidden())
        self.view.set_unsaved_changes(True)
        self.view.set_busy(True)
        self.application.processEvents()
        self.assertTrue(self.view.cancel_button.isVisible())
        self.assertTrue(self.view.preview_button.isVisible())
        self.assertTrue(self.view.sync_button.isVisible())
        self.assertGreaterEqual(self.view.preview_button.width(), 96)
        self.assertGreaterEqual(self.view.sync_button.width(), 104)
        self.view.set_busy(False)

    def test_progress_panel_only_shows_for_preview_or_active_work(self):
        """Hides the large progress panel from ordinary plan-editing tabs."""
        self.assertTrue(self.view.progress_panel.isHidden())
        self.view.settings_tabs.setCurrentWidget(self.view.preview_page)
        self.assertFalse(self.view.progress_panel.isHidden())
        self.view.settings_tabs.setCurrentWidget(self.view.folders_page)
        self.assertTrue(self.view.progress_panel.isHidden())
        self.view.set_busy(True)
        self.assertFalse(self.view.progress_panel.isHidden())
        self.view.set_busy(False)
        self.assertTrue(self.view.progress_panel.isHidden())

    def test_automation_mode_updates_enabled_controls(self):
        """Makes the selected automatic startup behavior clear and serializable."""
        self.view.automation_start_mode.setCurrentIndex(0)
        self.assertFalse(self.view.countdown_seconds.isEnabled())
        self.assertTrue(self.view.start_minimized.isEnabled())
        self.view.automation_start_mode.setCurrentIndex(2)
        self.assertFalse(self.view.countdown_seconds.isEnabled())
        self.assertFalse(self.view.start_minimized.isEnabled())
        self.assertEqual("confirm", self.view.get_plan()["automation"]["start_mode"])

    def test_new_variable_run_button_uses_play_icon_without_cropped_text(self):
        """Uses the bundled play icon and an accessible name for row query actions."""
        self.view._add_variable_row()
        run_cell = self.view.variables_table.cellWidget(0, 3)
        run_button = run_cell.findChild(QtWidgets.QPushButton)
        self.assertEqual("", run_button.text())
        self.assertEqual("Run Query", run_button.accessibleName())
        self.assertFalse(run_button.icon().isNull())
        self.assertEqual((1, 1, 1, 1), run_cell.layout().getContentsMargins())
        self.assertEqual(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
            run_button.sizePolicy().horizontalPolicy(),
        )
        self.assertEqual(
            qt_enum(QtWidgets.QSizePolicy, "Policy", "Expanding"),
            run_button.sizePolicy().verticalPolicy(),
        )

    def test_user_edits_and_pair_switch_preserve_exclusions(self):
        """Keeps edits while adding, switching, reordering, and removing pairs."""
        pair_editor = self.view.folders_page
        pair_editor._add_exclusion()
        pair_editor.exclusions_table.cellWidget(0, 0).setCurrentIndex(1)
        pair_editor.exclusions_table.cellWidget(0, 1).setText("[Example]")
        pair_editor.exclusions_table.cellWidget(0, 3).setCurrentIndex(1)
        pair_editor._add_exclusion()
        pair_editor.exclusions_table.cellWidget(1, 1).setText(".tmp")
        pair_editor.exclusions_table.cellWidget(1, 2).setCurrentIndex(2)
        pair_editor.exclusions_table.cellWidget(1, 3).setCurrentIndex(0)
        pair_editor.exclusions_table.cellWidget(1, 4).findChild(QtWidgets.QCheckBox).setChecked(False)
        pair_editor._add_pair()
        added_id = self.view.get_plan()["pairs"][1]["id"]
        pair_editor.pair_name.setText("Second pair")
        pair_editor._move_pair_for_id(added_id, -1)
        result = self.view.get_plan()
        self.assertEqual(added_id, result["pairs"][0]["id"])
        rules = result["pairs"][1]["exclusions"]
        self.assertEqual(["[Example]", ".tmp"], [rule["pattern"] for rule in rules])
        self.assertEqual(["exclude", "include"], [rule["effect"] for rule in rules])
        self.assertFalse(rules[1]["ignore_case"])
        pair_editor.pairs_list.setCurrentRow(1)
        self.assertEqual("[Example]", pair_editor.exclusions_table.cellWidget(0, 1).text())
        pair_editor._remove_pair()
        self.assertEqual([added_id], [pair["id"] for pair in self.view.get_plan()["pairs"]])

    def test_exclusion_pair_selector_switches_rules_without_attribute_error(self):
        """Shows each pair's own filters when the Filters selector changes."""
        first = copy.deepcopy(self.plan["pairs"][0])
        first["exclusions"] = [{
            "pattern": "first-only", "match": "exact", "applies_to": "files", "ignore_case": True,
        }]
        second = copy.deepcopy(first)
        second["id"] = "second-pair"
        second["name"] = "Second Pair"
        second["exclusions"] = [{
            "pattern": "second-only", "match": "contains", "applies_to": "folders", "ignore_case": False,
        }]
        plan = copy.deepcopy(self.plan)
        plan["pairs"] = [first, second]
        self.view.set_plan(plan)
        pair_editor = self.view.folders_page

        pair_editor.exclusion_pair_combo.setCurrentIndex(1)
        self.assertEqual(1, pair_editor.pairs_list.currentRow())
        self.assertEqual("second-only", pair_editor.exclusions_table.cellWidget(0, 1).text())
        pair_editor.exclusion_pair_combo.setCurrentIndex(0)
        self.assertEqual("first-only", pair_editor.exclusions_table.cellWidget(0, 1).text())

    def test_exclusions_have_a_separate_resizable_table(self):
        """Shows rules in their own tab and uses an interactive header."""
        self.assertEqual("Filters", self.view.settings_tabs.tabText(1))
        self.assertEqual(5, self.view.folders_page.exclusions_table.columnCount())
        self.assertEqual(
            QtWidgets.QHeaderView.ResizeMode.Interactive,
            self.view.folders_page.exclusions_table.horizontalHeader().sectionResizeMode(0),
        )
        self.assertEqual("Sync Plan", self.view.plan_heading.layout().itemAt(0).widget().text())

    def test_exclusion_columns_fill_the_table_and_ignore_case_is_centered(self):
        """Fills spare table width while keeping each section interactive."""
        self.view.settings_tabs.setCurrentWidget(self.view.exclusions_page)
        self.view.resize(1400, 850)
        self.view.show()
        self.application.processEvents()
        table = self.view.folders_page.exclusions_table
        self.view.folders_page._fill_exclusions_table()
        self.assertGreaterEqual(sum(table.columnWidth(i) for i in range(5)), table.viewport().width() - 2)
        self.assertEqual(
            QtWidgets.QHeaderView.ResizeMode.Interactive,
            table.horizontalHeader().sectionResizeMode(4),
        )
        self.view.folders_page._add_exclusion_row()
        checkbox_cell = table.cellWidget(0, 4)
        alignment = checkbox_cell.layout().itemAt(0).alignment()
        self.assertTrue(alignment & qt_enum(QtCore.Qt, "AlignmentFlag", "AlignCenter"))

    def test_custom_variables_roundtrip_through_the_editor(self):
        """Keeps custom values attached to the sync plan when edited in the tab."""
        self.view.variables_table.insertRow(0)
        self.view.variables_table.setItem(0, 0, QtWidgets.QTableWidgetItem("{backup-root}"))
        self.view.variables_table.setItem(0, 1, QtWidgets.QTableWidgetItem("/backups"))
        self.assertEqual({"{backup-root}": "/backups"}, self.view.get_plan()["variables"])

    def test_environment_variable_browser_shows_descriptions_and_row_copy_menu(self):
        """Shows descriptions in the table and keeps token copy in the RMB menu."""
        opened_dialogs = []

        def return_without_blocking(dialog):
            opened_dialogs.append(dialog)
            return 0

        with mock.patch.object(QtWidgets.QDialog, "exec", return_without_blocking):
            self.view.show_environment_variables({
                "{plan-dir}": "C:/Plans",
                "{sync-file-parent}": "C:/Actions",
            })
        dialog = opened_dialogs[0]
        table = dialog.findChild(QtWidgets.QTableWidget)
        self.assertEqual(
            ["Token", "Current value", "Description"],
            [table.horizontalHeaderItem(column).text() for column in range(3)],
        )
        plan_row = next(
            row for row in range(table.rowCount()) if table.item(row, 0).text() == "{plan-dir}"
        )
        self.assertIn("active .syncplan", table.item(plan_row, 2).text())
        self.assertEqual(
            qt_enum(QtCore.Qt, "ContextMenuPolicy", "CustomContextMenu"),
            table.contextMenuPolicy(),
        )
        button_texts = [button.text() for button in dialog.findChildren(QtWidgets.QPushButton)]
        self.assertNotIn("Copy Token", button_texts)

    def test_new_variables_use_braced_tokens_and_query_controls(self):
        """Starts with a braced token, serializes Query, and runs the row query."""
        self.view._add_variable_row()
        self.assertEqual("{variable-name}", self.view.variables_table.item(0, 0).text())
        run_button = self.view.variables_table.cellWidget(0, 3).findChild(QtWidgets.QPushButton)
        self.assertFalse(run_button.isEnabled())
        self.view.variables_table.item(0, 0).setText("{backup-root}")
        query = self.view.variables_table.cellWidget(0, 2).findChild(QtWidgets.QCheckBox)
        query.setChecked(True)
        self.assertTrue(run_button.isEnabled())
        self.view.variables_table.item(0, 1).setText('join(env["home-dir"], "Backups")')
        self.assertEqual(
            {"{backup-root}": {"value": 'join(env["home-dir"], "Backups")', "query": True}},
            self.view.get_plan()["variables"],
        )
        tested_variables = []
        self.view.variable_test_requested.connect(tested_variables.append)
        run_button.click()
        self.assertEqual(["{backup-root}"], tested_variables)

    def test_path_feedback_buttons_report_the_current_template(self):
        """Adds a dollar button beside each path editor and reports its value."""
        requests = []
        self.view.path_feedback_requested.connect(
            lambda label, path: requests.append((label, path))
        )
        self.view.folders_page.source_path.setText("{source-root}/scene")
        button = next(
            button for button in self.view.folders_page.findChildren(QtWidgets.QPushButton)
            if button.text() == "$"
        )
        button.click()
        self.assertEqual([("Source", "{source-root}/scene")], requests)

    def test_browse_and_variable_controls_use_bundled_icons(self):
        """Loads copied interface icons from SyncEngine's local resources."""
        buttons = self.view.folders_page.findChildren(QtWidgets.QPushButton)
        browse_button = next(button for button in buttons if button.toolTip() == "Browse for a folder")
        self.assertFalse(browse_button.icon().isNull())
        self.assertFalse(self.view.add_variable_button.icon().isNull())
        self.assertFalse(self.view.remove_variable_button.icon().isNull())

    def test_busy_disables_edits_and_keeps_cancel_available(self):
        """Allows cancellation throughout a countdown and background operation."""
        self.view.set_busy(True)
        self.view.set_countdown(7)
        self.assertEqual(False, self.view.folders_page.source_path.isEnabled())
        self.assertEqual(False, self.view.sync_button.isEnabled())
        self.assertEqual(True, self.view.cancel_button.isEnabled())
        self.assertEqual(False, self.view.cancel_button.isHidden())
        self.assertEqual("Automatic sync in 7s", self.view.progress_title.text())
        self.view.set_busy(False)
        self.assertEqual(True, self.view.folders_page.source_path.isEnabled())

    def test_countdown_status_does_not_hide_preview_scan_progress(self):
        """Keeps the indeterminate scan bar visible while countdown text updates."""
        self.view.update_progress({"phase": "scan", "scanned_entries": 24})
        scan_title = self.view.progress_title.text()
        self.view.set_countdown(6, preview_loading=True)
        self.assertEqual((0, 0), (self.view.progress_bar.minimum(), self.view.progress_bar.maximum()))
        self.assertEqual(scan_title, self.view.progress_title.text())
        self.assertIn("24 items scanned", self.view.status_label.text())
        self.assertIn("6s", self.view.status_label.text())

    def test_preview_shows_every_item_with_reason_color_and_full_width(self):
        """Shows all rows, explicit exclusion reasons, subtle status colors, and full width."""
        self.view.resize(1120, 850)
        self.view.show()
        self.application.processEvents()
        operations = [{"kind": "delete", "pair_id": "test-pair", "relative_path": f"file_{index}", "size": 4}
                      for index in range(2005)]
        unchanged = [{
            "kind": "preserved", "pair_id": "test-pair", "relative_path": "keep.txt", "size": 12,
            "reason": "Add-only mode preserves the existing destination file.",
        }]
        skipped = [{
            "kind": "skipped", "pair_id": "test-pair", "relative_path": "private.txt", "entry_type": "file",
            "reason": "File name exactly matches 'private.txt'.", "side": "source",
        }]
        self.view.set_preview({
            "operations": operations, "unchanged": unchanged, "skipped": skipped,
            "summary": {"delete": 2005, "bytes": 0, "skipped": 1}, "warnings": [],
        })
        self.assertEqual(2007, self.view.preview_table.rowCount())
        self.assertIn("2,005 removed", self.view.preview_summary.text())
        self.assertEqual("First pair", self.view.preview_table.item(0, 1).text())
        self.assertIn("Mirror mode", self.view.preview_table.item(0, 3).text())
        self.assertEqual("Excluded by rule: File name exactly matches 'private.txt'. (source)",
                         self.view.preview_table.item(2006, 3).text())
        self.assertEqual("#3a3026", self.view.preview_table.item(0, 0).background().color().name())
        table = self.view.preview_table
        self.assertLessEqual(
            abs(sum(table.columnWidth(index) for index in range(6)) - table.viewport().width()), 2
        )
        self.view.resize(820, 660)
        self.application.processEvents()
        self.assertLessEqual(
            abs(sum(table.columnWidth(index) for index in range(6)) - table.viewport().width()), 2
        )
        prior_size_width = table.columnWidth(4)
        table.setColumnWidth(4, max(60, prior_size_width - 20))
        self.assertEqual(max(60, prior_size_width - 20), table.columnWidth(4))
        self.assertLessEqual(
            abs(sum(table.columnWidth(index) for index in range(6)) - table.viewport().width()), 2
        )

    def test_folder_tree_preview_preserves_hierarchy_and_action_colors(self):
        """Separates equal filenames by target path and colors planned outcomes."""
        pairs = [
            dict(self.plan["pairs"][0], source="C:/source", destination="D:/backup"),
            {
                **dict(self.plan["pairs"][0]), "id": "second-pair", "name": "Second pair",
                "source": "E:/source", "destination": "F:/backup",
            },
        ]
        self.view.set_preview({
            "plan": {"pairs": pairs},
            "operations": [
                {"kind": "copy", "pair_id": "test-pair", "relative_path": "images/logo.png", "size": 64},
                {"kind": "copy", "pair_id": "test-pair", "relative_path": "archive/logo.png", "size": 128},
                {"kind": "update", "pair_id": "second-pair", "relative_path": "images/logo.png", "size": 256},
                {"kind": "conflict", "pair_id": "second-pair", "target_root": "destination",
                 "relative_path": "shared/scene.ma", "size": 512},
            ],
            "unchanged": [],
            "skipped": [{
                "kind": "skipped", "pair_id": "test-pair", "relative_path": "private/notes.txt",
                "entry_type": "file", "reason": "Name contains 'private'.", "side": "source",
            }],
            "summary": {"copy": 2, "update": 1, "conflict": 1, "skipped": 1, "bytes": 960},
            "warnings": [],
        })
        self.view.preview_mode_combo.setCurrentIndex(1)
        tree = self.view.preview_tree
        self.assertEqual(3, tree.topLevelItemCount())
        roots = [tree.topLevelItem(index) for index in range(tree.topLevelItemCount())]
        first_backup = next(item for item in roots if "Backup folder:" in item.toolTip(0)
                            and item.text(0).startswith("First pair"))
        first_source = next(item for item in roots if "Source folder:" in item.toolTip(0)
                            and item.text(0).startswith("First pair"))
        second_backup = next(item for item in roots if "Backup folder:" in item.toolTip(0)
                             and item.text(0).startswith("Second pair"))
        self.assertNotIn("Backup:", first_backup.text(0))
        self.assertFalse(first_backup.icon(0).isNull())
        images_item = next(first_backup.child(index) for index in range(first_backup.childCount())
                           if first_backup.child(index).text(0) == "images")
        archive_item = next(first_backup.child(index) for index in range(first_backup.childCount())
                            if first_backup.child(index).text(0) == "archive")
        self.assertEqual("logo.png", images_item.child(0).text(0))
        self.assertEqual("logo.png", archive_item.child(0).text(0))
        self.assertFalse(images_item.icon(0).isNull())
        self.assertFalse(images_item.child(0).icon(0).isNull())
        self.assertEqual("#26372c", images_item.child(0).background(1).color().name())
        self.assertTrue(any(first_source.child(index).text(0) == "private"
                            for index in range(first_source.childCount())))
        self.assertTrue(any(second_backup.child(index).text(0) == "images"
                            for index in range(second_backup.childCount())))
        conflict_folder = next(second_backup.child(index) for index in range(second_backup.childCount())
                               if second_backup.child(index).text(0) == "shared")
        conflict_item = conflict_folder.child(0)
        self.assertEqual("#402b2b", conflict_item.background(1).color().name())
        tree.setCurrentItem(conflict_item)
        self.assertTrue(self.view.use_source_button.isEnabled())
        resolutions = []
        self.view.conflict_resolution_requested.connect(
            lambda pair_id, payload: resolutions.append((pair_id, payload))
        )
        self.view._resolve_selected_conflict("source")
        self.assertEqual([("second-pair", "shared/scene.ma\x1fsource")], resolutions)

    def test_progress_maps_worker_fields_and_resets_countdown_format(self):
        """Presents byte progress, counts, and current-file progress accurately."""
        self.view.set_countdown(5)
        self.view.update_progress({
            "phase": "copy", "completed_bytes": 1024, "total_bytes": 2048,
            "completed_operations": 1, "total_operations": 3, "current_file_bytes": 256,
            "current_file_total": 1024, "speed_bytes_per_second": 1024,
            "elapsed_seconds": 4, "eta_seconds": 1, "current_path": "example_file",
        })
        self.assertEqual(483, self.view.progress_bar.value())
        self.assertEqual(250, self.view.file_progress.value())
        self.assertEqual("%p%", self.view.progress_bar.format())
        self.assertEqual("1 / 3 changes", self.view.operation_count.text())
        self.assertEqual("1.0 KiB / 2.0 KiB", self.view.bytes_label.text())

    def test_per_file_progress_updates_both_preview_modes(self):
        """Uses the same active-file progress in the table and hierarchy."""
        source = "C:/source/shot.ma"
        destination = "D:/backup/shot.ma"
        self.view.set_preview({
            "plan": {"pairs": [dict(self.plan["pairs"][0], source="C:/source", destination="D:/backup")]},
            "operations": [{
                "kind": "copy", "pair_id": "test-pair", "relative_path": "shot.ma",
                "source": source, "destination": destination, "size": 100,
            }],
            "unchanged": [], "skipped": [], "summary": {"copy": 1}, "warnings": [],
        })
        self.view.update_progress({
            "phase": "copy", "current_path": source,
            "current_file_bytes": 35, "current_file_total": 100,
        })
        table_index = self.view.preview_table.model().index(0, 5)
        self.assertEqual(350, table_index.data(qt_enum(QtCore.Qt, "ItemDataRole", "UserRole")))
        self.view.preview_mode_combo.setCurrentIndex(1)
        tree_item = self.view.preview_tree.topLevelItem(0).child(0)
        self.assertEqual(
            350,
            tree_item.data(4, qt_enum(QtCore.Qt, "ItemDataRole", "UserRole")),
        )

    def test_completed_sync_updates_action_labels_and_finishes_row_bars(self):
        """Shows the completed operation in both views and keeps it at 100 percent."""
        pair = dict(self.plan["pairs"][0], source="C:/source", destination="D:/backup")
        operations = [
            {
                "kind": "copy", "pair_id": "test-pair", "relative_path": "new.txt",
                "source": "C:/source/new.txt", "destination": "D:/backup/new.txt",
            },
            {
                "kind": "update", "pair_id": "test-pair", "relative_path": "updated.txt",
                "source": "C:/source/updated.txt", "destination": "D:/backup/updated.txt",
            },
            {
                "kind": "delete", "pair_id": "test-pair", "relative_path": "removed.txt",
                "destination": "D:/backup/removed.txt",
            },
        ]
        self.view.set_preview({
            "plan": {"pairs": [pair]}, "operations": operations, "unchanged": [], "skipped": [],
            "summary": {"copy": 1, "update": 1, "delete": 1}, "warnings": [],
        })
        completion_messages = (
            ("copy", "D:/backup/new.txt", "Completed copy: new.txt"),
            ("copy", "D:/backup/updated.txt", "Completed update: updated.txt"),
            ("delete", "D:/backup/removed.txt", "Removed removed.txt"),
        )
        for phase, path, message in completion_messages:
            self.view.update_progress({"phase": phase, "current_path": path, "message": message})

        expected_labels = ["Copied", "Updated", "Removed"]
        self.assertEqual(
            expected_labels,
            [self.view.preview_table.item(row, 0).text() for row in range(3)],
        )
        self.assertEqual(
            [1000, 1000, 1000],
            [self.view.preview_table.item(row, 5).data(self.view._data_role) for row in range(3)],
        )
        for operation, label in zip(operations, expected_labels):
            tree_item = self.view._preview_tree_items[(
                "test-pair", "destination", operation["relative_path"]
            )]
            self.assertEqual(label, tree_item.text(1))
            self.assertEqual(1000, tree_item.data(4, self.view._data_role))
        self.view.set_preview_preferences(False, 1)
        rebuilt_item = self.view._preview_tree_items[("test-pair", "destination", "new.txt")]
        self.assertEqual("Copied", rebuilt_item.text(1))
        self.assertEqual(1000, rebuilt_item.data(4, self.view._data_role))
        self.assertEqual("99%", self.view.preview_table.itemDelegateForColumn(5)._percentage_text(999))
        self.assertEqual("100%", self.view.preview_table.itemDelegateForColumn(5)._percentage_text(1000))

    def test_preview_path_preferences_and_plan_view_selection(self):
        """Shows path elision and retains the plan's selected preview mode."""
        self.view.set_preview_preferences(False, 1)
        self.view.preview_mode_combo.setCurrentIndex(1)
        self.view.set_preview({
            "plan": {"pairs": [dict(self.plan["pairs"][0], destination="D:/Archive/Work/Backup")]},
            "operations": [{
                "kind": "copy", "pair_id": "test-pair", "relative_path": "shot.ma", "size": 1,
            }],
            "unchanged": [], "skipped": [], "summary": {"copy": 1}, "warnings": [],
        })
        self.assertEqual("tree", self.view.preview_mode_combo.currentData())
        self.assertTrue(self.view.preview_tree.topLevelItem(0).text(0).endswith(".../Backup"))
        self.assertEqual("tree", self.view.get_plan()["preview_view"])
        self.view.set_preview_preferences(True, 2)
        self.assertTrue(
            self.view.preview_tree.topLevelItem(0).text(0).endswith("D:/Archive/Work/Backup")
        )

    def test_preview_view_is_restored_from_each_plan(self):
        """Keeps different preview modes independently with each plan."""
        self.view.preview_mode_combo.setCurrentIndex(1)
        tree_plan = self.view.get_plan()
        self.assertEqual("tree", tree_plan["preview_view"])
        table_plan = copy.deepcopy(self.plan)
        table_plan["id"] = "second-plan"
        self.view.set_plan(table_plan)
        self.assertEqual("table", self.view.preview_mode_combo.currentData())
        self.view.set_plan(tree_plan)
        self.assertEqual("tree", self.view.preview_mode_combo.currentData())

    def test_both_preview_modes_fit_columns_to_available_width(self):
        """Uses the full viewport width in both interactive preview layouts."""
        self.view.resize(1120, 850)
        self.view.show()
        self.application.processEvents()
        self.view._fill_preview_table()
        self.view._fill_preview_tree_columns()
        for widget in (self.view.preview_table, self.view.preview_tree):
            total_width = sum(widget.columnWidth(index) for index in range(widget.columnCount()))
            self.assertLessEqual(abs(total_width - widget.viewport().width()), 2)
        self.view.preview_tree.setColumnWidth(1, self.view.preview_tree.columnWidth(1) + 30)
        self.application.processEvents()
        tree_width = sum(
            self.view.preview_tree.columnWidth(index)
            for index in range(self.view.preview_tree.columnCount())
        )
        self.assertLessEqual(abs(tree_width - self.view.preview_tree.viewport().width()), 2)

    def test_preview_size_column_is_interactive_and_kept_compact(self):
        """Keeps Size content-sized while allowing a smaller manual width."""
        self.view.set_preview({
            "plan": {"pairs": [self.plan["pairs"][0]]},
            "operations": [{"kind": "copy", "pair_id": "test-pair", "relative_path": "a.txt", "size": 1}],
            "unchanged": [], "skipped": [], "summary": {"copy": 1}, "warnings": [],
        })
        header = self.view.preview_table.horizontalHeader()
        self.assertEqual(QtWidgets.QHeaderView.ResizeMode.Interactive, header.sectionResizeMode(4))
        self.view.preview_table.setColumnWidth(4, 48)
        self.view._fill_preview_table(resized_section=4)
        self.assertEqual(48, self.view.preview_table.columnWidth(4))

    def test_preview_generation_refreshes_live_rows_and_keeps_exclusions_visible(self):
        """Shows discovered paths as the scan runs and preserves filtered status."""
        self.view.begin_preview_generation()
        self.view.update_preview_scan_item({
            "pair_id": "test-pair", "pair_name": "First pair", "relative_path": "private.txt",
            "scan_side": "source", "kind": "skipped", "reason": "Excluded by rule: private prefix.",
        })
        self.assertEqual(1, self.view.preview_table.rowCount())
        self.assertEqual("Skipped", self.view.preview_table.item(0, 0).text())
        self.assertIn("Excluded by rule", self.view.preview_table.item(0, 3).text())

        self.view.update_preview_scan_item({
            "pair_id": "test-pair", "pair_name": "First pair", "relative_path": "private.txt",
            "scan_side": "backup", "kind": "scanning", "reason": "Scanning backup contents…",
        })

        self.assertEqual("Skipped", self.view.preview_table.item(0, 0).text())

    def test_automatic_confirmation_dialog_is_non_modal(self):
        """Leaves the preview window interactive while the user decides."""
        decisions = []
        self.view.confirm_automatic_run("Start this action?", decisions.append)
        dialog = self.view._automatic_confirmation_dialog
        self.assertIsNotNone(dialog)
        self.assertFalse(dialog.isModal())
        yes = qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
        dialog.button(yes).click()
        self.assertEqual([True], decisions)

    def test_preview_generation_restores_selected_view_after_content_sizing(self):
        """Restores the chosen hierarchy and sizes all columns from preview content."""
        self.view.resize(1120, 850)
        self.view.show()
        self.application.processEvents()
        self.view.preview_mode_combo.setCurrentIndex(1)
        self.view.begin_preview_generation()
        self.view.update_preview_scan_item({
            "pair_id": "test-pair", "pair_name": "First pair", "relative_path": "shot.ma",
            "scan_side": "source", "kind": "scanning", "reason": "Scanning source contents…",
        })
        self.view.set_preview({
            "plan": {"pairs": [dict(self.plan["pairs"][0], destination="D:/Archive/Work/Backup")]},
            "operations": [{
                "kind": "copy", "pair_id": "test-pair", "relative_path": "shot.ma", "size": 1024,
            }],
            "unchanged": [], "skipped": [], "summary": {"copy": 1}, "warnings": [],
        })
        self.application.processEvents()
        self.assertEqual(1, self.view.preview_stack.currentIndex())
        file_item = self.view.preview_tree.topLevelItem(0).child(0)
        self.assertEqual("New File to Backup", file_item.text(1))
        total_width = sum(
            self.view.preview_tree.columnWidth(index)
            for index in range(self.view.preview_tree.columnCount())
        )
        self.assertLessEqual(abs(total_width - self.view.preview_tree.viewport().width()), 2)
        self.assertGreaterEqual(self.view.preview_tree.columnWidth(0), 150)

    def test_overall_progress_waits_for_mirror_removals(self):
        """Keep overall progress below completion while removals remain."""
        self.view.update_progress({
            "phase": "delete", "completed_bytes": 2048, "total_bytes": 2048,
            "completed_operations": 1, "total_operations": 3,
        })
        self.assertLess(self.view.progress_bar.value(), self.view.progress_bar.maximum())
        self.view.update_progress({
            "phase": "succeeded", "percent": 100, "completed_bytes": 2048, "total_bytes": 2048,
            "completed_operations": 3, "total_operations": 3,
        })
        self.assertEqual(self.view.progress_bar.maximum(), self.view.progress_bar.value())

    def test_preview_invalidation_preserves_active_editor(self):
        """Keeps the user in the active settings tab when stale rows are cleared."""
        self.view.settings_tabs.setCurrentWidget(self.view.folders_page)
        self.view.set_preview({"operations": [], "summary": {}, "warnings": []})
        self.assertEqual(self.view.folders_page, self.view.settings_tabs.currentWidget())

    def test_semantic_buttons_emit_one_signal(self):
        """Ensures clicks reach the controller without a checked-state argument."""
        requests = []
        self.view.sync_requested.connect(lambda: requests.append("sync"))
        self.view.sync_button.click()
        self.assertEqual(["sync"], requests)


if __name__ == "__main__":
    unittest.main()
