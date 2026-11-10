from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...services import prompts
from ...services.llm import chat_json, role_model
from ..fields import FieldReachSet
from .entity_resolver import list_entities
from .registry import CapabilityRegistry


@dataclass(frozen=True)
class SubjectResolution:
    subject: str
    confidence: float
    alternatives: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


def resolve_subject(
    question: str,
    intent: str,
    reach: FieldReachSet,
    registry: CapabilityRegistry,
    grounded: dict[str, Any] | None = None,
    kind: str = "mysql",
    conflicting_subject: str = "",
    previous_resolution: SubjectResolution | None = None,
) -> SubjectResolution:
    grounded = grounded or {}
    candidates: list[str] = []
    evidence: list[str] = []
    entity_candidates: set[str] = set()
    raw_subject = str(grounded.get("subject") or "").strip()
    if raw_subject in registry.snapshot.subjects:
        candidates.append(raw_subject)
        evidence.append(f"field_grounding.subject={raw_subject}")
    table = str(grounded.get("table") or "").strip()
    if table:
        for subject_id, subject in registry.snapshot.subjects.items():
            if subject.base_table == table and subject_id not in candidates:
                candidates.append(subject_id)
                evidence.append(f"field_grounding.table={table}")
    conflicting = str(conflicting_subject or "").strip()
    if conflicting in registry.snapshot.subjects:
        if conflicting not in candidates:
            candidates.append(conflicting)
        evidence.append(f"planner_final_subject={conflicting}")
    if previous_resolution is not None:
        if previous_resolution.subject:
            evidence.append(
                f"previous_subject={previous_resolution.subject}"
            )
        evidence.extend(
            f"previous_evidence={item}"
            for item in previous_resolution.evidence
        )
    # 已注册实体值是“用户明确提到的业务对象”，但它们常常同时出现在多张表的
    # 注释里（例如路线名出现在路段台账、病害类型出现在病害表）。表级检索因此
    # 只能提供候选，不能决定主体；这里把字典实体的精确命中补成主体候选证据。
    for subject_id, subject in registry.snapshot.subjects.items():
        for entity_id in subject.entities:
            try:
                values = list_entities(registry, entity_id, kind)
            except Exception:
                values = ()
            matched = next(
                (
                    name
                    for value in values
                    for name in (value.label, *value.aliases)
                    if len(name) >= 2 and name in question
                ),
                "",
            )
            if not matched:
                continue
            if subject_id not in candidates:
                candidates.append(subject_id)
            entity_candidates.add(subject_id)
            evidence.append(f"entity={entity_id}:{matched}")
        for family in registry.snapshot.entity_families.values():
            if family.entity not in subject.entities:
                continue
            matched = next(
                (
                    name
                    for name in (family.label, *family.aliases)
                    if len(name) >= 2 and name in question
                ),
                "",
            )
            if not matched:
                continue
            if subject_id not in candidates:
                candidates.append(subject_id)
            entity_candidates.add(subject_id)
            evidence.append(f"entity_family={family.family_id}:{matched}")
    for hit in reach.hits:
        for subject_id, subject in registry.snapshot.subjects.items():
            if subject.base_table == hit.table and subject_id not in candidates:
                candidates.append(subject_id)
                evidence.append(f"reach={hit.table}.{hit.column}")
    for subject_id, subject in registry.snapshot.subjects.items():
        if subject_id in candidates:
            continue
        matching_fields = [
            field
            for field in registry.snapshot.fields.get(subject_id, {}).values()
            if field.label and len(field.label) >= 2 and field.label in question
        ]
        if matching_fields:
            candidates.append(subject_id)
            evidence.append(f"subject_field={subject_id}")
    # search_models 的表级结果只负责召回。登记实体是强证据，存在时不再引入表级
    # 噪声；其余情况下都要把召回表一并交给语义路由器。字段触达可能因为列注释里
    # 同时出现“巡检、病害”等泛化词而落错表，不能让这个软落点直接短路主体判断。
    if not entity_candidates:
        for table_item in grounded.get("tables") or []:
            table_name = str(
                table_item.get("proxy_table")
                or table_item.get("name")
                or table_item.get("table")
                or ""
            ).strip()
            if not table_name:
                continue
            for subject_id, subject in registry.snapshot.subjects.items():
                if subject.base_table != table_name:
                    continue
                if subject_id not in candidates:
                    candidates.append(subject_id)
                evidence.append(
                    f"grounding_table={table_name}:{table_item.get('score', 0)}"
                )
                evidence.append(
                    f"recall_table={table_name}:"
                    f"score={table_item.get('score', 0)}:"
                    f"table_score={table_item.get('table_score', 0)}"
                )
    if len(candidates) == 1:
        return SubjectResolution(
            subject=candidates[0],
            confidence=0.9,
            evidence=tuple(evidence),
        )
    if len(candidates) > 1:
        summaries = {
            subject_id: {
                "base_table": registry.snapshot.subjects[subject_id].base_table,
                "allowed_views": list(
                    registry.snapshot.subjects[subject_id].allowed_views
                ),
                "field_examples": [
                    field.label
                    for field in list(
                        registry.snapshot.fields.get(subject_id, {}).values()
                    )[:12]
                ],
            }
            for subject_id in candidates
        }
        try:
            routed = chat_json(
                prompts.messages(
                    "subject_router",
                    question=question,
                    subjects=summaries,
                    grounding_evidence=evidence,
                ),
                model=role_model("semantic"),
            )
        except Exception:
            routed = {}
        selected = str((routed or {}).get("subject") or "").strip()
        if selected in candidates:
            return SubjectResolution(
                subject=selected,
                confidence=float((routed or {}).get("confidence") or 0.7),
                evidence=tuple(evidence)
                + tuple(
                    str(item)
                    for item in (routed or {}).get("evidence") or ()
                ),
            )
        return SubjectResolution(
            subject="",
            confidence=float((routed or {}).get("confidence") or 0.5),
            alternatives=tuple(
                str(item)
                for item in (routed or {}).get("alternatives") or candidates
            ),
            evidence=tuple(evidence),
        )
    if len(registry.snapshot.subjects) == 1:
        subject_id = next(iter(registry.snapshot.subjects))
        return SubjectResolution(
            subject=subject_id,
            confidence=0.3,
            evidence=("only_registered_subject",),
        )
    return SubjectResolution(subject="", confidence=0.0)
