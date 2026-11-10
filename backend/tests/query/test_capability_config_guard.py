import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "backend" / "app"
RESOURCE_ROOT = REPO_ROOT / "resources"
CAPABILITY_ROOT = RESOURCE_ROOT / "capabilities"


class CapabilityConfigGuardTests(unittest.TestCase):
    def test_mdl_compilation_config_is_manifested(self):
        index = yaml.safe_load(
            (CAPABILITY_ROOT / "index.yaml").read_text(encoding="utf-8")
        )

        self.assertIn("mdl_compilation.yaml", index["files"])
        self.assertTrue((CAPABILITY_ROOT / "mdl_compilation.yaml").is_file())

    def test_subject_to_mdl_mapping_is_not_duplicated_in_python(self):
        forbidden_symbols = (
            "_SUBJECT_MODELS",
            "_SUBJECT_MODEL_BY_NAME",
            "_SUBJECT_MDL_MODEL",
        )
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for symbol in forbidden_symbols:
                if symbol in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {symbol}")
        self.assertEqual(offenders, [])

    def test_no_legacy_business_mapping_symbols_remain(self):
        forbidden = (
            "_DAMAGE_ALIASES",
            "expand_problem_types",
            "expand_problem_type_filters",
            "_CODE_FIELDS",
            "_DISPLAY_STORAGE_PAIRS",
        )
        offenders = []
        for path in APP_ROOT.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for symbol in forbidden:
                if symbol in text:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {symbol}")
        self.assertEqual(offenders, [])

    def test_no_business_mapping_yaml_outside_capability_directory(self):
        offenders = []
        for path in RESOURCE_ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                continue
            if CAPABILITY_ROOT in path.parents:
                continue
            name = path.name
            if any(
                token in name
                for token in (
                    "lexicon",
                    "metrics",
                    "semantics",
                    "trigger",
                    "capabilities",
                    "identifiers",
                    "repair_policy",
                    "predicate",
                )
            ):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
