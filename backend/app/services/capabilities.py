"""确定性能力的人工确认（docs/确定性能力人工确认方案.md）。

把三类确定性命中（preset 模板 / 已验证示例 / 代码模板）统一收集成**候选**，连同
"这次会怎么查"的口径和可改的槽位一起交给用户选；没有候选就直接走 Wren，不打扰。

这里只做"收集 + 按选择执行"，不自己判断对错：口径摆出来，人点确认。
"""

from __future__ import annotations

import os as _os
import json
import re
import time
import uuid
from typing import Any

from . import example_reuse, nl2sql, parsers, preset_lib, templatizer
from ..config import settings
from .logging_service import log_error, log_event
from .resource_loader import load_capability_file

# 主体 → 物理表（通用算子的入口表）
_SUBJECT_TABLES = {
    str(subject_id): str(raw.get("base_table") or "")
    for subject_id, raw in (load_capability_file("subjects.yaml", {}) or {}).items()
    if isinstance(raw, dict)
}

_TOKEN_TTL_SECONDS = settings.capability_token_ttl_minutes * 60
_STASH: dict[str, tuple[float, dict[str, Any], list[dict[str, Any]]]] = {}


class ChooseCapability(RuntimeError):
    """ask 模式下命中候选：需要用户先选口径（docs/确定性能力人工确认方案.md §二）。"""

    def __init__(
        self,
        plan: dict[str, Any] | None,
        candidates: list[dict[str, Any]] | None = None,
        plans: dict[int, dict[str, Any]] | None = None,
        tasks: list[dict[str, Any]] | None = None,
        note: str = "",
    ) -> None:
        super().__init__("请先确认查询口径")
        self.plan = plan
        self.candidates = candidates or []
        self.plans = plans or {}
        self.tasks = tasks or []
        # 给用户的一句解释（例如"这条口径还差条件没填全：方向"），会显示在卡片上方
        self.note = str(note or "")

def _slots_route() -> list[dict[str, str]]:
    return preset_lib.options_for("route_id", {"source": "routes_with_data"})


def _slots_damage() -> list[dict[str, str]]:
    return preset_lib.options_for("damage_type", {"source": "damage_types"})


def _param_slot(name: str, value: str) -> dict[str, Any]:
    spec = (preset_lib.load().get("params") or {}).get(name) or {}
    return {
        "name": name,
        "label": str(spec.get("label") or name),
        "value": value,
        "options": preset_lib.options_for(name, spec),
        "editable": True,
    }


def _example_slots(plan: dict[str, Any], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """相似示例卡片上的槽位：**默认值取本轮**，本轮没提的才沿用示例自己的值。

    以前时间和病害类型直接照抄示例 spec（问"8 月"卡片上还是 7 月），而且时间槽被写成
    `editable: false`，用户看见错了也改不了 —— 这是"换了月份却按老月份查"的直接原因
    （docs/示例参数对齐与同义召回放宽方案.md 第一节）。槽位全部可编辑，plan 抽错时用户
    在卡上改一个值就能确认。
    """
    slots = plan.get("slots") or {}
    out: list[dict[str, Any]] = []
    if spec.get("route") or spec.get("route_id"):
        route_id = str((slots.get("route_match") or {}).get("id") or "") or str(
            spec.get("route_id") or ""
        )
        out.append(_param_slot("route_id", route_id))
    if spec.get("damage_type"):
        names = [
            str(item.get("problem_type") or "")
            for item in slots.get("damage_matches") or []
            if str(item.get("problem_type") or "")
        ]
        if not names:
            damage = spec.get("damage_type")
            names = [
                str((damage[0] if isinstance(damage, list) and damage else damage) or "")
            ]
        value = next((name for name in names if name), "")
        if value:
            out.append(_param_slot("damage_type", value))
    start = str(slots.get("time_start") or "")
    end = str(slots.get("time_end") or "")
    if not (start or end):
        span = spec.get("time_range")
        if isinstance(span, (list, tuple)) and len(span) == 2:
            start, end = str(span[0] or ""), str(span[1] or "")
    if start or end:
        out.append(
            {
                "name": "time_range",
                "label": "时间范围",
                "value": f"{start[:10]} ~ {end[:10]}".strip(" ~"),
                "options": [],
                "editable": True,
            }
        )
    low, high = slots.get("pile_start"), slots.get("pile_end")
    if low is None and high is None:
        span = spec.get("pile_range")
        if isinstance(span, (list, tuple)) and len(span) == 2:
            low, high = span[0], span[1]
    if low is not None or high is not None:
        out.append(
            {
                "name": "pile_range",
                "label": "桩号区间(m)",
                "value": "%s ~ %s" % (
                    "" if low is None else int(low),
                    "" if high is None else int(high),
                ),
                "options": [],
                "editable": True,
            }
        )
    device = str(slots.get("device_id") or spec.get("device_code") or "")
    if device:
        out.append(
            {"name": "device_code", "label": "设备号", "value": device, "options": [], "editable": True}
        )
    return out


def _preset_candidates(plan: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in preset_lib.match_templates(plan, limit=limit):
        slots = [_param_slot(name, item["values"].get(name, "")) for name in item["params"]]
        out.append(
            {
                "kind": "preset",
                "id": item["id"],
                "title": item["title"] or item["id"],
                "question": item["question"],
                "scope": item["scope"] or "按模板口径",
                "shape": str(plan.get("shape") or ""),
                "subject": str(plan.get("subject") or ""),
                "slots": slots,
                "score": float(item.get("score") or 0),
                "reasons": list(item.get("reasons") or []),
                "warn": False,
                "sql_preview": item["sql"],
            }
        )
    return out


def _auto_candidates(plan: dict[str, Any], limit: int = 2) -> list[dict[str, Any]]:
    """L1：成功 SQL 自动沉淀的参数化模板（docs/RAG示例直达与AI直答旁路方案.md §二）。

    0 次大模型，只做结构指纹 + 槽位全覆盖匹配；命中后同样是"弹卡等用户确认"。
    """
    from . import chat_settings

    if not bool(chat_settings.load().get("auto_template_enabled", True)):
        return []
    try:
        return templatizer.match(plan, limit=limit)
    except Exception as exc:  # noqa: BLE001
        log_error("capabilities", "auto_candidates_failed", exc)
        return []


def _synonym_template_candidates(
    plan: dict[str, Any], hits: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], set[str]]:
    """同义问法 → 它沉淀出的模板（L1 的同义召回）。

    不建同义词表、不看关键词：召回这一段交给示例库的**向量召回 + 严格同问判定**
    （换个说法也能召回，见 direct_match），命中后按"这条已验证问法沉淀出的模板"
    做身份对齐（templatizer.find_by_question），再由 card_for_template 按本次计划的口径
    重新绑槽。口径判定和结构匹配共用同一份实现，两条入口不会给出互相矛盾的结论。

    返回 (模板卡列表, 已被模板卡覆盖的问法集合)：后者用来避免同一条问法同时摆
    "模板卡 + 原始示例卡"两张。
    """
    cards: list[dict[str, Any]] = []
    covered: set[str] = set()
    seen: set[str] = set()
    for hit in hits:
        question = str(hit.get("question") or "")
        template = templatizer.find_by_question(question)
        if not template or str(template.get("id")) in seen:
            continue
        score = float(hit.get("score") or 0)
        card = templatizer.card_for_template(
            template,
            plan,
            reason_prefix=f"同义问法召回（「{question[:20]}」已沉淀成模板）",
            score_bonus=round(0.4 * score, 2),
        )
        if not card:
            continue
        seen.add(str(template.get("id")))
        covered.add(question)
        cards.append(card)
        if len(cards) >= 2:
            break
    return cards, covered


def _selectable_semantic_fields() -> set[str]:
    """登记过的语义字段名（confidence / lng_lat / pic_* / problem_type_name…）。

    它们不在 information_schema 里，但字段目录/通用算子都能按表达式选出来
    （`field_catalog._SELECT_FIELD_EXPRS`），所以算"这张表能选的列"。
    """
    from . import field_catalog

    return set(field_catalog._SELECT_FIELD_EXPRS)  # noqa: SLF001


def _object_count(spec: dict[str, Any], question: str = "") -> int:
    """这条口径涉及几个对象：1（单对象）还是 2（多对象对比）。

    判据统一用 example_reuse.is_multi_object（路线 / 桥隧坡 / 病害类型 / 月份 / 编号并列）。
    """
    return 2 if example_reuse.is_multi_object(spec, question) else 1


def _example_candidates(
    message: str,
    plan: dict[str, Any],
    kind: str,
    limit: int,
    only_exact: bool = False,
) -> list[dict[str, Any]]:
    """L2 相似问法直达：召回 + 便宜模型严格同问打分（docs/RAG示例直达与AI直答旁路方案.md §三）。

    没有现成模板时，这条闸承接"换个说法但查询其实同构"的问题；命中同样只摆成候选，
    等用户点「确认查询」才执行。
    """
    from . import direct_match

    out: list[dict[str, Any]] = []
    try:
        # 先看"同一句"（纯本地、零成本）：用户自己问过并确认过的问题，不该因为
        # 语义打分没过门槛就不给出来——实测"宝安大道西乡段和…分别是几车道"问第二遍时，
        # 答案就在示例库/看板里，却因为只走"相似度闸"而没进候选。
        exact_hits = direct_match.gate(message, plan, kind, only_exact=True)
        hits = exact_hits
        if not exact_hits and not only_exact:
            hits = direct_match.gate(message, plan, kind)
    except Exception as exc:  # noqa: BLE001
        log_error("capabilities", "example_candidates_failed", exc, question=message[:60])
        return out
    # 同义召回：这些"已验证问法"里，凡是已经沉淀成模板的，直接摆**模板卡**（参数可改、
    # 0 次大模型、走确定性路径），比摆原始示例更准也更好用；同一条问法不再重复摆两张卡。
    synonym_cards, templated = _synonym_template_candidates(plan, hits)
    out.extend(synonym_cards)
    for hit in hits:
        if str(hit.get("question") or "") in templated:
            continue
        # 示例库里存的有的是 Wren MDL-SQL：先编译成物理 SQL 才摆给用户，
        # 编译不了（比如当前环境没注册这个 MDL 项目）就别让他点了才发现跑不通。
        executable = _executable_sql(str(hit.get("sql") or ""), kind)
        from_wren = False
        if not executable:
            # 同句精确命中（就是用户自己确认过的那句）**不能丢**：第一遍既然是走 Wren 跑通的，
            # 这次就照样走 Wren 重放（执行时回落到 Wren 链路）。只有"相似度召回的近似题"
            # 才保持"编译不出就不摆"的保守策略
            # （docs/示例直达被generic挤占与人话推荐理由整改.md P0-2）。
            if str(hit.get("confidence") or "") != "exact":
                log_error(
                    "capabilities",
                    "example_not_executable",
                    RuntimeError("示例 SQL 既不是物理 SQL，也编译不出物理 SQL"),
                    doc=str(hit.get("doc_name") or ""),
                )
                continue
            executable = str(hit.get("sql") or "")
            from_wren = True
        spec = hit.get("spec") or {}
        # 对象数（= 语义角色数）对齐（方案 §4.1）：示例涉及几个对象，问题就得涉及几个。
        # 多对象示例（两条路/两座桥/两个月份）摆给单对象问题，回填时"要两个路线值、问题里只有
        # 一个"——用户点了才报"条件没法补全"，或者更糟：按位置把同一个值填进两个槽。
        plan_objects = _object_count(
            nl2sql._reuse_plan_spec(plan), str(plan.get("message") or "")
        )
        example_objects = _object_count(
            spec, str(spec.get("display_question") or hit.get("question") or "")
        )
        if example_objects != plan_objects:
            log_event(
                "capabilities",
                "INFO",
                "example_object_count_mismatch",
                doc=str(hit.get("doc_name") or ""),
                example_objects=example_objects,
                plan_objects=plan_objects,
                question=str(plan.get("message") or "")[:60],
            )
            continue
        slots = _example_slots(plan, spec)
        # 这条示例还带了本轮没提的条件（默认值沿用示例的，且能在卡上改）——标出来并降到后面，
        # 别让它压过"条件完全对上"的候选（docs/示例参数对齐与同义召回放宽方案.md 第一节）。
        extra = sorted(
            nl2sql._reuse_condition_keys(spec)
            - nl2sql._reuse_condition_keys(nl2sql._reuse_plan_spec(plan))
        )
        # 反过来的方向：**本轮点了枚举取值，示例 SQL 里却没那一列** → 点了它就会静默丢掉这个
        # 条件（实测"看一下交安事件的病害样图"点第一条示例回了全部 42 张样图，"交安事件"没了）。
        # 这种示例不摆——宁可少一张卡，也不摆一张会丢条件的卡。
        unexpressed = _unexpressed_code_columns(plan, executable, str(hit.get("normalized_sql") or ""))
        if unexpressed:
            log_event(
                "capabilities",
                "INFO",
                "example_missing_code_condition",
                doc=str(hit.get("doc_name") or ""),
                columns="、".join(unexpressed),
                question=str(plan.get("message") or "")[:60],
            )
            continue
        # 完整性提示（方案 §4.3）：示例问法本身是多子问题、这条 SQL 只覆盖一部分
        partial_hint = templatizer.question_is_partial(
            str(spec.get("display_question") or hit.get("question") or plan.get("message") or ""),
            [str(item) for item in (spec.get("metrics") or [])],
        )
        out.append(
            {
                "kind": "example_direct",
                "id": str(hit.get("doc_name") or ""),
                # 展示用**对外问法**（长对话补全留下的"377 处"这类已在入库时清掉）：
                # 卡片上写着 377 会让人以为只能查那 377 处
                "title": "相似已验证问法："
                + str(
                    spec.get("display_question") or hit.get("question") or ""
                )[:40],
                "question": str(spec.get("display_question") or hit.get("question") or ""),
                "scope": (
                    ("按这条已确认过的问法口径（" + "、".join(
                        item["label"] + "=" + str(item["value"])
                        for item in slots
                        if item.get("value")
                    ) + "）")
                    if slots
                    else "按这条已确认过的问法口径"
                )
                + ("；这条第一遍是走 Wren 跑通的，本次将走 Wren 重放" if from_wren else ""),
                "shape": str(plan.get("shape") or ""),
                "subject": str(plan.get("subject") or ""),
                "slots": slots,
                "score": float(hit.get("score") or 0),
                "reasons": list(hit.get("reasons") or [])
                + (
                    ["这条示例还带了本轮没提的条件：" + "、".join(extra) + "（默认沿用示例值，可在卡上改）"]
                    if extra
                    else []
                )
                + (
                    ["这条示例只覆盖原问题的一部分（其余部分需追问或让 AI 用 Wren 查）"]
                    if partial_hint
                    else []
                ),
                "warn": bool(extra) or partial_hint,
                "partial": partial_hint,
                "loose": bool(extra),
                "sql_preview": executable,
                # 执行时用：这条示例的归一化 SQL + 结构 spec
                "normalized_sql": str(hit.get("normalized_sql") or hit.get("sql") or ""),
                "spec": spec,
                "replay_via_wren": from_wren,
            }
        )
    return out[:limit]


def _executable_sql(sql: str, kind: str) -> str | None:
    """示例/固化 SQL → 能直接执行的物理 SQL（MDL-SQL 先过 Wren 编译）。"""
    text = str(sql or "").strip()
    if not text:
        return None
    # 判定统一走 nl2sql.physical_sql_of（带缓存）。这里**不要**再来一份自己的
    # "含 wren_src_ 就当 MDL-SQL 编译"的拷贝：Wren 产出的物理 SQL 也带 wren_src_ 别名，
    # 拷贝版会把它误判成编译不出来的 MDL-SQL，于是已确认过的示例根本不摆给用户
    # （实测：西乡段里程桩点那条示例第二遍问就不见了，只剩错的通用算子）。
    return nl2sql.physical_sql_of(text, kind) or None


def _code_template_candidates(message: str, plan: dict[str, Any]) -> list[dict[str, Any]]:
    # 计划里带着"代码模板表达不了"的过滤条件（收藏/工单状态/数值阈值…）时，
    # 这个模板会把条件悄悄丢掉（实测就是"收藏的病害"答成全部病害）→ 不作为候选，
    # 交给通用算子或 Wren。
    # short_name / full_name 就是路线名（语义层有时直接给物理列名），算"模板能表达"的
    expressible = {
        "route_name",
        "route",
        "short_name",
        "full_name",
        "damage_type",
        "problem_type_name",
        "collection_time",
    }
    semantic_fields = {
        str(item.get("field") or "")
        for item in (plan.get("slots") or {}).get("semantic_conditions") or []
    }
    if semantic_fields - expressible:
        return []
    try:
        sql = nl2sql.build_template_sql(plan)
    except Exception as exc:  # noqa: BLE001
        log_error("capabilities", "code_template_failed", exc, question=message[:60])
        return []
    if not sql:
        return []
    # 问题点名了具体字段（"西乡段的纬度范围"→ lat），而这条内置模板的 SQL 根本没 SELECT 它
    # → 指标/字段对不上，不作为候选（docs/推荐卡硬推荐与指标空匹配方案.md §三.3）。
    plan_fields = [str(item) for item in ((plan.get("semantic") or {}).get("output_fields") or [])]
    if plan_fields and not nl2sql.sql_covers_fields(sql, plan_fields):
        log_event(
            "capabilities",
            "INFO",
            "code_template_field_mismatch",
            fields="、".join(plan_fields)[:60],
            question=str(message)[:60],
        )
        return []
    name, label = nl2sql.template_builder_label(plan, sql)
    scope = nl2sql.code_template_scope(plan, sql, label)
    # 分组题（distribution）不能拿"不分组"的模板接（实测"按工单状态分布"被
    # "台账/对象计数"接走）→ 拼出来的 SQL 没有 GROUP BY 就不作为候选
    if str(plan.get("shape") or "") == "distribution" and "group by" not in sql.lower():
        return []
    # 口径存疑改成**语义判断**：计划里的指标如果模板声明里根本没有（"尺寸"这种模糊指标
    # 经常被拆成模板不认识的词），就提示用户核对，不再靠"尺寸/最大"这类关键词去猜。
    warn = False
    plan_metrics = {str(item) for item in ((plan.get("semantic") or {}).get("metrics") or [])}
    if plan_metrics:
        from .resource_loader import load_capability_file

        declared: set[str] = set()
        for item in (load_capability_file("templates.yaml", {}) or {}).get("templates") or []:
            declared |= {str(metric) for metric in ((item or {}).get("match") or {}).get("metrics") or []}
        declared |= {
            "count",
            "total_area",
            "avg_area",
            "max_area",
            "area",
            "confidence",
            "disease_length",
            "density",
            "ratio",
            "rate",
        }
        unknown = plan_metrics - declared
        if unknown:
            warn = True
            reasons_extra = "；".join(sorted(unknown))
    reasons = [label or "内置代码模板", "按内置口径拼 SQL（不经过大模型）"]
    if warn:
        reasons.append(f"⚠ 这条口径里的指标模板没覆盖（{reasons_extra}），请核对后再用")
    return [
        {
            "kind": "code_template",
            "id": name or "code_template",
            "title": f"内置模板：{label or '代码模板'}",
            "question": str(message or ""),
            "scope": scope,
            "shape": str(plan.get("shape") or ""),
            "subject": str(plan.get("subject") or ""),
            "slots": [],
            "score": 5.0 if not warn else 2.0,
            "reasons": reasons,
            "warn": warn,
            "sql_preview": sql,
        }
    ]


def _generic_filters(plan: dict[str, Any], kind: str, table: str) -> tuple[list[dict[str, Any]], list[str]]:
    """把计划里的条件翻成通用算子的过滤项（列名走字段目录，翻不出来就说明）。"""
    from . import schema_catalog as catalog

    filters: list[dict[str, Any]] = []
    notes: list[str] = []
    slots = plan.get("slots") or {}
    # 已经被结构化槽位表达掉的**别名条件**：语义层还会给 route_name / has_image 这类
    # 逻辑字段名，不能再当成"翻不出来的条件"整体弃权
    handled_aliases: set[str] = set()
    route = slots.get("route_match") or {}
    if route.get("id"):
        # 路线条件按"这张表到路线表的真实关系"落列：主表自带路线列就直接用，
        # 要经中间表（如里程桩点 → 路段 → 路线）就自动补 JOIN，条件落中转表的 route_id。
        # 到不了路线 → 弃权交 Wren，绝不许猜成主键（P0-1，禁止写死 routes_id）。
        path = catalog.relation_path(kind, table, target_table=_SUBJECT_TABLES["route"])
        if not path:
            notes.append("route")
        else:
            column, joins = path
            filters.append(
                {
                    "column": column,
                    "op": "=",
                    "value": str(route["id"]),
                    "joins": joins,
                    "entity": "route",
                }
            )
    damage = slots.get("damage_matches") or []
    if damage:
        column = catalog.resolve_column(kind, table, "damage_type")
        if not column:
            notes.append("damage_type")
        else:
            filters.append(
                {
                    "column": str(column["name"]),
                    "op": "in",
                    "value": [item.get("problem_code") for item in damage],
                    "entity": "damage_type",
                }
            )

    # 时间范围：**必须落到真实时间列**。以前这一段完全没写，时间槽只在口径文案里出现，
    # 于是"2026 年 6 月全库病害"和"7 月"都答成全表 1702
    # （docs/通用算子过滤丢失与自动补物理SQL修复.md §一）。
    start, end = slots.get("time_start"), slots.get("time_end")
    if start or end:
        column = _time_column(kind, table, slots)
        if not column:
            notes.append("time_range")
        elif start and end:
            filters.append(
                {
                    "column": column,
                    "op": "between",
                    "value": [str(start), str(end)],
                    "entity": "time_range",
                }
            )
        else:
            filters.append(
                {
                    "column": column,
                    "op": ">=" if start else "<=",
                    "value": str(start or end),
                    "entity": "time_range",
                }
            )

    # 桩号区间：能落到这张表的数值桩号列才表达，否则弃权（不许把"K0+800~K0+900"当不存在）。
    low, high = slots.get("pile_start"), slots.get("pile_end")
    if low is not None or high is not None:
        column = _pile_column(kind, table)
        if not column:
            notes.append("pile_range")
        elif low is not None and high is not None:
            filters.append(
                {"column": column, "op": "between", "value": [low, high], "entity": "pile_range"}
            )
        else:
            filters.append(
                {
                    "column": column,
                    "op": ">=" if low is not None else "<=",
                    "value": low if low is not None else high,
                    "entity": "pile_range",
                }
            )

    # 方向（上行/下行）是条件，不是分组：语义层给了就必须落到列上
    # —— 但如果这一问本来就是"按上下行分组"（group_by=direction 且已在按方向分组），
    # 就不能再拿一个方向去过滤，否则"上下行分别多少"只会剩一行。
    if slots.get("up_or_down") and not _groups_by_direction(plan):
        column = catalog.resolve_column(kind, table, "up_or_down")
        if not column:
            notes.append("up_or_down")
        else:
            filters.append(
                {
                    "column": str(column["name"]),
                    "op": "=",
                    "value": str(slots["up_or_down"]),
                    "entity": "up_or_down",
                }
            )

    # 有图片/无图片：落到采集图片列上（以前这个条件会被静默丢掉 → 答成全库病害）
    if slots.get("has_image") or "has_image" in (plan.get("required_conditions") or []):
        column = _image_column(kind, table)
        want = _has_image_want(plan)
        if not column:
            notes.append("has_image")
        else:
            filters.append(
                {
                    "column": column,
                    "op": "empty" if want == "0" else "not empty",
                    "value": "",
                    "entity": "has_image",
                }
            )
            handled_aliases.update({"has_image", str(column)})

    # "未识别类型"（字典里没有对应类型）= damage_type 为空，不能拿字典值去比
    if slots.get("unknown_type"):
        column = catalog.resolve_column(kind, table, "damage_type")
        if not column:
            notes.append("unknown_type")
        else:
            filters.append(
                {
                    "column": str(column["name"]),
                    "op": "empty",
                    "value": "",
                    "entity": "unknown_type",
                }
            )
    # 语义层还会抽"路线名/病害类型名"这类**别名条件**：已经由上面的结构化槽位表达了，
    # 不当成"翻不出来的条件"（否则通用算子会被整体弃权）。
    if route.get("id"):
        handled_aliases.add("route_name")
    if damage:
        handled_aliases.update({"problem_type_name", "damage_type_name"})
    # 已经由结构化槽位落好的列（路线/时间/桩号/方向）不要再翻一遍，
    # 但语义层**单独**给出的时间列（没有 time_start 槽）仍要当条件用上。
    covered = {str(item.get("column") or "") for item in filters}
    for item in slots.get("semantic_conditions") or []:
        field = str(item.get("field") or "")
        if field in handled_aliases:
            continue
        column = catalog.resolve_column(kind, table, field)
        if not column:
            notes.append(field)
            continue
        if str(column["name"]) in covered:
            continue
        filters.append(
            {
                "column": str(column["name"]),
                "op": str(item.get("op") or "="),
                "value": item.get("value"),
            }
        )
    # 枚举取值触达（本地、零 LLM）：用户句子里出现了这张表某列的**枚举中文值**
    # （"交安事件"→disease_categories_type=15、"国道"→level=0），但条件一直没落进 SQL ——
    # 语义层经常漏这类过滤，漏了就静默答成全库（实测"看一下交安事件的病害样图"
    # 返回全部 42 张样图，"交安事件"不见了）。
    # 只补**这张表自己**能表达的列；只在用户明确点了一个取值时补；同列已有条件不重复加。
    for name, code in _grounded_code_values(plan, kind, table, covered):
        filters.append({"column": name, "op": "=", "value": code, "entity": f"code:{name}"})
        covered.add(name)
    # 兜底护栏：语义层认出来、但没能落到 SQL 的条件，一律弃权交 Wren，
    # 绝不生成"少条件"的 SQL（少条件 = 静默把总数放大，最难发现）。
    missing = _declared_conditions(slots, plan) - {str(item.get("entity")) for item in filters}
    notes.extend(sorted(name for name in missing if name not in notes))
    return filters, notes


def _unexpressed_code_columns(plan: dict[str, Any], *sql_texts: str) -> list[str]:
    """本轮点名的枚举取值列里，示例 SQL **一列都没提到**的那些（示例表达不了本轮条件）。

    判据很保守：只在"用户明确点了一个枚举取值"（"交安事件""国道""启用"）时才检查，
    而且只要求列名出现在示例 SQL 文本里——示例里带列就说明它自己会过滤，交回原来的对齐逻辑。
    """
    from . import field_grounding
    from . import schema_catalog as catalog

    wanted: list[str] = []
    for item in (plan.get("grounded") or {}).get("columns") or []:
        column = str(item.get("column") or "")
        code_map = item.get("code_map") or {}
        if not column or not code_map:
            continue
        hits = [
            value
            for flag in item.get("hits") or []
            if str(flag).startswith("取值命中：")
            for value in str(flag)[len("取值命中：") :].split("、")
            if value
        ]
        if not hits:
            continue
        codes = [str(code) for code, label in code_map.items() if str(label) in hits]
        if len(codes) != 1:
            continue
        if column not in wanted:
            wanted.append(column)
    text = " ".join(str(part or "") for part in sql_texts)
    if not text or not wanted:
        return []
    # **只看 WHERE 段**：示例 SQL 里这列大多出现在 SELECT（CASE 翻中文）里，
    # 光看整段文本会误判成"示例已经表达了这个条件"（实测那条"看病害样图"的示例就是这样）。
    upper = text.upper()
    start = upper.find("WHERE")
    if start < 0:
        return list(wanted)
    tail = text[start:]
    tail_upper = tail.upper()
    for keyword in ("GROUP BY", "ORDER BY", "HAVING", "LIMIT"):
        pos = tail_upper.find(keyword)
        if pos > 0:
            tail = tail[:pos]
            tail_upper = tail.upper()
    return [column for column in wanted if column not in tail]


def _grounded_code_values(
    plan: dict[str, Any], kind: str, table: str, covered: set[str]
) -> list[tuple[str, str]]:
    """字段触达判出的"用户点了这一列的某个枚举取值" → [(列名, 编码值)]。

    `covered` = 已经被别的条件表达过的列（同列不重复加）。
    一句话点了两个取值（"路面病害和交安事件"）时**不自动加**——那多半是两个取值的 OR/对比，
    交给语义层，别猜成一个。
    """
    from . import field_grounding
    from . import schema_catalog as catalog

    out: list[tuple[str, str]] = []
    for item in field_grounding.columns_of(plan, table):
        column = str(item.get("column") or "")
        code_map = item.get("code_map") or {}
        if not column or not code_map or column in covered:
            continue
        if not catalog.resolve_column(kind, table, column):
            continue
        hits = [
            value
            for flag in item.get("hits") or []
            if str(flag).startswith("取值命中：")
            for value in str(flag)[len("取值命中：") :].split("、")
            if value
        ]
        if not hits:
            continue
        codes = [str(code) for code, label in code_map.items() if str(label) in hits]
        if len(codes) != 1:
            continue
        out.append((column, codes[0]))
    return out


def _declared_conditions(slots: dict[str, Any], plan: dict[str, Any] | None = None) -> set[str]:
    """语义层在这条问题上认出来的**过滤条件**（通用算子必须全部表达出来）。"""
    required = set((plan or {}).get("required_conditions") or [])
    declared: set[str] = set()
    if (slots.get("route_match") or {}).get("id"):
        declared.add("route")
    if slots.get("damage_matches"):
        declared.add("damage_type")
    if slots.get("time_start") or slots.get("time_end"):
        declared.add("time_range")
    if slots.get("pile_start") is not None or slots.get("pile_end") is not None:
        declared.add("pile_range")
    if slots.get("up_or_down"):
        declared.add("up_or_down")
    if slots.get("has_image") or "has_image" in required:
        declared.add("has_image")
    if slots.get("unknown_type") or "unknown_type" in required:
        declared.add("unknown_type")
    return declared


def _groups_by_direction(plan: dict[str, Any]) -> bool:
    """这一问是不是"按上下行/方向分组"（分组维度就是方向）。"""
    slots = plan.get("slots") or {}
    group_by = str(slots.get("group_by") or "").lower()
    if group_by in {"direction", "up_or_down", "上下行", "方向"}:
        return True
    return str((plan.get("semantic") or {}).get("group_by") or "").lower() in {
        "direction",
        "up_or_down",
    }


# 需要"理解 + 推理 + 多步"的意图：通用算子只会答一半，一律弃权交 Wren / analysis
# （docs/通用算子触发规则收紧方案.md R1）。判据全部来自 plan 的结构字段 + 明确的意图词，
# 不做"哪个模板"式的端到端匹配。
def _needs_reasoning(plan: dict[str, Any], message: str) -> bool:
    semantic = plan.get("semantic") or {}
    slots = plan.get("slots") or {}
    if semantic.get("derived") or semantic.get("needs_conclusion"):
        return True
    # 一句话问多个指标 → 通用算子只会挑一个答
    if len([item for item in semantic.get("metrics") or [] if str(item)]) >= 2:
        return True
    # 多主体（两条路分别…）→ 交给对比模板 / Wren
    if plan.get("multi_route") or len(slots.get("routes") or []) >= 2:
        return True
    if plan.get("comparison") and not str(slots.get("group_by") or ""):
        return True
    return False


def _has_image_want(plan: dict[str, Any]) -> str:
    """"有图片 / 没有图片"：语义层把值放在 has_image 条件里（1=有、0=没有）。"""
    slots = plan.get("slots") or {}
    for item in slots.get("semantic_conditions") or []:
        if str(item.get("field") or "") != "has_image":
            continue
        value = str(item.get("value") or "").strip().lower()
        if value in {"0", "false", "no", "无", "没有", "否"}:
            return "0"
        return "1"
    value = str(slots.get("has_image") or "").strip().lower()
    if value in {"0", "false", "no", "无", "没有", "否"}:
        return "0"
    return "1"


# 语义层的指标名 → （物理列, 聚合方式）。取前 N / 极值只看这一份声明
_RANK_METRICS: dict[str, tuple[str, str]] = {
    "count": ("", "count"),
    "total_area": ("area", "sum"),
    "area": ("area", "sum"),
    "avg_area": ("area", "avg"),
    "max_area": ("area", "max"),
    "min_area": ("area", "min"),
    "confidence": ("confidence", "max"),
    "disease_length": ("disease_length", "sum"),
}


def _rank_metric(plan: dict[str, Any]) -> tuple[str, str]:
    """取语义层声明的第一个可排序指标；没声明就返回空（调用方弃权）。"""
    for item in (plan.get("semantic") or {}).get("metrics") or []:
        found = _RANK_METRICS.get(str(item))
        if found:
            return found
    return "", ""


def _human_reasons(
    plan: dict[str, Any],
    kind: str,
    table: str,
    shape: str,
    filters: list[dict[str, Any]] | None,
    action: str,
) -> list[str]:
    """把通用算子的推荐理由写成人话：查哪张表、按什么条件、做什么统计。

    以前写的是"字段目录解析（编码值翻中文）、不经过大模型"这种内部话术，用户看不懂这张卡
    到底答的是不是他问的那件事（docs/示例直达被generic挤占与人话推荐理由整改.md P0-4）。
    """
    from . import schema_catalog as catalog

    slots = plan.get("slots") or {}
    route = slots.get("route_match") or {}
    parts: list[str] = []
    label = str(
        route.get("short_name") or route.get("full_name") or slots.get("route") or ""
    ).strip()
    if label:
        parts.append("路线=" + label)
    start = str(slots.get("time_start") or "")[:10]
    end = str(slots.get("time_end") or "")[:10]
    if start or end:
        parts.append("时间=" + (f"{start}~{end}" if start and end else (start or end)))
    damage = [
        str(item.get("problem_type") or "")
        for item in slots.get("damage_matches") or []
        if item.get("problem_type")
    ]
    if damage:
        parts.append("病害类型=" + "、".join(damage))
    low, high = slots.get("pile_start"), slots.get("pile_end")
    if low is not None or high is not None:
        parts.append(f"桩号={low if low is not None else ''}~{high if high is not None else ''}米")
    if slots.get("up_or_down"):
        parts.append(f"方向={slots['up_or_down']}")
    if any(
        str(item.get("field") or "") == "has_image" for item in slots.get("semantic_conditions") or []
    ):
        parts.append("只看有图片的记录" if _has_image_want(plan) != "0" else "只看没有图片的记录")
    if slots.get("unknown_type"):
        parts.append("只看未识别类型的病害（字典里没有对应类型）")
    if not parts:
        parts.append("全库（没有业务过滤）")
    table_label = catalog.table_label(kind, table)
    reason = f"查{table_label}：{'、'.join(parts)}；{action}"
    if not filters:
        reason += "（全表统计，请核对口径）"
    return [reason, "这条是通用算子拼的（0 次大模型），口径可直接核对"]


def _image_column(kind: str, table: str) -> str:
    """这张表的"采集图片"列（有图片/无图片条件要落在它上面）。"""
    from . import schema_catalog as catalog

    columns = catalog.image_columns(kind, table)
    return str(columns[0]["name"]) if columns else ""


def _photo_image_columns(kind: str, table: str, plan: dict[str, Any]) -> list[str]:
    """看图题该按哪些图片列判"有图"。

    用户点名了某一列（"调出来路段封面"）就用它；没点名才把这张表的图片列全算上（OR）。
    不能只取第一列：路段表 `picture` 全空、真有封面的那 2 条在 `picture_cover` 上
    （实测），只认第一列会把有图的行全漏掉。列名一律来自目录 role=image，不写死名单。
    """
    from . import field_grounding
    from . import schema_catalog as catalog

    images = [str(col["name"]) for col in catalog.image_columns(kind, table)]
    if not images:
        return []
    grounded = [
        str(item.get("column"))
        for item in field_grounding.columns_of(plan, table)
        if str(item.get("column")) in images
    ]
    return grounded or images


def _time_column(kind: str, table: str, slots: dict[str, Any]) -> str:
    """这张表"业务时间"落在哪一列。

    优先语义层点名的列；否则按 nl2sql 的字段族顺序（采集时间 → 创建时间 → 通车日期）
    取表里真实存在的那一列；都没有 → 空串（调用方弃权，不许把时间条件丢掉）。
    """
    from . import nl2sql
    from . import schema_catalog as catalog

    entry = catalog.table_entry(kind, table) or {}
    names = {str(col.get("name") or "") for col in entry.get("columns") or []}
    for item in slots.get("semantic_conditions") or []:
        field = str(item.get("field") or "")
        if not field or field in {"create_time", "update_time"}:
            continue
        column = catalog.resolve_column(kind, table, field)
        if column and "time" in (column.get("roles") or []):
            return str(column["name"])
    family = tuple(getattr(nl2sql, "_FIELD_FAMILIES", {}).get("collection_time") or ())
    for candidate in family:
        if candidate in names:
            return candidate
    return ""


def _pile_column(kind: str, table: str) -> str:
    """这张表"桩号米数"落在哪一列：字段族里必须挑**数值列**（pile_number 是 K0+887 文本）。"""
    from . import nl2sql
    from . import schema_catalog as catalog

    family = tuple(getattr(nl2sql, "_FIELD_FAMILIES", {}).get("pile_number") or ())
    for candidate in family:
        column = catalog.resolve_column(kind, table, candidate)
        if column and "numeric" in (column.get("roles") or []):
            return str(column["name"])
    return ""


def _generic_candidates(
    message: str, plan: dict[str, Any], kind: str, want_images: bool = False
) -> list[dict[str, Any]]:
    """通用明细 / 通用分组：字段和表都从字段目录解析（方案第二、三节）。

    只在 preset/示例都没命中时兜底，且列名解析不出来就弃权（交给 Wren）。

    want_images：**语义**结论（意图分类 image），由调用方传进来；不在这里自己判断，
    更不拿关键词表猜（用户定的规矩：判断要不要看图靠语义，不靠穷举关键词）。

    落点**并列**时把几张表的口径都摆出来（用户要求"优先级不要固定得那么死，允许 Top1 并列"）：
    拿不准是"整条路的车道数"还是"路段的纬度"时，给用户两张卡让他自己选，比硬拍一张好。
    """
    # ── 准入收紧（docs/通用算子触发规则收紧方案.md R1/R5）：这两条与落点表无关，只判一次 ──
    if _needs_reasoning(plan, message):
        log_event("capabilities", "INFO", "generic_skipped", reason="needs_reasoning",
                  question=str(message)[:60])
        return []
    out: list[dict[str, Any]] = []
    for index, table in enumerate(_landing_tables(plan)):
        items = _generic_candidates_for_table(message, plan, kind, table, want_images)
        for item in items:
            if index:
                # 并列表（不是主表）的口径整体降一档：排在主表后面，但仍然摆出来
                item["score"] = round(float(item.get("score") or 0) - 0.3, 2)
                item["tied_table"] = True
        out.extend(items)
    return out


def _landing_tables(plan: dict[str, Any]) -> list[str]:
    """通用算子的落点表候选（主表在前）：主表 + 字段触达算出来的**并列表**。"""
    from . import field_grounding

    tables: list[str] = []
    for item in (plan.get("grounded") or {}).get("table_candidates") or []:
        name = str(item.get("table") or "")
        if name and name not in tables:
            tables.append(name)
    primary = field_grounding.subject_table(plan) or _SUBJECT_TABLES.get(
        str(plan.get("subject") or "disease")
    )
    if primary and primary not in tables:
        tables.insert(0, primary)
    return tables or ([primary] if primary else [])


def _generic_candidates_for_table(
    message: str,
    plan: dict[str, Any],
    kind: str,
    table: str,
    want_images: bool = False,
) -> list[dict[str, Any]]:
    """针对**一张落点表**的通用算子候选（_generic_candidates 的逐表实现）。"""
    from . import field_grounding
    from . import generic
    from . import schema_catalog as catalog

    subject = str(plan.get("subject") or "disease")
    shape = str(plan.get("shape") or "")
    # 同名列多表歧义 → **弃权交 Wren**（docs/清理不可靠引擎与通用算子修复方案.md §2.4）：
    # 用户点名的列在多张表里得分并列、表级也没证据时（field_grounding.ambiguous），
    # 通用算子按名字挑一张就是赌（实测"路段长度"被 JOIN 到边坡表的 length）。宁可弃权。
    for item in (plan.get("grounded") or {}).get("ambiguous") or []:
        candidates = {
            str(entry.get("table"))
            for entry in (item or {}).get("candidates") or []
            if isinstance(entry, dict)
        }
        if table in candidates:
            log_event(
                "capabilities",
                "INFO",
                "generic_ambiguity_veto",
                table=table,
                column=str((item or {}).get("column") or ""),
                question=str(message or "")[:60],
            )
            return []
    # photo 也要接：看图诉求（"给我看看西乡段病害的照片"）必须出一条能真的取出图片列的候选，
    # 否则用户看到的是"没有任何推荐/暂不可用"。以前这里把 photo 排除在外，靠内置模板兜，
    # 而注册表里没有 MySQL 侧的看图条目 → photo 题一个卡都不出
    # （docs/从打地鼠到回归防护体系方案.md §二.①："西乡段的病害图片"必须弹卡）。
    if shape not in {"list", "detail", "distribution", "count", "photo"}:
        return []
    # 「要图」= 语义结论（want_images，意图分类 image）或语义层给的 shape=photo——
    # 实测"调出来路段封面的图片"语义层给的是 shape=list（只有"看病害图"那种才给 photo），
    # 所以只认 shape 会漏。图片列一律从目录 role=image 取，不写死名单
    # （库里 26 个图片列，现在大多为空，以后填了数据就得能查到）。
    wants_photo = shape == "photo" or want_images
    slots = plan.get("slots") or {}
    semantic = plan.get("semantic") or {}
    group_by = str(slots.get("group_by") or "")
    # R2：要"分布"的问题不能拿单值 COUNT 顶（"上下行各有多少"只回一个总数就是错题）。
    # 单值计数只在**真的没有分组维度**时给；分组维度交给 distribution 分支 / Wren。
    if shape == "count" and group_by:
        return []
    # R4：取前 N / 极值必须由语义层的 metrics 指明指标，不靠问题里的关键词猜。
    if shape == "top" and group_by and not _rank_metric(plan):
        return []
    filters, unresolved = _generic_filters(plan, kind, table)
    if unresolved:
        # 字段触达挑的落点表表达不了本轮条件（实测"未识别类型的病害有多少处"被选成了
        # 类型字典 ai_problem_base，路线/未识别条件都落不下去）→ 退回语义主体表再试一次，
        # 都不行才弃权交 Wren
        fallback = _SUBJECT_TABLES.get(subject) or ""
        if fallback and fallback != table:
            alt_filters, alt_unresolved = _generic_filters(plan, kind, fallback)
            if not alt_unresolved:
                log_event(
                    "capabilities",
                    "INFO",
                    "generic_table_fallback",
                    picked=table,
                    fallback=fallback,
                    question=str(message)[:60],
                )
                table, filters, unresolved = fallback, alt_filters, []
    if unresolved:
        return []  # 有条件翻不成列 → 别硬查
    # 看图题只看**真有图片路径**的行：空的行取回去也渲染不出来（病害口径当初就是这么定的：
    # `problem_url IS NOT NULL AND <> '' AND <> 'NULL'`）。这条以前只有病害那条固定口径有，
    # 其他表的图片题（路段封面/资产样图/作业图片…）只能全表取前 500 行，用户点"确认"拿到的是
    # 一张封面列全空的表（实测 sections.picture_cover 只有 2/5840）。
    # 判据用**用户诉求**（wants_images）而不是只看 shape=photo：实测"调出来路段封面的图片"
    # 语义层给的是 shape=list（只有"看病害图"那种才给 photo），只认 shape 这条过滤根本不生效。
    photo_columns = _photo_image_columns(kind, table, plan) if wants_photo else []
    if photo_columns:
        filters.append(
            {
                "columns": photo_columns,
                "op": "not empty",
                "value": "",
                "entity": "has_image",
            }
        )
    # 用户点名的列只认字段触达（语义层 + 目录），不再从原问句扫列注释猜词。
    # 字段触达的列**先并进集合再截断**：以前是"点名列先占满 6 个名额、触达列追加不进去"，
    # 于是"分别列出路段桩号和相关的修改人"里的桩号被挤掉（同上 doc §三.3）。
    usable: list[str] = []
    grounded_columns = [
        str(item["column"])
        for item in field_grounding.columns_of(plan, table)
        # 语义字段（confidence / lng_lat / pic_*）不在 information_schema 里，
        # `catalog.resolve_column` 认不出来会把它们整批滤掉——"图片的置信度"于是
        # 只选出 pic_position（整列 JSON）而选不出 confidence，"经纬度"只选出 lat
        # （实测）。登记过的语义字段一律算"这张表能选出来的列"。
        if catalog.resolve_column(kind, table, str(item.get("column")))
        or str(item.get("column")) in _selectable_semantic_fields()
    ]
    for name in grounded_columns:
        if name not in usable:
            usable.append(name)
    if photo_columns:
        # 看图：图片列必须进 SELECT，否则"取一张图"变成了取别的列。哪几列算图片列由目录的
        # role=image 决定（用户点名封面就用封面，没点名才把这张表的图片列都带上）。
        for name in reversed(photo_columns):
            if name not in usable:
                usable.insert(0, name)
    usable = usable[:6]
    out: list[dict[str, Any]] = []
    group_by = str((plan.get("slots") or {}).get("group_by") or "")
    if shape == "distribution" and group_by:
        column = catalog.resolve_column(kind, table, group_by)
        if not column:
            # 维度名（route/section）不是列名：按**关系链**落到这张表真正的那个外键列上
            # （"各条路线分别有多少座桥梁"→ basic_facilities_bridges.routes_id，再由
            # generic.group_distribution JOIN 路线表按名字分组）。这不是"拿别的列顶替"，
            # 是把这个维度在这张表上的落点找出来；找不到才走下面的弃权。
            target = _SUBJECT_TABLES.get(group_by)
            if target:
                path = catalog.relation_path(kind, table, target_table=target)
                if path:
                    column = catalog.resolve_column(kind, table, str(path[0]))
        if not column:
            # R3：分组维度在这张表里不存在时**不许拿别的列顶替**（"按技术等级分布"被顶成
            # "按采集时间分布"就是张冠李戴）→ 整条弃权交 Wren
            # （docs/通用算子触发规则收紧方案.md R3）。
            log_event("capabilities", "INFO", "generic_dist_skipped",
                      reason=f"分组维度 {group_by} 在 {table} 里找不到")
            return []
        if column:
            sql, scope = generic.group_distribution(
                kind, table, str(column["name"]), filters=filters
            )
            if sql:
                out.append(
                    {
                        "kind": "generic",
                        "id": f"generic_dist:{table}:{column['name']}",
                        "title": f"通用分组：按{column.get('label')}统计",
                        "question": str(message or ""),
                        "scope": scope,
                        "shape": shape,
                        "subject": subject,
                        "slots": [],
                        "score": 4.0,
                        "reasons": _human_reasons(
                            plan, kind, table, shape, filters,
                            f"按「{column.get('label')}」分组统计数量与占比",
                        ),
                        "warn": not filters,
                        "sql_preview": sql,
                    }
                )
    # 「边坡的坡度分布」「各隧道的照明情况」「养护项目的投资额分布」：用户点了一列当分组维度，
    # 但语义层没给 group_by（它只说了 shape=distribution）→ 上面那个分支进不去，以前这类题
    # **一个候选都没有**，用户看到的是"没有任何推荐"，而字段目录里明明有那张表那一列
    # （docs/同名列多表消歧方案.md §七.3：别只服务病害+路线）。
    # 分组列用**字段触达在这张表上挑出来的那一列**（列注释词元重合，允许"照明"对上
    # "是否有隧道照明"这种长短不一致的写法）；最高分并列时说明没听懂问的是哪一列 → 弃权，
    # 不许拿别的列顶替（R3）。
    if shape == "distribution" and not group_by:
        column = _distribution_column(plan, table)
        if not column:
            # 「修改人」这种两字题：语义层只说了 distribution、字段触达也没挑出列，但用户在
            # 问题里点名了一列（列注释整段命中）→ 他要的就是"这一列有哪些取值"。
            # 只有"点名列恰好落在这张落点表上"才接，别拿别的表的同名列顶替（R3）。
            owner, named = _catalog_named_column(kind, message, prefer_table=table)
            if owner and named and owner == table:
                sql, scope = generic.distinct_values(kind, owner, named, filters)
                if sql:
                    out.append(
                        _generic_card(
                            "distinct", owner, scope, sql, subject, shape, message,
                            plan=plan, kind=kind, filters=filters,
                            action=f"列出「{named}」的取值",
                        )
                    )
        if column:
            column_name = str(column["column"])
            label = str(column.get("label") or column_name)
            sql, scope = generic.group_distribution(kind, table, column_name, filters=filters)
            if sql:
                out.append(
                    {
                        "kind": "generic",
                        "id": f"generic_dist:{table}:{column_name}",
                        "title": f"通用分组：按{label}统计",
                        "question": str(message or ""),
                        "scope": scope,
                        "shape": shape,
                        "subject": subject,
                        "slots": [],
                        "score": 4.0,
                        "reasons": _human_reasons(
                            plan, kind, table, shape, filters,
                            f"按「{label}」分组统计数量与占比",
                        ),
                        "warn": not filters,
                        "sql_preview": sql,
                    }
                )
    if shape == "count":
        # "一共多少条/几处"要的是单值 COUNT(*)；明细行数被 LIMIT 截断会报错总数
        # （P0-4：把 534 处报成 500 处）。
        sql, scope = generic.generic_count(kind, table, filters)
        if sql:
            out.append(
                {
                    "kind": "generic",
                    "id": f"generic_count:{table}",
                    "title": (
                        f"通用计数：{catalog.table_label(kind, table)}"
                        + ("（兜底口径）" if not filters else "")
                    ),
                    "question": str(message or ""),
                    "scope": scope,
                    "shape": shape,
                    "subject": subject,
                    "slots": [],
                    "score": 4.5,
                    "reasons": _human_reasons(plan, kind, table, shape, filters, "数符合条件的行数"),
                    "warn": not filters,
                    "sql_preview": sql,
                }
            )
    if shape in {"list", "detail", "photo"}:
        sql, scope = generic.generic_detail(
            kind, table, usable, filters, limit=500
        )
        if sql:
            out.append(
                {
                    "kind": "generic",
                    "id": f"generic_detail:{table}",
                    "title": (
                        f"通用明细：{catalog.table_label(kind, table)}"
                        + ("（兜底口径）" if not filters else "")
                    ),
                    "question": str(message or ""),
                    "scope": scope,
                    "shape": shape,
                    "subject": subject,
                    "slots": [],
                    "score": 4.5,
                    "reasons": _human_reasons(plan, kind, table, shape, filters, "列出明细"),
                    "warn": not filters,
                    "sql_preview": sql,
                }
            )
    # ── 其余通用算子也要能在对话里匹配到（方案 §三：不止明细/分组）────────────
    if shape == "top" and group_by:
        column = catalog.resolve_column(kind, table, group_by)
        if column:
            # R4：指标和聚合方式只能由语义层的 metrics 决定，不看用户原话里的关键词
            metric_column, agg = _rank_metric(plan)
            sql, scope = generic.rank(
                kind,
                table,
                str(column["name"]),
                metric_column,
                agg,
                int((plan.get("semantic") or {}).get("limit") or 10),
                filters,
            )
            if sql:
                out.append(_generic_card(
                    "rank", table, scope, sql, subject, shape, message,
                    plan=plan, kind=kind, filters=filters,
                    action=f"按「{column.get('label')}」取 {agg}({metric_column or '条数'}) 前 N",
                ))
    if group_by == "month" or shape == "trend":
        column_name = _time_column(kind, table, plan.get("slots") or {})
        if column_name:
            sql, scope = generic.time_series(kind, table, column_name, "month", filters=filters)
            if sql:
                out.append(_generic_card(
                    "time_series", table, scope, sql, subject, shape, message,
                    plan=plan, kind=kind, filters=filters, action="按月份统计数量",
                ))
    # 经纬度范围：看**语义层抽出的 output_fields**（lng/lat/map_position…），不靠"经纬度"关键词
    semantic_fields = {str(item) for item in (plan.get("semantic") or {}).get("output_fields") or []}
    geo_fields = {"lng", "lat", "lon", "map_position", "coordinate", "start_lnt", "end_lnt"}
    if semantic_fields & geo_fields:
        row = catalog.table_entry(kind, table) or {}
        if any("geo" in (col.get("roles") or []) for col in row.get("columns") or []):
            sql, scope = generic.geo_bounds(kind, table, filters)
            if sql:
                out.append(_generic_card(
                    "geo", table, scope, sql, subject, shape, message,
                    plan=plan, kind=kind, filters=filters, action="统计经纬度范围",
                ))
    # ── 用户点名了一列、问"有哪些取值"（高精度小入口，docs/冷门字典列可达与generic精确命中方案.md §三.2）
    # "病害的计算公式有哪些"这类题：列注释一对一命中某一列（formula）、没有过滤条件时，
    # 直接给这条列的取值清单（复用 generic.distinct_values，编码值会翻中文）。
    # 一个词对应多列 / 目录里找不唯一 → 不接，交给 Wren。
    if shape in {"list", "detail"}:
        owner, column_name = _catalog_named_column(kind, message, prefer_table=table)
        if owner and column_name:
            # 带条件的问法（"西乡段的损坏程度有哪些"）也算：条件落到这张表能表达就带上，
            # 表达不了（字典表没有路线列）就弃权——宁可不接，不可把条件丢掉
            distinct_filters, distinct_missing = _generic_filters(plan, kind, owner)
            sql, scope = (
                generic.distinct_values(kind, owner, column_name, distinct_filters)
                if not distinct_missing
                else (None, "")
            )
            if sql:
                out.append(_generic_card(
                    "distinct", owner, scope, sql, subject, shape, message,
                    plan=plan, kind=kind, filters=distinct_filters,
                    action=f"列出「{column_name}」的取值",
                ))
    return out


def _catalog_named_column(
    kind: str, message: str, prefer_table: str = ""
) -> tuple[str, str]:
    """在**字段目录（列注释）**里找用户点名的列，返回 (物理表, 列名)；判不准返回空。

    为什么不用 `nl2sql.named_columns`：那个走 Wren 模型的注释（MDL），和库里列注释不一定一致
    ——实测 `ai_problem.damage_type` 的库注释是「损坏程度」，模型侧对不上，于是"损坏程度有哪些"
    一个列都命不中。这题的判据本来就是"列注释是否出现在问题里"，字段目录就是唯一事实源
    （docs/冷门字典列可达与generic精确命中方案.md §三.3：复用目录，不写字段映射）。

    判据：取命中注释最长的那个；其余命中若是它的一部分（"类型" ⊂ "病害类型"）算泛词噪声忽略；
    否则视为一词多列 → 弃权。同一注释命中多张表时，优先落在本轮触达的表上。
    """
    from . import schema_catalog as catalog

    text = str(message or "")
    hits: list[tuple[str, str, str]] = []
    for table in catalog.field_catalog(kind).get("tables") or []:
        for col in table.get("columns") or []:
            label = str(col.get("label") or "").strip()
            if len(label) >= 2 and label in text:
                hits.append((label, str(table.get("table")), str(col.get("name"))))
    if not hits:
        return "", ""
    hits.sort(key=lambda item: -len(item[0]))
    top_label = hits[0][0]
    noisy = [
        item for item in hits[1:] if str(item[0]) not in top_label
    ]
    if noisy:
        return "", ""
    same_label = [item for item in hits if item[0] == top_label]
    if prefer_table:
        preferred = [item for item in same_label if item[1] == prefer_table]
        if preferred:
            return preferred[0][1], preferred[0][2]
    if len(same_label) == 1:
        return same_label[0][1], same_label[0][2]
    if len({item[1] for item in same_label}) == 1 and len({item[2] for item in same_label}) == 1:
        return same_label[0][1], same_label[0][2]
    # 同一个注释命中**多张表**、又没有本轮触达的表可优先 → 判不准，弃权交 Wren。
    # 不许按目录顺序挑一张：实测"路段的起止桩号"里的"桩号"会挑到 ai_asset_main.pile_number、
    # "资产的经纬度"里的"纬度"会挑到桩点表——这正是"优先级固定导致选错表"的那类错
    # （docs/同名列多表消歧方案.md §七.3）。
    return "", ""


def _distribution_column(plan: dict[str, Any], table: str) -> dict[str, Any] | None:
    """「X 的分布」缺分组维度时，用字段触达在**这张表**上挑出的那一列当分组维度。

    判据只有一条：最高分的那一列必须唯一。多个并列 = 没听懂问的是哪一列，弃权交 Wren
    （R3：不许拿别的列顶替——"边坡的坡度分布"就该弃权，因为边坡表根本没有坡度列，
    而不是退而求其次按"高度"分组）。时间列也不算：那是趋势题的口径，归 time_series 分支。
    """
    from . import field_grounding

    rows = [
        row
        for row in field_grounding.columns_of(plan, table)
        # 时间列不算（趋势题的口径，归 time_series 分支）；经纬度也不算——"纬度范围"问的是
        # 这一列的取值范围，把它当分组维度会答成"按纬度分组统计"（实测 generic_dist:…:lat）
        if not ({"time", "geo"} & set(row.get("roles") or []))
    ]
    if not rows:
        return None
    best = max(float(row.get("score") or 0) for row in rows)
    top = [row for row in rows if float(row.get("score") or 0) == best]
    return top[0] if len(top) == 1 else None


def _generic_card(
    operator: str,
    table: str,
    scope: str,
    sql: str,
    subject: str,
    shape: str,
    message: str,
    plan: dict[str, Any] | None = None,
    kind: str = "mysql",
    filters: list[dict[str, Any]] | None = None,
    action: str = "",
) -> dict[str, Any]:
    """通用算子候选卡片（统一结构，前端按 kind=generic 渲染）。"""
    return {
        "kind": "generic",
        "id": f"generic_{operator}:{table}",
        "title": f"通用算子：{scope.split('；')[0][:24]}",
        "question": str(message or ""),
        "scope": scope,
        "shape": shape,
        "subject": subject,
        "slots": [],
        "score": 4.2,
        "reasons": (
            _human_reasons(plan or {}, kind, table, shape, filters, action or scope.split("；")[0])
            if plan
            else ["按字段目录拼好的口径（0 次大模型），可直接核对"]
        ),
        "warn": bool(plan) and not filters,
        "sql_preview": sql,
    }


def collect_capabilities(
    message: str,
    plan: dict[str, Any],
    kind: str = "mysql",
    limit: int = 0,          # 0 = 用 .env 的 CAPABILITY_LIMIT
    deterministic_only: bool = False,
    want_images: bool = False,
) -> list[dict[str, Any]]:
    """收集候选（只收集不执行），按分数倒序，第一条即推荐。

    deterministic_only=True：只回"确定性模板 + 相似已验证问法"（复合题的整题闸用）。
    通用算子/代码模板口径最硬、但最容易在"一句话含两问"时只答一半，所以复合题不摆它们。

    want_images："这句是要看图"的语义结论（意图分类给），图片题据此只取真有图的行。
    """
    _started = time.perf_counter()
    limit = int(limit or settings.capability_limit)
    candidates: list[dict[str, Any]] = []
    # 顺序（docs/RAG示例直达与AI直答旁路方案.md §五）：手写模板 → 自动模板 → 相似已验证问法，
    # 都没有才轮到通用算子/代码模板。确定性模板排在前面，但**都要用户确认**才执行。
    # 每类多收一点：用户反馈"多给一点推荐"，而且通用算子常常把卡片占满，
    # 个性化模板/看板示例反而看不见。分类上限在下面排序后统一施加。
    deterministic = _preset_candidates(plan, limit=3) + _auto_candidates(plan, limit=3)
    candidates.extend(deterministic)
    # 已有确定性候选时，默认只补"同一句"的精确示例（省一次便宜模型打分，方案 §3.7）；
    # 但这些候选**槽位不齐**（弱候选，用户还得自己补条件）时不能省——看板里那条几乎一字不差的
    # 同义题（"请列出…"只差个前缀）比一堆待补槽的模板有价值得多
    # （docs/auto模板误匹配与看板同义召回被关方案.md §三.1/§三.4）。
    weak_deterministic = bool(deterministic) and any(
        item.get("missing_slots") for item in deterministic
    )
    candidates.extend(
        _example_candidates(
            message,
            plan,
            kind,
            limit=2 if deterministic else 3,
            only_exact=bool(deterministic) and not weak_deterministic,
        )
    )
    if not deterministic_only:
        # 通用算子 + 代码模板**始终参与候选**（只是排在模板/示例后面，同类最多 2 条）。
        # 以前"前面命中就不收它们"，用户看到的是"通用算子把卡占满"或"只有一个模板"——
        # 两个方向都不对：让候选都摆出来、按契合度排序，选哪个仍然由用户拍板。
        candidates.extend(_generic_candidates(message, plan, kind, want_images=want_images))
        candidates.extend(_code_template_candidates(message, plan))
    if not candidates:
        return []
    if want_images:
        # 看图题只摆"SQL 里真的会取图片列"的口径：否则用户点开是"病害明细/巡查任务"这种
        # 跟图片无关的卡（docs/弹卡链路系统性Bug整改方案.md Bug 2）。
        # 图片列一律来自字段目录 role=image（不写死名单）。
        from . import schema_catalog as _catalog

        image_columns = _catalog.image_column_names(kind)
        if image_columns:
            candidates = [
                item
                for item in candidates
                if any(name in str(item.get("sql_preview") or "") for name in image_columns)
            ]
            if not candidates:
                return []
    # 同一条口径可能同时被"结构匹配"和"同义问法召回"拿到：按 (kind, id) 去重，
    # 保留分数高的那条，免得确认卡上摆两张一模一样的卡。
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in candidates:
        key = (str(item.get("kind")), str(item.get("id")))
        if key not in unique or float(item.get("score") or 0) > float(unique[key].get("score") or 0):
            unique[key] = item
    candidates = list(unique.values())
    # 分层排序：确定性模板（preset/auto）在前，相似示例其次，通用算子/代码模板最后；
    # 同层内仍按分数排（保持原有"刚好对上"优先的规则）。
    tier = {
        "preset": 0,
        "auto": 0,
        # 看板/示例库的"同一句已验证问法"紧跟个性化模板，不被通用算子压到后面
        "example": 1,
        "example_direct": 1,
        "saved": 1,
        "saved_example": 1,
        "generic": 2,
        "code_template": 2,
    }
    candidates.sort(
        key=lambda item: (tier.get(str(item.get("kind")), 3), -float(item.get("score") or 0))
    )
    # 分差接近时让一次小模型**只排序**（不让它生成/改 SQL）；失败就保持规则顺序
    # （docs/确定性能力人工确认方案.md §四.2）。
    # 但**已经命中确定性模板时不排**：模板是 0 次大模型的路径，为排序花一次调用
    # 既慢又违背"L0/L1 零调用"（docs/RAG示例直达与AI直答旁路方案.md §七）。
    # 另外**只在同一层内比"分差接近"**：不同层的分数不可比（example 是 0~1、通用算子是 4.x），
    # 跨层相减恒为负 → 每次混层都会白花一次 20s 的排序模型（实测"每种病害类型对应的病害代码"
    # 因此从 14s 涨到 35s）。层与层之间的先后已经由 tier 定了，不需要模型再排。
    if (
        str(candidates[0].get("kind")) not in {"preset", "auto"}
        and len(candidates) >= 2
        and tier.get(str(candidates[0].get("kind")), 3) == tier.get(str(candidates[1].get("kind")), 3)
        and float(candidates[0].get("score") or 0) - float(candidates[1].get("score") or 0)
        <= settings.capability_rerank_max_gap
    ):
        _rerank_started = time.perf_counter()
        candidates = _llm_rerank(message, candidates)
        log_event(
            "capabilities",
            "INFO",
            "rerank_ms",
            ms=round((time.perf_counter() - _rerank_started) * 1000),
            question=str(message)[:60],
        )
    # 同类限量：通用算子/代码模板很"便宜"地就能拼出来，容易把卡片占满，
    # 个性化模板和看板示例反而被挤到看不见。同类最多 2 条，空位再补满。
    candidates = _apply_kind_caps(candidates, limit)
    # 默认推荐位：**纯兜底的通用算子不占第一位**（"别的都没命中就把它当权威答案"是错的，
    # 它只是最便宜的兜底）。有非兜底候选时，把它们排在前面
    # （docs/示例直达被generic挤占与人话推荐理由整改.md P0-3、通用算子触发规则收紧方案.md R5）。
    candidates = _prefer_non_fallback(candidates)
    for index, item in enumerate(candidates):
        item["recommended"] = index == 0
    # 弹卡耗时打点（docs/弹卡链路系统性Bug整改方案.md Bug 4）：候选收集整体耗时 + 条数
    log_event(
        "capabilities",
        "INFO",
        "collected",
        ms=round((time.perf_counter() - _started) * 1000),
        candidates=len(candidates[:limit]),
        want_images=bool(want_images),
        question=str(message)[:60],
        # 把候选 id 一起记下来：生命周期巡检（"摆了 N 次却从没被复用" → cold）靠它统计，
        # 不用每题都去写模板注册表（docs/模板沉淀标准与复用校验_系统性方案.md §5.3）
        # 回归/跑批时**不记候选 id**：那些"被摆出来"的次数会被生命周期巡检当成真实曝光，
        # 把刚挖出来的好模板误标成冷门（实测：一次回归跑批就触发了）。
        ids=(
            ""
            if _os.environ.get("CODEX_REGRESSION")
            else "、".join(str(item.get("id")) for item in candidates[:limit] if item.get("id"))
        ),
    )
    return candidates[:limit]


_KIND_CAP = {
    "generic": settings.capability_per_kind_cap,
    "code_template": settings.capability_per_kind_cap,
}


def _prefer_non_fallback(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """纯兜底/口径存疑的候选排到"条件完全对上"的候选后面。"""
    fallback = [item for item in candidates if _is_fallback(item)]
    solid = [item for item in candidates if not _is_fallback(item)]
    return solid + fallback


def _is_fallback(item: dict[str, Any]) -> bool:
    if item.get("loose"):
        # 示例还带了本轮没提的条件（默认沿用示例值）：能用，但不该排在"刚好对上"的前面
        return True
    return str(item.get("kind")) == "generic" and bool(item.get("warn"))


def _apply_kind_caps(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    for item in candidates:
        cap = _KIND_CAP.get(str(item.get("kind")))
        over = cap is not None and sum(
            1 for chosen in selected if str(chosen.get("kind")) == str(item.get("kind"))
        ) >= cap
        if over or len(selected) >= limit:
            deferred.append(item)
        else:
            selected.append(item)
    for item in deferred:
        if len(selected) >= limit:
            break
        selected.append(item)
    return selected


def _llm_rerank(message: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规则分差接近时，请小模型只做一次排序（拿不到结果就用原顺序）。

    结果按"问题 + 候选集合"缓存：同一个问题两次问，模型哪怕给出不同排序，
    用户看到的推荐顺序也一致（实测"同一个问题问两次推荐不一样"就是这么来的）。
    """
    from . import prompts
    from .llm import LLMError, chat_json, role_model

    key = (" ".join(str(message or "").split())[:80], tuple(str(item.get("id")) for item in candidates))
    cached = _RERANK_CACHE.get(key)
    if cached:
        return _apply_order(candidates, cached, {})
    # 候选排序不再让模型插一脚：口径选哪条**最终由用户在确认卡上点**，模型再排一次只会
    # 把用户能一眼看懂的候选顺序搅乱（`prompts/rank_capabilities.md` 已删）。
    # 保留函数与缓存结构，方便以后要恢复；现在直接按确定性分数返回。
    return candidates


_RERANK_CACHE: dict[tuple[str, tuple[str, ...]], list[str]] = {}


def _apply_order(
    candidates: list[dict[str, Any]], order: list[Any], reasons: dict[str, Any]
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for wanted in order:
        match = next((item for item in candidates if str(item.get("id")) == str(wanted)), None)
        if match and match not in ranked:
            ranked.append(match)
    for item in candidates:
        if item not in ranked:
            ranked.append(item)
    for item in ranked:
        note = str(reasons.get(str(item.get("id"))) or "").strip()
        if note:
            item["reasons"] = [f"排序理由：{note[:60]}"] + list(item.get("reasons") or [])
    return ranked


def stash(
    plan: dict[str, Any],
    candidates: list[dict[str, Any]],
    tasks: list[dict[str, Any]] | None = None,
    want_images: bool = False,
) -> str:
    """把 plan + 候选暂存起来，第二次请求凭 token 直接执行（不重新规划）。

    tasks 只在"复合题整题卡"里带：确认后要按这条口径出数，同时把"原问题其实分几步"
    一起带回来，好在回答里如实说明这次只覆盖了其中一部分。

    want_images："这句是要看图"的**语义结论**（由意图分类给出），随卡片一起带走——
    点确认那一次请求不会再问一遍意图（省一次模型往返），更不会退回关键词猜。
    """
    return _stash_payload(
        {
            "plan": plan,
            "candidates": candidates,
            "tasks": tasks or [],
            "want_images": bool(want_images),
        }
    )


def _stash_payload(payload: dict[str, Any]) -> str:
    token = uuid.uuid4().hex
    now = time.time()
    for key, (ts, _payload) in list(_STASH.items()):
        if now - ts > _TOKEN_TTL_SECONDS:
            _STASH.pop(key, None)
    _STASH[token] = (now, payload)
    return token


def take(token: str) -> dict[str, Any] | None:
    item = _STASH.pop(str(token or ""), None)
    if not item:
        return None
    ts, payload = item
    if time.time() - ts > _TOKEN_TTL_SECONDS:
        return None
    return payload


def _slot_overrides(slots: list[dict[str, Any]], values: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for slot in slots:
        name = str(slot.get("name") or "")
        if not name:
            continue
        # 用户在卡上给了这个槽（哪怕是空串）就用他的；没给才用默认值。
        # 以前是 `values.get(name) or slot.get("value")`，清空某个条件会被静默退回默认值。
        raw = (values or {}).get(name)
        value = str(slot.get("value") if raw is None else raw or "").strip()
        if value or raw is not None:
            out[name] = value
    return out


def _parse_time_range(text: str) -> tuple[str, str] | None:
    """卡片上的时间范围文本 → 起止时间（支持"2026-08" / "2026-08-01 ~ 2026-09-01"）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    parts = [part.strip() for part in re.split(r"[~～至到]|--", raw) if part.strip()]
    if len(parts) == 1 and re.fullmatch(r"\d{4}-\d{2}", parts[0]):
        year, month = int(parts[0][:4]), int(parts[0][5:7])
        end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
        return f"{year:04d}-{month:02d}-01 00:00:00", f"{end_year:04d}-{end_month:02d}-01 00:00:00"
    if len(parts) == 2:
        start, end = parts
        if re.fullmatch(r"\d{4}-\d{2}", start):
            start = f"{start}-01 00:00:00"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
            start = f"{start} 00:00:00"
        if re.fullmatch(r"\d{4}-\d{2}", end):
            year, month = int(end[:4]), int(end[5:7])
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
            end = f"{year:04d}-{month:02d}-01 00:00:00"
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            end = f"{end} 00:00:00"
        if start and end:
            return start, end
    return None


def _parse_pile_range(text: str) -> tuple[int, int] | None:
    """卡片上的桩号区间文本 → 米数（"800 ~ 900" / "K0+800~K0+900" 都认）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = parsers.parse_pile_range(raw)
    except Exception:  # noqa: BLE001
        parsed = None
    if parsed:
        return int(parsed[0]), int(parsed[1])
    numbers = re.findall(r"\d+", raw)
    if len(numbers) >= 2:
        return int(numbers[0]), int(numbers[1])
    return None


def execute(
    plan: dict[str, Any],
    candidate: dict[str, Any],
    values: dict[str, str] | None = None,
    kind: str = "mysql",
) -> tuple[str | None, str]:
    """按用户选定的候选（可能改过槽位）产出可执行 SQL。返回 (sql, 说明)。"""
    overrides = _slot_overrides(list(candidate.get("slots") or []), values or {})
    kind_name = str(candidate.get("kind") or "")
    if kind_name == "preset":
        query = preset_lib.query_by_id(str(candidate.get("id") or ""))
        if not query:
            return None, "这条模板已经不在了"
        sql, reason = preset_lib.bind(query, overrides)
        if not sql:
            return None, reason
        return sql, f"模板 {candidate.get('id')}"
    if kind_name in {"example", "example_direct"}:
        spec = {
            "route": "",
            "route_id": overrides.get("route_id", ""),
            "damage_type": [overrides["damage_type"]] if overrides.get("damage_type") else [],
        }
        if candidate.get("spec"):
            spec.update(candidate["spec"])
        if overrides.get("route_id"):
            spec["route_id"] = overrides["route_id"]
            # 名称也要跟着换：`{route_name}` 占位是从 spec["route"] 填的，只改 id 会拼出老路线名
            label = ""
            for option in _slots_route():
                if str(option.get("value")) == overrides["route_id"]:
                    label = str(option.get("label") or "").split("（")[0].strip()
                    break
            if label:
                spec["route"] = label
                spec["routes"] = [label]
            spec["route_ids"] = [overrides["route_id"]]
        if overrides.get("damage_type"):
            spec["damage_type"] = [overrides["damage_type"]]
        if overrides.get("time_range"):
            span = _parse_time_range(overrides["time_range"])
            if span:
                spec["time_range"] = list(span)
        if overrides.get("pile_range"):
            span = _parse_pile_range(overrides["pile_range"])
            if span:
                spec["pile_range"] = list(span)
        if overrides.get("device_code"):
            spec["device_code"] = overrides["device_code"]
        normalized = str(candidate.get("normalized_sql") or candidate.get("sql_preview") or "")
        filled = nl2sql.fill_example_slots(normalized, spec)
        if not filled:
            return None, "这条示例的条件没法按当前槽位补全"
        # 列别名里写死的旧路线名要跟着新参数走，否则"标签写西乡、值是恒明珠"
        # （docs/模板沉淀标准与复用校验_系统性方案.md §4.4）。按槽位一一对应换，
        # 旧值在 SQL 里出现多次就跳过（那多半还牵着过滤条件，宁可不换标签）。
        stored = candidate.get("spec") or {}
        old_names = ([str(stored.get("route"))] if stored.get("route") else []) + [
            str(item) for item in (stored.get("routes") or [])
        ]
        new_names = ([str(spec.get("route"))] if spec.get("route") else []) + [
            str(item) for item in (spec.get("routes") or [])
        ]
        # 多对象对比**不做别名替换**（docs/模板沉淀标准与复用校验_系统性方案.md §4.4 ⚠️）：
        # A→B、B→C、C→A 这种循环替换在多对象场景下极易出错，风险远大于收益；
        # 这类题只用示例原始 SQL + 参数回填，标签对不上就如实显示。
        multi_object = example_reuse.is_multi_object(stored) or example_reuse.is_multi_object(spec)
        if len(old_names) == len(new_names) and old_names and not multi_object:
            filled, relabeled = example_reuse.relabel_filled_values(filled, old_names, new_names)
            if relabeled:
                log_event(
                    "capabilities",
                    "INFO",
                    "example_alias_relabeled",
                    count=len(relabeled),
                    names="、".join(item[:12] for item in relabeled[:3]),
                )
        return filled, f"示例 {candidate.get('id')}"
    if kind_name == "code_template":
        try:
            sql = nl2sql.build_template_sql(plan)
        except Exception as exc:  # noqa: BLE001
            return None, f"模板重算失败：{exc}"
        return (sql, "内置代码模板") if sql else (None, "这条内置模板这次拼不出来")
    if kind_name == "auto":
        template = templatizer.find(str(candidate.get("template_id") or candidate.get("id") or ""))
        if not template:
            return None, "这条自动模板已经不在了"
        sql, reason = templatizer.bind(template, overrides)
        if not sql:
            return None, reason
        templatizer.bump_usage(str(template.get("id")))
        return sql, f"自动模板 {template.get('id')}"
    if kind_name == "generic":
        # 通用算子的 SQL 在候选收集时就已经按字段目录拼好并校验过了（列名/编码/数值都来自目录），
        # 这里直接执行它，不必再转一圈 Wren —— 以前没有这个分支，用户点了直接报错
        # （docs/复杂模板匹配与推荐改造方案.md P2-8）。
        sql = str(candidate.get("sql_preview") or "").strip()
        if not sql:
            return None, "这条通用算子的 SQL 没准备好"
        return sql, f"通用算子 {candidate.get('id')}"
    return None, "未知的候选类型"


def apply_overrides(plan: dict[str, Any], values: dict[str, str] | None) -> dict[str, Any]:
    """把用户在确认卡里改过的槽位写回 plan。

    不写回去的话，执行前校验还按老条件判（"路线过滤条件"里的 id 对不上），
    会把用户明确选的口径当成错的拦下来。
    """
    if not values:
        return plan
    slots = plan.setdefault("slots", {})
    route_id = str(values.get("route_id") or "").strip()
    if route_id:
        label = ""
        for option in _slots_route():
            if str(option.get("value")) == route_id:
                label = str(option.get("label") or "").split("（")[0]
                break
        slots["route_match"] = {"id": route_id, "short_name": label, "full_name": label}
        slots["route"] = label
    damage = str(values.get("damage_type") or "").strip()
    if damage:
        slots["damage_matches"] = [{"problem_code": "", "problem_type": damage}]
        slots["damage_type"] = damage
    return plan
