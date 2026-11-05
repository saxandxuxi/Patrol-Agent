from __future__ import annotations

import re
from typing import Any


_UNKNOWN_TYPE_PHRASES = (
    "认不出来",
    "认不出",
    "识别不出",
    "识别不了",
    "无法识别",
    "未识别",
    "未知类型",
    "类型未知",
    "没有识别",
    "没识别",
)
_PSEUDO_TYPE_VALUES = {"未知", "未知类型", "未识别", "其他", "无", "空"}
_AGG_HINTS = (
    "总数",
    "总量",
    "总记录数",
    "总数量",
    "总面积",
    "平均",
    "最大值",
    "最小值",
    "占比",
    "比例",
    "密度",
    "多少",
    "数量",
    "面积",
    "长度",
    "次数",
    "处数",
)
_TREND_HINTS = ("趋势", "增加", "减少", "上升", "下降", "变化", "环比", "同比")
_LIST_HINTS = ("列出", "明细", "清单", "哪些", "哪几个", "具体记录")


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _split_clauses(text: str) -> list[str]:
    parts = re.split(r"[；;]|(?:\s*同时\s*)|(?:\s*以及\s*)|(?:\s*并且\s*)", text)
    return [part.strip(" ，,。") for part in parts if part.strip(" ，,。")]


def _clauses_for_view(text: str) -> list[str]:
    if not text:
        return []
    connector = re.search(r"[；;]|(?:\s*同时\s*)|(?:\s*以及\s*)|(?:\s*并且\s*)", text)
    grouped = re.search(
        r"按[^，。；]{0,20}(?:分组|分别|统计|汇总)|"
        r"(?:每条|每个|各条|各个|不同)[^，。；]{0,16}",
        text,
    )
    if grouped and (connector is None or grouped.start() < connector.start()):
        return [text]
    return _split_clauses(text)


def _view_of(text: str) -> str:
    if _contains_any(text, _TREND_HINTS):
        return "trend"
    if "分组" in text or "分布" in text or bool(_group_by(text)):
        return "distribution"
    if _contains_any(text, _LIST_HINTS):
        return "list"
    if _contains_any(text, _AGG_HINTS):
        return "aggregate"
    return "data"


def _group_by(text: str) -> str:
    if re.search(r"(?:按|各|每(?:条|个)?)[^，。；]{0,8}置信度|置信度(?:区间)?分布", text):
        return "confidence"
    if re.search(
        r"(?:按|各|每(?:种|个)?|不同)[^，。；]{0,12}病害类型|"
        r"病害类型(?:分布|分组)|类型分布|每种病害",
        text,
    ):
        return "disease_type"
    if re.search(
        r"(?:按|各|每(?:条|个)?|不同)[^，。；]{0,12}(?:路线|道路|线路)|"
        r"(?:路线|道路|线路)(?:分布|分组)",
        text,
    ):
        return "route"
    if re.search(r"(?:按|各|每(?:个|月)?)[^，。；]{0,8}月|各月|按月", text):
        return "month"
    if re.search(r"(?:按|各|每(?:个)?)[^，。；]{0,8}季|按季|各季度", text):
        return "quarter"
    if re.search(r"(?:按|各|每(?:台|个)?)[^，。；]{0,8}设备|设备分布", text):
        return "device"
    if re.search(r"(?:按|各)[^，。；]{0,8}(?:上下行|方向)|方向分布", text):
        return "direction"
    if re.search(r"(?:按|各)[^，。；]{0,8}(?:巡查状态|任务状态)|状态分布", text):
        return "patrol_status"
    if re.search(r"(?:按|各)[^，。；]{0,8}(?:损坏程度|严重程度)|严重程度分布", text):
        return "damage_severity"
    return ""


def _metrics(text: str) -> list[str]:
    metrics: list[str] = []

    def add(value: str) -> None:
        if value not in metrics:
            metrics.append(value)

    if re.search(r"(?:多少|数量|总数|总量|处数|次数|条数|计数)", text):
        add("count")
    if "平均" in text:
        add("average")
    if "总面积" in text or "面积总和" in text:
        add("total_area")
    elif "面积" in text:
        add("area")
    if "长度" in text:
        add("length")
    if "密度" in text:
        add("density")
    if "占比" in text or "比例" in text or "百分比" in text:
        add("ratio")
    if "严重" in text:
        add("severity")
    return metrics


def _unknown_phrase(message: str) -> str:
    return next((word for word in _UNKNOWN_TYPE_PHRASES if word in message), "")


def _matches_known(value: str, known: list[str]) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    return [item for item in known if text == item or text in item or item in text]


def _quoted_type_candidates(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(r"['\"“”‘’]([^'\"“”‘’]{1,24})['\"“”‘’]", text):
        value = match.group(1).strip()
        prefix = text[max(0, match.start() - 16) : match.start()]
        if value and (
            any(word in prefix for word in ("类型", "病害", "标记", "枚举", "取值", "值为"))
            or value in _PSEUDO_TYPE_VALUES
        ):
            values.append(value)
    return values


def _sanitize_pseudo_types(question: str) -> str:
    text = str(question or "")
    pseudo = r"(?:未知(?:类型)?|未识别|其他|无类型)"
    patterns = (
        rf"(?:类型(?:为|是|标记为|标记成)|标记为)\s*['\"“”‘’]?{pseudo}"
        rf"(?:['\"“”‘’]?\s*(?:/|、|或|和)\s*['\"“”‘’]?{pseudo})*['\"“”‘’]?",
        rf"['\"“”‘’]?未知(?:类型)?['\"“”‘’]?\s*[/、或和]\s*['\"“”‘’]?其他['\"“”‘’]?",
    )
    for pattern in patterns:
        text = re.sub(pattern, "类型无法识别", text)
    return text


def _query_data_over_prior_result(question: str) -> bool:
    """只有“纯计算前序结果”才必须走 post_calc；按前序 ID 补查字段仍是数据查询。"""
    text = str(question or "")
    prior_pattern = (
        r"根据(?:步骤|前序)|基于[^，。；]{0,8}(?:前序查询结果|步骤)|"
        r"基于\s*(?:q|步骤)\s*\d+|前序查询结果|步骤\s*\d+"
    )
    has_prior = bool(re.search(prior_pattern, text))
    has_calc = bool(
        re.search(
            r"计算|比较|求差|求和|占比|比例|平均|合计|排序|取最大|取第一条|"
            r"最多|最少|最高|最低",
            text,
        )
    )
    query_text = re.sub(r"前序查询结果|步骤\s*\d+", "", text)
    has_new_query = bool(
        re.search(
            r"查询|补查|关联(?:查询)?|明细|记录|字段|图片|是否有",
            query_text,
        )
    )
    return has_prior and has_calc and not has_new_query


def build_step_contract(
    original_message: str,
    step: dict[str, Any],
    *,
    known_damage_types: list[str] | None = None,
) -> dict[str, Any]:
    step_type = str(step.get("type") or "data")
    question = str(step.get("question") or "")
    original = str(original_message or "")
    known = [str(item) for item in known_damage_types or [] if str(item).strip()]

    if step_type == "post_calc":
        return {
            "view": "post_calc",
            "question": question,
            "clauses": [],
            "group_by": "",
            "metrics": [],
            "conditions": [],
            "provenance": [],
            "required_outputs": [],
            "violations": [],
            "status": "ok",
        }
    if step_type in {"analysis", "knowledge", "image"}:
        view = "analysis" if step_type == "analysis" else step_type
        return {
            "view": view,
            "question": question,
            "clauses": [{"text": question, "view": view}],
            "group_by": "",
            "metrics": [],
            "conditions": [],
            "provenance": [],
            "required_outputs": [],
            "violations": [],
            "status": "ok",
        }

    clauses = [
        {"text": clause, "view": _view_of(clause)}
        for clause in (_clauses_for_view(question) or [question])
    ]
    views = [str(item["view"]) for item in clauses]
    view = views[0] if len(set(views)) == 1 else "mixed"
    violations: list[dict[str, str]] = []
    if view == "mixed":
        violations.append(
            {
                "code": "mixed_views",
                "message": (
                    "同一步同时包含多个不同视图："
                    + "、".join(dict.fromkeys(views))
                    + "。必须拆成独立子查询。"
                ),
            }
        )
    if re.match(r"^\s*(?:SELECT|WITH)\b", question, re.I):
        violations.append(
            {
                "code": "sql_in_planner_step",
                "message": "规划步骤只能写自然语言子问题，不能直接写 SQL。",
            }
        )
    if step_type == "data" and _query_data_over_prior_result(question):
        violations.append(
            {
                "code": "query_data_over_prior_result",
                "message": (
                    "这一步只基于前序结果做计算，必须使用 post_calc 并声明依赖，"
                    "不能重新查库。"
                ),
            }
        )
    if (
        re.search(r"\blimit\s+1\b", question, re.I)
        and re.search(r"哪些|分别|每种|每条|各个|各多少", original)
    ):
        violations.append(
            {
                "code": "premature_limit",
                "message": "用户要求列出多个分组，规划步骤不能提前 LIMIT 1。",
            }
        )

    provenance: list[dict[str, str]] = []
    seen_values: set[str] = set()
    for name in known:
        if name in question:
            seen_values.add(name)
            provenance.append(
                {
                    "value": name,
                    "canonical": name,
                    "source": "user" if name in original else "model_inference",
                    "resolved_as": name,
                }
            )
    for value in _quoted_type_candidates(question):
        if value in seen_values:
            continue
        seen_values.add(value)
        if value in _PSEUDO_TYPE_VALUES:
            provenance.append(
                {
                    "value": value,
                    "canonical": "",
                    "source": "user" if value in original else "model_inference",
                    "resolved_as": "unknown_type",
                }
            )
            continue
        matches = _matches_known(value, known)
        if matches:
            provenance.append(
                {
                    "value": value,
                    "canonical": matches[0],
                    "source": "user" if value in original else "model_inference",
                    "resolved_as": matches[0],
                }
            )
            continue
        if not known:
            message = f"病害类型字典不可用，模型推断值“{value}”不能进入 SQL"
            code = "enum_dictionary_unavailable"
        else:
            message = f"模型推断的病害类型“{value}”不在数据库字典中"
            code = "unresolved_enum"
        provenance.append(
            {
                "value": value,
                "canonical": "",
                "source": "model_inference",
                "resolved_as": "",
            }
        )
        violations.append({"code": code, "message": message})

    conditions: list[dict[str, str]] = []
    unknown_text = _unknown_phrase(original)
    if unknown_text or any(
        item["resolved_as"] == "unknown_type" for item in provenance
    ):
        conditions.append(
            {
                "field": "unknown_type",
                "source": "user",
                "text": unknown_text or "未识别类型",
            }
        )

    sanitized = _sanitize_pseudo_types(question)
    metrics: list[str] = []
    for clause in clauses:
        for metric in _metrics(str(clause["text"])):
            if metric not in metrics:
                metrics.append(metric)
    group_by = _group_by(question)
    required_outputs: list[str] = []
    if view == "distribution":
        required_outputs.append("group_label")
        if metrics:
            required_outputs.append("aggregate_value")
    elif view in {"aggregate", "trend"}:
        required_outputs.append("aggregate_value")
    elif view == "list":
        required_outputs.append("record")

    return {
        "view": view,
        "question": sanitized,
        "clauses": clauses,
        "group_by": group_by,
        "metrics": metrics,
        "conditions": conditions,
        "provenance": provenance,
        "required_outputs": required_outputs,
        "violations": violations,
        "status": "invalid" if violations else "ok",
    }


def _is_number(value: Any) -> bool:
    return bool(
        re.fullmatch(
            r"\s*-?(?:\d+(?:\.\d+)?|\.\d+)\s*",
            str(value if value is not None else ""),
        )
    )


def contract_issue_for_result(
    contract: dict[str, Any],
    rows: list[dict[str, Any]],
    columns: list[str],
) -> dict[str, str] | None:
    view = str(contract.get("view") or "")
    if view not in {"distribution", "aggregate", "trend"} or not rows:
        return None
    names = [str(item) for item in columns]
    numeric_columns = {
        name
        for name in names
        if any(_is_number(row.get(name)) for row in rows if isinstance(row, dict))
    }
    label_columns = [name for name in names if name not in numeric_columns]
    if view == "distribution" and not label_columns:
        return {
            "code": "missing_group_label",
            "message": "查询结果为分组分布，但缺少分组标签列。",
        }
    if view == "distribution" and label_columns:
        has_real_label = any(
            str(row.get(name) or "").strip().upper() not in {"", "NULL", "NONE"}
            for row in rows
            if isinstance(row, dict)
            for name in label_columns
        )
        if not has_real_label:
            return {
                "code": "unusable_group_label",
                "message": "分组标签全部为空或 NULL，不能回答“分别/哪些”类问题。",
            }
    if view in {"distribution", "aggregate", "trend"} and not numeric_columns:
        return {
            "code": "missing_aggregate_value",
            "message": "查询结果缺少可解析的数值指标列。",
        }
    if view == "trend":
        time_columns = [
            name
            for name in label_columns
            if any(hint in name for hint in ("月", "日", "年", "周", "季", "时间", "日期"))
        ]
        if not time_columns:
            return {
                "code": "missing_time_bucket",
                "message": "趋势查询缺少时间分组列。",
            }
    return None
