import unittest
from unittest.mock import patch

from app.services.llm import chat_json


class LlmJsonBudgetTests(unittest.TestCase):
    @patch("app.services.llm.chat", return_value='{"cases": []}')
    def test_chat_json_forwards_explicit_output_budget(self, chat):
        chat_json(
            [{"role": "user", "content": "output json"}],
            max_tokens=4096,
        )

        self.assertEqual(chat.call_args.kwargs["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()

