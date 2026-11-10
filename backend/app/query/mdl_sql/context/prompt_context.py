from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass, replace
from types import MappingProxyType
from typing import Any

from ...capability_registry import CapabilityRegistry
from ..errors import MdlSqlContextError
from ..models import (
    MdlContext,
    MdlQueryContract,
    MdlSnapshot,
    PromptContextBundle,
)
from .capabilities import build_capability_context
from .mdl import build_mdl_context


def build_prompt_context(
    contract: MdlQueryContract,
    registry: CapabilityRegistry,
    snapshot: MdlSnapshot,
) -> PromptContextBundle:
    policy = registry.snapshot.mdl_compilation
    if len(contract.allowed_models) > policy.max_models_per_contract:
        raise MdlSqlContextError(
            "context_budget_exceeded",
            {
                "reason": "max_models_per_contract_exceeded",
                "contract_id": contract.contract_id,
                "contract_hash": contract.contract_hash,
                "allowed_model_count": len(contract.allowed_models),
                "max_models_per_contract": policy.max_models_per_contract,
            },
        )
    return PromptContextBundle(
        schema_version=1,
        contract=contract,
        business_capability=build_capability_context(
            contract,
            registry.snapshot,
        ),
        mdl_subset=build_mdl_context(contract, snapshot),
    )


def serialize_prompt_context(
    context: PromptContextBundle,
    *,
    max_context_bytes: int,
) -> str:
    if (
        isinstance(max_context_bytes, bool)
        or not isinstance(max_context_bytes, int)
        or max_context_bytes <= 0
    ):
        raise MdlSqlContextError(
            "context_budget_exceeded",
            {
                "reason": "invalid_context_budget",
                "max_context_bytes": max_context_bytes,
            },
        )

    serialized = _serialize(context)
    if len(serialized.encode("utf-8")) <= max_context_bytes:
        return serialized

    reduced = _without_optional_descriptions(context)
    serialized = _serialize(reduced)
    required_bytes = len(serialized.encode("utf-8"))
    if required_bytes <= max_context_bytes:
        return serialized
    raise MdlSqlContextError(
        "context_budget_exceeded",
        {
            "reason": "required_context_exceeds_budget",
            "contract_id": context.contract.contract_id,
            "contract_hash": context.contract.contract_hash,
            "max_context_bytes": max_context_bytes,
            "required_bytes": required_bytes,
        },
    )


def _without_optional_descriptions(
    context: PromptContextBundle,
) -> PromptContextBundle:
    models = {
        model_name: {
            **model,
            "description": "",
            "columns": {
                column_name: {
                    **column,
                    "description": "",
                }
                for column_name, column in model["columns"].items()
            },
        }
        for model_name, model in context.mdl_subset.models.items()
    }
    return replace(
        context,
        mdl_subset=replace(
            context.mdl_subset,
            models=MappingProxyType(models),
        ),
    )


def _serialize(context: PromptContextBundle) -> str:
    return json.dumps(
        _jsonable(context.to_dict()),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=str)]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    return value
