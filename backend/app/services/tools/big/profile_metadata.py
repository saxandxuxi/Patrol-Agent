from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


@tool(
    name="profile_metadata",
    layer="big",
    domain="metadata",
    description="回答表/字段清单、字段空值率、完整度和数据画像问题，不进入业务 NL2SQL 主链。",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "kind": {"type": "string", "default": "mysql"},
        },
        "required": ["question"],
    },
    wraps="generic.profiling_reply:819",
)
def profile_metadata(question: str, kind: str = "mysql") -> dict[str, Any]:
    from ... import generic

    try:
        payload = generic.profiling_reply(question, kind)
    except Exception as exc:  # noqa: BLE001
        return observation(
            "profile_metadata",
            ok=False,
            summary=f"元数据画像失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="检查字段目录和数据库连接。",
        )
    if not payload:
        return observation(
            "profile_metadata",
            ok=False,
            summary="这条问题不属于元数据/字段画像类型。",
            error="not_metadata_question",
            next_hint="改用 query_data 或 query_knowledge。",
        )
    rows = list(payload.get("rows") or [])
    return observation(
        "profile_metadata",
        ok=True,
        summary=str(payload.get("reply") or ""),
        data={
            "row_count": len(rows),
            "columns": ["table", "column", "nulls", "total", "null_rate"],
            "preview_rows": rows[:10],
            "produced": {},
            "truncated": len(rows) >= 200,
            "source": payload.get("source"),
            "engine": payload.get("engine"),
        },
        next_hint="可作为最终答案或继续下钻指定表字段。",
    )
