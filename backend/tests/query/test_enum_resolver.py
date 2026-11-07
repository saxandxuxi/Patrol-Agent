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

    def test_surface_type_suffix_resolves_to_storage_value(self):
        """“沥青混凝土路面”要解析成存储值“沥青混凝土”，
        不能因为同时包含“沥青”被判成歧义。"""

        for text, code in (
            ("沥青混凝土", "沥青混凝土"),
            ("水泥混凝土", "水泥混凝土"),
            ("沥青混凝土路面", "沥青混凝土"),
            ("水泥混凝土路面", "水泥混凝土"),
            ("沥青路面", "沥青"),
        ):
            with self.subTest(text=text):
                result = resolve_enum(
                    self.registry,
                    "ai_problem",
                    "road_surface_type",
                    text,
                )

                self.assertEqual(result.status, "resolved")
                self.assertEqual(result.values[0].code, code)

    def test_equal_length_labels_stay_ambiguous(self):
        """并列具体的说法仍然要问用户，不能猜。"""

        result = resolve_enum(
            self.registry,
            "basic_facilities_routes",
            "level",
            "国道和省道",
        )

        self.assertEqual(result.status, "ambiguous")

    def test_surface_type_filter_is_dictionary_backed(self):
        value = resolve_value(
            "road_surface_type",
            "=",
            "沥青混凝土路面",
            "沥青混凝土路面上的病害有多少处？",
            "mysql",
            subject="disease",
            registry=self.registry,
        )

        self.assertEqual(value.storage_value, "沥青混凝土")
        self.assertEqual(value.resolver, "enum:ai_problem.road_surface_type")


if __name__ == "__main__":
    unittest.main()
