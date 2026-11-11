import unittest

from app.query.analysis import normalize_action
from app.query.contracts import QuerySpecError
from app.query.planning_repair.issues import repair_issue_from_error


class ActionContractTests(unittest.TestCase):
    def test_analyze_maps_to_non_querying_post_calc(self):
        node_type, assertions = normalize_action("analyze")

        self.assertEqual(node_type, "post_calc")
        self.assertIn("analysis_only", assertions)

    def test_missing_action_is_not_query_data(self):
        with self.assertRaisesRegex(QuerySpecError, "^invalid_action:"):
            normalize_action(None)

    def test_unknown_action_generates_action_only_repair_issue(self):
        issue = repair_issue_from_error(
            QuerySpecError("invalid_action: draw_chart"),
            {"views": [{"id": "q1", "action": "draw_chart"}]},
        )

        self.assertEqual(issue.code, "invalid_action")
        self.assertIn("/views/*/action", issue.allowed_paths)
        self.assertIn("/views/*/required_assertions", issue.allowed_paths)


if __name__ == "__main__":
    unittest.main()
