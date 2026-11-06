import unittest
from unittest.mock import patch

from app.query.capability_registry import CapabilityRegistry, resolve_subject
from app.query.capability_registry.subject_resolver import SubjectResolution
from app.query.contracts import QueryPlan
from app.query.fields import FieldReachHit, FieldReachSet
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
