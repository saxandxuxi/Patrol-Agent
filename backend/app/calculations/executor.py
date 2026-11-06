from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .columns import calculate_columns
from .domain.analysis import analyze_evidence
from .domain.rendering import render_calculation
from .evidence import compact_table_evidence, row_with_group_labels
from .models import CalculationOperand, CalculationRequest
from .numbers import numeric_value
from .registry import default_calculation_registry
from .scalar import calculate_scalar
from .serialization import json_value, result_payload
from .series import calculate_series
from ..services import react_repair


def dependency_map(
    results: dict[int | str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {str(key): value for key, value in (results or {}).items()}


def field_label(dependency: dict[str, Any], field: str) -> str:
    labels = dependency.get("field_labels") or {}
    return str(labels.get(field) or field)


def request_from_task(task: dict[str, Any]) -> CalculationRequest | None:
    value = task.get("calculation")
    if "calculation" in task and value is not None and not isinstance(
        value,
        Mapping,
    ):
        raise ValueError("calculation 必须是对象")
    if not isinstance(value, Mapping):
        return None
    return CalculationRequest.from_dict(dict(value))


def input_shape(tool: str) -> str:
    return {
        "calculate_scalar": "scalar_list",
        "calculate_columns": "rows",
        "calculate_series": "series",
    }.get(tool, "")


def calculation_failure(
    result: Any,
    task: dict[str, Any],
    request: CalculationRequest,
) -> dict[str, Any]:
    evidence = {
        "tool": request.tool,
        "operation": request.operation,
        "input_shape": input_shape(request.tool),
        "operands": [
            {
                "name": item.name,
                "value": json_value(item.value),
                "source": dict(item.source),
            }
            for item in request.operands
        ],
        "output": (
            request.output.__dict__ if request.output is not None else None
        ),
        "precision": request.precision,
        **dict(result.evidence),
    }
    failure = react_repair.build_failure(
        result.failure_code or "calculation_output_invalid",
        stage="post_calc",
        reason=result.reason or "计算没有返回有效结果",
        evidence={
            "step_id": task.get("id"),
            "step_question": str(task.get("question") or ""),
            "calculation": evidence,
        },
    )
    return {
        "reply": result.reason or "计算失败",
        "source": "error",
        "engine": "post_calc",
        "accepted": False,
        "failure": failure,
        "columns": [],
        "rows": [],
    }


def execute_calculation(
    task: dict[str, Any],
    request: CalculationRequest,
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    dependency_ids = [str(item) for item in task.get("depends_on") or ()]
    dependencies = [
        results[item] for item in dependency_ids if item in results
    ]
    if request.tool == "calculate_scalar":
        resolved: list[CalculationOperand] = []
        for operand in request.operands:
            if operand.source:
                source = results.get(str(operand.source.get("node") or ""))
                if source is None:
                    return calculation_failure(
                        calculate_scalar(
                            CalculationRequest(
                                tool=request.tool,
                                operation=request.operation,
                                operands=(),
                            )
                        ).failure(
                            "calculation_operand_missing",
                            f"缺少上游节点 {operand.source.get('node')}",
                        ),
                        task,
                        request,
                    )
                resolved.append(
                    CalculationOperand(
                        name=operand.name,
                        value=numeric_value(
                            source,
                            field_label(
                                source,
                                str(operand.source.get("field") or ""),
                            ),
                        ),
                    )
                )
            else:
                resolved.append(operand)
        result = calculate_scalar(
            CalculationRequest(
                tool=request.tool,
                operation=request.operation,
                operands=tuple(resolved),
                output=request.output,
                precision=request.precision,
            )
        )
    elif request.tool == "calculate_columns":
        source_nodes = {
            str(item.source.get("node") or "")
            for item in request.operands
            if item.source
        }
        if len(source_nodes) != 1:
            return calculation_failure(
                calculate_columns(
                    request.operation, [], {}, ""
                ).failure(
                    "calculation_operand_missing",
                    "列计算的所有操作数必须来自同一个上游节点",
                    source_nodes=sorted(source_nodes),
                ),
                task,
                request,
            )
        source_node = next(iter(source_nodes))
        source = results.get(source_node)
        if source is None:
            return calculation_failure(
                calculate_columns(
                    request.operation, [], {}, ""
                ).failure(
                    "calculation_operand_missing",
                    f"缺少上游节点 {source_node}",
                ),
                task,
                request,
            )
        rows = [
            row_with_group_labels(source, row)
            for row in source.get("rows") or []
            if isinstance(row, dict)
        ]
        operands = {
            item.name: field_label(
                source,
                str(item.source.get("field") or ""),
            )
            for item in request.operands
            if item.source
        }
        output_name = request.output.name if request.output else ""
        output_label = (
            request.output.label
            if request.output and request.output.label
            else output_name
        )
        capability = default_calculation_registry().capability(
            "calculate_columns"
        )
        result = calculate_columns(
            request.operation,
            rows,
            operands,
            output_label,
            precision=request.precision,
            zero_denominator=(
                request.zero_denominator
                or (
                    capability.zero_denominator
                    if capability is not None
                    else "error"
                )
            ),
        )
    elif request.tool == "calculate_series":
        result = _calculate_series_request(request, results)
        if isinstance(result, dict):
            return result
    else:
        return calculation_failure(
            calculate_columns(
                request.operation, [], {}, ""
            ).failure(
                "calculation_tool_unknown",
                f"未注册计算工具：{request.tool}",
            ),
            task,
            request,
        )
    if not result.accepted:
        return calculation_failure(result, task, request)
    payload = result_payload(result)
    if request.output is not None:
        payload["calculation"]["output"] = {
            "name": request.output.name,
            "label": request.output.label,
            "unit": request.output.unit,
        }
    output_rows = list(payload.get("rows") or [])
    output_field = (
        request.output.name
        if request.output and request.output.name
        else "value"
    )
    output_label = (
        request.output.label
        if request.output and request.output.label
        else output_field
    )
    fallback_reply = render_calculation(
        result,
        request,
        task,
        output_label,
    )
    if request.tool in {"calculate_scalar", "calculate_series"}:
        analysis = {
            "reply": fallback_reply,
            "source": "calc",
            "engine": "post_calc",
            "accepted": bool(fallback_reply),
        }
    else:
        analysis = analyze_evidence(
            str(task.get("question") or ""),
            [
                *dependencies,
                {
                    "question": str(task.get("question") or ""),
                    "reply": "",
                    "rows": output_rows[:10],
                    "row_count": len(output_rows),
                    "compact": compact_table_evidence(output_rows),
                    "field_labels": {output_field: output_label},
                },
            ],
            requirements={
                "calculation": {
                    **payload,
                    "rows": output_rows[:10],
                    "row_count": len(output_rows),
                },
                "required_assertions": list(
                    task.get("required_assertions") or []
                ),
                "declared_metrics": list(task.get("metrics") or []),
            },
            fallback_reply=fallback_reply,
        )
    return {
        **analysis,
        "rows": output_rows,
        "columns": list(result.columns),
        "calculation": payload,
    }


def _calculate_series_request(
    request: CalculationRequest,
    results: dict[str, dict[str, Any]],
) -> Any:
    by_name = {item.name: item for item in request.operands}
    labels_operand = by_name.get("labels")
    values_operand = by_name.get("values")
    if (
        labels_operand is None
        or values_operand is None
        or not labels_operand.source
        or not values_operand.source
    ):
        return calculate_series(
            request.operation, [], []
        ).failure(
            "calculation_operand_missing",
            "序列计算需要 labels 和 values 两个来源",
        )
    labels_source = results.get(str(labels_operand.source.get("node") or ""))
    values_source = results.get(str(values_operand.source.get("node") or ""))
    if labels_source is None or values_source is None:
        return calculate_series(
            request.operation, [], []
        ).failure(
            "calculation_operand_missing",
            "序列计算的来源节点不存在",
        )
    label_field = field_label(
        labels_source,
        str(labels_operand.source.get("field") or ""),
    )
    value_field = field_label(
        values_source,
        str(values_operand.source.get("field") or ""),
    )
    labels = [
        str(row.get(label_field) or "")
        for row in labels_source.get("rows") or []
        if isinstance(row, dict)
    ]
    order_items = (
        labels_source.get("spec", {}).get("order_by") or []
        if isinstance(labels_source.get("spec"), dict)
        else []
    )
    if any(
        str(item.get("direction") or "").lower() == "desc"
        for item in order_items
        if isinstance(item, dict)
    ):
        labels.reverse()
    common_key = (
        label_field
        if labels_source is values_source and label_field != value_field
        else next(
            (
                field_label(values_source, field)
                for field in (values_source.get("field_labels") or {})
                if field_label(values_source, field)
                in {
                    field_label(labels_source, item)
                    for item in (labels_source.get("field_labels") or {})
                }
                and field_label(values_source, field) != value_field
            ),
            "",
        )
    )
    if not common_key:
        return calculate_series(
            request.operation, [], []
        ).failure(
            "calculation_operand_missing",
            "序列标签与数值没有共同关联键",
            label_fields=list(labels_source.get("field_labels") or {}),
            value_fields=list(values_source.get("field_labels") or {}),
        )
    values_by_key = {
        str(row.get(common_key) or ""): row.get(value_field)
        for row in values_source.get("rows") or []
        if isinstance(row, dict)
    }
    values = [values_by_key.get(label, 0) for label in labels]
    return calculate_series(
        request.operation,
        labels,
        values,
        precision=request.precision,
    )
