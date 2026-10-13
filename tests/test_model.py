"""Regression tests for plan data safety, authorization, and startup dispatch."""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
import sync_engine_model as model
import sync_engine_startup as startup


class TestPlanModel(unittest.TestCase):
    """Tests pure data validation and atomic persistence without Qt or Maya."""

    def setUp(self):
        """Creates isolated storage and a fresh editable plan."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.plan = model.new_plan("Test plan")

    def test_blank_paths_remain_empty(self):
        """Ensures unused paths never silently resolve to the working directory."""
        path = self.directory / "editable.syncplan"
        model.save_plan(self.plan, path)
        loaded = model.load_plan(path)
        self.assertEqual("", loaded["pairs"][0]["source"])
        self.assertEqual("", loaded["pairs"][0]["destination"])

    def test_new_plan_and_folder_pair_use_title_case_defaults(self):
        """Uses the requested names for newly created persistent entries."""
        plan = model.new_plan()
        self.assertEqual("Untitled", plan["name"])
        self.assertEqual("Folder Pair", plan["pairs"][0]["name"])

    def test_automation_start_mode_is_validated_and_migrated(self):
        """Accepts the three startup modes and defaults older plans to countdown."""
        self.assertEqual("countdown", model.validate_plan(self.plan)["automation"]["start_mode"])
        for start_mode in ("immediate", "countdown", "confirm"):
            candidate = copy.deepcopy(self.plan)
            candidate["automation"]["start_mode"] = start_mode
            self.assertEqual(start_mode, model.validate_plan(candidate)["automation"]["start_mode"])
        invalid = copy.deepcopy(self.plan)
        invalid["automation"]["start_mode"] = "sometimes"
        with self.assertRaisesRegex(ValueError, "start mode"):
            model.validate_plan(invalid)

    def test_automation_close_after_run_defaults_off_and_round_trips(self):
        """Defaults to keeping the final result visible and validates the close option."""
        validated = model.validate_plan(self.plan)
        self.assertFalse(validated["automation"]["close_after_run"])
        candidate = copy.deepcopy(self.plan)
        candidate["automation"]["close_after_run"] = True
        self.assertTrue(model.validate_plan(candidate)["automation"]["close_after_run"])

    def test_validate_returns_independent_data(self):
        """Ensures edits to validated data cannot alter the original plan."""
        self.plan["pairs"][0]["exclusions"] = [{
            "pattern": "skip", "match": "starts_with", "applies_to": "folders",
        }]
        validated = model.validate_plan(self.plan)
        validated["pairs"][0]["exclusions"].append({
            "pattern": "other", "match": "exact", "applies_to": "files",
        })
        validated["automation"]["countdown_seconds"] = 0
        self.assertEqual(1, len(self.plan["pairs"][0]["exclusions"]))
        self.assertEqual(10, self.plan["automation"]["countdown_seconds"])

    def test_duplicate_refreshes_all_identifiers(self):
        """Ensures duplicated plans and pairs have independent stable identities."""
        self.plan["pairs"].append(model.new_pair("Other pair"))
        duplicate = model.duplicate_plan(self.plan)
        original_ids = {self.plan["id"]} | {pair["id"] for pair in self.plan["pairs"]}
        duplicate_ids = {duplicate["id"]} | {pair["id"] for pair in duplicate["pairs"]}
        self.assertEqual(set(), original_ids.intersection(duplicate_ids))
        self.assertEqual("Test plan copy", duplicate["name"])

    def test_duplicate_pair_identifiers_rejected(self):
        """Rejects duplicate pair identities that make editing ambiguous."""
        self.plan["pairs"].append(copy.deepcopy(self.plan["pairs"][0]))
        with self.assertRaisesRegex(ValueError, "unique"):
            model.validate_plan(self.plan)

    def test_pair_cannot_use_plan_identifier(self):
        """Rejects collisions between plan and pair identities."""
        self.plan["pairs"][0]["id"] = self.plan["id"]
        with self.assertRaisesRegex(ValueError, "unique"):
            model.validate_plan(self.plan)

    def test_invalid_types_and_options_rejected(self):
        """Rejects unsafe coercions and malformed enum or numeric options."""
        cases = (
            ("compare", []), ("compare", "timestamp"), ("pairs", {}),
            ("timestamp_tolerance", -1), ("timestamp_tolerance", float("nan")),
            ("timestamp_tolerance", 10 ** 1000),
            ("timestamp_tolerance", 3601),
            ("timestamp_tolerance", True), ("allow_empty_mirror", "false"),
            ("schema_version", True), ("schema_version", 99),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = copy.deepcopy(self.plan)
                invalid[field] = value
                with self.assertRaises(ValueError):
                    model.validate_plan(invalid)

    def test_invalid_automation_rejected(self):
        """Rejects malformed countdowns and permission values."""
        cases = (
            ("countdown_seconds", -1), ("countdown_seconds", 1.5),
            ("countdown_seconds", True), ("countdown_seconds", 86401),
            ("start_minimized", "yes"), ("allow_deletions", 1),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = copy.deepcopy(self.plan)
                invalid["automation"][field] = value
                with self.assertRaises(ValueError):
                    model.validate_plan(invalid)

    def test_invalid_pair_options_rejected(self):
        """Rejects malformed pair mode, exclusion patterns, and paths."""
        cases = (
            ("mode", "unknown"),
            ("exclusions", [{"pattern": "x", "match": "unknown", "applies_to": "both"}]),
            ("exclusions", [{"pattern": "x", "match": "contains", "applies_to": "both", "ignore_case": "yes"}]),
            ("source", None), ("destination", "bad\x00path"),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                invalid = copy.deepcopy(self.plan)
                invalid["pairs"][0][field] = value
                with self.assertRaises(ValueError):
                    model.validate_plan(invalid)

    def test_relative_paths_resolve_against_plan(self):
        """Keeps imported relative paths independent of the process directory."""
        path = self.directory / "project" / "test.syncplan"
        self.plan["pairs"][0]["source"] = "source"
        self.plan["pairs"][0]["destination"] = "../backup"
        model.save_plan(self.plan, path)
        loaded = model.load_plan(path)
        self.assertEqual(str(path.parent / "source"), loaded["pairs"][0]["source"])
        self.assertEqual(str(self.directory / "backup"), loaded["pairs"][0]["destination"])

    def test_plan_variables_expand_without_replacing_saved_templates(self):
        """Expands nested and sync-file variables only in a runtime plan copy."""
        path = self.directory / "plans" / "test.syncplan"
        root = self.directory / "backup-root"
        self.plan["variables"] = {"backup_root": str(root), "backup": "{backup_root}/daily"}
        self.plan["pairs"][0]["source"] = "{backup}/source"
        self.plan["pairs"][0]["destination"] = "{sync-file-dir}/backup"
        model.save_plan(self.plan, path)

        loaded = model.load_plan(path)
        resolved = model.resolve_plan_paths(loaded, path)

        self.assertEqual("{backup}/source", loaded["pairs"][0]["source"])
        self.assertEqual("{backup}/source", self.plan["pairs"][0]["source"])
        self.assertEqual(os.path.abspath(root / "daily" / "source"), resolved["pairs"][0]["source"])
        self.assertEqual(os.path.abspath(path.parent / "backup"), resolved["pairs"][0]["destination"])

    def test_unknown_and_cyclic_plan_variables_are_rejected(self):
        """Fails clearly when a path references an unknown or recursive token."""
        self.plan["pairs"][0]["source"] = "{sync_missing_variable}/source"
        with self.assertRaisesRegex(ValueError, "Unknown environment variable"):
            model.resolve_plan_paths(self.plan, self.directory / "plan.syncplan")

        self.plan["variables"] = {"first": "{second}", "second": "{first}"}
        self.plan["pairs"][0]["source"] = "{first}/source"
        with self.assertRaisesRegex(ValueError, "cycle"):
            model.resolve_plan_paths(self.plan, self.directory / "plan.syncplan")

    def test_custom_variable_names_cannot_replace_built_ins(self):
        """Protects built-in names and requires useful custom variable keys."""
        self.plan["variables"] = {"{plan-dir}": "somewhere"}
        with self.assertRaisesRegex(ValueError, "reserved"):
            model.validate_plan(self.plan)
        self.plan["variables"] = {"not a variable": "somewhere"}
        with self.assertRaisesRegex(ValueError, "valid environment variable name"):
            model.validate_plan(self.plan)

    def test_query_variables_resolve_with_restricted_environment_helpers(self):
        """Evaluates supported path queries and exposes them to the path browser."""
        plan_path = self.directory / "plans" / "query.syncplan"
        self.plan["variables"] = {
            "{backup-root}": {
                "value": 'join(env["home-dir"], "Sync Backups")', "query": True,
            },
            "{daily-root}": '{backup-root}/daily',
        }
        self.plan["pairs"][0]["source"] = "{daily-root}"

        resolved = model.resolve_plan_paths(self.plan, plan_path)
        expected = os.path.abspath(Path.home() / "Sync Backups" / "daily")

        self.assertEqual(expected, resolved["pairs"][0]["source"])
        self.assertEqual(expected, model.evaluate_plan_variable(self.plan, "{daily-root}", plan_path))
        self.assertIn("{sync-file-dir}", model.get_available_environment_variables(self.plan, plan_path))

    def test_path_template_resolves_using_current_plan_values(self):
        """Expands unsaved custom variables and anchors relative paths to the plan."""
        plan_path = self.directory / "plans" / "current.syncplan"
        self.plan["variables"] = {"{backup-root}": str(self.directory / "backup")}
        expected = os.path.abspath(self.directory / "backup" / "daily")
        self.assertEqual(
            expected,
            model.resolve_path_template(self.plan, "{backup-root}/daily", plan_path),
        )

    def test_path_template_rejects_empty_and_unknown_values(self):
        """Reports missing input and unknown tokens instead of guessing a path."""
        with self.assertRaisesRegex(ValueError, "Enter a path"):
            model.resolve_path_template(self.plan, "", self.directory / "plan.syncplan")
        with self.assertRaisesRegex(ValueError, "Unknown environment variable"):
            model.resolve_path_template(
                self.plan, "{missing-root}/daily", self.directory / "plan.syncplan"
            )

    def test_query_variables_reject_arbitrary_python_calls(self):
        """Prevents custom queries from importing or invoking arbitrary modules."""
        self.plan["variables"] = {"unsafe": {"value": '__import__("os")', "query": True}}
        self.plan["pairs"][0]["source"] = "{unsafe}"
        with self.assertRaisesRegex(ValueError, "Query for.*failed"):
            model.resolve_plan_paths(self.plan, self.directory / "query.syncplan")

    def test_execution_settings_are_validated_and_defaulted(self):
        """Adds wait, live-log, and reporting preferences to plan serialization."""
        validated = model.validate_plan(self.plan)
        self.assertEqual({
            "timeout_seconds": 0,
            "expand_log_while_syncing": False,
            "generate_report": False,
        }, validated["execution"])
        self.plan["execution"] = {"timeout_seconds": -1}
        with self.assertRaisesRegex(ValueError, "sync timeout"):
            model.validate_plan(self.plan)

    def test_preview_view_is_saved_per_plan_and_validated(self):
        """Defaults older plans to Table and persists valid plan-specific views."""
        validated = model.validate_plan(self.plan)
        self.assertEqual("table", validated["preview_view"])
        self.plan["preview_view"] = "tree"
        self.assertEqual("tree", model.validate_plan(self.plan)["preview_view"])
        self.plan["preview_view"] = "unknown"
        with self.assertRaisesRegex(ValueError, "Preview view"):
            model.validate_plan(self.plan)

    @unittest.skipUnless(os.name == "nt", "Windows path semantics")
    def test_drive_relative_path_rejected(self):
        """Rejects paths that depend on an implicit current directory for a drive."""
        self.plan["pairs"][0]["source"] = "C:relative"
        path = self.directory / "test.syncplan"
        model.save_plan(self.plan, path)
        with self.assertRaisesRegex(ValueError, "drive-relative"):
            model.load_plan(path)

    def test_atomic_save_failure_preserves_existing_file(self):
        """Keeps the prior plan and cleans temporary files after replacement fails."""
        path = self.directory / "test.syncplan"
        model.save_plan(self.plan, path)
        original_bytes = path.read_bytes()
        self.plan["name"] = "Changed"
        with mock.patch.object(model.os, "replace", side_effect=OSError("Unavailable target")):
            with self.assertRaises(OSError):
                model.save_plan(self.plan, path)
        self.assertEqual(original_bytes, path.read_bytes())
        self.assertEqual([path], list(self.directory.iterdir()))

    def test_unicode_round_trip(self):
        """Preserves readable Unicode names and exclusion patterns in saved plans."""
        self.plan["name"] = "Caf\u00e9 \u65e5\u672c\u8a9e"
        self.plan["pairs"][0]["exclusions"] = [{
            "effect": "exclude", "pattern": "r\u00e9sum\u00e9*.txt",
            "match": "ends_with", "applies_to": "files",
            "ignore_case": True,
        }]
        path = self.directory / "test.syncplan"
        model.save_plan(self.plan, path)
        self.assertEqual(self.plan, model.load_plan(path))
        self.assertIn("Caf\u00e9 \u65e5\u672c\u8a9e", path.read_text(encoding="utf-8"))

    def test_action_cannot_load_as_editable_plan(self):
        """Prevents confusing executable action envelopes with ordinary plan data."""
        path = self.directory / "test.syncrun"
        model.save_action(self.plan, path)
        with self.assertRaisesRegex(ValueError, "action file"):
            model.load_plan(path)

    def test_nonmirror_action_round_trip(self):
        """Loads a self-contained action without granting mirror permission."""
        path = self.directory / "test.syncrun"
        model.save_action(self.plan, path)
        loaded = model.load_action(path)
        self.assertEqual(self.plan, loaded)
        self.assertEqual(False, loaded["automation"]["allow_deletions"])

    def test_mirror_action_export_requires_permission(self):
        """Refuses action export when deletion has not been explicitly approved."""
        self.plan["pairs"][0]["mode"] = "mirror"
        self.plan["automation"]["allow_deletions"] = True
        path = self.directory / "test.syncrun"
        with self.assertRaisesRegex(ValueError, "explicit"):
            model.save_action(self.plan, path)
        self.assertEqual(False, path.exists())

    def test_authorized_mirror_action(self):
        """Preserves explicit automatic mirror authorization in exported actions."""
        self.plan["pairs"][0]["mode"] = "mirror"
        path = self.directory / "test.syncrun"
        model.save_action(self.plan, path, allow_deletions=True)
        loaded = model.load_action(path)
        self.assertEqual(True, loaded["automation"]["allow_deletions"])
        self.assertEqual(False, self.plan["automation"]["allow_deletions"])

    def test_mirror_action_permission_tampering_rejected(self):
        """Rejects permission disagreement and mirror plans without authorization."""
        self.plan["pairs"][0]["mode"] = "mirror"
        path = self.directory / "test.syncrun"
        model.save_action(self.plan, path, allow_deletions=True)
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["allow_deletions"] = False
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "do not agree"):
            model.load_action(path)
        envelope["plan"]["automation"]["allow_deletions"] = False
        path.write_text(json.dumps(envelope), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no explicit"):
            model.load_action(path)

    def test_invalid_action_schema_rejected(self):
        """Rejects an unsupported action envelope before automatic execution."""
        path = self.directory / "test.syncrun"
        path.write_text(json.dumps({"file_type": "synctasker_action", "schema_version": 99}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "schema"):
            model.load_action(path)

    def test_store_round_trip_and_remove(self):
        """Persists and removes only the requested plan file."""
        store = model.PlanStore(self.directory)
        path = store.save(self.plan)
        self.assertEqual(self.plan, store.load(self.plan["id"]))
        self.assertEqual([{"id": self.plan["id"], "name": "Test plan", "path": str(path)}], store.list_plans())
        store.remove(self.plan["id"])
        self.assertEqual([], store.list_plans())

    def test_store_path_traversal_rejected(self):
        """Prevents arbitrary paths from being used as plan identifiers."""
        store = model.PlanStore(self.directory)
        for operation in (store.load, store.remove):
            with self.assertRaises(ValueError):
                operation("../../important")

    def test_invalid_stored_plan_reported_without_deletion(self):
        """Reports corrupted plans while preserving the file for recovery."""
        store = model.PlanStore(self.directory)
        path = store.plans_directory / "corrupted.syncplan"
        path.write_text("invalid", encoding="utf-8")
        self.assertEqual([], store.list_plans())
        self.assertEqual(1, len(store.warnings))
        self.assertEqual(True, path.exists())

    def test_preferences_round_trip_and_corruption(self):
        """Preserves preference values and reports malformed files safely."""
        store = model.PlanStore(self.directory)
        self.assertEqual({}, store.read_preferences())
        store.write_preferences({"recent_plan": self.plan["id"], "window_size": [1200, 800]})
        self.assertEqual(self.plan["id"], store.read_preferences()["recent_plan"])
        store.preferences_path.write_text("[]", encoding="utf-8")
        self.assertEqual({}, store.read_preferences())
        self.assertEqual(1, len(store.warnings))

    def test_invalid_preferences_preserve_saved_file(self):
        """Rejects unserializable preferences before replacing previous data."""
        store = model.PlanStore(self.directory)
        store.write_preferences({"window_size": [1200, 800]})
        with self.assertRaises(ValueError):
            store.write_preferences({"invalid": float("nan")})
        self.assertEqual({"window_size": [1200, 800]}, store.read_preferences())

    def test_format_size(self):
        """Shows compact byte sizes without losing common unit distinctions."""
        self.assertEqual("0 B", model.format_size(0))
        self.assertEqual("1023 B", model.format_size(1023))
        self.assertEqual("1.0 KiB", model.format_size(1024))
        self.assertEqual("1.5 MiB", model.format_size(1572864))
        with self.assertRaises(ValueError):
            model.format_size(-1)


class TestStartup(unittest.TestCase):
    """Tests startup dispatch without opening a GUI or modifying file associations."""

    def test_headless_does_not_require_qt(self):
        """Runs pure CLI arguments unchanged without attempting a Qt probe."""
        arguments = ["--run", "relative folder/test.syncrun", "--headless", "--countdown", "0"]
        with mock.patch.object(startup, "_candidate_interpreters", return_value=["python"]):
            with mock.patch.object(startup, "_supports_qt") as probe:
                with mock.patch.object(startup.subprocess, "call", return_value=3) as launch:
                    self.assertEqual(3, startup.startup_main(arguments))
        self.assertEqual(0, probe.call_count)
        self.assertEqual(arguments, launch.call_args.args[0][3:])
        self.assertEqual(False, "cwd" in launch.call_args.kwargs)

    def test_maya_child_ignores_user_site(self):
        """Isolates Maya's bundled PySide from incompatible user-installed libraries."""
        environment = startup._startup_environment("mayapy.exe")
        self.assertEqual("1", environment["PYTHONNOUSERSITE"])
        self.assertEqual("utf-8", environment["PYTHONIOENCODING"])

    def test_preview_is_pure_command(self):
        """Allows command-line preview when no Qt runtime is installed."""
        with mock.patch.object(startup, "_candidate_interpreters", return_value=["python"]):
            with mock.patch.object(startup, "_supports_qt") as probe:
                with mock.patch.object(startup.subprocess, "call", return_value=0):
                    self.assertEqual(0, startup.startup_main(["--preview", "test.syncplan"]))
        self.assertEqual(0, probe.call_count)


if __name__ == "__main__":
    unittest.main()
