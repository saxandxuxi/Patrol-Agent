from __future__ import annotations

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
    SubjectCapability,
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
                if isinstance(raw, dict)
            }
            for subject, raw_metrics in docs["metrics"].items()
            if isinstance(raw_metrics, dict)
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
        )
        registry = cls(snapshot)
        registry._validate_physical_fields(kind)
        return registry

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
