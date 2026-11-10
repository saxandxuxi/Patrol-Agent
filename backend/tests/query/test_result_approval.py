import unittest
from unittest.mock import patch

from app.routers.chat import ApproveRequest, approve_result


class ResultApprovalTests(unittest.TestCase):
    @patch("app.routers.chat._templatize_in_background")
    @patch("app.routers.chat.nl2sql.resolve_example_target", return_value=None)
    @patch("app.routers.chat.nl2sql.save_successful_pair", return_value=True)
    @patch("app.routers.chat.get_result")
    def test_single_section_approval_saves_original_question_and_sql(
        self,
        get_result,
        save_successful_pair,
        _resolve_example_target,
        _templatize,
    ):
        question = "泛油有多少处？"
        sql = "SELECT COUNT(*) FROM ai_problem"
        get_result.return_value = {
            "question": question,
            "sections": [
                {
                    "node_id": "q1",
                    "question": question,
                    "status": "accepted",
                    "sql": sql,
                }
            ],
        }

        result = approve_result(ApproveRequest(result_id="result-1"))

        self.assertTrue(result["saved"])
        save_successful_pair.assert_called_once()
        args, kwargs = save_successful_pair.call_args
        self.assertEqual(args[:2], (question, sql))
        self.assertTrue(kwargs["template"])

    @patch("app.routers.chat._templatize_in_background")
    @patch("app.routers.chat.nl2sql.resolve_example_target", return_value=None)
    @patch("app.routers.chat.nl2sql.save_composite_pair", return_value=True)
    @patch("app.routers.chat.get_result")
    def test_composite_approval_saves_parent_and_each_child_sql(
        self,
        get_result,
        save_composite_pair,
        _resolve_example_target,
        _templatize,
    ):
        parent = "分别统计泛油和坑槽数量"
        sections = [
            {
                "node_id": "q1",
                "question": "泛油有多少处？",
                "status": "accepted",
                "sql": "SELECT COUNT(*) FROM ai_problem",
            },
            {
                "node_id": "q2",
                "question": "坑槽有多少处？",
                "status": "accepted",
                "sql": "SELECT COUNT(*) FROM ai_problem",
            },
        ]
        get_result.return_value = {
            "question": parent,
            "sections": sections,
        }

        result = approve_result(ApproveRequest(result_id="result-2"))

        self.assertTrue(result["saved"])
        save_composite_pair.assert_called_once()
        args, _kwargs = save_composite_pair.call_args
        self.assertEqual(args[0], parent)
        self.assertEqual(args[1], sections)


if __name__ == "__main__":
    unittest.main()
