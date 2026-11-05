import unittest
from unittest.mock import patch

from app.query.contracts import AnswerPackage
from app.query.service import TurnStart
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from app.services import agent
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
