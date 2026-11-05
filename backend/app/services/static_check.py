# -*- coding: utf-8 -*-
"""**不连数据**的静态校验：表在不在、列在不在、编码值在不在该列的 code_map 里、参数能不能绑。

为什么需要它（docs/从打地鼠到回归防护体系方案.md §六）：
空表上"跑一遍看结果"验不出口径错——回环验证比的是行数/内容，0 行 == 0 行永远成立；
而"SQL 里写的表名、列名、编码值到底对不对"是**纯结构**的事，现在就能验。

产物：`check_presets()` / `check_auto_templates()` / `check_generic_matrix()`，
`run_regression.py` 把它们当常驻用例跑（`--only static-check`）。
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from . import schema_catalog

# 目录外但**已人工核对**的表：预置模板有意用它（当时白名单里没有，但库里有数据）。
# 登记在这儿而不是加进语义层白名单，是为了不改"语义层能看到哪些表"这件事。
EXTRA_TABLES: dict[str, str] = {
    "ai_problem_asset": "资产巡检记录（125 行，预置模板用；未进 17 张白名单）",
}

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
_IDENTIFIER_LITERAL = re.compile(
    r"\b(?:route_id|routes_id|section_id|sections_id|patrol_id|"
    r"task_number|patrol_number|problem_number|vehicle_id|device_code|"
    r"route_name|short_name|full_name|problem_base_id)\b"
    r"\s*(?:=|IN)\s*\(?\s*'([^']+)'",
    re.IGNORECASE,
)


def _catalog(kind: str = "mysql") -> dict[str, Any]:
    return schema_catalog.field_catalog(kind)


def _fill_placeholders(sql: str, params: dict[str, Any]) -> str:
    """把 `{参数}` 换成**类型对**的代表值，好让 SQL 能解析（值本身不参与结构判断）。

    枚举/实体参数用它的第一个真实选项，避免拿 'x' 去撞"编码值不在字典里"的假警报。
    """
    text = str(sql or "")
    for name, spec in (params or {}).items():
        spec = spec or {}
        kind = str(spec.get("type"))
        if kind == "op":
            value = ">"
        elif name == "top_n" or kind == "int":
            value = str((spec.get("range") or [5])[0])
        elif kind == "numeric":
            value = "1"
        elif kind in {"time", "date"}:
            value = "2026-01-01"
        else:
            options = spec.get("options") or []
            value = str((options[0] or {}).get("value") or "x") if options else "x"
        text = text.replace("{" + name + "}", value)
    return text


def _columns_of(table: str, kind: str) -> dict[str, dict[str, Any]]:
    for entry in _catalog(kind).get("tables") or []:
        if str(entry.get("table")) != table:
            continue
        return {str(col.get("name")).lower(): col for col in entry.get("columns") or []}
    return {}


def _query_output_columns(query: Any) -> set[str] | None:
    """SELECT/CTE/派生表实际向消费者暴露的列；None 表示 `SELECT *` 无法静态确认。"""
    if not isinstance(query, exp.Select):
        return None
    out: set[str] = set()
    for projection in query.expressions:
        if projection.find(exp.Star):
            return None
        name = str(projection.alias_or_name or "")
        if name:
            out.add(name.lower())
    return out


def _cte_output_columns(cte: exp.CTE) -> set[str] | None:
    return _query_output_columns(cte.this)


def _inside_cte(node: Any, root: exp.Expression) -> bool:
    parent = node.parent
    while parent is not None and parent is not root:
        if isinstance(parent, exp.CTE):
            return True
        parent = parent.parent
    return False


def check_sql(sql: str, kind: str = "mysql") -> list[str]:
    """一条 SQL 的静态体检；返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    try:
        tree = sqlglot.parse_one(str(sql or ""), read=kind if kind != "postgresql" else "postgres")
    except Exception as exc:  # noqa: BLE001
        return [f"解析失败：{str(exc)[:90]}"]
    known = {str(t.get("table")) for t in _catalog(kind).get("tables") or []}
    # Wren 编译出来的物理 SQL 形如 `WITH disease AS (… FROM ai_problem …) AS wren_src_disease`：
    #   · `disease` 这种 CTE 名沿用了 MDL 模型名 → 要顺着 CTE 体回到**基表**再验列；
    #   · `wren_src_*` / 派生表别名（`(SELECT …) AS x`）是编译中间名 → 一律跳过。
    cte_names = {str(cte.alias_or_name or "").lower() for cte in tree.find_all(exp.CTE)}
    cte_outputs = {
        str(cte.alias_or_name or "").lower(): _cte_output_columns(cte)
        for cte in tree.find_all(exp.CTE)
    }
    cte_aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = str(table.name or "").lower()
        if name in cte_outputs:
            cte_aliases[str(table.alias_or_name or name).lower()] = name
    cte_sources: dict[str, str] = {}
    try:
        from . import templatizer

        cte_sources = {
            str(name).lower(): str(table)
            for name, table in templatizer._cte_source_tables(tree).items()  # noqa: SLF001
        }
    except Exception:  # noqa: BLE001
        cte_sources = {}
    derived = {
        str(node.alias or "").lower()
        for node in tree.find_all(exp.Subquery)
        if str(node.alias or "")
    }
    for join in tree.find_all(exp.Join):
        right = join.this
        if not isinstance(right, exp.Subquery):
            continue
        alias = str(right.alias or "").lower()
        outputs = _query_output_columns(right.this)
        on = join.args.get("on")
        if not alias or outputs is None or on is None:
            continue
        for column in on.find_all(exp.Column):
            if (
                str(column.table or "").lower() == alias
                and str(column.name).lower() not in outputs
            ):
                problems.append(
                    f"派生表字段契约不成立：{column.table}.{column.name} 未在派生表输出列中"
                )
    # Wren 的 CTE 链里会把逻辑列"算出来"（`… AS problem_type_name` / `AS has_image` /
    # `AS route_length_km`）：这些名字在基表里当然找不到，不能报"列不存在"。
    derived_columns = {
        str(node.alias or "").lower()
        for node in tree.find_all(exp.Alias)
        if str(node.alias or "")
    }
    aliases: dict[str, str] = {}
    for table in tree.find_all(exp.Table):
        name = str(table.name or "")
        alias = str(table.alias or name).lower()
        lowered = name.lower()
        if lowered in cte_names:
            source = cte_sources.get(lowered, "")
            if source:
                aliases[alias] = source
                aliases.setdefault(lowered, source)
            continue
        if lowered.startswith("wren_src_") or alias in derived:
            continue  # 编译中间名/派生表别名
        if lowered not in known:
            if lowered in EXTRA_TABLES:
                continue
            problems.append(f"表不在字段目录里：{name}")
            continue
        aliases[alias] = name
        aliases.setdefault(lowered, name)
    for column in tree.find_all(exp.Column):
        if isinstance(column.this, exp.Star) or str(column.name) == "*":
            continue  # `p.*`
        qualifier = str(column.table or "")
        cte_name = cte_aliases.get(qualifier.lower())
        if cte_name and not _inside_cte(column, tree):
            outputs = cte_outputs.get(cte_name)
            if outputs is not None and str(column.name).lower() not in outputs:
                problems.append(
                    f"CTE 字段契约不成立：{cte_name}.{column.name} 未在 CTE 输出列中"
                )
                continue
        table = aliases.get(qualifier.lower()) or (
            qualifier if qualifier in known else ""
        )
        if not table:
            continue
        columns = _columns_of(table, kind)
        col = columns.get(str(column.name).lower())
        if col is None:
            # 只在这张表里找不到时，才认为是"CTE 里算出来的逻辑列"并放过；
            # 表里**有**这一列就必须照常验（编码值检查跟着走，别被别名规则盖掉）
            if str(column.name).lower() in derived_columns:
                continue
            problems.append(f"列不存在：{table}.{column.name}")
            continue
        code_map = col.get("code_map") or {}
        if not code_map:
            continue
        for literal in tree.find_all(exp.Literal):
            if not literal.is_string:
                continue
            value = str(literal.this)
            parent = literal.parent
            # 只认"这一列 = 字面值"（含 CASE WHEN 里的）；`= ''` 是空值检查，不算编码值
            if not isinstance(parent, (exp.EQ, exp.In, exp.When)) or value == "":
                continue
            left = getattr(parent, "this", None)
            if not isinstance(left, exp.Column) or str(left.name) != str(column.name):
                continue
            if value not in code_map and value not in set(code_map.values()):
                problems.append(
                    f"编码值不在字典里：{table}.{column.name} = '{value}'"
                    f"（可选 {'、'.join(str(k) for k in list(code_map)[:6])}）"
                )
    return sorted(set(problems))


def check_presets() -> dict[str, Any]:
    """预置模板（hand-written SQL）静态体检。"""
    from . import preset_lib

    data = preset_lib.load(force=True)
    params_spec = data.get("params") or {}
    results: list[dict[str, Any]] = []
    for item in data.get("queries") or []:
        sql = _fill_placeholders(str(item.get("sql") or ""), params_spec)
        problems = check_sql(sql)
        if problems:
            results.append({"id": item.get("id"), "problems": problems})
    return {"total": len(data.get("queries") or []), "bad": results}


def check_auto_templates() -> dict[str, Any]:
    """自动沉淀模板静态体检（用挖槽那份"填占位符"的办法把 SQL 变回可解析形态）。"""
    from . import templatizer

    items = templatizer.list_templates()
    results: list[dict[str, Any]] = []
    for item in items:
        hardcoded = [
            match
            for match in _IDENTIFIER_LITERAL.findall(str(item.get("sql") or ""))
            if not str(match).startswith("{")
        ]
        if hardcoded:
            results.append(
                {
                    "id": item.get("id"),
                    "status": str(item.get("status") or ""),
                    "problems": [f"业务标识写死在 SQL 中：{value}" for value in hardcoded[:3]],
                }
            )
            continue
        text = templatizer._fill_for_profile(item) if hasattr(templatizer, "_fill_for_profile") else ""
        if not text or "{" in text:
            results.append({"id": item.get("id"), "problems": ["占位符没替干净，无法静态校验"]})
            continue
        problems = check_sql(text)
        if problems:
            results.append(
                {"id": item.get("id"), "status": str(item.get("status") or ""), "problems": problems}
            )
    return {"total": len(items), "bad": results}


def check_generic_matrix() -> dict[str, Any]:
    """coverage matrix：每张表 × 每个通用算子，产物 SQL 至少要在结构上站得住。

    这是"没有数据也能量覆盖"的那张表：静态必过；数据到位后同一张矩阵再验"能执行 + 命中行数>0"。
    """
    from . import generic

    checked = 0
    skipped: list[dict[str, str]] = []
    bad: list[dict[str, Any]] = []
    for entry in _catalog().get("tables") or []:
        table = str(entry.get("table"))
        columns = [str(col.get("name")) for col in entry.get("columns") or []]
        numeric = [
            str(col.get("name"))
            for col in entry.get("columns") or []
            if "numeric" in (col.get("roles") or [])
        ]
        time_col = next(
            (
                str(col.get("name"))
                for col in entry.get("columns") or []
                if "time" in (col.get("roles") or [])
            ),
            "",
        )
        code_col = next(
            (
                str(col.get("name"))
                for col in entry.get("columns") or []
                if (col.get("code_map") or {})
            ),
            "",
        )
        plans: list[tuple[str, str | None]] = [
            ("count", generic.generic_count("mysql", table, [])[0]),
            ("detail", generic.generic_detail("mysql", table, columns[:3], [], None, 10)[0]),
            ("nulls", generic.null_completeness("mysql", table, columns[:3])[0]),
            ("profile", None),  # 画像是 Python 侧聚合，没有 SQL 可查
        ]
        if numeric:
            plans.append(("stats", generic.column_stats("mysql", table, numeric[:2])[0]))
        if code_col:
            plans.append(("group", generic.group_distribution("mysql", table, code_col)[0]))
            plans.append(("distinct", generic.distinct_values("mysql", table, code_col)[0]))
        if time_col:
            plans.append(("time_series", generic.time_series("mysql", table, time_col, "month")[0]))
        for operator, sql in plans:
            if not sql:
                skipped.append({"table": table, "operator": operator, "why": "算子拒绝了这组参数"})
                continue
            checked += 1
            problems = check_sql(sql)
            if problems:
                bad.append({"table": table, "operator": operator, "problems": problems})
    return {"tables": len(_catalog().get("tables") or []), "checked": checked,
            "skipped": skipped, "bad": bad}


def run_all() -> dict[str, Any]:
    presets = check_presets()
    auto = check_auto_templates()
    matrix = check_generic_matrix()
    return {
        "presets": presets,
        "auto": auto,
        "matrix": matrix,
        "ok": not presets["bad"] and not auto["bad"] and not matrix["bad"],
    }


def problems_flat() -> list[str]:
    """给回归/管理页用的一行式问题清单。"""
    report = run_all()
    out: list[str] = []
    for item in report["presets"]["bad"]:
        out.append(f"预置模板 {item['id']}：{'；'.join(item['problems'])}")
    for item in report["auto"]["bad"]:
        # 候选模板本来就不参与推荐（要人工转正）→ 只当警告，不算红
        prefix = "[警告] 候选模板" if item.get("status") == "candidate" else "自动模板"
        out.append(f"{prefix} {item['id']}：{'；'.join(item['problems'])}")
    for item in report["matrix"]["bad"]:
        out.append(f"通用算子 {item['table']}.{item['operator']}：{'；'.join(item['problems'])}")
    return out


def blocking_problems() -> list[str]:
    """会让回归变红的那部分（预置模板 / mature 模板 / 通用算子矩阵）；
    候选模板的问题只进 `problems_flat()` 当警告——它们本来就不会被推荐。"""
    return [line for line in problems_flat() if not line.startswith("[警告]")]


def warnings() -> list[str]:
    """不影响回归、但维护页该看到的（目前只有"候选模板的静态问题"）。"""
    return [line for line in problems_flat() if line.startswith("[警告]")]
