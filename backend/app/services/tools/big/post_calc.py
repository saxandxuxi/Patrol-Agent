from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool
from ...react_repair import build_failure


@tool(
    name="post_calc",
    layer="big",
    domain="compose",
    description="对前序结果做差值、大小比较、求和、占比等纯计算，不查询数据库。",
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "object"},
            "results": {"type": "object", "description": "步骤 id → 前序 payload"},
        },
        "required": ["task", "results"],
    },
    wraps="agent._post_calc_payload:2411",
)
def post_calc(task: dict[str, Any], results: dict[int | str, dict[str, Any]]) -> dict[str, Any]:
    from ... import agent

    normalized = {int(key): value for key, value in (results or {}).items()}
    try:
        payload = agent._post_calc_payload(task, normalized)
    except Exception as exc:  # noqa: BLE001
        return observation(
            "post_calc",
            ok=False,
            summary=f"后置计算失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="检查前序结果是否含可用数字。",
        )
    accepted = bool(payload.get("accepted", True))
    failure = None
    if not accepted:
        upstream = {
            str(key): {
                "question": str((value or {}).get("question") or ""),
                "reply": str((value or {}).get("reply") or ""),
            }
            for key, value in (results or {}).items()
        }
        failure = build_failure(
            "post_calc_empty",
            stage="post_calc",
            reason=str(payload.get("reply") or "后置计算没有返回可用结果"),
            evidence={
                "step_id": task.get("id"),
                "step_question": str(task.get("question") or ""),
                "depends_on": list(task.get("depends_on") or []),
                "upstream": upstream,
            },
        )
    return observation(
        "post_calc",
        ok=accepted,
        accepted=accepted,
        failure=failure,
        summary=str(payload.get("reply") or ""),
        data={
            "row_count": len(payload.get("rows") or []),
            "columns": list(payload.get("columns") or []),
            "preview_rows": list(payload.get("rows") or [])[:10],
            "produced": {},
            "truncated": False,
            "answer": str(payload.get("reply") or ""),
            "source": payload.get("source"),
            "engine": payload.get("engine"),
        },
        next_hint="继续分析或直接作为最终答案。",
    )
