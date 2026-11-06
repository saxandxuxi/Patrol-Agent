import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
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


class CapabilityRepairTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
