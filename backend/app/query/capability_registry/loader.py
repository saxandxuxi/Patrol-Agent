from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class CapabilityConfigError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.evidence = dict(evidence or {})
        super().__init__(message)


_FILE_ROOT_KEYS = {
    "subjects.yaml": "subjects",
    "mdl_compilation.yaml": "mdl_compilation",
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
    "capability_gaps.yaml": "capability_gaps",
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

    _validate_single_source_facts(documents)
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


def _validate_single_source_facts(documents: dict[str, dict[str, Any]]) -> None:
    for subject, capability in documents["subjects"].items():
        if isinstance(capability, dict) and "joins" in capability:
            path = f"subjects.{subject}.joins"
            raise CapabilityConfigError(
                f"single_source_violation: {path} must come from Wren MDL",
                evidence={
                    "fact": "joins",
                    "required_source": "wren_mdl",
                    "path": path,
                },
            )

    path = _first_key_path(
        documents["field_overrides"],
        "code_map",
        "field_overrides",
    )
    if path:
        raise CapabilityConfigError(
            f"single_source_violation: {path} must be defined in enums.yaml",
            evidence={
                "fact": "code_map",
                "required_source": "enums.yaml",
                "path": path,
            },
        )

    enum_columns: set[tuple[str, str]] = set()
    display_owners: dict[tuple[str, str], str] = {}
    for enum_id, raw in documents["enums"].items():
        if not isinstance(raw, dict):
            continue
        table = str(raw.get("table") or "")
        column = str(raw.get("column") or "")
        display_column = str(raw.get("display_column") or "")
        if not table or not column:
            raise CapabilityConfigError(
                f"invalid_enum_key: {enum_id}",
            )
        expected_key = f"{table}.{column}"
        if str(enum_id) != expected_key:
            raise CapabilityConfigError(
                f"enum_key_mismatch: {enum_id} != {expected_key}",
            )
        if not display_column:
            raise CapabilityConfigError(
                f"missing_enum_display_column: {enum_id}",
            )
        owner_key = (table, display_column)
        owner = display_owners.get(owner_key)
        if owner and owner != str(enum_id):
            raise CapabilityConfigError(
                "duplicate_enum_display_column: "
                f"{table}.{display_column}",
                evidence={
                    "code": "duplicate_enum_display_column",
                    "table": table,
                    "display_column": display_column,
                    "owners": [owner, str(enum_id)],
                },
            )
        display_owners[owner_key] = str(enum_id)
        enum_columns.add((table, column))

    for subject, subject_fields in documents["fields"].items():
        if not isinstance(subject_fields, dict):
            continue
        for field_id, raw in subject_fields.items():
            if not isinstance(raw, dict) or not raw.get("enum_key"):
                continue
            enum_path = f"fields.{subject}.{field_id}"
            if raw.get("label"):
                raise CapabilityConfigError(
                    "single_source_violation: "
                    f"{enum_path}.label must come from enums.yaml",
                    evidence={
                        "fact": "enum_label",
                        "required_source": "enums.yaml",
                        "path": f"{enum_path}.label",
                    },
                )
            if "CASE " in str(raw.get("expression") or "").upper():
                raise CapabilityConfigError(
                    "single_source_violation: "
                    f"{enum_path}.expression must reference a display column",
                    evidence={
                        "fact": "enum_case",
                        "required_source": "enums.yaml",
                        "path": f"{enum_path}.expression",
                    },
                )

    for kind, raw in documents["field_overrides"].items():
        if not isinstance(raw, dict):
            continue
        raw_columns = raw.get("columns")
        if not isinstance(raw_columns, dict):
            continue
        for table, columns in raw_columns.items():
            if not isinstance(columns, dict):
                continue
            for column, override in columns.items():
                if (
                    (str(table), str(column)) in enum_columns
                    and isinstance(override, dict)
                    and override.get("label")
                ):
                    path = (
                        f"field_overrides.{kind}.columns."
                        f"{table}.{column}.label"
                    )
                    raise CapabilityConfigError(
                        "single_source_violation: "
                        f"{path} must come from enums.yaml",
                        evidence={
                            "fact": "enum_label",
                            "required_source": "enums.yaml",
                            "path": path,
                        },
                    )

    for subject, metrics in documents["metrics"].items():
        if str(subject).startswith("_") or not isinstance(metrics, dict):
            continue
        for metric_id, metric in metrics.items():
            if not isinstance(metric, dict):
                continue
            source = metric.get("source") or {}
            raw_kind = (
                source.get("kind")
                if isinstance(source, dict)
                else ""
            )
            kind = str(raw_kind or "").strip() or "resources"
            path = f"metrics.{subject}.{metric_id}"
            expression = str(metric.get("expression") or "").strip()
            raw_reference = (
                source.get("reference")
                if isinstance(source, dict)
                else ""
            )
            reference = str(raw_reference or "").strip()
            if kind == "mdl_cube":
                if expression or not reference:
                    raise CapabilityConfigError(
                        "single_source_violation: "
                        f"{path} must reference an MDL measure without "
                        "duplicating its expression",
                        evidence={
                            "fact": "metric_formula",
                            "required_source": "wren_mdl",
                            "path": path,
                        },
                    )
            elif kind == "resources":
                if not expression:
                    raise CapabilityConfigError(
                        "single_source_violation: "
                        f"{path} must own an expression",
                        evidence={
                            "fact": "metric_formula",
                            "required_source": "metrics.yaml",
                            "path": path,
                        },
                    )
            else:
                raise CapabilityConfigError(
                    f"invalid_metric_source: {path}",
                )


def _first_key_path(
    value: Any,
    wanted: str,
    path: str,
) -> str:
    if not isinstance(value, dict):
        return ""
    for key, item in value.items():
        item_path = f"{path}.{key}"
        if key == wanted:
            return item_path
        found = _first_key_path(item, wanted, item_path)
        if found:
            return found
    return ""
