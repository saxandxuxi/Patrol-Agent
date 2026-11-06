from __future__ import annotations

from typing import Any

from .domain.analysis import analyze_evidence
from .domain.comparison import compare_datasets
from .executor import (
    dependency_map,
    execute_calculation,
    request_from_task,
)
from ..services import react_repair


def execute_post_calc(
    task: dict[str, Any],
    results: dict[int | str, dict[str, Any]],
) -> dict[str, Any]:
    normalized_results = dependency_map(results)
    try:
        request = request_from_task(task)
    except (TypeError, ValueError) as exc:
        failure = react_repair.build_failure(
            "calculation_output_invalid",
            stage="post_calc",
            reason=str(exc),
            evidence={"step_id": task.get("id")},
        )
        return _failure_payload(failure)
    if request is not None:
        return execute_calculation(task, request, normalized_results)
    dependencies = [
        normalized_results[str(item)]
        for item in task.get("depends_on") or ()
        if str(item) in normalized_results
    ]
    if (
        "dataset_comparison"
        in {
            str(item)
            for item in task.get("required_assertions") or ()
        }
    ):
        comparison = compare_datasets(dependencies)
        if comparison:
            return comparison
    if "analysis_only" not in {
        str(item) for item in task.get("required_assertions") or ()
    }:
        failure = react_repair.build_failure(
            "calculation_required",
            stage="post_calc",
            reason=(
                "后置计算必须声明 calculation 工具和参数；"
                "只解释已有值时声明 analysis_only"
            ),
            evidence={
                "step_id": task.get("id"),
                "view": task.get("view"),
            },
        )
        return _failure_payload(failure)
    analyzed = analyze_evidence(
        str(task.get("question") or ""),
        dependencies,
        requirements={
            "required_assertions": list(
                task.get("required_assertions") or []
            ),
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
    return _failure_payload(failure)


def _failure_payload(failure: dict[str, Any]) -> dict[str, Any]:
    return {
        "reply": str(failure.get("reason") or "后置计算失败"),
        "source": "error",
        "engine": "post_calc",
        "accepted": False,
        "failure": failure,
        "columns": [],
        "rows": [],
    }
