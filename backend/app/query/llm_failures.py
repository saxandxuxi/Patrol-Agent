from __future__ import annotations


def describe_llm_failure(exc: BaseException) -> tuple[str, str]:
    category = str(getattr(exc, "category", "") or "invalid_output")
    messages = {
        "quota_exhausted": (
            "llm_quota_exhausted",
            "模型账户额度不足或已停用，请切换到有可用额度的模型，或充值后重试。",
        ),
        "rate_limited": (
            "llm_rate_limited",
            "模型请求超过供应商限流，系统已进入共享冷却；请稍后重试。",
        ),
        "transport": (
            "llm_transport_error",
            "连接模型供应商暂时失败，请稍后重试。",
        ),
        "provider_unavailable": (
            "llm_provider_unavailable",
            "模型供应商暂时不可用，请稍后重试或切换模型。",
        ),
        "model_not_found": (
            "llm_provider_unavailable",
            "当前模型在供应商端点上不存在，请检查模型设置。",
        ),
    }
    return messages.get(
        category,
        (
            "llm_output_invalid",
            "模型没有返回可解析的结构化查询计划，请重试或切换模型。",
        ),
    )
