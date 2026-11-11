import unittest
from unittest.mock import patch

from app.query.analysis import _assist_field_issue, analyze_single_turn
from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import QuerySpec, ValueBinding
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.service import start_single_turn
from app.query.values import resolve_value
from app.semantic_aids import canonical_field, detect_evidence, pile_meter


def rule_ids(question: str) -> set[str]:
    return {item.rule_id for item in detect_evidence(question)}


class SemanticAidTests(unittest.TestCase):
    def test_lane_ordinal_is_candidate_evidence_only(self):
        evidence = detect_evidence("G4京港澳高速第三车道的病害明细")
        lane = next(item for item in evidence if item.rule_id == "lane_ordinal")

        self.assertEqual(lane.candidate_field, "lane_index")
        self.assertEqual(lane.candidate_value, "3")
        self.assertFalse(lane.trusted)
        self.assertEqual(lane.source, "code_rule")

    def test_severity_maps_to_authoritative_damage_type_dictionary(self):
        evidence = detect_evidence(
            "最近一次巡检检出“严重”等级病害最多的路线"
        )
        severity = next(
            item for item in evidence if item.rule_id == "damage_severity"
        )

        self.assertEqual(
            severity.candidate_field,
            "damage_type",
        )
        self.assertNotEqual(severity.candidate_field, "problem_type_name")
        self.assertFalse(severity.trusted)
        self.assertIn(
            "history_damage_interval",
            severity.discouraged_fields,
        )
        self.assertTrue(severity.requires_field)

    def test_time_granularity_evidence_covers_year_week_month_day(self):
        year = detect_evidence("统计2026年各月病害数量")
        week = detect_evidence("最近8周每周病害趋势")
        month = detect_evidence("2026年10月检出的病害")
        day = detect_evidence("11月2日巡检了哪些路线")

        self.assertIn("time_year", {item.rule_id for item in year})
        self.assertIn("time_week", {item.rule_id for item in week})
        self.assertIn("time_month", {item.rule_id for item in month})
        self.assertIn("time_day", {item.rule_id for item in day})

    def test_daily_phrase_covers_every_day_wording(self):
        evidence = detect_evidence("泛油在10月的每一天分别是多少处？")
        rule_ids = {item.rule_id for item in evidence}

        self.assertIn("time_day", rule_ids)
        self.assertIn("trend_granularity_day", rule_ids)
        day = next(
            item
            for item in evidence
            if item.rule_id == "trend_granularity_day"
        )
        self.assertTrue(day.requires_field)
        self.assertEqual(day.candidate_field, "day")

    def test_trend_granularity_is_a_required_field_evidence(self):
        ranged = detect_evidence(
            "10月23日到10月26日，宝安中心--恒明珠的病害类型构成发生了什么变化？"
        )
        separate = detect_evidence(
            "10月23日和10月26日这两次巡查，纵向裂缝数量差多少？"
        )

        self.assertIn("date_range", {item.rule_id for item in ranged})
        self.assertNotIn("date_range", {item.rule_id for item in separate})

    def test_trend_granularity_evidence_is_required_field(self):
        day_evidence = detect_evidence(
            "按天统计2026年10月27日至11月4日的病害趋势"
        )
        week_evidence = detect_evidence(
            "2026年9月各周病害数量，能否判断9月趋势"
        )
        day = next(
            item
            for item in day_evidence
            if item.rule_id == "trend_granularity_day"
        )
        week = next(
            item
            for item in week_evidence
            if item.rule_id == "trend_granularity_week"
        )

        self.assertEqual(day.candidate_field, "day")
        self.assertEqual(week.candidate_field, "week")
        self.assertTrue(day.requires_field)
        self.assertTrue(week.requires_field)
        self.assertNotIn(
            "trend_granularity_month",
            {item.rule_id for item in week_evidence},
        )

    def test_time_field_aliases_are_canonicalized_by_rule_config(self):
        self.assertEqual(canonical_field("week_start_date"), "week")
        self.assertEqual(
            canonical_field("month_start_date"),
            "collection_time_month",
        )
        self.assertEqual(
            canonical_field("quarter_start_date"),
            "collection_time_quarter",
        )

    def test_identifier_and_route_evidence_is_centralized(self):
        evidence = detect_evidence(
            "G60沪昆高速由设备DEV-7712执行任务XCBH202611040003"
        )
        ids = {item.rule_id for item in evidence}

        self.assertIn("route_code", ids)
        self.assertIn("device_code", ids)
        self.assertIn("task_number", ids)

    def test_pile_interval_is_parsed_by_central_identifier_rule(self):
        ids = rule_ids("G4京港澳高速K100至K120区间的病害")
        binding = resolve_value(
            "pile_number",
            ">=",
            "K100",
            "G4京港澳高速K100至K120区间的病害",
            subject="disease",
        )

        self.assertIn("pile_interval", ids)
        self.assertEqual(binding.storage_value, (100000, 120000))
        self.assertEqual(binding.op, "between")
        self.assertEqual(binding.source, "code_rule")

    def test_explicit_kilometer_segment_is_untrusted_granularity_evidence(self):
        evidence = detect_evidence(
            "2026年11月长沙－芷江高速上病害最集中的3个公里段分别是哪里？"
        )
        kilometer = next(
            item
            for item in evidence
            if item.rule_id == "pile_segment_kilometer"
        )

        self.assertEqual(kilometer.candidate_field, "pile_segment_1000m")
        self.assertIn("pile_segment_500m", kilometer.discouraged_fields)
        self.assertFalse(kilometer.trusted)
        self.assertNotIn(
            "pile_segment_group",
            {item.rule_id for item in evidence},
        )

    def test_registered_identifier_overrides_the_wrong_model_field(self):
        binding = resolve_value(
            "task_number",
            "=",
            "XCBH202610310000",
            "XCBH202610310000检出的病害数量",
            subject="patrol",
        )

        self.assertEqual(binding.field, "patrol_number")
        self.assertEqual(binding.storage_value, "XCBH202610310000")
        self.assertEqual(binding.source, "user_explicit")
        self.assertEqual(binding.resolver, "identifier_dictionary:XCBH")

    def test_task_identifier_does_not_stay_bound_to_disease_problem_number(self):
        binding = resolve_value(
            "problem_number",
            "=",
            "XCBH202611040003",
            "XCBH202611040003检出病害数量以及病害分布情况",
            subject="disease",
        )

        self.assertEqual(binding.field, "patrol_number")
        self.assertEqual(binding.storage_value, "XCBH202611040003")
        self.assertEqual(binding.source, "user_explicit")

    def test_pile_meter_keeps_normalized_meters_and_zero(self):
        """值解析器给下游的桩号单位是米；数值不能再按公里换算一次（K120→1.2亿），
        K0 起点也必须是合法里程。"""

        self.assertEqual(pile_meter(120000), 120000)
        self.assertEqual(pile_meter(0), 0)
        self.assertEqual(pile_meter("K120+500"), 120500)

    def test_grouping_and_metric_rules_are_evidence(self):
        ids = rule_ids("按路线和病害类型统计总数量和总面积")

        self.assertIn("group_by_route", ids)
        self.assertIn("group_by_disease_type", ids)
        self.assertIn("metric_count", ids)
        self.assertIn("metric_total_area", ids)

    def test_mixed_surface_type_and_grade_keeps_both_dimensions(self):
        question = (
            "2026年11月，在沥青混凝土路面和水泥混凝土路面上，"
            "分别检出了多少处一级公路和二级公路的病害？"
            "请按路面类型和公路等级交叉统计病害数量。"
        )
        evidence = detect_evidence(question)
        fields = {item.candidate_field for item in evidence}

        self.assertIn("road_surface_type", fields)
        self.assertIn("tech_grade", fields)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("road_surface_type", "tech_grade"),
            filters=(),
            output_projection=("road_surface_type", "tech_grade", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question=question,
        )

        issue = _assist_field_issue(
            [spec],
            [item.to_dict() for item in evidence],
            CapabilityRegistry.load(),
            {spec.id: "query_data"},
        )

        self.assertFalse(issue)

    def test_shared_dimension_phrase_keeps_both_independent_fields(self):
        question = (
            "2026年11月，上海-昆明高速按公路技术等级和路面类型"
            "统计病害处数。"
        )
        evidence = detect_evidence(question)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("tech_grade", "road_surface_type"),
            filters=(),
            output_projection=("tech_grade", "road_surface_type", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question=question,
        )

        issue = _assist_field_issue(
            [spec],
            [item.to_dict() for item in evidence],
            CapabilityRegistry.load(),
            {spec.id: "query_data"},
        )

        self.assertFalse(issue)

    def test_admin_level_filter_can_coexist_with_tech_grade_dimension(self):
        question = (
            "2026年10月，在行政等级为“国道”的路段上，按公路技术等级"
            "（一级、二级、三级、四级）分别统计沥青混凝土路面和"
            "水泥混凝土路面各检出了多少处病害。"
        )
        evidence = detect_evidence(question)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("tech_grade", "road_surface_type"),
            filters=(
                ValueBinding(
                    field="route_level",
                    op="=",
                    storage_value="1",
                    display_value="国道",
                    source="user_explicit",
                    resolver="enum_dictionary",
                    raw_text="行政等级为“国道”",
                ),
            ),
            output_projection=("tech_grade", "road_surface_type", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question=question,
        )

        issue = _assist_field_issue(
            [spec],
            [item.to_dict() for item in evidence],
            CapabilityRegistry.load(),
            {spec.id: "query_data"},
        )

        self.assertFalse(issue)

    def test_grade_only_question_still_rejects_surface_type_field(self):
        question = "统计一级公路和二级公路的病害数量"
        evidence = detect_evidence(question)
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("road_surface_type",),
            filters=(),
            output_projection=("road_surface_type", "count"),
            order_by=(),
            limit=None,
            required_assertions=(),
            question=question,
        )

        issue = _assist_field_issue(
            [spec],
            [item.to_dict() for item in evidence],
            CapabilityRegistry.load(),
            {spec.id: "query_data"},
        )

        self.assertIn("road_surface_type", issue)
        self.assertIn("tech_grade", issue)

    def test_top_n_ordinal_is_untrusted_structural_evidence(self):
        evidence = detect_evidence(
            "长沙绕城查一下前四类病害，数量和平均面积都给我。"
        )
        top_n = next(item for item in evidence if item.rule_id == "top_n")

        self.assertEqual(top_n.candidate_type, "top_n")
        self.assertEqual(top_n.candidate_value, "4")
        self.assertFalse(top_n.trusted)

    def test_two_explicit_dates_and_comparison_are_evidence(self):
        evidence = detect_evidence(
            "10月23日和10月26日这两次巡查，病害类型变化大不大？"
        )
        dates = [
            item for item in evidence if item.rule_id == "explicit_date"
        ]

        self.assertEqual(len(dates), 2)
        self.assertIn(
            "comparison_cue",
            {item.rule_id for item in evidence},
        )

    def test_recent_patrol_window_is_untrusted_candidate_evidence(self):
        evidence = detect_evidence(
            "最近三次巡查里，出现次数最多的五种病害类型"
        )
        recent = next(
            item for item in evidence if item.rule_id == "recent_window"
        )

        self.assertEqual(recent.candidate_type, "recent_window")
        self.assertEqual(recent.candidate_field, "patrol_number")
        self.assertEqual(recent.candidate_value, "3")
        self.assertFalse(recent.trusted)

    @patch("app.query.analysis.chat_json")
    def test_planner_prompt_receives_untrusted_evidence(self, chat_json):
        chat_json.return_value = {
            "final_goal": "计数",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        analyze_single_turn(
            "第三车道有多少处病害",
            "mysql",
            PhysicalSchemaView(columns=frozenset()),
            FieldReachSet(),
        )

        messages = chat_json.call_args.args[0]
        prompt = "\n".join(item["content"] for item in messages)
        self.assertIn("lane_ordinal", prompt)
        self.assertIn('"trusted": false', prompt)

    def test_lane_value_is_resolved_only_after_planner_selects_field(self):
        binding = resolve_value(
            "lane_index",
            "=",
            "第三车道",
            "G4京港澳高速第三车道有多少处病害",
            subject="disease",
        )

        self.assertEqual(binding.storage_value, "3")
        self.assertEqual(binding.display_value, "第三车道")
        self.assertEqual(binding.source, "code_rule")
        self.assertIn("lane_ordinal", binding.resolver)

    @patch("app.query.analysis.chat_json")
    def test_lane_capability_failure_is_explicit_not_provenance_error(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "车道病害数量",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "lane_index",
                            "op": "=",
                            "raw_value": "第三车道",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                    "depends_on": [],
                    "inputs": [],
                }
            ],
        }

        result = start_single_turn(
            "G4京港澳高速第三车道有多少处病害",
            schema=PhysicalSchemaView(columns=frozenset()),
            reach=FieldReachSet(),
        )

        self.assertIn("车道分布尚未更新", result.answer.text)
        self.assertEqual(
            result.answer.sections[0]["failure_code"],
            "query_plan_not_executable",
        )


if __name__ == "__main__":
    unittest.main()

