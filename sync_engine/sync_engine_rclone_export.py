"""Builds standalone Windows batch exports for rclone-backed plan pairs."""

import os
from pathlib import Path

from sync_engine_model import validate_plan
from sync_engine_rclone import is_rclone_path, parse_rclone_path


def _validate_batch_value(value, label):
    """Rejects text that cannot be represented safely in a batch variable.

    Args:
        value (str): Value written into the generated batch file.
        label (str): User-facing name for validation errors.

    Returns:
        str: Validated text.

    Raises:
        ValueError: If the value contains batch expansion or control characters.
    """
    value = str(value)
    if any(character in value for character in ('%', '"', '^')):
        raise ValueError(
            f"{label} contains %, a quote, or ^, which cannot be safely written to a BAT file."
        )
    if any(ord(character) < 32 for character in value):
        raise ValueError(
            f"{label} contains a control character that cannot be written to a BAT file."
        )
    return value


def _rclone_filter_pattern(rule):
    """Converts a supported SyncEngine exclusion rule to an rclone glob.

    Args:
        rule (dict): Normalized SyncEngine exclusion rule.

    Returns:
        str: Equivalent rclone glob pattern for the entry name.

    Raises:
        ValueError: If the rule cannot be represented without changing its meaning.
    """
    pattern = rule["pattern"]
    match = rule["match"]
    if "/" in pattern or "\\" in pattern:
        raise ValueError(
            f"The filter {pattern!r} contains a path separator; "
            "rclone BAT export needs name-based filters."
        )
    if match == "glob":
        raise ValueError(
            f"The glob filter {pattern!r} cannot be mapped safely to "
            "rclone's file and folder rules. "
            "Use a name-based match or remove the glob before exporting."
        )
    if any(character in pattern for character in "*?[]{}"):
        raise ValueError(
            f"The {match.replace('_', ' ')} filter {pattern!r} contains glob characters; "
            "change it to a Glob rule before exporting."
        )
    if match == "starts_with":
        return f"{pattern}*"
    if match == "ends_with":
        return f"*{pattern}"
    if match == "contains":
        return f"*{pattern}*"
    return pattern


def _filter_settings(rules, pair_index):
    """Creates filter variables and command flags for one folder pair.

    Args:
        rules (list[dict]): Normalized SyncEngine exclusion rules.
        pair_index (int): One-based folder-pair number.

    Returns:
        tuple[list[str], list[str]]: Batch variable lines and rclone flag lines.

    Raises:
        ValueError: If a filter effect, scope, or case setting cannot be mapped.
    """
    if not rules:
        return [], []
    rules = [rule for rule in rules if not (
        rule.get("match") == "glob"
        and rule.get("applies_to", "both") == "folders"
    )]
    if not rules:
        return [], []
    if any(rule.get("effect", "exclude") != "exclude" for rule in rules):
        raise ValueError("Rclone BAT export currently supports Exclude filters only.")
    case_settings = {bool(rule.get("ignore_case", True)) for rule in rules}
    if len(case_settings) > 1:
        raise ValueError(
            "Rclone BAT export cannot mix case-sensitive and case-insensitive filters "
            "in one folder pair."
        )

    variable_lines = []
    filter_variables = []
    for rule_index, rule in enumerate(rules, start=1):
        pattern = _rclone_filter_pattern(rule)
        scope = rule.get("applies_to", "both")
        patterns = []
        if scope in ("files", "both"):
            patterns.append(pattern)
        if scope in ("folders", "both") and rule.get("match") != "glob":
            patterns.extend((
                f"{pattern}/",
                f"{pattern}/**",
                f"**/{pattern}/",
                f"**/{pattern}/**",
            ))
        for pattern_index, filter_pattern in enumerate(dict.fromkeys(patterns), start=1):
            variable_name = f"PAIR_{pair_index}_FILTER_{rule_index}_{pattern_index}"
            filter_pattern = _validate_batch_value(filter_pattern, "Filter pattern")
            variable_lines.append(f'set "{variable_name}={filter_pattern}"')
            filter_variables.append(variable_name)

    flag_lines = []
    if case_settings == {True}:
        flag_lines.append("--ignore-case")
    for variable_name in filter_variables:
        flag_lines.append(f'--exclude "%{variable_name}%"')
    return variable_lines, flag_lines


def _resolve_rclone_path(path, field, configured_remotes):
    """Converts one plan path to an rclone command-line path.

    Args:
        path (str): Resolved local or SyncEngine rclone URI.
        field (str): Either source or destination.
        configured_remotes (set[str]): Names available in the app rclone config.

    Returns:
        tuple[str, bool]: Command-line path and whether it is a remote path.

    Raises:
        ValueError: If the path is invalid or references an unconfigured remote.
    """
    if is_rclone_path(path):
        remote_name, remote_folder = parse_rclone_path(path)
        if not remote_folder:
            raise ValueError(
                f"The rclone {field} must name a folder below the remote root: {path}"
            )
        if remote_name not in configured_remotes:
            raise ValueError(
                f"The rclone remote '{remote_name}' is not configured in Options > rclone."
            )
        command_path = f"{remote_name}:{remote_folder}"
        return _validate_batch_value(command_path, f"Rclone {field}"), True
    if not os.path.isabs(path):
        raise ValueError(f"The {field} must resolve to an absolute local path: {path}")
    return _validate_batch_value(os.path.normpath(path), field.title()), False


def _paths_overlap(first_path, second_path, first_is_remote, second_is_remote):
    """Checks whether two local or rclone paths identify overlapping folders.

    Args:
        first_path (str): First resolved path.
        second_path (str): Second resolved path.
        first_is_remote (bool): Whether the first path is an rclone path.
        second_is_remote (bool): Whether the second path is an rclone path.

    Returns:
        bool: Whether both paths use the same storage and overlap.
    """
    if first_is_remote != second_is_remote:
        return False
    if first_is_remote:
        first_remote, first_folder = first_path.split(":", 1)
        second_remote, second_folder = second_path.split(":", 1)
        if first_remote.casefold() != second_remote.casefold():
            return False
        first_parts = [part.casefold() for part in first_folder.split("/") if part]
        second_parts = [part.casefold() for part in second_folder.split("/") if part]
        common_count = min(len(first_parts), len(second_parts))
        return first_parts[:common_count] == second_parts[:common_count]
    first_canonical = os.path.normcase(os.path.realpath(os.path.abspath(first_path)))
    second_canonical = os.path.normcase(os.path.realpath(os.path.abspath(second_path)))
    try:
        common_path = os.path.commonpath([first_canonical, second_canonical])
    except ValueError:
        return False
    return common_path in (first_canonical, second_canonical)


def _validate_path_relationships(pair_data):
    """Rejects overlapping source and destination folders in exported plans.

    Args:
        pair_data (list[dict]): Resolved rclone batch folder pairs.

    Raises:
        ValueError: If any source overlaps a destination or destinations overlap.
    """
    for pair_index, pair in enumerate(pair_data):
        for other_index, other_pair in enumerate(pair_data):
            if _paths_overlap(
                    pair["source"], other_pair["destination"],
                    pair["source_is_remote"], other_pair["destination_is_remote"]):
                raise ValueError(
                    "Source and destination folders must not overlap, including across pairs."
                )
            if (other_index > pair_index
                    and _paths_overlap(
                        pair["destination"], other_pair["destination"],
                        pair["destination_is_remote"], other_pair["destination_is_remote"])):
                raise ValueError("Destination folders must not overlap across pairs.")


def _format_modify_window(timestamp_tolerance):
    """Formats the timestamp window used for rclone-backed comparisons.

    Args:
        timestamp_tolerance (float): Plan tolerance in seconds.

    Returns:
        str: rclone duration string.
    """
    # SyncEngine treats rclone timestamps as having at least 1 ms precision.
    seconds = max(float(timestamp_tolerance), 0.001)
    return f"{format(seconds, '.9g')}s"


def _pair_runtime_lines(pair_index, source_is_remote, mode, allow_empty_mirror,
                        filter_flag_lines):
    """Builds runtime validation and safety checks for one pair.

    Args:
        pair_index (int): One-based folder-pair number.
        source_is_remote (bool): Whether the source uses an rclone remote.
        mode (str): SyncEngine mode for the pair.
        allow_empty_mirror (bool): Whether an empty source may clear a mirror target.
        filter_flag_lines (list[str]): Exclusion flags for the pair.

    Returns:
        tuple[list[str], list[str], list[str]]: Runtime validation lines,
            optional source scan command lines, and post-scan validation lines.
    """
    source_name = f"PAIR_{pair_index}_SOURCE"
    source_list_name = f"PAIR_{pair_index}_SOURCE_LIST"
    lines = []
    source_scan_lines = []
    post_scan_lines = []
    if not source_is_remote:
        lines.extend((
            f'set "FAILURE_REASON=The source folder for pair {pair_index} is unavailable."',
            f'if not exist "%{source_name}%\\." goto :failed',
        ))
    if mode == "mirror" and not allow_empty_mirror:
        lines.extend((
            f'echo [%TIME%] Checking included source files for mirror pair {pair_index}.',
        ))
        source_scan_lines = [
            f'"%RCLONE_EXE%" --config "%RCLONE_CONFIG%" --log-file "%LOG_FILE%" '
            f'--log-level "%RCLONE_LOG_LEVEL%" --modify-window "%RCLONE_MODIFY_WINDOW%" '
            f'lsf --recursive --files-only "%{source_name}%" ^',
        ]
        source_scan_lines.extend(f"    {flag} ^" for flag in filter_flag_lines)
        source_scan_lines.append(f'    > "%{source_list_name}%"')
        post_scan_lines.extend((
            f'for %%F in ("%{source_list_name}%") do if %%~zF EQU 0 (',
            f'    del "%{source_list_name}%" >nul 2>&1',
            f'    set "FAILURE_REASON=Mirror pair {pair_index} has no included source files; '
            'destination was left unchanged."',
            '    goto :failed',
            ')',
            f'del "%{source_list_name}%" >nul 2>&1',
        ))
    return lines, source_scan_lines, post_scan_lines


def _append_worker_task(lines, worker_commands, task_name, command_lines):
    """Adds an asynchronous rclone command while preserving its terminal output.

    Args:
        lines (list[str]): Parent batch lines.
        worker_commands (list[str]): Commands dispatched to worker labels.
        task_name (str): Unique batch-safe task identifier.
        command_lines (list[str]): Rclone command lines executed by the worker.

    Returns:
        None: The parent and worker line lists are updated in place.
    """
    wait_label = f"wait_for_{task_name}"
    finished_label = f"{task_name}_finished"
    worker_label = f"rclone_worker_{task_name}"
    lines.extend((
        'echo.',
        'if exist "%WORKER_EXIT_FILE%" del /q "%WORKER_EXIT_FILE%" >nul 2>&1',
        'if exist "%WORKER_EXIT_FILE%.tmp" del /q "%WORKER_EXIT_FILE%.tmp" >nul 2>&1',
        f'start "" /b "%ComSpec%" /d /c ""%~f0" __rclone_worker '
        f'"%WORKER_EXIT_FILE%" "{task_name}""',
        'if errorlevel 1 goto :failed',
        f":{wait_label}",
        f'if exist "%WORKER_EXIT_FILE%" goto :{finished_label}',
        'timeout /t 1 /nobreak >nul',
        f'goto :{wait_label}',
        f":{finished_label}",
        'set /p "RCLONE_EXIT_CODE=" < "%WORKER_EXIT_FILE%"',
        'del /q "%WORKER_EXIT_FILE%" >nul 2>&1',
        'del /q "%WORKER_EXIT_FILE%.tmp" >nul 2>&1',
        'if not "%RCLONE_EXIT_CODE%"=="0" goto :failed',
    ))
    worker_commands.extend((
        f":{worker_label}",
        *command_lines,
        'set "WORKER_EXIT_CODE=%ERRORLEVEL%"',
        'goto :rclone_worker_finished',
        '',
    ))


def build_rclone_batch(plan, rclone_executable, config_path, configured_remotes,
                       allow_deletions=False):
    """Builds a standalone BAT file for a plan whose pairs use rclone.

    Args:
        plan (dict): Plan with resolved local paths and rclone URIs.
        rclone_executable (str or pathlib.Path): Installed rclone executable.
        config_path (str or pathlib.Path): Private rclone configuration file.
        configured_remotes (list[str]): Names configured in that file.
        allow_deletions (bool): Explicit authorization for Mirror or Move pairs.

    Returns:
        str: UTF-8 batch file contents.

    Raises:
        ValueError: If the plan cannot be represented faithfully as an rclone BAT.
    """
    normalized = validate_plan(plan)
    if not normalized["pairs"]:
        raise ValueError("Add at least one folder pair before exporting an rclone BAT.")
    for pair_index, pair in enumerate(normalized["pairs"], start=1):
        if not (is_rclone_path(pair["source"]) or is_rclone_path(pair["destination"])):
            raise ValueError(
                f"Folder pair {pair_index} has no rclone path. Every pair needs an rclone source "
                "or destination to export an rclone-only BAT."
            )

    executable = Path(rclone_executable) if rclone_executable else None
    config_file = Path(config_path) if config_path else None
    if executable is None or not executable.is_file():
        raise ValueError("Set up rclone in Options before exporting an rclone BAT.")
    if config_file is None or not config_file.is_file():
        raise ValueError(
            "The private rclone configuration is missing. "
            "Configure a remote in Options > rclone."
        )
    configured_remote_names = set(configured_remotes or [])
    if not configured_remote_names:
        raise ValueError("Configure an rclone remote in Options before exporting an rclone BAT.")
    _validate_batch_value(str(executable), "Rclone executable path")
    _validate_batch_value(str(config_file), "Rclone configuration path")
    if normalized["execution"].get("timeout_seconds", 0):
        raise ValueError(
            "Rclone BAT export cannot preserve SyncEngine's overall run timeout. "
            "Set the plan timeout to 0 before exporting."
        )

    pair_data = []
    for pair_index, pair in enumerate(normalized["pairs"], start=1):
        source, source_is_remote = _resolve_rclone_path(
            pair["source"].strip(), "source", configured_remote_names,
        )
        destination, destination_is_remote = _resolve_rclone_path(
            pair["destination"].strip(), "destination", configured_remote_names,
        )
        if not source_is_remote and not destination_is_remote:
            raise ValueError(
                f"Folder pair {pair_index} has no rclone path. Every pair needs an rclone source "
                "or destination to export an rclone-only BAT."
            )
        mode = pair["mode"]
        if mode == "two_way":
            raise ValueError(
                f"Folder pair {pair_index} uses Two-Way mode. Two-Way plans need SyncEngine's "
                "conflict history and cannot be exported as a simple rclone BAT."
            )
        if mode in ("mirror", "move") and not allow_deletions:
            raise ValueError("Mirror and Move BAT exports require explicit removal authorization.")
        if normalized.get("compare", "metadata") != "metadata":
            raise ValueError(
                "Rclone BAT export currently supports Size and Modified Time comparison. "
                "SyncEngine's content comparison uses different hash behavior."
            )
        filter_variable_lines, filter_flag_lines = _filter_settings(
            pair.get("exclusions", []), pair_index,
        )
        pair_data.append({
            "index": pair_index,
            "source": source,
            "destination": destination,
            "source_is_remote": source_is_remote,
            "destination_is_remote": destination_is_remote,
            "mode": mode,
            "filter_variable_lines": filter_variable_lines,
            "filter_flag_lines": filter_flag_lines,
        })

        if not destination_is_remote and os.path.dirname(destination) == destination:
            raise ValueError("A filesystem root cannot be used as an rclone BAT destination.")

    _validate_path_relationships(pair_data)
    modify_window = _format_modify_window(normalized.get("timestamp_tolerance", 0.0))

    lines = [
        "@echo off",
        "setlocal EnableExtensions DisableDelayedExpansion",
        "chcp 65001 >nul",
        "rem Generated from SyncEngine. Re-export after changing the plan.",
        f'set "RCLONE_EXE={executable}"',
        f'set "RCLONE_CONFIG={config_file}"',
        'set "RCLONE_LOG_LEVEL=NOTICE"',
        'set "RCLONE_PROGRESS_FLAGS=--progress --stats 1s"',
        f'set "RCLONE_MODIFY_WINDOW={modify_window}"',
        'set "LOG_FILE=%~dpn0.log"',
        'set "WORKER_EXIT_FILE=%TEMP%\\syncengine_rclone_%RANDOM%_%RANDOM%_%RANDOM%.exit"',
        'set "FAILURE_REASON="',
        'set "FINAL_MESSAGE="',
        'set "RCLONE_EXIT_CODE="',
    ]
    for pair in pair_data:
        pair_index = pair["index"]
        source_name = f"PAIR_{pair_index}_SOURCE"
        destination_name = f"PAIR_{pair_index}_DESTINATION"
        source_list_name = f"PAIR_{pair_index}_SOURCE_LIST"
        action = {
            "add": "copy",
            "update": "copy",
            "mirror": "sync",
            "move": "move",
        }[pair["mode"]]
        lines.extend((
            f'set "{source_name}={pair["source"]}"',
            f'set "{destination_name}={pair["destination"]}"',
            f'set "PAIR_{pair_index}_ACTION={action}"',
            f'set "PAIR_{pair_index}_MODE={pair["mode"]}"',
        ))
        if pair["mode"] == "mirror" and not normalized.get("allow_empty_mirror", False):
            lines.append(
                f'set "{source_list_name}=%WORKER_EXIT_FILE%.pair_{pair_index}.lst"'
            )
        lines.extend(pair["filter_variable_lines"])
    lines.append('if /i "%~1"=="__rclone_worker" goto :rclone_worker')

    lines.extend((
        "",
        "echo.",
        f"echo [%DATE% %TIME%] Preparing rclone plan with {len(pair_data)} folder pair(s).",
        "echo The following transfers will run:",
    ))
    for pair in pair_data:
        pair_index = pair["index"]
        source_name = f"PAIR_{pair_index}_SOURCE"
        destination_name = f"PAIR_{pair_index}_DESTINATION"
        lines.extend((
            f'echo Pair {pair_index}: {pair["mode"]}.',
            f'echo Source: "%{source_name}%"',
            f'echo Destination: "%{destination_name}%"',
        ))
        filter_flags = pair["filter_flag_lines"]
        if filter_flags:
            case_matching = (
                "case-insensitive" if "--ignore-case" in filter_flags else "case-sensitive"
            )
            lines.extend((
                "echo Filters: excluded entries are skipped.",
                f"echo   Case matching: {case_matching}.",
            ))
            lines.extend(
                f"echo   {flag}" for flag in filter_flags if flag != "--ignore-case"
            )
        else:
            lines.append("echo Filters: none.")
    lines.extend((
        'echo Issues and final results will be logged to:',
        'echo "%LOG_FILE%"',
        'type nul > "%LOG_FILE%"',
        'if errorlevel 1 (',
        '    echo FAILED: Could not create the log file: "%LOG_FILE%"',
        '    exit /b 1',
        ')',
        'if not exist "%RCLONE_EXE%" (',
        '    set "FAILURE_REASON=The configured rclone executable was not found."',
        '    goto :failed',
        ')',
        'if not exist "%RCLONE_CONFIG%" (',
        '    set "FAILURE_REASON=The configured rclone config file was not found."',
        '    goto :failed',
        ')',
    ))

    worker_commands = []
    worker_task_names = []
    for pair in pair_data:
        pair_index = pair["index"]
        source_name = f"PAIR_{pair_index}_SOURCE"
        destination_name = f"PAIR_{pair_index}_DESTINATION"
        mode = pair["mode"]
        action = f"%PAIR_{pair_index}_ACTION%"
        lines.extend((
            "",
            f'echo [%TIME%] Running pair {pair_index}: {mode}.',
            f'echo Source: "%{source_name}%"',
            f'echo Destination: "%{destination_name}%"',
            f'set "FAILURE_REASON=Pair {pair_index} ({mode}) failed. Review the log file."',
        ))
        runtime_lines, source_scan_lines, post_scan_lines = _pair_runtime_lines(
            pair_index, pair["source_is_remote"], mode,
            normalized.get("allow_empty_mirror", False), pair["filter_flag_lines"],
        )
        lines.extend(runtime_lines)
        if source_scan_lines:
            worker_task_names.append(f"pair_{pair_index}_preflight")
            lines.append(
                f'set "FAILURE_REASON=Could not inspect source files for mirror pair {pair_index}."'
            )
            _append_worker_task(
                lines, worker_commands, f"pair_{pair_index}_preflight", source_scan_lines,
            )
            lines.extend(post_scan_lines)
        flags = list(pair["filter_flag_lines"])
        if mode == "add":
            flags.append("--ignore-existing")
        flags.append("--create-empty-src-dirs")
        command_lines = [
            f'"%RCLONE_EXE%" --config "%RCLONE_CONFIG%" --log-file "%LOG_FILE%" '
            f'--log-level "%RCLONE_LOG_LEVEL%" --modify-window "%RCLONE_MODIFY_WINDOW%" '
            f'%RCLONE_PROGRESS_FLAGS% {action} ^',
            f'    "%{source_name}%" "%{destination_name}%" ^',
        ]
        command_lines.extend(f"    {flag} ^" for flag in flags[:-1])
        command_lines.append(f"    {flags[-1]}")
        worker_task_names.append(f"pair_{pair_index}_transfer")
        _append_worker_task(
            lines, worker_commands, f"pair_{pair_index}_transfer", command_lines,
        )
        lines.append('set "FAILURE_REASON="')

    lines.extend((
        "",
        'for %%F in ("%LOG_FILE%") do if %%~zF GTR 0 goto :completed_with_warnings',
        'set "FINAL_MESSAGE=SUCCESS: The rclone sync plan completed at %DATE% %TIME%."',
        'goto :finished',
        "",
        ":completed_with_warnings",
        'set "FINAL_MESSAGE=WARNING: The rclone sync plan completed, but rclone logged a notice. '
        'Review the log."',
        "",
        ":finished",
        "echo.",
        "echo %FINAL_MESSAGE%",
        'echo Log: "%LOG_FILE%"',
        '>>"%LOG_FILE%" echo %FINAL_MESSAGE%',
        "exit /b 0",
        "",
        ":failed",
        'if exist "%WORKER_EXIT_FILE%" del /q "%WORKER_EXIT_FILE%" >nul 2>&1',
        'if exist "%WORKER_EXIT_FILE%.tmp" del /q "%WORKER_EXIT_FILE%.tmp" >nul 2>&1',
    ))
    for pair in pair_data:
        if pair["mode"] == "mirror" and not normalized.get("allow_empty_mirror", False):
            lines.append(
                f'if exist "%PAIR_{pair["index"]}_SOURCE_LIST%" '
                f'del /q "%PAIR_{pair["index"]}_SOURCE_LIST%" >nul 2>&1'
            )
    lines.extend((
        'set "FINAL_MESSAGE=FAILED: %FAILURE_REASON% At %DATE% %TIME%."',
        "echo.",
        "echo %FINAL_MESSAGE%",
        'echo Log: "%LOG_FILE%"',
        '>>"%LOG_FILE%" echo %FINAL_MESSAGE%',
        "exit /b 1",
        "",
        ":rclone_worker",
        'set "WORKER_EXIT_FILE=%~2"',
    ))
    for pair in pair_data:
        if pair["mode"] == "mirror" and not normalized.get("allow_empty_mirror", False):
            pair_index = pair["index"]
            lines.append(
                f'set "PAIR_{pair_index}_SOURCE_LIST='
                f'%WORKER_EXIT_FILE%.pair_{pair_index}.lst"'
            )
    for task_name in worker_task_names:
        lines.append(f'if /i "%~3"=="{task_name}" goto :rclone_worker_{task_name}')
    lines.extend((
        'set "WORKER_EXIT_CODE=1"',
        'goto :rclone_worker_finished',
        "",
        *worker_commands,
        ":rclone_worker_finished",
        'set "WORKER_EXIT_TEMP=%~2.tmp"',
        '>"%WORKER_EXIT_TEMP%" echo %WORKER_EXIT_CODE%',
        'move /y "%WORKER_EXIT_TEMP%" "%~2" >nul',
        'exit /b %WORKER_EXIT_CODE%',
    ))
    return "\r\n".join(lines) + "\r\n"


def save_rclone_batch(contents, path):
    """Writes a generated rclone batch file as UTF-8 with Windows line endings.

    Args:
        contents (str): Batch file contents returned by :func:`build_rclone_batch`.
        path (str or pathlib.Path): File selected by the user.

    Returns:
        pathlib.Path: Destination path written.
    """
    destination = Path(path)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        stream.write(contents)
    return destination
