from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldReachHit:
    table: str
    column: str
    matched_text: str
    source: str
    score: float


@dataclass(frozen=True)
class FieldReachSet:
    hits: tuple[FieldReachHit, ...] = ()

    def contains_physical(self, table: str, column: str) -> bool:
        # Reach hits are evidence for candidate selection, never proof that a
        # physical column exists. PhysicalSchemaView owns that decision.
        return False


@dataclass(frozen=True)
class PhysicalSchemaView:
    columns: frozenset[tuple[str, str]]
    snapshot: str = ""

    def has_column(self, table: str, column: str) -> bool:
        return (table, column) in self.columns


@dataclass(frozen=True)
class FieldRef:
    table: str
    column: str
    display_label: str


@dataclass(frozen=True)
class OutputProjection:
    fields: tuple[FieldRef, ...] = ()
