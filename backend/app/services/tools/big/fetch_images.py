from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


@tool(
    name="fetch_images",
    layer="big",
    domain="image",
    description="按自然语言子问题取一批现场图片及其出处；只负责找图和路径核验，不读取图片二进制。",
    input_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "force_ai": {"type": "boolean", "default": False},
        },
        "required": ["question"],
    },
    wraps="agent._image_result:3472",
)
def fetch_images(question: str, force_ai: bool = False) -> dict[str, Any]:
    from ... import agent

    try:
        payload = agent._image_result(question, force_ai=force_ai)
    except Exception as exc:  # noqa: BLE001
        return observation(
            "fetch_images",
            ok=False,
            summary=f"图片查询失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="确认图片路径和对象存储连接后重试。",
        )
    items = list(payload.get("image_items") or [])
    images = list(payload.get("images") or [])
    missing = list(payload.get("missing_images") or [])
    ok = str(payload.get("source") or "") not in {"error"}
    return observation(
        "fetch_images",
        ok=ok,
        summary=str(payload.get("reply") or ""),
        data={
            "row_count": len(items),
            "columns": ["path", "origin", "column", "shared_records"],
            "preview_rows": items[:10],
            "produced": {"image_count": str(len(images))},
            "truncated": bool(payload.get("truncated")),
            "image_count": len(images),
            "missing_count": len(missing),
            "result_id": payload.get("result_id"),
        },
        error=None if ok else str(payload.get("reply") or ""),
        next_hint="" if images else "没有可打开图片；可放宽条件或改用全库图片清单。",
    )
