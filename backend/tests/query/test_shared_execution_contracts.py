"""Unit-5 暴露的跨业务共用契约回归。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from app.calculations.domain.analysis import analyze_evidence
from app.calculations.post_calc import execute_post_calc
from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.entity_resolver import resolve_entity
from app.query.contracts import QuerySpec, ValueBinding
from app.query.scope_facts import (
    INSIDE,
    PARTIAL,
    coverage_facts,
    empty_scope_facts,
)
from app.query.shape_contracts import validate_shape_contract
from app.query.spec_mdl import compile_spec_mdl


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


def dependency(rows):
    return {
        "question": "上海-昆明高速 2026 年 11 月上下行病害数量",
        "reply": "",
        "rows": rows,
        "columns": list(rows[0]) if rows else [],
        "row_count": len(rows),
    }


class AnalysisOnlyNumberGroundingTests(unittest.TestCase):
    @patch("app.calculations.domain.analysis.chat")
    def test_analysis_only_cannot_invent_a_difference_number(self, chat):
        chat.return_value = "上行 99 处，下行 44 处，因此上行比下行多 55 处。"

        result = analyze_evidence(
            "比较上下行病害数量并给出差值",
            [
                dependency(
                    [
                        {"上下行方向": "上行", "数量": 99},
                        {"上下行方向": "下行", "数量": 44},
                    ]
                )
            ],
            requirements={
                "required_assertions": [
                    "multi_metric_comparison",
                    "analysis_only",
                ]
            },
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["failure_code"],
            "analysis_only_new_numeric_value",
        )
        self.assertIn("55", result["ungrounded_numbers"])

    @patch("app.calculations.domain.analysis.chat")
    def test_analysis_only_ignores_numbers_inside_business_identifiers(self, chat):
        chat.return_value = (
            "两次巡检间隔 1 天。最新批次 111 条，上一批次 0 条；"
            "新增编号包含 TZXB202610313448 至 TZXB202610313457，"
            "其中 …457 也属于本次检出。"
        )

        result = analyze_evidence(
            "汇总巡检间隔与新增病害",
            [
                {
                    "question": "计算巡检间隔",
                    "reply": "两次巡检间隔天数 = 1 天。",
                    "rows": [],
                    "columns": [],
                    "row_count": 0,
                },
                {
                    "question": "比较两次巡检的病害集合",
                    "reply": (
                        "最新批次 111 条，上一批次 0 条；"
                        "检出编号 TZXB202610313448 至 TZXB202610313457。"
                    ),
                    "rows": [],
                    "columns": [],
                    "row_count": 0,
                },
            ],
            requirements={"required_assertions": ["analysis_only"]},
        )

        self.assertTrue(result["accepted"], result)

    @patch("app.calculations.domain.analysis.chat")
    def test_grouped_comparison_uses_structured_extremes(self, chat):
        chat.return_value = (
            "包头-茂名和上海-昆明均为下行病害更集中。"
        )

        result = analyze_evidence(
            "每条路上下行哪边病害更多",
            [
                dependency(
                    [
                        {"路线名称": "上海-昆明", "上下行方向": "上行", "数量": 99},
                        {"路线名称": "上海-昆明", "上下行方向": "下行", "数量": 44},
                        {"路线名称": "包头-茂名", "上下行方向": "上行", "数量": 325},
                        {"路线名称": "包头-茂名", "上下行方向": "下行", "数量": 1416},
                    ]
                )
            ],
            requirements={
                "required_assertions": ["multi_metric_comparison"]
            },
        )

        self.assertTrue(result["accepted"])
        self.assertIn("上海-昆明：上行更高（99 对 44）", result["reply"])
        self.assertIn("包头-茂名：下行更高（1416 对 325）", result["reply"])
        self.assertNotIn("均为下行", result["reply"])

    @patch("app.calculations.domain.analysis.chat")
    def test_single_ranked_row_per_parent_is_reported_as_selected_item(
        self,
        chat,
    ):
        chat.return_value = "三条路线的主病害不完全相同。"

        result = analyze_evidence(
            "三条路线各自数量最多的病害类型，并比较是否一致",
            [
                dependency(
                    [
                        {
                            "路线名称": "上海-昆明",
                            "病害类型": "防眩板缺损",
                            "数量": 49,
                        },
                        {
                            "路线名称": "包头-茂名",
                            "病害类型": "条状修补",
                            "数量": 1442,
                        },
                        {
                            "路线名称": "怀化市绕城高速公路",
                            "病害类型": "条状修补",
                            "数量": 512,
                        },
                    ]
                )
            ],
            requirements={
                "required_assertions": ["multi_metric_comparison"]
            },
        )

        self.assertTrue(result["accepted"])
        self.assertIn(
            "上海-昆明：病害类型 防眩板缺损，数量 49",
            result["reply"],
        )
        self.assertIn(
            "包头-茂名：病害类型 条状修补，数量 1442",
            result["reply"],
        )
        self.assertIn("各分组的主项不完全相同", result["reply"])
        self.assertNotIn("在各分组间相同", result["reply"])

    @patch("app.calculations.post_calc.analyze_evidence")
    def test_post_calc_preserves_structured_analysis_failure(self, analyze):
        analyze.return_value = {
            "reply": "",
            "accepted": False,
            "failure_code": "analysis_only_new_numeric_value",
            "failure_reason": "需要计算工具",
            "ungrounded_numbers": ["55"],
        }

        result = execute_post_calc(
            {
                "id": "q2",
                "question": "比较上下行并给出差值",
                "depends_on": ["q1"],
                "required_assertions": [
                    "multi_metric_comparison",
                    "analysis_only",
                ],
            },
            {
                "q1": {
                    "rows": [
                        {"上下行方向": "上行", "数量": 99},
                        {"上下行方向": "下行", "数量": 44},
                    ],
                    "columns": ["上下行方向", "数量"],
                }
            },
        )

        self.assertFalse(result["accepted"])
        self.assertEqual(
            result["failure"]["failure_code"],
            "analysis_only_new_numeric_value",
        )


class CoverageFactsTests(unittest.TestCase):
    ROUTE_FACTS = {
        "route": "包头-茂名",
        "data_count": 1934,
        "data_min_pile": 2161990,
        "data_max_pile": 2204012,
        "data_min_time": "2026-10-28 00:00:00",
        "data_max_time": "2026-11-04 23:59:59",
    }

    def _spec(self):
        return QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("pile_segment_500m",),
            filters=(
                binding("route_name", "=", "route-1"),
                binding("collection_time", ">=", "2026-10-01 00:00:00"),
                binding("collection_time", "<", "2026-11-01 00:00:00"),
            ),
            output_projection=("pile_segment_500m", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="统计路线 500 米分段病害数量",
        )

    @patch("app.services.calibrate.route_facts", return_value=ROUTE_FACTS)
    def test_time_window_only_overlapping_catalog_is_partial(self, _facts):
        facts = empty_scope_facts(self._spec())

        self.assertEqual(facts.verdict, PARTIAL)
        self.assertIn("2026-10-28", facts.detail)

    @patch("app.services.calibrate.route_facts", return_value=ROUTE_FACTS)
    def test_non_empty_spatial_result_carries_time_and_pile_coverage(self, _facts):
        spec = replace(
            self._spec(),
            filters=(
                binding("route_name", "=", "route-1"),
                binding("collection_time", ">=", "2026-10-28 00:00:00"),
                binding("collection_time", "<", "2026-11-04 00:00:00"),
            ),
        )

        facts = coverage_facts(spec)

        self.assertEqual(facts.verdict, INSIDE)
        self.assertIn("采集时间", facts.detail)
        self.assertIn("桩号", facts.detail)


class SharedRegistryContractTests(unittest.TestCase):
    def test_net_crack_alias_resolves_through_disease_entity_config(self):
        resolution = resolve_entity(
            CapabilityRegistry.load(),
            "disease_type",
            "网裂",
        )

        self.assertEqual(resolution.status, "resolved")
        self.assertEqual(resolution.values[0].label, "网状裂缝")

    def test_top_per_outer_group_compiles_as_partitioned_ranking(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("problem_type_name", "pile_segment_500m"),
            filters=(),
            output_projection=("problem_type_name", "pile_segment_500m", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=1,
            required_assertions=("top_per_outer_group",),
            question="每种病害各自最集中的 500 米桩号段",
        )

        sql = compile_spec_mdl(spec)

        self.assertIn(
            "ROW_NUMBER() OVER (PARTITION BY COALESCE(disease.problem_type_name",
            sql,
        )
        self.assertIn("__top_rank <= 1", sql)

    def test_top_per_outer_group_requires_outer_and_inner_dimensions(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("pile_segment_500m",),
            filters=(),
            output_projection=("pile_segment_500m", "count"),
            order_by=({"field": "count", "direction": "desc"},),
            limit=1,
            required_assertions=("top_per_outer_group",),
            question="每个外层分组各自最集中的桩号段",
        )

        issues = validate_shape_contract(spec, CapabilityRegistry.load())

        self.assertTrue(
            any(
                issue.code == "missing_outer_group_dimension"
                for issue in issues
            )
        )


if __name__ == "__main__":
    unittest.main()
