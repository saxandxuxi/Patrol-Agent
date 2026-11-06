import unittest

from app.query.contracts import AnswerPackage
from app.query.review import (
    ReviewSuspicion,
    consume_review_gate,
    issue_review_gate,
)


def answer_with_sections() -> AnswerPackage:
    return AnswerPackage(
        "按 effective_disease_length 取前5条，结果只有一个聚合值",
        (
            {
                "node_id": "q1",
                "question": "按长度取前5条病害记录",
                "view": "top_records",
                "status": "repairable",
                "failure_code": "wrong_shape",
                "answer": "结果只有一行总长度，不是前5条明细",
                "scope_hash": "shape-scope",
                "immutable_scope_hash": "scope-1",
                "sql": "SELECT SUM(effective_disease_length) FROM ai_problem",
                "columns": ["有效病害总长度"],
                "rows": [{"有效病害总长度": "151579.56"}],
            },
        ),
    )


def suspicion() -> ReviewSuspicion:
    return ReviewSuspicion(
        original_question="把面积前五名和长度前五名的病害调出来",
        node_id="q1",
        reason="结果像聚合值，但问题要前5条明细",
        failure_layer="contract",
        failure_code="wrong_shape",
    )


class ReviewTests(unittest.TestCase):
    def test_review_gate_exposes_backend_evidence(self):
        gate = issue_review_gate(answer_with_sections(), [suspicion()])

        self.assertIsNotNone(gate)
        self.assertTrue(gate.review_token)
        self.assertEqual(gate.original_question, suspicion().original_question)
        self.assertEqual(gate.nodes[0].node_id, "q1")
        self.assertEqual(gate.nodes[0].view, "top_records")
        self.assertIn("SELECT SUM", gate.nodes[0].sql)
        self.assertEqual(
            gate.nodes[0].result_sample,
            ({"有效病害总长度": "151579.56"},),
        )

    def test_accept_consumes_token_once(self):
        gate = issue_review_gate(answer_with_sections(), [suspicion()])

        accepted = consume_review_gate(gate.review_token, "accept")
        repeated = consume_review_gate(gate.review_token, "accept")

        self.assertIsNotNone(accepted)
        self.assertEqual(accepted.decision, "accept")
        self.assertIsNone(repeated)

    def test_reject_returns_review_evidence_for_repair(self):
        gate = issue_review_gate(answer_with_sections(), [suspicion()])

        evidence = consume_review_gate(gate.review_token, "reject")

        self.assertIsNotNone(evidence)
        self.assertEqual(
            evidence.original_question,
            "把面积前五名和长度前五名的病害调出来",
        )
        self.assertEqual(evidence.scope_hash, "scope-1")
        self.assertEqual(evidence.suspect_node_ids, ("q1",))
        self.assertIn("SELECT SUM", evidence.sections[0].sql)
        self.assertEqual(
            evidence.sections[0].result_sample,
            ({"有效病害总长度": "151579.56"},),
        )

    def test_no_suspicion_does_not_create_gate(self):
        self.assertIsNone(issue_review_gate(answer_with_sections(), []))


if __name__ == "__main__":
    unittest.main()

