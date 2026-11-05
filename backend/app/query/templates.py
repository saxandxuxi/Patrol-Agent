from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Protocol

from .contracts import QuerySpec, ValueBinding, scope_hash
from .fields import PhysicalSchemaView


@dataclass(frozen=True)
class TemplateContract:
    id: str
    version: int
    source: str
    status: str
    kind: str
    subject: str
    view: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...]
    filters: tuple[str, ...]
    fixed_conditions: tuple[ValueBinding, ...]
    adapter_id: str = ""
    native_id: str = ""
    output_projection: tuple[str, ...] = ()
    required_assertions: tuple[str, ...] = ()
    physical_columns: tuple[tuple[str, str], ...] = ()
    title: str = ""


@dataclass(frozen=True)
class ConfirmationCard:
    source: str
    title: str
    version: int
    scope_summary: str
    bound_parameters: tuple[ValueBinding, ...]
    fixed_conditions: tuple[ValueBinding, ...]
    warnings: tuple[str, ...]
    sql_summary: str = ""


@dataclass(frozen=True)
class TemplateCandidate:
    candidate_id: str
    node_id: str
    contract: TemplateContract
    spec_fingerprint: str
    bound_parameters: tuple[ValueBinding, ...]
    covered_constraints: tuple[str, ...]
    fixed_conditions: tuple[ValueBinding, ...]
    warnings: tuple[str, ...]
    requires_confirmation: bool
    confirmation_token: str
    confirmation_card: ConfirmationCard


@dataclass(frozen=True)
class CandidateSet:
    candidates: tuple[TemplateCandidate, ...] = ()
    rejections: tuple[str, ...] = ()

    def merge(self, other: "CandidateSet") -> "CandidateSet":
        return CandidateSet(
            candidates=self.candidates + other.candidates,
            rejections=self.rejections + other.rejections,
        )


class TemplateProvider(Protocol):
    def declared_contracts(
        self,
        spec: QuerySpec,
        schema: PhysicalSchemaView,
    ) -> tuple[TemplateContract, ...]:
        ...


def _scope_summary(spec: QuerySpec) -> str:
    parts = [
        f"主体={spec.subject}",
        f"视图={spec.view}",
        f"指标={','.join(spec.metrics) or '无'}",
        f"维度={','.join(spec.dimensions) or '无'}",
    ]
    if spec.filters:
        parts.append(
            "条件="
            + ",".join(
                f"{item.field}:{item.display_value}" for item in spec.filters
            )
        )
    return "；".join(parts)


def _eligibility_issues(
    spec: QuerySpec,
    schema: PhysicalSchemaView,
    contract: TemplateContract,
) -> list[str]:
    issues: list[str] = []
    if contract.status != "active":
        issues.append(f"inactive_template:{contract.status}")
    if contract.kind != spec.kind:
        issues.append(f"kind_mismatch:{contract.kind}")
    if contract.subject != spec.subject:
        issues.append(f"subject_mismatch:{contract.subject}")
    if contract.view != spec.view:
        issues.append(f"view_mismatch:{contract.view}")
    issues.extend(
        f"missing_metric:{item}"
        for item in spec.metrics
        if item not in contract.metrics
    )
    issues.extend(
        f"missing_dimension:{item}"
        for item in spec.dimensions
        if item not in contract.dimensions
    )
    issues.extend(
        f"missing_output:{item}"
        for item in spec.output_projection
        if item not in contract.output_projection
    )
    for column in contract.physical_columns:
        if not schema.has_column(*column):
            issues.append(f"missing_physical_column:{column[0]}.{column[1]}")

    fixed_by_field = {item.field: item for item in contract.fixed_conditions}
    spec_by_field = {item.field: item for item in spec.filters}
    for item in spec.filters:
        if item.field not in contract.filters and item.field not in fixed_by_field:
            issues.append(f"missing_filter:{item.field}")
    for field_name, fixed in fixed_by_field.items():
        actual = spec_by_field.get(field_name)
        if actual is None:
            issues.append(f"missing_fixed_condition:{field_name}")
        elif str(actual.storage_value) != str(fixed.storage_value):
            issues.append(
                f"fixed_condition_conflict:{field_name}:"
                f"{actual.storage_value}!={fixed.storage_value}"
            )
    return issues


def resolve_templates(
    spec: QuerySpec,
    schema: PhysicalSchemaView,
    providers: list[TemplateProvider] | tuple[TemplateProvider, ...],
) -> CandidateSet:
    candidates: list[TemplateCandidate] = []
    rejections: list[str] = []
    for provider in providers:
        try:
            contracts = provider.declared_contracts(spec, schema)
        except Exception as exc:  # noqa: BLE001
            rejections.append(f"provider_error:{type(exc).__name__}:{exc}")
            continue
        for contract in contracts:
            issues = _eligibility_issues(spec, schema, contract)
            if issues:
                rejections.append(
                    f"{contract.adapter_id or contract.source}:{contract.id}:"
                    + ";".join(issues)
                )
                continue
            token = uuid.uuid4().hex
            candidate_id = (
                f"{contract.adapter_id or contract.source}:"
                f"{contract.native_id or contract.id}:{spec.id}:v{contract.version}"
            )
            card = ConfirmationCard(
                source=contract.source,
                title=contract.title or contract.id,
                version=contract.version,
                scope_summary=_scope_summary(spec),
                bound_parameters=spec.filters,
                fixed_conditions=contract.fixed_conditions,
                warnings=(),
            )
            candidates.append(
                TemplateCandidate(
                    candidate_id=candidate_id,
                    node_id=spec.id,
                    contract=contract,
                    spec_fingerprint=scope_hash(spec),
                    bound_parameters=spec.filters,
                    covered_constraints=(
                        spec.metrics
                        + spec.dimensions
                        + tuple(item.field for item in spec.filters)
                        + spec.output_projection
                    ),
                    fixed_conditions=contract.fixed_conditions,
                    warnings=(),
                    requires_confirmation=True,
                    confirmation_token=token,
                    confirmation_card=card,
                )
            )
    return CandidateSet(candidates=tuple(candidates), rejections=tuple(rejections))
