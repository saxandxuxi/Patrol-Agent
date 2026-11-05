"""⚠️ **已停用（2026-10-12）**：本模块不再被调用，保留只为兼容旧引用，后续版本可删除。

为什么停用：它靠**列名匹配**决定字段落在哪张表，选错也照样执行、还跳过用户确认，实测把
"路段长度"JOIN 到了边坡表的 `length`（结果完全错却看不出来）。跨表多字段题现在统一走
两条路：模板卡（用户确认后执行）或 Wren 语义层——见
docs/清理不可靠引擎与通用算子修复方案.md §三。

下面是它原本的说明（跨表多字段并列查询的执行层，docs/跨表多字段并列查询_系统性整改方案.md §三 层 2）。

用户一句话要了**多张表的字段**时（"养护任务编号和病害描述""每个路段多少米，以及每条路线的
起始桩号"），单表链路只能答一半或答错表。这里按字段之间的关系分三类处理：

  · joined（可关联）：字段所在表之间有确定性外键关系 → 一条 LEFT JOIN SQL 查回所有字段；
  · lookup（字典表）：其中一张是 `*_base` 字典表 → 同 joined（编码翻中文交给 generic._select_expr）；
  · parallel（并列）：几张表之间没有关系 → 每张表各出一条明细查询，按「分段结果」返回
    （复用复合题的结果结构，前端/结果工作台不用改）。

两条硬边界（不猜）：
  1. JOIN 条件只取字段目录里的外键/关系元数据，判不出方向就退化成并列；
  2. 计划里有过滤条件、而这些条件落不到本次查询的表/别名上时**直接弃权**返回 None，
     交回原链路——绝不静默把"西乡段7月"这种条件丢掉。
"""

from __future__ import annotations

import re
from typing import Any

from ..config import settings
from . import field_catalog, field_grounding, generic, nl2sql
from . import schema_catalog as catalog
from .logging_service import log_event

_ALIAS_REF = re.compile(r"\b([a-z][a-z0-9_]*)\.([a-z_][a-z0-9_]*)\b", re.IGNORECASE)


def _squash(text: str) -> str:
    return re.sub(r"[\s的了之,，。]+", "", str(text or "")).lower()


def _pick_base(tables: list[str], landing: str) -> str:
    """选主表：落点表优先；其次非字典表（`*_base` 一般是被 JOIN 的一侧）；最后取第一个。"""
    if landing and landing in tables:
        return landing
    for table in tables:
        if not table.endswith("_base"):
            return table
    return tables[0]


def _column_pool(kind: str) -> list[dict[str, Any]]:
    """字段解析的候选池 = 物理列 + **语义字段**（pic_position 的 JSON 字段、join 出来的中文名…）。

    为什么必须带上语义字段：`confidence` 这种名字在字典表里也有同名列（"置信度阈值"），
    只在物理列里找会被字典抢走 —— "调出所有病害图片的置信度"于是被判成跨两张表的多字段题
    （实测被拆成"病害记录 + 资产类型字典"）。语义字段归属 ai_problem，落点表优先即可定下来。
    """
    from . import field_catalog

    rows = [dict(item) for item in field_grounding._columns(kind)]  # noqa: SLF001
    seen = {(str(item.get("table")), str(item.get("column"))) for item in rows}
    for row in field_catalog._field_rows(kind):  # noqa: SLF001
        table = str(row.get("table") or "")
        column_name = str(row.get("name") or "")
        if not table or not column_name or (table, column_name) in seen:
            continue
        rows.append(
            {
                "table": table,
                "column": column_name,
                "label": str(row.get("label") or column_name),
                "expr": str(row.get("expr") or ""),
                "numeric": bool(row.get("numeric")),
                "roles": [],
                "code_map": {},
            }
        )
    return rows


def _match_column(
    kind: str, name: str, label: str, landing: str, ranked: list[str] | None = None
) -> tuple[dict[str, Any] | None, str, list[str]]:
    """把声明的一个字段解析成 (列, 物理表)；歧义时列返回 None 并给出候选表。"""
    pool: list[dict[str, Any]] = _column_pool(kind)
    exact: list[tuple[str, str]] = []
    by_label: list[tuple[str, str]] = []
    wanted_name = str(name or "").strip().lower()
    wanted_label = _squash(label)
    for column in pool:
        table = str(column["table"])
        if wanted_name and str(column["column"]).lower() == wanted_name:
            exact.append((table, str(column["column"])))
        elif wanted_label and _squash(str(column["label"])) == wanted_label:
            by_label.append((table, str(column["column"])))
    for pool in (exact, by_label):
        if not pool:
            continue
        tables = {table for table, _column in pool}
        if landing and landing in tables:
            table, column = next(item for item in pool if item[0] == landing)
            return catalog.resolve_column(kind, table, column), table, []
        # 多张表都有这一列时，用**这句话的语义表序**（字段触达的表级排名）消歧：
        # "每条路线的起始桩号"里 start_num 在 routes/sections/slope 都有，靠"路线"这个词
        # 就能定到 routes（docs/跨表多字段并列查询_系统性整改方案.md §三 层 2）
        for candidate in ranked or []:
            if candidate in tables:
                table, column = next(item for item in pool if item[0] == candidate)
                return catalog.resolve_column(kind, table, column), table, []
        if len(tables) == 1:
            table, column = pool[0]
            return catalog.resolve_column(kind, table, column), table, []
        return None, "", sorted(tables)
    return None, "", []


def resolve_fields(plan: dict[str, Any], kind: str = "mysql") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 plan 里的 `fields` 解析成 [{name,label,table,column}]；返回 (解析结果, 没找到的)。"""
    declared = list((plan.get("semantic") or {}).get("fields") or [])
    if len(declared) < 2:
        # 模型没给 fields 时退回 output_fields：只要它们跨表，一样按多字段处理
        # （实测同一条题三次里只有一两次会填 fields，光靠提示词不稳）
        declared = [
            {"name": str(name), "label": "", "table_hint": ""}
            for name in (plan.get("semantic") or {}).get("output_fields") or []
        ]
    grounded = dict((plan.get("grounded") or {}).get("table_fields") or {})
    landing = str((plan.get("grounded") or {}).get("table") or "")
    ranked = [
        str(item.get("name"))
        for item in (plan.get("grounded") or {}).get("tables") or []
        if item.get("name")
    ]
    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for item in declared:
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or "").strip()
        hint = str(item.get("table_hint") or "").strip()
        column: dict[str, Any] | None = None
        owner = ""
        if hint:
            column = catalog.resolve_column(kind, hint, name) or (
                catalog.resolve_column(kind, hint, label) if label else None
            )
            if column:
                owner = hint
        if column is None:
            # **落点表优先**（含语义字段）：同名列在多张表都有时，能落在本轮落点表上就用它。
            # "调出所有病害图片的置信度"两列（problem_url / confidence）都在 ai_problem 上，
            # 不该因为字典表也有 `confidence` 就被判成跨两张表的多字段题（实测被拆题）。
            if landing:
                preferred = next(
                    (
                        col
                        for col in _column_pool(kind)
                        if str(col.get("table")) == landing
                        and (
                            (name and str(col.get("column")).lower() == name.lower())
                            or (label and _squash(str(col.get("label"))) == _squash(label))
                        )
                    ),
                    None,
                )
                if preferred:
                    column, owner = preferred, landing
        if column is None:
            # 字段触达已经命中过这个字段名/中文名时，用它命中的那张表（比全库扫更准）
            for table, columns in grounded.items():
                hit = next(
                    (
                        col
                        for col in columns
                        if (name and str(col.get("column")) == name)
                        or (label and _squash(str(col.get("label"))) == _squash(label))
                    ),
                    None,
                )
                if hit:
                    column = catalog.resolve_column(kind, table, str(hit.get("column")))
                    if column:
                        owner = table
                        break
        candidates: list[str] = []
        if column is None:
            column, owner, candidates = _match_column(kind, name, label, landing, ranked)
        if column is None:
            missing.append({"field": name or label, "label": label or name, "tables": candidates})
            continue
        resolved.append(
            {
                "field": name or str(column.get("name") or ""),
                "label": label or str(column.get("label") or ""),
                "table": owner,
                "column": column,
            }
        )
    return resolved, missing


def _join_condition(kind: str, base: str, target: str) -> str | None:
    """base 到 target 的 JOIN 条件（模板串，`{base}`/`{alias}` 由调用方填）。

    只认字段目录里的外键元数据，方向自动判：
      · base 上有指向 target 的外键列 → `{base}.<列> = {alias}.<目标的被连列>`
      · target 上有指向 base 的外键列（反向） → `{alias}.<列> = {base}.<base 的被连列>`
    两个方向都没有 = 两张表没有确定关系 → 返回 None（调用方退化成并列，不猜）。
    """
    base_entry = catalog.table_entry(kind, base) or {}
    for column in base_entry.get("columns") or []:
        join = column.get("join") or {}
        if str(join.get("table") or "") == target:
            return f"{{base}}.{column['name']} = {{alias}}.{join.get('on') or 'id'}"
    target_entry = catalog.table_entry(kind, target) or {}
    for column in target_entry.get("columns") or []:
        join = column.get("join") or {}
        if str(join.get("table") or "") == base:
            return f"{{alias}}.{column['name']} = {{base}}.{join.get('on') or 'id'}"
    return None


def _has_column(kind: str, table: str, name: str) -> bool:
    return catalog.resolve_column(kind, table, name) is not None


def plan_queries(
    plan: dict[str, Any], resolved: list[dict[str, Any]], kind: str = "mysql"
) -> dict[str, Any] | None:
    """决定这条跨表问题怎么查：{mode: joined, sql} 或 {mode: parallel, queries}；判不出来返回 None。"""
    tables: list[str] = []
    for item in resolved:
        if item["table"] and item["table"] not in tables:
            tables.append(item["table"])
    if len(tables) < 2:
        return None  # 其实落在一张表上 → 交回原链路（单表能力更全）
    landing = str((plan.get("grounded") or {}).get("table") or "")
    base = _pick_base(tables, landing)
    aliases = dict(field_catalog._TABLE_ALIASES)  # noqa: SLF001 —— 条件片段用的就是这套别名
    conditions = [str(item) for item in nl2sql.condition_clauses(plan) if item]

    # ① 先试 JOIN（含字典表 lookup：字典表只是被 JOIN 的一侧）
    join_parts: list[str] = []
    alias_of: dict[str, str] = {base: aliases.get(base) or "m"}
    used_aliases = {alias_of[base]}
    for index, table in enumerate([item for item in tables if item != base], 1):
        condition = _join_condition(kind, base, table)
        if not condition:
            join_parts = []
            break
        alias = aliases.get(table) or f"j{index}"
        if alias in used_aliases:  # 别名撞了就退到 jN
            alias = f"j{index}"
        used_aliases.add(alias)
        alias_of[table] = alias
        on = condition.format(base=alias_of[base], alias=alias)
        if _has_column(kind, table, "del_flag"):
            on += f" AND {alias}.del_flag = 0"
        join_parts.append(f"LEFT JOIN {table} {alias} ON {on}")
    # 条件里引用的别名都得在场（"西乡段7月"那种条件落在 p./r./t. 上）
    needed = {
        match.group(1).lower()
        for clause in conditions
        for match in _ALIAS_REF.finditer(clause)
    }
    if join_parts and needed <= used_aliases:
        selects = [
            generic._select_expr(kind, item["table"], item["column"], alias_of[item["table"]])  # noqa: SLF001
            for item in resolved
        ]
        where = [f"{alias_of[base]}.del_flag = 0", *conditions]
        sql = (
            f"SELECT {', '.join(selects)} FROM {base} {alias_of[base]} "
            f"{' '.join(join_parts)} WHERE {' AND '.join(where)} LIMIT {int(settings.sql_max_rows)}"
        )
        log_event(
            "multi_field",
            "INFO",
            "planned",
            mode="joined",
            base=base,
            tables="、".join(tables),
        )
        return {"mode": "joined", "sql": sql, "base": base, "tables": tables}

    # ② 并列：各查各的。有计划条件却接不了 → 弃权（宁可交回原链路，也不静默丢条件）
    if conditions:
        log_event(
            "multi_field",
            "INFO",
            "parallel_declined_conditions",
            tables="、".join(tables),
            conditions="；".join(conditions)[:80],
        )
        return None
    queries: list[dict[str, Any]] = []
    for table in tables:
        columns = [str(item["column"]["name"]) for item in resolved if item["table"] == table]
        sql, scope = generic.generic_detail(kind, table, columns, limit=500)
        if not sql:
            return None
        queries.append({"table": table, "sql": sql, "columns": columns, "scope": scope})
    log_event(
        "multi_field", "INFO", "planned", mode="parallel", tables="、".join(tables)
    )
    return {"mode": "parallel", "queries": queries, "tables": tables}
