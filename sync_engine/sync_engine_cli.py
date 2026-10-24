"""Pure-Python command line previews and scheduled backup execution."""

import json
import logging
import os
import sys
import threading
import time

from sync_engine_engine import build_preview, execute_preview, SyncCancelled
from sync_engine_locking import AlreadyRunningError, RunLock
from sync_engine_logging import configure_logging
from sync_engine_model import (
    ACTION_EXTENSION, LEGACY_ACTION_EXTENSIONS, PlanStore, format_size, load_action, load_plan,
)


logger = logging.getLogger("sync_engine")


def load_input(path):
    """Load a plan or automatic action and determine its execution intent.

    Args:
        path (str): Path of a .syncplan or .syncrun file.

    Returns:
        tuple: Validated plan and whether the file is an automatic action.
    """
    if str(path).lower().endswith((ACTION_EXTENSION,) + LEGACY_ACTION_EXTENSIONS):
        plan = load_action(path)
        plan["_sync_file_path"] = os.path.abspath(path)
        return plan, True
    plan = load_plan(path)
    plan["_sync_file_path"] = os.path.abspath(path)
    plan["automation"]["allow_deletions"] = False
    return plan, False


def describe_preview(preview):
    """Format a compact scan summary.

    Args:
        preview (dict): Engine preview.

    Returns:
        str: Human-readable operation counts and transfer size.
    """
    summary = preview["summary"]
    return (
        f"{summary.get('copy', 0)} new, {summary.get('update', 0)} changed, "
        f"{summary.get('move', 0)} files to move, {summary.get('delete', 0)} files to remove, "
        f"{summary.get('conflict', 0)} conflicts, "
        f"{summary.get('rmdir', 0)} folders to remove; "
        f"{summary.get('unchanged', 0)} unchanged; "
        f"{summary.get('skipped', 0)} skipped; "
        f"{format_size(summary.get('bytes', 0))} to transfer"
    )


def run_headless(options):
    """Preview or execute a backup without importing Qt.

    Args:
        options (argparse.Namespace): Parsed command line options.

    Returns:
        int: Exit code: 0 success, 1 failure, 2 invalid, 3 canceled, 4 busy.
    """
    cancel_event = threading.Event()
    log_path = ""
    last_report_time = [0.0]
    operation_deadline = [None]
    timed_out = [False]
    loaded_plan = [None]
    current_preview = [None]
    run_started = [0.0]

    def report_progress(data):
        """Print useful progress at most once per second.

        Args:
            data (dict): Engine progress values.
        """
        if (operation_deadline[0] is not None and time.monotonic() >= operation_deadline[0]
                and not timed_out[0]):
            timed_out[0] = True
            cancel_event.set()
        if options.json:
            return
        current_time = time.monotonic()
        if current_time - last_report_time[0] < 1.0:
            return
        last_report_time[0] = current_time
        phase = data.get("phase", "scan")
        if phase == "copy":
            completed = format_size(data.get("completed_bytes", 0))
            total = format_size(data.get("total_bytes", 0))
            print(f"Copying {completed} / {total}: {data.get('current_path', '')}", flush=True)
        elif phase == "delete" and data.get("message"):
            print(data["message"], flush=True)
        elif phase in ("scan", "compare"):
            print(f"{phase.capitalize()}: {data.get('scanned_files', 0)} files", flush=True)

    try:
        store = PlanStore()
        log_path = configure_logging(store.logs_directory)
        plan, is_action = load_input(options.preview or options.run or options.file)
        loaded_plan[0] = plan
        sync_state = store.load_sync_state(plan["id"])
        if options.preview:
            timeout_seconds = plan.get("execution", {}).get("timeout_seconds", 0)
            run_started[0] = time.monotonic()
            operation_deadline[0] = run_started[0] + timeout_seconds if timeout_seconds else None
            preview = build_preview(plan, cancel_event, report_progress, sync_state=sync_state)
            if options.json:
                print(json.dumps({
                    "name": plan["name"], "summary": preview["summary"],
                    "operations": preview["operations"],
                    "unchanged": preview.get("unchanged", []),
                    "skipped": preview.get("skipped", []),
                    "warnings": preview["warnings"],
                }, ensure_ascii=False, default=str))
            else:
                print(describe_preview(preview))
                preview_rows = list(preview["operations"])
                preview_rows.extend(preview.get("unchanged", []))
                preview_rows.extend(preview.get("skipped", []))
                for operation in preview_rows[:100]:
                    if operation.get("kind") == "skipped":
                        print(
                            f"  {'skipped':7} {operation['relative_path']} "
                            f"({operation.get('reason', 'excluded by rule')})"
                        )
                    else:
                        print(f"  {operation['kind']:7} {operation['destination']}")
                if len(preview_rows) > 100:
                    print("  More preview rows omitted; use --json for the complete preview.")
                for warning in preview["warnings"]:
                    print(f"Warning: {warning}")
            return 0

        start_mode = plan["automation"].get("start_mode", "countdown")
        if is_action and start_mode == "confirm":
            print(
                "This automatic action requires confirmation. Run it without --headless "
                "to confirm in the SyncEngine window.",
                file=sys.stderr,
            )
            return 2
        countdown = options.countdown
        if countdown is None:
            countdown = plan["automation"]["countdown_seconds"] if start_mode == "countdown" else 0
        if countdown and not options.json:
            print(f"Starting '{plan['name']}' in {countdown} seconds. Ctrl+C cancels.", flush=True)
        deadline = time.monotonic() + countdown
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        timeout_seconds = plan.get("execution", {}).get("timeout_seconds", 0)
        run_started[0] = time.monotonic()
        operation_deadline[0] = run_started[0] + timeout_seconds if timeout_seconds else None
        with RunLock(store.directory):
            preview = build_preview(plan, cancel_event, report_progress, sync_state=sync_state)
            current_preview[0] = preview
            if not options.json:
                print(describe_preview(preview), flush=True)
            allow_delete = is_action and plan["automation"].get(
                "allow_deletions", plan["automation"].get("allow_mirror", False)
            )
            result = execute_preview(preview, cancel_event, report_progress, allow_delete=allow_delete)
        if timed_out[0]:
            result["status"] = "timed_out"
            result.setdefault("warnings", []).append(
                "The configured timeout elapsed. Completed file operations were retained."
            )
        if result.get("status") == "succeeded" and result.get("sync_state"):
            store.save_sync_state(result["sync_state"])
        result["log_path"] = log_path
        if plan.get("execution", {}).get("generate_report"):
            from sync_engine_report import write_sync_report

            try:
                report_path = write_sync_report(
                    plan, current_preview[0], result, store.logs_directory / "reports",
                    elapsed_seconds=max(0, time.monotonic() - run_started[0]), log_path=log_path,
                )
                result["report_path"] = str(report_path)
            except (OSError, ValueError) as error:
                result.setdefault("warnings", []).append(
                    f"Sync completed, but its report could not be written: {error}"
                )
        if options.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(
                f"{result['status'].capitalize()}: {result.get('copied', 0)} copied, "
                f"{result.get('moved', 0)} moved, {result.get('deleted', 0)} removed. Log: {log_path}", flush=True,
            )
            for message in result.get("errors", []) + result.get("warnings", []):
                print(message, flush=True)
        return {"succeeded": 0, "cancelled": 3}.get(result["status"], 1)
    except (KeyboardInterrupt, SyncCancelled):
        cancel_event.set()
        if timed_out[0]:
            result = {
                "status": "timed_out", "copied": 0, "moved": 0, "deleted": 0, "bytes": 0,
                "errors": [], "warnings": ["The configured timeout elapsed before the run completed."],
                "log_path": log_path,
            }
            plan = loaded_plan[0]
            if (not options.preview and plan and plan.get("execution", {}).get("generate_report")
                    and 'store' in locals()):
                from sync_engine_report import write_sync_report

                try:
                    report_path = write_sync_report(
                        plan, current_preview[0], result, store.logs_directory / "reports",
                        elapsed_seconds=max(0, time.monotonic() - run_started[0]), log_path=log_path,
                    )
                    result["report_path"] = str(report_path)
                except (OSError, ValueError) as error:
                    result.setdefault("warnings", []).append(
                        f"Sync timed out, but its report could not be written: {error}"
                    )
            if options.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"Timed out. Completed copies are retained. Log: {log_path}", flush=True)
            return 1
        logger.info("Backup canceled")
        if options.json:
            print(json.dumps({"status": "cancelled", "log_path": log_path}))
        else:
            print("Canceled. Completed copies are retained; temporary copies are cleaned up.")
        return 3
    except AlreadyRunningError as error:
        logger.warning("%s", error)
        _report_error(str(error), options.json, log_path)
        return 4
    except Exception as error:
        logger.exception("Headless operation failed")
        _report_error(str(error), options.json, log_path)
        return 1


def _report_error(message, as_json, log_path):
    """Write one command line error in the selected format.

    Args:
        message (str): Description of the failure.
        as_json (bool): Whether machine-readable output was requested.
        log_path (str): Path to the detailed session log.
    """
    if as_json:
        print(json.dumps({"status": "failed", "errors": [message], "log_path": log_path}))
    else:
        print(f"Failed: {message}\nLog: {log_path}", flush=True)
