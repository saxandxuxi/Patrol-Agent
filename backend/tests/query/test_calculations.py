import unittest
from decimal import Decimal

from app.calculations.columns import calculate_columns
from app.calculations.domain.trend import render_series_calculation
from app.calculations.evidence import compact_table_evidence
from app.calculations.models import CalculationOperand, CalculationRequest
from app.calculations.numbers import as_decimal, numeric_value
from app.calculations.registry import CalculationRegistry
from app.calculations.scalar import calculate_scalar
from app.calculations.series import calculate_series


def operand(name: str, value: object) -> CalculationOperand:
    return CalculationOperand(name=name, value=value)


def scalar_request(operation: str, **values: object) -> CalculationRequest:
    roles = {
        "add": ("left", "right"),
        "subtract": ("left", "right"),
        "multiply": ("left", "right"),
        "divide": ("numerator", "denominator"),
        "ratio": ("numerator", "denominator"),
        "percentage_share": ("numerator", "total"),
        "percentage_change": ("old", "new"),
    }[operation]
    return CalculationRequest(
        tool="calculate_scalar",
        operation=operation,
        operands=tuple(operand(role, values[role]) for role in roles),
    )


class ScalarCalculationTests(unittest.TestCase):
    def test_scalar_add_subtract_multiply_divide(self):
        self.assertEqual(
            calculate_scalar(
                scalar_request("add", left=2, right=3)
            ).value,
            Decimal("5"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("subtract", left=5, right=3)
            ).value,
            Decimal("2"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("multiply", left=4, right=3)
            ).value,
            Decimal("12"),
        )
        self.assertEqual(
            calculate_scalar(
                scalar_request("divide", numerator=12, denominator=4)
            ).value,
            Decimal("3"),
        )

    def test_scalar_zero_denominator_is_structured_failure(self):
        result = calculate_scalar(
            scalar_request("divide", numerator=12, denominator=0)
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_zero_denominator",
        )

    def test_percentage_share_and_change_use_named_roles(self):
        share = calculate_scalar(
            scalar_request(
                "percentage_share",
                numerator=25,
                total=200,
            )
        )
        change = calculate_scalar(
            scalar_request("percentage_change", old=100, new=80)
        )

        self.assertEqual(share.value, Decimal("12.5"))
        self.assertEqual(change.value, Decimal("-20"))

    def test_unknown_operation_is_rejected(self):
        request = CalculationRequest(
            tool="calculate_scalar",
            operation="eval",
            operands=(
                operand("left", 1),
                operand("right", 2),
            ),
        )

        result = calculate_scalar(request)

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_operation_unknown",
        )

    def test_registry_loads_closed_operation_set(self):
        registry = CalculationRegistry.load()

        self.assertIn("calculate_scalar", registry.snapshot.calculators)
        self.assertIn(
            "divide",
            registry.snapshot.calculators["calculate_scalar"].operations,
        )

    def test_precision_zero_is_preserved(self):
        request = scalar_request("add", left=1, right=2)
        request = CalculationRequest(
            tool=request.tool,
            operation=request.operation,
            operands=request.operands,
            precision=0,
        )

        result = calculate_scalar(request)

        self.assertEqual(result.value.as_tuple().exponent, 0)

    def test_invalid_precision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "precision"):
            CalculationRequest.from_dict(
                {
                    "tool": "calculate_scalar",
                    "operation": "add",
                    "operands": [],
                    "precision": "bad",
                }
            )

    def test_infinity_is_not_a_valid_number(self):
        self.assertIsNone(as_decimal("Infinity"))
        self.assertIsNone(as_decimal("NaN"))

    def test_numeric_value_uses_exact_field_name(self):
        value = numeric_value(
            {
                "rows": [
                    {
                        "count_total": "99",
                        "count": "7",
                    }
                ]
            },
            "count",
        )

        self.assertEqual(value, Decimal("7"))


class ColumnCalculationTests(unittest.TestCase):
    def test_column_division_preserves_row_context(self):
        result = calculate_columns(
            "divide",
            [
                {
                    "id": "A",
                    "route_name": "路线A",
                    "count": 10,
                    "length_km": 5,
                }
            ],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
            precision=4,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(result.rows[0]["id"], "A")
        self.assertEqual(result.rows[0]["route_name"], "路线A")
        self.assertEqual(result.rows[0]["density"], Decimal("2.0000"))

    def test_column_zero_denominator_reports_row(self):
        result = calculate_columns(
            "divide",
            [{"id": "A", "count": 1, "length_km": 0}],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_zero_denominator",
        )
        self.assertEqual(result.evidence["row_ids"], ["A"])

    def test_column_missing_role_column_is_structured_failure(self):
        result = calculate_columns(
            "divide",
            [{"id": "A", "count": 1}],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
        )

        self.assertFalse(result.accepted)
        self.assertEqual(
            result.failure_code,
            "calculation_operand_missing",
        )

    def test_column_skip_rows_reports_excluded_rows(self):
        result = calculate_columns(
            "divide",
            [
                {"id": "A", "count": 10, "length_km": 5},
                {"id": "B", "count": 3, "length_km": 0},
            ],
            {
                "numerator": "count",
                "denominator": "length_km",
            },
            "density",
            zero_denominator="skip_rows",
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.rows), 1)
        self.assertEqual(
            result.evidence["excluded_row_ids"],
            ["B"],
        )

    def test_column_percentage_share_accepts_constant_total(self):
        result = calculate_columns(
            "percentage_share",
            [
                {"route_name": "A", "count": 10},
                {"route_name": "B", "count": 30},
            ],
            {
                "numerator": "count",
                "total": "count",
            },
            "share",
            constants={"total": Decimal("40")},
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["share"] for row in result.rows],
            [Decimal("25.000000"), Decimal("75.000000")],
        )


class SeriesCalculationTests(unittest.TestCase):
    def test_series_preserves_zero_batches_and_computes_differences(self):
        result = calculate_series(
            "subtract",
            ["b1", "b2", "b3"],
            [0, 3, 15],
        )

        self.assertTrue(result.accepted)
        self.assertEqual([row["value"] for row in result.rows], [0, 3, 15])
        self.assertEqual(
            [row["difference"] for row in result.rows],
            [None, 3, 12],
        )

    def test_series_percentage_change_uses_previous_value(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [10, 12, 9],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["percentage_change"] for row in result.rows],
            [None, Decimal("20"), Decimal("-25")],
        )

    def test_series_percentage_change_marks_zero_baseline_as_undefined(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [0, 15, 3],
        )

        self.assertTrue(result.accepted)
        self.assertEqual(
            [row["percentage_change"] for row in result.rows],
            [None, None, Decimal("-80")],
        )
        self.assertEqual(result.evidence["undefined_points"], ("b2",))

    def test_series_render_explains_zero_baseline_points(self):
        result = calculate_series(
            "percentage_change",
            ["b1", "b2", "b3"],
            [0, 15, 3],
        )
        request = CalculationRequest.from_dict(
            {
                "tool": "calculate_series",
                "operation": "percentage_change",
            }
        )

        reply = render_series_calculation(result, request)

        self.assertIn("b2 的上一批次为 0", reply)
        self.assertIn("相邻变化为：无，-80", reply)

    def test_compact_evidence_extracts_metric_boundaries(self):
        evidence = compact_table_evidence(
            [
                {"路线": "A", "数量": 10},
                {"路线": "B", "数量": 3},
            ]
        )

        self.assertEqual(evidence["metrics"]["数量"]["max"]["group"], "A")
        self.assertEqual(evidence["metrics"]["数量"]["min"]["group"], "B")


if __name__ == "__main__":
    unittest.main()
