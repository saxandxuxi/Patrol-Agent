import unittest
from unittest.mock import patch

from app.query.contracts import AnswerPackage
from app.query.service import start_single_turn


def plan_payload(*, metric: str = "count", damage_type: str = "裂缝"):
    return {
        "final_goal": "统计病害数量",
        "intent_family": "count",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按病害类型统计数量",
                "subject": "disease",
                "view": "count",
                "metrics": [metric],
                "dimensions": [],
                "filters": [
                    {
                        "field": "problem_type_name",
                        "op": "=",
                        "raw_value": damage_type,
                    }
                ],
                "output_projection": ["count"],
                "order_by": [],
                "limit": None,
                "required_assertions": [],
                "depends_on": [],
                "inputs": [],
            }
        ],
    }


class CapabilityEndToEndTests(unittest.TestCase):
    @patch("app.query.service.field_grounding.ground")
    @patch("app.query.analysis.chat_json")
    def test_missing_views_with_unknown_terms_returns_clear_reason(
        self,
        chat_json,
        ground,
    ):
        """规划器说"能力覆盖不到"时必须把原因带给用户，而不是报内部错误。"""

        ground.return_value = {
            "subject": "disease",
            "table": "ai_problem",
            "columns": [],
        }
        chat_json.return_value = {
            "final_goal": "按300米分段统计病害数量",
            "intent_family": "distribution",
            "unknown_terms": [
                "当前只注册了 500 米和 1 公里两种分段粒度，可改用其中一种",
            ],
            "views": [],
        }

        result = start_single_turn(
            "长沙－芷江高速公路按每300米一段统计病害数量",
            providers=[],
        )

        self.assertIn("只注册了 500 米和 1 公里", result.answer.text)
        self.assertNotIn("缺少 views", result.answer.text)
        self.assertNotIn("换一种说法", result.answer.text)
        self.assertNotIn("切换模型重试", result.answer.text)

    @patch("app.query.service._execute_plan", return_value=AnswerPackage("ok"))
    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    @patch("app.query.service.field_grounding.ground")
    def test_crack_family_query_uses_registry_expansion(
        self,
        ground,
        chat_json,
        _audit,
        _execute,
    ):
        ground.return_value = {
            "subject": "disease",
            "table": "ai_problem",
            "columns": [],
        }
        chat_json.return_value = plan_payload()

        result = start_single_turn("裂缝有多少处", providers=[])

        filters = result.plan.nodes[0].spec.filters
        self.assertIn("横向裂缝", filters[0].storage_value)
        self.assertIn("纵向裂缝", filters[0].storage_value)
        self.assertEqual(filters[0].resolver, "entity_family:crack")

    @patch("app.query.service._execute_plan", return_value=AnswerPackage("ok"))
    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    @patch("app.query.service.field_grounding.ground")
    def test_fake_metric_is_repaired_before_compile(
        self,
        ground,
        chat_json,
        _audit,
        _execute,
    ):
        ground.return_value = {
            "subject": "disease",
            "table": "ai_problem",
            "columns": [],
        }
        chat_json.side_effect = [
            plan_payload(metric="disease_density"),
            plan_payload(metric="count"),
        ]

        result = start_single_turn("哪条路健康状况最好", providers=[])

        self.assertEqual(chat_json.call_count, 2)
        self.assertEqual(result.plan.nodes[0].spec.metrics, ("count",))
        self.assertIn(
            "allowed_values",
            chat_json.call_args_list[1].args[0][1]["content"],
        )

    @patch("app.query.service.field_grounding.ground")
    def test_planner_prompt_contains_closed_capability_view(self, ground):
        ground.return_value = {
            "subject": "disease",
            "table": "ai_problem",
            "columns": [],
        }
        captured = []

        def fake_chat_json(messages, **kwargs):
            captured.extend(messages)
            return plan_payload()

        with patch("app.query.analysis.chat_json", side_effect=fake_chat_json), patch(
            "app.query.analysis.audit_query_plan",
            return_value={"complete": True},
        ), patch(
            "app.query.service._execute_plan",
            return_value=AnswerPackage("ok"),
        ):
            start_single_turn("裂缝有多少处", providers=[])

        prompt = "\n".join(item["content"] for item in captured)
        capabilities = prompt.split("当前主体能力视图", 1)[1]
        self.assertIn('"enum_values"', capabilities)
        self.assertIn('"metrics"', capabilities)
        self.assertIn('"derived_metrics"', capabilities)
        self.assertIn('"calculators"', capabilities)


if __name__ == "__main__":
    unittest.main()
