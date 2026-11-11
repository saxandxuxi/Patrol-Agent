import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.contracts import ExecutionObservation, QuerySpec, ValueBinding
from app.query.empty_results import zero_count_observation


class AggregatePresentationTests(unittest.TestCase):
    def test_zero_count_keeps_all_requested_metrics(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count", "total_area", "average_area"),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT COUNT(*), SUM(area), AVG(area)",
            columns=("数量", "总面积", "平均面积"),
            row_count=1,
            preview_rows=(
                {
                    "数量": "0",
                    "总面积": "NULL",
                    "平均面积": "NULL",
                },
            ),
            result_rows=(
                {
                    "数量": "0",
                    "总面积": "NULL",
                    "平均面积": "NULL",
                },
            ),
            rows_fetched=1,
        )

        materialized = zero_count_observation(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(
            materialized.result_rows,
            (
                {
                    "数量": "0",
                    "总面积": None,
                    "平均面积": None,
                },
            ),
        )


class EnumPresentationTests(unittest.TestCase):
    @staticmethod
    def _multi_route_spec():
        return QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("route_name",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="in",
                    storage_value=[
                        "51cb17fca0f811f1a6310242ac120002",
                        "51c9c71ea0f811f1a6310242ac120002",
                    ],
                    display_value="包头-茂名、上海-昆明",
                    source="user_explicit",
                    resolver="route_dictionary",
                ),
            ),
            output_projection=("route_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )

    def test_explicit_entity_values_fill_cartesian_enum_groups(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("problem_type_name", "up_or_down"),
            filters=(
                ValueBinding(
                    field="problem_type_name",
                    op="=",
                    storage_value=["坑槽", "纵向裂缝"],
                    display_value="坑槽和纵向裂缝",
                    source="user_explicit",
                    resolver="damage_type_dictionary_expand",
                ),
            ),
            output_projection=("problem_type_name", "up_or_down", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT problem_type_name, up_or_down, COUNT(*)",
            columns=("病害类型", "上下行", "数量"),
            row_count=2,
            preview_rows=(
                {"病害类型": "纵向裂缝", "上下行": "上行", "数量": "104"},
                {"病害类型": "坑槽", "上下行": "上行", "数量": "9"},
            ),
            result_rows=(
                {"病害类型": "纵向裂缝", "上下行": "上行", "数量": "104"},
                {"病害类型": "坑槽", "上下行": "上行", "数量": "9"},
            ),
            rows_fetched=2,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(len(rows), 4)
        self.assertEqual(
            set(filled),
            {"坑槽/下行", "纵向裂缝/下行"},
        )
        self.assertIn(
            {"病害类型": "坑槽", "上下行": "下行", "数量": 0},
            rows,
        )

    def test_single_route_filter_does_not_fill_storage_id_group(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count", "total_area"),
            dimensions=("route_name",),
            filters=(
                ValueBinding(
                    field="route_name",
                    op="=",
                    storage_value="51cb17fca0f811f1a6310242ac120002",
                    display_value="包头-茂名",
                    source="user_explicit",
                    resolver="route_dictionary",
                ),
            ),
            output_projection=("route_name", "count", "total_area"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        row = {
            "路线名称": "包头-茂名",
            "数量": "1741",
            "总面积": "28234.94",
        }
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT route_name, COUNT(*), SUM(area)",
            columns=("路线名称", "数量", "总面积"),
            row_count=1,
            preview_rows=(row,),
            result_rows=(row,),
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(rows, [row])
        self.assertEqual(filled, [])

    def test_present_multi_route_groups_use_display_names_only(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        original_rows = [
            {"路线名称": "上海-昆明", "数量": "4"},
            {"路线名称": "包头-茂名", "数量": "2"},
        ]
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT route_name, COUNT(*)",
            columns=("路线名称", "数量"),
            row_count=2,
            preview_rows=tuple(original_rows),
            result_rows=tuple(original_rows),
            rows_fetched=2,
        )

        rows, filled = complete_explicit_enum_groups(
            self._multi_route_spec(),
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(rows, original_rows)
        self.assertEqual(filled, [])

    def test_missing_multi_route_group_is_filled_with_display_name(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        row = {"路线名称": "上海-昆明", "数量": "4"}
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT route_name, COUNT(*)",
            columns=("路线名称", "数量"),
            row_count=1,
            preview_rows=(row,),
            result_rows=(row,),
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            self._multi_route_spec(),
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(filled, ["包头-茂名"])
        self.assertIn({"路线名称": "包头-茂名", "数量": 0}, rows)
        self.assertFalse(
            any(
                "51cb17fca0f811f1a6310242ac120002" in str(value)
                for row in rows
                for value in row.values()
            )
        )

    def test_two_explicit_enum_dimensions_fill_cartesian_zero_groups(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("road_surface_type", "tech_grade"),
            filters=(
                ValueBinding(
                    field="road_surface_type",
                    op="in",
                    storage_value=["沥青混凝土", "水泥混凝土"],
                    display_value="沥青混凝土、水泥混凝土",
                    source="user_explicit",
                    resolver="enum:ai_problem.road_surface_type",
                ),
                ValueBinding(
                    field="tech_grade",
                    op="in",
                    storage_value=["1", "2"],
                    display_value="一级公路、二级公路",
                    source="user_explicit",
                    resolver="enum:basic_facilities_sections.tech_grade",
                ),
            ),
            output_projection=("road_surface_type", "tech_grade", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT road_surface_type, tech_grade, COUNT(*)",
            columns=("路面类型", "公路技术等级", "数量"),
            row_count=2,
            preview_rows=(
                {
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "409",
                },
                {
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "131",
                },
            ),
            result_rows=(
                {
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "409",
                },
                {
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "131",
                },
            ),
            rows_fetched=2,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(len(rows), 4)
        self.assertEqual(
            set(filled),
            {"水泥混凝土/一级公路", "水泥混凝土/二级公路"},
        )
        for row in rows:
            if row["路面类型"] == "水泥混凝土":
                self.assertEqual(row["数量"], 0)

    def test_unfiltered_enum_dimension_is_not_invented(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("road_surface_type", "tech_grade"),
            filters=(
                ValueBinding(
                    field="road_surface_type",
                    op="in",
                    storage_value=["沥青混凝土", "水泥混凝土"],
                    display_value="沥青混凝土、水泥混凝土",
                    source="user_explicit",
                    resolver="enum:ai_problem.road_surface_type",
                ),
            ),
            output_projection=("road_surface_type", "tech_grade", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        original_rows = (
            {
                "路面类型": "沥青混凝土",
                "公路技术等级": "一级公路",
                "数量": "409",
            },
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT road_surface_type, tech_grade, COUNT(*)",
            columns=("路面类型", "公路技术等级", "数量"),
            row_count=1,
            preview_rows=original_rows,
            result_rows=original_rows,
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(tuple(rows), original_rows)
        self.assertEqual(filled, [])

    def test_explicit_enum_domain_fills_missing_zero_group(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down",),
            filters=(),
            output_projection=("up_or_down", "count"),
            order_by=(),
            limit=None,
            required_assertions=("complete_enum_dimensions",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT up_or_down, COUNT(*)",
            columns=("上下行方向", "数量"),
            row_count=1,
            preview_rows=({"上下行方向": "上行", "数量": "16"},),
            result_rows=({"上下行方向": "上行", "数量": "16"},),
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(filled, ["下行"])
        self.assertIn({"上下行方向": "下行", "数量": 0}, rows)

    def test_direction_dimension_uses_registered_complete_domain_policy(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down",),
            filters=(),
            output_projection=("up_or_down", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT up_or_down, COUNT(*)",
            columns=("上下行方向", "数量"),
            row_count=1,
            preview_rows=({"上下行方向": "上行", "数量": "16"},),
            result_rows=({"上下行方向": "上行", "数量": "16"},),
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(filled, ["下行"])
        self.assertIn({"上下行方向": "下行", "数量": 0}, rows)

    def test_closed_direction_uses_observed_categories_for_counterfactual_zero(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("up_or_down", "problem_type_name"),
            filters=(),
            output_projection=("up_or_down", "problem_type_name", "count"),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT up_or_down, problem_type_name, COUNT(*)",
            columns=("上下行方向", "病害类型", "数量"),
            row_count=2,
            preview_rows=(
                {"上下行方向": "下行", "病害类型": "条状修补", "数量": "1396"},
                {"上下行方向": "下行", "病害类型": "横向裂缝", "数量": "173"},
            ),
            result_rows=(
                {"上下行方向": "下行", "病害类型": "条状修补", "数量": "1396"},
                {"上下行方向": "下行", "病害类型": "横向裂缝", "数量": "173"},
            ),
            rows_fetched=2,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(len(rows), 4)
        self.assertEqual(
            set(filled),
            {"上行/条状修补", "上行/横向裂缝"},
        )
        self.assertIn(
            {"上下行方向": "上行", "病害类型": "条状修补", "数量": 0},
            rows,
        )

    def test_top_groups_does_not_fill_zero_value_groups_after_ranking(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("up_or_down", "problem_type_name"),
            filters=(),
            output_projection=("up_or_down", "problem_type_name", "count"),
            order_by=(
                {
                    "field": "count",
                    "direction": "desc",
                },
            ),
            limit=1,
            required_assertions=(
                "preserve_user_scope",
                "top_per_outer_group",
            ),
        )
        original_rows = (
            {"上下行方向": "下行", "病害类型": "条状修补", "数量": "1396"},
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT up_or_down, problem_type_name, COUNT(*)",
            columns=("上下行方向", "病害类型", "数量"),
            row_count=1,
            preview_rows=original_rows,
            result_rows=original_rows,
            rows_fetched=1,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(tuple(rows), original_rows)
        self.assertEqual(filled, [])

    def test_top_groups_reports_configured_direction_missing_without_filter(self):
        from app.query.presentation.enumerations import (
            missing_explicit_top_group_labels,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="top_groups",
            metrics=("count",),
            dimensions=("up_or_down", "problem_type_name"),
            filters=(),
            output_projection=("up_or_down", "problem_type_name", "count"),
            order_by=(
                {
                    "field": "count",
                    "direction": "desc",
                },
            ),
            limit=1,
            required_assertions=(
                "preserve_user_scope",
                "top_per_outer_group",
            ),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT up_or_down, problem_type_name, COUNT(*)",
            columns=("上下行方向", "病害类型", "数量"),
            row_count=1,
            preview_rows=(
                {"上下行方向": "下行", "病害类型": "条状修补", "数量": "1396"},
            ),
            result_rows=(
                {"上下行方向": "下行", "病害类型": "条状修补", "数量": "1396"},
            ),
            rows_fetched=1,
        )

        missing = missing_explicit_top_group_labels(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(missing, ["上下行方向 上行"])

    def test_time_dimension_is_completed_from_explicit_time_window(self):
        from app.query.presentation.enumerations import (
            complete_explicit_enum_groups,
        )

        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="distribution",
            metrics=("count",),
            dimensions=("month", "road_surface_type", "tech_grade"),
            filters=(
                ValueBinding(
                    field="collection_time",
                    op=">=",
                    storage_value="2026-10-01 00:00:00",
                    display_value="2026-10-01 00:00:00",
                    source="user_explicit",
                    resolver="time_range",
                ),
                ValueBinding(
                    field="collection_time",
                    op="<",
                    storage_value="2026-12-01 00:00:00",
                    display_value="2026-12-01 00:00:00",
                    source="user_explicit",
                    resolver="time_range",
                ),
                ValueBinding(
                    field="road_surface_type",
                    op="in",
                    storage_value=["沥青混凝土", "水泥混凝土"],
                    display_value="沥青混凝土、水泥混凝土",
                    source="user_explicit",
                    resolver="enum:ai_problem.road_surface_type",
                ),
                ValueBinding(
                    field="tech_grade",
                    op="in",
                    storage_value=["1", "2"],
                    display_value="一级公路、二级公路",
                    source="user_explicit",
                    resolver="enum:basic_facilities_sections.tech_grade",
                ),
            ),
            output_projection=(
                "month",
                "road_surface_type",
                "tech_grade",
                "count",
            ),
            order_by=(),
            limit=None,
            required_assertions=("preserve_user_scope",),
        )
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT month, road_surface_type, tech_grade, COUNT(*)",
            columns=("月份", "路面类型", "公路技术等级", "数量"),
            row_count=4,
            preview_rows=(
                {
                    "月份": "2026-10",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "409",
                },
                {
                    "月份": "2026-10",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "131",
                },
                {
                    "月份": "2026-11",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "134",
                },
                {
                    "月份": "2026-11",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "54",
                },
            ),
            result_rows=(
                {
                    "月份": "2026-10",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "409",
                },
                {
                    "月份": "2026-10",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "131",
                },
                {
                    "月份": "2026-11",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "一级公路",
                    "数量": "134",
                },
                {
                    "月份": "2026-11",
                    "路面类型": "沥青混凝土",
                    "公路技术等级": "二级公路",
                    "数量": "54",
                },
            ),
            rows_fetched=4,
        )

        rows, filled = complete_explicit_enum_groups(
            spec,
            observation,
            CapabilityRegistry.load(),
        )

        self.assertEqual(len(rows), 8)
        self.assertEqual(
            set(filled),
            {
                "2026-10/水泥混凝土/一级公路",
                "2026-10/水泥混凝土/二级公路",
                "2026-11/水泥混凝土/一级公路",
                "2026-11/水泥混凝土/二级公路",
            },
        )
        for row in rows:
            if row["路面类型"] == "水泥混凝土":
                self.assertEqual(row["数量"], 0)


if __name__ == "__main__":
    unittest.main()
