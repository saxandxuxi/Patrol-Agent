from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import event_bus


@dataclass(frozen=True)
class Tool:
    name: str
    layer: str
    domain: str
    description: str
    input_schema: dict[str, Any]
    wraps: str
    run: Callable[..., dict[str, Any]] = field(repr=False, compare=False)

    def metadata(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("run", None)
        return data


_REGISTRY: dict[str, Tool] = {}


def tool(
    *,
    name: str,
    layer: str,
    domain: str,
    description: str,
    input_schema: dict[str, Any],
    wraps: str,
) -> Callable[[Callable[..., dict[str, Any]]], Callable[..., dict[str, Any]]]:
    def register(func: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
        if name in _REGISTRY:
            raise ValueError(f"工具名重复：{name}")
        if layer not in {"big", "atomic"}:
            raise ValueError(f"未知工具层级：{layer}")
        _REGISTRY[name] = Tool(
            name=name,
            layer=layer,
            domain=domain,
            description=description,
            input_schema=input_schema,
            wraps=wraps,
            run=func,
        )
        return func

    return register


def get_tool(name: str) -> Tool:
    return _REGISTRY[str(name)]


def list_tools(layer: str = "") -> list[Tool]:
    items = sorted(_REGISTRY.values(), key=lambda item: (item.layer, item.domain, item.name))
    if layer:
        items = [item for item in items if item.layer == layer]
    return items


def run_tool(name: str, **kwargs: Any) -> dict[str, Any]:
    """按名调用工具；异常也转成统一结果，交编排层作为观察处理。"""
    started = time.perf_counter()
    try:
        item = get_tool(name)
        event_bus.emit_event(
            "action",
            layer=item.layer,
            tool=item.name,
            args={
                key: (str(value)[:200] if not isinstance(value, (int, float, bool, type(None))) else value)
                for key, value in kwargs.items()
            },
        )
        result = item.run(**kwargs)
    except Exception as exc:  # noqa: BLE001
        result = {
            "ok": False,
            "tool": name,
            "value": None,
            "error": f"{type(exc).__name__}: {exc}",
        }
    data = result.get("data") if isinstance(result, dict) else None
    event_bus.emit_event(
        "observation",
        layer=getattr(locals().get("item"), "layer", "big"),
        tool=name,
        ok=bool(result.get("ok")) if isinstance(result, dict) else False,
        rows=len((data or {}).get("preview_rows") or []) if isinstance(data, dict) else 0,
        row_count=int((data or {}).get("row_count") or 0) if isinstance(data, dict) else 0,
        produced=(data or {}).get("produced") or {} if isinstance(data, dict) else {},
        truncated=bool((data or {}).get("truncated")) if isinstance(data, dict) else False,
        empty=bool(((result.get("coverage") or {}).get("empty"))) if isinstance(result, dict) else False,
        duration_ms=round((time.perf_counter() - started) * 1000),
        summary=str((result or {}).get("summary") or (result or {}).get("error") or "")[:240],
    )
    return result


def catalog() -> dict[str, Any]:
    return {
        "version": 1,
        "tools": [item.metadata() for item in list_tools()],
    }


def write_catalog(path: str | Path | None = None) -> Path:
    target = Path(path) if path else Path(__file__).with_name("catalog.json")
    target.write_text(
        json.dumps(catalog(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
