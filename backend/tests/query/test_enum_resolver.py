import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.capability_registry.enum_resolver import resolve_enum
from app.query.values import resolve_value


class EnumResolverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_up_or_down_resolves_per_table(self):
        disease = resolve_enum(
            self.registry,
            "ai_problem",
            "up_or_down",
            "全部",
        )
        section = resolve_enum(
            self.registry,
            "basic_facilities_sections",
            "up_or_down",
            "双向",
        )

        self.assertEqual(disease.status, "resolved")
        self.assertEqual(disease.values[0].code, "0")
        self.assertEqual(section.status, "resolved")
        self.assertEqual(section.values[0].code, "3")

    def test_all_code_map_columns_are_resolvable(self):
        for table, column in self.registry.snapshot.enums:
            enum = self.registry.snapshot.enums[(table, column)]
            sample = next(iter(enum.values.values()))
            result = resolve_enum(
                self.registry,
                table,
                column,
                sample.code,
            )
            self.assertEqual(
                result.status,
                "resolved",
                f"{table}.{column} label={sample.label}",
            )

    def test_values_uses_registry_enum_not_static_field_list(self):
        value = resolve_value(
            "up_or_down",
            "=",
            "全部",
            "病害上下行",
            "mysql",
            subject="disease",
            registry=self.registry,
        )

        self.assertEqual(value.storage_value, "0")
        self.assertEqual(value.display_value, "全部/未区分")


if __name__ == "__main__":
    unittest.main()
