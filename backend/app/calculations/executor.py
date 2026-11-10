from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from .columns import calculate_columns
from .datetime_interval import calculate_datetime_interval
from .domain.analysis import analyze_evidence
from .domain.proportion import is_proportion_calculation
from .domain.rendering import render_calculation
from .evidence import compact_table_evidence, row_with_group_labels
from .models import CalculationOperand, CalculationRequest
from .numbers import as_decimal, numeric_value
from .registry import default_calculation_registry
from .scalar import calculate_scalar
from .serialization import json_value, result_payload
from .series import calculate_series
from ..services import react_repair


def dependency_map(
    results: dict[int | str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {str(key): value for key, value in (results or {}).items()}


def _field_tokens(value: Any) -> set[str]:
    text = "".join(str(value or "").split()).lower()
    if not text:
        return set()
    tail = text.rsplit(".", 1)[-1]
    return {text, tail} if tail != text else {text}


def field_label(dependency: dict[str, Any], field: str) -> str:
    labels = dependency.get("field_labels") or {}
    mapped = labels.get(field)
    if mapped:
        return str(mapped)
    wanted = _field_tokens(field)
    if wanted:
        for key, column in labels.items():
            if column and _field_tokens(key) & wanted:
                return str(column)
        for column in labels.values():
            if column and _field_tokens(column) & wanted:
                return str(column)
    return str(field)


ROW_VIEWS = frozenset(
    {
        "distribution",
        "trend",
        "list",
        "detail",
        "top_groups",
        "bucket_compare",
    }
)


def _declared_view(source: dict[str, Any]) -> str:
    view = str(source.get("view") or "")
    if view:
        return view
    spec = source.get("spec")
    return str(spec.get("view") or "") if isinstance(spec, dict) else ""


def _operand_caption(source: dict[str, Any]) -> str:
    """给计算操作数起个人能看懂的名字：优先用上游节点的分组条件，其次用它的问法。"""
    labels = source.get("group_labels")
    if isinstance(labels, Mapping):
        parts = [
            str(value).strip()
            for value in labels.values()
            if str(value).strip()
        ]
        if parts:
            return "、".join(dict.fromkeys(parts))
    return str(source.get("question") or "").strip()


def _selected_row_source(
    source: dict[str, Any],
    operand: CalculationOperand,
) -> tuple[dict[str, Any] | None, str]:
    raw_index = operand.source.get("row_index")
    if raw_index is None:
        return source, ""
    if isinstance(raw_index, bool) or not isinstance(raw_index, int):
        return None, "row_index 必须是从 0 开始的整数"
    rows = [
        row
        for row in source.get("rows") or []
        if isinstance(row, dict)
    ]
    if raw_index < 0 or raw_index >= len(rows):
        return (
            None,
            f"row_index={raw_index} 超出上游结果范围（共 {len(rows)} 行）",
        )
    row = rows[raw_index]
    metric_label = field_label(
        source,
        str(operand.source.get("field") or ""),
    )
    spec = source.get("spec")
    dimensions = (
        tuple(spec.get("dimensions") or ())
        if isinstance(spec, Mapping)
        else ()
    )
    if dimensions:
        group_labels = {
            label: row[label]
            for field in dimensions
            if (label := field_label(source, str(field))) in row
            and row[label] not in (None, "")
        }
    else:
        # 没有结构元数据时只把非数值列当作分组条件。面积、长度等其他
        # 数值指标是同一行的并列指标，不能混进“路线 A、70”这样的操作数名称。
        group_labels = {
            str(key): value
            for key, value in row.items()
            if str(key) != metric_label
            and value not in (None, "")
            and as_decimal(value) is None
        }
    selected = dict(source)
    selected["rows"] = [row]
    selected["group_labels"] = group_labels
    return selected, ""


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
        "calculate_datetime_interval": "temporal_scalar_list",
    }.get(tool, "")


def calculation_failure(
    result: Any,
    task: dict[str, Any],
    request: CalculationRequest,
    operand_labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    labels = operand_labels or {}
    evidence = {
        "tool": request.tool,
        "operation": request.operation,
        "input_shape": input_shape(request.tool),
        "operands": [
            {
                "name": item.name,
                "value": json_value(item.value),
                "source": dict(item.source),
                **(
                    {"label": str(labels[item.name])}
                    if item.name in labels
                    else {}
                ),
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


def _temporal_operand(
    source: dict[str, Any] | None,
    operand: CalculationOperand,
) -> tuple[Any, str, str]:
    if source is None:
        return (
            None,
            "",
            f"缺少上游节点 {operand.source.get('node')}",
        )
    selected, source_error = _selected_row_source(source, operand)
    if selected is None:
        return None, "", source_error
    rows = [
        row
        for row in selected.get("rows") or []
        if isinstance(row, dict)
    ]
    if len(rows) != 1:
        return (
            None,
            "",
            "日期差计算的操作数必须从上游结果中确定唯一一行",
        )
    field = field_label(
        selected,
        str(operand.source.get("field") or ""),
    )
    if field not in rows[0]:
        return (
            None,
            "",
            f"日期字段 {field} 不在上游结果中",
        )
    return rows[0].get(field), _operand_caption(selected), ""


def execute_calculation(
    task: dict[str, Any],
    request: CalculationRequest,
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    dependency_ids = [str(item) for item in task.get("depends_on") or ()]
    dependencies = [
        results[item] for item in dependency_ids if item in results
    ]
    operand_labels: dict[str, str] = {}
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
                selected_source, source_error = _selected_row_source(
                    source,
                    operand,
                )
                if selected_source is None:
                    return calculation_failure(
                        calculate_scalar(
                            CalculationRequest(
                                tool=request.tool,
                                operation=request.operation,
                                operands=(),
                            )
                        ).failure(
                            "calculation_operand_missing",
                            source_error,
                            operand=operand.name,
                        ),
                        task,
                        request,
                    )
                resolved.append(
                    CalculationOperand(
                        name=operand.name,
                        value=numeric_value(
                            selected_source,
                            field_label(
                                selected_source,
                                str(operand.source.get("field") or ""),
                            ),
                        ),
                    )
                )
                caption = _operand_caption(selected_source)
                if caption:
                    operand_labels[operand.name] = caption
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
    elif request.tool == "calculate_datetime_interval":
        resolved: list[CalculationOperand] = []
        for operand in request.operands:
            if not operand.source:
                resolved.append(operand)
                continue
            value, caption, source_error = _temporal_operand(
                results.get(str(operand.source.get("node") or "")),
                operand,
            )
            if source_error:
                return calculation_failure(
                    calculate_datetime_interval(
                        CalculationRequest(
                            tool=request.tool,
                            operation=request.operation,
                            operands=(),
                        )
                    ).failure(
                        "calculation_operand_missing",
                        source_error,
                        operand=operand.name,
                    ),
                    task,
                    request,
                )
            resolved.append(
                CalculationOperand(name=operand.name, value=value)
            )
            if caption:
                operand_labels[operand.name] = caption
        result = calculate_datetime_interval(
            CalculationRequest(
                tool=request.tool,
                operation=request.operation,
                operands=tuple(resolved),
                output=request.output,
                precision=request.precision,
            )
        )
    elif request.tool == "calculate_columns":
        source_operands = [
            item for item in request.operands if item.source
        ]
        source_nodes = {
            str(item.source.get("node") or "")
            for item in source_operands
        }
        source_results = {
            node_id: results.get(node_id)
            for node_id in source_nodes
        }
        row_nodes = {
            node_id
            for node_id, source in source_results.items()
            if source is not None
            and (
                _declared_view(source) in ROW_VIEWS
                or len(source.get("rows") or []) > 1
            )
        }
        if len(row_nodes) != 1:
            return calculation_failure(
                calculate_columns(
                    request.operation, [], {}, ""
                ).failure(
                    "calculation_operand_missing",
                    (
                        "列计算必须且只能有一个多行来源节点；"
                        f"当前来源为 {sorted(row_nodes) or '空'}。"
                        "两个多行节点做逐组对比请改用 calculate_series，"
                        "labels 取分组键字段，values 取另一节点的数值字段。"
                    ),
                    source_nodes=sorted(row_nodes),
                ),
                task,
                request,
            )
        source_node = next(iter(row_nodes))
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
        operands: dict[str, str] = {}
        constants: dict[str, Any] = {}
        numerator_column = ""
        for item in source_operands:
            node_id = str(item.source.get("node") or "")
            field = str(item.source.get("field") or "")
            if node_id == source_node:
                column = field_label(source, field)
                operands[item.name] = column
                if item.name == "numerator":
                    numerator_column = column
                continue
            upstream = results.get(node_id)
            if upstream is None:
                return calculation_failure(
                    calculate_columns(
                        request.operation, [], {}, ""
                    ).failure(
                        "calculation_operand_missing",
                        f"缺少常量来源节点 {node_id}",
                    ),
                    task,
                    request,
                )
            constants[item.name] = numeric_value(
                upstream,
                field_label(upstream, field),
            )
        if request.operation == "percentage_share" and numerator_column:
            has_total_operand = any(
                item.name == "total" for item in request.operands
            )
            # 分母要么指向同一列（自己算总和），要么模型压根没给：
            # 这一列的合计能确定算出来，就不该把整步让给“缺少计算分母”。
            if (
                operands.get("total") == numerator_column
                or not has_total_operand
            ):
                total = Decimal("0")
                for row in rows:
                    value = as_decimal(row.get(numerator_column))
                    if value is not None:
                        total += value
                constants["total"] = total
                constants.setdefault("total_source", "sum_of_groups")
                operands.setdefault("total", numerator_column)
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
            constants=constants,
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
        return calculation_failure(
            result,
            task,
            request,
            operand_labels,
        )
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
        operand_labels or None,
    )
    if is_proportion_calculation(request):
        analysis = {
            "reply": fallback_reply,
            "source": "calc",
            "engine": "post_calc",
            "accepted": bool(fallback_reply),
        }
    elif request.tool in {
        "calculate_scalar",
        "calculate_series",
        "calculate_datetime_interval",
    }:
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
                    item
                    for item in task.get("required_assertions") or []
                    if str(item) != "analysis_only"
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
    label_rows = [
        row
        for row in labels_source.get("rows") or []
        if isinstance(row, dict)
    ]
    label_keys = {
        str(key) for row in label_rows for key in row
    }
    if label_rows and label_field not in label_keys:
        return calculate_series(
            request.operation, [], []
        ).failure(
            "calculation_operand_missing",
            f"标签字段 {label_field} 不在上游结果中",
            requested_field=str(labels_operand.source.get("field") or ""),
            available_columns=sorted(label_keys),
        )
    labels = [
        str(row.get(label_field) or "")
        for row in label_rows
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
    requested_key = str(labels_operand.source.get("field") or "")
    value_keys = {
        str(key)
        for row in values_source.get("rows") or []
        if isinstance(row, dict)
        for key in row
    }
    resolved_key = (
        field_label(values_source, requested_key)
        if requested_key
        else ""
    )
    common_key = (
        resolved_key
        if resolved_key
        and resolved_key in value_keys
        and resolved_key != value_field
        else ""
    )
    if not common_key:
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
