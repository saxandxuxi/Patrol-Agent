"""示例检索与复用：向量/骨架召回 → 结构同型 → 槽位对齐 → 回填执行。

从 `nl2sql.py` 拆出（docs/代码走查与目录结构重构方案.md 第三节）。与"入库/对账"侧
（`save_successful_pair` / `import_ragflow_examples` / `backfill_*` / 题目索引）**分开**：
这边只管"怎么把示例找出来并回填成可执行 SQL"。

`nl2sql.py` 里按原名 re-export，`nl2sql.find_reusable_examples(...)` 这类调用不受影响。

少数几个仍住在 nl2sql 的入库/索引侧工具（示例库文档读取、归一化、计划 spec 转换等）
通过 `_core()` 在函数内按需取——这样不会形成 nl2sql ⇄ example_reuse 的导入环。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from ..store import list_recorded_example_pairs
from ..config import settings
from . import calibrate
from .rag import document_content, list_documents
from .resource_loader import load_capability_file
from .logging_service import log_error, log_event
from .route_resolver import _known_damage_types, _resolve_route
from .sql_engine import validate_sql as sql_engine_validate


def _core():
    """按需取 nl2sql 里的入库/索引侧工具（避免循环 import）。"""
    from . import nl2sql

    return nl2sql


def _pair_document_content(pair_name: str) -> str:
    """按 doc_name 去示例库取那篇完整文档正文（本地表里没有时的兜底）。"""
    dataset_id = _core()._ensure_example_dataset()
    if not dataset_id or not pair_name:
        return ""
    try:
        for doc in list_documents(dataset_id):
            if str(doc.get("name")) == pair_name and str(doc.get("id")):
                return document_content(dataset_id, str(doc["id"]))
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "pair_document_fetch_failed", exc, doc_name=pair_name)
    return ""


def get_similar_question_sql(question: str, limit: int = 3) -> str:
    chunks = _core()._example_chunks(question, limit=limit)
    if chunks:
        return "\n\n".join(chunk.get("content", "") for chunk in chunks)
    return _core()._local_example_text(question, limit=limit)


def _example_key(text: str) -> str:
    """问题归一化：去掉空白/标点/全角差异，用来判"是不是同一句"。

    保守起见第一版只做"同一句"复用（同义改写仍走 Wren）——宁可少复用，也不能把
    一条确认过的 SQL 套到条件不同的新问题上（方案第七章"宁可不匹配不可错匹配"）。
    """
    text = str(text or "").lower()
    return re.sub(r"[\s，。；：、,.;:!？?！~\-—（）()【】\[\]\"'“”‘’]+", "", text)


# 示例能"表达"的过滤条件维度：计划的这些条件必须和示例一一对上才允许直接重放。
_REUSE_CONDITION_FIELDS = (
    "route",
    "routes",
    "damage_type",
    "time_range",
    "pile_range",
    "pile_side",
    "pile_split",
    "device_code",
)


def _reuse_condition_keys(spec: dict[str, Any]) -> frozenset[str]:
    return frozenset(name for name in _REUSE_CONDITION_FIELDS if spec.get(name))


_MULTI_OBJECT_TIME = re.compile(r"(\d{1,2})\s*月")
# 编号类对象（巡查编号 XCBH…/巡检任务号 XJJL…/病害编号）：一句话里出现 ≥2 个不同编号
# 就是多对象（"XCBH001 和 XCBH002 各有多少处"）
_MULTI_OBJECT_CODE = re.compile(r"\b[A-Z]{2,6}\d{6,}\b")


def is_multi_object(
    spec: dict[str, Any] | None, question: str = "", deep: bool = False
) -> bool:
    """这条问答是不是"多对象对比"（≥2 条路线/桥梁/边坡/隧道/病害类型/月份）。

    docs/模板沉淀标准与复用校验_系统性方案.md §2.2-6：多对象对比**不沉淀自动模板、不做别名
    替换**——多对象的 SQL 常有多个 CTE/子查询，别名替换是"A→B、B→C、C→A"这种循环替换，
    极易出错，而收益（省一次模型调用）远不值这个风险。这类题保留"相似问法召回"这条路。

    deep=False（默认，**弹卡热路径用这个**）：只做纯 CPU 判断——spec 里的多对象列表 +
    问题里的多月份/多编号。**不查库**：每张候选卡都去查路线/资产表，隧道一断就是每次 8 秒超时。
    deep=True（挖掘/清理这类离线路径用）：再补"问题里提到 ≥2 条路线 / ≥2 个桥隧坡"的查库判断。
    """
    data = spec or {}

    def _items(key: str) -> list[str]:
        value = data.get(key)
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if str(item or "").strip()]
        return [str(value)] if str(value or "").strip() else []

    if len(_items("routes")) >= 2 or len(_items("route_ids")) >= 2:
        return True
    if len(_items("damage_type")) >= 2:
        return True
    if "multi_route" in [str(item) for item in (data.get("must_keep") or [])]:
        return True
    text = str(question or "")
    if not text:
        return False
    # 多月份并列（"7月工单和8月工单各有多少"）也是多对象：挖成模板会永远查这两个月
    if len(set(_MULTI_OBJECT_TIME.findall(text))) >= 2:
        return True
    if len(set(_MULTI_OBJECT_CODE.findall(text.upper()))) >= 2:
        return True
    return False


# 条件键 → 归一化 SQL 里对应的占位符名：用来判断"这条示例的 SQL 能不能表达本轮这个条件"。
# 举例：示例 SQL 是 `DATE_FORMAT(collection_time,'%Y-%m') = '2026-07'`（月份写死），
# spec 里却有 time_range —— 这种示例对"8 月"是**表达不了**的：卡片上如果按本轮显示 8 月、
# SQL 却仍按 7 月查，就是"卡片说 8 月、结果 7 月"的静默错答。
_CONDITION_PLACEHOLDERS: dict[str, tuple[str, ...]] = {
    "route": ("route_id", "route_name", "route_ids", "route_id_2", "route_name_2"),
    "routes": ("route_ids", "route_id", "route_name", "route_id_2", "route_name_2"),
    "damage_type": ("damage_type", "damage_type_2"),
    "time_range": ("time_start", "time_end"),
    "pile_range": ("pile_start", "pile_end"),
    "device_code": ("device_code",),
    "up_or_down": ("up_or_down",),
}


def _sql_supports_condition(sql: str, key: str) -> bool:
    """SQL 里有没有对应占位符（有=能按本轮取值填）。"""
    for name in _CONDITION_PLACEHOLDERS.get(key, (key,)):
        if "{" + name + "}" in str(sql or ""):
            return True
    return False


def _parses_as_sql(sql: str) -> bool:
    """这条 SQL 语法上能不能解析（例：回填把引号拼坏了要在这里拦住）。"""
    try:
        import sqlglot

        return bool(sqlglot.parse(str(sql or ""), read="mysql"))
    except Exception:  # noqa: BLE001
        return False


def _spec_value(spec: dict[str, Any], key: str) -> str:
    """spec/plan 在某个条件上的取值文本（用于判"SQL 里写死的值是否正好等于本轮"）。"""
    spec = spec or {}
    if key in {"route", "routes"}:
        return str(spec.get("route_id") or spec.get("route") or "")
    if key == "damage_type":
        value = spec.get("damage_type") or []
        items = value if isinstance(value, list) else [value]
        return "、".join(str(item) for item in items if item)
    if key in {"time_range", "pile_range"}:
        span = spec.get(key) or []
        if isinstance(span, (list, tuple)) and len(span) == 2:
            return f"{span[0]}~{span[1]}"
        return ""
    return str(spec.get(key) or "")


def _reuse_plan_spec(plan: dict[str, Any]) -> dict[str, Any]:
    """当前计划的"结构 + 槽位值"：结构判同型，槽位值用来填示例 SQL 的占位。"""
    # 传进来的本来就是 spec（approve 时手里只有 spec，没有完整计划）就直接用
    if plan.get("shape") and not plan.get("slots"):
        return dict(plan)
    spec = _core().plan_to_spec(plan)
    route = (plan.get("slots") or {}).get("route_match") or {}
    if route.get("id"):
        spec["route_id"] = str(route["id"])
    start, end = (plan.get("slots") or {}).get("time_start"), (plan.get("slots") or {}).get("time_end")
    if start and end:
        spec.setdefault("time_range", [start, end])
    return spec


def _question_similarity(left: str, right: str) -> float:
    """问题文本相似度（本地兜底用）：2 字滑窗的 Jaccard，够判"同义不同字"。"""
    def grams(text: str) -> set[str]:
        cleaned = re.sub(r"[\s，。；：、,.;:!？?！~\-—（）()【】\[\]\"'“”‘’]+", "", str(text or ""))
        return {cleaned[i : i + 2] for i in range(max(len(cleaned) - 1, 0))} or {cleaned}

    a, b = grams(left), grams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _question_skeleton(text: str) -> str:
    """把问题里的"具体值"（路线名/病害类型/数字）换成占位后的骨架。

    "宝安中心--恒明珠的泛油有多少处" 和 "西乡段泛油病害有多少处" 字面差很远，但骨架
    都是"路线病害类型有多少处"——槽位对齐要的正是这种同型判断，值本身由计划填。
    """
    out = str(text or "")
    names, type_names = _skeleton_vocab()
    for name in names:
        out = out.replace(name, "路线")
    for type_name in type_names:
        if type_name in out:
            out = out.replace(type_name, "病害类型")
    out = re.sub(r"\d+", "N", out)
    return _example_key(out)


# 骨架化用的词表（路线名 / 病害类型名）：以前每次调用都去库里查一遍，
# 而骨架化在一次候选匹配里会被调几十次 —— 实测这一项就吃掉 6 秒（库还在 SSH 隧道后面）。
_SKELETON_VOCAB: tuple[float, tuple[str, ...], tuple[str, ...]] | None = None
_SKELETON_VOCAB_TTL = settings.example_vocab_ttl_seconds


def _skeleton_vocab() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(路线名, 病害类型名)，按长度倒序；进程内缓存 10 分钟（只用于换占位，不怕过期）。"""
    global _SKELETON_VOCAB
    now = time.time()
    if _SKELETON_VOCAB and now - _SKELETON_VOCAB[0] < _SKELETON_VOCAB_TTL:
        return _SKELETON_VOCAB[1], _SKELETON_VOCAB[2]
    try:
        names = tuple(
            sorted(
                {str(name) for name in calibrate.route_names() if str(name or "").strip()},
                key=len,
                reverse=True,
            )
        )
    except Exception:  # noqa: BLE001
        names = ()
    try:
        types = tuple(
            sorted(
                {
                    str(item.get("problem_type"))
                    for item in _known_damage_types()
                    if str(item.get("problem_type") or "").strip()
                },
                key=len,
                reverse=True,
            )
        )
    except Exception:  # noqa: BLE001
        types = ()
    _SKELETON_VOCAB = (now, names, types)
    return names, types


def _skeleton_similarity(left: str, right: str) -> float:
    return _question_similarity(_question_skeleton(left), _question_skeleton(right))


def infer_example_spec(question: str, sql: str) -> dict[str, Any]:
    """从 SQL 反推示例的"结构 + 条件维度"（老示例没存 spec 时用）。

    只用来判"能不能直接重放"：形状/主体/过滤维度；**判不准就标成不可用**
    （后面 `_example_spec_usable` 会检查归一化后还有没有残留字面值）。

    形状/主体/维度一律看 **AST 的最外层查询**，不用 `"group by" in lower` 这类子串判断：
    本项目的示例大多是 Wren 产出的 `WITH xxx AS (...)` 包一层，子查询/CTE 里出现 GROUP BY
    会把"计数题"误判成 distribution，进而错判能不能重放（docs/代码走查与目录结构重构方案.md
    第 2 条）。解析不出来才退回老的子串逻辑。
    """
    text = str(sql or "")
    tree = None
    exp_types: Any = None
    try:
        import sqlglot
        from sqlglot import exp as exp_types

        statements = sqlglot.parse(text, read="mysql")
        tree = statements[0] if statements else None
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "infer_spec_parse_failed", exc, sql=text[:120])
    if tree is not None and exp_types is not None and isinstance(tree, exp_types.Select):
        return _infer_spec_from_tree(tree)
    return _infer_spec_from_text(text)


def _infer_spec_from_tree(tree: Any) -> dict[str, Any]:
    """AST 版：形状看**最外层** SELECT（分组/排序/聚合），过滤维度看**整条语句**。

    为什么分开看：形状是"这条查询最后给用户什么"，当然只看最外层；而过滤条件（路线/病害类型/
    时间/桩号/设备）不管写在哪一层，都是这条问题的口径，归一化回填也要在那一层替换，
    所以整条语句里出现的都算（Wren 产出的 `WITH ... AS (...)` 把条件包在 CTE 里很常见）。
    """
    from sqlglot import exp

    spec: dict[str, Any] = {}
    group = tree.args.get("group")
    order = tree.args.get("order")
    # 平台默认行数上限（LIMIT 500 这类）不是"取前 N 名"：只有落在配置的 TopN 区间里才算
    limit_value = _limit_number(tree.args.get("limit"))
    low, high = _top_n_range()
    has_top_n = limit_value is not None and low <= limit_value <= high
    aggregates = [
        str(node.key or "").lower()
        for item in tree.expressions or []
        for node in item.find_all(exp.AggFunc)
    ]
    if group is not None:
        # 最外层分组 + 排序 + "取前 N 名" = TopN；否则是按维度分布
        spec["shape"] = "top" if (order is not None and has_top_n) else "distribution"
    elif order is not None and has_top_n:
        spec["shape"] = "top"  # 不分组取极值（"面积最大的那条"）
    elif "count" in aggregates:
        spec["shape"] = "count"
    elif any(name in {"sum", "avg"} for name in aggregates):
        spec["shape"] = "sum"
    else:
        spec["shape"] = "list"

    tables = {str(table.name or "").lower() for table in tree.find_all(exp.Table)}
    if "ai_maintain_patrol" in tables:
        spec["subject"] = "patrol"
    elif "ai_problem" not in tables and "basic_facilities_routes" in tables:
        spec["subject"] = "route"
    else:
        spec["subject"] = "disease"

    alias_tables = {
        str(table.alias_or_name or "").lower(): str(table.name or "").lower()
        for table in tree.find_all(exp.Table)
        if table.alias_or_name
    }
    predicates = [
        node
        for node in tree.find_all(exp.Expression)
        if isinstance(node, (exp.EQ, exp.NEQ, exp.In, exp.Between, exp.GT, exp.GTE, exp.LT, exp.LTE))
    ]
    for node in predicates:
        left = node.this if hasattr(node, "this") else None
        if not isinstance(left, exp.Column):
            continue
        name = str(left.name or "").lower()
        qualifier = str(left.table or "").lower()
        is_equality = isinstance(node, (exp.EQ, exp.In))
        if is_equality and (
            name in {"routes_id", "route_id"}
            or (name == "id" and alias_tables.get(qualifier) == "basic_facilities_routes")
        ):
            spec.setdefault("route", "")
        elif is_equality and name in {"problem_type", "damage_type"}:
            value = _predicate_literal(node)
            # 库里用 'NULL' 当哨兵（`problem_type <> 'NULL'` / `IN ('NULL')`），那不是病害类型条件
            if value and value.strip().upper() not in {"NULL", "NONE", "UNKNOWN"}:
                spec.setdefault("damage_type", value)
        elif name == "collection_time" and isinstance(
            node, (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
        ):
            spec.setdefault("time_range", [""])
        elif name == "pile_distance" and isinstance(node, exp.Between):
            spec.setdefault("pile_range", [0, 1])
        elif name == "device_code" and isinstance(node, exp.EQ):
            spec.setdefault("device_code", "")
    return spec


def _limit_number(limit: Any) -> int | None:
    """LIMIT 的字面值（不是常量就返回 None）。"""
    from sqlglot import exp

    expression = getattr(limit, "expression", None)
    if isinstance(expression, exp.Literal):
        text = str(expression.this)
        if text.isdigit():
            return int(text)
    return None


def _top_n_range() -> tuple[int, int]:
    """TopN 白名单区间（和挖模板共用同一份配置）。"""
    from .resource_loader import load_capability_file

    cfg = load_capability_file("predicates.yaml", {}) or {}
    low, high = (cfg.get("limit_range") or [1, 50])[:2]
    return int(low), int(high)


def _predicate_literal(node: Any) -> str:
    """谓词里的字面值（`= '泛油'` 或 `IN ('泛油','坑槽')` 取第一个）。"""
    from sqlglot import exp

    if isinstance(node, exp.In):
        for item in node.expressions or []:
            if isinstance(item, exp.Literal) and item.is_string:
                return str(item.this)
        return ""
    value = node.expression if hasattr(node, "expression") else None
    if isinstance(value, exp.Literal) and value.is_string:
        return str(value.this)
    return ""


def _infer_spec_from_text(text: str) -> dict[str, Any]:
    """老路子（子串判断）：只在 AST 解析不出来时兜底。"""
    spec: dict[str, Any] = {}
    lower = text.lower()
    if "group by" in lower:
        spec["shape"] = "distribution"
        if "order by" in lower and "limit" in lower:
            spec["shape"] = "top"
    elif re.search(r"\bcount\s*\(", lower):
        spec["shape"] = "count"
    elif re.search(r"\b(sum|avg)\s*\(", lower):
        spec["shape"] = "sum"
    else:
        spec["shape"] = "list"
    if "ai_maintain_patrol" in lower:
        spec["subject"] = "patrol"
    elif "ai_problem" not in lower and "basic_facilities_routes" in lower:
        spec["subject"] = "route"
    else:
        spec["subject"] = "disease"
    if re.search(r"(routes_id|route_id|\br\.id)\s*(=|in)", lower):
        spec["route"] = ""          # 只表示"这条示例有条路线条件"，值由计划填
    types = re.findall(r"problem_type\s*(?:=|in)\s*\(?\s*'([^']+)'", text)
    if types:
        spec["damage_type"] = types[0]
    if re.search(r"collection_time\s*(>=|>|=)", lower):
        spec["time_range"] = [""]   # 同上：只作维度声明
    if re.search(r"pile_distance\s+between", lower):
        spec["pile_range"] = [0, 1]
    if re.search(r"device_code\s*=", lower):
        spec["device_code"] = ""
    return spec


def _example_spec_usable(sql: str, spec: dict[str, Any]) -> bool:
    """归一化后还剩具体字面值 → 说明有我没识别出来的条件，宁可不复用。"""
    try:
        normalized = _core()._normalize_sql_for_example(sql, spec)
    except Exception:  # noqa: BLE001
        return False
    remainder = re.sub(r"\{[a-z_0-9]+\}", "", normalized)
    # 还留着"= '某某'"或"= 123"这种具体值比较 → 有未识别的条件
    return not re.search(r"=\s*'[^']+'|=\s*-?\d+(\.\d+)?\b", remainder)


def _reuse_candidates(
    question: str, plan_spec: dict[str, Any], limit: int = 6
) -> list[tuple[float, dict[str, Any], str]]:
    """召回候选示例：向量（RAGFlow）+ 本地骨架相似度。

    **不再看 trusted**：凡是进了示例库 / RAGFlow 问答集 / 固定看板的题一律参与召回
    （它们本来就是人工存进去的）。能用不能用交给后面的同问打分 + 用户在确认卡上点确认，
    不再靠一个事前的"已确认"标记把候选整条筛掉
    （docs/示例参数对齐与同义召回放宽方案.md 第二节）。
    """
    recorded = list_recorded_example_pairs(limit=500)
    sample_questions: dict[str, str] = {}   # 骨架 → 原始问题
    sample_names: dict[str, str] = {}
    for row in recorded:
        text = str(row.get("question") or "")
        if len(_example_key(text)) < 4:
            continue
        sample_questions[_example_key(text)] = text
        sample_names[_example_key(text)] = str(row.get("doc_name") or "")
    if not sample_questions:
        return []

    scored: dict[str, tuple[float, dict[str, Any], str]] = {}
    by_name = {
        str(row.get("doc_name") or ""): row
        for row in recorded
        if str(row.get("doc_name") or "")
    }
    by_question = {
        _example_key(str(row.get("question") or "")): row
        for row in recorded
        if str(row.get("question") or "")
    }
    # 1) 向量召回：**优先题目索引**（检索单元=问题，不被 SQL 稀释），没有就用整篇示例库兜底
    # 题目索引文档里只有问题，没有结构 → 同型重排要用的 spec 从本地示例按文档名取
    specs_by_name: dict[str, dict[str, Any]] = {}
    for name, row in by_name.items():
        try:
            specs_by_name[name] = json.loads(str(row.get("spec_json") or "{}")) or {}
        except (ValueError, TypeError):
            specs_by_name[name] = {}
    vector_hits = _core()._question_index_chunks(
        question, plan=plan_spec, limit=settings.example_recall_topk, specs_by_name=specs_by_name
    )
    vector_source = "question_index"
    if not vector_hits:
        vector_hits = _core()._example_chunks(
            question, plan=plan_spec, limit=settings.example_recall_topk
        )
        vector_source = "vector"
    for chunk in vector_hits:
        sample_question = str(chunk.get("_question") or "")
        if sample_question:
            # 题目索引命中：SQL 按关联名回本地表取（没有再去示例库拿）
            pair_name = str(chunk.get("_pair") or "") or str(
                chunk.get("document_keyword") or chunk.get("doc_name") or ""
            )
            # 关联名优先；对不上就按"问题文本"回查本地表（题目索引的关联行就是在本地表里）
            row = by_name.get(pair_name) or by_question.get(_example_key(sample_question))
            spec = chunk.get("_spec") or {}
            if not spec and row:
                # 题目索引文档里只有"问题 + 结构头"，没有完整 spec → 取本地那条的 spec
                # （必须在归一化**之前**补上，否则等于拿空 spec 去归一化，时间等条件替换不出来）
                try:
                    spec = json.loads(str(row.get("spec_json") or "{}")) or {}
                except (ValueError, TypeError):
                    spec = {}
            # 本地表存的是"当时真跑通"的**原始 SQL**（条件写死在里面），要按 spec 归一化成
            # 带占位符的版本再重放 —— 否则用户在卡上把"7 月"改成"8 月"也改不动 SQL。
            # 归一化是纯文本处理（毫秒级），不再对每个候选调一次 physical_sql_of（那要 EXPLAIN，
            # 一次 0.3s，20 条候选就是好几秒）；真正执行前 capabilities._executable_sql
            # 还会对选中的那条做编译/EXPLAIN 检查，安全性不丢。
            raw_sql = str((row or {}).get("sql_text") or "").strip()
            # 优先用**入库时就算好的**归一化版本（有占位符、可换参数），没有再现场归一化
            effective = str((row or {}).get("normalized_sql") or "").strip()
            if not effective and raw_sql:
                effective = _core()._normalize_sql_for_example(raw_sql, spec)
            if not effective:
                parsed = parse_example_document(_pair_document_content(pair_name))
                effective = _core().physical_sql_of(
                    str(parsed.get("physical_sql") or parsed.get("sql") or ""),
                    _core().example_kind(spec or parsed.get("spec") or {}),
                )
                spec = spec or (parsed.get("spec") or {})
                sample_question = sample_question or str(parsed.get("question") or "")
            if not effective:
                continue
            sample_key = _example_key(sample_question)
            score = max(
                float(chunk.get("similarity") or 0),
                _skeleton_similarity(question, sample_question),
            )
            scored.setdefault(
                sample_key,
                (
                    score,
                    {
                        "spec": spec,
                        "sql": effective,
                        "question": sample_question,
                        "doc_name": pair_name or sample_names.get(sample_key, ""),
                    },
                    vector_source,
                ),
            )
            continue
        # 整篇示例库命中（旧路径兜底）
        parsed = parse_example_document(str(chunk.get("content") or ""))
        effective = str(parsed.get("physical_sql") or "")
        if not effective:
            effective = _core().physical_sql_of(
                str(parsed.get("sql") or ""), _core().example_kind(parsed.get("spec") or {})
            )
        if not effective:
            continue
        parsed = {**parsed, "sql": effective}
        sample_question = str(parsed.get("question") or "")
        sample_key = _example_key(sample_question)
        if sample_key not in sample_questions:
            continue
        score = max(
            float(chunk.get("similarity") or 0),
            _skeleton_similarity(question, sample_question),
        )
        scored.setdefault(
            sample_key,
            (score, {**parsed, "doc_name": sample_names.get(sample_key, "")}, vector_source),
        )
    # 2) 本地骨架相似度（RAGFlow 不可用或向量还没解析好时，这是唯一的路）
    for sample_key, question_text in sample_questions.items():
        score = _skeleton_similarity(question, question_text)
        if sample_key in scored and scored[sample_key][0] >= score:
            continue
        row = next(
            (item for item in recorded if _example_key(str(item.get("question") or "")) == sample_key),
            None,
        )
        if not row:
            continue
        spec = {}
        try:
            spec = json.loads(str(row.get("spec_json") or "{}")) or {}
        except Exception:  # noqa: BLE001
            spec = {}
        raw_sql = _core().physical_sql_of(
            str(row.get("normalized_sql") or row.get("sql_text") or ""),
            _core().example_kind(spec or row),
        )
        if not raw_sql:
            continue  # Wren MDL-SQL 且编译不出来 → 这条示例没法重放
        if not spec:
            # 老示例没存 spec：从 SQL 反推；判不准（有残留字面值）就不参与直接复用
            spec = infer_example_spec(question_text, raw_sql)
            if not _example_spec_usable(raw_sql, spec):
                continue
        sql = _core()._normalize_sql_for_example(raw_sql, spec)
        if not sql.strip():
            continue
        scored[sample_key] = (
            score,
            {"spec": spec, "sql": sql, "question": question_text,
             "doc_name": sample_names.get(sample_key, "")},
            "local",
        )
    candidates = sorted(scored.values(), key=lambda item: -item[0])
    return candidates[:limit]


def find_reusable_examples(
    question: str,
    kind: str = "mysql",
    plan: dict[str, Any] | None = None,
    limit: int = 1,
    loose: bool = False,
) -> list[dict[str, Any]]:
    """第 2 道闸的完整版：向量召回 → 结构同型 → 槽位对齐 → 条件覆盖，全过才算命中。

    - plan=None：只判"问法同一句"（纯本地，给 ask 模式/意图短路用，最省）；
    - 给了 plan：走完整版——随机问法也能召回同义示例，把该示例的**归一化 SQL** 用本次
      计划的槽位填回去直接执行；多出示例表达不了的过滤条件（收藏/工单状态/面积阈值…）
      一律不命中，转 Wren（docs/固定看板重构与收藏固化方案.md 第七章 §3）。

    返回**有序候选**（人工确认卡要列出来给人选），limit=1 时等价于旧的单条命中。

    loose=True 是"有打分模型 + 用户确认兜底"的那条路（L2 示例直达）：结构（shape/主体）不一致
    也先交给打分模型判断，不在召回阶段就把候选筛掉 —— 实测"数据库里每种资产类型有多少个资产？"
    就因为库里那条示例的 spec 主体被记成了 disease，连打分模型都没看到
    （docs/示例参数对齐与同义召回放宽方案.md 第二节）。
    """
    hits: list[dict[str, Any]] = []
    key = _example_key(question)
    if len(key) < 4:
        return hits
    recorded = list_recorded_example_pairs(limit=500)
    if plan is None:
        for row in recorded:
            if _example_key(str(row.get("question") or "")) != key:
                continue
            sql = str(row.get("sql_text") or "").strip()
            if not sql:
                continue
            ok, _reason, _clean = sql_engine_validate(sql, kind)
            if ok:
                hits.append(
                    {
                        "sql": sql,
                        "normalized_sql": sql,
                        "spec": {},
                        "doc_name": str(row.get("doc_name") or ""),
                        "question": str(row.get("question") or ""),
                        "score": 1.0,
                        "reasons": ["问法完全一致（本地示例）"],
                        "confidence": "exact",
                    }
                )
        return hits[:limit]

    plan_spec = _reuse_plan_spec(plan)
    if not plan_spec.get("shape"):
        return hits
    plan_keys = _reuse_condition_keys(plan_spec)
    names_by_question = {
        _example_key(str(row.get("question") or "")): str(row.get("doc_name") or "")
        for row in recorded
    }
    for score, parsed, source in _reuse_candidates(question, plan_spec):
        spec = parsed.get("spec") or {}
        sql = str(parsed.get("sql") or "")
        sample_question = str(parsed.get("question") or "")
        spec_keys = _reuse_condition_keys(spec)
        # 本轮要用的每个条件，示例 SQL 必须能表达：要么有对应占位符，要么写死的值正好等于本轮。
        # 否则（例如"7 月"这种月份写死在 SQL 里的示例被拿来答"8 月"）整条弃权，
        # 交给模板/Wren —— 宁可慢一点，也不能"卡片显示 8 月、实际按 7 月查"。
        unsupported = next(
            (
                key
                for key in sorted(plan_keys)
                if not _sql_supports_condition(sql, key)
                and _spec_value(spec, key) != _spec_value(plan_spec, key)
            ),
            "",
        )
        if unsupported:
            log_event(
                "nl2sql",
                "INFO",
                "example_condition_unsupported",
                doc_name=str(parsed.get("doc_name") or ""),
                condition=unsupported,
                question=question[:60],
            )
            continue
        if loose:
            # 宽松：示例能覆盖本轮**全部**条件即可；示例多带的条件不算错——它们会作为
            # **可改的默认值**摆在卡上（本轮没提的沿用示例值，用户看到再确认）。
            if not plan_keys <= spec_keys:
                continue
            # …但"多带的条件"正是"看着对、其实错"的来源：示例带 7 月过滤、用户没问时间，
            # 卡片上的默认值不点开看不出来，答出来的却是 7 月的数（实测同一问题两次
            # 443 / 732）。这类卡**不摆**，交给模板/Wren 按本轮条件重新查
            # （docs/模板沉淀标准与复用校验_系统性方案.md §4.6）。
            extra_keys = sorted(spec_keys - plan_keys)
            if extra_keys:
                log_event(
                    "nl2sql",
                    "INFO",
                    "example_extra_condition_dropped",
                    doc_name=str(parsed.get("doc_name") or ""),
                    conditions="、".join(extra_keys),
                    question=question[:60],
                )
                continue
        else:
            if not _core()._same_structure(spec, plan_spec):
                continue
            # 条件覆盖硬门槛（严格路径）：两边的过滤维度必须完全一致
            if spec_keys != plan_keys:
                continue
        # 回填取值：本轮 plan 优先；本轮没提的（时间/桩号…）在**宽松路径**下沿用示例自己的值，
        # 这样"西乡段巡检了几次"也能复用"西乡段7月巡检几次"（卡上会把沿用值摆出来给人改）
        # （docs/示例参数对齐与同义召回放宽方案.md 第一节）。
        values_spec = dict(spec) if loose else {}
        values_spec.update({key: value for key, value in plan_spec.items() if value})
        filled = fill_example_slots(sql, values_spec)
        if not filled:
            continue
        ok, _reason, clean = sql_engine_validate(filled, kind)
        if not ok:
            continue
        # 再补一道**语法解析**闸：`sql_engine_validate` 是正则级检查，拼坏的引号
        # （实测出现过 `r.short_name = ''宝安大道西乡段'段'`）它能放过，用户点确认才报
        # ERROR 1064。这里用 sqlglot 解析一次（毫秒级），解析不过就不摆这条示例。
        if not _parses_as_sql(clean):
            log_event(
                "nl2sql",
                "INFO",
                "example_sql_unparsable",
                doc_name=str(parsed.get("doc_name") or ""),
                question=question[:60],
            )
            continue
        same_question = _example_key(sample_question) == key
        # 骨架相似度（值被替换掉以后的相似度）足够高就认；纯字面相似度只作参考
        skeleton_score = _skeleton_similarity(question, sample_question)
        if not same_question and max(score, skeleton_score) < settings.example_recall_min_score:
            # 中置信：不当"直接执行"，但这条示例仍在正常链路的 few-shot 里（不改行为）
            continue
        log_event(
            "nl2sql",
            "INFO",
            "example_reuse_hit",
            doc_name=names_by_question.get(_example_key(sample_question), ""),
            source=source,
            score=round(float(score), 2),
            question=question[:60],
        )
        hits.append(
            {
                "sql": clean,
                "normalized_sql": sql,
                "spec": spec,
                "doc_name": names_by_question.get(_example_key(sample_question), ""),
                "question": sample_question,
                "score": round(max(float(score), float(skeleton_score)), 2),
                "reasons": (
                    ["问法一致"] if same_question else ["同义问法（骨架/向量召回）"]
                )
                + [f"结构同型（{plan_spec.get('shape')}/{plan_spec.get('subject')}）",
                   "槽位已按本次条件填好",
                   "条件完整覆盖：" + ("、".join(sorted(plan_keys)) or "无条件")],
                "confidence": "high",
            }
        )
        if len(hits) >= limit:
            break
    return hits


def find_reusable_example(
    question: str, kind: str = "mysql", plan: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """兼容旧调用：只取最匹配的一条。"""
    hits = find_reusable_examples(question, kind, plan=plan, limit=1)
    return hits[0] if hits else None


def parse_example_document(content: str) -> dict[str, Any]:
    """把示例文档正文拆回「问题 + spec + 归一化 SQL」。"""
    text = str(content or "")
    question = ""
    matched = re.search(r"#\s*问题\s*\n(.+?)\n", text)
    if matched:
        question = matched.group(1).strip()
    # 题目索引文档里会带一行 `# 关联 pair-xxxx.md`（指向示例库那条完整文档）
    pair = ""
    pair_match = re.search(r"#\s*关联\s*\n(.+?)\n", text)
    if pair_match:
        pair = pair_match.group(1).strip()
    spec = _core()._chunk_spec(text)
    sql = ""
    sql_match = re.search(r"```sql\s*(.+?)```", text, re.S)
    if sql_match:
        sql = sql_match.group(1).strip()
    # 文档里可能同时存了"当时跑的 SQL"和"编译好的物理 SQL"：重放优先用物理版
    physical = ""
    physical_match = re.search(r"#\s*物理SQL\s*\n```sql\s*(.+?)```", text, re.S)
    if physical_match:
        physical = physical_match.group(1).strip()
    return {"question": question, "spec": spec, "sql": sql, "physical_sql": physical, "pair": pair}


def _name_pattern(name: str) -> str | None:
    """中文路线名的**模糊**正则：允许中间的分隔符增删。

    "宝安中心--恒明珠" 要能匹配 SQL 别名里的 "宝安中心恒明珠段"（用户/库里的写法常常
    少了 `--`），所以按"中文/字母/数字的连续段"拼，段之间允许任意空格与连接符。
    """
    parts = re.findall(r"[0-9A-Za-z\u4e00-\u9fa5]+", str(name or ""))
    if not parts:
        return None
    return r"[\s\-—–_]*".join(re.escape(part) for part in parts)


def relabel_filled_values(
    sql: str, olds: list[str], news: list[str]
) -> tuple[str, list[str]]:
    """把 SQL 里**硬编码的旧参数值**换成新值（列别名里常写着"西乡段平均面积"这种）。

    为什么需要（docs/模板沉淀标准与复用校验_系统性方案.md §4.4）：示例 SQL 的中文列别名是
    `AS 宝安大道西乡段泛油病害平均面积`，复用时参数换了、别名没换 → 标签和数据对不上。

    安全护栏（用户要求"不能硬编码，靠模糊匹配，除非是 id 这种唯一值"）：
      1. 只在**旧值在 SQL 里只出现一次**时才换（出现多次说明还牵着过滤条件，宁可留着旧标签
         也不动 SQL；换错比标签过时更糟）；
      2. 中文名按**去分隔符的模糊匹配**（"宝安中心--恒明珠" ↔ "宝安中心恒明珠"）；
         纯 id/编号这类唯一值直接精确替换；
      3. 新旧值相同、或旧值不知道 → 跳过。
    返回 (改过的 SQL, 实际替换掉了哪些旧值)。
    """
    text = str(sql or "")
    # **一遍扫描、同时替换**：逐条顺序替换会自己抵消
    # （"西乡→恒明珠"改完，紧接着"恒明珠→西乡"又把它换回来——实测踩到）。
    spans: list[tuple[int, int, str, str]] = []
    for old, new in zip(list(olds or []), list(news or [])):
        old = str(old or "").strip()
        new = str(new or "").strip()
        if not old or not new or old == new:
            continue
        # 唯一值（路线 id / 编号 / 纯数字）→ 精确匹配
        if text.count(old) == 1:
            start = text.find(old)
            spans.append((start, start + len(old), new, old))
            continue
        # 中文名 → 模糊匹配（去分隔符），同样要求只命中一次
        pattern = _name_pattern(old)
        if not pattern:
            continue
        hits = list(re.finditer(pattern, text))
        if len(hits) == 1:
            spans.append((hits[0].start(), hits[0].end(), new, old))
    if not spans:
        return text, []
    spans.sort(key=lambda item: item[0])
    out = text
    changed: list[str] = []
    last_start = len(text) + 1
    for start, end, new, old in reversed(spans):
        if end > last_start:  # 重叠（同一段被两条旧值命中）→ 只认靠后的那条
            continue
        out = out[:start] + new + out[end:]
        changed.append(old)
        last_start = start
    changed.reverse()
    return out, changed


def fill_example_slots(sql: str, spec: dict[str, Any] | None) -> str | None:
    """把归一化 SQL 里的 {占位} 用 spec 里的具体值填回去（填不全就返回 None）。

    RAGFlow 的示例库存的是归一化 SQL（不带具体值），要看板里能点着刷新，
    就得用它自己的 spec 把路线/病害类型/桩号/时间补回去。
    """
    sql_text = str(sql or "")
    if "{" not in sql_text:
        return sql_text
    spec = spec or {}
    values: dict[str, str] = {}

    def quote(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    route_name = str(spec.get("route") or "")
    # 多路线示例：{route_id} / {route_id_2}… 与 {route_name} / {route_name_2}… 按序号回填
    route_names = [
        str(item).strip()
        for item in ([route_name] + list(spec.get("routes") or []))
        if str(item or "").strip()
    ]
    route_ids = [str(item).strip() for item in (spec.get("route_ids") or []) if str(item or "").strip()]
    index = 0
    while index < 20:
        name_key = "route_name" if index == 0 else f"route_name_{index + 1}"
        id_key = "route_id" if index == 0 else f"route_id_{index + 1}"
        need_name = f"{{{name_key}}}" in sql_text
        need_id = f"{{{id_key}}}" in sql_text
        if not need_name and not need_id:
            break
        name = route_names[index] if index < len(route_names) else ""
        route_id = route_ids[index] if index < len(route_ids) else ""
        if need_name:
            if not name:
                return None
            values[name_key] = name
        if need_id:
            # 计划里已经解析出路线 id 时直接用，省一次查库；否则按名字回查
            if not route_id and name:
                try:
                    row = _resolve_route(name)
                except Exception:  # noqa: BLE001
                    row = None
                route_id = str((row or {}).get("id") or "")
            if not route_id:
                return None
            values[id_key] = route_id
        index += 1
    damage = spec.get("damage_type") or []
    damage_list = damage if isinstance(damage, list) else [damage]
    if "{damage_type}" in sql_text:
        if not damage_list or not damage_list[0]:
            return None
        values["damage_type"] = str(damage_list[0])
    pile = spec.get("pile_range")
    if isinstance(pile, (list, tuple)) and len(pile) == 2:
        values["pile_start"] = str(int(pile[0]))
        values["pile_end"] = str(int(pile[1]))
    time_range = spec.get("time_range")
    if isinstance(time_range, (list, tuple)) and len(time_range) == 2:
        values["time_start"] = str(time_range[0])
        values["time_end"] = str(time_range[1])
        # 月份桶占位符（`DATE_FORMAT(x,'%Y-%m') = {month}`）：用起始时间的月份回填
        month = str(time_range[0] or "")[:7]
        if re.fullmatch(r"\d{4}-\d{2}", month):
            values["month"] = month
    if spec.get("device_code"):
        values["device_code"] = str(spec["device_code"])

    # 存量文档里同一个占位名可能出现多次（`IN ({route_name}, {route_name})`）：
    # 按出现顺序逐个填 spec 里的多值，而不是两次都填第一个 —— 否则"两条路对比"会变成
    # 同一条路比两次（docs/推荐模板链路Bug清单.md P0-2）。新写入的示例已经编号化，
    # 这里主要是把老文档救回来。
    for name, sequence in (
        ("route_name", route_names),
        ("route_id", route_ids),
        ("damage_type", damage_list if isinstance(damage, list) else [damage] if damage else []),
    ):
        holder = "{" + name + "}"
        occurrences = sql_text.count(holder)
        if occurrences <= 1 or len([item for item in sequence if item]) < 2:
            continue
        for item in [value for value in sequence if value][:occurrences]:
            replacement = str(item) if name.startswith("pile") else quote(item)
            sql_text = sql_text.replace(f"'{holder}'", replacement, 1)
            sql_text = sql_text.replace(holder, replacement, 1)

    for name, value in values.items():
        # LIKE 通配里不能再加引号（`'%{route_name}%'`），否则拼出 `'%'宝安大道西乡段'%'`
        # 这种坏引号（docs/推荐模板链路Bug清单.md P2-11）。
        for wildcard in (f"%{{{name}}}%", f"%{{{name}}}", f"{{{name}}}%"):
            if wildcard in sql_text:
                sql_text = sql_text.replace(wildcard, wildcard.replace(f"{{{name}}}", str(value)))
        # 归一化时字符串条件的引号被吃掉了（'泛油' → {damage_type}），所以这里：
        #   · 占位两侧还带引号的，连引号一起换成带引号的值；
        #   · 光秃秃的占位（字符串类）补上引号；数值类（桩号米数）保持裸值。
        sql_text = sql_text.replace(f"'{{{name}}}'", quote(value))
        replacement = value if name in {"pile_start", "pile_end", "top_n", "bucket_m"} else quote(value)
        sql_text = sql_text.replace(f"{{{name}}}", replacement)
    if re.search(r"\{[a-z_0-9]+\}", sql_text):
        return None
    return sql_text
