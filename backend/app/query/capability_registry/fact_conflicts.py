from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from .mdl_facts import _expression_identity, _unit_identity
from .models import CapabilitySnapshot

if TYPE_CHECKING:
    from ..mdl_sql.models import MdlSnapshot


@dataclass(frozen=True)
class FactConflict:
    code: str
    subject: str
    fact_id: str
    sources: tuple[str, ...]
    expected: str
    actual: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "subject": self.subject,
            "fact_id": self.fact_id,
            "sources": list(self.sources),
            "expected": self.expected,
            "actual": self.actual,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class FactConflictReport:
    conflicts: tuple[FactConflict, ...]
    accepted: bool


def validate_fact_conflicts(
    snapshot: CapabilitySnapshot,
    mdl: Any,
) -> FactConflictReport:
    conflicts: list[FactConflict] = []
    conflicts.extend(_enum_conflicts(snapshot))
    conflicts.extend(_table_label_conflicts(snapshot, mdl))
    conflicts.extend(_subject_and_field_conflicts(snapshot, mdl))
    conflicts.extend(_metric_conflicts(snapshot, mdl))
    return FactConflictReport(
        conflicts=tuple(conflicts),
        accepted=not conflicts,
    )


def _enum_conflicts(snapshot: CapabilitySnapshot) -> list[FactConflict]:
    conflicts: list[FactConflict] = []
    for (table, column), enum in snapshot.enums.items():
        fact_id = f"{table}.{column}"
        if enum.key != fact_id:
            conflicts.append(
                _conflict(
                    "enum_key_mismatch",
                    subject="",
                    fact_id=fact_id,
                    sources=("enums.yaml",),
                    expected=fact_id,
                    actual=enum.key,
                )
            )
        for code, value in enum.values.items():
            if not value.label or not value.display:
                conflicts.append(
                    _conflict(
                        "enum_display_missing",
                        subject="",
                        fact_id=fact_id,
                        sources=("enums.yaml",),
                        expected="每个 code 都有 label 和 display",
                        actual=f"{code}: label={value.label!r}, display={value.display!r}",
                    )
                )
    for subject, fields in snapshot.fields.items():
        for field_id, capability in fields.items():
            if not capability.enum_key:
                continue
            enum = snapshot.enums.get(
                tuple(capability.enum_key.split(".", 1))
            )
            if enum is None:
                conflicts.append(
                    _conflict(
                        "enum_key_missing",
                        subject=subject,
                        fact_id=field_id,
                        sources=("fields.yaml", "enums.yaml"),
                        expected=capability.enum_key,
                        actual="missing",
                    )
                )
                continue
            if capability.label != enum.label:
                conflicts.append(
                    _conflict(
                        "enum_label_mismatch",
                        subject=subject,
                        fact_id=field_id,
                        sources=("fields.yaml", "enums.yaml"),
                        expected=enum.label,
                        actual=capability.label,
                    )
                )
    return conflicts


def _table_label_conflicts(
    snapshot: CapabilitySnapshot,
    mdl: Any,
) -> list[FactConflict]:
    conflicts: list[FactConflict] = []
    for table, label in snapshot.table_labels.items():
        model_name = mdl.physical_table_to_model.get(table)
        model = mdl.models.get(model_name or "")
        if model is None or not label:
            continue
        description = " ".join(model.description.split())
        if description == label or description.startswith(label + " "):
            conflicts.append(
                _conflict(
                    "table_label_duplicated_in_mdl",
                    subject="",
                    fact_id=table,
                    sources=("field_overrides.yaml", "wren_mdl"),
                    expected="MDL description 只写业务约束，不重复表中文名",
                    actual=description,
                    evidence={"label": label, "model": model.name},
                )
            )
    return conflicts


def _subject_and_field_conflicts(
    snapshot: CapabilitySnapshot,
    mdl: Any,
) -> list[FactConflict]:
    conflicts: list[FactConflict] = []
    for subject_id, subject in snapshot.subjects.items():
        model = mdl.models.get(subject.mdl_model)
        if model is None:
            conflicts.append(
                _conflict(
                    "missing_mdl_model",
                    subject=subject_id,
                    fact_id=subject.mdl_model,
                    sources=("subjects.yaml", "wren_mdl"),
                    expected=subject.mdl_model,
                    actual="missing",
                )
            )
            continue
        for field_id, field in snapshot.fields.get(subject_id, {}).items():
            if field.physical_table and field.physical_column:
                field_model_name = mdl.physical_table_to_model.get(
                    field.physical_table
                )
                field_model = mdl.models.get(field_model_name or "")
                if (
                    field_model is None
                    or field.physical_column not in field_model.columns
                ):
                    conflicts.append(
                        _conflict(
                            "missing_mdl_column",
                            subject=subject_id,
                            fact_id=field_id,
                            sources=("fields.yaml", "wren_mdl"),
                            expected=(
                                f"{field.physical_table}."
                                f"{field.physical_column}"
                            ),
                            actual="missing",
                        )
                    )
            for join in field.requires_join:
                if not _join_exists(model.name, join, mdl):
                    conflicts.append(
                        _conflict(
                            "missing_join",
                            subject=subject_id,
                            fact_id=field_id,
                            sources=("fields.yaml", "wren_mdl"),
                            expected=join,
                            actual="missing",
                        )
                    )
    return conflicts


def _metric_conflicts(
    snapshot: CapabilitySnapshot,
    mdl: Any,
) -> list[FactConflict]:
    conflicts: list[FactConflict] = []
    subject_by_model = {
        subject.mdl_model: subject_id
        for subject_id, subject in snapshot.subjects.items()
    }
    cube_measures: dict[str, tuple[str, str]] = {}
    measures_by_subject: dict[str, list[tuple[str, str]]] = {}
    for cube in mdl.cubes.values():
        subject = subject_by_model.get(cube.base_object)
        if not subject:
            continue
        for measure in cube.measures:
            reference = f"{cube.name}.{measure.name}"
            cube_measures[reference] = (subject, measure.expression)
            measures_by_subject.setdefault(subject, []).append(
                (reference, measure.expression)
            )

    for subject, metrics in snapshot.metrics.items():
        canonical = [
            metric
            for metric_id, metric in metrics.items()
            if metric_id == metric.metric_id
        ]
        by_formula: dict[str, str] = {}
        for metric in canonical:
            formula = _expression_identity(metric.expression)
            if formula and formula in by_formula:
                conflicts.append(
                    _conflict(
                        "duplicate_metric_formula_owner",
                        subject=subject,
                        fact_id=metric.metric_id,
                        sources=("metrics.yaml", "metrics.yaml"),
                        expected=by_formula[formula],
                        actual=metric.metric_id,
                        evidence={"formula": formula},
                    )
                )
            elif formula:
                by_formula[formula] = metric.metric_id

            if metric.source_kind == "mdl_cube":
                declared = cube_measures.get(metric.source_reference)
                if declared is None or declared[0] != subject:
                    conflicts.append(
                        _conflict(
                            "missing_mdl_measure",
                            subject=subject,
                            fact_id=metric.metric_id,
                            sources=("metrics.yaml", "wren_mdl"),
                            expected=metric.source_reference,
                            actual="missing",
                        )
                    )
                    continue
                measure_expression = declared[1]
                if (
                    formula
                    and formula
                    != _expression_identity(measure_expression)
                ):
                    conflicts.append(
                        _conflict(
                            "conflicting_metric_fact",
                            subject=subject,
                            fact_id=metric.metric_id,
                            sources=("metrics.yaml", "wren_mdl"),
                            expected=measure_expression,
                            actual=metric.expression,
                        )
                    )
                continue

            duplicate = next(
                (
                    reference
                    for reference, expression in measures_by_subject.get(
                        subject,
                        (),
                    )
                    if formula
                    and formula == _expression_identity(expression)
                ),
                "",
            )
            if duplicate:
                conflicts.append(
                    _conflict(
                        "duplicate_metric_formula_owner",
                        subject=subject,
                        fact_id=metric.metric_id,
                        sources=("metrics.yaml", "wren_mdl"),
                        expected=duplicate,
                        actual=metric.metric_id,
                        evidence={"formula": formula},
                    )
                )
    return conflicts


def _join_exists(model: str, reference: str, mdl: Any) -> bool:
    if reference in mdl.relationships:
        return True
    for relationship in mdl.relationships.values():
        if reference in relationship.models and model in relationship.models:
            return True
    return False


def _conflict(
    code: str,
    *,
    subject: str,
    fact_id: str,
    sources: tuple[str, ...],
    expected: str,
    actual: str,
    evidence: dict[str, Any] | None = None,
) -> FactConflict:
    return FactConflict(
        code=code,
        subject=subject,
        fact_id=fact_id,
        sources=sources,
        expected=expected,
        actual=actual,
        evidence=dict(evidence or {}),
    )
