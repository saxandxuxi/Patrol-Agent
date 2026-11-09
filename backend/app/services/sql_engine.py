from __future__ import annotations

import re
import time
from typing import Any

from ..config import settings
from .db import DatabaseError, db_manager
from .legacy_query_chain import raise_disabled
from . import event_bus
from .logging_service import log_event


DANGEROUS_WORDS = (
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "truncate",
    "grant",
    "revoke",
    "load_file",
    "exec",
)

# 词边界匹配，避免把 create_time / update_time 这类列名误判成危险关键字。
_DANGEROUS_PATTERN = re.compile(
    r"\b(?:" + "|".join(DANGEROUS_WORDS) + r")\b|\binto\s+outfile\b"
)

# from/join 后面的表名，兼容 schema.table 与 反引号/双引号/方括号 包裹
_TABLE_PATTERN = re.compile(
    r"\b(?:from|join)\s+((?:[`\"\[]?[A-Za-z_]\w*[`\"\]]?\.)*[`\"\[]?[A-Za-z_]\w*[`\"\]]?)"
)

_DAMAGE_DICT_SUBQUERY_JOIN = re.compile(
    r"\(\s*SELECT\s+DISTINCT\s+problem_code\s*,\s*problem_type\s+"
    r"FROM\s+ai_problem_base\s+WHERE\s+problem_type\s+IS\s+NOT\s+NULL\s*\)\s*"
    r"(?P<dict>[A-Za-z_]\w*)\s+ON\s+"
    r"(?P<disease>[A-Za-z_]\w*)\.damage_type\s*=\s*(?P=dict)\.problem_code",
    re.IGNORECASE,
)
_DAMAGE_DICT_SUBQUERY_MISSING_ID = re.compile(
    r"\(\s*SELECT\s+DISTINCT\s+problem_code\s*,\s*problem_type\s+"
    r"FROM\s+ai_problem_base\b",
    re.IGNORECASE,
)
_DAMAGE_DICT_DIRECT_JOIN = re.compile(
    r"(?P<disease>[A-Za-z_]\w*)\.damage_type\s*=\s*"
    r"(?P<dict>[A-Za-z_]\w*)\.problem_code",
    re.IGNORECASE,
)
_DAMAGE_DICT_CAST_JOIN = re.compile(
    r"CAST\s*\(\s*NULLIF\s*\(\s*TRIM\s*\(\s*"
    r"(?P<dict>[A-Za-z_]\w*)\.problem_code\s*\)\s*,\s*''\s*\)\s*AS\s+UNSIGNED\s*\)"
    r"\s*=\s*"
    r"CAST\s*\(\s*NULLIF\s*\(\s*TRIM\s*\(\s*"
    r"(?P<disease>[A-Za-z_]\w*)\.damage_type\s*\)\s*,\s*''\s*\)\s*AS\s+UNSIGNED\s*\)",
    re.IGNORECASE,
)
_AI_PROBLEM_SOURCE = re.compile(
    r"\bFROM\s+ai_problem\b(?:\s+(?:AS\s+)?(?P<alias>[A-Za-z_]\w*))?",
    re.IGNORECASE,
)
_LEGACY_DAMAGE_TYPE_JOIN = re.compile(
    r"(?:\b\w+\.)?damage_type\s*=\s*(?:\b\w+\.)?problem_code|"
    r"(?:\b\w+\.)?problem_code\s*=\s*(?:\b\w+\.)?damage_type",
    re.IGNORECASE,
)
_LENGTH_FALLBACK_SIMPLE = re.compile(
    r"COALESCE\s*\(\s*(?P<length>(?:\w+\.)?disease_length)\s*,\s*"
    r"GREATEST\s*\(\s*(?P<height>(?:\w+\.)?area_height)\s*,\s*"
    r"(?P<width>(?:\w+\.)?area_width)\s*\)\s*\)",
    re.IGNORECASE,
)
_LENGTH_FALLBACK_NULL_SAFE = re.compile(
    r"COALESCE\s*\(\s*(?P<length>(?:\w+\.)?disease_length)\s*,\s*"
    r"GREATEST\s*\(\s*COALESCE\s*\(\s*(?P<height>(?:\w+\.)?area_height)\s*,\s*0\s*\)\s*,\s*"
    r"COALESCE\s*\(\s*(?P<width>(?:\w+\.)?area_width)\s*,\s*0\s*\)\s*\)\s*\)",
    re.IGNORECASE,
)
def table_names(lowered_sql: str) -> set[str]:
    """SQL 里引用的表名，只取基础表名（Wren 编译产物是 schema.table 全限定名）。"""
    return {
        re.split(r"\.", match)[-1].strip('`"[] ')
        for match in _TABLE_PATTERN.findall(lowered_sql)
    }

MYSQL_DIALECT_MARKERS = (
    "::",
    " ilike ",
    "jsonb",
    "to_char(",
    "generate_series",
    "serial",
    "bigserial",
)

POSTGRESQL_DIALECT_MARKERS = (
    "ifnull(",
    "str_to_date(",
    "date_format(",
    "group_concat(",
)


class AmbiguousQuestion(Exception):
    """模型认为问题信息不足（提示词协议：输出 `AMBIGUOUS: <缺什么>`）。

    上层收到后应该去追问用户，而不是硬猜一个条件继续查。
    """


def raise_if_ambiguous(text: str) -> None:
    head = str(text or "").strip().lstrip("`").strip()
    if not head[:10].upper().startswith("AMBIGUOUS"):
        return
    reason = head.split(":", 1)[1].strip() if ":" in head else "信息不足"
    raise AmbiguousQuestion(reason or "信息不足")


def _normalize(sql: str) -> str:
    sql = sql.strip()
    while sql.endswith(";"):
        sql = sql[:-1].rstrip()
    return sql


def normalize_damage_code_joins(sql: str) -> str:
    """修正病害与类型字典的关联，并兼容旧的编码等值写法。

    `ai_problem.damage_type` 的库注释是“损坏程度”，不是病害类型；正确外键是
    `ai_problem.problem_base_id → ai_problem_base.id`。历史模板普遍写成了
    `damage_type = problem_code`，导致只有碰巧同码的几类能显示，其余被并成未识别。
    这里在执行前统一改到正确外键，旧模板不需要逐条手改。
    """

    text = str(sql or "")
    problem_aliases = {
        str(match.group("alias") or "ai_problem")
        for match in _AI_PROBLEM_SOURCE.finditer(text)
    }
    # 历史示例里还有一种“去重子查询只 SELECT problem_code/problem_type，
    # 但关联却写成 p.problem_base_id = b.id”的 SQL：直接执行会报 Unknown column b.id。
    # 先把 id 补进同一个派生表，后续关联改写才能把 code 比较稳定地迁到主键比较。
    text = _DAMAGE_DICT_SUBQUERY_MISSING_ID.sub(
        "(SELECT DISTINCT id, problem_code, problem_type FROM ai_problem_base",
        text,
    )
    text = _DAMAGE_DICT_SUBQUERY_JOIN.sub(
        lambda match: (
            (
                "(SELECT id, problem_type FROM ai_problem_base "
                "WHERE problem_type IS NOT NULL "
                "AND (del_flag = 0 OR del_flag IS NULL)) "
                f"{match.group('dict')} ON {match.group('disease')}.problem_base_id = "
                f"{match.group('dict')}.id"
            )
            if match.group("disease") in problem_aliases
            else match.group(0)
        ),
        text,
    )
    text = _DAMAGE_DICT_DIRECT_JOIN.sub(
        lambda match: (
            f"{match.group('disease')}.problem_base_id = {match.group('dict')}.id"
            if match.group("disease") in problem_aliases
            else match.group(0)
        ),
        text,
    )
    text = _DAMAGE_DICT_CAST_JOIN.sub(
        lambda match: (
            f"{match.group('dict')}.id = {match.group('disease')}.problem_base_id"
            if match.group("disease") in problem_aliases
            else match.group(0)
        ),
        text,
    )

    return text


def has_legacy_damage_type_join(sql: str) -> bool:
    """规范化后仍存在的“损坏程度→病害代码”旧关联。"""
    return bool(_LEGACY_DAMAGE_TYPE_JOIN.search(str(sql or "")))


def normalize_disease_length_expr(sql: str) -> str:
    """实际长度：优先正数 disease_length；NULL/0 时回退高度、宽度的较大值。"""

    def _replace(match: re.Match[str]) -> str:
        length = match.group("length")
        height = match.group("height")
        width = match.group("width")
        return (
            f"CASE WHEN {length} IS NOT NULL AND {length} > 0 THEN {length} "
            f"ELSE GREATEST(COALESCE({height}, 0), COALESCE({width}, 0)) END"
        )

    text = _LENGTH_FALLBACK_NULL_SAFE.sub(_replace, str(sql or ""))
    return _LENGTH_FALLBACK_SIMPLE.sub(_replace, text)


def dialect_issues(sql: str, kind: str) -> list[str]:
    """检测 SQL 是否混用了错误数据库方言的语法。"""
    lowered = sql.lower()
    issues: list[str] = []
    if kind == "mysql":
        if any(marker in lowered for marker in MYSQL_DIALECT_MARKERS):
            issues.append("检测到 PostgreSQL 方言语法（::/ILIKE/JSONB/to_char/generate_series/SERIAL 等）")
    else:
        if "`" in sql:
            issues.append("检测到 MySQL 反引号标识符")
        if re.search(r"\blimit\s+\d+\s*,\s*\d+", lowered):
            issues.append("检测到 MySQL 的 LIMIT offset,count 语法")
        if any(marker in lowered for marker in POSTGRESQL_DIALECT_MARKERS):
            issues.append("检测到 MySQL 专属函数（IFNULL/STR_TO_DATE/DATE_FORMAT/GROUP_CONCAT 等）")
    return issues


def extract_sql(llm_response: str) -> str:
    """Extract the last SQL statement from a model response."""
    text = llm_response.strip()

    for pattern in (
        r"```sql\s*\n(.*?)```",
        r"```\s*\n(.*?)```",
    ):
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        if matches:
            return _normalize(matches[-1])

    for pattern in (
        r"\bWITH\b.*?(?:;|$)",
        r"\bSELECT\b.*?(?:;|$)",
    ):
        matches = re.findall(pattern, text, re.DOTALL | re.IGNORECASE)
        if matches:
            return _normalize(matches[-1])
    return _normalize(text)


def add_del_flag_filter(sql: str) -> str | None:
    """给"忘了写 del_flag=0"的单表查询自动补上，而不是整条判失败。

    实测问字典/台账的属性（"病害类型字典里每条记录的启用状态"）时，模型只写 SELECT ... FROM 表，
    旧逻辑直接判"业务表查询必须包含 del_flag=0" → 整题不可答。补条件的语义和原来的校验一致
    （不返回已删除记录），只是一个自动补、一个直接拒绝。
    只处理最简单的单表查询（没有 JOIN/UNION），拿不准就返回 None 让调用方继续报错。
    """
    if not sql or re.search(r"\b(join|union)\b", sql, re.I):
        return None
    tail = re.search(r"(?is)\s+(order\s+by|group\s+by|limit|having)\b", sql)
    if re.search(r"\bwhere\b", sql, re.I):
        clause = " AND del_flag = 0 "
    else:
        clause = " WHERE del_flag = 0 "
    if tail:
        return sql[: tail.start()] + clause + sql[tail.start() :]
    return sql + clause


def validate_sql(sql: str, kind: str) -> tuple[bool, str, str]:
    sql = _normalize(sql)
    if kind == "mysql":
        sql = normalize_damage_code_joins(sql)
        sql = normalize_disease_length_expr(sql)
        if has_legacy_damage_type_join(sql):
            return (
                False,
                "检测到用损坏程度 damage_type 关联病害类型字典；"
                "病害类型必须使用 problem_base_id = ai_problem_base.id。",
                sql,
            )
    lowered = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE)
    lowered = re.sub(r"/\*.*?\*/", "", lowered, flags=re.DOTALL).lower()
    # Wren 编译出来的物理 SQL 是「WITH 模型 CTE ... SELECT ...」，同样只读。
    if not lowered.startswith(("select", "with")):
        return False, "只允许执行 SELECT 查询。", sql
    if _DANGEROUS_PATTERN.search(lowered):
        return False, "检测到危险 SQL 关键字。", sql
    if ";" in sql:
        return False, "一次只允许执行一条 SQL。", sql

    allowed = settings.mysql_allowed_tables if kind == "mysql" else settings.pg_allowed_tables
    used_tables = table_names(lowered)
    if not used_tables.intersection(set(allowed)):
        return False, f"SQL 使用的表不在白名单中：{', '.join(sorted(used_tables))}", sql
    if kind == "mysql" and "del_flag" not in lowered:
        patched = add_del_flag_filter(sql)
        if not patched:
            return False, "业务表查询必须包含 del_flag=0 过滤条件。", sql
        sql = patched
        lowered = re.sub(r"--.*?$", "", sql, flags=re.MULTILINE).lower()
        log_event("sql_engine", "INFO", "del_flag_auto_added", sql=sql[:120])
    dialect = dialect_issues(sql, kind)
    if dialect:
        return False, "；".join(dialect), sql
    if not re.search(r"\blimit\s+\d+", lowered):
        sql = f"{sql} LIMIT {settings.sql_max_rows}"
    return True, "", sql


def apply_row_limit(sql: str, limit: int) -> str:
    """把 SQL 末尾的 LIMIT 换成指定值（没有就补一个）。

    界面为了性能只取前几百行，导出 CSV 时要换成一个很大的上限重跑，
    否则下载到的只是屏幕上那点数据。
    """
    stripped = re.sub(r"\s+limit\s+\d+(\s*,\s*\d+)?\s*$", "", sql, flags=re.IGNORECASE)
    return f"{stripped} LIMIT {int(limit)}"


def generate_mdl_sql(
    question: str,
    models_text: str,
    required_conditions: list[str],
    slots: dict[str, Any],
    feedback: str = "",
    examples: str = "",
    errors: str = "",
    samples: str = "",
) -> str:
    """让 LLM 写 MDL-SQL（引用模型/字段名，不写物理表名），交给 Wren 编译。

    见 docs/WrenAI集成方案.md 阶段三：LLM 只负责选模型和字段，JOIN/字典/JSON/del_flag
    全部由 Wren 编译器补。
    """
    raise_disabled("sql_mdl")


def generate_sql(
    question: str,
    kind: str,
    schema_context: str,
    examples: str,
    required_conditions: list[str],
    slots: dict[str, Any],
    feedback: str = "",
    samples: str = "",
) -> str:
    raise_disabled("sql_physical")


_UNREQUESTED_ENUM_CONTEXTS: dict[str, tuple[str, ...]] = {
    "problem_source": (
        "病害来源",
        "来源",
        "手动新增",
        "车辆巡检",
        "人工巡检",
        "新增历史工单",
        "路长转养护",
        "路政转养护",
    ),
    "operate_type": ("病害操作", "操作类型"),
    "problem_status": ("工单状态", "病害状态"),
    "enable_type": ("基础状态",),
    "track_problem": ("重点关注",),
    "collect": ("收藏",),
}


def strip_unrequested_enum_filters(sql: str, question: str) -> str:
    """移除模型自行添加、但用户没有点名字段上下文的枚举过滤。

    “新增情况”是时间范围内新增的记录，不等于 `problem_source=15/0`。这类错误会
    把整条查询静默收窄成 0 行，因此在执行前按字段来源做最后一道确定性约束。
    """
    text = str(sql or "")
    message = str(question or "")
    for field, contexts in _UNREQUESTED_ENUM_CONTEXTS.items():
        if any(word in message for word in contexts):
            continue
        pattern = re.compile(
            rf"\s+AND\s+(?:[A-Za-z_]\w*\.)?`?{re.escape(field)}`?\s*=\s*"
            r"(?:'(?:''|[^'])*'|-?\d+(?:\.\d+)?)",
            re.IGNORECASE,
        )
        updated = pattern.sub("", text)
        if updated != text:
            log_event(
                "sql_engine",
                "WARNING",
                "unrequested_enum_filter_stripped",
                field=field,
                question=message[:80],
            )
            text = updated
    return text


def execute_sql(kind: str, sql: str, question: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    event_bus.emit_event(
        "action",
        layer="sql",
        tool="execute_sql",
        args={"kind": kind, "sql_chars": len(str(sql or ""))},
    )
    sql = extract_sql(sql)
    if question:
        sql = strip_unrequested_enum_filters(sql, question)
    ok, error, sql = validate_sql(sql, kind)
    if not ok:
        event_bus.emit_event(
            "observation",
            layer="sql",
            tool="execute_sql",
            ok=False,
            row_count=0,
            produced={},
            truncated=False,
            empty=False,
            duration_ms=round((time.perf_counter() - started) * 1000),
            summary=error[:240],
        )
        raise DatabaseError(error)
    result = db_manager.query(kind, sql)
    rows = list(result.get("rows") or [])
    event_bus.emit_event(
        "observation",
        layer="sql",
        tool="execute_sql",
        ok=True,
        rows=min(len(rows), 10),
        row_count=len(rows),
        produced={},
        truncated=len(rows) >= settings.sql_max_rows,
        empty=not rows,
        duration_ms=round((time.perf_counter() - started) * 1000),
        summary=f"SQL 返回 {len(rows)} 行",
    )
    return result
