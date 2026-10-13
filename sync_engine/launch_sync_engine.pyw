"""Double-click launcher that records failures without a console window."""

import sys
import traceback

from sync_engine_startup import _startup_log_directory, show_startup_message, startup_main


def main():
    """Runs the console-free launcher and records startup exceptions.

    Returns:
        int: Launcher exit status.
    """
    pure_command = any(argument in ("--headless", "--preview", "--help", "-h") for argument in sys.argv[1:])
    if pure_command:
        return startup_main(gui=False)
    try:
        log_directory = _startup_log_directory()
        with (log_directory / "launcher.log").open("a", encoding="utf-8") as log_stream:
            previous_stdout, previous_stderr = sys.stdout, sys.stderr
            try:
                sys.stdout = log_stream
                sys.stderr = log_stream
                return startup_main(gui=True)
            except Exception:
                traceback.print_exc()
                show_startup_message("SyncEngine could not start. See the app-local logs folder for details.")
                return 1
            finally:
                sys.stdout = previous_stdout
                sys.stderr = previous_stderr
    except OSError as error:
        show_startup_message(f"SyncEngine could not create a startup log: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
