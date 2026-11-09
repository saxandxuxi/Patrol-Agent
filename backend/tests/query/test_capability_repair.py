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
                    # post_calc 不声明 calculation 时必须声明解释型断言，
                    # 否则规划期审计就会按 calculation_required 打回重排。
                    required_assertions=("lower_is_better", "analysis_only"),
                ),
                depends_on=("q1",),
                node_type="post_calc",
            ),
        ),
        response_mode="analysis",
    )


def post_calc_plan(assertions: tuple[str, ...]) -> QueryPlan:
    """一个数据节点 + 一个解释型 post_calc 节点（不声明 calculation）。"""
    return QueryPlan(
        question="上海到昆明和二连浩特到广州哪条泛油更多",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="count",
                    metrics=("count",),
                    dimensions=(),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=(),
                ),
            ),
            PlanNode(
                id="pc_compare",
                spec=QuerySpec(
                    id="pc_compare",
                    kind="mysql",
                    subject="disease",
                    view="data",
                    metrics=(),
                    dimensions=(),
                    filters=(),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=assertions,
                ),
                depends_on=("q1",),
                node_type="post_calc",
            ),
        ),
        response_mode="analysis",
    )


def dataset_comparison_plan() -> QueryPlan:
    def source(node_id: str) -> PlanNode:
        return PlanNode(
            id=node_id,
            spec=QuerySpec(
                id=node_id,
                kind="mysql",
                subject="disease",
                view="list",
                metrics=(),
                dimensions=(),
                filters=(),
                output_projection=("problem_number",),
                order_by=(),
                limit=500,
                required_assertions=(),
            ),
        )

    return QueryPlan(
        question="比较两个巡检批次新增的病害",
        nodes=(
            source("before"),
            source("after"),
            PlanNode(
                id="pc_compare",
                spec=QuerySpec(
                    id="pc_compare",
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
        response_mode="analysis",
    )


def ordered_patrol_plan(view: str, limit: int | None) -> QueryPlan:
    return QueryPlan(
        question="按巡检里程从多到少列出所有有记录的设备",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="patrol",
                    view=view,
                    metrics=("patrol_mileage",),
                    dimensions=("equipment_id",),
                    filters=(),
                    output_projection=("equipment_id",),
                    order_by=(
                        {"field": "patrol_mileage", "direction": "desc"},
                    ),
                    limit=limit,
                    required_assertions=("preserve_user_scope",),
                ),
            ),
        ),
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
    def test_ordered_complete_result_is_repaired_from_top_groups_to_distribution(
        self,
        analyze_once,
        _audit,
    ):
        analyze_once.side_effect = [
            ordered_patrol_plan("top_groups", None),
            ordered_patrol_plan("distribution", None),
        ]

        plan = analyze_single_turn(
            "2026 年 10 月期间，每台巡检设备分别完成了多少公里的巡检里程？"
            "请按里程从多到少排序，并列出所有有记录的设备。",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="patrol",
        )

        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[4]
        self.assertIn("TopN", feedback)
        self.assertIn("distribution", feedback)
        self.assertEqual(plan.nodes[0].spec.view, "distribution")
        self.assertIsNone(plan.nodes[0].spec.limit)

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

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_post_calc_without_calculation_is_repaired_before_execution(
        self,
        analyze_once,
        _audit,
    ):
        """post_calc 既没有 calculation 也没有解释型断言时，规划期就反馈重排。

        以前的 `_query_plan_calculation_issue` 把这条判断写在
        `if calculation is None: continue` 后面，成了死代码：计划能过审计，
        只在执行期才报 calculation_required，用户看到的是一次白跑。
        """
        analyze_once.side_effect = [
            post_calc_plan(()),
            post_calc_plan(("analysis_only",)),
        ]

        plan = analyze_single_turn(
            "上海到昆明和二连浩特到广州哪条泛油更多",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(analyze_once.call_count, 2)
        feedback = analyze_once.call_args_list[1].args[4]
        self.assertIn('"failure_code": "calculation_required"', feedback)
        self.assertIn("analysis_only", feedback)
        self.assertEqual(plan.nodes[1].spec.required_assertions, ("analysis_only",))

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis._analyze_once")
    def test_post_calc_with_dataset_comparison_is_not_rejected(
        self,
        analyze_once,
        _audit,
    ):
        """dataset_comparison 是执行器真正实现的对比路径，规划期不能误杀。"""
        analyze_once.return_value = dataset_comparison_plan()

        plan = analyze_single_turn(
            "上海到昆明和二连浩特到广州哪条泛油更多",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(analyze_once.call_count, 1)
        self.assertEqual(
            plan.nodes[2].spec.required_assertions,
            ("dataset_comparison",),
        )


if __name__ == "__main__":
    unittest.main()
