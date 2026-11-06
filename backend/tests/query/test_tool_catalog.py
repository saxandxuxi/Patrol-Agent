import unittest

from app.services import tools


class CalculationToolCatalogTests(unittest.TestCase):
    def test_calculation_tools_are_registered(self):
        names = {item.name for item in tools.list_tools(layer="big")}

        self.assertTrue(
            {
                "calculate_scalar",
                "calculate_columns",
                "calculate_series",
            }.issubset(names)
        )

    def test_calculation_tool_returns_structured_failure(self):
        result = tools.run_tool(
            "calculate_scalar",
            operation="divide",
            operands=[
                {"name": "numerator", "value": 1},
                {"name": "denominator", "value": 0},
            ],
        )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["failure"]["failure_code"],
            "calculation_zero_denominator",
        )

    def test_column_tool_preserves_context(self):
        result = tools.run_tool(
            "calculate_columns",
            operation="divide",
            rows=[
                {
                    "route_name": "A",
                    "count": 10,
                    "length_km": 5,
                }
            ],
            operands={
                "numerator": "count",
                "denominator": "length_km",
            },
            output_field="density",
            precision=4,
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["data"]["preview_rows"][0]["route_name"],
            "A",
        )
        self.assertEqual(
            result["data"]["preview_rows"][0]["density"],
            "2.0000",
        )

    def test_catalog_contains_calculation_tools(self):
        names = {
            item["name"]
            for item in tools.catalog()["tools"]
        }

        self.assertIn("calculate_scalar", names)
        self.assertIn("calculate_columns", names)
        self.assertIn("calculate_series", names)

    def test_column_tool_schema_exposes_registry_zero_policy(self):
        tool = tools.get_tool("calculate_columns")

        self.assertEqual(
            tool.input_schema["properties"]["zero_denominator"]["enum"],
            ["error", "skip_rows"],
        )


if __name__ == "__main__":
    unittest.main()
