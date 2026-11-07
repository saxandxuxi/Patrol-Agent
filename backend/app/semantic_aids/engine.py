from __future__ import annotations

import re
from typing import Any

from ..services.resource_loader import load_yaml

from .models import AssistEvidence


_CHINESE_DIGITS = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def _config() -> dict[str, Any]:
    value = load_yaml("capabilities/assist_rules.yaml", {})
    return value if isinstance(value, dict) else {}


def _chinese_integer(value: str) -> str:
    text = str(value or "").strip().replace("第", "").replace("车道", "")
    if text.isdigit():
        return str(int(text))
    if text == "十":
        return "10"
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CHINESE_DIGITS.get(left, 1) if left else 1
        ones = _CHINESE_DIGITS.get(right, 0) if right else 0
        return str(int(tens) * 10 + int(ones))
    if len(text) == 1 and text in _CHINESE_DIGITS:
        return str(_CHINESE_DIGITS[text])
    return text


def _transform(name: str, value: str) -> str:
    if name == "chinese_integer":
        return _chinese_integer(value)
    return value


def detect_evidence(
    question: str,
    kind: str = "mysql",
) -> tuple[AssistEvidence, ...]:
    text = str(question or "")
    if not text:
        return ()
    config = _config()
    evidence: list[AssistEvidence] = []
    seen: set[tuple[str, str]] = set()

    def add(item: AssistEvidence) -> None:
        key = (item.rule_id, item.raw_span)
        if key in seen:
            return
        seen.add(key)
        evidence.append(item)

    for rule_id, raw in (config.get("keyword_rules") or {}).items():
        if not isinstance(raw, dict):
            continue
        matched = next(
            (
                str(word)
                for word in raw.get("any") or []
                if str(word) and str(word) in text
            ),
            "",
        )
        if not matched:
            continue
        add(
            AssistEvidence(
                rule_id=str(rule_id),
                source="code_rule",
                candidate_type=str(raw.get("candidate_type") or ""),
                raw_span=matched,
                candidate_field=str(raw.get("candidate_field") or ""),
                candidate_value=raw.get("candidate_value") or "",
                reason=str(raw.get("reason") or ""),
                discouraged_fields=tuple(
                    str(item)
                    for item in raw.get("discouraged_fields") or ()
                ),
                requires_field=bool(raw.get("requires_field", False)),
            )
        )

    for raw in config.get("regex_rules") or []:
        if not isinstance(raw, dict):
            continue
        try:
            matches = list(re.finditer(
                str(raw.get("pattern") or ""),
                text,
                re.IGNORECASE,
            ))
        except re.error:
            continue
        if not matches:
            continue
        if not raw.get("repeat_matches"):
            matches = matches[:1]
        group = int(raw.get("group") or 0)
        for matched in matches:
            value = matched.group(group) if group <= len(matched.groups()) else matched.group(0)
            transformed = _transform(
                str(raw.get("value_transform") or ""),
                value,
            )
            add(
                AssistEvidence(
                    rule_id=str(raw.get("id") or ""),
                    source="code_rule",
                    candidate_type=str(raw.get("candidate_type") or ""),
                    raw_span=matched.group(0),
                    candidate_field=str(raw.get("candidate_field") or ""),
                    candidate_value=(
                        raw.get("candidate_value")
                        if raw.get("candidate_value") not in {None, ""}
                        else transformed
                    ),
                    reason=str(raw.get("reason") or ""),
                    discouraged_fields=tuple(
                        str(item)
                        for item in raw.get("discouraged_fields") or ()
                    ),
                    requires_field=bool(raw.get("requires_field", False)),
                )
            )

    for rule_id, raw in (config.get("metric_rules") or {}).items():
        if not isinstance(raw, dict):
            continue
        try:
            matched = re.search(str(raw.get("pattern") or ""), text)
        except re.error:
            continue
        if matched is None:
            continue
        add(
            AssistEvidence(
                rule_id=str(rule_id),
                source="code_rule",
                candidate_type="metric",
                raw_span=matched.group(0),
                candidate_field=str(raw.get("metric") or ""),
                candidate_value=str(raw.get("metric") or ""),
            )
        )
    return tuple(evidence)


def canonical_field(field: str) -> str:
    normalized = str(field or "").rsplit(".", 1)[-1]
    aliases = _config().get("field_aliases") or {}
    return str(aliases.get(normalized) or normalized)
