import json
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import MappingProxyType

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.context.capabilities import build_capability_context
from app.query.mdl_sql.context.mdl import build_mdl_context
from app.query.mdl_sql.context.prompt_context import (
    build_prompt_context,
    serialize_prompt_context,
)
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.errors import MdlSqlContextError


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


def binding(
    field: str,
    value: object,
    *,
    display: str = "",
    resolver: str = "test",
) -> ValueBinding:
    return ValueBinding(
        field=field,
        op="=",
        storage_value=value,
        display_value=display or str(value),
        source="user_explicit",
        resolver=resolver,
        raw_text=display or str(value),
    )


def disease_list_spec() -> QuerySpec:
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view="list",
        metrics=(),
        dimensions=(),
        filters=(binding("route_level", "0", display="国道"),),
        output_projection=("route_name", "confidence"),
        order_by=(),
        limit=10,
        required_assertions=("preserve_user_scope",),
        question="列出国道病害及其置信度",
    )


def section_list_spec() -> QuerySpec:
    return QuerySpec(
        id="q2",
        kind="mysql",
        subject="section",
        view="list",
        metrics=(),
        dimensions=(),
        filters=(binding("route_level", "0", display="国道"),),
        output_projection=("id", "route_name"),
        order_by=(),
        limit=10,
        required_assertions=("preserve_user_scope",),
        question="列出国道上的路段",
    )


class MdlContextTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)

    def contract(self, spec: QuerySpec, plan_id: str = "plan-context"):
        return build_mdl_query_contract(
            spec,
            self.registry,
            self.snapshot,
            plan_id=plan_id,
        )

    def test_capability_context_contains_only_contract_references(self):
        contract = self.contract(disease_list_spec())

        context = build_capability_context(
            contract,
            self.registry.snapshot,
        )

        self.assertEqual(
            set(context.fields),
            {"route_level", "route_name", "confidence"},
        )
        self.assertNotIn("area", context.fields)
        self.assertNotIn("del_flag", context.fields)
        self.assertEqual(
            tuple(context.enum_values["route_level"]),
            ("0",),
        )
        self.assertEqual(
            context.enum_values["route_level"]["0"]["label"],
            "国道",
        )
        with self.assertRaises(FrozenInstanceError):
            context.subject = "patrol"
        with self.assertRaises(TypeError):
            context.fields["new"] = MappingProxyType({})

    def test_complete_enum_members_require_the_registered_assertion(self):
        spec = QuerySpec(
            id="q3",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down",),
            filters=(binding("route_level", "1", display="省道"),),
            output_projection=("up_or_down", "count"),
            order_by=(),
            limit=None,
            required_assertions=(
                "preserve_user_scope",
                "complete_enum_dimensions",
            ),
            question="按上下行完整统计病害",
        )
        contract = self.contract(spec)

        context = build_capability_context(
            contract,
            self.registry.snapshot,
        )

        registered = self.registry.snapshot.enums[
            ("ai_problem", "up_or_down")
        ]
        self.assertEqual(
            set(context.enum_values["up_or_down"]),
            set(registered.values),
        )
        self.assertEqual(
            set(context.enum_values["route_level"]),
            {"1"},
        )
        self.assertIn("complete_enum_dimensions", context.assertions)

    def test_capability_context_includes_only_referenced_entity_family(self):
        spec = QuerySpec(
            id="q4",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                binding(
                    "problem_type_name",
                    ["横向裂缝", "纵向裂缝"],
                    display="裂缝",
                    resolver="entity_family:crack",
                ),
            ),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="裂缝有多少处",
        )
        contract = self.contract(spec)

        context = build_capability_context(
            contract,
            self.registry.snapshot,
        )

        self.assertEqual(set(context.entity_families), {"crack"})
        self.assertEqual(
            context.entity_families["crack"]["members"],
            ("横向裂缝", "纵向裂缝", "网状裂缝"),
        )
        self.assertNotIn("strip_disease", context.entity_families)

    def test_capability_context_derives_only_from_bound_operands(self):
        disease_spec = QuerySpec(
            id="q5",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="病害数量",
        )
        route_spec = QuerySpec(
            id="q_route",
            kind="mysql",
            subject="route",
            view="sum",
            metrics=("total_route_length_km",),
            dimensions=(),
            filters=(),
            output_projection=("total_route_length_km",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="路线总长度",
        )
        disease_contract = self.contract(disease_spec)
        route_contract = self.contract(route_spec)
        contract = replace(
            disease_contract,
            metrics=(
                *disease_contract.metrics,
                *route_contract.metrics,
            ),
        )

        context = build_capability_context(
            contract,
            self.registry.snapshot,
        )

        self.assertIn("disease_density", context.derived_metrics)
        self.assertEqual(
            context.derived_metrics["disease_density"]["operands"],
            ("count", "total_route_length_km"),
        )
        self.assertEqual(
            context.derived_metrics["disease_density"]["evaluator"],
            "post_calc",
        )

    def test_mdl_context_has_join_facts_without_physical_names(self):
        contract = self.contract(section_list_spec())

        context = build_mdl_context(contract, self.snapshot)
        serialized = json.dumps(
            context.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertEqual(set(context.models), {"route", "section"})
        self.assertEqual(
            [item["relationship_id"] for item in context.relationships],
            ["section_route"],
        )
        self.assertIn("short_name", context.models["route"]["columns"])
        self.assertNotIn("basic_facilities_sections", serialized)
        self.assertNotIn("basic_facilities_routes", serialized)
        self.assertNotIn("new_four_road_base_ai", serialized)

    def test_prompt_context_carries_code_evidence_alongside_mdl(self):
        query_spec = replace(
            disease_list_spec(),
            question="G4京港澳高速第三车道的病害明细",
        )
        contract = self.contract(query_spec)

        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        prompt_context = context.to_dict()

        self.assertIn("mdl_subset", prompt_context)
        self.assertIn("business_capability", prompt_context)
        self.assertIn(
            "lane_ordinal",
            {
                item["rule_id"]
                for item in prompt_context["assist_evidence"]
            },
        )
        self.assertTrue(
            all(
                item["trusted"] is False
                for item in prompt_context["assist_evidence"]
            )
        )

    def test_prompt_context_budget_fails_without_truncation(self):
        contract = self.contract(section_list_spec())
        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        stripped_models = {
            model_name: {
                **model,
                "description": "",
                "columns": {
                    column_name: {
                        **column,
                        "description": "",
                    }
                    for column_name, column in model["columns"].items()
                },
            }
            for model_name, model in context.mdl_subset.models.items()
        }
        stripped = replace(
            context,
            mdl_subset=replace(
                context.mdl_subset,
                models=MappingProxyType(stripped_models),
            ),
        )
        minimal = serialize_prompt_context(
            stripped,
            max_context_bytes=1_000_000,
        )
        limit = len(minimal.encode("utf-8"))

        self.assertEqual(
            serialize_prompt_context(
                context,
                max_context_bytes=limit,
            ),
            minimal,
        )
        with self.assertRaises(MdlSqlContextError) as caught:
            serialize_prompt_context(
                context,
                max_context_bytes=limit - 1,
            )
        self.assertEqual(caught.exception.code, "context_budget_exceeded")
        self.assertEqual(
            caught.exception.evidence["required_bytes"],
            limit,
        )
        self.assertNotIn("serialized", caught.exception.evidence)


if __name__ == "__main__":
    unittest.main()
