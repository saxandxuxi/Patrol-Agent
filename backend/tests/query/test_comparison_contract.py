import unittest
from dataclasses import replace
from unittest.mock import patch

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import (
    InputBinding,
    PlanNode,
    QueryPlan,
    QuerySpec,
    QuerySpecError,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView


def _comparison_plan(
    left_view: str,
    right_view: str,
    *,
    projection: tuple[str, ...] = (),
) -> QueryPlan:
    def source(node_id: str, view: str) -> QuerySpec:
        return QuerySpec(
            id=node_id,
            kind="mysql",
            subject="disease",
            view=view,
            metrics=("count",) if view == "count" else (),
            dimensions=(),
            filters=(),
            output_projection=projection,
            order_by=(),
            limit=500 if view in {"list", "detail"} else None,
            required_assertions=("preserve_user_scope",),
        )

    return QueryPlan(
        question="比较两次巡查之间新增的坑槽病害",
        nodes=(
            PlanNode(id="before", spec=source("before", left_view)),
            PlanNode(id="after", spec=source("after", right_view)),
            PlanNode(
                id="compare",
                spec=QuerySpec(
                    id="compare",
                    kind="mysql",
                    subject="disease",
                    view="data",
                    metrics=(),
                    dimensions=(),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=("dataset_comparison",),
                ),
                depends_on=("before", "after"),
                node_type="post_calc",
            ),
        ),
    )


def _scalar_count_comparison_plan() -> QueryPlan:
    def source(node_id: str) -> QuerySpec:
        return QuerySpec(
            id=node_id,
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )

    return QueryPlan(
        question="比较两次巡查之间新增的坑槽病害",
        nodes=(
            PlanNode(id="before", spec=source("before")),
            PlanNode(id="after", spec=source("after")),
            PlanNode(
                id="compare",
                spec=QuerySpec(
                    id="compare",
                    kind="mysql",
                    subject="disease",
                    view="data",
                    metrics=(),
                    dimensions=(),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=("multi_metric_comparison",),
                ),
                depends_on=("before", "after"),
                node_type="post_calc",
            ),
        ),
    )


class DatasetComparisonContractTests(unittest.TestCase):
    def test_dataset_comparison_selects_explicit_entity_dependencies(self):
        from app.query.comparison_contract import (
            dataset_comparison_contract_issue,
        )

        def data_node(
            node_id: str,
            subject: str,
            output: tuple[str, ...],
        ):
            return PlanNode(
                id=node_id,
                spec=QuerySpec(
                    id=node_id,
                    kind="mysql",
                    subject=subject,
                    view="list",
                    metrics=(),
                    dimensions=(),
                    filters=(),
                    output_projection=output,
                    order_by=(),
                    limit=500,
                    required_assertions=("preserve_user_scope",),
                ),
            )

        plan = QueryPlan(
            question="比较最近两次巡检任务并查看期间新增病害",
            nodes=(
                data_node(
                    "patrol_before",
                    "patrol",
                    ("patrol_number", "create_time"),
                ),
                data_node(
                    "patrol_after",
                    "patrol",
                    ("patrol_number", "create_time"),
                ),
                data_node(
                    "disease_before",
                    "disease",
                    ("problem_number",),
                ),
                data_node(
                    "disease_after",
                    "disease",
                    ("problem_number",),
                ),
                PlanNode(
                    id="compare",
                    spec=QuerySpec(
                        id="compare",
                        kind="mysql",
                        subject="patrol",
                        view="data",
                        metrics=(),
                        dimensions=(),
                        filters=(),
                        output_projection=(),
                        order_by=(),
                        limit=None,
                        required_assertions=("dataset_comparison",),
                        comparison_depends_on=(
                            "disease_before",
                            "disease_after",
                        ),
                    ),
                    depends_on=(
                        "patrol_before",
                        "patrol_after",
                        "disease_before",
                        "disease_after",
                    ),
                    node_type="post_calc",
                ),
            ),
        )

        issue = dataset_comparison_contract_issue(
            plan,
            CapabilityRegistry.load(),
        )

        self.assertIsNone(issue)

    def test_dataset_comparison_rejects_scalar_count_dependencies(self):
        from app.query.comparison_contract import (
            dataset_comparison_contract_issue,
        )

        issue = dataset_comparison_contract_issue(
            _comparison_plan("count", "count"),
            CapabilityRegistry.load(),
        )

        self.assertIsNotNone(issue)
        self.assertEqual(
            issue.code,
            "dataset_comparison_requires_entity_details",
        )

    def test_dataset_comparison_accepts_shared_entity_identity(self):
        from app.query.comparison_contract import (
            dataset_comparison_contract_issue,
        )

        issue = dataset_comparison_contract_issue(
            _comparison_plan(
                "list",
                "list",
                projection=("problem_number", "pile_number"),
            ),
            CapabilityRegistry.load(),
        )

        self.assertIsNone(issue)

    def test_entity_set_operation_rejects_two_counts_even_when_labelled_comparison(self):
        from app.query.semantic_operations import (
            semantic_operation_contract_issue,
        )

        issue = semantic_operation_contract_issue(
            _scalar_count_comparison_plan(),
            "entity_set_comparison",
            CapabilityRegistry.load(),
        )

        self.assertIsNotNone(issue)
        self.assertEqual(
            issue.code,
            "entity_set_comparison_requires_dataset_node",
        )

    def test_entity_set_operation_accepts_dataset_comparison_plan(self):
        from app.query.semantic_operations import (
            semantic_operation_contract_issue,
        )

        issue = semantic_operation_contract_issue(
            _comparison_plan(
                "list",
                "list",
                projection=("problem_number",),
            ),
            "entity_set_comparison",
            CapabilityRegistry.load(),
        )

        self.assertIsNone(issue)

    def test_dataset_comparison_orders_previous_batch_before_latest(self):
        from app.query.comparison_contract import (
            normalize_dataset_comparison_order,
        )

        base = _comparison_plan(
            "list",
            "list",
            projection=("problem_number",),
        )
        before, after, comparison = base.nodes
        before = replace(
            before,
            spec=replace(
                before.spec,
                input_bindings=(
                    InputBinding(
                        from_node="batches",
                        from_field="patrol_number",
                        to_field="patrol_number",
                        row_index=1,
                    ),
                ),
            ),
        )
        after = replace(
            after,
            spec=replace(
                after.spec,
                input_bindings=(
                    InputBinding(
                        from_node="batches",
                        from_field="patrol_number",
                        to_field="patrol_number",
                        row_index=0,
                    ),
                ),
            ),
        )
        comparison = replace(
            comparison,
            spec=replace(
                comparison.spec,
                comparison_depends_on=("after", "before"),
            ),
        )
        batches = PlanNode(
            id="batches",
            spec=QuerySpec(
                id="batches",
                kind="mysql",
                subject="patrol",
                view="list",
                metrics=(),
                dimensions=("patrol_number", "create_time"),
                filters=(),
                output_projection=("patrol_number", "create_time"),
                order_by=({"field": "create_time", "direction": "desc"},),
                limit=2,
                required_assertions=("recent_window",),
            ),
        )
        plan = replace(
            base,
            nodes=(batches, before, after, comparison),
        )

        normalized = normalize_dataset_comparison_order(plan)

        compare_node = normalized.nodes[-1]
        self.assertEqual(
            compare_node.spec.comparison_depends_on,
            ("before", "after"),
        )

    @patch("app.query.analysis.audit_query_plan")
    @patch("app.query.analysis._analyze_once")
    def test_audit_operation_catches_mislabelled_scalar_plan(
        self,
        analyze_once,
        audit,
    ):
        from app.query.analysis import analyze_single_turn

        analyze_once.return_value = _scalar_count_comparison_plan()
        audit.return_value = {
            "complete": True,
            "issues": [],
            "answer_operation": "entity_set_comparison",
        }

        with self.assertRaises(QuerySpecError) as raised:
            analyze_single_turn(
                "长沙－芷江高速在2026年10月28日和2026年11月2日这两次巡检中，"
                "后一次比前一次新增了多少处坑槽病害？",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=CapabilityRegistry.load(),
            )

        self.assertIn("实体集合", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
