from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import sqlglot
from sqlglot import exp

from ...services import wren_engine
from .errors import MdlSqlConfigurationError
from .models import (
    MdlColumn,
    MdlCube,
    MdlCubeField,
    MdlModel,
    MdlRelationship,
    MdlSnapshot,
)


def load_mdl_snapshot(
    kind: str,
    project_dir: Path | None = None,
) -> MdlSnapshot:
    base = Path(project_dir or wren_engine._project_dir(kind)).resolve()
    raw = wren_engine._load_mdl(str(base))
    cards = {
        str(card.get("name") or ""): card
        for card in wren_engine._schema_index(str(base))
    }
    tables_by_model = raw.get("tables_by_model") or {}

    models = _build_models(raw.get("models") or (), cards, tables_by_model)
    if len(models) != len(raw.get("models") or ()):
        raise MdlSqlConfigurationError(
            "invalid_mdl_catalog",
            {"reason": "duplicate_or_missing_model_name"},
            message="MDL 模型名称缺失或重复",
        )
    cubes = _build_cubes(raw.get("cubes") or ())
    relationships = _build_relationships(
        raw.get("relationships") or (),
        models,
    )
    return MdlSnapshot(
        fingerprint=_mdl_fingerprint(base),
        models=MappingProxyType(models),
        cubes=MappingProxyType(cubes),
        relationships=MappingProxyType(relationships),
        source_dir=str(base),
    )


def _build_models(
    raw_models: list[dict[str, Any]],
    cards: Mapping[str, Mapping[str, Any]],
    tables_by_model: Mapping[str, Any],
) -> dict[str, MdlModel]:
    models: dict[str, MdlModel] = {}
    for raw in raw_models:
        if not isinstance(raw, Mapping):
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "model_is_not_mapping"},
                message="MDL 模型定义必须是对象",
            )
        name = str(raw.get("name") or "")
        if not name or name in models:
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "duplicate_or_missing_model_name", "model": name},
                message="MDL 模型名称缺失或重复",
            )
        table_reference = raw.get("table_reference") or {}
        physical_table = str(
            table_reference.get("table")
            if isinstance(table_reference, Mapping)
            else ""
        )
        if not physical_table:
            physical_table = str(
                (cards.get(name) or {}).get("table")
                or tables_by_model.get(name)
                or ""
            )

        columns: dict[str, MdlColumn] = {}
        for raw_column in raw.get("columns") or ():
            if not isinstance(raw_column, Mapping):
                raise MdlSqlConfigurationError(
                    "invalid_mdl_catalog",
                    {"reason": "column_is_not_mapping", "model": name},
                    message="MDL 字段定义必须是对象",
                )
            column_name = str(raw_column.get("name") or "")
            if not column_name or column_name in columns:
                raise MdlSqlConfigurationError(
                    "invalid_mdl_catalog",
                    {
                        "reason": "duplicate_or_missing_column_name",
                        "model": name,
                        "column": column_name,
                    },
                    message="MDL 字段名称缺失或重复",
                )
            column_properties = raw_column.get("properties") or {}
            columns[column_name] = MdlColumn(
                name=column_name,
                data_type=str(raw_column.get("type") or ""),
                description=str(
                    column_properties.get("description")
                    if isinstance(column_properties, Mapping)
                    else ""
                ),
                is_calculated=bool(raw_column.get("is_calculated", False)),
                expression=str(raw_column.get("expression") or ""),
                is_primary_key=bool(raw_column.get("is_primary_key", False)),
            )

        properties = raw.get("properties") or {}
        models[name] = MdlModel(
            name=name,
            physical_table=physical_table,
            description=str(
                properties.get("description")
                if isinstance(properties, Mapping)
                else ""
            ),
            columns=MappingProxyType(columns),
        )
    return models


def _build_cubes(raw_cubes: list[dict[str, Any]]) -> dict[str, MdlCube]:
    cubes: dict[str, MdlCube] = {}
    for raw in raw_cubes:
        if not isinstance(raw, Mapping):
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "cube_is_not_mapping"},
                message="MDL 指标定义必须是对象",
            )
        name = str(raw.get("name") or "")
        if not name or name in cubes:
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "duplicate_or_missing_cube_name", "cube": name},
                message="MDL 指标名称缺失或重复",
            )
        properties = raw.get("properties") or {}
        cubes[name] = MdlCube(
            name=name,
            base_object=str(raw.get("base_object") or ""),
            description=str(
                properties.get("description")
                if isinstance(properties, Mapping)
                else ""
            ),
            measures=_build_cube_fields(raw.get("measures") or (), name),
            dimensions=_build_cube_fields(raw.get("dimensions") or (), name),
        )
    return cubes


def _build_cube_fields(
    raw_fields: list[dict[str, Any]],
    cube: str,
) -> tuple[MdlCubeField, ...]:
    fields: list[MdlCubeField] = []
    seen: set[str] = set()
    for raw in raw_fields:
        if not isinstance(raw, Mapping):
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "cube_field_is_not_mapping", "cube": cube},
                message="MDL 指标字段定义必须是对象",
            )
        name = str(raw.get("name") or "")
        if not name or name in seen:
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {
                    "reason": "duplicate_or_missing_cube_field",
                    "cube": cube,
                    "field": name,
                },
                message="MDL 指标字段名称缺失或重复",
            )
        seen.add(name)
        properties = raw.get("properties") or {}
        fields.append(
            MdlCubeField(
                name=name,
                expression=str(raw.get("expression") or ""),
                data_type=str(raw.get("type") or ""),
                description=str(
                    properties.get("description")
                    if isinstance(properties, Mapping)
                    else ""
                ),
            )
        )
    return tuple(fields)


def _build_relationships(
    raw_relationships: list[dict[str, Any]],
    models: Mapping[str, MdlModel],
) -> dict[str, MdlRelationship]:
    relationships: dict[str, MdlRelationship] = {}
    for raw in raw_relationships:
        if not isinstance(raw, Mapping):
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {"reason": "relationship_is_not_mapping"},
                message="MDL 关联定义必须是对象",
            )
        relationship_id = str(raw.get("name") or "")
        raw_models = raw.get("models") or ()
        if isinstance(raw_models, str):
            raw_models = tuple(
                item.strip().strip("'\"")
                for item in raw_models.strip("[]").split(",")
                if item.strip()
            )
        relationship_models = tuple(str(item) for item in raw_models)
        condition = str(raw.get("condition") or "")
        if (
            not relationship_id
            or relationship_id in relationships
            or len(relationship_models) != 2
            or relationship_models[0] == relationship_models[1]
            or any(model not in models for model in relationship_models)
            or not condition
        ):
            raise MdlSqlConfigurationError(
                "invalid_mdl_catalog",
                {
                    "reason": "invalid_relationship",
                    "relationship": relationship_id,
                },
                message="MDL 关联定义缺少名称、模型或条件",
            )
        join_type = str(raw.get("join_type") or "")
        relationships[relationship_id] = MdlRelationship(
            relationship_id=relationship_id,
            models=(relationship_models[0], relationship_models[1]),
            join_type=join_type,
            cardinality=str(raw.get("cardinality") or join_type),
            condition=condition,
            field_pairs=_parse_equality_pairs(
                condition,
                relationship_id=relationship_id,
            ),
            source=str(raw.get("source") or "relationships.yml"),
        )
    return relationships


def _parse_equality_pairs(
    condition: str,
    *,
    relationship_id: str,
) -> tuple[tuple[str, str], ...]:
    try:
        expression = sqlglot.parse_one(condition, read="mysql")
    except sqlglot.errors.ParseError as exc:
        raise MdlSqlConfigurationError(
            "invalid_mdl_catalog",
            {
                "reason": "relationship_condition_parse_failed",
                "relationship": relationship_id,
            },
            message="MDL 关联条件无法解析",
        ) from exc

    pairs: list[tuple[str, str]] = []
    for equality in expression.find_all(exp.EQ):
        left = equality.this
        right = equality.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            continue
        left_name = f"{left.table}.{left.name}" if left.table else left.name
        right_name = (
            f"{right.table}.{right.name}" if right.table else right.name
        )
        if not left.table or not right.table:
            continue
        pairs.append((left_name, right_name))
    if not pairs:
        raise MdlSqlConfigurationError(
            "invalid_mdl_catalog",
            {
                "reason": "relationship_condition_has_no_column_equality",
                "relationship": relationship_id,
            },
            message="MDL 关联条件缺少字段等值关系",
        )
    return tuple(pairs)


def _mdl_fingerprint(base: Path) -> str:
    paths: set[Path] = set()
    for name in ("wren_project.yml", "overrides.yml", "relationships.yml"):
        path = base / name
        if path.is_file():
            paths.add(path)
    for directory, pattern in (
        ("models", "*.yml"),
        ("cubes", "*.yml"),
    ):
        root = base / directory
        if root.is_dir():
            paths.update(root.rglob(pattern))
    target = base / "target" / "mdl.json"
    if target.is_file():
        paths.add(target)

    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(base).as_posix()):
        relative = path.relative_to(base).as_posix().encode("utf-8")
        raw = path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return digest.hexdigest()
