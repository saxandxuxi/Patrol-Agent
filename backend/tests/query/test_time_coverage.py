import unittest
from datetime import datetime

from app.query.contracts import QuerySpec, ValueBinding
from app.query.time_coverage import time_window_coverage


def binding(field, op, value):
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="test",
        resolver="test",
        raw_text=str(value),
    )


def spec(view, dimension, start, end):
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view=view,
        metrics=("count",),
        dimensions=(dimension,),
        filters=(
            binding("collection_time", ">=", start),
            binding("collection_time", "<", end),
        ),
        output_projection=(dimension, "count"),
        order_by=(),
        limit=None,
        required_assertions=(),
        question="时间覆盖测试",
    )


class TimeCoverageTests(unittest.TestCase):
    def test_quarter_distribution_names_missing_quarter(self):
        note = time_window_coverage(
            spec(
                "distribution",
                "collection_time_quarter",
                "2026-01-01 00:00:00",
                "2027-01-01 00:00:00",
            ),
            [
                {"季度": "2026-Q2", "数量": 10},
                {"季度": "2026-Q3", "数量": 11},
                {"季度": "2026-Q4", "数量": 12},
            ],
        )

        self.assertIn("2026-Q1", note)

    def test_empty_week_trend_is_not_reported_as_a_valid_trend(self):
        note = time_window_coverage(
            spec(
                "trend",
                "week",
                "2026-08-01 00:00:00",
                "2026-09-01 00:00:00",
            ),
            [],
        )

        self.assertIn("没有匹配记录", note)
        self.assertIn("无法判断趋势", note)

    def test_day_trend_reports_observed_and_missing_days(self):
        note = time_window_coverage(
            spec(
                "trend",
                "day",
                "2026-10-27 00:00:00",
                "2026-11-05 00:00:00",
            ),
            [
                {"日期": "2026-10-27", "数量": 10},
                {"日期": "2026-10-31", "数量": 12},
                {"日期": "2026-11-04", "数量": 7},
            ],
        )

        self.assertIn("2026-10-27", note)
        self.assertIn("缺少日期", note)
        self.assertIn("2026-10-28", note)

    def test_partial_month_trend_window_is_flagged(self):
        """2026-10-08 ~ 2026-11-07 按月分桶时首尾月都不完整，
        直接比较 11886 与 1883 会得出“下降 84%”的假结论。"""

        note = time_window_coverage(
            spec(
                "trend",
                "month",
                "2026-10-08 00:00:00",
                "2026-11-08 00:00:00",
            ),
            [
                {"月份": "2026-10", "数量": 11886},
                {"月份": "2026-11", "数量": 1883},
            ],
        )

        self.assertIn("首尾时间桶不完整", note)
        self.assertIn("末月只到 11-07", note)

    def test_complete_month_trend_window_has_no_partial_note(self):
        note = time_window_coverage(
            spec(
                "trend",
                "month",
                "2026-09-01 00:00:00",
                "2026-11-01 00:00:00",
            ),
            [
                {"月份": "2026-09", "数量": 10},
                {"月份": "2026-10", "数量": 12},
            ],
        )

        self.assertEqual(note, "")

