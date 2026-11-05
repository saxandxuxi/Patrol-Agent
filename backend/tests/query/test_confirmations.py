import unittest
from unittest.mock import patch

from app.query.analysis import QuerySpecError
from app.query.confirmations import (
    ConfirmationError,
    consume_confirmation,
    issue_confirmation,
)
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
    card = ConfirmationCard(
        source="builtin",
        title="病害数量",
        version=1,
        scope_summary="主体=disease；视图=count",
        bound_parameters=(),
        fixed_conditions=(),
        warnings=(),
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
                confirmation_card=card,
            ),
        ),
        rejections=(),
    )


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.plan = count_plan()

    def test_token_is_single_use(self):
        ticket = issue_confirmation(
            self.plan,
            CandidateSet(candidates=(), rejections=()),
            "schema-1",
        )
        consume_confirmation(ticket.token, choices=(), overrides={})
        with self.assertRaises(ConfirmationError):
            consume_confirmation(ticket.token, choices=(), overrides={})

    def test_scope_hash_mismatch_is_rejected(self):
        ticket = issue_confirmation(
            self.plan,
            CandidateSet(candidates=(), rejections=()),
            "schema-1",
        )
        with self.assertRaisesRegex(ConfirmationError, "scope"):
            consume_confirmation(
                ticket.token,
                choices=(),
                overrides={},
                scope_hash="tampered",
            )

    def test_invalid_override_is_rejected_before_execution(self):
        ticket = issue_confirmation(
            self.plan,
            candidate_set_with_one_template(),
            "schema-1",
        )
        with patch(
            "app.query.confirmations.resolve_value",
            side_effect=QuerySpecError("unknown enum"),
        ):
            with self.assertRaisesRegex(ConfirmationError, "override"):
                consume_confirmation(
                    ticket.token,
                    choices={"q1": "c1"},
                    overrides={"q1": {"problem_type_name": "其他"}},
                )
