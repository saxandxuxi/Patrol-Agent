from __future__ import annotations

from typing import Any

from .capability_registry import CapabilityRegistry
from .contracts import ExecutionObservation
from .presentation.aggregates import zero_aggregate_observation
from .scope_facts import OUTSIDE, ScopeFacts


def aggregate_result_is_empty(
    spec: Any,
    observation: ExecutionObservation,
) -> bool:
    """Return whether a scalar aggregate represents an empty fact."""

    view = str(getattr(spec, "view", "") or "")
    if view not in {"count", "sum", "avg"}:
        return False
    rows = observation.result_rows or observation.preview_rows
    if len(rows) != 1:
        return False
    if not isinstance(rows[0], dict):
        return False
    values = list(rows[0].values())
    if not values:
        return False
    zero_is_empty = view == "count"
    for value in values:
        text = "" if value is None else str(value).strip().upper()
        if value is None or text in {"", "NULL", "NONE"}:
            continue
        if not zero_is_empty:
            return False
        try:
            if float(value) != 0.0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def is_outside_scope_empty_answerable(spec: Any, facts: ScopeFacts) -> bool:
    """Known ledger coverage makes an empty fact distinguishable from missing data."""

    return (
        str(getattr(spec, "view", "") or "")
        in {"count", "distribution"}
        and facts.verdict == OUTSIDE
    )


def zero_count_observation(
    spec: Any,
    observation: ExecutionObservation,
    registry: CapabilityRegistry | None,
) -> ExecutionObservation:
    return zero_aggregate_observation(spec, observation, registry)


def outside_scope_empty_answer(
    view: str,
    base_answer: str,
    facts: ScopeFacts,
) -> str:
    detail = str(facts.detail or "").strip().rstrip("。")
    caveat = (
        "当前结果仅代表现有台账在该查询条件下的记录，"
        "不能据此推断该范围历史上绝对没有病害。"
    )
    if str(view or "") == "distribution":
        prefix = "当前台账在该查询范围内没有可返回的分组记录"
        if detail:
            return f"{prefix}；{detail}。{caveat}"
        return f"{prefix}。{caveat}"
    if detail:
        return f"{base_answer}；{detail}。{caveat}"
    return f"{base_answer}；{caveat}"
