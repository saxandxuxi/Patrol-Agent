from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    if len(candidates) == 1:
        return SubjectResolution(
            subject=candidates[0],
            confidence=0.9,
            evidence=tuple(evidence),
        )
    if len(candidates) > 1:
        return SubjectResolution(
            subject="",
            confidence=0.5,
            alternatives=tuple(candidates),
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
