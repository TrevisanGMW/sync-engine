"""Read-only backup previews and conservative, cancellable file synchronization.

Only the standard library is used. Move mode removes source files only after
their destination versions are verified. Filtered files and folders, symbolic
links and Windows reparse points are outside the synchronization scope,
including removal in mirror mode.
"""

import copy
import fnmatch
import hashlib
import logging
import os
import shutil
import stat
import tempfile
import threading

from sync_engine_model import normalize_exclusions, resolve_plan_paths


CHUNK_SIZE = 1024 * 1024
OPERATION_KINDS = {"copy", "update", "move", "delete", "mkdir", "rmdir", "conflict"}
LOGGER = logging.getLogger("sync_engine")


class SyncError(Exception):
    """Raised when a backup cannot be planned or performed safely."""


class SyncCancelled(SyncError):
    """Raised when cancellation is requested during preview or sync work."""


class OperationControl:
    """Coordinates cooperative cancellation and pausing at safe checkpoints."""

    def __init__(self):
        self._cancel_event = threading.Event()
        self._resume_event = threading.Event()
        self._resume_event.set()

    def is_set(self):
        """Returns whether cancellation was requested."""
        return self._cancel_event.is_set()

    def set(self):
        """Requests cancellation and releases a paused operation."""
        self._cancel_event.set()
        self._resume_event.set()

    @property
    def is_paused(self):
        """Whether the operation is currently held at a checkpoint."""
        return not self._resume_event.is_set() and not self.is_set()

    def pause(self):
        """Requests a pause at the next engine checkpoint."""
        if not self.is_set():
            self._resume_event.clear()

    def resume(self):
        """Allows a paused operation to continue."""
        self._resume_event.set()

    def wait_if_paused(self):
        """Waits without blocking the UI thread and exits promptly on cancel."""
        while not self._resume_event.wait(0.1):
            if self.is_set():
                return


def _check_cancel(cancel_event):
    """Pauses at a safe point and stops if the cancellation event is set.

    Args:
        cancel_event (threading.Event, OperationControl, or None): Cooperative control.
    """
    if cancel_event is None:
        return
    wait_if_paused = getattr(cancel_event, "wait_if_paused", None)
    if wait_if_paused is not None:
        wait_if_paused()
    if cancel_event.is_set():
        raise SyncCancelled("Synchronization cancelled.")


def _report(callback, **values):
    """Emits progress without requiring a user interface.

    Args:
        callback (callable or None): Function receiving a progress dictionary.
        **values (dict): Progress fields for the current operation.
    """
    if callback is not None:
        callback(values)


def _canonical(path):
    """Returns an absolute, normalized path for overlap checks.

    Args:
        path (str): Filesystem path.

    Returns:
        str: Canonical path with platform-appropriate case normalization.
    """
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _overlap(first_path, second_path):
    """Checks whether paths identify the same directory or nested directories.

    Args:
        first_path (str): First canonical absolute path.
        second_path (str): Second canonical absolute path.

    Returns:
        bool: Whether one path contains the other.
    """
    try:
        return os.path.commonpath([first_path, second_path]) in (first_path, second_path)
    except ValueError:
        return False


def _is_link(file_stat):
    """Identifies symbolic links and Windows filesystem reparse points.

    Args:
        file_stat (os.stat_result): Non-following filesystem metadata.

    Returns:
        bool: Whether the entry redirects filesystem access.
    """
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        getattr(file_stat, "st_file_attributes", 0) & reparse_attribute
    )


def _validate_components(path):
    """Rejects redirecting or non-directory ancestors of a directory path.

    Args:
        path (str): Absolute directory path, which may not exist yet.
    """
    current_path = path
    while True:
        try:
            current_stat = os.lstat(current_path)
        except FileNotFoundError:
            current_stat = None
        except OSError as error:
            raise SyncError(f"Cannot inspect directory '{current_path}': {error}") from error
        if current_stat is not None:
            if _is_link(current_stat):
                raise SyncError(f"Linked directories cannot be used as targets: {current_path}")
            if not stat.S_ISDIR(current_stat.st_mode):
                raise SyncError(f"A path component is not a directory: {current_path}")
        parent_path = os.path.dirname(current_path)
        if parent_path == current_path:
            break
        current_path = parent_path


def _prepare_pairs(plan):
    """Validates the plan and resolves safe absolute folder pairs.

    Args:
        plan (dict): Serializable backup plan.

    Returns:
        list[dict]: Normalized pairs with canonical paths.
    """
    if not isinstance(plan, dict) or not isinstance(plan.get("pairs"), list):
        raise SyncError("The plan must contain a list of folder pairs.")
    if not plan["pairs"]:
        raise SyncError("Add at least one folder pair before comparing.")
    if plan.get("compare", "metadata") not in ("metadata", "content"):
        raise SyncError("Comparison must be 'metadata' or 'content'.")
    tolerance = plan.get("timestamp_tolerance", 0.0)
    if not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 3600:
        raise SyncError("Timestamp tolerance must be between 0 and 3600 seconds.")
    pairs = []
    used_ids = set()
    for pair in plan["pairs"]:
        if not isinstance(pair, dict):
            raise SyncError("Every folder pair must be an object.")
        pair_id = pair.get("id")
        if not isinstance(pair_id, str) or not pair_id or pair_id in used_ids:
            raise SyncError("Every folder pair must have a unique, nonempty ID.")
        used_ids.add(pair_id)
        if pair.get("mode", "update") not in ("add", "update", "mirror", "move", "two_way"):
            raise SyncError(f"Unknown backup mode for pair '{pair_id}'.")
        normalized = copy.deepcopy(pair)
        try:
            normalized["exclusions"] = normalize_exclusions(pair)
        except ValueError as error:
            raise SyncError(str(error)) from error
        for path_key in ("source", "destination"):
            raw_path = pair.get(path_key, "")
            if not isinstance(raw_path, str) or not raw_path.strip():
                raise SyncError(f"Choose a {path_key} folder for '{pair.get('name', pair_id)}'.")
            raw_path = os.path.expanduser(raw_path)
            if not os.path.isabs(raw_path):
                raise SyncError(f"The {path_key} path must be absolute: {raw_path}")
            normalized[path_key] = os.path.abspath(raw_path)
            normalized[f"canonical_{path_key}"] = _canonical(raw_path)
        destination = normalized["destination"]
        if os.path.dirname(destination) == destination:
            raise SyncError("A filesystem root cannot be a backup destination.")
        _validate_components(normalized["source"])
        _validate_components(destination)
        if not os.path.isdir(normalized["source"]):
            raise SyncError(f"The source folder does not exist: {normalized['source']}")
        pairs.append(normalized)
    for pair in pairs:
        for other_pair in pairs:
            if _overlap(pair["canonical_destination"], other_pair["canonical_source"]):
                raise SyncError("Source and destination folders must not overlap, including across pairs.")
            if pair["id"] != other_pair["id"] and _overlap(
                pair["canonical_destination"], other_pair["canonical_destination"]
            ):
                raise SyncError("Destination folders must not overlap across pairs.")
    return pairs


def _signature(file_stat):
    """Builds a stable, serializable metadata signature.

    Args:
        file_stat (os.stat_result): Filesystem metadata.

    Returns:
        dict: File identity, size and modification information.
    """
    return {
        "size": file_stat.st_size,
        "mtime_ns": file_stat.st_mtime_ns,
        # Python 3.14 Windows lstat and fstat disagree about st_ctime.
        # Birth time remains a consistent file-identity attribute on Windows.
        "ctime_ns": getattr(file_stat, "st_birthtime_ns", file_stat.st_ctime_ns)
        if os.name == "nt" else file_stat.st_ctime_ns,
        "device": file_stat.st_dev,
        "inode": file_stat.st_ino,
        "mode": file_stat.st_mode,
    }


def _file_signature(path):
    """Reads a regular file's current signature without following a link.

    Args:
        path (str): Absolute filename.

    Returns:
        dict: File metadata signature.
    """
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise SyncError(f"Cannot inspect file '{path}': {error}") from error
    if _is_link(file_stat) or not stat.S_ISREG(file_stat.st_mode):
        raise SyncError(f"Expected a regular, unlinked file: {path}")
    return _signature(file_stat)


def _matching_exclusion(name, relative_path, is_directory, pair):
    """Returns a matching Exclude rule or an Include allowlist miss.

    Args:
        name (str): Entry basename.
        relative_path (str): Entry path relative to the pair root.
        is_directory (bool): Whether the entry is a directory.
        pair (dict): Normalized folder pair.

    Returns:
        dict or None: Exclude rule, include miss, or no filtering decision.
    """
    target_name = name
    target = "folders" if is_directory else "files"
    normalized_path = relative_path.replace("\\", "/")
    basename = normalized_path.rsplit("/", 1)[-1]
    applicable_rules = []
    for rule in pair.get("exclusions", []):
        if rule.get("applies_to") not in (target, "both"):
            continue
        applicable_rules.append(rule)
    include_rules = [rule for rule in applicable_rules if rule.get("effect", "exclude") == "include"]
    matching_includes = []
    for rule in applicable_rules:
        ignore_case = rule.get("ignore_case", True)
        candidate = target_name.casefold() if ignore_case else target_name
        path_candidate = normalized_path.casefold() if ignore_case else normalized_path
        basename_candidate = basename.casefold() if ignore_case else basename
        pattern = rule.get("pattern", "")
        if ignore_case:
            pattern = pattern.casefold()
        match = rule.get("match", "exact")
        matches = (
            (match == "starts_with" and candidate.startswith(pattern))
            or (match == "ends_with" and candidate.endswith(pattern))
            or (match == "contains" and pattern in candidate)
            or (match == "exact" and candidate == pattern)
            or (match == "glob" and not is_directory and (
            fnmatch.fnmatchcase(path_candidate, pattern.replace("\\", "/"))
            or fnmatch.fnmatchcase(basename_candidate, pattern)
            ))
        )
        if matches:
            if rule.get("effect", "exclude") == "exclude":
                return rule
            matching_includes.append(rule)
    if include_rules and not matching_includes:
        return {
            "effect": "include", "matched": False, "applies_to": target,
            "pattern": "any configured Include rule",
        }
    return None


def _ignored_folder(folder_name, pair):
    """Checks whether any folder-scoped exclusion matches a name.

    Args:
        folder_name (str): Folder basename.
        pair (dict): Folder pair with normalized exclusion rules.

    Returns:
        bool: Whether the folder is excluded.
    """
    return _matching_exclusion(folder_name, folder_name, True, pair) is not None


def _ignored_file(relative_path, pair):
    """Checks whether any file-scoped exclusion matches a relative path.

    Args:
        relative_path (str): Path relative to a folder pair.
        pair (dict): Folder pair with normalized exclusion rules.

    Returns:
        bool: Whether the file is excluded.
    """
    basename = relative_path.replace("\\", "/").rsplit("/", 1)[-1]
    return _matching_exclusion(basename, relative_path, False, pair) is not None


def _exclusion_reason(rule, entry_type):
    """Creates concise, user-facing context for a skipped preview row.

    Args:
        rule (dict): Matching exclusion rule.
        entry_type (str): File or folder.

    Returns:
        str: Human-readable explanation.
    """
    match_labels = {
        "starts_with": "starts with",
        "ends_with": "ends with",
        "contains": "contains",
        "exact": "exactly matches",
        "glob": "matches legacy pattern",
    }
    if rule.get("effect", "exclude") == "include":
        if rule.get("filter_scope") == "folder_parent":
            return "Not inside a folder selected by an Include rule."
        scope = "folder" if entry_type == "folder" else "file"
        return f"Not matched by any Include rule for {scope}s."
    pattern = rule.get("pattern", "")
    reason = f"{entry_type.title()} name {match_labels.get(rule.get('match'), 'matches')} {pattern!r}"
    if rule.get("ignore_case", True):
        reason += " (ignoring case)"
    if entry_type == "folder":
        reason += "; contents are skipped without scanning"
    return reason


def _protected(relative_path, protected_paths):
    """Checks whether an entry belongs to a protected subtree.

    Args:
        relative_path (str): Relative path using forward slashes.
        protected_paths (set[str]): Protected entries and subtree roots.

    Returns:
        bool: Whether the entry must be left untouched.
    """
    candidate = _relative_key(relative_path)
    return any(
        candidate == _relative_key(protected_path)
        or candidate.startswith(f"{_relative_key(protected_path)}/")
        for protected_path in protected_paths
    )


def _protected_exact(relative_path, protected_paths):
    """Checks whether an exact entry is outside the included sync scope."""
    return _relative_key(relative_path) in protected_paths


def _relative_key(relative_path):
    """Normalizes relative paths consistently with the host filesystem.

    Args:
        relative_path (str): Relative filename or directory name.

    Returns:
        str: Case-normalized path using forward slashes.
    """
    return os.path.normcase(relative_path).replace("\\", "/")


def _iter_directory_entries(directory_path):
    """Yields entries as the operating system returns them.

    Keeping directory enumeration streaming lets preview progress appear before
    a slow or large directory has been read in full.

    Args:
        directory_path (str): Directory to enumerate.

    Yields:
        os.DirEntry: One filesystem entry.

    Raises:
        SyncError: The directory cannot be read.
    """
    try:
        with os.scandir(directory_path) as iterator:
            for entry in iterator:
                yield entry
    except OSError as error:
        raise SyncError(f"Cannot read folder '{directory_path}': {error}") from error


def _scan(root, pair, cancel_event, progress_callback, protected_paths=None, scan_side=None,
          list_skipped_folder_contents=False):
    """Scans a directory without following links or hiding enumeration failures.

    Args:
        root (str): Absolute directory path.
        pair (dict): Pair options used to filter entries.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Receives scan progress.
        protected_paths (set[str] or None): Additional excluded subtrees.
        list_skipped_folder_contents (bool): Whether to enumerate skipped folder descendants.

    Returns:
        dict: Files, directories, protected paths, warnings and root identity.
    """
    snapshot = {"files": {}, "directories": {}, "paths": {},
                "protected": [], "protected_exact": [], "skipped": [],
                "excluded": 0, "warnings": []}
    protected_paths = set(protected_paths or [])
    _check_cancel(cancel_event)
    _validate_components(root)
    try:
        root_stat = os.lstat(root)
    except FileNotFoundError:
        snapshot["root_signature"] = None
        return snapshot
    except OSError as error:
        raise SyncError(f"Cannot inspect folder '{root}': {error}") from error
    snapshot["root_signature"] = _signature(root_stat)
    pending = [(root, "", False, None)]
    directory_candidates = {}
    folder_include_rules = [
        item for item in pair.get("exclusions", [])
        if item.get("effect", "exclude") == "include"
        and item.get("applies_to") in ("folders", "both")
    ]
    file_include_rules = [
        item for item in pair.get("exclusions", [])
        if item.get("effect", "exclude") == "include"
        and item.get("applies_to") in ("files", "both")
    ]
    scanned_files = 0
    scanned_entries = 0
    seen_paths = set()
    while pending:
        _check_cancel(cancel_event)
        directory_path, relative_directory, selected_folder_parent, skipped_folder_context = pending.pop()
        for entry in _iter_directory_entries(directory_path):
            _check_cancel(cancel_event)
            scanned_entries += 1
            actual_relative_path = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
            relative_path = _relative_key(actual_relative_path)
            if relative_path in seen_paths:
                raise SyncError(
                    f"Folder contains names that resolve to the same path; compare cannot continue: {entry.path}"
                )
            seen_paths.add(relative_path)
            snapshot["paths"][relative_path] = actual_relative_path
            try:
                # Windows DirEntry.stat may report zero for inode and device;
                # lstat supplies identities consistent with subsequent checks.
                entry_stat = os.lstat(entry.path)
            except OSError as error:
                raise SyncError(f"Cannot inspect '{entry.path}': {error}") from error
            is_directory = stat.S_ISDIR(entry_stat.st_mode)
            is_link = _is_link(entry_stat)
            entry_type = "folder" if is_directory else "file"
            progress_values = {
                "phase": "scan", "pair_id": pair.get("id"), "scan_side": scan_side,
                "relative_path": actual_relative_path, "entry_type": entry_type,
                "scanned_entries": scanned_entries, "scanned_files": scanned_files,
                "current_path": entry.path,
            }
            if skipped_folder_context is not None:
                snapshot["excluded"] += 1
                reason = (
                    f"Inside skipped folder '{skipped_folder_context['relative_path']}'."
                )
                snapshot["skipped"].append({
                    "relative_path": actual_relative_path,
                    "entry_type": entry_type,
                    "rule": copy.deepcopy(skipped_folder_context.get("rule")),
                    "reason": reason,
                })
                if is_link:
                    snapshot["warnings"].append(
                        f"Skipped linked or reparse-point entry: {entry.path}"
                    )
                elif is_directory:
                    pending.append((
                        entry.path, actual_relative_path, False, skipped_folder_context,
                    ))
                _report(
                    progress_callback, **progress_values, kind="skipped",
                    reason=reason, message=f"Listing skipped folder contents: {entry.name}",
                )
                continue

            if _protected(relative_path, protected_paths) or is_link:
                snapshot["protected"].append(relative_path)
                snapshot["excluded"] += 1
                if is_link:
                    snapshot["warnings"].append(f"Skipped linked or reparse-point entry: {entry.path}")
                protected_folder = next((
                    protected_path for protected_path in protected_paths
                    if relative_path == _relative_key(protected_path)
                    and is_directory
                ), None)
                if (protected_folder is not None and list_skipped_folder_contents
                        and not is_link):
                    context = {
                        "relative_path": actual_relative_path,
                        "rule": None,
                    }
                    reason = "This folder's source counterpart is skipped by a filter."
                    snapshot["skipped"].append({
                        "relative_path": actual_relative_path,
                        "entry_type": "folder",
                        "rule": None,
                        "reason": reason,
                    })
                    pending.append((entry.path, actual_relative_path, False, context))
                    _report(
                        progress_callback, **progress_values, kind="skipped",
                        reason=reason, message=f"Listing skipped folder contents: {entry.name}",
                    )
                    continue
                _report(
                    progress_callback, **progress_values,
                    kind="scanning", message="Scanning folders…",
                )
                continue
            rule = _matching_exclusion(entry.name, actual_relative_path, is_directory, pair)
            if rule is not None and rule.get("effect", "exclude") == "exclude":
                snapshot["protected"].append(relative_path)
                snapshot["excluded"] += 1
                snapshot["skipped"].append({
                    "relative_path": actual_relative_path,
                    "entry_type": entry_type,
                    "rule": copy.deepcopy(rule),
                    "reason": (
                        _exclusion_reason(rule, entry_type).replace(
                            "; contents are skipped without scanning",
                            "; contents are listed as skipped",
                        )
                        if is_directory and list_skipped_folder_contents
                        else _exclusion_reason(rule, entry_type)
                    ),
                })
                if is_directory and list_skipped_folder_contents:
                    pending.append((
                        entry.path, actual_relative_path, False,
                        {"relative_path": actual_relative_path, "rule": copy.deepcopy(rule)},
                    ))
                _report(
                    progress_callback, **progress_values, kind="skipped",
                    reason=snapshot["skipped"][-1]["reason"],
                    message=(
                        f"Listing skipped folder contents: {entry.name}"
                        if is_directory and list_skipped_folder_contents
                        else f"Skipping excluded {entry_type}: {entry.name}"
                    ),
                )
                continue
            scan_kind = "scanning"
            scan_reason = f"Scanning {scan_side or 'folder'} contents…"
            if is_directory:
                signature = _signature(entry_stat)
                directory_candidates[relative_path] = signature
                included_folder = (
                    selected_folder_parent or not folder_include_rules or rule is None
                )
                if included_folder and (folder_include_rules or not file_include_rules):
                    snapshot["directories"][relative_path] = signature
                elif not included_folder:
                    snapshot["protected_exact"].append(relative_path)
                    snapshot["excluded"] += 1
                    snapshot["skipped"].append({
                        "relative_path": actual_relative_path,
                        "entry_type": "folder",
                        "rule": copy.deepcopy(rule),
                    })
                    scan_kind = "skipped"
                    scan_reason = _exclusion_reason(rule, "folder")
                pending.append((
                    entry.path, actual_relative_path,
                    bool(folder_include_rules and included_folder),
                    None,
                ))
            elif stat.S_ISREG(entry_stat.st_mode):
                included_by_folder = selected_folder_parent
                folder_gate_miss = bool(folder_include_rules and not included_by_folder)
                file_gate_miss = bool(
                    rule is not None and rule.get("effect", "exclude") == "include"
                )
                file_gate_pass = bool(file_include_rules and rule is None)
                include_allowed = (
                    included_by_folder or file_gate_pass
                    or (not folder_gate_miss and not file_gate_miss)
                )
                if include_allowed:
                    snapshot["files"][relative_path] = _signature(entry_stat)
                    scanned_files += 1
                else:
                    snapshot["protected_exact"].append(relative_path)
                    snapshot["excluded"] += 1
                    if folder_gate_miss and not file_include_rules:
                        rule = {
                            "effect": "include", "matched": False,
                            "applies_to": "folders", "filter_scope": "folder_parent",
                        }
                    snapshot["skipped"].append({
                        "relative_path": actual_relative_path,
                        "entry_type": "file",
                        "rule": copy.deepcopy(rule),
                    })
                    scan_kind = "skipped"
                    scan_reason = _exclusion_reason(rule, "file")
            else:
                snapshot["protected"].append(relative_path)
                snapshot["excluded"] += 1
                snapshot["warnings"].append(f"Skipped unsupported filesystem entry: {entry.path}")
                scan_kind = "skipped"
                scan_reason = "Unsupported filesystem entry."
            _report(
                progress_callback, **progress_values, kind=scan_kind,
                reason=scan_reason, message="Scanning folders…",
            )
    selected_paths = set(snapshot["files"]) | set(snapshot["directories"])
    promoted_directories = set()
    for selected_path in selected_paths:
        parent = selected_path.rpartition("/")[0]
        while parent:
            if parent in directory_candidates:
                promoted_directories.add(parent)
            parent = parent.rpartition("/")[0]
    for relative_path in promoted_directories:
        snapshot["directories"].setdefault(relative_path, directory_candidates[relative_path])
    if promoted_directories:
        snapshot["protected_exact"] = [
            path for path in snapshot["protected_exact"] if path not in promoted_directories
        ]
        snapshot["skipped"] = [
            row for row in snapshot["skipped"]
            if not (row.get("entry_type") == "folder"
                    and _relative_key(row.get("relative_path", "")) in promoted_directories)
        ]
    return snapshot


def _hash_file(path, expected_signature, cancel_event, progress_callback):
    """Hashes a file and fails if its metadata changes while it is read.

    Args:
        path (str): Absolute filename.
        expected_signature (dict): Metadata captured before comparison.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Receives comparison progress.

    Returns:
        str: SHA-256 hexadecimal digest.
    """
    digest = hashlib.sha256()
    if _file_signature(path) != expected_signature:
        raise SyncError(f"File changed while preparing the preview: {path}")
    try:
        with open(path, "rb") as source_stream:
            while True:
                _check_cancel(cancel_event)
                chunk = source_stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                _report(progress_callback, phase="compare", current_path=path, message="Comparing content…")
    except OSError as error:
        raise SyncError(f"Cannot compare '{path}': {error}") from error
    if _file_signature(path) != expected_signature:
        raise SyncError(f"File changed while preparing the preview: {path}")
    return digest.hexdigest()


def _join(root, relative_path):
    """Joins a relative operation path while ensuring it stays inside its root.

    Args:
        root (str): Absolute root directory.
        relative_path (str): Relative path using forward slashes.

    Returns:
        str: Absolute destination inside the supplied root.
    """
    if not isinstance(relative_path, str) or os.path.isabs(relative_path):
        raise SyncError("An operation contains an invalid relative path.")
    result = os.path.abspath(os.path.join(root, relative_path.replace("/", os.sep)))
    try:
        if os.path.normcase(os.path.commonpath([root, result])) != os.path.normcase(root):
            raise SyncError("An operation escapes its configured folder.")
    except ValueError as error:
        raise SyncError("An operation escapes its configured folder.") from error
    return result


def _operation(kind, pair, relative_path, source_signature=None, destination_signature=None,
               source_relative_path=None, destination_relative_path=None,
               source_root="source", target_root="destination"):
    """Creates a serializable operation with its original file signatures.

    Args:
        kind (str): Copy, update, move, delete, mkdir or rmdir.
        pair (dict): Normalized folder pair.
        relative_path (str): Path relative to the pair roots.
        source_signature (dict or None): Original source file metadata.
        destination_signature (dict or None): Original destination metadata.
        source_relative_path (str or None): Actual source spelling for filesystem access.
        destination_relative_path (str or None): Actual destination spelling for filesystem access.

    Returns:
        dict: Planned operation.
    """
    source_relative_path = relative_path if source_relative_path is None else source_relative_path
    destination_relative_path = relative_path if destination_relative_path is None else destination_relative_path
    return {
        "kind": kind, "pair_id": pair["id"], "relative_path": destination_relative_path,
        "source_relative_path": source_relative_path,
        "source_root": source_root, "target_root": target_root,
        "source": _join(pair[source_root], source_relative_path)
        if kind in ("copy", "update", "move") else "",
        "destination": _join(pair[target_root], destination_relative_path),
        "size": source_signature["size"] if source_signature else 0,
        "source_signature": copy.deepcopy(source_signature),
        "destination_signature": copy.deepcopy(destination_signature),
    }


def _same_file(source_path, destination_path, source_signature, destination_signature, plan,
               cancel_event, progress_callback):
    """Compares files using lightweight metadata or complete file contents.

    Args:
        source_path (str): Source filename.
        destination_path (str): Destination filename.
        source_signature (dict): Source metadata.
        destination_signature (dict): Destination metadata.
        plan (dict): Comparison settings.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Comparison progress receiver.

    Returns:
        bool: Whether the files satisfy the selected equality rule.
    """
    if source_signature["size"] != destination_signature["size"]:
        return False
    if plan.get("compare", "metadata") == "content":
        return _hash_file(source_path, source_signature, cancel_event, progress_callback) == _hash_file(
            destination_path, destination_signature, cancel_event, progress_callback
        )
    tolerance_ns = int(plan.get("timestamp_tolerance", 0.0) * 1_000_000_000)
    return abs(source_signature["mtime_ns"] - destination_signature["mtime_ns"]) <= tolerance_ns


def _destination_spelling(source_relative_path, destination_paths):
    """Preserves existing target ancestor spelling while naming new entries like the source.

    Args:
        source_relative_path (str): Actual relative source spelling.
        destination_paths (dict): Existing or planned destination spelling by normalized key.

    Returns:
        str: Relative target path with the appropriate existing ancestor spelling.
    """
    components = source_relative_path.split("/")
    for component_count in range(len(components), 0, -1):
        prefix = "/".join(components[:component_count])
        existing_prefix = destination_paths.get(_relative_key(prefix))
        if existing_prefix is not None:
            return "/".join([existing_prefix] + components[component_count:])
    return source_relative_path


def _pair_operations(pair, source_snapshot, destination_snapshot, plan, cancel_event, progress_callback,
                     sync_state=None, conflict_resolutions=None):
    """Produces operations for one pair without modifying either folder.

    Args:
        pair (dict): Normalized folder pair.
        source_snapshot (dict): Original source scan.
        destination_snapshot (dict): Original destination scan.
        plan (dict): Comparison and safety options.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Progress receiver.

    Returns:
        tuple[list[dict], int]: Planned operations and unchanged file count.
    """
    if pair.get("mode") == "two_way":
        return _two_way_operations(
            pair, source_snapshot, destination_snapshot, plan, cancel_event, progress_callback,
            sync_state or {}, conflict_resolutions or {},
        )
    source_files = source_snapshot["files"]
    source_directories = source_snapshot["directories"]
    destination_files = destination_snapshot["files"]
    destination_directories = destination_snapshot["directories"]
    protected_paths = set(source_snapshot["protected"] + destination_snapshot["protected"])
    protected_exact_paths = set(
        source_snapshot.get("protected_exact", [])
        + destination_snapshot.get("protected_exact", [])
    )
    source_paths = source_snapshot["paths"]
    destination_paths = destination_snapshot["paths"]
    planned_destination_paths = dict(destination_paths)
    collisions = set(source_files).intersection(destination_directories)
    collisions.update(set(source_directories).intersection(destination_files))
    if collisions:
        collision = sorted(collisions)[0]
        collision_path = _join(pair["destination"], destination_paths[collision])
        raise SyncError(f"File/folder type collision; resolve it before syncing: {collision_path}")
    operations = []
    if destination_snapshot["root_signature"] is None:
        operations.append(_operation("mkdir", pair, ""))
    for relative_path in sorted(source_directories, key=lambda path: (path.count("/"), path.casefold())):
        if relative_path not in destination_directories and not _protected(relative_path, protected_paths):
            destination_relative_path = _destination_spelling(source_paths[relative_path], planned_destination_paths)
            operations.append(_operation("mkdir", pair, destination_relative_path))
            planned_destination_paths[relative_path] = destination_relative_path
    unchanged = 0
    for relative_path, source_signature in sorted(source_files.items()):
        _check_cancel(cancel_event)
        if _protected(relative_path, protected_paths):
            continue
        destination_signature = destination_files.get(relative_path)
        if destination_signature is None:
            destination_relative_path = _destination_spelling(source_paths[relative_path], planned_destination_paths)
            if pair.get("mode") == "move":
                operation = _operation(
                    "move", pair, relative_path, source_signature, None,
                    source_paths[relative_path], destination_relative_path,
                )
                operation["copy_action"] = "copy"
                operations.append(operation)
            else:
                operations.append(_operation(
                    "copy", pair, relative_path, source_signature, None,
                    source_paths[relative_path], destination_relative_path
                ))
        elif pair.get("mode", "update") == "add":
            unchanged += 1
        else:
            same_file = _same_file(
                _join(pair["source"], source_paths[relative_path]),
                _join(pair["destination"], destination_paths[relative_path]),
                source_signature, destination_signature, plan, cancel_event, progress_callback
            )
            if pair.get("mode") == "move":
                operation = _operation(
                    "move", pair, relative_path, source_signature, destination_signature,
                    source_paths[relative_path], destination_paths[relative_path],
                )
                operation["copy_action"] = None if same_file else "update"
                operations.append(operation)
            elif same_file:
                unchanged += 1
            else:
                operations.append(_operation(
                    "update", pair, relative_path, source_signature, destination_signature,
                    source_paths[relative_path], destination_paths[relative_path]
                ))
    if pair.get("mode", "update") == "mirror":
        for relative_path in sorted(set(destination_files).difference(source_files)):
            if (not _protected(relative_path, protected_paths)
                    and not _protected_exact(relative_path, protected_exact_paths)):
                operations.append(_operation(
                    "delete", pair, destination_paths[relative_path], None, destination_files[relative_path]
                ))
        extra_directories = set(destination_directories).difference(source_directories)
        for relative_path in sorted(extra_directories, key=lambda path: (-path.count("/"), path.casefold())):
            all_protected_paths = protected_paths | protected_exact_paths
            contains_protected = any(
                _protected(protected_path, {relative_path}) for protected_path in all_protected_paths
            )
            if (not _protected(relative_path, protected_paths)
                    and not _protected_exact(relative_path, protected_exact_paths)
                    and not contains_protected):
                operations.append(_operation("rmdir", pair, destination_paths[relative_path], None,
                                             destination_directories[relative_path]))
        has_removals = any(operation["kind"] in ("delete", "rmdir") for operation in operations)
        if has_removals and not source_files and not plan.get("allow_empty_mirror", False):
            raise SyncError("Mirror removal is blocked because the included source contains no files. "
                            "Check the source and file filters, or explicitly allow an empty mirror.")
    return operations, unchanged


def _unchanged_preview_rows(pair, source_snapshot, destination_snapshot, operations):
    """Describes scanned files and folders that need no filesystem operation.

    Args:
        pair (dict): Normalized folder pair.
        source_snapshot (dict): Source scan results.
        destination_snapshot (dict): Destination scan results.
        operations (list[dict]): Planned operations for this pair.

    Returns:
        list[dict]: Display-only preview rows for untouched entries.
    """
    action_keys = {
        _relative_key(operation.get("relative_path", ""))
        for operation in operations
        if operation.get("kind") in ("copy", "update", "move", "delete", "conflict", "mkdir", "rmdir")
    }
    rows = []
    pair_mode = pair.get("mode", "update")
    for entry_type, collection in (("file", "files"), ("folder", "directories")):
        source_entries = source_snapshot[collection]
        destination_entries = destination_snapshot[collection]
        for relative_key in sorted(set(source_entries) | set(destination_entries)):
            if relative_key in action_keys:
                continue
            source_signature = source_entries.get(relative_key)
            destination_signature = destination_entries.get(relative_key)
            if source_signature is None and destination_signature is None:
                continue
            relative_path = (source_snapshot["paths"].get(relative_key)
                             or destination_snapshot["paths"].get(relative_key, relative_key))
            if pair_mode == "add" and entry_type == "file" and destination_signature is not None:
                reason = "Add-only mode preserves the existing destination file."
                kind = "preserved"
            elif source_signature is not None and destination_signature is not None:
                reason = "The item already exists on both sides and needs no change."
                kind = "unchanged"
            elif destination_signature is not None:
                reason = "Destination-only items are preserved in this sync mode."
                kind = "preserved"
            else:
                reason = "The item is already present and needs no change."
                kind = "unchanged"
            rows.append({
                "kind": kind,
                "pair_id": pair["id"],
                "relative_path": relative_path,
                "entry_type": entry_type,
                "size": source_signature.get("size", 0) if source_signature else 0,
                "reason": reason,
            })
    return rows


def _two_way_operations(pair, source_snapshot, destination_snapshot, plan, cancel_event,
                        progress_callback, sync_state, conflict_resolutions):
    """Builds a cautious bidirectional merge against the last successful baseline."""
    source_files = source_snapshot["files"]
    destination_files = destination_snapshot["files"]
    collisions = set(source_files).intersection(destination_snapshot["directories"])
    collisions.update(set(source_snapshot["directories"]).intersection(destination_files))
    if collisions:
        collision = sorted(collisions)[0]
        raise SyncError(f"File/folder type collision; resolve it before syncing: {collision}")
    source_paths = source_snapshot["paths"]
    destination_paths = destination_snapshot["paths"]
    protected = set(source_snapshot["protected"] + destination_snapshot["protected"])
    protected_exact = set(
        source_snapshot.get("protected_exact", [])
        + destination_snapshot.get("protected_exact", [])
    )
    pair_state = sync_state.get("pairs", {}).get(pair["id"], {})
    roots_match = (
        source_snapshot["root_signature"] is not None
        and destination_snapshot["root_signature"] is not None
        and
        pair_state.get("source_root") == pair["canonical_source"]
        and pair_state.get("destination_root") == pair["canonical_destination"]
    )
    previous = pair_state.get("files", {}) if roots_match else {}
    operations = []
    conflicts = 0
    unchanged = 0
    next_files = {}
    keys = sorted(set(source_files) | set(destination_files))

    def digest(root, relative_path, signature):
        if signature is None or plan.get("compare", "metadata") != "content":
            return None
        return _hash_file(_join(root, relative_path), signature, cancel_event, progress_callback)

    for key in keys:
        _check_cancel(cancel_event)
        source_signature = source_files.get(key)
        destination_signature = destination_files.get(key)
        source_relative = source_paths.get(key, destination_paths.get(key, key))
        destination_relative = destination_paths.get(key, source_relative)
        if (_protected(source_relative, protected) or _protected(destination_relative, protected)
                or _protected_exact(source_relative, protected_exact)
                or _protected_exact(destination_relative, protected_exact)):
            continue
        old = previous.get(key)
        source_hash = digest(pair["source"], source_paths[key], source_signature) if source_signature else None
        destination_hash = digest(
            pair["destination"], destination_paths[key], destination_signature
        ) if destination_signature else None
        if old is None:
            source_changed = source_signature is not None
            destination_changed = destination_signature is not None
        elif plan.get("compare", "metadata") == "content":
            source_changed = source_hash != old.get("source_hash")
            destination_changed = destination_hash != old.get("destination_hash")
        else:
            source_changed = source_signature != old.get("source_signature")
            destination_changed = destination_signature != old.get("destination_signature")

        choice_record = conflict_resolutions.get(pair["id"], {}).get(key)
        choice = choice_record
        if isinstance(choice_record, dict):
            choice = choice_record.get("direction")
            if (choice_record.get("source_signature") != source_signature
                    or choice_record.get("destination_signature") != destination_signature):
                choice = None
        direction = None
        if source_changed and not destination_changed:
            direction = "source"
        elif destination_changed and not source_changed:
            direction = "destination"
        elif source_changed and destination_changed:
            equal = False
            source_digest = None
            destination_digest = None
            if source_signature is not None:
                source_digest = source_hash or _hash_file(
                    _join(pair["source"], source_paths[key]), source_signature,
                    cancel_event, progress_callback,
                )
            if destination_signature is not None:
                destination_digest = destination_hash or _hash_file(
                    _join(pair["destination"], destination_paths[key]), destination_signature,
                    cancel_event, progress_callback,
                )
            if source_signature is not None and destination_signature is not None:
                equal = source_digest == destination_digest
            if equal or (source_signature is None and destination_signature is None):
                unchanged += 1
            elif isinstance(choice_record, dict) and (
                choice_record.get("source_hash") != source_digest
                or choice_record.get("destination_hash") != destination_digest
            ):
                choice = None
            if not equal and choice in ("source", "destination"):
                direction = choice
            elif not equal:
                conflicts += 1
                conflict_operation = _operation(
                    "conflict", pair, source_relative, source_signature, destination_signature,
                    source_relative, destination_relative,
                )
                conflict_operation["conflict_source_hash"] = source_digest if source_signature else None
                conflict_operation["conflict_destination_hash"] = (
                    destination_digest if destination_signature else None
                )
                operations.append(conflict_operation)
        else:
            unchanged += 1

        if direction:
            from_root = direction
            to_root = "destination" if direction == "source" else "source"
            from_signature = source_signature if from_root == "source" else destination_signature
            to_signature = destination_signature if to_root == "destination" else source_signature
            from_paths = source_paths if from_root == "source" else destination_paths
            to_paths = destination_paths if to_root == "destination" else source_paths
            relative_from = from_paths.get(key, source_relative)
            relative_to = to_paths.get(key, relative_from)
            if from_signature is None:
                if to_signature is not None:
                    operations.append(_operation(
                        "delete", pair, relative_to, None, to_signature,
                        relative_to, relative_to, source_root=from_root, target_root=to_root,
                    ))
            elif to_signature is None or from_signature != to_signature:
                kind = "copy" if to_signature is None else "update"
                operations.append(_operation(
                    kind, pair, relative_to, from_signature, to_signature,
                    relative_from, relative_to, source_root=from_root, target_root=to_root,
                ))

        if source_signature is not None or destination_signature is not None:
            next_files[key] = {
                "source_signature": copy.deepcopy(source_signature),
                "destination_signature": copy.deepcopy(destination_signature),
                "source_hash": source_hash,
                "destination_hash": destination_hash,
                "source_path": source_paths.get(key),
                "destination_path": destination_paths.get(key),
            }

    # Add missing folders in the receiving root before copies. Empty folders are
    # preserved in both directions, while deletions remove files only.
    for target_root, target_snapshot, other_snapshot in (
        ("source", source_snapshot, destination_snapshot),
        ("destination", destination_snapshot, source_snapshot),
    ):
        if target_snapshot["root_signature"] is None and other_snapshot["root_signature"] is not None:
            operations.append(_operation("mkdir", pair, "", target_root=target_root))
        for directory_key, directory_path in sorted(
            other_snapshot["directories"].items(), key=lambda item: (item[0].count("/"), item[0])
        ):
            if directory_key not in target_snapshot["directories"] and not _protected(directory_path, protected):
                operations.append(_operation(
                    "mkdir", pair, directory_path, source_relative_path=directory_path,
                    destination_relative_path=directory_path,
                    source_root="destination" if target_root == "source" else "source",
                    target_root=target_root,
                ))
    return operations, unchanged, conflicts, next_files


def build_preview(plan, cancel_event=None, progress_callback=None, sync_state=None,
                  conflict_resolutions=None, plan_file_path=None):
    """Builds a read-only backup preview and captures execution safety snapshots.

    Args:
        plan (dict): Backup plan with one or more source/destination pairs.
        cancel_event (threading.Event or None): Cooperative cancellation flag.
        progress_callback (callable or None): Function accepting progress dictionaries.
        plan_file_path (str or pathlib.Path or None): Current sync plan or action
            file, used when expanding sync-file variables and relative paths.

    Returns:
        dict: Operations, summary, warnings, original plan and safety snapshots.

    Raises:
        SyncError: Invalid paths, scan errors, collisions or unsafe mirror removal.
        SyncCancelled: Cancellation requested while scanning or comparing.
    """
    try:
        plan = resolve_plan_paths(plan, file_path=plan_file_path)
    except ValueError as error:
        raise SyncError(str(error)) from error
    pairs = _prepare_pairs(plan)
    summary = {key: 0 for key in ("copy", "update", "move", "delete", "delete_source", "delete_destination",
                                    "mkdir", "rmdir", "conflict", "unchanged", "excluded", "skipped", "bytes")}
    preview = {"operations": [], "unchanged": [], "skipped": [], "summary": summary, "warnings": [],
               "plan": plan, "snapshots": {}}
    skipped_lookup = {}
    state = copy.deepcopy(sync_state or {"schema_version": 1, "plan_id": plan.get("id"), "pairs": {}})
    state.setdefault("pairs", {})
    for pair in pairs:
        _check_cancel(cancel_event)
        source_snapshot = _scan(
            pair["source"], pair, cancel_event, progress_callback, scan_side="source",
            list_skipped_folder_contents=plan.get("list_skipped_folder_contents", False),
        )
        if source_snapshot["root_signature"] is None:
            raise SyncError(f"The source folder disappeared during scanning: {pair['source']}")
        destination_snapshot = _scan(
            pair["destination"], pair, cancel_event, progress_callback,
            set(source_snapshot["protected"]), scan_side="backup",
            list_skipped_folder_contents=plan.get("list_skipped_folder_contents", False),
        )
        pair_result = _pair_operations(
            pair, source_snapshot, destination_snapshot, plan, cancel_event, progress_callback,
            sync_state=state, conflict_resolutions=conflict_resolutions,
        )
        operations, unchanged = pair_result[:2]
        if pair.get("mode") == "two_way":
            conflicts, next_files = pair_result[2:]
            summary["conflict"] += conflicts
            state["pairs"][pair["id"]] = {
                "source_root": pair["canonical_source"],
                "destination_root": pair["canonical_destination"],
                "files": next_files,
            }
        preview["operations"].extend(operations)
        preview["unchanged"].extend(_unchanged_preview_rows(
            pair, source_snapshot, destination_snapshot, operations
        ))
        preview["snapshots"][pair["id"]] = {"source": source_snapshot, "destination": destination_snapshot}
        summary["unchanged"] += unchanged
        summary["excluded"] += source_snapshot["excluded"] + destination_snapshot["excluded"]
        preview["warnings"].extend(source_snapshot["warnings"] + destination_snapshot["warnings"])
        for side, snapshot in (("source", source_snapshot), ("backup", destination_snapshot)):
            for skipped in snapshot["skipped"]:
                skipped_key = (pair["id"], _relative_key(skipped["relative_path"]))
                skipped_row = skipped_lookup.get(skipped_key)
                if skipped_row is None:
                    skipped_row = {
                        "kind": "skipped", "pair_id": pair["id"],
                        "relative_path": skipped["relative_path"],
                        "entry_type": skipped["entry_type"],
                        "reason": skipped.get("reason") or _exclusion_reason(
                            skipped.get("rule"), skipped["entry_type"]
                        ),
                        "side": side,
                    }
                    preview["skipped"].append(skipped_row)
                    skipped_lookup[skipped_key] = skipped_row
                elif skipped_row["side"] != side:
                    skipped_row["side"] = "source and backup"
        for operation in operations:
            if operation["kind"] in summary:
                summary[operation["kind"]] += 1
            if operation["kind"] == "delete":
                summary[f"delete_{operation.get('target_root', 'destination')}"] += 1
            if operation["kind"] == "move":
                summary["delete_source"] += 1
            if operation["kind"] in ("copy", "update"):
                summary["bytes"] += operation["size"]
            elif operation["kind"] == "move" and operation.get("copy_action"):
                summary["bytes"] += operation["size"]
    summary["skipped"] = len(preview["skipped"])
    preview["skipped"].sort(key=lambda row: (row["pair_id"], row["relative_path"].casefold()))
    preview["unchanged"].sort(key=lambda row: (row["pair_id"], row["relative_path"].casefold()))
    preview["next_sync_state"] = state
    phase_order = {
        "mkdir": 0, "copy": 1, "update": 1, "move": 2,
        "conflict": 2, "delete": 3, "rmdir": 4,
    }
    preview["operations"].sort(key=lambda operation: phase_order[operation["kind"]])
    _report(progress_callback, phase="finished", message="Comparison complete.",
            total_bytes=summary["bytes"], total_operations=len(preview["operations"]),
            total_skipped=summary["skipped"])
    return preview


def _identity(signature):
    """Extracts directory identity without timestamps changed by child operations.

    Args:
        signature (dict or None): Original directory metadata.

    Returns:
        tuple or None: Identity and mode of the directory.
    """
    if signature is None:
        return None
    return signature["device"], signature["inode"], signature["mode"]


def _assert_snapshot(expected, current, root):
    """Rejects a changed folder scope before any synchronization writes.

    Args:
        expected (dict): Snapshot the user reviewed.
        current (dict): Newly scanned folder state.
        root (str): Folder path used in error messages.
    """
    if _identity(expected["root_signature"]) != _identity(current["root_signature"]):
        raise SyncError(f"Folder changed since the preview. Compare again: {root}")
    if expected["files"] != current["files"]:
        raise SyncError(f"Files changed since the preview. Compare again: {root}")
    expected_directories = {path: _identity(signature) for path, signature in expected["directories"].items()}
    current_directories = {path: _identity(signature) for path, signature in current["directories"].items()}
    if expected_directories != current_directories or set(expected["protected"]) != set(current["protected"]):
        raise SyncError(f"Folder contents changed since the preview. Compare again: {root}")
    if expected["paths"] != current["paths"]:
        raise SyncError(f"Entry names changed since the preview. Compare again: {root}")


def _check_target(path, expected_signature):
    """Checks a destination file immediately before replacing or removing it.

    Args:
        path (str): Destination filename.
        expected_signature (dict or None): Reviewed metadata, or expected absence.
    """
    if expected_signature is None:
        if os.path.lexists(path):
            raise SyncError(f"A new destination entry appeared. Compare again: {path}")
    elif _file_signature(path) != expected_signature:
        raise SyncError(f"Destination changed since the preview. Compare again: {path}")


def _verify_preview(preview, pairs):
    """Validates operation paths before accepting an execution preview.

    Args:
        preview (dict): Preview produced by build_preview.
        pairs (list[dict]): Validated folder pairs.
    """
    if not isinstance(preview.get("operations"), list) or not isinstance(preview.get("snapshots"), dict):
        raise SyncError("The preview is incomplete. Compare again before syncing.")
    pair_lookup = {pair["id"]: pair for pair in pairs}
    for operation in preview["operations"]:
        pair = pair_lookup.get(operation.get("pair_id"))
        kind = operation.get("kind")
        if pair is None or kind not in OPERATION_KINDS:
            raise SyncError("The preview contains an invalid operation.")
        target_root = operation.get("target_root", "destination")
        source_root = operation.get("source_root", "source")
        if target_root not in ("source", "destination") or source_root not in ("source", "destination"):
            raise SyncError("The preview contains an invalid operation direction.")
        expected_destination = _join(pair[target_root], operation.get("relative_path"))
        if os.path.normcase(expected_destination) != os.path.normcase(operation.get("destination", "")):
            raise SyncError("The preview contains an invalid destination path.")
        if kind in ("copy", "update", "move"):
            expected_source = _join(
                pair[source_root], operation.get("source_relative_path", operation["relative_path"])
            )
            if os.path.normcase(expected_source) != os.path.normcase(operation.get("source", "")):
                raise SyncError("The preview contains an invalid source path.")
        if kind == "move":
            copy_action = operation.get("copy_action")
            if (pair.get("mode") != "move" or target_root != "destination"
                    or source_root != "source" or not operation["relative_path"]
                    or not operation.get("source_signature")
                    or copy_action not in (None, "copy", "update")
                    or (copy_action is None and operation.get("destination_signature") is None)
                    or (copy_action == "copy" and operation.get("destination_signature") is not None)
                    or (copy_action == "update" and operation.get("destination_signature") is None)):
                raise SyncError("The preview contains an invalid move operation.")
        if kind in ("delete", "rmdir") and (
            pair.get("mode") not in ("mirror", "two_way") or not operation["relative_path"]
        ):
            raise SyncError("Removal operations require an authorized sync mode and cannot target its root.")
        if kind in ("delete", "rmdir") and pair.get("mode") == "mirror" and target_root != "destination":
            raise SyncError("Mirror removals may target the destination only.")
        if pair["id"] not in preview["snapshots"]:
            raise SyncError("The preview does not contain its original folder snapshots.")
        if kind == "move":
            source_relative_path = operation.get("source_relative_path", operation["relative_path"])
            if _relative_key(source_relative_path) != _relative_key(operation["relative_path"]):
                raise SyncError("The preview contains a move between different relative paths.")
            pair_snapshots = preview["snapshots"][pair["id"]]
            source_key = _relative_key(source_relative_path)
            destination_key = _relative_key(operation["relative_path"])
            if (pair_snapshots["source"]["files"].get(source_key) != operation["source_signature"]
                    or pair_snapshots["destination"]["files"].get(destination_key)
                    != operation.get("destination_signature")):
                raise SyncError("The move operation no longer matches its preview snapshot.")


def _revalidate_pairs(pairs, snapshots, cancel_event, progress_callback):
    """Rechecks every source and destination before starting a write phase.

    Args:
        pairs (list[dict]): Validated pairs.
        snapshots (dict): Expected state for each source and destination.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Scan progress receiver.
    """
    for pair in pairs:
        expected = snapshots[pair["id"]]
        current_source = _scan(pair["source"], pair, cancel_event, progress_callback)
        _assert_snapshot(expected["source"], current_source, pair["source"])
        current_destination = _scan(
            pair["destination"], pair, cancel_event, progress_callback, set(current_source["protected"])
        )
        _assert_snapshot(expected["destination"], current_destination, pair["destination"])


def _open_source(path, expected_signature):
    """Opens an unchanged regular source file without following links where supported.

    Args:
        path (str): Source filename.
        expected_signature (dict): Metadata captured during comparison.

    Returns:
        file: Binary stream owned by the caller.
    """
    if _file_signature(path) != expected_signature:
        raise SyncError(f"Source changed since the preview. Compare again: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        if _signature(os.fstat(descriptor)) != expected_signature:
            raise SyncError(f"Source changed while opening it: {path}")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def _copy_atomic(operation, result, progress, cancel_event, progress_callback, count_as_copy=True):
    """Copies to a temporary sibling and replaces the target only after verification.

    Args:
        operation (dict): Copy, update or move operation.
        result (dict): Aggregate result updated with copied bytes.
        progress (dict): Shared progress counters.
        cancel_event (threading.Event or None): Cancellation flag.
        progress_callback (callable or None): Receives chunk and file progress.
        count_as_copy (bool): Whether to include this transfer in the copy count.

    Returns:
        dict: Signature of the completed destination file.
    """
    source = operation["source"]
    destination = operation["destination"]
    parent = os.path.dirname(destination)
    _validate_components(parent)
    _check_target(destination, operation["destination_signature"])
    parent_identity = _identity(_signature(os.lstat(parent)))
    temporary_path = None
    file_completed = 0
    try:
        with _open_source(source, operation["source_signature"]) as source_stream:
            descriptor, temporary_path = tempfile.mkstemp(prefix=".sync_engine-", suffix=".tmp", dir=parent)
            with os.fdopen(descriptor, "wb") as destination_stream:
                while True:
                    _check_cancel(cancel_event)
                    chunk = source_stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    destination_stream.write(chunk)
                    file_completed += len(chunk)
                    progress["completed_bytes"] += len(chunk)
                    _report(
                        progress_callback, **progress, phase="copy", current_path=source,
                        current_file_bytes=file_completed, current_file_total=operation["size"],
                        message=f"Copying {operation['relative_path']}"
                    )
                destination_stream.flush()
                os.fsync(destination_stream.fileno())
            if _signature(os.fstat(source_stream.fileno())) != operation["source_signature"]:
                raise SyncError(f"Source changed during copying; destination was preserved: {source}")
        if file_completed != operation["size"] or _file_signature(source) != operation["source_signature"]:
            raise SyncError(f"Source changed during copying; destination was preserved: {source}")
        shutil.copystat(source, temporary_path, follow_symlinks=False)
        _check_cancel(cancel_event)
        _validate_components(parent)
        if _identity(_signature(os.lstat(parent))) != parent_identity:
            raise SyncError(f"Destination directory changed during copying: {parent}")
        _check_target(destination, operation["destination_signature"])
        if _file_signature(source) != operation["source_signature"]:
            raise SyncError(f"Source changed during copying; destination was preserved: {source}")
        os.replace(temporary_path, destination)
        temporary_path = None
        if count_as_copy:
            result["copied"] += 1
        result["bytes"] += file_completed
        return _file_signature(destination)
    finally:
        if temporary_path and os.path.lexists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                LOGGER.warning("Could not remove incomplete copy: %s", temporary_path, exc_info=True)


def _make_directory(operation, pair_snapshot):
    """Creates a previewed missing directory and records its new identity.

    Args:
        operation (dict): Planned mkdir operation.
        pair_snapshot (dict): Expected destination snapshot updated after creation.
    """
    destination = operation["destination"]
    _validate_components(destination)
    if os.path.lexists(destination):
        raise SyncError(f"A destination directory appeared since the preview: {destination}")
    os.makedirs(destination, exist_ok=False)
    signature = _signature(os.lstat(destination))
    if operation["relative_path"]:
        relative_key = _relative_key(operation["relative_path"])
        pair_snapshot["directories"][relative_key] = signature
        pair_snapshot["paths"][relative_key] = operation["relative_path"]
    else:
        pair_snapshot["root_signature"] = signature


def _guard_roots(pair, pair_snapshot):
    """Rejects replaced or redirected roots immediately before an operation.

    Args:
        pair (dict): Normalized pair paths.
        pair_snapshot (dict): Expected source and destination identities.
    """
    for root_name in ("source", "destination"):
        root_path = pair[root_name]
        _validate_components(root_path)
        expected_signature = pair_snapshot[root_name]["root_signature"]
        if expected_signature is not None:
            try:
                current_signature = _signature(os.lstat(root_path))
            except OSError as error:
                raise SyncError(f"Folder disappeared during synchronization: {root_path}") from error
            if _identity(expected_signature) != _identity(current_signature):
                raise SyncError(f"Folder was replaced during synchronization: {root_path}")
        elif os.path.lexists(root_path):
            raise SyncError(f"Destination folder appeared since comparison: {root_path}")


def _move_source_file(operation, pair, pair_snapshot, destination_signature, plan,
                      cancel_event, progress_callback):
    """Removes a source file only after its verified destination copy is present.

    Args:
        operation (dict): Reviewed move operation.
        pair (dict): Normalized folder pair.
        pair_snapshot (dict): Original source and updated destination snapshots.
        destination_signature (dict): Expected signature of the completed target file.
        plan (dict): Comparison settings.
        cancel_event (threading.Event or None): Cooperative cancellation flag.
        progress_callback (callable or None): Receives comparison progress.
    """
    source_path = operation["source"]
    destination_path = operation["destination"]
    _guard_roots(pair, pair_snapshot)
    _validate_components(os.path.dirname(source_path))
    _validate_components(os.path.dirname(destination_path))
    if _file_signature(source_path) != operation["source_signature"]:
        raise SyncError(f"Source changed before it could be moved; source preserved: {source_path}")
    if _file_signature(destination_path) != destination_signature:
        raise SyncError(f"Destination changed before source removal; source preserved: {destination_path}")
    if not _same_file(
        source_path, destination_path, operation["source_signature"], destination_signature,
        plan, cancel_event, progress_callback,
    ):
        raise SyncError(f"Destination does not contain the source file; source preserved: {source_path}")
    _check_cancel(cancel_event)
    if (_file_signature(source_path) != operation["source_signature"]
            or _file_signature(destination_path) != destination_signature):
        raise SyncError(f"A file changed before move completion; source preserved: {source_path}")
    os.unlink(source_path)
    source_snapshot = pair_snapshot["source"]
    source_key = _relative_key(operation.get("source_relative_path", operation["relative_path"]))
    source_snapshot["files"].pop(source_key, None)
    source_snapshot["paths"].pop(source_key, None)


def _remove_entry(operation, pair, pair_snapshot):
    """Removes a still-unchanged file or a verified empty extra directory.

    Args:
        operation (dict): Reviewed delete or rmdir operation.
        pair (dict): Normalized pair paths.
        pair_snapshot (dict): Expected state updated after removal.
    """
    destination = operation["destination"]
    target_root = operation.get("target_root", "destination")
    source_root = operation.get("source_root", "source")
    destination_snapshot = pair_snapshot[target_root]
    _guard_roots(pair, pair_snapshot)
    counterpart_root = "destination" if target_root == "source" else "source"
    source_counterpart = _join(pair[counterpart_root], operation["relative_path"])
    if os.path.lexists(source_counterpart):
        raise SyncError(f"A source entry appeared during synchronization; destination preserved: {source_counterpart}")
    _validate_components(os.path.dirname(destination))
    if operation["kind"] == "delete":
        _check_target(destination, operation["destination_signature"])
        os.unlink(destination)
        destination_snapshot["files"].pop(_relative_key(operation["relative_path"]))
    else:
        _validate_components(destination)
        signature = _signature(os.lstat(destination))
        if _identity(signature) != _identity(operation["destination_signature"]):
            raise SyncError(f"Destination directory changed since the preview: {destination}")
        with os.scandir(destination) as iterator:
            if next(iterator, None) is not None:
                raise SyncError(f"Extra directory contains new or protected entries; preserved: {destination}")
        os.rmdir(destination)
        destination_snapshot["directories"].pop(_relative_key(operation["relative_path"]))
    destination_snapshot["paths"].pop(_relative_key(operation["relative_path"]))


def execute_preview(preview, cancel_event=None, progress_callback=None, allow_delete=False,
                    source_removal_confirmation=None):
    """Executes a reviewed preview with revalidation, atomic copying and safe removal.

    All pairs are revalidated before the first write. All copies must succeed
    before mirror removals or source cleanup for moves. Another complete scan
    before removal detects additions or changes made while copying. Cancellation
    preserves completed copies; an unfinished move keeps its source file.

    Args:
        preview (dict): Result of build_preview.
        cancel_event (threading.Event or None): Cooperative cancellation flag.
        progress_callback (callable or None): Function receiving progress dictionaries.
        allow_delete (bool): Explicit authorization for planned removals and moves.
        source_removal_confirmation (callable or None): Optional callback called after move
            destinations are verified and before source files are removed. Receives the
            number of source files and returns whether removal is approved.

    Returns:
        dict: Final status, copied/moved/deleted file counts, bytes, errors and warnings.
    """
    result = {"status": "succeeded", "copied": 0, "moved": 0, "deleted": 0, "bytes": 0,
              "errors": [], "warnings": list(preview.get("warnings", []))}
    progress = {"completed_bytes": 0, "total_bytes": preview.get("summary", {}).get("bytes", 0),
                "completed_operations": 0, "total_operations": len(preview.get("operations", []))}
    pairs = []
    try:
        _check_cancel(cancel_event)
        pairs = _prepare_pairs(preview.get("plan"))
        pair_lookup = {pair["id"]: pair for pair in pairs}
        _verify_preview(preview, pairs)
        snapshots = copy.deepcopy(preview["snapshots"])
        operations = preview["operations"]
        deletions = [operation for operation in operations if operation["kind"] in ("delete", "rmdir")]
        moves = [operation for operation in operations if operation["kind"] == "move"]
        unresolved = [operation["relative_path"] for operation in operations
                      if operation["kind"] == "conflict"]
        if unresolved:
            examples = ", ".join(unresolved[:8])
            suffix = "" if len(unresolved) <= 8 else f" and {len(unresolved) - 8} more"
            raise SyncError(f"Resolve every two-way conflict before syncing: {examples}{suffix}")
        if (deletions or moves) and not allow_delete:
            raise SyncError("This sync includes removals and needs explicit confirmation before syncing.")
        _revalidate_pairs(pairs, snapshots, cancel_event, progress_callback)
        additions = [operation for operation in operations if operation["kind"] not in ("delete", "rmdir")]
        move_destination_signatures = {}
        for operation in additions:
            _check_cancel(cancel_event)
            _guard_roots(pair_lookup[operation["pair_id"]], snapshots[operation["pair_id"]])
            target_root = operation.get("target_root", "destination")
            destination_snapshot = snapshots[operation["pair_id"]][target_root]
            if operation["kind"] == "mkdir":
                _make_directory(operation, destination_snapshot)
            elif operation["kind"] == "move":
                if operation.get("copy_action"):
                    signature = _copy_atomic(
                        operation, result, progress, cancel_event, progress_callback,
                        count_as_copy=False,
                    )
                    relative_key = _relative_key(operation["relative_path"])
                    destination_snapshot["files"][relative_key] = signature
                    destination_snapshot["paths"][relative_key] = operation["relative_path"]
                else:
                    signature = operation["destination_signature"]
                move_destination_signatures[(operation["pair_id"], _relative_key(
                    operation["relative_path"]
                ))] = signature
            else:
                signature = _copy_atomic(operation, result, progress, cancel_event, progress_callback)
                relative_key = _relative_key(operation["relative_path"])
                destination_snapshot["files"][relative_key] = signature
                destination_snapshot["paths"][relative_key] = operation["relative_path"]
            if operation["kind"] != "move":
                progress["completed_operations"] += 1
                LOGGER.info("%s: source=%s destination=%s", operation["kind"],
                            operation.get("source", ""), operation["destination"])
                _report(
                    progress_callback, **progress, phase="copy", current_path=operation["destination"],
                    message=f"Completed {operation['kind']}: "
                    f"{operation['relative_path'] or 'destination folder'}",
                )
            elif operation.get("copy_action"):
                LOGGER.info("Prepared move destination: %s", operation["destination"])
                _report(
                    progress_callback, **progress, phase="copy", current_path=operation["source"],
                    message=f"Prepared move: {operation['relative_path']}",
                )
        if deletions or moves:
            _revalidate_pairs(pairs, snapshots, cancel_event, progress_callback)
        remove_sources = True
        if moves and source_removal_confirmation is not None:
            remove_sources = bool(source_removal_confirmation(len(moves)))
            if not remove_sources:
                result["status"] = "cancelled"
                result["warnings"].append(
                    "Destination copies were verified; source files were kept because removal was declined."
                )
        for operation in deletions:
            _check_cancel(cancel_event)
            _remove_entry(operation, pair_lookup[operation["pair_id"]], snapshots[operation["pair_id"]])
            if operation["kind"] == "delete":
                result["deleted"] += 1
            progress["completed_operations"] += 1
            LOGGER.info("%s: destination=%s", operation["kind"], operation["destination"])
            _report(progress_callback, **progress, phase="delete", current_path=operation["destination"],
                    message=f"Removed {operation['relative_path']}")
        if remove_sources:
            for operation in moves:
                _check_cancel(cancel_event)
                pair = pair_lookup[operation["pair_id"]]
                pair_snapshot = snapshots[operation["pair_id"]]
                destination_signature = move_destination_signatures[(
                    operation["pair_id"], _relative_key(operation["relative_path"])
                )]
                _move_source_file(
                    operation, pair, pair_snapshot, destination_signature, preview["plan"],
                    cancel_event, progress_callback,
                )
                result["moved"] += 1
                progress["completed_operations"] += 1
                LOGGER.info("Moved source=%s destination=%s", operation["source"], operation["destination"])
                _report(
                    progress_callback, **progress, phase="delete", current_path=operation["source"],
                    message=f"Moved {operation['relative_path']} to destination",
                )
    except SyncCancelled:
        result["status"] = "cancelled"
        result["warnings"].append("Cancelled. Completed operations were retained.")
    except (SyncError, OSError, KeyError, TypeError, ValueError) as error:
        result["status"] = "failed"
        result["errors"].append(str(error))
        LOGGER.error("Synchronization failed: %s", error, exc_info=True)
    if result["status"] == "succeeded" and any(pair.get("mode") == "two_way" for pair in pairs):
        try:
            result["sync_state"] = _state_after_sync(
                pairs, preview.get("plan", {}), cancel_event, progress_callback
            )
        except SyncCancelled:
            result["status"] = "cancelled"
            result["warnings"].append(
                "File changes completed, but the two-way history refresh was stopped. "
                "Run Preview again before the next sync."
            )
        except (SyncError, OSError) as error:
            result["status"] = "failed"
            result["errors"].append(f"Sync completed, but its two-way history could not be saved: {error}")
    _report(progress_callback, **progress, phase="finished", message=f"Sync {result['status']}.")
    return result


def _state_after_sync(pairs, plan, cancel_event, progress_callback):
    """Captures a fresh baseline after a successful bidirectional sync."""
    state = {"schema_version": 1, "plan_id": plan.get("id"), "pairs": {}}
    for pair in pairs:
        if pair.get("mode") != "two_way":
            continue
        source = _scan(pair["source"], pair, cancel_event, progress_callback)
        destination = _scan(pair["destination"], pair, cancel_event, progress_callback,
                            set(source["protected"]))
        files = {}
        for key in sorted(set(source["files"]) | set(destination["files"])):
            source_signature = source["files"].get(key)
            destination_signature = destination["files"].get(key)
            source_path = source["paths"].get(key)
            destination_path = destination["paths"].get(key)
            source_hash = None
            destination_hash = None
            if plan.get("compare") == "content":
                if source_signature is not None:
                    source_hash = _hash_file(
                        _join(pair["source"], source_path), source_signature, cancel_event, progress_callback
                    )
                if destination_signature is not None:
                    destination_hash = _hash_file(
                        _join(pair["destination"], destination_path), destination_signature,
                        cancel_event, progress_callback,
                    )
            files[key] = {
                "source_signature": copy.deepcopy(source_signature),
                "destination_signature": copy.deepcopy(destination_signature),
                "source_hash": source_hash,
                "destination_hash": destination_hash,
                "source_path": source_path,
                "destination_path": destination_path,
            }
        state["pairs"][pair["id"]] = {
            "source_root": pair["canonical_source"],
            "destination_root": pair["canonical_destination"],
            "files": files,
        }
    return state
