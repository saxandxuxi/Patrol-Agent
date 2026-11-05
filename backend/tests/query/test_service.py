import unittest
from unittest.mock import patch

from app.query.compiler import CompiledQuery
from app.query.contracts import ExecutionObservation
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import start_single_turn
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from tests.query.factories import count_plan


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


def compiled_count_query() -> CompiledQuery:
    return CompiledQuery("SELECT COUNT(*) AS n FROM ai_problem", "wren", "wren")


def count_observation() -> ExecutionObservation:
    return ExecutionObservation(
        node_id="q1",
        ok=True,
        engine="wren",
        sql="SELECT COUNT(*) AS n FROM ai_problem",
        columns=("n",),
        row_count=1,
        preview_rows=({"n": "20108"},),
        rows_fetched=1,
    )


class ServiceTests(unittest.TestCase):
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_template_match_returns_cards_without_execution(self, analyze, resolve):
        analyze.return_value = count_plan()
        resolve.return_value = candidate_set_with_one_template()
        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.candidate_set)
        self.assertIsNone(result.answer)

    @patch("app.query.service.execute_compiled", return_value=count_observation())
    @patch("app.query.service.compile_query", return_value=compiled_count_query())
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_no_template_can_continue_to_wren(
        self,
        analyze,
        resolve,
        _compile_query,
        _execute,
    ):
        analyze.return_value = count_plan()
        resolve.return_value = empty_candidate_set()
        result = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        self.assertIsNotNone(result.answer)
