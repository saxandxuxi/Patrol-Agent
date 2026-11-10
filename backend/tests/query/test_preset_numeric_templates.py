import unittest
from unittest.mock import patch

from app.routers import preset
from app.services import preset_lib


class PresetNumericTemplateTests(unittest.TestCase):
    def test_numeric_parameter_hint_is_exposed_to_quick_query_ui(self):
        with patch.object(preset, "_options_for", return_value=[]):
            data = preset.preset_queries(with_sql=0)

        spec = data["params"]["min_size_value"]
        self.assertEqual(spec["type"], "numeric")
        self.assertIn("严格大于", spec["hint"])

    def test_threshold_distribution_binds_optional_scope_and_number(self):
        query = preset_lib.query_by_id("disease_threshold_distribution")

        self.assertIsNotNone(query)
        sql, reason = preset_lib.bind(
            query,
            {
                "route_id": "__ALL__",
                "damage_type": "坑槽",
                "size_metric": "area",
                "min_size_value": "2.5",
            },
        )

        self.assertEqual(reason, "")
        self.assertIsNotNone(sql)
        self.assertIn("CASE 'area' WHEN 'area' THEN p.area", sql)
        self.assertIn("> 2.5", sql)
        self.assertNotIn("p.routes_id = '__ALL__'", sql)
        self.assertIn("b.problem_type = '坑槽'", sql)

    def test_threshold_template_rejects_non_numeric_threshold(self):
        query = preset_lib.query_by_id("disease_threshold_records")

        self.assertIsNotNone(query)
        sql, reason = preset_lib.bind(
            query,
            {
                "route_id": "__ALL__",
                "damage_type": "__ALL__",
                "size_metric": "effective_disease_length",
                "min_size_value": "3 OR 1=1",
            },
        )

        self.assertIsNone(sql)
        self.assertIn("数值", reason)

    def test_scope_top_types_binds_route_section_and_limit(self):
        query = preset_lib.query_by_id("disease_type_top_by_scope")

        self.assertIsNotNone(query)
        with patch.object(
            preset_lib,
            "options_for",
            side_effect=lambda name, spec: (
                [{"label": "路线1", "value": "route-1"}]
                if name == "route_id"
                else [{"label": f"前 {i}", "value": str(i)} for i in range(1, 51)]
                if name == "top_n"
                else [{"label": "全部", "value": "__ALL__"}]
            ),
        ):
            sql, reason = preset_lib.bind(
                query,
                {
                    "route_id": "route-1",
                    "section_id": "__ALL__",
                    "top_n": "5",
                },
            )

        self.assertEqual(reason, "")
        self.assertIsNotNone(sql)
        self.assertIn("p.routes_id = 'route-1'", sql)
        self.assertNotIn("p.sections_id = '__ALL__'", sql)
        self.assertIn("LIMIT 5", sql)


if __name__ == "__main__":
    unittest.main()
