"""Small nonblocking Google Drive folder chooser for SyncEngine."""

import json

from sync_engine_qt import QtCore, QtWidgets, qt_enum
from sync_engine_rclone import folder_list_command, make_rclone_path


class RcloneFolderDialog(QtWidgets.QDialog):
    """Browses configured rclone remotes and returns a selected Drive folder."""

    def __init__(self, remote_names, parent=None):
        """Creates a compact browser that lists folders asynchronously.

        Args:
            remote_names (list[str]): Configured Google Drive remote names.
            parent (QWidget, optional): Owning application window.
        """
        super().__init__(parent)
        self.remote_names = list(remote_names or [])
        self.current_path = ""
        self._output = bytearray()
        self._error_output = bytearray()
        self._build()
        self._connect()
        if self.remote_names:
            self._refresh_folders()

    def _build(self):
        """Constructs the remote picker controls."""
        self.setWindowTitle("Choose Google Drive Folder")
        self.setMinimumSize(430, 390)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(8)

        remote_row = QtWidgets.QHBoxLayout()
        remote_row.addWidget(QtWidgets.QLabel("Drive"))
        self.remote_combo = QtWidgets.QComboBox()
        self.remote_combo.addItems(self.remote_names)
        remote_row.addWidget(self.remote_combo, 1)
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        remote_row.addWidget(self.refresh_button)
        layout.addLayout(remote_row)

        self.location_label = QtWidgets.QLabel()
        self.location_label.setObjectName("mutedLabel")
        layout.addWidget(self.location_label)
        self.folder_list = QtWidgets.QListWidget()
        self.folder_list.setSelectionMode(
            qt_enum(QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection")
        )
        layout.addWidget(self.folder_list, 1)
        self.status_label = QtWidgets.QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("mutedLabel")
        layout.addWidget(self.status_label)

        button_row = QtWidgets.QHBoxLayout()
        self.up_button = QtWidgets.QPushButton("Up")
        self.up_button.setAutoDefault(False)
        button_row.addWidget(self.up_button)
        button_row.addStretch()
        self.cancel_button = QtWidgets.QPushButton("Cancel")
        self.cancel_button.setAutoDefault(False)
        button_row.addWidget(self.cancel_button)
        self.choose_button = QtWidgets.QPushButton("Use This Folder")
        self.choose_button.setAutoDefault(False)
        button_row.addWidget(self.choose_button)
        layout.addLayout(button_row)

        self.process = QtCore.QProcess(self)

    def _connect(self):
        """Connects folder navigation and rclone process updates."""
        self.remote_combo.currentIndexChanged.connect(self._remote_changed)
        self.refresh_button.clicked.connect(self._refresh_folders)
        self.up_button.clicked.connect(self._go_up)
        self.folder_list.itemDoubleClicked.connect(self._enter_selected_folder)
        self.choose_button.clicked.connect(self._choose_current_folder)
        self.cancel_button.clicked.connect(self.reject)
        self.process.readyReadStandardOutput.connect(self._read_stdout)
        self.process.readyReadStandardError.connect(self._read_stderr)
        self.process.finished.connect(self._process_finished)
        self.process.errorOccurred.connect(self._process_error)

    def _remote_name(self):
        """Returns the selected configured remote name.

        Returns:
            str: Selected rclone remote name.
        """
        return self.remote_combo.currentText().strip()

    def _location_text(self):
        """Formats the current remote folder for display.

        Returns:
            str: Current location in native rclone path form.
        """
        remote_name = self._remote_name()
        return f"{remote_name}:/{self.current_path}" if self.current_path else f"{remote_name}:/"

    def _set_loading(self, loading):
        """Locks navigation while rclone lists a folder.

        Args:
            loading (bool): Whether a listing process is active.
        """
        self.remote_combo.setEnabled(not loading)
        self.refresh_button.setEnabled(not loading)
        self.up_button.setEnabled(not loading and bool(self.current_path))
        self.folder_list.setEnabled(not loading)
        self.choose_button.setEnabled(
            not loading and bool(self.current_path) and bool(self.remote_names)
        )

    def _refresh_folders(self):
        """Lists only the folders directly below the selected location."""
        if (not self.remote_names or self.process.state() != qt_enum(
                QtCore.QProcess, "ProcessState", "NotRunning")):
            return
        self._output = bytearray()
        self._error_output = bytearray()
        self.folder_list.clear()
        self.location_label.setText(self._location_text())
        self.status_label.setText("Loading Google Drive folders...")
        self._set_loading(True)
        try:
            command = folder_list_command(self._remote_name(), self.current_path)
            self.process.start(command[0], command[1:])
        except Exception as error:
            self._set_loading(False)
            self.status_label.setText(f"Could not list Google Drive folders: {error}")

    def _read_stdout(self):
        """Collects listing JSON as the asynchronous process writes it."""
        self._output.extend(bytes(self.process.readAllStandardOutput()))

    def _read_stderr(self):
        """Collects rclone errors without putting OAuth tokens in the UI."""
        self._error_output.extend(bytes(self.process.readAllStandardError()))

    def _process_finished(self, exit_code, unused_exit_status=None):
        """Displays the returned subfolders or an actionable list error.

        Args:
            exit_code (int): Process exit code.
            unused_exit_status (QProcess.ExitStatus, optional): Qt process status.
        """
        self._read_stdout()
        self._read_stderr()
        self._set_loading(False)
        if exit_code:
            detail = self._error_output.decode("utf-8", errors="replace").strip()
            if detail:
                detail = detail.splitlines()[-1]
            else:
                detail = f"rclone exited with status {exit_code}."
            self.status_label.setText(f"Could not list this folder: {detail}")
            return
        try:
            entries = json.loads(self._output.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            self.status_label.setText("Rclone returned invalid folder data.")
            return
        if not isinstance(entries, list):
            self.status_label.setText("Rclone returned an unexpected folder list.")
            return
        folders = sorted(
            (entry for entry in entries if isinstance(entry, dict) and entry.get("IsDir")),
            key=lambda entry: str(entry.get("Name", "")).casefold(),
        )
        for entry in folders:
            name = str(entry.get("Name") or entry.get("Path") or "").strip("/")
            if name:
                self.folder_list.addItem(name)
        if folders:
            self.status_label.setText("Double-click a folder to open it.")
        elif self.current_path:
            self.status_label.setText(
                "No subfolders here. Choose this folder to use it as the destination."
            )
        else:
            self.status_label.setText("No subfolders at the Drive root. Choose a folder below it.")
        self.location_label.setText(self._location_text())

    def _process_error(self, error):
        """Shows asynchronous process startup failures.

        Args:
            error (QProcess.ProcessError): Error reported by Qt.
        """
        if error == qt_enum(QtCore.QProcess, "ProcessError", "FailedToStart"):
            self._set_loading(False)
            self.status_label.setText("Could not start rclone. Recheck the setup in Options.")

    def _enter_selected_folder(self, item):
        """Opens one selected folder in the current remote.

        Args:
            item (QListWidgetItem): Selected folder row.
        """
        name = str(item.text()).strip("/")
        if not name:
            return
        self.current_path = "/".join(part for part in (self.current_path, name) if part)
        self._refresh_folders()

    def _go_up(self):
        """Moves to the parent folder."""
        self.current_path = self.current_path.rpartition("/")[0]
        self._refresh_folders()

    def _remote_changed(self, unused_index):
        """Resets navigation when the user selects another Drive remote.

        Args:
            unused_index (int): Newly selected remote combo index.
        """
        self.current_path = ""
        self._refresh_folders()

    def _choose_current_folder(self):
        """Returns the selected non-root folder to the folder-pair editor."""
        if not self.current_path:
            self.status_label.setText("Choose a folder below the Google Drive root.")
            return
        self.accept()

    def selected_path(self):
        """Returns the selected folder in SyncEngine's serialized path form.

        Returns:
            str: Selected Google Drive destination URI.
        """
        return make_rclone_path(self._remote_name(), self.current_path)

    def reject(self):
        """Stops a pending listing before closing the dialog."""
        if self.process.state() != qt_enum(QtCore.QProcess, "ProcessState", "NotRunning"):
            self.process.kill()
            self.process.waitForFinished(1000)
        super().reject()

