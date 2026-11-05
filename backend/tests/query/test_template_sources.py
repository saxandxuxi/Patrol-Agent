import unittest

from app.query.template_sources import contract_from_builtin, contract_from_preset


class TemplateSourceTests(unittest.TestCase):
    def test_builtin_metrics_become_exact_support_set(self):
        contract = contract_from_builtin(
            {
                "id": "multi_metric",
                "title": "多指标",
                "source": "builtin",
                "match": {
                    "shapes": ["count"],
                    "subjects": ["disease"],
                    "metrics": ["count", "total_area"],
                },
            }
        )
        self.assertEqual(contract.metrics, ("count", "total_area"))
        self.assertEqual(contract.adapter_id, "builtin")
        self.assertEqual(contract.native_id, "multi_metric")

    def test_preset_fixed_conditions_keep_values(self):
        contract = contract_from_preset(
            {
                "id": "oil_count",
                "title": "泛油数量",
                "match": {
                    "shape": ["count"],
                    "subject": ["disease"],
                    "fixed": {"damage_type": "泛油"},
                },
            }
        )
        self.assertEqual(contract.fixed_conditions[0].display_value, "泛油")
        self.assertEqual(contract.fixed_conditions[0].storage_value, "泛油")
        self.assertEqual(contract.fixed_conditions[0].source, "template_fixed")
