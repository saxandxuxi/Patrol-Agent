import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import PlanNode, QueryPlan, QuerySpec, QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView


def metric_plan(metric: str) -> QueryPlan:
    return QueryPlan(
        question="路线健康比较",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="distribution",
                    metrics=(metric,),
                    dimensions=("route_name",),
                    filters=(),
                    output_projection=("route_name", metric),
                    order_by=(),
                    limit=None,
                    required_assertions=(),
                ),
            ),
        ),
    )


def derived_metric_plan(
    *,
    include_operand: bool,
    declare_derived: bool = True,
    post_calc_subject: str = "disease",
) -> QueryPlan:
    metrics = ("count", "total_area")
    if include_operand:
        metrics += ("route_length_km",)
    return QueryPlan(
        question="哪条路的健康状况最好",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="distribution",
                    metrics=metrics,
                    dimensions=("route_name",),
                    filters=(),
                    output_projection=("route_name", *metrics),
                    order_by=(),
                    limit=None,
                    required_assertions=(),
                ),
            ),
            PlanNode(
                id="q2",
                spec=QuerySpec(
                    id="q2",
                    kind="mysql",
                    subject=post_calc_subject,
                    view="data",
                    metrics=("disease_density",) if declare_derived else (),
                    dimensions=(),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=("lower_is_better",),
                ),
                depends_on=("q1",),
                node_type="post_calc",
            ),
        ),
        response_mode="analysis",
    )


class CapabilityRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_unknown_metric_retries_once_with_allowed_values(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            metric_plan("disease_density"),
            metric_plan("count"),
        ]

        plan = analyze_single_turn(
            "路线健康比较",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
        )

        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[4]
        self.assertIn('"failure_code": "unknown_metric"', feedback)
        self.assertIn("allowed_values", feedback)
        self.assertEqual(plan.nodes[0].spec.metrics, ("count",))

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_unknown_metric_stops_after_two_repairs(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            metric_plan("disease_density"),
            metric_plan("disease_density"),
            metric_plan("disease_density"),
        ]

        with self.assertRaisesRegex(QuerySpecError, "当前数据能力不支持"):
            analyze_single_turn(
                "路线健康比较",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
            )

        self.assertEqual(analyze_once.call_count, 3)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_missing_derived_operand_is_repaired_before_execution(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            derived_metric_plan(include_operand=False),
            derived_metric_plan(include_operand=True),
        ]

        plan = analyze_single_turn(
            "哪条路的健康状况最好",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[4]
        self.assertIn('"failure_code": "missing_derived_operand"', feedback)
        self.assertIn('"route_length_km"', feedback)
        self.assertIn("route_length_km", plan.nodes[0].spec.metrics)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_lower_is_better_requires_a_declared_derived_metric(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            derived_metric_plan(
                include_operand=False,
                declare_derived=False,
                post_calc_subject="route",
            ),
            derived_metric_plan(
                include_operand=True,
                post_calc_subject="route",
            ),
        ]

        plan = analyze_single_turn(
            "哪条路的健康状况最好",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[4]
        self.assertIn('"failure_code": "missing_derived_metric"', feedback)
        self.assertIn("disease_density", feedback)
        self.assertEqual(plan.nodes[1].spec.metrics, ("disease_density",))

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_missing_derived_operand_stops_after_two_repairs(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            derived_metric_plan(include_operand=False),
            derived_metric_plan(include_operand=False),
            derived_metric_plan(include_operand=False),
        ]

        with self.assertRaisesRegex(
            QuerySpecError,
            "缺少派生指标操作数",
        ):
            analyze_single_turn(
                "哪条路的健康状况最好",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

        self.assertEqual(analyze_once.call_count, 3)


if __name__ == "__main__":
    unittest.main()
