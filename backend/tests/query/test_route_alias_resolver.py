import unittest
from unittest.mock import patch

from app.query.values import resolve_value
from app.services.route_resolver import QueryPlanError, _resolve_route, _route_matches


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
    def test_spoken_connector_word_resolves_like_hyphen(self, _rows):
        """口语“包头到茂名”要能落到台账短名“包头-茂名”。"""

        route = _resolve_route("包头到茂名")

        self.assertEqual(route["id"], "r1")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_spoken_connector_with_suffix_resolves(self, _rows):
        """“长沙到芷江”+“高速公路”混合写法也要收敛到同一个 key。"""

        route = _resolve_route("长沙到芷江高速公路")

        self.assertEqual(route["id"], "r2")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "pd",
                "code": "S70",
                "short_name": "平江－洞口高速公路",
                "full_name": "",
            },
        ],
    )
    def test_spoken_direction_word_resolves(self, _rows):
        """“平江去洞口”与“平江到洞口”是同一路线的口语写法。"""

        route = _resolve_route("平江去洞口的高速")

        self.assertEqual(route["id"], "pd")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "pd",
                "code": "S70",
                "short_name": "平江－洞口高速公路",
                "full_name": "",
            },
        ],
    )
    def test_spoken_segment_determiner_resolves(self, _rows):
        """“平江去洞口那段”里的“那段”不是路线身份的一部分。"""

        route = _resolve_route("平江去洞口那段")

        self.assertEqual(route["id"], "pd")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_spoken_from_to_route_phrase_resolves(self, _rows):
        """“从长沙往芷江那条高速”应去掉句首方向词和指示词后唯一命中。"""

        route = _resolve_route("从长沙往芷江那条高速")

        self.assertEqual(route["id"], "r2")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "pd",
                "code": "S70",
                "short_name": "平江－洞口高速公路",
                "full_name": "",
            },
        ],
    )
    def test_endpoint_abbreviation_resolves_generically(self, _rows):
        """“平洞高速”应按两端地名首字的通用规则落到唯一路线。"""

        route = _resolve_route("平洞高速")

        self.assertEqual(route["id"], "pd")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "cz",
                "code": "S50",
                "short_name": "长沙－芷江高速公路",
                "full_name": "",
            },
        ],
    )
    def test_endpoint_abbreviation_with_line_suffix_resolves(self, _rows):
        """“长芷线”里的“线”是路线类型后缀，不参与路线身份。"""

        route = _resolve_route("长芷线")

        self.assertEqual(route["id"], "cz")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "shkm",
                "code": "G60",
                "short_name": "上海-昆明",
                "full_name": "上海-昆明高速公路",
            },
        ],
    )
    def test_spoken_demonstrative_route_phrase_resolves(self, _rows):
        """“上海到昆明那条高速”应去掉口语指示词后落到唯一路线。"""

        route = _resolve_route("上海到昆明那条高速")

        self.assertEqual(route["id"], "shkm")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_exact_short_name_still_resolves(self, _rows):
        route = _resolve_route("包头-茂名")

        self.assertEqual(route["id"], "r1")

    @patch("app.services.route_resolver._route_rows", return_value=ROWS)
    def test_equality_route_filter_strips_model_supplied_wildcards(
        self,
        _rows,
    ):
        """Even if the planner pairs wildcards with '=', route identity is resolved
        through the dictionary rather than passing '%...%' as a literal name."""

        binding = resolve_value(
            "route_name",
            "=",
            "%包头-茂名%",
            "11月2日，包头-茂名高速有多少处泛油？",
            subject="disease",
        )

        self.assertEqual(binding.op, "=")
        self.assertEqual(binding.storage_value, "r1")
        self.assertEqual(binding.display_value, "包头-茂名")
        self.assertEqual(binding.raw_text, "包头-茂名")

    @patch(
        "app.services.route_resolver.load_capability_file",
        return_value={
            "route_name": {
                "怀化市绕城高速公路": ["怀化绕城高速", "怀化绕城"],
            }
        },
    )
    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "hh",
                "code": "",
                "short_name": "怀化市绕城高速公路",
                "full_name": "",
            },
        ],
    )
    def test_registered_route_alias_resolves_without_guessing(
        self,
        _rows,
        _aliases,
    ):
        route = _resolve_route("怀化绕城高速")

        self.assertEqual(route["id"], "hh")

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

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "ws",
                "code": "G0422",
                "short_name": "武汉－深圳",
                "full_name": "G0422武深高速",
            },
        ],
    )
    def test_partial_piece_that_is_only_a_suffix_of_another_name_is_rejected(
        self,
        _rows,
    ):
        """“广深高速”里的“深高速”只是“武深高速”的尾巴，不能当成这条路。"""

        with self.assertRaises(QueryPlanError):
            _resolve_route("广深高速上坑槽有多少处？")

    @patch(
        "app.services.route_resolver._route_rows",
        return_value=[
            {
                "id": "xx",
                "code": "",
                "short_name": "宝安大道西乡段",
                "full_name": "",
            },
        ],
    )
    def test_partial_piece_with_matching_head_still_resolves(self, _rows):
        """“宝安中心恒明珠段”这种“名字前半截 + 片段”写法仍要能认出来。"""

        routes = _route_matches("宝安大道西乡段病害有多少处？")

        self.assertEqual([route["id"] for route in routes], ["xx"])

    @patch(
        "app.services.route_resolver.db_manager.query",
        return_value={"rows": []},
    )
    @patch("app.services.route_resolver._route_rows", return_value=[])
    def test_missing_route_states_the_current_ledger_limitation(
        self,
        _rows,
        _query,
    ):
        with self.assertRaises(QueryPlanError) as raised:
            _resolve_route("S20外环高速")

        message = str(raised.exception)
        self.assertIn("当前路线台账没有收录", message)
        self.assertIn("无法可靠统计", message)


if __name__ == "__main__":
    unittest.main()
