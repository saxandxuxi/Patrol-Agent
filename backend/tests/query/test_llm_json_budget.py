import unittest
from unittest.mock import patch

import httpx

from app.services.llm import _post, chat_json
from app.services.llm_json import parse_json_lenient


class LlmJsonBudgetTests(unittest.TestCase):
    @staticmethod
    def _response(status_code: int, *, json_body=None):
        return httpx.Response(
            status_code,
            json=json_body,
            request=httpx.Request(
                "POST",
                "https://example.test/chat/completions",
            ),
        )

    @patch("app.services.llm.chat", return_value='{"cases": []}')
    def test_chat_json_forwards_explicit_output_budget(self, chat):
        chat_json(
            [{"role": "user", "content": "output json"}],
            max_tokens=4096,
        )

        self.assertEqual(chat.call_args.kwargs["max_tokens"], 4096)

    def test_parser_uses_last_complete_object_after_model_reasoning(self):
        text = """
我先检查计划。
{"complete": false, "issues": [{"code": "missing_dimension"}]}
（自我修正）重新核对后计划其实完整。
```json
{"complete": true, "issues": []}
```
"""

        self.assertEqual(
            parse_json_lenient(text),
            {"complete": True, "issues": []},
        )

    def test_parser_keeps_nested_objects_from_final_result(self):
        text = """
分析文字
{"complete": false, "issues": [{"code": "old"}]}
最终结果：
{"complete": true, "issues": [], "meta": {"source": "audit"}}
"""

        self.assertEqual(
            parse_json_lenient(text),
            {
                "complete": True,
                "issues": [],
                "meta": {"source": "audit"},
            },
        )

    @patch("app.services.llm.httpx.Client")
    def test_post_preserves_provider_error_code_and_message(self, client_cls):
        response = httpx.Response(
            400,
            json={
                "error": {
                    "message": "Access denied because the account is overdue.",
                    "code": "Arrearage",
                }
            },
            request=httpx.Request("POST", "https://example.test/chat/completions"),
        )
        client_cls.return_value.__enter__.return_value.post.return_value = response

        with self.assertRaisesRegex(RuntimeError, "Arrearage"):
            _post(
                {
                    "base_url": "https://example.test",
                },
                {},
                {},
            )

    @patch("app.services.llm.time.sleep")
    @patch("app.services.llm.httpx.Client")
    def test_post_retries_rate_limit_a_bounded_number_of_times(
        self,
        client_cls,
        sleep,
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.side_effect = [
            self._response(
                429,
                json_body={"error": {"message": "rate limit"}},
            ),
            self._response(
                200,
                json_body={"choices": [{"message": {"content": "OK"}}]},
            ),
        ]

        result = _post(
            {"base_url": "https://example.test"},
            {},
            {},
        )

        self.assertEqual(result["choices"][0]["message"]["content"], "OK")
        self.assertEqual(client.post.call_count, 2)
        sleep.assert_called_once()

    @patch("app.services.llm.time.sleep")
    @patch("app.services.llm.httpx.Client")
    def test_post_stops_after_three_rate_limit_attempts(
        self,
        client_cls,
        sleep,
    ):
        client = client_cls.return_value.__enter__.return_value
        client.post.return_value = self._response(
            429,
            json_body={"error": {"message": "rate limit"}},
        )

        with self.assertRaisesRegex(RuntimeError, "HTTP 429"):
            _post(
                {"base_url": "https://example.test"},
                {},
                {},
            )

        self.assertEqual(client.post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("app.services.llm.time.sleep")
    @patch("app.services.llm.httpx.Client")
    def test_post_retries_network_timeout(self, client_cls, sleep):
        client = client_cls.return_value.__enter__.return_value
        client.post.side_effect = [
            httpx.ReadTimeout(
                "timed out",
                request=httpx.Request(
                    "POST",
                    "https://example.test/chat/completions",
                ),
            ),
            self._response(
                200,
                json_body={"choices": [{"message": {"content": "OK"}}]},
            ),
        ]

        result = _post(
            {"base_url": "https://example.test"},
            {},
            {},
        )

        self.assertEqual(result["choices"][0]["message"]["content"], "OK")
        self.assertEqual(client.post.call_count, 2)
        sleep.assert_called_once()


if __name__ == "__main__":
    unittest.main()

