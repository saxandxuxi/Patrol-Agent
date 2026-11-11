import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.errors import MdlSqlContractError


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"
SUBJECT_MODELS = {
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


def binding(field: str, op: str, value: object) -> ValueBinding:
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="test",
        raw_text=str(value),
    )


def count_spec(subject: str, **overrides: object) -> QuerySpec:
    values: dict[str, object] = {
        "id": f"{subject}_count",
        "kind": "mysql",
        "subject": subject,
        "view": "count",
        "metrics": ("count",),
        "dimensions": (),
        "filters": (),
        "output_projection": ("count",),
        "order_by": (),
        "limit": None,
        "required_assertions": ("preserve_user_scope",),
        "question": f"统计{subject}数量",
    }
    values.update(overrides)
    return QuerySpec(**values)


class MdlContractBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)

    def test_every_registered_subject_builds_a_count_contract(self):
        for subject, root_model in SUBJECT_MODELS.items():
            with self.subTest(subject=subject):
                contract = build_mdl_query_contract(
                    count_spec(subject),
                    self.registry,
                    self.snapshot,
                    plan_id="plan-count",
                )

                self.assertEqual(contract.root_model, root_model)
                self.assertEqual(contract.subject, subject)
                self.assertEqual(contract.metrics[0].metric_id, "count")
                self.assertEqual(
                    contract.metrics[0].expression,
                    (
                        "COUNT(DISTINCT patrol.id)"
                        if subject == "patrol"
                        else "COUNT(*)"
                    ),
                )
                self.assertIn(root_model, contract.allowed_models)
                self.assertEqual(len(contract.mandatory_predicates), 1)
                self.assertIn(
                    "del_flag",
                    contract.mandatory_predicates[0].expression,
                )

    def test_section_and_disease_contracts_keep_different_roots_and_joins(self):
        section_spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="section",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(binding("route_level", "=", "0"),),
            output_projection=("id", "route_name"),
            order_by=(),
            limit=100,
            required_assertions=("preserve_user_scope",),
            question="行政等级为国道的路段有哪些",
        )
        disease_spec = QuerySpec(
            id="q2",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(binding("route_level", "=", "0"),),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="统计国道路段中检出的病害",
        )

        section = build_mdl_query_contract(
            section_spec,
            self.registry,
            self.snapshot,
            plan_id="plan-roots",
        )
        disease = build_mdl_query_contract(
            disease_spec,
            self.registry,
            self.snapshot,
            plan_id="plan-roots",
        )

        self.assertEqual(section.root_model, "section")
        self.assertEqual(disease.root_model, "disease")
        self.assertIn("section_route", section.required_relationship_ids)
        self.assertIn("disease_route", disease.required_relationship_ids)
        self.assertNotEqual(section.contract_hash, disease.contract_hash)
        self.assertEqual(section.filters[0].source, "user_explicit")
        self.assertEqual(section.output_aliases["路线名称"], "route_name")
        self.assertEqual(
            section.field_bindings["route_name"].model,
            "route",
        )

    def test_requires_join_target_model_resolves_to_mdl_relationship_id(self):
        disease_fields = dict(self.registry.snapshot.fields["disease"])
        disease_fields["route_level"] = replace(
            disease_fields["route_level"],
            requires_join=("route",),
        )
        fields = dict(self.registry.snapshot.fields)
        fields["disease"] = disease_fields
        registry = CapabilityRegistry(
            replace(self.registry.snapshot, fields=fields)
        )

        contract = build_mdl_query_contract(
            count_spec(
                "disease",
                filters=(binding("route_level", "=", "0"),),
            ),
            registry,
            self.snapshot,
        )

        self.assertIn("disease_route", contract.required_relationship_ids)

    def test_filter_binding_uses_normalized_filter_expression_location(self):
        contract = build_mdl_query_contract(
            count_spec(
                "disease",
                filters=(
                    binding(
                        "pile_number",
                        "between",
                        ("K120+000", "K125+000"),
                    ),
                ),
            ),
            self.registry,
            self.snapshot,
        )

        binding_value = contract.filters[0]
        self.assertEqual(binding_value.expression, "disease.pile_distance")
        self.assertEqual(binding_value.model, "disease")
        self.assertEqual(binding_value.column, "pile_distance")
        self.assertEqual(binding_value.value, (120000, 125000))
        self.assertTrue(binding_value.between_inclusive)

    def test_contract_hash_is_stable_and_excludes_narrative_question(self):
        spec = count_spec("disease", id="stable")
        first = build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id="plan-stable",
        )
        second = build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id="plan-stable",
        )
        rephrased = build_mdl_query_contract(
            replace(spec, question="换一种自然语言问同样的事"),
            self.registry,
            self.snapshot,
            plan_id="plan-stable",
        )

        self.assertEqual(first.contract_hash, second.contract_hash)
        self.assertEqual(first.contract_hash, rephrased.contract_hash)
        self.assertEqual(first.query_spec_hash, rephrased.query_spec_hash)
        self.assertEqual(
            first.capability_snapshot_hash,
            self.registry.snapshot.config_hash,
        )
        self.assertEqual(first.mdl_fingerprint, self.snapshot.fingerprint)

    def test_contract_hash_tracks_authoritative_scope_changes(self):
        base = build_mdl_query_contract(
            count_spec(
                "disease",
                id="hash",
                filters=(binding("route_level", "=", "0"),),
            ),
            self.registry,
            self.snapshot,
            plan_id="plan-hash",
        )
        changed_filter = build_mdl_query_contract(
            count_spec(
                "disease",
                id="hash",
                filters=(binding("route_level", "=", "1"),),
            ),
            self.registry,
            self.snapshot,
            plan_id="plan-hash",
        )
        changed_output = build_mdl_query_contract(
            count_spec(
                "disease",
                id="hash",
                filters=(binding("route_level", "=", "0"),),
                output_projection=("count", "total_area"),
                metrics=("count", "total_area"),
            ),
            self.registry,
            self.snapshot,
            plan_id="plan-hash",
        )

        self.assertNotEqual(base.contract_hash, changed_filter.contract_hash)
        self.assertNotEqual(base.contract_hash, changed_output.contract_hash)

    def test_outputs_use_registered_business_labels(self):
        contract = build_mdl_query_contract(
            QuerySpec(
                id="labels",
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=("problem_number",),
                order_by=(),
                limit=5,
                required_assertions=("preserve_user_scope",),
                question="列出病害编号",
            ),
            self.registry,
            self.snapshot,
            plan_id="plan-labels",
        )

        output = contract.outputs[0]
        self.assertEqual(output.alias, "病害编号")
        self.assertEqual(
            contract.output_aliases["病害编号"],
            "problem_number",
        )

    def test_rejects_unavailable_fields_metrics_and_views(self):
        invalid_specs = (
            count_spec(
                "disease",
                output_projection=("not_a_field",),
                metrics=(),
                dimensions=("not_a_field",),
                view="distribution",
            ),
            count_spec("section", metrics=("not_a_metric",)),
            count_spec("section", view="photo"),
        )
        for spec in invalid_specs:
            with self.subTest(spec=spec):
                with self.assertRaises(MdlSqlContractError) as caught:
                    build_mdl_query_contract(
                        spec,
                        self.registry,
                        self.snapshot,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "contract_build_failed",
                )
                self.assertTrue(caught.exception.evidence["reason"])

    def test_soft_delete_predicate_only_uses_registered_role(self):
        fields = dict(self.registry.snapshot.fields)
        fields["section"] = {
            field_id: capability
            for field_id, capability in fields["section"].items()
            if "soft_delete" not in capability.roles
        }
        snapshot_without_role = replace(
            self.registry.snapshot,
            fields=fields,
        )
        registry_without_role = CapabilityRegistry(snapshot_without_role)

        contract = build_mdl_query_contract(
            count_spec("section"),
            registry_without_role,
            self.snapshot,
        )

        self.assertEqual(contract.mandatory_predicates, ())

    def test_contract_and_nested_bindings_are_read_only(self):
        contract = build_mdl_query_contract(
            count_spec("disease", id="immutable"),
            self.registry,
            self.snapshot,
        )

        with self.assertRaises(FrozenInstanceError):
            contract.limit = 1
        with self.assertRaises(TypeError):
            contract.field_bindings["new"] = None
        self.assertIsInstance(contract.field_bindings, MappingProxyType)

    def test_snapshot_exposes_exact_physical_table_to_model_index(self):
        self.assertEqual(
            self.snapshot.physical_table_to_model["basic_facilities_sections"],
            "section",
        )
        self.assertEqual(
            self.snapshot.physical_table_to_model["ai_problem"],
            "disease",
        )
        self.assertEqual(
            self.snapshot.models["section"].physical_table,
            "basic_facilities_sections",
        )


if __name__ == "__main__":
    unittest.main()
