"""字段触达（docs/字段目录配置与字段触达前置方案.md §3.2/§3.4）。

一句话里如果点了某张台账表的某一列（"隧道表的通风方式明细""隧道净宽净高"），
这句话的答案就是"那张表那一列的值"——把这件事**在本地确定性地算出来**，交给
plan / 选表 / 候选 / 写 SQL 各环节复用，避免：

  · 命中证据只当"软提示"给意图模型，模型一犹豫就落到别的表（实测落到 ai_asset_main）；
  · 表结构明明有这一列，却反问用户"用哪个字段"。

词项全部从表结构（表名/表注释/列名/列注释/枚举中文）**自动派生**，不手写业务词；
库结构变了重建字段目录即可（`schema_catalog` 按 5 分钟缓存重读 information_schema）。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from . import schema_catalog, wren_engine
from .logging_service import log_event

# 结构性停用列：每张表都有、单独命中不能作为"这句话问的是这一列"的证据
_STOP_COLUMNS = {
    "id",
    "del_flag",
    "tenant_id",
    "create_by",
    "create_time",
    "create_dept",
    "update_by",
    "update_time",
    "remark",
    "version",
}
_TABLE_PREFIXES = ("basic_facilities_", "ai_", "maintenance_", "project_manage_", "basic_")

def _subject_of(table: str) -> str:
    """表名 → 主体名（只做前缀剥离与复数归一，不维护业务词表）。"""
    name = str(table or "").lower()
    for prefix in _TABLE_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    if name.endswith("ies"):
        name = name[:-3] + "y"
    elif name.endswith("s") and not name.endswith("ss"):
        name = name[:-1]
    return name


def _column_roles(column: dict[str, Any]) -> list[str]:
    return [str(item) for item in column.get("roles") or []]


# ── 「这张表能不能给出用户点名的那一列」按**列族/角色**判，不按字面列名 ──────────────
# 语义层给的是语义字段名（lat/lng/start_num…），同一件事在不同表的物理列名并不一样：
#   · 经纬度：ai_problem 叫 lat/lng，而 sections/routes 叫 start_lat/start_lnt/end_lat…
#   · 起止桩号：sections 叫 start_num/end_num，tunnels 叫 stake_start_num/stake_end_num
# 只看字面列名会把"其实给得出来"的表判成给不出来，落点就被换到别的表（实测"路段的起止桩号"
# 落到了桩点表）。所以这里按**角色与列族**判：geo 角色算经纬度；start/end + num/pile/stake/桩
# 的构成算起止桩号族。
_GEO_FIELD_HINTS = ("lat", "lng", "lon", "lnt", "纬度", "经度", "经纬度", "坐标")
_PILE_FIELD_HINTS = ("start_num", "end_num", "起点桩号", "终点桩号", "起止桩号", "桩号范围")
_PILE_SIDE_RE = re.compile(r"(start|end|起|止|终)", re.IGNORECASE)
_PILE_WORD_RE = re.compile(r"(num|pile|stake|桩)", re.IGNORECASE)


def _field_families(field: str) -> set[str]:
    """语义层点名的字段 → 需要的列族（认不出来返回空 = 只能按字面列名/标签解析）。"""
    name = str(field or "").strip().lower()
    if not name:
        return set()
    if any(hint in name for hint in _GEO_FIELD_HINTS):
        return {"geo"}
    if any(hint in name for hint in _PILE_FIELD_HINTS):
        return {"pile_start_end"}
    return set()


def _column_families(column: dict[str, Any]) -> set[str]:
    """这一列属于哪些列族（角色优先，其次名字/标签的构成）。"""
    families: set[str] = set()
    if "geo" in _column_roles(column):
        families.add("geo")
    name = str(column.get("column") or "")
    blob = f"{name} {column.get('label') or ''}"
    if _PILE_SIDE_RE.search(name) and _PILE_WORD_RE.search(blob):
        families.add("pile_start_end")
    return families


def _expresses(
    kind: str, table: str, field: str, columns: list[dict[str, Any]] | None = None
) -> bool:
    """这张表能不能表达语义层点名的那个字段（先按列族/角色，其次列名/中文标签）。

    两条判据取并集：列族对得上（经纬度 → geo 角色、起止桩号 → start/end+num 族）**或**
    目录能按列名/中文标签解析到。判得松一点是故意的——宁可让它去查，也不要把"其实答得出来"
    的问题误报成"库里没这个字段"。
    """
    families = _field_families(field)
    if families:
        if columns is None:
            columns = [col for col in _columns(kind) if str(col["table"]) == table]
        if any(families & _column_families(col) for col in columns):
            return True
    # 列名/中文名对得上就算能表达——**必须在含语义字段的列清单里判**：
    # `confidence` 这类语义字段（pic_position 的 AI 置信度）不在 information_schema 里，
    # 只查目录会把它判成"这张表没这列"，于是回"库里查不到 confidence"（实测）。
    scoped = columns if columns is not None else [
        col for col in _columns(kind) if str(col["table"]) == table
    ]
    wanted = str(field or "").strip().lower()
    squashed = re.sub(r"[\s_\-（）()]+", "", wanted)
    for col in scoped:
        if wanted and str(col["column"]).strip().lower() == wanted:
            return True
        label = re.sub(r"[\s_\-（）()]+", "", str(col["label"] or "").lower())
        if squashed and label == squashed:
            return True
    return schema_catalog.resolve_column(kind, table, field) is not None


def _phrase_hit(label: str, text: str) -> bool:
    """列注释里的"短名字"整段出现在问题里（"通风方式""净宽"这种最硬的证据）。"""
    name = str(label or "").strip()
    if len(name) < 2 or len(name) > 8:
        return False
    return name in text


def _code_value_hits(code_map: dict[str, str], text: str) -> list[str]:
    """用户直接说了枚举中文值（"全面通风"）——也算命中这一列。

    **单字取值不算**：库里有 21 个布尔列的取值就是"是/否"（is_built、main_focus、is_close、
    built、is_rail_road、is_lighting…），中文句子几乎都含"是"，按"取值命中"给 +1.5 分就等于
    给一堆无关列加分——实测"西乡段和恒明珠分别**是**几车道"命中了 6 个不相关列、把落点表挤成空，
    "…的基本情况是什么样的"也把桥梁表的 is_rail_road 顶进了候选
    （docs/同名列多表消歧方案.md §七.4）。
    """
    return [
        str(label)
        for label in (code_map or {}).values()
        if len(str(label).strip()) >= 2 and str(label) in text
    ]


@lru_cache(maxsize=8)
def _columns(kind: str) -> tuple[dict[str, Any], ...]:
    """白名单表的列清单（含注释、枚举映射、角色）；结构变化由 schema_catalog 的 TTL 兜。

    **语义字段也要进来**（pic_position 的 JSON 字段、join 出来的中文名…）：它们不在
    information_schema 里，只在字段登记表里。少了它们，三处都会出错：
      · 列级证据（"标注经度"命中不到 pic_lng）；
      · "这张表没有这列"的如实回答闸 → 明明登记过的 `pic_lat` 被判成"17 张表都没有"（实测）；
      · 跨表多字段解析（`confidence` 只能落到字典表的同名列上 → 拆题）。
    """
    from . import field_catalog

    rows: list[dict[str, Any]] = []
    for table in schema_catalog.field_catalog(kind).get("tables") or []:
        table_name = str(table.get("table") or "")
        for column in table.get("columns") or []:
            name = str(column.get("name") or "")
            if not name or name in _STOP_COLUMNS:
                continue
            rows.append(
                {
                    "table": table_name,
                    "table_label": str(table.get("label") or table_name),
                    "column": name,
                    "label": str(column.get("label") or name),
                    "comment": str(column.get("comment") or ""),
                    "code_map": dict(column.get("code_map") or {}),
                    "aliases": [str(item) for item in column.get("aliases") or [] if str(item).strip()],
                    "roles": _column_roles(column),
                }
            )
    # 语义字段：登记在 field_catalog._SPECIAL_FIELDS 里，归属表也在那儿标着（ai_problem）
    try:
        existing = {(str(item["table"]), str(item["column"])) for item in rows}
        for row in field_catalog._field_rows(kind):  # noqa: SLF001
            table_name = str(row.get("table") or "")
            name = str(row.get("name") or "")
            if not table_name or not name or (table_name, name) in existing:
                continue
            rows.append(
                {
                    "table": table_name,
                    "table_label": schema_catalog.table_label(kind, table_name),
                    "column": name,
                    "label": str(row.get("label") or name),
                    "comment": str(row.get("label") or ""),
                    "code_map": {},
                    "roles": [],
                }
            )
    except Exception as exc:  # noqa: BLE001
        log_error("field_grounding", "special_fields_failed", exc, kind=kind)
    return tuple(rows)


@lru_cache(maxsize=8)
def _table_texts(kind: str) -> tuple[tuple[str, str], ...]:
    """(表名, 表名+表注释) —— 用来判"用户点的是哪张表"。"""
    out: list[tuple[str, str]] = []
    for table in schema_catalog.field_catalog(kind).get("tables") or []:
        name = str(table.get("table") or "")
        if not name:
            continue
        out.append((name, f"{name} {table.get('label') or ''} {table.get('comment') or ''}"))
    return tuple(out)


def _stats_question(plan: dict[str, Any] | None) -> bool:
    """是不是"统计/分组"题（count/sum/avg/distribution/trend/top/bucket_compare 或带分组维度）。

    用来区分两种"点了列名"的问法：统计题里点的列往往是**分组维度或过滤条件**
    （"各病害类型分别有多少处"里的"病害类型"），主体仍是主表；而"XX 有哪些取值"
    这类题点的列就是答案本身，落点应该跟着它走。
    """
    if not plan:
        return False
    shape = str(plan.get("shape") or "")
    if shape in {"count", "sum", "avg", "distribution", "trend", "top", "bucket_compare"}:
        return True
    slots = plan.get("slots") or {}
    # 签名里 plan 是可选的，但这里直接解引用会炸（实测 ground(kind, text) 抛 AttributeError）。
    # 没给计划时就当"没有语义判断"，只走本地列匹配那条路。
    semantic = (plan or {}).get("semantic") or {}
    return bool(slots.get("group_by") or semantic.get("group_by"))


def ground(
    kind: str,
    text: str,
    table_limit: int = 3,
    column_limit: int = 6,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """这句话最可能落在哪张表、哪一列。

    返回：tables（表级，复用 wren_engine.search_models 的打分与命中证据）、
    columns（列级，标注 role/code_map/命中原因）、table（首选表）、subject。

    plan（可选）：语义计划。用来区分"统计题点的分组维度"和"问某列取值"——
    前者不把落点翻到字典表，后者翻（见 docs/冷门字典列可达与generic精确命中方案.md）。
    """
    message = str(text or "")
    if not message.strip():
        return {"tables": [], "columns": [], "table": "", "subject": ""}
    # search_models 给的是**Wren 模型名**（route / section / disease…），而字段目录、表注释、
    # 通用算子用的都是**物理表名**（basic_facilities_routes…）。这里立刻换算成物理表名：
    # 否则"用户点的是哪张表"的判断会因为名字对不上而落空，退化成"按最强列所属表"——
    # 实测"…分别是几车道"在 basic_facilities_routes / basic_facilities_sections 之间来回跳
    # （两张表都有 lane_count），推荐结果跟着变。
    tables: list[dict[str, Any]] = []
    for item in wren_engine.search_models(kind, message, limit=table_limit):
        model = str(item.get("name") or "")
        physical = next(iter(wren_engine.physical_tables(kind, {model})), "")
        tables.append({**item, "model": model, "name": physical or model, "physical": physical})
    query_terms = wren_engine._terms(message)  # noqa: SLF001 —— 和表级检索共用同一套分词
    # 表级检索的排名（语义序）：同分时用它拍板，不用表名字典序——实测"基本情况"两边都靠
    # 表注释里的"基本设施_"拿到 1.0 分，按字母序会选到 basic_facilities_bridges，
    # 而这句话问的是路线（docs/同名列多表消歧方案.md §四.3/§五.3）。
    table_rank = {str(item.get("name")): index for index, item in enumerate(tables)}
    # "这句话问的是哪张表"只认**表名/表注释**的词元重合（用户点名"桥梁/隧道"）：
    # search_models 的总分会把"列注释碰巧像"的表顶上来（"桥梁的技术评定等级分布"里隧道表
    # 也有评定等级列、列多所以总分更高），它的 table_score 又掺了罕见单字加分（"净宽净高"
    # 的"高"字会让里程桩点表拿到分）——两样都不能直接当落点。
    top_table = ""
    best_identity = 0.0
    best_rank = len(table_rank)
    # 枚举值命中反哺表级：用户说的是某列的**枚举中文值**（"国道"/"特大桥"/"上行"）时，
    # 这句话很可能就是在问这张表——表级只看表名/表注释会漏掉这种诉求
    # （实测"记录的国道总共有多少条"里"国道"是 basic_facilities_routes.level 的枚举值，
    #  表级没有"路线"这个词，落点被"记录"带到了 ai_asset_main）。
    # 单字枚举（是/否/无）已在 _code_value_hits 里按长度≥2 过滤，不会误加分。
    table_evidence: dict[str, float] = {}
    for column in _columns(kind):
        if not column["code_map"]:
            continue
        code_hits = _code_value_hits(column["code_map"], message)
        if code_hits:
            owner = str(column["table"])
            table_evidence[owner] = table_evidence.get(owner, 0.0) + 1.0 * len(code_hits)
    # 语义层判过的主体对应哪张表：**只在表级证据打平时**当偏好键用，不并进 table_evidence——
    # 它有别的用途（"这张表答得出来吗"的判定会读 table_evidence，混进来会把"桥梁的经纬度"
    # 这种题跳到资产表）。
    subject = str((plan or {}).get("subject") or "")
    subject_target = ""
    if subject:
        try:
            from . import capabilities  # 懒导入：capabilities 反过来会 import 本模块

            subject_target = str(capabilities._SUBJECT_TABLES.get(subject) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            subject_target = ""
    best_key: tuple[float, int, int] | None = None
    for name, haystack in _table_texts(kind):
        # 表级候选（search_models 排出来的）也算"这句话可能问的是它"：按它的**表名证据分**
        # 参与比较（这样 MDL 模型名换算过来的物理表能正常参与，不再全靠列证据兜底）
        overlap = query_terms & wren_engine._terms(haystack)  # noqa: SLF001
        name_score = sum(len(token) ** 2 for token in overlap) / 4.0 if overlap else 0.0
        score = round(table_evidence.get(name, 0.0) + name_score, 4)
        if score <= 0:
            continue
        table_evidence[name] = score
        key = (
            score,
            1 if (subject_target and name == subject_target) else 0,
            -table_rank.get(name, len(table_rank)),
        )
        if best_key is None or key > best_key:
            best_key, top_table = key, name
    if top_table:
        best_identity = table_evidence.get(top_table, 0.0)
        best_rank = table_rank.get(top_table, len(table_rank))
    scored: list[dict[str, Any]] = []
    for column in _columns(kind):
        label = column["label"]
        comment = column["comment"]
        haystack = f"{label} {comment} {column['column']}"
        overlap = query_terms & wren_engine._terms(haystack)  # noqa: SLF001
        score = sum(len(token) ** 2 for token in overlap) / 4.0
        own_score = score
        hits: list[str] = []
        if overlap:
            hits.append("列注释：" + "、".join(sorted(overlap, key=len, reverse=True)[:3]))
        if _phrase_hit(label, message):
            score += 3.0
            own_score += 3.0
            hits.insert(0, f"列名命中：{label}")
        # 登记的列别名（"病害名称"→problem_type）：和列名命中同权重。库里的注释是"病害类型"，
        # 用户口语说"病害名称"时注释匹配不上，点名的字段会整列丢掉
        # （实测"看一下路面病害的病害样图以及对应的病害代码对应的病害名称"）。
        for alias in column.get("aliases") or []:
            if _phrase_hit(alias, message):
                score += 3.0
                own_score += 3.0
                hits.insert(0, f"列别名命中：{alias}")
                break
        code_hits = _code_value_hits(column["code_map"], message)
        if code_hits:
            score += 1.5 * len(code_hits)
            own_score += 1.5 * len(code_hits)
            hits.append("取值命中：" + "、".join(code_hits[:3]))
        # 列必须有自己的证据（列名/列注释/枚举取值），只靠"属于被点名的表"不算
        if own_score <= 0:
            continue
        if column["table"] == top_table:
            score += 1.0
        scored.append({**column, "score": round(score, 2), "own_score": round(own_score, 2), "hits": hits})
    # 同分时按**语义表序**（表级检索的排名），不按表名字典序——实测"西乡段和恒明珠的车道数
    # 分别是多少"里 routes/sections/tunnels 的"车道数"列同分，原来的次键是表名字母序
    # （basic_facilities_routes < ..._sections < ..._tunnels），选对整条路的粒度纯属运气
    # （docs/同名列多表消歧方案.md §五.3/§七.3）。表级排名来自 search_models，是按语义算的。
    scored.sort(
        key=lambda item: (
            -item["score"],
            table_rank.get(str(item["table"]), len(table_rank)),
            item["table"],
            item["column"],
        )
    )
    # 列级硬命中（列名整段出现在问题里）说明用户点了具体列：字典列（`*_base` 表）被点名时，
    # 它的表优先作为落点，压过"病害/资产"这类表级通用词——否则"病害的计算公式有哪些"
    # 会被锁到 ai_problem 主表，formula 整个被滤掉（列级 +3 分救不回来）。
    # 统计题除外："各病害类型分别有多少处"点的"病害类型"是分组维度，主体仍是病害。
    if top_table and not _stats_question(plan):
        for item in scored:
            if not any(str(hint).startswith("列名命中：") for hint in item.get("hits") or []):
                continue
            table = str(item.get("table") or "")
            if table.endswith("_base") and table != top_table:
                log_event(
                    "field_grounding",
                    "INFO",
                    "landing_table_from_column_phrase",
                    frm=top_table,
                    to=table,
                    column=str(item.get("column")),
                    question=message[:60],
                )
                top_table = table
            break
    # 表级证据没过阈值、但列级证据很硬时（"隧道净宽净高"没点"隧道"两个字），
    # 用最强列的所属表当落点——列本身就是"某张表的某一列"，比表级分数更具体。
    if not top_table and scored and float(scored[0]["score"]) >= 3.0:
        top_table = str(scored[0]["table"])
    # ── 落点表必须**答得出来**：能给出语义层点名的那些列（按列族/角色判，见 _field_families）
    # 表级词元/检索分只回答"这句话像在问哪张表"，回答不了"这张表有没有这些列"。实测反例：
    # "资产的经纬度"的 output_fields=[lng,lat]，落点却是资产**字典**表 ai_asset_base（一个经纬度列
    # 都没有）→ 卡片列的是资产代码/颜色/图标；"路段的起止桩号"（start_num/end_num）落到桩点表。
    table_candidates: list[dict[str, Any]] = []
    missing_fields: list[str] = []
    unavailable_fields: list[str] = []
    pending_fields: list[str] = []
    field_tables: dict[str, list[str]] = {}
    semantic = plan.get("semantic") or {}
    # 点名字段 = 目录能识别的 + **目录里没有的**（"桥在哪个省" → province）：后者全库都表达不了，
    # 会被下面的 unavailable_fields 收住，交给下游如实回答。
    # 两个来源都要收（output_fields 是"要看的列"，unknown_output_fields 是"模型确认列清单里
    # 没有的名字"）；模型经常两处都填同一个名字，这里**保序去重**，免得如实回答里出现"province、
    # province"这种重复。
    wanted = list(
        dict.fromkeys(
            str(item)
            for item in list(semantic.get("output_fields") or [])
            + list(semantic.get("unknown_output_fields") or [])
            if item
        )
    )
    if wanted:
        by_table: dict[str, list[dict[str, Any]]] = {}
        for column in _columns(kind):
            by_table.setdefault(str(column["table"]), []).append(column)
        # 目录读库失败时 field_catalog 会缓存成"0 张表"（5 分钟）：这时**不能**判
        # "库里没有这个字段"——lat/lng 都会因为白名单空掉而被误判（实测踩到）。
        if not by_table:
            log_event(
                "field_grounding",
                "WARNING",
                "catalog_empty_skip_expressibility",
                kind=kind,
                question=str(message)[:60],
            )
            wanted = []
    if wanted:
        owners: dict[str, list[str]] = {field: [] for field in wanted}
        for name, columns in by_table.items():
            for field in wanted:
                if _expresses(kind, name, field, columns):
                    owners[field].append(name)
        # 全库都没有的字段（"桥在哪个省"这种库里根本没入库的信息）：下游要如实说明，
        # 不能拿别的列顶替，也不该假装"没查到"。
        # 层4（docs/派生指标与跨表字段语义可达方案.md）：**本地匹配不到 ≠ 库里没有**。
        # 本地是字面匹配（"路段长度" vs 列注释），而语义层看得到完整列清单，可能把它映射到
        # `section_length_km` 这种派生列（层1 之后目录里已经有它）或另一个叫法上。
        # 所以只有**模型自己也说"列清单里没这个名字"**（`unknown_output_fields`）时才算不可达；
        # 其余未命中先挂 pending，交给规划/写 SQL 那层去映射，不在这里提前判死。
        model_unknown = {str(item) for item in (semantic.get("unknown_output_fields") or [])}
        pending_fields = [field for field in wanted if not owners[field] and field not in model_unknown]
        unavailable_fields = [field for field in wanted if not owners[field] and field in model_unknown]
        # 不截断：调用方要按这些表的列注释挑最通用的中文名（lat → 资产纬度/病害纬度/纬度）
        field_tables = {field: owners[field] for field in wanted}
        expressible = {
            name: sum(1 for field in wanted if name in owners[field]) for name in by_table
        }
        best_expressible = max(expressible.values(), default=0)
        if best_expressible:
            capable = [name for name, count in expressible.items() if count == best_expressible]
            if top_table not in capable:
                # 只换成"用户确实也可能在问它"的表（表级证据 > 0），先看证据再看语义表序。
                # 没有任何答得出来的表有表级证据时**不换**：说明用户点的那张主体表本来就没有这类列
                # （实测"桥梁的经纬度"——桥梁表一个经纬度列都没有），换到别的表就是答非所问；
                # 留在主体表上，缺哪些列记进 missing_fields，由下游如实说明。
                with_evidence = [name for name in capable if table_evidence.get(name, 0.0) > 0]
                if with_evidence:
                    top_table = min(
                        with_evidence,
                        key=lambda name: (
                            -table_evidence.get(name, 0.0),
                            table_rank.get(name, len(table_rank)),
                            name,
                        ),
                    )
            # 与主表**表级证据并列**的表都留着：优先级不写死，几张表都说得通时下游把它们都摆出来，
            # 让用户/模型自己判（用户要求"允许 Top1 并列"）。
            tied = [
                name
                for name in capable
                if table_evidence.get(name, 0.0) == table_evidence.get(top_table, 0.0)
            ]
            ranked = sorted(
                tied or [top_table],
                key=lambda name: (
                    name != top_table,
                    -table_evidence.get(name, 0.0),
                    table_rank.get(name, len(table_rank)),
                    name,
                ),
            )
            table_candidates = [
                {
                    "table": name,
                    "evidence": round(table_evidence.get(name, 0.0), 2),
                    "matched": expressible.get(name, 0),
                    "of": len(wanted),
                }
                for name in ranked[:3]
            ]
        # 主表给不出来的点名列（含全库都没有的）——下游据此如实回答，不拿别的列顶替
        missing_fields = [
            field
            for field in wanted
            if not (top_table and _expresses(kind, top_table, field, by_table.get(top_table)))
        ]
    # 落点表确定后，列证据只保留这张表的（"技术评定等级"隧道和桥梁都有，
   # 但用户点的是桥梁，就不该把隧道那一列摆到前面）
    # 真分不清的歧义记下来（同名列跨多张表、得分并列、表级也没证据）：以前这种局面完全靠
    # 表名序拍板、外面看不出发生过什么（docs/同名列多表消歧方案.md §五.2）。落点表一旦定了
    # 就不算歧义——表级证据已经做了判断。
    ambiguous: list[dict[str, Any]] = []
    if not top_table:
        by_name: dict[str, list[dict[str, Any]]] = {}
        for item in scored:
            by_name.setdefault(str(item["column"]), []).append(item)
        for name, group in by_name.items():
            if len({str(item["table"]) for item in group}) < 2:
                continue
            best = max(float(item["own_score"]) for item in group)
            tied = [item for item in group if float(item["own_score"]) == best]
            if len({str(item["table"]) for item in tied}) < 2:
                continue
            ambiguous.append(
                {
                    "column": name,
                    "candidates": [
                        {"table": str(item["table"]), "label": str(item["label"])}
                        for item in tied
                    ],
                }
            )
    if ambiguous:
        log_event(
            "field_grounding",
            "INFO",
            "column_ambiguous_across_tables",
            columns="、".join(str(item["column"]) for item in ambiguous)[:80],
            question=message[:60],
        )
    # 按表分组的列证据：一句话要了 A 表的 X + B 表的 Y 时，落点表只能是一张，但"哪些列在哪张表
    # 命中过"必须留下来，否则另一半诉求就没了（docs/跨表多字段并列查询_系统性整改方案.md §三 层 2）。
    # 注意要在下面按落点表过滤**之前**取。
    table_fields: dict[str, list[dict[str, Any]]] = {}
    for item in scored:
        table_fields.setdefault(str(item["table"]), []).append(
            {
                "column": str(item["column"]),
                "label": str(item.get("label") or ""),
                "score": item.get("score"),
            }
        )
    if top_table:
        scored = [item for item in scored if str(item["table"]) == top_table]
    return {
        "tables": tables,
        "columns": scored[:column_limit],
        "table": top_table,
        "subject": _subject_of(top_table) if top_table else "",
        "ambiguous": ambiguous,
        # 表 → 该表命中的列（全部保留，主表排最前）：跨表多字段题靠它找"另一半字段在哪张表"
        "table_fields": {
            name: table_fields[name]
            for name in sorted(table_fields, key=lambda item: (item != top_table, item))
        },
        # 落点候选（主表在前）：能给出语义层点名列、且表级证据并列的表。下游照着它把
        # "几张表都说得通"的口径都摆出来，而不是只认主表。
        "table_candidates": table_candidates,
        # 主表给不出来的点名列（正常是空）：说明用户点的主体表本来就没有这类列
        # （实测"桥梁的经纬度"），下游应当如实说明，而不是拿别的表的列顶替。
        "missing_fields": missing_fields,
        # 全库都没有的字段（"桥在哪个省"）：这类要答"库里没有这项记录"，不是"没查到"
        "unavailable_fields": unavailable_fields,
        # 本地没匹配上、但模型没确认"列清单里没有"的字段：先挂着，别判死
        "pending_fields": pending_fields,
        # 点名字段各自在哪几张表里有（给"这个字段在 X 表里有"这种引导用）
        "field_tables": field_tables,
    }


def tables_of(plan: dict[str, Any], limit: int = 2) -> list[str]:
    """plan 里触达到的表（按分数倒序）。"""
    grounded = plan.get("grounded") or {}
    out: list[str] = []
    for item in grounded.get("tables") or []:
        name = str(item.get("name") or "")
        if name and name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def table_of(plan: dict[str, Any]) -> str:
    """plan 里触达到的首选表（没有就返回空串）。"""
    return str((plan.get("grounded") or {}).get("table") or "")


def subject_table(plan: dict[str, Any]) -> str:
    """当"落点主体表"用的触达表：分组统计题里要排掉**字典表**。

    "西乡段各病害类型分别有多少处"会触达到 ai_problem_base（病害类型字典，因为句子里有
    "病害类型"），但用户要统计的对象是病害本身、字典只是分组维度——把字典当成主体表，
    对象名会写成"病害类型字典"，通用算子也会去查字典表。
    """
    table = table_of(plan)
    if not table:
        return ""
    # "未识别/未知类型"问的是**没有识别出类型的病害记录**，答案不可能在类型字典表里
    # （字典里根本没有"未识别"这个类型）→ 别把字典当主体，退回语义主体表
    if (plan.get("slots") or {}).get("unknown_type") and table.endswith("_base"):
        return ""
    if str((plan.get("slots") or {}).get("group_by") or "") and table.endswith("_base"):
        return ""
    # 字典表只在用户确实在问"类型/分类/字典"时才当主体（"资产台账有多少条"问的是资产实例，
    # 不是"资产类型字典"里有多少类型）
    label = _table_label(table)
    if "字典" in label:
        # 但"落点就是这张字典表、且不是统计/分组题"时，问的就是字典列的取值
        # （"病害的计算公式有哪些"→ ai_problem_base.formula），这时字典表就是主体
        landing = str((plan.get("grounded") or {}).get("table") or "")
        if landing == table and not _stats_question(plan):
            return table
        if not any(
            word in str(plan.get("message") or "")
            for word in ("字典", "类型", "分类", "大类", "种类", "几种")
        ):
            return ""
    return table


def _table_label(table: str) -> str:
    for item in schema_catalog.field_catalog("mysql").get("tables") or []:
        if str(item.get("table")) == table:
            return str(item.get("label") or "")
    return ""


def table_label_of(plan: dict[str, Any]) -> str:
    """触达表的**人话名字**（"基本设施_隧道表" → "隧道"），给口径卡片用。"""
    table = subject_table(plan)
    if not table:
        return ""
    label = _table_label(table).strip() or table
    for prefix in ("基本设施_", "基础设施_", "基本设施"):
        if label.startswith(prefix):
            label = label[len(prefix) :]
    if label.endswith("表"):
        label = label[:-1]
    return label


def columns_of(plan: dict[str, Any], table: str = "") -> list[dict[str, Any]]:
    """plan 里触达到的列；给了 table 就只回那张表的列。"""
    items = list((plan.get("grounded") or {}).get("columns") or [])
    if table:
        items = [item for item in items if str(item.get("table")) == table]
    return items
