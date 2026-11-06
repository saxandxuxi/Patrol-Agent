import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpecError
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.values import resolve_value


def calculation_payload(
    *,
    operation: str = "divide",
    denominator_field: str = "route_length_km",
) -> dict:
    return {
        "final_goal": "计算每条道路的病害密度",
        "intent_family": "analysis",
        "unknown_terms": [],
        "views": [
            {
                "id": "q1",
                "action": "query_data",
                "question": "按路线统计数量和路线长度",
                "subject": "disease",
                "view": "distribution",
                "metrics": ["count", "route_length_km"],
                "dimensions": ["route_name"],
                "filters": [],
                "output_projection": ["route_name", "count", "route_length_km"],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
                "depends_on": [],
                "inputs": [],
            },
            {
                "id": "density",
                "action": "post_calc",
                "question": "用数量除以路线长度计算病害密度",
                "subject": "disease",
                "view": "data",
                "metrics": ["disease_density"],
                "dimensions": ["route_name"],
                "filters": [],
                "output_projection": ["route_name", "disease_density"],
                "order_by": [],
                "limit": None,
                "required_assertions": [
                    "lower_is_better",
                    "multi_metric_comparison",
                ],
                "depends_on": ["q1"],
                "inputs": [],
                "calculation": {
                    "tool": "calculate_columns",
                    "operation": operation,
                    "operands": [
                        {
                            "name": "numerator",
                            "source": {"node": "q1", "field": "count"},
                        },
                        {
                            "name": "denominator",
                            "source": {
                                "node": "q1",
                                "field": denominator_field,
                            },
                        },
                    ],
                    "output": {
                        "name": "disease_density",
                        "label": "病害密度",
                        "unit": "处/km",
                    },
                    "precision": 4,
                },
            },
        ],
    }


class CalculationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_post_calc_parses_structured_calculation(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload()

        plan = analyze_single_turn(
            "计算每条道路的病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        calculation = plan.nodes[-1].spec.calculation
        self.assertIsNotNone(calculation)
        self.assertEqual(calculation.tool, "calculate_columns")
        self.assertEqual(calculation.operation, "divide")
        self.assertEqual(
            [item.name for item in calculation.operands],
            ["numerator", "denominator"],
        )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_unknown_calculation_operation_is_rejected_before_execution(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload(operation="eval")

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_operation_unknown",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_calculation_output_is_allowed_as_post_calc_metric(
        self,
        chat_json,
        _audit,
    ):
        payload = calculation_payload()
        post_calc = payload["views"][1]
        post_calc["metrics"] = ["custom_ratio"]
        post_calc["calculation"]["output"]["name"] = "custom_ratio"
        chat_json.return_value = payload

        plan = analyze_single_turn(
            "计算每条道路的病害密度",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
            audit=True,
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(
            plan.nodes[-1].spec.metrics,
            ("custom_ratio",),
        )

    @patch("app.query.analysis.audit_query_plan", return_value={"complete": True})
    @patch("app.query.analysis.chat_json")
    def test_calculation_source_must_be_produced_by_dependency(
        self,
        chat_json,
        _audit,
    ):
        chat_json.return_value = calculation_payload(
            denominator_field="missing_length",
        )

        with self.assertRaisesRegex(
            QuerySpecError,
            "calculation_operand_missing",
        ):
            analyze_single_turn(
                "计算每条道路的病害密度",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                audit=True,
                registry=self.registry,
                subject="disease",
            )

    def test_named_business_value_resolves_from_registry(self):
        binding = resolve_value(
            "confidence",
            "<",
            "low_confidence",
            "AI识别的病害有多少认不出来",
            registry=self.registry,
            subject="disease",
        )

        self.assertEqual(binding.storage_value, 0.7)
        self.assertEqual(binding.source, "business_rule")
        self.assertEqual(
            binding.resolver,
            "business_value:confidence:low_confidence",
        )


if __name__ == "__main__":
    unittest.main()
