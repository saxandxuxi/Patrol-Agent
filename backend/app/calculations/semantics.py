from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_DIVISION_OPERATIONS = frozenset(
    {"divide", "ratio", "percentage_share"}
)
_GROUPED_TOOLS = frozenset(
    {
        "calculate_columns",
        "calculate_series",
        "calculate_group_change",
        "calculate_datetime_interval",
    }
)


@dataclass(frozen=True)
class CalculationSourceShape:
    result_grain: str
    dimensions: tuple[str, ...]
    identity: str
    unit: str


@dataclass(frozen=True)
class CalculationSemanticIssue:
    code: str
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)


def validate_calculation_sources(
    tool: str,
    operation: str,
    sources: tuple[CalculationSourceShape, ...],
    output_unit: str,
) -> tuple[CalculationSemanticIssue, ...]:
    if len(sources) < 2:
        return ()
    issues: list[CalculationSemanticIssue] = []
    grouped = tuple(item for item in sources if item.dimensions)

    if tool == "calculate_scalar" and grouped:
        issues.append(
            _issue(
                "calculation_grain_mismatch",
                "标量计算只能消费无分组或显式选中单行的来源。",
                sources,
            )
        )
    elif grouped and tool in _GROUPED_TOOLS:
        signatures = {
            (item.dimensions, item.identity)
            for item in grouped
        }
        if len(signatures) != 1:
            issues.append(
                _issue(
                    "calculation_grain_mismatch",
                    "多行来源必须使用相同的分组维度和身份键。",
                    sources,
                )
            )

    if operation in _DIVISION_OPERATIONS:
        denominator = sources[1]
        if denominator.dimensions and not denominator.identity:
            issues.append(
                _issue(
                    "calculation_denominator_not_unique",
                    "分组除法的分母必须具有可对齐且唯一的身份键。",
                    sources,
                )
            )

    expected_unit = _compose_unit(operation, sources)
    if (
        output_unit
        and expected_unit
        and _unit_key(output_unit) != _unit_key(expected_unit)
    ):
        issues.append(
            CalculationSemanticIssue(
                code="calculation_unit_mismatch",
                message="计算输出单位与操作数单位不一致。",
                evidence={
                    "operation": operation,
                    "expected_unit": expected_unit,
                    "actual_unit": output_unit,
                    "source_units": [item.unit for item in sources],
                },
            )
        )
    return tuple(issues)


def _compose_unit(
    operation: str,
    sources: tuple[CalculationSourceShape, ...],
) -> str:
    left, right = sources[0].unit, sources[1].unit
    if not left or not right:
        return ""
    if operation in {"divide", "ratio"}:
        return f"{left}/{right}"
    if operation == "percentage_share":
        return "%"
    if operation == "multiply":
        return f"{left}·{right}"
    if operation in {"add", "subtract"}:
        return left if _unit_key(left) == _unit_key(right) else ""
    return ""


def _unit_key(value: str) -> str:
    return " ".join(str(value or "").split()).replace("／", "/")


def _issue(
    code: str,
    message: str,
    sources: tuple[CalculationSourceShape, ...],
) -> CalculationSemanticIssue:
    return CalculationSemanticIssue(
        code=code,
        message=message,
        evidence={
            "sources": [
                {
                    "result_grain": item.result_grain,
                    "dimensions": list(item.dimensions),
                    "identity": item.identity,
                    "unit": item.unit,
                }
                for item in sources
            ]
        },
    )
