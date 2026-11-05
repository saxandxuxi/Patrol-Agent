from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..services import preset_lib
from ..services.resource_loader import load_yaml
from .contracts import QuerySpec, ValueBinding
from .fields import PhysicalSchemaView
from .templates import TemplateContract


@dataclass(frozen=True)
class ContractConversion:
    ok: bool
    contract: TemplateContract | None
    code: str = ""


_FILTER_FIELDS = {
    "damage_type": "problem_type_name",
}


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _fixed_condition(field_name: str, value: Any) -> ValueBinding:
    display = str(value or "").strip()
    return ValueBinding(
        field=_FILTER_FIELDS.get(field_name, field_name),
        op="=",
        storage_value=value,
        display_value=display,
        source="template_fixed",
        resolver="template_fixed",
        raw_text=display,
    )


def _contracts_from_declaration(
    raw: dict[str, Any],
    *,
    adapter_id: str,
) -> tuple[TemplateContract, ...]:
    match = raw.get("match") or {}
    if not isinstance(match, dict):
        return ()
    shapes = _as_tuple(match.get("shapes") or match.get("shape"))
    subjects = _as_tuple(match.get("subjects") or match.get("subject"))
    if not shapes:
        shapes = ("data",)
    if not subjects:
        subjects = ("disease",)
    metrics = _as_tuple(match.get("metrics"))
    dimensions = _as_tuple(match.get("group_by"))
    filters = set(_as_tuple(match.get("slots")))
    filters.update(_FILTER_FIELDS.get(item, item) for item in (raw.get("params") or []))
    fixed = tuple(
        _fixed_condition(str(field_name), value)
        for field_name, value in (match.get("fixed") or {}).items()
        if str(field_name).strip()
    )
    fixed_fields = {item.field for item in fixed}
    filters = tuple(sorted((filters | fixed_fields) - {""}))
    native_id = str(raw.get("id") or "").strip()
    if not native_id:
        return ()
    contracts: list[TemplateContract] = []
    for shape in shapes:
        for subject in subjects:
            contracts.append(
                TemplateContract(
                    id=native_id,
                    version=int(raw.get("version") or 1),
                    source=str(raw.get("source") or adapter_id),
                    status=str(raw.get("status") or "active"),
                    kind=str(raw.get("kind") or "mysql"),
                    adapter_id=adapter_id,
                    native_id=native_id,
                    subject=subject,
                    view=shape,
                    metrics=metrics,
                    dimensions=dimensions,
                    filters=filters,
                    fixed_conditions=fixed,
                    output_projection=_as_tuple(match.get("output_projection")),
                    required_assertions=("preserve_user_scope",),
                    title=str(raw.get("title") or native_id),
                )
            )
    return tuple(contracts)


def contract_from_builtin(raw: dict[str, Any]) -> TemplateContract:
    contracts = _contracts_from_declaration(raw, adapter_id="builtin")
    if not contracts:
        raise ValueError("incomplete_template_contract")
    return contracts[0]


def contract_from_preset(raw: dict[str, Any]) -> TemplateContract:
    contracts = _contracts_from_declaration(raw, adapter_id="preset")
    if not contracts:
        raise ValueError("incomplete_template_contract")
    return contracts[0]


class BuiltinTemplateProvider:
    def declared_contracts(
        self,
        spec: QuerySpec,
        schema: PhysicalSchemaView,
    ) -> tuple[TemplateContract, ...]:
        registry = load_yaml("sql_templates/_index.yaml", {}) or {}
        contracts: list[TemplateContract] = []
        for raw in registry.get("templates") or []:
            if not isinstance(raw, dict) or raw.get("source") != "builtin":
                continue
            contracts.extend(_contracts_from_declaration(raw, adapter_id="builtin"))
        return tuple(contracts)


class PresetTemplateProvider:
    def declared_contracts(
        self,
        spec: QuerySpec,
        schema: PhysicalSchemaView,
    ) -> tuple[TemplateContract, ...]:
        contracts: list[TemplateContract] = []
        for raw in preset_lib.load().get("queries") or []:
            if not isinstance(raw, dict):
                continue
            contracts.extend(_contracts_from_declaration(raw, adapter_id="preset"))
        return tuple(contracts)
