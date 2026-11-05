import unittest

from app.query.template_sources import contract_from_auto, contract_from_example


class AutoExampleTemplateTests(unittest.TestCase):
    def test_auto_template_without_output_contract_is_rejected(self):
        result = contract_from_auto({"id": "t1", "sql": "SELECT 1"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "incomplete_template_contract")

    def test_example_contract_preserves_provenance_requirements(self):
        result = contract_from_example(
            {
                "doc_name": "doc-1",
                "spec": {
                    "view": "count",
                    "metrics": ["count"],
                    "conditions": {"damage_type": ["泛油"]},
                    "output_fields": [],
                },
            }
        )
        self.assertTrue(result.ok)
        self.assertTrue(all(item.source for item in result.contract.fixed_conditions))
