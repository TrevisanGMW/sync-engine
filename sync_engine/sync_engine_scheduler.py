"""Windows Task Scheduler integration and the separate automatic-runs window."""

import csv
import datetime
import os
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from functools import partial
from pathlib import Path

from sync_engine_model import save_action
from sync_engine_qt import QtCore, QtWidgets, qt_enum


TASK_FOLDER = "\\SyncEngine"
TASK_MARKER = "SyncEngine managed task;"
LEGACY_TASK_FOLDER = "\\SyncTasker"
LEGACY_TASK_MARKER = "SyncTasker managed task;"
NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
_ACTIVE_TASK_WORKERS = []


def _windows_only():
    """Rejects scheduler use outside Windows."""
    if os.name != "nt":
        raise OSError("Windows Task Scheduler is available on Windows only.")


def _run(arguments, timeout=30):
    """Runs a Windows management command and returns decoded output."""
    result = subprocess.run(
        arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Windows command failed."
        raise OSError(message)
    return result.stdout


def _current_sid():
    """Returns the current Windows account SID for task ownership checks."""
    output = _run(["whoami.exe", "/user", "/fo", "csv", "/nh"])
    row = next(csv.reader([output.strip()]), [])
    if len(row) < 2 or not row[-1].startswith("S-"):
        raise OSError("Could not determine the current Windows account SID.")
    return row[-1]


def _ensure_task_folder():
    """Creates the app-owned Task Scheduler folder without prompting for credentials."""
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$service=New-Object -ComObject Schedule.Service; $service.Connect(); "
        "try{$service.GetFolder('\\SyncEngine')|Out-Null} "
        "catch{$service.GetFolder('\\').CreateFolder('SyncEngine')|Out-Null}"
    )
    _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])


def _task_xml(action_path, application_directory, owner_sid, schedule, headless=False,
              python_executable=None):
    """Builds a least-privilege, current-user, interactive task definition."""
    root = ET.Element("Task", {"version": "1.2", "xmlns": NAMESPACE})
    registration = ET.SubElement(root, "RegistrationInfo")
    ET.SubElement(registration, "Date").text = datetime.datetime.now(
        datetime.timezone.utc
    ).isoformat(timespec="seconds")
    ET.SubElement(registration, "Author").text = "SyncEngine"
    schedule_label = schedule["label"]
    run_mode = "headless" if headless else "interactive"
    description = (
        f"{TASK_MARKER} owner={owner_sid}; schedule={schedule_label}; "
        f"mode={run_mode}; action={action_path}"
    )
    ET.SubElement(registration, "Description").text = description

    triggers = ET.SubElement(root, "Triggers")
    if schedule["kind"] == "logon":
        trigger = ET.SubElement(triggers, "LogonTrigger")
        ET.SubElement(trigger, "UserId").text = owner_sid
        ET.SubElement(trigger, "Enabled").text = "true"
    else:
        start = datetime.datetime.now().replace(
            hour=schedule["hour"], minute=schedule["minute"], second=0, microsecond=0
        )
        trigger = ET.SubElement(triggers, "CalendarTrigger")
        ET.SubElement(trigger, "StartBoundary").text = start.isoformat(timespec="seconds")
        ET.SubElement(trigger, "Enabled").text = "true"
        if schedule["kind"] == "weekly":
            repetition = ET.SubElement(trigger, "ScheduleByWeek")
            ET.SubElement(repetition, "WeeksInterval").text = str(schedule["interval"])
            days = ET.SubElement(repetition, "DaysOfWeek")
            for day in schedule["days"]:
                ET.SubElement(days, day).text = "true"
        else:
            repetition = ET.SubElement(trigger, "ScheduleByDay")
            ET.SubElement(repetition, "DaysInterval").text = str(schedule["interval"])

    principals = ET.SubElement(root, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "SyncEngineUser"})
    ET.SubElement(principal, "UserId").text = owner_sid
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"

    settings = ET.SubElement(root, "Settings")
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT72H"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, name).text = value
    actions = ET.SubElement(root, "Actions", {"Context": "SyncEngineUser"})
    execute = ET.SubElement(actions, "Exec")
    if getattr(sys, "frozen", False):
        command = sys.executable
        working_directory = Path(sys.executable).resolve().parent
        if headless:
            arguments = f'--headless --run "{action_path}" --countdown 0'
        else:
            arguments = f'--run "{action_path}"'
    elif headless:
        batch_path = Path(application_directory).parent / "scripts" / "SyncEngine.bat"
        command = "cmd.exe"
        arguments = f'/d /c ""{batch_path}" --headless --run "{action_path}" --countdown 0"'
        working_directory = Path(application_directory).parent
    else:
        command = python_executable or sys.executable
        application_script = Path(application_directory) / "sync_engine_task.py"
        arguments = f'"{application_script}" --run "{action_path}"'
        working_directory = Path(application_directory).parent
    ET.SubElement(execute, "Command").text = command
    ET.SubElement(execute, "Arguments").text = arguments
    ET.SubElement(execute, "WorkingDirectory").text = str(working_directory)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True), description


def create_task(action_path, application_directory, display_name, schedule, headless=False,
                python_executable=None):
    """Registers a SyncEngine schedule using the current interactive account.

    Args:
        action_path (str): Persistent .syncrun automatic action.
        application_directory (str): Package folder containing the application.
        display_name (str): Task label shown in Windows.
        schedule (dict): Daily, weekly, or sign-in trigger configuration.
        headless (bool): Whether to skip the UI countdown and run immediately.
        python_executable (str or None): Interpreter used for interactive runs.

    Returns:
        str: Registered Task Scheduler path.
    """
    _windows_only()
    _ensure_task_folder()
    owner_sid = _current_sid()
    safe_name = re.sub(r"[^A-Za-z0-9 _.-]+", "_", display_name).strip(" .") or "Automatic run"
    task_id = uuid.uuid4().hex[:12]
    task_path = f"{TASK_FOLDER}\\{safe_name[:70]} [{task_id}]"
    serialized, unused_description = _task_xml(
        action_path, application_directory, owner_sid, schedule,
        headless=headless, python_executable=python_executable,
    )
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as stream:
            temporary_path = stream.name
            stream.write(serialized)
        _run(["schtasks.exe", "/Create", "/TN", task_path, "/XML", temporary_path, "/F"])
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return task_path


def list_tasks():
    """Lists only tasks registered by this SyncEngine installation and user."""
    _windows_only()
    owner_sid = _current_sid()
    folder_paths = ",".join(
        f"'{folder}'" for folder in (TASK_FOLDER, LEGACY_TASK_FOLDER)
    )
    script = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        "$service=New-Object -ComObject Schedule.Service; $service.Connect(); $rows=@(); "
        f"foreach($folderPath in @({folder_paths})){{ "
        "try{$folder=$service.GetFolder($folderPath)}catch{continue}; "
        "foreach($task in $folder.GetTasks(1)){ $d=$task.Definition.RegistrationInfo.Description; "
        "$trigger=''; if($task.Definition.Triggers.Count -gt 0){$trigger=$task.Definition.Triggers.Item(1).Type}; "
        "$rows += [PSCustomObject]@{Name=$task.Name; Path=$task.Path; Enabled=$task.Enabled; "
        "State=$task.State; NextRun=$task.NextRunTime; LastRun=$task.LastRunTime; "
        "LastResult=$task.LastTaskResult; Description=$d; TriggerType=$trigger} } }; "
        "if($rows.Count -eq 0){Write-Output '[]'}else{$rows | ConvertTo-Json -Compress}"
    )
    try:
        output = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
    except OSError as error:
        if "folder" in str(error).lower() or "cannot find" in str(error).lower():
            return []
        raise
    if not output.strip():
        return []
    import json

    payload = json.loads(output)
    rows = payload if isinstance(payload, list) else [payload]
    tasks = []
    marker = f"{TASK_MARKER} owner={owner_sid};"
    legacy_marker = f"{LEGACY_TASK_MARKER} owner={owner_sid};"
    state_labels = {"0": "Unknown", "1": "Disabled", "2": "Queued", "3": "Ready", "4": "Running"}
    for row in rows:
        description = str(row.get("Description") or "")
        if not description.startswith((marker, legacy_marker)):
            continue
        schedule = _schedule_from_description(description)
        if description.startswith(legacy_marker):
            schedule = f"Legacy SyncTasker task - {schedule}"
        tasks.append({
            "name": row.get("Name", ""), "path": row.get("Path", ""),
            "enabled": bool(row.get("Enabled")),
            "state": state_labels.get(str(row.get("State", "")), str(row.get("State", ""))),
            "next_run": str(row.get("NextRun", "")), "last_run": str(row.get("LastRun", "")),
            "last_result": str(row.get("LastResult", "")),
            "schedule": schedule,
        })
    return sorted(tasks, key=lambda task: task["name"].casefold())


def _schedule_from_description(description):
    """Extracts a concise schedule label from app-owned task metadata."""
    match = re.search(r"; schedule=(.*?); mode=", description)
    schedule = match.group(1) if match else "SyncEngine schedule"
    mode = "Headless" if "; mode=headless;" in description else "Interactive"
    return f"{schedule} ({mode})"


def set_task_enabled(task_path, enabled):
    """Enables or disables one previously listed SyncEngine task."""
    _windows_only()
    _run(["schtasks.exe", "/Change", "/TN", task_path, "/ENABLE" if enabled else "/DISABLE"])


def delete_task(task_path):
    """Deletes one previously listed SyncEngine task."""
    _windows_only()
    _run(["schtasks.exe", "/Delete", "/TN", task_path, "/F"])


class _TaskListWorker(QtCore.QThread):
    """Queries the Windows scheduler without blocking the dialog event loop."""

    result_ready = QtCore.Signal(object)
    failed = QtCore.Signal(str)

    def run(self):
        """Loads the app-owned task list in a worker thread."""
        try:
            self.result_ready.emit(list_tasks())
        except Exception as error:
            self.failed.emit(str(error))


def _release_task_worker(worker):
    """Releases a completed task-list worker even if its dialog closed early."""
    if worker in _ACTIVE_TASK_WORKERS:
        _ACTIVE_TASK_WORKERS.remove(worker)
    worker.deleteLater()


class AutomaticRunsDialog(QtWidgets.QDialog):
    """Separate UI for creating, reviewing, and managing scheduled runs."""

    def __init__(self, store, current_plan, parent=None):
        """Builds scheduler controls and the owned-task list."""
        super().__init__(parent)
        self.store = store
        self.current_plan = current_plan
        self.setWindowTitle("SyncEngine Automatic Runs")
        self.setMinimumSize(760, 520)
        layout = QtWidgets.QVBoxLayout(self)
        intro = QtWidgets.QLabel(
            "Manage recurring runs registered by SyncEngine for your Windows account. "
            "Tasks run only while you are signed in, with your normal permissions."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        create_group = QtWidgets.QGroupBox("Create automatic run")
        form = QtWidgets.QGridLayout(create_group)
        self.name_edit = QtWidgets.QLineEdit(current_plan.get("name", "SyncEngine backup"))
        form.addWidget(QtWidgets.QLabel("Task name"), 0, 0)
        form.addWidget(self.name_edit, 0, 1, 1, 3)
        self.plan_combo = QtWidgets.QComboBox()
        self.plan_ids = []
        for item in store.list_plans():
            self.plan_combo.addItem(item["name"])
            self.plan_ids.append(item["id"])
        current_index = self.plan_ids.index(current_plan["id"]) if current_plan["id"] in self.plan_ids else 0
        if self.plan_ids:
            self.plan_combo.setCurrentIndex(current_index)
        form.addWidget(QtWidgets.QLabel("Sync Plan"), 1, 0)
        form.addWidget(self.plan_combo, 1, 1, 1, 3)
        self.schedule_combo = QtWidgets.QComboBox()
        self.schedule_combo.addItem("Daily", "daily")
        self.schedule_combo.addItem("Weekly", "weekly")
        self.schedule_combo.addItem("When I sign in", "logon")
        self.schedule_combo.currentIndexChanged.connect(self._update_schedule_controls)
        form.addWidget(QtWidgets.QLabel("Run"), 2, 0)
        form.addWidget(self.schedule_combo, 2, 1)
        self.time_edit = QtWidgets.QTimeEdit(QtCore.QTime(2, 0))
        self.time_edit.setDisplayFormat("HH:mm")
        form.addWidget(QtWidgets.QLabel("At"), 2, 2)
        form.addWidget(self.time_edit, 2, 3)
        self.interval = QtWidgets.QSpinBox()
        self.interval.setRange(1, 365)
        self.interval.setValue(1)
        self.interval.setSuffix(" week(s)")
        form.addWidget(QtWidgets.QLabel("Repeat every"), 3, 0)
        form.addWidget(self.interval, 3, 1)
        self.day_checks = {}
        day_layout = QtWidgets.QHBoxLayout()
        for label, key in (("Mon", "Monday"), ("Tue", "Tuesday"), ("Wed", "Wednesday"),
                           ("Thu", "Thursday"), ("Fri", "Friday"), ("Sat", "Saturday"),
                           ("Sun", "Sunday")):
            check = QtWidgets.QCheckBox(label)
            check.setChecked(key in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday"))
            self.day_checks[key] = check
            day_layout.addWidget(check)
        form.addWidget(QtWidgets.QLabel("On"), 3, 2)
        form.addLayout(day_layout, 3, 3)
        self.headless_check = QtWidgets.QCheckBox("Run headlessly with no countdown")
        self.headless_check.setToolTip(
            "By default, scheduled actions open SyncEngine and honor the action's countdown and "
            "minimized-window settings. Headless runs start immediately without a window."
        )
        self.headless_check.setChecked(False)
        form.addWidget(self.headless_check, 4, 0, 1, 3)
        self.create_button = QtWidgets.QPushButton("Create scheduled run")
        self.create_button.setObjectName("primaryButton")
        self.create_button.clicked.connect(self._create)
        form.addWidget(self.create_button, 5, 3)
        layout.addWidget(create_group)

        self.loading_label = QtWidgets.QLabel("Loading SyncEngine scheduled runs…")
        self.loading_label.setObjectName("mutedLabel")
        self.loading_label.hide()
        layout.addWidget(self.loading_label)
        self.loading_bar = QtWidgets.QProgressBar()
        self.loading_bar.setRange(0, 0)
        self.loading_bar.setTextVisible(False)
        self.loading_bar.hide()
        layout.addWidget(self.loading_bar)

        self.table = QtWidgets.QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Task", "Schedule", "Next run", "State", "Last result"])
        self.table.setSelectionBehavior(qt_enum(QtWidgets.QAbstractItemView, "SelectionBehavior", "SelectRows"))
        self.table.setSelectionMode(qt_enum(QtWidgets.QAbstractItemView, "SelectionMode", "SingleSelection"))
        self.table.setEditTriggers(qt_enum(QtWidgets.QAbstractItemView, "EditTrigger", "NoEditTriggers"))
        self.table.horizontalHeader().setSectionResizeMode(
            0, qt_enum(QtWidgets.QHeaderView, "ResizeMode", "Stretch")
        )
        for column in (1, 2, 3, 4):
            self.table.horizontalHeader().setSectionResizeMode(
                column, qt_enum(QtWidgets.QHeaderView, "ResizeMode", "ResizeToContents")
            )
        layout.addWidget(self.table, 1)
        buttons = QtWidgets.QHBoxLayout()
        self.refresh_button = QtWidgets.QPushButton("Refresh")
        self.enable_button = QtWidgets.QPushButton("Enable")
        self.disable_button = QtWidgets.QPushButton("Disable")
        self.delete_button = QtWidgets.QPushButton("Delete")
        close_button = QtWidgets.QPushButton("Close")
        for button in (self.refresh_button, self.enable_button, self.disable_button,
                       self.delete_button, close_button):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.refresh_button.clicked.connect(self.refresh)
        self.enable_button.clicked.connect(lambda: self._set_enabled(True))
        self.disable_button.clicked.connect(lambda: self._set_enabled(False))
        self.delete_button.clicked.connect(self._delete_selected)
        close_button.clicked.connect(self.accept)
        self.status = QtWidgets.QLabel()
        layout.addWidget(self.status)
        self._task_worker = None
        self._task_load_error = None
        self._update_schedule_controls()
        self.refresh()

    def _update_schedule_controls(self):
        """Shows only fields used by the selected trigger type."""
        weekly = self.schedule_combo.currentData() == "weekly"
        daily = self.schedule_combo.currentData() == "daily"
        self.time_edit.setEnabled(daily or weekly)
        self.interval.setEnabled(daily or weekly)
        self.interval.setSuffix(" day(s)" if daily else " week(s)")
        for check in self.day_checks.values():
            check.setEnabled(weekly)

    def _selected_task(self):
        """Returns the task record selected in the table."""
        row = self.table.currentRow()
        if row < 0 or row >= len(self.tasks):
            return None
        return self.tasks[row]

    def refresh(self):
        """Reloads SyncEngine-owned tasks asynchronously with visible progress."""
        if self._task_worker is not None and self._task_worker.isRunning():
            return
        self.loading_label.setText("Loading SyncEngine scheduled runs…")
        self.loading_label.show()
        self.loading_bar.show()
        self.status.setText("Contacting Windows Task Scheduler…")
        self.table.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.create_button.setEnabled(False)
        self.enable_button.setEnabled(False)
        self.disable_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self._task_load_error = None
        self._task_worker = _TaskListWorker(QtWidgets.QApplication.instance())
        _ACTIVE_TASK_WORKERS.append(self._task_worker)
        self._task_worker.result_ready.connect(self._tasks_loaded)
        self._task_worker.failed.connect(self._task_load_failed)
        self._task_worker.finished.connect(self._task_load_finished)
        self._task_worker.finished.connect(partial(_release_task_worker, self._task_worker))
        self._task_worker.start()

    @QtCore.Slot(object)
    def _tasks_loaded(self, tasks):
        """Displays a completed task query while the dialog remains responsive."""
        self.tasks = list(tasks)
        self.table.setRowCount(len(self.tasks))
        for row, task in enumerate(self.tasks):
            values = (task["name"], task["schedule"], task["next_run"],
                      task["state"] if task["enabled"] else "Disabled", task["last_result"])
            for column, value in enumerate(values):
                self.table.setItem(row, column, QtWidgets.QTableWidgetItem(str(value or "—")))
        self.status.setText(f"Showing {len(self.tasks)} SyncEngine task(s).")

    @QtCore.Slot(str)
    def _task_load_failed(self, message):
        """Retains a scheduler query error for display after the worker exits."""
        self._task_load_error = message

    @QtCore.Slot()
    def _task_load_finished(self):
        """Restores list controls after the scheduler query completes."""
        self.loading_label.hide()
        self.loading_bar.hide()
        self.table.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.create_button.setEnabled(True)
        self.enable_button.setEnabled(True)
        self.disable_button.setEnabled(True)
        self.delete_button.setEnabled(True)
        if self._task_load_error:
            self.tasks = []
            self.table.setRowCount(0)
            self.status.setText(self._task_load_error)
        self._task_worker = None

    def _create(self):
        """Exports the selected plan as a persistent action and registers its trigger."""
        try:
            if not self.plan_ids:
                raise ValueError("Create a backup plan before scheduling it.")
            plan = self.store.load(self.plan_ids[self.plan_combo.currentIndex()])
            if any(not pair["source"] or not pair["destination"] for pair in plan["pairs"]):
                raise ValueError("Set a source and destination for every folder pair before scheduling it.")
            has_deletions = any(pair["mode"] in ("mirror", "move", "two_way") for pair in plan["pairs"])
            allow_deletions = False
            if has_deletions:
                response = QtWidgets.QMessageBox.question(
                    self, "Allow scheduled removals?",
                    "This plan can permanently remove or move files. Allow this scheduled task to apply "
                    "the removals shown in its preview?",
                    qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
                    | qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
                    qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
                )
                allow_deletions = response == qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
                if not allow_deletions:
                    return
            action_directory = self.store.directory / "actions"
            action_directory.mkdir(parents=True, exist_ok=True)
            action_path = action_directory / f"{plan['id']}.syncrun"
            save_action(plan, action_path, allow_deletions=allow_deletions)
            schedule_kind = self.schedule_combo.currentData()
            label = schedule_kind
            schedule = {"kind": schedule_kind, "label": schedule_kind}
            if schedule_kind in ("daily", "weekly"):
                selected_time = self.time_edit.time()
                schedule.update({"hour": selected_time.hour(), "minute": selected_time.minute()})
                interval = self.interval.value()
                schedule["interval"] = interval
                label = f"{schedule_kind}, every {interval}"
                if schedule_kind == "weekly":
                    days = [key for key, check in self.day_checks.items() if check.isChecked()]
                    if not days:
                        raise ValueError("Choose at least one weekday.")
                    schedule["days"] = days
                    label += " week(s)"
                    label += " on " + ", ".join(day[:3] for day in days)
                label += f" at {selected_time.toString('HH:mm')}"
                schedule["label"] = label
            task_path = create_task(
                str(action_path), Path(__file__).resolve().parent, self.name_edit.text(), schedule,
                headless=self.headless_check.isChecked(), python_executable=sys.executable,
            )
            self.status.setText(f"Created {task_path}")
            self.refresh()
        except Exception as error:
            self.status.setText(str(error))

    def _set_enabled(self, enabled):
        """Changes the selected task's enabled state."""
        task = self._selected_task()
        if task is None:
            return
        try:
            set_task_enabled(task["path"], enabled)
            self.refresh()
        except Exception as error:
            self.status.setText(str(error))

    def _delete_selected(self):
        """Deletes a selected app-owned task after a direct confirmation."""
        task = self._selected_task()
        if task is None:
            return
        response = QtWidgets.QMessageBox.question(
            self, "Delete scheduled run?", f"Delete '{task['name']}' from Task Scheduler?",
            qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes")
            | qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
            qt_enum(QtWidgets.QMessageBox, "StandardButton", "No"),
        )
        if response != qt_enum(QtWidgets.QMessageBox, "StandardButton", "Yes"):
            return
        try:
            delete_task(task["path"])
            self.refresh()
        except Exception as error:
            self.status.setText(str(error))
