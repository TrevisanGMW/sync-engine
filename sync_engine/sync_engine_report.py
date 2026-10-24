"""Plain-text sync reports with planned and completed operation totals."""

import datetime
import os
import re
import tempfile
from pathlib import Path

from sync_engine_model import format_size


def _format_duration(value):
    """Formats an elapsed duration without requiring the Qt widget module."""
    seconds = max(0, int(value or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def build_sync_report(plan, preview, result, elapsed_seconds=0, log_path=""):
    """Builds a readable report from one preview and its execution result.

    Args:
        plan (dict): Validated plan used for the run.
        preview (dict or None): Preview created before execution.
        result (dict): Completed operation result.
        elapsed_seconds (int or float): Total scan and transfer duration.
        log_path (str): Session log associated with the run.

    Returns:
        str: Complete UTF-8 report text.
    """
    preview = preview or {}
    result = result or {}
    summary = preview.get("summary", {})
    status = "Timed out" if result.get("status") == "timed_out" else str(
        result.get("status", "unknown")
    ).replace("_", " ").capitalize()
    created = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    planned_removed = int(summary.get("delete", 0))
    planned_folders = int(summary.get("rmdir", 0))
    lines = [
        "SyncEngine Report",
        "=" * 60,
        f"Sync Plan: {plan.get('name', 'Untitled')}",
        f"Created: {created}",
        f"Status: {status}",
        f"Duration: {_format_duration(elapsed_seconds)}",
        "",
        "Summary",
        "-" * 60,
        f"Files transferred: {int(result.get('copied', 0)):,}",
        f"Files moved: {int(result.get('moved', 0)):,}",
        f"Data transferred: {format_size(result.get('bytes', 0))}",
        f"Files removed: {int(result.get('deleted', 0)):,}",
        f"Already up to date: {int(summary.get('unchanged', 0)):,}",
        f"New files planned: {int(summary.get('copy', 0)):,}",
        f"Changed files planned: {int(summary.get('update', 0)):,}",
        f"Files planned to move: {int(summary.get('move', 0)):,}",
        f"Removals planned: {planned_removed:,} files, {planned_folders:,} folders",
        f"Conflicts encountered: {int(summary.get('conflict', 0)):,} "
        f"({int(summary.get('conflict_resolved', 0)):,} resolved)",
        f"Excluded items skipped: {int(summary.get('skipped', len(preview.get('skipped', [])))):,}",
        f"Data planned: {format_size(summary.get('bytes', 0))}",
    ]
    if log_path:
        lines.extend([f"Session log: {log_path}"])
    errors = list(result.get("errors", []))
    warnings = list(result.get("warnings", [])) + list(preview.get("warnings", []))
    if errors:
        lines.extend(["", "Errors", "-" * 60])
        lines.extend(f"- {error}" for error in errors)
    if warnings:
        lines.extend(["", "Warnings", "-" * 60])
        lines.extend(f"- {warning}" for warning in warnings)
    operations = list(preview.get("operations", [])) + list(preview.get("skipped", []))
    if operations:
        lines.extend(["", "Operations", "-" * 60])
        maximum_rows = 10000
        for operation in operations[:maximum_rows]:
            kind = str(operation.get("kind", "item")).replace("_", " ").capitalize()
            pair_name = operation.get("pair_name") or operation.get("pair_id") or "Folder Pair"
            path = operation.get("relative_path") or operation.get("destination") or "(folder)"
            size = operation.get("size")
            size_text = f" ({format_size(size)})" if size else ""
            reason = operation.get("reason")
            detail = f" — {reason}" if reason else ""
            lines.append(f"{kind}: {pair_name} / {path}{size_text}{detail}")
        if len(operations) > maximum_rows:
            lines.append(f"… {len(operations) - maximum_rows:,} additional operation rows omitted.")
    lines.append("")
    return "\n".join(lines)


def write_sync_report(plan, preview, result, directory, elapsed_seconds=0, log_path=""):
    """Writes an atomic, timestamped report under the chosen report directory.

    Args:
        plan (dict): Validated plan used for the run.
        preview (dict or None): Preview created before execution.
        result (dict): Completed operation result.
        directory (str or pathlib.Path): Destination report folder.
        elapsed_seconds (int or float): Total scan and transfer duration.
        log_path (str): Session log associated with the run.

    Returns:
        pathlib.Path: Path of the report file.
    """
    destination = Path(directory).expanduser().absolute()
    destination.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", str(plan.get("name") or "Sync Plan")).strip("._")
    safe_name = safe_name or "Sync Plan"
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    report_path = destination / f"{safe_name}-{timestamp}.txt"
    suffix = 2
    while report_path.exists():
        report_path = destination / f"{safe_name}-{timestamp}-{suffix}.txt"
        suffix += 1
    text = build_sync_report(plan, preview, result, elapsed_seconds, log_path)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{report_path.name}.",
            suffix=".tmp", dir=str(destination), delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(report_path))
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return report_path
