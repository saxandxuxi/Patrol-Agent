from __future__ import annotations

import json
import re
from typing import Any


def parse_json_lenient(
    text: str,
    allow_list: bool = False,
) -> dict[str, Any] | list[Any] | None:
    """Parse JSON even when the model wraps it in reasoning or fences."""
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        parsed = _parse_candidate(candidate, allow_list)
        if parsed is not None:
            return parsed
    return _parse_last_complete_embedded_value(text, allow_list)


def _parse_candidate(
    text: str,
    allow_list: bool,
) -> dict[str, Any] | list[Any] | None:
    if not text:
        return None
    for payload in (text, re.sub(r",\s*([}\]])", r"\1", text)):
        for strict in (True, False):
            try:
                data = json.loads(payload, strict=strict)
            except (json.JSONDecodeError, ValueError):
                continue
            if _accepted(data, allow_list):
                return data
    return None


def _parse_last_complete_embedded_value(
    text: str,
    allow_list: bool,
) -> dict[str, Any] | list[Any] | None:
    starts = [
        index
        for index, character in enumerate(text)
        if character == "{" or (allow_list and character == "[")
    ]
    decoder = json.JSONDecoder(strict=False)
    parsed: list[tuple[int, int, dict[str, Any] | list[Any]]] = []
    for index in starts:
        try:
            data, end = decoder.raw_decode(text, index)
        except (json.JSONDecodeError, ValueError):
            continue
        if _accepted(data, allow_list):
            parsed.append((index, end, data))
    if not parsed:
        return None
    outer = [
        item
        for item in parsed
        if not any(
            other[0] <= item[0]
            and item[1] <= other[1]
            and (other[0], other[1]) != (item[0], item[1])
            for other in parsed
        )
    ]
    return max(outer, key=lambda item: item[0])[2]


def _accepted(data: Any, allow_list: bool) -> bool:
    return isinstance(data, dict) or (
        allow_list and isinstance(data, list)
    )
