"""第 5 单元考试暴露的共性契约回归：like、设备字段、空聚合验收。"""

import unittest

from app.query.capability_registry import CapabilityRegistry
from app.query.compiler import CompiledQuery
from app.query.contracts import (
    ExecutionObservation,
    QuerySpec,
    QuerySpecError,
    ValueBinding,
)
from app.query.spec_mdl import _patrol_dimension, _patrol_filter, _patrol_output
from app.query.service import _aggregate_is_empty
from app.query.values import resolve_value
from app.query.verifier import verify_result


def binding(field, op, value):
    return ValueBinding(
        field=field,
        op=op,
        storage_value=value,
        display_value=str(value),
        source="user_explicit",
        resolver="test",
        raw_text=str(value),
    )


class LikeOperatorContractTests(unittest.TestCase):
    """模型写 like 时必须回到字典解析，不能把通配模式编进 SQL。"""

    def test_like_crack_pattern_expands_through_family(self):
        value = resolve_value(
            "problem_type_name",
            "like",
            "%裂缝%",
            "长芷高速的裂缝各有多少处",
            "mysql",
            subject="disease",
        )

        self.assertEqual(value.op, "=")
        self.assertEqual(
            set(value.storage_value),
            {"横向裂缝", "纵向裂缝", "网状裂缝"},
        )
        self.assertTrue(value.resolver.startswith("entity_family:"))

    def test_like_unknown_disease_value_is_rejected(self):
        with self.assertRaises(QuerySpecError):
            resolve_value(
                "problem_type_name",
                "like",
                "%不存在的病害%",
                "查一下不存在的病害有多少处",
                "mysql",
                subject="disease",
            )

    def test_like_enum_value_resolves_to_storage_code(self):
        value = resolve_value(
            "up_or_down",
            "like",
            "%上行%",
            "只看上行的病害数量",
            "mysql",
            subject="disease",
        )

        self.assertEqual(value.op, "=")
        self.assertEqual(value.storage_value, "1")


class PatrolDeviceFieldContractTests(unittest.TestCase):
    """设备编号必须只有一个物理落点：过滤、分组、输出都指向 patrol.equipment_id。"""

    @classmethod
    def setUpClass(cls):
        cls.registry = CapabilityRegistry.load()

    def test_registry_maps_device_code_to_equipment_id(self):
        capability = self.registry.snapshot.fields["patrol"]["device_code"]

        self.assertEqual(capability.physical_table, "ai_maintain_patrol")
        self.assertEqual(capability.physical_column, "equipment_id")

    def test_dimension_and_filter_share_the_same_column(self):
        joins: set[str] = set()
        expression, label = _patrol_dimension("device_code", joins)
        where: list[str] = []
        _patrol_filter("device_code", "=", "DEV-305", joins, where)

        self.assertEqual(expression, "patrol.equipment_id")
        self.assertEqual(label, "设备编号")
        self.assertEqual(where, ["patrol.equipment_id = 'DEV-305'"])

    def test_output_projection_can_show_device_code(self):
        self.assertEqual(
            _patrol_output("device_code"),
            ("patrol.equipment_id", "设备编号"),
        )

    def test_user_supplied_device_code_list_keeps_user_explicit_source(self):
        value = resolve_value(
            "device_code",
            "in",
            ["DEV-8821", "DEV-9901"],
            "DEV-8821 和 DEV-9901 各自完成了多少次巡检任务？",
            "mysql",
            subject="patrol",
            registry=self.registry,
        )

        self.assertEqual(value.source, "user_explicit")
        self.assertEqual(value.storage_value, ["DEV-8821", "DEV-9901"])


class AggregateEmptyVerificationTests(unittest.TestCase):
    """已核对覆盖的空聚合（COUNT=0 / SUM=NULL）是事实，不是指标损坏。"""

    def _spec(self):
        return QuerySpec(
            id="q1",
            kind="mysql",
            subject="patrol",
            view="sum",
            metrics=("sum_patrol_mileage",),
            dimensions=(),
            filters=(
                binding("device_code", "=", "DEV-305"),
                binding("create_time", ">=", "2026-10-01 00:00:00"),
                binding("create_time", "<", "2026-11-01 00:00:00"),
            ),
            output_projection=("sum_patrol_mileage",),
            order_by=(),
            limit=None,
            required_assertions=(),
            question="统计 DEV-305 十月的巡检里程",
        )

    def _observation(self, value):
        return ExecutionObservation(
            node_id="q1",
            ok=True,
            engine="wren",
            sql="SELECT SUM(patrol_mileage)",
            rows_fetched=1,
            preview_rows=({"巡检里程": value},),
            result_rows=({"巡检里程": value},),
        )

    def test_null_sum_with_verified_scope_is_accepted(self):
        report = verify_result(
            self._spec(),
            CompiledQuery(sql="SELECT 1", engine="wren", source="spec"),
            self._observation(None),
            scope_has_records=True,
        )

        self.assertEqual(report.status, "accepted")

    def test_null_sum_is_a_valid_empty_fact_without_coverage_catalog(self):
        report = verify_result(
            self._spec(),
            CompiledQuery(sql="SELECT 1", engine="wren", source="spec"),
            self._observation(None),
            scope_has_records=False,
        )

        self.assertEqual(report.status, "accepted")

    def test_non_empty_sum_is_untouched(self):
        report = verify_result(
            self._spec(),
            CompiledQuery(sql="SELECT 1", engine="wren", source="spec"),
            self._observation("1024.50"),
            scope_has_records=True,
        )

        self.assertEqual(report.status, "accepted")

    def test_string_null_aggregate_is_treated_as_empty(self):
        """Wren 把空聚合返回成字符串 "NULL"，也要按空处理。"""

        self.assertTrue(_aggregate_is_empty(self._spec(), self._observation("NULL")))
        self.assertFalse(_aggregate_is_empty(self._spec(), self._observation("0.00")))
        self.assertFalse(_aggregate_is_empty(self._spec(), self._observation("1024.50")))


if __name__ == "__main__":
    unittest.main()
