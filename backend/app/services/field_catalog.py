"""字段目录与取值采样。

从 `nl2sql.py` 拆出（docs/代码走查与目录结构重构方案.md 第三节），**纯搬移**：
`nl2sql.py` 里按原名 re-export，`nl2sql._field_rows()` 这类调用不受影响。

为什么这三块一起搬：它们互相咬合——`_field_rows()` 用 `_sampled_hint()` 补取值提示，
`_sampled_hint()` 又用 `value_samples()`，`value_samples()` 用 `_SAMPLE_COLUMNS`；
分开搬反而要互相 import。

数据来源单一：库里有什么列、什么类型、注释写了什么，全部从 information_schema
（经 `schema_catalog.live_catalog`）读，代码里不再手写字段枚举。
"""

from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Any

from ..config import settings
from .db import db_manager
from .logging_service import log_error


# 枚举自动采样（报告第三章第 2/3 条）：注释里没写 code→中文的字段，直接看库里取哪些值。
# 只在**当前问题真的用到这一列**时才采一次（按列缓存），不给每个问题都跑一堆 DISTINCT。
_CODEISH_COLUMN = re.compile(
    r"(status|type|flag|source|method|level|grade|up_or_down|valid|region|categories)$", re.I
)
_SAMPLED_HINT_CACHE: dict[str, str] = {}


def _sampled_hint(field: str) -> str:
    row = next((item for item in _field_rows() if item.get("name") == field), None)
    if not row:
        return ""
    column = str(row.get("name") or "")
    table = str(row.get("table") or "")
    if not table or not _CODEISH_COLUMN.search(column):
        return ""
    if field in _SAMPLED_HINT_CACHE:
        return _SAMPLED_HINT_CACHE[field]
    hint = ""
    try:
        rows = db_manager.query(
            "mysql",
            f"SELECT DISTINCT `{column}` AS v FROM `{table}` "
            f"WHERE `{column}` IS NOT NULL AND `{column}` <> '' LIMIT 13",
        )["rows"]
        values = [str(item.get("v")) for item in rows if str(item.get("v") or "").strip()]
        if values and len(values) <= 12:
            hint = "库里取值为：" + "、".join(values)
    except Exception:  # noqa: BLE001
        hint = ""
    _SAMPLED_HINT_CACHE[field] = hint
    return hint


# 列名/取值缓存（实时结构来自 schema_catalog，进程内缓存，避免每题都查库）
_CATALOG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_SAMPLE_CACHE: dict[str, tuple[float, str]] = {}
_CATALOG_TTL = settings.field_catalog_ttl_seconds
_SAMPLE_TTL = settings.field_catalog_ttl_seconds

# 采样哪几列的取值：这些列的值（路线名/病害类型/巡查类型…）是模型最容易猜错的
_SAMPLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("basic_facilities_routes", "short_name"),
    ("ai_problem_base", "problem_type"),
    ("ai_maintain_patrol", "patrol_type"),
    ("basic_facilities_sections", "tech_grade"),
    ("basic_facilities_sections", "surface_type"),
)


def _catalog(kind: str) -> dict[str, Any]:
    """实时表结构（带 TTL 缓存）。"""
    import time

    cached = _CATALOG_CACHE.get(kind)
    if cached and time.time() - cached[0] < _CATALOG_TTL:
        return cached[1]
    from .schema_catalog import live_catalog

    try:
        catalog = live_catalog(kind)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "catalog_failed", exc, kind=kind)
        catalog = {"tables": []}
    _CATALOG_CACHE[kind] = (time.time(), catalog)
    return catalog


# ── 字段登记（从实时表结构生成，禁止再手写字段枚举）─────────────────────────────
# 以前这里有几张手写的表：字段→SQL表达式、字段→中文名、code→中文、字段别名、模板安全字段…
# 每加一列就要改四五处代码，而且和 MDL/库结构两份数据必然漂移。
# 现在：**库里有什么列、什么类型、注释里写了什么，就从 information_schema 读**。
# 代码里只保留"物理表里查不到"的语义字段（JSON 里的置信度、join 出来的中文名…）。
# ── pic_position（JSON 列）里的字段（docs/pic_position_JSON字段全量支持方案.md）──────
# 字段名: (JSON 键, 是否数值, 中文名, DECIMAL 精度)
# **统一带 pic_ 前缀**（confidence 是历史名字，保持不变）：JSON 里的 type/lat/lng 与物理列
# damage_type/lat/lng 是**两回事**——用户说"经纬度/位置"指的是病害坐标（表列 lng/lat），
# 说"标注/图片上的/标注框"才是 JSON 里的 pic_lng/pic_lat。同名会让这两件事打架（用户明确要求）。
PIC_JSON_FIELDS: dict[str, tuple[str, bool, str, str]] = {
    "confidence": ("confidence", True, "AI 置信度", "DECIMAL(10,6)"),
    "pic_type": ("type", False, "图片标注病害类型", ""),
    "pic_width": ("width", True, "图片标注宽度（米）", "DECIMAL(10,2)"),
    "pic_height": ("height", True, "图片标注高度（米）", "DECIMAL(10,2)"),
    "pic_lng": ("lng", True, "图片标注经度", "DECIMAL(12,8)"),
    "pic_lat": ("lat", True, "图片标注纬度", "DECIMAL(12,8)"),
    "pic_x0": ("x0", True, "标注框左上角X", "DECIMAL(10,2)"),
    "pic_x1": ("x1", True, "标注框右下角X", "DECIMAL(10,2)"),
    "pic_y0": ("y0", True, "标注框左上角Y", "DECIMAL(10,2)"),
    "pic_y1": ("y1", True, "标注框右下角Y", "DECIMAL(10,2)"),
}


def pic_json_expr(json_key: str, numeric: bool, cast: str = "") -> str:
    """pic_position 里某个键的 SQL 表达式（数值键 CAST 成 DECIMAL 才能比较/排序）。

    JSON 里存的是**字符串**（`"2.32"`），不 CAST 的话 `> 1` 会按字符串比，结果错。
    """
    inner = f"JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.{json_key}'))"
    return f"CAST({inner} AS {cast})" if numeric and cast else inner


_SPECIAL_FIELDS: dict[str, tuple[str, bool, str]] = {
    **{
        name: (pic_json_expr(key, numeric, cast), numeric, label)
        for name, (key, numeric, label, cast) in PIC_JSON_FIELDS.items()
    },
    # 字段名: (SQL 表达式, 是否数值, 中文名)
    "problem_type_name": ("b.problem_type", False, "病害类型"),
    "route_name": ("r.short_name", False, "路线名"),
    "task_number": ("t.task_number", False, "巡检任务编号"),
    "route_level": ("r.level", False, "路线行政等级"),
    "effective_disease_length": (
        "CASE WHEN p.disease_length IS NOT NULL AND p.disease_length > 0 "
        "THEN p.disease_length ELSE GREATEST(COALESCE(p.area_height, 0), "
        "COALESCE(p.area_width, 0)) END",
        True,
        "病害长度",
    ),
    "lng_lat": ("CONCAT(p.lng, ',', p.lat)", False, "经纬度"),
    "image": (
        "CASE WHEN p.problem_url IS NOT NULL AND p.problem_url <> '' THEN 1 ELSE 0 END",
        True,
        "有无图片",
    ),
    # has_image：和 image 同一个事实，但用"虚拟字段"的名字登记——
    # 语义层抽"没采集图片/有现场照片"时按 has_image=0/1 给条件，物理表达式在这里钉死
    # 用 problem_url（原始采集图），不跟 preview_url（画框缓存图）混
    # （docs/复杂模板匹配与推荐改造方案.md P0-1.3）。
    "has_image": (
        "CASE WHEN p.problem_url IS NOT NULL AND p.problem_url <> '' THEN 1 ELSE 0 END",
        True,
        "有无采集图片",
    ),
    "problem_id": ("p.id", False, "病害id"),
}
# 语义字段需要的 JOIN（列在表里不存在，必须显式关联）
_SPECIAL_FIELD_JOINS: dict[str, str] = {
    "problem_type_name": (
        "LEFT JOIN (SELECT DISTINCT id, problem_type FROM ai_problem_base "
        "WHERE problem_type IS NOT NULL "
        "AND (del_flag = 0 OR del_flag IS NULL)) b ON p.problem_base_id = b.id"
    ),
    "route_name": "JOIN basic_facilities_routes r ON p.routes_id = r.id",
    "task_number": "JOIN ai_maintain_patrol t ON p.patrol_id = t.id",
    "route_level": "JOIN basic_facilities_routes r ON p.routes_id = r.id",
}
# 表 → SQL 别名（和提示词里的约定一致）；没列到的表用物理表名限定
_TABLE_ALIASES: dict[str, str] = {
    "ai_problem": "p",
    "basic_facilities_routes": "r",
    "basic_facilities_sections": "s",
    "ai_maintain_patrol": "t",
    "ai_problem_base": "b",
}
_FIELD_TABLE_SKIP = {"id", "del_flag", "tenant_id", "create_by", "update_by"}


_COLUMN_LABEL_OVERRIDES = {
    ("basic_facilities_sections", "track_mileage"): "轨迹实测里程",
}


def _column_label(comment: str, name: str, table: str = "") -> str:
    """列注释里的中文名：`病害状态: 0待转工单,10已转工单` → 病害状态。"""
    override = _COLUMN_LABEL_OVERRIDES.get((str(table), str(name)))
    if override:
        return override
    text = str(comment or "").strip()
    if not text:
        return name
    head = re.split(r"[:：，,(（]|\d+\s*[.、]", text)[0].strip()
    return (head or name)[:20]


def _column_value_hint(comment: str) -> str:
    """列注释里的 code→中文 取值说明（库里本来就写了，不该再手抄一份）。"""
    text = str(comment or "")
    pairs = re.findall(r"(\d+)\s*[.、]?\s*([\u4e00-\u9fa5]{1,12})", text)
    if len(pairs) >= 2:
        return "，".join(f"{code}={label}" for code, label in pairs[:8])
    matched = re.search(r"(\d+)\s*[=:：]\s*([\u4e00-\u9fa5]{2,12})", text)
    if matched and "/" in text:
        return "，".join(f"{code}={label}" for code, label in re.findall(r"(\d+)\s*[=:：]\s*([\u4e00-\u9fa5]{2,12})", text)[:8])
    return ""


@lru_cache(maxsize=4)
def _field_rows(kind: str = "mysql") -> tuple[dict[str, Any], ...]:
    """字段登记原始行：真实列（自动）+ 语义字段（少量手写）。"""
    rows: list[dict[str, Any]] = []
    # 先登记 ai_problem（同名冲突时它优先），再登记其余白名单表：
    # 实测冷门字段（route.lane_count / section.tech_grade / patrol.valid_task）在物理兜底
    # 路径里没有登记，模型想引用也引用不到（P2-2）。
    allowed = list(settings.mysql_allowed_tables) if kind == "mysql" else list(settings.pg_allowed_tables)
    ordered = ["ai_problem"] + [name for name in allowed if name != "ai_problem"]
    # 注意：必须**按 ordered 的顺序**遍历，不能按表结构返回的顺序。
    # 以前按结构顺序遍历 + "同名只在第一次出现时登记"，`up_or_down / lng / lat /
    # luge_name / mission_flag / preview_url` 这些 ai_problem 自带的列被 ai_asset_main /
    # ai_distance_capture 抢走，于是病害查询里"上行方向"这类条件落到了资产表上
    # （docs/复杂模板匹配与推荐改造方案.md R2）。
    catalog_tables = {
        str(table.get("table") or ""): table for table in _catalog(kind).get("tables") or []
    }
    seen: set[str] = set()
    for table_name in ordered:
        table = catalog_tables.get(table_name)
        if not table:
            continue
        alias = _TABLE_ALIASES.get(table_name, "")
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            if not name or name in _FIELD_TABLE_SKIP:
                continue
            if name in seen:
                # 同名列表（lane_count 在 route 和 section 都有）：把另一种归属记到已有行上，
                # 给模型的说明里会写成"路线级 r.lane_count / 路段级 s.lane_count"，
                # 否则模型不知道该用哪张表，只能反问问用户（实测）。
                prefix_alt = f"{alias}." if alias else f"{table_name}."
                for row in rows:
                    if row["name"] == name:
                        row.setdefault("alts", []).append(f"{prefix_alt}{name}")
                        break
                continue
            ctype = str(column.get("type") or "").lower()
            seen.add(name)
            prefix = f"{alias}." if alias else f"{table_name}."
            rows.append(
                {
                    "name": name,
                    "expr": f"{prefix}{name}",
                    "numeric": any(key in ctype for key in ("int", "decimal", "double", "float")),
                    "type": ctype,
                    "table": table_name,
                    "label": _column_label(
                        str(column.get("comment") or ""), name, table_name
                    ),
                    "alts": [],
                    "hint": _column_value_hint(str(column.get("comment") or "")),
                    "join": "",
                }
            )
    for name, (expr, numeric, label) in _SPECIAL_FIELDS.items():
        rows.append(
            {
                "name": name,
                "expr": expr,
                "numeric": numeric,
                "label": label,
                "hint": "",
                "join": _SPECIAL_FIELD_JOINS.get(name, ""),
                "type": "datetime",
                # 语义字段都挂在病害表上（表达式里的 p./b./r./t. 都是 ai_problem 的别名）。
                # 标上归属表很关键：`confidence` 这种名字在字典表里也有同名列（"置信度阈值"），
                # 没有归属表时按名字解析会被字典抢走 → "病害图片的置信度"被拆成两张表
                # （实测被当成跨表多字段题）。
                "table": "ai_problem",
            }
        )
    return tuple(rows)


class _LazyFieldTable(dict):
    """按需生成的字段表：第一次访问时从表结构读，之后就是普通 dict。"""

    def __init__(self, builder):
        super().__init__()
        self._builder = builder
        self._loaded = False

    def _ensure(self) -> None:
        if not self._loaded:
            self._loaded = True
            try:
                super().update(self._builder())
            except Exception as exc:  # noqa: BLE001
                log_error("nl2sql", "field_table_failed", exc)

    def __contains__(self, key):  # type: ignore[override]
        self._ensure()
        return super().__contains__(key)

    def __getitem__(self, key):  # type: ignore[override]
        self._ensure()
        return super().__getitem__(key)

    def __iter__(self):  # type: ignore[override]
        self._ensure()
        return super().__iter__()

    def get(self, key, default=None):  # type: ignore[override]
        self._ensure()
        return super().get(key, default)

    def items(self):  # type: ignore[override]
        self._ensure()
        return super().items()

    def keys(self):  # type: ignore[override]
        self._ensure()
        return super().keys()

    def values(self):  # type: ignore[override]
        self._ensure()
        return super().values()


_CONDITION_FIELDS: dict[str, tuple[str, bool]] = _LazyFieldTable(
    lambda: {row["name"]: (row["expr"], row["numeric"]) for row in _field_rows()}
)
_CONDITION_LABELS: dict[str, str] = _LazyFieldTable(
    lambda: {row["name"]: row["label"] for row in _field_rows()}
)
_CONDITION_VALUE_HINTS: dict[str, str] = _LazyFieldTable(
    lambda: {row["name"]: row["hint"] for row in _field_rows() if row["hint"]}
)
_SELECT_FIELD_EXPRS: dict[str, str] = _LazyFieldTable(
    lambda: {row["name"]: row["expr"] for row in _field_rows()}
)
_SELECT_FIELD_JOINS: dict[str, str] = _LazyFieldTable(
    lambda: {row["name"]: row["join"] for row in _field_rows() if row["join"]}
)
_FIELD_TYPES: dict[str, str] = _LazyFieldTable(
    lambda: {row["name"]: str(row.get("type") or "") for row in _field_rows()}
)
# 中文名 → 列名（"巡查编号"→patrol_number）：从列注释自动建立，不再手写别名表
_ENTITY_FIELD_ALIASES: dict[str, str] = _LazyFieldTable(
    lambda: {
        row["label"]: row["name"]
        for row in _field_rows()
        if row["label"] and row["label"] != row["name"]
    }
)


def value_samples(kind: str = "mysql") -> str:
    """字段实际取值样例——相当于让模型"先查一眼再写 SQL"（升级6 工具调用的廉价版）。

    模型最容易猜错的就是这些列的取值范围（病害类型叫"泛油"、巡查类型是 0/5/10 这种代码），
    把真实值给它，比让它瞎猜准得多。
    """
    import time

    cached = _SAMPLE_CACHE.get(kind)
    if cached and time.time() - cached[0] < _SAMPLE_TTL:
        return cached[1]
    lines: list[str] = []
    for table, column in _SAMPLE_COLUMNS:
        if table not in (settings.mysql_allowed_tables if kind == "mysql" else settings.pg_allowed_tables):
            continue
        quoted = f"`{table}`" if kind == "mysql" else f'"{table}"'
        try:
            rows = db_manager.query(
                kind,
                f"SELECT DISTINCT `{column}` AS v FROM {quoted} "
                f"WHERE `{column}` IS NOT NULL AND `{column}` <> '' "
                + (
                    "AND (del_flag = 0 OR del_flag IS NULL) "
                    if table == "ai_problem_base"
                    else ""
                )
                + "LIMIT 12",
            )["rows"]
        except Exception:  # noqa: BLE001
            continue
        values = [str(row.get("v")) for row in rows if str(row.get("v") or "").strip()]
        if values:
            lines.append(f"- {table}.{column}：{'、'.join(values)}")
    text = "\n".join(lines)
    _SAMPLE_CACHE[kind] = (time.time(), text)
    return text


def field_vocabulary(
    kind: str,
    message: str = "",
    tables: list[str] | None = None,
    hits: dict[str, list[str]] | None = None,
    limit: int = 60,
) -> str:
    """给语义层看的**列清单**：按 (表, 列) 逐条给，不在 `_field_rows()` 的"按列名去重"里丢东西。

    为什么要它：`_field_rows()` 是**按列名去重**的（592 列 → 324 行，其余同名列表记成 alts），
    于是"西乡段的纬度范围"这种题，模型既不知道纬度=哪张表的哪一列，也不知道有哪些指标可选，
    只能回一个 metrics=[]（推荐卡于是被"几车道"这种路线模板硬套）。

    输出两段，都保持紧凑：
      1. 问题里出现列注释/列名的列（用户点名的）——直接告诉他表.列、中文名、是不是数值列；
      2. 候选表的其它列——让他知道这些表还有哪些指标（数值列）/维度（文本、时间列）。

    第 2 段**按候选表分配配额**：字段多的表（ai_problem 54 列）以前会把整个 limit 吃光，
    排在它后面的候选表（basic_facilities_routes…）一列都进不来——"病害状态以及内置的路线名称"
    就是因为模型看不到 routes.built，把"路线名称"猜成了 file_name。

    hits（可选）：字段触达命中的 表 → 列名。命中的列在各自表的配额里**排最前**，
    这样"内置"（列注释是"是否内置"，不是原样子串）也能把那列顶到模型眼前。
    """
    text = str(message or "")
    candidate_tables = {str(item) for item in (tables or [])}
    hit_wanted: dict[str, list[str]] = {
        str(name): [str(item) for item in (cols or [])]
        for name, cols in (hits or {}).items()
    }
    hit_lines: list[str] = []
    # 语义字段（pic_position 的 JSON 字段）**不在** information_schema 里，光靠目录列清单
    # 模型看不到它们——而"标注经度"这种问法里，"经度"会先命中表列 lng。这里把 JSON 字段
    # 也摆出来（只在提到标注/图片/框/AI 时才算"用户点到的列"，否则只作候选）。
    pic_lines: list[str] = []
    by_table: dict[str, list[tuple[str, str]]] = {}
    for table in _catalog(kind).get("tables") or []:
        table_name = str(table.get("table") or "")
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            if not name or name in _FIELD_TABLE_SKIP:
                continue
            comment = str(column.get("comment") or "")
            label = _column_label(comment, name, table_name)
            ctype = str(column.get("type") or "").lower()
            numeric = any(key in ctype for key in ("int", "decimal", "double", "float", "numeric"))
            line = f"  - {table_name}.{name}"
            if label and label != name:
                line += f"（{label}）"
            line += " · 数值列（可当指标：min/max/avg/sum）" if numeric else " · 文本/时间列（可当维度/输出字段）"
            # 列注释里写了编码取值的（"行政级别: 0.国道1.省道…"）也要给模型——不给它就只能猜，
            # 实测它把 route_level=3 解释成"县道"（实际 code_map 是 3=乡道），还自己编了
            # "1代表国道"这种映射（docs/宏观元数据查询…之外，见本轮对话）。
            hint = _column_value_hint(comment)
            if hint:
                line += f" · 取值 {hint}"
            if len(label) >= 2 and label in text:
                hit_lines.append(line)
            elif table_name in candidate_tables:
                by_table.setdefault(table_name, []).append((name, line))
    # 字段触达命中的列排在各表最前（它比"注释原样出现在句子里"宽松，且带打分排序）
    table_lines: list[tuple[str, str]] = []
    for table_name, items in by_table.items():
        wanted = hit_wanted.get(table_name) or []
        ranked = sorted(items, key=lambda pair: (pair[0] not in wanted,))
        table_lines.extend((table_name, line) for _name, line in ranked)
    out: list[str] = []
    pic_words = ("标注", "图片", "框", "AI", "识别")
    if any(word in text for word in pic_words):
        for name, (_key, numeric, label, _cast) in PIC_JSON_FIELDS.items():
            if name == "confidence":
                continue
            suffix = (
                "数值列（可当指标：min/max/avg/sum）"
                if numeric
                else "文本列（可当维度/输出字段）"
            )
            pic_lines.append(f"  - ai_problem.{name}（{label}） · {suffix}")
    if pic_lines:
        out.append(
            "pic_position（图片标注 JSON）里的字段——说「标注/图片上的/标注框」时用这些，"
            "不要用表里的同名列："
        )
        out.extend(pic_lines)
    if hit_lines:
        out.append("问题里点到的列（用户点了这些列 → 优先它们当指标/维度/输出字段）：")
        out.extend(hit_lines[:limit])
    if table_lines:
        # 每张候选表都要有代表：均分配额，至少 8 行，免得被字段最多的那张表挤掉
        per_table = max(int(limit) // max(len(candidate_tables), 1), 8)
        quota: dict[str, int] = {}
        kept: list[str] = []
        for table_name, line in table_lines:
            if quota.get(table_name, 0) >= per_table:
                continue
            quota[table_name] = quota.get(table_name, 0) + 1
            kept.append(line)
        out.append("候选表里还能用的其它列（数值列=可选指标，文本/时间列=可选维度）：")
        out.extend(kept)
    return "\n".join(out) or "（没有匹配到具体列）"


def _is_numeric_type(ctype: str) -> bool:
    text = str(ctype or "").lower()
    return any(key in text for key in ("int", "decimal", "double", "float", "numeric"))


def metric_names(kind: str = "mysql") -> set[str]:
    """语义层允许出现的**指标名**：基础口径 + 字段目录里任意数值列的 `<聚合>_<列名>`。

    为什么要从目录生成：原来这份名单是写死的 13 个（count/total_area/avg_area…），
    模型就算把"纬度范围"解析成 `min_lat/max_lat` 也会被后端白名单丢掉 → metrics 恒为空，
    推荐卡于是拿"几车道"这种模板硬套（docs/推荐卡硬推荐与指标空匹配方案.md §三）。
    命名与挖掘侧 `templatizer._metric_name` 保持一致（min/max/avg/total + 列名）。
    """
    from app.query.capability_registry import CapabilityRegistry

    registry = CapabilityRegistry.load(kind)
    return {
        metric_id
        for subject_metrics in registry.snapshot.metrics.values()
        for metric_id in subject_metrics
    }


def output_field_names(kind: str = "mysql") -> set[str]:
    """语义层允许出现的**输出字段名**：基础名单 + 字段目录里的每一个列名（全量可达）。"""
    names = {
        "problem_number",
        "pile_number",
        "area",
        "confidence",
        "collection_time",
        "route_name",
        "problem_type_name",
        "lng_lat",
        "image",
        "memo",
    }
    # pic_position 的字段全部可达（含 pic_type 这类文本字段）
    names.update(PIC_JSON_FIELDS)
    for table in _catalog(kind).get("tables") or []:
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            if name and name not in _FIELD_TABLE_SKIP:
                names.add(name)
    return names

