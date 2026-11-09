import unittest

from app.query.section_status import execution_failure_reasons


class SectionStatusTests(unittest.TestCase):
    def test_expected_blocked_dependent_does_not_mask_real_root_failure(self):
        sections = [
            {
                "node_id": "q1",
                "status": "repairable",
                "failure_code": "wrong_shape",
                "answer": "SQL 返回的结构没有满足分组契约。",
            },
            {
                "node_id": "q2",
                "status": "blocked",
                "failure_code": "dependency_failed",
                "depends_on": ["q1"],
                "answer": "依赖的前置结果不完整。",
            },
        ]

        self.assertEqual(
            execution_failure_reasons(sections),
            ["SQL 返回的结构没有满足分组契约。"],
        )

    def test_unsupported_root_and_dependent_are_not_runtime_errors(self):
        sections = [
            {
                "node_id": "q1",
                "status": "unsupported",
                "failure_code": "patrol_batch_not_resolved",
                "answer": "该日期没有对应巡检批次。",
            },
            {
                "node_id": "q2",
                "status": "blocked",
                "failure_code": "dependency_failed",
                "depends_on": ["q1"],
                "answer": "依赖的前置结果不完整。",
            },
        ]

        self.assertEqual(execution_failure_reasons(sections), [])


if __name__ == "__main__":
    unittest.main()
