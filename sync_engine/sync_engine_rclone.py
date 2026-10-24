"""Private rclone setup and Google Drive access for SyncEngine."""

import configparser
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path


VERSION_URL = "https://downloads.rclone.org/version.txt"
DOWNLOAD_ROOT = "https://downloads.rclone.org"
CHUNK_SIZE = 1024 * 1024
RCLONE_PATH_PATTERN = re.compile(r"^rclone://([^/]+)/?(.*)$", re.IGNORECASE)


class RcloneError(RuntimeError):
    """Raised when rclone setup or a Google Drive operation fails."""


class RcloneCancelled(Exception):
    """Raised when an active rclone process is stopped by the user."""


def is_rclone_path(path):
    """Checks whether a destination uses SyncEngine's Google Drive path form.

    Args:
        path (str): Candidate destination path.

    Returns:
        bool: Whether the path begins with the rclone destination prefix.
    """
    return isinstance(path, str) and bool(RCLONE_PATH_PATTERN.match(path.strip()))


def parse_rclone_path(path):
    """Splits a SyncEngine Google Drive destination into remote and folder.

    Args:
        path (str): Path in the ``rclone://remote/folder`` form.

    Returns:
        tuple[str, str]: Remote name and slash-separated folder path.

    Raises:
        ValueError: If the destination is malformed or contains an unsafe path.
    """
    match = RCLONE_PATH_PATTERN.match(str(path or "").strip())
    if not match:
        raise ValueError("Google Drive destinations must use rclone://remote/folder paths.")
    remote_name = match.group(1).strip()
    folder_path = match.group(2).replace("\\", "/").strip("/")
    if not remote_name or ":" in remote_name:
        raise ValueError("The Google Drive remote name is invalid.")
    if any(part in (".", "..") for part in folder_path.split("/")):
        raise ValueError("Google Drive folder paths cannot contain '.' or '..' components.")
    return remote_name, folder_path


def make_rclone_path(remote_name, folder_path):
    """Formats a remote and folder path for SyncEngine plan storage.

    Args:
        remote_name (str): Name of a configured rclone Drive remote.
        folder_path (str): Folder path relative to the remote root.

    Returns:
        str: SyncEngine Google Drive path.

    Raises:
        ValueError: If the remote or folder path is invalid.
    """
    remote_name = str(remote_name or "").strip()
    folder_path = str(folder_path or "").replace("\\", "/").strip("/")
    if not remote_name or ":" in remote_name or "/" in remote_name:
        raise ValueError("The Google Drive remote name is invalid.")
    if any(part in (".", "..") for part in folder_path.split("/")):
        raise ValueError("Google Drive folder paths cannot contain '.' or '..' components.")
    return f"rclone://{remote_name}/{folder_path}" if folder_path else f"rclone://{remote_name}/"


def rclone_remote_path(path):
    """Converts a SyncEngine path to rclone's ``remote:path`` syntax.

    Args:
        path (str): SyncEngine Google Drive path.

    Returns:
        str: rclone remote path.
    """
    remote_name, folder_path = parse_rclone_path(path)
    return f"{remote_name}:{folder_path}"


def join_rclone_path(path, relative_path):
    """Joins a relative file or folder path to a Google Drive destination.

    Args:
        path (str): SyncEngine Google Drive path.
        relative_path (str): Safe path relative to the chosen folder.

    Returns:
        str: Joined SyncEngine Google Drive path.

    Raises:
        ValueError: If the relative path is absolute or escapes the root.
    """
    remote_name, folder_path = parse_rclone_path(path)
    relative_path = str(relative_path or "").replace("\\", "/")
    if relative_path.startswith("/"):
        raise ValueError("An operation contains an absolute Google Drive path.")
    components = [part for part in relative_path.split("/") if part]
    if any(part in (".", "..") for part in components):
        raise ValueError("An operation escapes its Google Drive destination.")
    joined_path = "/".join(part for part in (folder_path, "/".join(components)) if part)
    return make_rclone_path(remote_name, joined_path)


def canonical_rclone_path(path):
    """Returns a normalized key used to compare remote destinations.

    Args:
        path (str): SyncEngine Google Drive path.

    Returns:
        str: Remote and normalized folder path.
    """
    remote_name, folder_path = parse_rclone_path(path)
    parts = [part.casefold() for part in folder_path.split("/") if part]
    return f"{remote_name.casefold()}:{'/'.join(parts)}"


def rclone_paths_overlap(first_path, second_path):
    """Checks whether two remote folders overlap on the same rclone remote.

    Args:
        first_path (str): First SyncEngine Google Drive path.
        second_path (str): Second SyncEngine Google Drive path.

    Returns:
        bool: Whether the paths are equal or one is nested in the other.
    """
    first_remote, first_folder = parse_rclone_path(first_path)
    second_remote, second_folder = parse_rclone_path(second_path)
    if first_remote.casefold() != second_remote.casefold():
        return False
    first_parts = [part.casefold() for part in first_folder.split("/") if part]
    second_parts = [part.casefold() for part in second_folder.split("/") if part]
    common_count = min(len(first_parts), len(second_parts))
    return first_parts[:common_count] == second_parts[:common_count]


def rclone_directory():
    """Returns the private, user-local rclone integration directory.

    Returns:
        pathlib.Path: Directory for the downloaded binary and OAuth config.
    """
    from sync_engine_model import default_data_directory

    return Path(default_data_directory()) / "integrations" / "rclone"


def config_path():
    """Returns the rclone config file used only by SyncEngine.

    Returns:
        pathlib.Path: Private rclone config path.
    """
    return rclone_directory() / "rclone.conf"


def _read_installed_version():
    """Reads the pinned version written after a verified rclone download.

    Returns:
        str: Installed version, or an empty string if none is available.
    """
    marker = rclone_directory() / "installed-version.txt"
    try:
        version = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return version if re.fullmatch(r"v\d+\.\d+\.\d+", version) else ""


def executable_path():
    """Returns the private rclone executable path, when it has been installed.

    Returns:
        pathlib.Path or None: Installed executable path when available.
    """
    extension = ".exe" if os.name == "nt" else ""
    version = _read_installed_version()
    if not version:
        return None
    path = rclone_directory() / f"rclone-{version}{extension}"
    return path if path.is_file() else None


def configured_drive_remotes():
    """Lists configured remotes whose backend is Google Drive.

    Returns:
        list[str]: Google Drive remote names in the private config file.
    """
    path = config_path()
    if not path.is_file():
        return []
    _set_private_permissions(path)
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    try:
        parser.read(str(path), encoding="utf-8")
    except (OSError, UnicodeError, configparser.Error):
        return []
    remote_names = [
        section for section in parser.sections()
        if parser.get(section, "type", fallback="").strip().casefold() == "drive"
    ]
    return sorted(remote_names, key=str.casefold)


def setup_status():
    """Returns a non-sensitive summary of the private rclone setup state.

    Returns:
        dict: Installed binary path, version, and configured Drive remotes.
    """
    return {
        "executable": executable_path(),
        "version": _read_installed_version(),
        "config_path": config_path(),
        "drive_remotes": configured_drive_remotes(),
    }


def ensure_drive_remote(path):
    """Checks that a destination points to an installed and configured Drive remote.

    Args:
        path (str): SyncEngine Google Drive path.

    Returns:
        tuple[pathlib.Path, pathlib.Path]: Binary and config paths.

    Raises:
        RcloneError: If setup is incomplete or the selected remote is not Drive.
    """
    remote_name, unused_folder = parse_rclone_path(path)
    executable = executable_path()
    if executable is None:
        raise RcloneError(
            "Google Drive is not set up. Open Options and choose Set Up Google Drive to download rclone."
        )
    config = config_path()
    if remote_name not in configured_drive_remotes():
        raise RcloneError(
            f"The Google Drive remote '{remote_name}' is not configured. Open Options and finish "
            "Google Drive setup, then choose the destination folder again."
        )
    _set_private_permissions(config)
    return executable, config


def _set_private_permissions(path):
    """Restricts rclone config access where the operating system supports it.

    Args:
        path (pathlib.Path): Config path whose parent contains OAuth credentials.
    """
    if os.name != "nt":
        try:
            path.parent.chmod(0o700)
            if path.exists():
                path.chmod(0o600)
        except OSError:
            pass


def _platform_asset_name():
    """Returns the official rclone download target for the current platform.

    Returns:
        str: Operating system and architecture suffix used by rclone downloads.

    Raises:
        RcloneError: If the current platform has no supported official build.
    """
    machine = platform.machine().casefold().replace(" ", "")
    if machine in ("x86_64", "amd64", "x64"):
        architecture = "amd64"
    elif machine in ("aarch64", "arm64"):
        architecture = "arm64"
    elif machine in ("i386", "i686", "x86"):
        architecture = "386"
    elif machine.startswith("armv7"):
        architecture = "arm-v7"
    elif machine.startswith("armv6"):
        architecture = "arm-v6"
    else:
        raise RcloneError(f"Rclone does not provide a supported build for {platform.machine()}.")

    if os.name == "nt":
        operating_system = "windows"
    elif sys.platform == "darwin":
        if architecture not in ("amd64", "arm64"):
            raise RcloneError("Rclone does not provide this macOS architecture.")
        operating_system = "osx"
    elif sys.platform.startswith("linux"):
        operating_system = "linux"
    else:
        raise RcloneError(f"Rclone downloads are not supported on {platform.system()}.")
    return f"{operating_system}-{architecture}"


def _download_bytes(url, timeout=30):
    """Reads a small HTTPS metadata file from the official rclone site.

    Args:
        url (str): Official metadata URL.
        timeout (int): Maximum request duration in seconds.

    Returns:
        bytes: Response body, limited to 64 KiB.

    Raises:
        RcloneError: If the metadata request fails.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "SyncEngine"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(64 * 1024)
    except (OSError, urllib.error.URLError) as error:
        raise RcloneError(f"Could not read rclone download metadata: {error}") from error


def download_rclone(report_progress=None, cancel_event=None):
    """Downloads and verifies the official rclone build for this computer.

    The executable is stored outside the application package. Its SHA-256 is
    checked against the versioned checksum manifest published by rclone.org.

    Args:
        report_progress (callable, optional): Receives short progress messages.
        cancel_event (threading.Event, optional): Cancellation flag.

    Returns:
        pathlib.Path: Installed rclone executable.

    Raises:
        RcloneError: If the release cannot be downloaded or verified.
        RcloneCancelled: If the user cancels while the archive is downloading.
    """
    root = rclone_directory()
    root.mkdir(parents=True, exist_ok=True)
    _set_private_permissions(root / "rclone.conf")
    asset_platform = _platform_asset_name()
    version_text = _download_bytes(VERSION_URL).decode("utf-8", errors="replace")
    match = re.search(r"\bv(\d+\.\d+\.\d+)\b", version_text)
    if not match:
        raise RcloneError("The rclone download site returned an invalid version number.")
    version = f"v{match.group(1)}"
    archive_name = f"rclone-{version}-{asset_platform}.zip"
    manifest_url = f"{DOWNLOAD_ROOT}/{version}/SHA256SUMS"
    manifest_text = _download_bytes(manifest_url).decode("ascii", errors="replace")
    expected_hash = None
    for line in manifest_text.splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == archive_name:
            expected_hash = fields[0].casefold()
            break
    if not expected_hash or not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
        raise RcloneError(f"The official checksum manifest does not include {archive_name}.")

    archive_path = None
    binary_name = "rclone.exe" if os.name == "nt" else "rclone"
    installed_path = root / f"rclone-{version}{'.exe' if os.name == 'nt' else ''}"
    try:
        descriptor, archive_name_on_disk = tempfile.mkstemp(
            prefix="rclone-download-", suffix=".zip", dir=str(root)
        )
        os.close(descriptor)
        archive_path = Path(archive_name_on_disk)
        archive_url = f"{DOWNLOAD_ROOT}/{version}/{archive_name}"
        request = urllib.request.Request(archive_url, headers={"User-Agent": "SyncEngine"})
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except (OSError, urllib.error.URLError) as error:
            raise RcloneError(f"Could not download rclone from downloads.rclone.org: {error}") from error
        digest = hashlib.sha256()
        total_bytes = int(response.headers.get("Content-Length") or 0)
        downloaded = 0
        with response, archive_path.open("wb") as archive_stream:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RcloneCancelled("Rclone download cancelled.")
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                archive_stream.write(chunk)
                digest.update(chunk)
                downloaded += len(chunk)
                if report_progress:
                    if total_bytes:
                        percentage = int(downloaded * 100 / total_bytes)
                        report_progress(f"Downloading rclone {version}: {percentage}%")
                    else:
                        report_progress(f"Downloading rclone {version}: {downloaded:,} bytes")
        if digest.hexdigest().casefold() != expected_hash:
            raise RcloneError("The downloaded rclone archive failed its SHA-256 check.")

        if report_progress:
            report_progress("Verifying the rclone archive...")
        try:
            with zipfile.ZipFile(str(archive_path), "r") as archive:
                damaged_member = archive.testzip()
                if damaged_member:
                    raise RcloneError(f"The rclone archive is damaged: {damaged_member}")
                candidates = [
                    item for item in archive.infolist()
                    if Path(item.filename.replace("\\", "/")).name == binary_name
                    and not item.is_dir()
                ]
                if len(candidates) != 1:
                    raise RcloneError("The rclone archive did not contain one expected executable.")
                member = candidates[0]
                member_parts = Path(member.filename.replace("\\", "/")).parts
                if len(member_parts) < 2 or any(part in (".", "..") for part in member_parts):
                    raise RcloneError("The rclone executable has an unsafe archive path.")
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix="rclone-install-", dir=str(root)
                )
                os.close(descriptor)
                temporary_binary = Path(temporary_name)
                try:
                    with archive.open(member, "r") as source_stream:
                        with temporary_binary.open("wb") as destination_stream:
                            shutil.copyfileobj(source_stream, destination_stream, CHUNK_SIZE)
                    if os.name != "nt":
                        temporary_binary.chmod(0o755)
                    os.replace(str(temporary_binary), str(installed_path))
                finally:
                    if temporary_binary.exists():
                        temporary_binary.unlink()
        except (OSError, zipfile.BadZipFile) as error:
            raise RcloneError(f"Could not extract the verified rclone executable: {error}") from error

        marker_path = root / "installed-version.txt"
        marker_temporary = root / ".installed-version.tmp"
        marker_temporary.write_text(version, encoding="utf-8")
        os.replace(str(marker_temporary), str(marker_path))
        if report_progress:
            report_progress(f"Rclone {version} is ready.")
        return installed_path
    finally:
        if archive_path is not None:
            try:
                archive_path.unlink()
            except OSError:
                pass


def command_prefix(path=None):
    """Builds the private rclone command prefix with SyncEngine's config path.

    Args:
        path (pathlib.Path or str, optional): Explicit executable path.

    Returns:
        list[str]: Executable and private config arguments.

    Raises:
        RcloneError: If rclone has not been installed.
    """
    executable = Path(path) if path else executable_path()
    if executable is None or not executable.is_file():
        raise RcloneError(
            "Rclone is not installed. Open Options and choose Set Up Google Drive to download it."
        )
    return [str(executable), "--config", str(config_path())]


def folder_list_command(remote_name, folder_path=""):
    """Builds an asynchronous command for listing one level of Drive folders.

    Args:
        remote_name (str): Configured rclone remote name.
        folder_path (str): Folder relative to the remote root.

    Returns:
        list[str]: Command arguments for QProcess.
    """
    normalized_folder = str(folder_path or "").replace("\\", "/").strip("/")
    remote_root = f"{remote_name}:{normalized_folder}"
    return command_prefix() + ["lsjson", "--dirs-only", "--max-depth", "1", remote_root]


def _report_progress(callback, phase, message, current_path=""):
    """Sends a detached progress dictionary to an optional engine callback.

    Args:
        callback (callable or None): Progress receiver.
        phase (str): Operation phase.
        message (str): Short status message.
        current_path (str): Optional path associated with the operation.
    """
    if callback is not None:
        callback({"phase": phase, "message": message, "current_path": current_path})


def run_rclone(arguments, cancel_event=None, progress_callback=None, phase="scan",
               message="Contacting Google Drive...", current_path="", timeout=0):
    """Runs rclone with cancellation checks without blocking the UI thread.

    Args:
        arguments (list[str]): Arguments following the global rclone options.
        cancel_event (threading.Event, optional): Cooperative cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.
        phase (str): User-visible phase for periodic status updates.
        message (str): Status text while rclone is running.
        current_path (str): Path shown in the application progress area.
        timeout (float): Optional maximum number of seconds for this command.

    Returns:
        tuple[int, str, str]: Exit code, UTF-8 stdout, and UTF-8 stderr.

    Raises:
        RcloneError: If the command cannot start or exceeds its timeout.
        RcloneCancelled: If cancellation is requested.
    """
    command = command_prefix() + [str(argument) for argument in arguments]
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise RcloneError(f"Could not start rclone: {error}") from error
    started_at = time.monotonic()
    while True:
        try:
            stdout, stderr = process.communicate(timeout=0.4)
            break
        except subprocess.TimeoutExpired:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise RcloneCancelled("Google Drive operation cancelled.")
            if timeout and time.monotonic() - started_at >= timeout:
                process.terminate()
                try:
                    process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise RcloneError("The Google Drive request timed out. Check the connection and try again.")
            _report_progress(progress_callback, phase, message, current_path)
    return (
        process.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


def run_rclone_checked(arguments, cancel_event=None, progress_callback=None, phase="scan",
                       message="Contacting Google Drive...", current_path="", timeout=0):
    """Runs rclone and raises an actionable error for a nonzero exit status.

    Args:
        arguments (list[str]): Arguments following the global rclone options.
        cancel_event (threading.Event, optional): Cooperative cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.
        phase (str): User-visible phase for periodic status updates.
        message (str): Status text while rclone is running.
        current_path (str): Path shown in the application progress area.
        timeout (float): Optional maximum command duration in seconds.

    Returns:
        tuple[str, str]: UTF-8 stdout and stderr.

    Raises:
        RcloneError: If the command fails.
        RcloneCancelled: If cancellation is requested.
    """
    return_code, stdout, stderr = run_rclone(
        arguments, cancel_event=cancel_event, progress_callback=progress_callback,
        phase=phase, message=message, current_path=current_path, timeout=timeout,
    )
    if return_code:
        detail = (stderr or stdout).strip()
        detail = detail.splitlines()[-1] if detail else f"rclone exited with status {return_code}."
        lowered = detail.casefold()
        if "token" in lowered or "auth" in lowered or "oauth" in lowered:
            detail += " Open Options > Google Drive and reconnect the account with rclone config."
        raise RcloneError(detail)
    return stdout, stderr


def _remote_argument(path):
    """Validates a SyncEngine remote and returns its native rclone path.

    Args:
        path (str): SyncEngine Google Drive path.

    Returns:
        tuple[pathlib.Path, str]: Executable path and native remote path.
    """
    executable, unused_config = ensure_drive_remote(path)
    return executable, rclone_remote_path(path)


def list_remote_entries(path, cancel_event=None, progress_callback=None, scan_side="backup"):
    """Lists all files and folders below a configured Google Drive destination.

    Args:
        path (str): SyncEngine Google Drive root path.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.
        scan_side (str): Label used in scan progress messages.

    Returns:
        list[dict]: rclone metadata entries below the selected root.

    Raises:
        RcloneError: If rclone cannot list or decode the remote contents.
        RcloneCancelled: If cancellation is requested.
    """
    unused_executable, remote_path = _remote_argument(path)
    output, unused_error = run_rclone_checked(
        ["lsjson", "--recursive", "--hash", remote_path],
        cancel_event=cancel_event, progress_callback=progress_callback,
        phase="scan", message="Listing Google Drive folders...", current_path=path,
    )
    try:
        entries = json.loads(output)
    except json.JSONDecodeError as error:
        raise RcloneError("Rclone returned invalid Google Drive listing data.") from error
    if not isinstance(entries, list):
        raise RcloneError("Rclone returned an unexpected Google Drive listing format.")
    return entries


def remote_entry_signature(entry, remote_identity):
    """Converts rclone JSON metadata to a serializable sync-engine signature.

    Args:
        entry (dict): rclone metadata for a file or folder.
        remote_identity (str): Stable path used when Drive does not return an ID.

    Returns:
        dict: Signature compatible with SyncEngine snapshot comparisons.

    Raises:
        ValueError: If rclone returned malformed entry data.
    """
    if not isinstance(entry, dict):
        raise ValueError("A Google Drive listing entry must be an object.")
    is_directory = bool(entry.get("IsDir"))
    size = entry.get("Size", 0)
    if is_directory:
        size = 0
    if type(size) is not int or size < 0:
        raise ValueError("Google Drive returned an invalid file size.")
    modified = entry.get("ModTime") or ""
    modified_ns = 0
    if modified:
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            modified_ns = int(parsed.timestamp() * 1_000_000_000)
        except (ValueError, OverflowError, OSError):
            modified_ns = 0
    hashes = entry.get("Hashes") if isinstance(entry.get("Hashes"), dict) else {}
    md5_hash = next((
        str(value).casefold() for key, value in hashes.items()
        if str(key).casefold() == "md5" and value
    ), None)
    return {
        "size": size,
        "mtime_ns": modified_ns,
        "ctime_ns": 0,
        "device": "rclone",
        "inode": str(entry.get("ID") or remote_identity),
        "mode": stat.S_IFDIR if is_directory else stat.S_IFREG,
        "hash_md5": md5_hash,
        "mime_type": str(entry.get("MimeType") or ""),
    }


def remote_root_signature(path, entry=None):
    """Returns a stable signature for a selected remote folder root.

    Args:
        path (str): SyncEngine Google Drive folder path.
        entry (dict, optional): Folder metadata returned by rclone.

    Returns:
        dict: Folder signature, using its Drive ID when metadata is available.
    """
    if entry is not None:
        return remote_entry_signature(entry, path)
    return {
        "size": 0,
        "mtime_ns": 0,
        "ctime_ns": 0,
        "device": "rclone",
        "inode": canonical_rclone_path(path),
        "mode": stat.S_IFDIR,
        "hash_md5": None,
        "mime_type": "",
    }


def remote_entry(path, relative_path="", cancel_event=None, progress_callback=None):
    """Looks up one file or folder, falling back to its parent when absent.

    Args:
        path (str): SyncEngine Google Drive root path.
        relative_path (str): Optional path below the selected root.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Progress receiver.

    Returns:
        tuple[dict or None, str]: Entry metadata and the full SyncEngine path.

    Raises:
        RcloneError: If the entry or its parent cannot be queried.
        RcloneCancelled: If cancellation is requested.
    """
    target = join_rclone_path(path, relative_path)
    unused_executable, remote_target = _remote_argument(target)
    return_code, output, unused_error = run_rclone(
        ["lsjson", "--stat", "--hash", remote_target],
        cancel_event=cancel_event, progress_callback=progress_callback,
        phase="scan", message="Checking Google Drive destination...", current_path=target,
    )
    if return_code == 0:
        try:
            entry = json.loads(output)
        except json.JSONDecodeError as error:
            raise RcloneError("Rclone returned invalid Google Drive entry data.") from error
        if not isinstance(entry, dict):
            raise RcloneError("Rclone returned an unexpected Google Drive entry format.")
        return entry, target

    # A missing target is normal for planned uploads. Querying its parent
    # distinguishes absence from an authentication or connection failure.
    remote_name, target_folder = parse_rclone_path(target)
    parent_folder, unused_separator, item_name = target_folder.rpartition("/")
    parent_path = f"{remote_name}:{parent_folder}"
    output, unused_error = run_rclone_checked(
        ["lsjson", "--hash", parent_path],
        cancel_event=cancel_event, progress_callback=progress_callback,
        phase="scan", message="Checking Google Drive destination...", current_path=target,
    )
    try:
        entries = json.loads(output)
    except json.JSONDecodeError as error:
        raise RcloneError("Rclone returned invalid Google Drive listing data.") from error
    if not isinstance(entries, list):
        raise RcloneError("Rclone returned an unexpected Google Drive listing format.")
    if any(not isinstance(entry, dict) for entry in entries):
        raise RcloneError("Rclone returned an invalid Google Drive listing entry.")
    matches = [entry for entry in entries if str(entry.get("Name", "")) == item_name]
    if len(matches) > 1:
        raise RcloneError(f"Google Drive contains multiple entries named '{item_name}'. Resolve that name first.")
    return (matches[0], remote_target) if matches else (None, remote_target)


def hash_remote_file(path, algorithm="sha256", cancel_event=None, progress_callback=None):
    """Hashes one remote file with cancellation checks during transfer and hashing.

    Args:
        path (str): SyncEngine Google Drive file path.
        algorithm (str): Hash algorithm supported by hashlib.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Progress receiver.

    Returns:
        str: Content digest as a lowercase hexadecimal string.

    Raises:
        RcloneError: If rclone cannot read the remote file.
        RcloneCancelled: If cancellation is requested.
    """
    unused_executable, remote_path = _remote_argument(path)
    command = command_prefix() + ["cat", remote_path]
    output_stream = tempfile.TemporaryFile()
    stderr_stream = tempfile.TemporaryFile()
    try:
        process = subprocess.Popen(
            command, stdout=output_stream, stderr=stderr_stream,
        )
    except OSError as error:
        output_stream.close()
        stderr_stream.close()
        raise RcloneError(f"Could not start rclone: {error}") from error
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise RcloneCancelled("Google Drive comparison cancelled.")
            try:
                process.wait(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                _report_progress(
                    progress_callback, "compare", "Comparing Google Drive file contents...", path,
                )
        return_code = process.returncode
        if return_code:
            stderr_stream.seek(0)
            detail = stderr_stream.read().decode("utf-8", errors="replace").strip()
            detail = detail.splitlines()[-1] if detail else f"rclone exit {return_code}."
            raise RcloneError(f"Could not read Google Drive file contents: {detail}")
        digest = hashlib.new(algorithm)
        output_stream.seek(0)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RcloneCancelled("Google Drive comparison cancelled.")
            chunk = output_stream.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            _report_progress(
                progress_callback, "compare", "Comparing Google Drive file contents...", path,
            )
        return digest.hexdigest()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        output_stream.close()
        stderr_stream.close()


def local_file_hash(path, algorithm="md5", cancel_event=None, progress_callback=None):
    """Hashes a local file in chunks for comparison with Google Drive metadata.

    Args:
        path (str): Local file path.
        algorithm (str): Hash algorithm supported by hashlib.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.

    Returns:
        str: Content digest as a lowercase hexadecimal string.

    Raises:
        RcloneError: If the local file cannot be read.
        RcloneCancelled: If cancellation is requested.
    """
    digest = hashlib.new(algorithm)
    try:
        with open(path, "rb") as stream:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise RcloneCancelled("Google Drive comparison cancelled.")
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                _report_progress(
                    progress_callback, "compare", "Comparing file contents...", path,
                )
    except OSError as error:
        raise RcloneError(f"Could not read '{path}' for comparison: {error}") from error
    return digest.hexdigest()


def copy_remote_file(source_path, destination_path, cancel_event=None, progress_callback=None):
    """Copies a local file to Drive or a Drive file to a local path.

    Args:
        source_path (str): Local or rclone source path.
        destination_path (str): Local or rclone destination path.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.

    Raises:
        RcloneError: If rclone cannot copy the file.
        RcloneCancelled: If cancellation is requested.
    """
    arguments = [
        "copyto", source_path, destination_path, "--ignore-times",
        "--stats", "1s", "--stats-one-line", "--stats-log-level", "NOTICE",
    ]
    run_rclone_checked(
        arguments, cancel_event=cancel_event, progress_callback=progress_callback,
        phase="copy", message="Transferring file with rclone...", current_path=destination_path,
    )


def make_remote_directory(path, cancel_event=None, progress_callback=None):
    """Creates one Google Drive folder using rclone.

    Args:
        path (str): SyncEngine folder path to create.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.

    Raises:
        RcloneError: If rclone cannot create the folder.
        RcloneCancelled: If cancellation is requested.
    """
    unused_executable, remote_path = _remote_argument(path)
    run_rclone_checked(
        ["mkdir", remote_path], cancel_event=cancel_event, progress_callback=progress_callback,
        phase="copy", message="Creating Google Drive folder...", current_path=path,
    )


def remove_remote_file(path, cancel_event=None, progress_callback=None):
    """Permanently removes one reviewed Google Drive file.

    Args:
        path (str): SyncEngine file path approved for removal.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.

    Raises:
        RcloneError: If rclone cannot remove the file.
        RcloneCancelled: If cancellation is requested.
    """
    unused_executable, remote_path = _remote_argument(path)
    run_rclone_checked(
        ["deletefile", remote_path, "--drive-use-trash=false"],
        cancel_event=cancel_event, progress_callback=progress_callback,
        phase="delete", message="Removing reviewed Google Drive file...", current_path=path,
    )


def remove_remote_directory(path, cancel_event=None, progress_callback=None):
    """Removes one reviewed empty Google Drive folder.

    Args:
        path (str): SyncEngine folder path approved for removal.
        cancel_event (threading.Event, optional): Cancellation flag.
        progress_callback (callable, optional): Engine progress receiver.

    Raises:
        RcloneError: If rclone cannot remove the folder.
        RcloneCancelled: If cancellation is requested.
    """
    unused_executable, remote_path = _remote_argument(path)
    run_rclone_checked(
        ["rmdir", remote_path], cancel_event=cancel_event, progress_callback=progress_callback,
        phase="delete", message="Removing reviewed Google Drive folder...", current_path=path,
    )


def launch_config_wizard():
    """Opens rclone config in a visible terminal for Google OAuth authorization.

    Returns:
        subprocess.Popen or None: Process on Windows, where SyncEngine can watch it.

    Raises:
        RcloneError: If no suitable terminal could be opened.
    """
    executable = executable_path()
    if executable is None:
        raise RcloneError("Download rclone before opening its configuration.")
    root = rclone_directory()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise RcloneError(f"Could not prepare the rclone configuration folder: {error}") from error
    _set_private_permissions(config_path())
    command = [str(executable), "--config", str(config_path()), "config"]
    if os.name == "nt":
        creation_flag = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)
        try:
            return subprocess.Popen(command, creationflags=creation_flag, close_fds=True)
        except OSError as error:
            raise RcloneError(f"Could not open rclone configuration: {error}") from error
    if sys.platform == "darwin":
        terminal_command = " ".join(shlex.quote(argument) for argument in command)
        escaped_command = terminal_command.replace("\\", "\\\\").replace('"', '\\"')
        script = f'tell application "Terminal" to do script "{escaped_command}"'
        try:
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except OSError as error:
            raise RcloneError(f"Could not open Terminal for rclone setup: {error}") from error
        return None

    terminal_commands = (
        ("x-terminal-emulator", ["-e"]),
        ("gnome-terminal", ["--"]),
        ("konsole", ["-e"]),
        ("xfce4-terminal", ["-e"]),
    )
    for terminal_name, prefix in terminal_commands:
        terminal = shutil.which(terminal_name)
        if terminal:
            try:
                subprocess.Popen([terminal] + prefix + command, close_fds=True)
                return None
            except OSError:
                continue
    raise RcloneError(
        "Could not find a terminal window. Open a terminal and run:\n"
        f"{' '.join(shlex.quote(argument) for argument in command)}"
    )
