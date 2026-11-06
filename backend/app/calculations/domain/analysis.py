from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from ...services import prompts
from ...services.llm import chat, role_model
from ...services.logging_service import log_error
from ..evidence import compact_table_evidence
from ..serialization import json_value


def analyze_evidence(
    question: str,
    dependencies: list[dict[str, Any]],
    *,
    requirements: dict[str, Any] | None = None,
    fallback_reply: str = "",
) -> dict[str, Any]:
    rows = [
        dict(row)
        for dependency in dependencies
        for row in dependency.get("rows") or []
        if isinstance(row, dict)
    ]
    evidence = {
        "upstream": [
            {
                "question": str(dependency.get("question") or ""),
                "reply": str(dependency.get("reply") or ""),
                "field_labels": json_value(
                    dependency.get("field_labels") or {}
                ),
                "row_count": int(
                    dependency.get("row_count")
                    or len(dependency.get("rows") or [])
                ),
                "compact": (
                    dependency.get("compact")
                    or compact_table_evidence(
                        list(dependency.get("rows") or [])
                    )
                ),
            }
            for dependency in dependencies
        ]
    }
    try:
        reply = chat(
            prompts.messages(
                "table_analysis",
                question=question,
                evidence=json.dumps(
                    evidence,
                    ensure_ascii=False,
                    default=str,
                ),
                requirements=json.dumps(
                    requirements or {},
                    ensure_ascii=False,
                    default=str,
                ),
            ),
            temperature=0,
            model=role_model("semantic"),
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("calculation", "analysis_failed", exc, question=question[:120])
        reply = ""
    if not reply:
        reply = fallback_reply or _fallback_conclusion(
            evidence,
            requirements or {},
        )
    metrics = {
        column: boundary
        for dependency in evidence.get("upstream") or []
        for column, boundary in (
            (dependency.get("compact") or {}).get("metrics") or {}
        ).items()
    }
    if reply and (
        len(reply.strip()) < 12
        or (
            metrics
            and not any(char.isdigit() for char in reply)
        )
    ):
        reply = fallback_reply or _fallback_conclusion(
            evidence,
            requirements or {},
        )
    return {
        "reply": reply,
        "source": "calc",
        "engine": "post_calc",
        "accepted": bool(reply),
        "columns": [],
        "rows": [],
    }


def _fallback_conclusion(
    evidence: dict[str, Any],
    requirements: dict[str, Any],
) -> str:
    parts: list[str] = []
    boundary_name = (
        "min"
        if "lower_is_better"
        in {
            str(item)
            for item in requirements.get("required_assertions") or ()
        }
        else "max"
    )
    groups: dict[str, int] = {}
    for dependency in evidence.get("upstream") or []:
        compact = dependency.get("compact") or {}
        metrics = compact.get("metrics") or {}
        for name, boundary in metrics.items():
            selected = boundary[boundary_name]
            parts.append(
                f"{name}{'最低' if boundary_name == 'min' else '最高'}的是"
                f"{selected['group']}（{selected['value']}）"
            )
            group = str(selected.get("group") or "")
            if group:
                groups[group] = groups.get(group, 0) + 1
    if not parts:
        return ""
    lead = max(groups, key=groups.get) if groups else ""
    prefix = (
        f"从可用代理指标看，{lead}在最多指标上更优；"
        if lead
        else "按可用代理指标："
    )
    return (
        prefix
        + "；".join(parts[:6])
        + "。这些是代理指标，不代表正式业务评分。"
    )
