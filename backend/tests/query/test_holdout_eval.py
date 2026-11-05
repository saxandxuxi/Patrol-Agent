import unittest

from evals.run_holdout_eval import run_case


class HoldoutEvaluationTests(unittest.TestCase):
    def test_semantic_judge_verdict_is_the_only_pass_signal(self):
        case = {
            "id": "held-1",
            "category": "病害",
            "question": "上行方向的病害有多少处？",
        }
        payload = {
            "answer": "上下行合计 10 处。",
            "plan": object(),
            "sections": [{"status": "accepted", "sql": "SELECT 1"}],
        }
        result = run_case(
            case,
            lambda _: payload,
            lambda _case, _payload, _doc: {
                "accepted": False,
                "verdict": "wrong_scope",
                "failure_layer": "contract",
                "reason": "回答了上下行合计，没有回答上行方向",
            },
            "业务文档",
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.failure_layer, "contract")

    def test_dry_run_case_without_reference_words_can_pass_semantically(self):
        case = {
            "id": "held-2",
            "category": "管养",
            "question": "各管养单位病害数量排名",
        }
        payload = {
            "answer": "当前数据库没有可用的管养单位名称，无法完成排名。",
            "plan": object(),
            "sections": [{"status": "unsupported", "reason": "缺少单位名称"}],
        }
        result = run_case(
            case,
            lambda _: payload,
            lambda _case, _payload, _doc: {
                "accepted": True,
                "verdict": "correct_unsupported",
                "failure_layer": "",
                "reason": "明确说明了缺失的数据能力",
            },
            "业务文档",
        )
        self.assertTrue(result.ok)
