from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


@tool(
    name="query_knowledge",
    layer="big",
    domain="knowledge",
    description="查询养护规范、定额、评定标准和制度说明；系统数据源配置类问题也可用，不查业务明细表。",
    input_schema={
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
    },
    wraps="agent._answer_knowledge:169",
)
def query_knowledge(question: str) -> dict[str, Any]:
    from ... import agent

    try:
        payload = agent._answer_knowledge(question)
    except Exception as exc:  # noqa: BLE001
        return observation(
            "query_knowledge",
            ok=False,
            summary=f"知识查询失败：{exc}",
            error=f"{type(exc).__name__}: {exc}",
            next_hint="检查 RAGFlow 状态，或仅基于现有数据作答。",
        )
    references = list(payload.get("references") or [])
    ok = str(payload.get("source") or "") not in {"error"}
    return observation(
        "query_knowledge",
        ok=ok,
        summary=str(payload.get("reply") or ""),
        data={
            "row_count": len(references),
            "columns": ["document", "content"],
            "preview_rows": [
                {
                    "document": str(item.get("document_keyword") or ""),
                    "content": str(item.get("content") or "")[:300],
                }
                for item in references[:10]
            ],
            "produced": {},
            "truncated": len(references) > 10,
            "answer": str(payload.get("reply") or ""),
            "source": payload.get("source"),
            "reference_count": len(references),
        },
        error=None if ok else str(payload.get("reply") or ""),
        next_hint="" if references or payload.get("source") == "config" else "知识库无命中，可转 ask_user 或只给数据事实。",
    )
