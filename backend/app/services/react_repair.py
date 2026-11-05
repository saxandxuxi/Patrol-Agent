from __future__ import annotations

import json
from typing import Any

from . import resource_loader


GLOBAL_REPAIR_LIMIT = 2
_DEFAULT = {
    "candidates": ["ask_user"],
    "max_repair_rounds": 0,
    "retryable": False,
}


def _entry(failure_code: str) -> dict[str, Any]:
    policies = resource_loader.load_yaml("repair_policy.yaml", {})
    value = policies.get(failure_code) if isinstance(policies, dict) else None
    if not isinstance(value, dict):
        return _DEFAULT
    candidates = value.get("candidates")
    rounds = value.get("max_repair_rounds")
    retryable = value.get("retryable")
    if (
        not isinstance(candidates, list)
        or not all(isinstance(item, str) and item for item in candidates)
        or not isinstance(rounds, int)
        or rounds < 0
        or not isinstance(retryable, bool)
    ):
        return _DEFAULT
    return {
        "candidates": list(candidates),
        "max_repair_rounds": rounds,
        "retryable": retryable,
    }


def candidate_tool_names(failure_code: str) -> list[str]:
    return list(_entry(str(failure_code or "tool_error"))["candidates"])


def build_failure(
    failure_code: str,
    *,
    stage: str,
    reason: str,
    evidence: dict[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    code = str(failure_code or "tool_error")
    policy = _entry(code)
    if policy is _DEFAULT:
        code = "tool_error"
        policy = _entry(code)
    safe_evidence = json.loads(
        json.dumps(evidence or {}, ensure_ascii=False, default=str)
    )
    return {
        "accepted": False,
        "failure_code": code,
        "stage": str(stage or "tool"),
        "retryable": policy["retryable"],
        "reason": str(reason or "工具执行失败"),
        "evidence": safe_evidence,
        "candidate_tools": list(policy["candidates"]),
        "attempt": max(1, int(attempt or 1)),
        "max_repair_rounds": policy["max_repair_rounds"],
    }


def format_repair_feedback(failure: dict[str, Any]) -> str:
    evidence = failure.get("evidence") or {}
    rows = list(evidence.get("rows") or [])[:5]
    columns = list(evidence.get("columns") or [])
    lines = [
        f"失败类型：{failure.get('failure_code') or 'tool_error'}",
        f"失败原因：{failure.get('reason') or '未知错误'}",
    ]
    if evidence.get("sql"):
        lines.append(f"上次 SQL：{evidence['sql']}")
    if columns:
        lines.append(f"返回字段：{json.dumps(columns, ensure_ascii=False)}")
    if rows:
        lines.append(
            "结果样本："
            + json.dumps(rows, ensure_ascii=False, default=str)
        )
    return "\n".join(lines)
