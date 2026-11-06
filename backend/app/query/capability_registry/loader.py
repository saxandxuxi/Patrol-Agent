from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class CapabilityConfigError(ValueError):
    pass


_FILE_ROOT_KEYS = {
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
    "calculators.yaml": "calculators",
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: Any, deep: bool = False) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise CapabilityConfigError(f"duplicate_key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True)
class LoadedCapabilityConfig:
    source_dir: Path
    version: str
    config_hash: str
    documents: dict[str, dict[str, Any]]


def default_capability_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "resources" / "capabilities"


def load_capability_config(config_dir: Path | None = None) -> LoadedCapabilityConfig:
    source_dir = Path(config_dir or default_capability_dir()).resolve()
    index_path = source_dir / "index.yaml"
    if not index_path.is_file():
        raise CapabilityConfigError(f"missing_config: {index_path}")
    index = _read_yaml(index_path)
    unknown_index = set(index) - {"version", "files"}
    if unknown_index:
        raise CapabilityConfigError(
            "unknown_key: " + ", ".join(sorted(str(item) for item in unknown_index))
        )
    version = str(index.get("version") or "").strip()
    if not version:
        raise CapabilityConfigError("missing_version: index.yaml")

    expected_files = set(_FILE_ROOT_KEYS)
    manifest_files = set(str(item) for item in index.get("files") or [])
    missing_from_manifest = expected_files - manifest_files
    missing_on_disk = {
        name for name in expected_files if not (source_dir / name).is_file()
    }
    if missing_from_manifest or missing_on_disk:
        missing = sorted(missing_from_manifest | missing_on_disk)
        raise CapabilityConfigError("missing_config: " + ", ".join(missing))

    documents: dict[str, dict[str, Any]] = {}
    digest = hashlib.sha256()
    for file_name, root_key in _FILE_ROOT_KEYS.items():
        path = source_dir / file_name
        raw = path.read_bytes()
        digest.update(file_name.encode("utf-8"))
        digest.update(raw)
        document = _read_yaml(path)
        unknown = set(document) - {root_key}
        if unknown:
            raise CapabilityConfigError(
                f"unknown_key: {file_name}: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        value = document.get(root_key)
        if not isinstance(value, dict):
            raise CapabilityConfigError(
                f"invalid_root: {file_name} must contain mapping {root_key}"
            )
        documents[root_key] = value

    return LoadedCapabilityConfig(
        source_dir=source_dir,
        version=version,
        config_hash=digest.hexdigest(),
        documents=documents,
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except CapabilityConfigError:
        raise
    except (OSError, yaml.YAMLError) as exc:
        raise CapabilityConfigError(f"invalid_yaml: {path}: {exc}") from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise CapabilityConfigError(f"invalid_yaml: {path} must contain a mapping")
    return value
