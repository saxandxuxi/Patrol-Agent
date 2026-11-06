from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..services import preset_lib
from ..services import templatizer
from ..services.logging_service import log_event
from ..services.resource_loader import load_capability_file
from .contracts import QuerySpec, ValueBinding
from .fields import PhysicalSchemaView
from .preset_bindings import filter_semantic_name
from .templates import TemplateContract


@dataclass(frozen=True)
class ContractConversion:
    ok: bool
    contract: TemplateContract | None
    code: str = ""


def _as_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _fixed_condition(field_name: str, value: Any) -> ValueBinding:
    display = str(value or "").strip()
    return ValueBinding(
        field=field_name,
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
    filters.update(str(item) for item in (raw.get("params") or []))
    required = _as_tuple(match.get("requires"))
    if not required and adapter_id == "preset":
        parameter_specs = preset_lib.load().get("params") or {}
        required = tuple(
            str(item)
            for item in (raw.get("params") or [])
            if not bool((parameter_specs.get(str(item)) or {}).get("allow_all"))
        )
    required_filters = tuple(
        sorted(
            {
                filter_semantic_name(item)
                for item in required
                if str(item).strip()
            }
        )
    )
    fixed = tuple(
        _fixed_condition(str(field_name), value)
        for field_name, value in (match.get("fixed") or {}).items()
        if str(field_name).strip()
    )
    fixed_fields = {item.field for item in fixed}
    filters = tuple(
        sorted(
            {
                filter_semantic_name(item)
                for item in (filters | fixed_fields)
                if str(item).strip()
            }
        )
    )
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
                    required_filters=required_filters,
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
        registry = load_capability_file("templates.yaml", {}) or {}
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


def contract_from_auto(raw: dict[str, Any]) -> ContractConversion:
    match = raw.get("match")
    params = raw.get("params")
    if (
        not str(raw.get("id") or "").strip()
        or not isinstance(match, dict)
        or not isinstance(params, dict)
        or not _as_tuple(match.get("shapes") or match.get("shape"))
        or not _as_tuple(match.get("subjects") or match.get("subject"))
        or "metrics" not in match
        or "group_by" not in match
        or "conditions" not in match
        or "fixed" not in match
        or "output_fields" not in match
    ):
        return ContractConversion(
            ok=False,
            contract=None,
            code="incomplete_template_contract",
        )
    normalized = {
        **raw,
        "source": "auto",
        "status": (
            "active"
            if raw.get("enabled", True) and str(raw.get("status") or "mature") == "mature"
            else "inactive"
        ),
        "match": {
            **match,
            "slots": match.get("conditions") or [],
            "output_projection": match.get("output_fields") or [],
        },
    }
    contracts = _contracts_from_declaration(normalized, adapter_id="auto")
    if not contracts:
        return ContractConversion(
            ok=False,
            contract=None,
            code="incomplete_template_contract",
        )
    return ContractConversion(ok=True, contract=contracts[0])


def contract_from_example(raw: dict[str, Any]) -> ContractConversion:
    spec = raw.get("spec")
    doc_name = str(raw.get("doc_name") or raw.get("id") or "").strip()
    if (
        not doc_name
        or not isinstance(spec, dict)
        or "metrics" not in spec
        or "conditions" not in spec
        or "output_fields" not in spec
    ):
        return ContractConversion(
            ok=False,
            contract=None,
            code="incomplete_template_contract",
        )
    conditions = spec.get("conditions") or {}
    if not isinstance(conditions, dict):
        return ContractConversion(
            ok=False,
            contract=None,
            code="incomplete_template_contract",
        )
    fixed: list[ValueBinding] = []
    for field_name, values in conditions.items():
        if isinstance(values, list) and len(values) == 1:
            fixed.append(_fixed_condition(str(field_name), values[0]))
    fixed_fields = {item.field for item in fixed}
    filters = tuple(
        sorted(
            {
                filter_semantic_name(str(item))
                for item in (set(conditions) | fixed_fields)
            }
        )
    )
    contract = TemplateContract(
        id=doc_name,
        version=int(raw.get("version") or 1),
        source="verified_example",
        status="active",
        kind=str(raw.get("kind") or spec.get("kind") or "mysql"),
        adapter_id="verified_example",
        native_id=doc_name,
        subject=str(spec.get("subject") or "disease"),
        view=str(spec.get("view") or spec.get("shape") or "data"),
        metrics=_as_tuple(spec.get("metrics")),
        dimensions=_as_tuple(spec.get("group_by")),
        filters=filters,
        fixed_conditions=tuple(fixed),
        output_projection=_as_tuple(spec.get("output_fields")),
        required_assertions=("preserve_user_scope",),
        title=str(raw.get("title") or doc_name),
    )
    return ContractConversion(ok=True, contract=contract)


class AutoTemplateProvider:
    def declared_contracts(
        self,
        spec: QuerySpec,
        schema: PhysicalSchemaView,
    ) -> tuple[TemplateContract, ...]:
        contracts: list[TemplateContract] = []
        for raw in templatizer.list_templates():
            result = contract_from_auto(raw)
            if not result.ok:
                log_event(
                    "query_templates",
                    "WARNING",
                    "template_contract_rejected",
                    template=str(raw.get("id") or ""),
                    code=result.code,
                )
                continue
            contracts.append(result.contract)
        return tuple(contracts)


class VerifiedExampleTemplateProvider:
    def __init__(self, examples: list[dict[str, Any]] | None = None) -> None:
        self.examples = list(examples or [])

    def declared_contracts(
        self,
        spec: QuerySpec,
        schema: PhysicalSchemaView,
    ) -> tuple[TemplateContract, ...]:
        contracts: list[TemplateContract] = []
        for raw in self.examples:
            result = contract_from_example(raw)
            if not result.ok:
                log_event(
                    "query_templates",
                    "WARNING",
                    "template_contract_rejected",
                    template=str(raw.get("doc_name") or raw.get("id") or ""),
                    code=result.code,
                )
                continue
            contracts.append(result.contract)
        return tuple(contracts)
