import unittest

from app.query.analysis import _metric_tuple
from app.query.capability_registry import CapabilityRegistry
from app.query.fields import FieldReachSet
from app.query.metrics.semantics import validate_metric_semantics
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

    def test_auto_mileage_metrics_share_the_registered_km_conversion(self):
        """自动指标不能绕过显式指标的单位换算。

        patrol_mileage 在数据库里以米存储，对外指标统一使用公里。
        """

        metrics = self.registry.snapshot.metrics["patrol"]
        for metric_id in (
            "sum_patrol_mileage",
            "avg_patrol_mileage",
            "min_patrol_mileage",
            "max_patrol_mileage",
        ):
            metric = metrics[metric_id]
            self.assertIn("/ 1000.0", metric.expression)
            self.assertEqual(metric.unit, "km")

    def test_planner_view_exposes_metric_and_derived_metric_contracts(self):
        view = self.registry.planner_view("disease", FieldReachSet()).to_prompt_dict()

        route_length = view["metrics"]["route_length_km"]
        density = view["derived_metrics"]["disease_density"]

        self.assertEqual(route_length["unit"], "km")
        self.assertEqual(route_length["aggregation"], "row")
        self.assertEqual(route_length["grain"], "route")
        self.assertEqual(route_length["identity"], "route.id")
        self.assertFalse(route_length["fanout_safe"])
        self.assertEqual(route_length["source_kind"], "resources")
        self.assertEqual(
            density["operands"],
            ["count", "route_length_km"],
        )
        self.assertEqual(density["evaluator"], "post_calc")

    def test_every_registered_metric_has_valid_semantics(self):
        for subject, metrics in self.registry.snapshot.metrics.items():
            for metric_id, metric in metrics.items():
                if metric_id != metric.metric_id:
                    continue
                self.assertEqual(
                    validate_metric_semantics(
                        metric,
                        subject=subject,
                    ),
                    (),
                    f"{subject}.{metric_id}",
                )

    def test_length_metrics_use_semantic_length_field(self):
        """原始列 disease_length 大面积为空（坑槽 44 条全为 NULL），
        平均/最长长度必须走语义字段 effective_disease_length。"""

        metrics = self.registry.snapshot.metrics["disease"]

        for metric_id in ("avg_disease_length", "max_disease_length"):
            expression = metrics[metric_id].expression
            self.assertIn("effective_disease_length", expression)
            self.assertNotIn("}.disease_length", expression)

    def test_effective_length_aliases_resolve_to_registered_metric_ids(self):
        self.assertEqual(
            _metric_tuple(["AVG(effective_disease_length)"]),
            ("avg_disease_length",),
        )
        self.assertEqual(
            _metric_tuple(["avg_effective_disease_length"]),
            ("avg_disease_length",),
        )

    def test_metric_views_follow_the_registered_aggregate_function(self):
        disease = self.registry.snapshot.metrics["disease"]
        patrol = self.registry.snapshot.metrics["patrol"]

        self.assertIn("sum", disease["sum_effective_disease_length"].allowed_views)
        self.assertIn("avg", disease["avg_disease_length"].allowed_views)
        self.assertIn("sum", patrol["patrol_mileage"].allowed_views)
        self.assertNotIn("sum", disease["count"].allowed_views)
        self.assertNotIn("avg", disease["count"].allowed_views)

    def test_distinct_type_count_metric_is_registered(self):
        """“多少种病害类型”是去重计数，不能拿 count 顶替。"""

        metric = self.registry.snapshot.metrics["disease"][
            "distinct_problem_type_count"
        ]

        self.assertIn("COUNT(DISTINCT", metric.expression.upper())
        self.assertIn("problem_type_name", metric.expression)
        self.assertEqual(metric.unit, "种")

    def test_strip_converted_area_uses_point_two_meter_width(self):
        """条状病害 0.2 米宽换算面积是注册指标，不是模型自造公式。"""

        metric = self.registry.snapshot.metrics["disease"]["strip_converted_area"]

        self.assertIn("effective_disease_length * 0.2", metric.expression)
        self.assertIn("problem_type_name = '条状修补'", metric.expression)
        self.assertIn("ELSE 0", metric.expression)
        self.assertEqual(metric.unit, "㎡")
        self.assertIn("top_groups", metric.allowed_views)

    def test_strip_disease_family_expands_to_registered_member(self):
        """“条状病害”是实体族，解析后落到注册成员条状修补。"""

        family = self.registry.snapshot.entity_families["strip_disease"]

        self.assertEqual(family.entity, "disease_type")
        self.assertEqual(family.members, ("条状修补",))
        self.assertIn("条状病害", {family.label, *family.aliases})


if __name__ == "__main__":
    unittest.main()
