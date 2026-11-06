from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class CalculationOperand:
    name: str
    value: Any = None
    source: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CalculationOutput:
    name: str
    label: str = ""
    unit: str = ""


@dataclass(frozen=True)
class CalculationRequest:
    tool: str
    operation: str
    operands: tuple[CalculationOperand, ...]
    output: CalculationOutput | None = None
    precision: int = 6
    zero_denominator: str = "error"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CalculationRequest":
        raw_operands = value.get("operands")
        operands = tuple(
            CalculationOperand(
                name=str(item.get("name") or ""),
                value=item.get("value"),
                source=dict(item.get("source") or {}),
            )
            for item in (raw_operands if isinstance(raw_operands, list) else ())
            if isinstance(item, dict)
        )
        raw_output = value.get("output")
        output = (
            CalculationOutput(
                name=str(raw_output.get("name") or ""),
                label=str(raw_output.get("label") or ""),
                unit=str(raw_output.get("unit") or ""),
            )
            if isinstance(raw_output, dict) and raw_output.get("name")
            else None
        )
        return cls(
            tool=str(value.get("tool") or ""),
            operation=str(value.get("operation") or ""),
            operands=operands,
            output=output,
            precision=int(value.get("precision") or 6),
            zero_denominator=str(value.get("zero_denominator") or "error"),
        )


@dataclass(frozen=True)
class CalculationResult:
    accepted: bool
    tool: str
    operation: str
    value: Decimal | None = None
    rows: tuple[dict[str, Any], ...] = ()
    columns: tuple[str, ...] = ()
    formula: str = ""
    failure_code: str = ""
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    def failure(
        self,
        code: str,
        reason: str,
        **evidence: Any,
    ) -> "CalculationResult":
        return CalculationResult(
            accepted=False,
            tool=self.tool,
            operation=self.operation,
            failure_code=code,
            reason=reason,
            evidence=evidence,
        )
