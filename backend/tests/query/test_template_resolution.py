import unittest

from app.query.contracts import QuerySpec
from app.query.fields import PhysicalSchemaView
from app.query.templates import TemplateContract, TemplateProvider, resolve_templates


class FixedProvider(TemplateProvider):
    def __init__(self, contracts):
        self.contracts = contracts

    def declared_contracts(self, spec, schema):
        return self.contracts


class TemplateResolutionTests(unittest.TestCase):
    def test_partial_metric_support_is_rejected(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "length"),
            dimensions=("route",),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        contract = TemplateContract(
            id="partial",
            version=1,
            source="preset",
            status="active",
            kind="mysql",
            adapter_id="preset",
            native_id="partial",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("route",),
            filters=(),
            fixed_conditions=(),
        )
        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )
        self.assertEqual(result.candidates, ())
        self.assertIn("missing_metric:length", result.rejections[0])

    def test_unique_complete_candidate_still_requires_confirmation(self):
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
        contract = TemplateContract(
            id="complete",
            version=1,
            source="builtin",
            status="active",
            kind="mysql",
            adapter_id="builtin",
            native_id="complete",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            fixed_conditions=(),
        )
        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )
        self.assertEqual(len(result.candidates), 1)
        self.assertTrue(result.candidates[0].requires_confirmation)
        self.assertEqual(result.candidates[0].node_id, "q1")
