from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from ..services.resource_loader import load_capability_file


@dataclass(frozen=True)
class CalculatorCapability:
    tool: str
    input_shapes: tuple[str, ...]
    operations: tuple[str, ...]
    precision: int
    zero_denominator: str


@dataclass(frozen=True)
class CalculationSnapshot:
    calculators: dict[str, CalculatorCapability]


class CalculationRegistry:
    def __init__(self, snapshot: CalculationSnapshot):
        self._snapshot = snapshot

    @property
    def snapshot(self) -> CalculationSnapshot:
        return self._snapshot

    @classmethod
    def load(cls) -> "CalculationRegistry":
        raw = load_capability_file("calculators.yaml", {}) or {}
        calculators = {
            str(tool): CalculatorCapability(
                tool=str(tool),
                input_shapes=tuple(
                    str(item) for item in value.get("input_shapes") or ()
                ),
                operations=tuple(
                    str(item) for item in value.get("operations") or ()
                ),
                precision=int(value.get("precision") or 6),
                zero_denominator=str(value.get("zero_denominator") or "error"),
            )
            for tool, value in raw.items()
            if isinstance(value, dict)
        }
        return cls(CalculationSnapshot(calculators=calculators))

    def capability(self, tool: str) -> CalculatorCapability | None:
        return self.snapshot.calculators.get(str(tool))


@lru_cache(maxsize=1)
def default_calculation_registry() -> CalculationRegistry:
    return CalculationRegistry.load()
