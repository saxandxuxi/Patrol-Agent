import unittest

from app.calculations.semantics import (
    CalculationSourceShape,
    validate_calculation_sources,
)


class CalculationSemanticsTests(unittest.TestCase):
    def test_scalar_density_composes_units_across_subjects(self):
        issues = validate_calculation_sources(
            "calculate_scalar",
            "divide",
            (
                CalculationSourceShape(
                    result_grain="disease",
                    dimensions=(),
                    identity="disease.id",
                    unit="米",
                ),
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=(),
                    identity="route.id",
                    unit="km",
                ),
            ),
            "米/km",
        )

        self.assertEqual(issues, ())

    def test_grouped_sources_with_different_identity_are_rejected(self):
        issues = validate_calculation_sources(
            "calculate_columns",
            "divide",
            (
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=("route_name",),
                    identity="route.id",
                    unit="处",
                ),
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=("route_name",),
                    identity="section.route_id",
                    unit="km",
                ),
            ),
            "处/km",
        )

        self.assertEqual(
            tuple(issue.code for issue in issues),
            ("calculation_grain_mismatch",),
        )

    def test_grouped_denominator_without_identity_is_not_unique(self):
        issues = validate_calculation_sources(
            "calculate_columns",
            "divide",
            (
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=("route_name",),
                    identity="route.id",
                    unit="处",
                ),
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=("route_name",),
                    identity="",
                    unit="km",
                ),
            ),
            "处/km",
        )

        self.assertIn(
            "calculation_denominator_not_unique",
            {issue.code for issue in issues},
        )

    def test_declared_output_unit_must_match_composed_unit(self):
        issues = validate_calculation_sources(
            "calculate_scalar",
            "divide",
            (
                CalculationSourceShape(
                    result_grain="disease",
                    dimensions=(),
                    identity="disease.id",
                    unit="处",
                ),
                CalculationSourceShape(
                    result_grain="route",
                    dimensions=(),
                    identity="route.id",
                    unit="km",
                ),
            ),
            "处/米",
        )

        self.assertEqual(
            tuple(issue.code for issue in issues),
            ("calculation_unit_mismatch",),
        )


if __name__ == "__main__":
    unittest.main()
