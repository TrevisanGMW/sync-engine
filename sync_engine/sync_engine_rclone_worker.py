"""Background worker for the user-requested rclone download."""

import threading

from sync_engine_qt import QtCore, Signal
from sync_engine_rclone import RcloneCancelled, download_rclone


class RcloneDownloadWorker(QtCore.QThread):
    """Downloads the optional rclone executable outside the Qt UI thread."""

    """Downloads the optional rclone executable without blocking the window."""

    progress = Signal(str)
    installed = Signal(str)
    failed = Signal(str)

    def __init__(self, parent=None):
        """Creates the worker and its cooperative cancellation flag.

        Args:
            parent (QObject, optional): Owning Qt object.
        """
        super().__init__(parent)
        self.cancel_event = threading.Event()

    def cancel(self):
        """Requests cancellation at the next archive download checkpoint."""
        self.cancel_event.set()

    def run(self):
        """Downloads the latest stable rclone release and forwards status."""
        try:
            path = download_rclone(
                report_progress=self.progress.emit,
                cancel_event=self.cancel_event,
            )
            self.installed.emit(str(path))
        except RcloneCancelled as error:
            self.failed.emit(str(error))
        except Exception as error:
            self.failed.emit(str(error))

