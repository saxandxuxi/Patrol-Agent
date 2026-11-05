import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.contracts import ValueBinding
from app.query.fields import FieldReachSet, PhysicalSchemaView
from app.query.values import resolve_value


class ValueAnalysisTests(unittest.TestCase):
    @patch("app.query.values.known_problem_types", return_value=["泛油", "纵向裂缝"])
    def test_damage_type_resolution_preserves_display_and_source(
        self,
        _known_problem_types,
    ):
        value = resolve_value(
            "problem_type_name",
            "=",
            "泛油",
            "泛油有多少处？",
            "mysql",
        )
        self.assertEqual(value.storage_value, "泛油")
        self.assertEqual(value.display_value, "泛油")
        self.assertEqual(value.source, "user_explicit")
        self.assertEqual(value.resolver, "damage_type_dictionary")

    @patch("app.query.analysis.resolve_value")
    @patch("app.query.analysis.chat_json")
    def test_analysis_returns_structured_query_plan(
        self,
        chat_json,
        resolve,
    ):
        chat_json.return_value = {
            "final_goal": "统计泛油数量",
            "intent_family": "count",
            "views": [
                {
                    "id": "q1",
                    "subject": "disease",
                    "view": "count",
                    "metrics": ["count"],
                    "dimensions": [],
                    "filters": [
                        {
                            "field": "problem_type_name",
                            "op": "=",
                            "raw_value": "泛油",
                        }
                    ],
                    "output_projection": [],
                    "order_by": [],
                    "limit": None,
                    "required_assertions": ["preserve_user_scope"],
                }
            ],
        }
        resolve.return_value = ValueBinding(
            field="problem_type_name",
            op="=",
            storage_value="泛油",
            display_value="泛油",
            source="user_explicit",
            resolver="damage_type_dictionary",
            raw_text="泛油",
        )
        plan = analyze_single_turn(
            "泛油有多少处？",
            "mysql",
            PhysicalSchemaView(columns=frozenset({("disease", "problem_base_id")})),
            FieldReachSet(hits=()),
        )
        spec = plan.nodes[0].spec
        self.assertEqual(spec.view, "count")
        self.assertEqual(spec.filters[0].display_value, "泛油")
        self.assertTrue(spec.filters[0].resolver)
