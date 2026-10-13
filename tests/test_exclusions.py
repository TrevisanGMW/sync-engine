"""Tests exclusion matching options used by the plan editor."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sync_engine"))
import sync_engine_engine as engine
import sync_engine_model as model


class TestExclusions(unittest.TestCase):
    """Covers literal contains rules and per-rule case handling."""

    def test_contains_defaults_to_ignoring_case(self):
        """Keeps existing case-insensitive behavior for newly saved rules."""
        pair = {"exclusions": [{"pattern": "cache", "match": "contains", "applies_to": "files"}]}
        pair["exclusions"] = model.normalize_exclusions(pair)
        rule = engine._matching_exclusion("Image_CACHE.tmp", "Image_CACHE.tmp", False, pair)
        self.assertEqual("cache", rule["pattern"])
        self.assertTrue(rule["ignore_case"])

    def test_contains_can_be_case_sensitive(self):
        """Matches the requested letter case when Ignore case is unchecked."""
        pair = {"exclusions": [{
            "pattern": "Cache", "match": "contains", "applies_to": "files", "ignore_case": False,
        }]}
        pair["exclusions"] = model.normalize_exclusions(pair)
        self.assertIsNotNone(engine._matching_exclusion("myCache.tmp", "myCache.tmp", False, pair))
        self.assertIsNone(engine._matching_exclusion("mycache.tmp", "mycache.tmp", False, pair))

    def test_ignore_case_requires_a_boolean(self):
        """Rejects malformed serialized case-sensitivity values."""
        pair = {"exclusions": [{
            "pattern": "cache", "match": "contains", "applies_to": "files", "ignore_case": "yes",
        }]}
        with self.assertRaises(ValueError):
            model.normalize_exclusions(pair)

    def test_include_rules_round_trip_and_legacy_rules_default_to_exclude(self):
        """Persists Include explicitly while retaining historical exclusion behavior."""
        legacy = model.normalize_exclusions({
            "exclusions": [{"pattern": "*.ma", "match": "glob", "applies_to": "files"}]
        })
        included = model.normalize_exclusions({
            "exclusions": [{
                "effect": "include", "pattern": "*.ma", "match": "glob", "applies_to": "files",
            }]
        })
        self.assertEqual("exclude", legacy[0]["effect"])
        self.assertEqual("include", included[0]["effect"])


if __name__ == "__main__":
    unittest.main()
