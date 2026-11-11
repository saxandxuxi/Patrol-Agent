from __future__ import annotations

from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ...services import (
    capability_mdl_projection,
    schema_catalog,
    wren_engine,
)
from ..metrics.semantics import validate_metric_semantics
from .loader import CapabilityConfigError, load_capability_config
from .mdl_facts import inherited_cube_metrics, merge_metric_facts
from .models import (
    CapabilitySnapshot,
    DerivedMetricCapability,
    EntityCapability,
    EntityFamilyCapability,
    EnumCapability,
    EnumValueCapability,
    FieldCapability,
    MdlCompilationPolicy,
    MetricCapability,
    PlannerCapabilityView,
    PlannerFieldView,
    SubjectCapability,
    freeze_snapshot,
    thaw_snapshot,
)


def _upsert_metric(
    metrics: dict[str, MetricCapability],
    incoming: MetricCapability,
    *,
    subject: str,
    existing_source: str,
    incoming_source: str,
) -> dict[str, MetricCapability]:
    existing = metrics.get(incoming.metric_id)
    if existing is None:
        for metric in tuple(metrics.values()):
            if (
                incoming.metric_id in metric.aliases
                or metric.metric_id in incoming.aliases
            ):
                existing = metric
                break
    if existing is None:
        updated = dict(metrics)
        updated[incoming.metric_id] = incoming
        return updated
    merged = merge_metric_facts(
        existing,
        incoming,
        subject=subject,
        metric_id=incoming.metric_id,
        existing_source=existing_source,
        incoming_source=incoming_source,
    )
    updated = dict(metrics)
    if existing.metric_id != merged.metric_id:
        updated.pop(existing.metric_id, None)
    updated[merged.metric_id] = merged
    return updated


class CapabilityRegistry:
    def __init__(self, snapshot: CapabilitySnapshot, kind: str = "mysql"):
        self._snapshot = snapshot
        self._kind = str(kind or "mysql")

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
        mdl_compilation = _parse_mdl_compilation_policy(
            docs["mdl_compilation"],
        )
        subjects = {
            str(subject_id): SubjectCapability(
                subject_id=str(subject_id),
                base_table=str(raw.get("base_table") or ""),
                mdl_model=str(raw.get("mdl_model") or ""),
                allowed_views=tuple(str(item) for item in raw.get("allowed_views") or ()),
                entities=tuple(str(item) for item in raw.get("entities") or ()),
            )
            for subject_id, raw in docs["subjects"].items()
            if isinstance(raw, dict)
        }
        for subject_id, subject in subjects.items():
            if not subject.mdl_model:
                raise CapabilityConfigError(
                    f"missing_mdl_model: {subject_id}",
                )
        fields = {
            str(subject): {
                str(field_id): FieldCapability(
                    field_id=str(field_id),
                    expression=str(raw.get("expression") or ""),
                    label=str(raw.get("label") or field_id),
                    filter_expression=str(
                        raw.get("filter_expression") or ""
                    ),
                    roles=tuple(str(item) for item in raw.get("roles") or ()),
                    unit=str(raw.get("unit") or ""),
                    physical_table=str(raw.get("physical_table") or ""),
                    physical_column=str(raw.get("physical_column") or ""),
                    requires_join=tuple(str(item) for item in raw.get("requires_join") or ()),
                    enum_key=str(raw.get("enum_key") or ""),
                    status=str(raw.get("status") or "available"),
                    unavailable_reason=str(
                        raw.get("unavailable_reason") or ""
                    ),
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
                    propagate_across_groups=bool(
                        raw.get("propagate_across_groups", False)
                    ),
                    aliases=tuple(
                        str(item)
                        for item in raw.get("aliases") or ()
                        if str(item).strip()
                    ),
                    aggregation=str(raw.get("aggregation") or ""),
                    grain=str(raw.get("grain") or ""),
                    identity=str(raw.get("identity") or ""),
                    fanout_safe=raw.get("fanout_safe", False),
                    source_kind=str(
                        (raw.get("source") or {}).get("kind")
                        or "resources"
                    ),
                    source_reference=str(
                        (raw.get("source") or {}).get("reference")
                        or f"metrics.yaml:{subject}.{metric_id}"
                    ),
                )
                for metric_id, raw in raw_metrics.items()
                if isinstance(raw, dict) and not str(metric_id).startswith("_")
            }
            for subject, raw_metrics in docs["metrics"].items()
            if isinstance(raw_metrics, dict) and not str(subject).startswith("_")
        }
        metric_generation = docs["metrics"].get("_field_metric_generation") or {}
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
                display_column=str(raw.get("display_column") or ""),
                values=values,
            )
        for subject, subject_fields in fields.items():
            for field_id, field in tuple(subject_fields.items()):
                if not field.enum_key or "." not in field.enum_key:
                    continue
                table, column = field.enum_key.split(".", 1)
                enum = enums.get((table, column))
                if enum is None:
                    raise CapabilityConfigError(
                        f"unknown_enum_key: {field.enum_key}",
                    )
                qualifier = (
                    field.expression.split(".", 1)[0]
                    if "." in field.expression
                    else subjects[subject].mdl_model
                )
                subject_fields[field_id] = replace(
                    field,
                    label=enum.label,
                    expression=capability_mdl_projection.enum_case_expression(
                        table,
                        column,
                        qualifier=qualifier,
                    ),
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
            mdl_compilation=mdl_compilation,
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
            capability_gaps=dict(docs["capability_gaps"]),
            templates=dict(docs["templates"]),
            calculators=dict(docs["calculators"]),
        )
        registry = cls(snapshot, kind)
        registry._augment_mdl_subjects(kind)
        registry._augment_physical_fields(kind)
        registry._augment_baseline_metrics()
        field_metadata = (
            docs["templates"].get("fields")
            if isinstance(docs["templates"], dict)
            else {}
        ) or {}
        for subject, subject_fields in registry.snapshot.fields.items():
            for field_id, field in tuple(subject_fields.items()):
                metadata = field_metadata.get(f"{subject}.{field_id}")
                if not isinstance(metadata, Mapping):
                    continue
                subject_fields[field_id] = replace(
                    field,
                    status=str(metadata.get("status") or "available"),
                    unavailable_reason=str(metadata.get("reason") or ""),
                )
        registry._augment_numeric_metrics(metric_generation)
        registry._augment_mdl_cube_metrics(kind)
        registry._augment_enums(kind)
        registry._expand_metric_aliases()
        registry._validate_physical_fields(kind)
        registry._validate_metric_semantics()
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
                unit=field.unit,
                enum_values=tuple(enum.values.values()) if enum else (),
                status=field.status,
                unavailable_reason=field.unavailable_reason,
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
                "aliases": list(metric.aliases),
                "allowed_views": list(metric.allowed_views),
                "requires_join": list(metric.requires_join),
                "propagate_across_groups": metric.propagate_across_groups,
                "aggregation": metric.aggregation,
                "grain": metric.grain,
                "identity": metric.identity,
                "fanout_safe": metric.fanout_safe,
                "source_kind": metric.source_kind,
                "source_reference": metric.source_reference,
            }
            for metric_id in self.snapshot.metric_ids(subject)
            for metric in (self.snapshot.metrics[subject][metric_id],)
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
        calculators = {
            tool: {
                "input_shapes": list(raw.get("input_shapes") or ()),
                "source_selectors": list(
                    raw.get("source_selectors") or ()
                ),
                "operations": list(raw.get("operations") or ()),
                "precision": int(raw.get("precision") or 6),
                "zero_denominator": str(
                    raw.get("zero_denominator") or "error"
                ),
            }
            for tool, raw in self.snapshot.calculators.items()
            if isinstance(raw, Mapping)
        }
        business_values = dict(
            (
                self.snapshot.predicates.get("business_values")
                if isinstance(self.snapshot.predicates, Mapping)
                else {}
            )
            or {}
        )
        entities: dict[str, dict[str, Any]] = {}
        if subject_cap.entities:
            from .entity_resolver import list_entities

        for entity_id in subject_cap.entities:
            capability = self.snapshot.entities.get(entity_id)
            if capability is None:
                continue
            try:
                values = list_entities(self, entity_id, self._kind)
            except Exception:  # noqa: BLE001
                values = ()
            entities[entity_id] = {
                "identity": capability.identity,
                "values": [
                    {
                        "code": item.code,
                        "label": item.label,
                        # 别名和规范名相同的不用重复占上下文，只留真正的别名。
                        "aliases": [
                            alias
                            for alias in item.aliases
                            if alias and alias != item.label
                        ],
                    }
                    for item in values
                ],
            }
        capability_gaps = {
            gap_id: {
                "status": str(raw.get("status") or "unavailable"),
                "reason": str(raw.get("reason") or ""),
            }
            for gap_id, raw in self.snapshot.capability_gaps.items()
            if isinstance(raw, Mapping)
            and (
                not raw.get("applies_to")
                or subject in {
                    str(item)
                    for item in raw.get("applies_to") or ()
                }
            )
        }
        return PlannerCapabilityView(
            subject=subject,
            base_table=subject_cap.base_table,
            description=subject_cap.description,
            fields=fields,
            metrics=self.snapshot.metric_ids(subject),
            derived_metrics=tuple(self.snapshot.derived_metrics.get(subject, {})),
            allowed_views=subject_cap.allowed_views,
            reach_evidence=evidence,
            metric_details=metric_details,
            derived_metric_details=derived_metric_details,
            calculators=calculators,
            business_values=business_values,
            entities=entities,
            capability_gaps=capability_gaps,
            query_semantics=thaw_snapshot(
                self.snapshot.query_shapes.get("planner_semantics") or {}
            ),
        )

    def metric(
        self,
        subject: str,
        metric_id: str,
    ) -> MetricCapability | None:
        return self.snapshot.metric(subject, metric_id)

    def resolve_metric_id(self, subject: str, metric_id: str) -> str:
        return self.snapshot.resolve_metric_id(subject, metric_id)

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
                    expression=f"{subject.mdl_model}.{field_id}",
                    label=str(column.get("label") or field_id),
                    data_type=str(column.get("type") or ""),
                    comment=str(column.get("comment") or ""),
                    roles=tuple(str(item) for item in column.get("roles") or ()),
                    physical_table=subject.base_table,
                    physical_column=field_id,
                )
            self.snapshot.fields[subject_id] = subject_fields

    def _augment_mdl_subjects(self, kind: str) -> None:
        mdl = wren_engine._load_mdl(
            str(wren_engine._project_dir(kind))
        )
        raw_models = {
            str(model.get("name") or ""): model
            for model in mdl.get("models") or ()
            if isinstance(model, Mapping)
            and str(model.get("name") or "")
        }
        for subject_id, subject in tuple(
            self.snapshot.subjects.items()
        ):
            if subject.mdl_model in raw_models:
                continue
            self.snapshot.subjects.pop(subject_id, None)
            self.snapshot.fields.pop(subject_id, None)
            self.snapshot.metrics.pop(subject_id, None)
            self.snapshot.derived_metrics.pop(subject_id, None)
        subjects_by_model: dict[str, list[str]] = {}
        for subject_id, subject in self.snapshot.subjects.items():
            subjects_by_model.setdefault(subject.mdl_model, []).append(
                subject_id
            )
        mapped_models = set(subjects_by_model)
        tables_by_model = mdl.get("tables_by_model") or {}
        for model_name, model in raw_models.items():
            description = str(
                (model.get("properties") or {}).get("description")
                or ""
            ).strip()
            if model_name in mapped_models:
                subject_ids = subjects_by_model[model_name]
                for subject_id in subject_ids:
                    self.snapshot.subjects[subject_id] = replace(
                        self.snapshot.subjects[subject_id],
                        description=description,
                    )
            else:
                table_reference = model.get("table_reference")
                base_table = str(
                    (
                        table_reference.get("table")
                        if isinstance(table_reference, Mapping)
                        else ""
                    )
                    or tables_by_model.get(model_name)
                    or model_name
                )
                self.snapshot.subjects[model_name] = SubjectCapability(
                    subject_id=model_name,
                    base_table=base_table,
                    mdl_model=model_name,
                    description=description,
                    allowed_views=(
                        "count",
                        "sum",
                        "avg",
                        "distribution",
                        "list",
                        "detail",
                        "top_records",
                        "top_groups",
                    ),
                )
                subject_ids = [model_name]
            base_table = self.snapshot.subjects[subject_ids[0]].base_table
            for subject_id in subject_ids:
                subject_fields = dict(
                    self.snapshot.fields.get(subject_id) or {}
                )
                for column in model.get("columns") or ():
                    if not isinstance(column, Mapping):
                        continue
                    field_id = str(column.get("name") or "")
                    if not field_id or field_id in subject_fields:
                        continue
                    description_text = str(
                        (column.get("properties") or {}).get(
                            "description"
                        )
                        or ""
                    ).strip()
                    data_type = str(column.get("type") or "")
                    subject_fields[field_id] = FieldCapability(
                        field_id=field_id,
                        expression=f"{model_name}.{field_id}",
                        filter_expression=f"{model_name}.{field_id}",
                        label=field_id,
                        data_type=data_type,
                        comment=description_text,
                        roles=_mdl_field_roles(
                            field_id,
                            data_type,
                        ),
                        physical_table=base_table,
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
                    display_column="",
                    values={
                        str(code): EnumValueCapability(
                            code=str(code),
                            label=str(label),
                            display=str(label),
                        )
                        for code, label in code_map.items()
                    },
                )

    def _augment_baseline_metrics(self) -> None:
        for subject_id in self.snapshot.subjects:
            metrics = dict(self.snapshot.metrics.get(subject_id) or {})
            if "count" in metrics:
                continue
            baseline = MetricCapability(
                metric_id="count",
                expression="COUNT(*)",
                label="数量",
                allowed_views=(
                    "count",
                    "distribution",
                    "top_groups",
                    "trend",
                ),
                aggregation="count",
                grain=subject_id,
                identity=(
                    f"{self.snapshot.subjects[subject_id].mdl_model}.id"
                ),
                fanout_safe=False,
                source_kind="resources",
                source_reference=f"baseline:count:{subject_id}",
            )
            metrics = _upsert_metric(
                metrics,
                baseline,
                subject=subject_id,
                existing_source="resources",
                incoming_source="baseline",
            )
            self.snapshot.metrics[subject_id] = metrics

    def _augment_mdl_cube_metrics(self, kind: str) -> None:
        mdl = wren_engine._load_mdl(
            str(wren_engine._project_dir(kind))
        )
        subject_by_model = {
            subject.mdl_model: subject_id
            for subject_id, subject in self.snapshot.subjects.items()
        }
        inherited = inherited_cube_metrics(mdl, subject_by_model)
        for subject_id, cube_metrics in inherited.items():
            metrics = dict(self.snapshot.metrics.get(subject_id) or {})
            for metric_id, metric in cube_metrics.items():
                metrics = _upsert_metric(
                    metrics,
                    metric,
                    subject=subject_id,
                    existing_source="resources",
                    incoming_source="mdl",
                )
            self.snapshot.metrics[subject_id] = metrics

    def _augment_numeric_metrics(
        self,
        generation_rules: Mapping[str, Any],
    ) -> None:
        for subject_id, fields in self.snapshot.fields.items():
            metrics = dict(self.snapshot.metrics.get(subject_id) or {})
            subject_rules = generation_rules.get(subject_id) or {}
            if not isinstance(subject_rules, Mapping):
                raise CapabilityConfigError(
                    f"invalid_metric_generation: {subject_id}"
                )
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
                rule = subject_rules.get(field_id) or {}
                if not isinstance(rule, Mapping):
                    raise CapabilityConfigError(
                        f"invalid_metric_generation: {subject_id}.{field_id}"
                    )
                divisor = str(rule.get("divisor") or "").strip()
                if divisor:
                    try:
                        valid_divisor = float(divisor) > 0
                    except ValueError:
                        valid_divisor = False
                    if not valid_divisor:
                        raise CapabilityConfigError(
                            "invalid_metric_generation_divisor: "
                            f"{subject_id}.{field_id}"
                        )
                output_unit = str(rule.get("unit") or field.unit)
                for prefix, aggregate in (
                    ("sum_", "SUM"),
                    ("avg_", "AVG"),
                    ("min_", "MIN"),
                    ("max_", "MAX"),
                ):
                    metric_id = f"{prefix}{field_id}"
                    expression = (
                        f"{aggregate}({{alias}}.{field.physical_column})"
                    )
                    if divisor:
                        expression = f"{expression} / {divisor}"
                    aggregate_views = {
                        "SUM": "sum",
                        "AVG": "avg",
                    }
                    allowed_views = tuple(
                        item
                        for item in (
                            "count",
                            aggregate_views.get(aggregate),
                            "distribution",
                            "top_groups",
                            "trend",
                        )
                        if item
                    )
                    generated = MetricCapability(
                        metric_id=metric_id,
                        expression=expression,
                        label=f"{field.label}{'合计' if prefix == 'sum_' else '平均' if prefix == 'avg_' else '最小' if prefix == 'min_' else '最大'}",
                        unit=output_unit,
                        allowed_views=allowed_views,
                        aggregation={
                            "SUM": "sum",
                            "AVG": "avg",
                            "MIN": "min",
                            "MAX": "max",
                        }[aggregate],
                        grain=subject_id,
                        identity=(
                            f"{self.snapshot.subjects[subject_id].mdl_model}.id"
                        ),
                        fanout_safe=False,
                        source_kind="resources",
                        source_reference=(
                            "_field_metric_generation:"
                            f"{subject_id}.{field_id}"
                        ),
                    )
                    metrics = _upsert_metric(
                        metrics,
                        generated,
                        subject=subject_id,
                        existing_source="resources",
                        incoming_source="generated_field",
                    )
            self.snapshot.metrics[subject_id] = metrics

    def _expand_metric_aliases(self) -> None:
        for subject_id, raw_metrics in tuple(
            self.snapshot.metrics.items()
        ):
            canonical_metrics: dict[str, MetricCapability] = {}
            for metric_id, metric in raw_metrics.items():
                if metric_id != metric.metric_id:
                    raise CapabilityConfigError(
                        "canonical_metric_key_mismatch: "
                        f"{subject_id}.{metric_id} -> {metric.metric_id}",
                    )
                canonical_metrics = _upsert_metric(
                    canonical_metrics,
                    metric,
                    subject=subject_id,
                    existing_source="resources",
                    incoming_source="resources",
                )
            aliases: dict[str, str] = {}
            for metric_id, metric in canonical_metrics.items():
                for alias in metric.aliases:
                    if not alias or alias == metric_id:
                        continue
                    existing = aliases.get(alias)
                    if existing and existing != metric_id:
                        raise CapabilityConfigError(
                            "duplicate_metric_alias: "
                            f"{subject_id}.{alias}",
                            evidence={
                                "code": "duplicate_metric_alias",
                                "subject": subject_id,
                                "alias": alias,
                                "owners": [existing, metric_id],
                            },
                        )
                    aliases[alias] = metric_id
            collisions = sorted(set(aliases) & set(canonical_metrics))
            if collisions:
                raise CapabilityConfigError(
                    "metric_alias_collision: "
                    + ", ".join(collisions),
                )
            expanded = dict(canonical_metrics)
            for alias, canonical_id in aliases.items():
                expanded[alias] = canonical_metrics[canonical_id]
            self.snapshot.metrics[subject_id] = expanded

    def _validate_metric_semantics(self) -> None:
        for subject_id, metrics in self.snapshot.metrics.items():
            for metric_id, metric in metrics.items():
                if metric_id != metric.metric_id:
                    continue
                issues = validate_metric_semantics(
                    metric,
                    subject=subject_id,
                )
                if not issues:
                    continue
                raise CapabilityConfigError(
                    f"invalid_metric_semantics: {subject_id}.{metric_id}",
                    evidence={
                        "subject": subject_id,
                        "metric_id": metric_id,
                        "issues": [
                            {
                                "code": issue.code,
                                "message": issue.message,
                                "evidence": dict(issue.evidence),
                            }
                            for issue in issues
                        ],
                    },
                )

    def _validate_physical_fields(self, kind: str) -> None:
        catalog = schema_catalog.field_catalog(kind)
        physical = {
            (str(table.get("table") or ""), str(column.get("name") or ""))
            for table in catalog.get("tables") or []
            for column in table.get("columns") or []
        }
        mdl = wren_engine._load_mdl(
            str(wren_engine._project_dir(kind))
        )
        tables_by_model = mdl.get("tables_by_model") or {}
        for model in mdl.get("models") or ():
            if not isinstance(model, Mapping):
                continue
            model_name = str(model.get("name") or "")
            table_reference = model.get("table_reference")
            table = str(
                (
                    table_reference.get("table")
                    if isinstance(table_reference, Mapping)
                    else ""
                )
                or tables_by_model.get(model_name)
                or model_name
            )
            physical.update(
                (table, str(column.get("name") or ""))
                for column in model.get("columns") or ()
                if isinstance(column, Mapping)
                and str(column.get("name") or "")
            )
        for subject_fields in self.snapshot.fields.values():
            for field in subject_fields.values():
                if not field.physical_table or not field.physical_column:
                    continue
                key = (field.physical_table, field.physical_column)
                if key not in physical:
                    raise CapabilityConfigError(
                        f"unknown_physical_field: {field.physical_table}.{field.physical_column}"
                    )


def _mdl_field_roles(field_name: str, data_type: str) -> tuple[str, ...]:
    roles: list[str] = ["output"]
    lowered_name = field_name.lower()
    lowered_type = data_type.lower()
    if field_name == "id":
        roles.extend(("primary", "identity", "dimension"))
    elif lowered_name.endswith("_id"):
        roles.extend(("fk", "dimension"))
    if field_name == "del_flag":
        roles.append("soft_delete")
    if "time" in lowered_name or "date" in lowered_type:
        roles.extend(("time", "dimension"))
    is_text = any(
        token in lowered_type
        for token in (
            "char",
            "text",
            "string",
            "enum",
            "bool",
            "date",
            "time",
            "json",
        )
    )
    if is_text:
        roles.append("dimension")
    if lowered_name.endswith("code") or lowered_name.endswith("number"):
        roles.extend(("code", "dimension"))
    if any(
        token in lowered_type
        for token in ("int", "decimal", "double", "float", "numeric", "number")
    ):
        roles.append("numeric")
    return tuple(dict.fromkeys(roles))


_MDL_COMPILATION_KEYS = frozenset(
    {
        "schema_version",
        "max_context_bytes",
        "max_models_per_contract",
        "max_relationship_hops",
        "llm_fallback_attempts",
        "forbidden_sql_types",
        "forbidden_functions",
        "require_soft_delete",
    }
)
_FORBIDDEN_SQL_TYPES = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "CREATE",
        "ALTER",
        "DROP",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "CALL",
        "EXEC",
    }
)


def _parse_mdl_compilation_policy(raw: Any) -> MdlCompilationPolicy:
    unknown = set(raw) - _MDL_COMPILATION_KEYS
    if unknown:
        raise CapabilityConfigError(
            "unknown_mdl_compilation_key: "
            + ", ".join(sorted(str(item) for item in unknown))
        )

    schema_version = _positive_policy_int(raw, "schema_version")
    if schema_version != 1:
        raise CapabilityConfigError(
            f"unsupported_mdl_compilation_schema: {schema_version}"
        )
    llm_fallback_attempts = _positive_policy_int(
        raw,
        "llm_fallback_attempts",
    )
    if llm_fallback_attempts != 1:
        raise CapabilityConfigError(
            "invalid_mdl_compilation: llm_fallback_attempts must be 1"
        )

    forbidden_sql_types = raw.get("forbidden_sql_types")
    if (
        not isinstance(forbidden_sql_types, (list, tuple))
        or not forbidden_sql_types
        or any(
            not isinstance(item, str)
            or item not in _FORBIDDEN_SQL_TYPES
            for item in forbidden_sql_types
        )
    ):
        raise CapabilityConfigError(
            "invalid_mdl_compilation: forbidden_sql_types"
        )

    forbidden_functions = raw.get("forbidden_functions")
    if (
        not isinstance(forbidden_functions, (list, tuple))
        or any(
            not isinstance(item, str) or not item.strip()
            for item in forbidden_functions
        )
    ):
        raise CapabilityConfigError(
            "invalid_mdl_compilation: forbidden_functions"
        )

    require_soft_delete = raw.get("require_soft_delete")
    if not isinstance(require_soft_delete, bool) or not require_soft_delete:
        raise CapabilityConfigError(
            "invalid_mdl_compilation: require_soft_delete must be true"
        )

    return MdlCompilationPolicy(
        schema_version=schema_version,
        max_context_bytes=_positive_policy_int(raw, "max_context_bytes"),
        max_models_per_contract=_positive_policy_int(
            raw,
            "max_models_per_contract",
        ),
        max_relationship_hops=_positive_policy_int(
            raw,
            "max_relationship_hops",
        ),
        llm_fallback_attempts=llm_fallback_attempts,
        forbidden_sql_types=tuple(forbidden_sql_types),
        forbidden_functions=tuple(forbidden_functions),
        require_soft_delete=require_soft_delete,
    )


def _positive_policy_int(raw: Mapping[str, Any], key: str) -> int:
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CapabilityConfigError(
            f"invalid_mdl_compilation: {key} must be a positive integer"
        )
    return value
