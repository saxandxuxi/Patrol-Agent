import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.compiler.deterministic import (
    compile_contract_to_mdl_sql,
)
from app.query.mdl_sql.compiler.pipeline import run_mdl_sql_pipeline
from app.query.mdl_sql.context.prompt_context import build_prompt_context
from app.query.mdl_sql.contract_builder import build_mdl_query_contract
from app.query.mdl_sql.models import MdlFilterBinding


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"
PG_PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_pg"


def spec(
    subject: str,
    *,
    kind: str = "mysql",
    view: str,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    order_by: tuple[dict[str, str], ...] = (),
    limit: int | None = None,
    filters: tuple[ValueBinding, ...] = (),
) -> QuerySpec:
    return QuerySpec(
        id=f"{subject}-{view}",
        kind=kind,
        subject=subject,
        view=view,
        metrics=metrics,
        dimensions=dimensions,
        filters=filters,
        output_projection=outputs,
        order_by=order_by,
        limit=limit,
        required_assertions=("preserve_user_scope",),
        question=f"验证 {subject} 的 {view} 查询",
    )


class MdlSqlEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
        cls.snapshot = load_mdl_snapshot(
            "mysql",
            project_dir=PROJECT_DIR,
        )

    def run_pipeline(self, query_spec: QuerySpec, fallback):
        return run_mdl_sql_pipeline(
            query_spec,
            plan_id=f"e2e:{query_spec.id}",
            registry=self.registry,
            mdl_snapshot=self.snapshot,
            generate_mdl_sql=fallback,
        )

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS count", ""),
    )
    def test_every_registered_subject_runs_the_audited_pipeline(self, _dry_plan):
        fallback = Mock()

        for subject, capability in self.registry.snapshot.subjects.items():
            with self.subTest(subject=subject):
                result = self.run_pipeline(
                    spec(
                        subject,
                        view="count",
                        metrics=("count",),
                        outputs=("count",),
                    ),
                    fallback,
                )

                self.assertEqual(
                    result.contract.root_model,
                    capability.mdl_model,
                )
                self.assertIsNone(result.candidate.failure)
                self.assertTrue(result.audit.accepted, result.audit)
                self.assertTrue(result.dry_plan.accepted, result.dry_plan)
                self.assertEqual(result.physical_sql, "SELECT 1 AS count")

        fallback.assert_not_called()

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS count", ""),
    )
    def test_representative_basic_views_share_the_same_pipeline(
        self,
        _dry_plan,
    ):
        fallback = Mock()
        cases = (
            spec(
                "disease",
                view="list",
                outputs=("problem_number",),
                order_by=({"field": "problem_number", "direction": "asc"},),
                limit=20,
            ),
            spec(
                "route",
                view="distribution",
                metrics=("count",),
                dimensions=("level",),
                outputs=("level", "count"),
            ),
            spec(
                "disease",
                view="trend",
                metrics=("count",),
                dimensions=("collection_time",),
                outputs=("collection_time", "count"),
            ),
            spec(
                "section",
                view="top_records",
                outputs=("id",),
                order_by=({"field": "id", "direction": "asc"},),
                limit=3,
            ),
            spec(
                "bridge",
                view="top_groups",
                metrics=("count",),
                dimensions=("id",),
                outputs=("id", "count"),
                order_by=({"field": "count", "direction": "desc"},),
                limit=3,
            ),
            spec(
                "maintenance_work_order",
                view="distribution",
                metrics=("count",),
                dimensions=("order_status",),
                outputs=("order_status", "count"),
            ),
        )

        for query_spec in cases:
            with self.subTest(subject=query_spec.subject, view=query_spec.view):
                result = self.run_pipeline(query_spec, fallback)

                self.assertIsNone(result.candidate.failure)
                self.assertTrue(result.audit.accepted, result.audit)
                self.assertTrue(result.dry_plan.accepted, result.dry_plan)
                self.assertTrue(result.physical_sql)

        fallback.assert_not_called()

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS count", ""),
    )
    def test_section_and_disease_keep_distinct_roots(self, _dry_plan):
        fallback = Mock()
        section_result = self.run_pipeline(
            spec(
                "section",
                view="count",
                metrics=("count",),
                outputs=("count",),
            ),
            fallback,
        )
        disease_result = self.run_pipeline(
            spec(
                "disease",
                view="count",
                metrics=("count",),
                outputs=("count",),
            ),
            fallback,
        )

        self.assertEqual(section_result.contract.root_model, "section")
        self.assertEqual(disease_result.contract.root_model, "disease")
        self.assertNotEqual(
            section_result.contract.contract_hash,
            disease_result.contract.contract_hash,
        )
        fallback.assert_not_called()

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS count", ""),
    )
    def test_route_filter_does_not_fan_out_patrol_count_or_detail(
        self,
        _dry_plan,
    ):
        fallback = Mock()
        route_filter = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-1",
            display_value="长沙－芷江高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="长沙－芷江高速",
        )
        cases = (
            spec(
                "patrol",
                view="count",
                metrics=("count",),
                outputs=("count",),
                filters=(route_filter,),
            ),
            spec(
                "patrol",
                view="list",
                outputs=("patrol_number", "patrol_status", "patrol_mileage"),
                order_by=({"field": "create_time", "direction": "desc"},),
                limit=100,
                filters=(route_filter,),
            ),
        )

        for query_spec in cases:
            with self.subTest(view=query_spec.view):
                result = self.run_pipeline(query_spec, fallback)

                self.assertIsNone(result.candidate.failure)
                self.assertEqual(result.candidate.source, "deterministic")
                self.assertTrue(result.audit.accepted, result.audit)
                self.assertIn("EXISTS", result.candidate.mdl_sql)
                self.assertNotIn(
                    "LEFT JOIN patrol_route",
                    result.candidate.mdl_sql,
                )

        fallback.assert_not_called()

    def test_filter_only_exists_keeps_target_mandatory_predicates(self):
        route_filter = ValueBinding(
            field="route_name",
            op="=",
            storage_value="route-1",
            display_value="长沙－芷江高速公路",
            source="user_explicit",
            resolver="route_dictionary",
            raw_text="长沙－芷江高速",
        )
        query_spec = spec(
            "patrol",
            view="count",
            metrics=("count",),
            outputs=("count",),
            filters=(route_filter,),
        )
        contract = build_mdl_query_contract(
            query_spec,
            self.registry,
            self.snapshot,
            plan_id="filter-only-mandatory",
        )
        contract = replace(
            contract,
            mandatory_predicates=(
                *contract.mandatory_predicates,
                MdlFilterBinding(
                    field_id="route_level",
                    op="=",
                    value="0",
                    display_value="国道",
                    source="capability_registry",
                    resolver="test:target_mandatory",
                    raw_text="",
                    model="route",
                    column="level",
                    expression="route.level",
                ),
            ),
        )

        context = build_prompt_context(
            contract,
            self.registry,
            self.snapshot,
        )
        candidate = compile_contract_to_mdl_sql(contract, context)

        self.assertIsNone(candidate.failure)
        self.assertIn("EXISTS", candidate.mdl_sql)
        self.assertIn("route.level = '0'", candidate.mdl_sql)
        self.assertIn(
            "patrol.del_flag = 0",
            candidate.mdl_sql.split("WHERE", 1)[-1],
        )

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS route_name", ""),
    )
    def test_section_relation_exists_uses_mdl_without_fanout(
        self,
        _dry_plan,
    ):
        fallback = Mock()
        result = self.run_pipeline(
            spec(
                "section",
                view="list",
                outputs=("route_name", "full_name", "start_num", "end_num"),
                order_by=({"field": "full_name", "direction": "asc"},),
                limit=50,
                filters=(
                    ValueBinding(
                        field="route_level",
                        op="=",
                        storage_value="0",
                        display_value="国道",
                        source="user_explicit",
                        resolver="enum:basic_facilities_sections.route_level",
                        raw_text="国道",
                    ),
                    ValueBinding(
                        field="has_disease",
                        op="exists",
                        storage_value=True,
                        display_value="存在关联病害",
                        source="business_rule",
                        resolver="relation_exists:disease_section",
                        raw_text="有病害",
                    ),
                ),
            ),
            fallback,
        )

        self.assertIsNone(result.candidate.failure)
        self.assertEqual(result.candidate.source, "deterministic")
        self.assertTrue(result.audit.accepted, result.audit)
        self.assertIn("EXISTS", result.candidate.mdl_sql)
        self.assertIn(
            "disease.sections_id = section.id",
            result.candidate.mdl_sql,
        )
        self.assertIn("disease.del_flag = 0", result.candidate.mdl_sql)
        self.assertNotIn("LEFT JOIN disease", result.candidate.mdl_sql)
        fallback.assert_not_called()

    @patch(
        "app.query.mdl_sql.dry_plan.wren_engine.dry_plan_mdl_sql",
        return_value=("SELECT 1 AS count", ""),
    )
    def test_postgres_mdl_models_use_the_same_pipeline(self, _dry_plan):
        registry = CapabilityRegistry.load("postgresql")
        snapshot = load_mdl_snapshot(
            "postgresql",
            project_dir=PG_PROJECT_DIR,
        )
        fallback = Mock()

        for subject in registry.snapshot.subjects:
            with self.subTest(subject=subject):
                query_spec = spec(
                    subject,
                    kind="postgresql",
                    view="count",
                    metrics=("count",),
                    outputs=("count",),
                )
                result = run_mdl_sql_pipeline(
                    query_spec,
                    plan_id=f"pg:{subject}",
                    kind="postgresql",
                    registry=registry,
                    mdl_snapshot=snapshot,
                    generate_mdl_sql=fallback,
                )

                self.assertIsNone(result.candidate.failure)
                self.assertTrue(result.audit.accepted, result.audit)
                self.assertTrue(result.dry_plan.accepted, result.dry_plan)

        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
