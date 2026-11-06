from __future__ import annotations

from collections.abc import Mapping
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
                source=(
                    dict(item.get("source"))
                    if isinstance(item.get("source"), Mapping)
                    else {}
                ),
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
        raw_precision = value.get("precision")
        if raw_precision in (None, ""):
            precision = 6
        else:
            try:
                precision = int(raw_precision)
            except (TypeError, ValueError) as exc:
                raise ValueError("precision must be an integer") from exc
            if precision < 0 or precision > 12:
                raise ValueError("precision must be between 0 and 12")
        return cls(
            tool=str(value.get("tool") or ""),
            operation=str(value.get("operation") or ""),
            operands=operands,
            output=output,
            precision=precision,
            zero_denominator=str(value.get("zero_denominator") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "operation": self.operation,
            "operands": [
                {
                    "name": item.name,
                    **({"value": item.value} if item.value is not None else {}),
                    **({"source": dict(item.source)} if item.source else {}),
                }
                for item in self.operands
            ],
            **(
                {
                    "output": {
                        "name": self.output.name,
                        "label": self.output.label,
                        "unit": self.output.unit,
                    }
                }
                if self.output
                else {}
            ),
            "precision": self.precision,
            "zero_denominator": self.zero_denominator,
        }


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
