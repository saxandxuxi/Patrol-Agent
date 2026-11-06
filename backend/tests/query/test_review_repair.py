import unittest
from unittest.mock import patch

from app.query.contracts import (
    AnswerPackage,
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
    immutable_scope_hash,
)
from app.query.review import ReviewEvidence, ReviewNodeEvidence, serialize_query_spec
from app.query.review_repair import repair_review_evidence


def spec(
    node_id,
    *,
    view="top_records",
    metrics=(),
    dimensions=(),
    filters=(),
):
    return QuerySpec(
        id=node_id,
        kind="mysql",
        subject="disease",
        view=view,
        metrics=metrics,
        dimensions=dimensions,
        filters=tuple(filters),
        output_projection=("problem_number", "area", "effective_disease_length"),
        order_by=({"field": "effective_disease_length", "direction": "desc"},),
        limit=5,
        required_assertions=("preserve_user_scope",),
        question=f"{node_id} question",
    )


def evidence_for(q1, q2) -> ReviewEvidence:
    sections = tuple(
        ReviewNodeEvidence(
            node_id=item.id,
            question=item.question,
            view=item.view,
            sql=f"SELECT * FROM disease LIMIT {item.limit}",
            columns=("problem_number",),
            result_sample=({"problem_number": "P1"},),
            reason="结果像聚合值，不是前5条明细",
            failure_code="wrong_shape",
            spec=serialize_query_spec(item),
            immutable_scope_hash=immutable_scope_hash(item),
        )
        for item in (q1, q2)
    )
    return ReviewEvidence(
        original_question="把面积前五名和长度前五名的病害调出来",
        scope_hash=sections[0].immutable_scope_hash,
        suspect_node_ids=("q1",),
        sections=sections,
    )


def plan(q1, q2) -> QueryPlan:
    return QueryPlan(
        question="把面积前五名和长度前五名的病害调出来",
        nodes=(
            PlanNode(id="q1", spec=q1),
            PlanNode(id="q2", spec=q2),
        ),
    )


class ReviewRepairTests(unittest.TestCase):
    @patch("app.query.review_repair._execute_plan")
    @patch("app.query.review_repair.analyze_single_turn")
    def test_repair_changes_only_suspect_node(self, analyze, execute):
        original_q1 = spec("q1")
        original_q2 = spec("q2")
        repaired_q1 = spec(
            "q1",
            view="top_groups",
            metrics=("count",),
            dimensions=("problem_type_name",),
        )
        analyze.return_value = plan(repaired_q1, original_q2)
        execute.return_value = AnswerPackage("已重新规划", ({"status": "accepted"},))

        result = repair_review_evidence(evidence_for(original_q1, original_q2))

        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.stopped_reason, "")
        self.assertIsNone(result.answer.review_gate)
        self.assertEqual(execute.call_count, 1)

    @patch("app.query.review_repair._execute_plan")
    @patch("app.query.review_repair.analyze_single_turn")
    def test_repair_rejects_scope_drift(self, analyze, execute):
        original_q1 = spec("q1")
        original_q2 = spec("q2")
        changed_filter = ValueBinding(
            field="route_name",
            op="=",
            storage_value="another-route",
            display_value="另一条路",
            source="model_inference",
            resolver="route",
        )
        analyze.return_value = plan(
            spec("q1", filters=(changed_filter,)),
            original_q2,
        )

        result = repair_review_evidence(evidence_for(original_q1, original_q2))

        self.assertEqual(result.stopped_reason, "scope_drift")
        self.assertEqual(
            result.answer.sections[0]["failure_code"],
            "scope_drift",
        )
        execute.assert_not_called()

    @patch("app.query.review_repair._execute_plan")
    @patch("app.query.review_repair.analyze_single_turn")
    def test_repair_rejects_change_to_non_suspect_node(self, analyze, execute):
        original_q1 = spec("q1")
        original_q2 = spec("q2")
        analyze.return_value = plan(
            original_q1,
            spec("q2", view="count", metrics=("count",)),
        )

        result = repair_review_evidence(evidence_for(original_q1, original_q2))

        self.assertEqual(result.stopped_reason, "non_suspect_node_changed")
        execute.assert_not_called()

    @patch("app.query.review_repair._execute_plan")
    @patch("app.query.review_repair.analyze_single_turn")
    def test_same_failed_plan_stops_after_first_attempt(self, analyze, execute):
        original_q1 = spec("q1")
        original_q2 = spec("q2")
        analyze.return_value = plan(original_q1, original_q2)
        execute.return_value = AnswerPackage(
            "仍然答非所问",
            ({"status": "repairable", "failure_code": "wrong_shape"},),
            review_gate={"review_token": "still-wrong"},
        )

        result = repair_review_evidence(evidence_for(original_q1, original_q2))

        self.assertEqual(result.stopped_reason, "repair_exhausted")
        self.assertEqual(execute.call_count, 1)
        self.assertIsNone(result.answer.review_gate)

    @patch("app.query.review_repair._execute_plan")
    @patch("app.query.review_repair.analyze_single_turn")
    def test_explicit_unsupported_is_not_treated_as_success(self, analyze, execute):
        original_q1 = spec("q1")
        original_q2 = spec("q2")
        analyze.return_value = plan(
            spec(
                "q1",
                view="top_groups",
                metrics=("count",),
                dimensions=("problem_type_name",),
            ),
            original_q2,
        )
        execute.return_value = AnswerPackage(
            "当前数据能力不支持该查询",
            (
                {
                    "status": "unsupported",
                    "failure_code": "unsupported_query_spec",
                    "answer": "当前数据能力不支持该查询",
                },
            ),
        )

        result = repair_review_evidence(evidence_for(original_q1, original_q2))

        self.assertEqual(result.stopped_reason, "repair_unsupported")
        self.assertIn("不支持", result.answer.text)


if __name__ == "__main__":
    unittest.main()

