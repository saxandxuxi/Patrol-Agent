"""L2 示例直达：RAG 示例召回 + 便宜模型严格同问打分（docs/RAG示例直达与AI直答旁路方案.md §三）。

复用已经写好的召回与硬门槛（`nl2sql.find_reusable_examples`：结构同型 + 槽位对齐 +
条件完整覆盖），这里只补两件事：

1. **严格同问判定**：非同一句的问法，用便宜模型按 7 要素打分（0~1），只做判定，绝不写 SQL；
2. **打分 → 摆卡**：模型判 `same_query` + 分数 ≥ `rag_direct_threshold` + 必需槽位齐全，
   就把这条示例摆进确认卡；`differences`（措辞差异）只当提示、不再一票否决
   （docs/示例参数对齐与同义召回放宽方案.md 第二节）。两条候选分不开时仍不摆（margin）。

注意：这里只产出**候选**，要不要用、用哪条，由用户在卡片上点确认（§〇 最高原则）。
"""

from __future__ import annotations

import time
from typing import Any

from . import chat_settings, nl2sql, prompts
from ..config import settings
from .llm import LLMError, chat_json, role_model
from .logging_service import log_error, log_event

# 打分结果短时缓存：同一句反复问不再重复花那一次便宜模型
_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_CACHE_TTL = settings.plan_cache_ttl_seconds
_CACHE_LIMIT = settings.cache_max_entries


def gate(
    message: str, plan: dict[str, Any], kind: str = "mysql", only_exact: bool = False
) -> list[dict[str, Any]]:
    """返回通过 L2 硬门槛的示例候选（按分数倒序，第一条即推荐）。

    only_exact=True：已经有确定性模板候选时用——只回"同一句"的精确命中，
    不再花那一次便宜模型打分（方案 §3.7：L1 能给出候选时不再调用 L2）。
    """
    settings = chat_settings.load()
    if not bool(settings.get("rag_direct_enabled", True)):
        return []
    if str(kind) != "mysql":
        return []
    topk = int(settings.get("rag_direct_topk") or 8)
    try:
        # 已经有确定性模板候选时只认"同一句"——那就走纯本地的精确查找，
        # 连向量召回都不用做（实测这一步要 7~10 秒，白花在已经有模板的题上）
        hits = nl2sql.find_reusable_examples(
            message,
            kind,
            plan=None if only_exact else plan,
            limit=1 if only_exact else topk,
            # 有打分模型 + 用户确认兜底的这条路：召开放宽（结构不一致也让它看一眼）
            loose=not only_exact,
        )
    except Exception as exc:  # noqa: BLE001
        log_error("direct_match", "recall_failed", exc, question=message[:60])
        return []
    if not hits:
        return []
    # 同一句（本地精确命中）不用花模型：直接当高置信候选
    exact = [hit for hit in hits if str(hit.get("confidence")) == "exact"]
    if exact:
        for hit in exact:
            hit["score"] = 1.0
            hit["reasons"] = ["问法完全一致（本地示例）"]
        return exact[:1]
    if only_exact:
        return []
    fast = _fast_accept(message, hits, plan)
    if fast:
        return fast
    # 只要一个阈值：相似度 ≥ 门槛 + 结构同型 → 直接摆卡。
    # 不再有打分模型（~20s）、不再看与次名分差——最后由用户在卡上点确认。
    log_event(
        "direct_match",
        "INFO",
        "fast_path_miss",
        top=round(float(hits[0].get("score") or 0), 2),
        question=message[:60],
    )
    return []


def _key(message: str) -> str:
    return " ".join(str(message or "").split())[:80]


def _score(
    message: str, hits: list[dict[str, Any]], plan: dict[str, Any]
) -> list[dict[str, Any]]:
    """便宜模型严格同问打分 + 代码硬门槛（阈值 + 分差 + 槽位齐 + 结构一致）。"""
    settings = chat_settings.load()
    threshold = float(settings.get("rag_direct_threshold") or 0.75)
    margin = float(settings.get("rag_direct_margin") or 0.08)
    listing = "\n".join(_describe(index, hit) for index, hit in enumerate(hits))
    try:
        data = chat_json(
            prompts.messages("example_direct_match", question=message, candidates=listing),
            model=role_model("semantic"),
        )
    except LLMError as exc:
        log_error("direct_match", "score_failed", exc, question=message[:60])
        return []
    except Exception as exc:  # noqa: BLE001
        log_error("direct_match", "score_failed", exc, question=message[:60])
        return []
    verdicts: dict[int, dict[str, Any]] = {}
    for item in (data.get("matches") or []) if isinstance(data, dict) else []:
        index = _index_of(str((item or {}).get("candidate_id") or ""), hits)
        if index is None:
            continue
        verdicts[index] = item
    accepted: list[dict[str, Any]] = []
    for index, item in verdicts.items():
        if not item.get("same_query"):
            continue
        if float(item.get("score") or 0) < threshold:
            continue
        # 缺槽分两种：示例自己**有默认值**的（时间/路线/病害类型…本轮没提就沿用示例值，
        # 摆在卡上可改）不算缺；示例也没有的才算真的缺，直接丢。
        # （docs/示例参数对齐与同义召回放宽方案.md 第一节：默认值取本轮，本轮没提的沿用示例）
        unfillable = [
            str(slot)
            for slot in (item.get("missing_slots") or [])
            if not _spec_can_fill(hits[index].get("spec") or {}, str(slot))
        ]
        if unfillable:
            continue
        # differences 只当**提示**，不再一票否决：同义改写（"有多少种"省略、"分别"换"每种"）
        # 总会让模型写出一两条措辞差异，以前直接丢弃 → 换个说法就召回不到
        # （docs/示例参数对齐与同义召回放宽方案.md 第二节）。模型判 same_query=false 的
        # （换对象/换指标）仍然会被上面那条挡住；最终还有用户在卡上点确认兜底。
        differences = [str(one) for one in (item.get("differences") or []) if str(one).strip()]
        hit = dict(hits[index])
        hit["score"] = round(float(item.get("score") or 0), 2)
        hit["reasons"] = [
            "同问判定通过（口径一致）",
            f"同问分 {hit['score']}",
            "条件完整覆盖：" + ("、".join(sorted(nl2sql._reuse_condition_keys(hit.get("spec") or {}))) or "无条件"),
        ]
        if differences:
            hit["reasons"].append("措辞差异（不影响口径）：" + "；".join(differences)[:60])
        if item.get("reason"):
            hit["reasons"].append(str(item["reason"])[:60])
        accepted.append(hit)
    accepted.sort(key=lambda item: -float(item.get("score") or 0))
    if len(accepted) >= 2 and (
        float(accepted[0]["score"]) - float(accepted[1]["score"]) < margin
    ):
        # 两条候选分不开 → 不冒险直达，交 L3（或让人在卡上自己挑）
        log_event(
            "direct_match",
            "INFO",
            "margin_too_close",
            top1=accepted[0]["score"],
            top2=accepted[1]["score"],
        )
        return []
    return accepted[:1]


def _index_of(candidate_id: str, hits: list[dict[str, Any]]) -> int | None:
    for index, hit in enumerate(hits):
        if str(hit.get("doc_name") or "") == candidate_id or candidate_id == f"c{index}":
            return index
    if candidate_id.isdigit() and int(candidate_id) < len(hits):
        return int(candidate_id)
    return None


def _fast_accept(
    message: str,
    hits: list[dict[str, Any]],
    plan: dict[str, Any],
) -> list[dict[str, Any]]:
    """高置信命中直接摆卡，**跳过同问打分模型**（省一次 3~10 秒的大模型调用）。

    判据全部是本地已有信息，不额外花时间：
      1) 相似度（题目索引/骨架分，已含"换月份/换路线同骨架 = 1.0"）≥ 门槛；
      2) 结构同型（shape + 主体 + 分组维度一致）；
      3) 条件覆盖已经由 find_reusable_examples 卡过（示例能覆盖本轮全部条件）。

    只要一个阈值：不看与次名分差、不调打分模型；最终仍由用户在卡上点确认。
    """
    settings = chat_settings.load()
    if not bool(settings.get("rag_direct_fast_enabled", True)):
        return []
    threshold = float(settings.get("rag_direct_fast_threshold") or 0.85)
    plan_spec = nl2sql._reuse_plan_spec(plan)
    ranked = sorted(hits, key=lambda hit: -float(hit.get("score") or 0))
    if not ranked:
        return []
    top = ranked[0]
    if float(top.get("score") or 0) < threshold:
        return []
    if not nl2sql._same_structure(top.get("spec") or {}, plan_spec):
        return []
    hit = dict(top)
    hit["reasons"] = [
        "相似度 %.2f ≥ %.2f 且结构同型 → 跳过打分模型直接摆卡" % (
            float(top.get("score") or 0), threshold),
        "条件完整覆盖：" + ("、".join(sorted(nl2sql._reuse_condition_keys(hit.get("spec") or {}))) or "无条件"),
    ]
    log_event(
        "direct_match",
        "INFO",
        "fast_path_hit",
        score=round(float(top.get("score") or 0), 3),
        doc_name=str(top.get("doc_name") or ""),
        question=message[:60],
    )
    return [hit]


# 槽名 → 示例 spec 里对应的键（缺槽时看示例自己有没有默认值）
_SLOT_SPEC_KEYS: dict[str, tuple[str, ...]] = {
    "route": ("route", "route_id", "routes", "route_ids"),
    "route_id": ("route_id", "route", "route_ids", "routes"),
    "route_name": ("route", "route_id", "routes", "route_ids"),
    "damage_type": ("damage_type",),
    "problem_type_name": ("damage_type",),
    "time_range": ("time_range", "time_start", "time_end"),
    "time_start": ("time_range", "time_start"),
    "time_end": ("time_range", "time_end"),
    "pile_range": ("pile_range",),
    "device_code": ("device_code",),
}


def _spec_can_fill(spec: dict[str, Any], slot_name: str) -> bool:
    """这条示例自己能不能给该槽一个默认值（能就只提示、不算缺）。"""
    for key in _SLOT_SPEC_KEYS.get(str(slot_name), (str(slot_name),)):
        value = spec.get(key)
        if isinstance(value, (list, tuple)):
            if any(str(item or "").strip() for item in value):
                return True
        elif str(value or "").strip():
            return True
    return False


def _describe(index: int, hit: dict[str, Any]) -> str:
    spec = hit.get("spec") or {}
    shape = str(spec.get("shape") or spec.get("query_type") or "")
    keys = sorted(nl2sql._reuse_condition_keys(spec))
    return (
        f"- id=c{index}｜标准问法：{str(hit.get('question') or '')[:60]}"
        f"｜形态：{shape}｜主体：{spec.get('subject') or ''}"
        f"｜过滤条件：{'、'.join(keys) or '无'}"
        f"｜参数化 SQL：{str(hit.get('normalized_sql') or hit.get('sql') or '')[:200]}"
    )
