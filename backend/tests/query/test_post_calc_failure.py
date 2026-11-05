import unittest
from unittest.mock import patch

from app.services import agent
from app.services.tools.big import post_calc


class PostCalcFailureTests(unittest.TestCase):
    def test_fallback_sentence_is_not_success(self):
        with patch(
            "app.services.agent.chat",
            side_effect=RuntimeError("model unavailable"),
        ):
            payload = agent._post_calc_payload(
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
