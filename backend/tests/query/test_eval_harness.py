import unittest

from evals.run_single_turn_eval import run_case


class EvalHarnessTests(unittest.TestCase):
    def test_explicit_failure_is_failure_for_solvable_case(self):
        result = run_case(
            {
                "id": "count",
                "question": "泛油有多少处？",
                "expected_status": "answered",
            },
            lambda _: {
                "answer": None,
                "verification": {"status": "repairable"},
            },
        )
        self.assertFalse(result.ok)

    def test_unsupported_case_requires_specific_reason(self):
        result = run_case(
            {
                "id": "missing",
                "question": "病害的坡度分布",
                "expected_status": "unsupported",
            },
            lambda _: {
                "answer": "系统暂不支持",
                "verification": {
                    "status": "unsupported",
                    "reason": "数据库没有坡度字段",
                },
            },
        )
        self.assertTrue(result.ok)
