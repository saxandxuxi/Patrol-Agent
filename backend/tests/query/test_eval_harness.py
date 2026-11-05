import unittest

from evals.run_single_turn_eval import run_case


class EvalHarnessTests(unittest.TestCase):
    def test_runner_exception_fails_case_without_aborting_suite(self):
        def broken_runner(_question):
            raise ValueError("query spec invalid")

        result = run_case(
            {
                "id": "broken",
                "question": "失败问题",
                "expected_status": "answered",
            },
            broken_runner,
        )
        self.assertFalse(result.ok)
        self.assertIn("query spec invalid", result.reason)

    def test_explicit_failure_is_failure_for_solvable_case(self):
        result = run_case(
            {
                "id": "count",
                "question": "泛油有多少处？",
                "expected_status": "answered",
            },
            lambda _: {
                "answer": None,
                "verification": {"status": "repairable"},
            },
        )
        self.assertFalse(result.ok)

    def test_unsupported_case_requires_specific_reason(self):
        result = run_case(
            {
                "id": "missing",
                "question": "病害的坡度分布",
                "expected_status": "unsupported",
            },
            lambda _: {
                "answer": "系统暂不支持",
                "verification": {
                    "status": "unsupported",
                    "reason": "数据库没有坡度字段",
                },
            },
        )
        self.assertTrue(result.ok)

    def test_required_filters_need_structured_evidence(self):
        result = run_case(
            {
                "id": "device-count",
                "question": "巡检设备分别完成了多少次巡检？",
                "expected_status": "answered",
                "required_filters": ["group_by_device", "non_null_device"],
            },
            lambda _: {
                "answer": "设备A完成 3 次巡检。",
                "verification": {"status": "accepted"},
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("group_by_device", result.reason)

    def test_required_output_fields_need_structured_evidence(self):
        result = run_case(
            {
                "id": "route-metrics",
                "question": "统计每条路线的病害数量和路线长度",
                "expected_status": "answered",
                "required_output_fields": ["route_length_km"],
            },
            lambda _: {
                "answer": "路线A有 10 处病害。",
                "verification": {"status": "accepted"},
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("route_length_km", result.reason)

    def test_blocked_section_cannot_be_reported_as_accepted(self):
        result = run_case(
            {
                "id": "dependency",
                "question": "计算依赖结果",
                "expected_status": "answered",
            },
            lambda _: {
                "answer": "依赖步骤内容。",
                "verification": {"status": "accepted"},
                "sections": [{"status": "blocked"}],
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("blocked", result.reason)

    def test_sum_equals_total_accepts_a_computed_month_total(self):
        result = run_case(
            {
                "id": "months",
                "question": "各月份次数和总数",
                "expected_status": "answered",
                "required_filters": ["group_by_month", "sum_equals_total"],
            },
            lambda _: {
                "answer": "6月 83；7月 62；10月 41；11月 20；共 206 次。",
                "specs": [
                    {
                        "spec": {
                            "view": "distribution",
                            "dimensions": ["create_time_month"],
                            "metrics": ["count"],
                            "filters": [],
                            "output_projection": ["create_time_month", "count"],
                        }
                    }
                ],
                "sections": [
                    {
                        "status": "accepted",
                        "columns": ["月份", "次数"],
                        "preview_rows": [
                            {"月份": "6月", "次数": "83"},
                            {"月份": "7月", "次数": "62"},
                            {"月份": "10月", "次数": "41"},
                            {"月份": "11月", "次数": "20"},
                        ],
                    }
                ],
                "verification": {"status": "accepted"},
            },
        )
        self.assertTrue(result.ok)

    def test_ai_reliability_requirements_are_verified_from_answer(self):
        result = run_case(
            {
                "id": "ai",
                "question": "AI识别靠谱吗",
                "expected_status": "answered",
                "required_filters": [
                    "total_count",
                    "low_confidence_count",
                    "confidence",
                ],
            },
            lambda _: {
                "answer": "AI识别共 34889 条，其中 17623 条属于低置信度，占 50.51%。",
                "verification": {"status": "accepted"},
            },
        )
        self.assertTrue(result.ok)

    def test_severity_aliases_accept_route_length_and_density_conclusion(self):
        result = run_case(
            {
                "id": "severity",
                "question": "哪个道路最严重",
                "expected_status": "answered",
                "required_filters": ["total_area", "route_length", "comparison"],
            },
            lambda _: {
                "answer": "按病害密度判断，密度最高的是A；总面积 10㎡，路线长度_km 2。",
                "verification": {"status": "accepted"},
            },
        )
        self.assertTrue(result.ok)
