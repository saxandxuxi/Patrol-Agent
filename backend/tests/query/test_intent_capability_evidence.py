import unittest
from unittest.mock import patch

from app.services import intent


class IntentCapabilityEvidenceTests(unittest.TestCase):
    def setUp(self):
        intent._INTENT_CACHE.clear()

    def tearDown(self):
        intent._INTENT_CACHE.clear()

    @patch("app.services.intent.chat_json")
    @patch("app.services.field_grounding.ground")
    def test_intent_prompt_receives_unavailable_field_fact(
        self,
        ground,
        chat_json,
    ):
        question = (
            "2026年11月，怀化绕城高速上检出的病害，如果我想按历史损坏程度"
            "区间分组统计各区间分别有多少处、累计面积多大，系统现在能直接支持吗？"
            "如果不能，请说明具体原因。"
        )
        ground.return_value = {
            "table": "ai_problem",
            "subject": "problem",
            "tables": [
                {
                    "name": "ai_problem",
                    "score": 69.2,
                    "hits": ["表说明：病害记录"],
                }
            ],
            "columns": [
                {
                    "table": "ai_problem",
                    "column": "history_damage_interval",
                    "label": "历史损坏程度区间",
                    "comment": "历史损坏程度区间",
                    "score": 24.5,
                    "hits": ["列名命中：历史损坏程度区间"],
                }
            ],
        }
        chat_json.return_value = {
            "intent": "analysis",
            "reason": "用户在询问数据库字段的可用性及分组分析能力",
        }

        self.assertEqual(intent.classify_intent(question), "analysis")

        prompt_text = str(chat_json.call_args)
        self.assertIn("history_damage_interval", prompt_text)
        self.assertIn("unavailable", prompt_text)
        self.assertIn("字段当前全部为空", prompt_text)


if __name__ == "__main__":
    unittest.main()
