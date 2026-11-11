import unittest
from types import MappingProxyType

from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.models import MetricCapability
from app.query.metrics.semantics import validate_metric_semantics
from app.query.mdl_sql.audit.view_audit import audit_view_shape
from app.query.mdl_sql.models import (
    MdlMetricBinding,
    MdlOutputBinding,
    MdlQueryContract,
)
from sqlglot import parse_one


def contract_with_metric(
    metric_id: str,
    *,
    expression: str,
    aggregation: str,
    subject: str = "route",
    view: str = "list",
    grain: str = "route",
    identity: str = "route.id",
    output_alias: str | None = None,
) -> MdlQueryContract:
    alias = output_alias or metric_id
    metric = MdlMetricBinding(
        metric_id=metric_id,
        expression=expression,
        label=metric_id,
        unit="",
        source="query_spec",
        requires_join=(),
        aggregation=aggregation,
        grain=grain,
        identity=identity,
        fanout_safe=False,
    )
    return MdlQueryContract(
        contract_id="contract",
        plan_id="plan",
        node_id="q1",
        query_spec_hash="spec",
        capability_snapshot_hash="capabilities",
        mdl_fingerprint="mdl",
        contract_hash="contract",
        subject=subject,
        view=view,
        root_model="route",
        field_bindings=MappingProxyType({}),
        filters=(),
        dimensions=(),
        metrics=(metric,),
        outputs=(
            MdlOutputBinding(
                output_id=metric_id,
                kind="metric",
                expression=expression,
                alias=alias,
                label=metric_id,
                unit="",
                source="query_spec",
            ),
        ),
        group_by=(),
        order_by=(),
        limit=None,
        required_assertions=(),
        allowed_models=("route",),
        required_relationship_ids=(),
        intermediate_models=(),
        mandatory_predicates=(),
        output_aliases=MappingProxyType({alias: metric_id}),
    )


class MetricSemanticsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_route_length_is_a_row_metric_not_an_aggregate(self):
        metric = self.registry.snapshot.metrics["disease"]["route_length_km"]

        self.assertEqual(metric.aggregation, "row")
        self.assertEqual(metric.grain, "route")
        self.assertEqual(metric.identity, "route.id")
        self.assertFalse(metric.fanout_safe)

    def test_row_metric_does_not_require_aggregate_function(self):
        issues = audit_view_shape(
            parse_one(
                "SELECT route.route_length_km AS route_length_km "
                "FROM route"
            ),
            aliases={"route": "route"},
            contract=contract_with_metric(
                "route_length_km",
                expression="route.route_length_km",
                aggregation="row",
            ),
            dialect="mysql",
        )

        self.assertNotIn(
            "aggregate_metric_missing",
            {item.evidence.get("reason") for item in issues},
        )

    def test_sum_metric_missing_aggregate_still_fails(self):
        issues = audit_view_shape(
            parse_one(
                "SELECT disease.effective_disease_length AS total "
                "FROM disease"
            ),
            aliases={"disease": "disease"},
            contract=contract_with_metric(
                "sum_effective_disease_length",
                expression=(
                    "SUM(disease.effective_disease_length)"
                ),
                aggregation="sum",
                grain="disease",
                identity="disease.id",
                output_alias="total",
            ),
            dialect="mysql",
        )

        self.assertIn(
            "aggregate_metric_missing",
            {item.evidence.get("reason") for item in issues},
        )

    def test_aggregate_kind_must_match_the_registered_metric(self):
        issues = audit_view_shape(
            parse_one(
                "SELECT COUNT(disease.effective_disease_length) "
                "AS total FROM disease"
            ),
            aliases={"disease": "disease"},
            contract=contract_with_metric(
                "sum_effective_disease_length",
                expression=(
                    "SUM(disease.effective_disease_length)"
                ),
                aggregation="sum",
                subject="disease",
                view="count",
                grain="disease",
                identity="disease.id",
                output_alias="total",
            ),
            dialect="mysql",
        )

        self.assertIn(
            "aggregate_metric_kind_mismatch",
            {item.evidence.get("reason") for item in issues},
        )

    def test_row_metric_needs_own_grain_proof_in_grouped_view(self):
        issues = audit_view_shape(
            parse_one(
                "SELECT route.route_length_km AS route_length_km "
                "FROM disease JOIN route "
                "ON disease.routes_id = route.id"
            ),
            aliases={"disease": "disease", "route": "route"},
            contract=contract_with_metric(
                "route_length_km",
                expression="route.route_length_km",
                aggregation="row",
                subject="disease",
                view="distribution",
            ),
            dialect="mysql",
        )

        self.assertIn(
            "metric_grain_unsafe",
            {item.evidence.get("reason") for item in issues},
        )

    def test_unsafe_metric_cannot_be_aggregated_across_disease_join(self):
        metric = MetricCapability(
            metric_id="aggregated_route_length",
            expression="SUM(route.route_length_km)",
            label="路线长度合计",
            unit="km",
            requires_join=("route",),
            aggregation="sum",
            grain="route",
            identity="route.id",
            fanout_safe=False,
            source_kind="resources",
            source_reference="test.metric",
        )

        issues = validate_metric_semantics(metric, subject="disease")

        self.assertEqual(issues[0].code, "metric_requires_own_grain")


if __name__ == "__main__":
    unittest.main()
