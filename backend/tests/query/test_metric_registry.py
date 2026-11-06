import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.fields import FieldReachSet
from app.query.metric_compiler import compile_registered_metric


class MetricRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_disease_density_is_derived_not_atomic(self):
        self.assertIsNone(
            self.registry.snapshot.metrics["disease"].get("disease_density")
        )
        self.assertIsNotNone(
            self.registry.snapshot.derived_metrics["disease"].get(
                "disease_density"
            )
        )

    def test_every_registered_metric_compiles_for_declared_views(self):
        aliases = {
            "disease": "d",
            "patrol": "p",
        }
        for subject, metrics in self.registry.snapshot.metrics.items():
            for metric in metrics.values():
                sql = compile_registered_metric(
                    subject,
                    metric,
                    aliases,
                )
                self.assertTrue(sql, f"{subject}.{metric.metric_id}")

    def test_numeric_physical_columns_become_registered_metrics(self):
        names = set(self.registry.snapshot.metrics["disease"])
        self.assertIn("sum_area", names)
        self.assertIn("avg_area", names)

    def test_planner_view_exposes_metric_and_derived_metric_contracts(self):
        view = self.registry.planner_view("disease", FieldReachSet()).to_prompt_dict()

        route_length = view["metrics"]["route_length_km"]
        density = view["derived_metrics"]["disease_density"]

        self.assertEqual(route_length["unit"], "km")
        self.assertEqual(
            density["operands"],
            ["count", "route_length_km"],
        )
        self.assertEqual(density["evaluator"], "post_calc")


if __name__ == "__main__":
    unittest.main()
