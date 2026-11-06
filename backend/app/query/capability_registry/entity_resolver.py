from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from ...config import settings
from ...services.db import db_manager
from .registry import CapabilityRegistry


@dataclass(frozen=True)
class EntityValue:
    code: str
    label: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class EntityResolution:
    status: Literal["resolved", "expanded", "ambiguous", "unsupported"]
    values: tuple[EntityValue, ...] = ()
    family: str | None = None
    evidence: tuple[dict[str, Any], ...] = ()


_CACHE: dict[tuple[str, str], tuple[float, tuple[EntityValue, ...]]] = {}


def resolve_entity(
    registry: CapabilityRegistry,
    entity: str,
    raw_value: Any,
    kind: str = "mysql",
) -> EntityResolution:
    entity_id = str(entity or "").strip()
    capability = registry.snapshot.entities.get(entity_id)
    if capability is None:
        return EntityResolution(status="unsupported")
    text = str(raw_value or "").strip()
    if not text:
        return EntityResolution(status="unsupported")
    values = _entity_values(kind, entity_id, capability.source)
    if not values:
        return EntityResolution(status="unsupported")

    for family_id, family in registry.snapshot.entity_families.items():
        if family.entity != entity_id:
            continue
        if text not in {family.label, *family.aliases}:
            continue
        by_label = {item.label: item for item in values}
        missing = [member for member in family.members if member not in by_label]
        if missing:
            return EntityResolution(
                status="unsupported",
                family=family_id,
                evidence=(
                    {
                        "reason": "family_members_missing",
                        "missing": missing,
                    },
                ),
            )
        members = tuple(by_label[member] for member in family.members)
        return EntityResolution(
            status="expanded",
            values=members,
            family=family_id,
            evidence=(
                {
                    "family": family_id,
                    "members": [item.label for item in members],
                },
            ),
        )

    alias_map = registry.snapshot.aliases.get(entity_id) or {}
    alias_target = (
        alias_map.get(text)
        if isinstance(alias_map, Mapping)
        else None
    )
    effective = str(alias_target or text)
    exact = [
        item
        for item in values
        if effective in {item.code, item.label, *item.aliases}
    ]
    if len(exact) == 1:
        return EntityResolution(
            status="resolved",
            values=(exact[0],),
            evidence=(
                {
                    "entity": entity_id,
                    "match": "alias" if alias_target else "exact",
                },
            ),
        )
    if len(exact) > 1:
        return EntityResolution(
            status="ambiguous",
            values=tuple(exact),
            evidence=({"entity": entity_id, "match": "duplicate"},),
        )
    partial = [
        item
        for item in values
        if effective in item.label
        or item.label in effective
        or any(effective in alias or alias in effective for alias in item.aliases)
    ]
    if len(partial) == 1:
        return EntityResolution(
            status="resolved",
            values=(partial[0],),
            evidence=({"entity": entity_id, "match": "unique_partial"},),
        )
    if len(partial) > 1:
        return EntityResolution(
            status="ambiguous",
            values=tuple(partial),
            evidence=({"entity": entity_id, "match": "multiple_partial"},),
        )
    return EntityResolution(status="unsupported")


def list_entities(
    registry: CapabilityRegistry,
    entity: str,
    kind: str = "mysql",
) -> tuple[EntityValue, ...]:
    capability = registry.snapshot.entities.get(str(entity or "").strip())
    if capability is None:
        return ()
    return _entity_values(kind, str(entity), capability.source)


def _entity_values(
    kind: str,
    entity_id: str,
    source: dict[str, Any],
) -> tuple[EntityValue, ...]:
    key = (kind, entity_id)
    cached = _CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < settings.reference_data_ttl_seconds:
        return cached[1]
    table = str(source.get("table") or "")
    label_column = str(source.get("value_column") or "")
    code_column = str(source.get("code_column") or label_column)
    alias_column = str(source.get("alias_column") or "")
    if not table or not label_column:
        return ()
    alias_select = f", `{alias_column}` AS entity_alias" if alias_column else ""
    try:
        rows = db_manager.query(
            kind,
            f"SELECT DISTINCT `{code_column}` AS entity_code, "
            f"`{label_column}` AS entity_label{alias_select} "
            f"FROM `{table}` WHERE `{label_column}` IS NOT NULL "
            f"AND `{label_column}` <> '' ORDER BY `{label_column}`",
        )["rows"]
    except Exception:
        return ()
    values = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        code = str(row.get("entity_code") or "")
        label = str(row.get("entity_label") or "")
        if not label or (code, label) in seen:
            continue
        seen.add((code, label))
        aliases = tuple(
            part.strip()
            for part in re.split(
                r"[,，、/;；|]",
                str(row.get("entity_alias") or ""),
            )
            if part.strip()
        )
        values.append(EntityValue(code=code, label=label, aliases=aliases))
    seen_labels = {item.label for item in values}
    for source_item in source.get("auxiliary_sources") or []:
        if not isinstance(source_item, Mapping):
            continue
        table_name = str(source_item.get("table") or "")
        column_name = str(source_item.get("value_column") or "")
        json_path = str(source_item.get("json_path") or "")
        if not table_name or not column_name or not json_path:
            continue
        try:
            extra_rows = db_manager.query(
                kind,
                f"SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(`{column_name}`, "
                f"'{json_path}')) AS entity_label FROM `{table_name}` "
                f"WHERE `{column_name}` IS NOT NULL",
            )["rows"]
        except Exception:
            continue
        for row in extra_rows:
            label = str(row.get("entity_label") or "")
            if not label or label in seen_labels:
                continue
            seen_labels.add(label)
            values.append(
                EntityValue(code=label, label=label, aliases=())
            )
    result = tuple(values)
    _CACHE[key] = (now, result)
    return result
