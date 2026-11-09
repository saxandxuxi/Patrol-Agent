import unittest
from unittest.mock import patch

from app.services import preset_lib
from app.query.template_sources import contract_from_builtin, contract_from_preset


class TemplateSourceTests(unittest.TestCase):
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
