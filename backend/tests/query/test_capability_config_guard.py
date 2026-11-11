import re
import unittest
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "backend" / "app"
RESOURCE_ROOT = REPO_ROOT / "resources"
CAPABILITY_ROOT = RESOURCE_ROOT / "capabilities"


class CapabilityConfigGuardTests(unittest.TestCase):
    def test_capability_file_responsibilities_match_manifest_and_disk(self):
        index = yaml.safe_load(
            (CAPABILITY_ROOT / "index.yaml").read_text(encoding="utf-8")
        )
        manifest = [
            str(item)
            for item in index.get("files") or []
        ]
        on_disk = sorted(
            path.name
            for path in CAPABILITY_ROOT.glob("*.yaml")
            if path.name != "index.yaml"
        )
        readme = (RESOURCE_ROOT / "README.md").read_text(encoding="utf-8")
        documented = re.findall(
            r"^\|\s*`([^`]+\.ya?ml)`\s*\|",
            readme,
            flags=re.MULTILINE,
        )
        documented = [
            name for name in documented
            if name != "index.yaml"
        ]

        self.assertEqual(len(manifest), len(set(manifest)))
        self.assertEqual(len(documented), len(set(documented)))
        self.assertEqual(set(manifest), set(on_disk))
        self.assertEqual(set(documented), set(on_disk))

    def test_subjects_do_not_duplicate_mdl_relationship_topology(self):
        subjects = yaml.safe_load(
            (CAPABILITY_ROOT / "subjects.yaml").read_text(encoding="utf-8")
        )["subjects"]

        offenders = [
            subject
            for subject, capability in subjects.items()
            if isinstance(capability, dict) and "joins" in capability
        ]
        self.assertEqual(offenders, [])

    def test_capability_join_references_resolve_to_mdl(self):
        mdl_root = REPO_ROOT / "wren_projects" / "road_mysql"
        relationships = yaml.safe_load(
            (mdl_root / "relationships.yml").read_text(encoding="utf-8")
        )["relationships"]
        mdl_names = {
            str(item["name"])
            for item in relationships
        }
        mdl_names.update(
            path.name
            for path in (mdl_root / "models").iterdir()
            if path.is_dir()
        )
        offenders = []
        for file_name in ("fields.yaml", "metrics.yaml"):
            document = yaml.safe_load(
                (CAPABILITY_ROOT / file_name).read_text(encoding="utf-8")
            )
            for subject, capabilities in document[
                file_name.removesuffix(".yaml")
            ].items():
                if not isinstance(capabilities, dict):
                    continue
                for capability_id, capability in capabilities.items():
                    if not isinstance(capability, dict):
                        continue
                    for reference in capability.get("requires_join") or ():
                        if str(reference) not in mdl_names:
                            offenders.append(
                                f"{file_name}:{subject}.{capability_id}"
                                f" -> {reference}"
                            )
        self.assertEqual(offenders, [])

    def test_manual_enum_maps_exist_only_in_enums_yaml(self):
        overrides = yaml.safe_load(
            (CAPABILITY_ROOT / "field_overrides.yaml").read_text(
                encoding="utf-8"
            )
        )["field_overrides"]

        offenders = []
        for kind, raw in overrides.items():
            if not isinstance(raw, dict):
                continue
            raw_columns = raw.get("columns")
            if not isinstance(raw_columns, dict):
                continue
            for table, columns in raw_columns.items():
                if not isinstance(columns, dict):
                    continue
                for column, override in columns.items():
                    if isinstance(override, dict) and "code_map" in override:
                        offenders.append(f"{kind}.{table}.{column}")
        self.assertEqual(offenders, [])

    def test_enum_labels_and_display_cases_are_not_duplicated(self):
        enums = yaml.safe_load(
            (CAPABILITY_ROOT / "enums.yaml").read_text(encoding="utf-8")
        )["enums"]
        fields = yaml.safe_load(
            (CAPABILITY_ROOT / "fields.yaml").read_text(encoding="utf-8")
        )["fields"]
        overrides = yaml.safe_load(
            (CAPABILITY_ROOT / "field_overrides.yaml").read_text(
                encoding="utf-8"
            )
        )["field_overrides"]
        enum_columns = {
            (str(raw.get("table") or ""), str(raw.get("column") or ""))
            for raw in enums.values()
            if isinstance(raw, dict)
        }

        field_offenders = []
        for subject, subject_fields in fields.items():
            for field_id, field in subject_fields.items():
                if not isinstance(field, dict) or not field.get("enum_key"):
                    continue
                if field.get("label"):
                    field_offenders.append(
                        f"fields.{subject}.{field_id}.label"
                    )
                if "CASE " in str(field.get("expression") or "").upper():
                    field_offenders.append(
                        f"fields.{subject}.{field_id}.expression"
                    )

        override_offenders = []
        for kind, raw in overrides.items():
            if not isinstance(raw, dict):
                continue
            raw_columns = raw.get("columns") or {}
            if not isinstance(raw_columns, dict):
                continue
            for table, columns in raw_columns.items():
                if not isinstance(columns, dict):
                    continue
                for column, override in columns.items():
                    if (
                        (str(table), str(column)) in enum_columns
                        and isinstance(override, dict)
                        and override.get("label")
                    ):
                        override_offenders.append(
                            f"field_overrides.{kind}.{table}.{column}.label"
                        )

        self.assertEqual(field_offenders, [])
        self.assertEqual(override_offenders, [])

    def test_enum_display_columns_are_declared_once_per_table(self):
        enums = yaml.safe_load(
            (CAPABILITY_ROOT / "enums.yaml").read_text(encoding="utf-8")
        )["enums"]

        missing = []
        owners: dict[tuple[str, str], str] = {}
        duplicates = []
        for enum_id, raw in enums.items():
            table = str(raw.get("table") or "")
            column = str(raw.get("column") or "")
            display_column = str(raw.get("display_column") or "")
            if not display_column:
                missing.append(str(enum_id))
                continue
            key = (table, display_column)
            if key in owners:
                duplicates.append(
                    f"{table}.{display_column}: {owners[key]}, {enum_id}"
                )
            else:
                owners[key] = str(enum_id)

        self.assertEqual(missing, [])
        self.assertEqual(duplicates, [])

    def test_enum_case_is_generated_only_from_enums_yaml(self):
        config = yaml.safe_load(
            (CAPABILITY_ROOT / "fields.yaml").read_text(encoding="utf-8")
        )
        offenders = []
        for subject, subject_fields in config["fields"].items():
            for field_id, field in subject_fields.items():
                if not isinstance(field, dict) or not field.get("enum_key"):
                    continue
                if "CASE " in str(field.get("expression") or "").upper():
                    offenders.append(f"{subject}.{field_id}")
                if field.get("label"):
                    offenders.append(f"{subject}.{field_id}.label")
        self.assertEqual(offenders, [])

    def test_metrics_do_not_own_dimension_or_time_lexicons(self):
        metrics = yaml.safe_load(
            (CAPABILITY_ROOT / "metrics.yaml").read_text(encoding="utf-8")
        )["metrics"]
        predicates = yaml.safe_load(
            (CAPABILITY_ROOT / "predicates.yaml").read_text(encoding="utf-8")
        )["predicates"]
        dimensions = yaml.safe_load(
            (CAPABILITY_ROOT / "dimensions.yaml").read_text(encoding="utf-8")
        )["dimensions"]
        time_buckets = yaml.safe_load(
            (CAPABILITY_ROOT / "time_buckets.yaml").read_text(encoding="utf-8")
        )["time_buckets"]

        self.assertNotIn("_lexicon", metrics)
        self.assertIn("_lexicon", dimensions)
        self.assertIn("_lexicon", time_buckets)
        self.assertIn("numeric_fields", predicates["_lexicon"])
        self.assertIn("numeric_ops", predicates["_lexicon"])
        self.assertNotIn("metric_expressions", predicates["_lexicon"])

    def test_mdl_descriptions_do_not_duplicate_table_labels(self):
        overrides = yaml.safe_load(
            (REPO_ROOT / "wren_projects" / "road_mysql" / "overrides.yml")
            .read_text(encoding="utf-8")
        )["models"]
        capability_overrides = yaml.safe_load(
            (CAPABILITY_ROOT / "field_overrides.yaml").read_text(
                encoding="utf-8"
            )
        )["field_overrides"]["mysql"]["tables"]

        offenders = []
        for table, raw in overrides.items():
            label = str((capability_overrides.get(table) or {}).get("label") or "")
            description = " ".join(str(raw.get("description") or "").split())
            if label and description.startswith(label):
                offenders.append(f"{table}: {description}")

        self.assertEqual(offenders, [])

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
