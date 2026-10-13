"""Console-free startup and optional user-scoped Windows file associations."""

import ctypes
import os
import shutil
import subprocess
import sys
from pathlib import Path

from sync_engine_model import default_data_directory


def _startup_environment(executable):
    """Builds a runtime environment that isolates Maya's bundled libraries.

    Args:
        executable (str or pathlib.Path): Selected Python executable.

    Returns:
        dict: Child environment with UTF-8 logging and safe Maya library loading.
    """
    environment = dict(os.environ)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    if Path(executable).name.lower().startswith("mayapy"):
        environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _hidden_process_options():
    """Suppresses subprocess consoles while allowing Qt windows to appear.

    STARTF_USESHOWWINDOW with SW_HIDE also hides Qt's first window, even when
    the application explicitly calls show(). CREATE_NO_WINDOW suppresses only
    the console and preserves normal and minimized application startup.

    Returns:
        dict: Platform-appropriate subprocess options.
    """
    if os.name != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def _candidate_interpreters():
    """Finds installed Python interpreters without installing dependencies.

    Returns:
        list: Deduplicated candidate executable paths, with the override first.
    """
    application = Path(__file__).resolve().parent.parent
    candidates = []
    override = os.environ.get("SYNCENGINE_PYTHON", "").strip()
    if not override:
        override = os.environ.get("SYNCTASKER_PYTHON", os.environ.get("FOLDER_RELAY_PYTHON", ""))
    override = override.strip().strip('"')
    if override:
        candidates.append(override)
    candidates.append(sys.executable)
    for relative in (
        ".venv/Scripts/python.exe", ".venv/bin/python", "runtime/python.exe",
        "runtime/Scripts/python.exe", "runtime/bin/python", ".runtime/python.exe",
    ):
        candidates.append(str(application / relative))
    for executable in ("pythonw.exe", "python.exe", "python3", "python"):
        discovered = shutil.which(executable)
        if discovered:
            candidates.append(discovered)
    if os.name == "nt":
        program_directories = {
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMW6432", r"C:\Program Files"),
        }
        for program_directory in sorted(program_directories):
            autodesk = Path(program_directory) / "Autodesk"
            if autodesk.is_dir():
                for maya_directory in sorted(autodesk.glob("Maya*"), reverse=True):
                    candidates.append(str(maya_directory / "bin" / "mayapy.exe"))
    unique = []
    seen = set()
    for candidate in candidates:
        resolved = shutil.which(candidate) or candidate
        path = Path(resolved)
        if not path.is_file():
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append(str(path.absolute()))
    return unique


def _supports_qt(executable):
    """Checks bundled Qt bindings in a short-lived process without creating UI.

    Args:
        executable (str): Interpreter to probe.

    Returns:
        bool: Whether PySide6 or PySide2 and its Qt widgets can import.
    """
    probe = (
        "import importlib\n"
        "for name in ('PySide6.QtWidgets', 'PySide2.QtWidgets'):\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "        break\n"
        "    except ImportError:\n"
        "        continue\n"
        "else:\n"
        "    raise SystemExit(1)\n"
    )
    try:
        result = subprocess.run(
            [executable, "-c", probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, env=_startup_environment(executable), **_hidden_process_options(),
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def show_startup_message(message, title="SyncEngine"):
    """Shows a native startup failure message before Qt is available.

    Args:
        message (str): Error and recovery guidance.
        title (str): Native dialog title.

    Returns:
        None: The message is displayed or written to standard error.
    """
    if os.name == "nt":
        try:
            ctypes.windll.user32.MessageBoxW(None, str(message), title, 0x10)
            return
        except (AttributeError, OSError):
            pass
    else:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(title, str(message), parent=root)
            root.destroy()
            return
        except Exception:
            pass
    if sys.stderr is not None:
        sys.stderr.write(f"{title}: {message}\n")


def _startup_log_directory():
    """Returns a writable log directory, using the user cache if needed.

    Returns:
        Path: Startup log directory.
    """
    log_directory = default_data_directory() / "logs"
    try:
        log_directory.mkdir(parents=True, exist_ok=True)
        return log_directory
    except OSError:
        if os.name == "nt":
            cache_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "SyncEngine"
        elif sys.platform == "darwin":
            cache_root = Path.home() / "Library" / "Caches" / "SyncEngine"
        else:
            cache_root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "syncengine"
        log_directory = cache_root / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        return log_directory


def _resolve_gui_interpreter(application_directory):
    """Finds an existing Qt interpreter or prepares SyncEngine's private runtime.

    Args:
        application_directory (Path): SyncEngine source directory.

    Returns:
        tuple: Selected interpreter path or None and a list of setup errors.
    """
    candidates = _candidate_interpreters()
    executable = next((candidate for candidate in candidates if _supports_qt(candidate)), None)
    setup_errors = []
    if executable is not None or not candidates:
        return executable, setup_errors
    try:
        from sync_engine_setup import ensure_qt_runtime
        from sync_engine_setup_progress import run_with_progress

        for candidate in candidates:
            try:
                executable = run_with_progress(
                    lambda report, base=candidate: ensure_qt_runtime(
                        base, application_directory, report_progress=report,
                    )
                )
                if _supports_qt(executable):
                    return executable, setup_errors
                setup_errors.append(f"The private runtime from {candidate} has no working Qt binding.")
                executable = None
            except Exception as error:
                setup_errors.append(f"{candidate}: {error}")
    except Exception as error:
        setup_errors.append(str(error))
    return None, setup_errors


def startup_main(arguments=None, gui=False):
    """Launches the app and privately installs Qt if no suitable binding exists.

    The working directory is preserved so relative command-line filenames retain
    their meaning. Existing Maya or system Qt bindings are reused without changes.

    Args:
        arguments (list, optional): App arguments, defaulting to sys.argv[1:].
        gui (bool): Whether the caller should detach and show native failures.

    Returns:
        int: Child exit code for CLI calls, or zero after a GUI launch.
    """
    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    application_directory = Path(__file__).resolve().parent.parent
    application = Path(__file__).resolve().parent / "sync_engine.py"
    pure_command = any(argument in ("--headless", "--preview", "--help", "-h") for argument in forwarded)
    candidates = _candidate_interpreters()
    executable = candidates[0] if pure_command and candidates else None
    setup_errors = []
    if not pure_command:
        executable, setup_errors = _resolve_gui_interpreter(application_directory)

    if pure_command and executable is None:
        executable = sys.executable
    if executable is None:
        message = (
            "SyncEngine could not prepare its graphical interface. It tried to install PySide6 "
            "into a private runtime and did not change the Python environment that launched the app."
        )
        if setup_errors:
            message += "\n\n" + "\n".join(setup_errors)
        message += (
            "\n\nCheck that the computer has internet access and that SyncEngine can "
            "write to its runtime folder. "
        )
        message += "Setup details are in data/logs/dependency_setup.log."
        if gui:
            show_startup_message(message)
        elif sys.stderr is not None:
            sys.stderr.write(message + "\n")
        return 1

    command = [executable, str(application), "--runtime-ready"] + forwarded
    options = _hidden_process_options() if gui else {}
    options["env"] = _startup_environment(executable)
    try:
        if gui:
            log_directory = _startup_log_directory()
            if pure_command:
                return subprocess.call(command, **options)
            with (log_directory / "startup.log").open("a", encoding="utf-8") as log_stream:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_stream, stderr=log_stream, **options)
            return 0
        return subprocess.call(command, **options)
    except OSError as error:
        message = f"Could not start SyncEngine: {error}"
        if gui:
            show_startup_message(message)
        elif sys.stderr is not None:
            sys.stderr.write(message + "\n")
        return 1


def _association_interpreter():
    """Chooses a console-free Python when one accompanies the current runtime.

    Returns:
        str: Interpreter used to open associated action and plan files.
    """
    sibling = Path(sys.executable).with_name("pythonw.exe")
    if sibling.is_file():
        return str(sibling)
    discovered = shutil.which("pythonw.exe")
    return discovered or sys.executable


def _refresh_windows_associations():
    """Notifies Explorer after user-scoped file association changes.

    Returns:
        None: The notification is issued when the Windows shell is available.
    """
    try:
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)
    except (AttributeError, OSError):
        pass


def register_file_associations():
    """Registers plan and action extensions for this user after an explicit click.

    Uses HKCU only. Plan files open for editing; action files request the countdown
    and automatic sync defined in their embedded plan.

    Returns:
        bool: True after registration.

    Raises:
        OSError: If Windows registration is unavailable or fails.
    """
    if os.name != "nt":
        raise OSError("File association registration is supported on Windows only.")
    import winreg

    launcher = Path(__file__).resolve().parent / "launch_sync_engine.pyw"
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}" "%1"'
    else:
        interpreter = _association_interpreter()
        command = f'"{interpreter}" "{launcher}" "%1"'
    for extension, program_id, description in (
        (".syncplan", "SyncEngine.Plan", "SyncEngine plan"),
        (".syncrun", "SyncEngine.Action", "SyncEngine automatic action"),
        (".relayplan", "SyncEngine.Plan", "SyncEngine plan"),
        (".relayrun", "SyncEngine.Action", "SyncEngine automatic action"),
    ):
        for suffix, value in (
            (extension, program_id),
            (program_id, description),
            (f"{program_id}\\shell\\open\\command", command),
        ):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{suffix}") as registry_key:
                winreg.SetValueEx(registry_key, "", 0, winreg.REG_SZ, value)
    _refresh_windows_associations()
    return True


def unregister_file_associations():
    """Removes this app's own association values without changing unrelated keys.

    Returns:
        bool: True after unregistering owned association values.

    Raises:
        OSError: If Windows registration is unavailable or removal fails.
    """
    if os.name != "nt":
        raise OSError("File association registration is supported on Windows only.")
    import winreg

    for extension, program_id in (
        (".syncplan", "SyncEngine.Plan"), (".syncrun", "SyncEngine.Action"),
        (".relayplan", "SyncEngine.Plan"), (".relayrun", "SyncEngine.Action"),
    ):
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{extension}", 0,
                winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE,
            ) as registry_key:
                if winreg.QueryValueEx(registry_key, "")[0] == program_id:
                    winreg.DeleteValue(registry_key, "")
        except FileNotFoundError:
            pass
        for suffix in ("\\shell\\open\\command", "\\shell\\open", "\\shell", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{program_id}{suffix}")
            except FileNotFoundError:
                pass
            except OSError:
                # Preserve any additional keys created by another application.
                pass
    _refresh_windows_associations()
    return True
