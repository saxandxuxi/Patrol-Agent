import unittest
from unittest.mock import patch

from app.calculations.post_calc import execute_post_calc
from app.services.tools.big import post_calc


class PostCalcFailureTests(unittest.TestCase):
    def test_fallback_sentence_is_not_success(self):
        with patch(
            "app.calculations.domain.analysis.chat",
            side_effect=RuntimeError("model unavailable"),
        ):
            payload = execute_post_calc(
                {"question": "计算密度", "depends_on": [1]},
                {1: {"rows": [], "columns": []}},
            )
            result = post_calc.post_calc(
                {"question": "计算密度", "depends_on": [1]},
                {1: {"rows": [], "columns": []}},
            )
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["source"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure"]["failure_code"], "post_calc_empty")

    def test_series_without_common_key_does_not_create_zero_series(self):
        payload = execute_post_calc(
            {
                "question": "计算变化",
                "view": "trend",
                "depends_on": ["q1", "q2"],
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {"node": "q1", "field": "patrol_number"},
                        },
                        {
                            "name": "values",
                            "source": {"node": "q2", "field": "count"},
                        },
                    ],
                },
            },
            {
                "q1": {
                    "field_labels": {"patrol_number": "任务编号"},
                    "rows": [{"任务编号": "P1"}],
                },
                "q2": {
                    "field_labels": {"count": "数量"},
                    "rows": [{"其他编号": "P1", "数量": "3"}],
                },
            },
        )

        self.assertFalse(payload["accepted"])
        self.assertEqual(
            payload["failure"]["failure_code"],
            "calculation_operand_missing",
        )

    def test_arithmetic_post_calc_without_calculation_is_rejected(self):
        payload = execute_post_calc(
            {
                "question": "计算变化",
                "view": "trend",
                "depends_on": ["q1"],
            },
            {"q1": {"rows": [{"月份": "10月", "数量": 1}]}},
        )

        self.assertFalse(payload["accepted"])
        self.assertEqual(
            payload["failure"]["failure_code"],
            "calculation_required",
        )

    def test_series_uses_label_field_to_join_different_source_nodes(self):
        payload = execute_post_calc(
            {
                "question": "最近三次巡查条状修补数量变化",
                "view": "trend",
                "depends_on": ["batches", "counts"],
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {
                                "node": "batches",
                                "field": "patrol_number",
                            },
                        },
                        {
                            "name": "values",
                            "source": {
                                "node": "counts",
                                "field": "count",
                            },
                        },
                    ],
                },
            },
            {
                "batches": {
                    "field_labels": {
                        "patrol_number": "任务编号",
                    },
                    "rows": [
                        {"任务编号": "P1"},
                        {"任务编号": "P2"},
                        {"任务编号": "P3"},
                    ],
                },
                "counts": {
                    "field_labels": {
                        "patrol_number": "任务编号",
                        "count": "数量",
                    },
                    "rows": [
                        {"任务编号": "P1", "数量": "3"},
                        {"任务编号": "P2", "数量": "15"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertEqual(
            [row["value"] for row in payload["rows"]],
            ["3", "15", "0"],
        )

    def test_series_resolves_node_qualified_field_names(self):
        payload = execute_post_calc(
            {
                "question": "最近三次巡查条状修补数量变化",
                "view": "trend",
                "depends_on": ["batches", "counts"],
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {
                                "node": "batches",
                                "field": "q1_patrols.patrol_number",
                            },
                        },
                        {
                            "name": "values",
                            "source": {
                                "node": "counts",
                                "field": "q2_counts.count",
                            },
                        },
                    ],
                },
            },
            {
                "batches": {
                    "field_labels": {
                        "patrol_number": "任务编号",
                    },
                    "rows": [
                        {"任务编号": "P1"},
                        {"任务编号": "P2"},
                    ],
                },
                "counts": {
                    "field_labels": {
                        "patrol_number": "任务编号",
                        "count": "数量",
                    },
                    "rows": [
                        {"任务编号": "P1", "数量": "3"},
                        {"任务编号": "P2", "数量": "15"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertEqual(
            [row["label"] for row in payload["rows"]],
            ["P1", "P2"],
        )
        self.assertEqual(
            [row["value"] for row in payload["rows"]],
            ["3", "15"],
        )

    def test_series_rejects_labels_that_miss_result_columns(self):
        payload = execute_post_calc(
            {
                "question": "最近三次巡查条状修补数量变化",
                "view": "trend",
                "depends_on": ["batches", "counts"],
                "calculation": {
                    "tool": "calculate_series",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "labels",
                            "source": {
                                "node": "batches",
                                "field": "unknown_field",
                            },
                        },
                        {
                            "name": "values",
                            "source": {
                                "node": "counts",
                                "field": "count",
                            },
                        },
                    ],
                },
            },
            {
                "batches": {
                    "field_labels": {"patrol_number": "任务编号"},
                    "rows": [{"任务编号": "P1"}, {"任务编号": "P2"}],
                },
                "counts": {
                    "field_labels": {"count": "数量"},
                    "rows": [{"数量": "3"}, {"数量": "15"}],
                },
            },
        )

        self.assertFalse(payload["accepted"])
        self.assertEqual(
            payload["failure"]["failure_code"],
            "calculation_operand_missing",
        )
        self.assertIn(
            "任务编号",
            str(payload["failure"].get("evidence")),
        )

    def test_column_calc_rejects_two_row_sources_with_hint(self):
        payload = execute_post_calc(
            {
                "question": "两次巡查各病害类型的增加量",
                "view": "distribution",
                "depends_on": ["oct26", "nov04"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "subtract",
                    "operands": [
                        {
                            "name": "left",
                            "source": {"node": "nov04", "field": "count"},
                        },
                        {
                            "name": "right",
                            "source": {"node": "oct26", "field": "count"},
                        },
                    ],
                },
            },
            {
                "oct26": {
                    "view": "distribution",
                    "field_labels": {"problem_type_name": "病害类型", "count": "数量"},
                    "rows": [{"病害类型": "纵向裂缝", "数量": "12"}],
                },
                "nov04": {
                    "view": "distribution",
                    "field_labels": {"problem_type_name": "病害类型", "count": "数量"},
                    "rows": [{"病害类型": "纵向裂缝", "数量": "20"}],
                },
            },
        )

        self.assertFalse(payload["accepted"])
        self.assertIn("calculate_series", payload["failure"]["reason"])

    def test_group_share_computes_total_when_planner_omits_it(self):
        payload = execute_post_calc(
            {
                "question": "每个季度各病害类型的占比",
                "view": "distribution",
                "depends_on": ["q3"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "percentage_share",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q3", "field": "count"},
                        },
                    ],
                    "output": {
                        "name": "share",
                        "label": "占比",
                        "unit": "%",
                    },
                },
            },
            {
                "q3": {
                    "view": "distribution",
                    "field_labels": {
                        "problem_type_name": "病害类型",
                        "count": "数量",
                    },
                    "rows": [
                        {"病害类型": "纵向裂缝", "数量": "501"},
                        {"病害类型": "标牌绿化遮挡", "数量": "501"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertEqual(
            [row["占比"] for row in payload["rows"]],
            ["50.000000", "50.000000"],
        )

    def test_group_share_uses_sum_of_group_counts_as_total(self):
        payload = execute_post_calc(
            {
                "question": "计算各路线的横向裂缝数量占比",
                "view": "distribution",
                "depends_on": ["q1"],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": "percentage_share",
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {
                                "node": "q1",
                                "field": "count",
                            },
                        },
                        {
                            "name": "total",
                            "source": {
                                "node": "q1",
                                "field": "count",
                            },
                        },
                    ],
                    "output": {
                        "name": "share",
                        "label": "占比",
                        "unit": "%",
                    },
                },
            },
            {
                "q1": {
                    "view": "distribution",
                    "field_labels": {
                        "route_name": "路线名称",
                        "count": "数量",
                    },
                    "rows": [
                        {"路线名称": "路线A", "数量": "10"},
                        {"路线名称": "路线B", "数量": "30"},
                    ],
                },
            },
        )

        self.assertTrue(payload["accepted"])
        self.assertEqual(
            [row["占比"] for row in payload["rows"]],
            ["25.000000", "75.000000"],
        )
