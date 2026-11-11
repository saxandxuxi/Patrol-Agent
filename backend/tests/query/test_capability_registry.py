import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

import yaml

from app.query.capability_registry import CapabilityConfigError, CapabilityRegistry
from app.services import wren_engine


CAPABILITY_DIR = Path(__file__).resolve().parents[3] / "resources" / "capabilities"


class CapabilityRegistryTests(unittest.TestCase):
    def test_registry_loads_all_capability_files(self):
        registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)

        self.assertIn("disease", registry.snapshot.subjects)
        self.assertIn("count", registry.snapshot.metrics["disease"])
        self.assertIn("crack", registry.snapshot.entity_families)
        self.assertTrue(registry.snapshot.version)
        self.assertTrue(registry.snapshot.config_hash)
        policy = registry.snapshot.mdl_compilation
        self.assertEqual(policy.max_context_bytes, 131072)
        self.assertEqual(policy.max_models_per_contract, 8)
        self.assertEqual(policy.max_relationship_hops, 4)
        self.assertEqual(policy.llm_fallback_attempts, 1)
        self.assertIsInstance(policy.forbidden_sql_types, tuple)
        self.assertIsInstance(policy.forbidden_functions, tuple)
        self.assertTrue(policy.require_soft_delete)

    def test_every_subject_has_the_registered_mdl_model(self):
        registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        configured = {
            "disease": "disease",
            "patrol": "patrol",
            "route": "route",
            "section": "section",
            "pile_point": "pile_point",
            "asset": "ai_asset_main",
            "work_order": "ai_work_order",
            "bridge": "basic_facilities_bridges",
            "tunnel": "basic_facilities_tunnels",
            "slope": "basic_facilities_slope",
            "project": "project_manage_project_main",
        }
        actual = {
            subject: capability.mdl_model
            for subject, capability in registry.snapshot.subjects.items()
        }

        self.assertEqual(
            {subject: actual[subject] for subject in configured},
            configured,
        )
        self.assertEqual(
            set(actual.values()),
            set(wren_engine.model_names("mysql")),
        )

    def test_registry_rejects_unknown_config_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "capabilities"
            shutil.copytree(CAPABILITY_DIR, config_dir)
            subjects = config_dir / "subjects.yaml"
            subjects.write_text(
                subjects.read_text(encoding="utf-8")
                + "\nunexpected_key: true\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CapabilityConfigError, "unknown_key"):
                CapabilityRegistry.load(config_dir=config_dir)

    def test_registry_rejects_subject_joins_in_favor_of_mdl(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "capabilities"
            shutil.copytree(CAPABILITY_DIR, config_dir)
            path = config_dir / "subjects.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            data["subjects"]["route"]["joins"] = {}
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaises(CapabilityConfigError) as caught:
                CapabilityRegistry.load(config_dir=config_dir)

        self.assertEqual(
            caught.exception.evidence,
            {
                "fact": "joins",
                "required_source": "wren_mdl",
                "path": "subjects.route.joins",
            },
        )

    def test_registry_rejects_code_map_outside_enums(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "capabilities"
            shutil.copytree(CAPABILITY_DIR, config_dir)
            path = config_dir / "field_overrides.yaml"
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            columns = data["field_overrides"]["mysql"]["columns"][
                "ai_problem"
            ]
            columns.setdefault("up_or_down", {})["code_map"] = {
                "0": "全部"
            }
            path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaises(CapabilityConfigError) as caught:
                CapabilityRegistry.load(config_dir=config_dir)

        self.assertEqual(
            caught.exception.evidence,
            {
                "fact": "code_map",
                "required_source": "enums.yaml",
                "path": (
                    "field_overrides.mysql.columns.ai_problem."
                    "up_or_down.code_map"
                ),
            },
        )

    def test_capability_snapshot_nested_mappings_are_read_only(self):
        registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)

        with self.assertRaises(TypeError):
            registry.snapshot.metrics["disease"]["count"] = None

    def test_mdl_compilation_policy_is_frozen(self):
        registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)

        with self.assertRaises(FrozenInstanceError):
            registry.snapshot.mdl_compilation.llm_fallback_attempts = 2

    def test_registry_rejects_invalid_mdl_compilation_policy(self):
        invalid_values = (
            {"llm_fallback_attempts": 0},
            {"max_context_bytes": 0},
            {"max_models_per_contract": 0},
            {"max_relationship_hops": 0},
            {"forbidden_sql_types": ["SELECT"]},
            {"forbidden_functions": [""]},
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with tempfile.TemporaryDirectory() as tmp:
                    config_dir = Path(tmp) / "capabilities"
                    shutil.copytree(CAPABILITY_DIR, config_dir)
                    path = config_dir / "mdl_compilation.yaml"
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                    data["mdl_compilation"].update(invalid)
                    path.write_text(
                        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                        encoding="utf-8",
                    )

                    with self.assertRaises(CapabilityConfigError):
                        CapabilityRegistry.load(config_dir=config_dir)


if __name__ == "__main__":
    unittest.main()
