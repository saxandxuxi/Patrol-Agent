from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from ...services import schema_catalog
from .loader import CapabilityConfigError, load_capability_config
from .models import (
    CapabilitySnapshot,
    DerivedMetricCapability,
    EntityCapability,
    EntityFamilyCapability,
    EnumCapability,
    EnumValueCapability,
    FieldCapability,
    MetricCapability,
    PlannerCapabilityView,
    PlannerFieldView,
    SubjectCapability,
    freeze_snapshot,
)


class CapabilityRegistry:
    def __init__(self, snapshot: CapabilitySnapshot):
        self._snapshot = snapshot

    @property
    def snapshot(self) -> CapabilitySnapshot:
        return self._snapshot

    @classmethod
    def load(
        cls,
        kind: str = "mysql",
        config_dir: Path | None = None,
    ) -> "CapabilityRegistry":
        loaded = load_capability_config(config_dir)
        docs = loaded.documents
        subjects = {
            str(subject_id): SubjectCapability(
                subject_id=str(subject_id),
                base_table=str(raw.get("base_table") or ""),
                joins=dict(raw.get("joins") or {}),
                allowed_views=tuple(str(item) for item in raw.get("allowed_views") or ()),
            )
            for subject_id, raw in docs["subjects"].items()
            if isinstance(raw, dict)
        }
        fields = {
            str(subject): {
                str(field_id): FieldCapability(
                    field_id=str(field_id),
                    expression=str(raw.get("expression") or ""),
                    label=str(raw.get("label") or field_id),
                    roles=tuple(str(item) for item in raw.get("roles") or ()),
                    unit=str(raw.get("unit") or ""),
                    physical_table=str(raw.get("physical_table") or ""),
                    physical_column=str(raw.get("physical_column") or ""),
                    requires_join=tuple(str(item) for item in raw.get("requires_join") or ()),
                    enum_key=str(raw.get("enum_key") or ""),
                )
                for field_id, raw in raw_fields.items()
                if isinstance(raw, dict)
            }
            for subject, raw_fields in docs["fields"].items()
            if isinstance(raw_fields, dict)
        }
        metrics = {
            str(subject): {
                str(metric_id): MetricCapability(
                    metric_id=str(metric_id),
                    expression=str(raw.get("expression") or ""),
                    label=str(raw.get("label") or metric_id),
                    unit=str(raw.get("unit") or ""),
                    allowed_views=tuple(str(item) for item in raw.get("allowed_views") or ()),
                    requires_join=tuple(str(item) for item in raw.get("requires_join") or ()),
                )
                for metric_id, raw in raw_metrics.items()
                if isinstance(raw, dict) and not str(metric_id).startswith("_")
            }
            for subject, raw_metrics in docs["metrics"].items()
            if isinstance(raw_metrics, dict) and not str(subject).startswith("_")
        }
        derived_metrics = {
            str(subject): {
                str(metric_id): DerivedMetricCapability(
                    metric_id=str(metric_id),
                    label=str(raw.get("label") or metric_id),
                    expression=str(raw.get("expression") or ""),
                    operands=tuple(str(item) for item in raw.get("operands") or ()),
                    evaluator=str(raw.get("evaluator") or "post_calc"),
                    unit=str(raw.get("unit") or ""),
                    lower_is_better=bool(raw.get("lower_is_better", False)),
                )
                for metric_id, raw in raw_group.items()
                if isinstance(raw, dict)
            }
            for subject, raw_group in docs["derived_metrics"].items()
            if isinstance(raw_group, dict)
        }
        enums: dict[tuple[str, str], EnumCapability] = {}
        for key, raw in docs["enums"].items():
            if not isinstance(raw, dict):
                continue
            table = str(raw.get("table") or "")
            column = str(raw.get("column") or "")
            if not table or not column:
                raise CapabilityConfigError(f"invalid_enum_key: {key}")
            enum_key = (table, column)
            if enum_key in enums:
                raise CapabilityConfigError(f"duplicate_enum: {table}.{column}")
            values = {
                str(code): EnumValueCapability(
                    code=str(code),
                    label=str(item.get("label") or ""),
                    display=str(item.get("display") or item.get("label") or ""),
                )
                for code, item in (raw.get("values") or {}).items()
                if isinstance(item, dict)
            }
            enums[enum_key] = EnumCapability(
                key=f"{table}.{column}",
                table=table,
                column=column,
                label=str(raw.get("label") or column),
                values=values,
            )
        entities = {
            str(entity_id): EntityCapability(
                entity_id=str(entity_id),
                source=dict(raw.get("source") or {}),
                identity=str(raw.get("identity") or ""),
            )
            for entity_id, raw in docs["entities"].items()
            if isinstance(raw, dict)
        }
        families = {
            str(family_id): EntityFamilyCapability(
                family_id=str(family_id),
                entity=str(raw.get("entity") or ""),
                label=str(raw.get("label") or family_id),
                aliases=tuple(str(item) for item in raw.get("aliases") or ()),
                members=tuple(str(item) for item in raw.get("members") or ()),
                resolution={
                    str(key): str(value)
                    for key, value in (raw.get("resolution") or {}).items()
                },
            )
            for family_id, raw in docs["families"].items()
            if isinstance(raw, dict)
        }
        snapshot = CapabilitySnapshot(
            version=loaded.version,
            config_hash=loaded.config_hash,
            subjects=subjects,
            fields=fields,
            metrics=metrics,
            derived_metrics=derived_metrics,
            enums=enums,
            entities=entities,
            entity_families=families,
            aliases=dict(docs["aliases"]),
            identifiers=dict(docs["identifiers"]),
            query_shapes=dict(docs["query_shapes"]),
            dimensions=dict(docs["dimensions"]),
            predicates=dict(docs["predicates"]),
            time_buckets=dict(docs["time_buckets"]),
            result_words=dict(docs["result_words"]),
            repair_policy=dict(docs["repair_policy"]),
            templates=dict(docs["templates"]),
            calculators=dict(docs["calculators"]),
        )
        registry = cls(snapshot)
        registry._augment_physical_fields(kind)
        registry._augment_numeric_metrics()
        registry._augment_enums(kind)
        registry._validate_physical_fields(kind)
        registry._snapshot = freeze_snapshot(registry.snapshot)
        return registry

    def planner_view(self, subject: str, reach: Any) -> PlannerCapabilityView:
        if subject not in self.snapshot.subjects:
            raise KeyError(f"unknown subject: {subject}")
        subject_cap = self.snapshot.subjects[subject]
        fields = {}
        for field_id, field in self.snapshot.fields.get(subject, {}).items():
            enum = self.snapshot.enums.get((field.physical_table, field.physical_column))
            fields[field_id] = PlannerFieldView(
                field_id=field_id,
                label=field.label,
                data_type=field.data_type,
                comment=field.comment,
                roles=field.roles,
                enum_values=tuple(enum.values.values()) if enum else (),
            )
        evidence = tuple(
            {
                "table": hit.table,
                "column": hit.column,
                "matched_text": hit.matched_text,
                "source": hit.source,
                "score": hit.score,
            }
            for hit in getattr(reach, "hits", ())
        )
        metric_details = {
            metric_id: {
                "label": metric.label,
                "unit": metric.unit,
                "allowed_views": list(metric.allowed_views),
                "requires_join": list(metric.requires_join),
            }
            for metric_id, metric in self.snapshot.metrics.get(subject, {}).items()
        }
        derived_metric_details = {
            metric_id: {
                "label": metric.label,
                "unit": metric.unit,
                "expression": metric.expression,
                "operands": list(metric.operands),
                "evaluator": metric.evaluator,
                "lower_is_better": metric.lower_is_better,
            }
            for metric_id, metric in self.snapshot.derived_metrics.get(
                subject,
                {},
            ).items()
        }
        return PlannerCapabilityView(
            subject=subject,
            base_table=subject_cap.base_table,
            fields=fields,
            metrics=tuple(self.snapshot.metrics.get(subject, {})),
            derived_metrics=tuple(self.snapshot.derived_metrics.get(subject, {})),
            allowed_views=subject_cap.allowed_views,
            reach_evidence=evidence,
            metric_details=metric_details,
            derived_metric_details=derived_metric_details,
        )

    def metric(
        self,
        subject: str,
        metric_id: str,
    ) -> MetricCapability | None:
        return self.snapshot.metrics.get(subject, {}).get(metric_id)

    def derived_metric(
        self,
        subject: str,
        metric_id: str,
    ) -> DerivedMetricCapability | None:
        return self.snapshot.derived_metrics.get(subject, {}).get(metric_id)

    def _augment_physical_fields(self, kind: str) -> None:
        catalog = schema_catalog.field_catalog(kind)
        by_table = {
            str(table.get("table") or ""): table
            for table in catalog.get("tables") or []
        }
        for subject_id, subject in self.snapshot.subjects.items():
            table = by_table.get(subject.base_table)
            if not table:
                continue
            subject_fields = dict(self.snapshot.fields.get(subject_id) or {})
            for column in table.get("columns") or []:
                field_id = str(column.get("name") or "")
                if not field_id:
                    continue
                if field_id in subject_fields:
                    existing = subject_fields[field_id]
                    subject_fields[field_id] = replace(
                        existing,
                        data_type=existing.data_type or str(column.get("type") or ""),
                        comment=existing.comment or str(column.get("comment") or ""),
                        roles=existing.roles
                        or tuple(str(item) for item in column.get("roles") or ()),
                        label=existing.label or str(column.get("label") or field_id),
                        physical_table=existing.physical_table or subject.base_table,
                        physical_column=existing.physical_column or field_id,
                    )
                    continue
                subject_fields[field_id] = FieldCapability(
                    field_id=field_id,
                    expression=f"{subject_id}.{field_id}",
                    label=str(column.get("label") or field_id),
                    data_type=str(column.get("type") or ""),
                    comment=str(column.get("comment") or ""),
                    roles=tuple(str(item) for item in column.get("roles") or ()),
                    physical_table=subject.base_table,
                    physical_column=field_id,
                )
            self.snapshot.fields[subject_id] = subject_fields

    def _augment_enums(self, kind: str) -> None:
        catalog = schema_catalog.field_catalog(kind)
        for table in catalog.get("tables") or []:
            table_name = str(table.get("table") or "")
            for column in table.get("columns") or []:
                column_name = str(column.get("name") or "")
                code_map = dict(column.get("code_map") or {})
                key = (table_name, column_name)
                if not code_map or key in self.snapshot.enums:
                    continue
                self.snapshot.enums[key] = EnumCapability(
                    key=f"{table_name}.{column_name}",
                    table=table_name,
                    column=column_name,
                    label=str(column.get("label") or column_name),
                    values={
                        str(code): EnumValueCapability(
                            code=str(code),
                            label=str(label),
                            display=str(label),
                        )
                        for code, label in code_map.items()
                    },
                )

    def _augment_numeric_metrics(self) -> None:
        for subject_id, fields in self.snapshot.fields.items():
            metrics = dict(self.snapshot.metrics.get(subject_id) or {})
            for field_id, field in fields.items():
                numeric = (
                    "numeric" in field.roles
                    or any(
                        token in field.data_type.lower()
                        for token in ("int", "decimal", "double", "float", "numeric")
                    )
                )
                if not numeric or not field.physical_column:
                    continue
                for prefix, aggregate in (
                    ("sum_", "SUM"),
                    ("avg_", "AVG"),
                    ("min_", "MIN"),
                    ("max_", "MAX"),
                ):
                    metric_id = f"{prefix}{field_id}"
                    if metric_id in metrics:
                        continue
                    metrics[metric_id] = MetricCapability(
                        metric_id=metric_id,
                        expression=f"{aggregate}({{alias}}.{field.physical_column})",
                        label=f"{field.label}{'合计' if prefix == 'sum_' else '平均' if prefix == 'avg_' else '最小' if prefix == 'min_' else '最大'}",
                        allowed_views=("count", "distribution", "top", "trend"),
                    )
            self.snapshot.metrics[subject_id] = metrics

    def _validate_physical_fields(self, kind: str) -> None:
        catalog = schema_catalog.field_catalog(kind)
        physical = {
            (str(table.get("table") or ""), str(column.get("name") or ""))
            for table in catalog.get("tables") or []
            for column in table.get("columns") or []
        }
        for subject_fields in self.snapshot.fields.values():
            for field in subject_fields.values():
                if not field.physical_table or not field.physical_column:
                    continue
                key = (field.physical_table, field.physical_column)
                if key not in physical:
                    raise CapabilityConfigError(
                        f"unknown_physical_field: {field.physical_table}.{field.physical_column}"
                    )
