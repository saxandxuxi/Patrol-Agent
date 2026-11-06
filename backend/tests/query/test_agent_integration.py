import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.query.contracts import AnswerPackage
from app.query.review import ReviewSuspicion, issue_review_gate
from app.query.review_repair import ReviewRepairResult
from app.query.service import TurnStart
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from app.services import agent
from app.routers.chat import QueryReviewRequest, query_review
from tests.query.factories import count_plan


def turn_with_confirmation_card() -> TurnStart:
    contract = TemplateContract(
        id="template-1",
        version=1,
        source="builtin",
        status="active",
        kind="mysql",
        adapter_id="builtin",
        native_id="template-1",
        subject="disease",
        view="count",
        metrics=("count",),
        dimensions=(),
        filters=(),
        fixed_conditions=(),
    )
    candidate = TemplateCandidate(
        candidate_id="c1",
        node_id="q1",
        contract=contract,
        spec_fingerprint="fingerprint",
        bound_parameters=(),
        covered_constraints=("count",),
        fixed_conditions=(),
        warnings=(),
        requires_confirmation=True,
        confirmation_token="candidate-token",
        confirmation_card=ConfirmationCard(
            source="builtin",
            title="病害数量",
            version=1,
            scope_summary="主体=disease；视图=count",
            bound_parameters=(),
            fixed_conditions=(),
            warnings=(),
        ),
    )
    from app.query.confirmations import issue_confirmation

    plan = count_plan()
    candidates = CandidateSet(candidates=(candidate,))
    ticket = issue_confirmation(plan, candidates, "schema-1")
    return TurnStart(
        plan=plan,
        candidate_set=candidates,
        confirmation_ticket=ticket,
    )


class AgentIntegrationTests(unittest.TestCase):
    def _issue_review_token(self) -> str:
        answer = AnswerPackage(
            "结果可能有误",
            (
                {
                    "node_id": "q1",
                    "question": "按长度取前5条明细",
                    "view": "top_records",
                    "status": "repairable",
                    "failure_code": "wrong_shape",
                    "answer": "结果像聚合值",
                    "scope_hash": "shape-scope",
                    "immutable_scope_hash": "scope-1",
                    "sql": "SELECT SUM(effective_disease_length) FROM ai_problem",
                    "rows": [{"有效病害总长度": "151579.56"}],
                },
            ),
        )
        gate = issue_review_gate(
            answer,
            [
                ReviewSuspicion(
                    original_question="把面积前五名和长度前五名的病害调出来",
                    node_id="q1",
                    reason="结果像聚合值",
                    failure_code="wrong_shape",
                )
            ],
        )
        return gate.review_token

    def test_review_request_does_not_accept_client_plan_or_sql(self):
        self.assertEqual(
            set(QueryReviewRequest.model_fields),
            {"review_token", "decision"},
        )

    def test_review_accept_returns_without_repair(self):
        token = self._issue_review_token()
        result = query_review(
            QueryReviewRequest(review_token=token, decision="accept")
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["decision"], "accept")
        self.assertFalse(result.get("repair_required"))

    @patch("app.query.review_repair.repair_review_evidence")
    def test_review_reject_runs_bounded_repair(self, repair):
        token = self._issue_review_token()
        repair.return_value = ReviewRepairResult(
            answer=AnswerPackage(
                "已重新规划并返回前5条明细",
                ({"status": "accepted", "rows": [{"problem_number": "P1"}]},),
            ),
            attempts=1,
        )
        result = query_review(
            QueryReviewRequest(review_token=token, decision="reject")
        )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["decision"], "reject")
        self.assertFalse(result["repair_required"])
        self.assertEqual(result["attempts"], 1)
        self.assertIn("前5条明细", result["reply"])

    def test_review_token_cannot_be_consumed_twice(self):
        token = self._issue_review_token()
        query_review(QueryReviewRequest(review_token=token, decision="reject"))

        with self.assertRaises(HTTPException) as caught:
            query_review(
                QueryReviewRequest(review_token=token, decision="reject")
            )

        self.assertEqual(
            caught.exception.detail["failure_code"],
            "review_token_consumed",
        )

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.chat_settings.load", return_value={"single_turn_compiler_enabled": False})
    @patch("app.query.service.start_single_turn")
    def test_disabled_flag_never_calls_single_turn_service(
        self,
        start,
        _settings,
        _persist,
    ):
        with patch.object(agent, "_handle_message", return_value={"reply": "legacy"}):
            payload = agent.handle_message("泛油有多少处？", session_id="s1")
        start.assert_not_called()
        self.assertEqual(payload["reply"], "legacy")

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.intent_service.classify_intent", return_value="data")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.start_single_turn")
    def test_enabled_data_question_uses_new_service(
        self,
        start,
        _settings,
        _intent,
        _persist,
    ):
        start.return_value = turn_with_confirmation_card()
        payload = agent.handle_message("泛油有多少处？", session_id="s1")
        self.assertTrue(payload["await_confirm"])
        self.assertEqual(payload["source"], "confirm")
        self.assertIn("compiler_context", payload)

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.intent_service.classify_intent", return_value="data")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.start_single_turn")
    def test_enabled_data_question_never_falls_back_to_legacy_flow(
        self,
        start,
        _settings,
        _intent,
        _persist,
    ):
        start.return_value = TurnStart(
            plan=count_plan(),
            answer=AnswerPackage("共 10 处。"),
        )
        with patch.object(
            agent,
            "_handle_message",
            side_effect=AssertionError("legacy flow must not run"),
        ):
            payload = agent.handle_message("泛油有多少处？", session_id="s1")
        self.assertEqual(payload["reply"], "共 10 处。")
        self.assertEqual(payload["source"], "single_turn_compiler")

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.intent_service.classify_intent", return_value="data")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.start_single_turn")
    def test_structured_query_failure_is_visible_in_process(
        self,
        start,
        _settings,
        _intent,
        _persist,
    ):
        start.return_value = TurnStart(
            plan=count_plan(),
            answer=AnswerPackage(
                "无法可靠规划该查询：病害查询不支持分组字段：device_code",
                (
                    {
                        "node_id": "q1",
                        "question": "每台设备检出了多少病害",
                        "type": "data",
                        "status": "unsupported",
                        "failure_code": "query_plan_not_executable",
                        "answer": "病害查询不支持分组字段：device_code",
                    },
                ),
            ),
        )

        payload = agent.handle_message("每台设备检出了多少病害", session_id="s1")

        self.assertEqual(payload["process"]["steps"][0]["engine"], "single_turn_compiler")
        self.assertEqual(
            payload["process"]["steps"][0]["failure_code"],
            "query_plan_not_executable",
        )
        self.assertIn("device_code", payload["process"]["steps"][0]["error"])

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.intent_service.classify_intent", return_value="data")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.start_single_turn")
    def test_structured_unsupported_is_not_top_level_system_error(
        self,
        start,
        _settings,
        _intent,
        _persist,
    ):
        start.return_value = TurnStart(
            plan=count_plan(),
            answer=AnswerPackage(
                "当前数据能力不支持：只有管养单位 ID",
                (
                    {
                        "node_id": "q1",
                        "question": "按管养片区比较",
                        "type": "data",
                        "status": "unsupported",
                        "failure_code": "unsupported_query_spec",
                        "answer": "当前数据能力不支持：只有管养单位 ID",
                    },
                ),
            ),
        )

        payload = agent.handle_message("按管养片区比较", session_id="s1")

        self.assertNotIn("error", payload)
        self.assertEqual(
            payload["process"]["steps"][0]["failure_code"],
            "unsupported_query_spec",
        )

    @patch("app.services.agent._persist_conversation_turn")
    @patch("app.services.agent.nl2sql.verify_answer")
    @patch("app.services.agent.intent_service.classify_intent", return_value="data")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={
            "single_turn_compiler_enabled": True,
            "reflect_check": True,
        },
    )
    @patch("app.query.service.start_single_turn")
    def test_semantic_suspicion_creates_user_review_gate(
        self,
        start,
        _settings,
        _intent,
        verify_answer,
        _persist,
    ):
        verify_answer.return_value = {
            "correct": False,
            "issue_type": "wrong_metric",
            "reason": "问题要前5条明细，返回的却是一个聚合值",
            "repair_target": "query",
        }
        start.return_value = TurnStart(
            plan=count_plan(),
            answer=AnswerPackage(
                "有效病害总长度 151579.56",
                (
                    {
                        "node_id": "q1",
                        "question": "按长度取前5条明细",
                        "view": "top_records",
                        "status": "accepted",
                        "scope_hash": "shape-scope",
                        "immutable_scope_hash": "scope-1",
                        "spec": {
                            "id": "q1",
                            "kind": "mysql",
                            "subject": "disease",
                            "view": "top_records",
                            "metrics": [],
                            "dimensions": [],
                            "filters": [],
                            "output_projection": ["area"],
                            "order_by": [{"field": "area", "direction": "desc"}],
                            "limit": 5,
                            "required_assertions": [],
                            "question": "按长度取前5条明细",
                            "input_bindings": [],
                            "calculation": None,
                        },
                        "sql": "SELECT SUM(area) FROM ai_problem",
                        "columns": ["总面积"],
                        "rows": [{"总面积": "151579.56"}],
                    },
                ),
            ),
        )

        payload = agent.handle_message(
            "把长度排名前五名的病害调出来",
            session_id="s1",
        )

        self.assertIsNotNone(payload.get("review_gate"))
        self.assertEqual(
            payload["review_gate"]["nodes"][0]["node_id"],
            "q1",
        )
        self.assertIn("聚合值", payload["review_gate"]["reason"])

    @patch("app.services.agent._persist_conversation_turn")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.pending_confirmation", return_value=None)
    def test_expired_confirmation_token_never_falls_back_to_legacy_flow(
        self,
        _pending,
        _settings,
        _persist,
    ):
        with patch.object(
            agent,
            "_handle_message",
            side_effect=AssertionError("legacy flow must not run"),
        ):
            payload = agent.handle_message(
                "102367594561设备一共检出多少病害",
                session_id="s1",
                choose_token="expired",
                capability_id="preset:device_month_records:q1:v1",
            )

        self.assertEqual(payload["source"], "error")
        self.assertEqual(
            payload["process"]["steps"][0]["failure_code"],
            "confirmation_ticket_missing",
        )

    @patch("app.services.agent._persist_conversation_turn")
    @patch(
        "app.services.agent.chat_settings.load",
        return_value={"single_turn_compiler_enabled": True},
    )
    @patch("app.query.service.pending_confirmation")
    def test_unknown_confirmed_candidate_never_silently_uses_first(
        self,
        pending_confirmation,
        _settings,
        _persist,
    ):
        pending_confirmation.return_value = turn_with_confirmation_card()
        with patch.object(
            agent,
            "_handle_message",
            side_effect=AssertionError("legacy flow must not run"),
        ):
            payload = agent.handle_message(
                "102367594561设备一共检出多少病害",
                session_id="s1",
                choose_token="ticket",
                capability_id="not-a-candidate",
            )

        self.assertEqual(payload["source"], "error")
        self.assertEqual(
            payload["process"]["steps"][0]["failure_code"],
            "confirmation_candidate_missing",
        )
