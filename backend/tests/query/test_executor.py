import unittest

from app.query.compiler import CompiledQuery
from app.query.executor import execute_compiled


class ExecutorTests(unittest.TestCase):
    def test_detail_observation_separates_fetched_and_total(self):
        compiled = CompiledQuery(
            "SELECT id FROM ai_problem LIMIT 500",
            "wren",
            "wren",
            None,
            None,
        )

        def fake_execute(kind, sql):
            if "COUNT(*)" in sql:
                return {"columns": ["n"], "rows": [{"n": "20108"}]}
            return {
                "columns": ["id"],
                "rows": [{"id": str(index)} for index in range(500)],
            }

        observation = execute_compiled(
            compiled,
            fake_execute,
            display_limit=500,
            count_sql="SELECT COUNT(*) AS n FROM ai_problem",
        )
        self.assertEqual(observation.rows_fetched, 500)
        self.assertEqual(observation.total_count, 20108)
        self.assertTrue(observation.truncated)
