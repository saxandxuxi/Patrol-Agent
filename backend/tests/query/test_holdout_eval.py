import unittest

from evals.run_holdout_eval import run_case
from app.services import prompts


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

    def test_correct_unsupported_allows_blocked_dependents(self):
        result = run_case(
            {
                "id": "held-2b",
                "category": "时间",
                "question": "对比两次并不存在的巡检",
            },
            lambda _: {
                "answer": "台账中没有这两个巡检批次，无法对比。",
                "sections": [
                    {
                        "status": "unsupported",
                        "failure_code": "patrol_batch_not_resolved",
                    },
                    {
                        "status": "blocked",
                        "failure_code": "dependency_failed",
                    },
                ],
                "verification": {"status": "blocked"},
            },
            lambda _case, _payload, _doc: {
                "accepted": True,
                "verdict": "correct_unsupported",
                "failure_layer": "unsupported",
                "reason": "已核实的批次数量为 0，用户前提与台账冲突",
            },
            "业务文档",
        )

        self.assertTrue(result.ok)

    def test_correct_unsupported_still_rejects_runtime_errors(self):
        result = run_case(
            {
                "id": "held-2c",
                "category": "时间",
                "question": "对比两次巡检",
            },
            lambda _: {
                "answer": "执行异常。",
                "error": "RuntimeError: database unavailable",
                "sections": [{"status": "error"}],
                "verification": {"status": "error"},
            },
            lambda _case, _payload, _doc: {
                "accepted": True,
                "verdict": "correct_unsupported",
                "failure_layer": "unsupported",
                "reason": "错误地接受了运行时故障",
            },
            "业务文档",
        )

        self.assertFalse(result.ok)

    def test_judge_cannot_accept_an_execution_failure_contradiction(self):
        result = run_case(
            {"id": "held-3", "category": "病害", "question": "问题"},
            lambda _: {
                "answer": "无法可靠规划",
                "sections": [{"status": "unsupported"}],
                "verification": {"status": "accepted"},
            },
            lambda _case, _payload, _doc: {
                "accepted": True,
                "verdict": "execution_failure",
                "failure_layer": "compiler",
                "reason": "计划失败",
            },
            "业务文档",
        )
        self.assertFalse(result.ok)

    def test_judge_prompt_distinguishes_structured_batch_premise_mismatch(self):
        prompt = prompts.load("query_holdout_judge")

        self.assertIn(
            "lookup_scope=patrol_route_association_batch_lookup",
            prompt,
        )
        self.assertIn("resolved_batch_count=0", prompt)
        self.assertIn("用户前提与巡检台账冲突", prompt)
        self.assertIn("不能泛化成所有空批次", prompt)
