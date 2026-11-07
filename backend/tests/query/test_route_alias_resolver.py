import unittest
from unittest.mock import patch

from app.services.route_resolver import QueryPlanError, _resolve_route


ROWS = [
    {"id": "r1", "code": "G65", "short_name": "包头-茂名", "full_name": "包头-茂名高速公路"},
    {"id": "r2", "code": "S50", "short_name": "长沙－芷江高速公路", "full_name": ""},
]


class RouteAliasResolverTests(unittest.TestCase):
    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_route_with_type_suffix_resolves_to_short_name(self, _rows):
        """口语“包头-茂名高速”要能落到台账短名“包头-茂名”。"""

        route = _resolve_route("包头-茂名高速")

        self.assertEqual(route["id"], "r1")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_route_with_hyphen_variants_and_suffix_resolves(self, _rows):
        route = _resolve_route("长沙-芷江高速")

        self.assertEqual(route["id"], "r2")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_exact_short_name_still_resolves(self, _rows):
        route = _resolve_route("包头-茂名")

        self.assertEqual(route["id"], "r1")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {"id": "a", "code": "S1", "short_name": "长沙-芷江", "full_name": ""},
            {"id": "b", "code": "S2", "short_name": "长沙－芷江", "full_name": ""},
        ],
    )
    def test_alias_matching_multiple_routes_still_asks_to_confirm(self, _rows):
        """归一后命中多条时不能猜，仍要返回确认提示。"""

        with self.assertRaisesRegex(QueryPlanError, "多条"):
            _resolve_route("长沙芷江高速")


if __name__ == "__main__":
    unittest.main()
