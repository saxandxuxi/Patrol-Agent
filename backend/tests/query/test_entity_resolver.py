import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.entity_resolver import resolve_entity


class EntityResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_crack_family_expands_to_real_dictionary_members(self):
        result = resolve_entity(
            self.registry,
            "disease_type",
            "裂缝",
        )

        self.assertEqual(result.status, "expanded")
        labels = {item.label for item in result.values}
        self.assertTrue({"横向裂缝", "纵向裂缝", "网状裂缝"}.issubset(labels))
        self.assertEqual(result.family, "crack")

    def test_alias_resolves_to_exact_entity(self):
        result = resolve_entity(
            self.registry,
            "disease_type",
            "龟裂",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.values[0].label, "网状裂缝")

    def test_unknown_entity_is_unsupported(self):
        result = resolve_entity(
            self.registry,
            "disease_type",
            "不存在的病害",
        )

        self.assertEqual(result.status, "unsupported")


if __name__ == "__main__":
    unittest.main()
