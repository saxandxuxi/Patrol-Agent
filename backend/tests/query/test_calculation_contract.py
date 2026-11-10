import unittest
from unittest.mock import patch

from app.query.analysis import (
    _normalize_group_share_calculation,
    _query_plan_calculation_issue,
    analyze_single_turn,
)
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import PlanNode, QueryPlan, QuerySpec, QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.semantic_operations import (
    GROUP_SHARE,
    semantic_operation_contract_issue,
)
from app.query.values import resolve_value


def calculation_payload(
    *,
    operation: str = "divide",
    denominator_field: str = "route_length_km",
) -> dict:
    return {
        "final_goal": "计算每条道路的病害密度",
        "intent_family": "analysis",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按路线统计数量和路线长度",
                "subject": "disease",
                "view": "distribution",
                "metrics": ["count", "route_length_km"],
                "dimensions": ["route_name"],
                "filters": [],
                "output_projection": ["route_name", "count", "route_length_km"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "density",
                "action": "post_calc",
                "question": "用数量除以路线长度计算病害密度",
                "subject": "disease",
                "view": "data",
                "metrics": ["disease_density"],
                "dimensions": ["route_name"],
                "filters": [],
                "output_projection": ["route_name", "disease_density"],
                "order_by": [],
                "limit": None,
                "required_assertions": [
                    "lower_is_better",
                    "multi_metric_comparison",
                ],
                "depends_on": ["q1"],
                "inputs": [],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": operation,
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1", "field": "count"},
                        },
                        {
                            "name": "denominator",
                            "source": {
                                "node": "q1",
                                "field": denominator_field,
                            },
                        },
                    ],
                    "output": {
                        "name": "disease_density",
                        "label": "病害密度",
                        "unit": "处/km",
                    },
                    "precision": 4,
                },
            },
        ],
    }


class CalculationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_post_calc_parses_structured_calculation(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload()

        plan = analyze_single_turn(
            "计算每条道路的病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        calculation = plan.nodes[-1].spec.calculation
        self.assertIsNotNone(calculation)
        self.assertEqual(calculation.tool, "calculate_columns")
        self.assertEqual(calculation.operation, "divide")
        self.assertEqual(
            [item.name for item in calculation.operands],
            ["numerator", "denominator"],
        )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_unknown_calculation_operation_is_rejected_before_execution(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload(operation="eval")

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_operation_unknown",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_calculation_output_is_allowed_as_post_calc_metric(
        self,
        chat_json,
        _audit,
    ):
        payload = calculation_payload()
        post_calc = payload["views"][1]
        post_calc["metrics"] = ["custom_ratio"]
        post_calc["calculation"]["output"]["name"] = "custom_ratio"
        chat_json.return_value = payload

        plan = analyze_single_turn(
            "计算每条道路的病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(
            plan.nodes[-1].spec.metrics,
            ("custom_ratio",),
        )

    def test_group_share_with_separate_total_uses_columns_calculation(self):
        from app.calculations.models import CalculationRequest

        groups = QuerySpec(
            id="top_types",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("problem_type_name",),
            filters=(),
            output_projection=("problem_type_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=3,
            required_assertions=(),
            question="按数量取前三种病害类型",
        )
        total = QuerySpec(
            id="total",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=("count",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="统计病害总数",
        )
        calculation = CalculationRequest.from_dict(
            {
                "tool": "calculate_scalar",
                "operation": "percentage_share",
                "operands": [
                    {
                        "name": "numerator",
                        "source": {
                            "node": "top_types",
                            "field": "count",
                            "row_index": 0,
                        },
                    },
                    {
                        "name": "total",
                        "source": {"node": "total", "field": "count"},
                    },
                ],
                "output": {
                    "name": "share",
                    "label": "占比",
                    "unit": "成",
                },
                "precision": 4,
            }
        )
        post = QuerySpec(
            id="shares",
            kind="mysql",
            subject="disease",
            view="data",
            metrics=("share",),
            dimensions=("problem_type_name",),
            filters=(),
            output_projection=("problem_type_name", "share"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="计算每种病害类型的占比",
            calculation=calculation,
        )
        plan = QueryPlan(
            question="前三种病害类型各占总数几成",
            nodes=(
                PlanNode(id="total", spec=total),
                PlanNode(id="top_types", spec=groups),
                PlanNode(
                    id="shares",
                    spec=post,
                    depends_on=("top_types", "total"),
                    node_type="post_calc",
                ),
            ),
        )

        normalized = _normalize_group_share_calculation(
            plan,
            answer_operation=GROUP_SHARE,
        )
        normalized_calculation = normalized.nodes[-1].spec.calculation

        self.assertIsNotNone(normalized_calculation)
        self.assertEqual(normalized_calculation.tool, "calculate_columns")
        self.assertEqual(
            normalized_calculation.operation,
            "percentage_share",
        )
        numerator = next(
            item
            for item in normalized_calculation.operands
            if item.name == "numerator"
        )
        self.assertNotIn("row_index", numerator.source)
        issue = semantic_operation_contract_issue(
            plan,
            GROUP_SHARE,
            self.registry,
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "group_share_requires_columns")

    def test_multi_metric_comparison_can_explain_existing_scalars(self):
        first = QuerySpec(
            id="left_count",
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
            question="统计第一种病害数量",
        )
        second = QuerySpec(
            id="right_count",
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
            question="统计第二种病害数量",
        )
        comparison = QuerySpec(
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
            question="比较两个已有数量，说明哪个更多并列出各自数值",
        )
        plan = QueryPlan(
            question="比较两个已有数量",
            nodes=(
                PlanNode(id="left_count", spec=first),
                PlanNode(id="right_count", spec=second),
                PlanNode(
                    id="compare",
                    spec=comparison,
                    depends_on=("left_count", "right_count"),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNone(issue)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_calculation_source_must_be_produced_by_dependency(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload(
            denominator_field="missing_length",
        )

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_operand_missing",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

    def test_named_business_value_resolves_from_registry(self):
        binding = resolve_value(
            "confidence",
            "<",
            "low_confidence",
            "AI识别的病害有多少认不出来",
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(binding.storage_value, 0.7)
        self.assertEqual(binding.source, "business_rule")
        self.assertEqual(
            binding.resolver,
            "business_value:confidence:low_confidence",
        )

    def test_series_tool_rejects_two_scalar_period_sources(self):
        first = QuerySpec(
            id="oct",
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
            question="10月数量",
        )
        second = QuerySpec(
            **{
                **first.__dict__,
                "id": "nov",
                "question": "11月数量",
            }
        )
        post = calculation_payload()["views"][1]
        post["calculation"] = {
            "tool": "calculate_series",
            "operation": "percentage_change",
            "operands": [
                {"name": "labels", "source": {"node": "oct", "field": "count"}},
                {"name": "values", "source": {"node": "nov", "field": "count"}},
            ],
        }
        spec = QuerySpec(
            id="change",
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
            question="计算两个月数量变化",
        )
        from app.calculations.models import CalculationRequest

        spec = QuerySpec(
            **{
                **spec.__dict__,
                "calculation": CalculationRequest.from_dict(
                    post["calculation"]
                ),
            }
        )
        plan = QueryPlan(
            question="计算两个月数量变化",
            nodes=(
                PlanNode(id="oct", spec=first),
                PlanNode(id="nov", spec=second),
                PlanNode(
                    id="change",
                    spec=spec,
                    depends_on=("oct", "nov"),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "calculation_tool_shape_mismatch")
        self.assertIn("calculate_scalar", issue.suggested_repair)

    def test_scalar_calculation_rejects_identical_source_twice(self):
        from app.calculations.models import CalculationRequest

        source = QuerySpec(
            id="months",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("count",),
            dimensions=("month",),
            filters=(),
            output_projection=("month", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="逐月任务数",
        )
        calculation = CalculationRequest.from_dict(
            {
                "tool": "calculate_scalar",
                "operation": "add",
                "operands": [
                    {
                        "name": "left",
                        "source": {"node": "months", "field": "count"},
                    },
                    {
                        "name": "right",
                        "source": {"node": "months", "field": "count"},
                    },
                ],
            }
        )
        post = QuerySpec(
            id="total",
            kind="mysql",
            subject="patrol",
            view="count",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="计算总数",
            calculation=calculation,
        )
        plan = QueryPlan(
            question="逐月任务与总数",
            nodes=(
                PlanNode(id="months", spec=source),
                PlanNode(
                    id="total",
                    spec=post,
                    depends_on=("months",),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "calculation_operand_missing")
        self.assertIn("相同的上游来源", issue.reason)

    def test_calculation_must_consume_every_declared_dependency(self):
        from app.calculations.models import CalculationRequest

        def scalar_source(node_id: str) -> QuerySpec:
            return QuerySpec(
                id=node_id,
                kind="mysql",
                subject="disease",
                view="count",
                metrics=("count", "sum"),
                dimensions=(),
                filters=(),
                output_projection=("count", "sum"),
                order_by=(),
                limit=None,
                required_assertions=(),
                question="方向病害数量与面积",
            )

        calculation = CalculationRequest.from_dict(
            {
                "tool": "calculate_scalar",
                "operation": "divide",
                "operands": [
                    {
                        "name": "numerator",
                        "source": {"node": "up", "field": "count"},
                    },
                    {
                        "name": "denominator",
                        "source": {"node": "up", "field": "sum"},
                    },
                ],
            }
        )
        post = QuerySpec(
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
            question="比较上行和下行",
            calculation=calculation,
        )
        plan = QueryPlan(
            question="比较上行和下行病害集中程度",
            nodes=(
                PlanNode(id="up", spec=scalar_source("up")),
                PlanNode(id="down", spec=scalar_source("down")),
                PlanNode(
                    id="compare",
                    spec=post,
                    depends_on=("up", "down"),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "calculation_dependency_unused")
        self.assertEqual(issue.node_id, "compare")
        self.assertIn("down", issue.reason)

    def test_scalar_calculation_can_select_distinct_rows_from_ordered_source(self):
        from app.calculations.models import CalculationRequest

        source = QuerySpec(
            id="routes",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("route_name",),
            filters=(),
            output_projection=("route_name", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=2,
            required_assertions=(),
            question="全路网病害数量前两条路线",
        )
        calculation = CalculationRequest.from_dict(
            {
                "tool": "calculate_scalar",
                "operation": "subtract",
                "operands": [
                    {
                        "name": "left",
                        "source": {
                            "node": "routes",
                            "field": "count",
                            "row_index": 0,
                        },
                    },
                    {
                        "name": "right",
                        "source": {
                            "node": "routes",
                            "field": "count",
                            "row_index": 1,
                        },
                    },
                ],
            }
        )
        post = QuerySpec(
            id="gap",
            kind="mysql",
            subject="disease",
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="第一条路线比第二条多多少处",
            calculation=calculation,
        )
        plan = QueryPlan(
            question="全路网病害最多的两条路相差多少",
            nodes=(
                PlanNode(id="routes", spec=source),
                PlanNode(
                    id="gap",
                    spec=post,
                    depends_on=("routes",),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNone(issue)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_scalar_comparison_completes_single_row_group_sources(
        self,
        chat_json,
        _audit,
    ):
        payload = {
            "final_goal": "比较两条路线的病害数量",
            "intent_family": "analysis",
            "answer_operation": "scalar_comparison",
            "unknown_terms": [],
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "统计长沙－芷江高速的病害数量与面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "长沙－芷江高速",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "q2",
                    "action": "query_data",
                    "question": "统计怀化绕城高速的病害数量与面积",
                    "subject": "disease",
                    "view": "distribution",
                    "metrics": ["count", "total_area"],
                    "dimensions": ["route_name"],
                    "filters": [
                        {
                            "field": "route_name",
                            "op": "=",
                            "raw_value": "怀化绕城高速",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                },
                {
                    "id": "compare",
                    "action": "post_calc",
                    "question": "比较两条路的病害数量差值",
                    "subject": "disease",
                    "view": "data",
                    "metrics": [],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["multi_metric_comparison"],
                    "depends_on": ["q1", "q2"],
                    "inputs": [],
                    "calculation": {
                        "tool": "calculate_scalar",
                        "operation": "subtract",
                        "operands": [
                            {
                                "name": "left",
                                "source": {
                                    "node": "q1",
                                    "field": "count",
                                },
                            },
                            {
                                "name": "right",
                                "source": {
                                    "node": "q2",
                                    "field": "count",
                                },
                            },
                        ],
                        "output": {
                            "name": "count_difference",
                            "label": "病害数量差值",
                            "unit": "处",
                        },
                        "precision": 0,
                    },
                },
            ],
        }
        chat_json.return_value = payload

        plan = analyze_single_turn(
            "比较长沙－芷江高速和怀化绕城高速的病害数量",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        by_id = {node.id: node for node in plan.nodes}
        self.assertEqual(
            by_id["q1"].spec.order_by,
            ({"field": "route_name", "direction": "asc"},),
        )
        self.assertEqual(
            by_id["q2"].spec.order_by,
            ({"field": "route_name", "direction": "asc"},),
        )
        operands = by_id["compare"].spec.calculation.operands
        self.assertEqual(
            [item.source.get("row_index") for item in operands],
            [0, 0],
        )
        self.assertIsNone(
            _query_plan_calculation_issue(plan, self.registry)
        )

    def test_scalar_calculation_rejects_grouping_key_as_denominator(self):
        from app.calculations.models import CalculationRequest

        source = QuerySpec(
            id="tasks",
            kind="mysql",
            subject="patrol",
            view="distribution",
            metrics=("count",),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number", "count"),
            order_by=(),
            limit=50,
            required_assertions=(),
            question="列出设备执行过的任务",
        )
        calculation = CalculationRequest.from_dict(
            {
                "tool": "calculate_scalar",
                "operation": "divide",
                "operands": [
                    {
                        "name": "numerator",
                        "value": 1,
                    },
                    {
                        "name": "denominator",
                        "source": {
                            "node": "tasks",
                            "field": "patrol_number",
                        },
                    },
                ],
            }
        )
        post = QuerySpec(
            id="average",
            kind="mysql",
            subject="patrol",
            view="data",
            metrics=(),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="计算平均每次任务检出的病害数量",
            calculation=calculation,
        )
        plan = QueryPlan(
            question="统计设备第三季度平均每次任务检出的病害数量",
            nodes=(
                PlanNode(id="tasks", spec=source),
                PlanNode(
                    id="average",
                    spec=post,
                    depends_on=("tasks",),
                    node_type="post_calc",
                ),
            ),
        )

        issue = _query_plan_calculation_issue(plan, self.registry)

        self.assertIsNotNone(issue)
        self.assertEqual(issue.code, "calculation_type_mismatch")
        self.assertIn("count", issue.suggested_repair)
        self.assertIn("分组", issue.reason)

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_duplicate_operand_roles_are_rejected(
        self,
        chat_json,
        _audit,
    ):
        payload = calculation_payload()
        operands = payload["views"][1]["calculation"]["operands"]
        operands.append(dict(operands[0]))
        chat_json.return_value = payload

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_operand_missing",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_non_object_calculation_is_rejected(
        self,
        chat_json,
        _audit,
    ):
        payload = calculation_payload()
        payload["views"][1]["calculation"] = "numerator / denominator"
        chat_json.return_value = payload

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_output_invalid",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )


if __name__ == "__main__":
    unittest.main()
