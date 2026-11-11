import unittest
from decimal import Decimal
from unittest.mock import patch

from app.calculations.domain.analysis import analyze_evidence
from app.calculations.columns import calculate_columns
from app.calculations.domain.trend import render_series_calculation
from app.calculations.evidence import compact_table_evidence
from app.calculations.executor import execute_calculation
from app.calculations.group_change import calculate_group_change
from app.calculations.models import (
    CalculationOperand,
    CalculationOutput,
    CalculationRequest,
)
from app.calculations.numbers import as_decimal, numeric_value
from app.calculations.post_calc import execute_post_calc
from app.calculations.registry import CalculationRegistry
from app.calculations.scalar import calculate_scalar
from app.calculations.series import calculate_series


def operand(name: str, value: object) -> CalculationOperand:
    return CalculationOperand(name=name, value=value)


def scalar_request(operation: str, **values: object) -> CalculationRequest:
    roles = {
        "add": ("left", "right"),
        "subtract": ("left", "right"),
        "multiply": ("left", "right"),
        "divide": ("numerator", "denominator"),
        "ratio": ("numerator", "denominator"),
        "percentage_share": ("numerator", "total"),
        "percentage_change": ("old", "new"),
    }[operation]
    return CalculationRequest(
        tool="calculate_scalar",
        operation=operation,
        operands=tuple(operand(role, values[role]) for role in roles),
    )


class ScalarCalculationTests(unittest.TestCase):
    def test_scalar_add_subtract_multiply_divide(self):
        self.assertEqual(
            calculate_scalar(
                scalar_request("add", left=2, right=3)
            ).value,
            Decimal("5"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("subtract", left=5, right=3)
            ).value,
            Decimal("2"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("multiply", left=4, right=3)
            ).value,
            Decimal("12"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("divide", numerator=12, denominator=4)
            ).value,
            Decimal("3"),
        )

    def test_scalar_zero_denominator_is_structured_failure(self):
        result = calculate_scalar(
            scalar_request("divide", numerator=12, denominator=0)
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_zero_denominator",
        )

    def test_percentage_share_and_change_use_named_roles(self):
        share = calculate_scalar(
            scalar_request(
                "percentage_share",
                numerator=25,
                total=200,
            )
        )
        change = calculate_scalar(
            scalar_request("percentage_change", old=100, new=80)
        )

        self.assertEqual(share.value, Decimal("12.5"))
        self.assertEqual(change.value, Decimal("-20"))

    def test_unknown_operation_is_rejected(self):
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="eval",
            operands=(
                operand("left", 1),
                operand("right", 2),
            ),
        )

        result = calculate_scalar(request)

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_operation_unknown",
        )

    def test_registry_loads_closed_operation_set(self):
        registry = CalculationRegistry.load()

        self.assertIn("calculate_scalar", registry.snapshot.calculators)
        self.assertIn(
            "divide",
            registry.snapshot.calculators["calculate_scalar"].operations,
        )


class AnalysisPresentationRepairTests(unittest.TestCase):
    @patch("app.calculations.domain.analysis.chat")
    def test_analysis_only_rewrites_ungrounded_numbers_once(self, chat):
        chat.side_effect = [
            (
                "上海-昆明最高发段为 K1402+000~K1403+000（20处），"
                "包头-茂名最高发段为 K2179+000~K2180+000（154处），"
                "包头高出134处。"
            ),
            (
                "两条路线最高发段位置不同：上海-昆明在 "
                "K1402+000~K1403+000（20处），包头-茂名在 "
                "K2179+000~K2180+000（154处）；包头最高段处数更高。"
            ),
        ]
        dependencies = [
            {
                "question": "上海-昆明高速病害最多的3个公里段",
                "rows": [
                    {"桩号分段": "K1402+000~K1403+000", "数量": "20"},
                    {"桩号分段": "K1411+000~K1412+000", "数量": "14"},
                    {"桩号分段": "K1416+000~K1417+000", "数量": "10"},
                ],
            },
            {
                "question": "包头-茂名高速病害最多的3个公里段",
                "rows": [
                    {"桩号分段": "K2179+000~K2180+000", "数量": "154"},
                    {"桩号分段": "K2180+000~K2181+000", "数量": "138"},
                    {"桩号分段": "K2178+000~K2179+000", "数量": "116"},
                ],
            },
        ]

        result = analyze_evidence(
            "对比两条路线高发段的位置差异",
            dependencies,
            requirements={
                "required_assertions": [
                    "analysis_only",
                    "multi_metric_comparison",
                ],
            },
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(chat.call_count, 2)
        self.assertIn("位置不同", result["reply"])

    def test_precision_zero_is_preserved(self):
        request = scalar_request("add", left=1, right=2)
        request = CalculationRequest(
            tool=request.tool,
            operation=request.operation,
            operands=request.operands,
            precision=0,
        )

        result = calculate_scalar(request)

        self.assertEqual(result.value.as_tuple().exponent, 0)

    def test_invalid_precision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "precision"):
            CalculationRequest.from_dict(
                {
                    "tool": "calculate_scalar",
                    "operation": "add",
                    "operands": [],
                    "precision": "bad",
                }
            )

    def test_infinity_is_not_a_valid_number(self):
        self.assertIsNone(as_decimal("Infinity"))
        self.assertIsNone(as_decimal("NaN"))

    def test_numeric_value_uses_exact_field_name(self):
        value = numeric_value(
            {
                "rows": [
                    {
                        "count_total": "99",
                        "count": "7",
                    }
                ]
            },
            "count",
        )

        self.assertEqual(value, Decimal("7"))

    def test_scalar_source_row_index_reads_the_ordered_row(self):
        task = {
            "id": "gap",
            "question": "第一条路线比第二条多多少处",
            "depends_on": ["routes"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="subtract",
            operands=(
                CalculationOperand(
                    name="left",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 0,
                    },
                ),
                CalculationOperand(
                    name="right",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 1,
                    },
                ),
            ),
            output=CalculationOutput(name="count_gap", label="数量差值"),
            precision=0,
        )

        payload = execute_calculation(
            task,
            request,
            {
                "routes": {
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "数量",
                    },
                    "rows": [
                        {"路线名称": "路线A", "数量": "7"},
                        {"路线名称": "路线B", "数量": "3"},
                    ],
                }
            },
        )

        self.assertIn("数量差值 = 4", str(payload["reply"]))

    def test_scalar_selected_row_caption_omits_other_metrics(self):
        task = {
            "id": "gap",
            "question": "两条路线的病害总处数相差多少",
            "depends_on": ["routes"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="subtract",
            operands=(
                CalculationOperand(
                    name="left",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 0,
                    },
                ),
                CalculationOperand(
                    name="right",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 1,
                    },
                ),
            ),
            output=CalculationOutput(name="count_gap", label="病害总处数差值"),
            precision=0,
        )

        payload = execute_calculation(
            task,
            request,
            {
                "routes": {
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "数量",
                        "total_area": "总面积",
                    },
                    "rows": [
                        {
                            "路线名称": "路线A",
                            "数量": "7",
                            "总面积": "70",
                        },
                        {
                            "路线名称": "路线B",
                            "数量": "3",
                            "总面积": "30",
                        },
                    ],
                },
            },
        )

        reply = str(payload["reply"])
        self.assertIn("路线A", reply)
        self.assertIn("路线B", reply)
        self.assertNotIn("70", reply)
        self.assertNotIn("30", reply)

    def test_scalar_source_row_index_out_of_range_is_structured_failure(self):
        task = {
            "id": "gap",
            "question": "选择不存在的行",
            "depends_on": ["routes"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="subtract",
            operands=(
                CalculationOperand(
                    name="left",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 0,
                    },
                ),
                CalculationOperand(
                    name="right",
                    source={
                        "node": "routes",
                        "field": "count",
                        "row_index": 5,
                    },
                ),
            ),
            output=CalculationOutput(name="count_gap", label="数量差值"),
        )

        payload = execute_calculation(
            task,
            request,
            {
                "routes": {
                    "field_labels": {"count": "数量"},
                    "rows": [{"数量": "7"}],
                }
            },
        )

        self.assertFalse(payload["accepted"])
        self.assertEqual(
            payload["failure"]["failure_code"],
            "calculation_operand_missing",
        )

    def test_scalar_zero_denominator_failure_keeps_operand_meaning(self):
        task = {
            "id": "avg_per_task",
            "question": "计算平均每次任务检出病害数",
            "depends_on": ["tasks"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="divide",
            operands=(
                CalculationOperand(name="numerator", value=0),
                CalculationOperand(
                    name="denominator",
                    source={"node": "tasks", "field": "count"},
                ),
            ),
            output=CalculationOutput(
                name="avg_per_task",
                label="平均每次任务检出病害数",
            ),
        )

        payload = execute_calculation(
            task,
            request,
            {
                "tasks": {
                    "question": "统计任务数量",
                    "field_labels": {"count": "数量"},
                    "rows": [{"数量": "0"}],
                }
            },
        )

        self.assertFalse(payload["accepted"])
        calculation = payload["failure"]["evidence"]["calculation"]
        denominator = next(
            item
            for item in calculation["operands"]
            if item["name"] == "denominator"
        )
        self.assertEqual(denominator["label"], "统计任务数量")
        self.assertEqual(calculation["zero_operand"], "denominator")

    def test_dataset_comparison_uses_declared_entity_dependencies(self):
        task = {
            "id": "compare",
            "question": "比较最近两次巡检之间的新增病害",
            "depends_on": [
                "patrol_before",
                "patrol_after",
                "disease_before",
                "disease_after",
            ],
            "comparison_depends_on": [
                "disease_before",
                "disease_after",
            ],
            "required_assertions": ["dataset_comparison"],
        }
        payload = execute_post_calc(
            task,
            {
                "patrol_before": {
                    "rows": [
                        {
                            "patrol_number": "P1",
                            "create_time": "2026-10-27 09:00:00",
                        }
                    ],
                },
                "patrol_after": {
                    "rows": [
                        {
                            "patrol_number": "P2",
                            "create_time": "2026-10-31 09:00:00",
                        }
                    ],
                },
                "disease_before": {
                    "rows": [
                        {"problem_number": "D1"},
                        {"problem_number": "D2"},
                    ],
                },
                "disease_after": {
                    "rows": [
                        {"problem_number": "D2"},
                        {"problem_number": "D3"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("problem_number", str(payload["reply"]))
        self.assertNotIn("create_time", str(payload["reply"]))
        self.assertEqual(
            {row["problem_number"] for row in payload["rows"]},
            {"D1", "D3"},
        )

    @patch("app.calculations.post_calc.analyze_evidence")
    def test_dataset_comparison_with_analysis_only_uses_both_contracts(
        self,
        analyze_evidence,
    ):
        task = {
            "id": "compare",
            "question": "比较两次巡检新增病害，并给出两次总数和新增桩号",
            "depends_on": [
                "disease_before",
                "disease_after",
                "count_before",
                "count_after",
            ],
            "comparison_depends_on": [
                "disease_before",
                "disease_after",
            ],
            "required_assertions": [
                "dataset_comparison",
                "analysis_only",
            ],
        }
        analyze_evidence.return_value = {
            "reply": (
                "前一次 0 处，后一次 16 处，新增 16 处；"
                "新增病害位于 K85+993。"
            ),
            "source": "calc",
            "engine": "post_calc",
            "accepted": True,
            "columns": [],
            "rows": [],
        }

        payload = execute_post_calc(
            task,
            {
                "disease_before": {
                    "spec": {
                        "output_projection": [
                            "problem_number",
                            "pile_number",
                        ]
                    },
                    "rows": [],
                },
                "disease_after": {
                    "spec": {
                        "output_projection": [
                            "problem_number",
                            "pile_number",
                        ]
                    },
                    "rows": [
                        {
                            "problem_number": "D1",
                            "pile_number": "K85+993",
                        }
                    ],
                },
                "count_before": {
                    "rows": [{"病害数量": "0"}],
                    "field_labels": {"count": "病害数量"},
                },
                "count_after": {
                    "rows": [{"病害数量": "16"}],
                    "field_labels": {"count": "病害数量"},
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("新增 16", payload["reply"])
        self.assertIn("K85+993", payload["reply"])
        self.assertEqual(payload["rows"][0]["差异类型"], "新增")
        analyzed_dependencies = analyze_evidence.call_args.args[1]
        self.assertTrue(
            any(
                "新增 1" in str(item.get("reply") or "")
                for item in analyzed_dependencies
            )
        )
        self.assertTrue(
            any(
                item.get("rows") == [{"病害数量": "16"}]
                for item in analyzed_dependencies
            )
        )

    @patch("app.calculations.post_calc.analyze_evidence")
    def test_dataset_comparison_does_not_drop_failed_analysis_contract(
        self,
        analyze_evidence,
    ):
        task = {
            "id": "compare",
            "question": "比较两次巡检新增病害，并给出新增桩号",
            "depends_on": ["before", "after"],
            "comparison_depends_on": ["before", "after"],
            "required_assertions": [
                "dataset_comparison",
                "analysis_only",
            ],
        }
        analyze_evidence.return_value = {
            "reply": "analysis_only 不能生成新的数值。",
            "source": "calc",
            "engine": "post_calc",
            "accepted": False,
            "failure_code": "analysis_only_new_numeric_value",
            "failure_reason": "analysis_only 只能复述上游已有数值。",
            "ungrounded_numbers": ["1"],
            "columns": [],
            "rows": [],
        }

        payload = execute_post_calc(
            task,
            {
                "before": {
                    "spec": {
                        "output_projection": [
                            "problem_number",
                            "pile_number",
                        ]
                    },
                    "rows": [],
                },
                "after": {
                    "spec": {
                        "output_projection": [
                            "problem_number",
                            "pile_number",
                        ]
                    },
                    "rows": [
                        {
                            "problem_number": "FY-1",
                            "pile_number": "K1+000",
                        }
                    ],
                },
            },
        )

        self.assertFalse(payload["accepted"])
        self.assertEqual(
            payload["failure"]["failure_code"],
            "analysis_only_new_numeric_value",
        )
        self.assertEqual(payload["rows"], [])
        self.assertNotIn("新增 1", payload["reply"])

    def test_datetime_interval_uses_registered_tool(self):
        payload = execute_post_calc(
            {
                "id": "interval",
                "question": "最近两次巡检相隔几天",
                "depends_on": ["earlier_patrol", "later_patrol"],
                "required_assertions": ["preserve_user_scope"],
                "calculation": {
                    "tool": "calculate_datetime_interval",
                    "operation": "elapsed_days",
                    "operands": [
                        {
                            "name": "earlier",
                            "source": {
                                "node": "earlier_patrol",
                                "field": "create_time",
                                "row_index": 0,
                            },
                        },
                        {
                            "name": "later",
                            "source": {
                                "node": "later_patrol",
                                "field": "create_time",
                                "row_index": 0,
                            },
                        },
                    ],
                    "output": {
                        "name": "interval_days",
                        "label": "巡检间隔",
                        "unit": "天",
                    },
                    "precision": 0,
                },
            },
            {
                "earlier_patrol": {
                    "rows": [
                        {"create_time": "2026-10-27 09:36:19"},
                    ],
                },
                "later_patrol": {
                    "rows": [
                        {"create_time": "2026-10-31 12:29:11"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertIn("巡检间隔 = 4 天", str(payload["reply"]))


class ColumnCalculationTests(unittest.TestCase):
    def test_column_division_preserves_row_context(self):
        result = calculate_columns(
            "divide",
            [
                {
                    "id": "A",
                    "route_name": "路线A",
                    "count": 10,
                    "length_km": 5,
                }
            ],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
            precision=4,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.rows[0]["id"], "A")
        self.assertEqual(result.rows[0]["route_name"], "路线A")
        self.assertEqual(result.rows[0]["density"], Decimal("2.0000"))

    def test_column_zero_denominator_reports_row(self):
        result = calculate_columns(
            "divide",
            [{"id": "A", "count": 1, "length_km": 0}],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_zero_denominator",
        )
        self.assertEqual(result.evidence["row_ids"], ["A"])

    def test_column_missing_role_column_is_structured_failure(self):
        result = calculate_columns(
            "divide",
            [{"id": "A", "count": 1}],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_operand_missing",
        )

    def test_column_skip_rows_reports_excluded_rows(self):
        result = calculate_columns(
            "divide",
            [
                {"id": "A", "count": 10, "length_km": 5},
                {"id": "B", "count": 3, "length_km": 0},
            ],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
            zero_denominator="skip_rows",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(
            result.evidence["excluded_row_ids"],
            ["B"],
        )

    def test_column_percentage_share_accepts_constant_total(self):
        result = calculate_columns(
            "percentage_share",
            [
                {"route_name": "A", "count": 10},
                {"route_name": "B", "count": 30},
            ],
            {
                "numerator": "count",
                "total": "count",
            },
            "share",
            constants={"total": Decimal("40")},
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["share"] for row in result.rows],
            [Decimal("25.000000"), Decimal("75.000000")],
        )

    def test_column_percentage_share_accepts_constant_without_column_role(self):
        """总数来自独立聚合节点时，分母是常量，不应再要求同名逐行列。"""

        result = calculate_columns(
            "percentage_share",
            [
                {"route_name": "A", "count": 10},
                {"route_name": "B", "count": 30},
            ],
            {"numerator": "count"},
            "share",
            constants={"total": Decimal("40")},
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["share"] for row in result.rows],
            [Decimal("25.000000"), Decimal("75.000000")],
        )


class ScalarDifferenceRenderingTests(unittest.TestCase):
    """差值要给出“谁更多、多多少”，不能只甩一条可能为负的公式。"""

    def _execute(
        self,
        left: dict[str, object],
        right: dict[str, object],
    ) -> dict[str, object]:
        task = {
            "id": "pc",
            "question": "比较两侧的病害数量",
            "depends_on": ["q1", "q2"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="subtract",
            operands=(
                CalculationOperand(
                    name="left",
                    source={"node": "q1", "field": "count"},
                ),
                CalculationOperand(
                    name="right",
                    source={"node": "q2", "field": "count"},
                ),
            ),
            output=CalculationOutput(name="count_change", label="数量差值"),
        )
        return execute_calculation(
            task,
            request,
            {"q1": dict(left), "q2": dict(right)},
        )

    def test_difference_reports_magnitude_and_winner(self):
        payload = self._execute(
            {
                "question": "统计行政等级为'国道'的病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "10872"}],
            },
            {
                "question": "统计行政等级为'省道'的病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "24017"}],
            },
        )

        reply = str(payload["reply"])
        self.assertIn("数量差值 = 13145", reply)
        self.assertIn("'省道'", reply)
        self.assertIn(
            "少 13145",
            reply,
        )
        self.assertNotIn("-13145", reply)

    def test_difference_without_labels_still_reports_positive_gap(self):
        payload = self._execute(
            {"field_labels": {"count": "数量"}, "rows": [{"数量": "3"}]},
            {"field_labels": {"count": "数量"}, "rows": [{"数量": "9"}]},
        )

        reply = str(payload["reply"])
        self.assertIn("相差 6", reply)
        self.assertNotIn("-6", reply)

    def test_difference_states_direction_from_left_to_right(self):
        payload = self._execute(
            {
                "question": "统计2026年10月病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "390"}],
            },
            {
                "question": "统计2026年11月病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "0"}],
            },
        )

        reply = str(payload["reply"])
        self.assertIn("390", reply)
        self.assertIn(
            "统计2026年10月病害数量比统计2026年11月病害数量多 390",
            reply,
        )
        self.assertNotIn("后者比前者", reply)
        self.assertNotIn("-390", reply)

    def test_difference_names_both_periods_without_ambiguous_pronouns(self):
        payload = self._execute(
            {
                "question": "统计本周的病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "14105"}],
            },
            {
                "question": "统计上周的病害数量",
                "field_labels": {"count": "数量"},
                "rows": [{"数量": "18677"}],
            },
        )

        reply = str(payload["reply"])
        self.assertIn("统计本周的病害数量", reply)
        self.assertIn("统计上周的病害数量", reply)
        self.assertIn(
            "统计本周的病害数量比统计上周的病害数量少 4572",
            reply,
        )
        self.assertNotIn("后者比前者", reply)

    def test_multiply_keeps_plain_formula(self):
        task = {
            "id": "pc",
            "question": "计算乘积",
            "depends_on": ["q1", "q2"],
        }
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="multiply",
            operands=(
                CalculationOperand(
                    name="left",
                    source={"node": "q1", "field": "count"},
                ),
                CalculationOperand(
                    name="right",
                    source={"node": "q2", "field": "count"},
                ),
            ),
            output=CalculationOutput(name="product", label="乘积"),
        )

        payload = execute_calculation(
            task,
            request,
            {
                "q1": {
                    "field_labels": {"count": "数量"},
                    "rows": [{"数量": "4"}],
                },
                "q2": {
                    "field_labels": {"count": "数量"},
                    "rows": [{"数量": "3"}],
                },
            },
        )

        self.assertEqual(
            str(payload["reply"]),
            "乘积 = 12.000000（4 * 3）。",
        )


class ProportionRenderingTests(unittest.TestCase):
    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=AssertionError("ratio rendering must not use the LLM"),
    )
    def test_ratio_rendering_scales_percent_and_cheng_from_the_same_value(
        self,
        _chat,
    ):
        payload = execute_calculation(
            {
                "id": "q2",
                "question": "按病害类型统计数量及占比",
                "depends_on": ["q1"],
                "required_assertions": ["preserve_user_scope"],
            },
            CalculationRequest(
                tool="calculate_columns",
                operation="ratio",
                operands=(
                    CalculationOperand(
                        name="numerator",
                        source={"node": "q1", "field": "count"},
                    ),
                    CalculationOperand(
                        name="denominator",
                        source={"node": "total", "field": "count"},
                    ),
                ),
                output=CalculationOutput(
                    name="ratio",
                    label="占比",
                    unit="成",
                ),
                precision=4,
            ),
            {
                "q1": {
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "count": "数量",
                    },
                    "rows": [
                        {"病害类型": "条状修补", "数量": "512"},
                        {"病害类型": "横向裂缝", "数量": "43"},
                        {"病害类型": "抛洒物", "数量": "24"},
                    ],
                },
                "total": {
                    "field_labels": {"count": "总数"},
                    "rows": [{"总数": "669"}],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        reply = str(payload["reply"])
        self.assertIn("条状修补", reply)
        self.assertIn("76.53%", reply)
        self.assertIn("7.65成", reply)
        self.assertIn("横向裂缝", reply)
        self.assertIn("6.43%", reply)
        self.assertIn("0.64成", reply)
        self.assertIn("抛洒物", reply)
        self.assertIn("3.59%", reply)
        self.assertIn("0.36成", reply)

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=AssertionError(
            "divide with a cheng output must not use the LLM"
        ),
    )
    def test_divide_with_cheng_output_uses_proportion_rendering(self, _chat):
        payload = execute_calculation(
            {
                "id": "q3",
                "question": "按病害类型计算各自占总数几成",
                "depends_on": ["q2", "total"],
                "required_assertions": ["preserve_user_scope"],
            },
            CalculationRequest(
                tool="calculate_columns",
                operation="divide",
                operands=(
                    CalculationOperand(
                        name="numerator",
                        source={"node": "q2", "field": "count"},
                    ),
                    CalculationOperand(
                        name="denominator",
                        source={"node": "total", "field": "count"},
                    ),
                ),
                output=CalculationOutput(
                    name="share",
                    label="占比",
                    unit="成",
                ),
                precision=4,
            ),
            {
                "q2": {
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "count": "数量",
                    },
                    "rows": [
                        {"病害类型": "条状修补", "数量": "512"},
                        {"病害类型": "横向裂缝", "数量": "43"},
                        {"病害类型": "抛洒物", "数量": "24"},
                    ],
                },
                "total": {
                    "field_labels": {"count": "总数"},
                    "rows": [{"总数": "669"}],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        reply = str(payload["reply"])
        self.assertIn("76.53%", reply)
        self.assertIn("7.65成", reply)
        self.assertIn("6.43%", reply)
        self.assertIn("0.64成", reply)
        self.assertIn("3.59%", reply)
        self.assertIn("0.36成", reply)

    @patch(
        "app.calculations.domain.analysis.chat",
        side_effect=AssertionError("percentage rendering must not use the LLM"),
    )
    def test_percentage_share_rendering_does_not_scale_twice(self, _chat):
        payload = execute_calculation(
            {
                "id": "q2",
                "question": "按路线统计数量占比",
                "depends_on": ["q1"],
                "required_assertions": ["preserve_user_scope"],
            },
            CalculationRequest(
                tool="calculate_columns",
                operation="percentage_share",
                operands=(
                    CalculationOperand(
                        name="numerator",
                        source={"node": "q1", "field": "count"},
                    ),
                    CalculationOperand(
                        name="total",
                        source={"node": "total", "field": "count"},
                    ),
                ),
                output=CalculationOutput(
                    name="share",
                    label="占比",
                    unit="%",
                ),
                precision=2,
            ),
            {
                "q1": {
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "数量",
                    },
                    "rows": [
                        {"路线名称": "路线A", "数量": "10"},
                        {"路线名称": "路线B", "数量": "30"},
                    ],
                },
                "total": {
                    "field_labels": {"count": "总数"},
                    "rows": [{"总数": "40"}],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        reply = str(payload["reply"])
        self.assertIn("路线A", reply)
        self.assertIn("25%", reply)
        self.assertIn("2.5成", reply)
        self.assertIn("路线B", reply)
        self.assertIn("75%", reply)
        self.assertIn("7.5成", reply)
        self.assertNotIn("2500%", reply)
        self.assertNotIn("7500%", reply)


class SeriesCalculationTests(unittest.TestCase):
    def test_series_preserves_zero_batches_and_computes_differences(self):
        result = calculate_series(
            "subtract",
            ["b1", "b2", "b3"],
            [0, 3, 15],
        )

        self.assertTrue(result.accepted)
        self.assertEqual([row["value"] for row in result.rows], [0, 3, 15])
        self.assertEqual(
            [row["difference"] for row in result.rows],
            [None, 3, 12],
        )

    def test_series_percentage_change_uses_previous_value(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [10, 12, 9],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["percentage_change"] for row in result.rows],
            [None, Decimal("20"), Decimal("-25")],
        )

    def test_series_percentage_change_marks_zero_baseline_as_undefined(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [0, 15, 3],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["percentage_change"] for row in result.rows],
            [None, None, Decimal("-80")],
        )
        self.assertEqual(result.evidence["undefined_points"], ("b2",))

    def test_series_render_explains_zero_baseline_points(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [0, 15, 3],
        )
        request = CalculationRequest.from_dict(
            {
                "tool": "calculate_series",
                "operation": "percentage_change",
            }
        )

        reply = render_series_calculation(result, request)

        self.assertIn("b2 的上一批次为 0", reply)
        self.assertIn("相邻变化为：无，-80", reply)

    def test_compact_evidence_extracts_metric_boundaries(self):
        evidence = compact_table_evidence(
            [
                {"路线": "A", "数量": 10},
                {"路线": "B", "数量": 3},
            ]
        )

        self.assertEqual(evidence["metrics"]["数量"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["数量"]["min"]["group"], "B")


class GroupChangeCalculationTests(unittest.TestCase):
    def test_group_change_aligns_rows_and_treats_missing_groups_as_zero(self):
        result = calculate_group_change(
            "subtract",
            [
                {"病害类型": "裂缝", "数量": 3},
                {"病害类型": "坑槽", "数量": 2},
            ],
            [
                {"病害类型": "裂缝", "数量": 1},
                {"病害类型": "泛油", "数量": 4},
            ],
            label_field="病害类型",
            before_field="数量",
            after_field="数量",
            output_field="增加数量",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [
                (
                    row["病害类型"],
                    row["前次值"],
                    row["后次值"],
                    row["增加数量"],
                )
                for row in result.rows
            ],
            [
                ("裂缝", 3, 1, -2),
                ("坑槽", 2, 0, -2),
                ("泛油", 0, 4, 4),
            ],
        )
        self.assertEqual(result.evidence["missing_before"], ("泛油",))
        self.assertEqual(result.evidence["missing_after"], ("坑槽",))

    def test_group_change_executor_reports_max_increase(self):
        request = CalculationRequest(
            tool="calculate_group_change",
            operation="subtract",
            operands=(
                CalculationOperand(
                    name="labels",
                    source={
                        "node": "before",
                        "field": "problem_type_name",
                    },
                ),
                CalculationOperand(
                    name="before",
                    source={"node": "before", "field": "count"},
                ),
                CalculationOperand(
                    name="after",
                    source={"node": "after", "field": "count"},
                ),
            ),
            output=CalculationOutput(
                name="count_increase",
                label="增加数量",
                unit="处",
            ),
        )

        payload = execute_calculation(
            {
                "id": "compare",
                "question": "两次巡查相比，哪种病害增加最多？",
                "depends_on": ["before", "after"],
            },
            request,
            {
                "before": {
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "count": "数量",
                    },
                    "rows": [
                        {"病害类型": "裂缝", "数量": "3"},
                        {"病害类型": "坑槽", "数量": "2"},
                    ],
                },
                "after": {
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "count": "数量",
                    },
                    "rows": [
                        {"病害类型": "裂缝", "数量": "1"},
                        {"病害类型": "泛油", "数量": "4"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["calculation"]["tool"], "calculate_group_change")
        self.assertIn("泛油", payload["reply"])
        self.assertIn("增加", payload["reply"])
        self.assertIn("4", payload["reply"])
        self.assertEqual(payload["rows"][-1]["病害类型"], "泛油")
        self.assertEqual(
            as_decimal(payload["rows"][-1]["增加数量"]),
            Decimal("4"),
        )


if __name__ == "__main__":
    unittest.main()
