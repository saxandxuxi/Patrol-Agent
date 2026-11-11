import unittest

from app.calculations.models import CalculationRequest
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec
from app.query.shape_contracts import (
    canonical_view,
    normalize_post_calc_shape,
    normalize_top_view,
    validate_shape_contract,
)


def order(field, direction="desc"):
    return {"field": field, "direction": direction}


def top_spec(**overrides):
    values = {
        "id": "q1",
        "kind": "mysql",
        "subject": "disease",
        "view": "top",
        "metrics": (),
        "dimensions": (),
        "filters": (),
        "output_projection": ("area",),
        "order_by": (order("area"),),
        "limit": 5,
        "required_assertions": (),
        "question": "按面积取前5条病害记录",
    }
    values.update(overrides)
    return QuerySpec(**values)


def issue_codes(spec):
    return tuple(
        issue.code
        for issue in validate_shape_contract(spec, CapabilityRegistry.load())
    )


class ShapeContractTests(unittest.TestCase):
    def test_top_without_metrics_normalizes_to_records(self):
        spec = normalize_top_view(
            top_spec(
                metrics=(),
                dimensions=(),
                output_projection=("area",),
                order_by=(order("area"),),
            )
        )
        self.assertEqual(spec.view, "top_records")

    def test_top_with_metrics_and_dimensions_normalizes_to_groups(self):
        spec = normalize_top_view(
            top_spec(
                metrics=("count",),
                dimensions=("route_name",),
                output_projection=(),
                order_by=(order("count"),),
            )
        )
        self.assertEqual(spec.view, "top_groups")

    def test_top_records_requires_output_projection(self):
        spec = top_spec(
            view="top_records",
            output_projection=(),
        )
        self.assertEqual(issue_codes(spec), ("missing_output_projection",))

    def test_top_records_rejects_metrics(self):
        spec = top_spec(
            view="top_records",
            metrics=("total_area",),
        )
        self.assertEqual(issue_codes(spec), ("metrics_not_allowed",))

    def test_top_records_requires_order_by(self):
        spec = top_spec(
            view="top_records",
            order_by=(),
        )
        self.assertEqual(issue_codes(spec), ("missing_order_by",))

    def test_top_records_order_field_must_be_projected(self):
        spec = top_spec(
            view="top_records",
            output_projection=("problem_number",),
        )
        self.assertEqual(
            issue_codes(spec),
            ("order_field_not_projected:area",),
        )

    def test_top_groups_requires_metrics(self):
        spec = top_spec(
            view="top_groups",
            metrics=(),
            dimensions=("route_name",),
            output_projection=(),
            order_by=(),
        )
        self.assertEqual(issue_codes(spec), ("missing_metrics",))

    def test_top_groups_requires_dimensions(self):
        spec = top_spec(
            view="top_groups",
            metrics=("count",),
            dimensions=(),
            output_projection=(),
            order_by=(order("count"),),
        )
        self.assertEqual(issue_codes(spec), ("missing_dimensions",))

    def test_missing_top_limit_explains_ordered_complete_result(self):
        spec = top_spec(
            view="top_groups",
            metrics=("count",),
            dimensions=("route_name",),
            output_projection=(),
            order_by=(order("count"),),
            limit=None,
        )
        issues = validate_shape_contract(spec, CapabilityRegistry.load())

        self.assertEqual(
            tuple(issue.code for issue in issues),
            ("missing_limit",),
        )
        self.assertIn("distribution", issues[0].reason)
        self.assertIn("查看全部对象", issues[0].reason)

    def test_top_groups_without_limit_normalizes_to_ordered_distribution(self):
        spec = normalize_top_view(
            top_spec(
                view="top_groups",
                metrics=("count",),
                dimensions=("patrol_number",),
                output_projection=("patrol_number", "count"),
                order_by=(order("count"),),
                limit=None,
            )
        )

        self.assertEqual(spec.view, "distribution")
        self.assertEqual(issue_codes(spec), ())

    def test_unknown_view_is_rejected(self):
        spec = top_spec(view="not_a_view")
        self.assertEqual(issue_codes(spec), ("unsupported_view",))

    def test_distribution_without_dimensions_normalizes_to_aggregate(self):
        """没有分组维度的"分布"就是一行总量：数量 + 总长度按聚合视图执行。

        模型偶尔把"多少处 + 加起来多长"写成 distribution，执行前契约以前会
        直接判"分布必须声明分组维度"，把有数据的问题整条拦死。
        """

        self.assertEqual(
            canonical_view(
                "distribution",
                ("count", "sum_effective_disease_length"),
                (),
                "query_data",
            ),
            "count",
        )

    def test_distribution_with_dimensions_stays_distribution(self):
        self.assertEqual(
            canonical_view(
                "distribution",
                ("count",),
                ("route_name",),
                "query_data",
            ),
            "distribution",
        )

    def test_post_calc_answer_operation_is_normalized_to_data_view(self):
        spec = top_spec(
            id="difference",
            view="single_result",
            output_projection=(),
            order_by=(),
            limit=None,
            calculation=CalculationRequest(
                tool="calculate_scalar",
                operation="subtract",
                operands=(),
            ),
            required_assertions=("preserve_user_scope",),
        )

        normalized = normalize_post_calc_shape(spec)

        self.assertEqual(normalized.view, "data")
        self.assertIsNotNone(normalized.calculation)

