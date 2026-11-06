"""资源加载器：读项目根 `resources/` 下的 YAML / JSON 知识文件。

仿 `prompts.py` 的成熟做法（docs/代码结构重构与模板外置方案.md §5.1）：
`lru_cache` + `(mtime, size)` 版本号热更新 + 缺失/语法错时**回退到安全默认并写 error 日志**，
绝不静默、绝不抛 500。启动时 `self_check()` 做一次资源自检。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT
from .logging_service import log_error, log_event

RESOURCES_DIR = Path(PROJECT_ROOT) / "resources"
_CACHE: dict[str, tuple[tuple[float, int] | None, Any]] = {}
_CAPABILITY_ROOT_KEYS = {
    "subjects.yaml": "subjects",
    "fields.yaml": "fields",
    "field_overrides.yaml": "field_overrides",
    "metrics.yaml": "metrics",
    "derived_metrics.yaml": "derived_metrics",
    "enums.yaml": "enums",
    "entities.yaml": "entities",
    "entity_hierarchies.yaml": "families",
    "aliases.yaml": "aliases",
    "identifiers.yaml": "identifiers",
    "query_shapes.yaml": "query_shapes",
    "dimensions.yaml": "dimensions",
    "predicates.yaml": "predicates",
    "time_buckets.yaml": "time_buckets",
    "result_words.yaml": "result_words",
    "repair_policy.yaml": "repair_policy",
    "templates.yaml": "templates",
}


def _path(name: str) -> Path:
    return RESOURCES_DIR / name


def _version(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
        return (stat.st_mtime, stat.st_size)
    except OSError:
        return None


def _load(name: str) -> Any:
    """按版本号缓存地读一个资源文件；文件变了自动重载。"""
    path = _path(name)
    version = _version(path)
    cached = _CACHE.get(name)
    if cached and cached[0] == version:
        return cached[1]
    if version is None:
        log_error("resources", "missing", FileNotFoundError(str(path)), resource=name)
        _CACHE[name] = (None, None)
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in {".yaml", ".yml"}:
            import yaml

            data = yaml.safe_load(text) or {}
        else:
            import json

            data = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        log_error("resources", "parse_failed", exc, resource=name)
        data = None
    else:
        log_event("resources", "INFO", "loaded", resource=name)
    _CACHE[name] = (version, data)
    return data


def load_yaml(name: str, default: Any = None) -> Any:
    data = _load(name)
    return default if data is None else data


def load_json(name: str, default: Any = None) -> Any:
    return load_yaml(name, default)


def load_capability_file(name: str, default: Any = None) -> Any:
    root_key = _CAPABILITY_ROOT_KEYS.get(str(name))
    if not root_key:
        raise KeyError(f"unknown capability file: {name}")
    data = _load(f"capabilities/{name}")
    if not isinstance(data, dict):
        return default
    return data.get(root_key, default)


def clear_cache() -> None:
    _CACHE.clear()


def words(name: str, section: str = "", default: tuple[str, ...] = ()) -> tuple[str, ...]:
    """取兼容词表；业务配置优先使用 load_capability_file。"""
    data = load_yaml(name, {}) or {}
    for key in [part for part in str(section).split(".") if part]:
        data = (data or {}).get(key) if isinstance(data, dict) else None
    if isinstance(data, list):
        return tuple(str(item) for item in data if str(item).strip())
    return default


def self_check() -> dict[str, Any]:
    """启动自检：目录里的资源文件能不能解析、模板 id 有没有重复。"""
    report: dict[str, Any] = {"resources_dir": str(RESOURCES_DIR), "files": 0, "errors": []}
    if not RESOURCES_DIR.exists():
        report["errors"].append("resources/ 目录不存在")
        log_event("resources", "WARNING", "self_check", **{"errors": report["errors"]})
        return report
    ids: list[str] = []
    for path in sorted(RESOURCES_DIR.rglob("*")):
        if path.is_dir() or path.suffix.lower() not in {".yaml", ".yml", ".json"}:
            continue
        report["files"] += 1
        data = _load(str(path.relative_to(RESOURCES_DIR)).replace("\\", "/"))
        if data is None:
            report["errors"].append(f"{path.name} 解析失败")
            continue
        if isinstance(data, dict) and isinstance(data.get("templates"), list):
            for item in data["templates"]:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
    duplicates = {item for item in ids if ids.count(item) > 1}
    if duplicates:
        report["errors"].append("模板 id 重复：" + "、".join(sorted(duplicates)))
    report["templates"] = len(ids)
    log_event("resources", "INFO", "self_check", **{k: v for k, v in report.items() if k != "resources_dir"})
    return report
