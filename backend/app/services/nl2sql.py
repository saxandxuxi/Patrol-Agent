from __future__ import annotations

import hashlib
import copy
import json
import re
import time
from datetime import datetime, timedelta
from functools import lru_cache
from contextvars import ContextVar
from typing import Any

from ..config import settings
from ..store import (
    add_example_pair,
    append_example_variant,
    delete_example_pairs as delete_recorded_example_pairs,
    get_example_by_doc_name,
    get_connection,
    list_recorded_example_pairs,
    mark_example_trusted,
    mark_examples_trusted,
    set_example_spec,
    untrust_examples,
    update_example_spec,
    update_example_normalized_sql,
)
from . import calibrate, clarify

# 同一个"人话字段"可能对应多列：经纬度在病害表是 lng/lat，在路线台账是 start_lnt/end_lnt…
def sql_covers_fields(sql: str, fields: list[str] | set[str] | None) -> bool:
    """SQL 有没有体现这些问题点名的字段（列名或同义列族都算）。

    推荐卡用它挡"指标/字段对不上"的硬套：问题点名"纬度"，模板却只 SELECT COUNT(*)
    （"数里程桩点条数"）→ 返回 False，这条模板不许当候选
    （docs/推荐卡硬推荐与指标空匹配方案.md §三.3）。
    """
    lower = str(sql or "").lower()
    for field in fields or []:
        name = str(field or "").strip().lower()
        if not name:
            continue
        for alias in _FIELD_FAMILIES.get(name, (name,)):
            if str(alias).lower() in lower:
                return True
    return False


_FIELD_FAMILIES: dict[str, tuple[str, ...]] = {
    "lng": ("lng", "start_lnt", "end_lnt", "start_lng", "end_lng"),
    "lat": ("lat", "start_lat", "end_lat"),
    "lng_lat": ("lng", "lat", "start_lnt", "end_lnt", "start_lat", "end_lat"),
    # 桩号列族：同一件事在库里有好几种写法——病害表是 pile_number/pile_distance，
    # 桩点表是 pile_map_position(_name)，路线/路段/边坡是 start_num/end_num，隧道是 stake_*。
    # 只登记前两个的话，"分别列出路段桩号和相关的修改人"会被判成"没带桩号列"而被结果闸拦掉
    # （桩点表的通用明细 SELECT 的是 pile_map_position*）。
    "pile_number": (
        "pile_number",
        "pile_distance",
        "pile_map_position",
        "stake_start_num",
        "stake_center_num",
        "stake_end_num",
        "start_num",
        "end_num",
    ),
    "route_name": ("route_name", "short_name", "full_name"),
    # 巡查的"编号/名称/ID"是同一件事的三种写法：能唯一指出是哪一次巡查即可。
    # 语义层有时给 patrol_number、有时给 patrol_name；SQL 只带其中一个不算漏字段
    # （实测"各次巡查分别检出多少处"因为只带编号被判"缺少 patrol_name"）。
    "patrol_number": ("patrol_number", "patrol_name", "patrol_id"),
    "patrol_name": ("patrol_name", "patrol_number", "patrol_id"),
    "problem_type_name": ("problem_type_name", "problem_type", "damage_type"),
    "area": ("area", "total_area", "avg_area", "max_area"),
    "effective_disease_length": (
        "effective_disease_length",
        "disease_length",
        "area_height",
        "area_width",
    ),
    "collection_time": ("collection_time", "create_time", "open_date"),
    # 置信度：MDL/字典表里是 confidence 列，病害物理表里在 pic_position 的 JSON 里
    "confidence": ("confidence", "pic_position"),
    # pic_position 里的其它 JSON 字段同理：SQL 里出现 pic_position 就说明这一列可达
    # （"标注宽度"这类问题点名了 pic_width，模板 SQL 只要抽了 pic_position 就算覆盖）
    "pic_type": ("pic_type", "pic_position"),
    "pic_width": ("pic_width", "pic_position"),
    "pic_height": ("pic_height", "pic_position"),
    "pic_lng": ("pic_lng", "pic_position"),
    "pic_lat": ("pic_lat", "pic_position"),
    "pic_x0": ("pic_x0", "pic_position"),
    "pic_x1": ("pic_x1", "pic_position"),
    "pic_y0": ("pic_y0", "pic_position"),
    "pic_y1": ("pic_y1", "pic_position"),
}


from . import asset_resolver, dictionary
from . import parsers
from . import prompts
from . import query_rewrite
from . import schema_catalog
from .db import DatabaseError, db_manager
from .llm import LLMError, chat, chat_json, role_model, sql_model
from .logging_service import log_error, log_event
from .rag import (
    RAGError,
    create_dataset,
    delete_documents,
    document_content,
    find_dataset,
    list_documents,
    parse_documents,
    retrieve,
    upload_text,
)
from .sql_engine import extract_sql, generate_sql, validate_sql as sql_engine_validate


# ── 路线 / 病害类型解析已拆到 route_resolver.py（纯搬移，见
#    docs/代码走查与目录结构重构方案.md 第三节）：这里 re-export，外部按老名字用即可。
from .route_resolver import (  # noqa: E402
    QueryPlanError,
    _DAMAGE_TYPES,
    _DAMAGE_TYPES_TTL,
    _GENERIC_ROUTE_WORDS,
    _QUESTION_WORDS,
    _ROUTE_ROWS,
    _ROUTE_ROWS_TTL,
    _ROUTE_TABLE,
    _known_damage_types,
    _resolve_damage_type,
    _resolve_route,
    _route_matches,
    _route_matches_in,
    _route_pieces,
    _route_rows,
    _route_section_count,
    _unmatched_route_token,
)


# ── 字段目录 + 取值采样已拆到 field_catalog.py（纯搬移，见
#    docs/代码走查与目录结构重构方案.md 第三节）：这里 re-export，外部按老名字用即可。
from .field_catalog import (  # noqa: E402
    _CATALOG_CACHE,
    _CATALOG_TTL,
    _CODEISH_COLUMN,
    _CONDITION_FIELDS,
    _CONDITION_LABELS,
    _CONDITION_VALUE_HINTS,
    _ENTITY_FIELD_ALIASES,
    _FIELD_TABLE_SKIP,
    _FIELD_TYPES,
    _LazyFieldTable,
    PIC_JSON_FIELDS,
    _SAMPLE_CACHE,
    _SAMPLE_COLUMNS,
    _SAMPLE_TTL,
    _SAMPLED_HINT_CACHE,
    _SELECT_FIELD_EXPRS,
    _SELECT_FIELD_JOINS,
    _SPECIAL_FIELDS,
    _SPECIAL_FIELD_JOINS,
    _TABLE_ALIASES,
    _catalog,
    _column_label,
    _column_value_hint,
    _field_rows,
    _sampled_hint,
    pic_json_expr,
    value_samples,
)


# ── SQL 体检已拆到 sql_lint.py（纯搬移，见 docs/代码走查与目录结构重构方案.md 第三节）
from .sql_lint import (  # noqa: E402
    _DICT_DUP_CACHE,
    _FIELD_SUGGEST_CACHE,
    _tenant_copied_dictionary,
    dictionary_join_issue,
    field_suggestions,
    unknown_columns,
)


# ── 示例检索/复用已拆到 example_reuse.py（见 docs/代码走查与目录结构重构方案.md 第三节）
from .example_reuse import (  # noqa: E402
    _CONDITION_PLACEHOLDERS,
    _REUSE_CONDITION_FIELDS,
    _SKELETON_VOCAB,
    _SKELETON_VOCAB_TTL,
    _example_key,
    _example_spec_usable,
    _infer_spec_from_text,
    _infer_spec_from_tree,
    _limit_number,
    _pair_document_content,
    _parses_as_sql,
    _predicate_literal,
    _question_similarity,
    _question_skeleton,
    _reuse_candidates,
    _reuse_condition_keys,
    _reuse_plan_spec,
    _skeleton_similarity,
    _skeleton_vocab,
    _spec_value,
    _sql_supports_condition,
    _top_n_range,
    fill_example_slots,
    find_reusable_example,
    find_reusable_examples,
    get_similar_question_sql,
    infer_example_spec,
    parse_example_document,
)


_example_dataset_id: str | None = None
# 题目索引数据集：只存"问题"文本（+ 结构头 + 关联名），专门用来做相似问法召回。
# 为什么要单开一份：示例文档里 70~80% 是 SQL、问题只占 0.5~3%，向量被 SQL 主导，
# 换个说法的题召回不稳（实测正确项相似度只有 0.55 左右、邻居多是"别的题但 SQL 同型"）。
# 这里检索单元 = 问题，命中后按文档名回本地表/示例库取 SQL
# （docs/示例参数对齐与同义召回放宽方案.md 之后的方案甲）。
_QUESTION_INDEX_DATASET = "SQL问题索引"
_question_dataset_id: str | None = None


_COMPARISON_HINTS = (
    "各有多少",
    "分别有多少",
    "各多少",
    "分别多少",
    "哪个多",
    "哪个更",
    "哪边",
    "哪条多",
    "多多少",
    "差多少",
    "对比",
    "差别",
    "区别",
    "差异",
)


def is_comparison(message: str) -> bool:
    """对比类问题（多对象 / 分类别），模板表达不了，必须交给 LLM。"""
    return any(hint in message for hint in _COMPARISON_HINTS)


def _lexicon(section: str, default: set[str]) -> set[str]:
    """从 `resources/lexicon/semantics.yaml` 读语义键表；读不到就用代码里的默认值。

    （docs/代码结构重构与模板外置方案.md §5.3：新增一种维度/指标只改 YAML，不动 .py）
    """
    try:
        from .resource_loader import words

        values = set(words("lexicon/semantics.yaml", section))
    except Exception:  # noqa: BLE001
        values = set()
    return values or set(default)


_SEMANTIC_SHAPES = _lexicon(
    "semantic.shapes",
    {"count", "sum", "avg", "distribution", "list", "top", "trend", "bucket_compare"},
)
_SEMANTIC_SUBJECTS = _lexicon("semantic.subjects", {
    "disease",
    "pile_point",
    "section",
    "patrol",
    "work_order",
    "asset",
    "route",
    # 台账类资产（桥梁/隧道/边坡/工程）——以前没登记，这类问题主体只能默认成 disease/asset，
    # 落点全靠字段触达兜底；登记后主体与落点一致（docs/模板沉淀标准与复用校验_系统性方案.md §三）
    "bridge",
    "tunnel",
    "slope",
    "project",
    # 跨表多字段：一句话要了多张表的字段（"养护任务编号和病害描述"）——
    # 见 docs/跨表多字段并列查询_系统性整改方案.md §三 层 1
    "multi",
})
# 跨表多字段的字段之间是什么关系：并列 / 可关联 / 字典表
_SEMANTIC_MULTI_RELATIONS = _lexicon("semantic.multi_relations", {
    "parallel",
    "joined",
    "lookup",
})
_SEMANTIC_GROUP_BY = _lexicon("semantic.group_by", {
    "damage_type",
    "route",
    "section",
    "direction",
    "image_presence",
    "category",
    "month",
    "day",
    "week",
    "hour",
    "device",
    "confidence",
    "area_bucket",
    # 桩号分段：语义层识别出来后必须能落到计划里，否则会被这里过滤成 null
    # （早先漏了这个值，提示词里写了 pile_bucket 也会被丢掉，桩号分段就退化成 LLM 即兴写）。
    "pile_bucket",
    "manage_dept",
    # 冷门维度（都是 ai_problem 上真实存在的列）：工单状态/病害来源/收藏/是否绑定任务/路格/历史损坏程度
    "problem_status",
    "problem_source",
    "collect",
    "mission_flag",
    "luge_name",
    "history_damage_interval",
})
_SEMANTIC_METRICS = _lexicon("semantic.metrics", {
    "count",
    "total_area",
    "avg_area",
    "max_area",
    "min_area",
    "avg_length",
    "converted_area",
    "confidence",
    "ratio",
    "density",
    "interval",
    "rate",
    "distinct_pile",
    "duration",
})
_SEMANTIC_DERIVED = _lexicon(
    "semantic.derived", {"ratio", "density", "interval", "difference", "linear_area"}
)
_SEMANTIC_OUTPUT_FIELDS = _lexicon("semantic.output_fields", {
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
})
_SEMANTIC_ORDER = _lexicon(
    "semantic.order", {"count_desc", "count_asc", "metric_desc", "metric_asc"}
)

# LLM 抽出来的过滤条件：字段 → (SQL 表达式, 是否数值字段)
# 这里只是一张"字段名 → 物理列"的翻译表，**不是关键词表**——
# 用户怎么表达由 LLM 理解（"不多不少正好 7.52"→ area = 7.52），代码只负责拼 SQL。
_CONDITION_OPS = _lexicon("semantic.condition_ops", {"=", "!=", ">", ">=", "<", "<=", "between", "in"})

# 上一轮"唯一一条"结果里记下的具体对象（字段 + 值），由 agent 在追问时设置。
# 为什么用代码而不是靠补全措辞：上一轮那个 id 到底属于哪一列（patrol_id 还是
# patrol_number）只有结果行知道，让 LLM 按"巡检编号"去猜会过滤到错列上（实测 0 条）。
_ENTITY_FILTER: ContextVar[dict[str, str] | None] = ContextVar("nl2sql_entity_filter", default=None)


def set_entity_filter(fields: list[str], value: str, values: list[str] | None = None) -> None:
    """上一轮结果的 id（并列时是多个）+ **值等于它的所有列名**（候选列，查询前探测挑一个）。"""
    _ENTITY_FILTER.set(
        {
            "fields": [str(item) for item in fields if item],
            "value": str(value),
            "values": [str(item) for item in (values or []) if item],
        }
    )


def clear_entity_filter() -> None:
    _ENTITY_FILTER.set(None)


# 本轮**要回答的完整问题**——也就是**问题优化器处理过的问题**（指代消解 + 上下文继承后的
# `effective_message`），不是用户敲的那几个字。长对话里用户只说"那7月的呢"，优化器会把路线/
# 时间补全，那些**是合法条件**，必须照填；语义层拿不到它就会把补全出来的条件当成编造。
#
# 它同时用来挡住另一种情况：拆解器给子问题**举例**（"（如'2026年第31周'）"）时，那个具体值
# 只存在于子问题文本里、不在本轮完整问题里 —— 那才是"用户没问过的条件"。实测照它填了时间，
# 整步被缩到那一周，再叠加 `LIMIT 1 OFFSET 1` 直接 0 行 → 链式题断在"缺依赖"。
# 复合题执行时由 agent 设置；顶层单题不设置，语义层就用问题本身（两者相同）。
_ROUND_REQUEST: ContextVar[str] = ContextVar("nl2sql_round_request", default="")


def set_round_request(text: str | None) -> None:
    """设置"本轮完整问题（优化后）"——条件是否合法以它为准。"""
    _ROUND_REQUEST.set(str(text or "").strip())


def clear_round_request() -> None:
    _ROUND_REQUEST.set("")


def round_request() -> str:
    return _ROUND_REQUEST.get()


# 上一轮结果里的列名可能是中文别名（"巡检编号""巡检任务ID"）——翻译成物理列名，
# 翻译不出就退到兜底候选列，靠"能不能查到数据"来选（见 _resolve_entity_condition）。
_ENTITY_FALLBACK_FIELDS: tuple[str, ...] = (
    "task_number",
    "patrol_number",
    "patrol_id",
    "problem_number",
    "problem_id",
    "id",
)


def _probe_id_field(values: list[str], plan: dict[str, Any], preferred: list[str] = ()) -> str | None:
    """拿这组编号去逐列试一次（LIMIT 1），返回**真能查到数据**的那一列。

    同一个编号家族只属于某一列：XJJL… 是巡检任务号（巡检表 task_number），
    XCBH… 是巡查编号（patrol_number），BPLHZD…/KZXB… 是病害编号（problem_number）。
    让模型按中文名猜列名会过滤到错列 → 0 条（实测：「任务号 XJJL…」落到
    ai_problem.patrol_number，那一列存的是 XCBH…，永远查不到）。
    """
    candidates: list[str] = []
    for name in preferred:
        candidates.append(_ENTITY_FIELD_ALIASES.get(str(name), str(name)))
    candidates.extend(item for item in _ENTITY_FALLBACK_FIELDS if item not in candidates)
    literals = [_condition_literal(item, False) for item in values]
    if any(item is None for item in literals):
        return None
    for field in candidates:
        if field not in _CONDITION_FIELDS:
            continue
        expr, _ = _CONDITION_FIELDS[field]
        # JOIN 要按候选列动态补：t.task_number 属于巡检表，没 JOIN 就直接报错
        joins, where = _metric_filters(plan)
        if expr.startswith("t.") and not any("ai_maintain_patrol" in item for item in joins):
            joins.append("JOIN ai_maintain_patrol t ON p.patrol_id = t.id")
        from_sql = f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
        if len(literals) == 1:
            match_sql = f"{expr} = {literals[0]}"
        else:
            match_sql = f"{expr} IN ({', '.join(literals)})"
        try:
            rows = db_manager.query("mysql", f"SELECT 1 AS ok {from_sql} AND {match_sql} LIMIT 1")["rows"]
        except Exception:  # noqa: BLE001
            continue
        if rows:
            return field
    return None


def _resolve_entity_condition(plan: dict[str, Any]) -> dict[str, Any] | None:
    """把上一轮记下的 id 落成一个**真能对上数据**的过滤条件。"""
    entity = _ENTITY_FILTER.get()
    if not entity or not entity.get("value"):
        return None
    values = [str(item) for item in (entity.get("values") or [entity["value"]]) if str(item).strip()]
    if not values:
        return None
    field = _probe_id_field(values, plan, preferred=list(entity.get("fields") or []))
    if not field:
        return None
    if len(values) == 1:
        return {"field": field, "op": "=", "value": values[0]}
    return {"field": field, "op": "in", "value": values}


# 编号类条件：字段名靠不住（同一个值在不同列里都可能"长得像"），必须探测定列。
_ID_CONDITION_FIELDS: tuple[str, ...] = (
    "patrol_number",
    "patrol_id",
    "task_number",
    "problem_number",
    "problem_id",
)


def _resolve_id_conditions(
    conditions: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """语义层抽出的编号条件也走"探测定列"——它按中文名猜的列不一定对。

    探不到数据的就保留原样（可能是我们还不认识的编号，别把用户条件丢掉）。
    """
    out: list[dict[str, Any]] = []
    for item in conditions:
        field = str(item.get("field") or "")
        if field not in _ID_CONDITION_FIELDS or str(item.get("op")) not in {"=", "in"}:
            out.append(item)
            continue
        raw = item.get("value")
        values = [str(part) for part in (raw if isinstance(raw, list) else [raw]) if str(part).strip()]
        if not values:
            out.append(item)
            continue
        probed = _probe_id_field(values, plan, preferred=[field])
        out.append({**item, "field": probed} if probed and probed != field else item)
    return out

_SEMANTIC_CACHE: dict[str, dict[str, Any]] = {}
_SEMANTIC_CACHE_LIMIT = settings.cache_max_entries

# 问题优化（抽象业务问法 → 可查口径）用的能力清单。
# 只列真实存在的维度和指标——列错了模型就会翻译出查不到的句子。
_CAPABILITY_HINT = (
    "维度：\n"
    "- 路线（路线名）、路段（一条路线下的分段）、桩号（pile_number 文本 / pile_distance 米）\n"
    "- 上下行（up_or_down）、病害类型（字典 problem_type）、病害大类（路面/路基/沿路设施/交安事件）\n"
    "- 时间（病害采集时间：按日/按月/按周/按时段）、采集设备（device_code）、来源（手动/车辆巡检/人工巡检）\n"
    "- 巡查任务与巡查类型（定期/日常/专项/人工复核/重点）、工单状态（待转/已转/已忽略）\n"
    "- 管养单位（路线台账的 manage_dept）、养护单位（maintain_dept）、所在地区（region）\n"
    "- 技术等级（tech_grade：高速/一级/二级…）、行政等级（route.level：国道/省道/县道/乡道/村道）、"
    "路面类型（surface_type）、路面宽度（surface_width）、车道数（lane_count）——"
    "这几项都在**路段/路线台账**上，不是病害自身的属性；技术等级和行政等级是两个不同字段，别混\n"
    "指标：\n"
    "- 处数、总面积（area，㎡）、平均面积、最大/最小面积、病害长度（effective_disease_length，m）、"
    "宽度（area_width）、高度（area_height）、不同桩号数\n"
    "- AI 识别置信度、有无图片\n"
    "- 路线长度（按台账起止桩号相减，不能用 distance 字段）、管养里程合计\n"
    "- 巡查任务次数（一次任务算一次，不能按病害条数）、巡检里程（patrol_mileage）\n"
    "- 派生指标（这些**系统本来就支持**，不要改写成近似口径）：\n"
    "  · 占比 / 比例 = 部分数量 ÷ 总数量 × 100%\n"
    "  · 病害密度 = 病害处数 ÷ 路线长度（公里）\n"
    "  · 平均间距 = 路线长度（米）÷ 病害处数\n"
    "  · 环比 / 两段时间变化 = 两个时间段的指标相减或相除\n"
    "算不出来的口径（必须进 unsupported，不要臆造）：\n"
    "- 车道级的病害归属：病害表没有车道列，只有路段台账有车道数\n"
    "- 同一处病害跨多次巡检的逐次扩张速度：病害是按次采集的快照，没有病害级历史关联\n"
    "- 计划巡检里程 / 任务完成率：当前没有计划里程字段\n"
)


_OPTIMIZER_INTERNAL_COLUMNS = {
    "id",
    "tenant_id",
    "del_flag",
    "create_by",
    "update_by",
}
_OPTIMIZER_GENERIC_DIMENSIONS = (
    "数量",
    "处数",
    "总数",
    "时间",
    "日期",
    "状态",
    "类型",
    "来源",
    "时间范围",
)


def question_optimization_context(
    question: str,
    kind: str = "mysql",
) -> dict[str, Any]:
    """给问题优化器的最小字段上下文：只列字段触达命中的主表。

    字段触达列、完整物理列目录、可用展示字段分别输出，不能混成一个全局字段清单。
    """
    from . import field_grounding

    grounded = field_grounding.ground(kind, question)
    table_name = str(grounded.get("table") or "")
    entry = schema_catalog.table_entry(kind, table_name) if table_name else None
    if not entry:
        return {
            "text": (
                _CAPABILITY_HINT
                + "\n本轮没有可靠命中具体台账：只能补充通用的分组和指标口径，"
                "不得臆造列名、状态名、来源名或跨表属性。"
            ),
            "source": "global_capability",
            "table": "",
            "subject": "",
            "touched_columns": [],
            "physical_columns": [],
            "display_columns": [],
        }

    columns = list(entry.get("columns") or [])
    touched = [
        {
            "column": str(item.get("column") or ""),
            "label": str(item.get("label") or ""),
            "source": "field_grounding",
        }
        for item in grounded.get("columns") or []
        if str(item.get("table") or "") == table_name
    ]
    physical_columns = [
        {
            "column": str(item.get("name") or ""),
            "label": str(item.get("label") or item.get("name") or ""),
            "type": str(item.get("type") or ""),
            "code_map": dict(item.get("code_map") or {}),
            "internal": str(item.get("name") or "") in _OPTIMIZER_INTERNAL_COLUMNS,
            "source": "schema_catalog",
        }
        for item in columns
        if item.get("name")
    ]
    display_columns = [
        item for item in physical_columns if not item["internal"]
    ]
    lines = [
        "字段上下文来源：field_grounding + schema_catalog；以下内容只针对本轮主表。",
        f"主表：{table_name}（{entry.get('label') or table_name}）",
        "字段触达列（用户这句话实际命中的列）：",
        *[
            f"- {item['column']}（{item['label']}）"
            for item in touched
        ],
        "完整物理列目录（只包含这张主表，可用于判断字段是否存在）：",
        *[
            f"- {item['column']}（{item['label']}）"
            for item in physical_columns
        ],
        "可用展示字段（内部 id/租户/删除标记等不作为业务字段展示）：",
        *[
            f"- {item['column']}（{item['label']}）"
            for item in display_columns
        ],
        "存储/展示约束：code 只用于 SQL 执行和比较；扩写问题与最终展示只能使用中文标签。",
        "禁止把其他表的字段名或属性塞进本题；确需跨表关系时交给下游规划器处理。",
        "指标约束：只允许使用本表字段和通用汇总词（数量/总数/平均/最大/最小/占比/变化）；"
        "不得引入其他表的维度或指标。",
    ]
    # 给枚举列补 code→中文，来源明确标成字段目录；同一列多值在目录里不会重复。
    storage_lines = [
        f"- {item['column']}（{item['label']}）："
        + "，".join(f"{code}={label}" for code, label in item["code_map"].items())
        for item in physical_columns
        if item["code_map"]
    ]
    if storage_lines:
        insert_at = lines.index("可用展示字段（内部 id/租户/删除标记等不作为业务字段展示）：")
        lines[insert_at:insert_at] = ["枚举存储值（执行用 code，展示用中文）：", *storage_lines]
    return {
        "text": "\n".join(lines),
        "source": "field_grounding",
        "table": table_name,
        "subject": str(grounded.get("subject") or ""),
        "touched_columns": touched,
        "physical_columns": physical_columns,
        "display_columns": display_columns,
    }


def _optimizer_dimension_supported(
    dimension: str,
    display_columns: list[dict[str, Any]],
) -> bool:
    text = _squash_text(dimension)
    if not text or any(word in text for word in _OPTIMIZER_GENERIC_DIMENSIONS):
        return True
    for item in display_columns:
        label = _squash_text(str(item.get("label") or ""))
        column = _squash_text(str(item.get("column") or ""))
        for candidate in (label, column):
            if len(candidate) >= 2 and (
                candidate in text
                or text in candidate
                or any(
                    text[index : index + 2] in candidate
                    for index in range(max(0, len(text) - 1))
                )
            ):
                return True
    return False


def _explicit_landing_subject(
    kind: str,
    message: str,
    grounded: dict[str, Any],
) -> str:
    """用户原话明确点名某张台账时，字段触达主体优先于语义模型的泛主体判断。"""
    table = str((grounded or {}).get("table") or "")
    subject = str((grounded or {}).get("subject") or "")
    if not table or subject not in _SEMANTIC_SUBJECTS:
        return ""
    label = _squash_text(schema_catalog.table_label(kind, table))
    return subject if label and label in _squash_text(message) else ""

_VERIFY_CACHE: dict[str, dict[str, Any]] = {}
_VERIFY_CACHE_LIMIT = settings.cache_max_entries

# 改写**不许丢信息**：这些名词出现在原问题里，改写后也必须还在。
# 实测踩过：把"西乡段泛油有多少处"改写成"西乡段病害有多少处"（丢了泛油）、
# 把"postgres 的倾角数据"并进病害题、"资产类型"理解成病害大类。
_PROTECTED_NOUNS = (
    "资产",
    "工单",
    "里程桩",
    "桩点",
    "路线",
    "道路",
    "路段",
    "巡查",
    "巡检",
    "倾角",
    "轨迹",
    "图片",
    "照片",
    "数据库",
)


def _lost_entities(original: str, rewritten: str) -> list[str]:
    """改写过程中丢掉的关键实体（空列表 = 没丢，可以用改写后的问题）。"""
    lowered_original = original.lower()
    lowered = rewritten.lower()
    lost: list[str] = []
    for item in _known_damage_types():
        name = str(item.get("problem_type") or "")
        if name and name in original and name not in rewritten:
            lost.append(name)
    try:
        from . import calibrate

        for name in calibrate.route_names():
            text = str(name or "")
            if text and text in original and text not in rewritten:
                lost.append(text)
    except Exception:  # noqa: BLE001
        pass
    for token in ("mysql", "postgres", "postgresql", "gpsdb"):
        if token in lowered_original and token not in lowered:
            lost.append(token)
    for token in re.findall(r"[Kk]\d+\+\d+", original):
        if token.lower() not in lowered:
            lost.append(token)
    for token in re.findall(r"\b\d{6,}\b", original):
        if token not in rewritten:
            lost.append(token)
    for noun in _PROTECTED_NOUNS:
        if noun in original and noun not in rewritten:
            lost.append(noun)
    return lost


def _pick_text(value: Any, limit: int = 400) -> str:
    return str(value or "").strip()[:limit]


def _pick_text_list(values: Any, limit: int = 8) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    picked: list[str] = []
    for item in values:
        text = _pick_text(item, 80)
        if text and text not in picked:
            picked.append(text)
    return picked[:limit]


_PILE_BUCKET_UNITS = {"公里": 1000.0, "千米": 1000.0, "km": 1000.0, "KM": 1000.0, "米": 1.0, "m": 1.0}


def _message_mentions_time(text: str) -> bool:
    """这句里到底有没有**时间说法**？——直接问规则解析器，不另立关键词表。

    只用来做一道**护栏**：语义层给了时间条件、但规则解析在句子里找不到任何时间说法时，
    那个条件多半是它拿"今天/本周"兜出来的（实测把用户的 7/20~7/26 盖成当前周），要清掉。
    """
    plain = re.sub(r"\s+", "", str(text))
    try:
        if parsers.parse_time_range(str(text)) is not None:
            return True
    except Exception:  # noqa: BLE001
        return True  # 判不了就别清（宁可留着语义层的值）
    # 规则解析器不认"本季度/本年度/本周/本月"这类**相对当前周期**的说法（它只认能算出绝对区间
    # 的写法），于是这道护栏会把语义层**正确**解析出来的当前季度/年度当成"拿 now 兜的"清掉
    # （实测"本季度泛油病害检出数量"被清成了没有时间条件）。补一条**封闭构词规则**：
    # 本/这/当 + 年|年度|季|季度|月|周|星期|日|天 —— 就这几个周期词，不是业务关键词表。
    if re.search(r"(本|这|当)(年|年度|季|季度|月|周|星期|日|天)", plain):
        return True
    return any(word in plain for word in ("今天", "明天", "昨天", "目前", "现在", "当前"))


def _looks_like_now_window(start: Any, end: Any) -> bool:
    """这个区间是不是"今天/本周/本月"那个**以 now 为基准**的窗口？

    只用来决定"语义层给的、但句子里没有时间说法的区间要不要清"：起点离今天 ±7 天内
    就认定是它拿 now 兜的（实测把用户的 7/20~7/26 盖成 10/12~10/19 就是这种）。
    "最近两周""上半年"这类起点离今天很远的，保留（那是语义层真解析出来的）。

    也要认**整月/整周**窗口：它们起点离今天可能超过 7 天（本月 1 号距今最多 30 天），
    实测"把一个月的上、中、下三段分开算"这种把"一个月"当**分段依据**的句子，
    语义层会兜一个"本月"出来——那不是用户问的时间条件，同样要清掉。
    """
    moment = _normalize_time_value(start)
    if not moment:
        return False
    try:
        begin = datetime.fromisoformat(moment)
    except ValueError:
        return False
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if begin == today:
        return True
    week_start = today - timedelta(days=today.weekday())
    if begin in {week_start, today.replace(day=1)}:
        return True
    return timedelta(days=-7) <= (begin - today) <= timedelta(days=7)


def _bucket_basis_dimension(question: str) -> str:
    """这句的切分依据是**时间刻度**吗？是的话返回对应的时间维度名，否则返回空串。

    为什么单独问一次：模型看到"段/哪一段"容易一律判成桩号分段，而"把一个月分成上、中、下三段"
    "每三个月一段"的依据其实是时间刻度。提示词里正反例都写了仍会被这个字带跑（实测），
    所以只在计划里**已经填了 pile_bucket** 时就这一件事让语义层表个态。
    判不出来（模型失败/没说 time）就返回空串，保持原判（宁可不动）。
    """
    try:
        data = chat_json(
            prompts.messages("bucket_basis", question=str(question)),
            model=role_model("semantic") or sql_model(),
        )
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "bucket_basis_failed", exc, question=str(question)[:60])
        return ""
    if not isinstance(data, dict) or str(data.get("basis") or "") != "time":
        return ""
    dim = str(data.get("dim") or "").strip()
    return dim if dim in _TIME_BUCKET_EXPRS else ""


def _pile_bucket_meters(message: str) -> float | None:
    """从问法里抽"分档粒度的米数"（"每 500 米""每1公里""100 米一段"都认）。

    只在语义层已经判定"按桩号分档"（group_by=pile_bucket）时才被调用——**要不要抽由语义层决定**，
    这里只把"数字 + 单位"读出来：单位词表是封闭的（米/公里/千米/km/m），所以说法怎么变都能抽，
    不是靠措辞关键词命中。用户没给数字（只说"按桩号段/分段"）就返回 None。
    """
    text = str(message or "")
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(公里|千米|km|KM|米|m)", text, re.IGNORECASE):
        number = float(match.group(1))
        unit = _PILE_BUCKET_UNITS.get(match.group(2), 0.0)
        meters = number * unit
        if meters > 0:
            return meters
    return None


def _normalize_time_value(value: Any) -> str | None:
    """时间条件必须是**合法时间**，否则丢掉。

    实测踩过：巡查编号 `2026073017132358253c131268ee6476` 里带着数字串，
    语义层把它当成了时间值 → 时间边界变成一个假时间 → 校准层误判"没有数据"。
    """
    text = str(value or "").strip()
    if not text:
        return None
    cleaned = (
        text.replace("年", "-").replace("月", "-").replace("日", " ").replace("/", "-").strip()
    )
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(cleaned[: len(fmt) + 6].strip(), fmt).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
    # 只认"YYYY-MM-DD..."这种形状，纯数字串（编号）一律不认
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?)?", cleaned)
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour, minute, second = int(match.group(4) or 0), int(match.group(5) or 0), int(match.group(6) or 0)
    try:
        return datetime(year, month, day, hour, minute, second).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _squash_text(text: str) -> str:
    """去掉填充字再比中文（"日常的巡查任务" 也要能对上 "日常巡查"）。"""
    return re.sub(r"[\s的了之,，。]+", "", str(text or ""))


# 通用词 → 库里常见列名的中文线索（**兜底**用；主判据仍是字段目录里列名/注释的匹配）。
# LLM 常把"名称/编号/类型/状态"直接拼成物理列名（route_name/route_number），而库里其实叫
# full_name/code，于是下游按不存在的列去查而报错
# （docs/枚举值字段触达与通用词列名映射_整改方案.md §二）。
_GENERIC_WORD_HINTS: dict[str, tuple[str, ...]] = {
    "name": ("名称", "全称", "简称"),
    "number": ("编号", "代码", "号码"),
    "no": ("编号", "代码"),
    "code": ("代码", "编号"),
    "type": ("类型", "分类", "类别", "等级"),
    "category": ("分类", "类别", "类型"),
    "status": ("状态", "情形"),
    "state": ("状态",),
    "desc": ("描述", "说明", "备注"),
    "description": ("描述", "说明", "备注"),
    "memo": ("备注", "说明"),
    "time": ("时间", "日期"),
    "date": ("日期", "时间"),
    "level": ("等级", "级别"),
    "grade": ("等级", "级别"),
}


def _physicalize_semantic_fields(plan: dict[str, Any], kind: str = "mysql") -> None:
    """把 plan 里**不是物理列**的字段名换成真实列名（route_name → full_name、number → code）。

    只动"全库都没有这一列"的名字（真物理列一律不动），换成的列取自落点表（没有落点就用语义
    主体表），依据是该列的列名/注释文本（不写死"哪个表用哪一列"）。找不到对应列就从
    `output_fields` 里去掉并把原名记进 `field_aliases`——否则下游会把它当成"用户点名了这一列"，
    实测会答成"库里查不到 number 这个字段"（这正是问题 B 的用户可见症状）。
    """
    from . import capabilities

    semantic = plan.get("semantic") or {}
    # output_fields 和 unknown_output_fields 都要过：后者是"目录里没有的名字"（number 这种
    # LLM 自己拼的词），一样得先换回物理列，换不回来才轮到"如实说没有这个字段"。
    names = [
        str(item)
        for item in list(semantic.get("output_fields") or [])
        + list(semantic.get("unknown_output_fields") or [])
        if str(item or "").strip()
    ]
    if not names:
        return
    # 这里必须用**完整物理列目录**：字段触达专用的 `_columns()` 会排除
    # create_time/update_time 等公共列来控制噪声。拿它做物理化会把用户明确
    # 点名的 create_time 误判成“未知字段”，再按“时间/日期”相似度猜成 end_date。
    columns = [
        {
            "table": str(table.get("table") or ""),
            "column": str(column.get("name") or ""),
            "label": str(column.get("label") or ""),
            "comment": str(column.get("comment") or ""),
        }
        for table in schema_catalog.field_catalog(kind).get("tables") or []
        for column in table.get("columns") or []
        if column.get("name")
    ]
    landing = str((plan.get("grounded") or {}).get("table") or "") or str(
        capabilities._SUBJECT_TABLES.get(str(plan.get("subject") or "")) or ""  # noqa: SLF001
    )
    # "要不要换"按**落点表**判（`name` 在桥梁表是列、在路线表不是 —— 落点是路线表时它就是
    # 需要换的名字）；没有落点表才退到全库判。
    scoped = [column for column in columns if not landing or str(column["table"]) == landing]
    # 语义字段（pic_position 的 JSON 字段、join 出来的中文名）**永远算"已知列"**：
    # 它们不在 information_schema 里，落点又是字典表时，这一步会把 `pic_type` 按"type→类型"
    # 猜成字典表的 `problem_type`——把上面刚做的"图片里的类型走 JSON"改回去（实测踩到）。
    physical = {str(column["column"]) for column in (scoped or columns)} | set(_SPECIAL_FIELDS)
    if all(name in physical for name in names):
        return
    kept: list[str] = []
    aliases: dict[str, str] = {}
    for name in names:
        if name in physical:
            kept.append(name)
            continue
        words = [part for part in re.split(r"[^a-z0-9]+", name.lower()) if part]
        hints = [hint for word in words for hint in _GENERIC_WORD_HINTS.get(word, ())]
        best: tuple[tuple[int, int], str] | None = None
        for column in scoped:
            label = str(column.get("label") or "")
            blob = f"{label} {column.get('comment') or ''}"
            hit = sum(1 for hint in hints if hint and hint in blob)
            if not hit:
                continue
            candidate = ((hit, -len(label)), str(column["column"]))
            if best is None or candidate[0] > best[0]:
                best = candidate
        if best:
            aliases[name] = best[1]
            kept.append(best[1])
        else:
            # 映射不出来就**原样保留**，交给下游的两种如实回答：
            #   · 别的表有这一列（lng/lat）→ "这张表没有这一列"
            #   · 全库都没有（province）→ "库里没有记录这项信息"
            # 以前这里会把名字丢掉，结果这两条如实回答都不再触发（实测"桥梁的经纬度"和
            # "桥在哪个省"都退回成硬查/答非所问）。
            aliases[name] = ""
            kept.append(name)
            log_event(
                "nl2sql",
                "WARNING",
                "semantic_field_dropped",
                field=name,
                table=landing,
                question=str(plan.get("message") or "")[:60],
            )
    if aliases:
        semantic["field_aliases"] = {**dict(semantic.get("field_aliases") or {}), **aliases}
        semantic["output_fields"] = kept
        # 已经换回物理列的名字不必再留在 unknown_output_fields 里（那里是"库里没这个字段"的证据）
        mapped = {key for key, value in aliases.items() if value}
        if mapped:
            semantic["unknown_output_fields"] = [
                item
                for item in semantic.get("unknown_output_fields") or []
                if str(item) not in mapped
            ]
        plan.setdefault("slots", {})["semantic_fields"] = kept
        # missing_fields / unavailable_fields 是字段触达**物理化之前**算的：名字换了之后要重算，
        # 否则"这5条国道的名称和编号"明明已经换成 full_name/code，下游还会答"库里查不到 number"
        # （这两个字段是"如实回答"的闸门，见 agent._missing_fields_payload）。
        grounded = plan.get("grounded") or {}
        if grounded:
            all_physical = {str(column["column"]) for column in columns}
            scoped_names = {str(column["column"]) for column in scoped} or all_physical
            still_missing = [name for name in kept if name not in scoped_names]
            grounded["missing_fields"] = still_missing
            grounded["unavailable_fields"] = [
                name for name in still_missing if name not in all_physical
            ]
        log_event(
            "nl2sql",
            "INFO",
            "semantic_fields_physicalized",
            table=landing,
            aliases="；".join(f"{key}→{value or '（丢掉）'}" for key, value in aliases.items())[:120],
        )


def _canonicalize_disease_time_fields(plan: dict[str, Any]) -> None:
    """病害时间语义统一用 collection_time；只有明确问入库/创建时间才保留 create_time。"""
    if str(plan.get("subject") or "disease") != "disease":
        return
    message = str(plan.get("message") or "")
    if any(word in message for word in ("入库时间", "创建时间", "录入时间", "写入时间")):
        return
    slots = plan.setdefault("slots", {})
    for key in ("semantic_conditions",):
        for item in slots.get(key) or []:
            if str(item.get("field") or "") == "create_time":
                item["field"] = "collection_time"
    semantic = plan.get("semantic") or {}
    fields = [
        "collection_time" if str(item) == "create_time" else str(item)
        for item in (semantic.get("output_fields") or [])
    ]
    if fields and fields != list(semantic.get("output_fields") or []):
        semantic["output_fields"] = fields
        slots["semantic_fields"] = fields


def _pick_multi_fields(values: Any) -> list[dict[str, str]]:
    """跨表多字段题的 `fields` 数组：`[{name, label, table_hint}]`（最多 6 条，非法项丢掉）。

    只管形状，不校验字段真的存在（那是执行层 `multi_field.resolve_fields` 的活）——
    这里丢掉反而是把"用户点了 A 表的 X"这个证据丢了。
    """
    if not isinstance(values, (list, tuple)):
        return []
    picked: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in values:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        label = str(item.get("label") or "").strip()
        hint = str(item.get("table_hint") or item.get("table") or "").strip()
        key = (name or label).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        picked.append({"name": name[:60], "label": label[:60], "table_hint": hint[:60]})
        if len(picked) >= 6:
            break
    return picked


def _field_code_map(field: str, table: str = "") -> dict[str, str]:
    """这个语义字段对应列的编码表（code→中文）；不是编码列就返回空。"""
    wanted = str(field or "").strip().lower()
    if not wanted:
        return {}
    for entry in schema_catalog.field_catalog("mysql").get("tables") or []:
        if table and str(entry.get("table") or "") != table:
            continue
        for column in entry.get("columns") or []:
            if str(column.get("name") or "").lower() == wanted and column.get("code_map"):
                return {str(k): str(v) for k, v in (column.get("code_map") or {}).items()}
    return {}


def localize_enum_rows(
    plan: dict[str, Any], rows: list[dict[str, Any]], columns: list[str] | None = None
) -> list[dict[str, Any]]:
    """按 semantic.output_fields 顺序，用字段 code_map 翻译结果里的编码值。"""
    fields = [
        str(item)
        for item in ((plan.get("semantic") or {}).get("output_fields") or [])
        if str(item)
    ]
    if not rows or not fields:
        return rows
    result_columns = list(columns or rows[0].keys())
    if len(fields) > len(result_columns):
        return rows
    for field, column in zip(fields, result_columns):
        landing = str((plan.get("grounded") or {}).get("table") or "")
        code_map = _field_code_map(field, landing)
        if not code_map:
            continue
        for row in rows:
            value = row.get(column)
            if value in (None, ""):
                continue
            label = code_map.get(str(value).strip())
            if label:
                row[column] = label
    return rows


def _infer_enum_conditions(plan: dict[str, Any]) -> None:
    """语义层漏抽枚举过滤时，用已触达字段的 code_map 与原话做确定性对齐。"""
    message = _squash_text(str(plan.get("message") or ""))
    grounded = plan.get("grounded") or {}
    landing = str(grounded.get("table") or "")
    candidates: list[dict[str, Any]] = []
    for item in grounded.get("columns") or []:
        if not isinstance(item, dict):
            continue
        if landing and str(item.get("table") or "") != landing:
            continue
        if item.get("code_map"):
            candidates.append(item)
    for columns in (grounded.get("table_fields") or {}).values():
        for item in columns or []:
            if not isinstance(item, dict) or not item.get("code_map"):
                continue
            field_name = str(item.get("column") or "")
            if field_name and not any(
                str(existing.get("column") or "") == field_name for existing in candidates
            ):
                candidates.append(
                    {
                        "table": item.get("table") or landing,
                        "column": field_name,
                        "label": item.get("label") or field_name,
                        "code_map": item["code_map"],
                    }
                )
    if not message or not candidates:
        return
    conditions = list((plan.get("slots") or {}).get("semantic_conditions") or [])
    existing_fields = {str(item.get("field") or "") for item in conditions}
    inferred: list[dict[str, Any]] = []
    for item in candidates:
        field = str(item.get("column") or "")
        if not field or field in existing_fields:
            continue
        field_label = _squash_text(str(item.get("label") or ""))
        matches: list[tuple[int, str]] = []
        for code, label in (item.get("code_map") or {}).items():
            text = _squash_text(str(label or ""))
            if len(text) < 2:
                continue
            # "下行"只是列名"上下行"的一部分，不是用户给的筛选值。
            if text in field_label:
                continue
            best = 0
            for size in range(2, min(len(text), 8) + 1):
                if text[:size] in message:
                    best = size
            if best:
                matches.append((best, str(code)))
        if not matches:
            continue
        best_score = max(score for score, _code in matches)
        best_codes = {code for score, code in matches if score == best_score}
        if len(best_codes) != 1:
            continue
        condition = {"field": field, "op": "=", "value": next(iter(best_codes))}
        if not _condition_mentioned(condition, message):
            continue
        inferred.append(condition)
    if inferred:
        plan.setdefault("slots", {})["semantic_conditions"] = conditions + inferred
        semantic = plan.setdefault("semantic", {})
        semantic["conditions"] = list(semantic.get("conditions") or []) + inferred
        log_event(
            "nl2sql",
            "INFO",
            "enum_conditions_inferred",
            fields="、".join(item["field"] for item in inferred),
            values="、".join(str(item["value"]) for item in inferred),
            question=str(plan.get("message") or "")[:60],
        )


def _condition_mentioned(condition: dict[str, Any], message: str, kind: str = "mysql") -> bool:
    """编码类条件是不是**真从用户原话里来的**（对不上就说明是模型硬造的，丢掉）。

    为什么必须有这道校验：语义层会凭空造过滤条件。实测"把养护巡查的明细调出来"被加上
    `patrol_type='10'`（10=专项巡查），用户从没说过"专项巡查"——库里没有这种记录，
    于是静默查成 0 行、回"未查询到符合条件的数据"，而那张表里有 147 条。
    条件该加不加会静默放大结果，不该加乱加会静默查成空，两种都得防。

    判据：等值/in 条件的值，它的**中文名**（该列 code_map 里的中文）或用户直接写出的非数字值
    要出现在原话里；时间/数值条件不做这个检查（"7月" 会落成 2026-07-01，字面当然对不上）。
    """
    if str(condition.get("field") or "") == "collection_time":
        return True
    if str(condition.get("op") or "=") not in {"=", "in"}:
        return True
    code_map = _field_code_map(str(condition.get("field") or ""))
    if not code_map:
        return True  # 不是编码列：交给原来的（数值/时间）逻辑
    values = condition.get("value")
    values = values if isinstance(values, list) else [values]
    text = _squash_text(message)
    field = str(condition.get("field") or "")
    label_text = _squash_text(str(_CONDITION_LABELS.get(field) or ""))
    field_context = {
        "problem_type_name": ("病害", "类型"),
        "disease_categories_type": ("病害", "大类"),
        "up_or_down": ("上行", "下行", "方向"),
        "patrol_type": ("巡查", "巡检", "类型"),
        "surface_type": ("路面", "类型"),
        "tech_grade": ("技术等级", "等级"),
        "problem_source": ("病害来源", "来源"),
        "problem_status": ("病害状态", "状态"),
        "operate_type": ("病害操作", "操作"),
        "enable_type": ("基础状态", "状态"),
        "track_problem": ("重点关注",),
        "collect": ("收藏",),
    }.get(field, ())
    has_field_context = bool(
        (label_text and label_text in text)
        or any(_squash_text(word) in text for word in field_context)
    )
    if not has_field_context:
        return False
    for value in values:
        code = str(value if value is not None else "").strip()
        if not code:
            continue
        label = str(code_map.get(code) or "").strip()
        squashed_label = _squash_text(label)
        if squashed_label and squashed_label in text:
            return True
        if any(
            squashed_label[:size] in text
            for size in range(2, min(len(squashed_label), 8) + 1)
        ):
            return True
        # 用户直接写了枚举值本身（非数字，避免 "10" 撞上桩号/日期）也算说到过
        if not code.isdigit() and _squash_text(code) in text:
            return True
    return False


def _normalize_enum_value(field: str, value: Any) -> Any:
    """编码列收到中文标签时，按字段 code_map 确定性翻回 code。"""
    code_map = _field_code_map(field)
    if not code_map or isinstance(value, (list, tuple, dict)):
        return value
    text = _squash_text(str(value or ""))
    if not text:
        return value
    if text in {_squash_text(code) for code in code_map}:
        return value
    matches = [
        code
        for code, label in code_map.items()
        if text == _squash_text(label)
        or (_squash_text(label).startswith(text) and len(text) >= 2)
        or (text.startswith(_squash_text(label)) and len(_squash_text(label)) >= 2)
    ]
    return matches[0] if len(matches) == 1 else value


def _kept_conditions(
    conditions: list[dict[str, Any]], message: str, kind: str = "mysql"
) -> list[dict[str, Any]]:
    """丢掉"用户没说过"的编码条件（见 _condition_mentioned），并记日志。"""
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for condition in conditions:
        if _condition_mentioned(condition, message, kind):
            kept.append(condition)
        else:
            dropped.append(
                "%s%s%s"
                % (
                    condition.get("field"),
                    condition.get("op") or "=",
                    condition.get("value"),
                )
            )
    if dropped:
        log_event(
            "nl2sql",
            "WARNING",
            "semantic_condition_dropped_unmentioned",
            dropped="、".join(dropped)[:120],
            question=str(message)[:80],
        )
    return kept


def _pick_conditions(values: Any) -> list[dict[str, Any]]:
    """规范化 LLM 抽出的过滤条件（字段/比较符/值），非法项直接丢掉。"""
    if not isinstance(values, (list, tuple)):
        return []
    # 空值类判断：没有 value 也合法（"没填的/有值的"）
    unary_ops = {"is_null", "not_null", "empty", "not_empty"}
    picked: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "").strip().lower()
        op = str(item.get("op") or "").strip().lower()
        value = item.get("value")
        if field not in _CONDITION_FIELDS or op not in _CONDITION_OPS:
            continue
        if op in unary_ops:
            picked.append({"field": field, "op": op, "value": None})
            continue
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                continue
            value = [value[0], value[1]]
        elif op == "in":
            if not isinstance(value, (list, tuple)) or not value:
                continue
            value = list(value)
        elif isinstance(value, (list, tuple)):
            continue
        if field == "collection_time":
            # 时间值必须合法：编号里的数字串不能当时间用
            if op == "between":
                continue
            if _normalize_time_value(value) is None:
                continue
        if op == "in":
            value = [_normalize_enum_value(field, part) for part in value]
        elif op != "between":
            value = _normalize_enum_value(field, value)
        picked.append({"field": field, "op": op, "value": value})
    return picked


def _condition_literal(value: Any, numeric: bool) -> str | None:
    """把条件值变成 SQL 字面量；数值字段出现非法值时返回 None（宁可丢条件也不拼坏 SQL）。"""
    if numeric:
        try:
            return repr(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
    text = str(value).replace("'", "''")
    return f"'{text}'"


def semantic_condition_clauses(plan: dict[str, Any]) -> list[str]:
    """把 LLM 抽出的过滤条件翻成 SQL 片段（"怎么表达"由 LLM 理解，代码只拼 SQL）。"""
    clauses: list[str] = []
    slots = plan.get("slots") or {}
    # 路线/病害类型这两类，计划里已经用 id / 字典解析过了（更可靠），
    # 不要再按名字拼一遍——多一个 JOIN 条件就多一处出错机会。
    skip_fields = set()
    if slots.get("route_match"):
        skip_fields.add("route_name")
    if slots.get("damage_matches"):
        skip_fields.add("problem_type_name")
    for item in (plan.get("slots") or {}).get("semantic_conditions") or []:
        if item["field"] in skip_fields:
            continue
        expr, numeric = _CONDITION_FIELDS[item["field"]]
        op = item["op"]
        value = item["value"]
        if op in {"is_null", "not_null", "empty", "not_empty"}:
            # "没填的 / 有值的"：空字符串和 NULL 都算没值（库里两种都存在）
            if op == "is_null":
                clauses.append(f"{expr} IS NULL")
            elif op == "not_null":
                clauses.append(f"{expr} IS NOT NULL")
            elif op == "empty":
                clauses.append(f"({expr} IS NULL OR {expr} = '')")
            else:
                clauses.append(f"({expr} IS NOT NULL AND {expr} <> '')")
            continue
        if op in {"between", "in"}:
            literals = [_condition_literal(part, numeric) for part in value]
            if any(part is None for part in literals):
                continue
            if op == "between":
                clauses.append(f"{expr} BETWEEN {literals[0]} AND {literals[1]}")
            else:
                clauses.append(f"{expr} IN ({', '.join(literals)})")
            continue
        literal = _condition_literal(value, numeric)
        if literal is None:
            continue
        clauses.append(f"{expr} {op} {literal}")
    return clauses


def condition_clauses(plan: dict[str, Any]) -> list[str]:
    """这次查询要用的全部过滤条件：**LLM 抽的结构优先**，正则版只作兜底补漏。"""
    clauses = semantic_condition_clauses(plan)
    # 兜底的取值来源是**本轮完整问题（优化后）**：复合题的子问题是拆解器改写的，里面会夹举例值
    # （"（如面积大于 5 平方米）"），从改写文本上抠条件等于给用户加了个没问过的条件。
    # 顶层单题没设 `_ROUND_REQUEST`，用的还是问题本身，行为不变。
    for fallback in numeric_conditions(_ROUND_REQUEST.get() or plan.get("message", "")):
        if fallback not in clauses:
            clauses.append(fallback)
    return clauses


# 代码值 → 中文（库里存的是 code，用户说的是中文）。写 SQL 的模型必须知道这层映射，
# 否则会把"待转工单"直接写进 WHERE 变成查不到。
_CONDITION_OP_TEXT = {
    "=": "等于",
    "!=": "不等于",
    ">": "大于",
    ">=": "不小于",
    "<": "小于",
    "<=": "不超过",
}


def _label_with_alts(field: str, label: str) -> str:
    """同名列表的说明：'车道数（路线级 r.lane_count / 路段级 s.lane_count）'。"""
    row = next((item for item in _field_rows() if item.get("name") == field), None)
    alts = list((row or {}).get("alts") or [])
    if not alts:
        return label
    return f"{label}（本字段在多张表都有：{' / '.join(alts)}，按问题选对应表）"


def condition_lines(plan: dict[str, Any]) -> list[str]:
    """把 LLM 抽出的条件写成给模型看的中文约束（Wren / 物理 SQL 两条路都要带上）。"""
    lines: list[str] = []
    for item in (plan.get("slots") or {}).get("semantic_conditions") or []:
        label = _label_with_alts(
            str(item["field"]), _CONDITION_LABELS.get(item["field"], item["field"])
        )
        expr = (_CONDITION_FIELDS.get(item["field"]) or ("", False))[0]
        op = item["op"]
        value = item["value"]
        if op == "between":
            text = f"{label} 在 {value[0]} 到 {value[1]} 之间"
        elif op == "in":
            text = f"{label} 属于 {('、'.join(str(part) for part in value))}"
        else:
            text = f"{label} {_CONDITION_OP_TEXT.get(op, op)} {value}"
        # 库里存的是 code（0/5/10/15 这种），用户说的是中文——把映射一起给模型，
        # 免得它把"待转工单"直接写进 WHERE。冷门字段（收藏/关注/工单状态/来源）全靠这层。
        hint = _CONDITION_VALUE_HINTS.get(item["field"]) or _sampled_hint(str(item["field"]))
        if hint:
            text += f"（取值：{hint}）"
        # 字段不在病害表上时（例如巡检任务编号在 ai_maintain_patrol），必须把**写法**写清楚，
        # 否则模型会照中文名猜成 p.patrol_number → 查空（实测踩过）。
        if expr and not expr.startswith("p."):
            literal = _condition_literal(value, False) if not isinstance(value, list) else None
            if isinstance(value, list):
                literals = ", ".join(_condition_literal(part, False) or "''" for part in value)
                snippet = f"{expr} IN ({literals})"
            else:
                snippet = f"{expr} = {literal}"
            extra = ""
            if expr.startswith("t."):
                extra = "，需要 JOIN ai_maintain_patrol t ON p.patrol_id = t.id"
            text += f"（写法：{snippet}{extra}；不要在病害表上找同名列）"
        lines.append(f"过滤条件（用户原话里的比较，必须出现在 SQL 里）：{text}")
    return lines


_VERIFY_ISSUES = {
    "none",
    "no_aggregation",
    "no_grouping",
    "missing_condition",
    "wrong_subject",
    "wrong_metric",
    "missing_dimensions",
    "wrong_metric_scope",
    "reasoning_insufficient",
    "other",
}


def verify_answer(
    question: str,
    sql: str,
    result: dict[str, Any],
    reply: str = "",
) -> dict[str, Any]:
    """结果校验：这一版结果有没有"答所问"。

    见 docs/结果校验与问题优化方案.md 第三节：只判断"问题与结果是否匹配"，
    不判断 SQL 语法。判错会触发"反思改 SQL + 重查"，所以判错必须能指到具体一类
    （见 prompts/verify_answer.md），不能凭感觉。

    模板路径（写死的 SQL）不调用这个函数——那条路不会答非所问，省掉一次 LLM 往返。
    """
    rows = (result.get("rows") or [])[:10]
    columns = result.get("columns") or []
    if not sql:
        return {"correct": True, "issue_type": "none", "reason": "没有 SQL，跳过校验"}
    # 回复也要进缓存键：判"漏维度"看的是最终回复，同一段 SQL 配不同回复结论不同。
    reply_text = (reply or str(result.get("reply") or ""))[:600]
    key = f"{question}\n{sql}\n{reply_text}"
    if key in _VERIFY_CACHE:
        return _VERIFY_CACHE[key]
    verdict: dict[str, Any] = {"correct": True, "issue_type": "none", "reason": ""}
    try:
        data = chat_json(
            prompts.messages(
                "verify_answer",
                question=question,
                sql=sql[:2000],
                columns=json.dumps(columns, ensure_ascii=False)[:400],
                rows=json.dumps(rows, ensure_ascii=False, default=str)[:1200],
                final_reply=reply_text or "（无）",
                question_optimization=json.dumps(
                    result.get("question_optimization") or {},
                    ensure_ascii=False,
                    default=str,
                )[:800],
            ),
            # 裁判模型可以单独配（chat_settings.verify_model）。实测用小的 flash 模型时，
            # "明显答非所问"也常被判通过，所以没单独配就跟着语义理解模型（默认 qwen3.7-plus），
            # 最后才回落到 SQL 模型。同一个模型既写 SQL 又判自己也容易自说自话。
            model=role_model("verify") or role_model("semantic") or sql_model(),
        )
        if isinstance(data, dict):
            issue = str(data.get("issue_type") or "none").strip().lower()
            target = str(data.get("repair_target") or "").strip().lower()
            if target not in {"none", "answer", "query"}:
                if issue in {"missing_dimensions", "reasoning_insufficient"}:
                    target = "answer"
                elif issue == "none":
                    target = "none"
                else:
                    target = "query"
            verdict = {
                "correct": bool(data.get("correct", True)),
                "issue_type": issue if issue in _VERIFY_ISSUES else "other",
                "reason": _pick_text(data.get("reason"), 200),
                "repair_target": target,
            }
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "verify_answer_failed", exc, question=question[:120])
        return verdict
    if len(_VERIFY_CACHE) >= _VERIFY_CACHE_LIMIT:
        _VERIFY_CACHE.clear()
    _VERIFY_CACHE[key] = verdict
    return verdict


# 错误记忆里用来做相似度匹配的词（问法千变万化，靠这些稳定的业务词对齐）
_ERROR_MATCH_WORDS = (
    "泛油",
    "未识别",
    "未知类型",
    "裂缝",
    "坑槽",
    "车辙",
    "沉陷",
    "平均",
    "占比",
    "比例",
    "密度",
    "间距",
    "每公里",
    "上下行",
    "上行",
    "下行",
    "分组",
    "分布",
    "月份",
    "时间",
    "桩号",
    "置信度",
    "图片",
    "巡查",
    "任务",
    "资产",
    "路线长度",
)


def error_keywords(question: str) -> str:
    """从问题里抽出用于错误记忆匹配的关键词。"""
    return " ".join(word for word in _ERROR_MATCH_WORDS if word in question)


def error_memory_hint(question: str, limit: int = 2) -> str:
    """取以前踩过的同类错误，放进写 SQL 的提示词里（升级5 错误记忆）。

    命中判据是业务词重叠（病害类型/指标/分组维度），没有相似经验就返回空串——
    宁可不提示，也不要把不相关的经验塞进去干扰模型。
    """
    words = set(error_keywords(question).split())
    if not words:
        return ""
    try:
        from ..store import list_sql_errors

        rows = list_sql_errors(limit=80)
    except Exception:  # noqa: BLE001
        return ""
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows:
        overlap = len(words & set(str(row.get("keywords") or "").split()))
        if overlap:
            scored.append((overlap, row))
    if not scored:
        return "（暂无同类错误经验）"
    scored.sort(key=lambda item: item[0], reverse=True)
    lines = []
    for _, row in scored[:limit]:
        note = str(row.get("fix_note") or "").strip()
        lines.append(
            f"- 类似问法「{str(row.get('question'))[:40]}」上次的问题："
            f"{row.get('issue_type')}（{row.get('reason')}）"
            + (f"；正确做法：{note}" if note else "")
        )
    return "\n".join(lines)


def fix_mdl_sql(
    question: str,
    mdl_sql: str,
    result: dict[str, Any],
    verify: dict[str, Any],
    plan: dict[str, Any] | None = None,
    kind: str = "mysql",
) -> str:
    """反思改 SQL：上一版 MDL-SQL 答非所问，让模型看着 SQL+结果自己改。

    见 docs/项目智能化升级方案.md 升级4——不是"改问题重查"，而是直接改 SQL：
    模型能看到自己漏了什么（没分组/漏条件/指标算错），比重新猜一遍有效。
    失败/说信息不足就返回空串，调用方保留原结果。
    """
    if not mdl_sql:
        return ""
    try:
        from . import wren_engine

        models = wren_engine.mdl_context(kind)
    except Exception:  # noqa: BLE001
        models = ""
    required = mdl_condition_lines(plan) if plan else ["无额外强制条件"]
    rows = (result.get("rows") or [])[:10]
    try:
        reply = chat(
            prompts.messages(
                "fix_sql",
                question=question,
                issue_type=str(verify.get("issue_type") or "other"),
                reason=str(verify.get("reason") or ""),
                mdl_sql=mdl_sql[:1500],
                rows=json.dumps(rows, ensure_ascii=False, default=str)[:1000],
                required="\n".join(f"- {item}" for item in required),
                models=models[:8000],
                errors=error_memory_hint(question),
            ),
            temperature=0,
            # 反思改 SQL：要读懂校验意见、判断"改哪一句"，实测是很关键的一环
            # （模型弱就改不对，白白多跑一轮），所以用语义理解模型（默认 qwen3.7-plus）。
            model=role_model("semantic") or role_model("verify") or sql_model(),
        )
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "fix_mdl_sql_failed", exc, question=question[:120])
        return ""
    if "AMBIGUOUS" in reply.upper():
        return ""
    fixed = extract_sql(reply)
    return fixed if fixed and fixed.strip().lower() != mdl_sql.strip().lower() else ""


def refine_question(
    question: str,
    verify: dict[str, Any],
    sql: str,
    result: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> str:
    """问题优化：带着"答非所问"的原因，把问题补成能查准的问法。

    只补口径（按什么分组、算什么指标），不改用户原始意图、不加用户没提的条件。
    改写丢了用户说过的实体就作废（复用 _lost_entities），返回空串让调用方用原问题。
    """
    rows = (result.get("rows") or [])[:10]
    optimization_scope = question_optimization_context(
        question,
        str((plan or {}).get("kind") or "mysql"),
    )
    try:
        reply = chat(
            prompts.messages(
                "optimize_question",
                mode="纠错补全",
                question=question,
                issue_type=str(verify.get("issue_type") or "other"),
                reason=str(verify.get("reason") or ""),
                sql=(sql or "")[:1200],
                rows=json.dumps(rows, ensure_ascii=False, default=str)[:800],
                plan=json.dumps(plan, ensure_ascii=False, default=str)[:800] if plan else "（无）",
                capability=optimization_scope["text"],
            ),
            temperature=0,
            # 问题优化同样是"读懂人话"的活，跟着裁判模型走，没配再用语义理解模型。
            model=role_model("verify") or role_model("semantic") or sql_model(),
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "refine_question_failed", exc, question=question[:120])
        return ""
    refined = reply.strip("`").strip()
    try:
        parsed = json.loads(refined)
        if isinstance(parsed, dict) and parsed.get("question"):
            refined = str(parsed["question"]).strip()
    except (ValueError, TypeError):
        pass
    # 只取前两行（最多两个子问题），去掉模型可能带上的"优化后："前缀
    lines = [line.strip() for line in refined.splitlines() if line.strip()]
    lines = [re.sub(r"^(优化后|输出)[:：]\s*", "", line) for line in lines[:2]]
    refined = "\n".join(lines).strip()
    if len(refined) < 4:
        return ""
    lost = _lost_entities(question, refined)
    if lost:
        log_event(
            "nl2sql",
            "INFO",
            "refine_rejected",
            lost="、".join(lost)[:80],
            question=question[:80],
        )
        return ""
    return refined


def expand_question_for_planning(question: str) -> dict[str, Any] | None:
    """在通用规划前补全“回答完整性”，但不扩大用户的过滤范围。

    返回值本身是带来源的中间对象：原始问题来自用户原话，扩写问题明确标记为问题优化器产出。
    下游规划/模板只能消费 ``question``，诊断与回归可以同时核对 ``original_question``。
    """
    original = str(question or "").strip()
    if not original:
        return None
    if re.search(
        r"(?:具体(?:的)?数据|明细|全部(?:都)?(?:调出来|列出来|列出)|逐条)",
        original,
    ):
        log_event(
            "nl2sql",
            "INFO",
            "expand_question_skipped_self_contained_detail",
            question=original[:80],
        )
        return None
    optimization_scope = question_optimization_context(original, "mysql")
    try:
        reply = chat(
            prompts.messages(
                "optimize_question",
                mode="预规划补全",
                question=original,
                issue_type="（预规划阶段，不适用）",
                reason="（预规划阶段，不适用）",
                sql="（无）",
                rows="（无）",
                plan="（尚未规划）",
                capability=optimization_scope["text"],
            ),
            temperature=0,
            model=role_model("verify") or role_model("semantic") or sql_model(),
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "expand_question_failed", exc, question=original[:120])
        return None
    raw = str(reply or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    payload: dict[str, Any] = {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            payload = parsed
    except (ValueError, TypeError):
        match = re.search(r"\{.*\}", raw, re.S)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    payload = parsed
            except (ValueError, TypeError):
                pass
    if payload:
        expanded = str(payload.get("question") or "").strip()
        dimensions = [
            str(item).strip()
            for item in payload.get("required_dimensions") or []
            if str(item).strip()
        ]
        unsupported_dimensions = [
            item
            for item in dimensions
            if not _optimizer_dimension_supported(
                item,
                list(optimization_scope.get("display_columns") or []),
            )
        ]
        if unsupported_dimensions and optimization_scope.get("table"):
            log_event(
                "nl2sql",
                "INFO",
                "expand_question_rejected_unsupported_fields",
                fields="、".join(unsupported_dimensions)[:80],
                table=str(optimization_scope.get("table") or ""),
                question=original[:80],
            )
            return None
    else:
        lines = [
            re.sub(r"^(优化后|补全后|输出)[:：]\s*", "", line.strip())
            for line in raw.strip("`").splitlines()
            if line.strip()
        ]
        expanded = " ".join(lines).strip()
        dimensions = []
    if len(expanded) < 4:
        return None
    # 预规划补全只能增加汇总维度；用户没要明细时，扩写不得把查询变成全表逐条记录。
    requests_detail = bool(
        re.search(r"全部(?:都)?(?:调出来|列出来|列出)|都调出来|逐条|明细|清单", original)
    )
    for term in ("明细", "全部记录", "逐条", "每条病害"):
        if term in expanded and term not in original and not requests_detail:
            log_event(
                "nl2sql",
                "INFO",
                "expand_question_rejected_unrequested_detail",
                term=term,
                question=original[:80],
            )
            return None
    lost = _lost_entities(original, expanded)
    if lost:
        log_event(
            "nl2sql",
            "INFO",
            "expand_question_rejected",
            lost="、".join(lost)[:80],
            question=original[:80],
        )
        return None
    return {
        "question": expanded,
        "original_question": original,
        "source": "question_optimizer",
        "mode": "preplan",
        "required_dimensions": list(dict.fromkeys(dimensions)),
        "optimization_scope": {
            "source": optimization_scope["source"],
            "table": optimization_scope["table"],
            "subject": optimization_scope["subject"],
            "touched_columns": optimization_scope["touched_columns"],
            "display_fields": [
                str(item.get("column") or "")
                for item in optimization_scope.get("display_columns") or []
            ],
        },
    }


def _pick(value: Any, allowed: set[str], default: Any = None) -> Any:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _pick_list(values: Any, allowed: set[str]) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    picked: list[str] = []
    for item in values:
        text = str(item or "").strip().lower()
        if text in allowed and text not in picked:
            picked.append(text)
    return picked


def _semantic_metric_names() -> set[str]:
    """语义层允许的指标名：短名单 ∪ 字段目录里数值列的 `<聚合>_<列名>`（全字段可达）。"""
    from . import field_catalog

    return set(_SEMANTIC_METRICS) | field_catalog.metric_names("mysql")


def _grounding_hint(grounded: dict[str, Any]) -> str:
    """把**字段触达**的结果压成给规划模型看的一小段（落点表 + 命中的列）。

    这是"指标解析不出来"的上游修法（docs/推荐卡硬推荐与指标空匹配方案.md §六）：
    模型在规划阶段就能看到"这句话点到的列是 里程桩点表.lat（纬度，数值列）"，
    于是"范围"能落到 min/max、output_fields 能落到具体列，而不是留空。

    落点表只能是一张，但**一句话点到两张表的列**是常态（"病害状态以及内置的路线名称"：
    problem_status 在 ai_problem、built 在 basic_facilities_routes）。这类另一张表的命中列
    以前被丢掉了，模型根本不知道 routes 上有 built，于是把"路线名称"猜成 file_name、
    subject 也判成 disease（实测）。这里把跨表命中一并给它，且明说"分属多张表就是 multi"。
    """
    table = str((grounded or {}).get("table") or "")
    strong: list[str] = []
    weak: list[str] = []
    for item in (grounded or {}).get("columns") or []:
        name = str(item.get("column") or "")
        if not name:
            continue
        label = str(item.get("label") or "").strip()
        numeric = "数值列（可当指标：min/max/avg/sum）" if item.get("numeric") else "文本/时间列"
        line = (
            f"  - {item.get('table')}.{name}"
            + (f"（{label}）" if label else "")
            + f" · {numeric} · 匹配分 {item.get('score')}"
        )
        # 强命中 = 列名整段出现在问题里 / 用户说了这列的枚举取值（"国道"）。只有弱命中
        # （靠"病害/名称"这种通用词元重合）的列不能写成"用户点名的列"——实测
        # "内置的路线名称"把 ai_problem.file_name 当成点名列，模型于是拿它当"路线名称"。
        flags = [str(hint) for hint in item.get("hits") or []]
        if any(flag.startswith(("列名命中：", "取值命中：")) for flag in flags):
            strong.append(line)
        else:
            weak.append(line)
    # 另一张表上也命中的列（跨表多字段题的另一半证据）。
    # 只收**表结构语义检索出的候选表**：全量列打分里 1.0 分的噪声列有一堆（ai_asset_base…
    # 都因为"内置/名称"这类通用词拿 1.0），不筛就会把真正要的 routes.built 挤出前几名。
    candidates = [
        str(item.get("name") or "")
        for item in (grounded or {}).get("tables") or []
        if item.get("name")
    ]
    candidate_rank = {name: index for index, name in enumerate(candidates)}
    other_rows: list[tuple[str, float, str]] = []
    for name, cols in ((grounded or {}).get("table_fields") or {}).items():
        name = str(name)
        if name == table or (candidate_rank and name not in candidate_rank):
            continue
        for item in (cols or [])[:3]:
            column = str(item.get("column") or "")
            if not column:
                continue
            label = str(item.get("label") or "").strip()
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            other_rows.append(
                (
                    name,
                    score,
                    f"  - {name}.{column}"
                    + (f"（{label}）" if label else "")
                    + f" · 匹配分 {item.get('score')}",
                )
            )
    other_rows.sort(key=lambda row: (-row[1], candidate_rank.get(row[0], 99)))
    others = [line for _name, _score, line in other_rows[:6]]
    head = f"本地列注释匹配出的落点表：{table or '（未定）'}"
    out = [head]
    if strong:
        out.append("命中列（用户点名的列就是这些，优先用它们当指标/输出字段）：")
        out.extend(strong[:8])
        if weak:
            out.append("这几列只是词元相近（**不算用户点名**，别拿它们顶替用户要的属性）：")
            out.extend(weak[:4])
    elif weak:
        out.append("（没有用户点名的列，下面这几列只是词元相近，仅供参考）：")
        out.extend(weak[:6])
    else:
        out.append("（没有点名的列——按用户语义自己判断指标）")
    if others:
        out.append(
            "⚠ 另一张表上同样命中了这些列（用户点名的属性如果分属不同表，"
            "subject 必须填 multi，并用 fields 逐个列出，带 table_hint）："
        )
        out.extend(others[:6])
    return "\n".join(out)


def _semantic_output_field_names() -> set[str]:
    """语义层允许的输出字段名：短名单 ∪ 字段目录里的每一个列名（全字段可达）。"""
    from . import field_catalog

    return set(_SEMANTIC_OUTPUT_FIELDS) | field_catalog.output_field_names("mysql")


def _canonicalize_semantic_fields(semantics: dict[str, Any]) -> dict[str, Any]:
    """把原始物理长度列统一到可计算的病害长度字段。

    `disease_length` 当前全为空；目录里已经定义了长度 = max(高度, 宽度)。
    所以在进入 SQL 生成前统一换成 `effective_disease_length`，条件和指标都不会再碰空列。
    """
    if not semantics:
        return semantics

    def _length_name(name: str) -> str:
        text = str(name or "")
        if text == "avg_length":
            return "avg_effective_disease_length"
        for prefix in ("sum_", "total_", "avg_", "min_", "max_"):
            if text == f"{prefix}disease_length":
                return f"{prefix}effective_disease_length"
        return "effective_disease_length" if text == "disease_length" else text

    semantics["conditions"] = [
        {**item, "field": _length_name(str(item.get("field") or ""))}
        for item in semantics.get("conditions") or []
        if isinstance(item, dict)
    ]
    semantics["metrics"] = [_length_name(str(item)) for item in semantics.get("metrics") or []]
    semantics["output_fields"] = [
        _length_name(str(item)) for item in semantics.get("output_fields") or []
    ]
    semantics["fields"] = [
        {**item, "name": _length_name(str(item.get("name") or ""))}
        for item in semantics.get("fields") or []
        if isinstance(item, dict)
    ]
    if semantics.get("bucket_metric"):
        semantics["bucket_metric"] = _length_name(str(semantics["bucket_metric"]))
    return semantics


def parse_semantics(
    message: str,
    grounding_hint: str = "",
    grounding_hits: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """维度识别（shape / group_by / metric / 排序 / 派生计算）交给 LLM。

    见 docs/Wren主路径重构方案.md §1.4、§2.2：
    规则版 parse_query_shape 只看关键词（"最多"→top 明细、"分布"→按类型），
    分不清"数量最多的病害类型"是要分组取第一还是取面积最大的单条。
    实体（路线/桩号/时间/病害类型）仍然由规则精确抽取，这一步只管"要什么样的 SQL"。

    失败/超时返回 {}，调用方自动退回规则结果——语义解析是增强，不是必经之路。
    """
    key = message.strip()
    if not key:
        return {}
    if key in _SEMANTIC_CACHE:
        return _SEMANTIC_CACHE[key]
    _started = time.perf_counter()
    semantics: dict[str, Any] = {}
    try:
        from . import wren_engine

        # 主体判定最容易踩的坑：用户用的是自己的叫法（"拍的片子""标了多少个点"），句子里
        # 又带着"病害"两个字，模型就会默认 disease。把**表结构语义检索**出的候选连同命中
        # 证据一起给它，让主体判定有据可依（不写死任何叫法）。
        candidates = (
            "\n".join(
                f"- {item['name']}（相关度 {item['score']}）：命中证据 "
                + "；".join(item.get("hits") or ["（无）"])
                for item in wren_engine.search_models("mysql", message, limit=4)
            )
            or "（无候选）"
        )
        # 列清单（全量字段目录，按 (表,列) 给）：模型要能直接看到"有哪些指标/维度可选"，
        # 而不是只看到表级候选就去猜——"西乡段的纬度范围"解析成 metrics=[] 就是缺这一段
        # （docs/推荐卡硬推荐与指标空匹配方案.md §四.4）。
        try:
            from . import field_catalog as _field_catalog

            models = {
                str(item.get("name") or "")
                for item in wren_engine.search_models("mysql", message, limit=4)
                if item.get("name")
            }
            physical_tables = sorted(wren_engine.physical_tables("mysql", models)) if models else []
            # 把字段触达命中的列塞到各表最前：模型一眼就能看到"用户点的是这列"，而不是
            # 被字段最多的那张表的列埋掉
            hits = {
                str(name): [str(item.get("column")) for item in (cols or [])]
                for name, cols in (grounding_hits or {}).items()
            }
            field_vocab = _field_catalog.field_vocabulary(
                "mysql", message, tables=physical_tables, hits=hits
            )
        except Exception:  # noqa: BLE001
            field_vocab = "（列清单不可用）"
        data = chat_json(
            prompts.messages(
                "query_semantic",
                message=message,
                # 本轮完整问题（优化后）：子问题会被拆解器改写成新句子，只给语义层看子问题的话，
                # 它分不清"优化器补全的合法条件（长对话继承的路线/时间）"和"子问题里的举例值"
                # （见 _ROUND_REQUEST 的说明）。
                round_request=_ROUND_REQUEST.get() or message,
                candidates=candidates,
                fields=field_vocab,
                grounding=grounding_hint or "（本次没有字段触达结果）",
            ),
            model=sql_model(),
        )
        if isinstance(data, dict):
            data = [data]
        # 一句话里问了多个子问题时，模型会回一个数组（每段一个对象）。
        # 这里合并成一个：分组/排序/limit 取第一个非空，指标和点名字段取并集。
        if isinstance(data, list):
            items = [item for item in data if isinstance(item, dict)]
            metrics: list[str] = []
            derived: list[str] = []
            fields: list[str] = []
            unknown_fields: list[str] = []
            # 跨表多字段：一句话要了多张表的字段时，LLM 逐个列出（带它猜的归属表）
            multi_fields: list[dict[str, str]] = []
            multi_relation = ""
            shape = subject = group_by = order_by = limit = None
            bucket_metric = bucket_low = bucket_high = None
            bucket_granularity = bucket_agg = None
            conditions: list[dict[str, Any]] = []
            comparison = None
            needs_conclusion = None
            time_spec: dict[str, Any] | None = None
            time_frame = ""
            bucket_meters: Any = None
            linear_width_m: float | None = None
            group_by_params: dict[str, int] | None = None
            for item in items:
                shape = shape or _pick(item.get("shape"), _SEMANTIC_SHAPES)
                subject = subject or _pick(item.get("subject"), _SEMANTIC_SUBJECTS)
                if not multi_fields:
                    multi_fields = _pick_multi_fields(item.get("fields"))
                if not multi_relation:
                    multi_relation = _pick(item.get("relation"), _SEMANTIC_MULTI_RELATIONS)
                group_by = group_by or _pick(item.get("group_by"), _SEMANTIC_GROUP_BY)
                if group_by_params is None and isinstance(item.get("group_by_params"), dict):
                    # 自定义粒度（每 N 天/小时/分钟）的 N：只认正整数，别让 0/负/字符串流到 SQL 里
                    number = _positive_int(item["group_by_params"].get("n"))
                    if number:
                        group_by_params = {"n": number}
                order_by = order_by or _pick(item.get("order_by"), _SEMANTIC_ORDER)
                if not bucket_metric:
                    bucket_metric = _pick_text(item.get("bucket_metric"), 40) or None
                    bucket_low = _pick_text(item.get("bucket_low_label"), 30) or None
                    bucket_high = _pick_text(item.get("bucket_high_label"), 30) or None
                    bucket_granularity = _pick_text(item.get("bucket_granularity"), 20) or None
                    bucket_agg = _pick_text(item.get("bucket_metric_agg"), 20) or None
                if comparison is None and isinstance(item.get("comparison"), bool):
                    comparison = item["comparison"]
                # 时间参照系：模型给的对象/字符串原样留下（简单表达给 start/end、复杂给 unit/offset/nth），
                # 换算与校验都在下面（`_build_query_plan_uncached` 里那段"时间参照系"）。
                if time_spec is None and isinstance(item.get("time"), dict):
                    time_spec = dict(item["time"])
                elif time_spec is None and str(item.get("time") or "").strip() not in {"", "null"}:
                    time_spec = {"raw": _pick_text(item.get("time"), 60)}
                if not time_frame:
                    time_frame = _pick_text(item.get("time_frame"), 12) or ""
                if bucket_meters in (None, ""):
                    bucket_meters = item.get("bucket_meters")
                if linear_width_m is None and item.get("linear_width_m") not in (None, ""):
                    try:
                        width = float(item["linear_width_m"])
                        if width > 0:
                            linear_width_m = width
                    except (TypeError, ValueError):
                        pass
                if needs_conclusion is None and isinstance(item.get("needs_conclusion"), bool):
                    needs_conclusion = item["needs_conclusion"]
                for condition in _pick_conditions(item.get("conditions")):
                    if condition not in conditions:
                        conditions.append(condition)
                if limit is None and isinstance(item.get("limit"), int):
                    limit = item["limit"]
                # 指标白名单 = 基础名单 ∪ 字段目录里数值列的 <聚合>_<列名>
                # （原来只有 13 个写死的名字，"纬度范围"→min_lat/max_lat 会被整条丢掉）
                for value in _pick_list(item.get("metrics"), _semantic_metric_names()):
                    if value not in metrics:
                        metrics.append(value)
                for value in _pick_list(item.get("derived"), _SEMANTIC_DERIVED):
                    if value not in derived:
                        derived.append(value)
                raw_fields = [
                    str(value or "").strip().lower()
                    for value in item.get("output_fields") or []
                    if str(value or "").strip()
                ]
                picked_fields = _pick_list(item.get("output_fields"), _semantic_output_field_names())
                for value in picked_fields:
                    if value not in fields:
                        fields.append(value)
                # 目录里查不到的字段名**不能丢**：它们是"用户点名、但库里没有这项记录"的唯一证据
                # （"桥在哪个省" → province），丢了下游只能拿别的列顶替或者含糊其辞。
                # 单独留一份，落点判定/如实回答用它，正常取数路径不受影响。
                for name in raw_fields:
                    if name not in set(picked_fields) and name not in unknown_fields:
                        unknown_fields.append(name)
            # 跨表多字段：只要 fields 里出现了 ≥2 张**不同**的表，subject 一律按 multi 处理。
            # 不指望 LLM 记得把 subject 改成 multi——实测它照样填 disease/route（fields 却填对了），
            # 于是跨表链路进不去（docs/跨表多字段并列查询_系统性整改方案.md §三 层 1）。
            hinted_tables = {item["table_hint"] for item in multi_fields if item["table_hint"]}
            if len(multi_fields) >= 2 and len(hinted_tables) >= 2:
                subject = "multi"
            semantics = {
                "shape": shape,
                "subject": subject,
                "group_by": group_by,
                "metrics": metrics,
                "order_by": order_by,
                "limit": limit,
                "derived": derived,
                "output_fields": fields,
                # 跨表多字段题的字段清单与关系类型（见 docs/跨表多字段并列查询_系统性整改方案.md）
                "fields": multi_fields,
                "relation": multi_relation,
                # 用户点名、但字段目录里没有的字段名（"库里没这项记录"的证据，见上面注释）
                "unknown_output_fields": unknown_fields,
                # 分档对比：按哪个指标分、两档分别叫什么（LLM 语义识别，不靠关键词）
                "bucket_metric": bucket_metric,
                "bucket_low_label": bucket_low,
                "bucket_high_label": bucket_high,
                "bucket_granularity": bucket_granularity,
                "bucket_metric_agg": bucket_agg,
                # 编码类条件必须能在用户原话里对上（语义层会凭空造条件，见 _condition_mentioned）
                "conditions": _kept_conditions(conditions, message),
                "comparison": comparison,
                "needs_conclusion": needs_conclusion,
                # ⚠️ 这里是**逐字段重建**返回值的：新加的字段必须在这里列出来，否则模型填了也会被丢掉
                # （实测 `time`/`time_frame`/`bucket_meters` 就是这么丢的——提示词里写了、模型也填了，
                #  但重建时没带，上层一直读到 None，看起来像"模型蠢"，其实是代码丢字段）。
                "time": time_spec,
                "time_frame": time_frame,
                "bucket_meters": bucket_meters,
                "linear_width_m": linear_width_m,
                # 自定义时间粒度的 N（"每 10 天/每 4 小时"）——上面 _positive_int 校验过
                "group_by_params": group_by_params,
            }
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "semantic_parse_failed", exc, question=message[:120])
        semantics = {}
    semantics = _canonicalize_semantic_fields(semantics)
    if len(_SEMANTIC_CACHE) >= _SEMANTIC_CACHE_LIMIT:
        _SEMANTIC_CACHE.clear()
    _SEMANTIC_CACHE[key] = semantics
    # 弹卡耗时打点（docs/弹卡链路系统性Bug整改方案.md Bug 4）：语义规划是弹卡前的一次 LLM
    # 调用，单独计时，才知道 9 秒到底花在哪一步
    log_event(
        "nl2sql",
        "INFO",
        "semantics_ms",
        ms=round((time.perf_counter() - _started) * 1000),
        subject=semantics.get("subject") or "",
    )
    return semantics


def build_query_plan(message: str, kind: str = "mysql") -> dict[str, Any]:
    """查询计划（带**同一句话**的进程内缓存）。

    语义解析本身早有缓存，但"组计划"这一步（规则解析 + 路线/病害类型解析 + 字段触达）以前
    每问一次都重跑：同一句话在同一轮对话里会被问到两三次（摆卡、点确认后的复核、
    用户自己再问一遍），每次都白花 0.5~1 秒的连库。
    缓存 5 分钟、上限 200 条；命中时返回深拷贝，避免调用方改到缓存对象。
    """
    key = (
        str(message or "").strip(),
        str(kind or "mysql"),
        # 追问里的"那次/那个"会把上一轮的对象当硬条件注入计划：它变了，计划就不是同一个
        json.dumps(_ENTITY_FILTER.get() or {}, ensure_ascii=False, sort_keys=True, default=str),
        # 本轮完整问题（优化后）也要进 key：语义层靠它判断"哪些条件是真的"（见 _ROUND_REQUEST），
        # 同一句子问题在不同完整问题下会解析成不同的计划——不并进 key 就会拿到别人的缓存结果。
        _ROUND_REQUEST.get(),
    )
    cached = _PLAN_CACHE.get(key)
    now = time.time()
    if cached and now - cached[0] < _PLAN_CACHE_TTL:
        return copy.deepcopy(cached[1])
    plan = _build_query_plan_uncached(message, kind)
    if len(_PLAN_CACHE) >= _PLAN_CACHE_LIMIT:
        _PLAN_CACHE.clear()
    _PLAN_CACHE[key] = (now, copy.deepcopy(plan))
    return plan


_PLAN_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_PLAN_CACHE_TTL = settings.plan_cache_ttl_seconds
_PLAN_CACHE_LIMIT = settings.cache_max_entries


def _build_query_plan_uncached(message: str, kind: str = "mysql") -> dict[str, Any]:
    shape = ""
    comparison = False
    slots = parsers.parse_slots(
        message,
        # 时间只认"用户原话里的说法"（复合题子问题的改写文本会夹举例值）。
        time_source=_ROUND_REQUEST.get() or message,
    )
    # 路线/病害类型/方向等命名实体只接受语义模型结果；规则只保留时间、桩号等结构槽。
    slots["route"] = None
    slots["damage_type"] = None
    slots["up_or_down"] = None
    plan: dict[str, Any] = {
        "kind": kind,
        "message": message,
        "shape": shape,
        "subject": "",
        "slots": slots,
        "required_conditions": [],
        "comparison": comparison,
    }
    # 字段触达（纯本地、零 LLM，~5ms）要在**语义规划之前**先算一次：规划提示词里那段
    # "用户点名的列就在这几行里"（<<grounding>>）以前永远传的是空串——调用方写的是
    # parse_semantics(message)，没带 grounding_hint，模型只能靠猜列名
    # （"内置的路线名称"被猜成 ai_problem.file_name 就是这么来的）。
    # 这里按**规则主体**先算一份喂给规划；规划拿到语义主体后会再算一份给 plan["grounded"]
    # 用（那一次的表级消歧要用语义主体，见下面 field_grounding.ground 的注释）。
    pre_grounded: dict[str, Any] = {}
    if kind in {"mysql", "postgresql"}:
        try:
            from . import field_grounding as _field_grounding

            pre_grounded = _field_grounding.ground(kind, message, plan=plan)
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "field_grounding_pre_failed", exc, question=message[:80])
    # 维度识别（shape/group_by/metric/排序/派生）交给 LLM，规则结果只做兜底。
    # 见 docs/Wren主路径重构方案.md §1.4 / §2.2：实体抽取保留规则，维度识别换 LLM。
    semantics = (
        parse_semantics(
            message,
            _grounding_hint(pre_grounded),
            (pre_grounded or {}).get("table_fields"),
        )
        if kind in {"mysql", "postgresql"}
        else {}
    )
    if semantics:
        plan["semantic"] = semantics
        # ── 语义优先（docs/项目智能化升级方案.md：判断权从规则拿给 LLM）──
        # shape / 主体 / 是否对比：**以 LLM 的语义理解为准**，规则结果只留作 LLM 失败时的兜底。
        # 规则的 shape 记在 rule_shape 里，只用于少数确定性口径的兜底判断。
        rule_shape = plan["shape"]
        plan["rule_shape"] = rule_shape
        if semantics.get("shape"):
            plan["shape"] = str(semantics["shape"])
        if semantics.get("subject") and kind == "mysql":
            plan["subject"] = str(semantics["subject"])
        if isinstance(semantics.get("comparison"), bool):
            plan["comparison"] = bool(semantics["comparison"])
        # 语义层说"要对比"时，校验里的 comparison 约束要跟着走（规则判的以语义为准）
        wants_comparison = bool(plan["comparison"])
        has_comparison = "comparison" in plan["required_conditions"]
        if wants_comparison and not has_comparison:
            plan["required_conditions"].append("comparison")
        elif not wants_comparison and has_comparison:
            plan["required_conditions"].remove("comparison")
        # 看图诉求：以语义层给的 shape=photo 为准（只有用户明确要图片本体才是 photo）。
        # output_fields 里的 image 只表示"顺手把图片列带上"——以前拿它硬改 shape，
        # 结果"把那次巡检的明细调出来"被当成看图，SQL 被加上"必须有图片"的过滤。
        # 语义层没给出 shape（LLM 超时/失败）时，才退回这个弱信号。
        if not semantics.get("shape") and "image" in (semantics.get("output_fields") or []):
            plan["shape"] = "photo"
        slots["semantic"] = semantics
        if semantics.get("group_by"):
            slots.setdefault("group_by", str(semantics["group_by"]))
        slots["semantic_metrics"] = semantics.get("metrics") or []
        slots["semantic_derived"] = semantics.get("derived") or []
        slots["semantic_fields"] = semantics.get("output_fields") or []
        # 分档对比（bucket_compare）：LLM 说清"哪个指标 + 两档叫什么"，
        # 中位数分界线由代码按数据算（见 _build_bucket_template）。
        if semantics.get("bucket_metric"):
            slots["bucket_metric"] = str(semantics["bucket_metric"])
            slots["bucket_low_label"] = str(semantics.get("bucket_low_label") or "低档")
            slots["bucket_high_label"] = str(semantics.get("bucket_high_label") or "高档")
            # 分档粒度/聚合：什么算"一条"（单条病害 / 每公里 / 每路段）、比它的什么值
            slots["bucket_granularity"] = str(semantics.get("bucket_granularity") or "row")
            slots["bucket_agg"] = str(semantics.get("bucket_metric_agg") or "count")
        # 分档粒度（米）：语义层从"每 100 米一段/每公里一段/每 500 米"里读出来（不靠关键词），
        # 模板复用时用它填"分档粒度"槽（`templatizer._plan_param_value` 的 bucket 分支）。
        granularity = semantics.get("bucket_meters")
        if granularity in (None, "") and str(slots.get("group_by") or "") == "pile_bucket":
            # 语义层已经判过"这是按桩号分档"（group_by=pile_bucket），只是没把粒度数字带上。
            # 数字在这里抽：**单位词表是封闭的**（米/公里/千米/km/m），说法怎么变都能抽；
            # 判断"要不要抽"仍然由语义层决定，不是关键词触发。
            granularity = _pile_bucket_meters(str(message))
        if granularity not in (None, ""):
            try:
                slots["bucket_meters"] = int(float(granularity))
            except (TypeError, ValueError):
                slots["bucket_meters"] = None
        if semantics.get("linear_width_m") not in (None, ""):
            try:
                slots["linear_width_m"] = float(semantics["linear_width_m"])
            except (TypeError, ValueError):
                slots["linear_width_m"] = None
        # 数值/等值过滤条件：LLM 抽出来（用户怎么表达都行），代码翻成 SQL
        if semantics.get("conditions"):
            slots["semantic_conditions"] = list(semantics["conditions"])
            # 编号类条件（巡查编号/巡检任务号/病害编号）靠探测落到正确的列上：
            # 语义层按中文名猜的列可能是空的（"任务号 XJJL…"→p.patrol_number 永远 0 条）。
            slots["semantic_conditions"] = _resolve_id_conditions(
                slots["semantic_conditions"], plan
            )
            _canonicalize_semantic_type_values(plan)
            # "未识别类型"归一化：语义层有时把它抽成 problem_type_name is_null（对），
            # 有时由于字典里没有这个词而整个丢掉，甚至错抽成 problem_status='0'
            # （"未识别"被当成了工单状态）→ 统一收敛成一个标记，交给下面各条 SQL 生成路径
            # 用 `damage_type IS NULL/''` 表达（见 _UNKNOWN_TYPE_WORDS / _unknown_type_sql）。
            _canonicalize_unknown_type(plan, message)
            # 时间条件的**方向**只有语义层分得清（"7月15开始"是起点、"到7月15为止"是终点）。
            # 规则解析在这种说法上会把"从X开始"当成整天/整个月（实测过），所以这里以语义为准覆盖。
            for item in slots["semantic_conditions"]:
                if item.get("field") != "collection_time":
                    continue
                op = str(item.get("op") or "")
                value = item.get("value")
                if op in {">", ">="} and value:
                    normalized = _normalize_time_value(value)
                    if not normalized:
                        continue
                    slots["time_start"] = normalized
                    slots["time_end"] = None
                    if "time_range" not in plan["required_conditions"]:
                        plan["required_conditions"].append("time_range")
                elif op in {"<", "<="} and value:
                    normalized = _normalize_time_value(value)
                    if not normalized:
                        continue
                    slots["time_end"] = normalized
                    if "time_range" not in plan["required_conditions"]:
                        plan["required_conditions"].append("time_range")
                elif op == "between" and isinstance(value, (list, tuple)) and len(value) == 2:
                    start = _normalize_time_value(value[0])
                    end = _normalize_time_value(value[1])
                    if not start or not end:
                        continue
                    slots["time_start"] = start
                    slots["time_end"] = end
                    if "time_range" not in plan["required_conditions"]:
                        plan["required_conditions"].append("time_range")
                break
    # ── 时间参照系（语义层）→ 区间（代码算术）：docs/值换算层统一修复方案.md §2.1 ────────────
    # 语义层只说"是哪一类时间"（`{"unit":"week","offset":-1,"nth":3}` 这种），
    # 周/月/季/季内的第几个怎么落到具体日期完全由代码算——说法再多也不用各自写一条正则。
    time_spec = semantics.get("time") if isinstance(semantics, dict) else None
    if isinstance(time_spec, dict) and time_spec:
        span = None
        # ① 简单时间（上半年/下半年/第 N 季度/7 月…）：语义层**直接给起止**，代码不再算
        direct_start = _normalize_time_value(time_spec.get("start") or time_spec.get("time_start"))
        direct_end = _normalize_time_value(time_spec.get("end") or time_spec.get("time_end"))
        if direct_start and direct_end and direct_start < direct_end:
            span = (datetime.fromisoformat(direct_start), datetime.fromisoformat(direct_end))
            source = "semantics_direct"
        else:
            # ② 中等/复杂的（上个月第三个星期、第 N 周、去年冬天…）：语义层只说"哪一类"，
            #    具体日期由代码算（这类的日期算术模型容易错）。
            span = parsers.time_range_from_spec(time_spec)
            source = "semantics_spec"
        if span:
            slots["time_start"] = span[0].strftime("%Y-%m-%d %H:%M:%S")
            slots["time_end"] = span[1].strftime("%Y-%m-%d %H:%M:%S")
            if "time_range" not in plan["required_conditions"]:
                plan["required_conditions"].append("time_range")
            log_event(
                "nl2sql",
                "INFO",
                "time_range_from_semantics",
                source=source,
                spec=str(time_spec)[:80],
                span=f"{span[0]:%Y-%m-%d}~{span[1]:%Y-%m-%d}",
                question=str(message)[:50],
            )
    if isinstance(semantics, dict):
        # 参照系（历史/现在/未来）：上层用它决定"要不要如实说没有这类数据"（如未来/预测）。
        slots["time_frame"] = str(semantics.get("time_frame") or "") or None
    # ── 时间：**语义优先、规则只兜底、绝不拿今天当默认**（docs/时间解析语义与规则协同方案）──
    # 上一版是反的（"规则和语义不一样时以规则为准"），那会把语义层正确的结果盖掉：
    # 实测"2026 年第三季度"被年分支吃成整年、"时间范围是 7/20~7/26 + 这周"被写成当前周。
    # 现在的顺序：
    #   ① 语义层给了区间（`{"start":…,"end":…}` 或 spec）→ **用它**（下面那段已经设好）；
    #   ② 语义层没给 → 规则解析兜底（只在没有任何时间条件时补）；
    #   ③ 语义层给了"正好是今天/本周/本月"、而句子里**没有任何时间说法** → 判为它拿 now 兜的，清掉。
    # ②的**取值来源是本轮完整问题（优化后）**（`_ROUND_REQUEST`），不是这一步的改写文本：
    # 拆解器会在子问题里夹举例值（"输出周次标签（如'2026年第31周'）"），从改写文本上抠时间
    # 等于给用户凭空加了个他根本没问过的条件——实测第一步被缩到 7/27~8/3，再叠加"取第2个"直接 0 行。
    # 规则抽实体本身是设计内的（完整问题里的"7月15日"要抽得准），错的是拿改写文本当来源。
    if not (slots.get("time_start") or slots.get("time_end")):
        rule_source = _ROUND_REQUEST.get() or str(message)
        try:
            rule_span = parsers.parse_time_range(rule_source)
        except Exception:  # noqa: BLE001
            rule_span = None
        if rule_span:
            slots["time_start"] = rule_span[0].strftime("%Y-%m-%d %H:%M:%S")
            slots["time_end"] = rule_span[1].strftime("%Y-%m-%d %H:%M:%S")
            if "time_range" not in plan["required_conditions"]:
                plan["required_conditions"].append("time_range")
            log_event(
                "nl2sql",
                "INFO",
                "time_range_from_rules_fallback",
                span=f"{slots['time_start'][:10]}~{slots['time_end'][:10]}",
                # 打出来源：排查"这个时间是哪来的"时一眼能看出是从用户原话还是改写文本抠的
                source=("round_request" if _ROUND_REQUEST.get() else "message"),
                question=rule_source[:60],
            )
    elif not _message_mentions_time(_ROUND_REQUEST.get() or str(message)) and _looks_like_now_window(
        slots.get("time_start"), slots.get("time_end")
    ):
        # 句子里没有任何时间说法，语义层却给了时间 → 多半是它拿 now 兜的，不能当条件用
        # （只在**看着就是"今天/本周/本月"那个窗口**时才清：规则不认识"最近两周"这类说法，
        #   那属于"规则兜底覆盖不全"，不是语义层瞎编，不能一起清掉）
        # "有没有时间说法"看的是**本轮完整问题（优化后）**，不是这一步的改写文本：
        # 实测拆解器写出的步骤问法里带"哪一周/这一周"，规则解析会在它上面抠出"本周"，
        # 于是护栏以为"句子里有时间说法"，把模型兜出来的本周留下了 → 第一步查空 → 链断。
        log_event(
            "nl2sql",
            "INFO",
            "time_range_dropped_no_evidence",
            got=f"{str(slots.get('time_start'))[:10]}~{str(slots.get('time_end'))[:10]}",
            question=str(message)[:60],
        )
        slots["time_start"] = None
        slots["time_end"] = None
        plan["required_conditions"] = [
            item for item in plan["required_conditions"] if item != "time_range"
        ]
    # “任务登记/涉及了哪些路线”必须以巡查任务为起点：ai_maintain_patrol 没有 route_id，
    # 只能经 disease.patrol_id -> disease.routes_id 走到 route。语义模型容易只看答案列
    # “路线名称”而把主体判成 route，这里按明确的业务关系纠正。
    semantic_route_scope = (
        _semantic_decided(plan)
        and str((semantics or {}).get("subject") or "") in {"route", "patrol"}
        and "route_name" in set((semantics or {}).get("output_fields") or [])
        and not _semantic_condition_values(plan, {"route_name", "route", "route_id"})
    )
    if kind == "mysql" and semantic_route_scope:
        previous = str(plan.get("subject") or "")
        plan["subject"] = "patrol"
        plan["route_scope_from_patrol"] = True
        if isinstance(semantics, dict):
            semantics["subject"] = "patrol"
            if semantic_route_scope and not is_patrol_route_overview_plan(plan):
                plan["shape"] = "list"
                semantics["shape"] = "list"
                semantics["metrics"] = []
                semantics["output_fields"] = []
                slots["semantic_metrics"] = []
                slots["semantic_fields"] = []
        log_event(
            "nl2sql",
            "INFO",
            "patrol_route_scope",
            previous_subject=previous,
            question=message[:60],
        )
    # 字段触达：这句话最可能落在哪张表/哪一列（纯本地、零 LLM，docs/字段目录配置与字段触达前置方案.md）。
    # 台账类问题（"隧道表的通风方式明细""隧道净宽净高"）靠它把落点锁到 basic_facilities_tunnels.ventilate_mode，
    # 而不是按语义层的 subject 回落到资产表；候选/选表/写 SQL 都复用它。
    # 字段触达：这句话最可能落在哪张表/哪一列（纯本地、零 LLM，docs/字段目录配置与字段触达前置方案.md）。
    # 台账类问题（"隧道表的通风方式明细""隧道净宽净高"）靠它把落点锁到 basic_facilities_tunnels.ventilate_mode，
    # 而不是按语义层的 subject 回落到资产表；候选/选表/写 SQL 都复用它。
    try:
        from . import field_grounding

        plan["grounded"] = field_grounding.ground(kind, message, plan=plan)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "field_grounding_failed", exc, question=message[:80])
    explicit_subject = _explicit_landing_subject(
        kind,
        message,
        plan.get("grounded") or {},
    )
    if explicit_subject and explicit_subject != str(plan.get("subject") or ""):
        previous = str(plan.get("subject") or "")
        plan["subject"] = explicit_subject
        if isinstance(semantics, dict):
            semantics["subject"] = explicit_subject
        slots["semantic"] = semantics
        log_event(
            "nl2sql",
            "INFO",
            "subject_from_explicit_table",
            frm=previous,
            to=explicit_subject,
            table=str((plan.get("grounded") or {}).get("table") or ""),
            question=message[:80],
        )
    # 语义层没给主体时，用**字段触达的落点表**反哺主体（表名 → 主体名，只做前缀剥离）。
    # 模型有时因为 <<grounding>> 已经写明落点就省略了 subject，plan["subject"] 于是退回规则默认的
    # disease——实测"宝安大道西乡段的基本情况是什么样的"因此被当成病害题，内置模板 route_info
    # （要求 subject=route）匹配不上。**只在模型没给主体时补**，绝不覆盖模型的结论。
    if kind == "mysql" and not str((semantics or {}).get("subject") or "").strip():
        grounded_subject = str((plan.get("grounded") or {}).get("subject") or "")
        if grounded_subject in _SEMANTIC_SUBJECTS and grounded_subject != plan.get("subject"):
            log_event(
                "nl2sql",
                "INFO",
                "subject_from_grounding",
                frm=str(plan.get("subject") or ""),
                to=grounded_subject,
                question=message[:60],
            )
            plan["subject"] = grounded_subject
    # 桥/隧/边坡的"名称 → id"：落点表本身就是这三张台账时，把问题里的资产名解析成 id 过滤，
    # 和路线走同一条链路（docs/桥隧边坡资产名称ID解析方案.md）。三张表现在是空的，
    # 解析不出名字就什么都不做；同名多座只记候选，不硬猜。
    try:
        from . import asset_resolver

        landing = str((plan.get("grounded") or {}).get("table") or "")
        asset_subject = asset_resolver.subject_of(landing)
        asset_values = _semantic_condition_values(
            plan, {"asset_name", "name", "bridge_name", "tunnel_name", "slope_name"}
        )
        if asset_subject and asset_values:
            hit = asset_resolver.resolve(kind, asset_subject, asset_values[0])
            if hit.get("id"):
                slots["asset_match"] = {
                    "id": str(hit["id"]),
                    "name": str(hit.get("name") or ""),
                    "table": landing,
                    "token": str(hit.get("token") or ""),
                }
                plan["required_conditions"].append("asset")
            elif hit.get("candidates"):
                slots["asset_candidates"] = hit["candidates"]
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "asset_resolve_failed", exc, question=message[:80])
    # 路段名称同样先由 plan 判定主体，再拿实时路段台账做字符匹配；不在代码里穷举“…线”词。
    section_requested = str(plan.get("subject") or "") == "section" or any(
        str(item.get("field") or "") == "section_name"
        for item in (slots.get("semantic_conditions") or [])
    )
    if kind == "mysql" and section_requested:
        try:
            from . import section_resolver

            semantic_sections = (
                _semantic_condition_values(plan, {"section_name", "route_name"})
                if _semantic_decided(plan)
                else []
            )
            hit = None
            if _semantic_decided(plan):
                for value in semantic_sections:
                    hit = section_resolver.resolve(value)
                    if hit.get("matches"):
                        break
            hit = hit or {"id": "", "name": "", "matches": [], "token": ""}
            if hit.get("unique") and hit.get("id"):
                slots["section_match"] = {
                    "id": str(hit["id"]),
                    "name": str(hit.get("name") or ""),
                    "token": str(hit.get("token") or ""),
                }
                plan["required_conditions"].append("section")
                if str(plan.get("subject") or "") == "section":
                    # 唯一路段 id 已经隐含所属路线；继续保留 route 条件只会诱导模型
                    # 多 JOIN disease/route，甚至把无病害的路段查成 0。
                    plan["required_conditions"] = [
                        item for item in plan["required_conditions"] if item != "route"
                    ]
                    slots.pop("route_match", None)
                    slots["semantic_conditions"] = [
                        item
                        for item in (slots.get("semantic_conditions") or [])
                        if str(item.get("field") or "") not in {"route_name", "route", "route_id"}
                    ]
            elif hit.get("matches"):
                slots["section_candidates"] = list(hit["matches"][:6])
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "section_resolve_failed", exc, question=message[:80])
    # 追问"那次/那个/它"时，上一轮记下的具体对象直接当硬条件
    # （候选列里探测出真能对上数据的那一列，避免过滤到错列 → 0 条）
    entity_condition = _resolve_entity_condition(plan)
    if entity_condition:
        entity_values = {
            str(item)
            for item in ((_ENTITY_FILTER.get() or {}).get("values") or [entity_condition["value"]])
        }
        # 同一个 id 值，模型可能猜到了别的列（"巡查编号"→patrol_number）；
        # 已经用探测定出正确列了，就把猜错的那条丢掉——两条 AND 起来永远是 0 条。
        conditions_now = [
            item
            for item in (slots.get("semantic_conditions") or [])
            if not (
                item.get("field") != entity_condition["field"]
                and str(item.get("value")) in entity_values
            )
        ]
        if entity_condition not in conditions_now:
            conditions_now.append(entity_condition)
        slots["semantic_conditions"] = conditions_now
    if comparison:
        # 对比类问题：不能按单一对象过滤，必须分组对比
        plan["required_conditions"].append("comparison")

    if kind == "mysql":
        semantic_available = _semantic_decided(plan)
        subject = str(plan.get("subject") or "disease")
        route_values: list[str] = []
        if semantic_available:
            if subject == "route":
                route_values = _semantic_condition_values(
                    plan, {"route_name", "route", "route_id", "full_name", "code"}
                )
            elif subject in {"disease", "patrol"}:
                route_values = _semantic_condition_values(
                    plan, {"route_name", "route", "route_id"}
                )
            matches = []
            seen_route_ids: set[str] = set()
            for value in route_values:
                route = _resolve_route(value)
                route_id = str(route.get("id") or "")
                if route_id and route_id not in seen_route_ids:
                    seen_route_ids.add(route_id)
                    matches.append(route)
            slots["route"] = (
                route_values[0] if len(route_values) == 1 else None
            )
        else:
            matches = []
        if len(matches) > 1:
            # 多路线对比：不能只过滤其中一条，交给 LLM 按路线分组统计
            slots["routes"] = [
                {
                    "id": str(item["id"]),
                    "name": str(
                        item.get("short_name") or item.get("full_name") or item.get("code") or ""
                    ),
                }
                for item in matches
            ]
            plan["multi_route"] = True
            plan["required_conditions"].append("multi_route")
            # 多路线时，语义层抽到的"单条路线名"条件必须丢掉：它只覆盖其中一条，
            # 和 r.id IN (两条) 与自己 AND 起来就只剩一条
            # （实测"西乡段和恒明珠段7月各多少处"只回了西乡段 911，恒明珠被说成没有记录）。
            if slots.get("semantic_conditions"):
                slots["semantic_conditions"] = [
                    item
                    for item in slots["semantic_conditions"]
                    if item.get("field") != "route_name"
                ]
        else:
            route_text = route_values[0] if semantic_available and route_values else ""
            if route_text:
                slots["route"] = route_text
                route = _resolve_route(route_text)
                slots["route_match"] = route
                plan["required_conditions"].append("route")
                # "按路段分组"在这条路只有一个整段时没有意义（只有一行、密度还会算错）。
                # 如实说明 + 自动改按桩号分段，比让模型自己把整段切成 100 组强。
                if str(slots.get("group_by") or "") == "section" and (
                    _route_section_count(str(route.get("id") or "")) <= 1
                ):
                    slots["group_by"] = "pile_bucket"
                    slots["group_note"] = (
                        "这条路台账里只有一个整段，按路段分组只有一行、看不出分布；"
                        "已改成按桩号分段来看更细的分布"
                    )
        if str(plan.get("subject") or "") == "section" and (slots.get("section_match") or {}).get("id"):
            # 路段 id 已经隐含所属路线；去掉后加上的 route 条件，避免 MDL 通过 disease
            # 绕回路线，导致“没有病害的路段”被 JOIN 成 0 条。
            plan["required_conditions"] = [
                item for item in plan["required_conditions"] if item != "route"
            ]
            slots.pop("route_match", None)
            slots["semantic_conditions"] = [
                item
                for item in (slots.get("semantic_conditions") or [])
                if str(item.get("field") or "") not in {"route_name", "route", "route_id"}
            ]
        # 「K1 以西」这类单边说法自带桩号条件，别再把括号里的 K1+000 当成区间。
        if (
            slots.get("pile_start") is not None
            and not comparison
            and not parsers.parse_pile_side(message)
        ):
            plan["required_conditions"].append("pile_range")
        semantic_type_values = _semantic_condition_values(
            plan, {"problem_type_name", "problem_type", "damage_type_name"}
        )
        if semantic_available:
            if semantic_type_values and not comparison:
                matched: list[dict[str, str]] = []
                seen_types: set[str] = set()
                for value in semantic_type_values:
                    for item in _resolve_damage_type(value):
                        name = str(item.get("problem_type") or "")
                        if name and name not in seen_types:
                            seen_types.add(name)
                            matched.append(item)
                slots["damage_matches"] = matched
                slots["damage_type"] = (
                    semantic_type_values[0] if len(semantic_type_values) == 1 else None
                )
                if "damage_type" not in plan["required_conditions"]:
                    plan["required_conditions"].append("damage_type")
            else:
                slots.pop("damage_type", None)
                slots.pop("damage_matches", None)
                plan["required_conditions"] = [
                    item for item in plan["required_conditions"] if item != "damage_type"
                ]
        # 设备号：MySQL 侧对应 ai_problem.device_code；短号先按设备台账解析成唯一编码。
        semantic_devices = _semantic_condition_values(plan, {"device_code"})
        raw_device = semantic_devices[0] if semantic_available and semantic_devices else None
        if raw_device:
            resolved = asset_resolver.resolve_device(raw_device)
            slots["device_id"] = resolved.get("code") or raw_device
            if resolved.get("name"):
                slots["device_name"] = resolved["name"]
            if resolved.get("code"):
                for item in slots.get("semantic_conditions") or []:
                    if str(item.get("field") or "") == "device_code":
                        item["value"] = resolved["code"]
        else:
            slots["device_id"] = ""
        if slots.get("device_id"):
            plan["required_conditions"].append("device_code")
        # 时间条件声明放在"起点/终点方向"修正之后：语义层抽出时间就得声明成必须条件，
        # 否则模板/生成的 SQL 把时间丢掉也能通过校验 —— 实测"2026 年 6 月全库病害"和
        # "2026 年 7 月"都答成全表 1702（条件只写在口径文案里）
        # （docs/通用算子过滤丢失与自动补物理SQL修复.md §一）。
        # 也不能再挂 `not comparison`：对比类问题照样有时间条件。
        if slots.get("time_start") or slots.get("time_end"):
            if "time_range" not in plan["required_conditions"]:
                plan["required_conditions"].append("time_range")
        # 「分布在多少个不同的桩号上」＝不同桩号个数（语义层的 metrics 里有 distinct_pile）
        if "distinct_pile" in (semantics.get("metrics") or []) or (
            # LLM 没识别出来时，用最保守的兜底：明确在问"不同桩号"才算
            "桩号" in message and any(word in message for word in ("不同", "几个桩号", "多少个桩号"))
        ):
            slots["distinct_pile"] = True
            plan["shape"] = "detail"
        # 「是通过什么类型的巡查发现的」→ 按巡查类型分组。
        # 只在**问题确实在问"巡查类型"**时才改写 shape，且不能覆盖语义层的判断
        # （实测这条规则把"哪次巡检发现的病害最多"从 top 改成了 distribution，导致并列没列出）。
        if (
            plan["subject"] == "patrol"
            and not slots.get("group_by")
            and plan["shape"] != "top"
            and any(word in message for word in ("类型", "方式", "patrol_type"))
        ):
            slots["patrol_group"] = "patrol_type"
            plan["shape"] = "distribution"
    if kind == "postgresql":
        pg_devices = _semantic_condition_values(plan, {"device_id", "device_code"})
        slots["device_id"] = pg_devices[0] if pg_devices else ""
        slots["pg_table"] = _pg_table_of(message)
        if slots.get("device_id"):
            plan["required_conditions"].append("device_id")
        if slots.get("time_start"):
            plan["required_conditions"].append("time_range")
    # pile_bucket 的语义复核：模型看到"段/哪一段"容易一律填桩号分段，而"把一个月分成
    # 上、中、下三段""每三个月一段"的**切分依据是时间刻度**（提示词正反例都写了仍会被带跑）。
    # 只在已经填了 pile_bucket 时才多花这一次调用；判出来是时间刻度就换成对应维度。
    if str(slots.get("group_by") or "") == "pile_bucket":
        rebased = _bucket_basis_dimension(_ROUND_REQUEST.get() or str(message))
        if rebased:
            log_event(
                "nl2sql",
                "INFO",
                "pile_bucket_rebased_to_time_dim",
                group_by=rebased,
                question=str(message)[:60],
            )
            slots["group_by"] = rebased
    if (
        kind == "mysql"
        and slots.get("pile_start") is not None
        and slots.get("pile_end") is not None
        and not (slots.get("route_match") or slots.get("section_match"))
    ):
        raise QueryPlanError("桩号区间必须同时指定路线或路段，避免跨路线累计桩号串数。")
    _infer_enum_conditions(plan)
    _canonicalize_disease_time_fields(plan)
    _physicalize_semantic_fields(plan, kind)
    return plan


def _pg_table_of(message: str) -> str:
    """PG 库只有 GPS 轨迹点 / 倾斜监测点两张表：按**表名+表注释**的词元重合选表。

    原来只认"倾角"这个词（"倾斜监测点里的接收时间"不含它，被当成 gps_points，答出一张
    不相干的表）。词项从 PG 表结构自动派生，不手写业务词表——同 field_grounding 的做法。
    两边都没命中时沿用默认的 gps_points。
    """
    from . import wren_engine

    terms = wren_engine._terms(message)  # noqa: SLF001 —— 和表级检索共用同一套分词
    best, best_score = "gps_points", 0
    for table in schema_catalog.field_catalog("postgresql").get("tables") or []:
        name = str(table.get("table") or "")
        if not name:
            continue
        haystack = f"{name} {table.get('label') or ''} {table.get('comment') or ''}"
        overlap = terms & wren_engine._terms(haystack)  # noqa: SLF001
        score = sum(len(token) ** 2 for token in overlap)
        if score > best_score:
            best, best_score = name, score
    return best


def _route_sql(plan: dict[str, Any], alias: str = "r") -> str:
    route_id = plan["slots"]["route_match"]["id"].replace("'", "''")
    return f"{alias}.id = '{route_id}'"


_DISEASE_LENGTH_EXPR = (
    "CASE WHEN p.disease_length IS NOT NULL AND p.disease_length > 0 "
    "THEN p.disease_length ELSE GREATEST(COALESCE(p.area_height, 0), "
    "COALESCE(p.area_width, 0)) END"
)
_AVG_METRICS: tuple[tuple[str, str], ...] = (("面积", "p.area"), ("长度", _DISEASE_LENGTH_EXPR))


def _unknown_type_sql(alias: str = "p") -> str:
    """"未识别类型"（字典里查不到对应类型）的 SQL 条件。

    病害类型外键是 `problem_base_id`，不是“损坏程度”字段 `damage_type`。
    外键为空或找不到字典行都算未识别。
    """
    return (
        f"NOT EXISTS (SELECT 1 FROM ai_problem_base ub "
        f"WHERE ub.id = {alias}.problem_base_id AND ub.problem_type IS NOT NULL "
        "AND (ub.del_flag = 0 OR ub.del_flag IS NULL))"
    )


def _damage_dict_join(
    disease_alias: str = "p",
    dict_alias: str = "b",
    join_word: str = "JOIN",
) -> str:
    """病害 → 类型字典的唯一合法关联；直连主键，不会按 problem_code 放大。"""
    return (
        f"{join_word} ai_problem_base {dict_alias} "
        f"ON {dict_alias}.id = {disease_alias}.problem_base_id "
        f"AND ({dict_alias}.del_flag = 0 OR {dict_alias}.del_flag IS NULL)"
    )


def _semantic_condition_values(plan: dict[str, Any], fields: set[str]) -> list[str]:
    """语义模型明确抽出的实体值；没有条件就返回空，不做文本猜测。"""
    out: list[str] = []
    for item in (plan.get("slots") or {}).get("semantic_conditions") or []:
        if str(item.get("field") or "") not in fields:
            continue
        raw = item.get("value")
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    return out


# 用户说这些词 = 要"字典里没有识别的类型"，不是某一种具体病害
_UNKNOWN_TYPE_WORDS = (
    "未识别",
    "未知类型",
    "无法识别",
    "认不出来",
    "认不出",
    "识别不了",
    "没有识别",
    "没识别",
    "识别不出",
    "无类型",
    "类型为空",
)
_TYPE_FIELDS = {"problem_type_name", "damage_type", "damage_type_name", "problem_type"}
_UNKNOWN_TYPE_VALUES = {"未识别", "未知", "未知类型", "无", "空", "null", "none", ""}


def _canonicalize_semantic_type_values(plan: dict[str, Any]) -> None:
    """把语义层给的类型值收敛到当前有效字典；完全不存在时明确报错。"""
    slots = plan.setdefault("slots", {})
    conditions = list(slots.get("semantic_conditions") or [])
    if not conditions:
        return
    known = [
        str(item.get("problem_type") or "")
        for item in _known_damage_types()
        if item.get("problem_type")
    ]
    if not known:
        return
    changed = False
    kept_conditions: list[dict[str, Any]] = []
    missing: list[str] = []
    for item in conditions:
        field = str(item.get("field") or "")
        op = str(item.get("op") or "")
        if field not in _TYPE_FIELDS or op not in {"=", "in"}:
            kept_conditions.append(item)
            continue
        raw_values = item.get("value")
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        matched: list[str] = []
        for raw in values:
            text = str(raw or "").strip()
            if not text or text.lower() in _UNKNOWN_TYPE_VALUES:
                continue
            hits = [
                name
                for name in known
                if name == text or text in name or name in text
            ]
            if hits:
                for name in hits:
                    if name not in matched:
                        matched.append(name)
            elif text not in missing:
                missing.append(text)
        changed = True
        if matched:
            kept_conditions.append(
                {
                    **item,
                    "field": "problem_type_name",
                    "op": "in",
                    "value": matched,
                }
            )
    if not changed:
        return
    if missing:
        slots["missing_damage_types"] = missing
    if not any(
        str(item.get("field") or "") == "problem_type_name"
        for item in kept_conditions
    ) and missing:
        raise QueryPlanError(
            "库里没有这些病害类型：" + "、".join(missing[:8])
        )
    slots["semantic_conditions"] = kept_conditions


def _canonicalize_unknown_type(plan: dict[str, Any], message: str) -> None:
    """把"未识别/未知类型"归一成 `slots["unknown_type"]=True`，并清掉语义层的错映射。

    三种输入都要接住：
    1) `problem_type_name is_null`（语义层抽对了）→ 直接认；
    2) `problem_type_name = '未识别'`（把伪类型当成了类型值）→ 认，并移掉那条条件；
    3) 什么都没有、或者错抽成 `problem_status = '0'`（"未识别"被当成工单状态）→
       只要用户原话里明确说了"未识别/未知类型"，就补成 unknown_type。
    """
    slots = plan.setdefault("slots", {})
    conditions = list(slots.get("semantic_conditions") or [])
    found = False
    kept: list[dict[str, Any]] = []
    for item in conditions:
        field = str(item.get("field") or "")
        op = str(item.get("op") or "")
        value = str(item.get("value") or "").strip().lower()
        if field in _TYPE_FIELDS and (
            op in {"is null", "is_null", "null"} or value in _UNKNOWN_TYPE_VALUES
        ):
            found = True
            continue  # 这条由 unknown_type 统一表达
        if field == "problem_status" and any(word in message for word in _UNKNOWN_TYPE_WORDS):
            # 用户没提工单状态，却出现状态条件 + 原话在说"未识别" → 判为语义层的错映射
            log_event(
                "nl2sql",
                "INFO",
                "unknown_type_replaces_status",
                value=str(item.get("value")),
                question=str(message)[:60],
            )
            found = True
            continue
        kept.append(item)
    if not found and any(word in str(message) for word in _UNKNOWN_TYPE_WORDS):
        # 语义层整条丢了（字典里没这个词最容易发生）→ 按用户原话补上
        found = True
        log_event(
            "nl2sql",
            "INFO",
            "unknown_type_from_message",
            question=str(message)[:60],
        )
    if not found:
        return
    if kept != conditions:
        slots["semantic_conditions"] = kept
    slots["unknown_type"] = True
    required = plan.setdefault("required_conditions", [])
    if "unknown_type" not in required:
        required.append("unknown_type")


def _metric_filters(
    plan: dict[str, Any],
    with_time: bool = True,
    with_damage: bool = True,
    with_pile: bool = True,
) -> tuple[list[str], list[str]]:
    """模板共用的 JOIN 与 WHERE 片段（路线 / 病害类型 / 时间）。"""
    slots = plan["slots"]
    joins = ["JOIN basic_facilities_routes r ON p.routes_id = r.id"]
    where = ["p.del_flag = 0", "r.del_flag = 0"]
    # 条件里用了巡检表字段（t.task_number 等）时，必须把巡检表 JOIN 上
    # （上一轮"唯一结果"是巡检任务编号时会出现这种情况）
    condition_exprs = [
        _CONDITION_FIELDS[item["field"]][0]
        for item in slots.get("semantic_conditions") or []
        if item.get("field") in _CONDITION_FIELDS
    ]
    if any(expr.startswith("t.") for expr in condition_exprs):
        joins.append("JOIN ai_maintain_patrol t ON p.patrol_id = t.id")
    if any(
        str(item.get("field") or "") == "problem_type_name"
        for item in slots.get("semantic_conditions") or []
    ) and not any("ai_problem_base" in join for join in joins):
        joins.append(_damage_dict_join())
    if slots.get("route_match"):
        where.append(_route_sql(plan))
    if with_pile and slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        where.append(
            f"p.pile_distance BETWEEN {int(slots['pile_start'])} AND {int(slots['pile_end'])}"
        )
    # 设备号在 MySQL 侧是 ai_problem.device_code（vehicle_id 是车辆 id）。
    if slots.get("device_id"):
        device = str(slots["device_id"]).replace("'", "''")
        where.append(f"p.device_code = '{device}'")
    if with_damage and slots.get("damage_matches"):
        joins.append(_damage_dict_join())
        values = ", ".join(
            "'" + item["problem_type"].replace("'", "''") + "'" for item in slots["damage_matches"]
        )
        where.append(f"b.problem_type IN ({values})")
    if with_damage and slots.get("unknown_type"):
        # "未识别类型"：字典里没有对应类型 → damage_type 为空（不是某个 type 值）
        where.append(_unknown_type_sql("p"))
    # 起点/终点各自独立判断：用户只说"从X开始"时只有起点，不能整段丢掉
    if with_time and slots.get("time_start"):
        where.append(f"p.collection_time >= '{slots['time_start']}'")
    if with_time and slots.get("time_end"):
        where.append(f"p.collection_time < '{slots['time_end']}'")
    return joins, where


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


_DAMAGE_TYPE_EXPR = "COALESCE(b.problem_type, '未知类型')"

# 维度：用户按什么分组（"哪天最多""哪条路""星期几""几点"…）
_DIMENSIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    # 周数统一用 **ISO 8601（mode=3）**：mode=1 的"第 1 周"从 1/5 起算，和用户说的"第 N 周"对不上
    # （实测用户说第 31 周是 7/27~8/2，mode=1 算成 8/3~8/9 → 说"没有数据"）
    # （docs/周数计算模式不一致与链式复合问题执行修复方案.md §1.2）。
    # 分组**标签**要给成人能读的 `2026年第31周`：以前直接用 `YEARWEEK(...)` 当分组表达式，
    # 结果列是 `202631` 这种裸数字——人看不懂，链式题的第二步拿它当条件也认不出来
    # （实测第一步答案写成"202631年第31周"、`{{peak_week}}` 回填后第二步照样查不出时间）。
    # `%x`=ISO 年、`%v`=ISO 周，和 mode=3 口径一致。
    (
        "week",
        ("哪几周", "按周", "第几周", "周维度", "每周"),
        "CONCAT(DATE_FORMAT(p.collection_time, '%x'), '年第', "
        "DATE_FORMAT(p.collection_time, '%v'), '周')",
    ),
    (
        "dow",
        ("星期几", "周几", "星期", "工作日"),
        "CASE DAYOFWEEK(p.collection_time) WHEN 1 THEN '周日' WHEN 2 THEN '周一' "
        "WHEN 3 THEN '周二' WHEN 4 THEN '周三' WHEN 5 THEN '周四' WHEN 6 THEN '周五' "
        "ELSE '周六' END",
    ),
    ("hour", ("几点", "时段", "时间段", "小时"), "HOUR(p.collection_time)"),
    ("day", ("哪天", "每天", "按日", "单日", "每日", "最近一次", "最近"), "DATE(p.collection_time)"),
    ("month", ("哪个月", "按月", "每月的", "月份"), "DATE_FORMAT(p.collection_time, '%Y-%m')"),
    ("route", ("哪条路", "各条路", "每条路", "哪条路线", "分布在哪", "按路线", "分路线"), "r.short_name"),
    (
        "damage_type",
        ("什么类型", "各类型", "按类型", "哪些类型", "什么病害", "病害类型", "哪种病害", "哪种类型"),
        _DAMAGE_TYPE_EXPR,
    ),
    # 方向：库里存的是代码（1 上行 / 2 下行），分组时必须翻中文，否则用户看到"1: 732处"
    (
        "direction",
        ("上行还是下行", "上下行", "什么方向", "方向分布", "上行和下行", "上行、下行", "各方向"),
        "CASE p.up_or_down WHEN '1' THEN '上行' WHEN '2' THEN '下行' WHEN '0' THEN '全部' "
        "WHEN '' THEN '未填' ELSE CONCAT('未知(', p.up_or_down, ')') END",
    ),
    ("device", ("哪台设备", "每台设备", "按设备"), "p.device_code"),
)

# 指标：用户要算什么
_AVG_PER_DAY_EXPR = "COUNT(*) / COUNT(DISTINCT DATE(p.collection_time))"


# 主体 → （主表别名, 关联链路）。疾病以外的对象也要能走同一套"维度×指标"构建。
_SUBJECT_SOURCES: dict[str, tuple[str, list[str]]] = {
    "disease": ("ai_problem p", ["JOIN basic_facilities_routes r ON p.routes_id = r.id"]),
    "pile_point": (
        "basic_facilities_sections_pile_detail p",
        [
            "JOIN basic_facilities_sections s ON p.sections_id = s.id AND s.del_flag = 0",
            "JOIN basic_facilities_routes r ON s.route_id = r.id",
        ],
    ),
    "section": (
        "basic_facilities_sections p",
        ["JOIN basic_facilities_routes r ON p.route_id = r.id"],
    ),
}


_NUMERIC_FIELDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("面积",), "p.area"),
    (
        ("置信度", "可信度", "信度"),
        pic_json_expr("confidence", True, "DECIMAL(10,6)"),
    ),
    # pic_position 里的标注尺寸/坐标：**必须带限定词**（标注/图片/AI/框），
    # 否则"宽度"会和表列面积宽度、"经度"会和表列 lng 打架（用户明确要求）。
    (("标注宽度", "图片宽度", "AI 宽度", "标注框宽"), pic_json_expr("width", True, "DECIMAL(10,2)")),
    (("标注高度", "图片高度", "AI 高度", "标注框高"), pic_json_expr("height", True, "DECIMAL(10,2)")),
    (("标注经度", "图片经度", "标注框经度"), pic_json_expr("lng", True, "DECIMAL(12,8)")),
    (("标注纬度", "图片纬度", "标注框纬度"), pic_json_expr("lat", True, "DECIMAL(12,8)")),
    (("长度",), _DISEASE_LENGTH_EXPR),
    (("桩号", "里程"), "p.pile_distance"),
)

_NUMERIC_OPS: tuple[tuple[str, str], ...] = (
    ("大于等于", ">="),
    ("不小于", ">="),
    ("不超过", "<="),
    ("不高于", "<="),
    ("等于", "="),
    # 「面积正好 7.52㎡」这种说法实测最多（长对话 R3/R4），"正好/恰好/刚好"都是等值条件
    ("正好", "="),
    ("恰好", "="),
    ("刚好", "="),
    ("为", "="),
    ("超过", ">"),
    ("大于", ">"),
    ("小于", "<"),
    ("低于", "<"),
    ("高于", ">"),
    (">=", ">="),
    ("<=", "<="),
    ("≥", ">="),
    ("≤", "<="),
    (">", ">"),
    ("<", "<"),
    ("=", "="),
)


def numeric_conditions(message: str) -> list[str]:
    """把「面积等于 5000 平方米」「置信度≥0.7」这类数值条件翻译成 SQL 片段。

    以前这类条件只能弃权交给模型，导致同一题两个跑法结果不同（D1/D2/D3/M5 都栽过）。
    """
    field_words = [word for words, _ in _NUMERIC_FIELDS for word in words]
    field_expr = {word: expression for words, expression in _NUMERIC_FIELDS for word in words}
    # 长的操作符先匹配（">=" 不能先被 ">" 吃掉）
    op_alternation = "|".join(re.escape(op) for op, _ in _NUMERIC_OPS)
    op_map = dict(_NUMERIC_OPS)
    field_alternation = "|".join(re.escape(word) for word in sorted(field_words, key=len, reverse=True))
    pattern = re.compile(
        rf"({field_alternation})?\s*({op_alternation})\s*(\d+(?:\.\d+)?)"
    )
    found: list[tuple[str, str]] = []
    last_field: str | None = None
    for match in pattern.finditer(message):
        if match.group(1):
            last_field = match.group(1)
        if not last_field:
            continue  # 只写了 ">5㎡" 却没说是哪个字段，不猜
        expression = field_expr[last_field]
        clause = f"{expression} {op_map.get(match.group(2), '=')} {match.group(3)}"
        if (expression, clause) not in [(expr, text) for expr, text in found]:
            found.append((expression, clause))
    # 后置写法：「置信度 0.95 以上」「面积 10 平方米以下」
    postfix = re.compile(
        rf"({field_alternation})?\s*(\d+(?:\.\d+)?)\s*(以上|以下|以内|之上|之下)"
    )
    for match in postfix.finditer(message):
        if match.group(1):
            last_field = match.group(1)
        if not last_field:
            continue
        expression = field_expr[last_field]
        op = ">=" if match.group(3) in {"以上", "之内", "之上"} else "<="
        clause = f"{expression} {op} {match.group(2)}"
        if (expression, clause) not in [(expr, text) for expr, text in found]:
            found.append((expression, clause))
    # 同一个字段出现多个阈值（「≥0.7 有几条、<0.5 有几条」）时是两个口径，
    # 不是一条 WHERE，交给 LLM 处理多口径。
    # 括号里给值：「面积大于西乡段泛油平均面积（1.0670㎡）」——
    # 操作符后面跟的是一段文字、真正的数值在括号里（F7 就是这种，以前识别不到 → 交给 LLM 反而答 0）。
    bracketed = re.compile(
        rf"({field_alternation})?\s*({op_alternation})[^（(）)]{{0,24}}[（(]\s*(\d+(?:\.\d+)?)"
    )
    for match in bracketed.finditer(message):
        if match.group(1):
            last_field = match.group(1)
        if not last_field:
            continue
        expression = field_expr[last_field]
        clause = f"{expression} {op_map.get(match.group(2), '=')} {match.group(3)}"
        if (expression, clause) not in [(expr, text) for expr, text in found]:
            found.append((expression, clause))
    expressions = [expr for expr, _ in found]
    if len(expressions) != len(set(expressions)):
        return []
    return [clause for _, clause in found]


# 17 张核心业务表：中文关键词 → 表名 / 关联路线的列 / 分组列
_TABLE_SUBJECTS: tuple[dict[str, Any], ...] = (
    {
        "label": "病害类型字典",
        "words": ("病害大类", "病害分类", "病害字典"),
        "extra_words": ("病害类型",),
        "require": ("大类", "分类", "几类", "哪些类型", "多少种", "种类"),
        "table": "ai_problem_base",
        "route_col": None,
        "group_col": "disease_categories_type",
        # 大类名称从列注释读（字典变了自动跟着变）
        "group_labels_fn": dictionary.problem_category_labels,
        # 字典表里同一 problem_code 有两行，必须按 code 去重（42 类不是 84 行）
        "count_expr": "COUNT(DISTINCT t.problem_code)",
        "group_count_expr": "COUNT(DISTINCT t.problem_code)",
    },
    {
        "label": "资产类型字典",
        "words": ("资产类型", "资产大类", "资产分类"),
        "require": ("大类", "分类", "几类", "哪些", "多少种", "种类", "分几"),
        "table": "ai_asset_base",
        "route_col": None,
        "group_col": "categories_type",
        "group_labels": {},
        "note": "字段注释写的是「0 条状 / 1 点状」，但实际值是 10/20/30/40/60/70/80 七组，注释与数据不一致，具体大类名称需查平台字典",
    },
    {"label": "巡查资产", "words": ("巡查资产", "资产信息", "资产实例"), "table": "ai_asset_main", "route_col": "route_id"},
    {"label": "定距采集", "words": ("定距",), "table": "ai_distance_capture", "route_col": "routes_id"},
    {"label": "桥梁", "words": ("桥梁", "桥"), "table": "basic_facilities_bridges", "route_col": "routes_id"},
    {"label": "隧道", "words": ("隧道",), "table": "basic_facilities_tunnels", "route_col": "belong_route_id"},
    {"label": "边坡", "words": ("边坡",), "table": "basic_facilities_slope", "route_col": "routes_id"},
    {"label": "AI巡检工单", "words": ("工单",), "table": "ai_work_order", "route_col": None},
    {"label": "养护工单", "words": ("养护工单",), "table": "maintenance_work_order", "route_col": None},
    {"label": "养护项目", "words": ("养护项目",), "table": "maintenance_project", "route_col": "route_id"},
    {"label": "道路作业记录", "words": ("路面作业", "作业记录"), "table": "maintenance_road_work_record", "route_col": "route_id"},
    {"label": "工程项目", "words": ("工程项目", "工程管理"), "table": "project_manage_project_main", "route_col": None},
)

# 主体 → 表：LLM 语义给的 subject 直接决定"数哪张表"，词表只做兜底
# （用户要求：不能纯靠词表判"问的是哪张表"）。
_SUBJECT_TABLE_BY_NAME = {
    "route": "basic_facilities_routes",
    "section": "basic_facilities_sections",
    "pile_point": "basic_facilities_sections_pile_detail",
    "asset": "ai_asset_main",
    "work_order": "ai_work_order",
    "patrol": "ai_maintain_patrol",
}
# "问的是字典本身"（分几类/多少种）→ 身体对应的字典表
_DICTIONARY_TABLE_BY_SUBJECT = {
    ("disease", "damage_type"): "ai_problem_base",
    ("disease", "category"): "ai_problem_base",
    ("asset", "category"): "ai_asset_base",
}


def _table_subject_entry(table: str) -> dict[str, Any]:
    """按表名拿一条 subject 记录；词表里没有的表（路线/巡查这类）现造一条。"""
    known = next((item for item in _TABLE_SUBJECTS if str(item.get("table")) == table), None)
    if known:
        return known
    return {
        "label": schema_catalog.table_label("mysql", table),
        "words": (),
        "table": table,
        "route_col": None,
    }


def _semantic_table_subjects(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM 语义（subject + 分组维度）决定要数哪张表；判不出来返回空（交词表兜底）。"""
    if not _semantic_decided(plan):
        return []
    semantic = plan.get("semantic") or {}
    subject = str(plan.get("subject") or "").lower()
    group_by = str(semantic.get("group_by") or (plan.get("slots") or {}).get("group_by") or "")
    table = _DICTIONARY_TABLE_BY_SUBJECT.get((subject, group_by)) or _SUBJECT_TABLE_BY_NAME.get(subject)
    return [_table_subject_entry(table)] if table else []


def _build_table_count_template(plan: dict[str, Any]) -> str | None:
    """按"业务对象"直接数表：桥梁/隧道/边坡/工单/养护项目/工程项目/字典表…

    空表也如实回答（0 条就是 0 条），字典表给按大类的分布。
    """
    message = plan.get("message", "")
    semantic_metrics = {
        str(item)
        for item in ((plan.get("semantic") or {}).get("metrics") or [])
        if item
    }
    asks_length = any(
        word in message for word in ("长度", "里程", "多长", "几公里", "多少公里")
    ) or any("length" in item for item in semantic_metrics)
    if asks_length and (
        str(plan.get("subject") or "") in {"route", "section"}
        or (plan.get("slots") or {}).get("route_match")
        or (plan.get("slots") or {}).get("section_match")
    ):
        return None
    # 语义优先：LLM 说了问的是哪类对象（subject，注册表也只在这些 subject 上放行），
    # 就直接选表；语义缺失（LLM 没跑/失败）才退回下面的词表兜底。
    hits: list[dict[str, Any]] = _semantic_table_subjects(plan)
    if hits:
        log_event(
            "nl2sql",
            "INFO",
            "table_count_subject_from_semantics",
            subject=str(plan.get("subject")),
            table=str(hits[0].get("table")),
        )
    if not hits:
        for subject in _TABLE_SUBJECTS:
            if not any(word in message for word in subject["words"]):
                # 「病害类型」这类通用词，要配合"分几大类/有哪些类型"才算在问字典
                if not (
                    any(word in message for word in subject.get("extra_words", ()))
                    and any(word in message for word in subject.get("require", ()))
                ):
                    continue
            # 「病害类型」「资产类型」这类词要先确认问的是字典本身（分几大类/有哪些类型），
            # 否则"按病害类型统计"也会被当成查字典。
            if subject.get("require") and not any(word in message for word in subject["require"]):
                continue
            if subject not in hits:
                hits.append(subject)
    if not hits:
        return None
    # 这些是"按对象数表"的问题，跟病害类型无关：清掉误命中的 damaged 条件
    # （"桥梁"里的"桥"会被病害字典的模糊匹配当成病害类型）。
    plan["slots"].pop("damage_matches", None)
    plan["slots"].pop("damage_type", None)
    plan["required_conditions"] = [
        item for item in plan.get("required_conditions", []) if item != "damage_type"
    ]
    # 「桥」和「桥梁」会命中同一条；「养护工单」也已经含在「工单」里，去重按表名。
    unique: list[dict[str, Any]] = []
    seen_tables: set[str] = set()
    for subject in hits:
        if subject["table"] in seen_tables:
            continue
        seen_tables.add(subject["table"])
        unique.append(subject)
    # 「巡查资产信息有多少」这类问题，顺带把资产类型字典（88 种）一起报，
    # 否则用户会分不清"资产类型不存在"和"资产实例还没采集"。
    if any(subject["label"] == "巡查资产" for subject in unique) and "资产" in message:
        for subject in _TABLE_SUBJECTS:
            if subject["label"] == "资产类型字典" and subject["table"] not in seen_tables:
                seen_tables.add(subject["table"])
                unique.append({**subject, "words": ()})
                break

    route = (plan["slots"].get("route_match") or {}).get("id")
    pieces: list[str] = []
    groups_used: dict[str, dict[str, str]] = {}
    for subject in unique:
        table = subject["table"]
        # 路线条件落在哪一列，按字段目录的关系链解析（和通用算子同一份关系来源）；
        # 声明里的 route_col 只当兜底——写死列名正是"路线条件绑错列"的同源问题
        # （docs/通用算子路线条件误绑主键修复方案.md P0-1）。
        path = schema_catalog.relation_path("mysql", table, target_table=_ROUTE_TABLE)
        route_col = path[0] if path else subject.get("route_col")
        if route and not route_col:
            # 这张表既没有路线列、也（按字段目录）接不到路线：带上路线条件就答不对，
            # 丢掉条件又会答成全库（"西乡段有多少个工单"→全库工单数）→ 整条弃权交 Wren。
            continue
        where = ["t.del_flag = 0"]
        joins = ""
        if route and route_col:
            source = "t"
            for index, step in enumerate(path[1] if path else []):
                joins += (
                    f" JOIN {step['table']} r{index}"
                    f" ON {source}.{step['on']} = r{index}.{step['to']}"
                )
                source = f"r{index}"
            joins += f" JOIN {_ROUTE_TABLE} r ON {source}.{route_col} = r.id"
            where.append("r.del_flag = 0")
            where.append(f"r.id = '{route}'")
        group_col = subject.get("group_col")
        semantic_group = str(
            (plan.get("semantic") or {}).get("group_by")
            or (plan.get("slots") or {}).get("group_by")
            or ""
        )
        # 语义优先：LLM 说了按 category/damage_type 分组，就直接按大类出分布；
        # 语义缺失才看"大类/分类/哪几类"这些措辞。
        wants_group = bool(group_col) and semantic_group in {"category", "damage_type"}
        if wants_group:
            labels = subject.get("group_labels") or {}
            if subject.get("group_labels_fn"):
                labels = subject["group_labels_fn"]()
            if labels:
                case = " ".join(
                    f"WHEN '{key}' THEN '{value}'" for key, value in labels.items()
                )
                expr = f"CASE t.{group_col} {case} ELSE CONCAT('其他(', t.{group_col}, ')') END"
            else:
                expr = f"t.{group_col}"
            groups_used[subject["label"]] = labels
            pieces.append(
                f"SELECT '{subject['label']}' AS dimension, {expr} AS type, "
                f"{subject.get('group_count_expr', 'COUNT(*)')} AS count "
                f"FROM {table} t {joins} WHERE {' AND '.join(where)} "
                f"GROUP BY {expr}"
            )
        else:
            pieces.append(
                f"SELECT '{subject['label']}' AS type, "
                f"{subject.get('count_expr', 'COUNT(*)')} AS count "
                f"FROM {table} t {joins} WHERE {' AND '.join(where)}"
            )

    plan["slots"]["table_notes"] = [
        subject["note"] for subject in unique if subject.get("note")
    ]
    plan["slots"]["metric_labels"] = {"type": "对象", "count": "记录数"}
    if not pieces:
        return None
    if len(pieces) == 1:
        return pieces[0] + " LIMIT 50"
    return " UNION ALL ".join(pieces) + " LIMIT 50"


# 路面类型 / 技术等级的中文口径（sections 表存的是代码，必须翻译）
def _surface_type_labels() -> dict[str, str]:
    """路面类型 code→名称：从 sections 表的列注释读（字典变了自动跟着变）。"""
    return dictionary.surface_type_labels()


def _tech_grade_labels() -> dict[str, str]:
    return dictionary.tech_grade_labels()


# 台账起止桩号 → 米 → 公里。**不能用 r.distance**。
_PILE_METERS_EXPR = (
    "(CAST(SUBSTRING_INDEX(SUBSTRING_INDEX({column}, '+', 1), 'K', -1) AS UNSIGNED) * 1000 "
    "+ CAST(SUBSTRING_INDEX({column}, '+', -1) AS UNSIGNED))"
)
_ROUTE_SPAN_LENGTH_KM = (
    "ROUND(("
    + _PILE_METERS_EXPR.format(column="r.end_num")
    + " - "
    + _PILE_METERS_EXPR.format(column="r.start_num")
    + ") / 1000, 2)"
)
_ROUTE_LENGTH_KM = (
    "ROUND(COALESCE("
    "NULLIF(" + _ROUTE_SPAN_LENGTH_KM + ", 0), "
    "NULLIF((SELECT SUM(s.end_meter_total - s.start_meter_total) / 1000.0 "
    "FROM basic_facilities_sections s "
    "WHERE s.route_id = r.id AND s.del_flag = 0), 0), "
    "0), 4)"
)
_SECTION_SPAN_LENGTH_KM = (
    "ROUND(COALESCE("
    "NULLIF(("
    + _PILE_METERS_EXPR.format(column="s.end_num")
    + " - "
    + _PILE_METERS_EXPR.format(column="s.start_num")
    + ") / 1000.0, 0), "
    "NULLIF((s.end_meter_total - s.start_meter_total) / 1000.0, 0), "
    "0), 4)"
)


def _case_expr(column: str, labels: dict[str, str]) -> str:
    return dictionary.case_expression(column, labels)


def _build_route_info_template(plan: dict[str, Any]) -> str | None:
    """路线/路段台账信息：起终点、全长、车道、路面类型、技术等级（+ 数据覆盖范围）。"""
    message = plan.get("message", "")
    slots = plan["slots"]
    section = slots.get("section_match") or {}
    if section.get("id") and (
        "section_length_km" in set((plan.get("semantic") or {}).get("output_fields") or [])
        or any(word in message for word in ("长度", "里程", "多长", "几公里", "多少公里"))
    ):
        section_id = str(section["id"]).replace("'", "''")
        if "米" in message and "公里" not in message:
            length_sql = f"ROUND(({_SECTION_SPAN_LENGTH_KM}) * 1000, 0) AS distance_m"
        else:
            length_sql = f"{_SECTION_SPAN_LENGTH_KM} AS distance_km"
        plan["slots"]["metric_labels"] = {
            "type": "路段",
            "distance_km": "长度(km)",
            "distance_m": "长度(米)",
        }
        return (
            f"SELECT s.full_name AS type, s.start_num AS ledger_start, "
            f"s.end_num AS ledger_end, {length_sql} "
            "FROM basic_facilities_sections s "
            f"WHERE s.del_flag = 0 AND s.id = '{section_id}' LIMIT 1"
        )
    route = slots.get("route_match") or {}
    route_list = [item for item in (slots.get("routes") or []) if item.get("id")]
    # 两条路的台账问题（"A 和 B 的长度分别是多少"）没有 route_match，只有 routes
    if not route.get("id") and not route_list:
        return None
    # 是否属于台账属性题只由语义层决定；语义缺失时不在代码里猜说法。
    if not _semantic_decided(plan):
        return None
    # 这是台账查询（走 sections.route_id），跟"病害表 routes_id"无关，别被病害侧校验拦下。
    plan["subject"] = "section"
    route_id = str(route.get("id") or (route_list[0]["id"] if route_list else "")).replace("'", "''")
    # 两条以上路线（"A 和 B 的长度分别是多少"）→ 一次查出来，各占一行
    routes = route_list
    if len(routes) >= 2:
        ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in routes)
        where_routes = f"r.id IN ({ids})"
        tail = "GROUP BY r.id, r.short_name ORDER BY r.short_name"
    else:
        where_routes = f"r.id = '{route_id}'"
        tail = "GROUP BY r.id, r.short_name LIMIT 1"
    # 问的是"多少米"就给米，别给公里（子问题"路线总长度是多少米"后面要拿去做除法）
    if "米" in message and "公里" not in message:
        length_sql = f"ROUND(({_ROUTE_LENGTH_KM}) * 1000, 0) AS distance_m"
    else:
        length_sql = f"{_ROUTE_LENGTH_KM} AS distance_km"
    output_fields = {
        str(item) for item in ((plan.get("semantic") or {}).get("output_fields") or [])
    }
    if output_fields and output_fields <= {"route_length_km"}:
        plan["slots"]["metric_labels"] = {
            "type": "路线",
            "distance_km": "长度(km)",
            "distance_m": "长度(米)",
        }
        return (
            f"SELECT r.short_name AS type, r.start_num AS ledger_start, "
            f"r.end_num AS ledger_end, {length_sql} "
            "FROM basic_facilities_routes r "
            f"WHERE r.del_flag = 0 AND {where_routes} {tail}"
        )
    sql = (
        "SELECT r.short_name AS type, r.start_num AS ledger_start, r.end_num AS ledger_end, "
        f"{length_sql}, MAX(s.lane_count) AS lane_count, "
        f"{_case_expr('MAX(s.surface_type)', _surface_type_labels())} AS surface_type_name, "
        f"{_case_expr('MAX(s.tech_grade)', _tech_grade_labels())} AS tech_grade_name, "
        "COUNT(DISTINCT s.id) AS section_count, MIN(p.pile_distance) AS data_min_pile, "
        "MAX(p.pile_distance) AS data_max_pile, COUNT(DISTINCT p.id) AS data_count "
        "FROM basic_facilities_routes r "
        "LEFT JOIN basic_facilities_sections s ON s.route_id = r.id AND s.del_flag = 0 "
        "LEFT JOIN ai_problem p ON p.sections_id = s.id AND p.del_flag = 0 "
        f"WHERE r.del_flag = 0 AND {where_routes} {tail}"
    )
    plan["slots"]["metric_labels"] = {
        "type": "路线",
        "ledger_start": "台账起点",
        "ledger_end": "台账终点",
        "distance_km": "全长(km)",
        "lane_count": "车道数",
        "surface_type_name": "路面类型",
        "tech_grade_name": "技术等级",
        "section_count": "路段数",
        "data_min_pile": "数据起点(m)",
        "data_max_pile": "数据终点(m)",
        "data_count": "数据条数",
    }
    plan["slots"]["pile_metrics"] = ["data_min_pile", "data_max_pile"]
    return sql


def _build_coverage_compare_template(plan: dict[str, Any]) -> str | None:
    """两条路谁的病害数据覆盖更远：各自 min/max 桩号 + 条数。"""
    message = plan.get("message", "")
    routes = plan["slots"].get("routes") or []
    if len(routes) < 2 or not _semantic_decided(plan):
        return None
    plan["subject"] = "route"
    pieces = []
    for item in routes:
        route_id = str(item["id"]).replace("'", "''")
        name = str(item.get("name") or route_id).replace("'", "''")
        pieces.append(
            f"SELECT '{name}' AS type, MIN(p.pile_distance) AS min_pile, "
            "MAX(p.pile_distance) AS max_pile, COUNT(*) AS count "
            "FROM ai_problem p JOIN basic_facilities_routes r ON p.routes_id = r.id "
            f"WHERE p.del_flag = 0 AND r.del_flag = 0 AND r.id = '{route_id}'"
        )
    plan["slots"]["metric_labels"] = {
        "type": "路线",
        "min_pile": "数据起点(m)",
        "max_pile": "数据终点(m)",
        "count": "病害数",
    }
    plan["slots"]["pile_metrics"] = ["min_pile", "max_pile"]
    return " UNION ALL ".join(pieces) + " LIMIT 10"


def _build_threshold_count_template(plan: dict[str, Any]) -> str | None:
    """单一主体 + 数值阈值的「有多少条」：直接进 WHERE，不交给 LLM。

    F7「恒明珠的泛油中，面积大于西乡段泛油平均面积（1.0670㎡）的有多少条？」就是这种：
    数值写在括号里，以前 numeric_conditions 认不出来 → 交给 LLM → 时对时错（实测答过 0）。
    只处理纯计数题；带"分布/各/分别"的交给对应的分组模板。
    """
    message = plan.get("message", "")
    if plan["shape"] != "count" or plan.get("subject", "disease") != "disease":
        return None
    # 带分组/拆分诉求的不能在这里答（"分路线呢"会丢掉分组，只剩总数）
    if any(
        word in message
        for word in ("分布", "各多少", "分别", "占", "比例", "分路线", "按路线", "每条路", "拆开")
    ):
        return None
    thresholds = condition_clauses(plan)
    if not thresholds:
        return None
    joins, where = _metric_filters(plan)
    # 「A 的泛油中，面积大于 B 的……”：A 是主体、B 只是阈值参照。
    # 这种题 plan 里没有 route_match（两条路都在 routes 里），按"谁先在问题里出现"定主体。
    if not plan["slots"].get("route_match"):
        routes = [item for item in (plan["slots"].get("routes") or []) if item.get("id")]
        if routes:
            def position(item: dict[str, Any]) -> int:
                name = str(item.get("name") or "")
                # 用户通常写简称（"恒明珠"/"西乡段"），要对路线名做片段匹配
                pieces = _route_pieces(message, name) or ([name] if name in message else [])
                found = [message.find(piece) for piece in pieces if piece and piece in message]
                return min(found) if found else 10**6

            subject = min(routes, key=position)
            route_id = str(subject["id"]).replace("'", "''")
            where.append(f"r.id = '{route_id}'")
    where.extend(thresholds)
    declared = {
        str(item) for item in ((plan.get("semantic") or {}).get("metrics") or []) if item
    }
    selects = ["COUNT(*) AS count"]
    labels = {"count": "处数"}
    if declared & {
        "sum_effective_disease_length",
        "total_effective_disease_length",
    }:
        selects.append(f"ROUND(SUM({_DISEASE_LENGTH_EXPR}), 2) AS total_length_m")
        labels["total_length_m"] = "总长度(米)"
    plan["slots"]["metric_labels"] = labels
    return (
        f"SELECT {', '.join(selects)} FROM ai_problem p {' '.join(joins)} "
        f"WHERE {' AND '.join(where)} LIMIT 1"
    )


def _build_per_km_template(plan: dict[str, Any]) -> str | None:
    """「平均每公里有多少处病害」= 病害数 ÷ 路线长度(km)。

    长度优先按所属路段起止米数求和（distance 字段不可信）。实测这条问题以前会交给 LLM，
    答出 162678.57 这种明显离谱的数（它把桩号米数当分母了）。
    """
    message = plan.get("message", "")
    if not _semantic_decided(plan):
        return None
    if plan.get("subject", "disease") != "disease":
        return None
    route = plan["slots"].get("route_match") or {}
    if not route.get("id"):
        return None
    joins, where = _metric_filters(plan)
    plan["slots"]["metric_labels"] = {
        "per_km": "每公里处数",
        "count": "处数",
        "length_km": "全长(km)",
    }
    return (
        f"SELECT ROUND(COUNT(*) / NULLIF({_ROUTE_LENGTH_KM}, 0), 2) AS per_km, "
        f"COUNT(*) AS count, {_ROUTE_LENGTH_KM} AS length_km "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)} LIMIT 1"
    )


_PROBLEM_TYPES_CACHE: tuple[float, list[str]] | None = None


def known_problem_types(kind: str = "mysql") -> list[str]:
    """库里**真实存在**的病害类型名：字典表 ∪ pic_position JSON 的 `$.type`（带缓存）。

    为什么两边都要：字典里 936 条"泛油"，JSON 里 574 条"纵向裂缝"——单看一边都会漏。
    """
    global _PROBLEM_TYPES_CACHE
    now = time.time()
    ttl = float(getattr(settings, "reference_data_ttl_seconds", 300) or 300)
    if _PROBLEM_TYPES_CACHE and now - _PROBLEM_TYPES_CACHE[0] < ttl:
        return _PROBLEM_TYPES_CACHE[1]
    names: list[str] = []
    try:
        for row in db_manager.query(
            kind,
            "SELECT DISTINCT problem_type AS v FROM ai_problem_base "
            "WHERE problem_type IS NOT NULL AND problem_type <> '' "
            "AND (del_flag = 0 OR del_flag IS NULL)",
        )["rows"]:
            value = str(row.get("v") or "").strip()
            if value:
                names.append(value)
        for row in db_manager.query(
            kind,
            "SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(pic_position, '$.type')) AS v "
            "FROM ai_problem WHERE del_flag = 0 AND pic_position IS NOT NULL "
            "AND JSON_UNQUOTE(JSON_EXTRACT(pic_position, '$.type')) IS NOT NULL",
        )["rows"]:
            value = str(row.get("v") or "").strip()
            if value and value not in names:
                names.append(value)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "known_problem_types_failed", exc)
        return _PROBLEM_TYPES_CACHE[1] if _PROBLEM_TYPES_CACHE else []
    _PROBLEM_TYPES_CACHE = (now, names)
    return names


def expand_problem_types(names: list[str], kind: str = "mysql") -> list[str]:
    """把用户说的类型名对齐到库里**真实存在**的类型名。

    两个作用：
      · "裂缝" 这种**上位的口语名** → 展开成所有包含它的真实类型（横向裂缝、纵向裂缝…），
        否则 `IN ('裂缝')` 永远 0 行（实测）；
      · 库里没有的名字原样保留（上层会如实回答"没有这个类型"）。
    """
    known = known_problem_types(kind)
    if not known:
        return [str(item) for item in names if str(item or "").strip()]
    out: list[str] = []
    for name in names:
        text = str(name or "").strip()
        if not text:
            continue
        hits = [value for value in known if value == text or text in value or value in text]
        for value in hits or [text]:
            if value not in out:
                out.append(value)
    return out


def _names_other_image_column(message: str) -> bool:
    """句子里点名的图片列是不是**别的表**上的（不是 ai_problem.problem_url）？

    "病害样图"（ai_problem_base.problem_sample_image）、"资产图片"、"路段封面"、"作业图片"…
    这些列的中文名都自带限定词，不会和"图片/照片"这种泛称混淆；命中就说明用户要的是
    那张表自己的图片列，不该被病害现场图口径抢走。列名单来自字段目录（role=image），不写死。
    """
    text = str(message or "")
    if not text:
        return False
    from . import schema_catalog as catalog

    for entry in catalog.field_catalog("mysql").get("tables") or []:
        table = str(entry.get("table") or "")
        if table == "ai_problem":  # 这张表就是病害现场图口径取的那张，不算"别的表"
            continue
        for column in catalog.image_columns("mysql", table):
            label = str(column.get("label") or "").strip()
            if len(label) >= 2 and label in text:
                return True
    return False


def _build_photo_template(plan: dict[str, Any]) -> str | None:
    """看图诉求（shape=photo）：**固定取 p.problem_url**，别让 LLM 自己挑图片列。

    为什么必须有这条模板：病害表里"图片"有两列——`problem_url`（原始图片，911 条有值）和
    `preview_url`（后台缓存画框图，实测大多是 'NULL'）。以前唯一会取 problem_url 的构造器
    过去住在旧的规则模板链里，看图题实际走 Wren 由 LLM 写 SQL，
    于是它偶尔挑成 preview_url、或者给列起中文别名，`_image_result` 一行路径都读不到，
    回"没有找到相关病害图片"（docs/图片查询ChooseCapability误报暂不可用修复.md §五.4）。
    """
    message_text = str(plan.get("message") or "")
    # 图片诉求只由语义计划决定，代码不再从措辞猜。
    if str(plan.get("shape") or "") != "photo":
        return None
    # 句子里**直接点名了别的表的图片列**（"病害样图"→ai_problem_base.problem_sample_image、
    # "路段封面"→sections.picture_cover、"资产图片"→ai_asset_main.picture…）时让位：
    # 这条口径只会取 ai_problem.problem_url，抢过来就是张冠李戴——实测
    # "看一下交安事件的病害样图"回了 3 张病害现场图，用户要的是样图。
    # 这些列的中文名都自带限定词，不会和"图片"这种泛称混淆，所以按名字判足够稳。
    if _names_other_image_column(message_text):
        return None
    if str(plan.get("subject") or "disease") != "disease":
        return None
    # 这条口径取的是**病害表**的原始图片列（ai_problem.problem_url）。问的是别的表上的图片列
    # 时不能拿它顶：实测"把所有的病害样图都取出来"的落点是 ai_problem_base、
    # 要的是 problem_sample_image（84 条样图），用这条口径会答成 ai_problem 的现场照片——张冠李戴。
    # 落点表不是病害表就弃权，交给通用算子按那张表自己的图片列取（`capabilities` role=image）。
    from . import field_grounding

    landing = field_grounding.subject_table(plan)
    # 落点是被"类型/字典"语境带偏的字典表时不算数：图片（problem_url）只在病害表上，
    # 主体是 disease 说明问的就是病害的图（实测"找一下类型为纵向裂缝的图片"落点被判成
    # ai_problem_base → 这条口径被跳过 → 574 条一张都出不来）。
    if landing and landing != "ai_problem" and not (
        landing in {"ai_problem_base"} and str(plan.get("subject") or "") == "disease"
    ):
        return None
    slots = plan.get("slots") or {}
    joins: list[str] = []
    where = ["p.del_flag = 0"]
    damage = slots.get("damage_matches") or []
    # 类型过滤只使用语义模型已经给出的实体。
    type_names = [str(item.get("problem_type")) for item in damage if item.get("problem_type")]
    type_values = expand_problem_types(type_names)
    # 类型过滤口径（方案 §4.1 + 数据现实）：图片题里的"XX病害"可能来自 **AI 标注**
    # （JSON pic_type）也可能来自台账字典（damage_type），两条路 **OR**，谁有数据就用谁。
    # 实测库里 damage_type 只有 10/NULL/5："横向裂缝"只在 JSON（61 行）、"泛油"只在字典
    # （936 行），单走一路都会 0 行——这就是"调出横向裂缝的图片"查空的根因。
    if type_values:
        values = ", ".join(
            "'" + str(name).replace("'", "''") + "'" for name in type_values
        )
        joins.append(_damage_dict_join(join_word="LEFT JOIN"))
        where.append(
            # JSON 侧内联非空过滤（方案 §2.4）：pic_position 为空时 JSON_EXTRACT 得到 NULL，
            # `NULL IN (...)` 是 NULL（不是 false），语义上容易踩空；字典侧照旧不受影响。
            f"((p.pic_position IS NOT NULL AND p.pic_position <> ''"
            f" AND JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) IN ({values}))"
            f" OR b.problem_type IN ({values}))"
        )
    else:
        joins.append(_damage_dict_join(join_word="LEFT JOIN"))
    if slots.get("route_match"):
        joins.append("JOIN basic_facilities_routes r ON p.routes_id = r.id")
        where.append("r.del_flag = 0")
        where.append(_route_sql(plan))
    if slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        where.append(
            f"p.pile_distance BETWEEN {int(slots['pile_start'])} AND {int(slots['pile_end'])}"
        )
    if slots.get("time_start"):
        where.append(f"p.collection_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"p.collection_time < '{slots['time_end']}'")
    if slots.get("device_id"):
        device = str(slots["device_id"]).replace("'", "''")
        where.append(f"p.device_code = '{device}'")
    for clause in condition_clauses(plan):
        # 类型过滤已由上面的 字典/JSON OR 表达：去掉语义层重复给的 pic_type 条件，
        # 避免 AND 后把结果清空（实测"泛油图片"语义给 pic_type='泛油'，JSON 里没有泛油 → 0 行）。
        if type_values and "JSON_EXTRACT(p.pic_position, '$.type')" in clause:
            continue
        # JSON 字段条件自动补非空过滤（方案 §2.4）：pic_position 为空时 JSON_EXTRACT 返回
        # NULL，`NULL > 2` / `NULL IN (...)` 都是 NULL，比较结果不符合预期。
        if "JSON_EXTRACT(p.pic_position" in clause:
            clause = f"(p.pic_position IS NOT NULL AND p.pic_position <> '' AND {clause})"
        where.append(clause)
    # 只看真有图片路径的行：problem_url 空的行取回去也渲染不出来
    where.append("p.problem_url IS NOT NULL")
    where.append("p.problem_url <> ''")
    where.append("p.problem_url <> 'NULL'")
    plan["slots"]["metric_labels"] = {
        "problem_url": "病害图片",
        "pile_number": "桩号",
        "collection_time": "采集时间",
    }
    # 列名保持英文 problem_url：图片查询按这个列名直接取路径（中文别名只当兜底）
    # 类型列：命中哪个来源就显示哪个来源的类型（JSON 标注 / 台账字典）
    type_column = "b.problem_type"
    if type_values:
        type_column = (
            f"CASE WHEN JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) IN ({values})"
            f" THEN JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) ELSE b.problem_type END"
        )
    columns = (
        f"p.problem_number, p.pile_number, {type_column} AS 病害类型, "
        "p.collection_time, p.problem_url"
    )
    return (
        f"SELECT {columns} FROM ai_problem p {' '.join(joins)} "
        f"WHERE {' AND '.join(where)} ORDER BY p.collection_time DESC "
        f"LIMIT {settings.sql_max_rows}"
    )


def build_photo_sql(message: str, kind: str = "mysql") -> str:
    """看图诉求的**固定口径** SQL（不经过 LLM）。图片查询取不到路径时用它重取一次。

    返回空串表示这句话不适合这条口径（主体不是病害等），调用方保持原样。
    """
    try:
        plan = build_query_plan(message, kind)
    except Exception:  # noqa: BLE001
        return ""
    if not plan:
        return ""
    return _build_photo_template({**plan, "shape": "photo"}) or ""


def build_image_catalog_sql(kind: str = "mysql") -> str:
    """**全库图片清单**口径：目录里所有 role=image 的列各取一次去重非空路径，一条 UNION 查回来。

    为什么需要它：照片口径（`build_photo_sql`）固定查病害表，于是"把能调出来的图片全部调出来"
    永远只出病害现场图——实测库里 1702 条病害记录只有 3 个图片文件真的存在，而本地图片缓存里
    有 47 张（42 张病害样图 + 2 张路段封面 + 3 张病害现场图）；用户问"全部"却只看到 3 张，
    自然会觉得图片题坏了。

    列名单来自字段目录的 role=image，不写死：以后新表加了图片列自动纳入。
    返回空串 = 目录读不出来（调用方保持原样，别静默回空）。
    """
    if kind != "mysql":
        return ""
    from . import schema_catalog as catalog

    branches: list[str] = []
    for entry in catalog.field_catalog(kind).get("tables") or []:
        table = str(entry.get("table") or "")
        if not table:
            continue
        # 软删除过滤：execute_sql 有硬校验（业务表必须带 del_flag=0），目录里 role=soft_delete
        # 的列就是这道闸要用的列；表里没有这一列就别加。
        soft = next(
            (
                str(col.get("name"))
                for col in entry.get("columns") or []
                if "soft_delete" in (col.get("roles") or [])
            ),
            "",
        )
        guard = f" AND `{soft}` = 0" if soft else ""
        for column in catalog.image_columns(kind, table):
            name = str(column.get("name") or "")
            if not name:
                continue
            branches.append(
                # 库里的图片列是 utf8mb3、字面量是 utf8mb4，直接 UNION 会报
                # "Illegal mix of collations"（实测）。两边都显式转成同一个排序规则。
                # **必须 DISTINCT**：不然 LIMIT 截的是原始行（ai_problem 有 1702 行），
                # 真实存在的那几张图排在第 1300 行开外会被整批截掉（实测只剩 54 条路径、
                # 3 张能打开的病害图一张都进不了清单）。
                f"(SELECT DISTINCT '{table}' COLLATE utf8mb4_general_ci AS tbl, "
                f"'{name}' COLLATE utf8mb4_general_ci AS col, "
                f"CONVERT(`{name}` USING utf8mb4) COLLATE utf8mb4_general_ci AS path "
                f"FROM `{table}` WHERE `{name}` IS NOT NULL AND `{name}` <> '' "
                f"AND `{name}` <> 'NULL'{guard} LIMIT 300)"
            )
    if not branches:
        return ""
    limit = int(getattr(settings, "image_catalog_limit", 300) or 300)
    return (
        "SELECT tbl, col, path FROM (" + " UNION ".join(branches) + ") t "
        f"ORDER BY tbl, col LIMIT {limit}"
    )


def _build_density_template(plan: dict[str, Any]) -> str | None:
    """「病害主要集中在哪一片 / 哪里最严重」→ 按 100 米分桶找最密集的那一段。"""
    if plan["slots"].get("linear_width_m") not in (None, ""):
        return _build_pile_bucket_template(plan)
    message = plan.get("message", "")
    route = plan["slots"].get("route_match") or {}
    if not route.get("id"):
        return None
    if not _semantic_decided(plan):
        return None
    route_id = str(route["id"]).replace("'", "''")
    labels = plan["slots"].get("damage_matches") or []
    damage_join = ""
    where_extra = ""
    if labels:
        damage_join = _damage_dict_join()
        values = ", ".join("'" + item["problem_type"].replace("'", "''") + "'" for item in labels)
        where_extra = f" AND b.problem_type IN ({values})"
    plan["slots"]["metric_labels"] = {"bucket": "桩号段(m)", "count": "病害数"}
    plan["slots"]["pile_metrics"] = ["bucket"]
    return (
        "SELECT FLOOR(p.pile_distance / 100) * 100 AS bucket, COUNT(*) AS count, "
        "ROUND(SUM(p.area), 2) AS total_area "
        "FROM ai_problem p JOIN basic_facilities_routes r ON p.routes_id = r.id "
        f"{damage_join} WHERE p.del_flag = 0 AND r.del_flag = 0 AND r.id = '{route_id}'"
        f"{where_extra} GROUP BY bucket ORDER BY count DESC LIMIT 3"
    )


def _build_other_routes_template(plan: dict[str, Any]) -> str | None:
    """「不属于 A 和 B 这两条路的病害有多少」→ NOT IN 过滤。"""
    message = plan.get("message", "")
    routes = plan["slots"].get("routes") or []
    if len(routes) < 2 or not _semantic_decided(plan):
        return None
    plan["subject"] = "route"
    ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in routes)
    plan["slots"]["metric_labels"] = {"type": "路线", "count": "病害数"}
    return (
        "SELECT r.short_name AS type, COUNT(*) AS count "
        "FROM ai_problem p JOIN basic_facilities_routes r ON p.routes_id = r.id "
        f"WHERE p.del_flag = 0 AND r.del_flag = 0 AND r.id NOT IN ({ids}) "
        "GROUP BY r.id, r.short_name LIMIT 20"
    )


def _detect_dimension(message: str) -> tuple[str, str] | None:
    """按"问题里先出现的那个维度"选分组维度。

    「上行和下行各有多少处？上下行数量最多的病害类型分别是什么？」同时提到方向和病害类型，
    这时主诉求是先出现的"方向"（表顺序是固定优先级，会答错对象）。
    """
    best: tuple[int, str, str] | None = None
    for name, words, expression in _DIMENSIONS:
        positions = [message.find(word) for word in words if word in message]
        if not positions:
            continue
        position = min(positions)
        if best is None or position < best[0]:
            best = (position, name, expression)
    return (best[1], best[2]) if best else None


def _plan_dimension(plan: dict[str, Any]) -> tuple[str, str] | None:
    """这个问题的分组维度只认语义层结果；表达不了就交给 Wren。"""
    name = str(((plan.get("slots") or {}).get("group_by")) or "").strip()
    if name:
        for dim_name, _words, expression in _DIMENSIONS:
            if dim_name == name:
                return (dim_name, expression)
        # 语义层判出的时间维度可能不在关键词表里（年/半年/季度/季节/旬/工作日/时段…）：
        # 分组表达式去 time_buckets 拿物理写法即可。
        # **不往关键词表里补这些词是有意的**：`_detect_dimension` 的判据是"问题里出现了这个词
        # 就按它分组"，而"7月上旬有多少处"是**过滤**不是分组——补了词会把计数题答成分布。
        # 那样一来"语义层没给 group_by"的过滤题会被规则兜底带偏。
        spec = _TIME_BUCKET_EXPRS.get(name)
        if spec:
            return (name, spec[1].format(alias="p"))
        return None
    return None


def interpretation_scores(plan: dict[str, Any]) -> dict[str, Any]:
    """给"这次理解得准不准"打个分（方案里的"模板打分"）。

    以前是二值命中：匹配上就用、匹配不上就丢给 LLM，中间没有"我不确定"这个状态。
    这里把确定性证据累加成分数：
      +2 识别出分组维度 / +2 有对比对象 / +1 每种主体过滤条件
      +1 识别出度量（总/平均/占比…）/ -2 同时命中多个分组维度（歧义）
    调用方可以用 margin 决定"执行还是追问"（见 ambiguous_reason）。
    """
    slots = plan["slots"]
    dimensions = [
        str(slots.get("group_by"))
    ] if slots.get("group_by") else []
    metrics = [
        (str(item), str(item), str(item))
        for item in (plan.get("semantic") or {}).get("metrics") or []
        if str(item)
    ]
    score = 0
    score += 2 if dimensions else 0
    score += 2 if (plan.get("comparison") or plan.get("multi_route")) else 0
    score += len(plan.get("required_conditions") or [])
    score += 1 if metrics and metrics[0][0] != "count" else 0
    score -= 2 if len(dimensions) > 1 else 0
    return {
        "score": score,
        "dimensions": dimensions,
        "metric": metrics[0][0] if metrics else "count",
        "conditions": list(plan.get("required_conditions") or []),
    }


_DIRECTION_EXPR = (
    "CASE p.up_or_down WHEN '1' THEN '上行' WHEN '2' THEN '下行' WHEN '0' THEN '全部' "
    "WHEN '' THEN '未填' ELSE CONCAT('未知(', p.up_or_down, ')') END"
)


def _build_direction_breakdown(plan: dict[str, Any]) -> str | None:
    """「上行和下行各有多少处？各自最多的病害类型是什么？」→ 两段并起来。

    第一段：每个方向的病害数；第二段：每个方向里数量最多的病害类型。
    以前这种复合问题只会答一半（要么只给方向数量、要么只给类型分布）。
    """
    joins, where = _metric_filters(plan)
    joins = list(joins)
    if not any("ai_problem_base" in join for join in joins):
        joins.append(_damage_dict_join(join_word="LEFT JOIN"))
    from_sql = f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    counts = (
        f"SELECT {_DIRECTION_EXPR} AS type, COUNT(*) AS count {from_sql} "
        f"GROUP BY {_DIRECTION_EXPR}"
    )
    top_type = (
        # 只投影 type/count 两列，否则 UNION 两边列数不一致（报 "different number of columns"）
        "SELECT type, count FROM ("
        f"SELECT CONCAT({_DIRECTION_EXPR}, '最多：', {_DAMAGE_TYPE_EXPR}) AS type, COUNT(*) AS count, "
        f"ROW_NUMBER() OVER (PARTITION BY {_DIRECTION_EXPR} ORDER BY COUNT(*) DESC) AS rn "
        f"{from_sql} GROUP BY {_DIRECTION_EXPR}, {_DAMAGE_TYPE_EXPR}) t WHERE t.rn = 1"
    )
    plan["slots"]["metric_labels"] = {"type": "分组", "count": "处数"}
    # 首段不能加括号：校验器只认 SELECT/WITH 开头（加括号会被判"不是只读查询"）
    return f"{counts} UNION ALL {top_type} LIMIT {settings.sql_max_rows}"


# 指标：**字段名 → (中文名, SQL 表达式)**。
# 语义 JSON 的 `metrics` 按字段名来查这张表 —— "算哪个指标"由语义决定，不由问法里的关键词决定。
# 关键词那一路（`_detect_metric_specs`）只保留成"语义层没给判断"时的降级。
_METRIC_COLUMNS: dict[str, tuple[str, str]] = {
    "count": ("处数", "COUNT(*)"),
    "total_area": ("总面积", "ROUND(SUM(p.area), 2)"),
    "avg_area": ("平均面积", "ROUND(AVG(p.area), 4)"),
    "avg_per_day": ("平均每天", _AVG_PER_DAY_EXPR),
    "distinct_pile_count": ("不同桩号", "COUNT(DISTINCT p.pile_number)"),
    "max_pile": ("最远桩号(m)", "MAX(p.pile_distance)"),
    "edge_time": ("最早时间", "MIN(p.collection_time)"),
    "last_time": ("最近时间", "MAX(p.collection_time)"),
}


def metric_columns(names: list[str]) -> list[tuple[str, str, str]] | None:
    """语义 JSON 点名的指标 → [(别名, 中文名, SQL)]；有一个落不到表上就返回 None（交降级）。"""
    picked: list[tuple[str, str, str]] = []
    for name in names:
        spec = _METRIC_COLUMNS.get(str(name))
        if not spec:
            return None
        picked.append((str(name), spec[0], spec[1]))
    return picked or None


def _detect_metric_specs(message: str) -> list[tuple[str, str, str]]:
    """返回 [(别名, 标签, SQL 表达式)]，一问多指标就多列。

    ⚠️ 这是**关键词降级**：只在语义层没给出 `metrics` 时才用（见 `metric_columns`）。
    """
    specs: list[tuple[str, str, str]] = []
    asks_count = any(
        word in message for word in ("多少", "几条", "几处", "数量", "总数", "统计", "有几")
    )
    asks_average = any(word in message for word in ("平均", "均值", "每处平均"))
    if any(
        word in message
        for word in (
            "总面积",
            "面积合计",
            "面积总和",
            "总工程量",
            "面积多少",
            "多少面积",
            "总共面积",
            "总面积为",
            "面积多大",
            "多大面积",
            "面积是多少",
            "工程量",
        )
    ) and not asks_average:
        specs.append(("total_area", "总面积", "ROUND(SUM(p.area), 2)"))
    # 平均面积的问法很多：「平均面积 / 平均每处多大面积 / 每处平均面积 / 面积均值」
    if any(
        word in message
        for word in ("平均面积", "平均每处", "每处平均", "平均多大", "平均大小", "面积均值")
    ):
        specs.append(("avg_area", "平均面积", "ROUND(AVG(p.area), 4)"))
    if "平均每天" in message or "日均" in message:
        specs.append(("avg_per_day", "平均每天", _AVG_PER_DAY_EXPR))
    if any(word in message for word in ("什么时候开始", "何时开始", "最早", "开始采集", "最远")):
        specs.append(
            (
                "edge_time" if "最远" not in message else "max_pile",
                "最早时间" if "最远" not in message else "最远桩号(m)",
                "MIN(p.collection_time)" if "最远" not in message else "MAX(p.pile_distance)",
            )
        )
    if "不同桩号" in message or "多少个桩号" in message or "几个桩号" in message:
        specs.append(("distinct_pile_count", "不同桩号", "COUNT(DISTINCT p.pile_number)"))
    if any(word in message for word in ("多久", "什么时候", "最近一次", "最新")):
        specs.append(("last_time", "最近时间", "MAX(p.collection_time)"))
    if asks_count or "面积" in message and not specs:
        specs.append(("count", "处数", "COUNT(*)"))
    if not specs:
        specs.append(("count", "处数", "COUNT(*)"))
    return specs


def _build_generic_metric_template(plan: dict[str, Any]) -> str | None:
    """维度 × 指标 的通用取数（替代"再加一个模板"）。

    覆盖：哪天最多 / 按周趋势 / 星期分布 / 时段分布 / 每月日均 /
    每条路的多个指标 / 多少处+总面积 / K1以西有多少处 ……
    只有当维度、多指标或空间单边能识别出来时才生效，否则返回 None 交给 LLM。
    """
    message = plan.get("message", "")
    slots = plan["slots"]
    subject = plan.get("subject", "disease")
    source = _SUBJECT_SOURCES.get(subject)
    # 巡查/工单有各自的链路；这里只处理疾病 / 里程桩点 / 路段 三类。
    if subject not in {"disease", "pile_point", "section"} or not source:
        return None
    # 对象表护栏：这题问的对象如果**不是**病害/桩点/路段（落点表是桥梁/隧道/边坡/巡查资产/工单/
    # 字典表…），拿主表拼出来的"按路线数病害"是错答——直接弃权，交给通用算子/Wren。
    # 判据用 field_grounding.subject_table（它已经处理了"统计题别把字典当主体"）。
    from . import field_grounding

    object_table = field_grounding.subject_table(plan)
    other_object_tables = {str(item.get("table")) for item in _TABLE_SUBJECTS} | {
        "basic_facilities_bridges",
        "basic_facilities_tunnels",
        "basic_facilities_slope",
        "basic_facilities_sections",
        "basic_facilities_sections_pile_detail",
        "basic_facilities_routes",
        "ai_maintain_patrol",
        "ai_asset_main",
        "ai_work_order",
    }
    if object_table and object_table in other_object_tables and object_table != (
        str(source[0]).split(" ")[0] if source else ""
    ):
        log_event(
            "nl2sql",
            "INFO",
            "generic_metric_object_mismatch",
            subject=subject,
            object_table=object_table,
            question=message[:60],
        )
        return None
    # 数值阈值/置信度条件直接按语义条件拼接。
    thresholds = condition_clauses(plan)
    # 「面积>3 且置信度<0.7 的病害有哪些」是列表查询，交给 list 模板逐条列出；
    # 这里（维度×指标）只会算成一个 COUNT，等于把列表需求吃掉。
    if plan["shape"] == "list" and thresholds:
        return None
    # 「平均面积是多少 + 超过平均值的有多少条」是平均模板的活（要自比均值），别在这里被拆成两列。
    if any(word in message for word in ("平均", "均值")) and "超过" in message:
        return None
    # 「面积/置信度/长度最大的那条（含并列）」交给专门的 metric-top 模板，
    # 它用 RANK() 保证并列不漏；这里只管"哪天最多/哪条路最多"这类计数比较。
    if any(word in message for word in ("最大", "最高", "最小", "最低")) and any(
        word in message for words, _ in _METRIC_EXPRESSIONS for word in words
    ):
        return None
    # 对比类问题里计划不会挂 damage_matches，这里按名词单独解析，否则会丢病害类型过滤。
    if slots.get("damage_type") and not slots.get("damage_matches"):
        try:
            slots["damage_matches"] = _resolve_damage_type(slots["damage_type"])
        except QueryPlanError:
            return None
    dimension = _plan_dimension(plan)
    # 「上下行各有多少处？各自最多的病害类型是什么？」这种复合问法，单独拼两段回答
    if (
        dimension
        and dimension[0] == "direction"
        and any(word in message for word in ("最多", "前几", "排名", "top"))
        and any(word in message for word in ("类型", "病害"))
    ):
        breakdown = _build_direction_breakdown(plan)
        if breakdown:
            return breakdown
    # 「6月和7月平均每天」问的是按月日均，不能按"每天"分组。
    if any(word in message for word in ("平均每天", "日均")):
        dimension = ("month", "DATE_FORMAT(p.collection_time, '%Y-%m')")
    # 多路线对比 + 聚合指标（哪条路平均面积大）→ 按路线分组
    if plan.get("multi_route") and not dimension:
        # 「恒明珠的泛油中，面积大于西乡段平均面积的有多少条」这种跨路线阈值题，
        # 分组统计会答错（要的是子查询比较），交给 LLM。
        if any(word in message for word in ("平均", "大于", "超过", "高于", "小于", "低于")):
            return None
        dimension = ("route", "r.short_name")
    # 指标：**算哪几个由语义 JSON 的 `metrics` 决定**，表达式按字段名查 `_METRIC_COLUMNS`
    # （"字段名 → 表达式"表）。JSON 点名的指标里只要有一个这张表表达不了
    # （`rate`/`ratio`/`density` 这类要靠派生计算），就整体退回关键词降级
    # ——宁可多一列，也不能把用户点名的那一列丢成空。
    declared_metrics = [
        str(item) for item in ((plan.get("semantic") or {}).get("metrics") or []) if item
    ]
    metrics = metric_columns(declared_metrics)
    if not metrics:
        return None
    side = parsers.parse_pile_side(message)

    damage = slots.get("damage_matches") or []
    wants_unknown = any(word in message for word in ("未知", "识别不出来", "无法识别"))
    by_damage_type = bool(dimension and dimension[0] == "damage_type")
    two_bucket = bool(damage) and wants_unknown

    if (
        not dimension
        and len(metrics) <= 1
        and not two_bucket
        and not side
        and all(alias == "count" for alias, _, _ in metrics)
        and subject == "disease"
        and not thresholds
    ):
        return None

    dict_join = _damage_dict_join(join_word="LEFT JOIN")
    # 「K1 以西」这类单边口径自带桩号条件，别再叠加括号里解析出来的桩号区间（会互相打架）。
    if subject == "disease":
        joins, where = _metric_filters(
            plan, with_time=False, with_damage=not two_bucket, with_pile=not side
        )
    else:
        # 桩点/路段这类主体：路线过滤走各自的链路，病害相关的条件一律不适用。
        table, joins = source
        where = ["p.del_flag = 0", "r.del_flag = 0"]
        if slots.get("route_match"):
            where.append(_route_sql(plan))
        joins = list(joins)
        # 「几个路段 + 多少个里程桩」一起问时，两个数都要给。
        if subject == "pile_point" and "路段" in message:
            route_id = str((slots.get("route_match") or {}).get("id") or "").replace("'", "''")
            pieces = [
                f"SELECT '里程桩点' AS type, COUNT(*) AS count FROM {table} {' '.join(joins)} "
                f"WHERE {' AND '.join(where)}",
                "SELECT '路段' AS type, COUNT(*) AS count FROM basic_facilities_sections s "
                "JOIN basic_facilities_routes r ON s.route_id = r.id "
                f"WHERE s.del_flag = 0 AND r.del_flag = 0 AND r.id = '{route_id}'",
            ]
            plan["slots"]["metric_labels"] = {"type": "对象", "count": "数量"}
            return " UNION ALL ".join(pieces) + " LIMIT 10"
        metrics = [("count", "处数", "COUNT(*)")]
        labels = {"count": "处数"}
        plan["slots"]["metric_labels"] = labels
        return (
            f"SELECT {metrics[0][2]} AS count FROM {table} {' '.join(joins)} "
            f"WHERE {' AND '.join(where)} LIMIT 1"
        )
    if two_bucket:
        values = ", ".join(
            "'" + item["problem_type"].replace("'", "''") + "'" for item in damage
        )
        labels = "、".join(item["problem_type"] for item in damage)
        metrics = [
            ("matched_count", labels, f"COALESCE(SUM(b.problem_type IN ({values})), 0)"),
            ("unknown_count", "未知类型", "COALESCE(SUM(b.problem_type IS NULL), 0)"),
            *metrics,
        ]
        joins.append(dict_join)
    elif by_damage_type and not any("ai_problem_base" in join for join in joins):
        joins.append(dict_join)

    routes_in_plan = slots.get("routes") or []
    if plan.get("multi_route") and routes_in_plan:
        ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in routes_in_plan)
        where.append(f"r.id IN ({ids})")

    if side and not any(alias == side[0] for alias, _, _ in metrics):
        where.append(side[2])
    # 「面积等于 5000 平方米」「置信度≥0.7」这类条件直接进 WHERE
    where.extend(thresholds)

    # 「6月和7月平均每天」这类：时间范围要覆盖全部月份，不能只取第一个月
    months = parsers.parse_month_metrics(message)
    if len(months) >= 2 and dimension and dimension[0] == "month":
        where = [item for item in where if "collection_time" not in item]
        where.append(f"p.collection_time >= '{_stamp(months[0][1])}'")
        where.append(f"p.collection_time < '{_stamp(months[-1][2])}'")
    elif slots.get("time_start") or slots.get("time_end"):
        if slots.get("time_start"):
            where.append(f"p.collection_time >= '{slots['time_start']}'")
        if slots.get("time_end"):
            where.append(f"p.collection_time < '{slots['time_end']}'")

    labels = {alias: label for alias, label, _ in metrics}
    select = ", ".join(f"{expr} AS {alias}" for alias, _, expr in metrics)
    group_by = ""
    order_by = ""
    limit = settings.sql_max_rows
    if dimension:
        name, expression = dimension
        select = f"{expression} AS type, {select}"
        group_by = f"GROUP BY {expression}"
        labels = {"type": _DIMENSION_LABELS.get(name, "分组"), **labels}
        # 时间分档的标签是**中文可读写法**（`2026年第3季度`、`2026年07月上旬`），按标签排是字典序：
        # 旬会排成"上旬、下旬、中旬"，季节会排成"冬季、夏季、春季、秋季"，周会排成"第31周、第3周"。
        # time_buckets 里给了第 3 项（排序表达式）的维度就按它排；没给的说明标签本身就能正确排序
        # （`2026-07`、`2026年07月上半月`、`HOUR()` 这种），继续按标签排。
        bucket = _TIME_BUCKET_EXPRS.get(name) or ()
        order_expr = bucket[2].format(alias="p") if len(bucket) > 2 else ""
        # 排序与取几行**都读语义 JSON 的字段**（`shape` / `order_by` / `limit`），
        # 不再用关键词判"最多/最近"——那是"处理逻辑靠关键词命中问法"。
        # 语义层没给 `order_by` 时的默认：按分档自身排（时间分档用它的排序表达式，其余按标签）。
        order_by = f"ORDER BY {order_expr}" if order_expr else "ORDER BY type"
        semantic = plan.get("semantic") or {}
        semantic_order = str(semantic.get("order_by") or "")
        if semantic_order.endswith("_desc") or semantic_order.endswith("_asc"):
            metric_alias = str(metrics[0][0]) if metrics else "count"
            direction = "DESC" if semantic_order.endswith("_desc") else "ASC"
            order_by = f"ORDER BY {metric_alias} {direction}"
        # `shape=top` 就是"取极值/前几名"：行数由 JSON 的 `limit` 给，没给就是 1（第一名）。
        # 别的形状（distribution/trend）看全量分档，不受这个限制。
        semantic_limit = semantic.get("limit")
        if str(plan.get("shape") or "") == "top":
            limit = semantic_limit if isinstance(semantic_limit, int) and semantic_limit > 0 else 1

    plan["slots"]["metric_labels"] = labels
    parts = [
        f"SELECT {select}",
        f"FROM ai_problem p {' '.join(joins)}",
        f"WHERE {' AND '.join(where)}",
        group_by,
        order_by,
        f"LIMIT {limit}",
    ]
    return " ".join(part for part in parts if part)
_DIMENSION_LABELS = {
    "year": "年份",
    "half_year": "半年",
    "quarter": "季度",
    "season": "季节",
    "week": "周次",
    "half_month": "半月",
    "ten_day": "旬",
    "dow": "星期",
    "hour": "小时",
    "day": "日期",
    "month": "月份",
    "workday": "工作日/周末",
    "time_of_day": "上午/下午/晚上",
    "minute": "分钟",
    "route": "路线",
    "damage_type": "病害类型",
    "direction": "方向",
    "device": "设备",
}


def _build_known_unknown_template(plan: dict[str, Any]) -> str | None:
    """「泛油和识别不出来的各有多少」→ 已知类型 + 字典里查不到的类型，各算一列。

    这类问题的陷阱是：JOIN 字典表求「泛油」会把字典里重复的 code 放大一倍，
    所以必须用 LEFT JOIN + SUM(条件)，不能 GROUP BY 后 COUNT。
    """
    message = plan.get("message", "")
    if not _semantic_decided(plan):
        return None
    # 带维度（每条路/按月/哪一天…）时交给通用"维度×指标"构建器，它会按维度分组。
    if _plan_dimension(plan):
        return None
    damage = plan["slots"].get("damage_matches") or []
    if not damage:
        return None
    values = ", ".join(
        "'" + item["problem_type"].replace("'", "''") + "'" for item in damage
    )
    label = "、".join(item["problem_type"] for item in damage)
    # 这里要保留「字典里查不到」的行，所以不能加 b.problem_type IN (...) 过滤。
    joins, where = _metric_filters(plan, with_time=False, with_damage=False)
    if plan["slots"].get("time_start"):
        where.append(f"p.collection_time >= '{plan['slots']['time_start']}'")
    if plan["slots"].get("time_end"):
        where.append(f"p.collection_time < '{plan['slots']['time_end']}'")
    joins.append(_damage_dict_join(join_word="LEFT JOIN"))
    plan["slots"]["metric_labels"] = {"matched_count": label, "unknown_count": "未知类型"}
    return (
        f"SELECT SUM(b.problem_type IN ({values})) AS matched_count, "
        "SUM(b.problem_type IS NULL) AS unknown_count, COUNT(*) AS total_count "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    )


def _build_multi_metric_template(plan: dict[str, Any]) -> str | None:
    """一问多个口径（6月多少？7月多少？7月29日多少？K1以西多少？）→ 一条 SQL 每口径一列。

    实测模型会把它们写成互斥的 CASE WHEN，7 月把 7/29 吃掉、K1以西永远命中不到，
    数字全错。这里改成各口径独立的 SUM(...)，彼此不互斥。
    """
    message = plan.get("message", "")
    # 「平均每天/按周/星期几/时段」是时间维度聚合，交给通用维度构建器。
    if any(
        word in message
        for word in ("平均每天", "日均", "按周", "哪几周", "第几周", "星期几", "周几", "时段", "几点", "每小时")
    ):
        return None
    # 对比类问题里不会挂 damage_matches，这里按名词单独解析，否则会丢病害类型过滤。
    if plan["slots"].get("damage_type") and not plan["slots"].get("damage_matches"):
        try:
            plan["slots"]["damage_matches"] = _resolve_damage_type(plan["slots"]["damage_type"])
        except QueryPlanError:
            return None
    metrics: list[tuple[str, str, str]] = []
    for label, start, end in parsers.parse_month_metrics(message):
        metrics.append(
            (
                f"month_{start.strftime('%Y%m')}",
                label,
                f"p.collection_time >= '{_stamp(start)}' AND p.collection_time < '{_stamp(end)}'",
            )
        )
    for label, start, end in parsers.parse_day_metrics(message):
        metrics.append(
            (
                f"day_{start.strftime('%Y%m%d')}",
                label,
                f"p.collection_time >= '{_stamp(start)}' AND p.collection_time < '{_stamp(end)}'",
            )
        )
    side = parsers.parse_pile_side(message)
    if side:
        metrics.append(side)

    # 同一句里还问「都是什么类型 / 有图片吗」时，一并算出来（设备那类是典型）。
    slots = plan["slots"]
    extra_joins: list[str] = []
    damage = slots.get("damage_matches") or []
    wants_unknown = any(word in message for word in ("未知", "识别不出来", "无法识别"))
    if damage and any(word in message for word in ("类型", "病害类型", "是什么类型", "什么类型")):
        values = ", ".join(
            "'" + item["problem_type"].replace("'", "''") + "'" for item in damage
        )
        metrics.append(
            (
                "matched_count",
                "、".join(item["problem_type"] for item in damage),
                f"SUM(b.problem_type IN ({values}))",
            )
        )
        if wants_unknown:
            metrics.append(("unknown_count", "未知类型", "COALESCE(SUM(b.problem_type IS NULL), 0)"))
        extra_joins.append(_damage_dict_join(join_word="LEFT JOIN"))
    if any(word in message for word in ("图片", "照片", "影像")):
        metrics.append(("with_image_count", "有图片", "p.problem_url IS NOT NULL AND p.problem_url <> ''"))
    if len(metrics) < 2:
        return None

    seen: set[str] = set()
    unique: list[tuple[str, str, str]] = []
    for alias, label, condition in metrics:
        if alias in seen:
            continue
        seen.add(alias)
        unique.append((alias, label, condition))

    joins, where = _metric_filters(
        plan, with_time=False, with_damage=not extra_joins, with_pile=not side
    )
    joins.extend(extra_joins)
    # 「都是什么类型」但没点名类型 → 追加一列类型分布（泛油:402；未知类型:364）
    if any(word in message for word in ("类型", "什么病害")) and not damage and not extra_joins:
        sub_joins = " ".join(joins)
        sub_where = " AND ".join(where)
        for source, target in (("p.", "p2."), ("r.", "r2.")):
            sub_joins = sub_joins.replace(source, target)
            sub_where = sub_where.replace(source, target)
        # 表别名本身没有点号，要单独换（否则 ON 用 r2，别名还是 r）。
        sub_joins = sub_joins.replace("basic_facilities_routes r ON", "basic_facilities_routes r2 ON")
        sub_joins += " " + _damage_dict_join(
            disease_alias="p2", dict_alias="b2", join_word="LEFT JOIN"
        )
        # 注意：select 是用 unique 拼的，这里必须加到 unique 上。
        unique.append(
            (
                "type_breakdown",
                "类型分布",
                "(SELECT GROUP_CONCAT(CONCAT_WS(':', t.tp, t.c) ORDER BY t.c DESC SEPARATOR '；') "
                "FROM (SELECT COALESCE(b2.problem_type, '未知类型') AS tp, COUNT(*) AS c "
                f"FROM ai_problem p2 {sub_joins} WHERE {sub_where} "
                "GROUP BY COALESCE(b2.problem_type, '未知类型')) t)",
            )
        )
    # 普通口径是"条件成立就计 1"，外面套 SUM；标量子查询（如类型分布）直接取用。
    select = ", ".join(
        f"{condition} AS {alias}" if condition.startswith("(") else f"SUM({condition}) AS {alias}"
        for alias, _, condition in unique
    )
    plan["slots"]["metric_labels"] = {alias: label for alias, label, _ in unique}
    return (
        f"SELECT {select}, COUNT(*) AS total_count "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    )


def _build_ratio_template(plan: dict[str, Any]) -> str | None:
    """「A 和 B 两天占 7 月总数的多少比例」→ 分子/分母/百分比一次算出来。"""
    message = plan.get("message", "")
    # 对比类问题不会挂 damage_matches，这里按名词单独解析，否则比例会算成"全类型"。
    if plan["slots"].get("damage_type") and not plan["slots"].get("damage_matches"):
        try:
            plan["slots"]["damage_matches"] = _resolve_damage_type(plan["slots"]["damage_type"])
        except QueryPlanError:
            return None
    if not any(word in message for word in ("占比", "比例", "占")):
        return None
    days = parsers.parse_days(message)
    month = parsers.parse_month_range(message)
    if not days or not month:
        return None
    if days[0] < month[0] or days[1] > month[1]:
        return None

    joins, where = _metric_filters(plan, with_time=False)
    part = f"p.collection_time >= '{_stamp(days[0])}' AND p.collection_time < '{_stamp(days[1])}'"
    base = f"p.collection_time >= '{_stamp(month[0])}' AND p.collection_time < '{_stamp(month[1])}'"
    return (
        f"SELECT SUM(CASE WHEN {part} THEN 1 ELSE 0 END) AS part_count, "
        "COUNT(*) AS total_count, "
        f"ROUND(100.0 * SUM(CASE WHEN {part} THEN 1 ELSE 0 END) / COUNT(*), 2) AS ratio_percent "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where + [base])}"
    )


def _build_quality_template(plan: dict[str, Any]) -> str | None:
    """「这些病害覆盖什么时间范围、有什么共同特征」→ 时间跨度 + 各字段缺失统计。"""
    message = plan.get("message", "")
    if plan["shape"] not in {"detail", "list"}:
        return None
    if not any(
        word in message for word in ("时间范围", "共同特征", "有什么特征", "数据质量", "异常")
    ):
        return None
    joins, where = _metric_filters(plan)
    joins.append("LEFT JOIN basic_facilities_sections s ON p.sections_id = s.id")
    return (
        "SELECT COUNT(*) AS count, MIN(p.collection_time) AS first_time, "
        "MAX(p.collection_time) AS last_time, "
        "SUM(NOT EXISTS (SELECT 1 FROM ai_problem_base ub "
        "WHERE ub.id = p.problem_base_id AND ub.problem_type IS NOT NULL "
        "AND (ub.del_flag = 0 OR ub.del_flag IS NULL))) AS unknown_type_count, "
        "SUM(p.up_or_down IS NULL OR p.up_or_down IN ('', 'NULL')) AS no_direction_count, "
        "SUM(s.id IS NULL OR s.tech_grade IS NULL OR s.tech_grade IN ('', 'NULL')) "
        "AS no_section_attr_count "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    )


def _build_average_template(plan: dict[str, Any]) -> str | None:
    """「X 的平均值是多少？超过平均值的有多少条？」一次算出来。

    之前模板直接按 count 答了「共 936 处」，把平均和超均值这两问都丢了。
    """
    message = plan.get("message", "")
    if not any(word in message for word in ("平均", "均值")):
        return None
    # 多路线对比要按路线分别求平均，交给"维度×指标"构建器。
    if plan.get("multi_route"):
        return None
    expression = next((expr for word, expr in _AVG_METRICS if word in message), None)
    if not expression:
        return None
    joins, where = _metric_filters(plan)
    inner = (
        f"SELECT {expression} AS metric, AVG({expression}) OVER () AS avg_metric "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    )
    return (
        "SELECT ROUND(AVG(s.metric), 4) AS avg_metric, "
        "SUM(s.metric > s.avg_metric) AS above_avg_count, COUNT(*) AS total_count "
        f"FROM ({inner}) s"
    )


# 「按桩号区间分组统计各段病害数量」：分段写法必须由代码定死。
# 实测交给 LLM 自由发挥时会写成 CONCAT('K', FLOOR(pile_distance/1000))，
# 全路段 0~887m 全落进 "K0" 一段，答出"K0 911 处"这种没意义的分布。
_PILE_STEP_RE = re.compile(r"每\s*(\d+(?:\.\d+)?)\s*(公里|千米|km|KM|米|m|M)")


def _pile_bucket_step(plan: dict[str, Any]) -> int:
    """分段长度：用户说了就按用户说的，没说默认 100 米（"前几个桩号段"要的是细粒度）。"""
    semantic_step = (plan.get("slots") or {}).get("bucket_meters")
    try:
        step = int(float(semantic_step))
        if step > 0:
            return step
    except (TypeError, ValueError):
        pass
    matched = _PILE_STEP_RE.search(str(plan.get("message") or ""))
    if not matched:
        return 100
    value = float(matched.group(1))
    if matched.group(2) in {"公里", "千米", "km", "KM"}:
        value *= 1000
    step = int(value)
    return step if step > 0 else 100


def _build_pile_bucket_template(plan: dict[str, Any]) -> str | None:
    """桩号段分布：SELECT 桩号段, COUNT(*) GROUP BY 段。"""
    slots = plan["slots"]
    if str(slots.get("group_by") or "") != "pile_bucket":
        return None
    step = _pile_bucket_step(plan)
    start = f"FLOOR(p.pile_distance / {step}) * {step}"

    def pile_point(expr: str) -> str:
        return (
            f"CONCAT('K', FLOOR(({expr}) / 1000), '+', "
            f"LPAD(MOD({expr}, 1000), 3, '0'))"
        )

    label = f"CONCAT({pile_point(start)}, '~', {pile_point(f'{start} + {step}')})"
    joins, where = _metric_filters(plan)
    where.append("p.pile_distance IS NOT NULL")
    width = slots.get("linear_width_m")
    if width not in (None, ""):
        limit = 1 if plan.get("shape") == "top" else 20
        return (
            f"SELECT {label} AS 桩号段, COUNT(*) AS 病害数量, "
            f"ROUND(SUM({_DISEASE_LENGTH_EXPR} * {float(width):g}), 2) AS 换算面积 "
            f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)} "
            f"GROUP BY {start} ORDER BY 换算面积 DESC LIMIT {limit}"
        )
    # 每一段自己的密度（段长固定，所以是 段内病害数 ÷ (step/1000) 公里）与平均面积，
    # 这样"按桩号段分组统计数量/密度/平均面积"一次就够。
    return (
        f"SELECT {label} AS 桩号段, COUNT(*) AS 病害数量, "
        f"ROUND(COUNT(*) / {step / 1000.0}, 1) AS `病害密度(处/公里)`, "
        f"ROUND(AVG(p.area), 2) AS 平均面积 "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)} "
        f"GROUP BY {start} ORDER BY 病害数量 DESC LIMIT 20"
    )


def _build_distinct_pile_template(plan: dict[str, Any]) -> str | None:
    """「这些病害分布在多少个不同的桩号上」→ COUNT(DISTINCT pile_number)。"""
    if not plan["slots"].get("distinct_pile"):
        return None
    joins, where = _metric_filters(plan)
    return (
        "SELECT COUNT(DISTINCT p.pile_number) AS distinct_pile_count "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)} LIMIT 1"
    )


_METRIC_EXPRESSIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("置信度", "可信度", "信度"),
        pic_json_expr("confidence", True, "DECIMAL(10,6)"),
    ),
    (("面积",), "p.area"),
    (("长度",), _DISEASE_LENGTH_EXPR),
    # 标注尺寸（同样要限定词，避免和"宽度/高度"这类表列语义打架）
    (("标注宽度", "图片宽度", "AI 宽度"), pic_json_expr("width", True, "DECIMAL(10,2)")),
    (("标注高度", "图片高度", "AI 高度"), pic_json_expr("height", True, "DECIMAL(10,2)")),
)


def _build_metric_top_template(plan: dict[str, Any]) -> str | None:
    """「XX 最高的病害是哪一条」：用 RANK() 把并列的全都列出来。

    实测 LLM 只会给 LIMIT 1 的那一条，而最高值往往多条并列（例如泛油置信度 0.923649 有 18 条）。
    """
    if plan["shape"] != "top":
        return None
    message = plan.get("message", "")
    expression = next(
        (expr for words, expr in _METRIC_EXPRESSIONS if any(word in message for word in words)),
        None,
    )
    if not expression:
        return None

    joins, where = _metric_filters(plan)
    if "JSON_EXTRACT" in expression:
        # JSON 字段可能为空（没图片的病害 pic_position 就是空）→ 比较前先排掉，
        # 否则 JSON_EXTRACT 返回 NULL，`> 2` 之类的条件结果不符合预期（方案 §2.4）。
        where.append("p.pic_position IS NOT NULL AND p.pic_position <> ''")

    return (
        "SELECT * FROM ("
        f"SELECT p.problem_number, p.pile_number, r.short_name AS route_name, "
        f"{expression} AS metric, RANK() OVER (ORDER BY {expression} DESC) AS rnk "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}) t "
        f"WHERE t.rnk = 1 ORDER BY t.route_name, t.pile_number LIMIT {settings.sql_max_rows}"
    )


def _build_patrol_task_template(plan: dict[str, Any]) -> str | None:
    """巡检任务类问题：「多少次巡查任务 / 这些任务发现多少处病害 / 各类型任务各多少次」。

    实测 LLM 会把病害数当任务数（936/1702 vs 正确的 10），所以链路和口径都固定下来：
    路线巡查次数走 ai_maintain_routes_patrol；病害检出数才走 ai_problem.patrol_id。
    任务数用 COUNT(DISTINCT t.id)——一次任务对应多条路线/病害时不去重会翻倍。
    """
    if plan.get("subject", "disease") != "patrol":
        return None
    message = plan.get("message", "")
    slots = plan["slots"]
    # 只有明确问"任务"的才算任务数；否则（"通过什么类型的巡查发现的" / "要不要重新巡检核实"）
    # 是别的模板的活——上面那两类都踩过坑：会把病害数当任务数、或抢走置信度统计。
    wants_tasks = "任务" in message and any(
        word in message for word in ("多少", "几次", "次数", "几个", "统计", "分别")
    )
    if not wants_tasks:
        return None
    wants_diseases = any(word in message for word in ("病害", "问题"))

    labels = dictionary.patrol_type_labels()
    type_expr = dictionary.case_expression("t.patrol_type", labels) if labels else "t.patrol_type"
    # 只有"要按路线筛"或"要数发现的病害数"时才回连病害表；
    # 「一共执行过多少次巡检任务」是数巡查表本身（147 次），回连病害会少算成 37。
    joins = []
    where = ["t.del_flag = 0"]
    if wants_diseases:
        joins.append("JOIN ai_problem p ON p.patrol_id = t.id")
        where.append("p.del_flag = 0")
    if slots.get("route_match"):
        joins.append("JOIN ai_maintain_routes_patrol pr ON pr.patrol_id = t.id")
        joins.append("JOIN basic_facilities_routes r ON pr.routes_id = r.id")
        where.append("pr.del_flag = 0")
        where.append("r.del_flag = 0")
        where.append(_route_sql(plan))
    if slots.get("time_start") or slots.get("time_end"):
        # 巡检表的时间字段是 create_time（没有 start_time）
        if slots.get("time_start"):
            where.append(f"t.create_time >= '{slots['time_start']}'")
        if slots.get("time_end"):
            where.append(f"t.create_time < '{slots['time_end']}'")
    if slots.get("damage_matches"):
        joins.append(_damage_dict_join())
        values = ", ".join(f"'{item['problem_type']}'" for item in slots["damage_matches"])
        where.append(f"b.problem_type IN ({values})")

    # 按类型分组：用户问「各类型/分别/每种」才算；只问一种类型就是总量
    grouped = any(word in message for word in ("各类型", "分别", "每种", "各巡检类型", "按类型"))
    metrics = []
    if wants_tasks:
        metrics.append(("tasks", "任务次数", "COUNT(DISTINCT t.id)"))
    if wants_diseases:
        metrics.append(("diseases", "发现病害数", "COUNT(DISTINCT p.id)"))
    # 「最近一次是什么时候」→ 顺带把最后一次任务时间带出来
    if any(word in message for word in ("最近", "最新", "什么时候", "什么时间")):
        metrics.append(("last_time", "最近一次时间", "MAX(t.create_time)"))
    if not metrics:
        metrics = [("tasks", "任务次数", "COUNT(DISTINCT t.id)")]
    labels_map = {alias: label for alias, label, _ in metrics}
    select = ", ".join(f"{expr} AS {alias}" for alias, _, expr in metrics)
    from_sql = f"FROM ai_maintain_patrol t {' '.join(joins)} WHERE {' AND '.join(where)}"
    if grouped:
        plan["slots"]["metric_labels"] = {"type": "巡检类型", **labels_map}
        return (
            f"SELECT {type_expr} AS type, {select} {from_sql} "
            f"GROUP BY {type_expr} ORDER BY {metrics[0][0]} DESC LIMIT {settings.sql_max_rows}"
        )
    plan["slots"]["metric_labels"] = labels_map
    return f"SELECT {select} {from_sql} LIMIT 1"


def _build_patrol_route_overview_template(plan: dict[str, Any]) -> str | None:
    """巡查任务级概览：一次取回任务、状态、里程、路线归属和病害数。

    只消费语义计划，不按用户原话做模板识别。病害按 ``patrol_id + routes_id``
    绑定到具体任务和路线；同一次巡查挂多条路线时，各路线分别计数。
    """
    if not is_patrol_route_overview_plan(plan):
        return None
    slots = plan.get("slots") or {}
    where = ["t.del_flag = 0"]
    if slots.get("time_start"):
        where.append(f"t.create_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"t.create_time < '{slots['time_end']}'")
    route = slots.get("route_match") or {}
    if route.get("id"):
        route_id = str(route["id"]).replace("'", "''")
        where.append(f"pr.routes_id = '{route_id}'")

    status_labels = dictionary.patrol_status_labels()
    status_name = dictionary.case_expression("t.patrol_status", status_labels)
    plan["slots"]["patrol_overview"] = True
    plan["slots"]["metric_labels"] = {
        "task_number": "任务编号",
        "patrol_number": "巡查编号",
        "patrol_status": "巡查状态编码",
        "patrol_status_name": "巡查状态",
        "patrol_mileage_m": "巡检里程（米）",
        "route_name": "路线",
        "disease_count": "病害数",
    }
    return (
        "SELECT "
        "t.task_number AS task_number, "
        "t.patrol_status AS patrol_status, "
        "t.patrol_mileage AS patrol_mileage_m, "
        "r.full_name AS route_name, "
        "t.id AS task_id, "
        "t.patrol_number AS patrol_number, "
        "t.create_time AS task_created_at, "
        f"{status_name} AS patrol_status_name, "
        "r.code AS route_code, "
        "COUNT(DISTINCT p.id) AS disease_count "
        "FROM ai_maintain_patrol t "
        "LEFT JOIN ai_maintain_routes_patrol pr "
        "  ON pr.patrol_id = t.id AND pr.del_flag = 0 "
        "LEFT JOIN basic_facilities_routes r "
        "  ON r.id = pr.routes_id AND r.del_flag = 0 "
        "LEFT JOIN ai_problem p "
        "  ON p.patrol_id = t.id AND p.routes_id = pr.routes_id AND p.del_flag = 0 "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY t.id, t.task_number, t.patrol_number, t.patrol_status, t.patrol_mileage, "
        "t.create_time, r.code, r.full_name "
        "ORDER BY t.create_time, t.task_number, r.code, r.full_name "
        f"LIMIT {settings.sql_max_rows}"
    )


def _build_patrol_group_template(plan: dict[str, Any]) -> str | None:
    """「这些病害是通过什么类型的巡查发现的」→ 按巡查类型分组。

    实测 LLM 会拿 patrol_method_type（巡查方式）当巡查类型，答出 1428/274 这种错的分布。
    """
    message = plan.get("message", "")
    pieces: list[str] = []
    # 问"这些病害是通过什么类型的巡查发现的"→ 数病害（走 patrol_id 关联）；
    # 问"巡检任务都是什么类型"→ 数巡查任务表本身。两者口径不同。
    disease_centric = any(word in message for word in ("病害", "问题", "泛油"))
    base_from = (
        "ai_problem p JOIN ai_maintain_patrol t ON p.patrol_id = t.id"
        if disease_centric
        else "ai_maintain_patrol t"
    )
    base_where = "p.del_flag = 0 AND t.del_flag = 0" if disease_centric else "t.del_flag = 0"
    if plan["slots"].get("patrol_group") == "patrol_type" or "类型" in message:
        # code→名称 从库里列注释读（字典驱动），读不到就原样输出代码
        type_case = dictionary.case_expression(
            "t.patrol_type", dictionary.patrol_type_labels()
        )
        pieces.append(
            f"SELECT '巡查类型' AS dimension, {type_case} AS type, COUNT(*) AS count "
            f"FROM {base_from} "
            f"WHERE {base_where} "
            "GROUP BY t.patrol_type"
        )
    if any(word in message for word in ("状态", "完成情况", "完成", "进度")):
        status_case = dictionary.case_expression(
            "t.patrol_status", dictionary.patrol_status_labels()
        )
        pieces.append(
            f"SELECT '巡查状态' AS dimension, {status_case} AS type, COUNT(*) AS count "
            f"FROM {base_from} "
            f"WHERE {base_where} "
            "GROUP BY t.patrol_status"
        )
    if not pieces:
        return None
    plan["slots"]["metric_labels"] = {"type": "口径值", "count": "次数"}
    compiled = f"SELECT dimension, type, count FROM ({' UNION ALL '.join(pieces)}) t LIMIT 50" if len(pieces) > 1 else pieces[0] + " LIMIT 20"
    plan["slots"]["patrol_multi"] = len(pieces) > 1
    return compiled


_TECH_GRADE_CASE = (
    "CASE MAX(s.tech_grade) WHEN '0' THEN '高速公路' WHEN '1' THEN '一级公路' "
    "WHEN '2' THEN '二级公路' WHEN '3' THEN '三级公路' WHEN '4' THEN '四级公路' "
    "WHEN '5' THEN '等外公路' ELSE MAX(s.tech_grade) END"
)


def _build_multi_route_template(plan: dict[str, Any]) -> str | None:
    """多路线对比 + 路线属性：等级/车道/宽度在 sections 表，病害数在 ai_problem 表。

    这个组合实测 LLM 老是写成 routes.tech_grade 而报 Unknown column，
    干脆用模板固定下来。
    """
    routes = plan["slots"].get("routes") or []
    attrs = {
        str(item)
        for item in ((plan.get("semantic") or {}).get("output_fields") or [])
    }
    if len(routes) < 2 or not attrs & {
        "level",
        "route_level",
        "tech_grade",
        "lane_count",
        "surface_width",
        "surface_type",
    }:
        return None
    # 台账类问题（"两条路的路线长度分别是多少"）交给 _build_route_info_template：
    # 它按起止桩号算长度，而这里只会按"每条路的病害数"回答（实测答成 749/911）。
    if any(
        word in plan.get("message", "")
        for word in ("长度", "全长", "多少公里", "起点", "终点", "起终点")
    ):
        return None
    # 把每一列的口径标出来，回答层就不用靠猜列名了。
    plan["slots"]["metric_labels"] = {
        "type": "路线",
        "disease_count": "病害数",
        "tech_grade_name": "技术等级",
        "lane_count": "车道数",
        "surface_width": "路面宽度",
    }
    ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in routes)
    joins = [
        "LEFT JOIN basic_facilities_sections s ON s.route_id = r.id AND s.del_flag = 0",
        "LEFT JOIN ai_problem p ON p.sections_id = s.id AND p.del_flag = 0",
    ]
    damage_select = "COUNT(DISTINCT p.id) AS disease_count"
    if plan["slots"].get("damage_matches"):
        joins.append(_damage_dict_join(join_word="LEFT JOIN"))
        values = ", ".join(
            "'" + item["problem_type"].replace("'", "''") + "'" for item in plan["slots"]["damage_matches"]
        )
        damage_select = (
            f"COUNT(DISTINCT CASE WHEN b.problem_type IN ({values}) THEN p.id END) AS disease_count"
        )
    return (
        "SELECT r.short_name AS type, "
        f"{_TECH_GRADE_CASE} AS tech_grade_name, MAX(s.tech_grade) AS tech_grade, "
        "MAX(s.lane_count) AS lane_count, MAX(s.surface_width) AS surface_width, "
        f"{damage_select} "
        f"FROM basic_facilities_routes r {' '.join(joins)} "
        f"WHERE r.del_flag = 0 AND r.id IN ({ids}) "
        "GROUP BY r.id, r.short_name LIMIT 10"
    )


def _build_spatial_split_template(plan: dict[str, Any]) -> str | None:
    """前 N 米 / 后 N 米 的分段对比：前段 [0,N)，后段 [N,2N)。"""
    span = plan["slots"].get("spatial_split")
    if not span or not plan["slots"].get("route_match"):
        return None
    slots = plan["slots"]
    joins = ["JOIN basic_facilities_routes r ON p.routes_id = r.id"]
    where = ["p.del_flag = 0", "r.del_flag = 0", _route_sql(plan), f"p.pile_distance < {int(span) * 2}"]
    if slots.get("damage_matches"):
        joins.append(_damage_dict_join())
        values = ", ".join(
            "'" + item["problem_type"].replace("'", "''") + "'" for item in slots["damage_matches"]
        )
        where.append(f"b.problem_type IN ({values})")
    if slots.get("time_start"):
        where.append(f"p.collection_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"p.collection_time < '{slots['time_end']}'")
    segment = (
        f"CASE WHEN p.pile_distance < {int(span)} THEN '前{int(span)}米' "
        f"ELSE '后{int(span)}米' END"
    )
    return (
        f"SELECT {segment} AS type, COUNT(*) AS count "
        f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)} "
        f"GROUP BY {segment} ORDER BY MIN(p.pile_distance) LIMIT 10"
    )


def _build_patrol_top_template(plan: dict[str, Any]) -> str | None:
    """「哪一次巡查发现的问题最多」这类 TOP 问题：按巡查任务分组取前几名。

    巡查表没有路线字段，必须经病害回连（ai_problem.patrol_id），这也是模型最容易答错的地方
    （实测答成全库病害数 1702）。
    **并列要全列出**：实测 7 月西乡段有 6 次巡查都是 104 处，只回一条是错的。
    """
    if plan["shape"] != "top":
        return None
    slots = plan["slots"]
    message = plan.get("message", "")
    joins = ["JOIN ai_problem p ON p.patrol_id = t.id"]
    where = ["t.del_flag = 0", "p.del_flag = 0"]
    if slots.get("route_match"):
        route_id = slots["route_match"]["id"].replace("'", "''")
        joins.append("JOIN basic_facilities_routes r ON p.routes_id = r.id")
        where.append("r.del_flag = 0")
        where.append(f"r.id = '{route_id}'")
    if slots.get("time_start"):
        where.append(f"p.collection_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"p.collection_time < '{slots['time_end']}'")
    # 明确问了"前 N 次"就按 N 取；只说"哪次最多"就把**并列的全部**列出来
    top_n = re.search(r"前\s*(\d+)", message)
    if top_n:
        return (
            "SELECT t.task_number, t.patrol_name, COUNT(*) AS count "
            f"FROM ai_maintain_patrol t {' '.join(joins)} "
            f"WHERE {' AND '.join(where)} "
            f"GROUP BY t.id, t.task_number, t.patrol_name ORDER BY count DESC LIMIT {int(top_n.group(1))}"
        )
    return (
        "SELECT task_number, patrol_name, count FROM ("
        "SELECT t.task_number, t.patrol_name, COUNT(*) AS count, "
        "RANK() OVER (ORDER BY COUNT(*) DESC) AS rnk "
        f"FROM ai_maintain_patrol t {' '.join(joins)} "
        f"WHERE {' AND '.join(where)} "
        "GROUP BY t.id, t.task_number, t.patrol_name) x WHERE x.rnk = 1 "
        f"ORDER BY task_number LIMIT {settings.sql_max_rows}"
    )


def _build_pg_template(plan: dict[str, Any]) -> str | None:
    shape = plan["shape"]
    slots = plan["slots"]
    table = slots.get("pg_table", "gps_points")
    where: list[str] = []
    if slots.get("time_start"):
        where.append(f"create_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"create_time < '{slots['time_end']}'")
    if slots.get("device_id"):
        device_id = str(slots["device_id"]).replace("'", "''")
        where.append(f"device_id = '{device_id}'")
    where_sql = " AND ".join(where) if where else "TRUE"
    limit = settings.sql_max_rows

    if shape == "count":
        return f"SELECT COUNT(*) AS count FROM {table} WHERE {where_sql} LIMIT 1"
    if shape == "trend":
        return (
            f"SELECT to_char(create_time, 'YYYY-MM-DD') AS day, COUNT(*) AS count "
            f"FROM {table} WHERE {where_sql} GROUP BY day ORDER BY day LIMIT {limit}"
        )
    if shape in {"list", "photo", "detail"}:
        return (
            f"SELECT * FROM {table} WHERE {where_sql} "
            f"ORDER BY create_time DESC LIMIT {limit}"
        )
    return None


# ── 模板快速通道准入（docs/Wren主路径重构方案.md §3.2）──────────────────
# 模板是端到端的：一个模板从自然语言直接拼出完整 SQL，所以它只会看关键词，不判断
# SQL 形状。实测被它截胡的题（"数量最多的病害类型"被当成 top 明细、"泛油占百分之多少"
# 走成病害明细、"每公里分段"走成整体密度）全是这个原因。
#
# 所以模板只保留"单主体 + 单度量 + 不用分组 + 没有派生计算"的最简查询，
# 其余一律交给 Wren 语义层（LLM 只选模型和字段，JOIN/字典/JSON 由编译器补）。
# 派生计算：答案不是任何一个原始字段，得先算一步（后置计算），模板表达不了。
# 模板能表达的**分组维度**（这是"模板会怎么写 GROUP BY"的能力清单，不是用户说法清单）。
# 语义层给出别的分组维度（工单状态/来源/路格…）就交给 Wren，别让模板瞎猜。
_TEMPLATE_SAFE_GROUPS: frozenset[str] = frozenset(
    {
        "",
        "damage_type",
        "route",
        "section",
        "direction",
        "image_presence",
        "category",
        "month",
        "day",
        "week",
        "hour",
        "device",
        "confidence",
    "area_bucket",
        "pile_bucket",
        "manage_dept",
    }
)


def _template_unsafe_condition(plan: dict[str, Any]) -> bool:
    """语义层抽到了模板表达不了的条件/维度吗？（是 → 不许走模板）"""
    slots = plan.get("slots") or {}
    for item in slots.get("semantic_conditions") or []:
        # 模板渲染条件走的是 _CONDITION_FIELDS（同一张自动生成的字段登记表）——
        # 登记表里有的字段模板就能拼，没有的（将来加的奇怪条件）一律交给 Wren。
        if str(item.get("field") or "") not in _CONDITION_FIELDS:
            return True
    group_by = str(slots.get("group_by") or "")
    if group_by and group_by not in _TEMPLATE_SAFE_GROUPS:
        return True
    return False


def is_patrol_route_overview_plan(plan: dict[str, Any]) -> bool:
    """语义计划是不是“巡查任务级路线/病害概览”。

    这是模板准入条件，不是用户原话识别：要求语义层明确给出巡查主体、路线归属，
    以及任务编号/状态/里程中的至少两项，并要求统计病害。条件只允许时间与路线过滤，
    避免把普通的“巡查任务次数”或“病害列表”误套成概览模板。
    """
    if str(plan.get("kind") or "mysql") != "mysql":
        return False
    if str(plan.get("subject") or "") != "patrol":
        return False
    semantic = plan.get("semantic") or {}
    fields = {str(item) for item in semantic.get("output_fields") or []}
    metrics = {str(item) for item in semantic.get("metrics") or []}
    has_route = bool(fields & {"route_name", "route", "routes_id"})
    task_fields = fields & {
        "task_number",
        "patrol_number",
        "patrol_status",
        "patrol_mileage",
        "patrol_mileage_m",
    }
    asks_disease = bool(
        metrics & {"count", "disease_count"}
        or fields & {"disease_count", "problem_count"}
    )
    required = set(plan.get("required_conditions") or [])
    filters_ok = not (required - {"time_range", "route", "route_name"})
    if has_route and len(task_fields) >= 2 and asks_disease and filters_ok:
        return True
    # 问题优化器在规划前已明确补出的汇总维度，是被规划消费的显式输入，
    # 不能因为语义模型偶尔漏掉 route_name 就退回“只查任务数”的窄模板。
    optimization = plan.get("question_optimization") or {}
    dimensions = {
        str(item).strip()
        for item in optimization.get("required_dimensions") or []
        if str(item).strip()
    }
    groups = (
        {"任务次数", "巡查次数"},
        {"完成状态"},
        {"任务编号", "巡查编号"},
        {"巡检里程", "巡查里程", "里程"},
        {"已登记路线", "已登记线路"},
        {"未登记路线", "未登记线路"},
        {"病害数量", "检出病害数", "病害总数"},
        {"病害来源", "病害归属"},
    )
    return bool(
        plan.get("subject") == "patrol"
        and filters_ok
        and all(dimensions & group for group in groups)
    )


def is_patrol_route_overview_sql(sql: str | None) -> bool:
    """实际执行的 SQL 是不是任务级概览口径（按 SQL 结构判断）。"""
    compact = " ".join(str(sql or "").lower().split())
    return all(
        part in compact
        for part in (
            "from ai_maintain_patrol t",
            "left join ai_maintain_routes_patrol pr",
            "left join ai_problem p",
            "as patrol_mileage_m",
            "as route_name",
            "as disease_count",
        )
    )


def is_simple_single_query(plan: dict[str, Any]) -> bool:
    """能不能走模板快速通道（docs/Wren主路径重构方案.md §3.2）。

    **判断完全由语义层给**（docs/项目智能化升级方案.md：判断权从规则拿给 LLM）：
    语义层说"单主体、单度量、没有分组、没有派生计算"才走模板；说不清就给 Wren。
    关键词规则只在语义层没有结论时兜底（LLM 超时/返回非法）。
    """
    # 分档对比（LLM 识别的 bucket_compare）走确定性模板：分界线由代码按数据算，
    # 而且回答必须带依据；交给 LLM 自己写中位数反而容易算错、丢依据。
    if _is_bucket_plan(plan):
        return True
    if is_patrol_route_overview_plan(plan):
        return True
    # 冷门条件闸：模板只认识"路线/桩号/病害类型/时间/上下行"这几样，
    # 用户问题里的其他条件（收藏、关注、工单状态、病害来源、路格、历史损坏程度…）
    # 它一个都表达不了——实测"西乡段收藏的病害有多少处"被模板答成"共 911 处"。
    # 只要语义层抽出了模板不认识的条件，就不许走模板，交给 Wren/LLM 去写。
    if _template_unsafe_condition(plan):
        return False
    # 桩号段分布同理：分段写法由代码定死（见 _build_pile_bucket_template），
    # 否则 LLM 会把整段里程缩成 "K0" 一段。
    if str(plan["slots"].get("group_by") or "") == "pile_bucket":
        return True
    # "给我看看这些病害的照片"这类看图诉求：必须走能取出 problem_url 的列表模板。
    # 实测被语义层当成聚合（SQL 变成 SUM(problem_url IS NOT NULL) 计数），
    # 结果一张图都取不到，回"没有找到相关病害图片"。
    if plan.get("shape") == "photo":
        return True
    # 巡查"哪次最多"这类 TOP：模板里写死了关联链路（巡查表没有路线字段，必须经病害回连）
    # 且保证并列全列出（实测 6 次巡查并列 104 处时 LLM 只回一条）。
    if plan.get("subject") == "patrol" and plan.get("shape") == "top":
        return True
    if plan.get("kind") != "mysql":
        return False
    semantic = plan.get("semantic") or {}
    if not semantic:
        return False
    # 多路线对比 / 多口径对比
    if plan.get("multi_route") or plan.get("comparison"):
        return False
    # 里程桩点/路段/巡检/工单/资产等主体：模板本来就要弃权（答过"病害数"这种错主体）
    if plan.get("subject", "disease") != "disease":
        return False
    # 语义层说：要分组 / 要派生计算 / 要多个指标 → 都不算简单查询
    if str(semantic.get("shape") or "") in {"distribution", "trend"}:
        return False
    if semantic.get("group_by") or semantic.get("derived"):
        return False
    if len(semantic.get("metrics") or []) > 1:
        return False
    shape = str(semantic.get("shape") or plan.get("rule_shape") or plan["shape"])
    return shape in {"count", "sum", "avg", "list", "photo", "detail", "top"}

# ── 抽象概念分档（docs/抽象概念识别方案-LLM语义识别.md）────────────────────
# "是哪个维度、两档叫什么"由 LLM 语义识别（slots.bucket_*），这里只做两件事：
#   ① 按数据算出分界线（中位数，不写死阈值）② 把分界线写进回答，别黑盒。
# 维度**不写死**：LLM 给什么指标就按什么算（下面这张表只是"指标名 → SQL 表达式"的翻译）。
# 分档指标**不再枚举**：语义层给的是"按哪个维度分档"（列名或特殊键），
# 代码只负责把列名翻成 SQL 表达式（字段登记表里有）＋给个默认档名。
# 以前这里写死了 8 个指标，新加一个可比较的列（比如病害长度之外的尺寸列）就分不了档。
_BUCKET_METRIC_DEFAULT_LABELS: dict[str, tuple[str, str, str]] = {
    # 指标 → (低档默认名, 高档默认名, 中文名兜底)
    "collection_time": ("早的", "晚的", "采集时间"),
    "confidence": ("低的", "高的", "AI 识别置信度"),
    "pile_distance": ("靠前的", "靠后的", "桩号位置"),
    "area": ("小的", "大的", "病害面积"),
    "disease_length": ("短的", "长的", "病害长度"),
    "area_width": ("窄的", "宽的", "病害宽度"),
    "area_height": ("矮的", "高的", "病害高度"),
}

# 语义层回中文名/近义写法时，翻译成上面的规范键（只是把 LLM 的判断对上物理列）
_BUCKET_METRIC_ALIASES: dict[str, str] = {
    "time": "collection_time",
    "采集时间": "collection_time",
    "时间": "collection_time",
    "date": "collection_time",
    "新旧": "collection_time",
    "早晚": "collection_time",
    "桩号": "pile_distance",
    "桩号位置": "pile_distance",
    "位置": "pile_distance",
    "里程": "pile_distance",
    "里程位置": "pile_distance",
    "一头两头": "pile_distance",
    "前段后段": "pile_distance",
    "面积": "area",
    "大小": "area",
    "置信度": "confidence",
    "可信度": "confidence",
    "准确度": "confidence",
    "长度": "disease_length",
    "病害长度": "disease_length",
    "宽度": "area_width",
    "高度": "area_height",
}


def _bucket_metric_spec(plan: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    """从语义识别结果里取分档规格：(指标名, SQL表达式, 中文名, 低档名, 高档名)。

    粒度/聚合由 bucket_granularity / bucket_agg 表达（见 _build_bucket_template）——
    "密集/稀疏"不是新模板，它就是同一个算子在"每公里/每路段"粒度上跑一遍。
    """
    slots = plan.get("slots") or {}
    metric = str(slots.get("bucket_metric") or "").strip().lower()
    # 语义层偶尔会回中文名或近义写法（"采集时间"/"桩号"/"时间"）——这里做**翻译**，
    # 不是猜用户文本：LLM 已经判断出"按哪个维度分档"，代码只是把它对上物理列。
    canonical = _BUCKET_METRIC_ALIASES.get(metric, metric)
    # 表达式来自**字段登记表**（自动：库里有什么可比较的列就能按它分档），
    # 代码只保留"档名叫什么"的默认值和中文名兜底。
    field = _CONDITION_FIELDS.get(canonical)
    # 能分档的列 = 数值列 或 时间列（"老问题/新问题"按时分档）
    column_type = _FIELD_TYPES.get(canonical, "")
    bucketable = bool(field) and (
        field[1] or "time" in column_type or "date" in column_type
    )
    if not bucketable:
        return None
    expr = field[0]
    low_default, high_default, label_fallback = _BUCKET_METRIC_DEFAULT_LABELS.get(
        canonical, ("低的", "高的", _CONDITION_LABELS.get(canonical, canonical))
    )
    label = _CONDITION_LABELS.get(canonical) or label_fallback
    return (
        canonical,
        expr,
        label,
        str(slots.get("bucket_low_label") or low_default),
        str(slots.get("bucket_high_label") or high_default),
    )


# 分档"一条"是什么（粒度）→ 分组键表达式；"比它的什么值" → 聚合表达式
_BUCKET_GRANULARITY_KEYS: dict[str, tuple[str, str]] = {
    # 粒度 → (分组键, 说明)
    "row": ("", "单条病害"),
    "kilometer": ("FLOOR(p.pile_distance / 1000)", "每个公里段"),
    "section": ("p.sections_id", "每个路段"),
    "route": ("p.routes_id", "每条路线"),
}


def _is_bucket_plan(plan: dict[str, Any]) -> bool:
    """这次是不是"分档对比"，而且我们能确定性执行（不用交给 LLM 自由发挥）。

    粒度不是单条病害时（每公里/每路段），被分档的值就是聚合值，不依赖指标映射；
    粒度是单条病害时才需要"按哪个字段分"。
    """
    slots = plan.get("slots") or {}
    granularity = str(slots.get("bucket_granularity") or "")
    if granularity and granularity != "row":
        return granularity in _BUCKET_GRANULARITY_KEYS
    return _bucket_metric_spec(plan) is not None
_BUCKET_AGG_EXPRS: dict[str, tuple[str, str]] = {
    "count": ("COUNT(*)", "病害数量"),
    "area_sum": ("SUM(p.area)", "总面积"),
    "avg_area": ("AVG(p.area)", "平均面积"),
    "confidence_avg": (
        "AVG(CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)))",
        "平均置信度",
    ),
}


def _bucket_boundary(
    expr: str,
    plan: dict[str, Any],
    group_key: str,
    agg_expr: str,
) -> Any:
    """算分界线（中位数）。

    粒度=单条病害 → 直接对病害指标取中位数；
    粒度=每公里/每路段 → 先按单位聚合（每单位有多少处/总面积…），再对这些聚合值取中位数。
    这样"密集/稀疏"和"新旧/大小"共用同一套代码，不需要各写一个模板。
    """
    if not group_key:
        return _metric_median(expr, plan)
    from .db import db_manager

    joins, where = _metric_filters(plan)
    if "JSON_EXTRACT" in agg_expr:
        where.append("p.pic_position IS NOT NULL")
    from_sql = f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    try:
        total = int(
            db_manager.query(
                "mysql",
                f"SELECT COUNT(*) AS c FROM (SELECT {group_key} AS k {from_sql} GROUP BY {group_key}) t",
            )["rows"][0]["c"]
        )
        if total <= 0:
            return None
        row = db_manager.query(
            "mysql",
            f"SELECT bucket_value FROM (SELECT {group_key} AS bucket_key, {agg_expr} AS bucket_value "
            f"{from_sql} GROUP BY {group_key}) t "
            f"ORDER BY bucket_value LIMIT 1 OFFSET {total // 2}",
        )["rows"]
        return row[0]["bucket_value"] if row else None
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "bucket_boundary_failed", exc, metric=group_key[:60])
        return None


def _metric_median(expr: str, plan: dict[str, Any]) -> Any:
    """按数据算出分界线（中位数）——不写死阈值，数据变了自动跟着变。"""
    from .db import db_manager

    joins, where = _metric_filters(plan)
    from_sql = f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    try:
        total = int(db_manager.query("mysql", f"SELECT COUNT(*) AS c {from_sql}")["rows"][0]["c"])
        if total <= 0:
            return None
        row = db_manager.query(
            "mysql",
            f"SELECT {expr} AS v {from_sql} AND {expr} IS NOT NULL "
            f"ORDER BY {expr} LIMIT 1 OFFSET {total // 2}",
        )["rows"]
        return row[0]["v"] if row else None
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "median_failed", exc, metric=expr[:60])
        return None


def _build_bucket_template(plan: dict[str, Any]) -> str | None:
    """分档对比（通用算子）：粒度 × 指标的任意组合，都走这一条路径。

    粒度=单条病害 → 直接按病害指标分档（新旧/大小/高低/一头两头）；
    粒度=每公里/每路段 → 先把每条聚合出来，再按这些值的中位数分档
    （"密集的路段多还是稀疏的路段多"就是它，不需要再写一个模板）。
    新维度不用改代码：语义层从 schema 里挑指标，这里只管拼 SQL。
    """
    slots = plan["slots"]
    granularity = str(slots.get("bucket_granularity") or "row")
    agg = str(slots.get("bucket_agg") or "count")
    if granularity not in _BUCKET_GRANULARITY_KEYS or agg not in _BUCKET_AGG_EXPRS:
        return None
    group_key, unit_label = _BUCKET_GRANULARITY_KEYS[granularity]
    agg_expr, agg_label = _BUCKET_AGG_EXPRS[agg]
    if granularity == "row":
        # 单条病害分档：必须知道按哪个病害字段分（新旧/大小/高低…）
        spec = _bucket_metric_spec(plan)
        if not spec:
            return None
        _, expr, label, low_label, high_label = spec
    else:
        # 每公里/每路段分档：被分档的值就是"每个单位的聚合值"，指标名无关紧要
        # （语义层给 density/密集 之类都能用），标签按聚合口径生成
        expr = ""
        label = agg_label
        low_label = str(slots.get("bucket_low_label") or f"{unit_label}少的")
        high_label = str(slots.get("bucket_high_label") or f"{unit_label}多的")
    boundary = _bucket_boundary(expr, plan, group_key, agg_expr)
    if boundary in (None, ""):
        return None
    joins, where = _metric_filters(plan)
    if "JSON_EXTRACT" in expr:
        where.append("p.pic_position IS NOT NULL")
    from_sql = f"FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}"
    if granularity != "row":
        # 聚合值：计数取整、面积两位小数、置信度四位小数
        if agg == "count":
            boundary_text = f"{float(boundary):g}"
        elif agg == "confidence_avg":
            boundary_text = f"{float(boundary):.4f}"
        else:
            boundary_text = f"{float(boundary):.2f}"
    elif expr.endswith("collection_time"):
        boundary_text = str(boundary)[:19]
    elif "pile_distance" in expr:
        # 桩号分档（"集中在一头还是两头"）：分界线用 Kx+yyy 的写法，用户才看得懂
        try:
            from . import calibrate

            boundary_text = calibrate.fmt_pile(float(boundary))
        except Exception:  # noqa: BLE001
            boundary_text = f"{float(boundary):g} 米"
    elif "confidence" in expr:
        boundary_text = f"{float(boundary):.4f}"
    else:
        boundary_text = f"{float(boundary):g}"
    if granularity == "row":
        plan["slots"]["boundary_note"] = (
            f"按{label}中位数（{boundary_text}）分：小于它的算「{low_label}」，不小于的算「{high_label}」"
        )
        value_sql = expr
    else:
        plan["slots"]["boundary_note"] = (
            f"按{unit_label}的{agg_label}中位数（{boundary_text}）分："
            f"不到它的算「{low_label}」，达到的算「{high_label}」"
        )
        # 粒度不是单条病害时：先按单位聚合，再拿聚合值分档
        value_sql = "t.bucket_value"
    plan["slots"]["metric_labels"] = {
        "metric": "分组依据",
        "low": low_label,
        "high": high_label,
        "total": "同口径总数",
    }
    if granularity == "row":
        return (
            f"SELECT '{plan['slots']['boundary_note']}' AS metric, "
            f"SUM(CASE WHEN {value_sql} < '{boundary}' THEN 1 ELSE 0 END) AS low, "
            f"SUM(CASE WHEN {value_sql} >= '{boundary}' THEN 1 ELSE 0 END) AS high, "
            # 同口径总数一起查出来：两档之和必须等于它，不等就说明 SQL 漏了过滤条件
            # （实测见过"851+851=1702"——查成全库了却没人发现）
            f"COUNT(*) AS total "
            f"{from_sql} LIMIT 1"
        )
    # 粒度=每公里/每路段：先在子查询里按单位聚合，再按"每个单位的指标值"分档，
    # total 是**单位个数**（多少个路段落在这两档里），所以 low+high 必然等于 total。
    return (
        f"SELECT '{plan['slots']['boundary_note']}' AS metric, "
        f"SUM(CASE WHEN {value_sql} < '{boundary}' THEN 1 ELSE 0 END) AS low, "
        f"SUM(CASE WHEN {value_sql} >= '{boundary}' THEN 1 ELSE 0 END) AS high, "
        f"COUNT(*) AS total "
        f"FROM (SELECT {group_key} AS bucket_key, {agg_expr} AS bucket_value "
        f"{from_sql} GROUP BY {group_key}) t LIMIT 1"
    )


# 代码模板构造器 → 人话口径（人工确认卡里要让用户看见"这条会怎么算"）。
# 顺序与 build_template_sql 内部的尝试顺序一致，用于反查"这条 SQL 是谁拼的"。
_CODE_TEMPLATE_LABELS: tuple[tuple[str, str], ...] = (
    ("_build_pg_template", "GPS/倾角库查询"),
    ("_build_bucket_template", "按指标中位数分两档对比"),
    ("_build_multi_route_template", "多路线对比（每条路一行）"),
    ("_build_known_unknown_template", "已识别类型 vs 未识别类型分别计数"),
    ("_build_ratio_template", "占比计算（部分 ÷ 总数）"),
    ("_build_multi_metric_template", "一问多答（多个口径各占一列）"),
    ("_build_table_count_template", "台账/对象计数"),
    ("_build_pile_bucket_template", "按桩号分段统计"),
    ("_build_route_info_template", "路线台账信息"),
    ("_build_per_km_template", "每公里指标（密度/间距）"),
    ("_build_threshold_count_template", "数值阈值筛选后计数"),
    ("_build_coverage_compare_template", "覆盖范围对比"),
    ("_build_other_routes_template", "不属于指定路线的病害"),
    ("_build_density_template", "病害密度"),
    ("_build_generic_metric_template", "通用「维度 × 指标」聚合"),
    ("_build_patrol_task_template", "巡查任务统计"),
    ("_build_patrol_top_template", "巡查任务 TOP"),
    ("_build_patrol_group_template", "巡查任务分组统计"),
    ("_build_spatial_split_template", "前 N 米 / 后 N 米分段对比"),
    ("_build_metric_top_template", "按指标取极值（并列出并列）"),
    ("_build_average_template", "平均值 + 超过平均值的条数"),
    ("_build_distinct_pile_template", "不同桩号计数"),
    ("_build_quality_template", "时间跨度/字段缺失等质量统计"),
)


def template_builder_label(plan: dict[str, Any], sql: str) -> tuple[str, str]:
    """反查这条 SQL 是哪个代码构造器拼出来的（构造器都是纯函数，重跑一遍对比即可）。"""
    if not sql:
        return "", ""
    # 在副本上重跑：构造器会顺手写回计划（subject 等），不能让"反查标签"这一步污染真计划
    plan = _template_scope(plan)
    labels = dict(_CODE_TEMPLATE_LABELS)
    # 先按已知顺序试，再兜底扫描所有 _build_* 函数（新增构造器不用改这张表也能标出来）
    names = [name for name, _ in _CODE_TEMPLATE_LABELS]
    names += [
        name
        for name in list(globals())
        if name.startswith("_build_") and name not in labels and callable(globals().get(name))
    ]
    for name in names:
        builder = globals().get(name)
        if not callable(builder):
            continue
        try:
            built = builder(plan)
        except Exception:  # noqa: BLE001
            continue
        if built and built == sql:
            return name, labels.get(name, name)
    # 走到这儿说明它是 build_template_sql 里**内联**拼出来的（明细/列表兜底那几段）
    return "", "代码模板（内置明细/计数）"


def code_template_scope(plan: dict[str, Any], sql: str, label: str) -> str:
    """代码模板候选的人话口径：说清"按哪个列、怎么排序、取前几"。

    模糊指标（"尺寸"可能指面积/长度/宽度/高度）必须在这里点明，用户才看得出分歧。
    """
    parts: list[str] = [label or "确定性模板"]
    text = str(sql or "")
    columns = _column_cn_names()
    used: list[str] = []
    for column in ("area_width", "area_height", "area", "disease_length", "confidence", "pile_distance"):
        if column in text and columns.get(column):
            used.append(f"{columns[column]}（{column}）")
    # 只在 SQL 尾部找排序/分组/上限：前面那些是 CTE/子查询里的，不是最终口径
    tail = text[-220:]
    order = next(iter(re.findall(r"order\s+by\s+([a-z_\.]+)\s*(desc|asc)?", tail, re.I)[::-1]), None)
    if order:
        field = str(order[0]).split(".")[-1]
        direction = "降序" if str(order[1] or "").lower() == "desc" else "升序"
        parts.append(f"排序：{columns.get(field, field)}（{field}）{direction}")
    limit_hits = re.findall(r"limit\s+(\d+)", tail, re.I)
    if limit_hits:
        parts.append(f"最多返回 {limit_hits[-1]} 行")
    if used:
        parts.append("涉及的指标列：" + "、".join(used))
    group_hits = re.findall(r"group\s+by\s+([a-z_\.]+)", tail, re.I)
    group = group_hits[-1] if group_hits else ""
    if group:
        field = group.split(".")[-1]
        parts.append(f"分组：{columns.get(field, field)}（{field}）")
    return "；".join(parts)


@lru_cache(maxsize=4)
def _column_cn_names(kind: str = "mysql") -> dict[str, str]:
    """列名 → 中文注释（口径文案用），来自字段登记表。"""
    mapping: dict[str, str] = {}
    for row in _field_rows(kind):
        name = str(row.get("name") or "")
        label = str(row.get("label") or "")
        if name and label and name not in mapping:
            mapping[name] = label
    return mapping


def build_template_sql(plan: dict[str, Any]) -> str | None:
    """内置代码模板的唯一入口：**先按语义 plan 选模板，再只调那一个构造器**。

    以前是按顺序把 24 个构造器全试一遍、各自再用关键词判断该不该命中——那是端到端的
    穷举匹配。现在选模板交给 `preset_lib.match_builtin_templates`（只看 shape / 主体 /
    分组维度 / 指标 / 派生指标 / 布尔特征），声明里表达不了的就返回 None 交 Wren
    （docs/代码结构重构与模板外置方案.md §5.2 第 3 条）。
    """
    from . import preset_lib

    # 任务→路线范围题必须先出路线清单；巡检计数模板只给总数，会直接答丢用户第一问。
    if plan.get("route_scope_from_patrol") and not is_patrol_route_overview_plan(plan):
        return None
    work = _template_scope(plan)
    for item in preset_lib.match_builtin_templates(work, limit=3):
        sql = build_template_sql_for(work, str(item.get("builder") or ""))
        if sql:
            _merge_template_annotations(plan, work)
            log_event(
                "nl2sql",
                "INFO",
                "builtin_template_matched",
                template=str(item.get("id")),
                reasons="；".join(item.get("reasons") or [])[:120],
            )
            return sql
    return None


def simple_route_group_mdl(plan: dict[str, Any]) -> str | None:
    """简单“病害按路线计数”的确定性 MDL-SQL。

    disease 模型已通过 ref_sql 提供 route_name；直接用它可以避免把 route 模型
    完整材料化并计算 route_length_km。这里只接管无额外过滤的窄场景。
    """
    if plan.get("kind") != "mysql":
        return None
    if str(plan.get("subject") or "") != "disease":
        return None
    semantic = plan.get("semantic") or {}
    group_by = str(
        semantic.get("group_by") or (plan.get("slots") or {}).get("group_by") or ""
    )
    if group_by != "route":
        return None
    if plan.get("required_conditions") or plan.get("comparison"):
        return None
    if (plan.get("slots") or {}).get("semantic_conditions"):
        return None
    metrics = {str(item) for item in semantic.get("metrics") or []}
    if metrics - {"count"} or semantic.get("derived"):
        return None
    return (
        "SELECT disease.route_name AS 路线名称, COUNT(*) AS 病害数量\n"
        "FROM disease\n"
        "WHERE disease.del_flag = 0 AND disease.route_name IS NOT NULL\n"
        "GROUP BY disease.route_name\n"
        "ORDER BY 病害数量 DESC\n"
        f"LIMIT {settings.sql_max_rows}"
    )


def simple_patrol_route_list_mdl(plan: dict[str, Any]) -> str | None:
    """任务登记涉及路线的确定性 MDL-SQL（以路线关联表为准）。"""
    if plan.get("kind") != "mysql" or not plan.get("route_scope_from_patrol"):
        return None
    if str(plan.get("shape") or "") not in {"list", "detail"}:
        return None
    required = set(plan.get("required_conditions") or [])
    if required - {"time_range"}:
        return None
    slots = plan.get("slots") or {}
    where = [
        "t.del_flag = 0",
        "pr.del_flag = 0",
        "r.del_flag = 0",
    ]
    if slots.get("time_start"):
        where.append(f"t.create_time >= '{slots['time_start']}'")
    if slots.get("time_end"):
        where.append(f"t.create_time < '{slots['time_end']}'")
    return (
        "SELECT r.short_name AS 路线名称, COUNT(DISTINCT d.id) AS 病害数量\n"
        "FROM patrol t\n"
        "JOIN patrol_route pr ON pr.patrol_id = t.id\n"
        "JOIN route r ON pr.routes_id = r.id\n"
        "LEFT JOIN disease d ON d.patrol_id = t.id\n"
        "  AND d.routes_id = r.id AND d.del_flag = 0\n"
        f"WHERE {' AND '.join(where)}\n"
        "GROUP BY r.id, r.short_name\n"
        "ORDER BY 病害数量 DESC, r.short_name\n"
        f"LIMIT {settings.sql_max_rows}"
    )


def _merge_template_annotations(plan: dict[str, Any], work: dict[str, Any]) -> None:
    """把构造器在副本上写的结果注解合并回原计划（身份字段不动）。"""
    slots = work.get("slots") or {}
    target = plan.setdefault("slots", {})
    for key in _TEMPLATE_ANNOTATION_KEYS:
        if key in slots:
            target[key] = slots[key]
    # 分档模板会把分界线说明写进 slots；空列说明等在 plan 顶层
    for key in ("empty_column_note",):
        if key in work:
            plan[key] = work[key]


# 构造器允许顺手写回的**结果注解**（回答里要用）：分档说明、指标中文名、口径改写说明…
# 其余字段（尤其 subject/shape/comparison 这类"这句话问的是什么"的身份字段）一律不许被
# 构造器改：构造器是"翻译器"，不是"理解器"。实测 `_build_route_info_template` 会写
# `plan["subject"]="section"`、`_build_coverage_compare_template` 写回 "route"，
# 结果同一份计划第一次匹配到路线系模板、第二次就再也匹配不到——推荐时好时坏，
# 落点还会从路线表被带到路段表（docs 里说的"同一问题推荐不一样"）。
_TEMPLATE_ANNOTATION_KEYS = (
    "boundary_note",
    "metric_labels",
    "group_note",
    "empty_column_note",
    "patrol_overview",
)


def _template_scope(plan: dict[str, Any]) -> dict[str, Any]:
    """构造器只在**计划副本**上跑，跑完只把结果注解合并回去。"""
    return copy.deepcopy(plan)


def build_template_sql_for(plan: dict[str, Any], builder: str) -> str | None:
    """只调用**语义匹配选中的那一个**构造器（不再按顺序穷举试）。

    选模板由 preset_lib.match_builtin_templates 按 plan 的语义字段决定
    （docs/代码结构重构与模板外置方案.md §5.2 第 3 条：不允许按用户原话关键词端到端匹配）。
    """
    name = str(builder or "").strip()
    func = _builtin_builders().get(name)
    if not callable(func):
        # 名字写错/实现被删：以前只返回 None，静默落到 Wren，谁都发现不了。
        log_error("nl2sql", "builtin_builder_missing", KeyError(name or "(空)"), builder=name)
        return None
    try:
        return func(plan)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "template_builder_failed", exc, builder=name)
        return None


def _semantic_decided(plan: dict[str, Any]) -> bool:
    """LLM 语义解析是否给出了可用判断（shape / 指标 / 派生 / 分组 / 输出字段任一）。

    构造器里的关键词判断据此降级成**兜底**：语义在就听语义的，语义缺失（LLM 没跑或失败）
    才回头看措辞。原则见 docs/项目智能化升级方案.md——判断权在 LLM，关键词不许抢先。
    """
    semantic = plan.get("semantic") or {}
    return bool(
        semantic.get("shape")
        or semantic.get("metrics")
        or semantic.get("derived")
        or semantic.get("group_by")
        or semantic.get("output_fields")
    )


def _builtin_builders() -> dict[str, Any]:
    """内置模板构造器注册表：`_index.yaml` 里的 `builder` 名字 → 实现。"""
    return {
        name: obj
        for name, obj in globals().items()
        if name.startswith("_build_") and name.endswith("_template") and callable(obj)
    }


def builtin_registry_issues() -> list[dict[str, str]]:
    """注册表自检：声明了 builder 却找不到实现 / 实现了却没人指向。

    选模板现在完全由 `resources/sql_templates/_index.yaml` 驱动，而 builder 名字写错、
    或者条目忘写 builder 时，`build_template_sql` 只会静默落到 Wren——现在把它显式列出来，
    管理页/日志能一眼看到配置漏了哪条。
    """
    from .resource_loader import load_yaml

    registry = (load_yaml("sql_templates/_index.yaml", {}) or {}).get("templates") or []
    builders = _builtin_builders()
    referenced = {
        str((item or {}).get("builder") or "")
        for item in registry
        if str((item or {}).get("source") or "builtin") == "builtin"
    }
    issues: list[dict[str, str]] = []
    for item in registry:
        if str((item or {}).get("source") or "builtin") != "builtin":
            continue
        template_id = str((item or {}).get("id") or "")
        name = str((item or {}).get("builder") or "")
        if not name:
            issues.append({"id": template_id, "issue": "条目没声明 builder（永远不会被选中）"})
        elif name not in builders:
            issues.append({"id": template_id, "issue": f"builder 找不到实现：{name}"})
    for name in sorted(builders):
        if name not in referenced:
            issues.append({"id": name, "issue": "有实现但没有注册条目（不会被选中）"})
    return issues


def _db_markers() -> dict[str, tuple[str, ...]]:
    """库名从配置读（换库名不用改代码）。"""
    return {
        "mysql": ("mysql", settings.mysql_database),
        "postgresql": ("postgresql", settings.pg_database),
        "seaweedfs": ("seaweedfs",),
    }


def _filter_chunks_for_db(chunks: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    markers = _db_markers().get(kind, (kind,))
    filtered: list[dict[str, Any]] = []
    for chunk in chunks:
        doc = str(chunk.get("document_keyword") or "").lower()
        if "总览" in doc:
            continue
        content = str(chunk.get("content") or "").lower()
        if any(marker in doc for marker in markers) or any(marker in content for marker in markers):
            filtered.append(chunk)
    return filtered


def _retrieve_chunks(
    question: str,
    dataset_name: str,
    limit: int = 6,
) -> list[dict[str, Any]]:
    try:
        dataset = find_dataset(dataset_name)
    except RAGError:
        # 字段字典/示例召回是增强信息，不是执行数据库查询的前置条件。
        return []
    if not dataset or not dataset.get("id"):
        return []
    use_kg = dataset_name in settings.ragflow_graphrag_datasets
    try:
        chunks = retrieve(
            query_rewrite.rewrite(question),
            [str(dataset["id"])],
            use_kg=use_kg,
        )[:limit]
    except RAGError:
        if use_kg:
            try:
                chunks = retrieve(query_rewrite.rewrite(question), [str(dataset["id"])])[:limit]
            except RAGError:
                return []
        else:
            return []
    if use_kg and not chunks:
        try:
            chunks = retrieve(query_rewrite.rewrite(question), [str(dataset["id"])])[:limit]
        except RAGError:
            return []
    for chunk in chunks:
        chunk["dataset_name"] = dataset_name
    return chunks


def _context_from_dataset(question: str, dataset_name: str, limit: int = 6) -> str:
    chunks = _retrieve_chunks(question, dataset_name, limit)
    return "\n\n".join(chunk.get("content", "") for chunk in chunks)


def _selected_schema_text(kind: str, models: set[str]) -> str:
    """物理 SQL 路径的"第二步"：只给选中的表（+JOIN 闭包）的列名/类型/注释。

    以前物理路径吃的是 RAGFlow 检索出来的字段字典文本，全量 592 列会被 [:16000] 截断，
    后面的表和冷门字段直接消失。这里改用**实时表结构**，选中的表一列不少。
    """
    from . import wren_engine

    tables = wren_engine.physical_tables(kind, models)
    if not tables:
        return ""
    lines = ["本次涉及的表（只引用这里的列名；没有的列不要编）："]
    for table in _catalog(kind).get("tables") or []:
        name = str(table.get("table") or "")
        if name not in tables:
            continue
        comment = str(table.get("comment") or "").strip()
        lines.append(f"- {name}{f'（{comment}）' if comment else ''}")
        for column in table.get("columns") or []:
            note = str(column.get("comment") or "").strip()
            lines.append(f"    {column.get('name')} {column.get('type')} {note}")
    return "\n".join(lines)


def _schema_hint(kind: str) -> str:
    """字段速查表：保证 LLM 认识“桩号/面积/置信度”这类业务词对应的真实列名。"""
    try:
        return clarify.compact_fields(kind)
    except Exception:
        return ""


def _business_hint() -> str:
    """业务词 → 真实列/表的映射。

    字段字典里有列名，但猜不到「置信度藏在 pic_position 的 JSON 里」「设备号是 device_code
    不是 vehicle_id」「里程桩点在另一张表」这类事，实测这些是丢分最集中的地方。
    """
    return (
        "业务词 → 真实列/表映射（必须照这个写，不要臆造列名）：\n"
        "- 桩号/里程：ai_problem.pile_number（文本桩号）、ai_problem.pile_distance（米，整数）\n"
        "- 面积：ai_problem.area（㎡）；病害长度："
        "CASE WHEN disease_length > 0 THEN disease_length "
        "ELSE GREATEST(COALESCE(area_height, 0), COALESCE(area_width, 0)) END（米）\n"
        "- 置信度/可信度：没有 confidence 列，它在图片定位 JSON 里，写法 "
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(pic_position, '$.confidence')) AS DECIMAL(10,6))，"
        "值是字符串，比较前必须 CAST\n"
        "- 设备号/采集设备：ai_problem.device_code（vehicle_id 是车辆 id，不是设备号）\n"
        "- 里程桩点/桩点/标桩：basic_facilities_sections_pile_detail d "
        "JOIN basic_facilities_sections s ON d.sections_id = s.id "
        "JOIN basic_facilities_routes r ON s.route_id = r.id\n"
        "- 路段/几段：basic_facilities_sections（route_id → basic_facilities_routes.id）\n"
        "- 路线属性（技术等级 tech_grade、车道数 lane_count、路面宽度 surface_width、路面类型 surface_type）"
        "在 basic_facilities_sections 表，不在 basic_facilities_routes："
        "basic_facilities_routes r JOIN basic_facilities_sections s ON s.route_id = r.id；"
        "病害所属路段是 ai_problem.sections_id → basic_facilities_sections.id。"
        "写 routes.tech_grade 一定会报 Unknown column\n"
        "- 巡检/巡查：统计某路线被巡查多少次，用 ai_maintain_patrol t "
        "JOIN ai_maintain_routes_patrol pr ON pr.patrol_id = t.id "
        "JOIN basic_facilities_routes r ON pr.routes_id = r.id，"
        "用 COUNT(DISTINCT t.id)；统计某次巡查检出病害才用 ai_problem.patrol_id\n"
        "- 巡查「类型」是 ai_maintain_patrol.patrol_type（0定期巡查/5日常巡查/10专项巡查/15人工复核/20重点巡查），"
        "「方式」才是 patrol_method_type，两者别混用\n"
        "- 工单：ai_work_order w JOIN ai_problem p ON w.problem_id = p.id "
        "JOIN basic_facilities_routes r ON p.routes_id = r.id\n"
    )


def _damage_code_hint() -> str:
    """病害类型外键对照：ai_problem.problem_base_id = ai_problem_base.id。"""
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT DISTINCT id, problem_type FROM ai_problem_base "
            "WHERE problem_type IS NOT NULL AND problem_type <> '' "
            "AND (del_flag = 0 OR del_flag IS NULL) ORDER BY problem_code",
        )["rows"]
    except Exception:
        return ""
    pairs = [
        f"{row['id']}={row['problem_type']}"
        for row in rows
        if row.get("id") and row.get("problem_type")
    ]
    if not pairs:
        return ""
    return (
        "病害类型外键对照（ai_problem.problem_base_id = id；"
        "ai_problem.damage_type 是损坏程度，不能拿来关联）：\n" + "，".join(pairs[:80])
    )


def _route_literals_for(sql: str) -> list[tuple[str, str]]:
    """历史示例没有 spec：按库里真实路线名/ID 反查 SQL 里出现的具体值。

    只给已入池示例的清洗用（见 renormalize_examples），新示例走 spec 归一化。
    只在引号字面量里替换、且只认中文片段，避免把 basic_facilities_routes 里的
    ASCII 片段（路线 code）误当成路线名替换掉表名。
    """
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT id, code, short_name, full_name FROM basic_facilities_routes WHERE del_flag = 0",
        )["rows"]
    except Exception:  # noqa: BLE001
        return []
    literals = [match for match in re.findall(r"'([^']*)'", sql) if match.strip()]
    found: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("id"):
            found.add((str(row["id"]), "{route_id}"))
        for key in ("short_name", "full_name", "code"):
            name = str(row.get(key) or "").strip()
            if not name or name.isascii():
                continue
            for literal in literals:
                for piece in _route_pieces(literal, name):
                    if piece.isascii() or len(piece) < 2:
                        continue
                    found.add((piece, "{route_name}"))
    # 长的先替换，避免「西乡段」先被「西乡」替换成半截。
    return sorted(found, key=lambda item: len(item[0]), reverse=True)


def _normalize_sql_for_example(sql: str, spec: dict[str, Any] | None) -> str:
    """示例 SQL 入池前做条件归一化：把具体值换成占位符。

    见 docs/RAGFlow知识库精简方案.md §2.5 问题一：示例里带着「宝安大道西乡段」
    这种具体条件，LLM 会把它抄进下一次查询（LD1「两边加起来」就是这么错的）。
    多值维度要**编号**：`IN ('A','B')` 归一化成 `IN ('{route_id}', '{route_id_2}')`，
    不能两个都写成 `{route_id}` —— 那样回填时两条路线会被填成同一个 id
    （实测"…分别是几车道"的示例就是这么废掉的：回填直接失败/只回一条路线）。
    """
    normalized = sql
    spec = spec or {}
    route_names = [spec.get("route"), *(spec.get("routes") or [])]
    for text, placeholder in _numbered_placeholders(route_names, "route_name"):
        normalized = normalized.replace(f"'{text}'", "{" + placeholder + "}")
    damage = spec.get("damage_type") or []
    for text, placeholder in _numbered_placeholders(
        damage if isinstance(damage, list) else [damage], "damage_type"
    ):
        normalized = normalized.replace(f"'{text}'", "{" + placeholder + "}")
    pile = spec.get("pile_range")
    if isinstance(pile, (list, tuple)) and len(pile) == 2:
        normalized = _replace_pile_literals(normalized, int(pile[0]), int(pile[1]))
    time_range = spec.get("time_range")
    if isinstance(time_range, (list, tuple)) and len(time_range) == 2:
        normalized = normalized.replace(f"'{time_range[0]}'", "{time_start}")
        normalized = normalized.replace(f"'{time_range[1]}'", "{time_end}")
        # 月份桶写法（内置模板常用）：`DATE_FORMAT(x,'%Y-%m') = '2026-07'`。
        # 这种 SQL 里没有完整时间戳，只有 'YYYY-MM'，以前替换不到 → 月份永远焊死
        # （"7 月"的示例拿来问"8 月"改不动）。换成 {month}，回填用 time_range 的月份。
        month = str(time_range[0] or "")[:7]
        if re.fullmatch(r"\d{4}-\d{2}", month):
            normalized = re.sub(
                r"(DATE_FORMAT\([^)]*'%Y-%m'\)\s*=\s*)'\d{4}-\d{2}'",
                lambda match: match.group(1) + "{month}",
                normalized,
            )
            normalized = normalized.replace(f"'{month}'", "{month}")
    if spec.get("device_code"):
        normalized = normalized.replace(f"'{spec['device_code']}'", "{device_code}")
    if not spec:
        for literal, placeholder in _route_literals_for(sql):
            # 只替换**被引号包住的完整字面量**：以前是裸文本替换，"西乡" 会把 '西乡段'
            # 换掉一半，拼出 `'{route_name}段'` 这种坏 SQL（回填后必然 1064）。
            # 完整字面量匹配同时也保证「长的先替换」不被半截名字抢先。
            normalized = normalized.replace("'" + literal + "'", "{" + placeholder + "}")
    # 路线 ID 不进 spec（只留名字），按形状归一化：库里路线 id 都是 32 位十六进制。
    # 按出现顺序编号，不同 id 给不同占位符。
    literal_ids = [match.group(1) for match in re.finditer(r"'([0-9a-fA-F]{32})'", normalized)]
    for text, placeholder in _numbered_placeholders(literal_ids, "route_id"):
        normalized = normalized.replace(f"'{text}'", "{" + placeholder + "}")
    return normalized


def _replace_pile_literals(sql_text: str, low: int, high: int) -> str:
    """只把**桩号谓词里的**字面量换成占位符，绝不整段裸替换。

    以前是 `normalized.replace(str(int(pile[0])), "{pile_start}")`：桩号从 0 开始时
    等于把整条 SQL 里所有 `0` 都换掉 —— 实测把 `CAST(... AS DECIMAL(10,6))` 换成
    `DECIMAL(1{pile_start},6)`、`* 1000` 换成 `* 1{pile_start}{pile_start}{pile_start}`，
    这条示例换任何桩号都跑不了（docs/推荐模板链路Bug清单.md P0-3）。
    """
    columns = "|".join(
        re.escape(str(name)) for name in (_FIELD_FAMILIES.get("pile_number") or ("pile_number",))
    )
    if not columns:
        return sql_text
    column = rf"(?:`?\w+`?\.)?`?(?:{columns})`?"
    text = re.sub(
        rf"({column})\s+BETWEEN\s+{int(low)}\s+AND\s+{int(high)}\b",
        lambda match: f"{match.group(1)} BETWEEN {{pile_start}} AND {{pile_end}}",
        str(sql_text),
        flags=re.IGNORECASE,
    )
    # 两条单边谓词（`pile_distance >= 1802 AND pile_distance <= 2801`）也认
    text = re.sub(
        rf"({column})\s*>=\s*{int(low)}\b",
        lambda match: f"{match.group(1)} >= {{pile_start}}",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(
        rf"({column})\s*<=\s*{int(high)}\b",
        lambda match: f"{match.group(1)} <= {{pile_end}}",
        text,
        flags=re.IGNORECASE,
    )


def _numbered_placeholders(values: list[Any], base: str) -> list[tuple[str, str]]:
    """去重后按顺序编号：第一个 `{base}`、第二个 `{base}_2`、第三个 `{base}_3`…"""
    seen: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return [(text, base if index == 0 else f"{base}_{index + 1}") for index, text in enumerate(seen)]


def _chunk_spec(content: str) -> dict[str, Any]:
    """从示例片段里抠「查询意图 JSON」：老示例没写就是空 dict。"""
    match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _same_structure(spec: dict[str, Any], plan: dict[str, Any] | None) -> bool:
    """示例的查询结构和本次提问是否同型（形态 + 主体）。"""
    if not spec or not plan or not spec.get("shape"):
        return False
    if spec["shape"] != plan.get("shape"):
        return False
    if spec.get("subject", "disease") != plan.get("subject", "disease"):
        return False
    # 分组维度也要一致：都是"按类型分布"才算同型（"按状态分布"不是同一题）
    left, right = spec.get("group_by"), plan.get("group_by")
    if left or right:
        return str(left or "") == str(right or "")
    return True


def example_interpretation(question: str, limit: int = 3) -> str:
    """阶段4：检索"类似问题的结果当时是怎么解读的"（方案 §一 阶段4）。

    只取示例文档里的 `# 解读` 段，不把 SQL/结构塞进解读提示词。
    """
    try:
        dataset_id = _ensure_example_dataset()
    except RAGError:
        return []
    if not dataset_id:
        return ""
    try:
        chunks = retrieve(query_rewrite.rewrite(question), [dataset_id])
    except RAGError:
        return ""
    for chunk in chunks[: limit * 3]:
        content = str(chunk.get("content") or "")
        match = re.search(r"#\s*解读\s*\n(.+)", content)
        if not match:
            continue
        text = match.group(1).strip()
        if text:
            return text[:300]
    return ""


def example_chunks_for_stage(
    question: str,
    query_type: str = "",
    limit: int = 3,
) -> list[dict[str, Any]]:
    """按阶段检索示例（方案 §一：每个阶段只拿它需要的字段）。

    query_type="composite" → 拆分阶段用：拿"这条复合问题拆成了几个子问题、分别是什么"。
    结构头（`# 拆分结构` / `# 结构`）里带 query_type，命中的优先返回。
    """
    dataset_id = _ensure_example_dataset()
    if not dataset_id:
        return []
    try:
        chunks = retrieve(query_rewrite.rewrite(question), [dataset_id])
    except RAGError:
        return []
    matched: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        content = str(chunk.get("content") or "")
        if not content or content in seen:
            continue
        seen.add(content)
        chunk["dataset_name"] = "SQL问答示例"
        if not query_type or _chunk_query_type(content) == query_type:
            matched.append(chunk)
        else:
            rest.append(chunk)
    return (matched or rest)[:limit]


def _chunk_query_type(content: str) -> str:
    """从示例片段里读 query_type：先看拆分结构头，再看查询意图 JSON。"""
    for pattern in (r"#\s*拆分结构\s*\n(\{.*?\})", r"#\s*结构\s*\n(\{.*?\})"):
        match = re.search(pattern, content, re.S)
        if not match:
            continue
        try:
            return str(json.loads(match.group(1)).get("query_type") or "")
        except (ValueError, TypeError):
            continue
    return str(_chunk_spec(content).get("shape") or "")


def _example_chunks(
    question: str,
    plan: dict[str, Any] | None = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """检索 few-shot 示例：结构同型的优先，不足再用文本相似的补。

    见 docs/RAGFlow知识库精简方案.md §2.5：纯文本相似度会把「西乡段有多少处病害」
    这种不同结构的示例召回给「两边加起来」，结构过滤能压掉这类错召回。
    """
    try:
        dataset_id = _ensure_example_dataset()
    except RAGError:
        return []
    if not dataset_id:
        return []
    try:
        chunks = retrieve(query_rewrite.rewrite(question), [dataset_id])
    except RAGError:
        return []
    picked: list[dict[str, Any]] = []
    matched: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        content = chunk.get("content", "")
        if (
            float(chunk.get("similarity") or 0) < settings.retrieval_min_similarity
            or not content
            or content in seen
        ):
            continue
        seen.add(content)
        chunk["dataset_name"] = "SQL问答示例"
        if _same_structure(_chunk_spec(content), plan):
            matched.append(chunk)
        else:
            picked.append(chunk)
    return (matched + picked)[:limit]


def _local_example_text(
    question: str,
    plan: dict[str, Any] | None = None,
    limit: int = 3,
) -> str:
    # 只用「已入池」的示例（模板自动 + 人工确认），不能拿每次查询的历史 SQL 当 few-shot，
    # 否则答错的写法会被反复召回，越用越偏。
    rows = [row for row in list_recorded_example_pairs(limit=200) if row.get("sql_text")]
    scored = []
    question_chars = set(question)
    for row in rows:
        score = len(question_chars & set(row["question"]))
        try:
            spec = json.loads(row["spec_json"]) if row.get("spec_json") else {}
        except (ValueError, TypeError):
            spec = {}
        # 结构同型的示例排在前面（同分时由结构决定先后）。
        if _same_structure(spec, plan):
            score += 100
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)
    blocks = []
    for _, row in scored[:limit]:
        if row["question"] == question:
            continue
        block = f"Q: {row['question']}"
        if row.get("spec_json"):
            block += f"\nJSON: {row['spec_json']}"
        try:
            spec = json.loads(row["spec_json"]) if row.get("spec_json") else None
        except (ValueError, TypeError):
            spec = None
        # 本地表存原始 SQL 供人工核对，喂给 LLM 的这份要先把具体条件归一化。
        block += f"\nSQL: {_normalize_sql_for_example(row['sql_text'], spec)}"
        blocks.append(block)
    return "\n\n".join(
        blocks
    )


_TABLE_SELECT_CACHE: dict[str, list[str]] = {}

# 语义层判的"主体"→ MDL 模型名（主体枚举来自 prompts/query_semantic.md）
_SUBJECT_MODELS: dict[str, str] = {
    "disease": "disease",
    "route": "route",
    "section": "section",
    "patrol": "patrol",
    "pile_point": "pile_point",
    "asset": "ai_asset_main",
    "work_order": "ai_work_order",
}


def select_tables(
    question: str,
    kind: str = "mysql",
    subject: str = "",
    route_via_patrol: bool = False,
) -> set[str]:
    """两步走第一步：让语义模型从表清单里挑出回答这个问题要用的表（+关系闭包）。

    见 docs/两步走选表方案.md：17 张表 592 个字段塞不进提示词（会被 [:12000] 截断，
    后面的表和字段直接消失——冷门字段问不出来就是这个原因）。所以先选表、再只把小范围
    字段给写 SQL 的模型。选不出来的（LLM 不可用）返回空集合 = 不做过滤，退回老行为。
    """
    # PG 也要走选表：以前直接返回空集，PG 侧就永远拿不到"小范围字段"，模型只能看见
    # 一整坨 schema，还容易把倾斜监测点当成"系统里没有这张表"。
    if kind not in {"mysql", "postgresql"}:
        return set()
    key = question.strip()
    key = f"{key}\n{subject}\nroute_via_patrol={int(route_via_patrol)}"
    cached = _TABLE_SELECT_CACHE.get(key)
    if cached is not None:
        return set(cached)
    from . import wren_engine

    listing = wren_engine.table_list(kind)
    if not listing:
        return set()
    subject_model = _SUBJECT_MODELS.get(subject, "") if kind == "mysql" else ""
    hint_text = (
        f"本轮语义层判定的主体是「{subject}」（对应模型 {subject_model}）："
        "只选回答该主体必需的辅助表，不要把无关的大表（尤其病害主表 disease）拉进来。"
        if subject_model
        else "（暂无主体判定，按问题语义自行判断。）"
    )
    # 用户的叫法穷举不完（"拍的片子""标了多少个点""构造物清单"…），所以不写别名表，
    # 而是拿**表自己的语义**（说明 + 列注释）去检索候选，把命中的证据一起给选表模型。
    candidates = wren_engine.search_models(kind, question, limit=4)
    candidate_text = (
        "\n".join(
            f"- {item['name']}（相关度 {item['score']}）：命中证据 "
            + "；".join(item.get("hits") or ["（无）"])
            for item in candidates
        )
        or "（无候选）"
    )
    names: list[str] = []
    try:
        data = chat_json(
            prompts.messages(
                "table_selector",
                table_list=listing,
                question=question,
                subject=subject or "未判定",
                subject_hint=hint_text,
                candidates=candidate_text,
            ),
            model=role_model("semantic"),
            allow_list=True,
        )
        if isinstance(data, dict):
            data = data.get("tables") or data.get("models") or []
        if isinstance(data, list):
            names = [str(item).strip() for item in data if str(item).strip()]
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "table_select_failed", exc, question=question[:120])
        names = []
    known = {str(model.get("name")) for model in wren_engine._load_mdl(str(wren_engine._project_dir(kind)))["models"]}
    picked = [name for name in names if name in known][:3]
    if not picked:
        # 选表模型不可用/没给出结论时，用语义检索结果兜底（低于阈值的不要——宁可退回
        # "不过滤"，也别把一张明显不相关的表当成答案的表）。
        picked = [item["name"] for item in candidates if item["score"] >= 4][:2]
        if picked:
            log_event(
                "nl2sql",
                "INFO",
                "table_select_fallback",
                tables="、".join(picked),
                question=question[:60],
            )
    if subject_model and subject_model in known and subject_model not in picked:
        picked.insert(0, subject_model)
    if route_via_patrol:
        # 巡查任务覆盖的路线以路线关联表为准；经病害表会漏掉零病害巡查。
        for required in ("patrol", "patrol_route", "route"):
            if required in known and required not in picked:
                picked.append(required)
        picked = [name for name in ("patrol", "patrol_route", "route") if name in picked]
    # 主体不是病害、而选表器顺手把病害表也选上时，把它剔掉（"路线等级"这类问题）
    if (
        not route_via_patrol
        and subject_model
        and subject_model != "disease"
        and subject_model in picked
    ):
        picked = [name for name in picked if name != "disease"] or picked
    expanded = wren_engine.related_models(kind, set(picked)) if picked else set()
    result = sorted(expanded)
    if len(_TABLE_SELECT_CACHE) >= 500:
        _TABLE_SELECT_CACHE.clear()
    _TABLE_SELECT_CACHE[key] = result
    if result:
        log_event(
            "nl2sql",
            "INFO",
            "tables_selected",
            tables="、".join(result),
            by_model="、".join(picked),
            candidates="、".join(f"{item['name']}:{item['score']}" for item in candidates),
            question=question[:60],
        )
    return set(result)


def retrieve_query_context(
    question: str,
    kind: str,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按知识库分别检索，各自保留自己的 top-K，不合成一个池子。"""
    groups: list[tuple[str, list[dict[str, Any]]]] = [
        (
            "数据库字段字典",
            _filter_chunks_for_db(_retrieve_chunks(question, "数据库字段字典", limit=4), kind),
        ),
        ("SQL问答示例", _example_chunks(question, plan=plan, limit=3)),
    ]

    references: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, str]] = set()
    by_dataset: dict[str, int] = {}
    texts: dict[str, str] = {}
    for name, chunks in groups:
        by_dataset[name] = len(chunks)
        texts[name] = "\n\n".join(chunk.get("content", "") for chunk in chunks)
        for chunk in chunks:
            key = (
                chunk.get("document_id"),
                chunk.get("id"),
                str(chunk.get("content", ""))[:80],
            )
            if key in seen:
                continue
            seen.add(key)
            references.append(chunk)

    schema_text = texts["数据库字段字典"]
    hint = _schema_hint(kind)
    # Wren MDL 语义层：结构化模型/字段/指标优先于文本字段字典（可在 config 里开关）
    from . import wren_engine

    # 两步走：先选表（语义判断），只把选中的表 + 关系闭包渲染进提示词
    # 主体由语义层判（plan.subject），选表据此收紧——"路线等级"这种不该把病害表拉进来。
    subject = str((plan or {}).get("subject") or "")
    selected = select_tables(question, kind, subject=subject) if settings.wren_use_mdl_context else set()
    if selected:
        prune_conditions_for_selection(plan, selected)
    mdl_text = (
        wren_engine.mdl_context(kind, models=selected) if settings.wren_use_mdl_context else ""
    )
    # 物理路径也走"第二步"：把全量字段字典换成选中表的实时表结构（选不出来就退回原样）
    if selected:
        focused = _selected_schema_text(kind, selected)
        if focused:
            schema_text = focused
    if mdl_text:
        hint = f"{mdl_text}\n\n{hint}" if hint else mdl_text
    if kind == "mysql":
        business = _business_hint()
        hint = f"{hint}\n\n{business}" if hint else business
        codes = _damage_code_hint()
        if codes:
            hint = f"{hint}\n\n{codes}" if hint else codes
        subject = (plan or {}).get("subject", "disease")
        if subject != "disease":
            main_table = parsers.SUBJECT_MAIN_TABLE.get(subject, "")
            hint = f"【问题主体：{subject}（主表 {main_table}），不要用病害表 ai_problem 作答】\n{hint}"
    return {
        "schema": (f"{hint}\n\n{schema_text}" if hint else schema_text).strip(),
        "examples": texts["SQL问答示例"] or _local_example_text(question, plan),
        "by_dataset": by_dataset,
        "references": references,
    }


def _ensure_example_dataset() -> str | None:
    global _example_dataset_id
    if _example_dataset_id:
        return _example_dataset_id
    dataset = find_dataset("SQL问答示例") or create_dataset("SQL问答示例")
    _example_dataset_id = str(dataset.get("id", ""))
    return _example_dataset_id or None


def _ensure_question_dataset() -> str | None:
    """题目索引数据集（只存问题文本）的 id。"""
    global _question_dataset_id
    if _question_dataset_id:
        return _question_dataset_id
    dataset = find_dataset(_QUESTION_INDEX_DATASET) or create_dataset(_QUESTION_INDEX_DATASET)
    _question_dataset_id = str(dataset.get("id", ""))
    return _question_dataset_id or None


def outward_question(
    question: str, sql: str = "", spec: dict[str, Any] | None = None
) -> str:
    """对外的"问法"：把**长对话补全留下的痕迹**清掉（上一轮答案里的"377 处"这类）。

    用在哪：写进示例库/RAGFlow 的文档正文（`# 问题`）、问题索引集、看板与同义召回的展示。
    **本地记录的 `question` 不动**——它是"用户当时确实这么问过"的留档，`doc_name` 也是按它算的，
    改了身份就变了（docs/自动模板挖掘代码评审与整改方案.md 实施记录 2）。
    """
    if spec and str(spec.get("display_question") or "").strip():
        return str(spec["display_question"]).strip()
    try:
        from . import templatizer

        return templatizer.clean_template_question(question, sql)
    except Exception:  # noqa: BLE001
        return str(question or "").strip()


def question_index_document(question: str, spec: dict[str, Any] | None, pair_name: str) -> str:
    """题目索引文档正文：**只有问题本身**，一个字符都不多。

    这份文档是专门用来做"问法相似"向量召回的，所以正文必须干净：
    结构 JSON、关联名、SQL 全都不放 —— 它们会被一起向量化，等于往问题向量里掺噪声
    （之前放 `# 结构` JSON 就是这样：问题只占一小半，相似度被拉平）。

    结构信息改成命中后从本地那条示例的 spec 取（反正要按文档名去拿 SQL，顺手就拿到了）；
    RAGFlow 检索结果里天然带文档名（`document_keyword`），关联名也不用写进正文。
    `spec`/`pair_name` 参数保留只是为了让调用点不用改。
    """
    return outward_question(question, "", spec)


def upsert_question_index(
    question: str,
    spec: dict[str, Any] | None,
    pair_name: str,
    force: bool = True,
) -> bool:
    """把一条"问题索引"文档写进题目库（同名覆盖）。失败只记日志，不影响主流程。

    force=False：索引里已经有同名文档就直接返回——"确保它存在"用（approve 去重命中等
    高频路径），不再每次删了重传。
    """
    dataset_id = _ensure_question_dataset()
    if not dataset_id or not question or not pair_name:
        return False
    name = pair_name
    try:
        existing = next(
            (
                str(doc.get("id"))
                for doc in list_documents(dataset_id)
                if str(doc.get("name")) == name and doc.get("id")
            ),
            "",
        )
        if existing and not force:
            return True
        if existing:
            delete_documents(dataset_id, [existing])
        doc_id = upload_text(dataset_id, name, question_index_document(question, spec, pair_name))
        parse_documents(dataset_id, [doc_id])
        return True
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "question_index_upsert_failed", exc, doc_name=name)
        return False


def backfill_normalized_sqls(limit: int = 1000) -> dict[str, Any]:
    """把示例的 `sql_text` 归一化成带占位符的 `normalized_sql`（可以换参数重放）。

    为什么需要：库里 60% 的示例把条件写死在 SQL 里（尤其 Wren 产出的 CTE 物理 SQL），
    换个月份/换条路就改不动 —— 卡片上显示"8 月"、SQL 仍按 7 月查。
    `normalized_sql` 单独一列存归一化版本（不动 `sql_text`/`doc_name`，避免牵连 RAGFlow 文档名）。

    校验（不通过就不写，宁可保持现状）：用 spec 回填归一化 SQL 后，文本应与原始 SQL 一致
    （归一化只做替换），并且回填结果要能解析成 SQL。
    """
    updated = skipped = scanned = 0
    rows = list_recorded_example_pairs(limit=limit)
    for row in rows:
        raw = str(row.get("sql_text") or "").strip()
        if not raw:
            continue
        scanned += 1
        try:
            spec = json.loads(str(row.get("spec_json") or "{}")) or {}
        except (ValueError, TypeError):
            spec = {}
        if not spec:
            spec = infer_example_spec(str(row.get("question") or ""), raw)
            if not spec or not _example_spec_usable(raw, spec):
                skipped += 1
                continue
        normalized = _normalize_sql_for_example(raw, spec)
        if normalized.strip() == raw:
            skipped += 1  # 没有可归一化的具体值（清一色占位符或纯结构 SQL）
            continue
        filled = fill_example_slots(normalized, spec)
        if not filled or not _parses_as_sql(filled):
            skipped += 1
            continue
        if _squash(filled) != _squash(raw):
            log_event(
                "nl2sql",
                "INFO",
                "normalize_roundtrip_mismatch",
                doc_name=str(row.get("doc_name") or ""),
            )
            skipped += 1
            continue
        if update_example_normalized_sql(str(row.get("doc_name") or ""), normalized):
            updated += 1
    if updated:
        log_event("nl2sql", "INFO", "normalized_sql_backfill", updated=updated, skipped=skipped)
    return {"scanned": scanned, "updated": updated, "skipped": skipped}


def _squash(sql: str) -> str:
    """压掉空白后比对（归一化只做替换，回填后文本应当一致）。"""
    return re.sub(r"\s+", "", str(sql or ""))


def backfill_question_index(limit: int = 1000, force: bool = False) -> dict[str, Any]:
    """对账：题目库补齐缺失的问题、删掉示例库里已经没有的。

    force=True：**全部重传**（改了题目文档格式后用，比如把 `# 结构` JSON 去掉那次）。
    幂等；RAGFlow 不可用时返回原因，不抛。
    """
    dataset_id = _ensure_question_dataset()
    if not dataset_id:
        return {"added": 0, "removed": 0, "reason": "没有题目索引数据集"}
    rows = [
        row
        for row in list_recorded_example_pairs(limit=limit)
        if str(row.get("question") or "").strip()
    ]
    names = {}
    for row in rows:
        question = str(row.get("question") or "")
        sql = str(row.get("sql_text") or "")
        name = str(row.get("doc_name") or example_pair_name(question, sql))
        names[name] = (question, row.get("spec_json"))
    try:
        remote = {
            str(doc.get("name")): str(doc.get("id"))
            for doc in list_documents(dataset_id)
            if str(doc.get("name"))
        }
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "question_index_list_failed", exc)
        return {"added": 0, "removed": 0, "reason": str(exc)[:120]}
    added = 0
    for name, (question, spec_json) in names.items():
        if name in remote and not force:
            continue
        try:
            spec = json.loads(str(spec_json or "{}")) or {}
        except (ValueError, TypeError):
            spec = {}
        if upsert_question_index(question, spec, name):
            added += 1
    stale = [doc_id for name, doc_id in remote.items() if name not in names]
    removed = 0
    if stale:
        try:
            delete_documents(dataset_id, stale)
            removed = len(stale)
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "question_index_cleanup_failed", exc)
    if added or removed:
        log_event("nl2sql", "INFO", "question_index_synced", added=added, removed=removed)
    return {"added": added, "removed": removed, "total": len(names), "indexed": len(remote)}


def _question_index_chunks(
    question: str,
    plan: dict[str, Any] | None = None,
    limit: int = 20,
    specs_by_name: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """题目索引检索：命中的 chunk 带上问题文本 + 文档名（形状与 _example_chunks 一致）。

    检索单元是**纯问题文本**（文档正文就是问题），所以相似度直接反映"问法像不像"。
    结构信息不在这份文档里：按文档名从 `specs_by_name`（本地示例的 spec）取来做同型重排。
    """
    dataset_id = _ensure_question_dataset()
    if not dataset_id:
        return []
    try:
        chunks = retrieve(query_rewrite.rewrite(question), [dataset_id])
    except RAGError:
        return []
    matched: list[dict[str, Any]] = []
    picked: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        content = str(chunk.get("content") or "").strip()
        if not content or content in seen:
            continue
        if float(chunk.get("similarity") or 0) < settings.retrieval_min_similarity:
            continue
        # 正文就是问题（兼容历史文档：带 `# 问题` 头的老格式仍能解析）
        sample_question = content.split("\n")[0].strip()
        if sample_question.startswith("#"):
            parsed = parse_example_document(content)
            sample_question = str(parsed.get("question") or "").strip()
        if not sample_question:
            continue
        seen.add(content)
        pair_name = str(
            chunk.get("document_keyword")
            or chunk.get("document_name")
            or chunk.get("doc_name")
            or ""
        )
        chunk["_question"] = sample_question
        chunk["_spec"] = (specs_by_name or {}).get(pair_name) or {}
        chunk["_pair"] = pair_name
        chunk["dataset_name"] = _QUESTION_INDEX_DATASET
        if _same_structure(chunk["_spec"], plan):
            matched.append(chunk)
        else:
            picked.append(chunk)
    return (matched + picked)[:limit]


def resolve_example_target(
    question: str, spec: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """这条问法最终会落到哪条已有示例上（精确同题，或同型变体）。

    `save_successful_pair` 和 approve 接口都用它，避免"库里去重了、接口却说新建了"。
    """
    existing = find_existing_example(question)
    if existing:
        return {**existing, "variant": False}
    if spec:
        try:
            similar = find_reusable_example(question, plan=spec)
        except Exception as exc:  # noqa: BLE001
            similar = None
            log_error("nl2sql", "example_variant_check_failed", exc, question=question[:60])
        if similar and similar.get("doc_name"):
            return {
                "doc_name": str(similar["doc_name"]),
                "question": question,
                "local": True,
                "trusted": True,
                "variant": True,
            }
    return None


def find_existing_example(question: str) -> dict[str, Any] | None:
    """这个问法是不是已经在示例库里了（本地表 或 RAGFlow 问答集）。

    用户的同一个问题不该反复入库：问法归一化后一致就算同一题，approve 时只把它标成
    "已确认"（trusted），不再新传一份文档、也不再重复落一条。
    """
    key = _example_key(question)
    if len(key) < 4:
        return None
    for row in list_recorded_example_pairs(limit=1000):
        if _example_key(str(row.get("question") or "")) != key:
            continue
        return {
            "doc_name": str(row.get("doc_name") or ""),
            "question": str(row.get("question") or ""),
            "local": True,
            "trusted": bool(row.get("trusted")),
        }
    # 本地还没有、但 RAGFlow 问答集里已经有同题（例如别人直接传上去的）
    for chunk in _example_chunks(question, limit=6):
        parsed = parse_example_document(str(chunk.get("content") or ""))
        if _example_key(str(parsed.get("question") or "")) != key:
            continue
        doc_name = str(
            chunk.get("document_keyword")
            or chunk.get("document_name")
            or chunk.get("doc_name")
            or ""
        )
        if not doc_name:
            continue
        return {
            "doc_name": doc_name,
            "question": str(parsed.get("question") or question),
            "local": False,
            "trusted": False,
        }
    return None


_EXAMPLE_IMPORT_AT = 0.0


def import_ragflow_examples(force: bool = False, min_interval: float = 60.0) -> dict[str, Any]:
    """把 RAGFlow 示例库里**本地还没有**的问答对导进本地示例表（看板卡片统一走本地表）。

    看板不区分"系统自带 / 我固化的"：凡是示例库（RAGFlow"SQL问答示例"数据集）里
    已有的问答对，一律成为可点可刷新的卡片。导入是幂等的（按 doc_name 去重）。
    """
    global _EXAMPLE_IMPORT_AT
    import time as _time

    now = _time.time()
    if not force and now - _EXAMPLE_IMPORT_AT < min_interval:
        return {"skipped": True}
    _EXAMPLE_IMPORT_AT = now
    dataset = find_dataset("SQL问答示例")
    if not dataset or not dataset.get("id"):
        return {"imported": 0, "reason": "没有示例数据集"}
    dataset_id = str(dataset["id"])
    try:
        docs = list_documents(dataset_id)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "example_import_list_failed", exc)
        return {"imported": 0, "reason": str(exc)[:120]}
    from . import rag as rag_service

    local_rows = list_recorded_example_pairs(limit=2000)
    known = {str(row.get("doc_name") or "") for row in local_rows}
    need_spec = {
        str(row.get("doc_name") or "")
        for row in local_rows
        if not str(row.get("spec_json") or "").strip()
    }
    # 人工池事实源对账：RAGFlow「SQL问答示例」里的题按产品定义**都是人工确认过的**
    # （只有点过"结果正确，存入示例库"的案例才会进这个数据集），本地却有历史条目
    # trusted=0 → 挖掘器看不到，60 条人工题只挖出 5 条
    # （docs/人工示例池全量参与模板挖掘方案.md P0-2/P0-3）。
    # 只回填"RAGFlow 里确实存在"的名字，纯本地自动采集的样本不受影响（P0-4）。
    rag_names = {str(doc.get("name") or "") for doc in docs if str(doc.get("name") or "")}
    untrusted = [
        str(row.get("doc_name") or "")
        for row in local_rows
        if str(row.get("doc_name") or "") in rag_names and not row.get("trusted")
    ]
    trusted_backfilled = mark_examples_trusted(untrusted) if untrusted else 0
    imported = 0
    backfilled = 0
    skipped = 0
    reparsed = 0
    for doc in docs:
        name = str(doc.get("name") or "")
        if not name:
            continue
        if name in known and name not in need_spec:
            continue
        try:
            content = rag_service.document_content(dataset_id, str(doc.get("id") or ""))
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "example_import_content_failed", exc, doc_name=name)
            skipped += 1
            continue
        parsed = parse_example_document(content)
        question = str(parsed.get("question") or "").strip()
        if name in known:
            # 老条目：只补结构 spec（它的 SQL/问题已经在了），补上看板/复用才能判同型
            if parsed.get("spec") and update_example_spec(name, json.dumps(parsed["spec"], ensure_ascii=False)):
                backfilled += 1
            continue
        sql = fill_example_slots(str(parsed.get("sql") or ""), parsed.get("spec") or {})
        if not question or not sql:
            skipped += 1  # 复合题外层没有 SQL、或 spec 填不全 → 先不导
            continue
        add_example_pair(
            question,
            sql,
            name,
            True,
            json.dumps(parsed.get("spec") or {}, ensure_ascii=False),
        )
        imported += 1
    # RAGFlow 侧解析失败的文档（chunk_count=0，常见原因是上传时 embedding 服务不可达）：
    # 它"存进去了"，但没进向量索引 —— 检索召回、回放、补 spec 全都看不到它。
    # 这里顺手重触发一次解析（只挑 chunk_count=0 的，每轮最多 5 条，失败不报错）。
    for doc in docs:
        if reparsed >= 5:
            break
        chunks = doc.get("chunk_count")
        if chunks is None or int(chunks or 0) > 0:
            continue
        doc_id = str(doc.get("id") or "")
        if not doc_id:
            continue
        try:
            parse_documents(dataset_id, [doc_id])
            reparsed += 1
            log_event(
                "nl2sql",
                "INFO",
                "example_reparse",
                doc_name=str(doc.get("name") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "example_reparse_failed", exc, doc_name=str(doc.get("name")))
    if imported or backfilled or trusted_backfilled:
        log_event(
            "nl2sql",
            "INFO",
            "examples_imported",
            count=imported,
            backfilled=backfilled,
            trusted_backfilled=trusted_backfilled,
        )
    return {
        "imported": imported,
        "backfilled": backfilled,
        "trusted_backfilled": trusted_backfilled,
        "reparsed": reparsed,
        "skipped": skipped,
        "total": len(docs),
    }


def example_sync_status() -> dict[str, Any]:
    """示例库同步状态（管理页展示）：本地条数 / 看板卡片 / RAGFlow 文档数。"""
    rows = list_recorded_example_pairs(limit=2000)
    status: dict[str, Any] = {
        "local": len(rows),
        "with_sql": sum(1 for row in rows if str(row.get("sql_text") or "").strip()),
    }
    dataset = find_dataset("SQL问答示例")
    if dataset and dataset.get("id"):
        try:
            status["ragflow"] = len(list_documents(str(dataset["id"])))
        except Exception as exc:  # noqa: BLE001
            status["ragflow"] = f"读取失败：{str(exc)[:60]}"
    else:
        status["ragflow"] = "没有示例数据集"
    return status


def plan_derived_field(plan: dict[str, Any], kind: str = "mysql") -> str:
    """计划里点到 **MDL 计算列**（`section_length_km` 这种派生列）时返回它的名字。

    语义层看得到这些列（字段目录为了"认得出用户说法"把它们并进来了），但它们**物理表里没有**：
    物理 SQL 点它必然报 `Unknown column`（实测"路段米数"选口径后就是这个错）。
    所以调用方拿到非空结果时应当**强制走 Wren**——Wren 按模型把表达式内联进去。
    """
    try:
        from . import schema_catalog as catalog

        derived = {
            str(col.get("name"))
            for table in catalog.field_catalog(kind).get("tables") or []
            for col in table.get("columns") or []
            if str(col.get("source") or "") == "mdl_calculated"
        }
    except Exception:  # noqa: BLE001
        return ""
    if not derived:
        return ""
    semantic = plan.get("semantic") or {}
    wanted = [
        str(item)
        for item in list(semantic.get("output_fields") or [])
        + list(semantic.get("metrics") or [])
        + [semantic.get("group_by")]
        if item
    ]
    return next((item for item in wanted if item in derived), "")


def plan_to_spec(plan: dict[str, Any]) -> dict[str, Any]:
    """把内部计划压成人类可读、可复用的"查询意图"JSON（三元组里的中间那一份）。

    只放逻辑口径（路线名/桩号区间/病害类型/时间/设备/分组/指标），
    不放表别名和 SQL 片段——它同时给 LLM 当 few-shot、给人工核对、给将来的编译器复用。
    """
    slots = plan.get("slots", {})
    route = slots.get("route_match") or {}
    spec: dict[str, Any] = {
        "kind": plan.get("kind"),
        "subject": plan.get("subject", "disease"),
        "shape": plan.get("shape"),
    }
    optimization = plan.get("question_optimization")
    if isinstance(optimization, dict) and optimization.get("source"):
        spec["question_optimization"] = {
            "source": str(optimization.get("source")),
            "original_question": str(optimization.get("original_question") or ""),
            "expanded_question": str(optimization.get("question") or ""),
            "required_dimensions": list(optimization.get("required_dimensions") or []),
            "scope": dict(optimization.get("optimization_scope") or {}),
        }
    # 分组维度也要进 spec：示例复用要靠它判断"按类型分布"和"按状态分布"不是同一题
    if (plan.get("slots") or {}).get("group_by"):
        spec["group_by"] = str(plan["slots"]["group_by"])
    # 语义层判的"要不要下结论"（为什么/好不好/该怎么做）——回答摘要据此决定走不走分析
    if (plan.get("semantic") or {}).get("needs_conclusion") is not None:
        spec["needs_conclusion"] = bool(plan["semantic"]["needs_conclusion"])
    # 口径被代码改过（"按路段分组"→按桩号分段）时带着说明，回答里要如实告诉用户
    if slots.get("group_note"):
        spec["group_note"] = str(slots["group_note"])
    if route:
        spec["route"] = route.get("short_name") or route.get("full_name") or route.get("code")
    routes = slots.get("routes") or []
    if routes:
        spec["routes"] = [str(item.get("name") or item.get("id")) for item in routes]
    side = parsers.parse_pile_side(plan.get("message", ""))
    if side:
        spec["pile_side"] = side[1]
    # 「K1以西（K1+000前）」这类单边说法，别把括号里的 K1+000 当成区间写进 JSON。
    elif slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        spec["pile_range"] = [int(slots["pile_start"]), int(slots["pile_end"])]
    if slots.get("spatial_split"):
        spec["pile_split"] = f"前{slots['spatial_split']}米/后{slots['spatial_split']}米"
    if slots.get("damage_matches"):
        spec["damage_type"] = [item["problem_type"] for item in slots["damage_matches"]]
    elif slots.get("damage_type"):
        spec["damage_type"] = slots["damage_type"]
    if slots.get("time_start") and slots.get("time_end"):
        spec["time_range"] = [slots["time_start"], slots["time_end"]]
    elif slots.get("time_start") or slots.get("time_end"):
        # 只说"从X开始/到X为止"时只有一端，查询意图里要如实记下来
        spec["time_range"] = [slots.get("time_start"), slots.get("time_end")]
    if slots.get("device_id"):
        spec["device_code"] = slots["device_id"]
    if "has_image" in (plan.get("required_conditions") or []) or any(
        str(item.get("field") or "") == "has_image"
        for item in slots.get("semantic_conditions") or []
        if isinstance(item, dict)
    ):
        spec["has_image"] = True
    if slots.get("group_by"):
        spec["group_by"] = slots["group_by"]
    labels = slots.get("metric_labels")
    if labels:
        spec["metrics"] = [str(label) for label in labels.values() if label != "分组"]
    if plan.get("comparison"):
        spec["comparison"] = True
    if plan.get("overview"):
        spec["overview"] = True
    if plan.get("required_conditions"):
        spec["must_keep"] = list(plan["required_conditions"])
    return spec


def example_pair_name(question: str, sql: str) -> str:
    return f"pair-{hashlib.sha256((question + sql).encode('utf-8')).hexdigest()[:16]}.md"


_PHYSICAL_CACHE: dict[tuple[str, str], str] = {}


def example_kind(source: dict[str, Any] | None) -> str:
    """这条示例属于哪个库：`spec.kind`，认不出来按 mysql。

    示例库是跨库的（MySQL 业务表 + PostgreSQL 的 GPS/倾角）。判定"能不能直接跑"必须知道
    该拿哪个库去 EXPLAIN/编译——以前一律按 mysql，PG 那条 gps 示例会被误判成"不可重放"，
    还会让 Wren 打一行 `table 'wren.public.gps_points' not found` 的跨库编译噪音。
    `source` 既接受 spec 本身，也接受带 `spec_json` 的本地登记行。
    """
    value = source if isinstance(source, dict) else {}
    if "spec_json" in value:
        raw = value.get("spec_json")
        if isinstance(raw, str):
            try:
                value = json.loads(raw) or {}
            except (ValueError, TypeError):
                value = {}
        elif isinstance(raw, dict):
            value = raw
        else:
            value = {}
    kind = str((value or {}).get("kind") or "").strip().lower()
    return kind if kind in {"mysql", "postgresql"} else "mysql"


def physical_sql_of(sql: str, kind: str = "mysql") -> str:
    """把一条 SQL 变成"能直接跑物理库"的 SQL。

    已经是物理 SQL 的原样返回；Wren 的 MDL-SQL（含 `wren_src_`）先过 Wren 编译，
    编译不出来返回空串（调用方据此判断"这条示例不可重放"）。

    `kind` 必须传**这条示例自己的库**（`example_kind(spec)`）：拿 mysql 去判 PG 的示例，
    EXPLAIN 和编译都会失败。
    """
    text = str(sql or "").strip()
    if not text:
        return ""
    if "wren_src_" not in text.lower():
        return text
    kind = kind if kind in {"mysql", "postgresql"} else "mysql"
    # 缓存键必须带库类型：同一条 SQL 在不同库下的判定结果不一样
    cache_key = (str(kind or "mysql"), text)
    if cache_key in _PHYSICAL_CACHE:
        return _PHYSICAL_CACHE[cache_key]
    # 归一化 SQL（带 {占位符}）本来就不是拿来直接跑的：它要 fill_example_slots 填完才执行。
    # 送进 Wren 只会让引擎打 `ParserError("Expected: :, found: }")` 刷屏，直接原样返回
    # （调用方拿到后照常填槽/校验）。
    if re.search(r"\{[a-z_][a-z0-9_]*\}", text):
        _PHYSICAL_CACHE[cache_key] = text
        return text
    from . import wren_engine

    compiled = ""
    # Wren 的 dry_plan 产出的**物理 SQL** 里也带 `wren_src_` 别名（那只是子查询名），
    # 拿它去编译必然报"不是本项目的模型"。所以先看它是不是本来就能跑：只要引用了本项目
    # 的物理表、EXPLAIN 也过，就直接当物理 SQL 用——否则"已确认过的示例"会被误判成
    # 不可重放而不摆给用户（实测：西乡段里程桩点 52 条那条示例第二遍问就不见了）。
    if wren_engine.references_physical_table(kind, text) and _explains(kind, text):
        compiled = text
    else:
        try:
            compiled = str(wren_engine.compile_mdl_sql(kind, text) or "")
        except Exception as exc:  # noqa: BLE001
            log_error("nl2sql", "physical_sql_compile_failed", exc)
            compiled = ""
    if len(_PHYSICAL_CACHE) > 200:
        _PHYSICAL_CACHE.clear()
    _PHYSICAL_CACHE[cache_key] = compiled
    return compiled


def _explains(kind: str, sql: str) -> bool:
    """这条 SQL 能不能直接交给物理库跑（用 EXPLAIN 问一次，不改数据）。"""
    try:
        db_manager.query(kind, f"EXPLAIN {sql}")
        return True
    except Exception as exc:  # noqa: BLE001
        log_event("nl2sql", "INFO", "example_sql_not_runnable", reason=str(exc)[:150])
        return False


def _example_document(
    question: str,
    sql: str,
    spec: dict[str, Any] | None,
    note: str = "",
    interpretation: str = "",
) -> str:
    """示例文档正文：结构头 + 问题 + 查询意图 JSON + 归一化后的 SQL。

    结构头按 docs/SQL问答对存储与检索方案.md 的"按阶段检索"设计：SQL 生成阶段先按
    query_type / subject 粗筛，再按问题相似度排序，避免把"分布题示例"喂给"计数题"。
    """
    # 对外问法（清掉长对话补全留下的痕迹）；"用户当时确实这么问过"的原句留在本地 question 里
    content = f"# 问题\n{outward_question(question, sql, spec)}\n"
    if note:
        content += f"\n# 澄清\n{note}\n"
    if spec:
        header = {
            "query_type": spec.get("shape"),
            "subject": spec.get("subject", "disease"),
            "metrics": spec.get("metrics") or [],
            "dimensions": [key for key in ("group_by", "pile_split") if spec.get(key)],
            "must_keep": spec.get("must_keep") or [],
        }
        content += f"\n# 结构\n{json.dumps(header, ensure_ascii=False)}\n"
    if spec:
        content += (
            f"\n# 查询意图(JSON)\n```json\n"
            f"{json.dumps(spec, ensure_ascii=False, indent=2)}\n```\n"
        )
    content += f"\n# SQL\n```sql\n{_normalize_sql_for_example(sql, spec)}\n```\n"
    # Wren 生成的 MDL-SQL 不能直接跑物理库：把编译出来的**物理 SQL** 一起存下来，
    # 之后重放/挖模板都用物理版，不再依赖 Wren 环境
    # （docs/确认卡重复气泡耗时整改与污染模板库重置.md 附带项）。
    physical = physical_sql_of(sql, example_kind(spec or {}))
    if physical and physical != str(sql).strip():
        content += f"\n# 物理SQL\n```sql\n{_normalize_sql_for_example(physical, spec)}\n```\n"
    # 阶段4（结果解读）用的字段：这类查询结果当时是怎么说成中文的
    if interpretation.strip():
        content += f"\n# 解读\n{interpretation.strip()[:400]}\n"
    return content


def save_composite_pair(
    question: str,
    sections: list[dict[str, Any]],
    note: str = "",
    interpretation: str = "",
    plan: dict[str, Any] | None = None,
) -> bool:
    """复合问题**双层存储**（docs/SQL问答对存储与检索方案.md §二 方案3）。

    外层：原始复合问题 + 拆分结构（拆分阶段学"这种问题该怎么拆"）
    内层：每个带 SQL 的子问题单独一条（SQL 生成阶段学"每个子问题的 SQL 怎么写"）

    这样两层的用途不打架：拆分时看到的是总览，写 SQL 时看到的是单条示例。
    interpretation 是外层整体解读；内层优先用各子问题自己的解读（阶段4 示例）。
    """
    summary = [
        {
            "id": section.get("id"),
            "type": section.get("type"),
            "question": section.get("question"),
            "needs": section.get("needs") or [],
            "produces": section.get("produces") or [],
            "sql": section.get("sql") or "",
            "spec": section.get("spec") or {},
            "plan": section.get("plan") or {},
        }
        for section in sections
    ]
    independent = bool(sections) and all(
        not section.get("needs") and not section.get("produces") for section in sections
    )
    saved_inner = False
    for section in sections:
        section_sql = section.get("sql")
        section_question = str(section.get("question") or "")
        if not section_sql or not section_question:
            continue  # post_calc 没有 SQL，不单独存
        section_spec = dict(section.get("spec") or {})
        section_spec["composite_parent"] = question
        section_spec["composite_independent"] = independent
        if save_successful_pair(
            section_question,
            str(section_sql),
            # 复合题也是用户确认过的（approve 触发），子问题同样按"人工确认"入池
            template=True,
            note=f"父问题：{question}",
            spec=section_spec,
            interpretation=str(section.get("reply") or ""),
        ):
            saved_inner = True

    dataset_id = _ensure_example_dataset()
    if not dataset_id:
        return saved_inner
    header = {
        "query_type": "composite",
        "subtask_count": len(summary),
        "independent": independent,
        "subtasks_summary": summary,
        "plan": plan
        or {
            "intent": "composite",
            "steps": [
                {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "question": item.get("question"),
                    "needs": item.get("needs") or [],
                    "produces": item.get("produces") or [],
                    "sql": item.get("sql") or "",
                    "spec": item.get("spec") or {},
                }
                for item in summary
            ],
        },
    }
    content = f"# 问题\n{question}\n"
    if note:
        content += f"\n# 澄清\n{note}\n"
    content += f"\n# 拆分结构\n{json.dumps(header, ensure_ascii=False)}\n"
    if interpretation.strip():
        content += f"\n# 解读\n{interpretation.strip()[:400]}\n"
    name = example_pair_name(question, "composite")
    try:
        doc_id = upload_text(dataset_id, name, content)
        parse_documents(dataset_id, [doc_id])
    except Exception:
        return saved_inner
    # 复合题外层同样标 trusted（approve 触发的），这样看板里能看到这张卡片；
    # 它没有 SQL，重放时按子问题逐段走（先看子问题的固化条目）。
    header_json = json.dumps(header, ensure_ascii=False)
    if get_example_by_doc_name(name):
        # 重复确认同一条复合题时覆盖拆分结构，避免旧版本继续缺 SQL/plan。
        set_example_spec(name, header_json)
        mark_example_trusted(name)
    else:
        add_example_pair(question, "", name, True, header_json)
    # 复合题外层也进题目索引（它是"这种复合问法"的问法本体，匹配到就按子问题逐段重放）
    upsert_question_index(question, {}, name)
    return True


def save_successful_pair(
    question: str,
    sql: str,
    template: bool = True,
    note: str = "",
    spec: dict[str, Any] | None = None,
    interpretation: str = "",
) -> bool:
    """把「问题 + 查询意图(JSON) + SQL」三元组写进示例库。

    template=True 表示模板自动入池，False 表示人工确认后入池。
    中间那份 JSON 既给 LLM 当 few-shot 教口径格式，也能反过来核对 SQL 有没有跑偏。
    """
    dataset_id = _ensure_example_dataset()
    if not dataset_id:
        return False
    name = example_pair_name(question, sql)
    # 对外问法（清掉长对话补全留下的数量痕迹）记进 spec：文档正文、问题索引集、看板/同义召回
    # 的展示都读它；本地 `question` 保持用户原话（身份 doc_name 也是按原话算的）
    display_question = outward_question(question, sql, spec)
    if display_question and display_question != str(question or "").strip():
        spec = {**(spec or {}), "display_question": display_question}
        log_event(
            "nl2sql",
            "INFO",
            "context_question_cleaned",
            frm=str(question or "")[:50],
            to=display_question[:50],
        )
    # 同一个问法在示例库里已经有（本地表或 RAGFlow 问答集）→ 不重复存：
    # 只把已有那条标成"已确认"，SQL 沿用先入库的那版（用户的同一句话不该存两份）。
    # 精确同题 or 换个说法的同型变体（向量+结构+槽位+条件覆盖都对上）→ 都不新存
    existing = resolve_example_target(question, spec)
    if existing and existing.get("variant"):
        append_example_variant(str(existing.get("doc_name") or ""), question)
    if existing:
        if template and existing.get("local"):
            mark_example_trusted(str(existing.get("doc_name") or ""))
        if not existing.get("local"):
            # 只在 RAGFlow 里、本地还没有 → 补一条本地登记（SQL 用本次这条），不新传文档
            add_example_pair(
                question,
                sql,
                str(existing.get("doc_name") or name),
                template,
                json.dumps(spec, ensure_ascii=False) if spec else None,
            )
        # 去重命中也要保证"问题索引"集里有它：老条目可能是在题目索引上线前入库的，
        # 只同步问答示例集会让两个集对不上（索引里缺这条 → 相似问法召回不到）。
        upsert_question_index(
            question, spec, str(existing.get("doc_name") or name), force=False
        )
        log_event(
            "nl2sql",
            "INFO",
            "example_deduped",
            doc_name=str(existing.get("doc_name") or ""),
            question=question[:60],
        )
        return True
    with get_connection() as conn:
        exists = conn.execute(
            "SELECT 1 FROM query_history WHERE question = ? AND sql_text = ? LIMIT 1",
            (question, sql),
        ).fetchone()
    recorded = {row["doc_name"] for row in list_recorded_example_pairs(limit=1000)}
    if name in recorded and exists:
        # 重复确认（用户又点了一次"答对了/固化到看板"）时，把这条升级成"已确认"：
        # 老条目可能是自动入池的 trusted=0，不升级就永远进不了看板、也不能被复用。
        if template:
            mark_example_trusted(name)
        return True
    # 本地先落（事实源），再传 RAGFlow（向量投影）：RAGFlow 临时不可用时这次 approve
    # 也不会丢——看板/复用读的都是本地表（docs/固定看板重构与收藏固化方案.md 第八章 §2）。
    add_example_pair(
        question,
        sql,
        name,
        template,
        json.dumps(spec, ensure_ascii=False) if spec else None,
        # 可重放的归一化版本（带占位符）：重放/看板/复用都优先用它，条件写死的原样 SQL
        # 保留在 sql_text 里（doc_name 是按 sql_text 生成的，不能改）
        _normalize_sql_for_example(sql, spec),
    )
    # 题目索引：同时写一份"只有问题"的文档到题目库，供相似问法召回用（失败不影响入库）
    if template:
        upsert_question_index(question, spec, name)
    content = _example_document(question, sql, spec, note, interpretation)
    try:
        doc_id = upload_text(dataset_id, name, content)
        parse_documents(dataset_id, [doc_id])
    except Exception as exc:  # noqa: BLE001
        # 记忆写入失败不影响主查询，也不影响本地这条能不能用；下次对账会补传。
        log_error("nl2sql", "example_upload_failed", exc, doc_name=name)
        return True
    return True


def _example_roundtrip_ok(raw: str, normalized: str, spec: dict[str, Any]) -> bool:
    """归一化 SQL + spec 能不能**回填出原始 SQL**（示例复用安全的充要条件）。"""
    if not raw or not normalized:
        return False
    filled = fill_example_slots(normalized, spec)
    if not filled:
        return False
    try:
        return _squash(filled) == _squash(raw)
    except Exception:  # noqa: BLE001
        return False


def _reorder_spec_routes(spec: dict[str, Any], raw: str) -> dict[str, Any] | None:
    """按**原始 SQL 里各路线的出现位置**重排 spec.routes / route_ids。

    为什么需要：spec.routes 是按路线台账行序生成的，而归一化 SQL 的
    `{route_id}` / `{route_id_2}` 是按 SQL 里的先后编号的。两边顺序不一致时回填就把
    参数填反——实测 pair-3f50df81…：`{route_id}`（算平均面积的路线）被填成恒明珠、
    `{route_id_2}`（被比较的路线）被填成西乡，SQL 跑的是"恒明珠的平均 + 西乡大于它的数量"，
    而列别名还写着西乡/恒明珠，标签和数据完全对不上（docs/示例复用参数填反与多指标总结缺失_整改方案.md）。
    """
    from . import route_resolver

    names = [str(item).strip() for item in (spec.get("routes") or []) if str(item or "").strip()]
    ids = [str(item).strip() for item in (spec.get("route_ids") or []) if str(item or "").strip()]
    if len(names) < 2 and len(ids) < 2:
        return None
    positions: dict[str, int] = {}
    for name in names:
        route_id = ""
        try:
            route_id = str((route_resolver._resolve_route(name) or {}).get("id") or "")
        except Exception:  # noqa: BLE001
            route_id = ""
        found = raw.find(route_id) if route_id and route_id in raw else raw.find(name)
        if found < 0:
            return None  # 有一条在原始 SQL 里找不到位置 → 不猜，保持原样
        positions[name] = found
    ordered = sorted(names, key=lambda item: positions[item])
    if ordered == names:
        return None  # 顺序本来就对
    fixed = dict(spec)
    fixed["routes"] = ordered
    if len(ids) == len(names):
        by_name = dict(zip(names, ids))
        fixed["route_ids"] = [by_name[item] for item in ordered]
    return fixed


def repair_example_spec_order(limit: int = 1000) -> dict[str, Any]:
    """修"spec 里的路线顺序和 SQL 占位符顺序不一致"的存量示例（免得示例复用把参数填反）。

    对每条示例跑一次回环校验（`fill(normalize(raw)) == raw`）：
      · 通过 → 跳过；
      · 不通过且按原始 SQL 重排 spec.routes 后能通过 → 覆盖 spec（修好）；
      · 还是不通过 → 把示例标成未确认（宁可退回 Wren，也不拿填反的 SQL 当答案）。
    """
    fixed_rows = 0
    blocked: list[str] = []
    scanned = 0
    for row in list_recorded_example_pairs(limit=limit):
        name = str(row.get("doc_name") or "")
        raw = str(row.get("sql_text") or "").strip()
        normalized = str(row.get("normalized_sql") or "").strip()
        if not name or not raw or not normalized:
            continue
        try:
            spec = json.loads(str(row.get("spec_json") or "{}") or "{}")
        except (ValueError, TypeError):
            spec = {}
        scanned += 1
        if not spec or _example_roundtrip_ok(raw, normalized, spec):
            continue
        repaired = _reorder_spec_routes(spec, raw)
        if repaired and _example_roundtrip_ok(raw, normalized, repaired):
            if set_example_spec(name, json.dumps(repaired, ensure_ascii=False)):
                fixed_rows += 1
                log_event(
                    "nl2sql",
                    "INFO",
                    "example_spec_reordered",
                    doc_name=name,
                    routes="、".join(str(item) for item in repaired.get("routes") or []),
                )
            continue
        blocked.append(name)
    blocked_rows = untrust_examples(blocked) if blocked else 0
    if blocked:
        log_event(
            "nl2sql",
            "WARNING",
            "example_spec_unfixable",
            doc_names="、".join(blocked[:5]),
            count=len(blocked),
        )
    return {"scanned": scanned, "fixed": fixed_rows, "blocked": blocked_rows}


def renormalize_examples() -> dict[str, Any]:
    """按当前归一化规则重写已入池示例，清洗历史示例里的具体路线名/ID。

    见 docs/RAGFlow知识库精简方案.md §5.2：旧示例是原始 SQL，会被 LLM 抄进新查询。
    本地登记表保留原始 SQL（供人工核对），这里只重写 RAGFlow 侧文档正文。
    """
    dataset = find_dataset("SQL问答示例")
    if not dataset or not dataset.get("id"):
        return {"dataset_id": None, "rewritten": 0, "removed": 0, "errors": []}
    dataset_id = str(dataset["id"])
    remote = {str(doc.get("name")): str(doc.get("id")) for doc in list_documents(dataset_id)}
    rows = list_recorded_example_pairs(limit=1000)
    live = {example_pair_name(row["question"], row["sql_text"]) for row in rows}
    # 数据集里没有本地登记、或名字带重复后缀的历史文档一并清掉，避免同一示例被召回两次。
    stale = [doc_id for name, doc_id in remote.items() if name not in live]
    rewritten = 0
    errors: list[str] = []
    for row in rows:
        name = example_pair_name(row["question"], row["sql_text"])
        try:
            spec = json.loads(row["spec_json"]) if row.get("spec_json") else None
        except (ValueError, TypeError):
            spec = None
        try:
            old = remote.get(name)
            if old:
                delete_documents(dataset_id, [old])
            doc_id = upload_text(dataset_id, name, _example_document(row["question"], row["sql_text"], spec))
            parse_documents(dataset_id, [doc_id])
            rewritten += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")
    if stale:
        try:
            delete_documents(dataset_id, stale)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"stale: {exc}")
    return {
        "dataset_id": dataset_id,
        "rewritten": rewritten,
        "removed": len(stale),
        "errors": errors,
    }


def backfill_physical_sql() -> dict[str, Any]:
    """给示例库里的 Wren MDL-SQL 补一份编译好的物理 SQL。

    为什么：MDL-SQL 只能过 Wren 才能跑，环境一变就重放不了（本机就是这样）。
    物理 SQL 存进文档的 `# 物理SQL` 段落后，重放/挖模板都不再依赖 Wren。
    编译不出来的（例如本机 Wren 项目名对不上）如实报出来，不动原文、不删数据。
    """
    rows = [
        row
        for row in list_recorded_example_pairs(limit=1000)
        if row.get("trusted")
        and "wren_src_" in str(row.get("sql_text") or "").lower()
    ]
    if not rows:
        return {"candidates": 0, "compiled": [], "failed": [], "note": "没有带 wren_src_ 的示例"}
    compiled: list[str] = []
    failed: list[dict[str, str]] = []
    for row in rows:
        question = str(row.get("question") or "")
        raw = str(row.get("sql_text") or "")
        physical = physical_sql_of(raw, example_kind(row))
        if not physical:
            failed.append({"question": question[:60], "reason": "Wren 编译不出物理 SQL"})
            continue
        if physical.strip() == raw.strip():
            # 本来就是物理 SQL（Wren 的 dry_plan 产物也带 wren_src_ 别名，能直接跑）：
            # 不用"补"，重写一遍只会白跑一次 Wren 编译。
            continue
        try:
            spec = json.loads(row["spec_json"]) if row.get("spec_json") else None
        except (ValueError, TypeError):
            spec = None
        # 本地登记换成物理 SQL：重放/挖模板都读它
        try:
            with db_manager_connection() as conn:
                conn.execute(
                    "UPDATE example_pairs SET sql_text = ? WHERE doc_name = ?",
                    (physical, str(row.get("doc_name") or "")),
                )
        except Exception as exc:  # noqa: BLE001
            failed.append({"question": question[:60], "reason": f"本地登记更新失败：{exc}"})
            continue
        # RAGFlow 文档补一段物理 SQL（同一个 doc_name 重传）
        dataset = find_dataset("SQL问答示例")
        if dataset and dataset.get("id"):
            try:
                name = str(row.get("doc_name") or example_pair_name(question, physical))
                dataset_id = str(dataset["id"])
                for doc in list_documents(dataset_id):
                    if str(doc.get("name")) == name and str(doc.get("id")):
                        delete_documents(dataset_id, [str(doc["id"])])
                doc_id = upload_text(
                    dataset_id, name, _example_document(question, physical, spec)
                )
                parse_documents(dataset_id, [doc_id])
            except Exception as exc:  # noqa: BLE001
                failed.append({"question": question[:60], "reason": f"RAGFlow 重写失败：{exc}"})
        compiled.append(question[:60])
    return {"candidates": len(rows), "compiled": compiled, "failed": failed}


def db_manager_connection():
    """复用 store 的连接（本地登记表在 SQLite 里）。"""
    from ..store import get_connection

    return get_connection()


def list_example_pairs(limit: int = 50) -> list[dict[str, Any]]:
    """只列出真正进了「SQL问答示例」数据集的 问题-SQL 对，供人工核对与清洗。"""
    recorded = list_recorded_example_pairs(limit=1000)
    # 以数据集里的真实文档为准；查不到远端时（RAGFlow 不可用）退回本地登记表。
    remote: set[str] | None = None
    dataset = find_dataset("SQL问答示例")
    if dataset and dataset.get("id"):
        try:
            remote = {str(doc.get("name")) for doc in list_documents(str(dataset["id"]))}
        except RAGError:
            remote = None
    # 同一个 doc_name 可能因为历史原因登记了多条，列表里只显示最新的一条。
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for row in sorted(recorded, key=lambda item: item.get("created_at") or "", reverse=True):
        if row["doc_name"] in seen:
            continue
        if remote is not None and row["doc_name"] not in remote:
            continue
        seen.add(row["doc_name"])
        rows.append(
            {
                "id": row["id"],
                "question": row["question"],
                "sql": row["sql_text"],
                "spec": json.loads(row["spec_json"]) if row.get("spec_json") else None,
                "created_at": row["created_at"],
                "doc_name": row["doc_name"],
                "source": "template" if row.get("trusted") else "approved",
            }
        )
    rows.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return rows[:limit]


def purge_example_pairs(doc_names: list[str]) -> dict[str, Any]:
    """从 SQL问答示例 数据集删除指定示例，并清理对应的本地历史记录。"""
    names = {name for name in doc_names if name}
    if not names:
        return {"deleted_docs": 0, "removed_records": 0, "removed_history": 0}

    deleted = 0
    dataset = find_dataset("SQL问答示例")
    if dataset and dataset.get("id"):
        remote = {
            str(doc.get("name")): str(doc.get("id"))
            for doc in list_documents(str(dataset["id"]))
        }
        targets = [remote[name] for name in names if name in remote]
        if targets:
            delete_documents(str(dataset["id"]), targets)
            deleted = len(targets)

    # 题目索引集同一份名字也要删：只删问答示例集会留下孤儿文档，
    # 相似问法召回会把已经不存在的问题召回来（两个集必须同进同出）。
    deleted_index = 0
    index_dataset = find_dataset(_QUESTION_INDEX_DATASET)
    if index_dataset and index_dataset.get("id"):
        remote_index = {
            str(doc.get("name")): str(doc.get("id"))
            for doc in list_documents(str(index_dataset["id"]))
        }
        index_targets = [remote_index[name] for name in names if name in remote_index]
        if index_targets:
            try:
                delete_documents(str(index_dataset["id"]), index_targets)
                deleted_index = len(index_targets)
            except Exception as exc:  # noqa: BLE001
                log_error("nl2sql", "question_index_purge_failed", exc)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, question, sql_text FROM query_history "
            "WHERE sql_text IS NOT NULL AND sql_text <> ''"
        ).fetchall()
        doomed = [
            row["id"]
            for row in rows
            if example_pair_name(row["question"], row["sql_text"]) in names
        ]
        for row_id in doomed:
            conn.execute("DELETE FROM query_history WHERE id = ?", (row_id,))
    removed_records = delete_recorded_example_pairs(sorted(names))
    return {
        "deleted_docs": deleted,
        "deleted_index": deleted_index,
        "removed_records": removed_records,
        "removed_history": len(doomed),
    }


def purge_example_pairs_by_filter(
    question_contains: str = "",
    purge_all: bool = False,
) -> dict[str, Any]:
    """按问题关键词（或全部）批量清洗示例库。"""
    pairs = list_example_pairs(limit=1000)
    keyword = question_contains.replace(" ", "")
    names = [
        pair["doc_name"]
        for pair in pairs
        if purge_all
        or (keyword and keyword in pair["question"].replace(" ", ""))
    ]
    result = purge_example_pairs(names)
    return {**result, "selected": len(names)}


def required_condition_lines(plan: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    summary = semantic_summary(plan)
    if summary:
        lines.append(summary)
    lines.extend(condition_lines(plan))
    slots = plan["slots"]
    subject = plan.get("subject", "disease")
    if "comparison" in plan["required_conditions"]:
        lines.append(
            "comparison: 用户要对比 / 分别统计，必须按对象分组（GROUP BY 或 CASE WHEN），"
            "每个对象各占一行，不要只统计其中一个，也不要只给一个总数"
        )
    if "route" in plan["required_conditions"] and slots.get("route_match"):
        route_id = slots["route_match"]["id"].replace("'", "''")
        # 路线过滤要按主体走各自的关联链路，否则会把里程桩点/路段问题强行套到病害表上。
        if subject == "pile_point":
            lines.append(
                f"route: JOIN basic_facilities_sections s ON d.sections_id = s.id AND s.del_flag = 0 "
                f"JOIN basic_facilities_routes r ON s.route_id = r.id AND r.del_flag = 0 "
                f"AND r.id = '{route_id}'"
            )
        elif subject == "section":
            lines.append(
                f"route: JOIN basic_facilities_routes r ON s.route_id = r.id AND r.del_flag = 0 "
                f"AND r.id = '{route_id}'"
            )
        elif subject == "patrol":
            lines.append(
                f"route: ai_maintain_patrol t "
                f"JOIN ai_maintain_routes_patrol pr ON pr.patrol_id = t.id AND pr.del_flag = 0 "
                f"JOIN basic_facilities_routes r ON pr.routes_id = r.id AND r.del_flag = 0 "
                f"AND r.id = '{route_id}'，统计用 COUNT(DISTINCT t.id)"
            )
        elif subject == "work_order":
            lines.append(
                f"route: ai_work_order w JOIN ai_problem p ON w.problem_id = p.id "
                f"JOIN basic_facilities_routes r ON p.routes_id = r.id AND r.del_flag = 0 "
                f"AND r.id = '{route_id}'"
            )
        elif subject == "route":
            # 问的是路线台账本身（长度/等级/车道/路面），不涉及病害表。
            # 这里必须写物理表名 basic_facilities_routes：以前没有这个分支，
            # 条件行里只有"route"这个语义层模型名，写物理 SQL 的模型会照抄成 FROM route，
            # 被表白名单拦下（实测"西乡段这条路有多长？"报"表不在白名单中：route"）。
            lines.append(
                f"route: 只查路线台账 basic_facilities_routes r（不 JOIN ai_problem），"
                f"WHERE r.del_flag = 0 AND r.id = '{route_id}'；"
                "路线长度按起止桩号相减，不要用 r.distance"
            )
        else:
            lines.append(f"route: JOIN basic_facilities_routes r ON p.routes_id = r.id AND r.id = '{route_id}'")
    if "has_image" in plan["required_conditions"]:
        lines.append("has_image: p.problem_url IS NOT NULL AND p.problem_url <> ''")
    # 多路线对比：只按 id 过滤。实测模型会顺手加一句 r.short_name IN ('西乡段','恒明珠段')
    # ——那是用户写的简称，库里没有这个名称，AND 上去必然 0 条（"7月两条路各多少处"就这么查空的）。
    multi_routes = slots.get("routes") or []
    if plan.get("multi_route") and multi_routes:
        ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in multi_routes)
        names = "、".join(str(item.get("name") or "") for item in multi_routes)
        lines.append(
            f"multi_route: 路线过滤只写 r.id IN ({ids})（{names}）；"
            "**不要再写 r.short_name / r.full_name 过滤**——用户写的是简称，库里没有这个名称，AND 上去会查空"
        )
    if "device_code" in plan["required_conditions"] and slots.get("device_id"):
        device = str(slots["device_id"]).replace("'", "''")
        lines.append(f"device_code: p.device_code = '{device}'")
    if "pile_range" in plan["required_conditions"]:
        lines.append(f"pile_range: p.pile_distance BETWEEN {int(slots['pile_start'])} AND {int(slots['pile_end'])}")
    asset = slots.get("asset_match") or {}
    if "asset" in plan["required_conditions"] and asset.get("id"):
        table = str(asset.get("table") or "")
        asset_id = str(asset["id"]).replace("'", "''")
        lines.append(
            f"asset: {table}.id = '{asset_id}'（{asset.get('name') or ''}）"
            "——**按 id 过滤，不要再按名称字符串过滤**（用户写的是简称，库里不一定有）"
        )
    section_match = slots.get("section_match") or {}
    if "section" in plan["required_conditions"] and section_match.get("id"):
        section_id = str(section_match["id"]).replace("'", "''")
        if subject == "section":
            lines.append(
                f"section: basic_facilities_sections s.id = '{section_id}'"
                f"（{section_match.get('name') or ''}），按 id 过滤，不要再按名称字符串过滤"
            )
        elif subject == "pile_point":
            lines.append(
                f"section: JOIN basic_facilities_sections s ON p.sections_id = s.id "
                f"AND s.id = '{section_id}'"
            )
        else:
            lines.append(
                f"section: JOIN basic_facilities_sections s ON p.sections_id = s.id "
                f"AND s.id = '{section_id}'"
            )
    if "damage_type" in plan["required_conditions"] and slots.get("damage_matches"):
        values = ", ".join("'" + item["problem_type"].replace("'", "''") + "'" for item in slots["damage_matches"])
        lines.append(
            f"damage_type: {_damage_dict_join()} "
            f"AND b.problem_type IN ({values})"
        )
    if "time_range" in plan["required_conditions"]:
        # 只有一端时只写一端（用户说"从X开始"就是没有上界，别拼出 < 'None'）
        bounds = []
        if slots.get("time_start"):
            bounds.append(f"p.collection_time >= '{slots['time_start']}'")
        if slots.get("time_end"):
            bounds.append(f"p.collection_time < '{slots['time_end']}'")
        if bounds:
            lines.append("time_range: " + " AND ".join(bounds))
    if slots.get("group_by") == "route":
        lines.append(
            "group_by_route: 用户问的是「分布在哪条路线」，必须 "
            "JOIN basic_facilities_routes r ON p.routes_id = r.id 并 GROUP BY r.short_name，"
            "SELECT r.short_name AS type, COUNT(*) AS count；不要按病害类型分组"
        )
    if _asks_length_or_density(plan):
        lines.append(_length_hint("physical"))
    return lines or ["无额外强制条件"]


_SEMANTIC_LABELS = {
    "damage_type": "病害类型",
    "route": "路线",
    "section": "路段",
    "route_level": "行政等级",
    "tech_grade": "公路技术等级",
    "surface_type": "路面类型",
    "direction": "上下行",
    "image_presence": "有无图片",
    "category": "病害大类",
    # 时间维度：按粒度从粗到细。缺一个，提示词里就会写成裸键（实测"按「half_month」分组"）。
    "year": "年份",
    "half_year": "半年",
    "quarter": "季度",
    "season": "季节",
    "month": "月份",
    "half_month": "半月",
    "ten_day": "旬",
    "day": "日期",
    "week": "周次",
    "dow": "星期几",
    "workday": "工作日/周末",
    "hour": "时段",
    "time_of_day": "上午/下午/晚上",
    "minute": "分钟",
    "every_n_days": "每N天",
    "every_n_hours": "每N小时",
    "every_n_minutes": "每N分钟",
    "device": "设备",
    "confidence": "置信度分档",
    "area_bucket": "面积分档",
    "manage_dept": "管养单位",
}


def _asks_length_or_density(plan: dict[str, Any]) -> bool:
    """这个问题的答案要用到"长度/密度/每公里/间距"吗？（决定要不要给长度口径提示）"""
    semantic = plan.get("semantic") or {}
    derived = {str(item) for item in (semantic.get("derived") or [])}
    if derived & {"density", "interval"}:
        return True
    if semantic.get("bucket_metric") == "density":
        return True
    return False


def _length_hint(dialect: str) -> str:
    """长度口径提示：台账 distance 不准，必须按起止桩号相减。"""
    if dialect == "mdl":
        return (
            "长度口径：**不要用 route.distance / section.distance**。"
            "路线长度用 route.route_length_km（所属路段 end_meter_total-start_meter_total 求和；"
            "无路段时才用路线起止桩号兜底），路段长度用 section.section_length_km"
        )
    return (
        "长度口径：**不要用 r.distance / s.distance**。路线长度优先按所属路段 "
        "end_meter_total-start_meter_total 求和，没有路段时才用路线起止桩号兜底；写成 "
        + _ROUTE_LENGTH_KM.replace("r.", "{路线或路段表别名}.")
        + " AS route_length_km（桩号带 K 前缀，必须先 SUBSTRING_INDEX 去掉 K 再 CAST，"
        "否则长度会算成 0.1）"
    )


# 语义层点名的字段 → 物理 SQL 里的真实写法。
# 为什么必须有这张表：语义层的字段名（problem_type_name / image / lng_lat …）在物理表里
# **不存在**，提示词里直接把名字丢给模型，它就会 SELECT problem_type_name → Unknown column
# （实测「把那次巡检的病害明细调出来」就这么失败的）。


def _output_fields_hint(
    fields: list[Any], dialect: str, plan: dict[str, Any] | None = None
) -> str:
    """把"用户点名要看的字段"翻成**可以直接照抄的写法**（按方言给不同写法）。"""
    names = [str(item) for item in fields if item]
    if not names:
        return ""
    if dialect == "mdl":
        # MDL 里这些是模型字段/计算字段，直接用模型名即可；模型名要按本轮主体走，
        # 不能一律写 disease（问隧道/路线时 disease 上根本没这些列）。
        anchor, _columns = _mdl_anchor_model(plan or {})
        return "SELECT 里必须包含（Wren 模型字段）：" + "、".join(
            f"{anchor}.{name}" for name in names
        )
    rendered: list[str] = []
    joins: list[str] = []
    for name in names:
        expr = _SELECT_FIELD_EXPRS.get(name)
        if not expr:
            continue
        rendered.append(f"{expr} AS {name}")
        join = _SELECT_FIELD_JOINS.get(name)
        if join and join not in joins:
            joins.append(join)
    if not rendered:
        return ""
    text = (
        "SELECT 里必须包含这些列（**照抄下面的写法，不要直接把字段名当列名**）："
        + "、".join(rendered)
    )
    if joins:
        text += "；需要 " + "；".join(joins)
    return text


# 空列探测：库里这一列是不是压根没数据（全 NULL/空串）。
# 实测"西乡段历史损坏程度严重的病害有多少处？"→ 答"处数 0处"，而真相是
# history_damage_interval 这一列 1702 条记录全是 NULL——必须如实说"字段没数据"，
# 不能让用户以为是"没有符合的病害"。探测结果按列缓存。
_EMPTY_COLUMN_CACHE: dict[str, bool] = {}


def column_is_empty(field: str, kind: str = "mysql") -> bool:
    expr_info = _CONDITION_FIELDS.get(field)
    if not expr_info:
        return False
    expr = expr_info[0]
    if not expr.startswith("p.") or "JSON_EXTRACT" in expr:
        return False
    column = expr.split(".")[-1]
    key = f"{kind}:{column}"
    if key in _EMPTY_COLUMN_CACHE:
        return _EMPTY_COLUMN_CACHE[key]
    empty = False
    try:
        rows = db_manager.query(
            kind,
            f"SELECT COUNT(`{column}`) AS c FROM ai_problem WHERE del_flag = 0 AND `{column}` IS NOT NULL AND `{column}` <> ''",
        )["rows"]
        empty = int(rows[0].get("c") or 0) == 0
    except Exception:  # noqa: BLE001
        empty = False
    _EMPTY_COLUMN_CACHE[key] = empty
    return empty


def _empty_condition_note(plan: dict[str, Any]) -> str:
    for item in (plan.get("slots") or {}).get("semantic_conditions") or []:
        field = str(item.get("field") or "")
        if not column_is_empty(field, plan.get("kind") or "mysql"):
            continue
        label = _CONDITION_LABELS.get(field, field)
        return (
            f"⚠️ 库里的「{label}」这一列目前没有任何数据（记录都是空的），"
            f"所以按它筛选一定是 0 —— 不是「没有符合条件的病害」，是这个字段还没录数据。"
        )
    return ""


# 时间分组的**标准写法**：MDL 里没有 day/week/month 这些字段（实测模型写 disease.day
# 直接编译失败：No field named disease.day），必须给出 DATE_FORMAT 之类的确切表达式。
_TIME_BUCKET_EXPRS: dict[str, tuple[str, str]] = {
    "day": ("DATE({alias}.collection_time)", "DATE({alias}.collection_time)"),
    # 周标签给中文可读写法（`2026年第31周`），不要 `YEARWEEK` 的裸数字 `202631`
    "week": (
        "CONCAT(DATE_FORMAT({alias}.collection_time, '%x'), '年第', "
        "DATE_FORMAT({alias}.collection_time, '%v'), '周')",
        "YEARWEEK({alias}.collection_time, 3)",
    ),
    "month": (
        "DATE_FORMAT({alias}.collection_time, '%Y-%m')",
        "DATE_FORMAT({alias}.collection_time, '%Y-%m')",
    ),
    "hour": ("HOUR({alias}.collection_time)", "HOUR({alias}.collection_time)"),
}


# ── 词表 / 语义键→SQL 映射外置（docs/代码结构重构与模板外置方案.md §5.3）──────
# `resources/lexicon/metrics.yaml` 有对应段就用它覆盖，没有就保持上面代码里的默认值，
# 行为完全不变；以后新增一个指标/维度改 YAML 即可。
def _lexicon_section(section: str) -> Any:
    try:
        from .resource_loader import load_yaml

        return (load_yaml("lexicon/metrics.yaml", {}) or {}).get(section)
    except Exception:  # noqa: BLE001
        return None


def _lexicon_pairs(section: str, default: Any) -> Any:
    """[{fallback_words: [...], expr: "..."}] → ((words...), expr) 元组，和代码里的形状一致。

    `fallback_words` 是**降级用**的词表：主映射是"语义层 JSON 的字段名 → 表达式"。
    旧名 `words` 仍然认（YAML 里还没改完的条目），新加条目一律写 `fallback_words`。
    """
    rows = _lexicon_section(section)
    if not isinstance(rows, list) or not rows:
        return default
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        words = item.get("fallback_words") or item.get("words")
        expr = item.get("expr")
        if words and expr:
            out.append((tuple(str(word) for word in words), str(expr)))
    return tuple(out) or default


def _lexicon_dimensions(section: str, default: Any) -> Any:
    """[{key, expr, fallback_words}] → ((key, words...), expr)，和代码里的形状一致。

    `key` 是**字段名**（语义层 JSON 的 `group_by` 按它查这里的 `expr`）；
    `fallback_words` 只在语义层不可用（LLM 没跑/失败）时用，**不许拿它当主路径**。
    """
    rows = _lexicon_section(section)
    if not isinstance(rows, list) or not rows:
        return default
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        words = item.get("fallback_words") or item.get("words")
        expr = item.get("expr")
        if key and words and expr:
            out.append((str(key), tuple(str(word) for word in words), str(expr)))
    return tuple(out) or default


def _lexicon_buckets(section: str, default: Any) -> Any:
    rows = _lexicon_section(section)
    if not isinstance(rows, dict) or not rows:
        return default
    return {str(key): tuple(str(part) for part in value) for key, value in rows.items()} or default


_DIMENSIONS = _lexicon_dimensions("dimensions", _DIMENSIONS)
_NUMERIC_FIELDS = _lexicon_pairs("numeric_fields", _NUMERIC_FIELDS)
_METRIC_EXPRESSIONS = _lexicon_pairs("metric_expressions", _METRIC_EXPRESSIONS)
_TIME_BUCKET_EXPRS = _lexicon_buckets("time_buckets", _TIME_BUCKET_EXPRS)


def _positive_int(value: Any) -> int:
    """语义层给的粒度（N）→ 正整数；不是正数就返回 0（调用方据此走兜底）。"""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if 0 < number <= 100000 else 0


_EVERY_N_FALLBACK = {"every_n_days": "day", "every_n_hours": "hour", "every_n_minutes": "minute"}


def _every_n_expr(group_by: str, alias: str, n: int) -> str:
    """自定义粒度（每 N 天/小时/分钟）的分组表达式。

    **标签必须人能读、也要能被第二步反解**（"去年冬天"那类同理）：只写桶序号（`4851`）
    人会看不懂、链式题第二步也没法把它当时间条件（`202631` 那个坑的翻版）。
    所以每 N 天写成 `2026-07-01起每10天`——既看得懂，又能唯一确定那一段区间。
    锚点固定用 2000-01-01：不随数据增长而漂移，模板才能被复用（锚在"数据第一天"会漂）。
    """
    column = f"{alias}.collection_time"
    if group_by == "every_n_days":
        start = (
            f"DATE_ADD('2000-01-01', INTERVAL "
            f"FLOOR(DATEDIFF({column}, '2000-01-01') / {n}) * {n} DAY)"
        )
        return f"CONCAT(DATE_FORMAT({start}, '%Y-%m-%d'), '起每{n}天')"
    if group_by == "every_n_hours":
        return (
            f"CONCAT(DATE_FORMAT({column}, '%Y-%m-%d '), "
            f"LPAD(FLOOR(HOUR({column}) / {n}) * {n}, 2, '0'), ':00')"
        )
    return (
        f"CONCAT(DATE_FORMAT({column}, '%Y-%m-%d %H:'), "
        f"LPAD(FLOOR(MINUTE({column}) / {n}) * {n}, 2, '0'))"
    )


def _time_bucket_hint(
    group_by: str, dialect: str, params: dict[str, Any] | None = None
) -> str:
    """这个分组的**标准写法**（模型必须照抄，别自己猜；MDL 里没有 day/week 这些字段）。

    MDL 用二元组的第 1 个表达式（人能读的标签），物理 SQL 用第 2 个（分组用的值）。
    """
    name = str(group_by)
    alias = "disease" if dialect == "mdl" else "p"
    if name in _EVERY_N_FALLBACK:
        n = _positive_int((params or {}).get("n"))
        if not n:
            # 没拿到 N：不能替用户瞎定一个粒度（每 7 天和每 3 天是两张完全不同的图）。
            # 退回同族最细粒度并**把这件事写出来**，让模型按这个写、回答里也能如实说明。
            fallback = _EVERY_N_FALLBACK[name]
            spec = _TIME_BUCKET_EXPRS.get(fallback)
            if not spec:
                return ""
            expr = spec[0].format(alias=alias) if dialect == "mdl" else spec[1].format(alias=alias)
            return (
                f"按「{_SEMANTIC_LABELS.get(fallback, fallback)}」分组（用户没给自定义粒度的 N，"
                f"先用这个）：GROUP BY {expr}，SELECT 里也写 {expr} AS 分组"
            )
        expr = _every_n_expr(name, alias, n)
        return (
            f"按「每{n}{'天' if name == 'every_n_days' else ('小时' if name == 'every_n_hours' else '分钟')}」"
            f"分组的写法：GROUP BY {expr}，SELECT 里也写 {expr} AS 分组；"
            f"**不要写 {alias}.{name}**（表里没有这个字段）"
        )
    spec = _TIME_BUCKET_EXPRS.get(name)
    if not spec:
        return ""
    expr = spec[0].format(alias=alias) if dialect == "mdl" else spec[1].format(alias=alias)
    # 第 3 项是排序表达式（有就告诉模型）：中文标签按字典序排会得到"第31周、第3周"这种错顺序。
    order_hint = ""
    if len(spec) > 2:
        order_expr = spec[2].format(alias=alias)
        order_hint = f"；排序用 {order_expr}（**别按分组标签排**，中文标签是字典序）"
    return (
        f"按「{_SEMANTIC_LABELS.get(name, name)}」分组的写法：GROUP BY {expr}，"
        f"SELECT 里也写 {expr} AS 分组；"
        f"**不要写 {alias}.{name}**（表里没有这个字段）"
        f"{order_hint}"
    )


# ── 人话口径卡片（docs/产品化与结果可信度整改方案.md 第三节第 1、8 条）──────────
# 用户看不懂 SQL，他判断"对不对"靠的是：范围清不清、时间覆盖到哪、排除了什么、数据新不新。
# 数据质量提示（方案六·2）：把"明显不对劲"的地方主动说出来，别让用户自己发现
_AREA_ANOMALY = 5000.0


def composition_check(plan: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> str:
    """分项加总核对（方案三·2）：分项之和要等于总数，不等就说清差在哪。

    只对"按类型分组"这类**天然应该等于总数**的分布做（同一口径下再查一次总数）。
    """
    rows = list(rows or [])
    slots = plan.get("slots") or {}
    if str(slots.get("group_by") or "") not in {"damage_type", "category"}:
        return ""
    if len(rows) < 2 or len(rows) > 40:
        return ""
    values: list[float] = []
    for row in rows:
        raw = row.get("count") or row.get("数量") or row.get("病害数量")
        try:
            values.append(float(str(raw)))
        except (TypeError, ValueError):
            return ""
    if not values:
        return ""
    total_shown = sum(values)
    try:
        joins, where = _metric_filters(plan)
        total = float(
            db_manager.query(
                "mysql",
                f"SELECT COUNT(*) AS c FROM ai_problem p {' '.join(joins)} WHERE {' AND '.join(where)}",
            )["rows"][0].get("c")
            or 0
        )
    except Exception:  # noqa: BLE001
        return ""
    if not total:
        return ""
    if abs(total - total_shown) < settings.number_compare_epsilon:
        return f"构成核对：各分项相加 = {total_shown:g}，与同口径总数 {total:g} 一致 ✔"
    return (
        f"⚠️ 构成核对：各分项相加 = {total_shown:g}，同口径总数 = {total:g}，"
        f"相差 {total - total_shown:g}（差额多为未归类/未识别记录）"
    )


def data_quality_notes(plan: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> list[str]:
    """从结果里认出三类可确定判断的异常，写进口径卡片。"""
    notes: list[str] = []
    rows = list(rows or [])
    for row in rows[:200]:
        for key, value in row.items():
            if "面积" not in str(key) and str(key) not in {"area", "avg_area", "max_area"}:
                continue
            try:
                number = float(str(value))
            except (TypeError, ValueError):
                continue
            if number >= _AREA_ANOMALY:
                notes.append(
                    f"有记录的面积为 {number:g} ㎡（≥{_AREA_ANOMALY:g}），明显异常，疑似录入/占位值，"
                    "平均值已被它拉高"
                )
                break
        if notes:
            break
    # 未识别类型占比高：类型分布里出现"未识别/NULL"且数量不小
    for row in rows[:20]:
        label = str(row.get("type") or row.get("病害类型") or row.get("problem_type_name") or "")
        if "未识别" not in label and label.upper() not in {"NULL", "NONE"}:
            continue
        value = row.get("count") or row.get("数量") or row.get("病害数量")
        if value in (None, ""):
            continue
        notes.append(f"其中 {value} 条病害的类型 AI 没有识别出来，归为「未识别」")
        break
    return notes


def restate(plan: dict[str, Any]) -> str:
    """把这一轮要查的口径说成人话，给用户确认（方案第二章）。只读槽位，不查库。"""
    slots = plan.get("slots") or {}
    route = slots.get("route_match") or {}
    parts: list[str] = []
    route_name = str(route.get("short_name") or slots.get("route") or "").strip()
    parts.append(route_name or "全库")
    if slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        parts.append(f"{calibrate.fmt_pile(slots['pile_start'])}~{calibrate.fmt_pile(slots['pile_end'])}")
    start, end = slots.get("time_start"), slots.get("time_end")
    if start or end:
        parts.append(f"{str(start or '最早')[:7]} ~ {str(end or '最新')[:7]}")
    damage = [str(item.get("problem_type")) for item in (slots.get("damage_matches") or [])]
    if damage:
        parts.append("、".join(damage) + "病害")
    group_by = str(slots.get("group_by") or "")
    if group_by:
        parts.append("按" + _SEMANTIC_LABELS.get(group_by, group_by) + "分组")
    metrics = [str(item) for item in ((plan.get("semantic") or {}).get("metrics") or [])]
    if metrics:
        parts.append("统计" + "、".join(_METRIC_LABELS.get(item, item) for item in metrics))
    return "我将查询：" + "，".join(part for part in parts if part) + "。"


_METRIC_LABELS = {
    "count": "数量",
    "total_area": "总面积",
    "avg_area": "平均面积",
    "max_area": "最大面积",
    "min_area": "最小面积",
    "avg_length": "平均长度",
    "avg_effective_disease_length": "平均长度",
    "sum_effective_disease_length": "总长度",
    "total_effective_disease_length": "总长度",
    "max_effective_disease_length": "最大长度",
    "min_effective_disease_length": "最小长度",
    "converted_area": "换算面积",
    "confidence": "置信度",
    "ratio": "占比",
    "density": "密度",
    "interval": "平均间距",
    "rate": "平均水平",
    "distinct_pile": "不同桩号数",
    "duration": "时间跨度",
}


def needs_confirm(plan: dict[str, Any]) -> bool:
    """要不要先复述确认：复杂问题才问，简单问题直接查（方案第二章规则 1/2）。"""
    slots = plan.get("slots") or {}
    semantic = plan.get("semantic") or {}
    conditions = [item for item in (slots.get("semantic_conditions") or []) if item.get("field") != "route_name"]
    if slots.get("group_by") or semantic.get("group_by"):
        return True
    if plan.get("comparison") or plan.get("multi_route"):
        return True
    if [item for item in (semantic.get("derived") or []) if item]:
        return True
    if len(semantic.get("metrics") or []) >= 2:
        return True
    return len(conditions) >= 3


def _subject_label(plan: dict[str, Any]) -> str:
    """主体的人话名称（口径卡片用）：从 MDL 模型说明里取，不写死清单。"""
    # 字段触达已经锁定落点表时（"隧道表的通风方式明细"→basic_facilities_tunnels），
    # 直接用它的人话名，别再靠主体枚举回落到"病害大类"这种错标。
    try:
        from . import field_grounding

        grounded_subject = str((plan.get("grounded") or {}).get("subject") or "")
        if grounded_subject == str(plan.get("subject") or "disease"):
            grounded_label = field_grounding.table_label_of(plan)
        else:
            grounded_label = ""
    except Exception:  # noqa: BLE001
        grounded_label = ""
    if grounded_label:
        return grounded_label
    anchor, _columns = _mdl_anchor_model(plan)
    from . import wren_engine

    kind = str(plan.get("kind") or "mysql")
    cards = wren_engine._schema_index(str(wren_engine._project_dir(kind)))
    # 主体模型没被选表器选上时（"边坡台账明细"这种语义主体词表里没有的问法），以**选表结果**
    # 为准：否则口径卡片会写成"全部病害"，校验器判答非所问，问题还会被改写成离谱的样子。
    selected: set[str] = set()
    try:
        selected = select_tables(
            str(plan.get("message") or ""), kind, subject=str(plan.get("subject") or "")
        )
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "subject_label_select_failed", exc)
    if selected and anchor == "disease" and "disease" not in selected:
        anchor = next((name for name in sorted(selected) if name != "disease"), anchor)
    if selected and anchor not in selected:
        # 主体词只是"资产/设施"这类粗粒度时（"边坡台账""隧道台账"都会被判成 asset），
        # 用选表器**按语义检索排出来**的那张表更准。
        ranked = [
            item["name"]
            for item in wren_engine.search_models(kind, str(plan.get("message") or ""), limit=6)
            if item["name"] in selected
        ]
        if ranked:
            anchor = ranked[0]
    if anchor == "disease":
        return "全部病害"
    for card in cards:
        if card["name"] != anchor:
            continue
        description = str(card.get("description") or "")
        # "路线台账（路线名、技术等级…）。路线过滤走这里。" → 只取括号前那截当前缀
        label = re.split(r"[（(。]", description)[0].strip()
        return label or anchor
    return anchor


def scope_card(plan: dict[str, Any], rows: list[dict[str, Any]] | None = None) -> str:
    """把本轮查询口径写成人话（不查库，全部来自槽位与结果）。"""
    slots = plan.get("slots") or {}
    route = slots.get("route_match") or {}
    route_name = str(route.get("short_name") or slots.get("route") or "").strip()
    lines: list[str] = []
    scope = route_name or "全库"
    if slots.get("pile_start") is not None and slots.get("pile_end") is not None:
        scope += f" {calibrate.fmt_pile(slots['pile_start'])}~{calibrate.fmt_pile(slots['pile_end'])}"
    if slots.get("up_or_down"):
        scope += f"（{slots['up_or_down']}）"
    lines.append(f"- 范围：{scope}")
    start, end = slots.get("time_start"), slots.get("time_end")
    if start or end:
        lines.append(f"- 时间：{str(start or '最早')[:10]} ~ {str(end or '最新')[:10]}")
    damage = [str(item.get("problem_type")) for item in (slots.get("damage_matches") or [])]
    # 字段触达把落点锁在别的台账表上时（隧道/桥梁/边坡…），对象就写这张台账：
    # 否则会写成"病害大类""边坡病害"这种与本次查询无关的对象名（口径卡片是给用户核对用的）。
    _grounded_table = ""
    try:
        from . import field_grounding

        _grounded_table = field_grounding.subject_table(plan)
        _grounded_label = field_grounding.table_label_of(plan)
    except Exception:  # noqa: BLE001
        _grounded_label = ""
    if _grounded_table and _grounded_table != "ai_problem" and _grounded_label:
        lines.append(f"- 对象：{_grounded_label}")
    elif damage:
        lines.append(f"- 对象：{'、'.join(damage)}病害")
    else:
        # 主体不是病害时不能再写"全部病害"——口径卡片写错对象，校验器就会判"答非所问"
        # 去重写问题（实测"把路线台账明细调出来"被改写成离谱的字段清单，最后报
        # "没有找到路线'包括路'"）。对象名从 MDL 模型说明取，不写死。
        lines.append(
            f"- 对象：{_SEMANTIC_LABELS.get(str(slots.get('group_by') or '')) or _subject_label(plan)}"
        )
    # 只在**真按 del_flag 过滤过**时才这么写：PG 的 gps_points / inclination_points 没有这一列，
    # 无脑写上去就是假口径（实测 PG 题的口径卡也写了"排除：已删除记录（del_flag=0）"）
    executed = str(plan.get("sql") or plan.get("executed_sql") or "")
    if "del_flag" in executed.lower() or str(plan.get("kind") or "mysql") == "mysql":
        lines.append("- 排除：已删除记录（del_flag=0）")
    newest = ""
    for row in (rows or [])[:200]:
        for key in ("collection_time", "日期", "采集时间", "date"):
            value = str(row.get(key) or "")
            if len(value) >= 10 and value[:4].isdigit():
                newest = max(newest, value)
    facts: dict[str, Any] = {}
    if not newest:
        # 结果里没有时间列（问"多少处"这种）时，补一次廉价的 MAX(collection_time)：
        # 业务用户第一个疑问就是"这数据是不是最新的"，直接答掉（方案第三节第 8 条）。
        # 这个事实 calibrate 已经按路线查过（带 5 分钟缓存），直接复用它 ——
        # 自己再查一次等于每次回答都多花一次连库往返（库在 SSH 隧道后面，实测 0.3s/次）。
        try:
            route_id = str(route.get("id") or "")
            if route_id:
                facts = calibrate.route_facts(route_id)
                newest = str(facts.get("data_max_time") or "")
            else:
                newest = str(calibrate.global_data_range().get("max_time") or "")
        except Exception:  # noqa: BLE001
            newest = ""
    # 空表时 MAX(collection_time) 可能被驱动返回成字符串 "NULL"/"None"：别原样印给用户
    # （实测"朝朝线有多少处病害"的口径卡片上出现了"数据截至：NULL"）。
    newest = str(newest or "").strip()
    if newest.upper() in {"NULL", "NONE"}:
        newest = ""
    if newest:
        lines.append(f"- 数据截至：{str(newest)[:19]}")
    elif facts.get("data_count") is not None and int(facts.get("data_count") or 0) == 0:
        lines.append("- 数据截至：该路线暂无病害记录（0 条）")
    check = composition_check(plan, rows)
    if check:
        lines.append(f"- {check}")
    for note in data_quality_notes(plan, rows):
        lines.append(f"- ⚠️ {note}")
    return "统计口径：\n" + "\n".join(lines)


def semantic_summary(plan: dict[str, Any], dialect: str = "physical") -> str:
    """把 LLM 的维度识别结果压成一句"必须照此执行"，放进写 SQL 的强制条件里。

    写成命令式（"必须按 X 分组"）而不是说明式（"分组维度=X"）：这一段是跟在
    "这些必须条件要在 SQL 里出现"后面的，说明式的措辞实测会被模型当成背景信息忽略。
    """
    semantic = plan.get("semantic") or {}
    if not semantic:
        return ""
    parts: list[str] = []
    # 分档对比：告诉写 SQL 的模型"按哪个指标的中位数分两档、两档叫什么"（方案 §3.2）
    spec = _bucket_metric_spec(plan)
    if spec:
        _, _, label, low_label, high_label = spec
        parts.append(
            f"这是【分档对比】：请按「{label}」的中位数分两档（小于中位数的算「{low_label}」，"
            f"不小于的算「{high_label}」），SQL 用 CASE WHEN + GROUP BY 实现，"
            "回答里必须说明分界线是多少"
        )
    group_by = semantic.get("group_by")
    if group_by:
        if group_by == "pile_bucket":
            step = _pile_bucket_step(plan)
            alias = "d" if dialect == "mdl" else "p"
            bucket_start = f"(FLOOR({alias}.pile_distance / {step}) * {step})"
            bucket_end = f"({bucket_start} + {step})"
            label_sql = (
                f"CONCAT('K', FLOOR({bucket_start} / 1000), '+', "
                f"LPAD(MOD({bucket_start}, 1000), 3, '0'), '~K', "
                f"FLOOR({bucket_end} / 1000), '+', "
                f"LPAD(MOD({bucket_end}, 1000), 3, '0'))"
            )
            parts.append(
                f"必须按 {step} 米桩号段分组：只 GROUP BY "
                f"FLOOR({alias}.pile_distance / {step})；"
                f"**不要**再把 MOD({alias}.pile_distance, {step})、pile_number 或单点桩号放进 GROUP BY。"
                f"桩号段标签必须原样用：{label_sql} AS 桩号段。"
            )
            width = (plan.get("slots") or {}).get("linear_width_m")
            if width not in (None, ""):
                length_expr = (
                    "d.effective_disease_length"
                    if dialect == "mdl"
                    else _DISEASE_LENGTH_EXPR
                )
                parts.append(
                    f"病害换算面积 = {length_expr} × {float(width):g}，"
                    "同时 SELECT COUNT(*) AS 泛油数；"
                    "先按桩号段 SUM 换算面积，再按该汇总值降序取第一名"
                )
        else:
            parts.append(
                f"必须按「{_SEMANTIC_LABELS.get(group_by, group_by)}」分组（GROUP BY），每个分组各占一行"
            )
            parts.append(
                _time_bucket_hint(group_by, dialect, semantic.get("group_by_params"))
            )
    derived = [str(item) for item in (semantic.get("derived") or [])]
    if "ratio" in derived:
        parts.append("必须算出占比（部分 ÷ 总数 × 100%）")
    if "density" in derived:
        # 这里不要写 MDL 专有字段名（route_length_km 只存在于语义层）：
        # 物理表 basic_facilities_routes 没有这一列，物理 SQL 照抄会报 Unknown column
        # （实测：密度题重查时报 Unknown column 'r.route_length_km'）。
        parts.append("必须算出每公里密度（病害数量 ÷ 路线长度；路线长度按台账起止桩号相减算，不要用 distance 字段）")
    if "interval" in derived:
        parts.append("必须算出平均间距（路线长度（米）÷ 病害数量；路线长度按台账起止桩号相减算）")
    if "difference" in derived:
        parts.append("必须给出「谁更多、差多少」的结论，不要只丢两个数")
    metrics = [str(item) for item in (semantic.get("metrics") or [])]
    if metrics:
        parts.append("指标：" + "、".join(metrics))
    if semantic.get("shape") == "top" and group_by:
        parts.append("这是「按分组比数量取第一名」，不是取单条最大的明细")
    if semantic.get("order_by"):
        parts.append(f"排序：{semantic['order_by']}")
    if semantic.get("limit"):
        parts.append(f"取前 {semantic['limit']} 名")
    if semantic.get("output_fields"):
        parts.append(_output_fields_hint(list(semantic["output_fields"]), dialect, plan))
    if not parts:
        return ""
    return "语义理解（必须照此执行）：" + "；".join(parts)


_COLUMN_OWNERS: dict[str, dict[str, set[str]]] = {}


def _column_owners(kind: str) -> dict[str, set[str]]:
    """列名 → 拥有它的模型（MDL 索引，全自动；用于判断"这个字段属于本轮的表吗"）。"""
    cached = _COLUMN_OWNERS.get(kind)
    if cached is not None:
        return cached
    from . import wren_engine

    owners: dict[str, set[str]] = {}
    try:
        for card in wren_engine._schema_index(str(wren_engine._project_dir(kind))):
            for column_name, _note in card["columns"]:
                owners.setdefault(column_name, set()).add(str(card["name"]))
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "column_owners_failed", exc)
    _COLUMN_OWNERS[kind] = owners
    return owners


def prune_conditions_for_selection(plan: dict[str, Any], models: set[str] | None) -> list[str]:
    """选中的表里根本没有这个条件要用的表 → 本轮条件不适用，直接摘掉。

    实测：问"把边坡台账的明细调出来"时，语义层把"边坡"当成病害类型抽成 damage_type 条件，
    写进 SQL/口径就变成"边坡病害"，校验器判成答非所问，又把问题改写成了字段清单。
    表是选表器按语义挑出来的，它没选病害/字典表，就说明这条问题不是病害题。
    """
    picked = {str(item) for item in (models or set())}
    if not picked:
        return []
    dropped: list[str] = []
    required = plan.get("required_conditions") or []
    if "damage_type" in required and not picked & {"disease", "damage_dict"}:
        plan["required_conditions"] = [item for item in required if item != "damage_type"]
        slots = plan.get("slots")
        if isinstance(slots, dict):
            slots.pop("damage_matches", None)
            slots.pop("damage_type", None)
        dropped.append("damage_type")
    if dropped:
        log_event(
            "nl2sql",
            "INFO",
            "conditions_pruned",
            dropped="、".join(dropped),
            tables="、".join(sorted(picked)),
            question=str(plan.get("message") or "")[:60],
        )
    return dropped


def _mdl_anchor_model(plan: dict[str, Any]) -> tuple[str, frozenset[str]]:
    """本问题主体的 MDL 模型名 + 它的列名集合（不认识就退回 disease）。

    主体 → 模型不写死映射：主体给的是物理表名（parsers.SUBJECT_MAIN_TABLE），拿它去
    MDL 索引里反查模型名，库结构变了也不用改代码。
    """
    subject = str(plan.get("subject") or "disease")
    # SUBJECT_MAIN_TABLE 只登记了主业务表；route/asset 这类主体名本身就是（或出现在）模型名里，
    # 直接拿主体名去索引里找，别一律退回 disease（实测"路线台账明细"被当成病害）。
    table = parsers.SUBJECT_MAIN_TABLE.get(subject) or subject
    if not table:
        return "disease", frozenset()
    from . import wren_engine

    kind = str(plan.get("kind") or "mysql")
    cards = wren_engine._schema_index(str(wren_engine._project_dir(kind)))
    selected: set[str] = set()
    try:
        selected = select_tables(str(plan.get("message") or ""), kind, subject=subject)
    except Exception as exc:  # noqa: BLE001
        log_error("nl2sql", "anchor_select_failed", exc)

    def columns_of(model_name: str) -> frozenset[str]:
        for card in cards:
            if card["name"] == model_name:
                return frozenset(name for name, _ in card["columns"])
        return frozenset()

    # 只有主体粗到 asset 时才用选表结果改锚点；patrol/route 等明确主体不能被
    # “答案字段在 route 上”带跑（任务涉及路线仍要从 patrol 出发）。
    if subject == "asset" and selected and table not in selected:
        ranked = [
            item["name"]
            for item in wren_engine.search_models(kind, str(plan.get("message") or ""), limit=6)
            if item["name"] in selected
        ]
        if ranked and ranked[0] != table:
            return ranked[0], columns_of(ranked[0])
    for card in cards:
        if card["name"] == table or card["table"] == table:
            return str(card["name"]), frozenset(name for name, _ in card["columns"])
    for card in cards:
        if table and (table in str(card["name"]) or table in str(card["table"])):
            return str(card["name"]), frozenset(name for name, _ in card["columns"])
    return "disease", frozenset()


def mdl_condition_lines(plan: dict[str, Any]) -> list[str]:
    """给 Wren MDL-SQL 用的必须条件（模型名 + 原样照抄的字面值）。

    和 required_condition_lines 的区别：那一份是物理 SQL 写法（内置 LLM 用），
    这一份只出现 MDL 模型名。字面值必须原样——实测模型会把「宝安中心--恒明珠」
    写成「宝安中心--恒明珠段」，SQL 直接查空（F7 时对时错就是这个原因）。
    """
    lines: list[str] = []
    # 本条问题的主体模型：条件要挂在它身上，不能一律写成 disease.xxx。
    # 实测"病害工单的处理状态"主体已经是 work_order，但条件里还写 disease.del_flag=0，
    # 模型就照着 FROM disease 写了（用户自己的叫法永远对不上表名，只能靠主体锚定）。
    anchor, anchor_columns = _mdl_anchor_model(plan)
    if anchor != "disease":
        lines.append(
            f"本条问题的主体是「{plan.get('subject')}」：FROM 用 {anchor}，不要用 disease 作答"
            "（需要别的表时按 MDL 关系显式 JOIN）。"
        )

    def qualified(column: str) -> str:
        """主体模型上有这一列就用它，没有才退回 disease（并说明要 JOIN 过去）。"""
        if column in anchor_columns:
            return f"{anchor}.{column}"
        return f"disease.{column}"

    def join_note(column: str) -> str:
        if anchor == "disease" or column in anchor_columns:
            return ""
        return f"（{anchor} 没有 {column}：需要时按 MDL 关系 JOIN disease 再按 disease.{column} 过滤）"

    summary = semantic_summary(plan, dialect="mdl")
    if summary:
        lines.append(summary)
    lines.extend(condition_lines(plan))
    slots = plan["slots"]
    required = plan["required_conditions"]
    if "route" in required and slots.get("route_match"):
        route = slots["route_match"]
        # 用 id 而不是名称：名称有 short_name/full_name 混用风险（写成名称可能查空），
        # 而且语义校验要求 SQL 里出现路线 id。JOIN 必须显式写，Wren 不做隐式关联。
        route_id = str(route.get("id") or "")
        name = str(route.get("short_name") or route.get("full_name") or route.get("code") or "")
        lines.append(
            f"路线：JOIN route ON {qualified('routes_id')} = route.id AND route.id = '{route_id}'"
            f"（这是「{name}」的 id，原样照抄；不要用名称过滤）"
            + join_note("routes_id")
        )
    if "section" in required and (slots.get("section_match") or {}).get("id"):
        section = slots["section_match"]
        section_id = str(section.get("id") or "").replace("'", "''")
        name = str(section.get("name") or "")
        if anchor == "section":
            lines.append(
                f"路段：section.id = '{section_id}'（这是「{name}」的路段 id，原样照抄；"
                "不要用名称过滤）"
            )
        else:
            lines.append(
                f"路段：JOIN section ON disease.sections_id = section.id "
                f"AND section.id = '{section_id}'（这是「{name}」的路段 id，原样照抄；"
                "不要用名称过滤）"
            )
    if "damage_type" in required and slots.get("damage_matches"):
        values = ", ".join(f"'{item['problem_type']}'" for item in slots["damage_matches"])
        lines.append(
            f"病害类型：JOIN damage_dict ON {qualified('problem_base_id')} = damage_dict.id "
            f"AND damage_dict.problem_type IN ({values})（原样照抄）"
            + join_note("problem_base_id")
        )
    if "pile_range" in required:
        lines.append(
            f"桩号：{qualified('pile_distance')} BETWEEN {int(slots['pile_start'])} "
            f"AND {int(slots['pile_end'])}" + join_note("pile_distance")
        )
    if "time_range" in required:
        # 「6月和7月各多少」是**两个独立月份**，不能只写成一个连续区间
        # （实测 query plan 给的就是 6/1~7/1，LLM 只查了 6 月）。这里把并列月份展开成两列。
        months = parsers.parse_month_metrics(plan.get("message", ""))
        # 「平均每天」= 该时段病害数 ÷ 该时段**实际有采集记录的天数**，
        # 不能拿固定 30/31 天去除（实测 Wren 路径因此算出 10.77 / 44.48，真值 53.8 / 197）。
        daily = any(word in plan.get("message", "") for word in ("平均每天", "日均", "每天平均"))
        if len(months) >= 2:
            columns = ", ".join(
                (
                    f"ROUND(COUNT(CASE WHEN disease.collection_time >= '{_stamp(start)}' "
                    f"AND disease.collection_time < '{_stamp(end)}' THEN 1 END) "
                    f"/ NULLIF(COUNT(DISTINCT CASE WHEN disease.collection_time >= '{_stamp(start)}' "
                    f"AND disease.collection_time < '{_stamp(end)}' THEN DATE(disease.collection_time) END), 0), 1) "
                    f"AS `{label}平均每天`"
                    if daily
                    else f"SUM(CASE WHEN disease.collection_time >= '{_stamp(start)}' "
                    f"AND disease.collection_time < '{_stamp(end)}' THEN 1 ELSE 0 END) AS `{label}`"
                )
                for label, start, end in months[:4]
            )
            lines.append(f"时间（每个月份单独一列，必须全查）：SELECT {columns}")
            if daily:
                lines.append(
                    "「平均每天」的分母是**该月实际有采集记录的天数**（COUNT(DISTINCT DATE(...))），"
                    "不是固定 30/31 天，也不要用整段区间去平均"
                )
        else:
            bounds = []
            if slots.get("time_start"):
                bounds.append(f"{qualified('collection_time')} >= '{slots['time_start']}'")
            if slots.get("time_end"):
                bounds.append(f"{qualified('collection_time')} < '{slots['time_end']}'")
            if bounds:
                lines.append("时间：" + " AND ".join(bounds) + join_note("collection_time"))
    if "device_code" in required and slots.get("device_id"):
        lines.append(
            f"设备：{qualified('device_code')} = '{slots['device_id']}'" + join_note("device_code")
        )
    if "has_image" in required:
        lines.append(
            f"有图片：{qualified('has_image')} = TRUE（不用 JOIN）" + join_note("has_image")
        )
    if "comparison" in required:
        lines.append("对比：必须按对象分组（GROUP BY），每个对象各占一行，不要只给一个总数")
    if plan.get("route_scope_from_patrol"):
        lines.append(
            "路线范围：必须从巡查任务经路线关联表——"
            "FROM patrol t JOIN patrol_route pr ON pr.patrol_id = t.id "
            "JOIN route r ON pr.routes_id = r.id；输出 DISTINCT 路线名称。"
            "不能只 FROM route，否则会把整个路线台账当成任务涉及的路线。"
        )
    semantic_group = str(
        (plan.get("semantic") or {}).get("group_by")
        or (plan.get("slots") or {}).get("group_by")
        or ""
    )
    if semantic_group == "route" and str(plan.get("subject") or "") == "disease":
        lines.append(
            "路线分组：disease 模型已提供 route_name，直接 "
            "SELECT disease.route_name, COUNT(*) GROUP BY disease.route_name；"
            "不要为了取路线名再 JOIN route，避免物化 route_length_km。"
        )
    if _asks_length_or_density(plan):
        lines.append(_length_hint("mdl"))
    public = slots.get("routes") or []
    if plan.get("multi_route") and public:
        ids = ", ".join("'" + str(item["id"]).replace("'", "''") + "'" for item in public)
        names = "、".join(str(item.get("name") or "") for item in public)
        lines.append(
            f"多路线（{names}）：路线过滤只写 route.id IN ({ids})，"
            "不要用 route.short_name 过滤（用户写的简称不是库里的名称，会查空）"
        )
    # 字典/路线模型是主数据，没有业务删除语义；只有病害表需要 del_flag=0。
    lines.append(
        f"有效数据：{qualified('del_flag')} = 0（route / damage_dict 不要再加 del_flag 条件）"
    )
    return lines


def generate_with_context(plan: dict[str, Any], context: dict[str, str], feedback: str = "") -> str:
    response = generate_sql(
        question=plan["message"],
        kind=plan["kind"],
        schema_context=context.get("schema", ""),
        examples=context.get("examples", ""),
        required_conditions=required_condition_lines(plan),
        slots=plan["slots"],
        feedback=feedback,
        # 字段真实取值（路线名/病害类型/巡查类型…）：让模型"先查一眼再写"，别瞎猜（升级6）
        samples=value_samples(plan["kind"]),
    )
    return extract_sql(response)
