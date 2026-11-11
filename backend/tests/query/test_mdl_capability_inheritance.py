import unittest
from pathlib import Path

from app.query.analysis import _query_plan_compile_error
from app.query.capability_registry import (
    CapabilityConfigError,
    CapabilityRegistry,
)
from app.query.capability_registry.mdl_facts import (
    _expression_identity,
    merge_metric_facts,
)
from app.query.capability_registry.models import MetricCapability
from app.query.contracts import PlanNode, QueryPlan, QuerySpec
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.services import schema_catalog


REPO_ROOT = Path(__file__).resolve().parents[3]
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


class MdlCapabilityInheritanceTests(unittest.TestCase):
    def test_registry_inherits_every_mdl_model_without_replacing_business_aliases(
        self,
    ):
        registry = CapabilityRegistry.load()
        snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)
        subjects_by_model = {
            subject.mdl_model: subject
            for subject in registry.snapshot.subjects.values()
        }

        self.assertEqual(set(snapshot.models), set(subjects_by_model))
        self.assertEqual(
            subjects_by_model["ai_asset_main"].subject_id,
            "asset",
        )
        self.assertEqual(
            subjects_by_model["ai_asset_base"].subject_id,
            "ai_asset_base",
        )

    def test_mdl_only_subject_gets_fields_and_baseline_count(self):
        registry = CapabilityRegistry.load()
        fields = registry.snapshot.fields.get("maintenance_work_order") or {}
        metrics = registry.snapshot.metrics.get("maintenance_work_order") or {}

        self.assertTrue(fields)
        self.assertIn("id", fields)
        self.assertIn("count", metrics)
        self.assertIn("dimension", fields["order_status"].roles)
        self.assertIn("output", fields["order_status"].roles)

    def test_mdl_cube_measures_are_inherited_and_business_metrics_remain(self):
        registry = CapabilityRegistry.load()
        metrics = registry.snapshot.metrics["disease"]

        self.assertIn("disease_count", metrics)
        self.assertIn("distinct_pile_count", metrics)
        self.assertEqual(metrics["disease_count"].metric_id, "count")
        self.assertEqual(metrics["disease_count"].expression, "COUNT(*)")
        self.assertIn("avg_area", metrics)
        self.assertEqual(metrics["avg_area"].metric_id, "average_area")
        self.assertEqual(metrics["total_area"].label, "总面积")

    def test_auto_fields_use_the_mdl_model_name_not_the_business_alias(self):
        registry = CapabilityRegistry.load()
        field = registry.snapshot.fields["asset"]["id"]

        self.assertEqual(field.expression, "ai_asset_main.id")

    def test_mdl_model_description_reaches_the_planner(self):
        registry = CapabilityRegistry.load()
        view = registry.planner_view(
            "maintenance_work_order",
            reach=None,
        )

        self.assertEqual(
            view.to_prompt_dict()["description"],
            "养护工单：养护_任务工单",
        )

    def test_field_catalog_uses_the_central_enum_source(self):
        catalog = schema_catalog.field_catalog("mysql")
        table = next(
            item
            for item in catalog["tables"]
            if item["table"] == "ai_problem"
        )
        column = next(
            item
            for item in table["columns"]
            if item["name"] == "up_or_down"
        )

        self.assertEqual(
            column["code_map"],
            {
                "0": "全部/未区分",
                "1": "上行",
                "2": "下行",
            },
        )

    def test_enum_backed_fields_derive_label_and_display_from_enums(self):
        registry = CapabilityRegistry.load()
        disease_fields = registry.snapshot.fields["disease"]
        route_level = schema_catalog.field_catalog("mysql")
        route_table = next(
            item
            for item in route_level["tables"]
            if item["table"] == "basic_facilities_routes"
        )
        level_column = next(
            item
            for item in route_table["columns"]
            if item["name"] == "level"
        )

        self.assertEqual(disease_fields["up_or_down"].label, "上下行")
        self.assertIn("CASE", disease_fields["up_or_down"].expression.upper())
        self.assertEqual(disease_fields["route_level"].label, "行政等级")
        self.assertEqual(disease_fields["road_level"].label, "行政等级")
        self.assertEqual(disease_fields["tech_grade"].label, "公路技术等级")
        self.assertEqual(level_column["label"], "行政等级")

    def test_each_database_inherits_its_own_mdl_models(self):
        registry = CapabilityRegistry.load("postgresql")

        self.assertIn("gps_point", registry.snapshot.subjects)
        self.assertIn("inclination_point", registry.snapshot.subjects)
        self.assertNotIn("disease", registry.snapshot.subjects)
        self.assertIn("create_time", registry.snapshot.fields["gps_point"])

    def test_planner_compile_audit_does_not_skip_mdl_only_subjects(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="maintenance_work_order",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
            question="养护工单有多少",
        )
        issue = _query_plan_compile_error(
            QueryPlan(
                question=spec.question,
                nodes=(PlanNode(id=spec.id, spec=spec),),
            )
        )

        self.assertIsNone(issue)

    def test_equivalent_metric_facts_merge_without_losing_capabilities(self):
        resource_metric = MetricCapability(
            metric_id="total_area",
            expression="SUM({alias}.area)",
            label="总面积",
            unit="㎡",
            allowed_views=("count", "sum"),
        )
        mdl_metric = MetricCapability(
            metric_id="total_area",
            expression="SUM(area)",
            label="总面积（㎡）",
            allowed_views=("distribution", "top_groups"),
        )

        merged = merge_metric_facts(
            resource_metric,
            mdl_metric,
            subject="disease",
            metric_id="total_area",
            existing_source="resources",
            incoming_source="mdl",
        )

        self.assertEqual(merged.expression, resource_metric.expression)
        self.assertEqual(merged.label, "总面积")
        self.assertEqual(merged.unit, "㎡")
        self.assertEqual(
            set(merged.allowed_views),
            {"count", "sum", "distribution", "top_groups"},
        )

    def test_conflicting_metric_facts_return_structured_evidence(self):
        resource_metric = MetricCapability(
            metric_id="total_area",
            expression="SUM({alias}.area)",
            label="总面积",
        )
        mdl_metric = MetricCapability(
            metric_id="total_area",
            expression="SUM({alias}.disease_length)",
            label="总面积",
        )

        with self.assertRaises(CapabilityConfigError) as caught:
            merge_metric_facts(
                resource_metric,
                mdl_metric,
                subject="disease",
                metric_id="total_area",
                existing_source="resources",
                incoming_source="mdl",
            )

        self.assertEqual(
            caught.exception.evidence["code"],
            "conflicting_metric_fact",
        )
        self.assertEqual(
            caught.exception.evidence["sources"],
            ["resources", "mdl"],
        )
        self.assertIn(
            "SUM({alias}.area)",
            caught.exception.evidence["existing"]["expression"],
        )

    def test_equivalent_metrics_require_an_explicit_alias(self):
        canonical = MetricCapability(
            metric_id="average_area",
            expression="AVG({alias}.area)",
            label="平均面积",
            unit="㎡",
            aliases=("avg_area",),
        )
        alias_fact = MetricCapability(
            metric_id="avg_area",
            expression="AVG({alias}.area)",
            label="平均面积（㎡）",
            unit="㎡",
        )

        merged = merge_metric_facts(
            canonical,
            alias_fact,
            subject="disease",
            metric_id="avg_area",
            existing_source="resources",
            incoming_source="mdl",
        )

        self.assertEqual(merged.metric_id, "average_area")
        self.assertIn("avg_area", merged.aliases)

        with self.assertRaises(CapabilityConfigError) as caught:
            merge_metric_facts(
                MetricCapability(
                    metric_id="average_area",
                    expression="AVG({alias}.area)",
                    label="平均面积",
                ),
                alias_fact,
                subject="disease",
                metric_id="avg_area",
                existing_source="resources",
                incoming_source="mdl",
            )

        self.assertEqual(
            caught.exception.evidence["code"],
            "undeclared_metric_alias",
        )

    def test_planner_exposes_only_canonical_metric_facts(self):
        registry = CapabilityRegistry.load()
        view = registry.planner_view("disease", reach=None).to_prompt_dict()

        self.assertIn("count", view["metrics"])
        self.assertIn("average_area", view["metrics"])
        self.assertNotIn("disease_count", view["metrics"])
        self.assertNotIn("avg_area", view["metrics"])
        self.assertIn("disease_count", view["metrics"]["count"]["aliases"])
        self.assertIn("avg_area", view["metrics"]["average_area"]["aliases"])

    def test_query_spec_metric_aliases_compile_to_canonical_ids(self):
        registry = CapabilityRegistry.load()
        snapshot = load_mdl_snapshot("mysql", project_dir=PROJECT_DIR)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("avg_area",),
            dimensions=(),
            filters=(),
            output_projection=("avg_area",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="病害平均面积是多少",
        )

        contract = build_mdl_query_contract(
            spec,
            registry,
            snapshot,
        )

        self.assertEqual(contract.metrics[0].metric_id, "average_area")
        self.assertEqual(contract.outputs[0].output_id, "average_area")

    def test_each_subject_has_one_canonical_metric_per_formula(self):
        registry = CapabilityRegistry.load()

        duplicates = []
        for subject, metrics in registry.snapshot.metrics.items():
            seen = {}
            for metric_id in registry.snapshot.metric_ids(subject):
                metric = metrics[metric_id]
                identity = _expression_identity(metric.expression)
                if identity in seen:
                    duplicates.append(
                        f"{subject}: {seen[identity]} == {metric_id}"
                    )
                seen[identity] = metric_id

        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
