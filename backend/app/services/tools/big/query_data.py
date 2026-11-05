from __future__ import annotations

import re
from typing import Any

from ....store import save_result
from ... import nl2sql
from ...react_repair import build_failure
from ..observation import normalize_week_label, observation
from ..registry import tool


def _verification_failure(
    question: str, payload: dict[str, Any]
) -> dict[str, Any] | None:
    sql = str(payload.get("sql") or "")
    if not sql:
        return None
    from ... import agent

    if str(payload.get("engine") or "") in agent._DETERMINISTIC_ENGINES:
        return None
    if not list(payload.get("rows") or []):
        return build_failure(
            "empty_unexpected",
            stage="verify",
            reason="查询执行成功但没有返回任何数据",
            evidence={
                "step_question": question,
                "sql": sql,
                "columns": list(payload.get("columns") or []),
                "rows": [],
            },
        )
    rows = list(payload.get("rows") or [])
    reply = str(payload.get("reply") or "")
    if str(payload.get("shape") or "") in {"distribution", "trend"} and len(rows) <= 10:
        numeric_values = {
            str(value).strip()
            for row in rows
            if isinstance(row, dict)
            for value in row.values()
            if re.fullmatch(r"-?\d+(?:\.\d+)?", str(value).strip())
        }
        if numeric_values and all(value in reply for value in numeric_values):
            return None
    verdict = nl2sql.verify_answer(
        question,
        sql,
        payload,
        str(payload.get("reply") or ""),
    )
    if verdict.get("correct", True):
        return None
    return build_failure(
        "sql_semantic_mismatch",
        stage="verify",
        reason=str(verdict.get("reason") or "结果没有回答当前子问题"),
        evidence={
            "step_question": question,
            "sql": sql,
            "columns": list(payload.get("columns") or []),
            "rows": list(payload.get("rows") or [])[:10],
            "verdict": verdict,
        },
    )


@tool(
    name="query_data",
    layer="big",
    domain="data",
    description="按一句自然语言子问题查询业务数据，内部复用现有模板、Wren、SQL 校验和执行链路。需要精确换算时，先让本工具完成；只有观察异常才调用原子工具纠偏。",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "intent": {"type": "string", "default": "data"},
            "kind": {"type": ["string", "null"]},
            "plan": {"type": ["object", "null"]},
            "values": {"type": "object", "description": "占位符需要回填的键值"},
            "wren_first": {"type": "boolean", "default": False},
            "want_images": {"type": "boolean", "default": False},
            "produces": {"type": "array", "items": {"type": "object"}},
            "repair_feedback": {"type": ["object", "null"]},
        },
        "required": ["question"],
    },
    wraps="agent._build_query_result:1636",
)
def query_data(
    question: str,
    intent: str = "data",
    kind: str | None = None,
    plan: dict[str, Any] | None = None,
    values: dict[str, str] | None = None,
    wren_first: bool = False,
    want_images: bool = False,
    produces: list[dict[str, Any]] | None = None,
    repair_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ... import agent, clarify
    from ....config import settings

    filled_question, missing = agent._fill_placeholders(question, values or {})
    if missing:
        return observation(
            "query_data",
            ok=False,
            summary="缺少链式回填值：" + "、".join(missing),
            error="missing_values",
            next_hint="先调用 extract_produced 或回填正确的 values。",
        )
    try:
        payload = agent._build_query_result(
            filled_question,
            intent,
            kind=kind,
            template_fallback=True,
            wren_first=wren_first,
            plan=plan,
            skip_capabilities=True,
            want_images=want_images,
            repair_feedback=repair_feedback,
        )
    except clarify.ClarifyNeeded as exc:
        reason = str(exc.reason)
        runtime_error = bool(
            re.search(
                r"ERROR \d+|You have an error in your SQL|Unknown column|doesn't exist",
                reason,
                re.IGNORECASE,
            )
        )
        failure = build_failure(
            "sql_runtime_error" if runtime_error else "missing_dependency",
            stage="execute",
            reason=reason,
            evidence={"step_question": filled_question},
        )
        return observation(
            "query_data",
            ok=False,
            accepted=False,
            failure=failure,
            summary=reason,
            error=reason,
            next_hint="信息不足，调用 ask_user 补充条件。",
        )
    except Exception as exc:  # noqa: BLE001
        return observation(
            "query_data",
            ok=False,
            summary=f"查询失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="检查字段、时间区间或改用更小的原子工具。",
        )

    result_id = str(payload.get("result_id") or "")
    if result_id:
        # ReAct 直接调用 _build_query_result，不会经过 handle_message 的统一落盘；
        # 不在这里保存，_section 就取不到子查询的 SQL/明细，approve 会误报"没有可入库的 SQL"。
        save_result(result_id, payload)

    rows = list(payload.get("rows") or [])
    columns = [str(item) for item in payload.get("columns") or []]
    limit = int(getattr(settings, "sql_max_rows", 500) or 500)
    produced: dict[str, str] = {}
    for item in produces or []:
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        value = agent._extract_produced(rows, item)
        if value:
            produced[key] = normalize_week_label(value)
    calibration = payload.get("calibration") or {}
    facts = calibration.get("facts") or {}
    coverage = {
        "asked_range": {
            "start": str((payload.get("context") or {}).get("time_start") or ""),
            "end": str((payload.get("context") or {}).get("time_end") or ""),
        },
        "actual_range": {
            "start": str(facts.get("data_min_time") or ""),
            "end": str(facts.get("data_max_time") or ""),
        },
        "empty": not rows,
    }
    ok = str(payload.get("source") or "") not in {"error", "clarification", "confirm"}
    preview_rows = [
        {key: normalize_week_label(value) for key, value in row.items()}
        if isinstance(row, dict)
        else row
        for row in rows[:10]
    ]
    failure = _verification_failure(filled_question, payload) if ok else None
    accepted = ok and failure is None
    return observation(
        "query_data",
        ok=accepted,
        accepted=accepted,
        failure=failure,
        summary=normalize_week_label(str(payload.get("reply") or "")),
        data={
            "row_count": len(rows),
            "columns": columns,
            "preview_rows": preview_rows,
            "produced": produced,
            "truncated": len(rows) >= limit,
            "result_id": result_id or None,
            "engine": payload.get("engine"),
            "source": payload.get("source"),
            "answer": str(payload.get("reply") or ""),
            "question": filled_question,
            "risk_warnings": list(payload.get("risk_warnings") or []),
        },
        coverage=coverage,
        error=(
            None
            if accepted
            else str(
                (failure or {}).get("reason")
                or payload.get("reply")
                or "查询没有通过验收"
            )
        ),
        next_hint="" if accepted and rows else "结果为空或未通过验收，先核对失败证据。",
    )
