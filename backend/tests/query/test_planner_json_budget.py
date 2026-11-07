import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.audit import audit_query_plan
from app.query.fields import FieldReachSet, PhysicalSchemaView
from tests.query.factories import count_plan


class PlannerJsonBudgetTests(unittest.TestCase):
    @patch("app.query.analysis.chat_json")
    def test_planner_uses_long_output_budget(self, chat_json):
        chat_json.return_value = {
            "final_goal": "计数",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        analyze_single_turn(
            "病害一共多少处",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        self.assertEqual(chat_json.call_args.kwargs["max_tokens"], 4096)

    @patch("app.query.audit.chat_json", return_value={"complete": True, "issues": []})
    def test_plan_audit_uses_long_output_budget(self, chat_json):
        audit_query_plan(
            "展示过去8周每周病害趋势",
            count_plan(),
        )

        self.assertEqual(chat_json.call_args.kwargs["max_tokens"], 4096)
        prompt = "\n".join(
            item["content"] for item in chat_json.call_args.args[0]
        )
        self.assertIn("当前日期：", prompt)


if __name__ == "__main__":
    unittest.main()

