from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...services import prompts
from ...services.llm import chat_json, role_model
from ..fields import FieldReachSet
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
) -> SubjectResolution:
    grounded = grounded or {}
    candidates: list[str] = []
    evidence: list[str] = []
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
