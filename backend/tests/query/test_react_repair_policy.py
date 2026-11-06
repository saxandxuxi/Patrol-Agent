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

    def test_zero_denominator_routes_to_calculation_tools(self):
        self.assertEqual(
            react_repair.candidate_tool_names(
                "calculation_zero_denominator"
            ),
            [
                "calculate_scalar",
                "calculate_columns",
                "calculate_series",
            ],
        )

    def test_calculation_repair_filters_by_input_shape(self):
        self.assertEqual(
            react_repair.candidate_tool_names(
                "calculation_zero_denominator",
                "scalar_list",
            ),
            ["calculate_scalar"],
        )
        self.assertEqual(
            react_repair.candidate_tool_names(
                "calculation_zero_denominator",
                "series",
            ),
            ["calculate_series"],
        )


if __name__ == "__main__":
    unittest.main()
