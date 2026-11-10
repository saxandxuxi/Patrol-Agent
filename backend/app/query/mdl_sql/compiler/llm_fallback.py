from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ....query.capability_registry import CapabilityRegistry
from ....services import llm, prompts
from ....services.llm import LLMError, role_model
from ....services.llm_json import parse_json_lenient
from ..audit import parse_mdl_sql
from ..context.prompt_context import serialize_prompt_context
from ..errors import MdlSqlFallbackError
from ..models import (
    CompilerFailure,
    MdlSqlCandidate,
    PromptContextBundle,
)


_OUTPUT_KEYS = {
    "mdl_sql",
    "used_models",
    "used_relationship_ids",
}


def generate_mdl_sql_once(
    context: PromptContextBundle,
    failure: CompilerFailure,
    *,
    model: dict[str, Any] | None = None,
) -> MdlSqlCandidate:
    context_json = serialize_prompt_context(
        context,
        max_context_bytes=(
            CapabilityRegistry.load()
            .snapshot.mdl_compilation.max_context_bytes
        ),
    )
    messages = prompts.messages(
        "mdl_sql_generator",
        context_json=context_json,
        failure_json=json.dumps(
            asdict(failure),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    try:
        reply = llm.chat(
            messages,
            model=model or role_model("sql"),
            max_tokens=4096,
        )
    except LLMError as exc:
        raise MdlSqlFallbackError(
            _infrastructure_code(exc),
            {
                "provider_category": str(exc.category or ""),
                "http_status": exc.status_code,
                "contract_id": context.contract.contract_id,
                "contract_hash": context.contract.contract_hash,
                "reason": str(exc)[:500],
            },
        ) from exc

    payload = _strict_payload(reply)
    mdl_sql = payload["mdl_sql"]
    parsed = parse_mdl_sql(mdl_sql, "mysql")
    if not parsed.valid:
        raise _invalid_sql(
            context,
            "invalid_read_only_sql",
            reply=reply,
            parsed=parsed,
        )

    return MdlSqlCandidate(
        source="llm_fallback",
        mdl_sql=mdl_sql,
        contract_id=context.contract.contract_id,
        contract_hash=context.contract.contract_hash,
        used_models=tuple(payload["used_models"]),
        used_relationship_ids=tuple(
            payload["used_relationship_ids"]
        ),
    )


def _strict_payload(reply: str) -> dict[str, Any]:
    text = str(reply or "").strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise _invalid_reply("not_a_single_json_object", text)

    parsed = parse_json_lenient(text)
    if not isinstance(parsed, dict):
        raise _invalid_reply("not_a_json_object", text)
    try:
        strict = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        raise _invalid_reply("json_has_non_json_wrapper", text) from None
    if strict != parsed:
        raise _invalid_reply("json_contains_multiple_values", text)
    if set(strict) != _OUTPUT_KEYS:
        raise _invalid_reply("output_keys_mismatch", text)

    mdl_sql = strict.get("mdl_sql")
    if not isinstance(mdl_sql, str) or not mdl_sql.strip():
        raise _invalid_reply("mdl_sql_must_be_non_empty_string", text)
    for key in ("used_models", "used_relationship_ids"):
        value = strict.get(key)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in value
        ):
            raise _invalid_reply(f"{key}_must_be_string_list", text)
    return strict


def _invalid_reply(reason: str, reply: str) -> MdlSqlFallbackError:
    return MdlSqlFallbackError(
        "llm_sql_invalid",
        {
            "reason": reason,
            "reply_preview": reply[:500],
        },
    )


def _invalid_sql(
    context: PromptContextBundle,
    reason: str,
    *,
    reply: str,
    parsed: Any,
) -> MdlSqlFallbackError:
    return MdlSqlFallbackError(
        "llm_sql_invalid",
        {
            "reason": reason,
            "contract_id": context.contract.contract_id,
            "contract_hash": context.contract.contract_hash,
            "issues": [
                {
                    "code": issue.code,
                    "message": issue.message,
                    "path": issue.path,
                    "evidence": dict(issue.evidence),
                }
                for issue in parsed.issues
            ],
            "reply_preview": reply[:500],
        },
    )


def _infrastructure_code(exc: LLMError) -> str:
    category = str(exc.category or "")
    status = exc.status_code
    if category == "quota_exhausted" or status == 402:
        return "llm_quota_exhausted"
    if category == "rate_limited" or status == 429:
        return "llm_rate_limited"
    if category == "transport":
        return "llm_transport_error"
    return "llm_provider_unavailable"
