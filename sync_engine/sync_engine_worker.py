"""Qt background workers for filesystem-only backup operations."""

import logging
import threading
import time

from sync_engine_engine import build_preview, execute_preview, SyncCancelled
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
        plan_file_path (str or Path or None): File path used to expand plan variables.
        parent (QObject or None): Qt object owning the worker.
    """

    progress = Signal(object)
    preview_ready = Signal(object)
    result_ready = Signal(object)
    failed = Signal(str)

    def __init__(self, plan, data_directory, preview=None, allow_delete=False,
                 sync_state=None, conflict_resolutions=None, parent=None, plan_file_path=None,
                 deadline=None):
        """Store the operation and create its cancellation flag.

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
        self.timed_out = False
        self.cancel_event = threading.Event()
        self._last_progress_time = 0.0
        self._last_phase = None

    def cancel(self):
        """Request cooperative cancellation at the next engine checkpoint."""
        self.cancel_event.set()

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
        complete = data.get("completed_operations", -1) == data.get("total_operations", -2)
        message = str(data.get("message", ""))
        operation_completed = message.startswith("Completed ") or message.startswith("Removed ")
        if (phase != self._last_phase or complete or operation_completed
                or current_time - self._last_progress_time >= 0.1):
            self._last_progress_time = current_time
            self._last_phase = phase
            self.progress.emit(dict(data))

    def run(self):
        """Execute the requested operation and emit its result."""
        try:
            if self.preview is None:
                preview = build_preview(
                    self.plan, self.cancel_event, self._report_progress,
                    sync_state=self.sync_state, conflict_resolutions=self.conflict_resolutions,
                    plan_file_path=self.plan_file_path,
                )
                self.preview_ready.emit(preview)
            else:
                with RunLock(self.data_directory):
                    result = execute_preview(
                        self.preview,
                        self.cancel_event,
                        self._report_progress,
                        allow_delete=self.allow_delete,
                    )
                if self.timed_out:
                    result["status"] = "timed_out"
                    result.setdefault("warnings", []).append(
                        "The configured timeout elapsed. Completed file operations were retained."
                    )
                self.result_ready.emit(result)
        except SyncCancelled:
            status = "timed_out" if self.timed_out else "cancelled"
            warnings = (["The configured timeout elapsed before the scan completed."]
                        if self.timed_out else [])
            self.result_ready.emit({
                "status": status, "copied": 0, "deleted": 0,
                "bytes": 0, "errors": [], "warnings": warnings,
            })
        except Exception as error:
            logger.exception("Backup operation failed")
            self.failed.emit(str(error))
