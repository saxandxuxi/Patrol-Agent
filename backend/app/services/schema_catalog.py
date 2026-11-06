from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import settings
from .db import db_manager
from .logging_service import log_error, log_event


def _mysql_catalog() -> list[dict[str, Any]]:
    database = settings.mysql_database.replace("'", "''")
    tables = db_manager.query(
        "mysql",
        "SELECT TABLE_NAME, TABLE_COMMENT "
        "FROM information_schema.TABLES "
        f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME",
    )["rows"]
    columns = db_manager.query(
        "mysql",
        "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
        "COLUMN_DEFAULT, COLUMN_COMMENT, COLUMN_KEY, ORDINAL_POSITION "
        "FROM information_schema.COLUMNS "
        f"WHERE TABLE_SCHEMA = '{database}' ORDER BY TABLE_NAME, ORDINAL_POSITION",
    )["rows"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        grouped.setdefault(col["TABLE_NAME"], []).append(col)
    return [
        {
            "database": settings.mysql_database,
            "db_type": "mysql",
            "table": table["TABLE_NAME"],
            "comment": table["TABLE_COMMENT"],
            "columns": grouped.get(table["TABLE_NAME"], []),
        }
        for table in tables
    ]


def _pg_catalog() -> list[dict[str, Any]]:
    tables = db_manager.query(
        "postgresql",
        "SELECT table_name, "
        "obj_description(('public.' || table_name)::regclass, 'pg_class') AS comment "
        "FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name",
    )["rows"]
    columns = db_manager.query(
        "postgresql",
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "col_description(('public.' || table_name)::regclass, ordinal_position) AS comment, "
        "ordinal_position "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, ordinal_position",
    )["rows"]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for col in columns:
        grouped.setdefault(col["table_name"], []).append(col)
    return [
        {
            "database": settings.pg_database,
            "db_type": "postgresql",
            "table": table["table_name"],
            "comment": table["comment"],
            "columns": grouped.get(table["table_name"], []),
        }
        for table in tables
    ]


def build_catalog() -> dict[str, Any]:
    mysql_allowed = set(settings.mysql_allowed_tables)
    pg_allowed = set(settings.pg_allowed_tables)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mysql": [
            table
            for table in _mysql_catalog()
            if str(table.get("table") or "") in mysql_allowed
        ],
        "postgresql": [
            table
            for table in _pg_catalog()
            if str(table.get("table") or "") in pg_allowed
        ],
    }
    path = settings.data_dir / "schema_catalog.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def catalog_to_markdown(catalog: dict[str, Any]) -> str:
    lines = ["# 数据库字段字典", "", f"生成时间：{catalog.get('generated_at', '')}", ""]
    for section in ("mysql", "postgresql"):
        lines.append(f"## {section}")
        for table in catalog.get(section, []):
            lines.append(f"### {table['table']}")
            if table.get("comment"):
                lines.append(f"说明：{table['comment']}")
            lines.append("| 字段 | 类型 | 可空 | 默认值 | 说明 |")
            lines.append("|---|---|---|---|---|")
            for col in table.get("columns", []):
                name = col.get("COLUMN_NAME") or col.get("column_name")
                dtype = col.get("COLUMN_TYPE") or col.get("data_type")
                nullable = col.get("IS_NULLABLE") or col.get("is_nullable")
                default = col.get("COLUMN_DEFAULT") or col.get("column_default") or ""
                comment = col.get("COLUMN_COMMENT") or col.get("comment") or ""
                lines.append(f"| {name} | {dtype} | {nullable} | {default} | {comment} |")
            lines.append("")
    return "\n".join(lines)


def save_catalog_markdown(markdown: str) -> Path:
    path = settings.data_dir / "数据库字段字典.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def live_catalog(kind: str, include_rows: bool = False) -> dict[str, Any]:
    """实时从数据库读取结构（不依赖本地缓存文件），只返回白名单内的表。"""
    if kind == "mysql":
        allowed = set(settings.mysql_allowed_tables)
        raw = _mysql_catalog()
        database = settings.mysql_database
    elif kind == "postgresql":
        allowed = set(settings.pg_allowed_tables)
        raw = _pg_catalog()
        database = settings.pg_database
    else:
        raise ValueError(f"不支持的数据库类型：{kind}")
    return _shape_catalog(kind, raw, database, allowed, include_rows)


def _mdl_calculated_columns(kind: str) -> dict[str, list[dict[str, str]]]:
    """Wren 模型里声明、但物理表没有的列 → {物理表名: [{name, description, model}]}。

    只读本地 MDL 项目文件（`wren_projects/<kind>/models/*/metadata.yml`），不连库。
    读不到（没建 MDL / 解析失败）就返回空，目录退化成"只有物理列"，不影响原有行为。
    """
    try:
        from . import wren_engine

        cards = wren_engine._schema_index(str(wren_engine._project_dir(kind)))
    except Exception as exc:  # noqa: BLE001
        log_error("schema_catalog", "mdl_columns_failed", exc, kind=str(kind))
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for card in cards:
        table = str(card.get("table") or "")
        if not table:
            continue
        out.setdefault(table, []).extend(
            {
                "name": str(column_name),
                "description": str(note or ""),
                "model": str(card.get("name") or ""),
            }
            for column_name, note in card.get("columns") or []
        )
    return out


def _shape_catalog(
    kind: str,
    raw: list[dict[str, Any]],
    database: str,
    allowed: set[str],
    include_rows: bool = False,
) -> dict[str, Any]:
    """把 information_schema 的原始行整理成目录结构（实时读与磁盘快照共用这一份）。

    除了物理列，还并入 **Wren 模型里声明的计算列**（`ref_sql` / 派生列，物理表里没有这一列）：
    像 `section_length_km`（路段实际长度，起止桩号相减算出）、`route_length_km` 这种**派生指标**，
    语义最准、提示词里也要求拿它算密度/间距——但它只存在于 MDL，字段目录只读 information_schema
    时完全看不到，于是"路段长度"被判成"库里没有这列"（docs/派生指标与跨表字段语义可达方案.md）。
    计算列的描述直接用 MDL 里的 description（那是它唯一的语义来源）。
    只读本地项目文件，不新增数据库查询。
    """
    computed_by_table = _mdl_calculated_columns(kind)
    tables: list[dict[str, Any]] = []
    for table in raw:
        name = str(table["table"])
        if name not in allowed:
            continue
        columns = [
            {
                "name": col.get("COLUMN_NAME") or col.get("column_name"),
                "type": col.get("COLUMN_TYPE") or col.get("data_type"),
                "nullable": col.get("IS_NULLABLE") or col.get("is_nullable"),
                "default": col.get("COLUMN_DEFAULT") or col.get("column_default"),
                "key": col.get("COLUMN_KEY") or "",
                "comment": str(col.get("COLUMN_COMMENT") or col.get("comment") or "").strip(),
            }
            for col in table.get("columns", [])
        ]
        physical = {str(col["name"]) for col in columns if col.get("name")}
        for item in computed_by_table.get(name) or []:
            if str(item.get("name")) in physical:
                continue
            columns.append(
                {
                    "name": str(item.get("name")),
                    "type": "calculated",
                    "nullable": "",
                    "default": None,
                    "key": "",
                    # 计算列的"注释"就是 MDL 的描述——后面 _decorate 会用它生成 label
                    "comment": str(item.get("description") or ""),
                    "source": "mdl_calculated",
                    "model": str(item.get("model") or ""),
                }
            )
        entry: dict[str, Any] = {
            "table": name,
            "comment": str(table.get("comment") or "").strip(),
            "columns": columns,
        }
        if include_rows:
            quoted = f"`{name}`" if kind == "mysql" else f'"{name}"'
            try:
                rows = db_manager.query(kind, f"SELECT COUNT(*) AS c FROM {quoted}")["rows"]
                entry["rows"] = rows[0]["c"] if rows else None
            except Exception:
                entry["rows"] = None
        tables.append(entry)

    return {
        "kind": kind,
        "database": database,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "table_count": len(tables),
        "column_count": sum(len(item["columns"]) for item in tables),
        "tables": tables,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 字段目录（docs/通用模板扩充与全字段明细方案.md 第一节）
#   label（中文名）+ code_map（编码→中文）+ 字段角色 + 外键 JOIN 方式
#   通用明细/通用算子都从这里取列，不再各处硬编码中文名。
# ─────────────────────────────────────────────────────────────────────────────

_CODE_PAIR = re.compile(r"(\d+)\s*[.、:：]?\s*([\u4e00-\u9fa5]{1,12})")
# 目录缓存有效期 / 读库失败后的重试间隔都走 .env（FIELD_CATALOG_TTL_SECONDS /
# FIELD_CATALOG_RETRY_SECONDS）；以前这里写死 300，.env 里的 600 根本没生效。
# 缓存值 = (到期时间戳, 目录)
_catalog_cache: dict[str, tuple[float, dict[str, Any]]] = {}

# 外键显示值：列 → (目标表, 目标列, 连接列)
_FOREIGN_KEYS: dict[str, tuple[str, str, str]] = {
    "routes_id": ("basic_facilities_routes", "short_name", "id"),
    "route_id": ("basic_facilities_routes", "short_name", "id"),
    "belong_route_id": ("basic_facilities_routes", "short_name", "id"),
    "sections_id": ("basic_facilities_sections", "full_name", "id"),
    "section_id": ("basic_facilities_sections", "full_name", "id"),
    "belong_section_id": ("basic_facilities_sections", "full_name", "id"),
    "problem_base_id": ("ai_problem_base", "problem_type", "id"),
    "asset_details_id": ("ai_asset_base", "asset_type", "id"),
    "patrol_id": ("ai_maintain_patrol", "patrol_number", "id"),
}
# 图片列**不写死清单**：库里 26 个存图片路径的列（病害图/路段封面/资产样图…），现在大多是空的
# （实测 basic_facilities_sections.picture_cover 只有 2/5840、routes 的 0/3627），以后填了数据
# 就得能查到，所以按**列名 + 注释**推导，而不是维护一张会过期的名单。
#   - 命中：列名含 picture/image/icon/photo/cover/avatar，或注释含 图片/照片/封面/图标/样图；
#   - 排除：位置/文件/轨迹（pic_position、file_name、plan_path、estimate_file…）——这些也常带
#     "图"字（"病害在图片的位置，json格式"）或叫 file，但存的是坐标/文件名/文档，不是图片路径。
_IMAGE_NAME_HINTS = ("picture", "image", "icon", "photo", "cover", "avatar")
_IMAGE_COMMENT_HINTS = ("图片", "照片", "封面", "图标", "样图")
_IMAGE_EXCLUDE_HINTS = ("position", "json", "_path", "path_", "_file", "file_")
_GEO_COLUMNS = {"lng", "lat", "start_lnt", "start_lat", "end_lnt", "end_lat", "lon"}
_TIME_HINTS = ("time", "date", "_at")


def _is_image_column(name: str, comment: str, ctype: str = "") -> bool:
    """这一列是不是"存图片路径"的列（判据见 _IMAGE_NAME_HINTS 上面的注释）。"""
    low = str(name or "").lower()
    if any(hint in low for hint in _IMAGE_EXCLUDE_HINTS):
        return False
    if any(hint in low for hint in _IMAGE_NAME_HINTS):
        return True
    if "json" in low or "json" in str(ctype or "").lower():
        return False
    return any(hint in str(comment or "") for hint in _IMAGE_COMMENT_HINTS)


def _code_map_from_comment(comment: str) -> dict[str, str]:
    """从注释里抠"0=待转工单 / 5.车辆巡检"这类编码映射。"""
    pairs = _CODE_PAIR.findall(comment or "")
    if len(pairs) < 2:
        return {}
    out: dict[str, str] = {}
    for code, label in pairs:
        out.setdefault(code, label)
    return out


def _label_from_comment(comment: str, name: str) -> str:
    text = str(comment or "").strip()
    if not text:
        return name
    head = re.split(r"[:：，,(（]|\d+\s*[.、]", text)[0].strip()
    return (head or name)[:24]


# ── 极简注释列的确定性补全 ────────────────────────────────────────────────
# 有些列的注释只剩一个单位字（实测桩点表 `meter` 的注释就是"米"）。单字标签在字段触达里
# 两头都拿不到分：`_phrase_hit` 要求标签长度 ≥2，词元重合又要求问题里出现同一个字——
# 用户说"长度/米数"，这一列永远不会被认出来。这里**按列名做确定性的词素映射**补全标签
# （不引入业务关键词表，也不改库），补出来是"里程桩点米数"这种能参与匹配的名字。
_TINY_LABEL_WORDS = (
    "米", "千米", "公里", "吨", "个", "次", "处", "条", "座", "根", "%", "天", "小时",
    "元", "年", "月", "日", "秒", "分", "升", "千克", "米数", "数量",
)
_COLUMN_TERM_CN = (
    ("meter", "米数"),
    ("distance", "距离"),
    ("length", "长度"),
    ("width", "宽度"),
    ("height", "高度"),
    ("pile", "桩号"),
    ("area", "面积"),
    ("count", "数量"),
    ("code", "编码"),
    ("name", "名称"),
    ("time", "时间"),
    ("date", "日期"),
    ("level", "等级"),
    ("type", "类型"),
    ("num", "编号"),
)


def _enrich_tiny_label(table_label: str, column_name: str, label: str) -> str:
    """标签太短（只有一个单位字）时，补成"表业务名 + 列名语义"；否则原样返回。"""
    text = re.sub(r"[\s.,，。:：;；()（）]", "", str(label or ""))
    if len(text) >= 2 and text not in _TINY_LABEL_WORDS:
        return label
    low = str(column_name or "").lower()
    base = next((cn for token, cn in _COLUMN_TERM_CN if token in low), "")
    if not base:
        base = str(label or "").strip() or str(column_name or "")
    # 表业务名去掉"台账/表/明细"后缀，免得拼出"路段台账米数"这种别扭的名字
    prefix = re.sub(r"(台账|表|明细)$", "", str(table_label or "").strip())
    return f"{prefix}{base}" if prefix else base


def _overrides() -> dict[str, Any]:
    """字段元数据的静态覆盖层（docs/代码结构重构与模板外置方案.md §5.4）。

    事实源是库里的 COLUMN_COMMENT，这里只写例外；文件放在项目根 `resources/field_meta/`。
    老的 `backend/app/data/` 位置仍兼容（迁移期）。
    """
    from .resource_loader import load_capability_file

    data = load_capability_file("field_overrides.yaml", {})
    return data if isinstance(data, dict) else {}


def _decorate(kind: str, live: dict[str, Any]) -> dict[str, Any]:
    """给实时结构补上 label / code_map / 角色 / 外键，成为"字段目录"。"""
    raw_overrides = _overrides().get(kind) or {}
    table_overrides = raw_overrides.get("tables") or {}
    overrides = raw_overrides.get("columns") or {}
    tables: list[dict[str, Any]] = []
    for table in live.get("tables") or []:
        name = str(table.get("table"))
        table_label = str(
            (table_overrides.get(name) or {}).get("label")
            or table.get("comment")
            or name
        )
        columns: list[dict[str, Any]] = []
        for col in table.get("columns") or []:
            column_name = str(col.get("name") or "")
            if not column_name:
                continue
            comment = str(col.get("comment") or "")
            override = (overrides.get(name) or {}).get(column_name) or {}
            label = str(override.get("label") or _label_from_comment(comment, column_name))
            # 极简注释列（注释就一个"米"字）补成"表业务名 + 列名语义"，否则它在字段触达里
            # 永远拿不到分（_phrase_hit 要求标签长度 ≥2）。人工 override 的 label 不动。
            if not override.get("label"):
                label = _enrich_tiny_label(table_label, column_name, label)
            ctype = str(col.get("type") or "").lower()
            code_map = dict(override.get("code_map") or _code_map_from_comment(comment) or {})
            # 列别名（口语说法）：库里的列注释是"病害类型"，用户常说"病害名称"——
            # 注释匹配不上这类说法，点名的字段整列丢掉（实测"病害代码对应的病害名称"）。
            aliases = [str(item) for item in override.get("aliases") or [] if str(item).strip()]
            fk = _FOREIGN_KEYS.get(column_name)
            roles: list[str] = []
            if str(col.get("key") or "").upper() == "PRI" or column_name == "id":
                roles.append("primary")
            if column_name == "del_flag":
                roles.append("soft_delete")
            if any(hint in column_name for hint in _TIME_HINTS) or "datetime" in ctype:
                roles.append("time")
            if any(key in ctype for key in ("int", "decimal", "double", "float", "numeric")):
                roles.append("numeric")
            if code_map:
                roles.append("code")
            if _is_image_column(column_name, comment, ctype):
                roles.append("image")
            if column_name in _GEO_COLUMNS:
                roles.append("geo")
            if fk:
                roles.append("fk")
            columns.append(
                {
                    "name": column_name,
                    "label": label,
                    "type": ctype,
                    "comment": comment,
                    "code_map": code_map,
                    "aliases": aliases,
                    "roles": roles,
                    "join": {"table": fk[0], "column": fk[1], "on": fk[2]} if fk else None,
                    # 计算列（来自 MDL）标出来：下游据此知道"这列物理表里没有、要交给 Wren 算"，
                    # 也不会把它当"停用列"（docs/派生指标与跨表字段语义可达方案.md §6.2）
                    **(
                        {"source": str(col.get("source")), "model": str(col.get("model") or "")}
                        if col.get("source")
                        else {}
                    ),
                }
            )
        tables.append(
            {
                "table": name,
                "label": str(
                    (table_overrides.get(name) or {}).get("label")
                    or table.get("comment")
                    or name
                ),
                "comment": str(table.get("comment") or ""),
                "columns": columns,
            }
        )
    return {
        "kind": kind,
        "database": live.get("database"),
        "generated_at": live.get("generated_at"),
        "tables": tables,
    }


def field_catalog(kind: str = "mysql", force: bool = False) -> dict[str, Any]:
    """字段目录（带 5 分钟缓存；结构变了可 force 刷新）。

    读库失败的处理（以前是个坑）：原来失败也把**空目录**写进缓存、钉 5 分钟，于是字段白名单
    整段变空——`lat/lng` 会被判成"目录里没有这个字段"，"如实回答"就会误报"库里查不到"。现在：
      · 先退回本地快照 `data/schema_catalog.json`（`build_catalog()` 生成，clarify 也用它兜底），
        口径不会变空；
      · 快照也没有才给空目录，而且只短缓存 `FIELD_CATALOG_RETRY_SECONDS` 秒就重试，
        不会把空结果钉住 5 分钟。
    """
    now = time.time()
    cached = _catalog_cache.get(kind)
    if cached and not force and now < cached[0]:
        return cached[1]
    try:
        live = live_catalog(kind, include_rows=False)
        catalog = _decorate(kind, live)
    except Exception as exc:  # noqa: BLE001
        log_error("schema_catalog", "field_catalog_failed", exc, kind=kind)
        snapshot, generated_at = _catalog_from_snapshot(kind)
        if snapshot:
            log_event(
                "schema_catalog",
                "WARNING",
                "field_catalog_stale_snapshot",
                kind=kind,
                generated_at=generated_at,
                tables=len(snapshot.get("tables") or []),
            )
        catalog = snapshot or {"kind": kind, "tables": []}
        retry = float(getattr(settings, "field_catalog_retry_seconds", 15.0) or 15.0)
        _catalog_cache[kind] = (now + max(retry, 1.0), catalog)
        return catalog
    ttl = float(getattr(settings, "field_catalog_ttl_seconds", 600.0) or 600.0)
    _catalog_cache[kind] = (now + max(ttl, 1.0), catalog)
    return catalog


def _catalog_from_snapshot(kind: str) -> tuple[dict[str, Any], str]:
    """读库失败时用本地快照兜底：返回 (目录, 快照时间)；没有快照就返回空目录。"""
    path = settings.data_dir / "schema_catalog.json"
    if not path.exists():
        return {"kind": kind, "tables": []}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        log_error("schema_catalog", "catalog_snapshot_parse_failed", exc, file=str(path))
        return {"kind": kind, "tables": []}, ""
    raw = [item for item in (data.get(kind) or []) if isinstance(item, dict)]
    if not raw:
        return {"kind": kind, "tables": []}, str(data.get("generated_at") or "")
    allowed = set(
        settings.mysql_allowed_tables if kind == "mysql" else settings.pg_allowed_tables
    )
    database = settings.mysql_database if kind == "mysql" else settings.pg_database
    live = _shape_catalog(kind, raw, database, allowed, include_rows=False)
    return _decorate(kind, live), str(data.get("generated_at") or "")


def table_entry(kind: str, table: str) -> dict[str, Any] | None:
    for item in field_catalog(kind).get("tables") or []:
        if str(item.get("table")) == table:
            return item
    return None


def image_columns(kind: str, table: str) -> list[dict[str, Any]]:
    """这张表里"存图片路径"的列（按目录里的 role=image），没有就返回空。

    调用方（图片题筛选、图片路径识别、看图模板）统一走这里，不再各自维护列名单。
    """
    entry = table_entry(kind, table) or {}
    return [
        col for col in entry.get("columns") or [] if "image" in (col.get("roles") or [])
    ]


def image_column_names(kind: str) -> set[str]:
    """这个库里所有图片列的名字（给"从结果行里认图片路径"用）。"""
    names: set[str] = set()
    for item in field_catalog(kind).get("tables") or []:
        names.update(
            str(col.get("name"))
            for col in item.get("columns") or []
            if "image" in (col.get("roles") or [])
        )
    return names


def resolve_column(kind: str, table: str, name_or_label: str) -> dict[str, Any] | None:
    """按物理名或中文名找列；找不到返回 None（交给 Wren，不猜）。

    "退一步做包含匹配"只给**中文标签**用（"收藏标记" → "收藏"）。物理名一律只精确匹配：
    `routes_id` 这种带下划线的名字如果参与包含匹配，会被兜到主键 `id` 上（`id` 是
    `routes_id` 的子串），于是生成 `m.id = '路线id'` 这种必然 0 行的 SQL
    （docs/通用算子路线条件误绑主键修复方案.md P0-3）。
    """
    entry = table_entry(kind, table)
    if not entry:
        return None
    wanted = str(name_or_label or "").strip().lower()
    if not wanted:
        return None
    for col in entry.get("columns") or []:
        if str(col.get("name")).lower() == wanted:
            return col
    for col in entry.get("columns") or []:
        if str(col.get("label")).lower() == wanted:
            return col
    # 物理名（纯 ASCII/下划线）不再做包含匹配
    if _PHYSICAL_NAME_RE.match(wanted):
        return None
    # 退一步做包含匹配（"收藏标记" → "收藏"）：主键和通用短名列不参与
    for col in entry.get("columns") or []:
        label = str(col.get("label") or "")
        if len(label) < 2 or label.lower() in _GENERIC_COLUMN_NAMES:
            continue
        if "primary" in (col.get("roles") or []):
            continue
        if label and (label in name_or_label or name_or_label in label):
            return col
    return None


_PHYSICAL_NAME_RE = re.compile(r"^[a-z0-9_]+$")
# 这些短名做包含匹配太容易误伤（"id" 是几乎所有 xxx_id 的子串）
_GENERIC_COLUMN_NAMES = {"id", "name", "code", "sort", "type", "status", "remark", "memo", "value"}


def relation_path(
    kind: str,
    table: str,
    column_names: tuple[str, ...] = (),
    target_table: str = "",
    max_depth: int = 3,
) -> tuple[str, list[dict[str, str]]] | None:
    """从 `table` 出发沿字段目录里的外键关系，找"该按哪一列过滤"。

    返回 `(列名, joins)`：joins 按从主表向外依次拼接，每项是
    `{"table": 目标表, "on": 本表(或上一跳)列, "to": 目标表列}`；主表自己就有这一列时 joins 为空。

    只用目录里已有的外键信息推导，不写死"表 + 列"的组合：
    - `ai_problem.routes_id` → 一层就命中（joins=[]）；
    - `basic_facilities_sections_pile_detail` 没有路线列 → 走
      `sections_id → basic_facilities_sections.route_id`，返回 joins 两跳、落点列 route_id；
    - 到不了目标（表之间没有外键链）→ None，调用方必须弃权，不许猜主键。
    """
    entry = table_entry(kind, table)
    if not entry:
        return None
    wanted = {str(name).lower() for name in column_names if str(name or "").strip()}
    if target_table and str(table) == str(target_table):
        return "id", []
    queue: list[tuple[str, list[dict[str, str]]]] = [(str(table), [])]
    seen = {str(table)}
    depth = 0
    while queue and depth <= max_depth:
        next_queue: list[tuple[str, list[dict[str, str]]]] = []
        for node, path in queue:
            node_entry = table_entry(kind, node)
            if not node_entry:
                continue
            for column in node_entry.get("columns") or []:
                name = str(column.get("name") or "")
                if wanted and name.lower() in wanted:
                    return name, path
                join = column.get("join") or {}
                join_table = str(join.get("table") or "")
                if not join_table:
                    continue
                if target_table and join_table == str(target_table):
                    return name, path
                if join_table in seen:
                    continue
                seen.add(join_table)
                next_queue.append(
                    (
                        join_table,
                        path + [{"table": join_table, "on": name, "to": str(join.get("on") or "id")}],
                    )
                )
        queue = next_queue
        depth += 1
    return None


def resolve_table(kind: str, name_or_label: str) -> str | None:
    wanted = str(name_or_label or "").strip().lower()
    if not wanted:
        return None
    for item in field_catalog(kind).get("tables") or []:
        table = str(item.get("table"))
        if table.lower() == wanted or str(item.get("label") or "").lower() == wanted:
            return table
    for item in field_catalog(kind).get("tables") or []:
        if wanted and (wanted in str(item.get("label") or "").lower() or wanted in str(item.get("table")).lower()):
            return str(item.get("table"))
    return None


def table_label(kind: str, table: str) -> str:
    entry = table_entry(kind, table)
    return str((entry or {}).get("label") or table)
