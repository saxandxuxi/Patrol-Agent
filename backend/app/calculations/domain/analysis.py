from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from ...services import prompts
from ...services.llm import chat, role_model
from ...services.logging_service import log_error
from ..evidence import compact_table_evidence
from ..serialization import json_value


_LIST_PREFIX = re.compile(r"(?m)^\s*\d+[.、)]\s*")
_BUSINESS_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_.:-]*"
)
_ABBREVIATED_IDENTIFIER = re.compile(r"(?:…|\.{3})\s*\d+")
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])([-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
)


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
    reply = _request_analysis(question, evidence, requirements)
    assertions = {
        str(item)
        for item in requirements.get("required_assertions") or ()
    } if requirements else set()
    grouped_comparison = _group_comparison_conclusion(
        evidence,
        assertions,
    )
    presentation_repair: dict[str, Any] | None = None
    if grouped_comparison:
        reply = grouped_comparison
    elif "analysis_only" in assertions and reply:
        ungrounded = _ungrounded_numbers(reply, evidence)
        if ungrounded:
            repair_requirements = {
                **(requirements or {}),
                "presentation_repair": {
                    "invalid_numbers": ungrounded,
                    "original_reply": reply,
                    "instruction": (
                        "只能在证据已有数字之间作定性比较；"
                        "不得计算差值、合计、比例或任何新数字。"
                    ),
                },
            }
            repaired = _request_analysis(
                question,
                evidence,
                repair_requirements,
            )
            repaired_ungrounded = _ungrounded_numbers(repaired, evidence)
            if repaired and not repaired_ungrounded:
                presentation_repair = {
                    "invalid_numbers": ungrounded,
                    "repaired": True,
                }
                reply = repaired
            else:
                return {
                    "reply": "",
                    "source": "calc",
                    "engine": "post_calc",
                    "accepted": False,
                    "failure_code": "analysis_only_new_numeric_value",
                    "failure_reason": (
                        "analysis_only 只能复述上游已有数值，不能生成新的算术结果。"
                        "请在 post_calc 中声明 calculation 工具，并引用上游真实字段。"
                    ),
                    "ungrounded_numbers": ungrounded,
                }
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
    result = {
        "reply": reply,
        "source": "calc",
        "engine": "post_calc",
        "accepted": bool(reply),
        "columns": [],
        "rows": [],
    }
    if presentation_repair:
        result["presentation_repair"] = presentation_repair
    return result


def _request_analysis(
    question: str,
    evidence: dict[str, Any],
    requirements: dict[str, Any] | None,
) -> str:
    try:
        return chat(
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
        return ""


def _number_key(value: Any) -> str:
    try:
        number = Decimal(str(value).replace(",", ""))
    except (ArithmeticError, ValueError):
        return ""
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


def _numbers(text: str) -> set[str]:
    cleaned = _LIST_PREFIX.sub("", str(text or ""))
    # Business identifiers are labels, not arithmetic operands. Mask full
    # alphanumeric IDs and their abbreviated ellipsis form before grounding.
    cleaned = _BUSINESS_IDENTIFIER.sub(" ", cleaned)
    cleaned = _ABBREVIATED_IDENTIFIER.sub(" ", cleaned)
    return {
        normalized
        for match in _NUMBER.finditer(cleaned)
        if (normalized := _number_key(match.group(1)))
    }


def _ungrounded_numbers(
    reply: str,
    evidence: dict[str, Any],
) -> list[str]:
    source_numbers = _numbers(
        json.dumps(evidence, ensure_ascii=False, default=str)
    )
    return sorted(_numbers(reply) - source_numbers)


def _format_number(value: Any) -> str:
    normalized = _number_key(value)
    return normalized or str(value)


def _group_comparison_conclusion(
    evidence: dict[str, Any],
    assertions: set[str],
) -> str:
    if "multi_metric_comparison" not in assertions:
        return ""
    boundary_name = "min" if "lower_is_better" in assertions else "max"
    direction = "更低" if boundary_name == "min" else "更高"
    opposite_name = "max" if boundary_name == "min" else "min"
    parts: list[str] = []
    single_item_signatures: list[tuple[str, ...]] = []
    all_groups_are_single_items = True
    for dependency in evidence.get("upstream") or []:
        compact = dependency.get("compact") or {}
        for summary in compact.get("group_summaries") or []:
            detail_columns = list(summary.get("detail_columns") or ())
            for group in summary.get("groups") or []:
                row_count = int(group.get("row_count") or 0)
                if row_count != 1:
                    all_groups_are_single_items = False
                parent = " / ".join(
                    str(value)
                    for value in (group.get("group") or {}).values()
                    if str(value or "").strip()
                )
                metrics = group.get("metrics") or {}
                for metric, boundaries in metrics.items():
                    selected = boundaries.get(boundary_name) or {}
                    opposite = boundaries.get(opposite_name) or {}
                    selected_row = selected.get("row") or {}
                    opposite_row = opposite.get("row") or {}
                    selected_detail = " / ".join(
                        str(selected_row.get(column))
                        for column in detail_columns
                        if selected_row.get(column) not in (None, "")
                    )
                    opposite_detail = " / ".join(
                        str(opposite_row.get(column))
                        for column in detail_columns
                        if opposite_row.get(column) not in (None, "")
                    )
                    if not parent or not selected_detail or not opposite_detail:
                        continue
                    selected_value = selected.get("value")
                    opposite_value = opposite.get("value")
                    if row_count == 1:
                        single_detail = "，".join(
                            f"{column} {_normalized_value(selected_row.get(column))}"
                            for column in detail_columns
                            if selected_row.get(column) not in (None, "")
                        )
                        single_item_signatures.append(
                            tuple(
                                f"{column}={_normalized_value(selected_row.get(column))}"
                                for column in detail_columns
                            )
                        )
                        parts.append(
                            f"{parent}：{single_detail}，"
                            f"{metric} {_format_number(selected_value)}"
                        )
                        continue
                    if _number_key(selected_value) == _number_key(opposite_value):
                        parts.append(
                            f"{parent}：{metric}在各选项中相同"
                            f"（{_format_number(selected_value)}）"
                        )
                        continue
                    parts.append(
                        f"{parent}：{selected_detail}{direction}"
                        f"（{_format_number(selected_value)} 对 "
                        f"{_format_number(opposite_value)}）"
                    )
    if (
        all_groups_are_single_items
        and len(single_item_signatures) > 1
    ):
        if len(set(single_item_signatures)) == 1:
            parts.append("各分组的主项相同")
        else:
            parts.append("各分组的主项不完全相同")
    if not parts:
        return ""
    return "按结构化结果比较：" + "；".join(parts[:10]) + "。"


def _normalized_value(value: Any) -> str:
    return str(value if value is not None else "").strip()


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
