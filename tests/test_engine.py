"""Filesystem safety and synchronization regression tests using disposable folders."""

import copy
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
import sync_engine_engine as engine


class TestSyncEngine(unittest.TestCase):
    """Exercises backups against temporary folders without touching user data."""

    def setUp(self):
        """Creates isolated source and destination folders for every test."""
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="sync_engine_test_")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.destination = self.root / "destination"
        self.source.mkdir()
        self.destination.mkdir()
        self.plan = {
            "schema_version": 1, "id": "plan-test", "name": "Test plan",
            "pairs": [{"id": "pair-one", "name": "Pair", "source": str(self.source),
                       "destination": str(self.destination), "mode": "update", "ignore_folders": [],
                       "ignore_match": "prefix", "ignore_files": []}],
            "compare": "metadata", "timestamp_tolerance": 0.0, "allow_empty_mirror": False,
            "automation": {"countdown_seconds": 10, "start_minimized": False, "allow_deletions": False},
        }
        logger_patch = mock.patch.object(engine.LOGGER, "error")
        logger_patch.start()
        self.addCleanup(logger_patch.stop)

    def write_file(self, directory, relative_path, content="content", timestamp=None):
        """Writes a fixture and optionally supplies a deterministic modification time.

        Args:
            directory (pathlib.Path): Root folder for the fixture.
            relative_path (str): Filename relative to the root.
            content (str or bytes): File data.
            timestamp (int or None): Modification time in nanoseconds.

        Returns:
            pathlib.Path: Created file.
        """
        path = directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        if timestamp is not None:
            os.utime(path, ns=(timestamp, timestamp))
        return path

    def preview(self):
        """Builds a preview from the current fixture plan.

        Returns:
            dict: Read-only synchronization preview.
        """
        return engine.build_preview(self.plan)

    def test_build_preview_expands_custom_and_sync_file_variables(self):
        """Scans the resolved roots while leaving the original templates intact."""
        self.write_file(self.source, "example.txt", "source")
        target_root = self.root / "variable_target"
        self.plan["variables"] = {"target_root": str(target_root)}
        self.plan["pairs"][0]["source"] = "{plan-dir}/source"
        self.plan["pairs"][0]["destination"] = "{target_root}/backup"
        plan_file = self.root / "nightly.syncplan"

        preview = engine.build_preview(self.plan, plan_file_path=plan_file)

        self.assertEqual("{plan-dir}/source", self.plan["pairs"][0]["source"])
        self.assertEqual(os.path.abspath(self.source), preview["plan"]["pairs"][0]["source"])
        self.assertEqual(os.path.abspath(target_root / "backup"), preview["plan"]["pairs"][0]["destination"])
        self.assertEqual(1, preview["summary"]["copy"])

    def sync(self, allow_delete=False):
        """Builds and executes the current fixture plan.

        Args:
            allow_delete (bool): Whether mirror deletion is authorized.

        Returns:
            dict: Synchronization execution result.
        """
        return engine.execute_preview(self.preview(), allow_delete=allow_delete)

    def create_directory_link(self, target, link_path):
        """Creates a Windows junction or a symbolic directory link for safety tests.

        Args:
            target (pathlib.Path): Existing folder outside the synchronization scope.
            link_path (pathlib.Path): New linked folder path.
        """
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", str(link_path), str(target)],
                capture_output=True, check=False,
            )
            if completed.returncode:
                self.skipTest("Windows junction creation is unavailable.")
            self.addCleanup(os.rmdir, link_path)
        else:
            try:
                os.symlink(target, link_path, target_is_directory=True)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"Symbolic link creation unavailable: {error}")

    def test_preview_is_read_only_and_creates_missing_destination_on_execution(self):
        """Preview leaves absent destinations untouched until execution."""
        self.destination.rmdir()
        self.write_file(self.source, "nested/新しい.txt", "日本語")
        preview = self.preview()
        self.assertFalse(self.destination.exists())
        self.assertEqual(2, preview["summary"]["mkdir"])
        self.assertEqual(1, preview["summary"]["copy"])
        result = engine.execute_preview(preview)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("日本語", (self.destination / "nested/新しい.txt").read_text(encoding="utf-8"))

    def test_unchanged_metadata_skips_file_and_preserves_timestamp(self):
        """Equal size and modification time avoid copying on subsequent runs."""
        source_file = self.write_file(self.source, "item.txt", "one", 1_700_000_000_000_000_000)
        result = self.sync()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(source_file.stat().st_mtime_ns, (self.destination / "item.txt").stat().st_mtime_ns)
        preview = self.preview()
        self.assertEqual(1, preview["summary"]["unchanged"])
        self.assertEqual([], preview["operations"])
        self.assertEqual(["item.txt"], [row["relative_path"] for row in preview["unchanged"]])

    def test_new_files_and_folders_preserve_source_spelling(self):
        """New output names retain mixed-case spelling on Windows."""
        self.write_file(self.source, "CamelFolder/NestedFolder/MixedCase.TXT", "source")
        result = self.sync()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(["CamelFolder"], [entry.name for entry in self.destination.iterdir()])
        self.assertEqual(["NestedFolder"], [entry.name for entry in (self.destination / "CamelFolder").iterdir()])
        self.assertEqual(["MixedCase.TXT"],
                         [entry.name for entry in (self.destination / "CamelFolder/NestedFolder").iterdir()])

    @unittest.skipUnless(os.name == "nt", "Requires Windows case-insensitive folder lookup.")
    def test_update_preserves_existing_destination_spelling(self):
        """Updates use source and destination names independently on Windows."""
        self.write_file(self.source, "CamelFolder/MixedCase.TXT", "updated source")
        self.write_file(self.destination, "camelfolder/mixedcase.txt", "old")
        preview = self.preview()
        self.assertEqual(1, preview["summary"]["update"])
        self.assertEqual(0, preview["summary"]["copy"])
        result = engine.execute_preview(preview)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(["camelfolder"], [entry.name for entry in self.destination.iterdir()])
        self.assertEqual(["mixedcase.txt"], [entry.name for entry in (self.destination / "camelfolder").iterdir()])
        self.assertEqual("updated source", (self.destination / "camelfolder/mixedcase.txt").read_text())

    @unittest.skipUnless(os.name == "nt", "Requires Windows case-insensitive folder lookup.")
    def test_mirror_new_entries_preserve_existing_ancestor_spelling(self):
        """Mixed ancestor casing stays consistent through the mirror revalidation scan."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "CamelFolder/NewFolder/NewFile.TXT", "source")
        self.write_file(self.destination, "camelfolder/obsolete.txt", "remove")
        result = self.sync(allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(["camelfolder"], [entry.name for entry in self.destination.iterdir()])
        self.assertEqual(["NewFolder"], [entry.name for entry in (self.destination / "camelfolder").iterdir()])
        self.assertEqual(["NewFile.TXT"],
                         [entry.name for entry in (self.destination / "camelfolder/NewFolder").iterdir()])

    def test_update_replaces_changed_files_and_keeps_destination_extras(self):
        """Update mode copies changed/new files and retains extra destinations."""
        self.write_file(self.source, "item.txt", "updated source")
        self.write_file(self.source, "new.txt", "new")
        self.write_file(self.destination, "item.txt", "old")
        self.write_file(self.destination, "extra.txt", "keep")
        preview = self.preview()
        self.assertEqual(1, preview["summary"]["copy"])
        self.assertEqual(1, preview["summary"]["update"])
        self.assertEqual(0, preview["summary"]["delete"])
        preserved = next(row for row in preview["unchanged"] if row["relative_path"] == "extra.txt")
        self.assertIn("preserved", preserved["reason"])
        result = engine.execute_preview(preview)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("updated source", (self.destination / "item.txt").read_text())
        self.assertTrue((self.destination / "extra.txt").exists())

    def test_add_mode_never_replaces_existing_files(self):
        """Add mode ignores existing paths even when their contents differ."""
        self.plan["pairs"][0]["mode"] = "add"
        self.write_file(self.source, "existing.txt", "replace me")
        self.write_file(self.destination, "existing.txt", "keep me")
        self.write_file(self.source, "new.txt", "new")
        result = self.sync()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(1, result["copied"])
        self.assertEqual("keep me", (self.destination / "existing.txt").read_text())

    def test_mirror_removes_extra_files_and_empty_directories(self):
        """Mirror pruning happens after copies and removes empty extra subtrees."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "keep.txt", "source")
        self.write_file(self.destination, "obsolete/deeper/old.txt", "obsolete")
        preview = self.preview()
        self.assertEqual(1, preview["summary"]["delete"])
        self.assertEqual(2, preview["summary"]["rmdir"])
        result = engine.execute_preview(preview, allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(1, result["deleted"])
        self.assertFalse((self.destination / "obsolete").exists())

    def test_mirror_requires_explicit_delete_permission_before_any_writes(self):
        """Unapproved mirror removal prevents copying as well as deleting."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "new.txt", "new")
        self.write_file(self.destination, "extra.txt", "old")
        result = self.sync()
        self.assertEqual("failed", result["status"])
        self.assertFalse((self.destination / "new.txt").exists())
        self.assertTrue((self.destination / "extra.txt").exists())

    def test_empty_mirror_is_blocked_unless_explicitly_allowed(self):
        """Empty sources cannot prune destinations without an explicit override."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.destination, "valuable.txt", "preserve")
        with self.assertRaises(engine.SyncError):
            self.preview()
        self.plan["allow_empty_mirror"] = True
        result = self.sync(allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertFalse((self.destination / "valuable.txt").exists())

    def test_folder_prefixes_are_literal_and_protect_destination_subtrees(self):
        """Bracketed prefixes match literal names and exclusions protect mirror data."""
        self.plan["pairs"][0].update({"mode": "mirror", "ignore_folders": ["[Inbox]", "02_tasks", "03_output"]})
        self.write_file(self.source, "keep.txt", "kept")
        for relative_path in ("[Inbox] waiting/a.txt", "nested/02_tasks/a.txt", "03_output_v2/a.txt"):
            self.write_file(self.source, relative_path, "ignored")
            self.write_file(self.destination, relative_path, "preserved")
        self.write_file(self.destination, "[Inbox] absent/target-only.txt", "preserved")
        self.write_file(self.destination, "remove.txt", "obsolete")
        result = self.sync(allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertFalse((self.destination / "remove.txt").exists())
        self.assertEqual("preserved", (self.destination / "[Inbox] waiting/a.txt").read_text())
        self.assertTrue((self.destination / "[Inbox] absent/target-only.txt").exists())
        self.assertTrue((self.destination / "nested/02_tasks/a.txt").exists())

    def test_exact_folder_filter_keeps_longer_names(self):
        """Exact matching excludes only complete folder names."""
        self.plan["pairs"][0].update({"ignore_folders": ["tasks"], "ignore_match": "exact"})
        self.write_file(self.source, "tasks/a.txt", "ignore")
        self.write_file(self.source, "tasks_v2/a.txt", "include")
        result = self.sync()
        self.assertEqual("succeeded", result["status"])
        self.assertFalse((self.destination / "tasks").exists())
        self.assertTrue((self.destination / "tasks_v2/a.txt").exists())

    def test_preview_explains_exclusion_rule_for_skipped_items(self):
        """Emits a human-readable reason for every item excluded during scanning."""
        self.plan["pairs"][0]["exclusions"] = [{
            "pattern": "private", "match": "starts_with", "applies_to": "files", "ignore_case": False,
        }]
        self.write_file(self.source, "private_notes.txt", "excluded")
        preview = self.preview()
        self.assertEqual(1, len(preview["skipped"]))
        self.assertEqual("private_notes.txt", preview["skipped"][0]["relative_path"])
        self.assertIn("starts with 'private'", preview["skipped"][0]["reason"])

    def test_scan_progress_identifies_live_preview_entries_and_filter_reasons(self):
        """Reports discovered paths and explains excluded entries before finalization."""
        self.plan["pairs"][0]["exclusions"] = [{
            "pattern": "private", "match": "starts_with", "applies_to": "files", "ignore_case": True,
        }]
        self.write_file(self.source, "public.txt")
        self.write_file(self.source, "private_notes.txt")
        progress = []

        engine.build_preview(self.plan, progress_callback=lambda values: progress.append(dict(values)))

        entry_updates = [
            values for values in progress
            if values.get("phase") == "scan" and values.get("relative_path")
        ]
        self.assertTrue(entry_updates)
        self.assertTrue(any(values.get("relative_path") == "public.txt" for values in entry_updates))
        skipped = next(values for values in entry_updates if values.get("relative_path") == "private_notes.txt")
        self.assertEqual("pair-one", skipped["pair_id"])
        self.assertEqual("source", skipped["scan_side"])
        self.assertEqual("skipped", skipped["kind"])
        self.assertIn("starts with 'private'", skipped["reason"])

    def test_include_file_glob_limits_copy_and_protects_filtered_mirror_files(self):
        """Copies matching files while preserving entries outside an Include allowlist."""
        self.plan["pairs"][0].update({
            "mode": "mirror",
            "exclusions": [{
                "effect": "include", "pattern": "*.ma", "match": "glob",
                "applies_to": "files", "ignore_case": True,
            }],
        })
        self.write_file(self.source, "scene.ma", "selected")
        self.write_file(self.source, "notes.txt", "filtered source")
        self.write_file(self.destination, "old.ma", "remove selected extra")
        self.write_file(self.destination, "keep.txt", "filtered destination")

        preview = self.preview()
        self.assertEqual(1, preview["summary"]["copy"])
        self.assertEqual(1, preview["summary"]["delete"])
        self.assertEqual(2, preview["summary"]["skipped"])
        self.assertTrue(any("Include rule" in item["reason"] for item in preview["skipped"]))
        result = engine.execute_preview(preview, allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("selected", (self.destination / "scene.ma").read_text())
        self.assertFalse((self.destination / "old.ma").exists())
        self.assertEqual("filtered destination", (self.destination / "keep.txt").read_text())
        self.assertFalse((self.destination / "notes.txt").exists())

    def test_include_folder_rule_keeps_matching_folder_contents(self):
        """Scans selected folders and filters unmatched folder subtrees from the plan."""
        self.plan["pairs"][0]["exclusions"] = [{
            "effect": "include", "pattern": "Wanted", "match": "exact",
            "applies_to": "folders", "ignore_case": False,
        }]
        self.write_file(self.source, "Wanted/scene.ma", "selected")
        self.write_file(self.source, "Other/notes.txt", "filtered")
        preview = self.preview()
        self.assertEqual(1, preview["summary"]["copy"])
        self.assertTrue(any(item["relative_path"] == "Other" for item in preview["skipped"]))
        result = engine.execute_preview(preview)
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("selected", (self.destination / "Wanted/scene.ma").read_text())
        self.assertFalse((self.destination / "Other").exists())

    def test_file_globs_protect_matching_destination_files(self):
        """File globs match basenames and relative paths and prevent pruning."""
        self.plan["pairs"][0].update({"mode": "mirror", "ignore_files": ["*.tmp", "cache/*.bin"]})
        self.write_file(self.source, "keep.txt", "keep")
        self.write_file(self.source, "skip.tmp", "ignore")
        self.write_file(self.destination, "old.tmp", "keep excluded")
        self.write_file(self.destination, "cache/old.bin", "keep excluded")
        result = self.sync(allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertFalse((self.destination / "skip.tmp").exists())
        self.assertTrue((self.destination / "old.tmp").exists())
        self.assertTrue((self.destination / "cache/old.bin").exists())

    def test_timestamp_tolerance_can_skip_filesystem_rounding_differences(self):
        """A configurable tolerance accepts modest timestamp rounding."""
        timestamp = 1_700_000_000_000_000_000
        self.write_file(self.source, "item.txt", "same", timestamp)
        self.write_file(self.destination, "item.txt", "same", timestamp + 1_000_000_000)
        self.assertEqual(1, self.preview()["summary"]["update"])
        self.plan["timestamp_tolerance"] = 2.0
        self.assertEqual(1, self.preview()["summary"]["unchanged"])

    def test_content_comparison_detects_same_size_same_time_content_changes(self):
        """Content comparison catches differences metadata comparison cannot see."""
        timestamp = 1_700_000_000_000_000_000
        self.write_file(self.source, "item.txt", "AAAA", timestamp)
        self.write_file(self.destination, "item.txt", "BBBB", timestamp)
        self.assertEqual(1, self.preview()["summary"]["unchanged"])
        self.plan["compare"] = "content"
        self.assertEqual(1, self.preview()["summary"]["update"])

    def test_content_comparison_skips_equal_data_with_different_timestamps(self):
        """Identical content is skipped even when timestamps differ."""
        timestamp = 1_700_000_000_000_000_000
        self.write_file(self.source, "item.txt", "same", timestamp)
        self.write_file(self.destination, "item.txt", "same", timestamp + 10_000_000_000)
        self.plan["compare"] = "content"
        self.assertEqual(1, self.preview()["summary"]["unchanged"])

    def test_missing_source_rejects_preview(self):
        """Missing inputs never produce a preview that can erase a destination."""
        self.source.rmdir()
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_unreadable_directory_rejects_entire_preview(self):
        """A directory enumeration error is raised instead of treated as empty."""
        with mock.patch.object(engine.os, "scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(engine.SyncError):
                self.preview()

    def test_source_destination_overlap_rejects_preview(self):
        """Nested targets cannot write into a source or delete source data."""
        self.plan["pairs"][0]["destination"] = str(self.source / "nested")
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_cross_pair_overlap_rejects_preview(self):
        """One pair cannot write into another pair's source."""
        second_source = self.root / "second_source"
        second_source.mkdir()
        second_pair = copy.deepcopy(self.plan["pairs"][0])
        second_pair.update({
            "id": "pair-two", "source": str(second_source), "destination": str(self.source / "nested"),
        })
        self.plan["pairs"].append(second_pair)
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_overlapping_destination_pairs_reject_preview(self):
        """Independent pairs cannot manage overlapping destination scopes."""
        second_pair = copy.deepcopy(self.plan["pairs"][0])
        second_pair.update({"id": "pair-two", "destination": str(self.destination / "nested")})
        self.plan["pairs"].append(second_pair)
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_filesystem_root_destination_is_rejected(self):
        """The filesystem root cannot be used as a destination."""
        self.plan["pairs"][0]["destination"] = self.destination.anchor
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_relative_or_blank_paths_are_rejected(self):
        """Unconfigured paths never silently resolve against the working folder."""
        for invalid_path in ("", "relative-folder"):
            with self.subTest(path=invalid_path):
                self.plan["pairs"][0]["destination"] = invalid_path
                with self.assertRaises(engine.SyncError):
                    self.preview()

    def test_file_folder_collisions_require_manual_resolution(self):
        """Colliding types never cause a delete-before-copy replacement."""
        self.write_file(self.source, "folder/item.txt", "source")
        self.write_file(self.destination, "folder", "valuable file")
        with self.assertRaises(engine.SyncError):
            self.preview()
        self.assertEqual("valuable file", (self.destination / "folder").read_text())

    def test_duplicate_normalized_names_fail_instead_of_collapsing_entries(self):
        """Case collisions cannot silently hide a source file or directory."""
        first_file = self.write_file(self.source, "first.txt", "first")
        second_file = self.write_file(self.source, "second.txt", "second")
        second_directory = self.source / "folder"
        second_directory.mkdir()
        for second_path in (second_file, second_directory):
            with self.subTest(second_path=second_path.name):
                entries = [mock.Mock(name="first-entry"), mock.Mock(name="second-entry")]
                entries[0].name, entries[0].path = "A.txt", str(first_file)
                entries[1].name, entries[1].path = "a.txt", str(second_path)
                iterator = mock.MagicMock()
                iterator.__enter__.return_value = iter(entries)
                with mock.patch.object(engine.os, "scandir", return_value=iterator):
                    with mock.patch.object(engine, "_relative_key", side_effect=str.casefold):
                        with self.assertRaisesRegex(engine.SyncError, "same path"):
                            self.preview()

    def test_changed_source_after_preview_prevents_all_writes(self):
        """Stale source previews require comparison again before execution."""
        source_file = self.write_file(self.source, "item.txt", "initial")
        preview = self.preview()
        source_file.write_text("changed after preview")
        result = engine.execute_preview(preview)
        self.assertEqual("failed", result["status"])
        self.assertFalse((self.destination / "item.txt").exists())

    def test_changed_destination_after_preview_is_preserved(self):
        """Destination edits made after comparison cannot be overwritten."""
        self.write_file(self.source, "item.txt", "source replacement")
        destination_file = self.write_file(self.destination, "item.txt", "old")
        preview = self.preview()
        destination_file.write_text("new user edits")
        result = engine.execute_preview(preview)
        self.assertEqual("failed", result["status"])
        self.assertEqual("new user edits", destination_file.read_text())

    def test_new_destination_entry_after_preview_is_preserved(self):
        """New destination entries invalidate the original preview."""
        self.write_file(self.source, "item.txt", "source")
        preview = self.preview()
        self.write_file(self.destination, "newly-created.txt", "keep")
        result = engine.execute_preview(preview)
        self.assertEqual("failed", result["status"])
        self.assertFalse((self.destination / "item.txt").exists())

    def test_cancelled_preview_raises_sync_cancelled(self):
        """An already requested cancellation interrupts a read-only preview."""
        cancel_event = threading.Event()
        cancel_event.set()
        with self.assertRaises(engine.SyncCancelled):
            engine.build_preview(self.plan, cancel_event=cancel_event)

    def test_cancel_during_copy_preserves_original_and_cleans_temporary_file(self):
        """Chunk cancellation leaves no partial replacement or temporary sibling."""
        self.write_file(self.source, "item.bin", b"X" * (engine.CHUNK_SIZE * 3))
        destination_file = self.write_file(self.destination, "item.bin", b"old")
        cancel_event = threading.Event()
        progress_events = []

        def cancel_after_chunk(progress):
            """Cancels as soon as a chunk has been reported.

            Args:
                progress (dict): Engine progress information.
            """
            progress_events.append(progress)
            if progress.get("current_file_bytes", 0):
                cancel_event.set()

        result = engine.execute_preview(self.preview(), cancel_event, cancel_after_chunk)
        self.assertEqual("cancelled", result["status"])
        self.assertEqual(b"old", destination_file.read_bytes())
        self.assertEqual([], list(self.destination.glob(".sync_engine-*")))
        self.assertEqual(0, result["copied"])
        self.assertTrue(any(progress.get("current_file_total") for progress in progress_events))

    def test_source_changes_during_copy_preserve_destination(self):
        """Mid-copy source mutation prevents committing an inconsistent file."""
        source_file = self.write_file(self.source, "item.bin", b"X" * (engine.CHUNK_SIZE * 3))
        destination_file = self.write_file(self.destination, "item.bin", b"original")
        mutated = []

        def mutate_source(progress):
            """Changes the source after its first copied chunk.

            Args:
                progress (dict): Engine progress information.
            """
            if progress.get("current_file_bytes", 0) and not mutated:
                mutated.append(True)
                with source_file.open("ab") as stream:
                    stream.write(b"new contents")

        result = engine.execute_preview(self.preview(), progress_callback=mutate_source)
        self.assertEqual("failed", result["status"])
        self.assertEqual(b"original", destination_file.read_bytes())
        self.assertEqual([], list(self.destination.glob(".sync_engine-*")))

    def test_destination_changes_during_copy_preserve_new_edit(self):
        """A destination edit made while copying is checked before atomic replace."""
        self.write_file(self.source, "item.bin", b"X" * (engine.CHUNK_SIZE * 3))
        destination_file = self.write_file(self.destination, "item.bin", b"original")
        mutated = []

        def mutate_destination(progress):
            """Edits the destination during a pending temporary copy.

            Args:
                progress (dict): Engine progress information.
            """
            if progress.get("current_file_bytes", 0) and not mutated:
                mutated.append(True)
                destination_file.write_bytes(b"user edit")

        result = engine.execute_preview(self.preview(), progress_callback=mutate_destination)
        self.assertEqual("failed", result["status"])
        self.assertEqual(b"user edit", destination_file.read_bytes())

    def test_copy_failure_never_runs_mirror_removals(self):
        """A failed atomic replacement leaves mirror extras untouched."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "item.txt", "new")
        self.write_file(self.destination, "extra.txt", "keep on failure")
        with mock.patch.object(engine.os, "replace", side_effect=PermissionError("locked")):
            result = self.sync(allow_delete=True)
        self.assertEqual("failed", result["status"])
        self.assertTrue((self.destination / "extra.txt").exists())
        self.assertEqual([], list(self.destination.glob(".sync_engine-*")))

    def test_failure_in_later_pair_prevents_earlier_pair_mirror_removal(self):
        """Every pair's copies finish before any pair's mirror files are removed."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "first.txt", "copy first")
        self.write_file(self.destination, "extra.txt", "keep on later failure")
        second_source = self.root / "second_source"
        second_destination = self.root / "second_destination"
        second_source.mkdir()
        second_destination.mkdir()
        self.write_file(second_source, "second.txt", "fails")
        second_pair = copy.deepcopy(self.plan["pairs"][0])
        second_pair.update({"id": "pair-two", "source": str(second_source),
                            "destination": str(second_destination), "mode": "update"})
        self.plan["pairs"].append(second_pair)
        original_replace = os.replace

        def fail_second_copy(source_path, destination_path):
            """Fails only the second pair's atomic replacement.

            Args:
                source_path (str): Temporary copied file.
                destination_path (str): Intended output filename.
            """
            if Path(destination_path).name == "second.txt":
                raise PermissionError("locked second output")
            original_replace(source_path, destination_path)

        with mock.patch.object(engine.os, "replace", side_effect=fail_second_copy):
            result = self.sync(allow_delete=True)
        self.assertEqual("failed", result["status"])
        self.assertTrue((self.destination / "first.txt").exists())
        self.assertTrue((self.destination / "extra.txt").exists())

    def test_new_destination_file_during_copy_blocks_mirror_removal(self):
        """A scan after copying catches additions before mirror pruning."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "new.txt", "new")
        self.write_file(self.destination, "extra.txt", "keep")
        mutated = []

        def add_destination_file(progress):
            """Adds a destination file after the planned copy commits.

            Args:
                progress (dict): Engine progress information.
            """
            if progress.get("completed_operations", 0) and not mutated:
                mutated.append(True)
                self.write_file(self.destination, "created-during-sync.txt", "keep")

        result = engine.execute_preview(self.preview(), progress_callback=add_destination_file, allow_delete=True)
        self.assertEqual("failed", result["status"])
        self.assertTrue((self.destination / "extra.txt").exists())
        self.assertTrue((self.destination / "created-during-sync.txt").exists())

    def test_new_source_file_after_final_scan_blocks_its_destination_deletion(self):
        """Source counterparts are checked immediately before deleting extras."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.write_file(self.source, "keep.txt", "same", 1_700_000_000_000_000_000)
        self.write_file(self.destination, "keep.txt", "same", 1_700_000_000_000_000_000)
        self.write_file(self.destination, "extra.txt", "keep")
        extra_scans = []

        def add_source_file_on_final_scan(progress):
            """Adds a source counterpart during the last destination enumeration.

            Args:
                progress (dict): Engine progress information.
            """
            if progress.get("phase") == "scan" and progress.get("current_path") == str(self.destination / "extra.txt"):
                extra_scans.append(True)
                if len(extra_scans) == 2:
                    self.write_file(self.source, "extra.txt", "new source counterpart")

        result = engine.execute_preview(
            self.preview(), progress_callback=add_source_file_on_final_scan, allow_delete=True
        )
        self.assertEqual("failed", result["status"])
        self.assertTrue((self.destination / "extra.txt").exists())

    def test_linked_source_subtree_protects_destination(self):
        """Skipped source links never imply destination removal in mirror mode."""
        self.plan["pairs"][0]["mode"] = "mirror"
        outside = self.root / "outside"
        outside.mkdir()
        self.write_file(outside, "outside.txt", "never copied")
        self.write_file(self.source, "keep.txt", "keep")
        self.write_file(self.destination, "linked/preserved.txt", "preserve")
        self.create_directory_link(outside, self.source / "linked")
        result = self.sync(allow_delete=True)
        self.assertEqual("succeeded", result["status"])
        self.assertTrue((self.destination / "linked/preserved.txt").exists())
        self.assertFalse((self.destination / "linked/outside.txt").exists())

    def test_linked_destination_ancestor_is_rejected(self):
        """A redirected target ancestor cannot escape the intended destination."""
        outside = self.root / "outside"
        outside.mkdir()
        linked = self.root / "linked"
        self.create_directory_link(outside, linked)
        self.plan["pairs"][0]["destination"] = str(linked / "nested")
        with self.assertRaises(engine.SyncError):
            self.preview()

    def test_modified_preview_cannot_escape_destination(self):
        """Execution rejects operation paths changed outside the configured pair."""
        self.write_file(self.source, "item.txt", "source")
        preview = self.preview()
        preview["operations"][0]["destination"] = str(self.root / "outside.txt")
        result = engine.execute_preview(preview)
        self.assertEqual("failed", result["status"])
        self.assertFalse((self.root / "outside.txt").exists())

    def test_repeated_execution_requires_new_preview(self):
        """Already executed previews cannot be replayed over a changed destination."""
        self.write_file(self.source, "item.txt", "source")
        preview = self.preview()
        self.assertEqual("succeeded", engine.execute_preview(preview)["status"])
        self.assertEqual("failed", engine.execute_preview(preview)["status"])


if __name__ == "__main__":
    unittest.main()
