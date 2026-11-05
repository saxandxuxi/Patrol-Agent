import unittest

from app.query.compiler import compile_query
from app.query.contracts import QuerySpec, UserDecision
from app.query.templates import ConfirmationCard, TemplateCandidate, TemplateContract


def template_candidate() -> TemplateCandidate:
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
    return TemplateCandidate(
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


class CompilerTests(unittest.TestCase):
    def test_template_compilation_requires_consumed_decision(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        with self.assertRaisesRegex(ValueError, "confirmation"):
            compile_query(
                spec,
                UserDecision(token="", choices={"q1": "c1"}, overrides={}),
                candidate=template_candidate(),
                template_executor=None,
                wren_executor=None,
            )

    def test_no_candidate_compiles_through_wren(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        decision = UserDecision(token="issued", choices={}, overrides={})
        result = compile_query(
            spec,
            decision,
            candidate=None,
            template_executor=None,
            wren_executor=lambda current: "SELECT COUNT(*) FROM ai_problem",
        )
        self.assertEqual(result.engine, "wren")
        self.assertIsNone(result.template_id)
