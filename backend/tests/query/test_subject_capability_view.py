import unittest
from unittest.mock import patch

from app.query.capability_registry import CapabilityRegistry, resolve_subject
from app.query.capability_registry.subject_resolver import SubjectResolution
from app.query.analysis import _analyze_once
from app.query.contracts import QueryPlan, QuerySpecError, QuerySpecUnsupported
from app.query.fields import FieldReachHit, FieldReachSet, PhysicalSchemaView
from app.query.service import _field_reach_from_ground, start_single_turn


class SubjectCapabilityViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_planner_view_has_comments_types_roles_and_enum_values(self):
        view = self.registry.planner_view("disease", FieldReachSet())

        up_down = view.fields["up_or_down"]
        self.assertEqual(up_down.label, "上下行")
        self.assertIn("code", up_down.roles)
        self.assertTrue(up_down.data_type)
        self.assertEqual(
            {item.code: item.label for item in up_down.enum_values},
            {"0": "全部", "1": "上行", "2": "下行"},
        )
        self.assertIn("route_length_km", view.fields)
        self.assertIn("route_length_km", view.metrics)

    def test_planner_view_exposes_registered_units(self):
        view = self.registry.planner_view("disease", FieldReachSet())

        self.assertEqual(view.fields["route_length_km"].unit, "km")
        self.assertEqual(
            view.to_prompt_dict()["fields"]["route_length_km"]["unit"],
            "km",
        )

    def test_planner_view_exposes_patrol_route_relation(self):
        view = self.registry.planner_view("patrol", FieldReachSet())

        self.assertEqual(view.fields["route_name"].label, "路线名称")
        self.assertEqual(view.fields["routes_id"].label, "路线ID")
        self.assertIn("dimension", view.fields["route_name"].roles)
        self.assertIn("dimension", view.fields["routes_id"].roles)

    def test_planner_view_exposes_linked_disease_count_for_patrol(self):
        view = self.registry.planner_view("patrol", FieldReachSet())

        self.assertIn("linked_disease_count", view.metrics)
        metric = view.to_prompt_dict()["metrics"]["linked_disease_count"]
        self.assertEqual(metric["label"], "任务关联病害数")
        self.assertEqual(metric["unit"], "处")
        self.assertEqual(metric["requires_join"], ["disease"])

    def test_planner_view_exposes_unavailable_field_status(self):
        view = self.registry.planner_view("disease", FieldReachSet())

        severity = view.fields["history_damage_interval"]
        lane = view.fields["lane_index"]
        self.assertEqual(severity.status, "unavailable")
        self.assertIn("全部为空", severity.unavailable_reason)
        self.assertEqual(lane.status, "unavailable")
        self.assertIn("车道分布尚未更新", lane.unavailable_reason)

    def test_planner_view_exposes_category_and_severity_dictionaries(self):
        view = self.registry.planner_view("disease", FieldReachSet())

        category = view.fields["category_name"]
        severity = view.fields["damage_type"]
        self.assertEqual(category.status, "available")
        self.assertEqual(severity.status, "available")
        self.assertEqual(
            {item.code: item.label for item in category.enum_values},
            {
                "0": "路面病害",
                "5": "路基病害",
                "10": "沿路设施",
                "15": "交安事件",
            },
        )
        self.assertEqual(
            {item.code: item.label for item in severity.enum_values},
            {"0": "轻度", "5": "中度", "10": "重度"},
        )

    def test_planner_view_exposes_declared_business_capability_gaps(self):
        view = self.registry.planner_view("disease", FieldReachSet())
        gaps = view.to_prompt_dict()["capability_gaps"]

        self.assertEqual(gaps["management_zone"]["status"], "unavailable")
        self.assertIn("片区", gaps["management_zone"]["reason"])
        self.assertEqual(
            gaps["patrol_pile_scope"]["status"],
            "partial",
        )
        self.assertIn("病害记录", gaps["patrol_pile_scope"]["reason"])
        self.assertEqual(
            gaps["inspection_coverage_scope"]["status"],
            "partial",
        )
        self.assertIn(
            "ai_maintain_routes_patrol",
            gaps["inspection_coverage_scope"]["reason"],
        )
        self.assertIn(
            "病害记录",
            gaps["inspection_coverage_scope"]["reason"],
        )

    def test_planner_view_exposes_shared_query_semantics(self):
        view = self.registry.planner_view("patrol", FieldReachSet())
        semantics = view.to_prompt_dict()["query_semantics"]

        ordered = semantics["view_selection"]["ordered_complete_result"]
        self.assertEqual(ordered["grouped_view"], "distribution")
        self.assertTrue(ordered["preserve_order_by"])
        self.assertIsNone(ordered["limit"])

        explicit_top = semantics["view_selection"]["explicit_top_n"]
        self.assertEqual(explicit_top["grouped_view"], "top_groups")
        self.assertTrue(explicit_top["require_positive_limit"])

    @patch("app.query.analysis.chat_json")
    def test_declared_capability_gap_stops_before_query_generation(self, chat_json):
        chat_json.return_value = {
            "final_goal": "按管养片区统计病害",
            "intent_family": "analysis",
            "unknown_terms": ["capability_gap:management_zone"],
            "views": [
                {
                    "id": "q1",
                    "action": "query_data",
                    "question": "按管养片区统计病害",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                }
            ],
        }

        with self.assertRaisesRegex(QuerySpecUnsupported, "管养片区"):
            _analyze_once(
                "按管养片区统计病害",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                registry=self.registry,
                subject="disease",
            )

    @patch("app.query.analysis.chat_json")
    def test_registered_unavailable_field_id_is_a_valid_gap_evidence_token(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "final_goal": "按车道统计病害",
            "intent_family": "analysis",
            "unknown_terms": ["capability_gap:lane_index"],
            "views": [],
        }

        with self.assertRaisesRegex(QuerySpecUnsupported, "车道分布尚未更新"):
            _analyze_once(
                "按车道统计病害",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                registry=self.registry,
                subject="disease",
            )

    @patch("app.query.analysis.chat_json")
    def test_partial_gap_without_views_uses_business_reason(self, chat_json):
        chat_json.return_value = {
            "final_goal": "判断某桩号区间本月是否没有病害",
            "intent_family": "analysis",
            "unknown_terms": [
                "capability_gap:inspection_coverage_scope",
                "capability_gap:patrol_pile_scope",
            ],
            "views": [],
        }

        with self.assertRaises(QuerySpecUnsupported) as raised:
            _analyze_once(
                "某桩号区间本月能否判定为没有病害",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                registry=self.registry,
                subject="disease",
            )

        message = str(raised.exception)
        self.assertIn("完整的桩号覆盖范围", message)
        self.assertIn("不能把“没有记录”说成“没有病害”", message)
        self.assertNotIn("capability_gap:", message)

    @patch("app.query.analysis.chat_json")
    def test_unknown_declared_gap_feedback_lists_registered_ids(self, chat_json):
        chat_json.return_value = {
            "final_goal": "按管养片区统计病害",
            "intent_family": "analysis",
            "unknown_terms": ["capability_gap:missing_route_length_field"],
            "views": [],
        }

        with self.assertRaises(QuerySpecError) as raised:
            _analyze_once(
                "按管养片区统计病害",
                "mysql",
                PhysicalSchemaView(columns=frozenset()),
                FieldReachSet(),
                registry=self.registry,
                subject="disease",
            )

        message = str(raised.exception)
        self.assertIn("management_zone", message)
        self.assertIn("inspection_coverage_scope", message)

    def test_subject_resolver_uses_grounding_fact(self):
        reach = FieldReachSet(
            hits=(FieldReachHit("ai_problem", "up_or_down", "上下行", "test", 3.0),)
        )

        resolution = resolve_subject(
            "病害有多少处",
            "analysis",
            reach,
            self.registry,
            grounded={"subject": "disease", "table": "ai_problem"},
        )

        self.assertEqual(resolution.subject, "disease")
        self.assertGreater(resolution.confidence, 0)

    @patch("app.query.capability_registry.subject_resolver.chat_json")
    def test_subject_router_considers_all_grounded_tables(self, chat_json):
        chat_json.return_value = {
            "subject": "disease",
            "confidence": 0.88,
            "evidence": ["最终答案是病害数量"],
        }
        reach = FieldReachSet(
            hits=(
                FieldReachHit(
                    "ai_maintain_patrol",
                    "patrol_number",
                    "巡查",
                    "test",
                    4.0,
                ),
            )
        )

        resolution = resolve_subject(
            "坑槽在最近5次巡查的数量变化",
            "data",
            reach,
            self.registry,
            grounded={
                "subject": "maintain_patrol",
                "table": "ai_maintain_patrol",
                "tables": [
                    {"name": "ai_maintain_patrol", "score": 15.3},
                    {"name": "ai_problem", "score": 6.6},
                ],
            },
        )

        self.assertEqual(resolution.subject, "disease")
        chat_json.assert_called_once()

    def test_reach_conversion_preserves_real_column_hits(self):
        reach = _field_reach_from_ground(
            {
                "subject": "disease",
                "table": "ai_problem",
                "columns": [
                    {
                        "table": "ai_problem",
                        "column": "up_or_down",
                        "score": 4.5,
                        "hits": ["列名命中：上下行"],
                    }
                ],
            }
        )

        self.assertEqual(reach.hits[0].table, "ai_problem")
        self.assertEqual(reach.hits[0].column, "up_or_down")

    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    @patch("app.query.service.field_grounding.ground")
    @patch(
        "app.query.service.resolve_subject",
        return_value=SubjectResolution(subject="disease", confidence=0.9),
    )
    def test_start_single_turn_passes_real_field_reach(
        self,
        _resolve_subject,
        ground,
        analyze,
        resolve_templates,
    ):
        ground.return_value = {
            "subject": "disease",
            "table": "ai_problem",
            "columns": [
                {
                    "table": "ai_problem",
                    "column": "up_or_down",
                    "score": 4.5,
                    "hits": ["列名命中：上下行"],
                }
            ],
        }
        analyze.return_value = QueryPlan(question="上下行分别有多少处", nodes=())
        resolve_templates.return_value.candidates = ()

        start_single_turn("上下行分别有多少处", providers=[])

        passed_reach = analyze.call_args.args[3]
        self.assertTrue(passed_reach.hits)
        self.assertEqual(passed_reach.hits[0].column, "up_or_down")


if __name__ == "__main__":
    unittest.main()
