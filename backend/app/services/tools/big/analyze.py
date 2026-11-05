from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


@tool(
    name="analyze",
    layer="big",
    domain="compose",
    description="把前序数据查询和规范依据放在一起做综合判断、原因分析或养护建议，不重新查库。",
    input_schema={
        "type": "object",
        "properties": {
            "task": {"type": "object"},
            "results": {"type": "object"},
            "session_id": {"type": ["string", "null"]},
        },
        "required": ["task", "results"],
    },
    wraps="agent._analysis_payload:2328",
)
def analyze(
    task: dict[str, Any],
    results: dict[int | str, dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    from ... import agent

    normalized = {int(key): value for key, value in (results or {}).items()}
    try:
        payload = agent._analysis_payload(task, normalized, session_id=session_id)
    except Exception as exc:  # noqa: BLE001
        return observation(
            "analyze",
            ok=False,
            summary=f"综合分析失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="只返回已有事实，或先补齐数据/规范工具。",
        )
    return observation(
        "analyze",
        ok=True,
        summary=str(payload.get("reply") or ""),
        data={
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "produced": {},
            "truncated": False,
            "answer": str(payload.get("reply") or ""),
            "source": payload.get("source"),
            "engine": payload.get("engine"),
        },
        next_hint="可作为最终答案返回。",
    )
