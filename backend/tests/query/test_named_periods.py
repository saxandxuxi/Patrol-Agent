import unittest
from datetime import datetime

from app.query.contracts import PlanNode, QueryPlan, QuerySpec, ValueBinding
from app.query.periods import (
    normalize_named_period_window,
    resolve_named_period,
)
from app.services.parsers import parse_time_range

from app.semantic_aids import detect_evidence


def spec(node_id: str, filters: tuple[ValueBinding, ...]) -> QuerySpec:
    return QuerySpec(
        id=node_id,
        kind="mysql",
        subject="disease",
        view="distribution",
        metrics=("count",),
        dimensions=("route_name",),
        filters=filters,
        output_projection=(),
        order_by=(),
        limit=None,
        required_assertions=(),
        question="统计路线分布",
    )


def bound(field: str, op: str, value: object) -> ValueBinding:
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="model_inference",
        resolver="time_range",
    )


class NamedPeriodTests(unittest.TestCase):
    def test_named_quarter_resolves_to_whole_calendar_quarter(self):
        now = datetime(2026, 11, 7, 10, 0, 0)

        self.assertEqual(
            resolve_named_period("本季度", now),
            (datetime(2026, 10, 1), datetime(2027, 1, 1)),
        )
        self.assertEqual(
            resolve_named_period("第四季度", now),
            (datetime(2026, 10, 1), datetime(2027, 1, 1)),
        )
        self.assertEqual(
            resolve_named_period("本月", now),
            (datetime(2026, 11, 1), datetime(2026, 12, 1)),
        )

    def test_named_period_evidence_is_centralized(self):
        self.assertIn(
            "named_period",
            {item.rule_id for item in detect_evidence("第四季度以来坑槽分布")},
        )
        self.assertNotIn(
            "named_period",
            {item.rule_id for item in detect_evidence("10月1日到10月31日泛油分布")},
        )
        self.assertIn(
            "named_period",
            {item.rule_id for item in detect_evidence("最近三个月的病害趋势")},
        )

    def test_recent_months_use_a_complete_rolling_day_window(self):
        now = datetime(2026, 11, 9, 4, 18, 43)

        self.assertEqual(
            resolve_named_period("最近三个月", now),
            (datetime(2026, 8, 9), datetime(2026, 11, 10)),
        )

    def test_relative_year_with_explicit_month_keeps_month_granularity(self):
        now = datetime(2026, 11, 9, 4, 18, 43)

        self.assertEqual(
            parse_time_range("今年11月", now),
            (datetime(2026, 11, 1), datetime(2026, 12, 1)),
        )
        self.assertEqual(
            parse_time_range("去年11月", now),
            (datetime(2025, 11, 1), datetime(2025, 12, 1)),
        )

    def test_rolling_window_replaces_duplicate_equivalent_time_filters(self):
        duplicate = (
            bound(
                "collection_time",
                "between",
                ("2026-08-09 04:18:43", "2026-11-09 04:18:43"),
            ),
            bound(
                "collection_time",
                "between",
                ("2026-08-09 04:18:43", "2026-11-09 04:18:43"),
            ),
        )
        plan = QueryPlan(
            question="长沙－芷江高速公路最近三个月的病害数量变化趋势",
            nodes=(
                PlanNode(
                    id="q1",
                    spec=spec("q1", duplicate),
                ),
            ),
        )

        normalized = normalize_named_period_window(
            plan,
            ["最近三个月"],
            datetime(2026, 11, 9, 4, 18, 43),
        )

        filters = normalized.nodes[0].spec.filters
        self.assertEqual(len(filters), 1)
        self.assertEqual(
            filters[0].storage_value,
            ["2026-08-09 00:00:00", "2026-11-10 00:00:00"],
        )

    def test_clipped_quarter_window_is_widened(self):
        plan = QueryPlan(
            question="第四季度以来坑槽分布",
            nodes=(
                PlanNode(
                    id="q1",
                    spec=spec(
                        "q1",
                        (
                            bound("collection_time", ">=", "2026-10-01 00:00:00"),
                            bound("collection_time", "<", "2026-11-07 00:00:00"),
                        ),
                    ),
                ),
            ),
        )

        normalized = normalize_named_period_window(
            plan,
            ["第四季度"],
            datetime(2026, 11, 7, 10, 0, 0),
        )

        filters = normalized.nodes[0].spec.filters
        self.assertEqual(filters[1].storage_value, "2027-01-01 00:00:00")
        self.assertEqual(filters[1].resolver, "named_period")

    def test_month_inside_current_year_is_not_widened_to_the_year_end(self):
        plan = QueryPlan(
            question="今年10月全路网的坑槽分布",
            nodes=(
                PlanNode(
                    id="q1",
                    spec=spec(
                        "q1",
                        (
                            bound("collection_time", ">=", "2026-10-01 00:00:00"),
                            bound("collection_time", "<", "2026-11-01 00:00:00"),
                        ),
                    ),
                ),
            ),
        )

        normalized = normalize_named_period_window(
            plan,
            ["今年"],
            datetime(2026, 11, 8, 10, 0, 0),
        )

        self.assertEqual(normalized, plan)

    def test_previous_quarter_node_is_not_widened(self):
        plan = QueryPlan(
            question="本季度和上季度对比",
            nodes=(
                PlanNode(
                    id="now",
                    spec=spec(
                        "now",
                        (
                            bound("collection_time", ">=", "2026-10-01 00:00:00"),
                            bound("collection_time", "<", "2026-11-07 00:00:00"),
                        ),
                    ),
                ),
                PlanNode(
                    id="before",
                    spec=spec(
                        "before",
                        (
                            bound("collection_time", ">=", "2026-07-01 00:00:00"),
                            bound("collection_time", "<", "2026-10-01 00:00:00"),
                        ),
                    ),
                ),
            ),
        )

        normalized = normalize_named_period_window(
            plan,
            ["本季度"],
            datetime(2026, 11, 7, 10, 0, 0),
        )

        self.assertEqual(
            normalized.nodes[0].spec.filters[1].storage_value,
            "2027-01-01 00:00:00",
        )
        self.assertEqual(
            normalized.nodes[1].spec.filters[1].storage_value,
            "2026-10-01 00:00:00",
        )


if __name__ == "__main__":
    unittest.main()
