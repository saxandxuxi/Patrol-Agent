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
