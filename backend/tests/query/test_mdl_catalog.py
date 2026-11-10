import shutil
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

from app.query.mdl_sql import (
    FAILURE_CODES,
    CompilerFailure,
    MdlModel,
    MdlRelationship,
    MdlSnapshot,
    build_relationship_subgraph,
    failure_message,
    load_mdl_snapshot,
)
from app.query.mdl_sql.errors import MdlSqlRelationshipError
from app.services import wren_engine


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"
CANONICAL_FAILURE_CODES = {
    "contract_build_failed",
    "context_budget_exceeded",
    "relationship_path_ambiguous",
    "compiler_not_covered",
    "compiler_exception",
    "root_subject_mismatch",
    "unexpected_model",
    "unexpected_field",
    "missing_filter",
    "extra_filter",
    "filter_value_mismatch",
    "illegal_relationship",
    "cardinality_risk",
    "invalid_view_shape",
    "missing_output",
    "order_required",
    "invalid_limit",
    "physical_table_reference",
    "soft_delete_missing",
    "sql_parse_failed",
    "multiple_statements",
    "forbidden_sql_type",
    "dangerous_function",
    "star_projection",
    "dry_plan_failed",
    "physical_sql_invalid",
    "llm_sql_invalid",
    "llm_declared_scope_mismatch",
    "llm_fallback_failed",
    "llm_rate_limited",
    "llm_quota_exhausted",
    "llm_transport_error",
    "llm_provider_unavailable",
}


def _model(name: str) -> MdlModel:
    return MdlModel(
        name=name,
        physical_table=name,
        description=name,
        columns={},
    )


def _relationship(
    relationship_id: str,
    left: str,
    right: str,
) -> MdlRelationship:
    return MdlRelationship(
        relationship_id=relationship_id,
        models=(left, right),
        join_type="MANY_TO_ONE",
        cardinality="MANY_TO_ONE",
        condition=f"{left}.id = {right}.id",
        field_pairs=((f"{left}.id", f"{right}.id"),),
        source="test",
    )


def _snapshot(*relationships: MdlRelationship) -> MdlSnapshot:
    names = {
        model
        for relationship in relationships
        for model in relationship.models
    }
    return MdlSnapshot(
        fingerprint="test",
        models={name: _model(name) for name in names},
        cubes={},
        relationships={
            relationship.relationship_id: relationship
            for relationship in relationships
        },
    )


class MdlCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)
        cls.raw = wren_engine._load_mdl(str(PROJECT_DIR))

    def test_loads_real_mdl_facts_and_calculated_columns(self):
        self.assertEqual(len(self.snapshot.models), len(self.raw["models"]))
        self.assertEqual(len(self.snapshot.cubes), len(self.raw["cubes"]))
        self.assertEqual(
            len(self.snapshot.relationships),
            len(self.raw["relationships"]),
        )
        self.assertIn("disease", self.snapshot.models)
        self.assertIn("disease_metrics", self.snapshot.cubes)

        calculated = self.snapshot.models["disease"].columns[
            "effective_disease_length"
        ]
        self.assertTrue(calculated.is_calculated)
        self.assertIn("disease_length", calculated.expression)
        self.assertTrue(calculated.description)

    def test_snapshot_is_recursively_read_only(self):
        with self.assertRaises(TypeError):
            self.snapshot.models["disease"].columns["id"] = None
        with self.assertRaises(TypeError):
            self.snapshot.relationships["disease_route"] = None
        with self.assertRaises(FrozenInstanceError):
            self.snapshot.models["disease"].description = "changed"
        self.assertIsInstance(self.snapshot.models, MappingProxyType)

    def test_fingerprint_changes_for_every_mdl_input(self):
        relative_files = (
            Path("wren_project.yml"),
            Path("overrides.yml"),
            Path("relationships.yml"),
            Path("models/disease/metadata.yml"),
            Path("cubes/disease_metrics/metadata.yml"),
            Path("target/mdl.json"),
        )
        for relative_path in relative_files:
            with self.subTest(path=str(relative_path)):
                with tempfile.TemporaryDirectory() as tmp:
                    project_dir = Path(tmp) / "road_mysql"
                    shutil.copytree(PROJECT_DIR, project_dir)
                    before = load_mdl_snapshot(
                        "mysql",
                        project_dir=project_dir,
                    ).fingerprint

                    target = project_dir / relative_path
                    target.write_bytes(target.read_bytes() + b"\n")
                    wren_engine._load_mdl.cache_clear()
                    wren_engine._schema_index.cache_clear()
                    after = load_mdl_snapshot(
                        "mysql",
                        project_dir=project_dir,
                    ).fingerprint

                    self.assertNotEqual(before, after)

    def test_relationship_condition_is_parsed_structurally(self):
        self.assertEqual(
            self.snapshot.relationships["disease_route"].field_pairs,
            (("disease.routes_id", "route.id"),),
        )
        relationship = self.snapshot.relationships["disease_route"]
        self.assertEqual(relationship.join_type, "MANY_TO_ONE")
        self.assertEqual(relationship.cardinality, "MANY_TO_ONE")
        self.assertTrue(relationship.source)

    def test_relationship_pairs_match_wren_parity(self):
        allowed = wren_engine._relationship_columns("mysql")
        for relationship in self.snapshot.relationships.values():
            with self.subTest(relationship=relationship.relationship_id):
                pair = frozenset(relationship.models)
                parsed_pairs = frozenset(
                    {
                        tuple(field.split(".", 1))
                        for field in relationship.field_pairs[0]
                    }
                )
                self.assertIn(parsed_pairs, allowed[pair])

    def test_failure_code_registry_is_closed_and_explained(self):
        self.assertEqual(set(FAILURE_CODES), CANONICAL_FAILURE_CODES)
        for code, message in FAILURE_CODES.items():
            with self.subTest(code=code):
                self.assertTrue(message)
                self.assertLessEqual(len(message), 40)
                self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in message))

        unknown = failure_message(
            CompilerFailure(
                code="not_a_real_failure_code",
                stage="audit",
                evidence={},
            )
        )
        self.assertNotIn("not_a_real_failure_code", unknown)
        self.assertNotIn("KeyError", unknown)
        self.assertTrue(any("\u4e00" <= char <= "\u9fff" for char in unknown))
        with self.assertRaises(TypeError):
            FAILURE_CODES["new_code"] = "新原因"

    def test_relationship_subgraph_finds_unique_two_hop_path(self):
        snapshot = _snapshot(
            _relationship("ab", "a", "b"),
            _relationship("bc", "b", "c"),
        )

        subgraph = build_relationship_subgraph(
            snapshot,
            root_model="a",
            required_models=("c",),
            required_relationship_ids=(),
            max_hops=2,
        )

        self.assertEqual(
            [item.relationship_id for item in subgraph.relationships],
            ["ab", "bc"],
        )
        self.assertEqual(subgraph.models, ("a", "b", "c"))

    def test_relationship_subgraph_rejects_ambiguous_paths(self):
        snapshot = _snapshot(
            _relationship("ab", "a", "b"),
            _relationship("bc", "b", "c"),
            _relationship("ad", "a", "d"),
            _relationship("dc", "d", "c"),
        )

        with self.assertRaises(MdlSqlRelationshipError) as caught:
            build_relationship_subgraph(
                snapshot,
                root_model="a",
                required_models=("c",),
                required_relationship_ids=(),
                max_hops=2,
            )

        self.assertEqual(caught.exception.code, "relationship_path_ambiguous")
        self.assertEqual(caught.exception.evidence["reason"], "multiple_paths")

    def test_relationship_subgraph_terminates_on_cycles(self):
        snapshot = _snapshot(
            _relationship("ab", "a", "b"),
            _relationship("bc", "b", "c"),
            _relationship("ca", "c", "a"),
        )

        with self.assertRaises(MdlSqlRelationshipError) as caught:
            build_relationship_subgraph(
                snapshot,
                root_model="a",
                required_models=("b",),
                required_relationship_ids=(),
                max_hops=2,
            )

        self.assertEqual(caught.exception.code, "relationship_path_ambiguous")

    def test_required_relationship_id_is_fixed(self):
        snapshot = _snapshot(
            _relationship("ab", "a", "b"),
            _relationship("bc", "b", "c"),
            _relationship("ac", "a", "c"),
        )

        subgraph = build_relationship_subgraph(
            snapshot,
            root_model="a",
            required_models=("c",),
            required_relationship_ids=("ab", "bc"),
            max_hops=2,
        )

        self.assertEqual(
            [item.relationship_id for item in subgraph.relationships],
            ["ab", "bc"],
        )

    def test_relationship_subgraph_rejects_path_over_max_hops(self):
        snapshot = _snapshot(
            _relationship("ab", "a", "b"),
            _relationship("bc", "b", "c"),
        )

        with self.assertRaises(MdlSqlRelationshipError) as caught:
            build_relationship_subgraph(
                snapshot,
                root_model="a",
                required_models=("c",),
                required_relationship_ids=(),
                max_hops=1,
            )

        self.assertEqual(
            caught.exception.evidence["reason"],
            "unreachable_within_max_hops",
        )


if __name__ == "__main__":
    unittest.main()
