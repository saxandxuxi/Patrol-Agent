from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


@tool(
    name="ask_user",
    layer="big",
    domain="clarify",
    description="信息不足或无法继续获取时结束当前链路，生成明确追问；这是终止型工具。",
    input_schema={
        "type": "object",
        "properties": {
            "kind": {"type": "string", "default": "mysql"},
            "reason": {"type": "string"},
            "lead": {"type": ["string", "null"]},
            "show_fields": {"type": "boolean", "default": True},
            "subject": {"type": "string", "default": ""},
            "choices": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["reason"],
    },
    wraps="clarify.ClarifyNeeded:12",
)
def ask_user(
    reason: str,
    kind: str = "mysql",
    lead: str | None = None,
    show_fields: bool = True,
    subject: str = "",
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from ... import clarify

    need = clarify.ClarifyNeeded(
        kind,
        reason,
        lead=lead,
        show_fields=show_fields,
        choices=choices,
        subject=subject,
    )
    reply = clarify.clarification_reply(
        need.kind,
        need.reason,
        lead=need.lead,
        show_fields=need.show_fields,
        subject=need.subject,
    )
    return observation(
        "ask_user",
        ok=True,
        summary=reply,
        data={
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "produced": {},
            "truncated": False,
            "await_user": True,
            "choices": need.choices,
        },
        next_hint="等待用户补充条件后再继续。",
    )
