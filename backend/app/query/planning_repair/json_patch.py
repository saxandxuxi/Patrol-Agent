from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from collections import Counter
from typing import Any

from .models import RepairTicket


def _decode_pointer(path: str) -> list[str]:
    value = str(path or "")
    if value == "":
        return []
    if not value.startswith("/"):
        raise ValueError("repair path must be a JSON Pointer")
    return [
        token.replace("~1", "/").replace("~0", "~")
        for token in value[1:].split("/")
    ]


def _resolve_parent(document: Any, tokens: list[str]) -> tuple[Any, str]:
    if not tokens:
        raise ValueError("repair path cannot replace the whole document")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, list):
            try:
                current = current[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"repair path does not exist: {token}") from exc
        elif isinstance(current, dict) and token in current:
            current = current[token]
        else:
            raise ValueError(f"repair path does not exist: {token}")
    return current, tokens[-1]


def _list_index(container: list[Any], token: str) -> int:
    if token == "-":
        return len(container)
    try:
        index = int(token)
    except ValueError as exc:
        raise ValueError("repair list index must be an integer") from exc
    if index < 0 or index > len(container):
        raise ValueError("repair list index is out of range")
    return index


def _apply_one(document: dict[str, Any], patch: dict[str, Any]) -> None:
    operation = str(patch.get("op") or "")
    path = str(patch.get("path") or "")
    tokens = _decode_pointer(path)
    parent, token = _resolve_parent(document, tokens)
    if operation == "set":
        if "value" not in patch:
            raise ValueError("set patch requires value")
        if isinstance(parent, list):
            index = _list_index(parent, token)
            if index >= len(parent):
                raise ValueError("set patch target does not exist")
            parent[index] = copy.deepcopy(patch["value"])
            return
        if not isinstance(parent, dict) or token not in parent:
            raise ValueError("set patch target does not exist")
        parent[token] = copy.deepcopy(patch["value"])
        return
    if operation == "add":
        if "value" not in patch:
            raise ValueError("add patch requires value")
        if isinstance(parent, list):
            parent.insert(_list_index(parent, token), copy.deepcopy(patch["value"]))
            return
        if not isinstance(parent, dict):
            raise ValueError("add patch parent must be an object or array")
        parent[token] = copy.deepcopy(patch["value"])
        return
    if operation == "remove":
        if isinstance(parent, list):
            index = _list_index(parent, token)
            if index >= len(parent):
                raise ValueError("remove patch target does not exist")
            parent.pop(index)
            return
        if not isinstance(parent, dict) or token not in parent:
            raise ValueError("remove patch target does not exist")
        parent.pop(token)
        return
    raise ValueError(f"unsupported repair operation: {operation}")


def _normalized_filters(
    draft: dict[str, Any],
    ignored_fields: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for view in draft.get("views") or []:
        if not isinstance(view, dict):
            continue
        for item in view.get("filters") or []:
            if not isinstance(item, dict):
                continue
            field = str(item.get("field") or "")
            if field in ignored_fields:
                continue
            entry = {
                "field": field,
                "op": str(item.get("op") or "="),
                "raw_value": item.get("raw_value", item.get("value")),
            }
            key = json.dumps(
                entry,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if key in seen:
                continue
            seen.add(key)
            normalized.append(entry)
    return normalized


def _view_scope_filters(
    view: dict[str, Any],
    ignored_fields: frozenset[str],
) -> Counter[str]:
    filters: Counter[str] = Counter()
    for item in view.get("filters") or []:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        if field in ignored_fields:
            continue
        filters[
            json.dumps(
                {
                    "field": field,
                    "op": str(item.get("op") or "="),
                    "raw_value": item.get(
                        "raw_value",
                        item.get("value"),
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        ] += 1
    return filters


def _scope_filters_by_view(
    draft: dict[str, Any],
    ignored_fields: frozenset[str] = frozenset(),
) -> dict[str, Counter[str]]:
    scopes: dict[str, Counter[str]] = {}
    for index, view in enumerate(draft.get("views") or []):
        if not isinstance(view, dict):
            continue
        view_id = str(view.get("id") or f"q{index + 1}").strip()
        if view_id in scopes:
            raise ValueError(f"duplicate repair view id: {view_id}")
        scopes[view_id] = _view_scope_filters(view, ignored_fields)
    return scopes


def _scope_preserved(
    ticket: RepairTicket,
    repaired: dict[str, Any],
) -> bool:
    ignored = frozenset(ticket.issue.mutable_filter_fields)
    before = _scope_filters_by_view(ticket.draft, ignored)
    after = _scope_filters_by_view(repaired, ignored)
    existing_scope = set().union(*before.values()) if before else set()
    for view_id, filters in before.items():
        if after.get(view_id) != filters:
            return False
    for view_id, filters in after.items():
        if view_id in before:
            continue
        if not set(filters).issubset(existing_scope):
            return False
    return True


def scope_signature(
    draft: dict[str, Any],
    mutable_filter_fields: tuple[str, ...] = (),
) -> str:
    payload = json.dumps(
        _normalized_filters(
            draft,
            frozenset(mutable_filter_fields),
        ),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path_allowed(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        path == pattern or fnmatch.fnmatchcase(path, pattern)
        for pattern in patterns
    )


def _patch_signature(patch: dict[str, Any]) -> str:
    payload = json.dumps(
        patch,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def apply_repair_patches(
    ticket: RepairTicket,
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    if not patches:
        raise ValueError("repair patches must not be empty")
    repaired = copy.deepcopy(ticket.draft)
    seen = set(ticket.previous_signatures)
    for patch in patches:
        if not isinstance(patch, dict):
            raise ValueError("repair patch must be an object")
        path = str(patch.get("path") or "")
        if not _path_allowed(path, ticket.issue.allowed_paths):
            raise ValueError(f"repair path is not allowed: {path}")
        signature = _patch_signature(patch)
        if signature in seen:
            raise ValueError("duplicate repair patch")
        seen.add(signature)
        _apply_one(repaired, patch)

    if not _scope_preserved(ticket, repaired):
        raise ValueError("repair changed the user scope")
    return repaired
