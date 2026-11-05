"""通用明细模板 + 通用聚合算子 + 数据画像。

见 docs/通用模板扩充与全字段明细方案.md 第二、三、四节：不再为每个字段/每种问法写模板，
表名、列名、编码值全部从 schema_catalog（库里的注释）解析；解析不了就弃权交 Wren。

安全约束（方案第七节）：表/列白名单、操作符白名单、值类型校验、只读单条 SELECT、
明细强制 LIMIT、统一补 del_flag=0，绝不把用户原文拼进 SQL。
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import schema_catalog as catalog
from ..config import settings
from .db import db_manager
from .logging_service import log_error

DETAIL_LIMIT = settings.generic_detail_limit
_OPS = {
    "=", "!=", ">", ">=", "<", "<=", "between", "in",
    "is null", "is not null", "not empty", "empty", "like",
}
_AGG = {"count", "sum", "avg", "max", "min"}
_TIME_UNITS = {"year": "%Y", "quarter": "CONCAT(YEAR({c}), 'Q', QUARTER({c}))", "month": "%Y-%m",
    # 周数统一 ISO 8601：MySQL 里 ISO 周要用 `%x-%v`（%Y-%W/%Y-%u 都是"第一个周一"那种算法，
    # 和用户说的"第 N 周"差一周，docs/周数计算模式不一致与链式复合问题执行修复方案.md §1.2）
    "week": "%x-%v", "day": "%Y-%m-%d", "hour": "%Y-%m-%d %H", "dow": None}


def _quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _is_numeric(column: dict[str, Any]) -> bool:
    return "numeric" in (column.get("roles") or [])


def _literal(column: dict[str, Any], value: Any) -> str | None:
    """按列类型 + code_map 把用户给的值翻成 SQL 字面值（判不准返回 None）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    code_map = column.get("code_map") or {}
    if code_map:
        for code, label in code_map.items():
            if text == code or text == label:
                return _quote(code) if not _is_numeric(column) else str(code)
        return None
    if _is_numeric(column):
        if not re.fullmatch(r"-?\d+(\.\d+)?", text):
            return None
        return text
    if re.search(r"['\";\\]", text):
        return None
    return _quote(text)


def _select_expr(kind: str, table: str, column: dict[str, Any], alias: str) -> str:
    """一列 → 物理 SELECT 表达式（编码列翻中文、外键列带出名称列）。

    **调用前必须先过 `_physical_column`**：MDL 计算列在物理表里没有这一列。
    """
    name = str(column["name"])
    label = str(column.get("label") or name)
    # 语义字段（pic_position 的 JSON 字段、join 出来的中文名…）带着自己的表达式：
    # 直接用表达式选，别拼成 `{alias}.{name}`（那列在物理表里根本不存在）。
    expr = str(column.get("expr") or "").strip()
    if expr:
        return f"{expr} AS {_quote(label)}"
    code_map = column.get("code_map") or {}
    if code_map:
        when = " ".join(
            f"WHEN {_quote(code)} THEN {_quote(text)}" for code, text in code_map.items()
        )
        return f"CASE {alias}.{name} {when} ELSE {alias}.{name} END AS {_quote(label)}"
    return f"{alias}.{name} AS {_quote(label)}"


def _physical_column(kind: str, table: str, name: str) -> tuple[dict[str, Any] | None, str]:
    """物理路径取列 —— **全库通用算子都走这道收口**。

    为什么单独一层：字段目录为了"认得出用户说法"，把 MDL 计算列（`section_length_km` 路段实际长度、
    `route_length_km`、`category_name` …）也列进去了；但它们在**物理表里没有这一列**，
    物理 SQL 里点它必然报 `Unknown column`（实测"路段米数"选口径后就是这个错）。
    所以物理路径一律取不到 → 返回原因让构造器弃权，交给 Wren 按模型算表达式。
    """
    column = catalog.resolve_column(kind, table, str(name or ""))
    if not column:
        return None, f"字段「{name}」在 {table} 里找不到"
    if str(column.get("source") or "") == "mdl_calculated":
        label = str(column.get("label") or name)
        return None, (
            f"「{label}」是语义层的派生列（{column.get('model') or 'MDL'}.{column.get('name')}），"
            "物理表里没有这一列，要按模型算，交给 Wren"
        )
    return column, ""


def _joins_for(
    kind: str,
    table: str,
    columns: list[dict[str, Any]],
    alias: str,
    alias_map: dict[tuple[str, str], str] | None = None,
) -> tuple[list[str], list[str]]:
    joins: list[str] = []
    extras: list[str] = []
    seen: set[str] = set()
    for index, column in enumerate(columns):
        join = column.get("join") or {}
        if not join:
            continue
        target = str(join.get("table") or "")
        if not target or target in seen:
            continue
        seen.add(target)
        alias_name = f"j{index}"
        if alias_map is not None:
            alias_map[(target, str(column["name"]))] = alias_name
        joins.append(
            f"LEFT JOIN {target} {alias_name} ON {alias}.{column['name']} = {alias_name}.{join.get('on')}"
        )
        # 带出来的名称列也必须是物理列：万一外键表映射到了 MDL 计算列，宁可不带这一列，
        # 也不能让物理 SQL 里出现一个不存在的列名。
        label_column, _reason = _physical_column(kind, target, str(join.get("column")))
        if label_column:
            extras.append(
                f"{alias_name}.{label_column['name']} AS "
                f"{_quote(str(label_column.get('label') or join.get('column')))}"
            )
    return joins, extras


def _filter_sql(
    kind: str,
    table: str,
    filters: list[dict[str, Any]],
    alias: str,
    alias_map: dict[tuple[str, str], str],
) -> tuple[list[str], list[str], str | None]:
    """把过滤项翻成 SQL。过滤列不在主表时，按目录里的关系补 JOIN（P0-2）。

    返回 (join 片段, where 片段, 失败原因)；失败原因非空时调用方必须整体弃权。
    """
    joins: list[str] = []
    where: list[str] = []
    for index, item in enumerate(filters or []):
        # 多列「非空」条件（图片题用）：一张表可能有多个图片列（picture / picture_cover /
        # installation_picture…），用户没点名具体哪一列时按 OR 组起来——只取单列会把另一列
        # 有图的行漏掉（实测路段表 picture 全空、picture_cover 才是真有封面那 2 条）。
        multi_columns = [str(name) for name in (item.get("columns") or []) if name]
        if item.get("op") == "not empty" and multi_columns:
            expressions: list[str] = []
            for name in multi_columns:
                column, reason = _physical_column(kind, table, name)
                if not column:
                    return joins, where, reason
                expr = f"{alias}.{column['name']}"
                expressions.append(f"({expr} IS NOT NULL AND {expr} <> '')")
            where.append("(" + " OR ".join(expressions) + ")")
            continue
        path = item.get("joins") or []
        current = alias
        target = table
        for step_index, step in enumerate(path):
            step_table = str(step.get("table") or "")
            source_column = str(step.get("on") or "")
            to_column = str(step.get("to") or "id")
            if not step_table or not source_column:
                return joins, where, f"过滤条件的关系链不完整：{item}"
            key = (step_table, source_column)
            join_alias = alias_map.get(key)
            if not join_alias:
                join_alias = f"f{index}_{step_index}"
                joins.append(
                    f"LEFT JOIN {step_table} {join_alias} "
                    f"ON {current}.{source_column} = {join_alias}.{to_column}"
                )
                alias_map[key] = join_alias
            current = join_alias
            target = step_table
        column, reason = _physical_column(kind, target, str(item.get("column") or ""))
        op = str(item.get("op") or "=").strip().lower()
        if not column or op not in _OPS:
            # 过滤条件落在派生列上（物理表没这列）时，用 _physical_column 给的原因说明白，
            # 别回一句"过滤条件不支持：{...}"让人看不懂
            return joins, where, reason or f"过滤条件不支持：{item}"
        if "primary" in (column.get("roles") or []) and item.get("entity"):
            # 实体条件（路线/设备…）不该落在主键上：这是"条件没接对关系"的信号，
            # 宁可弃权交 Wren，也不许硬查一个 0（P0-5）。
            return joins, where, f"实体条件「{item.get('entity')}」被解析到主键 {target}.{column['name']}"
        expr = f"{current}.{column['name']}"
        if op in {"is null", "is not null"}:
            where.append(f"{expr} IS {'NULL' if op == 'is null' else 'NOT NULL'}")
            continue
        if op == "not empty":
            # "有图片"这类"非空"条件：空串也算没有（NULL / '' 都要排除）
            where.append(f"({expr} IS NOT NULL AND {expr} <> '')")
            continue
        if op == "empty":
            # "没有图片"：NULL 和空串都算没有
            where.append(f"({expr} IS NULL OR {expr} = '')")
            continue
        value = item.get("value")
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                return joins, where, "between 需要两个值"
            low, high = (_literal(column, v) for v in value)
            if low is None or high is None:
                return joins, where, f"「{column['label']}」的取值不合法"
            where.append(f"{expr} BETWEEN {low} AND {high}")
            continue
        if op == "in":
            values = value if isinstance(value, (list, tuple)) else [value]
            literals = [_literal(column, v) for v in values]
            if not literals or any(literal is None for literal in literals):
                return joins, where, f"「{column['label']}」的取值不合法"
            where.append(f"{expr} IN ({', '.join(literals)})")
            continue
        literal = _literal(column, value)
        if literal is None:
            return joins, where, f"「{column['label']}」不支持取值「{value}」"
        where.append(f"{expr} {op if op != 'like' else 'LIKE'} {literal}")
    return joins, where, None


def generic_detail(
    kind: str,
    table: str,
    columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order_by: dict[str, Any] | None = None,
    limit: int | None = None,
) -> tuple[str | None, str]:
    """任意表 + 任意列 + 任意条件的明细查询。返回 (sql, 原因/口径)。"""
    entry = catalog.table_entry(kind, table)
    if not entry:
        return None, f"表 {table} 不在白名单"
    resolved: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for name in columns or []:
        column = catalog.resolve_column(kind, table, name)
        if not column:
            # 语义字段（confidence / lng_lat / pic_*）：不在 information_schema 里，
            # 但字段登记表里带着表达式。以前这里直接报"字段在表里找不到"→ 整条明细弃权，
            # "图片的置信度""图片的经纬度"于是只能退化成别的列（实测）。
            from . import field_catalog

            wanted = str(name or "").strip().lower()
            row = next(
                (
                    item
                    for item in field_catalog._field_rows(kind)  # noqa: SLF001
                    if str(item.get("table") or "") == table
                    and str(item.get("name") or "").strip().lower() == wanted
                ),
                None,
            )
            if row:
                column = {
                    "name": str(row.get("name")),
                    "label": str(row.get("label") or row.get("name")),
                    "expr": str(row.get("expr") or ""),
                    "code_map": {},
                    "roles": [],
                    "type": str(row.get("type") or ""),
                }
        if not column:
            return None, f"字段「{name}」在 {table} 里找不到"
        # MDL 计算列（`section_length_km` 这种派生列）**物理表里没有这一列**：物理 SQL 里点它
        # 必然报 `Unknown column`（实测"路段米数"选了口径后就是这个错）。这里弃权，让上层走 Wren——
        # Wren 按模型把计算列的表达式内联进去，算得出来。
        if str(column.get("source") or "") == "mdl_calculated":
            label = str(column.get("label") or name)
            return None, f"「{label}」是语义层的派生列（{column.get('model') or 'MDL'}.{name}），要按模型算，交给 Wren"
        # 同一列被点名多次只取一次：上游按"同义词/落点"展开时会把同一列重复塞进来
        # （实测"分别列出路段桩号和相关的修改人"生成了 6 个 m.update_by AS '修改人'），
        # SQL 能跑但列重复、结果看着像坏了。
        col_name = str(column.get("name") or "")
        if col_name and col_name in seen_names:
            continue
        seen_names.add(col_name)
        resolved.append(column)
    if not resolved:
        # 默认列：优先非空、非系统字段的前 12 个
        skip = {"del_flag", "tenant_id", "create_by", "update_by"}
        resolved = [
            col
            for col in entry.get("columns") or []
            if str(col.get("name")) not in skip and "primary" not in (col.get("roles") or [])
            # 默认列里也不能带 MDL 计算列：它不在物理表里，SELECT 出来就是 Unknown column
            and str(col.get("source") or "") != "mdl_calculated"
        ][:12]
    alias = "m"
    alias_map: dict[tuple[str, str], str] = {}
    joins, extras = _joins_for(kind, table, resolved, alias, alias_map)
    selects = [_select_expr(kind, table, col, alias) for col in resolved] + extras
    where = []
    if any(str(col.get("name")) == "del_flag" for col in entry.get("columns") or []):
        where.append(f"{alias}.del_flag = 0")
    filter_joins, filter_where, error = _filter_sql(kind, table, filters or [], alias, alias_map)
    if error:
        return None, error
    joins.extend(filter_joins)
    where.extend(filter_where)
    order_sql = ""
    if order_by and order_by.get("column"):
        column, reason = _physical_column(kind, table, str(order_by["column"]))
        if not column:
            return None, reason or f"排序字段「{order_by['column']}」找不到"
        direction = "DESC" if str(order_by.get("direction") or "desc").lower().startswith("desc") else "ASC"
        order_sql = f" ORDER BY {alias}.{column['name']} {direction}"
    sql = (
        # 多租户字典表（同一 code 每个租户一份）明细必须去重，否则"42 种病害类型"会列出 84 行
        # （docs/多租户字典去重与租户限定整改方案.md §3.2）
        f"SELECT {'DISTINCT ' if _tenant_copied(kind, table) else ''}{', '.join(selects)} "
        f"FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}{order_sql}"
        f" LIMIT {int(limit or DETAIL_LIMIT)}"
    )
    labels = "、".join(str(col.get("label")) for col in resolved[:8])
    scope = f"{catalog.table_label(kind, table)}明细；字段：{labels}"
    if filters:
        scope += "；条件：" + "、".join(
            _filter_text(item) for item in filters
        )
    return sql, scope


def generic_count(
    kind: str,
    table: str,
    filters: list[dict[str, Any]] | None = None,
) -> tuple[str | None, str]:
    """任意表 + 任意条件的计数：返回单值 COUNT(*)（P0-4）。

    "多少条 / 几处 / 一共多少" 这类问题要的是总数，不是 500 行明细——
    明细行数被 LIMIT 截断后会被摘要器当成"总数"报出去（曾经把 534 报成 500）。
    """
    entry = catalog.table_entry(kind, table)
    if not entry:
        return None, f"表 {table} 不在白名单"
    alias = "m"
    alias_map: dict[tuple[str, str], str] = {}
    joins, filter_where, error = _filter_sql(kind, table, filters or [], alias, alias_map)
    if error:
        return None, error
    where: list[str] = []
    if any(str(col.get("name")) == "del_flag" for col in entry.get("columns") or []):
        where.append(f"{alias}.del_flag = 0")
    where.extend(filter_where)
    sql = (
        f"SELECT COUNT(*) AS 记录数 FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
    )
    scope = f"{catalog.table_label(kind, table)}计数"
    if filters:
        scope += "；条件：" + "、".join(
            f"{item.get('column')}{item.get('op') or '='}{item.get('value')}" for item in filters
        )
    return sql, scope


def _tenant_copied(kind: str, table: str) -> bool:
    """这张表是不是"每个租户复制一份"的字典表（明细要 DISTINCT 去重）。

    判据只看结构：有 tenant_id 列 + 有 code 类列 +（现查一次，带缓存）总行数大于去重行数。
    """
    entry = catalog.table_entry(kind, table) or {}
    columns = {str(col.get("name")).lower() for col in entry.get("columns") or []}
    if "tenant_id" not in columns:
        return False
    code_column = "problem_code" if "problem_code" in columns else ("code" if "code" in columns else "")
    if not code_column:
        return False
    key = f"tenant_copied:{kind}:{table}:{code_column}"
    cached = _TENANT_COPIED_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < 300.0:
        return cached[1]
    risky = False
    try:
        where = " WHERE del_flag = 0" if "del_flag" in columns else ""
        row = db_manager.query(
            kind,
            f"SELECT COUNT(*) AS total, COUNT(DISTINCT `{code_column}`) AS distinct_codes "
            f"FROM `{table}`{where}",
        )["rows"][0]
        risky = int(row.get("total") or 0) > int(row.get("distinct_codes") or 0)
    except Exception as exc:  # noqa: BLE001
        log_error("generic", "tenant_copied_check_failed", exc, table=table)
    _TENANT_COPIED_CACHE[key] = (now, risky)
    return risky


_TENANT_COPIED_CACHE: dict[str, tuple[float, bool]] = {}


def _where_for(
    kind: str,
    table: str,
    filters: list[dict[str, Any]] | None,
    alias: str,
) -> tuple[list[str], list[str], str | None]:
    """统一的条件拼装入口：del_flag + 业务过滤（含过滤条件驱动的 JOIN）。

    明细、计数、分组、时间序列、TopN、空间范围**全部**走这一份——以前只有明细拼 filters，
    聚合类各自写死 `del_flag=0`，于是"2026 年 6 月全库病害"和"7 月"都答成全表 1702
    （docs/通用算子过滤丢失与自动补物理SQL修复.md §一）。
    """
    entry = catalog.table_entry(kind, table) or {}
    where: list[str] = []
    if any(str(col.get("name")) == "del_flag" for col in entry.get("columns") or []):
        where.append(f"{alias}.del_flag = 0")
    alias_map: dict[tuple[str, str], str] = {}
    joins, filter_where, error = _filter_sql(kind, table, filters or [], alias, alias_map)
    if error:
        return joins, where, error
    where.extend(filter_where)
    return joins, where, None


def _filters_note(filters: list[dict[str, Any]] | None) -> str:
    return "；条件：" + "、".join(
        _filter_text(item) for item in filters or []
    ) if filters else ""


def _filter_text(item: dict[str, Any]) -> str:
    """过滤条件的中文说明（卡片口径里给人看的那句）。"""
    names = [str(name) for name in (item.get("columns") or []) if name]
    if names:
        return f"{'、'.join(names)}{item.get('op') or '='}"
    return f"{item.get('column')}{item.get('op') or '='}{item.get('value')}"


def group_distribution(
    kind: str,
    table: str,
    column: str,
    metric: str = "count",
    filters: list[dict[str, Any]] | None = None,
):
    """任意分类列分组：类别（编码翻中文）+ 数量 + 占比。"""
    col, reason = _physical_column(kind, table, column)
    if not col:
        return None, reason or f"分组字段「{column}」找不到"
    alias = "m"
    # 外键列：JOIN 目标表，按**名称列**分组——直接按 ID 分组，用户看到的是 UUID
    # （"各条路线分别有多少座桥梁"要的是路线名，不是 routes_id）。
    join = col.get("join") or {}
    foreign_joins: list[str] = []
    if join.get("table") and join.get("column"):
        on_column = str(join.get("on") or "id")
        foreign_joins.append(
            f"LEFT JOIN {join['table']} j0 ON {alias}.{col['name']} = j0.{on_column}"
        )
        group_expr = f"j0.{join['column']}"
        group_label = str(join.get("label") or col.get("label") or col["name"])
    else:
        group_expr = _select_expr(kind, table, col, alias).split(" AS ")[0]
        group_label = str(col.get("label") or col["name"])
    agg = f"COUNT(*)" if metric == "count" else f"SUM({alias}.{metric})"
    joins, where, error = _where_for(kind, table, filters, alias)
    if error:
        return None, error
    sql = (
        f"SELECT {group_expr} AS 类别, {agg} AS 数量, "
        f"ROUND({agg} * 100.0 / NULLIF(SUM({agg}) OVER (), 0), 2) AS 占比 "
        f"FROM {table} {alias} {' '.join(foreign_joins + joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''} "
        f"GROUP BY {group_expr} ORDER BY 数量 DESC LIMIT 200"
    )
    return sql, f"按「{group_label}」分组统计数量与占比{_filters_note(filters)}"


def time_series(
    kind: str,
    table: str,
    column: str,
    unit: str = "month",
    metric: str = "count",
    filters: list[dict[str, Any]] | None = None,
):
    """任意时间列按年/季/月/周/日/时分组。"""
    col, reason = _physical_column(kind, table, column)
    if not col or unit not in _TIME_UNITS:
        return None, reason or f"时间列或粒度不支持：{column}/{unit}"
    alias = "m"
    if unit == "quarter":
        bucket = _TIME_UNITS["quarter"].replace("{c}", f"{alias}.{col['name']}")
    elif unit in {"week", "dow"}:
        bucket = (
            f"DATE_FORMAT({alias}.{col['name']}, '{_TIME_UNITS['week']}')"
            if unit == "week"
            else f"DAYOFWEEK({alias}.{col['name']})"
        )
    else:
        bucket = f"DATE_FORMAT({alias}.{col['name']}, '{_TIME_UNITS[unit]}')"
    agg = "COUNT(*)" if metric == "count" else f"SUM({alias}.{metric})"
    joins, where, error = _where_for(kind, table, filters, alias)
    if error:
        return None, error
    sql = (
        f"SELECT {bucket} AS 时间, {agg} AS 数量 FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''} "
        f"GROUP BY 时间 ORDER BY 时间"
    )
    return sql, f"按「{col.get('label')}」的{unit}粒度统计{_filters_note(filters)}"


def rank(
    kind: str,
    table: str,
    group_column: str,
    metric_column: str = "",
    agg: str = "count",
    top: int = 10,
    filters: list[dict[str, Any]] | None = None,
):
    """任意分组列 × 任意指标，取 Top N。"""
    group_col, reason = _physical_column(kind, table, group_column)
    if not group_col or agg not in _AGG:
        return None, reason or f"分组列或聚合不支持：{group_column}/{agg}"
    alias = "m"
    group_expr = _select_expr(kind, table, group_col, alias).split(" AS ")[0]
    if agg == "count" or not metric_column:
        metric_expr = "COUNT(*)"
    else:
        metric_col, metric_reason = _physical_column(kind, table, metric_column)
        if not metric_col:
            return None, metric_reason or f"指标列「{metric_column}」找不到"
        metric_expr = f"{agg.upper()}({alias}.{metric_col['name']})"
    joins, where, error = _where_for(kind, table, filters, alias)
    if error:
        return None, error
    sql = (
        f"SELECT {group_expr} AS 对象, {metric_expr} AS 指标 FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''} "
        f"GROUP BY {group_expr} ORDER BY 指标 DESC LIMIT {int(top)}"
    )
    label = catalog.resolve_column(kind, table, metric_column) if metric_column else None
    scope = (
        f"按「{group_col.get('label')}」取 {agg}({label.get('label') if label else '条数'}) 前 {top}"
        f"{_filters_note(filters)}"
    )
    return sql, scope


def column_stats(kind: str, table: str, columns: list[str] | None = None):
    """任意数值列的 count/sum/avg/max/min + 非空率。"""
    entry = catalog.table_entry(kind, table)
    if not entry:
        return None, f"表 {table} 不在白名单"
    picked = []
    for name in columns or []:
        col = catalog.resolve_column(kind, table, name)
        if col and str(col.get("source") or "") == "mdl_calculated":
            # 点名要统计派生列 → 弃权交 Wren（静默丢掉会给出"别的列的统计"这种错答案）
            return None, (
                f"「{col.get('label') or name}」是语义层的派生列"
                f"（{col.get('model') or 'MDL'}.{col.get('name')}），要按模型算，交给 Wren"
            )
        if col:
            picked.append(col)
    if not picked:
        picked = [
            c
            for c in entry.get("columns") or []
            if _is_numeric(c) and str(c.get("source") or "") != "mdl_calculated"
        ][:6]
    if not picked:
        return None, "这张表没有可统计的数值列"
    alias = "m"
    parts = []
    for col in picked:
        expr = f"{alias}.{col['name']}"
        parts.extend(
            [
                f"COUNT({expr}) AS {_quote(str(col['label']) + '非空数')}",
                f"SUM({expr}) AS {_quote(str(col['label']) + '合计')}",
                f"ROUND(AVG({expr}), 4) AS {_quote(str(col['label']) + '平均')}",
                f"MAX({expr}) AS {_quote(str(col['label']) + '最大')}",
                f"MIN({expr}) AS {_quote(str(col['label']) + '最小')}",
            ]
        )
    where = [f"{alias}.del_flag = 0"] if any(
        str(c.get("name")) == "del_flag" for c in entry.get("columns") or []
    ) else []
    sql = (
        f"SELECT COUNT(*) AS 总行数, {', '.join(parts)} FROM {table} {alias}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
    )
    return sql, "数值列统计（非空数/合计/平均/最大/最小）"


def null_completeness(kind: str, table: str, columns: list[str] | None = None):
    """任意列的空值数、空值率、填报完整率。"""
    entry = catalog.table_entry(kind, table)
    if not entry:
        return None, f"表 {table} 不在白名单"
    picked = []
    for name in columns or []:
        col = catalog.resolve_column(kind, table, name)
        if col and str(col.get("source") or "") == "mdl_calculated":
            return None, (
                f"「{col.get('label') or name}」是语义层的派生列"
                f"（{col.get('model') or 'MDL'}.{col.get('name')}），要按模型算，交给 Wren"
            )
        if col:
            picked.append(col)
    if not picked:
        skip = {"del_flag", "tenant_id", "create_by", "update_by", "id"}
        picked = [
            c
            for c in entry.get("columns") or []
            if str(c.get("name")) not in skip
            and str(c.get("source") or "") != "mdl_calculated"
        ][:20]
    alias = "m"
    is_pg = kind == "postgresql"
    parts = []
    for col in picked:
        expr = f"{alias}.{col['name']}"
        ctype = str(col.get("type") or "")
        empty = f"{expr} IS NULL"
        if not is_pg and any(key in ctype for key in ("char", "text")):
            empty = f"({empty} OR {expr} = '')"
        parts.append(f"SUM({empty}) AS {_quote(str(col['label']) + '空值数')}")
    where = [f"{alias}.del_flag = 0"] if any(
        str(c.get("name")) == "del_flag" for c in entry.get("columns") or []
    ) else []
    sql = (
        f"SELECT COUNT(*) AS 总行数, {', '.join(parts)} FROM {table} {alias}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
    )
    return sql, "各列空值数与填报完整度"


def distinct_values(
    kind: str,
    table: str,
    column: str,
    filters: list[dict[str, Any]] | None = None,
):
    """任意列的取值清单（编码给中文）+ 种数。"""
    col = catalog.resolve_column(kind, table, column)
    if not col:
        return None, f"字段「{column}」找不到"
    alias = "m"
    expr = _select_expr(kind, table, col, alias).split(" AS ")[0]
    joins, where, error = _where_for(kind, table, filters, alias)
    if error:
        return None, error
    sql = (
        f"SELECT {expr} AS 取值, COUNT(*) AS 数量 FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''} "
        f"GROUP BY 取值 ORDER BY 数量 DESC LIMIT 200"
    )
    return sql, f"「{col.get('label')}」的取值清单与种数{_filters_note(filters)}"


def filter_count(kind: str, table: str, filters: list[dict[str, Any]] | None = None):
    """任意条件组合的计数（generic_detail 的计数版）。"""
    # 直接拼 COUNT(*)：以前是把明细 SQL 按字符串切开再拼 FROM，过滤条件带 JOIN 时会拼出
    # 引用不存在别名的废 SQL（"f0_0.route_id"）。
    joins, where, error = _where_for(kind, table, filters, "m")
    if error:
        return None, error
    sql = (
        f"SELECT COUNT(*) AS 数量 FROM {table} m {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
    )
    return sql, f"{catalog.table_label(kind, table)}计数{_filters_note(filters)}"


def geo_bounds(kind: str, table: str, filters: list[dict[str, Any]] | None = None):
    """经纬度范围（分布在哪一侧、空间范围）。"""
    entry = catalog.table_entry(kind, table)
    if not entry:
        return None, f"表 {table} 不在白名单"
    geo_cols = [c for c in entry.get("columns") or [] if "geo" in (c.get("roles") or [])]
    if not geo_cols:
        return None, "这张表没有经纬度列"
    alias = "m"
    parts = []
    for col in geo_cols[:4]:
        parts.append(f"ROUND(MIN({alias}.{col['name']}), 6) AS {_quote('最小' + str(col['label']))}")
        parts.append(f"ROUND(MAX({alias}.{col['name']}), 6) AS {_quote('最大' + str(col['label']))}")
    joins, where, error = _where_for(kind, table, filters, alias)
    if error:
        return None, error
    sql = (
        f"SELECT COUNT(*) AS 记录数, {', '.join(parts)} FROM {table} {alias} {' '.join(joins)}"
        f"{' WHERE ' + ' AND '.join(where) if where else ''}"
    )
    return sql, f"经纬度范围（最小/最大）{_filters_note(filters)}"


def profile_columns(
    kind: str = "mysql",
    table: str | None = None,
    only_empty: bool = False,
    threshold: float | None = None,
) -> dict[str, Any]:
    """（见上方 docstring）"""
    """数据画像：每张表多少行、每列空值数与空值率。

    判空口径（方案 §四）：数值/日期/json 只判 IS NULL（0 不算空）；字符型判 IS NULL 或空串。
    """
    from .db import db_manager

    cat = catalog.field_catalog(kind)
    tables = [t for t in cat.get("tables") or [] if not table or str(t.get("table")) == table]
    rows: list[dict[str, Any]] = []
    empty_tables: list[dict[str, Any]] = []
    for entry in tables:
        name = str(entry.get("table"))
        alias = "m"
        is_pg = kind == "postgresql"
        parts = []
        for col in entry.get("columns") or []:
            expr = f"{alias}.{col['name']}"
            ctype = str(col.get("type") or "")
            empty = f"{expr} IS NULL"
            if not is_pg and any(key in ctype for key in ("char", "text")):
                empty = f"({empty} OR {expr} = '')"
            parts.append(f"SUM({empty}) AS {_quote(str(col['name']))}")
        where = [f"{alias}.del_flag = 0"] if any(
            str(c.get("name")) == "del_flag" for c in entry.get("columns") or []
        ) else []
        sql = (
            f"SELECT COUNT(*) AS __total__, {', '.join(parts)} FROM {name} {alias}"
            f"{' WHERE ' + ' AND '.join(where) if where else ''}"
        )
        try:
            result = db_manager.query(kind, sql)
        except Exception as exc:  # noqa: BLE001
            log_error("generic", "profile_table_failed", exc, table=name, kind=kind)
            continue
        data = (result.get("rows") or [{}])[0]
        total = int(data.get("__total__") or 0)
        if total == 0:
            empty_tables.append(
                {"table": name, "label": entry.get("label"), "columns": len(entry.get("columns") or [])}
            )
            continue
        for col in entry.get("columns") or []:
            nulls = data.get(str(col.get("name")))
            nulls = int(nulls or 0)
            rate = round(nulls * 100.0 / total, 2) if total else 0.0
            if only_empty and nulls != total:
                continue
            if threshold is not None and rate < threshold:
                continue
            rows.append(
                {
                    "table": name,
                    "table_label": entry.get("label"),
                    "column": str(col.get("name")),
                    "label": str(col.get("label")),
                    "type": str(col.get("type")),
                    "total": total,
                    "nulls": nulls,
                    "null_rate": rate,
                }
            )
    rows.sort(key=lambda item: -item["null_rate"])
    return {
        "kind": kind,
        "total_tables": len(tables),
        "empty_tables": empty_tables,
        "columns": rows,
    }


# ── 数据画像入口（方案第四节：meta/profiling 不经过业务 plan）─────────────────
# 词表外置到 resources/lexicon/trigger_words.yaml（改说法不用动代码）
def _profile_words() -> tuple[str, ...]:
    from .resource_loader import words

    return words(
        "lexicon/trigger_words.yaml",
        "profiling.signals",
        (
            "没数据",
            "没有数据",
            "没填",
            "全空",
            "空字段",
            "空值率",
            "完整度",
            "填报",
            "覆盖率",
            "数据画像",
        ),
    )


def _profile_subjects() -> tuple[str, ...]:
    from .resource_loader import words

    return words("lexicon/trigger_words.yaml", "profiling.subjects", ("字段", "列", "表"))


def is_profiling_question(message: str) -> bool:
    text = str(message or "")
    if not text:
        return False
    has_field_or_table = any(word in text for word in _profile_subjects())
    return has_field_or_table and any(word in text for word in _profile_words())


def profiling_reply(message: str, kind: str = "mysql") -> dict[str, Any] | None:
    """元数据类问题：直接跑画像，不走业务 plan（避免被套成"缺路线"澄清）。"""
    text = str(message or "")
    if not is_profiling_question(text):
        return None
    table = None
    for entry in field_catalog_tables(kind):
        name = str(entry.get("table"))
        label = str(entry.get("label") or "")
        if name and (name.lower() in text.lower() or (label and label in text)):
            table = name
            break
    only_empty = any(word in text for word in ("没数据", "没有数据", "全空", "空字段"))
    try:
        result = profile_columns(kind, table=table, only_empty=only_empty)
    except Exception as exc:  # noqa: BLE001
        log_error("generic", "profiling_failed", exc, question=text[:80])
        return None
    lines: list[str] = []
    if table:
        lines.append(f"表 {table} 的字段画像：")
    else:
        empties = result.get("empty_tables") or []
        lines.append(
            f"全库 {result.get('total_tables')} 张表里，有 {len(empties)} 张是空表："
            + "、".join(str(item.get("label") or item.get("table")) for item in empties)
        )
    rows = result.get("columns") or []
    if only_empty:
        lines.append(f"整列全空的字段共 {len(rows)} 个：")
    else:
        lines.append(f"空值率最高的字段（前 20）：")
    for item in rows[:20]:
        lines.append(
            f"- {item['table_label']}.{item['label']}（{item['column']}）："
            f"空 {item['nulls']}/{item['total']}，空值率 {item['null_rate']}%"
        )
    if not rows:
        lines.append("（没有符合条件的数据）")
    return {
        "reply": "\n".join(lines),
        "source": "database",
        "engine": "profiling",
        "answer_format": "text",
        "sql": None,
        "columns": [],
        "rows": rows[:200],
        "chart_data": None,
        "profiling": result,
    }


def field_catalog_tables(kind: str) -> list[dict[str, Any]]:
    return list(catalog.field_catalog(kind).get("tables") or [])
