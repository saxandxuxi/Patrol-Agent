from __future__ import annotations

from decimal import Decimal
from typing import Any

from .columns import calculate_columns
from .domain.analysis import analyze_evidence
from .domain.comparison import compare_datasets
from .evidence import row_with_group_labels
from .models import CalculationOperand, CalculationRequest
from .numbers import numeric_value
from .scalar import calculate_scalar
from .serialization import json_value, result_payload
from .series import calculate_series
from .registry import default_calculation_registry
from ..services import react_repair


def _dependency_map(
    results: dict[int | str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        str(key): value
        for key, value in (results or {}).items()
    }


def _field_label(dependency: dict[str, Any], field: str) -> str:
    labels = dependency.get("field_labels") or {}
    return str(labels.get(field) or field)


def _request_from_task(task: dict[str, Any]) -> CalculationRequest | None:
    value = task.get("calculation")
    if not isinstance(value, dict):
        return None
    return CalculationRequest.from_dict(value)


def _calculation_failure(result: Any, task: dict[str, Any]) -> dict[str, Any]:
    failure = react_repair.build_failure(
        result.failure_code or "calculation_output_invalid",
        stage="post_calc",
        reason=result.reason or "计算没有返回有效结果",
        evidence={
            "step_id": task.get("id"),
            "step_question": str(task.get("question") or ""),
            "calculation": result.evidence,
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


def _execute_calculation(
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
                    return _calculation_failure(
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
                    )
                resolved.append(
                    CalculationOperand(
                        name=operand.name,
                        value=numeric_value(
                            source,
                            _field_label(
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
            fallback = calculate_columns(
                request.operation,
                [],
                {},
                request.output.name if request.output else "",
            )
            return _calculation_failure(
                fallback.failure(
                    "calculation_operand_missing",
                    "列计算的所有操作数必须来自同一个上游节点",
                    source_nodes=sorted(source_nodes),
                ),
                task,
            )
        source_node = next(iter(source_nodes))
        source = results.get(source_node)
        if source is None:
            return _calculation_failure(
                calculate_columns(
                    request.operation, [], {}, ""
                ).failure(
                    "calculation_operand_missing",
                    f"缺少上游节点 {source_node}",
                ),
                task,
            )
        rows = [
            row_with_group_labels(source, row)
            for row in source.get("rows") or []
            if isinstance(row, dict)
        ]
        operands = {
            item.name: _field_label(
                source,
                str(item.source.get("field") or ""),
            )
            for item in request.operands
            if item.source
        }
        output_name = request.output.name if request.output else ""
        output_label = (
            request.output.label if request.output and request.output.label
            else output_name
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
                    default_calculation_registry()
                    .capability("calculate_columns")
                    .zero_denominator
                )
            ),
        )
    elif request.tool == "calculate_series":
        by_name = {item.name: item for item in request.operands}
        labels_operand = by_name.get("labels")
        values_operand = by_name.get("values")
        if (
            labels_operand is None
            or values_operand is None
            or not labels_operand.source
            or not values_operand.source
        ):
            return _calculation_failure(
                calculate_series(
                    request.operation, [], []
                ).failure(
                    "calculation_operand_missing",
                    "序列计算需要 labels 和 values 两个来源",
                ),
                task,
            )
        labels_source = results.get(str(labels_operand.source.get("node") or ""))
        values_source = results.get(str(values_operand.source.get("node") or ""))
        if labels_source is None or values_source is None:
            return _calculation_failure(
                calculate_series(
                    request.operation, [], []
                ).failure(
                    "calculation_operand_missing",
                    "序列计算的来源节点不存在",
                ),
                task,
            )
        label_field = _field_label(
            labels_source,
            str(labels_operand.source.get("field") or ""),
        )
        value_field = _field_label(
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
        common_key = next(
            (
                _field_label(values_source, field)
                for field in (values_source.get("field_labels") or {})
                if _field_label(values_source, field) in {
                    _field_label(labels_source, item)
                    for item in (labels_source.get("field_labels") or {})
                }
                and _field_label(values_source, field) != value_field
            ),
            "",
        )
        values_by_key = {
            str(row.get(common_key) or ""): row.get(value_field)
            for row in values_source.get("rows") or []
            if isinstance(row, dict)
        } if common_key else {}
        values = [
            values_by_key.get(label, 0)
            for label in labels
        ]
        result = calculate_series(
            request.operation,
            labels,
            values,
            precision=request.precision,
        )
    else:
        return _calculation_failure(
            calculate_columns(
                request.operation, [], {}, ""
            ).failure(
                "calculation_tool_unknown",
                f"未注册计算工具：{request.tool}",
            ),
            task,
        )
    if not result.accepted:
        return _calculation_failure(result, task)
    payload = result_payload(result)
    output_rows = list(payload.get("rows") or [])
    output_field = (
        request.output.name if request.output and request.output.name
        else "value"
    )
    output_label = (
        request.output.label
        if request.output and request.output.label
        else output_field
    )
    requirements = {
        "calculation": payload,
        "required_assertions": list(task.get("required_assertions") or []),
        "declared_metrics": list(task.get("metrics") or []),
    }
    fallback_reply = _calculation_fallback(
        result,
        request,
        output_label,
        task,
    )
    if request.tool in {"calculate_scalar", "calculate_series"}:
        analysis = {
            "reply": fallback_reply,
            "source": "calc",
            "engine": "post_calc",
            "accepted": bool(fallback_reply),
        }
    else:
        calculation_dependency = {
            "question": str(task.get("question") or ""),
            "reply": "",
            "rows": output_rows,
            "field_labels": {
                output_field: output_label,
            },
        }
        analysis = analyze_evidence(
            str(task.get("question") or ""),
            [*dependencies, calculation_dependency],
            requirements=requirements,
            fallback_reply=fallback_reply,
        )
    return {
        **analysis,
        "rows": output_rows,
        "columns": list(result.columns),
        "calculation": payload,
    }


def _calculation_fallback(
    result: Any,
    request: CalculationRequest,
    output_field: str,
    task: dict[str, Any],
) -> str:
    if request.tool == "calculate_scalar":
        label = (
            request.output.label
            if request.output and request.output.label
            else request.output.name
            if request.output
            else "计算结果"
        )
        return f"{label} = {result.value}（{result.formula}）。"
    if request.tool == "calculate_series":
        points = "、".join(
            f"{row.get('label')}={row.get('value')}"
            for row in result.rows
        )
        change_name = (
            "difference"
            if request.operation == "subtract"
            else "percentage_change"
        )
        change_unit = (
            request.output.unit
            if request.output and request.output.unit
            else ""
        )
        changes = "，".join(
            _format_change(row.get(change_name))
            for row in result.rows[1:]
        )
        if change_unit:
            changes = "，".join(
                f"{_format_change(row.get(change_name))}{change_unit}"
                for row in result.rows[1:]
            )
        if not points:
            return "序列计算没有可用数据。"
        changes_values = [
            row.get(change_name)
            for row in result.rows[1:]
            if row.get(change_name) is not None
        ]
        if changes_values and all(value <= 0 for value in changes_values):
            direction = "整体下降"
        elif changes_values and all(value >= 0 for value in changes_values):
            direction = "整体上升"
        else:
            direction = "有升有降"
        return (
            f"{direction}。序列为：{points}。相邻变化为：{changes}。"
        )
    values = [
        row
        for row in result.rows
        if row.get(output_field) is not None
    ]
    if not values:
        return "列计算没有可用结果。"
    lower_is_better = "lower_is_better" in {
        str(item) for item in task.get("required_assertions") or ()
    }
    selected = (
        min(values, key=lambda row: row[output_field])
        if lower_is_better
        else max(values, key=lambda row: row[output_field])
    )
    label_field = next(
        (
            key
            for key, value in selected.items()
            if key != output_field
            and value not in (None, "")
            and not isinstance(value, (int, float))
        ),
        "",
    )
    label = (
        f"{label_field}={selected.get(label_field)}，"
        if label_field
        else ""
    )
    unit = (
        request.output.unit
        if request.output and request.output.unit
        else ""
    )
    excluded = list(result.evidence.get("excluded_row_ids") or [])
    exclusion_note = (
        f"；已排除 {len(excluded)} 个零长度行："
        + "、".join(excluded[:5])
        if excluded
        else ""
    )
    return (
        f"已计算 {len(values)} 行；"
        f"{label}{request.output.label if request.output else output_field}"
        f"={selected.get(output_field)}{unit}{exclusion_note}。"
    )


def _format_change(value: Any) -> str:
    if value is None:
        return "无"
    number = value if isinstance(value, Decimal) else Decimal(str(value))
    text = format(number.normalize(), "f")
    return f"+{text}" if number > 0 else text


def execute_post_calc(
    task: dict[str, Any],
    results: dict[int | str, dict[str, Any]],
) -> dict[str, Any]:
    normalized_results = _dependency_map(results)
    request = _request_from_task(task)
    if request is not None:
        return _execute_calculation(task, request, normalized_results)
    dependencies = [
        normalized_results[str(item)]
        for item in task.get("depends_on") or ()
        if str(item) in normalized_results
    ]
    if str(task.get("required_assertions") or ()) and (
        "dataset_comparison"
        in {
            str(item)
            for item in task.get("required_assertions") or ()
        }
    ):
        comparison = compare_datasets(dependencies)
        if comparison:
            return comparison
    analyzed = analyze_evidence(
        str(task.get("question") or ""),
        dependencies,
        requirements={
            "required_assertions": list(task.get("required_assertions") or []),
            "declared_metrics": list(task.get("metrics") or []),
        },
    )
    if analyzed.get("accepted"):
        return analyzed
    failure = react_repair.build_failure(
        "post_calc_empty",
        stage="post_calc",
        reason="后置计算没有返回可用结果",
        evidence={
            "step_id": task.get("id"),
            "step_question": str(task.get("question") or ""),
            "depends_on": list(task.get("depends_on") or []),
        },
    )
    return {
        "reply": "这一步需要基于前面的结果做计算，但数据不足，算不出来。",
        "source": "error",
        "engine": "post_calc",
        "accepted": False,
        "failure": failure,
        "columns": [],
        "rows": [],
    }
