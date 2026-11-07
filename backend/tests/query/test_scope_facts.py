import unittest
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.contracts import ExecutionObservation
from app.query.scope_facts import INSIDE, OUTSIDE, empty_scope_facts
from app.query.service import _aggregate_is_empty


def binding(field, op, value):
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="test",
        raw_text=str(value),
    )


def spec(filters):
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view="count",
        metrics=("count",),
        dimensions=(),
        filters=filters,
        output_projection=("count",),
        order_by=(),
        limit=None,
        required_assertions=(),
        question="结构化子问题",
    )


FACTS = {
    "route": "长沙－芷江高速公路",
    "data_min_pile": 235,
    "data_max_pile": 327412,
    "data_count": 13769,
}


class ScopeFactsTests(unittest.TestCase):
    @patch("app.services.calibrate.route_facts", return_value=FACTS)
    def test_pile_range_beyond_coverage_is_outside(self, _facts):
        """K400~K410 超出该路线桩号覆盖（K0+235~K327+412），不能当成 0 处。"""

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "=", "route-1"),
                    binding("pile_number", "between", (400000, 410000)),
                )
            )
        )

        self.assertEqual(facts.verdict, OUTSIDE)
        self.assertIn("K0+235~K327+412", facts.detail)

    @patch("app.services.calibrate.route_facts", return_value=FACTS)
    def test_pile_range_inside_coverage_is_inside(self, _facts):
        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "=", "route-1"),
                    binding("pile_number", "between", (16500, 17000)),
                )
            )
        )

        self.assertEqual(facts.verdict, INSIDE)

    @patch("app.services.calibrate.route_facts", return_value=FACTS)
    def test_partially_overlapping_range_is_not_rejected(self, _facts):
        """区间和覆盖只搭上一部分时算有数据，不能误判为超出覆盖。"""

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "=", "route-1"),
                    binding("pile_number", "between", (300000, 400000)),
                )
            )
        )

        self.assertEqual(facts.verdict, INSIDE)

    def test_no_window_filters_returns_empty_facts(self):
        facts = empty_scope_facts(
            spec((binding("route_name", "=", "route-1"),))
        )

        self.assertEqual(facts.verdict, "")

    def test_count_zero_row_is_treated_as_empty_scope(self):
        """COUNT 无匹配行时返回 1 行 0，不能当成"有数据、结果是 0"。"""

        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*)",
            rows_fetched=1,
            preview_rows=({"病害数量": "0"},),
            result_rows=({"病害数量": "0"},),
        )

        self.assertTrue(_aggregate_is_empty(spec(()), observation))

    def test_non_zero_count_row_is_not_empty_scope(self):
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*)",
            rows_fetched=1,
            preview_rows=({"病害数量": "12"},),
            result_rows=({"病害数量": "12"},),
        )

        self.assertFalse(_aggregate_is_empty(spec(()), observation))

    def test_sum_null_row_is_empty_scope_but_zero_is_real_data(self):
        null_observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT SUM(area)",
            rows_fetched=1,
            preview_rows=({"总面积": None},),
            result_rows=({"总面积": None},),
        )
        zero_observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT SUM(area)",
            rows_fetched=1,
            preview_rows=({"总面积": "0"},),
            result_rows=({"总面积": "0"},),
        )
        sum_view = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="sum",
            metrics=("sum_area",),
            dimensions=(),
            filters=(),
            output_projection=("sum_area",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="结构化子问题",
        )

        self.assertTrue(_aggregate_is_empty(sum_view, null_observation))
        self.assertFalse(_aggregate_is_empty(sum_view, zero_observation))


if __name__ == "__main__":
    unittest.main()
