import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.fields import PhysicalSchemaView
from app.services import preset_lib
from app.query.template_sources import (
    BuiltinTemplateProvider,
    contract_from_builtin,
    contract_from_preset,
)
from app.query.templates import resolve_templates
from app.services.nl2sql import builtin_registry_issues


def _binding(field, value, op="="):
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="user_explicit",
        raw_text=str(value),
    )


class TemplateSourceTests(unittest.TestCase):
    def test_builtin_template_registry_has_no_dangling_builders(self):
        self.assertEqual(builtin_registry_issues(), [])

    def test_builtin_metrics_become_exact_support_set(self):
        contract = contract_from_builtin(
            {
                "id": "multi_metric",
                "title": "多指标",
                "source": "builtin",
                "match": {
                    "shapes": ["count"],
                    "subjects": ["disease"],
                    "metrics": ["count", "total_area"],
                },
            }
        )
        self.assertEqual(contract.metrics, ("count", "total_area"))
        self.assertEqual(contract.adapter_id, "builtin")
        self.assertEqual(contract.native_id, "multi_metric")

    def test_preset_fixed_conditions_keep_values(self):
        contract = contract_from_preset(
            {
                "id": "oil_count",
                "title": "泛油数量",
                "match": {
                    "shape": ["count"],
                    "subject": ["disease"],
                    "fixed": {"damage_type": "泛油"},
                },
            }
        )
        self.assertEqual(contract.fixed_conditions[0].display_value, "泛油")
        self.assertEqual(contract.fixed_conditions[0].storage_value, "泛油")
        self.assertEqual(contract.fixed_conditions[0].source, "template_fixed")

    def test_patrol_times_preset_uses_patrol_creation_time(self):
        query = preset_lib.query_by_id("patrol_times")
        sql, reason = preset_lib.bind(
            query,
            {
                "route_id": "__ALL__",
                "date_granularity": "day",
                "date_from": "2026-11-02",
                "date_to": "2026-11-02",
            },
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(sql)
        self.assertIn("t.create_time >=", sql)
        self.assertIn("t.create_time <", sql)
        self.assertNotIn("p.collection_time", sql)
        self.assertNotIn("ai_problem p", sql)

    def test_patrol_times_preset_uses_task_route_relation(self):
        query = preset_lib.query_by_id("patrol_times")
        original_options_for = preset_lib.options_for

        def fake_options(name, spec):
            if name == "route_id":
                return [{"label": "路线1", "value": "route-1"}]
            return original_options_for(name, spec)

        with patch.object(
            preset_lib,
            "options_for",
            side_effect=fake_options,
        ):
            sql, reason = preset_lib.bind(
                query,
                {
                    "route_id": "route-1",
                    "date_granularity": "day",
                    "date_from": "2026-11-02",
                    "date_to": "2026-11-02",
                },
            )

        self.assertEqual(reason, "")
        self.assertIsNotNone(sql)
        self.assertIn("ai_maintain_routes_patrol", sql)
        self.assertIn("pr.routes_id = 'route-1'", sql)
        self.assertNotIn("p.routes_id", sql)

    def test_numeric_distribution_has_a_confirmation_contract(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "sum_area", "sum_effective_disease_length"),
            dimensions=("route_name", "problem_type_name"),
            filters=(
                _binding("collection_time", ("2026-11-01", "2026-12-01"), "between"),
                _binding("effective_disease_length", 2, ">"),
            ),
            output_projection=("route_name",),
            order_by=(),
            limit=None,
            required_assertions=(),
        )

        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [BuiltinTemplateProvider()],
        )

        self.assertIn(
            "disease_numeric_distribution",
            {item.contract.id for item in result.candidates},
        )

    def test_numeric_record_list_has_a_confirmation_contract(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="list",
            metrics=(),
            dimensions=(),
            filters=(_binding("area", 2.5, ">"),),
            output_projection=(
                "problem_number",
                "work_order_id",
                "area",
                "collection_time",
            ),
            order_by=(),
            limit=None,
            required_assertions=(),
        )

        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [BuiltinTemplateProvider()],
        )

        self.assertIn(
            "disease_numeric_records",
            {item.contract.id for item in result.candidates},
        )

    def test_damage_type_top_has_a_confirmation_contract(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count", "sum_area"),
            dimensions=("problem_type_name",),
            filters=(_binding("route_name", "route-1"),),
            output_projection=("problem_type_name",),
            order_by=({"field": "count", "direction": "desc"},),
            limit=3,
            required_assertions=(),
        )

        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [BuiltinTemplateProvider()],
        )

        self.assertIn(
            "disease_type_top",
            {item.contract.id for item in result.candidates},
        )
