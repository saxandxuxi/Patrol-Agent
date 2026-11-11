import unittest
from types import MappingProxyType, SimpleNamespace

from app.query.capability_registry.fact_conflicts import (
    validate_fact_conflicts,
)
from app.query.capability_registry.models import (
    EnumCapability,
    EnumValueCapability,
    FieldCapability,
    MetricCapability,
    SubjectCapability,
)
from app.query.mdl_sql.models import (
    MdlColumn,
    MdlCube,
    MdlCubeField,
    MdlModel,
    MdlSnapshot,
)


def empty_snapshot(**changes):
    values = {
        "subjects": {},
        "fields": {},
        "metrics": {},
        "enums": {},
        "table_labels": {},
    }
    values.update(changes)
    return SimpleNamespace(**values)


def empty_mdl(**changes):
    values = {
        "models": {},
        "cubes": {},
        "relationships": {},
        "physical_table_to_model": {},
    }
    values.update(changes)
    return MdlSnapshot(
        fingerprint="test",
        models=MappingProxyType(values["models"]),
        cubes=MappingProxyType(values["cubes"]),
        relationships=MappingProxyType(values["relationships"]),
        physical_table_to_model=MappingProxyType(
            values["physical_table_to_model"]
        ),
        source_dir="",
    )


class FactConflictTests(unittest.TestCase):
    def test_metric_formula_identity_keeps_explicit_model_qualifiers(self):
        snapshot = empty_snapshot(
            subjects={
                "patrol": SubjectCapability(
                    subject_id="patrol",
                    base_table="ai_maintain_patrol",
                    mdl_model="patrol",
                )
            },
            metrics={
                "patrol": {
                    "count": MetricCapability(
                        metric_id="count",
                        expression="COUNT(DISTINCT {alias}.id)",
                        label="任务数",
                        source_kind="resources",
                        source_reference="metrics.yaml:patrol.count",
                    ),
                    "linked_disease_count": MetricCapability(
                        metric_id="linked_disease_count",
                        expression="COUNT(DISTINCT {disease}.id)",
                        label="关联病害数",
                        source_kind="resources",
                        source_reference=(
                            "metrics.yaml:patrol.linked_disease_count"
                        ),
                    ),
                }
            },
        )
        mdl = empty_mdl(
            models={
                "patrol": MdlModel(
                    name="patrol",
                    physical_table="ai_maintain_patrol",
                    description="",
                    columns={
                        "id": MdlColumn("id", "VARCHAR", ""),
                    },
                )
            },
            physical_table_to_model={
                "ai_maintain_patrol": "patrol",
            },
        )

        report = validate_fact_conflicts(snapshot, mdl)

        self.assertTrue(report.accepted, report.conflicts)

    def test_duplicate_metric_formula_owner_fails_before_context_build(self):
        snapshot = empty_snapshot(
            subjects={
                "disease": SubjectCapability(
                    subject_id="disease",
                    base_table="ai_problem",
                    mdl_model="disease",
                )
            },
            metrics={
                "disease": {
                    "total_area": MetricCapability(
                        metric_id="total_area",
                        expression="SUM(area)",
                        label="总面积",
                        source_kind="resources",
                        source_reference="metrics.yaml:disease.total_area",
                    )
                }
            },
        )
        mdl = empty_mdl(
            models={
                "disease": MdlModel(
                    name="disease",
                    physical_table="ai_problem",
                    description="",
                    columns={"area": MdlColumn("area", "DOUBLE", "")},
                )
            },
            cubes={
                "disease_metrics": MdlCube(
                    name="disease_metrics",
                    base_object="disease",
                    description="",
                    measures=(
                        MdlCubeField(
                            name="total_area",
                            expression="SUM(area)",
                            data_type="DOUBLE",
                            description="",
                        ),
                    ),
                    dimensions=(),
                )
            },
        )

        report = validate_fact_conflicts(snapshot, mdl)

        self.assertFalse(report.accepted)
        self.assertEqual(
            report.conflicts[0].code,
            "duplicate_metric_formula_owner",
        )

    def test_missing_join_fails_as_structured_conflict(self):
        snapshot = empty_snapshot(
            subjects={
                "disease": SubjectCapability(
                    subject_id="disease",
                    base_table="ai_problem",
                    mdl_model="disease",
                )
            },
            fields={
                "disease": {
                    "route_name": FieldCapability(
                        field_id="route_name",
                        expression="disease.route_name",
                        label="路线名称",
                        physical_table="ai_problem",
                        physical_column="route_name",
                        requires_join=("route",),
                    )
                }
            },
        )
        mdl = empty_mdl(
            models={
                "disease": MdlModel(
                    name="disease",
                    physical_table="ai_problem",
                    description="",
                    columns={
                        "route_name": MdlColumn(
                            "route_name",
                            "VARCHAR",
                            "",
                        )
                    },
                ),
                "route": MdlModel(
                    name="route",
                    physical_table="route",
                    description="",
                    columns={},
                ),
            },
            physical_table_to_model={
                "ai_problem": "disease",
                "route": "route",
            },
        )

        report = validate_fact_conflicts(snapshot, mdl)

        self.assertFalse(report.accepted)
        self.assertEqual(report.conflicts[0].code, "missing_join")

    def test_enum_label_mismatch_is_structured(self):
        snapshot = empty_snapshot(
            enums={
                ("ai_problem", "up_or_down"): EnumCapability(
                    key="ai_problem.up_or_down",
                    table="ai_problem",
                    column="up_or_down",
                    label="上下行",
                    display_column="up_or_down_name",
                    values={
                        "1": EnumValueCapability(
                            code="1",
                            label="上行",
                            display="上行",
                        )
                    },
                )
            },
            fields={
                "disease": {
                    "up_or_down": FieldCapability(
                        field_id="up_or_down",
                        expression="disease.up_or_down_name",
                        label="方向错误",
                        physical_table="ai_problem",
                        physical_column="up_or_down",
                        enum_key="ai_problem.up_or_down",
                    )
                }
            },
        )

        report = validate_fact_conflicts(snapshot, empty_mdl())

        self.assertFalse(report.accepted)
        self.assertEqual(report.conflicts[0].code, "enum_label_mismatch")

    def test_table_label_cannot_be_repeated_as_mdl_description(self):
        snapshot = empty_snapshot(
            subjects={
                "disease": SubjectCapability(
                    subject_id="disease",
                    base_table="ai_problem",
                    mdl_model="disease",
                )
            },
            table_labels={"ai_problem": "病害记录"},
        )
        mdl = empty_mdl(
            models={
                "disease": MdlModel(
                    name="disease",
                    physical_table="ai_problem",
                    description="病害记录",
                    columns={},
                )
            },
            physical_table_to_model={"ai_problem": "disease"},
        )

        report = validate_fact_conflicts(snapshot, mdl)

        self.assertFalse(report.accepted)
        self.assertEqual(
            report.conflicts[0].code,
            "table_label_duplicated_in_mdl",
        )


if __name__ == "__main__":
    unittest.main()
