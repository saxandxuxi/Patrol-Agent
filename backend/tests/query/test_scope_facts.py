import unittest
from dataclasses import replace
from unittest.mock import patch

from app.query.contracts import QuerySpec, ValueBinding
from app.query.contracts import ExecutionObservation
from app.query.scope_facts import (
    INSIDE,
    OUTSIDE,
    PARTIAL,
    empty_scope_facts,
    spatial_coverage_facts,
)
from app.query.service import _aggregate_is_empty
from app.services.calibrate import route_facts


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

TIME_FACTS = {
    "route-1": {
        "route": "路线甲",
        "data_min_time": "2026-10-01 08:00:00",
        "data_max_time": "2026-10-31 18:00:00",
        "data_count": 120,
    },
    "route-2": {
        "route": "路线乙",
        "data_min_time": "2026-10-20 09:00:00",
        "data_max_time": "2026-10-27 17:00:00",
        "data_count": 80,
    },
}


class ScopeFactsTests(unittest.TestCase):
    @patch("app.services.calibrate._query")
    def test_route_facts_resolves_exact_short_name_before_querying_disease_scope(self, query):
        query.side_effect = [
            [{"route_id": "route-id-1", "short_name": "路线甲"}],
            [
                {
                    "min_pile": 0,
                    "max_pile": 1000,
                    "min_time": "2026-10-01 00:00:00",
                    "max_time": "2026-10-31 23:59:59",
                    "cnt": 10,
                }
            ],
        ]

        facts = route_facts("路线甲-精确短名测试")

        self.assertEqual(facts["route_id"], "route-id-1")
        self.assertIn("routes_id = 'route-id-1'", query.call_args_list[1].args[0])

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
        self.assertIn("超出该路线桩号覆盖范围", facts.detail)
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
    def test_grouped_pile_distribution_carries_catalog_coverage(self, _facts):
        grouped = replace(
            spec((binding("route_name", "=", "route-1"),)),
            view="distribution",
            dimensions=("pile_segment_1000m",),
            output_projection=("pile_segment_1000m", "count"),
        )

        facts = spatial_coverage_facts(grouped)

        self.assertEqual(facts.verdict, INSIDE)
        self.assertIn("K0+235~K327+412", facts.detail)

    @patch("app.services.calibrate.route_facts", return_value=FACTS)
    def test_partially_overlapping_range_is_partial_coverage(self, _facts):
        """区间只覆盖一部分时必须单独标成 partial，不能冒充完整覆盖。"""

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "=", "route-1"),
                    binding("pile_number", "between", (300000, 400000)),
                )
            )
        )

        self.assertEqual(facts.verdict, PARTIAL)
        self.assertIn("未覆盖部分不能判定为无病害", facts.detail)

    def test_no_window_filters_returns_empty_facts(self):
        facts = empty_scope_facts(
            spec((binding("route_name", "=", "route-1"),))
        )

        self.assertEqual(facts.verdict, "")

    @patch("app.services.calibrate.route_facts")
    def test_all_multi_route_time_windows_beyond_coverage_are_outside(self, route_facts):
        route_facts.side_effect = TIME_FACTS.__getitem__

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "in", ["route-1", "route-2"]),
                    binding(
                        "collection_time",
                        "between",
                        ("2026-11-01 00:00:00", "2026-12-01 00:00:00"),
                    ),
                )
            )
        )

        self.assertEqual(facts.verdict, OUTSIDE)
        self.assertIn("路线甲", facts.detail)
        self.assertIn("路线乙", facts.detail)
        self.assertIn("2026-10-31 18:00", facts.detail)
        self.assertIn("2026-10-27 17:00", facts.detail)

    @patch("app.services.calibrate.route_facts")
    def test_multi_route_time_window_inside_every_route_is_inside(self, route_facts):
        route_facts.side_effect = lambda route_ref: {
            **TIME_FACTS[route_ref],
            "data_min_time": "2026-11-01 00:00:00",
            "data_max_time": "2026-11-30 23:59:59",
        }

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "in", ["route-1", "route-2"]),
                    binding(
                        "collection_time",
                        "between",
                        ("2026-11-02 00:00:00", "2026-11-03 00:00:00"),
                    ),
                )
            )
        )

        self.assertEqual(facts.verdict, INSIDE)
        self.assertIn("路线甲", facts.detail)
        self.assertIn("路线乙", facts.detail)

    @patch("app.services.calibrate.route_facts")
    def test_multi_route_mixed_time_coverage_is_outside_and_names_partial_gap(self, route_facts):
        route_facts.side_effect = lambda route_ref: (
            TIME_FACTS["route-1"]
            if route_ref == "route-1"
            else {
                **TIME_FACTS["route-2"],
                "data_min_time": "2026-11-01 00:00:00",
                "data_max_time": "2026-11-30 23:59:59",
            }
        )

        facts = empty_scope_facts(
            spec(
                (
                    binding("route_name", "in", ["route-1", "route-2"]),
                    binding(
                        "collection_time",
                        "between",
                        ("2026-11-01 00:00:00", "2026-12-01 00:00:00"),
                    ),
                )
            )
        )

        self.assertEqual(facts.verdict, OUTSIDE)
        self.assertIn("部分路线覆盖不足", facts.detail)
        self.assertIn("路线甲", facts.detail)
        self.assertIn("2026-10-31 18:00", facts.detail)
        self.assertIn("路线乙", facts.detail)

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
