from __future__ import annotations

from typing import Any


_PREFIX = "pile_segment_"
_SUFFIX = "m"


def parse_pile_segment_meters(field: Any) -> int | None:
    name = str(field or "").rsplit(".", 1)[-1]
    if name == "pile_segment":
        return 500
    if not name.startswith(_PREFIX) or not name.endswith(_SUFFIX):
        return None
    raw = name[len(_PREFIX):-len(_SUFFIX)]
    if not raw.isdigit():
        return None
    meters = int(raw)
    return meters if meters > 0 else None


def is_pile_segment_field(field: Any) -> bool:
    return parse_pile_segment_meters(field) is not None


def pile_segment_expression(meters: int) -> str:
    bucket = f"FLOOR(disease.pile_distance / {meters}) * {meters}"
    return (
        f"CONCAT('K', FLOOR({bucket} / 1000), '+', "
        f"LPAD(MOD({bucket}, 1000), 3, '0'), '~K', "
        f"FLOOR(({bucket} + {meters}) / 1000), '+', "
        f"LPAD(MOD({bucket} + {meters}, 1000), 3, '0'))"
    )
