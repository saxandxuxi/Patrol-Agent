from __future__ import annotations

from typing import Any

from . import prompts
from ..config import settings
from .llm import LLMError, chat_json, role_model


# 同一句话在一次请求里可能多次问意图，缓存一下省调用。值 = (意图, 是否拿到了 LLM 的判断)
_INTENT_CACHE: dict[str, tuple[str, bool]] = {}
_INTENT_CACHE_LIMIT = settings.cache_max_entries

def fallback_intent(message: str) -> str:
    """LLM 不可用时的保守默认值；不按措辞猜业务意图。"""
    return "data"


def classify_intent(message: str) -> str:
    """意图分类**直接交给 LLM**（docs/项目智能化升级方案.md 升级1）。

    以前是"规则先判、判不出来才找 LLM"，结果"健康状况更好"这类会被规则判成 knowledge。
    现在先问 LLM；LLM 不可用时使用保守默认值。
    """
    return _classify(message)[0]


def wants_images(message: str) -> bool:
    """这句话是不是要**把图拿出来看**（只认语义判定）。

    "要不要看图"以 LLM 的意图判断为准（image = 要看图本体，见 prompts/intent.md 的判别标准：
    用户是想看到图，还是想要那一列的值）。和 `classify_intent` 共用同一份缓存、同一次调用，
    所以不会多花一次模型往返。

    LLM 没给出判断时返回 False，不再按关键词猜。
    """
    intent, answered = _classify(message)
    if answered:
        return intent == "image"
    return False


def _classify(message: str) -> tuple[str, bool]:
    """返回 (意图, 是否拿到了 LLM 的判断)。"""
    key = message.strip()
    cached = _INTENT_CACHE.get(key)
    if cached:
        return cached
    import time as _time

    started = _time.perf_counter()
    quick = fallback_intent(message)
    intent = quick
    answered = False
    try:
        from . import wren_engine

        # 用户用的是自己的叫法（"拍的片子""标了多少个点"），意图模型认不出就容易把
        # 数据查询判成 knowledge（实测"病害工单的处理状态"被判成知识问答，回了"资料中
        # 没有相关内容"）。把表结构语义检索的候选当**证据**给它，判断权还在模型手里。
        candidates = (
            "\n".join(
                f"- {item['name']}（相关度 {item['score']}）：命中证据 "
                + "；".join(item.get("hits") or ["（无）"])
                for item in wren_engine.search_models("mysql", message, limit=3)
            )
            or "（无候选：这句话可能不是在问表里的数据）"
        )
        data = chat_json(
            prompts.messages("intent", message=message, rule_hint=quick, candidates=candidates),
            # 意图分类是"读懂人话"的环节：用语义理解模型（.env 的 SEMANTIC_MODEL）
            model=role_model("semantic"),
        )
        candidate = str(data.get("intent", "")).lower()
        # metadata = 问数据库/表/字段本身的结构（有哪些表、哪张表记录最多、某表有哪些字段），
        # 见 docs/宏观元数据查询_语义理解方案.md：这类问题走 metadata_service，不进业务 plan。
        allowed = {"knowledge", "data", "analysis", "image", "composite", "metadata"}
        if candidate in allowed:
            intent = candidate
            answered = True
    except (LLMError, Exception):
        intent = quick
    if len(_INTENT_CACHE) >= _INTENT_CACHE_LIMIT:
        _INTENT_CACHE.clear()
    _INTENT_CACHE[key] = (intent, answered)
    # 弹卡耗时打点（docs/弹卡链路系统性Bug整改方案.md Bug 4）：先量再优化
    from .logging_service import log_event

    log_event(
        "intent",
        "INFO",
        "classified",
        ms=round((_time.perf_counter() - started) * 1000),
        source="llm" if answered else "rule",
        intent=intent,
    )
    return intent, answered
