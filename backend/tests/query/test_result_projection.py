import unittest

from app.query.contracts import ExecutionObservation, QuerySpec
from app.query.result_projection import result_field_map
from app.query.service import _observation_payload


def scalar_spec():
    return QuerySpec(
        id="q1",
        kind="mysql",
        subject="disease",
        view="count",
        metrics=("count", "total_area"),
        dimensions=(),
        filters=(),
        output_projection=(),
        order_by=(),
        limit=None,
        required_assertions=(),
        question="统计数量和总面积",
    )


class ResultProjectionTests(unittest.TestCase):
    def test_order_by_field_does_not_break_positional_mapping(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="list",
            metrics=(),
            dimensions=("patrol_number",),
            filters=(),
            output_projection=("patrol_number",),
            order_by=({"field": "create_time", "direction": "desc"},),
            limit=None,
            required_assertions=(),
            question="最近5次巡查",
        )

        mapping = result_field_map(spec, ["任务编号"])

        self.assertEqual(mapping["patrol_number"], "任务编号")

    def test_metric_aliases_ignore_extra_columns_from_templates(self):
        columns = ("路线", "处数", "总面积", "平均面积", "最早采集", "最晚采集")

        mapping = result_field_map(scalar_spec(), columns)

        self.assertEqual(mapping["count"], "处数")
        self.assertEqual(mapping["total_area"], "总面积")

    def test_observation_payload_uses_real_metric_columns(self):
        observation = ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="template:preset",
            sql="SELECT 1",
            columns=("路线", "处数", "总面积", "平均面积"),
            row_count=1,
            preview_rows=(
                {
                    "路线": "路线A",
                    "处数": 10,
                    "总面积": 20,
                    "平均面积": 2,
                },
            ),
            result_rows=(
                {
                    "路线": "路线A",
                    "处数": 10,
                    "总面积": 20,
                    "平均面积": 2,
                },
            ),
            rows_fetched=1,
        )

        payload = _observation_payload(
            observation,
            spec=scalar_spec(),
            question="统计数量和总面积",
            answer="",
        )

        self.assertEqual(payload["field_labels"]["count"], "处数")
        self.assertEqual(payload["field_labels"]["total_area"], "总面积")

