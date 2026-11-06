import unittest

from app.services import react_repair


class ReactRepairPolicyTests(unittest.TestCase):
    def test_capability_root_file_is_read_as_failure_policy(self):
        self.assertEqual(
            react_repair.candidate_tool_names("sql_runtime_error"),
            ["query_data"],
        )

    def test_named_policy_keeps_its_retry_budget(self):
        failure = react_repair.build_failure(
            "sql_runtime_error",
            stage="execute",
            reason="syntax error",
            evidence={"sql": "SELECT broken"},
        )

        self.assertTrue(failure["retryable"])
        self.assertEqual(failure["max_repair_rounds"], 2)


if __name__ == "__main__":
    unittest.main()
