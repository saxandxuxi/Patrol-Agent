import unittest

from app.query.contracts import QuerySpec, ValueBinding
from app.query.fields import (
    FieldReachHit,
    FieldReachSet,
    FieldRef,
    OutputProjection,
    PhysicalSchemaView,
)
from app.query.validator import validate_query_spec


class FieldValidatorTests(unittest.TestCase):
    def test_reach_hit_does_not_prove_physical_column(self):
        reach = FieldReachSet(
            hits=(FieldReachHit("disease", "missing_col", "缺失列", "用户原话", 3.0),)
        )
        schema = PhysicalSchemaView(columns=frozenset({("disease", "id")}))
        self.assertFalse(reach.contains_physical("disease", "missing_col"))
        self.assertFalse(schema.has_column("disease", "missing_col"))

    def test_unknown_physical_field_is_rejected(self):
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
        schema = PhysicalSchemaView(columns=frozenset({("disease", "id")}))
        projection = OutputProjection(
            fields=(FieldRef("disease", "missing_col", "缺失列"),)
        )
        issues = validate_query_spec(spec, schema, projection)
        self.assertIn("unknown_physical_field", {item.code for item in issues})

    def test_model_inferred_value_without_resolver_is_rejected(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="problem_type_name",
                    op="=",
                    storage_value="其他",
                    display_value="其他",
                    source="model_inference",
                    resolver="",
                    raw_text="其他",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        issues = validate_query_spec(
            spec,
            PhysicalSchemaView(columns=frozenset({("disease", "problem_base_id")})),
            OutputProjection(fields=()),
        )
        self.assertIn("unresolved_provenance", {item.code for item in issues})

    def test_model_inferred_identity_value_is_not_dictionary_resolution(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("route_name",),
            filters=(
                ValueBinding(
                    field="patrol_id",
                    op="=",
                    storage_value="LAST_PATROL_ID",
                    display_value="LAST_PATROL_ID",
                    source="model_inference",
                    resolver="identity:patrol_id",
                    raw_text="最近一次巡检",
                ),
            ),
            output_projection=("route_name",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )

        issues = validate_query_spec(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            OutputProjection(fields=()),
        )

        self.assertIn("unresolved_provenance", {item.code for item in issues})
