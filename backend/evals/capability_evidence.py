from __future__ import annotations

from typing import Any

from app.query.capability_registry import CapabilityRegistry
from app.query.fields import FieldReachSet


def _payload_subjects(payload: dict[str, Any]) -> tuple[str, ...]:
    subjects: set[str] = set()

    def add_node(node: Any) -> None:
        if not isinstance(node, dict):
            return
        subject = str(node.get("subject") or "").strip()
        if subject:
            subjects.add(subject)
        spec = node.get("spec")
        if isinstance(spec, dict):
            spec_subject = str(spec.get("subject") or "").strip()
            if spec_subject:
                subjects.add(spec_subject)

    for section in payload.get("sections") or ():
        add_node(section)
    plan = payload.get("plan")
    if isinstance(plan, dict):
        for node in plan.get("nodes") or ():
            add_node(node)
    return tuple(sorted(subjects))


def judge_capability_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose the same logical field facts used by planning to the semantic judge."""
    subjects = _payload_subjects(payload)
    if not subjects:
        return {}
    registry = CapabilityRegistry.load()
    facts: dict[str, Any] = {}
    for subject in subjects:
        try:
            view = registry.planner_view(subject, FieldReachSet()).to_prompt_dict()
        except (KeyError, ValueError):
            continue
        facts[subject] = {
            "base_table": view.get("base_table"),
            "fields": view.get("fields") or {},
            "metrics": view.get("metrics") or {},
            "derived_metrics": view.get("derived_metrics") or {},
            "allowed_views": view.get("allowed_views") or [],
        }
    return {
        "source": "unified_capability_registry",
        "subjects": facts,
    }
