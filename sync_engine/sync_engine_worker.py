"""Qt background workers for filesystem-only backup operations."""

import logging
import threading
import time

from sync_engine_engine import OperationControl, SyncCancelled, build_preview, execute_preview
from sync_engine_locking import RunLock
from sync_engine_qt import QtCore, Signal


logger = logging.getLogger("sync_engine")


class BackupWorker(QtCore.QThread):
    """Run a filesystem scan or backup without blocking the Qt event loop.

    Args:
        plan (dict): Validated backup plan.
        data_directory (str or Path): Directory containing the execution lock.
        preview (dict or None): Preview to execute; None requests a scan.
        allow_delete (bool): Whether deletions in the preview are authorized.
        source_removal_confirmation (bool): Whether to request a GUI decision after move
            destinations are verified and before removing source files.
        plan_file_path (str or Path or None): File path used to expand plan variables.
        parent (QObject or None): Qt object owning the worker.
    """

    progress = Signal(object)
    preview_ready = Signal(object)
    result_ready = Signal(object)
    failed = Signal(str)
    source_removal_confirmation_requested = Signal(int)

    def __init__(self, plan, data_directory, preview=None, allow_delete=False,
                 sync_state=None, conflict_resolutions=None, parent=None, plan_file_path=None,
                 deadline=None, source_removal_confirmation=False):
        """Store the operation and create its cancellation and pause controls.

        Args:
            plan (dict): Validated backup plan.
            data_directory (str or Path): Directory containing the execution lock.
            preview (dict or None): Preview to execute, or None for scanning.
            allow_delete (bool): Whether planned deletions are authorized.
            parent (QObject or None): Qt object owning the worker.
        """
        super().__init__(parent)
        self.plan = plan
        self.data_directory = data_directory
        self.preview = preview
        self.allow_delete = allow_delete
        self.sync_state = sync_state
        self.conflict_resolutions = conflict_resolutions
        self.plan_file_path = plan_file_path
        self.deadline = deadline
        self.source_removal_confirmation_enabled = bool(source_removal_confirmation)
        self.timed_out = False
        self.cancel_event = OperationControl()
        self._source_confirmation_event = threading.Event()
        self._source_confirmation_response = False
        self._last_progress_time = 0.0
        self._last_phase = None
        self._pending_log_messages = []
        self._pending_completed_items = []
        self._last_progress_data = None

    def cancel(self):
        """Request cooperative cancellation at the next engine checkpoint."""
        self.cancel_event.set()

    @property
    def is_paused(self):
        """Whether this operation is paused at a safe checkpoint."""
        return self.cancel_event.is_paused

    def pause(self):
        """Request a pause at the next engine checkpoint."""
        self.cancel_event.pause()

    def resume(self):
        """Resume an operation paused at a safe checkpoint."""
        self.cancel_event.resume()

    def resolve_source_removal_confirmation(self, approved):
        """Returns the GUI's source-removal decision to the execution thread."""
        self._source_confirmation_response = bool(approved)
        self._source_confirmation_event.set()

    def _request_source_removal_confirmation(self, count):
        """Waits for the GUI decision while allowing cancellation and timeouts."""
        self._source_confirmation_response = False
        self._source_confirmation_event.clear()
        self.source_removal_confirmation_requested.emit(int(count))
        while not self._source_confirmation_event.wait(0.1):
            if self.cancel_event.is_set():
                return False
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self.timed_out = True
                self.cancel_event.set()
                return False
        return self._source_confirmation_response and not self.cancel_event.is_set()

    def _report_progress(self, data):
        """Throttle progress updates while retaining phase changes.

        Args:
            data (dict): Engine progress values.
        """
        current_time = time.monotonic()
        if self.deadline is not None and current_time >= self.deadline and not self.timed_out:
            self.timed_out = True
            self.cancel_event.set()
            data = dict(data)
            data.update({"phase": "timed_out", "message": "Sync timed out; stopping at a safe checkpoint."})
        phase = data.get("phase")
        total_operations = data.get("total_operations", 0)
        complete = total_operations > 0 and data.get("completed_operations", 0) >= total_operations
        message = str(data.get("message", ""))
        operation_completed = (
            message.startswith("Completed ") or message.startswith("Removed ")
            or message.startswith("Moved ")
        )
        if operation_completed:
            self._pending_log_messages.append(message)
            self._pending_completed_items.append({
                "phase": phase,
                "message": message,
                "current_path": data.get("current_path"),
            })
        self._last_progress_data = dict(data)
        if phase != self._last_phase or complete or current_time - self._last_progress_time >= 0.1:
            self._emit_progress(data, current_time)

    def _emit_progress(self, data, current_time=None):
        """Emits one coalesced progress update and any buffered operation logs."""
        payload = dict(data)
        if self._pending_log_messages:
            payload["completed_messages"] = self._pending_log_messages
            self._pending_log_messages = []
        if self._pending_completed_items:
            payload["completed_items"] = self._pending_completed_items
            self._pending_completed_items = []
        self.progress.emit(payload)
        self._last_progress_time = current_time or time.monotonic()
        self._last_phase = payload.get("phase")

    def _flush_progress(self):
        """Delivers buffered operation logs before the worker sends its result."""
        if self._pending_log_messages and self._last_progress_data is not None:
            self._emit_progress(self._last_progress_data)

    def run(self):
        """Execute the requested operation and emit its result."""
        try:
            if self.preview is None:
                preview = build_preview(
                    self.plan, self.cancel_event, self._report_progress,
                    sync_state=self.sync_state, conflict_resolutions=self.conflict_resolutions,
                    plan_file_path=self.plan_file_path,
                )
                self._flush_progress()
                self.preview_ready.emit(preview)
            else:
                with RunLock(self.data_directory):
                    result = execute_preview(
                        self.preview,
                        self.cancel_event,
                        self._report_progress,
                        allow_delete=self.allow_delete,
                        source_removal_confirmation=(
                            self._request_source_removal_confirmation
                            if self.source_removal_confirmation_enabled else None
                        ),
                    )
                if self.timed_out:
                    result["status"] = "timed_out"
                    result.setdefault("warnings", []).append(
                        "The configured timeout elapsed. Completed file operations were retained."
                    )
                self._flush_progress()
                self.result_ready.emit(result)
        except SyncCancelled:
            self._flush_progress()
            status = "timed_out" if self.timed_out else "cancelled"
            warnings = (["The configured timeout elapsed before the scan completed."]
                        if self.timed_out else [])
            self.result_ready.emit({
                "status": status, "copied": 0, "moved": 0, "deleted": 0,
                "bytes": 0, "errors": [], "warnings": warnings,
            })
        except Exception as error:
            self._flush_progress()
            logger.exception("Backup operation failed")
            self.failed.emit(str(error))
