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
