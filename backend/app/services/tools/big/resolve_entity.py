from __future__ import annotations

from typing import Any

from ..observation import observation
from ..registry import tool


_ASSET_SECTION_COLUMNS = {
    "bridge": ("basic_facilities_bridges", "sections_id"),
    "tunnel": ("basic_facilities_tunnels", "belong_section_id"),
    "slope": ("basic_facilities_slope", "sections_id"),
}


def _result_observation(subject: str, rows: list[dict[str, str]], summary: str):
    return observation(
        "resolve_entity",
        ok=True,
        summary=summary,
        data={
            "row_count": len(rows),
            "columns": ["subject", "id", "name", "section_ids", "route_ids"],
            "preview_rows": rows[:10],
            "produced": {
                "section_ids": "、".join(
                    sorted({str(item.get("section_ids") or "") for item in rows if item.get("section_ids")})
                )
            },
            "truncated": False,
            "subject": subject,
        },
        next_hint="" if rows else "资产台账没有可用记录，不要据此判断病害数为零。",
    )


@tool(
    name="resolve_entity",
    layer="big",
    domain="entity",
    description="把路线、路段、桥梁、隧道、边坡名称解析成标准 id，并补齐所属路段/路线关联链；禁止把资产 id 直接当 sections_id 查病害。",
    input_schema={
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "entity_text": {"type": "string"},
            "kind": {"type": "string", "default": "mysql"},
        },
        "required": ["subject", "entity_text"],
    },
    wraps="asset_resolver.resolve:106",
)
def resolve_entity(subject: str, entity_text: str, kind: str = "mysql") -> dict[str, Any]:
    from ... import asset_resolver, route_resolver
    from ...db import db_manager

    subject = str(subject or "").strip().lower()
    if kind != "mysql":
        return observation(
            "resolve_entity",
            ok=False,
            summary="实体解析目前只支持 MySQL 业务库。",
            error="unsupported_kind",
        )
    if subject == "route":
        try:
            route = route_resolver._resolve_route(entity_text)
        except Exception as exc:  # noqa: BLE001
            return _result_observation("route", [], f"路线未解析：{exc}")
        section_rows = db_manager.query(
            "mysql",
            "SELECT id FROM basic_facilities_sections WHERE route_id = '"
            + str(route["id"]).replace("'", "''")
            + "' AND del_flag = 0 LIMIT 100",
        )["rows"]
        row = {
            "subject": "route",
            "id": str(route["id"]),
            "name": str(route.get("short_name") or route.get("full_name") or ""),
            "section_ids": "、".join(str(item["id"]) for item in section_rows),
            "route_ids": str(route["id"]),
        }
        return _result_observation("route", [row], f"已解析路线：{row['name']}。")
    if subject == "section":
        from ... import section_resolver

        hit = section_resolver.resolve(entity_text)
        matches = list(hit.get("matches") or [])
        if hit.get("unique") and hit.get("best"):
            matches = [hit["best"]]
        if not matches:
            return _result_observation("section", [], "问题中没有识别到路段名称。")
        data = [
            {
                "subject": "section",
                "id": str(item["id"]),
                "name": str(item.get("full_name") or ""),
                "section_ids": str(item["id"]),
                "route_ids": str(item.get("route_id") or ""),
            }
            for item in matches[:6]
        ]
        return _result_observation(
            "section",
            data,
            (
                f"唯一匹配路段：{data[0]['name']}。"
                if hit.get("unique") and len(data) == 1
                else f"识别到 {len(data)} 个路段候选，请结合名称区分。"
            ),
        )
    meta = _ASSET_SECTION_COLUMNS.get(subject)
    if not meta:
        return observation(
            "resolve_entity",
            ok=False,
            summary=f"不支持的实体类型：{subject}",
            error="unsupported_subject",
        )
    resolved = asset_resolver.resolve("mysql", subject, entity_text)
    if not resolved.get("id"):
        summary = (
            f"资产台账暂无可匹配数据。候选：{resolved.get('candidates')}"
            if not resolved.get("candidates")
            else f"存在同名候选：{resolved.get('candidates')}"
        )
        return _result_observation(subject, [], summary)
    table, section_column = meta
    rows = db_manager.query(
        "mysql",
        f"SELECT id, {section_column} AS section_id FROM {table} "
        f"WHERE id = '{str(resolved['id']).replace(chr(39), chr(39) * 2)}' AND del_flag = 0 LIMIT 1",
    )["rows"]
    section_ids = [str(item.get("section_id")) for item in rows if item.get("section_id")]
    route_ids: list[str] = []
    if section_ids:
        quoted = ", ".join("'" + item.replace("'", "''") + "'" for item in section_ids)
        route_rows = db_manager.query(
            "mysql",
            "SELECT DISTINCT route_id FROM basic_facilities_sections "
            f"WHERE id IN ({quoted}) AND del_flag = 0",
        )["rows"]
        route_ids = [str(item["route_id"]) for item in route_rows if item.get("route_id")]
    row = {
        "subject": subject,
        "id": str(resolved["id"]),
        "name": str(resolved.get("name") or ""),
        "section_ids": "、".join(section_ids),
        "route_ids": "、".join(route_ids),
    }
    return _result_observation(subject, [row], f"已解析{subject}：{row['name']}。")
