from __future__ import annotations

from typing import Any

from ..config import settings
from ..store import get_memory, set_memory


_KEY = "app_setting:chat"


def _provider_choices() -> list[str]:
    """各供应商的「供应商/模型」清单（只含**填了 key** 的那些），给前端下拉用。"""
    try:
        from . import llm

        return llm.provider_choices()
    except Exception:  # noqa: BLE001
        return []


def _reuse_mode(value: Any) -> str:
    """复用策略只有两种：ask（命中先弹卡让用户确认）/ off（不推荐）。

    历史上存过的 `auto`（命中自动直出）一律按 ask 处理——"这次用哪条口径"的决定权
    必须在用户（docs/RAG示例直达与AI直答旁路方案.md §〇 最高原则）。
    """
    text = str(value or "").strip().lower()
    if text == "off":
        return "off"
    return "ask"


def load() -> dict[str, Any]:
    """问答开关 + 两个角色的模型选择，优先用前端保存的值，否则回落到 .env 默认。"""
    stored = get_memory(_KEY)
    stored = stored if isinstance(stored, dict) else {}
    return {
        "enable_thinking": bool(
            stored.get("enable_thinking", settings.knowledge_enable_thinking)
        ),
        "enable_search": bool(stored.get("enable_search", settings.knowledge_enable_search)),
        # 是否允许"追问自动沿用上一条的条件"——这个判断交给用户，不再由系统猜。
        "inherit_context": bool(stored.get("inherit_context", True)),
        # 回答发出前的反思自检 / 回答末尾的后续建议（docs/项目智能化升级整体方案.md 升级3、升级4）
        "confirm_before_query": bool(
            stored.get("confirm_before_query", settings.confirm_before_query)
        ),
        "single_turn_compiler_enabled": bool(
            stored.get("single_turn_compiler_enabled", False)
        ),
        "reflect_check": bool(
            stored.get("reflect_check", settings.reflect_check_enabled)
        ),
        "followup_suggestions": bool(
            stored.get("followup_suggestions", settings.followup_suggestions_enabled)
        ),
        # 查询引擎：hybrid=确定性模板优先（默认）；wren=跳过模板，强制走 Wren 语义层（用于实测 Wren 覆盖率）
        "engine_mode": str(stored.get("engine_mode") or settings.nl2sql_engine or "wren"),
        # 能力复用策略（docs/RAG示例直达与AI直答旁路方案.md §6）：
        #   ask=命中模板/示例先弹确认卡（**默认**，对话流强制）；off=不推荐，全走 L3。
        #   历史上的 auto（命中就直接出数）已废弃——一律按 ask 处理，决定权只在用户。
        "reuse_mode": _reuse_mode(stored.get("reuse_mode")),
        # L1：自动沉淀模板要不要纳入候选推荐（0 次大模型；不改变"必须确认"）
        "auto_template_enabled": bool(stored.get("auto_template_enabled", True)),
        # 自动挖掘（离线把成功 SQL 挖成模板）。**默认关**：docs/确认卡重复气泡耗时整改与
        # 污染模板库重置.md §三 要求——现有自动模板已被污染（name/id 槽错配、选项脏值、
        # 计数口径漏算），挖掘器修好、人工核对过再打开。关掉只影响"挖新的"，
        # 不影响已存在的模板推荐，也不影响预置模板与看板示例。
        "auto_template_mine_enabled": bool(stored.get("auto_template_mine_enabled", False)),
        # L2：RAG 示例直达（召回 + 便宜模型严格同问打分）
        "rag_direct_enabled": bool(stored.get("rag_direct_enabled", True)),
        "rag_direct_threshold": float(
            stored.get("rag_direct_threshold", settings.rag_direct_threshold)
        ),
        # 同问**打分模型**开关：默认关——打分实测要 ~20s，而且候选最后是用户在卡上拍板的，
        # 本地三道门槛（相似度阈值 + 结构同型 + 与次名分差）已经够用
        # （docs/auto模板误匹配与看板同义召回被关方案.md §三补）。要做"严格同问判定"时再打开。
        "rag_direct_score_enabled": bool(
            stored.get("rag_direct_score_enabled", settings.rag_direct_score_enabled)
        ),
        "rag_direct_margin": float(stored.get("rag_direct_margin", settings.rag_direct_margin)),
        "rag_direct_topk": int(stored.get("rag_direct_topk", settings.rag_direct_topk)),
        # 高置信直摆：相似度 ≥ 门槛 + 结构同型 → 跳过同问打分模型（省 3~10 秒）
        "rag_direct_fast_enabled": bool(
            stored.get("rag_direct_fast_enabled", settings.rag_direct_fast_enabled)
        ),
        # 默认门槛 0.78：实测"请列出每种病害类型对应的病害代码。"这种只差前缀的看板题
        # 向量+骨架分是 0.79（原门槛 0.85 够不着 → 每题白等 ~20s 的打分模型）；
        # 而模糊的几条（0.87/0.83/0.82…）分差只有 0.02~0.04，会被 _fast_accept 的分差规则挡住，
        # 所以降门槛只影响"清晰胜出"的那类（docs/auto模板误匹配与看板同义召回被关方案.md §三补）。
        "rag_direct_fast_threshold": float(
            stored.get("rag_direct_fast_threshold", settings.rag_direct_fast_threshold)
        ),
        # 与次名的分差下限：两条候选太接近就不自动挑一条，交打分模型/用户（都在 .env 可调）
        "rag_direct_fast_margin": float(
            stored.get("rag_direct_fast_margin", settings.rag_direct_fast_margin)
        ),
        # 空字符串表示「跟随默认模型」；区分「没存过」和「存成空」，
        # 否则前端清空选择后又会回落到 .env 里的值，清不掉。
        # defaults：每个角色"跟随默认"时**实际用的是哪个模型**（值来自 .env）。
        # 前端拿它显示「默认（.env：qwen3.7-plus）」，用户一眼能看出默认是什么。
        "_defaults": {
            "default_model": str(settings.qwen_model or ""),
            "sql_model": str(settings.nl2sql_model or settings.qwen_model or ""),
            "knowledge_model": str(settings.qwen_model or ""),
            "verify_model": str(settings.verify_model or settings.qwen_model or ""),
            "semantic_model": str(settings.semantic_model or settings.qwen_model or ""),
            "planner_model": str(
                settings.planner_model or settings.semantic_model or settings.qwen_model or ""
            ),
            "controller_model": str(
                settings.controller_model or settings.semantic_model or settings.qwen_model or ""
            ),
        },
        "sql_model": (
            str(stored["sql_model"]) if "sql_model" in stored else str(settings.nl2sql_model or "")
        ),
        "knowledge_model": (
            str(stored["knowledge_model"])
            if "knowledge_model" in stored
            else str(settings.qwen_model or "")
        ),
        # 校验/纠错/优化的裁判模型：前端没选就回落到 .env 的 VERIFY_MODEL
        "verify_model": (
            str(stored["verify_model"])
            if "verify_model" in stored
            else str(settings.verify_model or "")
        ),
        # 语义理解模型：前端没选就回落到 .env 的 SEMANTIC_MODEL
        "semantic_model": (
            str(stored["semantic_model"])
            if "semantic_model" in stored
            else str(settings.semantic_model or "")
        ),
        "planner_model": (
            str(stored["planner_model"])
            if "planner_model" in stored
            else str(settings.planner_model or "")
        ),
        "controller_model": (
            str(stored["controller_model"])
            if "controller_model" in stored
            else str(settings.controller_model or "")
        ),
        # 下拉候选：内置清单 + **各供应商的「供应商/模型」**（只列填了 key 的供应商）。
        # 角色模型存的就是这个字符串，llm.role_model 认「供应商/模型」写法（key/base_url 一起换）。
        "model_choices": sorted(
            set(list(settings.model_choices) + _provider_choices())
        ),
    }


def save(payload: dict[str, Any]) -> dict[str, Any]:
    current = load()
    for key in (
        "enable_thinking",
        "enable_search",
        "inherit_context",
        "reflect_check",
        "followup_suggestions",
        "confirm_before_query",
        "single_turn_compiler_enabled",
        "auto_template_enabled",
        "auto_template_mine_enabled",
        "rag_direct_enabled",
        "rag_direct_score_enabled",
        "rag_direct_fast_enabled",
    ):
        if payload.get(key) is not None:
            current[key] = bool(payload[key])
    # engine_mode 是字符串（hybrid/wren），不能走 bool 分支
    if payload.get("engine_mode") is not None:
        current["engine_mode"] = str(payload["engine_mode"]).strip() or "hybrid"
    if payload.get("reuse_mode") is not None:
        current["reuse_mode"] = _reuse_mode(payload["reuse_mode"])
    for key, cast in (
        ("rag_direct_threshold", float),
        ("rag_direct_margin", float),
        ("rag_direct_topk", int),
        ("rag_direct_fast_threshold", float),
        ("rag_direct_fast_margin", float),
    ):
        if payload.get(key) is not None:
            try:
                current[key] = cast(payload[key])
            except (TypeError, ValueError):
                continue
    for key in (
        "sql_model",
        "knowledge_model",
        "verify_model",
        "semantic_model",
        "planner_model",
        "controller_model",
    ):
        if payload.get(key) is not None:
            current[key] = str(payload[key]).strip()
    set_memory(_KEY, {key: value for key, value in current.items() if key != "model_choices"})
    return current
