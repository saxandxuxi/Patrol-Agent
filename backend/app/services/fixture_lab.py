# -*- coding: utf-8 -*-
"""**只读夹具库**：不往业务库写一个字节，用"结构生成的小数据"在本地 SQLite 里把 SQL 跑起来。

为什么需要（docs/从打地鼠到回归防护体系方案.md §六.②）：
空表上"跑一遍看结果"验不出口径——回环验证比的是行数/内容，0 行 == 0 行永远成立。
有了一份结构合法的夹具，就能验：join 到底出不出行、编码值能不能对上、条件是不是把结果筛空了。

三条硬约束：
1. **绝不写业务库**：只建自己的 SQLite 文件（默认 `logs/test/fixture_lab.sqlite`），
   对 MySQL/PG 只有 SELECT；
2. 夹具**由字段目录生成**（列名/类型/角色/枚举/code_map/外键），库结构改了夹具跟着变，不手写数据；
3. 只当"口径体检"，不当答案来源——它不是真数据，命中行数只用来判断"这条 SQL 结构上筛得到东西吗"。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from . import schema_catalog

_DB_PATH = Path("logs/test/fixture_lab.sqlite")
_ROWS_PER_TABLE = 12


def _sqlite_type(mysql_type: str) -> str:
    text = str(mysql_type or "").lower()
    if any(key in text for key in ("int", "decimal", "double", "float", "numeric")):
        return "REAL"
    return "TEXT"


def _extra_tables() -> dict[str, list[dict[str, Any]]]:
    """目录外但**已登记**的表（`static_check.EXTRA_TABLES`）：列从库里读（只读）。"""
    from . import static_check
    from .db import db_manager

    out: dict[str, list[dict[str, Any]]] = {}
    for table in static_check.EXTRA_TABLES:
        try:
            rows = db_manager.query(
                "mysql",
                "SELECT column_name AS c, data_type AS t, column_comment AS m "
                "FROM information_schema.columns WHERE table_schema = DATABASE() "
                f"AND table_name = '{table}' ORDER BY ordinal_position",
            )["rows"]
        except Exception:  # noqa: BLE001
            continue
        out[table] = [
            {
                "name": str(row["c"]),
                "label": str(row["m"] or row["c"]),
                "type": str(row["t"]),
                "roles": ["primary"] if str(row["c"]) == "id" else [],
                "code_map": {},
            }
            for row in rows
        ]
    return out


def _rename_self_named_cte(sql: str) -> str | None:
    """把"CTE 名和物理表同名"的写法改名，让 SQLite 能跑（真 MySQL 是能跑的）。

    Wren 编译出来的物理 SQL 长这样：
        WITH basic_facilities_bridges AS (SELECT … FROM basic_facilities_bridges …)
        SELECT COUNT(*) FROM basic_facilities_bridges AS b WHERE …
    MySQL 把 CTE 体里的同名表当**物理表**（体里看不到自己），SQLite 却判 `circular reference`
    ——那是方言差异，不是 SQL 错。这里把 CTE 名改成 `zz_cte_<原名>`、外层引用跟着改，
    CTE 体里的物理表引用保持原名 → SQLite 的语义就和 MySQL 一致了。
    """
    try:
        tree = sqlglot.parse_one(str(sql), read="mysql")
    except Exception:  # noqa: BLE001
        return None
    known = {
        str(entry.get("table")).lower()
        for entry in schema_catalog.field_catalog("mysql").get("tables") or []
    }
    renamed = False
    for cte in tree.find_all(exp.CTE):
        name = str(cte.alias_or_name or "")
        if not name or name.lower() not in known:
            continue
        new_name = f"zz_cte_{name}"
        body = cte.this
        for node in tree.find_all(exp.Table):
            if str(node.name or "").lower() != name.lower():
                continue
            if body is not None and node.find_ancestor(exp.CTE) is cte:
                continue  # CTE 体里的物理表引用：保持原名
            node.set("this", exp.to_identifier(new_name))
        cte.set("alias", exp.TableAlias(this=exp.to_identifier(new_name)))
        renamed = True
    if not renamed:
        return str(sql)
    return tree.sql(dialect="mysql")


def _register_udf(conn: sqlite3.Connection) -> None:
    """补几个 MySQL 常用函数（SQLite 没有）：只影响夹具库，不碰业务库。"""
    import datetime as _dt

    def _substring_index(text: Any, delim: Any, count: Any) -> str:
        text, delim, count = str(text or ""), str(delim or ""), int(count or 0)
        if not delim:
            return text
        parts = text.split(delim)
        if count >= 0:
            return delim.join(parts[:count]) if count else ""
        return delim.join(parts[count:]) if count else ""

    def _date_format(value: Any, fmt: Any) -> str:
        text = str(value or "")
        try:
            parsed = _dt.datetime.fromisoformat(text.replace("/", "-")[:19])
        except ValueError:
            return text
        return parsed.strftime(str(fmt).replace("%Y", "%Y").replace("%m", "%m").replace("%d", "%d"))

    conn.create_function("substring_index", 3, _substring_index)
    conn.create_function("json_unquote", 1, lambda value: value)
    conn.create_function("date_format", 2, _date_format)
    conn.create_function("concat", -1, lambda *args: "".join(str(a) for a in args if a is not None))
    conn.create_function("lpad", 3, lambda text, width, pad: str(text or "").rjust(int(width or 0), str(pad or " ")))
    conn.create_function("greatest", -1, lambda *args: max(args) if args else None)
    conn.create_function("least", -1, lambda *args: min(args) if args else None)
    conn.create_function("if", 3, lambda cond, a, b: a if cond else b)
    # MySQL 的 LAST_DAY 在 SQLite 里没有：夹具只需要"能算出一个日期字符串"
    conn.create_function("last_day", 1, lambda value: str(value or "")[:8] + "28")
    # 日期分量函数（MySQL 有、SQLite 没有）
    for name in ("day", "month", "year", "hour", "minute", "second"):
        conn.create_function(
            name, 1,
            lambda value, _name=name: str(value or "")[{"day": 8, "month": 5, "year": 0,
                                                         "hour": 11, "minute": 14,
                                                         "second": 17}[_name]:][:2].lstrip("0") or "1",
        )


def _value_for(column: dict[str, Any], index: int, sample_ids: dict[str, str]) -> Any:
    """按列的角色/类型造一个"结构合法"的值（可复现，不依赖真数据）。"""
    roles = set(column.get("roles") or [])
    code_map = column.get("code_map") or {}
    if "primary" in roles:
        return f"fixture-{column.get('table') or ''}-{index:03d}"
    if "soft_delete" in roles:
        return "0"
    if "fk" in roles:
        join = column.get("join") or {}
        parent = str(join.get("table") or "")
        # 外键指向父表夹具里的某个 id（父表先建，就能取到）
        return sample_ids.get(parent, f"fixture-{parent or 'parent'}-{index:03d}")
    # JSON 型列要给**合法 JSON**：SQL 里会用 JSON_EXTRACT(pic_position,'$.confidence') 取值，
    # 塞普通文本会报 malformed JSON（那是夹具的问题，不是被验 SQL 的问题）
    if str(column.get("name")) in {"pic_position", "map_position", "coordinate", "plan_path"}:
        return '{"confidence": "0.9", "lat": "22.55", "lng": "113.88", "type": "摄像头"}'
    if "image" in roles:
        return f"fixture/{index:03d}.jpeg"
    if code_map:
        keys = list(code_map)
        return keys[index % len(keys)]
    if "time" in roles:
        day = (index % 28) + 1
        return f"2026-07-{day:02d} 08:00:00"
    if "numeric" in roles:
        return round(1.0 + index * 2.5, 2)
    label = str(column.get("label") or column.get("name") or "")
    return f"{label}-样例{index}"


def build(path: Path | None = None) -> dict[str, Any]:
    """按字段目录建一份夹具 SQLite；返回 {表: 行数}。"""
    target = Path(path) if path else _DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()  # 只删自己那份夹具文件
    catalog = schema_catalog.field_catalog("mysql")
    conn = sqlite3.connect(str(target))
    _register_udf(conn)
    sample_ids: dict[str, str] = {}
    tables = list(catalog.get("tables") or [])
    for table, columns in _extra_tables().items():
        tables.append({"table": table, "columns": columns})
    # PostgreSQL 两张点位表也建进来（各自目录里的结构），这样 PG 题的 SQL 也能探测
    for entry in schema_catalog.field_catalog("postgresql").get("tables") or []:
        tables.append(
            {
                "table": str(entry.get("table")),
                "columns": [dict(col) for col in entry.get("columns") or []],
            }
        )
    for entry in tables:
        table = str(entry.get("table"))
        columns = list(entry.get("columns") or [])
        ddl = ", ".join(
            f'"{col.get("name")}" {_sqlite_type(str(col.get("type") or ""))}'
            for col in columns
        )
        conn.execute(f'CREATE TABLE "{table}" ({ddl})')
        rows = []
        for index in range(_ROWS_PER_TABLE):
            row = []
            for col in columns:
                col = {**col, "table": table}
                row.append(_value_for(col, index, sample_ids))
            rows.append(row)
            if index == 0:
                # 记下这张表的一个 id，给后面的外键用（外键表可能在目录里排在前面，
                # 这里只保证"指向真实存在过的 id"这件事对大部分表成立）
                for col, value in zip(columns, row):
                    if "primary" in set(col.get("roles") or []):
                        sample_ids[table] = str(value)
                    if "fk" in set(col.get("roles") or []) and str(col.get("name")) in {
                        "routes_id",
                        "route_id",
                    }:
                        sample_ids[table] = str(value)
        marks = ",".join("?" for _ in columns)
        conn.executemany(f'INSERT INTO "{table}" VALUES ({marks})', rows)
    conn.commit()
    counts = {
        str(entry.get("table")): _ROWS_PER_TABLE for entry in tables
    }
    conn.close()
    return counts


def run_sql(
    sql: str, path: Path | None = None, kind: str = "mysql"
) -> tuple[bool, int, str]:
    """把 SQL（mysql/postgres 方言）转成 SQLite 跑一遍（夹具库）。返回 (能跑, 行数, 失败原因)。"""
    ok, rows, reason = run_sql_rows(sql, path, kind)
    return ok, len(rows), reason


def run_sql_rows(
    sql: str, path: Path | None = None, kind: str = "mysql"
) -> tuple[bool, list[dict[str, Any]], str]:
    """同上，但返回**行内容**（聚合题要看值：COUNT(*) 永远只回 1 行）。"""
    target = Path(path) if path else _DB_PATH
    if not target.exists():
        return False, [], "夹具库还没建（先 build()）"
    try:
        raw = str(sql)
        # 去掉库/schema 前缀：`new_four_road_base_ai.basic_...`、`"public".gps_points`
        raw = re.sub(r"\b[a-z_][a-z0-9_]*\.(basic_|ai_|maintenance_|project_)", r"\1", raw)
        raw = re.sub(r'"?public"?\.', "", raw)
        raw = re.sub(r"\bnew_four_road_base_ai\.", "", raw)
        text = _rename_self_named_cte(raw)
        if text is None:
            return False, [], "解析失败（重命名自名 CTE 时）"
        # 复核：改完还得能解析
        sqlglot.parse_one(text, read=("postgres" if str(kind) == "postgresql" else "mysql"))
        statements = sqlglot.transpile(
            text, read=("postgres" if str(kind) == "postgresql" else "mysql"), write="sqlite"
        )
    except Exception as exc:  # noqa: BLE001
        return False, [], f"方言转译失败：{str(exc)[:90]}"
    if not statements:
        return False, [], "转译后是空语句"
    conn = sqlite3.connect(str(target))
    _register_udf(conn)
    try:
        cursor = conn.execute(statements[0])
        names = [str(item[0]) for item in (cursor.description or [])]
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        return True, rows, ""
    except Exception as exc:  # noqa: BLE001
        return False, [], f"执行失败：{str(exc)[:110]}"
    finally:
        conn.close()


def probe(sql: str, kind: str = "mysql", path: Path | None = None) -> dict[str, Any]:
    """给"在测试库上试这条 SQL"用的三态结论（**只在夹具库里跑，绝不碰业务库**）。

    - `ok`：跑出行，结构能对上；
    - `empty`：能跑但 0 行 —— 值得核 join/条件/编码值（也可能只是夹具没有那条取值）；
    - `unsupported`：SQLite 方言差异（拿这种当 bug 会冤枉好 SQL）；
    - `unavailable`：夹具库建不出来 / SQL 空的。
    """
    text = str(sql or "").strip()
    if not text:
        return {"status": "unavailable", "rows": 0, "reason": "这条结果没有 SQL", "kind": kind}
    target = Path(path) if path else _DB_PATH
    if not text:
        return {"status": "unavailable", "rows": 0, "reason": "空 SQL", "kind": kind}
    if not target.exists():
        try:
            build(target)
        except Exception as exc:  # noqa: BLE001
            return {"status": "unavailable", "rows": 0, "reason": f"夹具库建不出来：{exc}", "kind": kind}
    runs, rows, reason = run_sql_rows(text, target, kind)
    if not runs and "no such table" in reason:
        # 夹具库是旧版建的（那会儿还没建这几张表）→ 重建一次再试
        try:
            build(target)
            runs, rows, reason = run_sql_rows(text, target, kind)
        except Exception:  # noqa: BLE001
            pass
    if not runs:
        state = "unsupported" if any(
            key in reason for key in ("no such function", "circular reference", "方言转译失败")
        ) else "error"
        return {"status": state, "rows": 0, "reason": reason, "kind": kind, "db": str(target)}
    return {
        "status": "ok" if rows else "empty",
        "rows": len(rows) if isinstance(rows, list) else 0,
        # 聚合题（COUNT/SUM）永远只回 1 行，值在行里——把首行一并带上，卡片才好显示"计数 = 0"
        "sample": (rows[0] if isinstance(rows, list) and rows else None),
        "reason": "" if rows else "夹具库里没筛出行：值得核 join 键 / 条件 / 编码值",
        "kind": kind,
        "db": str(target),
    }


def check_all(path: Path | None = None) -> dict[str, Any]:
    """把预置模板 + 自动模板都在夹具库上跑一遍，按结果分类。

    - `broken`：转译/执行报错 —— 真问题（SQL 结构/方言/表列对不上）；
    - `empty`：能跑但 0 行 —— 提醒（条件是不是把结果筛空了，或夹具本身没有对应取值）；
    - `ok`：跑出了行 —— 这条 SQL 至少结构上"筛得到东西"。
    """
    from . import static_check, templatizer

    target = Path(path) if path else _DB_PATH
    counts = build(target)
    broken: list[dict[str, str]] = []
    empty: list[str] = []
    ok = 0

    def _check(label: str, sql: str, params: dict[str, Any]) -> None:
        nonlocal ok
        filled = static_check._fill_placeholders(sql, params or {})  # noqa: SLF001
        runs, rows, reason = run_sql(filled, target)
        if not runs:
            broken.append({"id": label, "reason": reason})
        elif rows == 0:
            empty.append(label)
        else:
            ok += 1

    from . import preset_lib

    data = preset_lib.load(force=True)
    for item in data.get("queries") or []:
        _check(str(item.get("id")), str(item.get("sql") or ""), data.get("params") or {})
    for item in templatizer.list_templates():
        _check(str(item.get("id")), str(item.get("sql") or ""), item.get("params") or {})
    return {
        "tables": len(counts),
        "rows_per_table": _ROWS_PER_TABLE,
        "ok": ok,
        "empty": empty,
        "broken": broken,
        "db": str(target),
    }
