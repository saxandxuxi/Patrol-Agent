import unittest
from decimal import Decimal

from app.calculations.models import CalculationOperand, CalculationRequest
from app.calculations.registry import CalculationRegistry
from app.calculations.scalar import calculate_scalar


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


if __name__ == "__main__":
    unittest.main()
