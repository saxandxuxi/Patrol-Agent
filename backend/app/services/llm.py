from __future__ import annotations

import json
import time
from typing import Any

import httpx

from ..store import get_default_model
from ..config import settings
from .llm_json import parse_json_lenient
from .llm_rate_limit import (
    model_request_key,
    requests_per_minute,
    shared_rate_limiter,
)
from .logging_service import log_error, log_event


class LLMError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "provider_error",
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retryable = retryable


def _endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return f"{base}/chat/completions"


_ROLE_FALLBACKS: dict[str, tuple[str, ...]] = {
    "planner": ("semantic",),
    "controller": ("semantic",),
    "verify": ("semantic",),
}


def _role_choice(
    settings_payload: dict[str, Any],
    role: str,
    visited: set[str] | None = None,
) -> str:
    seen = set(visited or set())
    if role in seen:
        return ""
    seen.add(role)
    chosen = str(settings_payload.get(f"{role}_model") or "").strip()
    if chosen:
        return chosen
    defaults = settings_payload.get("_defaults")
    if isinstance(defaults, dict):
        role_default = str(defaults.get(f"{role}_model") or "").strip()
        if role_default:
            return role_default
    for fallback in _ROLE_FALLBACKS.get(role, ()):
        inherited = _role_choice(settings_payload, fallback, seen)
        if inherited:
            return inherited
    return ""


def role_model(role: str) -> dict[str, Any] | None:
    """按角色取模型：sql（生成 SQL）/ knowledge（知识问答）。

    API Key 和 Base URL 沿用默认模型，只换模型名。前端「模型设置」里可分别选，
    没选就回落到 .env（NL2SQL_MODEL / QWEN_MODEL）。返回 None 表示用默认模型。
    """
    from .chat_settings import load as load_chat_settings

    chosen = _role_choice(load_chat_settings(), role)
    if not chosen:
        return None
    # 「供应商/模型」写法（如 deepseek/deepseek-chat）：key 和 base_url 都跟着换
    if "/" in chosen:
        provider, model_name = chosen.split("/", 1)
        found = provider_model(provider, model_name)
        if found:
            return found
    base = get_default_model()
    if not base:
        return None
    if base.get("model") == chosen:
        return None
    return {**base, "model": chosen}


def provider_model(provider: str, model: str) -> dict[str, Any] | None:
    """按「供应商 + 模型名」取一套（api_key/base_url/model 一起换）。"""
    config = settings.provider_configs().get(str(provider or "").strip().lower())
    if not config:
        return None
    name = str(model or "").strip()
    if not name:
        names = list(config.get("models") or [])
        if not names:
            return None
        name = str(names[0])
    return {
        "api_key": str(config.get("api_key") or ""),
        "base_url": str(config.get("base_url") or ""),
        "model": name,
        "provider": str(provider).strip().lower(),
    }


def provider_choices() -> list[str]:
    """前端下拉用：「供应商/模型」全清单（未填 key 的供应商不列）。"""
    out: list[str] = []
    for provider, config in settings.provider_configs().items():
        for name in config.get("models") or []:
            out.append(f"{provider}/{name}")
    return out


def sql_model() -> dict[str, Any] | None:
    """生成 SQL 用的模型（值来自 .env 的 NL2SQL_MODEL）。

    实测 coder 族写 SQL 比通用族稳，所以和语义/校验分开配；注意**模型名要选端点上在架的**
    ——qwen3-coder-plus / qwen3-max 已下架（404），在架的是 qwen3-coder-flash / qwen3.7-max /
    qwen3.7-plus / qwen3.7-flash / qwen-plus / qwen-flash / deepseek-v4.1-flash。
    """
    return role_model("sql")


def chat(
    messages: list[dict[str, str]],
    model: dict[str, Any] | None = None,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    enable_search: bool = False,
    max_tokens: int | None = None,
) -> str:
    content, _ = chat_detail(
        messages,
        model=model,
        temperature=temperature,
        enable_thinking=enable_thinking,
        enable_search=enable_search,
        max_tokens=max_tokens,
    )
    return content


def chat_detail(
    messages: list[dict[str, str]],
    model: dict[str, Any] | None = None,
    temperature: float = 0.1,
    enable_thinking: bool = False,
    enable_search: bool = False,
    max_tokens: int | None = None,
) -> tuple[str, str]:
    """返回 (回复内容, 思考过程)。开启思考模式时第二个值才有意义。"""
    model = model or get_default_model()
    if not model:
        raise LLMError("没有可用的 LLM 模型配置，请先在模型设置中填写 API Key。")
    # 供应商小差异：Kimi K2 只接受 temperature=1（实测 400："only 1 is allowed for this model"）；
    # 思考型模型（glm-4.6 / kimi-k2.x）token 预算太小时会把额度全用在思考上、正文回空，
    # 所以给它们一个下限，避免"连通但回空"被误判成模型不行。
    provider = str(model.get("provider") or "")
    model_name = str(model.get("model") or "")
    # 带 "供应商/模型" 前缀的名字**不能原样发给端点**：DashScope 会回
    # 404 `model does not exist`（实测 dashscope/deepseek-v4.1-flash → 404，
    # 用户看到的就是这句）。能解析就换成该供应商的整套配置，解析不了就只发模型名。
    if "/" in model_name:
        prefix, bare = model_name.split("/", 1)
        found = provider_model(prefix, bare)
        if found:
            model = found
            provider = str(model.get("provider") or prefix)
        else:
            log_error("llm", "provider_prefix_stripped", RuntimeError(model_name))
            model = {**model, "model": bare}
        model_name = str(model.get("model") or bare)
    model_name_lower = model_name.lower()
    if provider == "moonshot" or "kimi-k2" in model_name_lower:
        temperature = 1
    if (
        "glm-4.6" in model_name_lower
        and (max_tokens or 0)
        and max_tokens < 256
    ):
        max_tokens = 256
    max_tokens = max_tokens or settings.llm_max_tokens
    # Kimi K2 的 reasoning_content 与最终正文共用输出预算。实测 1024～4096
    # 时常出现“思考了 2000～10000 字、content 为空”，JSON 裁判因此失败。
    if "kimi-k2" in model_name_lower:
        max_tokens = max(max_tokens, 8192)
    # DeepSeek reasoner 的长 reasoning 和最终正文共用响应预算。复杂 QuerySpec
    # 在 1024 tokens 下会只返回 reasoning_content、content 为空，表现为
    # "模型没有返回合法 JSON"。给最终正文留出可完成 JSON 的固定下限。
    if "reasoner" in model_name_lower:
        max_tokens = max(max_tokens, 4096)
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
        "stream": False,
        "max_tokens": max_tokens,
    }
    # qwen3.x 这类模型默认会思考，SQL 生成/摘要等调用必须显式关掉，否则慢好几倍
    payload["enable_thinking"] = bool(enable_thinking)
    # 智谱 GLM 不认 DashScope 的 enable_thinking（它自己默认开思考）：实测 glm-4.6
    # 关掉后 11.2s/正文 0 字 → 4.2s/正文 106 字。只在不需要思考时下发，别动"要思考"的场景。
    if provider == "zhipu" and not enable_thinking:
        payload["thinking"] = {"type": "disabled"}
    if enable_search:
        payload["enable_search"] = True
    headers = {
        "Authorization": f"Bearer {model['api_key']}",
        "Content-Type": "application/json",
    }
    started = time.perf_counter()
    try:
        try:
            data = _post(model, payload, headers)
        except Exception as exc:
            # 个别模型/网关不认这个参数时，去掉再试一次
            if "enable_thinking" in str(exc) and "enable_thinking" in payload:
                payload.pop("enable_thinking", None)
                data = _post(model, payload, headers)
            else:
                raise
        message = data["choices"][0]["message"]
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        duration_ms = round((time.perf_counter() - started) * 1000)
        log_event(
            "llm",
            "INFO",
            "chat_success",
            model=model["model"],
            duration_ms=duration_ms,
            messages=len(messages),
            thinking=enable_thinking,
            search=enable_search,
            content_chars=len(content),
            reasoning_chars=len(reasoning),
        )
        return content, reasoning
    except LLMError as exc:
        if exc.category == "model_not_found":
            raise LLMError(
                f"LLM 调用失败（404 模型不存在）：当前模型 `{model_name}` 在你配置的供应商端点上不存在。"
                "检查 .env 里该供应商的 BASE_URL/API_KEY，或在前端「模型设置」里换一个该供应商支持的模型名。"
                f"原始错误：{str(exc)[:150]}",
                category=exc.category,
                status_code=exc.status_code,
                retryable=False,
            ) from exc
        raise
    except Exception as exc:
        # 404 单独说清楚：它是"模型名不存在/供应商没配好"，不是"没钱"（那是 401/403/429）。
        # 用户实测看到裸的 404 完全不知道怎么办，这里给可操作的提示。
        text = str(exc)
        if "404" in text or "model_not_found" in text:
            raise LLMError(
                f"LLM 调用失败（404 模型不存在）：当前模型 `{model_name}` 在你配置的供应商端点上不存在。"
                "检查 .env 里该供应商的 BASE_URL/API_KEY，或在前端「模型设置」里换一个该供应商支持的模型名。"
                f"原始错误：{text[:150]}",
                category="model_not_found",
            ) from exc
        raise LLMError(f"LLM 调用失败：{exc}") from exc


def _post(model: dict[str, Any], payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    retryable_statuses = {408, 429, 500, 502, 503, 504}
    max_attempts = 3
    rate_limit_key = model_request_key(model)
    rpm = requests_per_minute(model)
    with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
        for attempt in range(max_attempts):
            shared_rate_limiter.acquire(rate_limit_key, rpm)
            try:
                resp = client.post(
                    _endpoint(model["base_url"]),
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                if attempt + 1 >= max_attempts:
                    raise LLMError(
                        f"HTTP network error after {max_attempts} attempts: {exc}",
                        category="transport",
                        retryable=True,
                    ) from exc
                delay = min(8, 2**attempt)
                log_event(
                    "llm",
                    "WARNING",
                    "post_retry",
                    reason=type(exc).__name__,
                    attempt=attempt + 1,
                    delay_seconds=delay,
                )
                time.sleep(delay)
                continue
            if not resp.is_error:
                return resp.json()
            detail = resp.text.strip()
            error_code = ""
            error_message = ""
            try:
                body = resp.json()
                error = body.get("error") if isinstance(body, dict) else None
                if isinstance(error, dict):
                    error_code = str(error.get("code") or "").strip()
                    error_message = str(error.get("message") or "").strip()
                    detail = ": ".join(
                        item for item in (error_code, error_message) if item
                    )
            except ValueError:
                pass
            quota_text = f"{error_code} {error_message or detail}".lower()
            quota_exhausted = any(
                marker in quota_text
                for marker in (
                    "insufficient balance",
                    "insufficient_quota",
                    "quota exhausted",
                    "account overdue",
                    "account is suspended",
                    "arrearage",
                )
            )
            if resp.status_code == 429 and quota_exhausted:
                raise LLMError(
                    f"HTTP 429: {detail[:500]}",
                    category="quota_exhausted",
                    status_code=429,
                    retryable=False,
                )
            if (
                resp.status_code in retryable_statuses
                and attempt + 1 < max_attempts
            ):
                retry_after = str(resp.headers.get("Retry-After") or "").strip()
                try:
                    delay = float(retry_after) if retry_after else 2**attempt
                except ValueError:
                    delay = 2**attempt
                if resp.status_code == 429 and not retry_after:
                    delay = 60.0
                delay = max(0.0, min(delay, 300.0))
                shared_rate_limiter.defer(rate_limit_key, delay)
                log_event(
                    "llm",
                    "WARNING",
                    "post_retry",
                    reason=f"HTTP {resp.status_code}",
                    attempt=attempt + 1,
                    delay_seconds=delay,
                )
                if rpm <= 0:
                    time.sleep(delay)
                continue
            category = (
                "model_not_found"
                if resp.status_code == 404
                else "rate_limited"
                if resp.status_code == 429
                else "provider_unavailable"
                if resp.status_code in retryable_statuses
                else "provider_error"
            )
            raise LLMError(
                f"HTTP {resp.status_code}: {detail[:500]}",
                category=category,
                status_code=resp.status_code,
                retryable=resp.status_code in retryable_statuses,
            )
    raise LLMError(
        "LLM request failed without a response",
        category="transport",
        retryable=True,
    )


def chat_json(
    messages: list[dict[str, str]],
    model: dict[str, Any] | None = None,
    allow_list: bool = False,
    max_tokens: int | None = None,
    temperature: float = 0,
) -> dict[str, Any] | list[Any]:
    text = chat(
        messages,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    ).strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = parse_json_lenient(text, allow_list=allow_list)
    if data is not None:
        return data
    # 模型偶尔会吐出坏 JSON（字符串里有裸引号/换行/中文引号），实测这一条就把
    # "同义改写能不能召回示例"整个判没了：direct_match 抛 JSONDecodeError → 返回空候选。
    # 再问一次，并明确要求只输出 JSON。
    retry = chat(
        list(messages)
        + [
            {
                "role": "system",
                "content": (
                    f"你上一次的输出不是合法 JSON。请只输出{('一个 JSON 对象或 JSON 数组' if allow_list else '一个 JSON 对象')}，"
                    "字符串内部不要出现未转义的双引号或换行。"
                ),
            }
        ],
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
    ).strip()
    retry = retry.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = parse_json_lenient(retry, allow_list=allow_list)
    if data is not None:
        return data
    raise LLMError(f"模型没有返回合法 JSON：{text[:200]}")

def test_connection(model: dict[str, Any]) -> dict[str, str]:
    reply = chat(
        [{"role": "user", "content": "请只回复：连接成功"}],
        model=model,
        temperature=0,
    )
    return {"ok": True, "message": reply[:200]}
