from __future__ import annotations

from typing import Any, Iterable


_EXECUTION_FAILURE_STATUSES = frozenset({"error", "failed", "repairable"})


def execution_failure_reasons(sections: Iterable[dict[str, Any]]) -> list[str]:
    """Return only failures caused by compilation, execution, or verification.

    A downstream node blocked by a declared capability limitation is part of a
    supported "cannot answer" result. The same node blocked by a real failure
    must still surface that root failure.
    """

    by_id = {
        str(section.get("node_id") or ""): section
        for section in sections
        if isinstance(section, dict) and section.get("node_id")
    }
    reasons: list[str] = []
    for section in by_id.values():
        if not _has_execution_failure(section, by_id, set()):
            continue
        if str(section.get("status") or "") == "blocked":
            continue
        reason = str(
            section.get("answer") or section.get("error") or ""
        ).strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    return reasons


def _has_execution_failure(
    section: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    seen: set[str],
) -> bool:
    node_id = str(section.get("node_id") or "")
    if node_id and node_id in seen:
        return False
    if node_id:
        seen.add(node_id)
    status = str(section.get("status") or "accepted")
    if status in _EXECUTION_FAILURE_STATUSES:
        return True
    if status != "blocked":
        return False
    dependencies = [
        str(item) for item in section.get("depends_on") or ()
    ]
    if not dependencies:
        return True
    for dependency in dependencies:
        upstream = by_id.get(dependency)
        if upstream is None or _has_execution_failure(upstream, by_id, seen):
            return True
    return False
