import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
LEGACY_PATHS = (
    REPO_ROOT / "resources" / "field_meta" / "code_map_overrides.json",
    REPO_ROOT / "resources" / "lexicon" / "metrics.yaml",
    REPO_ROOT / "resources" / "lexicon" / "result_words.yaml",
    REPO_ROOT / "resources" / "lexicon" / "semantics.yaml",
    REPO_ROOT / "resources" / "lexicon" / "trigger_words.yaml",
    REPO_ROOT / "resources" / "query_capabilities.yaml",
    REPO_ROOT / "resources" / "query_identifiers.yaml",
    REPO_ROOT / "resources" / "repair_policy.yaml",
    REPO_ROOT / "resources" / "slots" / "predicate_slots.yaml",
    REPO_ROOT / "resources" / "sql_templates" / "_index.yaml",
)
LEGACY_REFERENCES = (
    "lexicon/metrics.yaml",
    "lexicon/result_words.yaml",
    "lexicon/semantics.yaml",
    "lexicon/trigger_words.yaml",
    "field_meta/code_map_overrides.json",
    "query_capabilities.yaml",
    "query_identifiers.yaml",
    "slots/predicate_slots.yaml",
    "sql_templates/_index.yaml",
)


class CapabilityConfigMigrationTests(unittest.TestCase):
    def test_legacy_resource_files_are_deleted(self):
        for path in LEGACY_PATHS:
            self.assertFalse(path.exists(), str(path))

    def test_legacy_resource_paths_are_not_referenced(self):
        app_dir = REPO_ROOT / "backend" / "app"
        offenders = []
        for path in app_dir.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for legacy in LEGACY_REFERENCES:
                if legacy in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {legacy}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
