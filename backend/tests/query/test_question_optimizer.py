import unittest

from app.services import nl2sql


class LegacyQuestionOptimizerTests(unittest.TestCase):
    def test_legacy_question_optimizer_is_disabled(self):
        with self.assertRaisesRegex(RuntimeError, "旧查询链路已停用"):
            nl2sql.expand_question_for_planning(
                "结合数据，你觉得养护资金应该投哪条路？"
            )
