"""Runs one or more dropped SyncEngine .syncrun action files in order."""

import subprocess
import sys
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = APP_ROOT / "sync_engine"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from sync_engine_startup import (  # noqa: E402
    _hidden_process_options,
    _resolve_gui_interpreter,
    _startup_environment,
    show_startup_message,
)


def main(action_paths=None):
    """Launches each dropped action and stops if one is canceled or fails.

    Args:
        action_paths (list[str] or None): Dropped .syncrun paths, defaulting to
            command-line arguments supplied by Windows Explorer.

    Returns:
        int: Zero on success, otherwise the child or validation error code.
    """
    paths = list(sys.argv[1:] if action_paths is None else action_paths)
    if not paths:
        show_startup_message(
            "Drag one or more .syncrun files onto "
            "scripts/RunSyncActions.pyw to run them."
        )
        return 2

    actions = []
    for raw_path in paths:
        action_path = Path(raw_path).expanduser()
        if action_path.suffix.lower() != ".syncrun":
            show_startup_message(f"This runner accepts .syncrun files only:\n\n{raw_path}")
            return 2
        if not action_path.is_file():
            show_startup_message(f"The action file could not be found:\n\n{action_path}")
            return 2
        actions.append(action_path.resolve())

    executable, setup_errors = _resolve_gui_interpreter(APP_ROOT)
    if executable is None:
        message = "SyncEngine could not prepare its graphical interface."
        if setup_errors:
            message += "\n\n" + "\n".join(setup_errors)
        show_startup_message(message)
        return 1

    application = PACKAGE_ROOT / "sync_engine.py"
    for action_path in actions:
        command = [
            executable, str(application), "--runtime-ready", "--run", str(action_path),
        ]
        try:
            process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                env=_startup_environment(executable),
                **_hidden_process_options(),
            )
            result = process.wait()
        except OSError as error:
            show_startup_message(f"Could not start SyncEngine for {action_path}:\n\n{error}")
            return 1
        if result:
            if result != 3:
                show_startup_message(
                    f"SyncEngine stopped while running this action:\n\n{action_path}\n\n"
                    f"Exit code: {result}. Check the SyncEngine log for details."
                )
            return result
    return 0


if __name__ == "__main__":
    sys.exit(main())
