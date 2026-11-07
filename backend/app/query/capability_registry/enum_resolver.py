from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .models import EnumValueCapability
from .registry import CapabilityRegistry


@dataclass(frozen=True)
class EnumResolution:
    status: Literal["resolved", "ambiguous", "unsupported"]
    values: tuple[EnumValueCapability, ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()


def resolve_enum(
    registry: CapabilityRegistry,
    table: str,
    column: str,
    raw_value: Any,
) -> EnumResolution:
    enum = registry.snapshot.enums.get((str(table), str(column)))
    if enum is None:
        return EnumResolution(status="unsupported")
    text = str(raw_value or "").strip()
    if not text:
        return EnumResolution(status="unsupported")
    exact = [
        value
        for value in enum.values.values()
        if text in {value.code, value.label, value.display}
    ]
    if len(exact) == 1:
        return EnumResolution(
            status="resolved",
            values=(exact[0],),
            evidence=({"field": enum.key, "match": "exact"},),
        )
    if len(exact) > 1:
        return EnumResolution(
            status="ambiguous",
            values=tuple(exact),
            evidence=({"field": enum.key, "match": "duplicate_label"},),
        )
    alias_map = registry.snapshot.aliases.get(enum.key) or {}
    alias_target = (
        alias_map.get(text)
        if isinstance(alias_map, Mapping)
        else None
    )
    if alias_target:
        alias_value = next(
            (
                value
                for value in enum.values.values()
                if alias_target in {value.code, value.label, value.display}
            ),
            None,
        )
        if alias_value:
            return EnumResolution(
                status="resolved",
                values=(alias_value,),
                evidence=(
                    {
                        "field": enum.key,
                        "match": "alias",
                        "alias": text,
                    },
                ),
            )
    partial = [
        value
        for value in enum.values.values()
        if text in value.label or value.label in text
    ]
    if len(partial) == 1:
        return EnumResolution(
            status="resolved",
            values=(partial[0],),
            evidence=({"field": enum.key, "match": "unique_label"},),
        )
    if len(partial) > 1:
        # 输入同时包含多个枚举标签时（“沥青混凝土路面”既含“沥青混凝土”也含“沥青”），
        # 更具体的更长标签优先；只有并列最长才算真正的歧义。
        contained = [
            value
            for value in partial
            if str(value.label) in text or str(value.display or "") in text
        ]
        if contained:
            longest = max(len(str(value.label)) for value in contained)
            best = [
                value
                for value in contained
                if len(str(value.label)) == longest
            ]
            if len(best) == 1:
                return EnumResolution(
                    status="resolved",
                    values=(best[0],),
                    evidence=(
                        {"field": enum.key, "match": "longest_label"},
                    ),
                )
            return EnumResolution(
                status="ambiguous",
                values=tuple(best),
                evidence=({"field": enum.key, "match": "multiple_labels"},),
            )
        return EnumResolution(
            status="ambiguous",
            values=tuple(partial),
            evidence=({"field": enum.key, "match": "multiple_labels"},),
        )
    return EnumResolution(status="unsupported")
