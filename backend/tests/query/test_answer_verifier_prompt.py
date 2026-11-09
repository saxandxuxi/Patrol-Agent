import unittest
from unittest.mock import patch

from app.services import nl2sql


class AnswerVerifierPromptTests(unittest.TestCase):
    @patch.object(nl2sql, "chat_json")
    def test_verifier_receives_complete_sql(self, chat_json):
        chat_json.return_value = {
            "correct": True,
            "issue_type": "none",
            "reason": "",
        }
        marker = "collection_time >= '2026-11-01 00:00:00'"
        sql = (
            "WITH disease AS (SELECT 1 AS id) "
            + ("/* long generated CTE */\n" * 180)
            + f"SELECT * FROM disease WHERE {marker}"
        )

        nl2sql.verify_answer(
            "验证长 SQL 的完整条件",
            sql,
            {"columns": ["id"], "rows": [{"id": "1"}]},
            "共 1 条",
        )

        prompt = "\n".join(
            str(message.get("content") or "")
            for message in chat_json.call_args.args[0]
        )
        self.assertIn(marker, prompt)


if __name__ == "__main__":
    unittest.main()
