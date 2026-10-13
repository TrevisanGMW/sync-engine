"""Standalone SyncEngine entry point for GUI and scheduled backups."""

import argparse
import logging
import sys


def parse_arguments(arguments=None):
    """Parse app startup and command line backup options.

    Args:
        arguments (list or None): Command line arguments, or None for sys.argv.

    Returns:
        argparse.Namespace: Validated startup options.
    """
    parser = argparse.ArgumentParser(description="SyncEngine: preview and run file sync plans.")
    parser.add_argument("file", nargs="?", help="Open a .syncplan, or automatically run a .syncrun.")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--run", metavar="FILE", help="Automatically run the given plan or action.")
    operations.add_argument("--preview", metavar="FILE", help="Print a read-only preview without opening Qt.")
    parser.add_argument("--headless", action="store_true", help="Run without a window; suitable for Task Scheduler.")
    parser.add_argument("--minimized", action="store_true", help="Start an automatic GUI run minimized.")
    parser.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--countdown", type=int, metavar="SECONDS", help="Override the automatic start delay (0-86400).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON for command line preview/run results.")
    parser.add_argument("--runtime-ready", action="store_true", help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    if options.file and (options.run or options.preview):
        parser.error("Supply one input file using either a positional path, --run, or --preview.")
    if options.headless and not (options.file or options.run or options.preview):
        parser.error("--headless requires a .syncrun or --run FILE.")
    if options.countdown is not None and not 0 <= options.countdown <= 86400:
        parser.error("--countdown must be between 0 and 86400 seconds.")
    if options.json and not (options.headless or options.preview):
        parser.error("--json is available for --preview and --headless.")
    return options


def run_gui(options):
    """Construct the Qt window and begin an optional automatic action.

    Args:
        options (argparse.Namespace): Parsed app options.

    Returns:
        int: Qt application exit code.
    """
    from sync_engine_cli import load_input
    from sync_engine_controller import SyncEngineController
    from sync_engine_model import PlanStore
    from sync_engine_qt import QtGui, QtWidgets, application_exec
    from sync_engine_startup import set_windows_app_user_model_id
    from sync_engine_style import STYLESHEET, icon_path
    from sync_engine_view import SyncEngineView

    initial_plan = None
    automatic = False
    input_path = options.run or options.file
    if input_path:
        initial_plan, is_action = load_input(input_path)
        automatic = bool(options.run or is_action)
    set_windows_app_user_model_id()
    application = QtWidgets.QApplication.instance() or QtWidgets.QApplication([sys.argv[0]])
    application.setApplicationName("SyncEngine")
    application.setOrganizationName("SyncEngine")
    application.setStyle("Fusion")
    application.setStyleSheet(STYLESHEET)
    application.setWindowIcon(QtGui.QIcon(icon_path()))
    view = SyncEngineView()
    controller = SyncEngineController(
        view, PlanStore(), initial_plan=initial_plan, auto_run=automatic,
        countdown=options.countdown,
    )
    view.controller = controller
    if automatic:
        view.set_automatic_run_mode(True)

    def report_exception(exception_type, exception, traceback):
        """Record uncaught Qt slot errors in the session log.

        Args:
            exception_type (type): Type of the unexpected error.
            exception (Exception): Unexpected error instance.
            traceback (object): Python traceback.
        """
        logging.getLogger("sync_engine").error(
            "Unexpected application error", exc_info=(exception_type, exception, traceback),
        )
        view.set_status(f"Unexpected error: {exception}. See the session log.", "error")

    sys.excepthook = report_exception
    requires_confirmation = controller.plan["automation"].get("start_mode") == "confirm"
    minimized = automatic and not requires_confirmation and (
        options.minimized or controller.plan["automation"]["start_minimized"]
    )
    if minimized:
        view.showMinimized()
    else:
        view.show()
    return application_exec(application)


def main(arguments=None):
    """Dispatch read-only CLI, headless execution, or the GUI.

    Args:
        arguments (list or None): Command line arguments, or None for sys.argv.

    Returns:
        int: Process exit code.
    """
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    options = parse_arguments(arguments)
    if options.preview or options.headless:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        from sync_engine_cli import run_headless

        if options.headless and options.file and not options.file.lower().endswith((".syncrun", ".relayrun")):
            if not options.run:
                print("Use --run FILE to run a .syncplan headlessly; .syncrun actions run automatically.")
                return 2
        return run_headless(options)
    from sync_engine_startup import show_startup_message, startup_main

    if not options.runtime_ready and not getattr(sys, "frozen", False):
        return startup_main(arguments, gui=True)
    try:
        return run_gui(options)
    except Exception as error:
        logging.getLogger("sync_engine").exception("Could not start the GUI")
        show_startup_message(f"SyncEngine could not start.\n\n{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
