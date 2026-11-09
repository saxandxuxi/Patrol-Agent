import unittest
from unittest.mock import patch

from app.query.capability_registry import CapabilityRegistry, resolve_subject
from app.query.capability_registry.entity_resolver import EntityValue
from app.query.fields import FieldReachSet
from app.services import field_grounding, wren_engine


class FieldGroundingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_date_numbers_do_not_become_field_semantic_terms(self):
        terms = wren_engine._terms("11月2日，宝安大道西乡段有多少处泛油？")

        self.assertNotIn("11", terms)
        self.assertNotIn("2", terms)

    def test_route_name_does_not_become_a_section_landing(self):
        grounded = field_grounding.ground(
            "mysql",
            "11月2日，宝安大道西乡段有多少处泛油？",
        )

        # search_models is a recall stage: the section candidate may rank first
        # because "西乡段" resembles its table description. It must remain only
        # a candidate and must not become the execution landing.
        self.assertEqual(
            grounded["tables"][0]["name"],
            "basic_facilities_sections",
        )
        self.assertEqual(grounded["table"], "")
        self.assertEqual(grounded["columns"], [])
        self.assertNotIn(
            "surface_type",
            {
                str(item.get("column") or "")
                for item in grounded["columns"]
            },
        )

    @patch("app.query.capability_registry.subject_resolver.list_entities")
    def test_registered_entity_value_is_a_subject_candidate(
        self,
        list_entities,
    ):
        list_entities.return_value = (
            EntityValue(code="FY", label="泛油", aliases=()),
        )

        resolution = resolve_subject(
            "11月2日，宝安大道西乡段有多少处泛油？",
            "data",
            FieldReachSet(),
            self.registry,
            grounded={
                "tables": [
                    {"name": "basic_facilities_sections", "score": 7.65},
                    {"name": "basic_facilities_routes", "score": 5.45},
                ],
            },
            kind="mysql",
        )

        self.assertEqual(resolution.subject, "disease")
        self.assertTrue(
            any("entity=disease_type:泛油" in item for item in resolution.evidence)
        )
        self.assertFalse(
            any(
                str(item).startswith("grounding_table=")
                for item in resolution.evidence
            )
        )

    @patch("app.query.capability_registry.subject_resolver.chat_json")
    @patch("app.query.capability_registry.subject_resolver.list_entities")
    def test_recall_tables_are_subject_candidates_only_as_fallback(
        self,
        list_entities,
        chat_json,
    ):
        list_entities.return_value = ()
        chat_json.return_value = {
            "subject": "section",
            "confidence": 0.72,
            "evidence": ["用户询问路段台账"],
        }

        resolution = resolve_subject(
            "有哪些路段记录？",
            "data",
            FieldReachSet(),
            self.registry,
            grounded={
                "table": "",
                "tables": [
                    {"name": "basic_facilities_sections", "score": 7.65},
                    {"name": "basic_facilities_routes", "score": 5.45},
                ],
            },
            kind="mysql",
        )

        self.assertEqual(resolution.subject, "section")
        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn("basic_facilities_sections", prompt)
        self.assertIn("grounding_table=basic_facilities_sections", prompt)

    @patch("app.query.capability_registry.subject_resolver.chat_json")
    @patch("app.query.capability_registry.subject_resolver.list_entities")
    def test_generic_column_comment_landing_does_not_hide_recall_subjects(
        self,
        list_entities,
        chat_json,
    ):
        list_entities.return_value = ()
        chat_json.return_value = {
            "subject": "patrol",
            "confidence": 0.86,
            "evidence": ["最终每一行是巡检任务，病害数只是任务关联聚合"],
        }

        resolution = resolve_subject(
            "2026年11月5日执行了哪些巡检任务？列出任务编号和该任务检出的病害总数。",
            "data",
            FieldReachSet(),
            self.registry,
            grounded={
                "subject": "work_order",
                "table": "ai_work_order",
                "columns": [
                    {
                        "table": "ai_work_order",
                        "column": "order_source",
                        "hits": ["列注释：巡检、病害"],
                    }
                ],
                "tables": [
                    {"name": "ai_problem", "score": 29.4, "table_score": 2.0},
                    {
                        "name": "ai_maintain_patrol",
                        "score": 20.8,
                        "table_score": 15.8,
                    },
                ],
            },
            kind="mysql",
        )

        self.assertEqual(resolution.subject, "patrol")
        chat_json.assert_called_once()
        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn("ai_maintain_patrol", prompt)
        self.assertIn("recall_table=ai_maintain_patrol", prompt)
        self.assertIn("field_grounding.subject=work_order", prompt)


if __name__ == "__main__":
    unittest.main()
