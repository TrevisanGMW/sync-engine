"""Connect backup plan editing, filesystem workers, and user feedback."""

import logging
import math
import os
import re
import time
from functools import partial

from sync_engine_cli import describe_preview, load_input
from sync_engine_logging import configure_logging
from sync_engine_model import (
    duplicate_plan, evaluate_plan_variable, get_available_environment_variables, new_plan,
    resolve_path_template, save_action, save_plan, validate_plan,
)
from sync_engine_qt import QtCore, QtWidgets, Slot, qt_enum
from sync_engine_style import padded_icon
from sync_engine_worker import BackupWorker


logger = logging.getLogger("sync_engine")


def _suggest_export_filename(plan_name, extension):
    """Builds a lowercase, filesystem-friendly export name from a sync plan.

    Args:
        plan_name (str): Display name of the sync plan.
        extension (str): Required filename extension including the dot.

    Returns:
        str: Sanitized filename with the requested extension.
    """
    base_name = re.sub(r"[^a-z0-9]+", "_", str(plan_name).casefold()).strip("_")
    return f"{base_name or 'sync_plan'}{extension}"


class SyncEngineController(QtCore.QObject):
    """Manage plans and cooperative background operations.

    Args:
        view (SyncEngineView): App window.
        store (PlanStore): User-local plan and preference storage.
        initial_plan (dict or None): Plan supplied on the command line.
        auto_run (bool): Whether to start an automatic countdown.
        countdown (int or None): Command line countdown override.
    """

    def __init__(self, view, store, initial_plan=None, auto_run=False, countdown=None):
        """Initialize controller state and bind the window's actions.

        Args:
            view (SyncEngineView): App window.
            store (PlanStore): User-local storage.
            initial_plan (dict or None): Plan supplied on the command line.
            auto_run (bool): Whether to begin an automatic countdown.
            countdown (int or None): Countdown override in seconds.
        """
        super().__init__(view)
        self.view = view
        self.store = store
        self.plan = None
        self.plan_file_path = None
        self.preview = None
        self.worker = None
        self.dirty = False
        self.state = "idle"
        self._run_after_preview = False
        self._automatic = False
        self._countdown_active = False
        self._scan_pass_counts = {}
        self._close_pending = False
        self._pending_preview = None
        self._pending_result = None
        self._pending_error = None
        self._started_at = 0.0
        self._transfer_started_at = None
        self._progress_data = {}
        self._operation_deadline = None
        self._last_live_log_message = None
        self._conflicts_seen = 0
        self._conflicts_resolved = 0
        self._conflict_resolutions = {}
        self._close_after_automatic = False
        self.log_path = configure_logging(store.logs_directory)
        self.countdown_timer = QtCore.QTimer(self)
        self.countdown_timer.setInterval(200)
        self.countdown_timer.timeout.connect(self._tick_countdown)
        self._connect_actions()
        preferences = store.read_preferences()
        self.view.set_preview_preferences(
            show_full_target_path=preferences.get("preview_show_full_target_path", False),
            path_depth=preferences.get("preview_target_path_depth", 2),
        )
        plans = store.list_plans()
        if initial_plan is not None:
            self.plan_file_path = initial_plan.get("_sync_file_path") or str(
                store.plans_directory / f"{initial_plan.get('id', '')}.syncplan"
            )
            self.plan = validate_plan(initial_plan)
        elif plans:
            selected_id = preferences.get("last_plan_id", plans[0]["id"])
            if not isinstance(selected_id, str) or selected_id not in {item["id"] for item in plans}:
                selected_id = plans[0]["id"]
            self.plan = store.load(selected_id)
            self.plan_file_path = str(store.plans_directory / f"{self.plan['id']}.syncplan")
        else:
            self.plan = new_plan()
            self.plan_file_path = str(store.save(self.plan))
        self.view.set_plans(store.list_plans(), self.plan["id"])
        self.view.set_plan(self.plan)
        self._close_after_automatic = bool(
            self.plan.get("automation", {}).get("close_after_run", False)
        )
        self.view.append_log(f"Session log: {self.log_path}")
        for warning in store.warnings:
            self.view.append_log(warning)
        geometry = preferences.get("geometry")
        if isinstance(geometry, str):
            try:
                self.view.restoreGeometry(QtCore.QByteArray.fromBase64(geometry.encode("ascii")))
            except (ValueError, UnicodeError):
                logger.warning("Ignored invalid saved window geometry")
        if auto_run:
            automation = self.plan["automation"]
            start_mode = automation.get("start_mode", "countdown")
            if start_mode == "confirm":
                QtCore.QTimer.singleShot(0, self._confirm_automatic_run)
            elif countdown is not None:
                self._automatic = True
                QtCore.QTimer.singleShot(0, partial(self.start_countdown, countdown))
            elif start_mode == "immediate":
                self._automatic = True
                QtCore.QTimer.singleShot(0, lambda: self._start_scan(run_after=True))
            else:
                self._automatic = True
                QtCore.QTimer.singleShot(
                    0, partial(self.start_countdown, automation["countdown_seconds"])
                )

    def _connect_actions(self):
        """Connect the view's semantic signals to controller actions."""
        connections = {
            "new_plan_requested": self._new_plan,
            "duplicate_plan_requested": self._duplicate_plan,
            "remove_plan_requested": self._remove_plan,
            "save_requested": self._save_plan,
            "import_requested": self._import_plan,
            "export_requested": self._export_plan,
            "export_action_requested": self._export_action,
            "preview_requested": self._request_preview,
            "sync_requested": self._request_sync,
            "cancel_requested": self.cancel,
            "plan_selected": self._select_plan,
            "plan_edited": self._plan_edited,
            "register_actions_requested": self._register_actions,
            "show_logs_requested": self._show_logs,
            "preferences_requested": self._show_preferences,
            "automatic_runs_requested": self._show_automatic_runs,
            "show_environment_variables_requested": self._show_environment_variables,
            "show_reports_requested": self._show_reports,
            "plan_context_requested": self._plan_context_action,
            "variable_test_requested": self._test_variable,
            "path_feedback_requested": self._inspect_path,
            "conflict_resolution_requested": self._resolve_conflict,
            "closing": self._closing,
        }
        for signal_name, callback in connections.items():
            getattr(self.view, signal_name).connect(callback)

    def _refresh_plans(self):
        """Refresh the sidebar while retaining the current plan selection."""
        self.view.set_plans(self.store.list_plans(), self.plan["id"])

    def _read_plan(self):
        """Validate and retrieve the currently edited plan.

        Returns:
            dict: Validated backup plan.
        """
        return validate_plan(self.view.get_plan())

    @Slot()
    def _plan_edited(self):
        """Invalidate the preview when editable settings change."""
        if self.state == "idle":
            self.dirty = True
            self.view.set_unsaved_changes(True)
            self._conflict_resolutions = {}
            self._conflicts_seen = 0
            self._conflicts_resolved = 0
            if self.preview is not None:
                self.preview = None
                self.view.set_preview({"operations": [], "summary": {}, "warnings": []})
            self.view.set_status("")

    def _save_plan(self, quiet=False):
        """Save this plan to user-local storage.

        Args:
            quiet (bool): Whether to suppress routine success feedback.

        Returns:
            bool: Whether saving succeeded.
        """
        try:
            self.plan = self._read_plan()
            self.plan_file_path = str(self.store.save(self.plan))
            self.dirty = False
            self.view.set_unsaved_changes(False)
            self._refresh_plans()
            if not quiet:
                self.view.set_status(f"Saved '{self.plan['name']}'.", "success")
            return True
        except Exception as error:
            self._show_failure(error)
            return False

    def _set_plan(self, plan):
        """Display another plan and clear the previous scan.

        Args:
            plan (dict): Plan to display.
        """
        self.plan = plan
        self.plan_file_path = str(self.store.plans_directory / f"{plan['id']}.syncplan")
        self.preview = None
        self.dirty = False
        self.view.set_unsaved_changes(False)
        self._automatic = False
        self._conflict_resolutions = {}
        self._conflicts_seen = 0
        self._conflicts_resolved = 0
        self.view.set_plan(plan)
        self.view.set_preview({"operations": [], "summary": {}, "warnings": []})
        self._refresh_plans()
        self.view.set_status(f"Editing '{plan['name']}'.")

    @Slot(str)
    def _select_plan(self, plan_id):
        """Select a saved plan, first preserving current edits.

        Args:
            plan_id (str): Stable plan identifier selected in the sidebar.
        """
        if self.state != "idle" or plan_id == self.plan["id"]:
            return
        if self.dirty and not self._save_plan(quiet=True):
            self._refresh_plans()
            return
        try:
            self._set_plan(self.store.load(plan_id))
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _new_plan(self):
        """Create and persist a new empty backup plan."""
        if self.dirty and not self._save_plan(quiet=True):
            return
        try:
            plan = new_plan()
            self.store.save(plan)
            self._set_plan(plan)
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _duplicate_plan(self):
        """Duplicate the current plan with independent identifiers."""
        if self.dirty and not self._save_plan(quiet=True):
            return
        try:
            plan = duplicate_plan(self._read_plan())
            self.store.save(plan)
            self._set_plan(plan)
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _remove_plan(self):
        """Remove one saved configuration after an explicit confirmation."""
        if not self.view.confirm(
            f"Remove the saved plan '{self.plan['name']}'?\n\nYour source and backup files are unaffected.",
            "Remove sync plan",
        ):
            return
        try:
            self.store.remove(self.plan["id"])
            plans = self.store.list_plans()
            if plans:
                self._set_plan(self.store.load(plans[0]["id"]))
            else:
                plan = new_plan()
                self.store.save(plan)
                self._set_plan(plan)
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _import_plan(self):
        """Import a plan or action as editable settings without running it."""
        if self.dirty and not self._save_plan(quiet=True):
            return
        path, unused_filter = QtWidgets.QFileDialog.getOpenFileName(
            self.view, "Import sync plan", "",
            "SyncEngine (*.syncplan *.syncrun *.relayplan *.relayrun);;JSON (*.json)",
        )
        if not path:
            return
        try:
            imported, unused_is_action = load_input(path)
            plan = duplicate_plan(imported)
            plan["name"] = imported["name"]
            plan["automation"]["allow_deletions"] = False
            self.store.save(plan)
            self._set_plan(plan)
            self.view.set_status(f"Imported '{plan['name']}'.", "success")
        except Exception as error:
            self._show_failure(error)

    def _choose_export(self, title, extension, plan_name):
        """Choose an export filename and confirm suffix-adjusted overwrites.

        Args:
            title (str): Save dialog title.
            extension (str): Required filename extension including the dot.
            plan_name (str): Sync plan name used to suggest a readable filename.

        Returns:
            str: Selected path or an empty string if canceled.
        """
        suggested_name = _suggest_export_filename(plan_name, extension)
        dialog = QtWidgets.QFileDialog(self.view, title)
        dialog.setOption(
            qt_enum(QtWidgets.QFileDialog, "Option", "DontUseNativeDialog"), True,
        )
        dialog.setOption(
            qt_enum(QtWidgets.QFileDialog, "Option", "DontConfirmOverwrite"), True,
        )
        dialog.setAcceptMode(qt_enum(QtWidgets.QFileDialog, "AcceptMode", "AcceptSave"))
        dialog.setFileMode(qt_enum(QtWidgets.QFileDialog, "FileMode", "AnyFile"))
        dialog.setNameFilter(f"SyncEngine (*{extension})")
        dialog.setDefaultSuffix(extension.lstrip("."))
        dialog.selectFile(suggested_name)
        dialog.setMinimumSize(640, 420)
        dialog.resize(860, 560)
        dialog.setSizeGripEnabled(True)
        execute_dialog = getattr(dialog, "exec", None) or dialog.exec_
        accepted = qt_enum(QtWidgets.QDialog, "DialogCode", "Accepted")
        if execute_dialog() != accepted:
            return ""
        selected_paths = dialog.selectedFiles()
        path = selected_paths[0] if selected_paths else ""
        if path and not path.lower().endswith(extension):
            path = f"{path}{extension}"
        if path and os.path.exists(path):
            if not self.view.confirm(f"Replace this file?\n{path}", "Replace export"):
                return ""
        return path

    @Slot()
    def _export_plan(self):
        """Export editable settings to a portable plan file."""
        try:
            plan = self._read_plan()
            path = self._choose_export("Export sync plan", ".syncplan", plan.get("name", ""))
            if path:
                save_plan(plan, path)
                self.view.set_status(f"Plan exported: {path}", "success")
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _export_action(self):
        """Export an automatic action with explicit mirror authorization."""
        try:
            plan = self._read_plan()
            allow_deletions = plan["automation"].get("allow_deletions", False)
            if (any(pair["mode"] in ("mirror", "two_way") for pair in plan["pairs"])
                    and not allow_deletions):
                allow_deletions = self.view.confirm(
                    "This action includes a mode that can remove files. Mirror removes destination extras; "
                    "two-way sync propagates deletions from either side.\n\n"
                    "Authorize automatic deletions for this action?",
                    "Authorize automatic deletions",
                )
                if not allow_deletions:
                    return
                plan["automation"]["allow_deletions"] = True
            path = self._choose_export(
                "Export automatic action", ".syncrun", plan.get("name", "")
            )
            if path:
                save_action(plan, path, allow_deletions=allow_deletions)
                self.view.set_status(f"Automatic action exported: {path}", "success")
        except Exception as error:
            self._show_failure(error)

    def start_countdown(self, seconds):
        """Starts preview generation and a cancelable automatic-run countdown.

        Args:
            seconds (int): Delay before synchronizing.
        """
        if self.state != "idle":
            return
        self._automatic = True
        self._countdown_active = True
        self._deadline = time.monotonic() + max(0, seconds)
        self._run_after_preview = False
        self.view.set_busy(True)
        logger.info("Automatic action armed; plan=%s countdown=%s", self.plan["name"], seconds)
        if seconds <= 0:
            self._countdown_active = False
            self._start_scan(run_after=True)
            return
        self.state = "countdown"
        self.view.set_countdown(seconds)
        self.countdown_timer.start()
        self._start_scan(run_after=False)

    def _confirm_automatic_run(self):
        """Scans immediately and presents a non-blocking automatic-run prompt."""
        self._automatic = False
        self._start_scan(run_after=False)
        if self.worker is None and self.preview is None:
            return

        def handle_decision(accepted):
            """Continues or declines the automatic run after the modeless prompt."""
            if accepted:
                self._automatic = True
                self._run_after_preview = True
                self.view.set_status(
                    "Confirmation received. Sync will start when the preview is ready."
                    if self.worker is not None else "Confirmation received.",
                )
                if self.preview is not None and self.worker is None:
                    self._execute_preview()
                return
            self._automatic = False
            self._run_after_preview = False
            if self.worker is not None:
                self.view.set_status(
                    "Automatic sync declined. Preview generation will finish without syncing.",
                    "warning",
                )
            else:
                self.view.set_status("Automatic sync was not started.", "warning")
            if self._close_after_automatic and self.worker is None:
                self.view.close()

        self.view.confirm_automatic_run(
            f"Start the automatic sync plan '{self.plan['name']}' now?",
            handle_decision,
            "Confirm Automatic Sync",
        )

    @Slot()
    def _tick_countdown(self):
        """Updates the countdown while the read-only preview is being prepared."""
        remaining = max(0, math.ceil(self._deadline - time.monotonic()))
        scanning = self.worker is not None
        self.view.set_countdown(remaining, preview_loading=scanning)
        if remaining == 0:
            self.countdown_timer.stop()
            self._countdown_active = False
            self._run_after_preview = True
            if self.preview is not None and self.worker is None:
                self._execute_preview()
            elif self.worker is not None:
                self.view.set_status(
                    "Countdown finished. Sync will start as soon as the preview is ready."
                )

    @Slot()
    def _request_preview(self):
        """Scan without modifying either folder."""
        self._automatic = False
        self._start_scan(run_after=False, preserve_conflicts=bool(self._conflict_resolutions))

    @Slot()
    def _request_sync(self):
        """Scan current settings before executing a manual sync."""
        self._automatic = False
        self._start_scan(run_after=True, preserve_conflicts=bool(self._conflict_resolutions))

    def _start_scan(self, run_after, preserve_conflicts=False):
        """Start a read-only background scan.

        Args:
            run_after (bool): Whether the completed scan should be executed.
        """
        countdown_scan = self._countdown_active and self.state == "countdown"
        if self.state != "idle" and not countdown_scan:
            return
        try:
            self.plan = self._read_plan()
            if not preserve_conflicts:
                self._conflicts_seen = 0
                self._conflicts_resolved = 0
            self._run_after_preview = run_after
            self.preview = None
            self.state = "scanning"
            self._scan_pass_counts = {}
            self._started_at = time.monotonic()
            timeout_seconds = self.plan.get("execution", {}).get("timeout_seconds", 0)
            self._operation_deadline = self._started_at + timeout_seconds if timeout_seconds else None
            self._last_live_log_message = None
            if run_after and self.plan.get("execution", {}).get("expand_log_while_syncing"):
                self.view.set_run_log_expanded(True)
            self._transfer_started_at = None
            self._progress_data = {}
            self.view.set_busy(True)
            self.view.begin_preview_generation()
            self.view.set_status("Generating preview…")
            self.view.update_progress({"phase": "scan", "scanned_files": 0, "elapsed_seconds": 0})
            self._start_worker(BackupWorker(
                self.plan, self.store.directory,
                sync_state=self.store.load_sync_state(self.plan["id"]),
                conflict_resolutions=self._conflict_resolutions,
                plan_file_path=self.plan_file_path,
                deadline=self._operation_deadline,
                parent=self,
            ))
        except Exception as error:
            self.state = "idle"
            self._countdown_active = False
            self.countdown_timer.stop()
            self.view.set_busy(False)
            self._show_failure(error)

    def _start_worker(self, worker):
        """Bind and start one filesystem worker.

        Args:
            worker (BackupWorker): Thread performing a scan or sync.
        """
        self.worker = worker
        self._pending_preview = None
        self._pending_result = None
        self._pending_error = None
        worker.progress.connect(self._progress)
        worker.preview_ready.connect(self._receive_preview)
        worker.result_ready.connect(self._receive_result)
        worker.failed.connect(self._receive_error)
        worker.finished.connect(self._worker_finished)
        worker.start()

    @Slot(object)
    def _receive_preview(self, preview):
        """Retain a scan result until its worker has fully stopped.

        Args:
            preview (dict): Completed preview.
        """
        self._pending_preview = preview

    @Slot(object)
    def _receive_result(self, result):
        """Retain a sync result until its worker has fully stopped.

        Args:
            result (dict): Completed execution result.
        """
        self._pending_result = result

    @Slot(str)
    def _receive_error(self, message):
        """Retain a worker failure for display after cleanup.

        Args:
            message (str): Error description.
        """
        self._pending_error = message

    @Slot()
    def _worker_finished(self):
        """Release the completed thread and continue its operation."""
        canceled = self.worker.cancel_event.is_set()
        self.worker.deleteLater()
        self.worker = None
        countdown_waiting = self._countdown_active
        self.state = "countdown" if countdown_waiting else "idle"
        self.view.set_busy(countdown_waiting)
        if self._close_pending:
            self.view.close()
            return
        if self._pending_error:
            self._countdown_active = False
            self.countdown_timer.stop()
            self.state = "idle"
            self.view.set_busy(False)
            was_automatic = self._automatic
            self._show_failure(self._pending_error)
            if was_automatic and self._close_after_automatic:
                QtCore.QTimer.singleShot(1500, partial(QtWidgets.QApplication.exit, 1))
        elif self._pending_result is not None:
            self._countdown_active = False
            self.countdown_timer.stop()
            self.state = "idle"
            self.view.set_busy(False)
            self._show_result(self._pending_result)
        elif self._pending_preview is not None:
            self._show_preview(self._pending_preview, canceled)

    def _show_preview(self, preview, canceled):
        """Present a preview and optionally proceed to execution.

        Args:
            preview (dict): Completed engine scan.
            canceled (bool): Whether cancellation arrived after the scan.
        """
        pair_names = {pair["id"]: pair["name"] for pair in self.plan["pairs"]}
        for operation in preview["operations"]:
            operation["pair_name"] = pair_names.get(operation["pair_id"], "Folder Pair")
        self.preview = preview
        self._conflicts_seen = max(
            self._conflicts_seen, int(preview.get("summary", {}).get("conflict", 0))
        )
        self.view.set_preview(preview)
        summary = describe_preview(preview)
        self.view.append_log(summary)
        logger.info("Preview completed; plan=%s %s", self.plan["name"], summary)
        for warning in preview["warnings"]:
            self.view.append_log(f"Warning: {warning}")
        self.view.set_status("Preview ready. " + summary)
        if self._countdown_active:
            remaining = max(0, math.ceil(self._deadline - time.monotonic()))
            self.view.set_status(f"Preview ready. Automatic sync starts in {remaining:,}s.")
        else:
            self.view.set_status("Preview ready. " + summary)
        if preview.get("summary", {}).get("conflict"):
            self.view.set_status(
                f"{preview['summary']['conflict']} two-way conflict(s) need a choice in the Preview tab.",
                "warning",
            )
            if self._run_after_preview:
                if self.plan.get("execution", {}).get("generate_report"):
                    self._write_sync_report({
                        "status": "blocked_conflicts", "copied": 0, "deleted": 0,
                        "bytes": 0, "errors": [], "warnings": [],
                    }, preview=preview)
                self._run_after_preview = False
                self._automatic = False
                return
        if canceled:
            self.view.set_status("Canceled before copying.", "warning")
        elif self._run_after_preview:
            self._execute_preview()

    def _execute_preview(self):
        """Authorize destructive changes, then start backup execution."""
        summary = self.preview["summary"]
        if summary.get("conflict"):
            self.view.set_status("Choose a version for every two-way conflict before syncing.", "warning")
            self.countdown_timer.stop()
            self._countdown_active = False
            self._run_after_preview = False
            self._automatic = False
            if self.state == "countdown":
                self.state = "idle"
                self.view.set_busy(False)
            return
        timeout_seconds = self.plan.get("execution", {}).get("timeout_seconds", 0)
        if self._automatic and timeout_seconds:
            self._operation_deadline = time.monotonic() + timeout_seconds
        has_deletions = bool(summary.get("delete", 0) or summary.get("rmdir", 0))
        allow_delete = False
        if has_deletions:
            if self._automatic:
                allow_delete = self.plan["automation"].get(
                    "allow_deletions", self.plan["automation"].get("allow_mirror", False)
                )
                if not allow_delete:
                    self.view.set_status("This automatic action has no deletion permission.", "error")
                    return
            else:
                allow_delete = self.view.confirm(
                    f"This sync will permanently remove {summary.get('delete', 0)} files "
                    f"({summary.get('delete_source', 0)} from the source side, "
                    f"{summary.get('delete_destination', 0)} from the backup side) and "
                    f"{summary.get('rmdir', 0)} destination folders.\n\n"
                    "Review the Preview tab before continuing. Proceed with this sync?",
                    "Confirm file removals",
                )
                if not allow_delete:
                    self.view.set_status("Sync canceled. Preview remains available.", "warning")
                    return
        self.state = "syncing"
        self._transfer_started_at = None
        self.view.set_busy(True)
        self.view.set_status("Synchronizing...")
        self.view.append_log(f"Started '{self.plan['name']}'.")
        self._start_worker(BackupWorker(
            self.plan, self.store.directory, preview=self.preview,
            allow_delete=allow_delete, deadline=self._operation_deadline, parent=self,
        ))
        if self.plan.get("execution", {}).get("expand_log_while_syncing"):
            self.view.set_run_log_expanded(True)

    @Slot(object)
    def _progress(self, data):
        """Enrich progress with transfer rate, elapsed time, and remaining time.

        Args:
            data (dict): Engine progress values.
        """
        self._progress_data.update(data)
        progress = dict(self._progress_data)
        if progress.get("phase") == "scan" and self.state == "scanning":
            pair_id = str(progress.get("pair_id", ""))
            scan_side = str(progress.get("scan_side", ""))
            if pair_id and scan_side:
                key = (pair_id, scan_side)
                self._scan_pass_counts[key] = max(
                    self._scan_pass_counts.get(key, 0),
                    int(progress.get("scanned_entries", 0)),
                )
            scanned_entries = sum(self._scan_pass_counts.values())
            progress["scanned_entries"] = scanned_entries
            pair_names = {pair["id"]: pair["name"] for pair in self.plan["pairs"]}
            if progress.get("relative_path"):
                live_item = dict(progress)
                live_item["pair_name"] = pair_names.get(pair_id, "Folder Pair")
                self.view.update_preview_scan_item(live_item)
        current_time = time.monotonic()
        elapsed = current_time - self._started_at
        progress["elapsed_seconds"] = elapsed
        if progress.get("phase") == "copy":
            if self._transfer_started_at is None:
                self._transfer_started_at = current_time
            transfer_elapsed = max(0.1, current_time - self._transfer_started_at)
            speed = progress.get("completed_bytes", 0) / transfer_elapsed
            progress["speed_bytes_per_second"] = speed
            remaining = max(0, progress.get("total_bytes", 0) - progress.get("completed_bytes", 0))
            progress["eta_seconds"] = remaining / speed if speed else None
        self.view.update_progress(progress)
        if progress.get("phase") == "scan" and self.state == "scanning":
            status = f"Generating preview · {progress.get('scanned_entries', 0):,} items scanned"
            if self._countdown_active:
                remaining = max(0, math.ceil(self._deadline - time.monotonic()))
                status += f" · Sync starts in {remaining:,}s"
            self.view.set_status(status)
        message = progress.get("message")
        if (progress.get("phase") in ("copy", "delete") and message
                and message != self._last_live_log_message):
            self._last_live_log_message = message
            self.view.append_log(message)

    def _show_result(self, result):
        """Display the final execution status and persistent log information.

        Args:
            result (dict): Engine execution result.
        """
        status = result["status"]
        if status == "succeeded" and result.get("sync_state"):
            try:
                self.store.save_sync_state(result["sync_state"])
            except (OSError, ValueError) as error:
                result.setdefault("warnings", []).append(
                    f"Files synchronized, but two-way history could not be saved: {error}"
                )
        display_status = "Timed out" if status == "timed_out" else status.capitalize()
        message = (
            f"{display_status}: {result.get('copied', 0)} copied, "
            f"{result.get('deleted', 0)} removed."
        )
        level = {"succeeded": "success", "cancelled": "warning"}.get(status, "error")
        self.view.set_status(message, level)
        self.view.append_log(message)
        logger.info("%s", message)
        for detail in result.get("errors", []) + result.get("warnings", []):
            self.view.append_log(detail)
        if self.plan.get("execution", {}).get("generate_report"):
            self._write_sync_report(result)
        final_progress = dict(self._progress_data)
        final_progress.update({
            "phase": status, "message": message,
            "elapsed_seconds": time.monotonic() - self._started_at,
        })
        if status == "succeeded":
            final_progress["percent"] = 100
            final_progress["completed_bytes"] = final_progress.get("total_bytes", result.get("bytes", 0))
            final_progress["completed_operations"] = final_progress.get("total_operations", 0)
        self.view.update_progress(final_progress)
        self.preview = None
        self._automatic = False
        if self._close_after_automatic:
            exit_code = 0 if status == "succeeded" else 3 if status == "cancelled" else 1
            QtCore.QTimer.singleShot(1500, partial(QtWidgets.QApplication.exit, exit_code))

    @Slot()
    def cancel(self):
        """Cancel countdown or request cancellation of a filesystem worker."""
        if self._countdown_active:
            was_automatic = self._automatic
            self.countdown_timer.stop()
            self._countdown_active = False
            self._automatic = False
            self._run_after_preview = False
            if self.worker is not None:
                self.worker.cancel()
                self.view.set_status("Stopping preview. Automatic sync canceled.", "warning")
            else:
                self.state = "idle"
                self.view.set_busy(False)
                self.view.set_status("Automatic sync canceled before syncing.", "warning")
                self.view.update_progress({
                    "phase": "cancelled", "percent": 0,
                    "message": "Automatic sync canceled before syncing.",
                    "completed_bytes": 0, "total_bytes": 0,
                    "completed_operations": 0, "total_operations": 0, "elapsed_seconds": 0,
                })
            logger.info("Automatic countdown canceled")
            if was_automatic and self._close_after_automatic:
                QtCore.QTimer.singleShot(500, partial(QtWidgets.QApplication.exit, 3))
        elif self.worker is not None:
            self.worker.cancel()
            self.view.set_status("Stopping safely...", "warning")

    def _show_failure(self, error):
        """Show concise status feedback and retain a detailed session log.

        Args:
            error (Exception or str): Error description.
        """
        message = str(error)
        self.view.set_status(message, "error")
        self.view.append_log(f"Failed: {message}")
        logger.error("%s", message, exc_info=isinstance(error, Exception))
        failure_progress = dict(self._progress_data)
        failure_progress.update({"phase": "failed", "message": message})
        self.view.update_progress(failure_progress)
        self._automatic = False

    @Slot()
    def _register_actions(self):
        """Register plan/action extensions for the current Windows user."""
        try:
            from sync_engine_startup import register_file_associations

            register_file_associations()
            self.view.set_status(
                "Registered .syncplan and .syncrun, with legacy file support, for this Windows user.", "success"
            )
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _show_logs(self):
        """Open the existing session log directory using the system browser."""
        from sync_engine_qt import QtGui

        if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.store.logs_directory))):
            self.view.set_status("Could not open the log folder.", "warning")

    @Slot()
    def _show_environment_variables(self):
        """Opens the built-in variable browser for the current plan context."""
        try:
            plan = self._read_plan()
            values = get_available_environment_variables(plan, self.plan_file_path)
            self.view.show_environment_variables(values)
        except Exception as error:
            self._show_failure(error)

    @Slot(str)
    def _test_variable(self, name):
        """Evaluates the selected custom variable without saving the plan."""
        try:
            plan = self._read_plan()
            result = evaluate_plan_variable(plan, name, self.plan_file_path)
            message = f"{name} = {result}"
            self.view.set_status(message, "success")
            self.view.append_log(f"Variable query: {message}")
        except Exception as error:
            self.view.set_status(str(error), "error")
            self.view.append_log(f"Variable query failed: {error}")

    @Slot(str, str)
    def _inspect_path(self, label, path_template):
        """Shows the resolved path and current filesystem state for one field."""
        lines = ["Path template:", str(path_template or "<empty>"), ""]
        try:
            plan = self._read_plan()
            resolved_path = resolve_path_template(plan, path_template, self.plan_file_path)
            exists = os.path.exists(resolved_path)
            is_directory = os.path.isdir(resolved_path)
            is_file = os.path.isfile(resolved_path)
            lines.extend([
                "Resolved path:", resolved_path, "",
                f"Exists: {exists}",
                f"Directory: {is_directory}",
                f"File: {is_file}",
            ])
            self.view.set_status(
                f"{label} path resolves to an existing location." if exists
                else f"{label} path resolves, but does not exist yet.",
                "success" if exists else "warning",
            )
            self.view.append_log(f"Inspected {label.lower()} path: {resolved_path} (exists: {exists})")
        except Exception as error:
            lines.extend(["Resolved path: unavailable", "", f"Resolution error: {error}"])
            self.view.set_status(f"Could not resolve the {label.lower()} path: {error}", "warning")
            self.view.append_log(f"Path inspection failed for {label.lower()}: {error}")
        self.view.show_path_feedback(f"{label} Path Details", lines)

    @Slot()
    def _show_reports(self):
        """Opens the report folder, creating it when no sync has run yet."""
        from sync_engine_qt import QtGui

        directory = self.store.logs_directory / "reports"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(directory))):
                self.view.set_status(f"Reports are saved in {directory}", "warning")
        except OSError as error:
            self._show_failure(error)

    def _write_sync_report(self, result, preview=None):
        """Writes and logs the report for a completed or conflict-blocked run."""
        from sync_engine_report import write_sync_report

        try:
            report_path = write_sync_report(
                self.plan, self._report_preview(preview), result,
                self.store.logs_directory / "reports",
                elapsed_seconds=max(0, time.monotonic() - self._started_at),
                log_path=str(self.log_path),
            )
            self.view.append_log(f"Sync report saved: {report_path}")
            logger.info("Sync report saved: %s", report_path)
        except (OSError, ValueError) as error:
            self.view.append_log(f"Unable to write sync report: {error}")
            self.view.set_status(f"Sync finished, but its report could not be written: {error}", "warning")

    def _report_preview(self, preview=None):
        """Returns report data that retains conflicts resolved during review."""
        report_preview = preview if preview is not None else self.preview
        if report_preview is None:
            return None
        report_preview = dict(report_preview)
        summary = dict(report_preview.get("summary", {}))
        summary["conflict"] = max(int(summary.get("conflict", 0)), self._conflicts_seen)
        summary["conflict_resolved"] = self._conflicts_resolved
        report_preview["summary"] = summary
        return report_preview

    @Slot(str, str)
    def _resolve_conflict(self, pair_id, payload):
        """Stores a user-selected winner and refreshes the read-only preview."""
        try:
            relative_path, direction = payload.rsplit("\x1f", 1)
            if direction not in ("source", "destination"):
                raise ValueError("Choose either the source or backup version.")
            key = os.path.normcase(relative_path).replace("\\", "/")
            operation = next((item for item in self.preview.get("operations", [])
                              if item.get("kind") == "conflict" and item.get("pair_id") == pair_id
                              and os.path.normcase(item.get("relative_path", "")).replace("\\", "/") == key), None)
            if operation is None:
                raise ValueError("That conflict preview is stale. Run Preview again before choosing a version.")
            pair_resolutions = self._conflict_resolutions.setdefault(pair_id, {})
            if key not in pair_resolutions:
                self._conflicts_resolved += 1
            pair_resolutions[key] = {
                "direction": direction,
                "source_signature": operation.get("source_signature"),
                "destination_signature": operation.get("destination_signature"),
                "source_hash": operation.get("conflict_source_hash"),
                "destination_hash": operation.get("conflict_destination_hash"),
            }
            self.view.append_log(f"Conflict choice: {relative_path} uses the {direction} version.")
            self._start_scan(run_after=False, preserve_conflicts=True)
        except Exception as error:
            self._show_failure(error)

    @Slot(str, str)
    def _plan_context_action(self, plan_id, action_name):
        """Runs a context-menu command against the plan under the pointer."""
        if self.state != "idle":
            return
        if plan_id != self.plan.get("id"):
            self._select_plan(plan_id)
            if plan_id != self.plan.get("id"):
                return
        actions = {
            "duplicate": self._duplicate_plan, "export": self._export_plan,
            "export_action": self._export_action, "import": self._import_plan,
            "preview": self._request_preview, "sync": self._request_sync,
            "delete": self._remove_plan,
        }
        callback = actions.get(action_name)
        if callback:
            callback()

    @Slot()
    def _show_preferences(self):
        """Presents storage and preview display preferences."""
        from sync_engine_model import default_data_directory
        from sync_engine_qt import QtGui

        dialog = QtWidgets.QDialog(self.view)
        dialog.setWindowTitle("SyncEngine Preferences")
        dialog.setMinimumWidth(620)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel(
            "Choose where SyncEngine keeps editable plans, sync history, automatic actions, and logs. "
            "Changing this location copies existing plans and history; the old folder is left intact."
        ))
        row = QtWidgets.QHBoxLayout()
        path_edit = QtWidgets.QLineEdit(str(self.store.directory))
        browse_button = QtWidgets.QPushButton()
        browse_button.setIcon(padded_icon("icons/folder_open.svg"))
        if browse_button.icon().isNull():
            browse_button.setIcon(QtWidgets.QApplication.style().standardIcon(
                qt_enum(QtWidgets.QStyle, "StandardPixmap", "SP_DirOpenIcon")
            ))
        browse_button.setIconSize(QtCore.QSize(24, 24))
        browse_button.setStyleSheet("QPushButton { padding: 0px; }")
        browse_button.setFixedSize(36, 34)
        browse_button.setToolTip("Browse for a project storage folder")
        row.addWidget(path_edit, 1)
        row.addWidget(browse_button)
        layout.addLayout(row)

        def browse():
            selected = QtWidgets.QFileDialog.getExistingDirectory(
                dialog, "Choose project storage folder", path_edit.text()
            )
            if selected:
                path_edit.setText(selected)

        browse_button.clicked.connect(browse)
        default_button = QtWidgets.QPushButton("Use tool default")
        default_button.clicked.connect(lambda: path_edit.setText(str(default_data_directory())))
        layout.addWidget(default_button, 0, qt_enum(QtCore.Qt, "AlignmentFlag", "AlignLeft"))

        preview_group = QtWidgets.QGroupBox("Preview Display")
        preview_form = QtWidgets.QFormLayout(preview_group)
        show_full_path = QtWidgets.QCheckBox("Show the full target-folder path")
        path_depth = QtWidgets.QSpinBox()
        path_depth.setRange(1, 99)
        path_depth.setSuffix(" folders")
        path_depth.setToolTip(
            "When the full path is off, show this many trailing folder names in each tree root."
        )
        preferences = self.store.read_preferences()
        show_full_path.setChecked(bool(preferences.get("preview_show_full_target_path", False)))
        try:
            path_depth.setValue(max(1, min(99, int(preferences.get("preview_target_path_depth", 2)))))
        except (TypeError, ValueError):
            path_depth.setValue(2)
        path_depth.setEnabled(not show_full_path.isChecked())
        show_full_path.toggled.connect(lambda checked: path_depth.setEnabled(not checked))
        preview_form.addRow("Target Path", show_full_path)
        preview_form.addRow("Maximum Folders to Show", path_depth)
        layout.addWidget(preview_group)

        buttons = QtWidgets.QDialogButtonBox(
            qt_enum(QtWidgets.QDialogButtonBox, "StandardButton", "Save")
            | qt_enum(QtWidgets.QDialogButtonBox, "StandardButton", "Cancel")
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog_runner = getattr(dialog, "exec", None) or dialog.exec_
        if dialog_runner() != qt_enum(QtWidgets.QDialog, "DialogCode", "Accepted"):
            return
        try:
            directory = self.store.change_projects_directory(path_edit.text())
            preferences["preview_show_full_target_path"] = show_full_path.isChecked()
            preferences["preview_target_path_depth"] = path_depth.value()
            self.store.write_preferences(preferences)
            self.view.set_preview_preferences(
                show_full_target_path=preferences["preview_show_full_target_path"],
                path_depth=preferences["preview_target_path_depth"],
            )
            self.plan_file_path = str(
                self.store.plans_directory / f"{self.plan['id']}.syncplan"
            )
            self.log_path = configure_logging(self.store.logs_directory)
            self._refresh_plans()
            self.view.set_status(f"Project storage is now {directory}", "success")
            self.view.append_log(f"Project storage changed to {directory}")
        except Exception as error:
            self._show_failure(error)

    @Slot()
    def _show_automatic_runs(self):
        """Opens the separate Windows Task Scheduler manager."""
        try:
            if not self._save_plan(quiet=True):
                return
            from sync_engine_scheduler import AutomaticRunsDialog

            dialog = AutomaticRunsDialog(self.store, self.plan, self.view)
            dialog_runner = getattr(dialog, "exec", None) or dialog.exec_
            dialog_runner()
        except Exception as error:
            self._show_failure(error)

    @Slot(object)
    def _closing(self, event):
        """Preserve edits and wait for safe worker cancellation before closing.

        Args:
            event (QCloseEvent): Window close event.
        """
        if self.worker is not None:
            event.ignore()
            self._close_pending = True
            self.cancel()
            return
        if self.state == "countdown":
            self.cancel()
        if self.dirty and not self._save_plan(quiet=True):
            event.ignore()
            return
        preferences = self.store.read_preferences()
        preferences["last_plan_id"] = self.plan["id"]
        preferences["geometry"] = bytes(self.view.saveGeometry().toBase64()).decode("ascii")
        try:
            self.store.write_preferences(preferences)
        except OSError:
            logger.exception("Could not save window preferences")
        event.accept()
