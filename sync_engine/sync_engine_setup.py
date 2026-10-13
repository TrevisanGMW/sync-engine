"""Creates a private Qt runtime for source launches when no binding is available."""

import hashlib
import os
import platform
import subprocess
import sys
from pathlib import Path

from sync_engine_model import default_data_directory


def _base_interpreter(executable):
    """Returns a console-capable interpreter when a Windows pythonw is selected.

    Args:
        executable (str): Candidate Python executable.

    Returns:
        str: Interpreter path usable for venv and pip operations.
    """
    path = Path(executable)
    if os.name == "nt" and path.name.lower() == "pythonw.exe":
        console_python = path.with_name("python.exe")
        if console_python.is_file():
            return str(console_python)
    return str(path)


def _runtime_root(application_directory):
    """Chooses an app-local runtime, falling back to the user's cache directory.

    Args:
        application_directory (Path): SyncEngine source directory.

    Returns:
        Path: Writable runtime root.
    """
    local_root = Path(application_directory) / ".runtime"
    try:
        local_root.mkdir(parents=True, exist_ok=True)
        probe = local_root / ".write-check"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return local_root
    except OSError:
        if os.name == "nt":
            cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SyncEngine" / "runtime"
        elif sys.platform == "darwin":
            cache_root = Path.home() / "Library" / "Caches" / "SyncEngine" / "runtime"
        else:
            cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "syncengine" / "runtime"
        cache_root.mkdir(parents=True, exist_ok=True)
        return cache_root


def _python_version(executable):
    """Reads the selected interpreter's major and minor version.

    Args:
        executable (str): Python executable path.

    Returns:
        str: Version label suitable for a runtime folder name.
    """
    result = subprocess.run(
        [executable, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
    )
    return result.stdout.strip()


def _runtime_python(runtime_directory):
    """Locates the interpreter inside a standard-library virtual environment.

    Args:
        runtime_directory (Path): Virtual environment directory.

    Returns:
        Path: Virtual environment Python executable.
    """
    relative = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
    return runtime_directory / relative


def ensure_qt_runtime(base_executable, application_directory, report_progress=None):
    """Creates or updates an isolated PySide6 environment and returns its Python.

    Args:
        base_executable (str): Python executable used to create the virtualenv.
        application_directory (Path): SyncEngine source directory.
        report_progress (callable, optional): Receives short status messages.

    Returns:
        str: Private Python executable path.

    Raises:
        RuntimeError: If the private runtime cannot be created or dependencies fail.
    """
    application_directory = Path(application_directory)
    base_executable = _base_interpreter(base_executable)
    requirements = application_directory / "requirements.txt"
    if not requirements.is_file():
        raise RuntimeError(f"Missing dependency list: {requirements}")
    requirement_text = requirements.read_text(encoding="utf-8")
    version = _python_version(base_executable)
    identity = "|".join((base_executable, version, platform.platform(), requirement_text))
    fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:10]
    runtime_root = _runtime_root(application_directory)
    runtime_directory = runtime_root / f"python-{version}-{fingerprint}"
    runtime_python = _runtime_python(runtime_directory)
    logs_directory = default_data_directory() / "logs"
    try:
        logs_directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        logs_directory = runtime_root / "logs"
        logs_directory.mkdir(parents=True, exist_ok=True)
    log_path = logs_directory / "dependency_setup.log"

    def report(message):
        if report_progress:
            report_progress(message)

    marker_path = runtime_directory / ".requirements.sha256"
    requirement_fingerprint = hashlib.sha256(requirement_text.encode("utf-8")).hexdigest()
    if runtime_python.is_file() and marker_path.is_file():
        try:
            if marker_path.read_text(encoding="utf-8").strip() == requirement_fingerprint:
                probe = subprocess.run(
                    [str(runtime_python), "-c", "from PySide6 import QtWidgets"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                )
                if probe.returncode == 0:
                    return str(runtime_python)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not runtime_python.is_file():
        report(f"Creating a private Python {version} environment...")
        try:
            create_result = subprocess.run(
                [base_executable, "-m", "venv", str(runtime_directory)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", timeout=120,
            )
            if create_result.returncode:
                raise RuntimeError(create_result.stdout.strip() or "venv exited with an error")
        except Exception as error:
            raise RuntimeError(f"Could not create the private Python environment: {error}") from error
    report("Installing SyncEngine's graphical interface...")
    command = [
        str(runtime_python), "-m", "pip", "install", "--disable-pip-version-check",
        "--no-input", "--upgrade", "--requirement", str(requirements),
    ]
    with log_path.open("a", encoding="utf-8") as log_stream:
        log_stream.write(f"\nSyncEngine dependency setup using {base_executable}\n")
        log_stream.write("Command: " + " ".join(command) + "\n")
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        for line in process.stdout:
            log_stream.write(line)
            log_stream.flush()
            stripped = line.strip()
            if stripped.startswith(("Collecting ", "Installing collected packages", "Successfully installed")):
                report(stripped[:100])
        return_code = process.wait()
    if return_code:
        raise RuntimeError(
            f"Dependency installation failed (exit code {return_code}).\n"
            f"See the setup log: {log_path}"
        )
    marker_path.write_text(requirement_fingerprint, encoding="utf-8")
    report("Graphical interface is ready.")
    return str(runtime_python)
