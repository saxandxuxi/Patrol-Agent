import unittest
from unittest.mock import patch

from app.query.compiler import CompiledQuery
from app.query.contracts import AnswerPackage, ExecutionObservation, VerificationReport
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import start_single_turn
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from tests.query.factories import count_plan, length_query_plan


def candidate_set_with_one_template() -> CandidateSet:
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
    return CandidateSet(
        candidates=(
            TemplateCandidate(
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
            ),
        )
    )


def empty_candidate_set() -> CandidateSet:
    return CandidateSet()


def count_observation() -> ExecutionObservation:
    return ExecutionObservation(
        node_id="summary",
        ok=True,
        engine="wren",
        sql="SELECT COUNT(*) FROM ai_problem",
        columns=("n",),
        row_count=1,
        preview_rows=({"n": "20108"},),
        rows_fetched=1,
    )


def detail_observation(rows_fetched: int, total_count: int) -> ExecutionObservation:
    return ExecutionObservation(
        node_id="detail",
        ok=True,
        engine="wren",
        sql="SELECT id FROM ai_problem LIMIT 500",
        columns=("id",),
        row_count=rows_fetched,
        preview_rows=({"id": "1"},),
        rows_fetched=rows_fetched,
        total_count=total_count,
        truncated=True,
    )


def accepted_report() -> VerificationReport:
    return VerificationReport(status="accepted")


class SingleTurnEndToEndTests(unittest.TestCase):
    @patch(
        "app.query.service.assemble_answer",
        return_value=AnswerPackage("共 20108 处；明细展示前 500 条。"),
    )
    @patch(
        "app.query.service.verify_result",
        side_effect=[accepted_report(), accepted_report()],
    )
    @patch(
        "app.query.service.execute_compiled",
        side_effect=[count_observation(), detail_observation(500, 20108)],
    )
    @patch(
        "app.query.service.compile_query",
        side_effect=[
            CompiledQuery("SELECT COUNT(*) FROM ai_problem", "wren", "wren"),
            CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "wren", "wren"),
        ],
    )
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn", return_value=length_query_plan())
    def test_length_query_separates_summary_count_and_detail_page(
        self,
        _analyze,
        _resolve,
        _compile,
        execute,
        _verify,
        _assemble,
    ):
        result = start_single_turn(
            "病害长度大于 2 米的有多少处？把明细列出来",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertEqual(execute.call_count, 2)
        self.assertIn("20108", result.answer.text)
        self.assertIn("前 500", result.answer.text)

    @patch("app.query.service.execute_compiled")
    @patch(
        "app.query.service.resolve_templates",
        return_value=candidate_set_with_one_template(),
    )
    @patch("app.query.service.analyze_single_turn", return_value=count_plan())
    def test_template_query_does_not_execute_before_confirmation(
        self,
        _analyze,
        _resolve,
        execute,
    ):
        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.candidate_set)
        execute.assert_not_called()
