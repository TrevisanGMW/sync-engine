"""Pure Python plan validation, persistence, and local application storage."""

import copy
import ast
import datetime
import getpass
import json
import math
import os
import re
import shutil
import socket
import sys
import tempfile
import uuid
from pathlib import Path

from sync_engine_rclone import is_rclone_path


SCHEMA_VERSION = 1
PLAN_EXTENSION = ".syncplan"
ACTION_EXTENSION = ".syncrun"
LEGACY_PLAN_EXTENSIONS = (".relayplan",)
LEGACY_ACTION_EXTENSIONS = (".relayrun",)
_ENVIRONMENT_VARIABLE_PATTERN = re.compile(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}")
_ENVIRONMENT_VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_BUILT_IN_ENVIRONMENT_VARIABLES = frozenset({
    "date", "day", "desktop-dir", "documents-dir", "downloads-dir", "home-dir",
    "hostname", "month", "plan-dir", "plan-file", "plan-id", "plan-name",
    "plan-parent-dir", "project-dir", "project-file-dir", "sync-file", "sync-file-dir",
    "sync-file-parent", "temp-dir", "time", "user", "user-name", "year",
})


def new_pair(name="Folder Pair"):
    """Creates an editable folder pair without assigning any private paths.

    Args:
        name (str): Display name for the pair.

    Returns:
        dict: Fresh folder pair with a unique identifier.
    """
    return {
        "id": str(uuid.uuid4()),
        "name": name,
        "source": "",
        "destination": "",
        "mode": "update",
        "exclusions": [],
    }


def new_plan(name="Untitled"):
    """Creates a plan with one blank folder pair and safe automation defaults.

    Args:
        name (str): Display name for the plan.

    Returns:
        dict: Fresh serializable plan.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "id": str(uuid.uuid4()),
        "name": name,
        "pairs": [new_pair()],
        "variables": {},
        "compare": "metadata",
        "preview_view": "table",
        "list_skipped_folder_contents": False,
        "timestamp_tolerance": 0.0,
        "allow_empty_mirror": False,
        "execution": {
            "timeout_seconds": 0,
            "expand_log_while_syncing": False,
            "generate_report": False,
            "skip_source_removal_confirmation": False,
        },
        "automation": {
            "start_mode": "countdown",
            "countdown_seconds": 10,
            "start_minimized": False,
            "allow_deletions": False,
            "close_after_run": False,
        },
    }


def _identifier(value, label):
    """Validates and canonicalizes a UUID without permitting path traversal.

    Args:
        value (str): Identifier to validate.
        label (str): Description included in validation errors.

    Returns:
        str: Canonical UUID string.
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a UUID string.")
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{label} must be a valid UUID.") from error


def _string(value, label, fallback=None):
    """Validates textual values and optionally substitutes an empty display name.

    Args:
        value (str): Value to normalize.
        label (str): Description included in validation errors.
        fallback (str, optional): Display name used for empty text.

    Returns:
        str: Stripped text or the provided fallback.
    """
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{label} must be text without null characters.")
    normalized = value.strip()
    if fallback is not None:
        return normalized or fallback
    return normalized


def _boolean(value, label):
    """Validates a real Boolean rather than accepting integers or text.

    Args:
        value (bool): Value to validate.
        label (str): Description included in validation errors.

    Returns:
        bool: Validated Boolean.
    """
    if type(value) is not bool:
        raise ValueError(f"{label} must be true or false.")
    return value


def _patterns(value, label):
    """Validates exclusion lists and removes duplicate or empty entries.

    Args:
        value (list): Folder prefixes or filename glob patterns.
        label (str): Description included in validation errors.

    Returns:
        list: Independent normalized pattern list.
    """
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of text values.")
    patterns = []
    for entry in value:
        normalized = _string(entry, label)
        if normalized and normalized not in patterns:
            patterns.append(normalized)
    return patterns


def _normalize_variables(value):
    """Validates custom environment variables stored on a sync plan.

    Args:
        value (dict): Variable names mapped to text values.

    Returns:
        dict: Variables with clean, unbraced names.

    Raises:
        ValueError: If a name is malformed, repeated, or reserved.
    """
    if not isinstance(value, dict):
        raise ValueError("Custom environment variables must be an object.")
    normalized = {}
    seen = set()
    for raw_name, raw_value in value.items():
        if not isinstance(raw_name, str):
            raise ValueError("Custom environment variable names must be text.")
        name = raw_name.strip()
        if name.startswith("{") and name.endswith("}"):
            name = name[1:-1].strip()
        if not _ENVIRONMENT_VARIABLE_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"'{raw_name}' is not a valid environment variable name.")
        key = name.casefold()
        if key in _BUILT_IN_ENVIRONMENT_VARIABLES:
            raise ValueError(f"'{name}' is reserved for a built-in environment variable.")
        if key in seen:
            raise ValueError(f"Custom environment variable '{name}' is defined more than once.")
        if isinstance(raw_value, dict):
            if set(raw_value) - {"value", "query"}:
                raise ValueError(f"The definition for '{name}' contains unsupported options.")
            variable_value = raw_value.get("value", "")
            query = raw_value.get("query", False)
            if type(query) is not bool:
                raise ValueError(f"The Query option for '{name}' must be true or false.")
        else:
            variable_value = raw_value
            query = False
        if not isinstance(variable_value, str) or "\x00" in variable_value:
            raise ValueError(f"The value for '{name}' must be text without null characters.")
        seen.add(key)
        value = variable_value.strip()
        normalized[name] = {"value": value, "query": True} if query else value
    return normalized


def _built_in_environment_values(plan, file_path=None):
    """Builds environment and application variables for one plan context.

    Args:
        plan (dict): Plan supplying its stable name and identifier.
        file_path (str or pathlib.Path, optional): Active plan or action file.

    Returns:
        dict: Case-folded environment variable names mapped to string values.
    """
    file_path = file_path or plan.get("_sync_file_path")
    sync_file = os.path.abspath(os.fspath(file_path)) if file_path else ""
    sync_directory = os.path.dirname(sync_file) if sync_file else ""
    now = datetime.datetime.now()
    home_directory = os.path.expanduser("~")
    values = {name.casefold(): str(value) for name, value in os.environ.items()}
    values.update({
        "date": now.strftime("%Y-%m-%d"),
        "day": now.strftime("%d"),
        "desktop-dir": os.path.join(home_directory, "Desktop"),
        "documents-dir": os.path.join(home_directory, "Documents"),
        "downloads-dir": os.path.join(home_directory, "Downloads"),
        "home-dir": home_directory,
        "hostname": socket.gethostname(),
        "month": now.strftime("%m"),
        "plan-dir": sync_directory,
        "plan-file": sync_file,
        "plan-id": str(plan.get("id", "")),
        "plan-name": str(plan.get("name", "")),
        "plan-parent-dir": os.path.dirname(sync_directory) if sync_directory else "",
        "project-dir": sync_directory,
        "project-file-dir": sync_directory,
        "sync-file": sync_file,
        "sync-file-dir": sync_directory,
        "sync-file-parent": sync_directory,
        "temp-dir": tempfile.gettempdir(),
        "time": now.strftime("%H-%M-%S"),
        "user": getpass.getuser(),
        "user-name": getpass.getuser(),
        "year": now.strftime("%Y"),
    })
    return values


def get_available_environment_variables(plan=None, file_path=None):
    """Returns built-in and operating-system variable tokens for the Utilities menu.

    Args:
        plan (dict, optional): Active plan context.
        file_path (str or pathlib.Path, optional): Active plan or action file.

    Returns:
        dict: Braced variable tokens mapped to their current values.
    """
    context = plan or new_plan()
    values = _built_in_environment_values(context, file_path)
    return {
        f"{{{name}}}": values[name]
        for name in sorted(_BUILT_IN_ENVIRONMENT_VARIABLES)
    }


def _evaluate_query_expression(expression, lookup_environment, expand_text):
    """Evaluates a restricted, read-only expression for a custom variable.

    Supported calls are ``getenv``, ``join``, ``parent``, ``basename``,
    ``exists``, ``today``, ``now``, and ``expand``. Expressions can read
    ``env[\"NAME\"]`` or ``env.get(\"NAME\", default)``. Arbitrary Python,
    attribute access, imports, and assignments are not allowed.

    Args:
        expression (str): Query expression entered by the user.
        lookup_environment (callable): Resolves a built-in or custom token.
        expand_text (callable): Expands tokens in a query result.

    Returns:
        object: Result of the supported query expression.

    Raises:
        ValueError: If the expression uses unsupported syntax or fails.
    """
    functions = {
        "getenv": lambda name, default="": _query_lookup(lookup_environment, name, default),
        "join": lambda *parts: os.path.join(*(str(part) for part in parts)),
        "parent": lambda value: os.path.dirname(os.path.expanduser(str(value))),
        "basename": lambda value: os.path.basename(os.path.expanduser(str(value))),
        "exists": lambda value: os.path.exists(os.path.expanduser(str(value))),
        "today": lambda pattern="%Y-%m-%d": datetime.datetime.now().strftime(str(pattern)),
        "now": lambda pattern="%H-%M-%S": datetime.datetime.now().strftime(str(pattern)),
        "expand": lambda value: expand_text(str(value), []),
    }

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)):
            return node.value
        if isinstance(node, ast.Name) and node.id == "env":
            return "__sync_engine_environment__"
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == "env":
            key = evaluate(node.slice)
            return lookup_environment(str(key))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = evaluate(node.left)
            right = evaluate(node.right)
            if type(left) not in (str, int, float) or type(right) not in (str, int, float):
                raise ValueError("Query addition supports only text and numbers.")
            return left + right
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in functions:
                if node.keywords:
                    raise ValueError("Query functions use positional arguments only.")
                return functions[node.func.id](*(evaluate(argument) for argument in node.args))
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "env"):
                if not 1 <= len(node.args) <= 2 or node.keywords:
                    raise ValueError("env.get() accepts a name and an optional default.")
                key = evaluate(node.args[0])
                default = evaluate(node.args[1]) if len(node.args) == 2 else ""
                return _query_lookup(lookup_environment, key, default)
            raise ValueError("That query function is not available.")
        raise ValueError("Query supports text, env lookups, +, and the listed path/date functions only.")

    try:
        return evaluate(ast.parse(expression, mode="eval"))
    except (SyntaxError, TypeError) as error:
        raise ValueError(f"Invalid query expression: {error}") from error


def _query_lookup(lookup_environment, name, default=""):
    """Looks up a token for a query and uses a default when it is unknown."""
    try:
        return lookup_environment(str(name))
    except ValueError:
        return default


def _legacy_file_rule(pattern):
    """Maps common old glob patterns to the new literal match choices.

    Args:
        pattern (str): File glob from a legacy plan.

    Returns:
        tuple: Match mode and pattern for one exclusion rule.
    """
    if pattern.startswith("*") and pattern.count("*") == 1 and "?" not in pattern and len(pattern) > 1:
        return "ends_with", pattern[1:]
    if pattern.endswith("*") and pattern.count("*") == 1 and "?" not in pattern and len(pattern) > 1:
        return "starts_with", pattern[:-1]
    if "*" not in pattern and "?" not in pattern:
        return "exact", pattern
    return "glob", pattern


def normalize_exclusions(pair):
    """Validates new exclusion rows and migrates older folder/file filters.

    Args:
        pair (dict): Folder pair containing current rules or legacy filter fields.

    Returns:
        list: Normalized exclusion rows with pattern, match, and applies_to keys.

    Raises:
        ValueError: If an exclusion row or legacy filter is malformed.
    """
    if not isinstance(pair, dict):
        raise ValueError("A folder pair must be an object.")
    if "exclusions" in pair:
        rules = pair.get("exclusions")
        if not isinstance(rules, list):
            raise ValueError("Exclusions must be a list of rule objects.")
    else:
        rules = []
        old_match = pair.get("ignore_match", "prefix")
        if old_match not in ("prefix", "exact"):
            raise ValueError("Legacy folder exclusions must use prefix or exact matching.")
        folder_match = "starts_with" if old_match == "prefix" else "exact"
        for pattern in _patterns(pair.get("ignore_folders", []), "Ignored folder names"):
            rules.append({"pattern": pattern, "match": folder_match, "applies_to": "folders"})
        for pattern in _patterns(pair.get("ignore_files", []), "Ignored file patterns"):
            match, migrated_pattern = _legacy_file_rule(pattern)
            rules.append({"pattern": migrated_pattern, "match": match, "applies_to": "files"})

    normalized = []
    seen = set()
    match_aliases = {"prefix": "starts_with"}
    match_values = {"starts_with", "ends_with", "contains", "exact", "glob"}
    scope_values = {"files", "folders", "both"}
    for position, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValueError(f"Exclusion rule {position} must be an object.")
        pattern = _string(rule.get("pattern", ""), f"Exclusion rule {position} pattern")
        if not pattern:
            continue
        match = rule.get("match", "exact")
        if not isinstance(match, str):
            raise ValueError(f"Exclusion rule {position} match must be text.")
        match = match_aliases.get(match, match)
        if match not in match_values:
            raise ValueError(f"Exclusion rule {position} has an unknown match mode.")
        applies_to = rule.get("applies_to", "both")
        if not isinstance(applies_to, str) or applies_to not in scope_values:
            raise ValueError(f"Exclusion rule {position} must apply to files, folders, or both.")
        ignore_case = _boolean(rule.get("ignore_case", True), f"Exclusion rule {position} ignore_case")
        pattern_key = pattern.casefold() if ignore_case else pattern
        effect = rule.get("effect", "exclude")
        if not isinstance(effect, str) or effect not in ("include", "exclude"):
            raise ValueError(f"Exclusion rule {position} must use Include or Exclude.")
        key = (effect, pattern_key, match, applies_to, ignore_case)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "effect": effect,
            "pattern": pattern,
            "match": match,
            "applies_to": applies_to,
            "ignore_case": ignore_case,
        })
    return normalized


def validate_plan(plan):
    """Validates a plan and returns independent normalized data.

    Blank folder paths are valid while editing. The sync engine validates that
    runnable pairs contain safe, reachable paths before scanning or writing.

    Args:
        plan (dict): Plan data to validate.

    Returns:
        dict: Normalized deep copy containing the supported schema fields.

    Raises:
        ValueError: If data types, schema, identifiers, or options are invalid.
    """
    if not isinstance(plan, dict):
        raise ValueError("A plan must be a JSON object.")
    if type(plan.get("schema_version")) is not int or plan["schema_version"] != SCHEMA_VERSION:
        raise ValueError("This plan uses an unsupported schema version.")
    normalized = new_plan()
    normalized["id"] = _identifier(plan.get("id"), "Plan identifier")
    normalized["name"] = _string(plan.get("name", "Untitled"), "Plan name", "Untitled")
    normalized["variables"] = _normalize_variables(plan.get("variables", {}))
    preview_view = plan.get("preview_view", "table")
    if preview_view not in ("table", "tree"):
        raise ValueError("Preview view must be table or tree.")
    normalized["preview_view"] = preview_view
    normalized["list_skipped_folder_contents"] = _boolean(
        plan.get("list_skipped_folder_contents", False),
        "List skipped folder contents",
    )
    comparison = plan.get("compare", "metadata")
    if not isinstance(comparison, str) or comparison not in ("metadata", "content"):
        raise ValueError("Comparison must be metadata or content.")
    normalized["compare"] = comparison
    tolerance = plan.get("timestamp_tolerance", 0.0)
    try:
        valid_tolerance = (
            type(tolerance) in (int, float) and math.isfinite(tolerance) and 0 <= tolerance <= 3600
        )
    except OverflowError:
        valid_tolerance = False
    if not valid_tolerance:
        raise ValueError("Timestamp tolerance must be a finite number from 0 to 3600 seconds.")
    normalized["timestamp_tolerance"] = float(tolerance)
    normalized["allow_empty_mirror"] = _boolean(
        plan.get("allow_empty_mirror", False), "Allow empty mirror"
    )
    execution = plan.get("execution", {})
    if not isinstance(execution, dict):
        raise ValueError("Execution options must be an object.")
    timeout_seconds = execution.get("timeout_seconds", 0)
    if type(timeout_seconds) is not int or not 0 <= timeout_seconds <= 604800:
        raise ValueError("The sync timeout must be a whole number from 0 to 604800 seconds.")
    normalized["execution"] = {
        "timeout_seconds": timeout_seconds,
        "expand_log_while_syncing": _boolean(
            execution.get("expand_log_while_syncing", False), "Expand log while syncing"
        ),
        "generate_report": _boolean(execution.get("generate_report", False), "Generate report"),
        "skip_source_removal_confirmation": _boolean(
            execution.get("skip_source_removal_confirmation", False),
            "Skip source-removal confirmation",
        ),
    }
    automation = plan.get("automation", {})
    if not isinstance(automation, dict):
        raise ValueError("Automation options must be a JSON object.")
    countdown = automation.get("countdown_seconds", 10)
    if type(countdown) is not int or not 0 <= countdown <= 86400:
        raise ValueError("Countdown must be a whole number from 0 to 86400 seconds.")
    start_mode = automation.get("start_mode", "countdown")
    if start_mode not in ("immediate", "countdown", "confirm"):
        raise ValueError("Automatic start mode must be immediate, countdown, or confirm.")
    delete_permission = automation.get("allow_deletions", automation.get("allow_mirror", False))
    start_minimized = _boolean(automation.get("start_minimized", False), "Start minimized")
    close_after_run = _boolean(automation.get("close_after_run", False), "Close after automatic run")
    normalized["automation"] = {
        "start_mode": start_mode,
        "countdown_seconds": countdown,
        "start_minimized": start_minimized if start_mode != "confirm" else False,
        "allow_deletions": _boolean(delete_permission, "Automatic deletion permission"),
        "close_after_run": close_after_run,
    }
    pairs = plan.get("pairs", [])
    if not isinstance(pairs, list):
        raise ValueError("Folder pairs must be a list.")
    normalized["pairs"] = []
    seen_identifiers = {normalized["id"]}
    for position, pair in enumerate(pairs, start=1):
        if not isinstance(pair, dict):
            raise ValueError(f"Folder pair {position} must be a JSON object.")
        pair_id = _identifier(pair.get("id"), f"Folder pair {position} identifier")
        if pair_id in seen_identifiers:
            raise ValueError("Plan and folder pair identifiers must be unique.")
        seen_identifiers.add(pair_id)
        mode = pair.get("mode", "update")
        if not isinstance(mode, str) or mode not in ("add", "update", "mirror", "move", "two_way"):
            raise ValueError(f"Folder pair {position} has an unknown sync mode.")
        normalized["pairs"].append({
            "id": pair_id,
            "name": _string(pair.get("name", "Folder Pair"), "Pair name", "Folder Pair"),
            "source": _string(pair.get("source", ""), "Source folder"),
            "destination": _string(pair.get("destination", ""), "Destination folder"),
            "mode": mode,
            "exclusions": normalize_exclusions(pair),
        })
    return normalized


def _resolve_paths(plan, file_path):
    """Resolves nonempty relative folders against the containing plan file.

    Args:
        plan (dict): Validated plan whose paths will be normalized.
        file_path (str or pathlib.Path): File from which the plan was loaded.

    Returns:
        dict: Plan with resolved folder paths.
    """
    parent = Path(file_path).absolute().parent
    for pair in plan["pairs"]:
        for field in ("source", "destination"):
            value = pair[field]
            if not value:
                continue
            if field == "destination" and is_rclone_path(value):
                pair[field] = value.strip()
                continue
            if _ENVIRONMENT_VARIABLE_PATTERN.search(value):
                continue
            folder = Path(os.path.expandvars(os.path.expanduser(value)))
            if folder.drive and not folder.is_absolute():
                raise ValueError(f"{field.title()} uses an ambiguous drive-relative path.")
            if not folder.is_absolute():
                folder = parent / folder
            pair[field] = os.path.abspath(folder)
    return plan


def resolve_plan_paths(plan, file_path=None):
    """Expands plan variables and resolves source and destination folders.

    The plan retains its variable templates for editing and serialization. This
    function returns an expanded copy for preview and sync operations.

    Args:
        plan (dict): Sync plan containing source and destination templates.
        file_path (str or pathlib.Path, optional): Current .syncplan or
            .syncrun path, used by the sync-file variables and relative paths.

    Returns:
        dict: Independent copy with absolute source and destination paths.

    Raises:
        ValueError: If a variable is unknown or cyclic, or a path is invalid.
    """
    if not isinstance(plan, dict):
        raise ValueError("A plan must be a JSON object.")
    normalized = copy.deepcopy(plan)
    normalized["variables"] = _normalize_variables(plan.get("variables", {}))
    if not isinstance(normalized.get("pairs"), list):
        raise ValueError("Folder pairs must be a list.")
    file_path = file_path or plan.get("_sync_file_path")
    sync_file = os.path.abspath(os.fspath(file_path)) if file_path else ""
    sync_directory = os.path.dirname(sync_file) if sync_file else ""
    values = _built_in_environment_values(normalized, file_path)
    custom = {name.casefold(): value for name, value in normalized["variables"].items()}
    path_context_names = {
        "plan-dir", "plan-file", "plan-parent-dir", "project-dir", "project-file-dir",
        "sync-file", "sync-file-dir", "sync-file-parent",
    }

    def resolve_variable(name, stack):
        """Resolves one name and detects custom-variable cycles."""
        key = name.casefold()
        if key in stack:
            chain = " -> ".join(stack + [key])
            raise ValueError(f"Custom environment variables form a cycle: {chain}.")
        if key in custom:
            definition = custom[key]
            if isinstance(definition, dict) and definition.get("query"):
                def lookup_environment(query_name):
                    query_key = query_name.casefold()
                    if query_key in custom:
                        return resolve_variable(query_name, list(stack))
                    if query_key in values:
                        if (query_key in path_context_names and not sync_file
                                and query_key in _BUILT_IN_ENVIRONMENT_VARIABLES):
                            raise ValueError(f"{{{query_name}}} requires the sync plan or action file to have a path.")
                        return values[query_key]
                    raise ValueError(f"Unknown environment variable '{{{query_name}}}'.")

                try:
                    query_value = _evaluate_query_expression(
                        definition["value"], lookup_environment, expand_text,
                    )
                except Exception as error:
                    raise ValueError(f"Query for '{{{name}}}' failed: {error}") from error
                return str(query_value)
            value = definition.get("value", "") if isinstance(definition, dict) else definition
            return expand_text(value, stack + [key])
        if key in _BUILT_IN_ENVIRONMENT_VARIABLES and key in path_context_names and not sync_file:
            raise ValueError(f"{{{name}}} requires the sync plan or action file to have a path.")
        if key not in values:
            raise ValueError(f"Unknown environment variable '{{{name}}}'.")
        return values[key]

    def expand_text(value, stack=()):
        """Expands every known braced token while preserving ordinary text."""
        expanded = str(value)
        for unused_attempt in range(32):
            matches = list(_ENVIRONMENT_VARIABLE_PATTERN.finditer(expanded))
            if not matches:
                return expanded
            expanded = _ENVIRONMENT_VARIABLE_PATTERN.sub(
                lambda match: resolve_variable(match.group(1), list(stack)), expanded,
            )
        raise ValueError("Environment variable expansion exceeded 32 nested replacements.")

    parent_directory = Path(sync_directory) if sync_directory else None
    for pair in normalized["pairs"]:
        if not isinstance(pair, dict):
            raise ValueError("Every folder pair must be an object.")
        for field in ("source", "destination"):
            value = pair.get(field, "")
            if not value:
                continue
            expanded = os.path.expandvars(os.path.expanduser(expand_text(value)))
            if field == "destination" and is_rclone_path(expanded):
                pair[field] = expanded.strip()
                continue
            folder = Path(expanded)
            if folder.drive and not folder.is_absolute():
                raise ValueError(f"{field.title()} uses an ambiguous drive-relative path.")
            if not folder.is_absolute():
                if parent_directory is None:
                    pair[field] = expanded
                    continue
                folder = parent_directory / folder
            pair[field] = os.path.abspath(str(folder))
    return normalized


def evaluate_plan_variable(plan, name, file_path=None):
    """Evaluates one built-in or custom variable for the Variables tab.

    Args:
        plan (dict): Current plan, including unsaved variable definitions.
        name (str): Braced or unbraced variable name.
        file_path (str or pathlib.Path, optional): Current plan or action path.

    Returns:
        str: Resolved variable value.

    Raises:
        ValueError: If the variable is unknown or its query cannot be evaluated.
    """
    if not isinstance(name, str):
        raise ValueError("Variable name must be text.")
    key = name.strip().strip("{}").casefold()
    temporary = copy.deepcopy(plan)
    temporary["pairs"] = [new_pair()]
    temporary["pairs"][0]["source"] = "{" + key + "}"
    resolved = resolve_plan_paths(temporary, file_path=file_path)
    return resolved["pairs"][0]["source"]


def resolve_path_template(plan, path_template, file_path=None, field=None):
    """Resolves one path template with the active plan's environment values.

    Args:
        plan (dict): Current plan, including unsaved variable definitions.
        path_template (str): Source or destination path template to inspect.
        file_path (str or pathlib.Path, optional): Current plan or action path.
        field (str, optional): Path field being inspected, either ``source`` or ``destination``.

    Returns:
        str: Expanded absolute local path or normalized Google Drive destination URI.

    Raises:
        ValueError: If the template is empty, contains an unknown variable, or
            cannot be resolved with the current plan context.
    """
    if not isinstance(path_template, str) or not path_template.strip():
        raise ValueError("Enter a path before checking its resolved result.")
    temporary = validate_plan(plan)
    temporary["pairs"] = [new_pair()]
    field = field or ("destination" if is_rclone_path(path_template) else "source")
    if field not in ("source", "destination"):
        raise ValueError("The inspected path field must be source or destination.")
    temporary["pairs"][0][field] = path_template
    resolved = resolve_plan_paths(temporary, file_path=file_path)
    return resolved["pairs"][0][field]


def _read_json(path):
    """Reads UTF-8 JSON and wraps malformed data in a useful validation error.

    Args:
        path (str or pathlib.Path): File to read.

    Returns:
        object: Parsed JSON data.
    """
    try:
        with Path(path).open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("The selected file does not contain valid UTF-8 JSON.") from error


def _write_json(data, path):
    """Writes JSON atomically so an interrupted save preserves the prior file.

    Args:
        data (object): JSON-compatible data.
        path (str or pathlib.Path): Destination file.

    Returns:
        None: The file is written on success.
    """
    serialized = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    destination = Path(path).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=f".{destination.name}.",
            suffix=".tmp", dir=str(destination.parent), delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def load_plan(path):
    """Loads a plan for editing without authorizing automatic execution.

    Args:
        path (str or pathlib.Path): Saved plan file.

    Returns:
        dict: Validated plan with relative folder paths resolved.
    """
    data = _read_json(path)
    if isinstance(data, dict) and "file_type" in data:
        raise ValueError("This is an action file. Open actions with the action loader.")
    return _resolve_paths(validate_plan(data), path)


def save_plan(plan, path):
    """Saves editable plan data as readable atomic JSON.

    Args:
        plan (dict): Plan to save.
        path (str or pathlib.Path): Destination plan file.

    Returns:
        None: The validated plan is saved.
    """
    _write_json(validate_plan(plan), path)


def duplicate_plan(plan):
    """Duplicates a plan with fresh identifiers and an independent nested state.

    Args:
        plan (dict): Source plan.

    Returns:
        dict: Duplicate with fresh plan and pair identifiers.
    """
    duplicate = validate_plan(plan)
    duplicate["id"] = str(uuid.uuid4())
    duplicate["name"] = f"{duplicate['name']} copy"
    for pair in duplicate["pairs"]:
        pair["id"] = str(uuid.uuid4())
    return duplicate


def save_action(plan, path, allow_mirror=False, allow_deletions=None):
    """Exports a self-contained automatic action with explicit removal permission.

    Args:
        plan (dict): Plan embedded in the action.
        path (str or pathlib.Path): Destination action file.
        allow_mirror (bool): Legacy argument for automatic-removal authorization.
        allow_deletions (bool or None): Optional current name for the permission.

    Returns:
        None: The action is saved.

    Raises:
    ValueError: If a removal mode lacks explicit automatic-removal authorization.
    """
    normalized = validate_plan(plan)
    if allow_deletions is None:
        allow_deletions = allow_mirror
    permission = _boolean(allow_deletions, "Automatic removal permission")
    has_deletion_mode = any(pair["mode"] in ("mirror", "move", "two_way")
                            for pair in normalized["pairs"])
    if has_deletion_mode and not permission:
        raise ValueError("Exporting this action requires explicit automatic-removal permission.")
    normalized["automation"]["allow_deletions"] = permission
    _write_json({
        "file_type": "syncengine_action",
        "schema_version": SCHEMA_VERSION,
        "allow_deletions": permission,
        "plan": normalized,
    }, path)


def load_action(path):
    """Loads an action and validates its explicit automatic deletion permission.

    Args:
        path (str or pathlib.Path): Self-contained action file.

    Returns:
        dict: Embedded plan with resolved paths and validated authorization.

    Raises:
        ValueError: If the action schema or removal permission is invalid.
    """
    action = _read_json(path)
    if not isinstance(action, dict) or action.get("file_type") not in (
        "syncengine_action", "synctasker_action", "folder_relay_action",
    ):
        raise ValueError("The selected file is not a SyncEngine automatic action.")
    if type(action.get("schema_version")) is not int or action["schema_version"] != SCHEMA_VERSION:
        raise ValueError("This action uses an unsupported schema version.")
    normalized = validate_plan(action.get("plan"))
    permission = _boolean(
        action.get("allow_deletions", action.get("allow_mirror", False)), "Action deletion permission",
    )
    embedded_permission = normalized["automation"]["allow_deletions"]
    if permission != embedded_permission:
        raise ValueError("The action and embedded plan deletion permissions do not agree.")
    if any(pair["mode"] in ("mirror", "move", "two_way") for pair in normalized["pairs"]) and not permission:
        raise ValueError("This action has no explicit automatic removal permission.")
    return _resolve_paths(normalized, path)


def format_size(byte_count):
    """Formats a nonnegative byte count in compact binary units.

    Args:
        byte_count (int or float): Number of bytes.

    Returns:
        str: Human-readable size such as ``1.5 MiB``.
    """
    if type(byte_count) not in (int, float) or not math.isfinite(byte_count) or byte_count < 0:
        raise ValueError("Byte count must be a finite nonnegative number.")
    amount = float(byte_count)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024


def default_data_directory():
    """Returns the app-local default data location without machine-specific paths.

    Returns:
        pathlib.Path: Application data directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path(__file__).resolve().parent.parent / "data"


class PlanStore:
    """Manages plans, sync history, preferences, and logs independently of the UI."""

    def __init__(self, directory=None):
        """Creates the application storage folders.

        Args:
            directory (str or pathlib.Path, optional): Application data override.
        """
        self.preferences_directory = Path(directory) if directory is not None else default_data_directory()
        self.preferences_directory = self.preferences_directory.absolute()
        self.preferences_path = self.preferences_directory / "preferences.json"
        self.warnings = []
        self._initial_warnings = []
        saved_directory = self.read_preferences().get("projects_directory")
        if directory is not None:
            self.directory = Path(directory).absolute()
        elif isinstance(saved_directory, str) and saved_directory.strip():
            self.directory = Path(saved_directory).expanduser().absolute()
        else:
            self.directory = self.preferences_directory
        self.plans_directory = self.directory / "plans"
        self.state_directory = self.directory / "sync_state"
        self.actions_directory = self.directory / "actions"
        self.logs_directory = self.directory / "logs"
        try:
            self._create_storage_directories()
        except OSError as error:
            if directory is not None or not saved_directory or self.directory == self.preferences_directory:
                raise
            self.warnings.append(
                f"Could not open the selected project folder; using the tool default instead: {error}"
            )
            self.directory = self.preferences_directory
            self.plans_directory = self.directory / "plans"
            self.state_directory = self.directory / "sync_state"
            self.actions_directory = self.directory / "actions"
            self.logs_directory = self.directory / "logs"
            self._create_storage_directories()
        self._migrate_legacy_plans()
        self._initial_warnings = list(self.warnings)

    def _create_storage_directories(self):
        """Creates the selected project-storage folders."""
        self.plans_directory.mkdir(parents=True, exist_ok=True)
        self.state_directory.mkdir(parents=True, exist_ok=True)
        self.actions_directory.mkdir(parents=True, exist_ok=True)
        self.logs_directory.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy_plans(self):
        """Copies legacy app data into the new project folder once.

        Legacy plan files stay in place so another installation is unaffected.
        """
        preferences = self.read_preferences()
        if preferences.get("legacy_relay_plan_migration_complete"):
            return
        legacy_base = self.preferences_directory / "legacy-migration-unavailable"
        if os.name == "nt" and self._is_default_preferences_directory(self.preferences_directory):
            local_app_data = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
            if local_app_data:
                legacy_base = Path(local_app_data) / "FolderRelay"
        else:
            return
        legacy_plans = legacy_base / "plans"
        if not legacy_plans.is_dir():
            return
        current_ids = {path.stem for path in self.plans_directory.glob(f"*{PLAN_EXTENSION}")}
        for path in legacy_plans.glob("*.relayplan"):
            try:
                plan = load_plan(path)
                if plan["id"] in current_ids:
                    continue
                self.save(plan)
                current_ids.add(plan["id"])
            except (OSError, ValueError) as error:
                self.warnings.append(f"Could not import a legacy plan ({path.name}): {error}")
        preferences["legacy_relay_plan_migration_complete"] = True
        try:
            self.write_preferences(preferences)
        except OSError as error:
            self.warnings.append(f"Could not save the legacy plan migration marker: {error}")

    def change_projects_directory(self, directory):
        """Switches project storage after copying plans and sync history safely.

        Args:
            directory (str or pathlib.Path): New user-selected storage folder.

        Returns:
            pathlib.Path: Selected absolute storage folder.
        """
        target_directory = Path(directory).expanduser()
        if not target_directory.is_absolute():
            raise ValueError("Choose an absolute project folder.")
        target_directory = target_directory.resolve(strict=False)
        target_plans = target_directory / "plans"
        target_state = target_directory / "sync_state"
        target_actions = target_directory / "actions"
        target_logs = target_directory / "logs"
        target_plans.mkdir(parents=True, exist_ok=True)
        target_state.mkdir(parents=True, exist_ok=True)
        target_actions.mkdir(parents=True, exist_ok=True)
        target_logs.mkdir(parents=True, exist_ok=True)
        for source_path in self.plans_directory.glob(f"*{PLAN_EXTENSION}"):
            plan = load_plan(source_path)
            destination_path = target_plans / f"{plan['id']}{PLAN_EXTENSION}"
            if destination_path.exists():
                existing = load_plan(destination_path)
                if existing != plan:
                    raise ValueError(f"A different plan already exists in the chosen folder: {destination_path}")
            else:
                save_plan(plan, destination_path)
        for source_path in self.state_directory.glob("*.json"):
            destination_path = target_state / source_path.name
            if destination_path.exists():
                if _read_json(destination_path) != _read_json(source_path):
                    raise ValueError(f"Different sync history already exists: {destination_path.name}")
            else:
                shutil.copy2(source_path, destination_path)
        for source_path in self.actions_directory.glob(f"*{ACTION_EXTENSION}"):
            destination_path = target_actions / source_path.name
            if destination_path.exists():
                if _read_json(destination_path) != _read_json(source_path):
                    raise ValueError(f"A different automatic action already exists: {destination_path.name}")
            else:
                shutil.copy2(source_path, destination_path)
        preferences = self.read_preferences()
        preferences["projects_directory"] = str(target_directory)
        self.write_preferences(preferences)
        self.directory = target_directory
        self.plans_directory = target_plans
        self.state_directory = target_state
        self.actions_directory = target_actions
        self.logs_directory = target_logs
        return target_directory

    @staticmethod
    def _is_default_preferences_directory(path):
        """Checks whether project storage uses the built-in app-local setting path.

        Args:
            path (pathlib.Path): Preferences location.

        Returns:
            bool: Whether it equals the default location.
        """
        return path == default_data_directory().absolute()

    def list_plans(self):
        """Lists valid saved plans and records invalid-file warnings for the UI.

        Returns:
            list: Summaries containing each plan's id, name, and file path.
        """
        self.warnings = list(self._initial_warnings)
        summaries = []
        for path in sorted(self.plans_directory.glob(f"*{PLAN_EXTENSION}")):
            try:
                plan = load_plan(path)
                if path.stem != plan["id"]:
                    raise ValueError("Stored filename does not match its plan identifier.")
                summaries.append({"id": plan["id"], "name": plan["name"], "path": str(path)})
            except (OSError, ValueError) as error:
                self.warnings.append(f"Could not load {path.name}: {error}")
        return sorted(summaries, key=lambda summary: summary["name"].casefold())

    def save(self, plan):
        """Saves a plan using its validated stable identifier.

        Args:
            plan (dict): Plan to persist.

        Returns:
            pathlib.Path: Stored plan path.
        """
        normalized = validate_plan(plan)
        path = self.plans_directory / f"{normalized['id']}{PLAN_EXTENSION}"
        save_plan(normalized, path)
        return path

    def load(self, plan_id):
        """Loads a plan by its stable identifier.

        Args:
            plan_id (str): Plan UUID.

        Returns:
            dict: Validated plan.
        """
        identifier = _identifier(plan_id, "Plan identifier")
        return load_plan(self.plans_directory / f"{identifier}{PLAN_EXTENSION}")

    def remove(self, plan_id):
        """Removes one saved plan without touching its source or destination files.

        Args:
            plan_id (str): Plan UUID.

        Returns:
            None: Existing saved plan file is removed.
        """
        identifier = _identifier(plan_id, "Plan identifier")
        path = self.plans_directory / f"{identifier}{PLAN_EXTENSION}"
        if path.exists():
            path.unlink()

    def load_sync_state(self, plan_id):
        """Loads baseline data used to detect changes in two-way pairs.

        Args:
            plan_id (str): Plan UUID.

        Returns:
            dict: Valid sync state or an empty state for a new plan.
        """
        identifier = _identifier(plan_id, "Plan identifier")
        path = self.state_directory / f"{identifier}.json"
        empty = {"schema_version": 1, "plan_id": identifier, "pairs": {}}
        if not path.exists():
            return empty
        try:
            state = _read_json(path)
            if (not isinstance(state, dict) or state.get("plan_id") != identifier
                    or not isinstance(state.get("pairs"), dict)):
                raise ValueError("Sync history has an invalid structure.")
            return state
        except (OSError, ValueError) as error:
            self.warnings.append(f"Could not load sync history: {error}")
            return empty

    def save_sync_state(self, state):
        """Persists two-way comparison baselines for a plan.

        Args:
            state (dict): Sync state returned from an executed preview.

        Returns:
            pathlib.Path: Saved state file.
        """
        if not isinstance(state, dict):
            raise ValueError("Sync history must be a JSON object.")
        identifier = _identifier(state.get("plan_id"), "Plan identifier")
        normalized = copy.deepcopy(state)
        normalized["schema_version"] = 1
        normalized["plan_id"] = identifier
        normalized.setdefault("pairs", {})
        path = self.state_directory / f"{identifier}.json"
        _write_json(normalized, path)
        return path

    def read_preferences(self):
        """Loads application preferences, recording malformed-file warnings.

        Returns:
            dict: Preferences or an empty dictionary for absent or invalid data.
        """
        if not self.preferences_path.exists():
            return {}
        try:
            preferences = _read_json(self.preferences_path)
            if not isinstance(preferences, dict):
                raise ValueError("Preferences must be a JSON object.")
            return preferences
        except (OSError, ValueError) as error:
            self.warnings.append(f"Could not load preferences: {error}")
            return {}

    def write_preferences(self, preferences):
        """Atomically saves independent JSON-compatible application preferences.

        Args:
            preferences (dict): Application preferences.

        Returns:
            None: Preferences are saved.
        """
        if not isinstance(preferences, dict):
            raise ValueError("Preferences must be a dictionary.")
        _write_json(copy.deepcopy(preferences), self.preferences_path)
