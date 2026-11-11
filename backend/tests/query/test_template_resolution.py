import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.fields import PhysicalSchemaView
from app.query.preset_bindings import values_from_spec
from app.query.template_sources import contract_from_preset
from app.query.templates import TemplateContract, TemplateProvider, resolve_templates
from app.services import preset_lib
from tests.query.factories import count_spec
from tests.query.preset_fixture import PresetFixtureTestCase


class FixedProvider(TemplateProvider):
    def __init__(self, contracts):
        self.contracts = contracts

    def declared_contracts(self, spec, schema):
        return self.contracts


class TemplateResolutionTests(PresetFixtureTestCase):
    def test_date_granularity_follows_time_dimension(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("count",),
            dimensions=("month",),
            filters=(),
            output_projection=(),
            order_by=({"field": "month", "direction": "asc"},),
            limit=None,
            required_assertions=(),
        )

        self.assertEqual(values_from_spec(spec)["date_granularity"], "month")

    def test_granularity_parameter_does_not_cover_time_range_filter(self):
        raw = preset_lib.query_by_id("route_month_trend")
        self.assertIsNotNone(raw)
        contract = contract_from_preset(raw)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="trend",
            metrics=("count",),
            dimensions=("month",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="route-id",
                    display_value="测试路线",
                    source="user_explicit",
                    resolver="route_dictionary",
                    raw_text="测试路线",
                ),
                ValueBinding(
                    field="collection_time",
                    op="between",
                    storage_value=("2026-08-09", "2026-11-10"),
                    display_value="最近三个月",
                    source="user_explicit",
                    resolver="named_period",
                    raw_text="最近三个月",
                ),
            ),
            output_projection=(),
            order_by=({"field": "month", "direction": "asc"},),
            limit=None,
            required_assertions=(),
        )

        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )

        self.assertEqual(result.candidates, ())
        self.assertTrue(
            any("missing_filter:time_range" in item for item in result.rejections)
        )

    def test_preset_with_unbound_required_time_is_not_offered(self):
        raw = preset_lib.query_by_id("device_month_records")
        self.assertIsNotNone(raw)
        contract = contract_from_preset(raw)
        result = resolve_templates(
            count_spec(
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
                )
            ),
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )

        self.assertEqual(result.candidates, ())
        self.assertIn("missing_required_filter:time_range", result.rejections[0])

    def test_preset_accepts_bound_time_range_and_device(self):
        raw = preset_lib.query_by_id("device_month_records")
        self.assertIsNotNone(raw)
        contract = contract_from_preset(raw)
        result = resolve_templates(
            count_spec(
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
                )
            ),
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            result.candidates[0].candidate_id,
            "preset:device_month_records:q1:v1",
        )

    def test_preset_sql_accepts_the_bound_time_column(self):
        raw = preset_lib.query_by_id("patrol_times")
        self.assertIsNotNone(raw)
        contract = contract_from_preset(raw)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="create_time",
                    op=">=",
                    storage_value="2026-10-31 00:00:00",
                    display_value="2026-10-31",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="2026年10月31日",
                ),
                ValueBinding(
                    field="create_time",
                    op="<",
                    storage_value="2026-11-01 00:00:00",
                    display_value="2026-11-01",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="2026年10月31日",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )

        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )

        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(
            [item.field for item in result.candidates[0].bound_parameters],
            ["create_time", "create_time"],
        )

    def test_preset_sql_rejects_missing_bound_time_column(self):
        raw = dict(preset_lib.query_by_id("patrol_times"))
        raw["sql"] = str(raw["sql"]).replace("t.create_time", "t.update_time")
        contract = contract_from_preset(raw)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(
                ValueBinding(
                    field="create_time",
                    op=">=",
                    storage_value="2026-10-31 00:00:00",
                    display_value="2026-10-31",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="2026年10月31日",
                ),
                ValueBinding(
                    field="create_time",
                    op="<",
                    storage_value="2026-11-01 00:00:00",
                    display_value="2026-11-01",
                    source="user_explicit",
                    resolver="time_range",
                    raw_text="2026年10月31日",
                ),
            ),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )

        with patch.object(preset_lib, "query_by_id", return_value=raw):
            result = resolve_templates(
                spec,
                PhysicalSchemaView(columns=frozenset()),
                [FixedProvider([contract])],
            )

        self.assertEqual(result.candidates, ())
        self.assertTrue(
            any("missing_bound_column:create_time" in item for item in result.rejections)
        )

    def test_preset_with_identifier_outside_options_is_not_offered(self):
        raw = preset_lib.query_by_id("device_month_records")
        self.assertIsNotNone(raw)
        contract = contract_from_preset(raw)
        result = resolve_templates(
            count_spec(
                filters=(
                    ValueBinding(
                        field="device_code",
                        op="=",
                        storage_value="102367594561",
                        display_value="102367594561",
                        source="user_explicit",
                        resolver="identifier",
                        raw_text="102367594561",
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
                )
            ),
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )

        self.assertEqual(result.candidates, ())
        self.assertIn("unbound:", result.rejections[0])

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
