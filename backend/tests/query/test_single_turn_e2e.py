import unittest
from unittest.mock import patch

from app.query.compiler import CompiledQuery
from app.query.contracts import (
    AnswerPackage,
    ExecutionObservation,
    PlanNode,
    QueryPlan,
    QuerySpec,
    ValueBinding,
    VerificationReport,
)
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import confirm_single_turn, start_single_turn
from app.query.templates import (
    CandidateSet,
    ConfirmationCard,
    TemplateCandidate,
    TemplateContract,
)
from tests.query.factories import count_plan, length_query_plan
from tests.query.preset_fixture import PresetFixtureTestCase


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


def device_time_plan() -> QueryPlan:
    return QueryPlan(
        question="全部设备在11月2日检出了多少病害",
        nodes=(
            PlanNode(
                id="q1",
                spec=QuerySpec(
                    id="q1",
                    kind="mysql",
                    subject="disease",
                    view="count",
                    metrics=("count",),
                    dimensions=(),
                    filters=(
                        ValueBinding(
                            field="device_code",
                            op="=",
                            storage_value="__ALL__",
                            display_value="全部设备",
                            source="user_explicit",
                            resolver="identifier",
                            raw_text="全部设备",
                        ),
                        ValueBinding(
                            field="collection_time",
                            op=">=",
                            storage_value="2026-11-02 00:00:00",
                            display_value="2026-11-02",
                            source="user_explicit",
                            resolver="time_range",
                            raw_text="11月2日",
                        ),
                        ValueBinding(
                            field="collection_time",
                            op="<",
                            storage_value="2026-11-03 00:00:00",
                            display_value="2026-11-03",
                            source="user_explicit",
                            resolver="time_range",
                            raw_text="11月2日",
                        ),
                    ),
                    output_projection=(),
                    order_by=(),
                    limit=None,
                    required_assertions=("preserve_user_scope",),
                    question="全部设备在11月2日检出了多少病害",
                ),
            ),
        ),
    )


class SingleTurnEndToEndTests(PresetFixtureTestCase):
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

    @patch("app.query.service.execute_compiled")
    @patch(
        "app.query.service.compile_query",
        side_effect=ValueError("template executor returned empty SQL"),
    )
    @patch(
        "app.query.service.resolve_templates",
        return_value=candidate_set_with_one_template(),
    )
    @patch("app.query.service.analyze_single_turn", return_value=count_plan())
    def test_confirmed_template_compile_failure_returns_structured_answer(
        self,
        _analyze,
        _resolve,
        _compile,
        execute,
    ):
        pending = start_single_turn(
            "泛油有多少处？",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        result = confirm_single_turn(
            pending.confirmation_ticket.token,
            {"q1": "c1"},
            {},
        )

        self.assertIn("template executor returned empty SQL", result.text)
        self.assertEqual(result.sections[0]["status"], "unsupported")
        self.assertEqual(result.sections[0]["failure_code"], "query_compile_failed")
        execute.assert_not_called()

    @patch(
        "app.query.service.verify_result",
        return_value=accepted_report(),
    )
    @patch(
        "app.query.service.execute_compiled",
        return_value=count_observation(),
    )
    @patch(
        "app.query.service.analyze_single_turn",
        return_value=device_time_plan(),
    )
    def test_confirmed_preset_compiles_bound_time_and_device(
        self,
        _analyze,
        execute,
        _verify,
    ):
        pending = start_single_turn(
            "全部设备在11月2日检出了多少病害",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )
        candidate = pending.candidate_set.candidates[0]
        result = confirm_single_turn(
            pending.confirmation_ticket.token,
            {candidate.node_id: candidate.candidate_id},
            {},
        )

        compiled = execute.call_args.args[0]
        self.assertEqual(result.sections[0]["status"], "accepted")
        self.assertIn("2026-11-02", compiled.sql)
        self.assertIn("DATE_ADD('2026-11-02', INTERVAL 1 DAY)", compiled.sql)
