import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec
from app.query.mdl_sql import load_mdl_snapshot
from app.query.mdl_sql.compiler.pipeline import run_mdl_sql_pipeline


REPO_ROOT = Path(__file__).resolve().parents[3]
CAPABILITY_DIR = REPO_ROOT / "resources" / "capabilities"
PROJECT_DIR = REPO_ROOT / "wren_projects" / "road_mysql"


def spec(
    subject: str,
    *,
    view: str,
    metrics: tuple[str, ...] = (),
    dimensions: tuple[str, ...] = (),
    outputs: tuple[str, ...] = (),
    order_by: tuple[dict[str, str], ...] = (),
    limit: int | None = None,
) -> QuerySpec:
    return QuerySpec(
        id=f"{subject}-{view}",
        kind="mysql",
        subject=subject,
        view=view,
        metrics=metrics,
        dimensions=dimensions,
        filters=(),
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


if __name__ == "__main__":
    unittest.main()
