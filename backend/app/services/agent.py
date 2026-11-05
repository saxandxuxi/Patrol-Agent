from __future__ import annotations

import json
import os
import time
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from ..config import settings
from ..store import (
    add_conversation_message,
    add_history,
    add_retrieval_log,
    ensure_conversation,
    get_result,
    save_result,
)
from . import analysis as analysis_service
from . import calibrate
from . import capabilities
from . import chat_settings
from . import clarify
from . import cross_db
from . import event_bus
from . import intent as intent_service
from . import memory as memory_service
from . import metadata_service
from . import nl2sql
from . import orchestrator
from . import prompts
from . import query_rewrite
from . import react_repair
from . import sql_engine
from . import wren_engine
from .db import DatabaseError, db_manager


def _is_batch_run() -> bool:
    """这次调用是不是跑批/回归（不是真实用户提问）。

    跑批不能往用户的表里写东西：`run_regression.py` 会把每个用例跑很多遍，
    以前每次都写 `query_history` / 会话 / 检索日志——实测"看一下病害样图"被写进去几十遍，
    用户在前端历史里看到一堆重复（`CODEX_REGRESSION=1` 由回归脚本设置）。
    """
    return bool(os.environ.get("CODEX_REGRESSION"))

# 弹卡前的"意图分类 + 语义规划"并行跑用的小线程池（只这两个纯函数，不碰数据库连接）
_PARALLEL_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-parallel")
from .llm import chat, chat_detail, chat_json, role_model
from .logging_service import log_error, log_event
from .rag import RAGError, find_dataset, retrieve


def _rag_context(message: str, dataset_names: list[str]) -> tuple[str, list[dict[str, Any]]]:
    dataset_ids: list[str] = []
    for name in dataset_names:
        try:
            dataset = find_dataset(name)
        except RAGError:
            # RAGFlow 是知识增强，不是业务数据前置条件；挂掉时数据库查询仍应继续，
            # 只让规范/示例召回降级为空。
            continue
        if dataset and dataset.get("id"):
            dataset_ids.append(str(dataset["id"]))
    if not dataset_ids:
        return "", []
    use_kg = any(name in settings.ragflow_graphrag_datasets for name in dataset_names)
    try:
        chunks = retrieve(query_rewrite.rewrite(message), dataset_ids, use_kg=use_kg)
    except RAGError:
        if not use_kg:
            return "", []
        # GraphRAG（知识图谱检索）不可用时回退普通向量检索。
        try:
            chunks = retrieve(query_rewrite.rewrite(message), dataset_ids)
        except RAGError:
            return "", []
    if len(dataset_names) == 1:
        for chunk in chunks:
            chunk["dataset_name"] = dataset_names[0]
    context = "\n\n".join(
        [f"【{chunk.get('document_keyword', '知识')}】{chunk.get('content', '')}" for chunk in chunks]
    )
    return context, chunks


def _knowledge_datasets(message: str) -> list[str]:
    """知识问答检索全部已配置知识库；主题判断交给检索与语义模型。"""
    names = [settings.knowledge_default_dataset]
    for name in settings.knowledge_measure_datasets:
        if name not in names:
            names.append(name)
    return names


# 系统配置问答是固定事实，不属于业务实体识别；只在明确问“系统/平台连接了哪些
# 数据源”时直接答配置，避免检索到规范中的“应建立数据库”。
_DATASOURCE_ASK = ("数据源", "数据库", "连了", "连接了", "存储")
_DATASOURCE_SCOPE = ("系统", "平台", "哪几个", "哪些", "几个", "都有哪些")


def _datasource_answer(message: str) -> str:
    if not any(word in message for word in _DATASOURCE_ASK):
        return ""
    if not any(word in message for word in _DATASOURCE_SCOPE):
        return ""
    return "\n".join(
        [
            "系统接了 3 个数据源：",
            f"1. **MySQL `{settings.mysql_database}`**：主业务库，存病害、路线台账、巡查任务、"
            "路段、资产等业务数据（NL2SQL 查询走这里）。",
            f"2. **PostgreSQL `{settings.pg_database}`**：GPS 轨迹库和倾角传感器数据（当前两张表都是空的）。",
            "3. **SeaweedFS 对象存储**：病害图片、巡检照片等非结构化数据，数据库里只存图片路径。",
            "另有 RAGFlow 知识库用于养护规范/工程量文件检索，但它不是业务数据源。",
        ]
    )


def _answer_knowledge(message: str) -> dict[str, Any]:
    datasource = _datasource_answer(message)
    if datasource:
        return {
            "reply": datasource,
            "source": "config",
            "references": [],
            "lineage": {"datasets": [], "documents": [], "chunks": 0},
        }
    datasets = _knowledge_datasets(message)
    context, chunks = _rag_context(message, datasets)
    if not context:
        return {
            "reply": "当前养护知识库还没有可检索内容，或 RAGFlow 未配置完成。",
            "source": "ragflow",
            "references": [],
            "lineage": {"datasets": datasets, "documents": [], "chunks": 0},
        }
    options = chat_settings.load()
    reply, thinking = chat_detail(
        prompts.messages("knowledge", context=context[:4000], message=message),
        temperature=0,
        model=role_model("knowledge"),
        enable_thinking=options["enable_thinking"],
        enable_search=options["enable_search"],
        max_tokens=settings.knowledge_max_tokens,
    )
    documents = sorted({chunk.get("document_keyword", "") for chunk in chunks if chunk.get("document_keyword")})
    return {
        "reply": reply,
        "thinking": thinking,
        "source": "ragflow",
        "references": chunks,
        "lineage": {
            "datasets": datasets,
            "documents": documents,
            "chunks": len(chunks),
        },
    }


def _is_zero_result(result: dict[str, Any]) -> bool:
    """结果为 0 行、或计数结果全是 0。"""
    rows = result.get("rows") or []
    if not rows:
        return True
    values: list[float] = []
    all_empty = True
    has_non_numeric = False
    for row in rows:
        for value in row.values():
            if value in (None, "") or str(value).strip().upper() == "NULL":
                continue
            all_empty = False
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                has_non_numeric = True
    if not values:
        # 整行都是 NULL：空表做聚合就是这样（`COUNT/SUM over empty set` → NULL）。
        # 这属于"没有数据"，不是"结构解读不了"，必须当零结果走诚实回答分支。
        return all_empty and not has_non_numeric
    return all(value == 0 for value in values)


def _all_values_null(result: dict[str, Any]) -> bool:
    """结果里每个格子都是 NULL/空串（空表聚合的典型形状）。"""
    rows = result.get("rows") or []
    if not rows:
        return False
    for row in rows:
        for value in row.values():
            # 注意别写成 `value or ""`：0 会被 or 吃成空串，把"计数=0"误判成"没数据"
            if str(value).strip().upper() not in {"", "NULL", "NONE"}:
                return False
    return True


def _has_null_value(result: dict[str, Any]) -> bool:
    """结果里有没有 NULL 格子（空表做聚合时常见）。"""
    for row in result.get("rows") or []:
        for value in row.values():
            if str(value).strip().upper() in {"NULL", "NONE"}:
                return True
    return False


def _overview_summary(rows: list[dict[str, Any]]) -> str:
    """「整体情况」的汇总：条数 / 面积合计 / 平均 / 最大并列，全部由结果行直接算。"""
    areas = [
        float(row["area"])
        for row in rows
        if row.get("area") not in (None, "", "NULL")
    ]
    if not areas:
        return f"共 {len(rows)} 条明细记录，请在结果工作台查看表格与图表。"
    total = sum(areas)
    average = total / len(areas)
    top = max(areas)
    tied = [row for row in rows if row.get("area") not in (None, "", "NULL") and abs(float(row["area"]) - top) < 1e-9]
    piles = sorted({str(row.get("pile_number") or "") for row in tied if row.get("pile_number")})
    tail = "" if len(piles) <= 12 else f" 等 {len(piles)} 个桩号"
    return (
        f"共 {len(rows)} 处，总面积 {total:.2f}㎡，平均 {average:.4f}㎡；"
        f"最大 {top:g}㎡，并列 {len(tied)} 条 / {len(piles)} 个桩号"
        f"（{'、'.join(piles[:12])}{tail}）。"
    )


_UNREGISTERED_ROUTE_LABELS = {
    "",
    "NULL",
    "NONE",
    "未知",
    "未知路线",
    "未登记",
    "未登记路线",
}


def _patrol_route_display(row: dict[str, Any]) -> str:
    """路线展示值：编码 + 全称；占位路线明确标成“未登记路线”。"""
    code = str(row.get("route_code") or "").strip()
    name = str(row.get("route_name") or "").strip()
    if (code.upper() in _UNREGISTERED_ROUTE_LABELS and name.upper() in _UNREGISTERED_ROUTE_LABELS):
        return "未登记路线"
    if name.upper() in _UNREGISTERED_ROUTE_LABELS:
        name = ""
    if code.upper() in _UNREGISTERED_ROUTE_LABELS:
        code = ""
    if name and code and code not in name:
        return f"{code} {name}"
    return name or code or "未登记路线"


def _patrol_overview_date(result: dict[str, Any]) -> str:
    """从实际执行口径里取日期标签；没有时间范围就不编日期。"""
    values = (result.get("spec") or {}).get("time_range") or []
    start = str(values[0] if values else "").strip()
    try:
        moment = datetime.fromisoformat(start[:10])
    except ValueError:
        return ""
    return f"{moment.month} 月 {moment.day} 日"


def _patrol_overview_summary(result: dict[str, Any]) -> str:
    """巡查任务级概览的确定性回答。

    任务次数、里程和状态按 task_id 去重；路线和病害按任务-路线行汇总。
    这样同一任务挂多条路线时，里程不会重复相加，病害仍能分别归属。
    """
    rows = [row for row in result.get("rows") or [] if isinstance(row, dict)]
    if not rows:
        return ""
    tasks: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        task_id = str(row.get("task_id") or row.get("task_number") or f"row-{index}")
        task = tasks.setdefault(
            task_id,
            {
                "task_id": task_id,
                "task_number": str(row.get("task_number") or "").strip(),
                "patrol_number": str(row.get("patrol_number") or "").strip(),
                "status_code": str(row.get("patrol_status") or "").strip(),
                "status_name": str(row.get("patrol_status_name") or "").strip(),
                "mileage_m": _to_float(row.get("patrol_mileage_m")) or 0.0,
                "routes": [],
                "disease_count": 0,
            },
        )
        task["disease_count"] += int(_to_float(row.get("disease_count")) or 0)
        route = _patrol_route_display(row)
        if route not in task["routes"]:
            task["routes"].append(route)

    task_list = list(tasks.values())
    if not task_list:
        return ""
    completed = sum(
        1
        for task in task_list
        if task["status_code"] == "15" or task["status_name"] == "已完成"
    )
    if completed == len(task_list):
        status_text = "全部完成"
    else:
        status_parts: dict[str, int] = {}
        for task in task_list:
            label = task["status_name"] or task["status_code"] or "状态未知"
            status_parts[label] = status_parts.get(label, 0) + 1
        status_text = "、".join(f"{name} {count} 次" for name, count in status_parts.items())
    mileage_km = sum(float(task["mileage_m"]) for task in task_list) / 1000.0
    date_text = _patrol_overview_date(result)
    first = (
        f"{date_text}共 {len(task_list)} 次巡查，{status_text}，"
        f"累计里程 {mileage_km:.1f} 公里。"
    )

    registered_tasks = [
        (index, task)
        for index, task in enumerate(task_list, 1)
        if any(route != "未登记路线" for route in task["routes"])
    ]
    unregistered_only = [
        task
        for task in task_list
        if not any(route != "未登记路线" for route in task["routes"])
    ]
    route_parts: list[str] = []
    if unregistered_only:
        route_parts.append(f"{len(unregistered_only)} 次只挂了未登记路线")
    if len(registered_tasks) == 1:
        ordinal, task = registered_tasks[0]
        names = [route for route in task["routes"] if route != "未登记路线"]
        route_parts.append(
            f"第 {ordinal} 次巡查 {task['patrol_number'] or task['task_number']} 归属 {'、'.join(names)}"
        )
    elif registered_tasks:
        joined = []
        for ordinal, task in registered_tasks:
            names = [route for route in task["routes"] if route != "未登记路线"]
            joined.append(
                f"第 {ordinal} 次 {task['patrol_number'] or task['task_number']} → {'、'.join(names)}"
            )
        route_parts.append("已登记路线归属：" + "；".join(joined))
    else:
        route_parts.append("当天巡查均未挂接已登记路线")
    second = "；".join(route_parts) + "。"

    disease_total = sum(int(task["disease_count"]) for task in task_list)
    disease_tasks = [
        (index, task)
        for index, task in enumerate(task_list, 1)
        if int(task["disease_count"]) > 0
    ]
    if disease_total <= 0:
        third = "当天未检出病害。"
    elif len(disease_tasks) == 1:
        ordinal, task = disease_tasks[0]
        names = [route for route in task["routes"] if route != "未登记路线"]
        third = f"当天共检出病害 {disease_total} 条，全部来自第 {ordinal} 次巡查"
        if names:
            third += f"，全部落在 {'、'.join(names)}。"
        else:
            third += "，这些病害未关联已登记路线。"
    else:
        attribution = "；".join(
            f"第 {ordinal} 次巡查 {task['disease_count']} 条"
            for ordinal, task in disease_tasks
        )
        third = f"当天共检出病害 {disease_total} 条，分别来自：{attribution}。"
    return first + second + third


_GROUP_LABEL_KEYS = (
    "type",
    "route_name",
    "short_name",
    "full_name",
    "name",
    "damage_type",
    "segment",
    "month",
    "day",
)

_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _numeric_columns(rows: list[dict[str, Any]]) -> list[str]:
    """哪些列是数值列——按数据判断，而不是按列名写死。"""
    if not rows:
        return []
    numeric: list[str] = []
    for column in rows[0]:
        values = [row.get(column) for row in rows if row.get(column) not in (None, "", "NULL")]
        if values and all(_NUMBER_RE.match(str(value)) for value in values):
            numeric.append(column)
    return numeric


def _label_column(rows: list[dict[str, Any]], numeric: list[str]) -> str | None:
    if not rows:
        return None
    candidates = [column for column in rows[0] if column not in numeric]
    for key in _GROUP_LABEL_KEYS:
        if key in candidates:
            return key
    return candidates[0] if candidates else None


# 问题里的中文词 → 列名里可能出现的英文词。用来判断"用户问的到底是哪个指标"。
_METRIC_TERM_MAP: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("病害", "问题"), ("disease", "problem")),
    (("车道",), ("lane",)),
    (("面积",), ("area",)),
    (("桩号", "里程"), ("pile", "meter")),
    # 路线长度：台账 distance 不准，模板/语义层算出来的列多叫 distance_km / route_length_km
    (("长度", "公里", "间距"), ("length", "km", "长度")),
    (("置信度", "可信度"), ("confidence", "conf")),
    (("巡查", "巡检"), ("patrol",)),
    (("设备",), ("device",)),
    (("路段",), ("section",)),
    (("方向", "上下行"), ("direction", "up_or_down")),
)


def _primary_metric(
    numeric: list[str], message: str = "", rows: list[dict[str, Any]] | None = None
) -> str | None:
    """从数值列里挑出"用户真正问的那个指标"。

    以前只看列名里有没有 count，结果「车道数 lane_count」会被当成数量，
    把「病害 911 处」答成「车道 6 处」。
    """
    if not numeric:
        return None
    lowered_message = message or ""
    for words, keys in _METRIC_TERM_MAP:
        if not any(word in lowered_message for word in words):
            continue
        matched = [
            column
            for column in numeric
            if any(key in str(column).lower() for key in keys)
        ]
        if len(matched) == 1:
            return matched[0]
    # 多行结果里，恒定的那一列多半是"总计"而不是分组值，优先取会变化的列。
    if rows and len(rows) > 1:
        varying = [
            column
            for column in numeric
            if len({str(row.get(column)) for row in rows if row.get(column) not in (None, "")}) > 1
        ]
        if varying:
            numeric = varying
    for column in numeric:
        lowered = str(column).lower()
        if "count" in lowered or "cnt" in lowered or "数量" in str(column):
            return column
    return numeric[0]


_MONTH_TOKEN = re.compile(r"(\d{1,2})\s*月")

def _to_float(value: Any) -> float | None:
    try:
        if value in (None, "", "NULL"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _row_sum_column(rows: list[dict[str, Any]], column: str, others: list[str]) -> bool:
    """这一列是不是"同排其它数值列之和"（合计列）——**按数据判，不按列名猜**。

    列名猜不可靠：`总病害数` 确实是人检+车检之和（该当合计），而多指标结果里的 `总数`
    是用户点名要看的指标本身（按"总数"这个词当合计吞掉，就会出现"合计 911 处"顶替总数）。
    """
    checked = 0
    for row in rows:
        value = _to_float(row.get(column))
        parts = [_to_float(row.get(item)) for item in others]
        if value is None or not parts or any(part is None for part in parts):
            continue
        if abs(value - sum(parts)) > 1e-6:
            return False
        checked += 1
    return checked > 0


def _looks_like_total(column: str) -> bool:
    """列名像不像"合计"（只在数据分不清谁是合计时用来消歧，不单独当判据）。"""
    text = str(column or "").strip().lower()
    return text.startswith("总") or any(
        hint in text for hint in ("合计", "总计", "小计", "total", "sum")
    )


def _summary_metrics(
    rows: list[dict[str, Any]], numeric: list[str]
) -> tuple[list[str], list[str]]:
    """拆成 (要逐行念的数值列, 合计列)；合计列排在最后单独用括号念。

    为什么不能只挑一个数值列（docs/SQL结果正确但总结缺失二维分组_整改方案.md）：结果是
    「路线 × {人工巡检数, 车辆巡检数, 总病害数}」时，只渲染一个数值列，用户问的
    "人工巡检和车辆巡检各多少"就整个消失了——实测总结只剩"恒明珠 749、西乡段 911"。
    `_primary_metric` 保留，但只用于**结论**里主要对比哪个指标（同上 §3.4）。
    """
    candidates: list[str] = []
    for column in numeric:
        others = [item for item in numeric if item != column]
        if others and _row_sum_column(rows, column, others):
            candidates.append(column)
    if len(candidates) > 1:
        # 人工=0 时"车辆巡检数"和"总病害数"都等于"其它两列之和"（0+749=749），
        # 光看数据分不出谁是合计。这时才用列名兜底消歧；名字也说不清就**不当合计列**，
        # 宁可多念一列，也不能把用户点名的指标折叠成"（合计 …）"。
        named = [column for column in candidates if _looks_like_total(column)]
        candidates = named if len(named) == 1 else []
    normal: list[str] = []
    totals: list[str] = []
    for column in numeric:
        if column in candidates:
            totals.append(column)
        else:
            normal.append(column)
    return normal + totals, totals


def _complete_domain_columns(columns: list[str]) -> tuple[list[str], list[str]]:
    """有限域补齐（**列方向**）：结果里只有"上行"没有"下行"时补一列 0。

    一维分组补的是"行"（`_domain_for_labels`），二维/透视结果里方向成了**列**，
    补行没用——"西乡段上行和下行各多少"会只念上行。
    """
    domain = _domain_for_labels(columns)
    if not domain:
        return columns, []
    extra = [
        name
        for name in domain
        if not any(name in column or column in name for column in columns)
    ]
    return columns + extra, extra

# 问的结论数据库里没有、必须再算一步（"环比增长率是多少"）——这类别用确定性模板
# 糊一句"共 X 处"就算答完，交给 LLM 把结论说出来。
_DERIVED_ASK = ("环比", "同比", "增长率", "增幅", "下降率")


# 单列结果的英文别名 → 中文说法（模板/语义层给列起名时常用这些短别名）
_RESULT_LABELS = {
    "interval": "平均间距 ",
    "distance_m": "路线长度（米） ",
    "distance_km": "路线长度（公里） ",
    "route_length_km": "路线长度（公里） ",
    "length_m": "路线长度（米） ",
    "length_km": "路线长度（公里） ",
    "per_km": "每公里处数 ",
    "density": "病害密度 ",
    "count": "共 ",
    "cnt": "共 ",
    "total_area": "总面积 ",
    "avg_area": "平均面积 ",
    "avg_metric": "平均值 ",
}


def _month_gap_note(message: str, rows: list[dict[str, Any]]) -> str:
    """一句话里问了多个月份时，把结果里缺的月份补成 0。

    例："西乡段6月和7月的泛油病害分别有多少？" → 库里只有 7 月的数据，
    GROUP BY 结果只有一行；这里补成"6月 0 处、7月 534 处"，而不是只回一个总数。
    """
    asked = list(dict.fromkeys(_MONTH_TOKEN.findall(message)))
    if len(asked) < 2 or not rows:
        return ""
    month_column = next(
        (
            column
            for column in rows[0]
            if "月" in str(column) or "month" in str(column).lower()
        ),
        None,
    )
    if not month_column:
        return ""
    metric = _primary_metric(_numeric_columns(rows), message, rows)
    if not metric:
        return ""
    present: dict[str, Any] = {}
    for row in rows:
        value = row.get(month_column)
        if value in (None, ""):
            continue
        digits = re.sub(r"\D", "", str(value))[-2:]
        if digits:
            present[f"{int(digits):02d}"] = row.get(metric)
    parts: list[str] = []
    for month in asked:
        key = f"{int(month):02d}"
        value = present.get(key)
        if value in (None, ""):
            parts.append(f"{int(month)}月 0 处")
            continue
        try:
            parts.append(f"{int(month)}月 {float(value):g} 处")
        except (TypeError, ValueError):
            parts.append(f"{int(month)}月 {value} 处")
    return "、".join(parts)


# 分组维度里"就那么几档"的域：某档没记录时也要报出来，不能只报有数据的那档
# （实测"按上下行分，哪个方向更多"只回"上行 911 处"，下行 0 处被吞掉）。
_KNOWN_GROUP_DOMAINS: dict[str, tuple[str, ...]] = {
    "direction": ("上行", "下行"),
    "image_presence": ("有图片", "无图片"),
}
# 这些标签不算"另一个域"，是数据本身缺失的兜底档
_DOMAIN_LOOSE_LABELS = {"未填写", "未知", "未知类型", "其他", "无方向", ""}


def _domain_for_labels(observed: list[str]) -> tuple[str, ...]:
    """从**结果里出现的标签**反推这是不是一个"就那么几档"的分组。

    为什么不只看语义层的 group_by：追问补全后语义层时常给不出 group_by（实测"对比这两个
    方向的数量"那轮 group_by 就是空的），可结果里明明写着"上行"——这时按数据判断更可靠。
    """
    names = [str(item).strip() for item in observed if str(item).strip()]
    if not names:
        return ()
    for domain in _KNOWN_GROUP_DOMAINS.values():
        inside = [item for item in names if any(name in item or item in name for name in domain)]
        outside = [item for item in names if item not in inside and item not in _DOMAIN_LOOSE_LABELS]
        if inside and not outside:
            return domain
    return ()


def _spec_group_by(spec: dict[str, Any] | None) -> str:
    """语义层给的分组维度（判断"该不该补缺失的组/要不要给结论"用语义，不猜关键词）。"""
    if not isinstance(spec, dict):
        return ""
    slots = spec.get("slots") or {}
    semantic = spec.get("semantic") or {}
    return str(slots.get("group_by") or semantic.get("group_by") or "")


# 只有"可加总的计数/面积"才谈"合计"：长度、密度、比例加起来没有意义
_SUMMABLE_UNITS = {"处", "条", "个", "次", "㎡", "元", "张"}


def _unit_by_words(text: str) -> str | None:
    """按列名里的词判单位；**判不出来返回 None**（调用方决定先看尾巴还是整体）。"""
    text = str(text or "")
    if "分布" in text:
        return ""
    if "per_km" in text.lower() or "每公里" in text or "密度" in text:
        return "处/公里"
    if "面积" in text or "工程量" in text:
        return "㎡"
    if "公里" in text or "km" in text.lower():
        return "公里"
    if re.search(r"(?:路线|路段).{0,4}长度", text):
        return "公里"
    if any(word in text for word in ("长度", "宽度", "高度", "间距", "距离", "里程")):
        return "米"
    if "车道" in text:
        return "条"
    if "次数" in text or "巡查" in text or "巡检" in text:
        return "次"
    if "等级" in text or "类型" in text:
        return ""
    # 经纬度/坐标是数值但不是"处数"，加"处"会闹笑话（实测"最小经度 1.1处"）
    if any(word in text for word in ("经度", "纬度", "坐标", "比例", "占比", "比率", "百分比")):
        return ""
    if "时间" in text:
        return ""
    if "桩号" in text:
        return "个"
    if "病害" in text or "处" in text:
        return "处"
    if text in {"数量", "个数", "记录数"}:
        return "个"
    return None


def _metric_unit(label: str, value: Any) -> str:
    """按**指标名**给数值配单位（不是所有数值都是"处"）。

    实测：路线长度对比答成"恒明珠 19 处、西乡段 5.1 处、合计 24.1 处"——
    长度被套上了计数单位"处"。这里按指标名判单位，判不出来才回落到"处"。

    先看**名字尾部**再整体判：中文列名的中心词在最后，而限定语里常混进别的单位词——
    "宝安中心恒明珠段面积大于上述平均值的泛油病害数量"的中心词是"数量"（→ 处），
    中间那个"面积"只是限定语，按整体判会把它念成"132㎡"。
    """
    text = str(label or "")
    if not _NUMBER_RE.match(str(value)):
        return ""  # 值不是数字（"二级公路"）不加单位
    tail = _unit_by_words(text[-6:])
    if tail is not None:
        return tail
    whole = _unit_by_words(text)
    return whole if whole is not None else "处"


def _grouped_summary(
    rows: list[dict[str, Any]], message: str = "", spec: dict[str, Any] | None = None
) -> str | None:
    """分组结果统一渲染：自动认出「标签列 + 数值列」，不依赖模型起的列名。

    以前写死 row["count"]，模型把列名叫成 count_per_route 就会渲染出空格和 0。

    """
    numeric = _numeric_columns(rows)
    metric = _primary_metric(numeric, message, rows)
    label = _label_column(rows, numeric)
    if not metric or not label:
        return None
    # 多数值列 = 二维分组/多指标（见 docs/SQL结果正确但总结缺失二维分组_整改方案.md）：
    # 只挑一个数值列渲染，用户问的另一半就消失了，改走"每个数值列都念出来"的分支。
    # 只有"上行"这一列、缺"下行"时也算（补出来的列让它变成二维）。
    metrics, totals = _summary_metrics(rows, numeric)
    render_columns, extra_columns = _complete_domain_columns(
        [column for column in metrics if column not in totals]
    )
    if len(render_columns) + len(totals) >= 2:
        return _multi_metric_summary(
            rows, render_columns, totals, extra_columns, label, metric, spec
        )
    parts: list[str] = []
    pairs: list[tuple[str, float]] = []
    shown_rows = rows if len(rows) <= 30 else rows[:10]
    for row in shown_rows:
        value = row.get(metric)
        if value in (None, "", "NULL"):
            continue
        name = str(row.get(label) or "").strip()
        # 标签是 NULL/空 的行不能当分组念出来（空表聚合会出现"分布：NULL 0 处"）
        if not name or name.upper() in {"NULL", "NONE"}:
            continue
        parts_value = value
        try:
            parts_value = f"{float(value):g}"
        except (TypeError, ValueError):
            pass
        pairs.append((name, float(value) if _NUMBER_RE.match(str(value)) else 0.0))
        parts.append(f"{name} {parts_value}{_metric_unit(str(metric), value)}")
    if not parts:
        return None
    # 有限域的组补齐：有 "上行" 没 "下行" 时补 "下行 0 处"
    # （维度名优先看语义层，语义层没给就看结果里的标签反推）
    reported = [name for name, _ in pairs]
    domain = _KNOWN_GROUP_DOMAINS.get(_spec_group_by(spec), ()) or _domain_for_labels(reported)
    for name in domain:
        if not any(name in item for item in reported):
            parts.append(f"{name} 0 处")
            pairs.append((name, 0.0))
    total = sum(
        float(row[metric])
        for row in rows
        if row.get(metric) not in (None, "") and _NUMBER_RE.match(str(row[metric]))
    )
    unit = _metric_unit(str(metric), total)
    tail = f"，合计 {total:g}{unit}" if len(rows) > 1 and unit in _SUMMABLE_UNITS else ""
    # 分组数本身就是答案的一部分（"一共有多少种资产类型？"），
    # 列表只显示前 10 行，所以组数必须单独报，不能靠用户数行数。
    if len(rows) > len(parts):
        tail = f"，共 {len(rows)} 组" + tail
    text = "分布：" + "，".join(parts) + tail + "。"
    # 分组问题必须给结论：用户问"哪个方向/哪种类型更多"，光列分布等于没答。
    if len(pairs) >= 2:
        text += _extreme_conclusion(
            pairs,
            label_text="处数" if unit in _SUMMABLE_UNITS else str(metric),
            partial=_is_top_subset(spec),
            unit=unit,
        )
    return text


def _single_row_metrics(row: dict[str, Any], message: str = "") -> str | None:
    """单行多指标的渲染：**每个数值列都念出来**（列名 + 单位）。

    只接"≥2 个数值列"的情况，1 个数值列的老路径（"共 X 处"）一律不动。
    案例：复合题复用示例后结果是一行两列（西乡的平均面积 + 恒明珠大于该值的数量），
    以前只念一个，用户问的两个指标答了一半（docs/示例复用参数填反与多指标总结缺失_整改方案.md §3.3）。
    """
    numeric = _numeric_columns([row])
    if len(numeric) < 2:
        return None
    normal, totals = _summary_metrics([row], numeric)
    pieces: list[str] = []
    for column in normal:
        value = _to_float(row.get(column))
        if value is None:
            continue
        pieces.append(f"{column} {value:g}{_metric_unit(column, value)}")
    if not pieces:
        return None
    text = "，".join(pieces)
    if totals:
        value = _to_float(row.get(totals[0]))
        if value is not None:
            text += f"（合计 {value:g}{_metric_unit(totals[0], value)}）"
    return text + "。"


def _multi_metric_summary(
    rows: list[dict[str, Any]],
    render_columns: list[str],
    total_columns: list[str],
    extra_columns: list[str],
    label: str,
    primary: str,
    spec: dict[str, Any] | None = None,
) -> str | None:
    """二维分组（标签 × 多个数值列）的渲染：**每个数值列都念出来**。

    docs/SQL结果正确但总结缺失二维分组_整改方案.md §三：
      · 同源指标（人工巡检数 / 车辆巡检数）→ 交叉表，结论要说清"哪个维度全是 0"这类
        只有二维数据才看得出来的信息；
      · 不同指标（总数 / 平均面积 / 最大面积）→ 一样每列都念、各带单位，
        结论只针对主要指标（用户点名的那个）。
    """
    kept = [
        row
        for row in rows
        if str(row.get(label) or "").strip()
        and str(row.get(label) or "").strip().upper() not in {"NULL", "NONE"}
    ]
    if not kept:
        return None

    def _value(row: dict[str, Any], column: str) -> float | None:
        if column in extra_columns:
            return 0.0
        return _to_float(row.get(column))

    parts: list[str] = []
    for row in kept[:10]:
        name = str(row.get(label) or "").strip()
        pieces: list[str] = []
        for column in render_columns:
            value = _value(row, column)
            if value is None:
                continue
            pieces.append(f"{column} {value:g}{_metric_unit(column, value)}")
        if not pieces:
            continue
        text = f"{name}：" + "，".join(pieces)
        if total_columns:
            value = _value(row, total_columns[0])
            if value is not None:
                text += f"（合计 {value:g}{_metric_unit(total_columns[0], value)}）"
        parts.append(text)
    if not parts:
        return None

    # 总计：优先用结果里的合计列（它才是真的总量）；没有才拿主要指标求和
    total_text = ""
    total_unit = ""
    if len(kept) > 1:
        if total_columns:
            total = sum(
                value
                for row in kept
                if (value := _value(row, total_columns[0])) is not None
            )
            total_unit = _metric_unit(total_columns[0], total)
        else:
            total = sum(value for row in kept if (value := _value(row, primary)) is not None)
            total_unit = _metric_unit(primary, total)
        if total_unit in _SUMMABLE_UNITS:
            total_text = f"，总计 {total:g}{total_unit}"
    groups_text = f"，共 {len(kept)} 组" if len(rows) > len(parts) else ""
    if len(parts) < len(kept):
        groups_text += f"（正文列出前 {len(parts)} 组，完整明细见结果表）"

    # 结论：先给二维信息（哪个维度各分组全是 0），再给主要指标的排名
    zero_note = ""
    zero_columns = [
        column
        for column in render_columns
        if all((_value(row, column) or 0.0) == 0 for row in kept)
    ]
    if zero_columns:
        zero_note = "；".join(f"{column}在各分组都是 0" for column in zero_columns) + "；"
    pairs: list[tuple[str, float]] = []
    for row in kept:
        value = _value(row, primary)
        if value is None:
            continue
        pairs.append((str(row.get(label) or "").strip(), value))
    conclusion = ""
    if len(pairs) >= 2:
        conclusion = _extreme_conclusion(
            pairs,
            label_text=str(primary),
            partial=_is_top_subset(spec),
            unit=_metric_unit(primary, pairs[0][1]),
            prefix=zero_note,
            with_metric_label=True,
        )
    elif zero_note:
        conclusion = f"（结论：{zero_note.rstrip('；')}。）"
    return "分布：" + "；".join(parts) + groups_text + total_text + "。" + conclusion


def _sql_is_row_query(sql: Any) -> bool:
    text = f" {str(sql or '').lower()} "
    return (
        " group by " not in text
        and not re.search(r"\b(count|sum|avg|min|max)\s*\(", text)
    )


def _summarize_result(message: str, result: dict[str, Any]) -> str:
    if result.get("patrol_overview"):
        overview = _patrol_overview_summary(result)
        if overview:
            card = str(result.get("scope_card") or "").strip()
            return f"{overview}\n\n{card}" if card else overview
    reply = _summarize_rows(message, result)
    subject = str((result.get("spec") or {}).get("subject") or "")
    if _is_zero_result(result) and subject in {
        "work_order",
        "asset",
        "bridge",
        "tunnel",
        "slope",
        "culvert",
        "channel",
        "service_area",
        "sign",
        "other_facility",
    }:
        note = "查询结果为 0 条。该台账可能尚未接入数据，不能据此判断业务上实际为零。"
        return f"{note}\n\n{reply}" if reply else note
    # 空表/无记录的聚合结果（整行全是 NULL）：诚实说明"这张台账当前没有数据"，
    # 不要让它掉进"结构解读不了"或让模型猜一个数（docs/字段目录配置与字段触达前置方案.md §3.3）。
    if (
        _is_zero_result(result)
        and (result.get("rows") or [])
        # 有 NULL 格子说明是"空集上的聚合"（NULL 不是 0，是没数据）：
        # `隧道有照明和消防设施吗，各有多少座` 这种情况必须说清"台账当前没有数据"，
        # 否则会被渲染成"分布：NULL 0 处"。
        and (_all_values_null(result) or _has_null_value(result))
    ):
        card = str(result.get("scope_card") or "").strip()
        note = "该台账当前没有入库数据（0 条记录），这不代表业务上实际为零。"
        return f"{note}\n\n{card}" if card else note
    # P0（Wren 路径错因分析 D1/M2）：一行多列时，解读环节经常只说第一个数。
    # 这里做确定性兜底：逐列核对数值有没有出现在回答里，缺了就补一句"各口径：…"。
    reply = _ensure_all_values_reported(reply, result)
    # 安全网：列表/明细查询返回行数正好等于 LIMIT，说明很可能被截断，
    # 这时候"共 N 处"是假的（实测「西乡段泛油一共多少条」答成 500，真值 534）。
    rows = result.get("rows") or []
    if (
        result.get("shape") in {"list", "detail", "photo"}
        and len(rows) >= max(1, settings.sql_max_rows)
    ):
        reply = (
            f"这次返回的是前 {len(rows)} 条（已达到取数上限，实际可能更多）；"
            f"要准确总数请直接问「有多少处/多少条」。\n"
        ) + reply
    # 后置计算（方案 docs/问题拆分重构方案-LLM语义拆分.md 的 post_calc）：
    # 「哪条更多 / 多多少 / 差多少」这类问题数据库里没有答案，必须基于前序结果算。
    # 多指标的横向对比（"哪个养护压力更大"）也要给结论，不能只列数据。
    spec = result.get("spec") or {}
    wants_conclusion = bool(
        spec.get("comparison") or "comparison" in (spec.get("required_conditions") or [])
    )
    conclusion = (
        ""
        if result.get("is_detail_rows")
        else _comparison_conclusion(
            message,
            result.get("rows") or [],
            result.get("metric_labels") or {},
            wants_conclusion=wants_conclusion,
            shape=str(result.get("shape") or ""),
        )
    )
    # 分组渲染那边已经给过"（结论：…）"就别再来一句（实测会出现两条意思一样的话）
    if conclusion and "（结论：" in reply:
        conclusion = ""
    # 抽象概念（老/新、大/小、高/低置信度）必须说明判断依据，不能黑盒
    # （docs/业务覆盖与抽象概念定义方案.md §二）。
    note = str((result.get("boundary_note") or "")).strip()
    spec_now = result.get("spec") or {}
    # 条件落在"库里整列都是空的"字段上时，必须说明白：0 不是"没有符合条件的病害"，是没数据
    empty_note = str(result.get("empty_column_note") or nl2sql._empty_condition_note(spec_now))
    if empty_note:
        prefix_empty = f"{empty_note}\n"
        reply = prefix_empty + reply
    # 口径被代码改过（"按路段分组"→改成按桩号分段）也要说明，别让用户以为问的是 A 答的是 B。
    group_note = str(
        spec_now.get("group_note") or (spec_now.get("slots") or {}).get("group_note") or ""
    ).strip()
    if group_note and group_note not in reply:
        note = f"{note}；{group_note}" if note else group_note
    # 分档模板已经把分界线作为一列报出来了，就别再前置重复一遍
    prefix = f"（判断依据：{note}）\n" if note and note not in reply else ""
    body = f"{prefix}{reply}{conclusion}" if (prefix or conclusion) else reply
    # 口径卡片附在回答末尾（明细/看图类也有用：让人知道这批数据是哪来的、覆盖到什么时候）
    card = str(result.get("scope_card") or "").strip()
    if card and card not in body:
        body = f"{body}\n\n{card}"
    warnings = [str(item).strip() for item in result.get("risk_warnings") or [] if str(item).strip()]
    if warnings:
        body += "\n\n数据风险提示：" + "；".join(warnings[:2])
    return body


def _ensure_all_values_reported(reply: str, result: dict[str, Any]) -> str:
    """保证结果里每个数值都在回答里出现过——单行多列、多行多列都管。

    见 docs/结果校验与问题优化方案.md §五：以前只处理单行多列，遇到"两条路 × 三个维度"
    （行数=2）直接跳过，总结就容易只报其中一列（实测 H7 只说了泛油数，漏了病害总数和平均面积）。
    """
    rows = result.get("rows") or []
    if not rows:
        return reply
    # 明细/列表类：清单本来就整张列在结果表里，不必再把每行数值念一遍
    if result.get("is_detail_rows") or str(result.get("shape") or "") in {
        "list",
        "detail",
        "photo",
    }:
        return reply
    labels = result.get("metric_labels") or {}
    numeric = _numeric_columns(rows)
    # 多行时用"分组标签列"的值当行名（路线名/病害类型/月份…）
    row_label_column = _label_column(rows, numeric) if len(rows) > 1 else None
    missing: list[str] = []
    numeric_seen = 0
    for row in rows:
        row_label = ""
        if row_label_column:
            row_label = str(row.get(row_label_column) or "").strip()
        for key, value in row.items():
            if key == row_label_column or value in (None, "", "NULL"):
                continue
            text = str(value)
            if not _NUMBER_RE.match(text):
                continue
            numeric_seen += 1
            if _number_reported(text, reply):
                continue
            label = str(labels.get(key) or key)
            missing.append(f"{row_label}的{label} = {text}" if row_label else f"{label} = {text}")
    # 单行多列时，只有一个数就不用补（本来就只问一件事）
    if numeric_seen < 2 or not missing:
        return reply
    head = "、".join(missing[:8])
    if len(missing) > 8:
        head += f" 等 {len(missing)} 项"
    return reply + "（各口径：" + head + "）"


def _number_reported(text: str, reply: str) -> bool:
    """这个数值算不算"已经在回答里说过了"（含四舍五入后的各种写法）。"""
    variants = {text}
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        variants |= {f"{number:g}", f"{number:.1f}", f"{number:.2f}", f"{number:.4f}"}
    return any(variant and variant in reply for variant in variants)
    return reply


def _comparison_conclusion(
    message: str,
    rows: list[dict[str, Any]],
    labels_extra: dict[str, Any] | None = None,
    wants_conclusion: bool = False,
    shape: str = "",
) -> str:
    """对比类问题：数据摆完必须给结论（docs/抽象题失败原因分析与整改方向.md 根因1）。

    单指标 → 走 _difference_conclusion（谁多、多多少）。
    多指标（"哪个养护压力更大"这种 5~6 个指标横向比）→ 说清"谁在哪些指标上更高"，
    再按领先的指标个数给一句综合判断——不能只把数据列出来让用户自己看。

    要不要给结论由**语义层**说了算（spec.comparison / 分组维度），不靠"多多少/哪条更"
    这类词——实测用户说"多了多少处""哪条路的病害更多"，关键词一个都没命中，
    结论就整段丢了。
    """
    # 明细/列表类结果是"把清单给你看"，不是拿来做大小比较的——
    # 对 104 行明细套"最多/最少"会得出"k0+193 最多（5000）"这种没意义的话。
    if shape in {"list", "detail", "photo"}:
        return ""
    if not rows:
        # 空结果：没法比对，也别在这下结论（这里以前会去取 rows[0] 直接崩）
        return ""
    if len(rows) < 2:
        # 只有一个对象却要比较：说清"没得比"，别硬下"其他对象没有数据"的结论。
        # 例外：这是"上下行/有无图片"这类**有限域**——另一档真的就是 0，
        # 分组渲染那边已经补了"下行 0 处"，不需要再提示"做不了比较"。
        label_key = _label_column(rows, _numeric_columns(rows))
        domain_labels = (
            [str(item.get(label_key) or "").strip() for item in rows] if label_key else []
        )
        if _domain_for_labels(domain_labels):
            return ""
        # 单行多列（一列一个对象/口径）本身就是"列与列的对比"，不是"只有一个对象"：
        # 实测复合题"西乡的平均面积 + 恒明珠大于该值的数量"结果就一行两列，却被报成
        # "只有 1 个对象，做不了比较"（docs/示例复用参数填反与多指标总结缺失_整改方案.md 根因4）。
        if len(_numeric_columns(rows)) >= 2:
            return ""
        if wants_conclusion:
            return (
                "（注意：这次结果里只有 1 个对象的记录，做不了比较——"
                "多半是比较的另一方没查到，或条件没带全，建议换个问法再确认。）"
            )
        return ""
    if len(rows) > 2:
        numeric_many = _numeric_columns(rows)
        label_many = _label_column(rows, numeric_many)
        if not numeric_many or not label_many:
            return ""
        metric_many = _primary_metric(numeric_many, message, rows) or numeric_many[0]
        pairs: list[tuple[str, float]] = []
        for row in rows:
            name = str(row.get(label_many) or "").strip()
            try:
                pairs.append((name, float(row.get(metric_many))))
            except (TypeError, ValueError):
                continue
        return _extreme_conclusion(
            pairs,
            str((labels_extra or {}).get(metric_many) or _RESULT_LABELS.get(str(metric_many), "") or metric_many),
        )
    numeric = _numeric_columns(rows)
    if len(numeric) <= 1:
        return _difference_conclusion(message, rows, force=wants_conclusion)
    if not wants_conclusion and not any(
        word in message for word in ("哪个更", "哪条更", "哪边", "谁更", "哪个多", "哪条多", "对比", "差别", "区别")
    ):
        return ""
    label = _label_column(rows, numeric)
    if not label:
        return ""
    first, second = rows
    first_name, second_name = str(first.get(label)), str(second.get(label))
    if not first_name or not second_name:
        return ""
    labels = {
        key: str(
            (labels_extra or {}).get(key)
            or _RESULT_LABELS.get(str(key), "").strip()
            or key
        )
        for key in numeric
    }
    lead: dict[str, list[str]] = {first_name: [], second_name: []}
    for key in numeric:
        try:
            left, right = float(first.get(key) or 0), float(second.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if left == right:
            continue
        lead[first_name if left > right else second_name].append(labels[key])
    parts = [f"{name}在 {'、'.join(keys)} 上更高" for name, keys in lead.items() if keys]
    if not parts:
        return ""
    winner = max(lead, key=lambda name: len(lead[name]))
    if len({len(keys) for keys in lead.values()}) > 1:
        parts.append(f"按指标多数算，{winner}更突出")
    return "（结论：" + "；".join(parts) + "。）"


def _is_top_subset(spec: dict[str, Any] | None) -> bool:
    """结果是"前几名"而不是全集吗（取极值/有 limit）？

    是的话只能说"谁最多"，不能说"谁最少"——没上榜的那些可能更小。
    """
    if not isinstance(spec, dict):
        return False
    return str(spec.get("shape") or "") == "top" or bool(spec.get("limit"))


def _extreme_conclusion(
    pairs: list[tuple[str, float]],
    label_text: str = "数量",
    partial: bool = False,
    unit: str = "",
    prefix: str = "",
    with_metric_label: bool = False,
) -> str:
    """分组/多对象：给"谁最多、谁最少、差多少"的结论（不靠关键词，靠数据本身）。

    partial=True 表示这只是前几名（有 LIMIT），只敢说"最多"。
    prefix 给二维分组用：结论前面先补一句"哪个维度全是 0"这类二维信息。
    with_metric_label 给二维分组用：结论里带上指标名（"西乡段的车辆巡检发现数最多"），
    否则一维那句"西乡段最多"看不出比的是哪个维度。
    """
    if len(pairs) < 2:
        return ""
    top = max(pairs, key=lambda item: item[1])
    top_name = f"{top[0]}的{label_text}" if with_metric_label else str(top[0])
    if partial:
        body = f"{top[0]}的{label_text}最多（{top[1]:g}{unit}）"
        return f"（结论：{prefix}{body}。）"
    bottom = min(pairs, key=lambda item: item[1])
    if top[1] == bottom[1]:
        body = f"{len(pairs)} 个{label_text}一样多，都是 {top[1]:g}{unit}"
        return f"（结论：{prefix}{body}。）"
    if bottom[1] == 0:
        body = f"{top_name}最多（{top[1]:g}{unit}），{bottom[0]}没有记录"
        return f"（结论：{prefix}{body}。）"
    body = (
        f"{top_name}最多（{top[1]:g}{unit}），{bottom[0]}最少（{bottom[1]:g}{unit}），"
        f"相差 {top[1] - bottom[1]:g}{unit}"
    )
    return f"（结论：{prefix}{body}。）"


def _difference_conclusion(
    message: str, rows: list[dict[str, Any]], force: bool = False
) -> str:
    """两个对象对比时补一句"谁更多、差多少"。数据不齐就什么都不说。"""
    if not force and not any(
        word in message for word in ("多多少", "差多少", "相差", "谁多", "哪条更", "哪个更", "哪个多", "哪条多")
    ):
        return ""
    if len(rows) != 2:
        return ""
    numeric = _numeric_columns(rows)
    if not numeric:
        return ""
    metric = _primary_metric(numeric, message, rows) or numeric[0]
    label = _label_column(rows, numeric)
    if not label:
        return ""
    pairs: list[tuple[str, float]] = []
    for row in rows:
        try:
            pairs.append((str(row.get(label)), float(row[metric])))
        except (TypeError, ValueError):
            return ""
    (left, first), (right, second) = pairs
    bigger, smaller = (left, first) if first >= second else (right, second)
    top, bottom = max(first, second), min(first, second)
    if top == bottom:
        return f"（两者一样多：各 {top:g}。）"
    return f"（{bigger} 更多：{top:g} vs {bottom:g}，多 {top - bottom:g}。）"


def _summarize_rows(message: str, result: dict[str, Any]) -> str:
    """结果 → 中文。多月份问句先补一段"每个月各多少"，其余照常。"""
    rows = result.get("rows", [])
    if not rows:
        return "未查询到符合条件的数据。"
    # 归因类（"为什么集中在一段时间里"）不能只把分布念一遍——要总结特点 + 给推测原因，
    # 这只有 LLM 解读做得到，所以这类问题跳过确定性模板直接走 LLM。
    # 见 docs/抽象题失败原因分析与整改方向.md 根因5。
    # 要不要"下结论"只由语义层判断。
    spec = result.get("spec") or {}
    needs_conclusion = spec.get("needs_conclusion")
    if needs_conclusion is True:
        if len(rows) <= 30 and not re.search(
            r"为什么|原因|好不好|严重|建议|判断|评价|趋势",
            message,
        ):
            return _summarize_rows_core(message, result)
        return _llm_result_summary(message, result)
    note = _month_gap_note(message, rows)
    text = _summarize_rows_core(message, result)
    if not note:
        return text
    asked = _MONTH_TOKEN.findall(message)
    if asked and f"{int(asked[0])}月" in text:
        return text  # 核心摘要已经按月说了，别重复
    # 核心摘要只是把总数又说了一遍（"共 534 处。"）时，别再重复一遍
    core_numbers = set(re.findall(r"\d+(?:\.\d+)?", text))
    note_numbers = set(re.findall(r"\d+(?:\.\d+)?", note))
    if core_numbers and core_numbers <= note_numbers:
        return f"{note}。"
    return f"{note}。{text}"


def _small_grouped_rows_summary(result: dict[str, Any]) -> str:
    """小结果集（≤30 行）按真实分组列确定性渲染，避免 LLM 只看到前 10 行。"""
    rows = list(result.get("rows") or [])
    if not (1 < len(rows) <= 30):
        return ""
    numeric = _numeric_columns(rows)
    if not numeric:
        return ""
    columns = [str(item) for item in result.get("columns") or rows[0].keys()]
    labels = [
        column
        for column in columns
        if column not in numeric
        and any(str(row.get(column) or "").strip() for row in rows)
    ]
    if not labels:
        return ""
    metric_labels = result.get("metric_labels") or {}
    lines: list[str] = []
    for row in rows:
        label = " / ".join(
            str(row.get(column) or "").strip()
            for column in labels
            if str(row.get(column) or "").strip()
        )
        values = []
        for metric in numeric:
            value = row.get(metric)
            if value in (None, ""):
                continue
            name = str(metric_labels.get(metric) or metric)
            values.append(f"{name} {value}{_metric_unit(name, value)}")
        if label and values:
            lines.append(f"{label}：" + "，".join(values))
    if not lines:
        return ""
    suffix = f"。共 {len(lines)} 组"
    if len(numeric) == 1:
        try:
            total = sum(float(row.get(numeric[0]) or 0) for row in rows)
            unit = _metric_unit(str(metric_labels.get(numeric[0]) or numeric[0]), total)
            if unit in _SUMMABLE_UNITS:
                suffix += f"，合计 {total:g}{unit}"
        except (TypeError, ValueError):
            pass
    return "；".join(lines) + suffix + "，完整明细见结果表。"


def _time_scope_label(message: str) -> str:
    match = re.search(
        r"(?:20\d{2}年)?第[一二三四]季度(?:[（(][^）)]*[）)])?",
        str(message or ""),
    )
    return match.group(0) if match else ""


def _summarize_rows_core(message: str, result: dict[str, Any]) -> str:
    rows = result.get("rows", [])
    if not rows:
        return "未查询到符合条件的数据。"
    if result.get("is_detail_rows") or (
        _sql_is_row_query(result.get("sql")) and not _numeric_columns(rows)
    ):
        return f"共 {len(rows)} 条明细记录，请在结果工作台查看表格与图表。"
    if " group by " in f" {str(result.get('sql') or '').lower()} ":
        small = _small_grouped_rows_summary(result)
        if small:
            scope = _time_scope_label(message)
            if scope:
                small = f"{scope}病害分布：{small}"
            return small

    # 一问多个口径（6月/7月/7月29日/K1以西）时，逐项列出来，别让模型自由发挥。
    labels = result.get("metric_labels") or {}
    if labels:
        # 单位判定统一走 _metric_unit（分布渲染和逐口径渲染共用一份，免得两处口径跑偏）
        unit_of = _metric_unit

        def row_text(row: dict[str, Any]) -> str:
            parts = []
            for alias, label in labels.items():
                if alias in row_label_keys or row.get(alias) in (None, ""):
                    continue
                value = row.get(alias)
                # 桩号类数值统一换算成 Kx+yyy，别甩 20000 这种米数
                if "桩号" in str(label) or str(alias) in {"min_pile", "max_pile", "bucket", "data_min_pile", "data_max_pile"}:
                    value = calibrate.fmt_pile(value)
                    parts.append(f"{label} {value}")
                    continue
                parts.append(f"{label} {value}{unit_of(label, value)}")
            return "、".join(parts)

        # 行标签列：以前写死 "type"，可模板/SQL 的别名不一定是它（实测车道数那条 SQL
        # 用的是 `AS route_name`，结果渲染成"None：车道数 2条"）——按数据认出标签列。
        # 只认"声明了、而且结果里真的有这列"的标签键：声明是 type、SQL 却起名 route_name 时，
        # 以前会渲染成"None：…"，现在退回"按数据认标签列"。
        row_label_keys = [
            key for key in ("type", "dimension") if key in labels and key in rows[0]
        ]
        if not row_label_keys:
            guess = _label_column(rows, _numeric_columns(rows))
            if guess:
                row_label_keys = [guess]
        # 分组多行（每条路的多个指标）逐行列出；单行多口径则一行内列出。
        if len(rows) > 1 and row_label_keys:
            shown_rows = rows if len(rows) <= 30 else rows[:10]
            lines = [
                f"{row.get(row_label_keys[0])}：{row_text(row)}"
                for row in shown_rows
                if row.get(row_label_keys[0]) not in (None, "")
                if row_text(row)
            ]
            if lines:
                text = "；".join(lines)
                if len(shown_rows) < len(rows):
                    text += f"（仅列前 {len(shown_rows)} 组，完整明细见结果表）"
                # 只有一个计数列时顺手给合计（"有多少条、分布在哪"这类问题要的就是这个数）
                count_like = [
                    alias
                    for alias in labels
                    if alias not in row_label_keys and "count" in str(alias).lower()
                ]
                if len(count_like) == 1:
                    try:
                        total = sum(float(row.get(count_like[0]) or 0) for row in rows)
                        text += f"（合计 {total:g}）"
                    except (TypeError, ValueError):
                        pass
                return text + "。"
        else:
            text = row_text(rows[0])
            # 单行结果也别把"对象/口径"那列丢了（"AI巡检工单：记录数 0条"）
            scope_value = next(
                (rows[0].get(key) for key in row_label_keys if rows[0].get(key)), None
            )
            if scope_value not in (None, ""):
                text = f"{scope_value}：{text}" if text else str(scope_value)
            if text:
                total = rows[0].get("total_count")
                tail_text = f"（合计 {total} 处）" if total not in (None, "") else ""
                return text + tail_text + "。"

    shape = result.get("shape")
    # 单行多指标：每个数值列都念（明细/看图类结果不套，那种"一行几列"不是聚合口径）
    if len(rows) == 1 and str(shape or "") not in {"list", "detail", "photo"}:
        single = _single_row_metrics(rows[0], message)
        if single:
            return single
    if (
        shape == "count"
        and len(rows) == 1
        and len(rows[0]) <= 2
        and not any(word in message for word in _DERIVED_ASK)
    ):
        first_row = rows[0]
        # 单行的计数结果：数值列里挑一个（列名由模型决定，不能写死）。
        numeric = _numeric_columns(rows)
        count_key = _primary_metric(numeric, message)
        # 只有"这一列真的是计数"时才说"共 X 处"：宽度/高度/车道数/里程/日期这些属性
        # 以前也被念成"共 100.00 处"（实测最大宽度 100 的那题）。
        column_name = str(count_key or next(iter(first_row), "")).lower()
        asks_count = any(word in message for word in ("多少处", "多少条", "几处", "几条", "多少个", "多少次"))
        looks_count = any(
            word in column_name for word in ("count", "数量", "处数", "条数", "个数", "记录数", "次数")
        )
        if len(first_row) == 1 or count_key is not None:
            value = first_row[count_key] if count_key else next(iter(first_row.values()))
            if str(value).strip() != "" and (looks_count or asks_count):
                if any(word in message for word in ("多少条", "记录数", "条数")):
                    unit = "条"
                elif any(word in message for word in ("巡检", "巡查", "多少次")):
                    unit = "次"
                else:
                    unit = "处"
                return f"共 {value} {unit}。"

    if shape == "count" and len(rows) > 1:
        grouped = _grouped_summary(rows, message, result.get("spec"))
        if grouped:
            return grouped

    if shape == "distribution":
        grouped = _grouped_summary(rows, message, result.get("spec"))
        if grouped:
            return grouped

    if shape in {"list", "photo"}:
        if result.get("overview"):
            return _overview_summary(rows)
        # 单行且取到的值就是空（台账没填）时别念"共 1 条明细"——实测"什么时候通车"就是这么答偏的
        if len(rows) == 1 and len(rows[0]) == 1:
            only_key, only_value = next(iter(rows[0].items()))
            if only_value in (None, "", "NULL", "null", "None"):
                label = str(
                    (result.get("metric_labels") or {}).get(only_key)
                    or _RESULT_LABELS.get(str(only_key), "").strip()
                    or only_key
                )
                return f"{label}这一项在台账里是空的（未填写），不是没有符合条件的数据。"
        return f"共 {len(rows)} 条明细记录，请在结果工作台查看表格与图表。"

    if rows and rows[0].get("area") not in (None, "") and shape == "detail":
        # detail + 面积字段（有明确范围的问题）直接给汇总，比"共 N 条明细"有用。
        return _overview_summary(rows)

    if shape == "top" and rows[0].get("metric") not in (None, ""):
        # TOP 类问题最高值往往多条并列，必须把并列的都说出来。
        top_value = rows[0].get("metric")
        tied = [row for row in rows if str(row.get("metric")) == str(top_value)]
        labels = [
            f"{row.get('route_name', '')} {row.get('pile_number', '')}".strip()
            for row in tied[:20]
            if row.get("pile_number")
        ]
        detail = "、".join(labels)
        if len(tied) > 20:
            detail += f" 等（共 {len(tied)} 条）"
        return f"最高 {top_value}，共 {len(tied)} 条并列：{detail}。"

    if len(rows) == 1:
        first_row = rows[0]
        # 下面两处判断都要用这三个值（count 分支早返回，但属性分支也要用）：统一在这里算。
        # 以前只有 count 分支算过，遇到"工单状态"这种单行多列题会直接 UnboundLocalError。
        numeric = _numeric_columns(rows)
        asks_count = any(
            word in message for word in ("多少处", "多少条", "几处", "几条", "多少个", "多少次")
        )
        looks_count = any(
            word in str(next(iter(first_row), "")).lower()
            for word in ("count", "数量", "处数", "条数", "个数", "记录数", "次数")
        )
        pile_key = next(
            (
                key
                for key in first_row
                if "pile" in str(key).lower() and "count" in str(key).lower()
            ),
            None,
        )
        if pile_key:
            return f"共 {first_row[pile_key]} 个不同桩号。"
        # 单行多列且都是属性/极值（宽度、高度、车道数、里程、经纬度…）：逐列念，别套"共 X 处"
        if len(first_row) > 1 and len(numeric) == len(first_row) and not looks_count and not asks_count:
            parts = []
            for key in numeric:
                value = first_row.get(key)
                if value in (None, ""):
                    continue
                label = str(
                    (result.get("metric_labels") or {}).get(key)
                    or _RESULT_LABELS.get(str(key), "").strip()
                    or key
                )
                parts.append(f"{label} = {value}")
            if parts:
                return "，".join(parts) + "。"

        if "avg_metric" in first_row and "above_avg_count" in first_row:
            return (
                f"平均值为 {first_row['avg_metric']}，超过平均值的有 {first_row['above_avg_count']} 条"
                f"（共 {first_row.get('total_count', '')} 条）。"
            )
        if "ratio_percent" in first_row:
            return (
                f"共 {first_row.get('part_count')} 处，占总数 {first_row.get('total_count')} 处的 "
                f"{first_row['ratio_percent']}%。"
            )
        if "first_time" in first_row and "last_time" in first_row:
            return (
                f"共 {first_row.get('count')} 条，时间范围 {first_row.get('first_time')} ~ "
                f"{first_row.get('last_time')}；其中类型未知 {first_row.get('unknown_type_count')} 条、"
                f"无方向 {first_row.get('no_direction_count')} 条、"
                f"未挂接路段属性 {first_row.get('no_section_attr_count')} 条。"
            )
        aggregate_keys = ("avg_area", "average_area", "max_area", "min_area", "total_area", "avg", "max", "min", "sum")
        for key in aggregate_keys:
            if key in first_row and first_row[key] not in (None, "", "NULL"):
                return f"{key} 为 {first_row[key]}。"
        # 只有单个字段时才直接拼；多字段的单行结果（例如同时问「≥0.7 有几条、<0.5 有几条」）
        # 交给 LLM 写成通顺的中文，别甩一串英文列名。
        # 单行单列且值为空（台账没填）时如实说明——实测"什么时候通车"回了"共 1 条明细记录"
        if len(first_row) == 1:
            only_value = next(iter(first_row.values()), None)
            if only_value in (None, "", "NULL", "null"):
                only_key = next(iter(first_row), "")
                label = str(
                    (result.get("metric_labels") or {}).get(only_key)
                    or _RESULT_LABELS.get(str(only_key), "").strip()
                    or only_key
                )
                return f"{label}这一项在台账里是空的（未填写），不是没有符合条件的数据。"
        if len(first_row) <= 1:
            values = [
                f"{_RESULT_LABELS.get(str(key), key)}{value}"
                for key, value in first_row.items()
                if value not in (None, "")
            ]
            if values:
                return "，".join(values) + "。"
        raw_fallback = "，".join(
            f"{key}={value}" for key, value in first_row.items() if value not in (None, "")
        )
    else:
        raw_fallback = ""

    # 一行结果的"对象 + 指标"（如"哪条路病害密度最高" → 一行 路线名+密度）：
    # 直接按数据渲染，别让 LLM 转述——实测 LLM 会从 few-shot 示例里抄名字，
    # 把"宝安大道西乡段密度最高"说成"宝安中心--恒明珠"。
    if len(rows) == 1:
        result_row = rows[0]
        numeric_columns = _numeric_columns(rows)
        label_column = _label_column(rows, numeric_columns)
        metric_key = _primary_metric(numeric_columns, message, rows)
        if label_column and metric_key and str(result_row.get(label_column) or "").strip():
            metric_label = (
                (result.get("metric_labels") or {}).get(metric_key)
                or _RESULT_LABELS.get(str(metric_key), "").strip()
                or metric_key
            )
            return (
                f"{result_row.get(label_column)}：{metric_label} {result_row.get(metric_key)}。"
            )

    summary = _llm_result_summary(message, result)
    if summary:
        return summary
    return raw_fallback + "。" if raw_fallback else "查询完成。"


def _llm_result_summary(message: str, result: dict[str, Any]) -> str:
    """让 LLM 把结果说成中文（对比类要下结论、归因类要给推测原因）。失败返回空串。"""
    rows = (result.get("rows") or [])[:20]
    try:
        return chat(
            prompts.messages(
                "result_summary",
                message=message,
                columns=result.get("columns", []),
                preview=json.dumps(rows, ensure_ascii=False, default=str),
                # 阶段4（结果解读）示例：类似问题当时是怎么说成中文的
                # （见 docs/SQL问答对存储与检索方案.md §一 阶段4）
                examples=nl2sql.example_interpretation(message),
            ),
            temperature=0,
            # 结果解读要"下结论/给推测"（对比类、归因类都靠它），用语义理解模型
            model=role_model("semantic"),
        )
    except Exception:
        return ""


def _repair_result_presentation(
    question: str,
    result: dict[str, Any],
    verdict: dict[str, Any],
) -> str:
    """只修最终回复的措辞和单位；查询结果已经可信，禁止重跑或改数值。"""
    try:
        repaired = chat(
            prompts.messages(
                "repair_presentation",
                question=question,
                reply=str(result.get("reply") or ""),
                sql=str(result.get("sql") or "")[:2000],
                columns=json.dumps(result.get("columns") or [], ensure_ascii=False),
                rows=json.dumps(
                    list(result.get("rows") or [])[:10],
                    ensure_ascii=False,
                    default=str,
                )[:2400],
                reason=str(verdict.get("reason") or ""),
            ),
            temperature=0,
            model=role_model("semantic") or role_model("verify"),
            max_tokens=1200,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "repair_presentation_failed", exc, question=question[:120])
        return ""
    return repaired.strip("`").strip()


def _answer_format(shape: str | None) -> str:
    if shape == "count":
        return "number"
    if shape in {"distribution", "trend"}:
        return "chart"
    if shape in {"list", "photo"}:
        return "table" if shape == "list" else "image"
    return "table"


def _lineage(kind: str, result: dict[str, Any], plan: dict[str, Any], sql: str | None) -> dict[str, Any]:
    # Wren 编译产物是 schema.table 全限定名，这里统一取基础表名
    tables = sorted(sql_engine.table_names((sql or "").lower()))
    return {
        "db": kind,
        "tables": tables,
        "conditions": plan.get("required_conditions", []) + ["del_flag=0"],
        "rows": len(result.get("rows", [])),
        "engine": result.get("engine", "builtin"),
        "retrieval": result.get("retrieval_by_dataset", {}),
        "sql": sql,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _future_boundary_result(message: str, plan: dict[str, Any], kind: str) -> dict[str, Any]:
    """未来/预测类问题的**如实回答**：库里只有已经发生过的记录，没有"未来"这类数据。

    "宝乡大道最近的泛油病害需要多长时间来治理好？"要的是**未来**（治理时长/工期/预测），
    而库里只有已发生的巡检与病害记录 → 直接说明数据边界，并告诉用户能查什么
    （现在有多少处、历史同类病害的处置时长…），**不拿历史数据硬凑一个答案，也不推测**。
    """
    data_note = ""
    try:
        rows = db_manager.query(
            "mysql",
            "SELECT MAX(collection_time) AS latest FROM ai_problem WHERE del_flag = 0",
        )["rows"]
        if rows and rows[0].get("latest"):
            data_note = f"，库里最新的病害记录到 {str(rows[0]['latest'])[:10]}"
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "future_boundary_stat_failed", exc, question=message[:80])
    reply = (
        "这条问题要的是**未来**的信息（预计多久、什么时候能完成），"
        f"而库里只有**已经发生**的巡检/病害记录{data_note}——没有治理时长、工期计划、预测这类字段，"
        "所以按现有数据答不了，我也不做推测。\n\n"
        f"（判定依据：这句话的时间参照系是「未来」；今天是 {datetime.now():%Y-%m-%d}。）\n\n"
        "换个口径我能查：\n"
        "1. 现在（或某段时间）这类病害有多少处、都在哪些桩号\n"
        "2. 这类病害历史上从发现到处置的平均时长（前提是库里记了处置时间）\n"
        "3. 同类病害的面积/置信度分布，用来估工作量"
    )
    return {
        "reply": reply,
        "source": "database",
        "engine": "time-boundary",
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "result_id": str(uuid.uuid4()),
        "question": message,
        "used_question": message,
        "reviewable": False,
        "time_frame": "future",
    }


# 尾部 LIMIT：允许后面跟 OFFSET（"第 2 多"就是 LIMIT 1 OFFSET 1 这种位次查询）。
_TOP_LIMIT_TAIL = re.compile(r"(?is)\blimit\s+(\d+)(\s+offset\s+\d+)?\s*$")
# 只有"这句 SQL 本身真在聚合"时才允许钉 LIMIT：含 GROUP BY，或 SELECT 列表里有聚合函数。
_TOP_GROUP_BY = re.compile(r"(?is)\bgroup\s+by\b")
_TOP_AGG = re.compile(r"(?is)\b(?:count|sum|avg|min|max)\s*\(")


def _sql_select_head(sql: str) -> str:
    """第一条**顶层** FROM 之前的片段（≈ SELECT 列表）。

    括号里的 FROM（子查询/CTE）不算；引号内的 FROM 也不算。扫不出来就返回整句——
    宁可多认出聚合、也不要在"这句到底聚没聚合"上猜错。
    """
    depth = 0
    quote = ""
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if quote:
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and sql[index : index + 4].lower() == "from":
            before = sql[index - 1] if index else " "
            after = sql[index + 4] if index + 4 < length else " "
            if not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_"):
                return sql[:index]
        index += 1
    return sql


def _enforce_top_limit(sql: str | None, plan: dict[str, Any]) -> str | None:
    """语义层说了"取前 N 名"（shape=top + semantic.limit）时，把生成 SQL 的行数**钉死**。

    为什么必须在代码里钉：实测模型会把"取前 3 名"写成 `LIMIT 1`（或写成只取最大值），
    于是"哪三个星期…把明细列出来"的第一步只回 1 行，第二步就只覆盖到 1 周
    （实测 401 条，而三周应为 534 条）——用户看不出少了两周。

    只动**最外层**那一句 LIMIT：尾部有就改成 N、没有就补上；子查询里的 LIMIT 不动。
    用户已确认的口径（forced_sql）不走这里：那条 SQL 是他看过的，行数不该被偷偷改。
    """
    if not sql:
        return sql
    if str((plan or {}).get("shape") or "") != "top":
        return sql
    raw = ((plan or {}).get("semantic") or {}).get("limit")
    if not isinstance(raw, int) or raw <= 0:
        return sql
    text = str(sql).rstrip().rstrip(";")
    # 这句没分组、SELECT 里也没有聚合 → 它是**明细** SQL：把 500 条明细钉成 N 条
    # 会把"多给了一堆明细"变成"只给一条随便取到的明细"，看起来像有答案却答非所问
    # （实测"哪半个月最多"识别不出分组时会走到这里）。明细题的行数不归这条管。
    if not (_TOP_GROUP_BY.search(text) or _TOP_AGG.search(_sql_select_head(text))):
        log_event(
            "agent",
            "INFO",
            "top_limit_skipped_not_aggregated",
            question=str((plan or {}).get("message") or "")[:60],
        )
        return sql
    # 结尾如果是注释行，先把注释摘掉再补 LIMIT，否则补出来的 LIMIT 会被注释吃掉。
    lines = text.splitlines()
    while lines and lines[-1].strip().startswith("--"):
        lines.pop()
    text = "\n".join(lines).rstrip()
    tail = _TOP_LIMIT_TAIL.search(text)
    if tail:
        # 带 OFFSET 的是**位次**查询（"第 2 多的那一周"= LIMIT 1 OFFSET 1）：
        # 在后面再拼一个 LIMIT 会直接语法错（实测 0 行、链路报缺依赖）。原样放过。
        if tail.group(2):
            return sql
        if int(tail.group(1)) == raw:
            return sql
        return text[: tail.start()].rstrip() + f" LIMIT {raw}"
    return text + f" LIMIT {raw}"


def _build_query_result(
    message: str,
    intent: str,
    kind: str | None = None,
    template_fallback: bool = False,
    wren_first: bool = False,
    use_saved: bool | None = None,
    forced_sql: str | None = None,
    forced_engine: str = "",
    plan: dict[str, Any] | None = None,
    skip_capabilities: bool = False,
    want_images: bool = False,
    repair_feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if kind is None:
        kind = cross_db.resolve_db_kind(message)
    try:
        plan = plan or nl2sql.build_query_plan(message, kind)
    except Exception as exc:
        reason = str(exc)
        # 实体不存在（路线/病害类型）不是"字段没说清"，不该甩字段清单，
        # 提示里已经带了相近候选。
        entity_missing = "没有找到路线" in reason or "没有识别到病害类型" in reason
        raise clarify.ClarifyNeeded(
            kind, reason, show_fields=not entity_missing
        ) from exc
    event_bus.emit_event(
        "phase",
        title=f"查询计划：{plan.get('subject') or '-'} / {plan.get('shape') or '-'}",
    )
    section_candidates = list((plan.get("slots") or {}).get("section_candidates") or [])
    if section_candidates:
        labels = []
        for item in section_candidates[:5]:
            version = str(item.get("section_version") or "").strip()
            name = str(item.get("full_name") or item.get("code") or item.get("id") or "")
            labels.append(f"{name}（版本 {version or '未标'}）")
        raise clarify.ClarifyNeeded(
            kind,
            "路段名称匹配到多个版本：" + "；".join(labels) + "。请补充路段版本或编码。",
            show_fields=False,
        )
    if str((plan.get("slots") or {}).get("time_frame") or "") == "future":
        # 未来/预测类（"最近的病害需要多长时间治理好"）：库里只有**历史巡检记录**，
        # 没有未来计划、治理时长、预测这类数据 → 如实说明数据边界，不拿历史数据硬答、更不许编
        # （docs/值换算层统一修复方案.md §1.2 的思路：判不出来就如实说，别用默认值糊过去）。
        return _future_boundary_result(message, plan, kind)
    # 用户点名的字段这张表（或整个库）根本没有 → 如实说清楚，别拿别的列顶替、也别假装"没查到"。
    # 实测"桥梁的经纬度"（桥梁表一个经纬度列都没有）、"桥在哪个省"（库里没有省份这项记录）。
    missing_payload = _missing_fields_payload(message, plan, kind)
    if missing_payload:
        return missing_payload
    # 计划里点到 **MDL 计算列**（派生列，如 section_length_km）→ **强制走 Wren**：
    # 物理路径（内置模板/内置 LLM）会把列名原样写进物理 SQL，物理表没这列 → Unknown column
    # （实测"路段米数"选了通用明细那张卡就是这个错）。Wren 按模型把表达式内联进去，算得出来。
    derived_field = nl2sql.plan_derived_field(plan, kind)
    if derived_field and not nl2sql.is_patrol_route_overview_plan(plan):
        log_event(
            "agent", "INFO", "derived_field_forced_wren",
            field=derived_field, question=message[:60],
        )
        wren_first = True
        skip_capabilities = True

    # 曾经这里会做一次"示例复用"召回（向量 + 结构同型 + 槽位对齐）：那是模板可以自动直出的
    # 时代留下的。现在"用哪条口径"必须由用户在确认卡上点（docs/RAG示例直达与AI直答旁路方案.md
    # §〇），同一句也会由 L2 摆成卡片，所以这条召回只留下成本、不再产生任何执行——直接不做。
    # 路径优先级（docs/Wren主路径重构方案.md §3.1）：
    #   模板只是"快速通道"——只处理单主体/单度量/不用分组的最简查询；
    #   其余（分组统计、占比、密度、多指标、多路线、其他主体）先走 Wren 语义层。
    # 引擎开关（前端「引擎：混合 / 仅 Wren」）：
    #   hybrid = 按上面的规则选模板或 Wren
    #   wren   = 全部跳过模板，强制走 Wren（用于实测 Wren 覆盖率）
    engine_mode = chat_settings.load().get("engine_mode", "hybrid")
    reuse_mode = str(chat_settings.load().get("reuse_mode") or "ask")
    event_bus.emit_event("phase", title=f"选择执行路径：{engine_mode}")
    template_sql = None
    matched_template = ""
    # 用户已经在确认卡里选了口径 → 直接用那条 SQL，不再重新匹配
    if forced_sql:
        template_sql = forced_sql
    # ask 模式：命中确定性能力就先把口径摆出来给人选，不自动执行
    # （docs/确定性能力人工确认方案.md §二、§四.6）。
    if (
        not template_sql
        and not skip_capabilities
        and reuse_mode == "ask"
        and not wren_first
        and use_saved is not False
        and kind == "mysql"
    ):
        candidates = capabilities.collect_capabilities(
            message, plan, kind, want_images=want_images
        )
        if candidates:
            raise capabilities.ChooseCapability(plan, candidates)
    # 规划结果已经明确是巡查任务级概览时，优先用内置的固定关联链路；
    # 外部预置查询可能只覆盖任务总数，不能把完整概览降级成窄口径。
    if (
        not template_sql
        and not wren_first
        and nl2sql.is_patrol_route_overview_plan(plan)
    ):
        overview_sql = nl2sql.build_template_sql(plan)
        if nl2sql.is_patrol_route_overview_sql(overview_sql):
            template_sql = overview_sql
            matched_template = "patrol_route_overview"
    reusable_hit = bool(forced_sql)
    if (
        not reusable_hit
        and not forced_sql
        and reuse_mode != "off"
        and not wren_first
        and kind == "mysql"
    ):
        # 第 1 道闸（模板结构匹配）**在建检索上下文之前**先试一次：命中就不查 RAGFlow、
        # 也不要 few-shot，省一次检索往返（方案第七章 §3：命中即停）。
        try:
            from . import preset_lib

            _t = time.perf_counter()
            reusable_hit = preset_lib.match_template(plan) is not None
            log_event(
                "agent", "INFO", "template_match_ms",
                ms=round((time.perf_counter() - _t) * 1000), hit=reusable_hit,
            )
        except Exception as exc:  # noqa: BLE001
            reusable_hit = False
            log_error("agent", "template_match_failed", exc, question=message[:80])
    if reusable_hit or template_sql:
        context = {"schema": "", "examples": "", "by_dataset": {}, "references": []}
    else:
        _t = time.perf_counter()
        context = nl2sql.retrieve_query_context(message, kind, plan)
        log_event("agent", "INFO", "retrieve_ms", ms=round((time.perf_counter() - _t) * 1000))
    # 复用与"引擎 hybrid/wren"是两个维度：engine=wren 只是不拼模板。
    # "这次用哪条口径"一律由用户在确认卡上点（docs/RAG示例直达与AI直答旁路方案.md §〇）：
    # 能走到这里说明用户点了「让 AI 回答」或关掉了复用——都不该再偷偷重放示例 SQL。
    if not template_sql and reuse_mode != "off" and not wren_first and kind == "mysql":
        # 第 1 道闸：模板库**结构匹配**（shape + 主体 + 槽位覆盖），命中就按参数绑定直接执行，
        # 不走向量、也不生成 SQL（docs/固定看板重构与收藏固化方案.md 第七章 §3）。
        # 上面为了省一次检索已经试过一遍，这里再取一次绑好的 SQL（纯内存操作）。
        try:
            from . import preset_lib

            matched = preset_lib.match_template(plan)
        except Exception as exc:  # noqa: BLE001
            matched = None
            log_error("agent", "template_match_failed", exc, question=message[:80])
        if matched:
            template_sql = matched["sql"]
            matched_template = matched["id"]
    if (
        not template_sql
        and engine_mode != "wren"
        and not wren_first
        and nl2sql.is_simple_single_query(plan)
    ):
        template_sql = nl2sql.build_template_sql(plan)
    sql = template_sql
    from_template = False
    last_error = (
        react_repair.format_repair_feedback(repair_feedback)
        if repair_feedback
        else ""
    )
    # 提示词里的 AMBIGUOUS 协议：模型说"信息不足"时记下来，两个引擎都说不足才去追问用户
    ambiguous_reason = ""
    result: dict[str, Any] | None = None
    sql_risk_warnings: list[str] = []

    # 用户在确认卡里选定的口径（forced_sql）：这条 SQL 和口径他已经看过并点了确认——
    # 直接执行，不跑范围校准、不进后面"生成期校验 + 重写"的重试环
    # （docs/确认卡重复气泡耗时整改与污染模板库重置.md §二 P0-1/P0-2）。
    # 以前校准可能把确认过的查询提前截胡（返回 engine=calibrated，根本没执行他的 SQL），
    # 校验环还可能误判后用大模型重写，把用户选的模板偷偷换成 Wren/LLM 的结果。
    if sql and forced_sql:
        join_warning = nl2sql.dictionary_join_issue(sql)
        if join_warning:
            log_event(
                "agent",
                "WARNING",
                "dictionary_join_risk",
                risk=join_warning,
                question=message[:80],
            )
            sql_risk_warnings.append(join_warning)
        try:
            result = sql_engine.execute_sql(kind, sql)
        except DatabaseError as exc:
            # 如实返回数据库错误，不自由重写
            log_error("agent", "chosen_sql_failed", exc, question=message[:120])
            return _error_payload(f"你选的口径执行时报错了：{exc}")
        from_template = True

    if result is None:
        # Wren 路径：模板弃权且 Wren 可用时，让 LLM 写 MDL-SQL、由 Wren 编译成物理 SQL。
        # 编译失败/不可用都静默回退到下面的内置 LLM（方案里的三级降级）。
        if not sql and wren_engine.available(kind):
            mdl_sql = (
                nl2sql.simple_route_group_mdl(plan)
                or nl2sql.simple_patrol_route_list_mdl(plan)
                or ""
            )
            if mdl_sql:
                log_event(
                    "wren",
                    "INFO",
                    "deterministic_route_mdl",
                    question=message[:80],
                )
            # 槽位里的路线是用户原话（可能只写了"西乡段"），语义层模型会误判成"缺完整路线名"
            # 而回 AMBIGUOUS。这里把解析出来的规范路线名补进去。
            wren_slots = dict(plan["slots"])
            route_match = wren_slots.get("route_match") or {}
            if route_match.get("short_name"):
                wren_slots["route"] = str(route_match["short_name"])
            # 选表结果也要作用到写 SQL 的模型身上：选中的表展开字段明细，其余表仍然
            # 列在清单里（按需仍可用），选漏一张不至于直接写不出来。
            selected_models = nl2sql.select_tables(
                message,
                kind,
                subject=str(plan.get("subject") or ""),
                route_via_patrol=bool(plan.get("route_scope_from_patrol")),
            )
            if selected_models:
                # 选中的表里没有病害/字典表时，"病害类型"这类条件不适用，别带进 SQL。
                nl2sql.prune_conditions_for_selection(plan, selected_models)
            mdl_ctx = wren_engine.mdl_context(kind, focus=selected_models)
            if selected_models:
                mdl_ctx = (
                    "【本轮语义选表结果（优先用这几张，其他表按需才用）】："
                    + "、".join(sorted(selected_models))
                    + "\n"
                    + mdl_ctx
                )
            if not mdl_sql:
                try:
                    mdl_sql = sql_engine.generate_mdl_sql(
                        message,
                        mdl_ctx,
                        nl2sql.mdl_condition_lines(plan),
                        wren_slots,
                        feedback=last_error,
                        # P0：把 RAGFlow 检索到的 few-shot 示例也给 Wren 路径
                        examples=context.get("examples", ""),
                        # 错误记忆：以前同类问题踩过的坑，写 SQL 前先提醒一下（升级5）
                        errors=nl2sql.error_memory_hint(message),
                        # 字段真实取值：相当于让模型先查一眼再写 SQL（升级6）
                        samples=nl2sql.value_samples(kind),
                    )
                except sql_engine.AmbiguousQuestion as exc:
                    # 提示词协议：这个模型认为信息不足。别急着追问——让内置 LLM 也试一次，
                    # 两边都说信息不足才真的去问用户（实测 Wren 偶尔误判"缺路线 ID"而内置 LLM 能答对）。
                    ambiguous_reason = str(exc)
                    last_error = f"模型认为信息不足：{ambiguous_reason}"
                    log_event("wren", "INFO", "ambiguous", reason=ambiguous_reason[:120])
                except Exception as exc:
                    log_error("wren", "mdl_generate_failed", exc, question=message[:120])
            # 用带原因的版本：编译报 "No field named X" 时把该模型的真实列清单回喂重写，
            # 而不是把错误丢掉换一条路（报告 P1-8）。
            physical, compile_error = (
                wren_engine.compile_mdl_sql_verbose(kind, mdl_sql) if mdl_sql else (None, "")
            )
            if compile_error:
                last_error = f"上一版 MDL-SQL 编译失败：{compile_error}"
                log_event("wren", "INFO", "compile_feedback", reason=compile_error[:120])
            if physical:
                join_warning = nl2sql.dictionary_join_issue(physical)
                if join_warning:
                    log_event(
                        "wren",
                        "WARNING",
                        "dictionary_join_risk",
                        risk=join_warning,
                        question=message[:80],
                    )
                    sql_risk_warnings.append(join_warning)
                try:
                    result = sql_engine.execute_sql(
                        kind,
                        _enforce_top_limit(physical, plan),
                        question=message,
                    )
                    result["engine"] = "wren"
                    sql = result["sql"]
                    result["mdl_sql"] = mdl_sql
                except Exception as exc:
                    result = None
                    log_error("wren", "execute_failed", exc, question=message[:120])
                    last_error = (
                        f"上一版 SQL 执行失败：{type(exc).__name__}: {exc}"
                    )
            elif mdl_sql:
                # 编译失败：同样不要把 Wren 的内部错误当成物理 SQL 的修改建议
                last_error = ""

        # 子问题兜底（§3.3）：Wren 没跑出来就退回确定性模板，别直接把子问题丢给 LLM。
        if (
            result is None
            and template_sql is None
            and template_fallback
            and engine_mode != "wren"
        ):
            template_sql = nl2sql.build_template_sql(plan)
            sql = template_sql

        for _ in range(settings.retry_limit + 1):
            # Wren 这一步已经跑出结果了就别再重试：否则会把 Wren 编译出的 SQL
            # 再执行一遍，还会把 engine 标签覆盖成 llm（结果对、来源标错）。
            if result is not None:
                break
            candidate = sql
            if not candidate:
                try:
                    candidate = nl2sql.generate_with_context(plan, context, feedback=last_error)
                except sql_engine.AmbiguousQuestion as exc:
                    # 内置 LLM 也说信息不足 → 记下来，重试用尽后统一转成追问
                    ambiguous_reason = str(exc)
                    last_error = f"模型认为信息不足：{ambiguous_reason}"
                    continue
                except Exception as exc:
                    last_error = str(exc)
                    continue

            # 字段名防呆：用了真实表里没有的列（例如把语义层的 route_length_km 写进物理 SQL）
            # 就不要去撞数据库报 Unknown column，直接把可用列反馈回去重写。
            unknown = nl2sql.unknown_columns(candidate, kind)
            if unknown:
                last_error = (
                    "SQL 里用了不存在的列：" + "、".join(unknown[:4])
                    + "；请只用数据库里真实存在的列名（不确定就查 schema）"
                )
                # 用户在表结构里的"真实叫法"也一起回喂：按列名/列注释反查它八成想写哪个字段，
                # 不然模型只会照着同一个错名字再编一遍。
                hints = nl2sql.field_suggestions(unknown[:4], kind)
                if hints:
                    last_error += "；你要的多半是：" + "、".join(hints)
                log_event("agent", "INFO", "unknown_columns", columns="、".join(unknown[:4]))
                sql = None
                continue
            # 多租户 JOIN 风险只记录、不阻断执行。按主键 id 关联时这里通常为空；
            # 真正按非唯一 code 关联时才作为风险证据随结果返回。
            join_warning = nl2sql.dictionary_join_issue(candidate)
            if join_warning:
                log_event(
                    "agent",
                    "WARNING",
                    "dictionary_join_risk",
                    risk=join_warning,
                    question=message[:80],
                )

            from_template = bool(template_sql) and candidate == template_sql
            try:
                result = sql_engine.execute_sql(
                    kind,
                    _enforce_top_limit(candidate, plan),
                    question=message,
                )
                if join_warning and join_warning not in sql_risk_warnings:
                    sql_risk_warnings.append(join_warning)
                sql = result["sql"]
                break
            except Exception as exc:
                last_error = str(exc)
                sql = None

    if result is None:
        # 两个引擎都说"信息不足" → 如实追问，而不是硬凑一个答案
        if ambiguous_reason:
            raise clarify.ClarifyNeeded(
                kind,
                ambiguous_reason,
                lead="这条问题我还差一点信息，补充一下我马上查。",
            )
        # SQL 报错（Unknown column / 语法错）时不该让用户去挑字段，要如实说清是哪一步没跑通。
        sql_failed = bool(
            re.search(r"ERROR \d+|Unknown column|You have an error in your SQL|doesn't exist", last_error)
        )
        # "缺少必须条件"是我们自己的条件校验没过，不是用户没讲清字段，也不该甩字段清单。
        condition_failed = "缺少必须条件" in last_error or "语义" in last_error
        show_fields = not sql_failed and not condition_failed
        if plan.get("comparison") or plan.get("multi_route"):
            raise clarify.ClarifyNeeded(
                kind,
                f"多个对象的对比查询没跑通：{last_error}" if sql_failed else f"我没能把多个对象分开统计（{last_error}）",
                lead=(
                    "这条对比 SQL 执行报错了，我没有硬凑一个答案。"
                    if sql_failed
                    else "这个问题的对比对象我没对齐，直接给一个总数会误导你。"
                ),
                show_fields=show_fields,
            )
        lead = None
        if condition_failed:
            lead = "这次是我这边的条件校验没过（不是你没说清字段），所以没有硬凑一个答案。"
        elif sql_failed:
            lead = "这条 SQL 执行报错了，我没有硬凑一个答案。"
        raise clarify.ClarifyNeeded(
            kind,
            f"没能生成满足条件的 SQL：{last_error}",
            lead=lead,
            show_fields=show_fields,
            subject=str(plan.get("subject") or ""),
        )

    if not nl2sql.is_patrol_route_overview_plan(plan):
        result["rows"] = nl2sql.localize_enum_rows(
            plan,
            list(result.get("rows") or []),
            list(result.get("columns") or []),
        )
    chart_intent = "analysis" if plan["shape"] in {"distribution", "trend"} else "data"
    event_bus.emit_event(
        "phase",
        title=f"SQL 执行完成：{len(result.get('rows') or [])} 行",
    )
    result["chart_data"] = analysis_service.analyze(result, chart_intent, plan["shape"])
    result["result_id"] = str(uuid.uuid4())
    result["intent"] = intent
    result["shape"] = plan["shape"]
    result["overview"] = bool(plan["slots"].get("overview"))
    result["patrol_overview"] = bool(
        plan["slots"].get("patrol_overview")
        and nl2sql.is_patrol_route_overview_sql(sql)
    )
    result["metric_labels"] = plan["slots"].get("metric_labels")
    # 三元组里的"查询意图"：随结果一起返回，人工确认入库时一起存。
    result["spec"] = nl2sql.plan_to_spec(plan)
    result["question_optimization"] = plan.get("question_optimization")
    result["required_conditions"] = plan["required_conditions"]
    result["requested_fields"] = list(
        (plan.get("semantic") or {}).get("output_fields") or []
    )
    result["is_detail_rows"] = bool(
        result["requested_fields"]
        and len(result["requested_fields"]) > 1
        and _sql_is_row_query(result.get("sql"))
    )
    result["answer_format"] = _answer_format(plan["shape"])
    # 本轮用了哪几张表：随结果带出去，反思器/前端/日志都能看到"这次到底查了什么"，
    # 而不是各环节各猜一份（选表结果就是"本轮涉及的表/字段"的唯一来源）。
    # 用户已选口径（确认卡）时，这次涉及的表就是那条 SQL 里的表——直接从 SQL 取，
    # 不再调选表模型（那是给"要写 SQL"的场景用的，直出路径花这个钱纯属浪费）。
    if forced_sql:
        result["tables"] = sorted(sql_engine.table_names(str(forced_sql).lower()))
        result["direct"] = True
    else:
        result["tables"] = sorted(
            nl2sql.select_tables(
                message,
                kind,
                subject=str(plan.get("subject") or ""),
                route_via_patrol=bool(plan.get("route_scope_from_patrol")),
            )
        )
    # 如实区分：模板拼的 / Wren 编译的 / 内置 LLM 生成的
    result.setdefault("engine", "template" if from_template else "llm")
    if matched_template:
        result["engine"] = "template_match"
        result["matched_template"] = matched_template
    elif forced_engine:
        result["engine"] = forced_engine
    result["risk_warnings"] = list(dict.fromkeys(sql_risk_warnings))
    result["references"] = context.get("references", [])
    result["retrieval_by_dataset"] = context.get("by_dataset", {})
    # 这次理解的置信度打分（可观测：出问题时知道是哪个维度没识别出来）
    result["interpretation"] = nl2sql.interpretation_scores(plan)
    # 分档统计（老/新、大/小…）的分界线：回答里必须说明依据，别让用户看黑盒
    boundary_note = str((plan.get("slots") or {}).get("boundary_note") or "").strip()
    if boundary_note:
        result["boundary_note"] = boundary_note
    # 口径说明（"按路段分组"→按桩号分段）要在渲染前就带上，否则回答里看不到
    result.setdefault("spec", nl2sql.plan_to_spec(plan))
    # 条件里的列在库里整列都是空的时候，先算好说明（渲染时直接用）
    result.setdefault("empty_column_note", nl2sql._empty_condition_note(plan))
    # 人话口径卡片：范围/时间/对象/排除/数据截至（方案第三节第 1、8 条）
    result.setdefault("scope_card", nl2sql.scope_card(plan, list(result.get("rows") or [])))
    result["reply"] = _summarize_result(message, result)
    # 结果为 0 时给出原因（类型字典有数据无 / 数据范围没覆盖 / 时间未开始），别只回"共 0 处"
    if _is_zero_result(result):
        try:
            explanation = calibrate.explain_zero(plan)
            if not explanation:
                for condition in nl2sql.condition_clauses(plan):
                    explanation = calibrate.explain_numeric_zero(plan, condition)
                    if explanation:
                        break
        except Exception as exc:
            log_error("agent", "explain_zero_failed", exc, question=message[:120])
            explanation = None
        if explanation:
            result["reply"] = explanation
            result["answer_format"] = "calibrated"
    # 前/后 N 米是口径推断出来的，回答里必须写清楚，别让用户猜。
    span = plan["slots"].get("spatial_split")
    if span:
        result["reply"] = (
            f"（按「前{span}米 = 起点~{span}米、后{span}米 = {span}~{int(span) * 2}米」计算）\n"
            + result["reply"]
        )
    result["lineage"] = _lineage(kind, result, plan, sql)
    context = memory_service.context_from_plan(plan)
    # 记住"这批记录是谁"，下一轮问"它是什么类型/在哪条路"时按编号再查一次。
    subject = memory_service.result_subject(result)
    if subject:
        context["last_subject"] = subject
    # 本轮是"唯一一条"结果（最多的那次巡检/最高的那条病害）时，记住它的 id，
    # 下一轮"把那次巡检的明细调出来"才能按 id 过滤（docs/... Bug 6）
    entity = memory_service.result_entity(result)
    if entity:
        context["last_entity"] = entity
    # 记住本轮结果的"表格预览"，下一轮说"结合上面的表格分析"时要用
    # （docs/长对话智能化升级完整方案-给codex.md 第二部分 §4）
    preview_rows = (result.get("rows") or [])[:10]
    if preview_rows:
        context["table_preview"] = {
            "columns": result.get("columns") or list(preview_rows[0].keys()),
            "rows": preview_rows,
            "reply": str(result.get("reply") or "")[:300],
        }
    result["context"] = context
    # 本轮各主体的数值入池，供下一轮"加起来/哪边多/多多少"直接用
    entries = memory_service.pool_entries(plan, result)
    if entries:
        result["context"]["pool_entries"] = entries
    result["used_question"] = message
    # 任何 SQL 都不自动入池：示例库只收用户在界面上点过「结果正确」的案例，
    # 模板也可能会答错主体（例如把「几次巡检」答成病害数）。
    result["reviewable"] = bool(sql)
    _decorate_formula_rows(result)
    return result


_FORMULA_OP_LABELS = {"*": "×", "x": "×", "X": "×", "/": "÷", "+": "+", "-": "−"}


def readable_formula(value: Any) -> str:
    """把字典表 `formula` 那串 JSON 数组渲染成人能读的公式。

    库里的存法（实测 `ai_problem_base.formula`）：
      `[{"label":"长","value":1},{"label":"*","value":6},{"label":"宽","value":2}]`
      → 长 × 宽；带 `num` 的输入框（`{"label":"输入框","value":3,"num":0.4}`）→ 输入框(默认0.4)。

    **只按 label 拼**：`value` 看着是项的编号（长=1/宽=2/输入框=3、运算符=6），不拿它当幂次，
    免得把"长 × 宽"讲成"长^1 × 宽^2"。原始 JSON 会一并留在结果里，谁都能核对。
    """
    text = str(value or "").strip()
    if not text or not text.startswith("["):
        return ""
    try:
        items = json.loads(text)
    except ValueError:
        return ""
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        num = item.get("num")
        if num not in (None, ""):
            parts.append(f"{label}(默认{num})")
            continue
        parts.append(_FORMULA_OP_LABELS.get(label, label))
    return " ".join(parts)


def _decorate_formula_rows(result: dict[str, Any]) -> None:
    """结果里带 `formula`（计算公式）时补一列可读公式：表格和总结都看得到，
    原始 JSON 原样保留（不删列，谁也不丢信息）。"""
    rows = result.get("rows") or []
    if not rows or not isinstance(rows[0], dict):
        return
    if "formula" not in rows[0]:
        return
    changed = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = readable_formula(row.get("formula"))
        if text and not row.get("计算公式"):
            row["计算公式"] = text
            changed = True
    if changed:
        columns = [str(item) for item in (result.get("columns") or list(rows[0].keys()))]
        if "计算公式" not in columns:
            result["columns"] = columns + ["计算公式"]


def _task_title(task: dict[str, Any]) -> str:
    labels = {
        "data": "数据查询",
        "distribution": "病害分布",
        "aggregate": "聚合测算",
        "knowledge": "规范依据",
        "post_calc": "后置计算",
        "analysis": "综合分析",
    }
    return f"{labels.get(task['type'], '子任务')} {task['id']}"


def _clarify_payload(
    message: str,
    need: clarify.ClarifyNeeded,
    session_id: str | None,
) -> dict[str, Any]:
    memory_service.set_pending_question(session_id, message)
    reply = clarify.clarification_reply(
        need.kind,
        need.reason,
        lead=need.lead,
        show_fields=need.show_fields,
        subject=need.subject,
    )
    return {
        "reply": reply,
        "source": "clarification",
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "result_id": None,
        "references": [],
        "clarify": {"kind": need.kind, "question": message},
        **({"choices": need.choices} if need.choices else {}),
    }


def _error_payload(reply: str, source: str = "error") -> dict[str, Any]:
    """失败/异常的答复。

    `source` 默认是 **error**（不是 database）：反思自检、结果校验、后续建议这三步都会跳过
    error，模型就没有机会把"查询失败"改写成一段编出来的答案——实测这条路径会把
    "模型 404" 讲成"系统配置异常"，还顺手编了一张假的病害代码→名称对照表。
    """
    return {
        "reply": reply,
        "source": source,
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "result_id": None,
        "references": [],
    }


def _repair_direct_payload(message: str, payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("sections") or payload.get("engine") == "react":
        return payload
    reason = str(payload.get("reply") or payload.get("error") or "")
    sql = str(payload.get("sql") or "")
    failure: dict[str, Any] = {}
    if re.search(
        r"ERROR \d+|You have an error in your SQL|Unknown column|doesn't exist",
        reason,
        re.IGNORECASE,
    ):
        failure = react_repair.build_failure(
            "sql_runtime_error",
            stage="execute",
            reason=reason,
            evidence={"step_question": message, "sql": sql},
        )
    elif sql and str(payload.get("engine") or "") not in _DETERMINISTIC_ENGINES:
        from .tools.big.query_data import _verification_failure

        failure = _verification_failure(message, payload) or {}
    if not failure:
        return payload

    if not bool(failure.get("retryable")):
        partial = dict(payload)
        reason = str(failure.get("reason") or "查询结果表达修正失败")
        partial.update(
            {
                "accepted": False,
                "partial": True,
                "error": reason,
                "failure": failure,
                "failures": [{"step_id": 1, "failure": failure}],
            }
        )
        return partial

    from . import tools as tool_registry

    kind = cross_db.resolve_db_kind(message)
    current = failure
    for _ in range(react_repair.GLOBAL_REPAIR_LIMIT):
        observation = tool_registry.run_tool(
            "query_data",
            question=message,
            intent="data",
            kind=kind,
            wren_first=True,
            repair_feedback=current,
        )
        if observation.get("accepted"):
            result_id = str((observation.get("data") or {}).get("result_id") or "")
            repaired = get_result(result_id) if result_id else None
            if repaired:
                return repaired
        current = observation.get("failure") or current

    observed = str(observation.get("summary") or observation.get("error") or "")
    reply = observed or str(current.get("reason") or "查询没有通过验收")
    return {
        "reply": reply,
        "source": "error",
        "accepted": False,
        "partial": True,
        "error": str(current.get("reason") or reply),
        "failure": current,
        "failures": [{"step_id": 1, "failure": current}],
        "sections": [],
        "references": [],
        "result_id": str(uuid.uuid4()),
    }


def _reply_looks_incomplete(text: str) -> bool:
    """粗粒度判断模型正文是否在句中截断。只用于分析生成后的重试门控。"""
    value = str(text or "").strip()
    if len(value) < 40:
        return False
    return value[-1] not in "。！？.!?；;）)]】」”\""


def _analysis_payload(
    task: dict[str, Any],
    results: dict[int, dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """综合分析子任务：把前序的"数据结果 + 规范/知识"放一起推理，给结论。

    见 docs/长对话智能化升级完整方案-给codex.md 第二部分/第三部分：
    以前复合问题只是把子任务结果拼成"数据是XX、规范是XX"，用户要的是"结合分析、给判断和建议"。
    用户说"结合上面的表格"时，还会带上上一轮存的表格预览。
    """
    question = str(task.get("question") or "")
    data_parts: list[str] = []
    knowledge_parts: list[str] = []
    for task_id in task.get("depends_on") or []:
        dependency = results.get(int(task_id))
        if not dependency:
            continue
        reply = str(dependency.get("reply") or "").strip()
        label = str(dependency.get("result_label") or "").strip()
        rows = dependency.get("rows") or []
        dep_type = str(dependency.get("_task_type") or "")
        if dep_type == "knowledge" or (not dep_type and not rows):
            # 知识结果也可能带 preview_rows（引用片段），但要放进 knowledge_result，
            # 不能因为“有行”就误归到数据库结果。
            knowledge_parts.append(reply[:1500])
        elif rows:
            data_parts.append(
                f"- {label + '：' if label else ''}{reply[:1200]}\n  明细"
                + ("（全部）" if len(rows) <= 80 else f"（前 80/{len(rows)} 行）")
                + "："
                + json.dumps(rows[:80], ensure_ascii=False, default=str)[:8000]
            )
        elif reply:
            data_parts.append(f"- {label + '：' if label else ''}前置数据无结果：{reply[:400]}")
    # 依赖没标全时兜底：把其余子结果也带上（宁可多给上下文，不可漏）
    for task_id, dependency in results.items():
        if int(task_id) in (task.get("depends_on") or []):
            continue
        reply = str(dependency.get("reply") or "").strip()
        if not reply:
            continue
        label = str(dependency.get("result_label") or "").strip()
        dep_type = str(dependency.get("_task_type") or "")
        if dep_type == "knowledge":
            knowledge_parts.append(reply[:800])
        elif dependency.get("rows"):
            data_parts.append(f"- {label + '：' if label else ''}{reply[:300]}")
        else:
            knowledge_parts.append(reply[:800])
    if "密度" in question:
        derived = _density_rows(
            question,
            [
                dependency
                for task_id, dependency in results.items()
                if int(task_id) in (task.get("depends_on") or [])
            ],
        )
        if derived:
            data_parts.append(
                "\n- 确定性密度计算结果（直接采用，不要重算）：\n"
                + json.dumps(
                    derived.get("rows") or [],
                    ensure_ascii=False,
                    default=str,
                )[:8000]
            )
    previous_table = ""
    if any(word in question for word in ("上面", "上表", "表格", "刚才", "前面", "上一轮")):
        context = memory_service.load_session(session_id) or {}
        preview = context.get("table_preview") or {}
        if preview:
            previous_table = (
                f"字段：{json.dumps(preview.get('columns'), ensure_ascii=False)}\n"
                f"数据：{json.dumps(preview.get('rows'), ensure_ascii=False, default=str)[:900]}\n"
                f"当时的结论：{str(preview.get('reply') or '')[:200]}"
            )
    try:
        messages = prompts.messages(
            "analysis",
            question=question,
            data_result="\n".join(data_parts)[:12000] or "（本次没有新的数据查询）",
            knowledge_result="\n".join(knowledge_parts)[:3000] or "（没有检索到相关规范）",
            previous_table=previous_table or "（无）",
        )
        reply, thinking = chat_detail(
            messages,
            temperature=0,
            model=role_model("knowledge"),
            max_tokens=settings.knowledge_max_tokens,
        )
        if _reply_looks_incomplete(reply):
            log_event("agent", "WARNING", "analysis_reply_truncated", question=question[:80])
            try:
                retried, retry_thinking = chat_detail(
                    [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                "上一次输出在句中截断。请重新回答，控制在 600 字以内，"
                                "先回答用户问的关键结论，再给必要依据，必须完整收尾。"
                            ),
                        },
                    ],
                    temperature=0,
                    model=role_model("knowledge"),
                    max_tokens=settings.knowledge_max_tokens,
                )
                if retried and (
                    not _reply_looks_incomplete(retried) or len(retried) > len(reply)
                ):
                    reply, thinking = retried, retry_thinking
            except Exception as exc:  # noqa: BLE001
                log_error("agent", "analysis_truncated_retry_failed", exc, question=question[:120])
        if _reply_looks_incomplete(reply):
            reply = (
                "分析模型返回的回答不完整。已获取的数据事实："
                + ("；".join(data_parts)[:800] or "（无）")
                + "\n规范依据："
                + ("；".join(knowledge_parts)[:800] or "（无）")
            )
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "analysis_failed", exc, question=question[:120])
        fallback = "；".join(part.strip() for part in data_parts + knowledge_parts if part.strip())
        reply, thinking = (fallback or "这次没能完成综合分析。"), ""
    return {
        "reply": reply,
        "thinking": thinking,
        "source": "analysis",
        "engine": "analysis",
        "answer_format": "text",
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "result_id": str(uuid.uuid4()),
        "references": [],
    }


def _numeric_value(dep: dict[str, Any], key: str = "") -> float | None:
    if key:
        produced = dep.get("produced") or {}
        try:
            return float(str(produced.get(key)))
        except (TypeError, ValueError):
            pass
    for row in dep.get("rows") or []:
        if not isinstance(row, dict):
            continue
        for name, value in row.items():
            if key and key not in str(name):
                continue
            try:
                return float(str(value))
            except (TypeError, ValueError):
                continue
    if not key:
        for row in dep.get("rows") or []:
            if not isinstance(row, dict):
                continue
            for value in row.values():
                try:
                    return float(str(value))
                except (TypeError, ValueError):
                    continue
    return None


def _ratio_and_average_conclusion(
    question: str,
    task: dict[str, Any],
    deps: list[dict[str, Any]],
) -> str:
    need_items = [
        (int(item.get("from_step") or 0), str(item.get("key") or ""))
        for item in task.get("needs") or []
        if isinstance(item, dict) and item.get("from_step")
    ]
    needs = {step_id: key for step_id, key in need_items}
    dep_ids = [int(item) for item in task.get("depends_on") or []]
    denominator = None
    numerator = None
    denominator_item = next(
        (
            (step_id, key)
            for step_id, key in need_items
            if any(word in key.lower() for word in ("total", "count", "处数", "总数"))
        ),
        (0, ""),
    )
    denominator_id, denominator_key = denominator_item
    for step_id, dep in zip(dep_ids, deps):
        value = _numeric_value(
            dep,
            denominator_key if step_id == denominator_id else needs.get(step_id, ""),
        )
        if value is None:
            value = _numeric_value(dep)
        if step_id == denominator_id:
            denominator = value
        elif numerator is None:
            numerator = value
    if numerator is None and len(deps) > 1:
        numerator = _numeric_value(deps[-1])
        denominator = denominator or _numeric_value(deps[0])
    parts: list[str] = []
    if denominator and numerator is not None:
        if any(word in question for word in ("比例", "占比", "百分比")):
            parts.append(f"占比 {numerator / denominator * 100:.2f}%")
        if "平均面积" in question:
            total = _numeric_value(deps[0], "total_area")
            count = _numeric_value(deps[0], "total_count")
            if total is not None and count:
                parts.append(f"平均面积 {total / count:.2f}㎡")
    return "；".join(parts)


def _per_group_max_rows(
    question: str,
    deps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """从“路线 × 类型 × 数量”结果中确定性地取每条路线数量最大的一行。"""
    if not re.search(r"每条|每个|每一组|各组|每组", question) or not re.search(
        r"最多|最大|Top|数量最大|主要病害",
        question,
        re.I,
    ):
        return None
    source_rows = [
        dict(row)
        for dep in deps
        for row in (dep.get("rows") or [])
        if isinstance(row, dict) and row
    ]
    if not source_rows:
        return None
    columns = list(source_rows[0])
    numeric_columns: list[str] = []
    for column in columns:
        values = [str(row.get(column) or "").strip() for row in source_rows]
        numbers = [value for value in values if re.fullmatch(r"-?\d+(?:\.\d+)?", value)]
        if numbers and len(numbers) >= max(1, len(values) // 2):
            numeric_columns.append(column)
    if not numeric_columns:
        return None
    metric = next(
        (
            column
            for column in numeric_columns
            if re.search(r"数量|处数|总数|病害数|count|次数|total", column, re.I)
        ),
        numeric_columns[-1],
    )
    group = next(
        (
            column
            for column in columns
            if column != metric
            and any(
                not re.fullmatch(r"-?\d+(?:\.\d+)?", str(row.get(column) or "").strip())
                for row in source_rows
            )
        ),
        "",
    )
    if not group:
        return None
    label = next(
        (
            column
            for column in columns
            if column not in {group, metric}
            and any(
                not re.fullmatch(r"-?\d+(?:\.\d+)?", str(row.get(column) or "").strip())
                for row in source_rows
            )
        ),
        "",
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in source_rows:
        key = str(row.get(group) or "")
        if not key:
            continue
        try:
            value = float(str(row.get(metric)))
        except (TypeError, ValueError):
            continue
        previous = grouped.get(key)
        if previous is None or value > float(previous["_value"]):
            grouped[key] = {**row, "_value": value}
    result_rows: list[dict[str, Any]] = []
    for row in grouped.values():
        item = {group: row.get(group), metric: row.get(metric)}
        if label:
            item[label] = row.get(label)
        result_rows.append(item)
    if not result_rows:
        return None
    selected = [group, metric] + ([label] if label else [])
    summary = "；".join(
        f"{row.get(group)}："
        + (f"{row.get(label)} " if label else "")
        + f"{row.get(metric)}"
        for row in result_rows
    )
    return {
        "reply": summary,
        "source": "calc",
        "engine": "post_calc",
        "accepted": True,
        "columns": selected,
        "rows": result_rows,
    }


def _density_rows(
    question: str,
    deps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """按分组行确定性地计算“数量 / 长度”密度。"""
    if "密度" not in question:
        return None
    rows = [
        dict(row)
        for dep in deps
        for row in (dep.get("rows") or [])
        if isinstance(row, dict) and row
    ]
    if not rows:
        return None
    columns = list(rows[0])
    count_column = next(
        (
            column
            for column in columns
            if re.search(r"(?:病害|泛油)?(?:总数|数量|处数)|count", column, re.I)
        ),
        "",
    )
    area_column = next(
        (
            column
            for column in columns
            if re.search(r"总面积|面积总和|^面积$", column)
        ),
        "",
    )
    length_column = next(
        (
            column
            for column in columns
            if re.search(r"长度|(?:^|_)km$|公里", column, re.I)
        ),
        "",
    )
    label_column = next(
        (
            column
            for column in columns
            if column not in {count_column, length_column}
            and any(
                not re.fullmatch(r"-?\d+(?:\.\d+)?", str(row.get(column) or "").strip())
                for row in rows
            )
        ),
        "",
    )
    if not (count_column and length_column and label_column):
        return None
    area_density = bool(re.search(r"面积\s*[/÷]\s*长度|面积密度", question))
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            count = float(str(row.get(count_column)))
            length = float(str(row.get(length_column)))
        except (TypeError, ValueError):
            continue
        if length <= 0:
            continue
        item = {
            label_column: row.get(label_column),
            count_column: row.get(count_column),
            length_column: row.get(length_column),
            "密度_处_per_km": round(count / length, 4),
        }
        if area_column:
            try:
                area = float(str(row.get(area_column)))
            except (TypeError, ValueError):
                area = None
            item[area_column] = row.get(area_column)
            item["面积密度_㎡_per_km"] = (
                round(area / length, 4) if area is not None else None
            )
        out.append(item)
    if not out:
        return None
    metric = "面积密度_㎡_per_km" if area_density and area_column else "密度_处_per_km"
    out.sort(
        key=lambda item: float(item.get(metric) or 0),
        reverse=True,
    )
    top = out[0]
    reply = (
        f"密度最高的是{top[label_column]}，"
        f"{top['密度_处_per_km']} 处/公里"
        f"（{top[count_column]} 处 ÷ {top[length_column]} 公里）"
    )
    if area_column:
        reply += f"；面积密度 {top['面积密度_㎡_per_km']} ㎡/公里"
    reply += "。"
    selected = [label_column, count_column]
    if area_column:
        selected.append(area_column)
    selected.extend([length_column, "密度_处_per_km"])
    if area_column:
        selected.append("面积密度_㎡_per_km")
    return {
        "reply": reply,
        "source": "calc",
        "engine": "post_calc",
        "accepted": True,
        "columns": selected,
        "rows": out,
    }


def _per_group_average_rows(
    question: str,
    deps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """按现有分组行计算“面积 / 数量”的平均值，避免再让 LLM 猜。"""
    if "平均" not in question or not re.search(
        r"每(?:一)?条|各路线|按路线|每个路线",
        question,
    ):
        return None
    rows = [
        dict(row)
        for dep in deps
        for row in (dep.get("rows") or [])
        if isinstance(row, dict) and row
    ]
    if not rows:
        return None
    columns = list(rows[0])
    numerator = next(
        (
            column
            for column in columns
            if re.search(r"总面积|面积总和|^面积$", column)
        ),
        "",
    )
    denominator = next(
        (
            column
            for column in columns
            if re.search(r"病害总数|总数|数量|处数|count", column, re.I)
        ),
        "",
    )
    if not (numerator and denominator):
        return None
    output_column = "平均单处病害面积"
    output_rows: list[dict[str, Any]] = []
    for row in rows:
        try:
            total = float(str(row.get(numerator)))
        except (TypeError, ValueError):
            total = None
        try:
            count = float(str(row.get(denominator)))
        except (TypeError, ValueError):
            count = None
        output_rows.append(
            {
                **row,
                output_column: (
                    round(total / count, 4)
                    if total is not None and count
                    else None
                ),
            }
        )
    if not output_rows:
        return None
    return {
        "reply": (
            f"已按 {numerator} ÷ {denominator} 计算 {output_column}，"
            f"共 {len(output_rows)} 条路线，完整结果见表格。"
        ),
        "source": "calc",
        "engine": "post_calc",
        "accepted": True,
        "columns": columns + [output_column],
        "rows": output_rows,
    }


def _post_calc_payload(
    task: dict[str, Any],
    results: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """后置计算子任务：基于前序结果算差值/比大小，不查数据库。

    见 docs/问题拆分重构方案-LLM语义拆分.md：常见的"哪条更长/多多少/差多少"用确定性算术，
    更复杂的（比例、增长率）才交给 LLM，且只允许用前序结果里的数字。
    """
    deps = [results[tid] for tid in (task.get("depends_on") or []) if tid in results]
    rows = [row for dep in deps for row in (dep.get("rows") or [])]
    question = str(task.get("question") or "")
    density = _density_rows(question, deps)
    if density:
        return density
    average_rows = _per_group_average_rows(question, deps)
    if average_rows:
        return average_rows
    arithmetic = _ratio_and_average_conclusion(question, task, deps)
    if arithmetic:
        return {
            "reply": arithmetic,
            "source": "calc",
            "engine": "post_calc",
            "accepted": True,
            "columns": [],
            "rows": [],
        }
    grouped_max = _per_group_max_rows(question, deps)
    if grouped_max:
        return grouped_max
    # 1) 确定性：两个对象比大小/求差（覆盖绝大多数场景）
    conclusion = _difference_conclusion(question or "多多少", rows)
    if conclusion:
        return {
            "reply": conclusion.strip("（）"),
            "source": "calc",
            "engine": "post_calc",
            "accepted": True,
            "columns": [],
            "rows": [],
        }
    # 2) 兜底：把前序结果交给 LLM 做算术（不查库）
    context_parts: list[str] = []
    for dep in deps:
        rows = list(dep.get("rows") or [])
        columns = list(dep.get("columns") or [])
        block = f"- {dep.get('question', '')}: {dep.get('reply', '')}"
        if rows:
            block += (
                f"\n  列：{json.dumps(columns, ensure_ascii=False)}\n"
                f"  行（前 {min(len(rows), 80)} 条）："
                + json.dumps(rows[:80], ensure_ascii=False, default=str)
            )
        context_parts.append(block)
    context = "\n".join(context_parts) or "（没有可用的前序结果）"
    try:
        reply = chat(
            prompts.messages(
                "post_calc",
                question=question,
                calc_logic=str(task.get("calc_logic") or question),
                context=context,
            ),
            temperature=0,
            # 后置计算要读懂前序结果的口径（哪个数除以哪个数），用语义理解模型
            model=role_model("semantic"),
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "post_calc_failed", exc, question=question[:120])
        reply = ""
    if not reply:
        reply = "这一步需要基于前面的结果做计算，但数据不足，算不出来。"
    return {
        "reply": reply,
        "source": "calc",
        "engine": "post_calc",
        "accepted": bool(reply),
        "columns": [],
        "rows": [],
    }


# ── 链式依赖回填（docs/多步推理链式依赖整改方案.md）──────────────────────────
# "7月病害最多的那一天，主要是什么类型"——第 2 步的条件来自第 1 步的答案。
# 规划器用 {{key}} 占位符声明依赖、用 produces 声明产出，这里负责填值。
def _fill_placeholders(question: str, values: dict[str, str]) -> tuple[str, list[str]]:
    """把 {{key}} 换成本步之前已经查出来的真实值；返回（填好的问题, 缺哪些 key）。"""
    missing: list[str] = []

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = str(values.get(key) or "").strip()
        if not value:
            missing.append(key)
            return match.group(0)
        return value

    return re.sub(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}", replace, question), missing


def _extract_produced(rows: list[dict[str, Any]], item: dict[str, Any]) -> str:
    """按 produces 声明的 from_column 从结果第一行取值（取不到就退第一列）。"""
    if not rows:
        return ""
    key = str(item.get("key") or "").strip()
    is_multi_value = bool(
        re.search(
            r"(?:routes|weeks|months|days|types|dates|patrols|devices)$",
            key,
            re.IGNORECASE,
        )
    )
    is_scalar_key = bool(
        re.match(
            r"^(?:top|peak|max|min|count|avg|sum|latest|earliest)_",
            key,
            re.IGNORECASE,
        )
    )
    if (
        len(rows) > 1
        and not str(item.get("from_column") or "").strip()
        and not is_multi_value
        and not is_scalar_key
    ):
        # 无列、非单数键的 produces 表示“整张前序结果”，不是文本槽位。
        return ""
    if len(rows) > 1 and not is_multi_value:
        # 单数键（top_route、max_area…）只取排名第一行；把多行合成字符串会
        # 把“最勤路线”写成十条路线串，后续分析也无法比较。
        rows = rows[:1]
    row = rows[0]
    column = str(item.get("from_column") or "").strip()

    def _value_of(target: dict[str, Any]) -> str:
        if column:
            if column in target:
                text = _normalize_key_value(column, target.get(column))
                if text:
                    return text
            # 列存在但这一行是空的（实测 Top-N 的结果里只有第一行填了分组标签列）→
            # **不要就此返回空**，继续走下面的"第一个非数字值"兜底，否则多值会被吃掉。
        for name, value in target.items():
            text = str(value or "").strip()
            if not text or text.upper() == "NULL":
                continue
            if re.match(r"^-?\d+(?:\.\d+)?$", text):
                continue
            return _normalize_key_value(str(name), value)
        return ""

    # 「前 N 名」这类**多行**结果（"哪三个星期最多"）：把 N 个标签连成一句话
    # （`2026年第31周、2026年第30周、2026年第29周`），第二步才能按"这几个"一次性过滤。
    # 判据是**数据本身有 N 行**（第 1 步要的就是前 N 名），不是关键词。
    if len(rows) > 1:
        joined: list[str] = []
        for target in rows:
            text = _value_of(target)
            if text and text not in joined:
                joined.append(text)
        if joined:
            return "、".join(joined)
    if column:
        if column in row:
            return _normalize_key_value(column, row.get(column))
        # 列名对不上（模型申明的 from_column 和实际列名不一致）时，退到下面的
        # "第一个非数字值"兜底——不要取第一列（实测把"宝安中心--恒明珠"当分布数据填了下去），
        # 也不要直接放弃（会让整条链断在"缺依赖"）。
    # 兜底：取**第一个非数字值**——"排序取第一条"的结果里，
    # 标签列（路线名/类型名/编号）永远是文本，指标列是数字，所以这比"取第一列"稳得多。
    for name, value in row.items():
        text = str(value or "").strip()
        if not text or text.upper() == "NULL":
            continue
        if re.match(r"^-?\d+(?:\.\d+)?$", text):
            continue
        return _normalize_key_value(str(name), value)
    return ""


def _normalize_key_value(column: str, value: Any) -> str:
    """回填值的形态：日期转成"2026年7月30日"，其它原样（见方案"改动3"）。"""
    if value in (None, "", "NULL"):
        return ""
    text = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        head = text[:10]
        year, month, day = head.split("-")
        return f"{int(year)}年{int(month)}月{int(day)}日"
    return text


def _handle_composite(
    message: str,
    scope: list[str] | None = None,
    session_id: str | None = None,
    force_ai: bool = False,
    plan: dict[str, Any] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    intent: str = "data",
    original_message: str | None = None,
) -> dict[str, Any]:
    # 「按步骤答 / 每步选口径」已删除：复合题只摆**整题卡**（一次答对整条），
    # 或者让 AI 走 Wren；子步骤不再有候选卡，也不会带着"用户逐步选择"进来。
    # tasks 由调用方传进来时（image 路由已经先拆过一次）直接复用，不重复规划。
    tasks = (
        tasks
        if tasks is not None
        else cross_db.decompose_cross_db(
            message,
            scope,
            original_message=original_message or message,
        )
    )
    # 复合题也先当"普通题"过一遍确定性闸（用户要求：先模板/示例匹配，靠用户决策；
    # 选了模板就直接出结果、跳过后面的所有步骤）。两条硬约束：
    #   · 数据 + 规范混合的问题不能被模板截胡（只答了数字、漏了规范 —— H2/H3/H10 栽过）；
    #   · 只摆确定性模板 / 相似已验证问法，通用算子、代码模板不参与（它们最容易只答一半）。
    # 没有整题候选就直接进各步子任务（子步骤统一走 Wren）。
    if (
        not force_ai
        and len(tasks) > 1
        and str(chat_settings.load().get("reuse_mode") or "ask") == "ask"
    ):
        try:
            # 注意：**不要在这里写 `from . import nl2sql`**——那会让 `nl2sql` 在**整个函数**里
            # 变成局部名，函数前面用到它的地方（子步骤设 `set_round_request`）就会抛
            # `cannot access local variable 'nl2sql'`（实测把链式题第一步整成 0 行）。
            # 模块级已经有 `from . import nl2sql`（见文件头），这里直接用即可。
            kind = cross_db.resolve_db_kind(message)
            plan = plan or nl2sql.build_query_plan(message, kind)
            candidates = capabilities.collect_capabilities(
                message, plan, kind, deterministic_only=True
            )
        except Exception as exc:  # noqa: BLE001
            candidates = []
            log_error("agent", "composite_whole_gate_failed", exc, question=message[:120])
        if candidates:
            raise capabilities.ChooseCapability(plan, candidates, tasks=tasks)
    # 子步骤统一走 Wren，不再摆"每一步选口径"的候选卡（用户要求：要么一次答出来，要么靠 Wren）。
    if len(tasks) <= 1:
        try:
            from . import react_agent

            goal = next(
                (
                    str(item.get("_plan_goal") or "").strip()
                    for item in tasks
                    if str(item.get("_plan_goal") or "").strip()
                ),
                message,
            )
            brain_payload = react_agent.run(
                message,
                {
                    "is_simple": True,
                    "intent": intent,
                    "final_goal": goal,
                    "steps": tasks,
                    "spec": nl2sql.plan_to_spec(plan) if plan else {},
                },
            )
            if brain_payload:
                return brain_payload
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "react_single_branch_failed", exc, question=message[:120])
        kind = tasks[0].get("kind") if tasks else None
        result = _build_query_result(
            message,
            "data",
            kind=kind,
            wren_first=force_ai,
            skip_capabilities=force_ai,
            use_saved=False if force_ai else None,
        )
        save_result(result["result_id"], result)
        return {
            "reply": result["reply"],
            "source": result.get("source"),
            "sql": result.get("sql"),
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "chart_data": result.get("chart_data"),
            "result_id": result.get("result_id"),
            "shape": result.get("shape"),
            "answer_format": result.get("answer_format"),
            "lineage": result.get("lineage"),
            "references": result.get("references", []),
            "engine": result.get("engine"),
            "question": message,
            "used_question": result.get("used_question"),
            "reviewable": result.get("reviewable"),
            # 单任务也走复合分支时，必须把上下文带回去存进会话，否则下一句追问接不上。
            "context": result.get("context"),
            "metric_labels": result.get("metric_labels"),
            "spec": result.get("spec"),
            "risk_warnings": result.get("risk_warnings") or [],
        }

    # 阶段 2：只在多步且存在 produces/needs 的纯数据链上启用 ReAct。
    # 简单题、图片/知识混合题以及大脑执行失败仍走下面的静态循环。
    try:
        from . import react_agent

        if react_agent.should_use_react(message, tasks):
            goal = next(
                (
                    str(item.get("_plan_goal") or "").strip()
                    for item in tasks
                    if str(item.get("_plan_goal") or "").strip()
                ),
                message,
            )
            semantic_spec = nl2sql.plan_to_spec(plan) if plan else {}
            brain_payload = react_agent.run(
                message,
                {
                    "is_simple": False,
                    "intent": intent,
                    "final_goal": goal,
                    "steps": tasks,
                    "spec": semantic_spec,
                },
            )
            if brain_payload:
                return brain_payload
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "react_branch_failed", exc, question=message[:120])

    sections: list[dict[str, Any]] = []
    all_references: list[dict[str, Any]] = []
    # 后置计算要按依赖顺序拿到前序结果（LLM 拆分器会给 depends_on）
    task_results: dict[int, dict[str, Any]] = {}
    # 链式依赖：前面步骤产出的关键值（peak_date/patrol_number/route_name…）填给后面的步骤
    produced: dict[str, str] = {}

    def _dependency_failure(task: dict[str, Any]) -> str:
        dependency_ids = list(task.get("depends_on") or [])
        if not dependency_ids and task.get("type") == "analysis":
            dependency_ids = list(task_results)
        for task_id in dependency_ids:
            dependency = task_results.get(int(task_id))
            if not dependency:
                return f"第 {task_id} 步没有可用结果，综合分析没有执行。"
            source = str(dependency.get("source") or "")
            if source in {"error", "clarification"} or dependency.get("error"):
                return (
                    f"第 {task_id} 步没有成功返回数据，综合分析没有执行。"
                    + (f"原因：{dependency.get('reply')}" if dependency.get("reply") else "")
                )
            dependency_type = str(dependency.get("_task_type") or "")
            if dependency_type in {"data", "aggregate"} and not dependency.get("rows"):
                return (
                    f"前置数据查询没有返回结果（第 {task_id} 步），"
                    "依赖它的综合分析没有执行，避免基于空数据编造结论。"
                )
        return ""

    def _composite_failure(
        reason: str,
        task: dict[str, Any],
        question: str,
    ) -> dict[str, Any]:
        failed = {
            "id": task.get("id"),
            "type": task.get("type"),
            "title": _task_title(task),
            "question": question,
            "reply": reason,
            "source": "error",
            "error": reason,
            "columns": [],
            "rows": [],
            "references": [],
        }
        failed_sections = [*sections, failed]
        return {
            "reply": reason,
            "source": "error",
            "sections": failed_sections,
            "references": all_references,
            "result_id": str(uuid.uuid4()),
            "answer_format": "text",
            "error": reason,
            "plan": plan or {"steps": tasks},
            "process": {
                "steps": [
                    {
                        "id": item.get("id"),
                        "type": item.get("type"),
                        "label": str(item.get("label") or ""),
                        "engine": item.get("engine"),
                        "rows": len(item.get("rows") or []),
                        "images": len(item.get("images") or []),
                        "wants_images": bool(item.get("wants_images")),
                        "question": str(item.get("question") or ""),
                        "error": item.get("error"),
                    }
                    for item in failed_sections
                ],
                "chain_values": dict(produced),
            },
        }

    for task in tasks:
        task_kind = task.get("kind")
        # 单个子任务失败（或需要澄清）不应该把整段回答废掉，其余的照常给。
        task_question = str(task.get("question") or "")
        # 这一步是不是"用上一步的结果当条件"（query 里有 {{key}}）：有的话，它的条件来自链路，
        # 没的话只能来自**本轮完整问题（优化后）**（见下面 set_round_request 的说明）。
        used_placeholders = "{{" in task_question
        # 回填要**先于**上下文补全：子问题自己的条件（尤其时间）在 `{{key}}` 里，
        # 必须先还原成完整问法，再交给后续语义解析。
        filled_question, missing_keys = _fill_placeholders(task_question, produced)
        task_question = filled_question
        # 分析/后置计算不查库，文本里缺个把 key 不影响（它还能拿到前序结果），不跳过
        if missing_keys:
            reason = (
                f"这一步需要前面步骤查出的「{'、'.join(missing_keys)}」，"
                "但上一步没有产出该值，所以没有继续执行依赖它的分析。"
            )
            if task["type"] in {"analysis", "post_calc"}:
                return _composite_failure(reason, task, task_question)
            sections.append(
                {
                    "id": task["id"],
                    "type": task["type"],
                    "title": _task_title(task),
                    "question": task_question,
                    "reply": reason,
                    "source": "clarification",
                    "columns": [],
                    "rows": [],
                    "error": "missing_dependency",
                }
            )
            continue
        if task["type"] in {"analysis", "post_calc"}:
            reason = _dependency_failure(task)
            if reason:
                return _composite_failure(reason, task, task_question)
        # 子问题也要继承会话里的粗条件（路线/时间/病害类型）——实测拆分后的子问题
        # 因为"缺路线名称"直接失败，而路线就在会话上下文里（和追问补全走同一套）。
        # 这一步只**补缺**：子问题自己已经带的（上面刚填好的时间/路线）不会再补。
        # ⚠️ 必须尊重前端的"沿用上轮"开关：以前这里无条件补全，**关掉沿用**后子问题照样
        # 被塞进上一轮的"巡查任务 XJJL…/路面类型=沥青…"（实测同一条前缀在 5 个步骤里重复出现）。
        if (
            task["type"] in {"data", "aggregate", "analysis"}
            and "{{" not in task_question
            and chat_settings.load().get("inherit_context", True)
        ):
            try:
                inherited_question, _ = memory_service.contextualize(task_question, session_id)
                if inherited_question and len(inherited_question) <= 200:
                    task_question = inherited_question
            except Exception:  # noqa: BLE001
                pass
        try:
            if task["type"] == "post_calc":
                payload = _post_calc_payload(task, task_results)
            elif task["type"] == "analysis":
                # 综合分析：把前序 data/knowledge 的结果放一起推理，给结论（不是罗列）
                payload = _analysis_payload(task, task_results, session_id)
            elif task["type"] == "knowledge":
                payload = _answer_knowledge(task_question)
            elif (
                # 这一步被标成"看图"（跨库判定给了 kind=seaweedfs、规划 action=query_images、
                # 或每步的 wants_images），但**都要过一遍语义校验**：这一步自己的问法是不是真要看图。
                # 只对已标成看图的子问题校验（子问题是新句子、语义判定要多花一次模型往返，
                # 对每个 data 子问题都判一次会让复合题白等）。
                # 为什么必须有这道闸：整句带"图片"时跨库判定会把**整句**判成对象存储，
                # 于是两个子步骤都带上 kind=seaweedfs——实测第 1 步"哪个星期平均面积最大"
                # 被当成看图走了照片口径（rows=192 张图），周均面积根本没算、链路也断了。
                (
                    task_kind == "seaweedfs"
                    or task["type"] == "image"
                    or task.get("wants_images")
                )
                and intent_service.wants_images(task_question)
            ):
                # force_ai 一路透传：用户点了"让 AI 自己查"，子问题也不许再摆确认卡
                payload = _image_result(task_question, force_ai=force_ai)
            else:
                task_intent = "analysis" if task["type"] == "aggregate" else "data"
                # 子步骤没有"用户逐步选口径"这条路了（步骤卡已删）：一律按
                # "简单走模板、其余走 Wren"分流，Wren 跑不出来再退回模板。
                step_sql = ""
                step_plan = None
                step_engine = ""
                # seaweedfs 只是"这一步要看图"的**标记**，不是能查业务数据的库：图片路径本来
                # 就存在 MySQL（`ai_problem.problem_url` …）。整句带"图片"时跨库判定会给整句
                # 挂上这个标记，子步骤跟着带上——实测第 1 步"哪个星期平均面积最大"拿着
                # kind=seaweedfs 去查库，字段目录直接报"不支持的数据库类型：seaweedfs"、
                # Wren 还用了别的项目（只有 gps_point/inclination_point）。数据步统一落 MySQL。
                step_kind = "mysql" if task_kind == "seaweedfs" else task_kind
                # 子问题同样按"简单走模板、其余走 Wren"分流；Wren 跑不出来再退回模板。
                # 把**本轮完整问题（优化后）**告诉语义层：这一步的问法是拆解器改写的，里面可能夹举例值
                # （"（如'2026年第31周'）"）。语义层只看得见改写文本时，会把例子当成真条件，
                # 整步被缩到那一周 → 数就错了。给了完整问题它才能判断"这个时间到底要不要当条件"
                # （长对话里优化器补全出来的路线/时间是合法的，照填）。
                # 例外：声明了链路依赖的步骤（{{key}} 回填过），它的条件来自上一步的结果，
                # 那不是"编的"——这时把完整问题限定撤掉，让这一步用自己的问法。
                if used_placeholders:
                    nl2sql.clear_round_request()
                else:
                    nl2sql.set_round_request(message)
                try:
                    payload = _build_query_result(
                        task_question,
                        task_intent,
                        kind=step_kind,
                        template_fallback=True,
                        forced_sql=step_sql or None,
                        forced_engine=step_engine,
                        plan=step_plan if step_sql else None,
                        # 子步骤一律不再弹候选卡：要么用用户在前面选的，要么直接走 Wren
                        # （复合题的候选收集在 _handle_composite 开头统一做）。
                        skip_capabilities=True,
                        wren_first=(not step_sql),
                    )
                finally:
                    nl2sql.clear_round_request()
                save_result(payload["result_id"], payload)
                # 这一步声明产出什么关键值，就按 from_column 从结果里取出来，填给后面依赖它的步骤
                for item in task.get("produces") or []:
                    key = str(item.get("key") or "").strip()
                    if not key:
                        continue
                    value = _extract_produced(list(payload.get("rows") or []), item)
                    if value:
                        produced[key] = value
                        log_event(
                            "agent",
                            "INFO",
                            "chain_produce_debug",
                            step=int(task.get("id") or 0),
                            rows=len(payload.get("rows") or []),
                            key=key,
                            from_column=str(item.get("from_column") or ""),
                            value=value[:60],
                        )
                        log_event(
                            "agent",
                            "INFO",
                            "chain_value_filled",
                            key=key,
                            value=value[:40],
                            column=str(item.get("from_column") or "")[:20],
                        )
        except clarify.ClarifyNeeded as need:
            payload = {
                "reply": clarify.clarification_reply(
                    need.kind,
                    need.reason,
                    subject=need.subject,
                    # 子问题这条以前漏了 show_fields：我方"缺少必须条件"的生成失败会被当成
                    # "用户没说清字段"，于是反问"哪种病害/哪一段桩号"（实测桥梁题就是被这么问的）
                    show_fields=need.show_fields,
                ),
                "source": "clarification",
                "rows": [],
                "columns": [],
            }
        except Exception as exc:
            log_error("agent", "subtask_failed", exc, question=task["question"][:120])
            payload = {
                "reply": f"这个子问题这次没做成：{exc}",
                "source": "error",
                "rows": [],
                "columns": [],
            }

        result_label = next(
            (
                str(item.get("label") or "").strip()
                for item in task.get("produces") or []
                if str(item.get("label") or "").strip()
            ),
            "",
        )
        if result_label:
            payload["result_label"] = result_label
        section = {
            "id": task["id"],
            "type": task["type"],
            "title": _task_title(task),
            "label": result_label,
            "question": task_question,
            "reply": payload.get("reply", "已完成"),
            "source": payload.get("source"),
            "sql": payload.get("sql"),
            "columns": payload.get("columns", []),
            "rows": payload.get("rows", []),
            "chart_data": payload.get("chart_data"),
            "result_id": payload.get("result_id"),
            "shape": payload.get("shape"),
            "answer_format": payload.get("answer_format"),
            "lineage": payload.get("lineage"),
            "references": payload.get("references", []),
            "engine": payload.get("engine"),
            "images": payload.get("images", []),
            # 拿到了路径但存储里没有文件的那些：分段里也要带上——回答里会如实写出来，
            # 排查/回归也据此区分"没找到图"和"图找到了但文件不在存储里"。
            "missing_images": payload.get("missing_images") or [],
            "used_question": payload.get("used_question"),
            "reviewable": payload.get("reviewable"),
            "context": payload.get("context"),
            "spec": payload.get("spec"),
            # 这一步是不是"要图"（planning 的 wants_images）：中间过程里能看到，
            # 排查"图挂在哪一步"时不用再猜。
            "wants_images": bool(task.get("wants_images")),
        }
        sections.append(section)
        task_results[task["id"]] = {**payload, "_task_type": task["type"]}
        all_references.extend(section["references"])
    # 中间过程（docs/…长问法排查）：把每一步的问法/引擎/行数 + 链路回填的键值附在回答末尾。
    # 排查"哪一步断了、参数有没有回填对"时先看这里，不用另写脚本读日志（开关 SHOW_PROCESS_STEPS）。
    process_note = ""
    if bool(getattr(settings, "show_process_steps", True)) and sections:
        lines_out: list[str] = []
        for item in sections:
            rows_count = len(item.get("rows") or [])
            images_count = len(item.get("images") or [])
            extra = f"，{images_count} 张图" if images_count else ""
            if item.get("wants_images"):
                extra += "（这步要图）"
            lines_out.append(
                f"- 第{item.get('id')}步[{item.get('type')}]"
                f"（{item.get('engine') or '未执行'}，{rows_count} 行{extra}）："
                f"{str(item.get('question'))[:70]}"
            )
        if produced:
            lines_out.append(
                "- 链路回填：" + "；".join(f"{key}={value[:40]}" for key, value in produced.items())
            )
        process_note = "\n\n<details><summary>中间过程</summary>\n\n" + "\n".join(lines_out) + "\n\n</details>"

    seen = set()
    references = []
    for ref in all_references:
        key = ref.get("document_keyword") or ref.get("document_id") or ref.get("id")
        if not key or key in seen:
            continue
        seen.add(key)
        references.append(ref)

    lines = [f"已拆解为 {len(sections)} 个子问题："]
    for index, section in enumerate(sections, 1):
        label = str(section.get("label") or section["title"])
        lines.append(f"{index}. {label}：{section['reply']}")
    # 有综合分析子任务时，答案就是综合分析的结论（而不是把子结果拼给用户看）。
    # 子结果仍在 sections 里，结果工作台能看到（docs/长对话智能化升级完整方案 §二/§三）。
    conclusion = next(
        (str(section.get("reply") or "") for section in reversed(sections) if section.get("type") == "analysis"),
        "",
    )
    # 分解器没给出"综合分析"子任务，但用户这轮要的是判断/评价/建议时，
    # 不能把子结果拼成清单丢回去——把各段数据交给综合分析补一个结论
    # （实测"结合前面的数据给个整体评价"被拆成两个数据子问题，回复只列数字）。
    if not conclusion and intent_service.classify_intent(message) == "analysis":
        synthesized = _analysis_payload(
            {"question": message, "depends_on": [int(section["id"]) for section in sections]},
            task_results,
            session_id,
        )
        conclusion = str(synthesized.get("reply") or "").strip()
    last_context = next((s.get("context") for s in reversed(sections) if s.get("context")), None)
    # 各子结果也写进对话池：下一句"哪条路更多？多多少？"就能直接用它们算（长对话报告场景8）
    pooled: list[dict[str, Any]] = []
    for section in sections:
        pooled.extend(
            memory_service.pool_entries_from_rows(
                str(section.get("question") or ""),
                section.get("rows") or [],
                str(((section.get("context") or {}).get("damage_type")) or ""),
            )
        )
    if pooled:
        last_context = {**(last_context or {}), "pool_entries": pooled}
    # 链式依赖产出的关键值（peak_date=2026年7月29日、patrol_number=XCBH…）也存进会话：
    # 下一轮用户说"那天/那次"时，补全器能直接拿到真值，而不是只有一句自然语言回复。
    if produced:
        last_context = {**(last_context or {}), "chain_values": dict(produced)}
    reply = conclusion or "\n".join(lines)
    if process_note:
        reply = f"{reply}{process_note}"
    return {
        "reply": reply,
        "source": "database+ragflow" if any(s["type"] == "knowledge" for s in sections) else "database",
        "sections": sections,
        "references": references,
        "result_id": str(uuid.uuid4()),
        "answer_format": "mixed",
        "lineage": {"sections": [s.get("lineage") for s in sections]},
        "context": last_context,
        # 前端也能直接渲染（不想读回复里的折叠块时可以拿这个字段）：
        "process": {
            "steps": [
                {
                    "id": item.get("id"),
                    "type": item.get("type"),
                    "label": str(item.get("label") or ""),
                    "engine": item.get("engine"),
                    "rows": len(item.get("rows") or []),
                    "images": len(item.get("images") or []),
                    "wants_images": bool(item.get("wants_images")),
                    "question": str(item.get("question") or ""),
                }
                for item in sections
            ],
            "chain_values": dict(produced),
        },
    }


# 图片列的**展示优先级**（不是白名单）：哪张图先出现。认哪些列算图片列由字段目录的
# role=image 决定（26 个：病害图/路段封面/资产样图/作业图片…），这里只是把最常用的排前面。
_IMAGE_COLUMN_ORDER = ("problem_url", "preview_url", "distance_capture_url", "picture")
# 认不出列名时的兜底判据：值本身就是个图片路径
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff")


def _image_column_names() -> list[str]:
    """这个库里所有"存图片路径"的列名（来自目录 role=image，不写死名单）。

    单点维护：以后新表加了图片列、或某个列注释补上"图片"，这里自动就能认出来。
    """
    from . import schema_catalog as catalog

    names: set[str] = set()
    for kind in ("mysql", "postgresql"):
        try:
            names |= catalog.image_column_names(kind)
        except Exception:  # noqa: BLE001
            continue
    return [name for name in _IMAGE_COLUMN_ORDER if name in names] + sorted(
        names - set(_IMAGE_COLUMN_ORDER)
    )


def _image_fields_of(
    row: dict[str, Any], columns: list[str] | None = None
) -> list[tuple[str, str]]:
    """这一行里的图片 → [(列名, 路径)]（列名认不出来时给空串）。

    先认**英文列名**（模板/通用算子产出的列名就是它们，名单来自字段目录），认不到再按
    **值本身像不像图片路径**兜底：语义层写 SQL 时爱把列起中文别名
    （`p.problem_url AS 病害图片`），只认英文列名会整批漏掉——实测西乡段 911 条
    有图片的病害，回答却是"没有找到相关病害图片"
    （docs/图片查询ChooseCapability误报暂不可用修复.md §四.2）。

    带上**列名**是为了多列图片能分别标注：一张表可能有 picture / picture_cover /
    installation_picture 好几个图片列，前端要按列分组显示，不能混成一堆不知道出处。
    """
    columns = columns if columns is not None else _image_column_names()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for column in columns:
        value = str(row.get(column) or "").strip()
        if value and value.lower() not in {"null", "none", "0"} and value not in seen:
            seen.add(value)
            found.append((column, value))
    if found:
        return found
    for name, raw in row.items():
        text = str(raw or "").strip()
        if text and text.lower().endswith(_IMAGE_SUFFIXES) and text not in seen:
            seen.add(text)
            # 中文别名（`p.problem_url AS 病害图片`）在这里就是列名，正好用来标注
            found.append((str(name or ""), text))
    return found


def _image_values_of(row: dict[str, Any], columns: list[str] | None = None) -> list[str]:
    """这一行里的图片路径（按优先级，最多几个不同路径）。"""
    return [value for _, value in _image_fields_of(row, columns)]


def _column_label(column: str, tables: list[str] | None = None) -> str:
    """列的中文名（图片按列标注、图片出处都用它；查不到就退回列名）。"""
    from . import schema_catalog as catalog

    name = str(column or "").strip()
    if not name:
        return ""
    for table in tables or []:
        found = catalog.resolve_column("mysql", table, name)
        if found:
            return str(found.get("label") or name)
    for table in catalog.field_catalog("mysql").get("tables") or []:
        for col in table.get("columns") or []:
            if str(col.get("name")) == name:
                return str(col.get("label") or name)
    return name


def _image_origin(
    row: dict[str, Any], image_columns: list[str], tables: list[str] | None = None
) -> str:
    """这张图出自哪条记录（回复里"图片出处"那一行）。

    以前只认 route_name/pile_number/collection_time/problem_number，于是路段表、资产表里的
    图片一律显示成「（未记录路线）（无桩号）」——行里明明有路段全称/路段代码。
    现在：先用这些定位字段，一个都没有时退回"这一行里前几个非图片字段"（标签取自字段目录）。
    """
    known = [
        str(row.get("route_name") or row.get("short_name") or row.get("route") or ""),
        str(row.get("pile_number") or ""),
        str(row.get("collection_time") or ""),
        str(row.get("problem_number") or ""),
    ]
    parts = [item.strip() for item in known if item and item.strip().lower() != "null"]
    if parts:
        return " ".join(parts)
    fallback: list[str] = []
    for name, raw in row.items():
        text = str(raw or "").strip()
        if not text or text.lower() in {"null", "none"}:
            continue
        if name in image_columns or text.lower().endswith(_IMAGE_SUFFIXES):
            continue
        # 裸 0/1 多半是布尔标记或未翻译的编码（"是否为城市道路路段：1"），读者看不出含义 —— 跳过
        if text in {"0", "1"}:
            continue
        label = _column_label(str(name), tables)
        fallback.append(f"{label}：{text}" if label else text)
        if len(fallback) >= 3:
            break
    return " ".join(fallback)


def _missing_fields_payload(
    message: str, plan: dict[str, Any], kind: str = "mysql"
) -> dict[str, Any] | None:
    """用户点名的字段这张表（或整个库）根本没有时，如实说明。

    为什么单独做一条：以前这种情况会**换一张表照样出结果**（"资产的经纬度"落到资产字典表、
    "桥梁的经纬度"落到别的表），或者干脆按"没查到数据"回答——两者都是在骗用户。
    用户明确要求：达不到的要求要说出来。区分两种：
      · 全库都没有这个字段（"桥在哪个省"）→ 库里没有这项记录，不是没查到；
      · 主体表没有、别的表有（"桥梁的经纬度"）→ 说清这张表有什么、这个字段在哪张表里有。
    """
    from . import schema_catalog as catalog

    if plan.get("route_scope_from_patrol"):
        # route_name 在 patrol 主表上不存在，但本轮明确要从 patrol 经 disease
        # 关联 route 取名称；这不是“用户点了一个不存在的字段”。
        return None
    grounded = plan.get("grounded") or {}
    missing = [str(item) for item in grounded.get("missing_fields") or [] if item]
    # 层4（docs/派生指标与跨表字段语义可达方案.md）：本地字面匹配没命中的字段先挂在 pending 上
    # ——可能是叫法不同，也可能是 `section_length_km` 这种派生列。只有在**模型也确认"列清单里
    # 没有这个名字"**（unknown_output_fields）时才如实回答"没有这列"。pending 的先摘掉，
    # 让这道题走正常查询（模板/Wren），别在字段触达这一步就判死。
    pending = {str(item) for item in grounded.get("pending_fields") or []}
    if pending:
        missing = [item for item in missing if item not in pending]
    if not missing:
        return None
    # 目录读库失败时（field_catalog 缓存成 0 张表）不能下"库里没有这个字段"的结论：
    # 这时字段白名单是空的，lat/lng 都会被误判成不存在（实测踩到）。宁可不说。
    catalog_tables = catalog.field_catalog(kind).get("tables") or []
    if not catalog_tables:
        log_error(
            "agent",
            "missing_fields_skipped_catalog_empty",
            RuntimeError("field_catalog 返回 0 张表"),
            question=str(message)[:60],
        )
        return None
    # 只对"给我看看某一列/某某字段是多少"这类题出手（list/detail）：统计/分布题的
    # output_fields 常是聚合别名（ratio、density…），拿它们判"库里没这个字段"容易误伤。
    if str(plan.get("shape") or "") not in {"list", "detail"}:
        return None
    table = str(grounded.get("table") or "")
    label = catalog.table_label(kind, table) if table else "库里"
    field_tables = grounded.get("field_tables") or {}

    def _label_of(field: str) -> str:
        """字段的中文名：能从有这张表的目录里查到就用中文，查不到退回字段名。

        同一个字段在几张表上的叫法不一样（lat → 资产纬度/病害纬度/纬度），取**最短的那个**
        最通用——"桥梁的经纬度"说成「资产纬度」会让人误会是资产表的字段。
        """
        labels: list[str] = []
        for owner in field_tables.get(field) or []:
            column = catalog.resolve_column(kind, str(owner), field)
            if column:
                labels.append(str(column.get("label") or field))
        return min(labels, key=len) if labels else field

    wanted_text = "、".join(f"「{_label_of(item)}」" for item in missing)
    unavailable = {str(item) for item in grounded.get("unavailable_fields") or []}
    # 用户没点名这些字段、而且一口气缺了一堆 → 多半是"把这表数据调出来"时语义层自己凑的投影字段
    # （实测"把基本设施_路线表数据调出来"凑了 6 个，其中"桥梁名称"根本不属于路线表）。
    # 这种不能按"用户要的字段库里没有"如实回答，否则等于把一个正常问题挡掉。
    def _squash(text: str) -> str:
        return re.sub(r"[\s的了之,，。]+", "", str(text or ""))

    planted = [item for item in missing if _squash(_label_of(item)) in _squash(message)]
    if not (set(missing) & unavailable) and len(missing) > 2 and not planted:
        log_event(
            "agent",
            "INFO",
            "missing_fields_skipped_projection_guess",
            table=table,
            fields="、".join(missing)[:100],
            question=str(message)[:60],
        )
        return None
    if set(missing) <= unavailable:
        # 整个库都没有：先把"没有这项记录"说清楚，再给一个能往下走的出口
        names = "、".join(f"「{_label_of(item)}」" for item in sorted(unavailable & set(missing)))
        reply = (
            f"库里查不到{names}这个字段：现有 {len(settings.mysql_allowed_tables)} 张业务表都没有"
            "记录它，所以这个问题按现有数据答不了。\n"
            "这不是「没查到数据」，是这类信息本身没有入库——换个能查到的口径我再查。\n"
            "（如果我理解错了字段的中文叫法，告诉我列名，我再查一遍。）"
        )
    else:
        fields = [
            str(col.get("label") or col.get("name"))
            for col in (catalog.table_entry(kind, table) or {}).get("columns") or []
            if str(col.get("name")) not in {"del_flag", "tenant_id", "create_by", "update_by"}
            and "primary" not in (col.get("roles") or [])
        ][:10]
        elsewhere = {
            str(item)
            for names in field_tables.values()
            for item in names or []
        } - {table}
        reply = (
            f"{label}里没有{wanted_text}这{'些' if len(missing) > 1 else ''}列，"
            "所以这个问题按现有数据答不了。\n\n"
            f"{label}里能查的字段是：{'、'.join(fields)}。\n"
        )
        if elsewhere:
            hint = "、".join(
                catalog.table_label(kind, item) for item in sorted(elsewhere)[:3]
            )
            reply += f"（{wanted_text}这类字段在{hint}上有——如果你问的是那边的对象，我可以换过去查。）\n"
        reply += "换个字段或换个问法我再查。"
    log_event(
        "agent",
        "INFO",
        "missing_fields_answered",
        table=table,
        fields="、".join(missing)[:80],
        question=str(message)[:60],
    )
    return {
        "reply": reply,
        "source": "no_field",
        "sql": None,
        "columns": [],
        "rows": [],
        "chart_data": None,
        "result_id": None,
        "references": [],
        "question": message,
        "used_question": message,
    }


def _save_image_payload(message: str, payload: dict[str, Any]) -> dict[str, Any]:
    """图片回答也落一份到结果缓存，并补齐 result_id / question。

    为什么要存：`查看这次结果` 和 `固定看板刷新` 都是按 result_id 取缓存里的那条结果渲染的。
    以前图片只挂在**对话回复**上，缓存里是"没有图片那道查询"——工作台打开就没图
    （实测：病害图那条路径连 result_id 都没有，看板基本没入口）。
    """
    result_id = str(payload.get("result_id") or uuid.uuid4())
    payload["result_id"] = result_id
    payload.setdefault("question", message)
    payload.setdefault("used_question", message)
    save_result(result_id, payload)
    return payload


def _photo_result(message: str) -> dict[str, Any] | None:
    """看图题的**固定口径**取数：只查真有 `problem_url` 的病害行（不经过 LLM）。

    口径不适用（主体不是病害、计划都建不出来）时返回 None，交回原链路。
    """
    """看图题的**固定口径**取数：只查真有 `problem_url` 的病害行（不经过 LLM）。

    口径不适用（主体不是病害、计划都建不出来）时返回 None，交回原链路。
    """
    sql = nl2sql.build_photo_sql(message)
    if not sql:
        return None
    try:
        result = sql_engine.execute_sql("mysql", sql)
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "photo_fixed_sql_failed", exc, question=str(message)[:80])
        return None
    log_event(
        "agent",
        "INFO",
        "photo_fixed_sql",
        rows=len(result.get("rows") or []),
        question=str(message)[:60],
    )
    return result


# 「把能调出来的图片全部调出来」这类**全库清单**诉求。
# 语义层今天对这句话给的是 shape=photo + subject=disease（库里图片最多的表就是病害表，
# 那是默认值不是判断结果），所以这里用词兜底把口径放宽成"全库图片列"；**只在图片题内部生效**，
# 不改变任何其它问法，也不会把已经确定的病害口径收窄。
_IMAGE_CATALOG_WORDS = ("全部", "所有", "全库", "都调出来", "都能调出来", "能调出来的")
# 点名了具体对象就不放宽（"把西乡段的病害图片全部调出来"仍按病害口径走）。
_IMAGE_CATALOG_OBJECTS = (
    "病害", "工单", "资产", "路段", "路线", "桥梁", "隧道", "边坡",
    "样图", "封面", "巡查", "定距", "项目",
)


def _wants_image_catalog(message: str) -> bool:
    """这句要的是"全库能打开的图片"，而不是某一类对象的图？"""
    text = str(message or "")
    if not any(word in text for word in _IMAGE_CATALOG_WORDS):
        return False
    return not any(word in text for word in _IMAGE_CATALOG_OBJECTS)


def _table_label(table: str) -> str:
    """表的中文名（目录里的 label；查不到退回物理表名）。"""
    from . import schema_catalog as catalog

    entry = catalog.table_entry("mysql", table) or {}
    return str(entry.get("label") or entry.get("comment") or table)


def _image_catalog_result(message: str) -> dict[str, Any] | None:
    """**全库图片清单**取数：目录里所有 role=image 的列各取一次去重路径（不经过 LLM）。

    返回 None = 口径不适用 / 目录读不出来 / 一条路径都没有（交回原链路）。
    """
    sql = nl2sql.build_image_catalog_sql()
    if not sql:
        return None
    try:
        result = sql_engine.execute_sql("mysql", sql)
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "image_catalog_sql_failed", exc, question=str(message)[:80])
        return None
    grouped: dict[tuple[str, str], list[str]] = {}
    for row in result.get("rows") or []:
        path = str(row.get("path") or "").strip().lstrip("/")
        table = str(row.get("tbl") or "")
        column = str(row.get("col") or "")
        if not path or not table or not column:
            continue
        bucket = grouped.setdefault((table, column), [])
        if path not in bucket:
            bucket.append(path)
    if not grouped:
        return None
    limit = int(getattr(settings, "image_catalog_limit", 300) or 300)
    rows: list[dict[str, Any]] = []
    labels: list[str] = []
    for (table, column), paths in grouped.items():
        label = f"{_table_label(table)} · {_column_label(column, [table])}"
        labels.append(f"{label} {len(paths)} 张")
        for path in paths:
            # 清单题没有"哪条记录"可说出处，用「表 · 列」当出处最准确；
            # _image_origin 优先读 route_name，所以挂在这里。
            rows.append({column: path, "route_name": label})
    log_event("agent", "INFO", "image_catalog_sql", sources=len(grouped), paths=len(rows))
    return {
        "rows": rows[:limit],
        "tables": sorted({table for table, _column in grouped}),
        "columns": sorted({column for _table, column in grouped}),
        "note": "全库图片列清点：" + "；".join(labels) + "。",
        "image_limit": limit,
    }


def _multi_field_payload(
    message: str, plan: dict[str, Any], kind: str = "mysql", session_id: str | None = None
) -> dict[str, Any] | None:
    """跨表多字段题的执行（docs/跨表多字段并列查询_系统性整改方案.md §三 层 2）。

    · joined/lookup：字段所在表之间有确定性外键 → 一条 LEFT JOIN 查回所有字段；
    · parallel：表之间没关系 → 每张表出一段结果（复用复合题的分段结构，前端不用改）；
    · 字段解析不出来 → **只问真正缺的那几个**（不把整句打回）；
    · 判不出来 / 计划条件落不到本次查询的表上 → 返回 None 交回原链路（绝不静默丢条件）。
    """
    from . import multi_field

    resolved, missing = multi_field.resolve_fields(plan, kind)
    if not resolved:
        return None
    if missing:
        from . import schema_catalog as catalog

        # 只有"确实是跨表题、又有个别字段找不到"才反问；否则交回原链路——
        # 单表题点名的语义别名（route_name 这种库里没有的列名）在这里会误判成"字段找不到"，
        # 原链路本来能靠外键带出它，别把好问题挡掉。
        tables = {str(item["table"]) for item in resolved if item.get("table")}
        if len(tables) < 2:
            return None
        lost = "、".join(f"「{item.get('label') or item.get('field')}」" for item in missing)
        lines = [f"这几个字段我在库里没找到对应的列：{lost}。"]
        for item in missing:
            tables = [str(name) for name in (item.get("tables") or [])][:3]
            if tables:
                lines.append(
                    "「%s」在%s里都有，你要的是哪一张表？"
                    % (
                        item.get("label") or item.get("field"),
                        "、".join(catalog.table_label(kind, name) for name in tables),
                    )
                )
        kept = "、".join(f"「{item.get('label') or item.get('field')}」" for item in resolved)
        lines.append(f"其余字段（{kept}）我现在就能查，要的话我先把这部分给你。")
        log_event(
            "agent",
            "INFO",
            "multi_field_missing",
            fields=lost[:80],
            question=str(message)[:60],
        )
        return {
            "reply": "\n".join(lines),
            "source": "clarification",
            "sql": None,
            "columns": [],
            "rows": [],
            "result_id": None,
            "references": [],
            "question": message,
            "used_question": message,
        }

    planned = multi_field.plan_queries(plan, resolved, kind)
    if not planned:
        return None

    if planned["mode"] == "joined":
        sql = str(planned["sql"])
        try:
            result = sql_engine.execute_sql(kind, sql)
        except DatabaseError as exc:
            log_error("agent", "multi_field_joined_failed", exc, question=str(message)[:80])
            return None  # 执行不了就交回原链路，不硬报错
        result["sql"] = sql
        result["shape"] = "list"
        result["message"] = message
        result["tables"] = planned["tables"]
        result_id = str(uuid.uuid4())
        result["result_id"] = result_id
        reply = _summarize_result(message, result)
        payload = {
            "reply": reply,
            "source": "database",
            "sql": sql,
            "columns": result.get("columns", []),
            "rows": result.get("rows", []),
            "chart_data": result.get("chart_data"),
            "result_id": result_id,
            "shape": "list",
            "answer_format": "table",
            "engine": "multi_field_join",
            "tables": planned["tables"],
            "references": [],
            "question": message,
            "used_question": message,
            "lineage": _lineage(kind, result, {}, sql),
        }
        save_result(result_id, result)
        log_event(
            "agent",
            "INFO",
            "multi_field_answered",
            mode="joined",
            rows=len(result.get("rows") or []),
            question=str(message)[:60],
        )
        return payload

    # parallel：每张表一段，结构对齐复合题（前端/结果工作台按 sections 渲染）
    sections: list[dict[str, Any]] = []
    for index, query in enumerate(planned["queries"], 1):
        sql = str(query["sql"])
        title = f"{catalog_label(kind, str(query['table']))}（{'、'.join(str(name) for name in query['columns'])}）"
        section: dict[str, Any] = {
            "id": index,
            "type": "data",
            "title": title,
            "question": message,
            "source": "database",
            "sql": sql,
            "columns": [],
            "rows": [],
            "references": [],
            "engine": "multi_field_parallel",
        }
        try:
            part = sql_engine.execute_sql(kind, sql)
        except DatabaseError as exc:
            log_error("agent", "multi_field_part_failed", exc, question=str(message)[:80])
            section["reply"] = f"这一段没查成：{exc}"
            section["source"] = "error"
            sections.append(section)
            continue
        part["sql"] = sql
        part["shape"] = "list"
        part["message"] = message
        result_id = str(uuid.uuid4())
        part["result_id"] = result_id
        section.update(
            {
                "reply": _summarize_result(title, part),
                "columns": part.get("columns", []),
                "rows": part.get("rows", []),
                "chart_data": part.get("chart_data"),
                "result_id": result_id,
                "shape": "list",
                "answer_format": "table",
                "lineage": _lineage(kind, part, {}, sql),
            }
        )
        save_result(result_id, part)
        sections.append(section)

    lines = [f"这条问题要的是 {len(sections)} 张表各自的字段，我分开查了："]
    # 并列（没找到确定外键关系）时如实说明"不是逐条对应"，别让用户以为这是关联后的结果
    if not multi_field._join_condition(kind, planned["tables"][0], planned["tables"][1]):
        lines.append(
            "（这几张表之间没有找到确定的外键关系，所以是**分别列出**的，不是逐条对应；"
            "如果你要的是「一条对应一条」，告诉我该按哪一列关联，我改成 JOIN 查。）"
        )
    for index, section in enumerate(sections, 1):
        lines.append(f"{index}. {section['title']}：{section['reply']}")
    log_event(
        "agent",
        "INFO",
        "multi_field_answered",
        mode="parallel",
        sections=len(sections),
        question=str(message)[:60],
    )
    return {
        "reply": "\n".join(lines),
        "source": "database",
        "sections": sections,
        "references": [],
        "result_id": str(uuid.uuid4()),
        "answer_format": "mixed",
        "engine": "multi_field_parallel",
        "lineage": {"sections": [section.get("lineage") for section in sections]},
        "question": message,
        "used_question": message,
    }


def catalog_label(kind: str, table: str) -> str:
    """表的中文名（分段结果的小标题用）。"""
    from . import schema_catalog as schema

    return schema.table_label(kind, table)


def _image_result(message: str, force_ai: bool = False) -> dict[str, Any]:
    """看图题取数。

    force_ai：用户点了确认卡上的「让 AI 自己查」。**必须往上传**：以前这条参数没传进来，
    于是"让 AI 自己查"在图片题上被忽略 → 又去收集确定性候选 → 又摆一张一模一样的卡
    （实测"看一下病害样图"点一次弹一次）。
    """
    try:
        # 取图片路径的元数据只在 MySQL 里（ai_problem.problem_url …），
        # 不能因为问题里出现「图片」就把库判成 seaweedfs。
        # 先走**固定口径**（只查真有 problem_url 的病害行，不经过 LLM）：语义层写 SQL 时会自己
        # 挑图片列，挑成过 preview_url（那列大多是 'NULL'）或给列起中文别名，于是"库里有 911 张图
        # 却回没有找到"（docs/图片查询ChooseCapability误报暂不可用修复.md §五.4）。口径不适用
        # （问的是资产/定距图片等）时返回 None，交回原链路。
        # want_images=True："看图"这件事在这里已经是**语义结论**（意图分类 image 才会走到这），
        # 往下传是为了让候选 SQL 只取真有图的行、并且按图渲染，不用再靠关键词判一次。
        result = _photo_result(message) or _build_query_result(
            message,
            "data",
            kind="mysql",
            want_images=True,
            # "让 AI 自己查"：跳过确定性候选卡，直接走 Wren/大模型写 SQL
            skip_capabilities=force_ai,
            wren_first=force_ai,
            use_saved=False if force_ai else None,
        )
        return _save_image_payload(message, _images_payload(message, result))
    except capabilities.ChooseCapability as need:
        # "请先确认查询口径"是**正常信号**（能力确认卡：这题还差条件，弹卡让用户选），
        # 不是"图片查询坏了"。以前被下面的通用兜底接住，回一句"图片查询暂不可用：
        # 请先确认查询口径"，把可用功能说成不可用
        # （docs/图片查询ChooseCapability误报暂不可用修复.md）。
        payload = _choose_capability_payload(need, want_images=True)
        payload["images"] = []
        payload["image_items"] = []
        return payload
    except Exception as exc:
        # source=error：这条是**失败回执**，不能被反思自检/后续建议拿去改写成一段"看起来
        # 像答案"的话（实测模型把 404 讲成"系统配置异常"，还编了一张假的病害代码对照表）。
        return {"reply": f"图片查询暂不可用：{exc}", "source": "error", "images": []}


def _images_payload(message: str, result: dict[str, Any]) -> dict[str, Any]:
    """一次查询结果 → 图片回答（images / image_items / 出处）。

    `_image_result`（看图题主链路）和"用户选定了口径、结果里有图片列"的走法共用这一份，
    免得两处各写一套渲染逻辑。图片列由**目录的 role=image** 决定，不写死名单。
    """
    # 展示多少张：不再看关键词（用户定的规矩：不靠穷举关键词触发）。这条查询取回多少条就展示
    # 多少张，上限走 .env 的 IMAGE_DISPLAY_LIMIT。以前是"随口看看只给 6 张、说了'所有'才多给"，
    # 实测"看一下病害样图"表里 84 条却只出 5 张——用户以为图丢了。
    display_limit = int(getattr(settings, "image_display_limit", 120) or 120)
    # 全库图片清单题（result 带 image_limit）要先把目录里的路径**全部**收齐再判存在，
    # 否则清单会被展示上限截断，用户看到的还是"只有几张"。
    requested_limit = int(result.get("image_limit") or 0)
    if requested_limit > display_limit:
        display_limit = requested_limit
    check_limit = display_limit
    candidate_limit = display_limit
    # 图片要和它的"出处"一起带出来（路段/桩号/时间）——用户接着问
    # "这几张是在哪些路段或桩号拍的"就不用再猜，也不用重新查一次。
    rows_all = list(result.get("rows") or [])
    image_columns = _image_column_names()
    result_tables = [str(item) for item in (result.get("tables") or []) if item]
    # 一句话问了多种病害（"裂缝、块状修补、泛油的图"）时，按行顺序取前 N 张会被一种类型占满：
    # 行是按采集时间排的，前 120 行很可能全是同一类 → 用户看到"每次都那几张"。
    # 这里按**类型轮转**取行，保证每种问到的病害都能出图（types 里的顺序 = 结果里的出现顺序）。
    type_key = next(
        (
            key
            for key in ("病害类型", "pic_type", "problem_type", "problem_type_name", "damage_type")
            if rows_all and key in rows_all[0]
        ),
        "",
    )
    if type_key and len({str(row.get(type_key) or "") for row in rows_all}) > 1:
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows_all:
            buckets.setdefault(str(row.get(type_key) or ""), []).append(row)
        queues = [list(items) for items in buckets.values()]
        interleaved: list[dict[str, Any]] = []
        while any(queues) and len(interleaved) < len(rows_all):
            for queue in queues:
                if queue:
                    interleaved.append(queue.pop(0))
        rows_all = interleaved
    candidates: list[str] = []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    shared: dict[str, int] = {}
    for row in rows_all:
        for column, value in _image_fields_of(row, image_columns):
            path = value.lstrip("/")
            shared[path] = shared.get(path, 0) + 1
            if path in seen:
                continue
            seen.add(path)
            candidates.append(path)
            items.append(
                {
                    "path": path,
                    # 多列图片要能分别标注：这张图来自哪一列（picture / picture_cover…）
                    "column": column,
                    "column_label": _column_label(column, result_tables),
                    # 出处：桩号/时间那套字段没有时，用行里的标识字段（路段全称/编码…）拼
                    "origin": _image_origin(row, image_columns, result_tables),
                    # 只认真正的路线列：以前回落到 type，会把病害类型（"泛油"）当成路段名显示
                    "route_name": str(
                        row.get("route_name") or row.get("short_name") or row.get("route") or ""
                    ),
                    "pile_number": str(row.get("pile_number") or ""),
                    "collection_time": str(row.get("collection_time") or ""),
                    "problem_number": str(row.get("problem_number") or ""),
                    # 同一张图被多少条记录共用（采集端路径重复时很常见）：
                    # 卡片/回复里标出来，避免"同一张图反复出现"被当成系统出错
                    "shared_records": shared.get(path, 1),
                }
            )
            # **不 break**：一行里可能有好几个图片列（路段图片 + 路段封面 + 现场安装图片…），
            # 每一列都要收上来（同一行同一列只取第一个值，路径去重在上面 done）。
            # 以前这里 break，多列图片只出第一列，前端也就没法按列标注。
        if len(candidates) >= candidate_limit:
            break
    # 共用条数要等全部行扫完才准（上面是边扫边填的）
    for item in items:
        item["shared_records"] = shared.get(str(item.get("path")), 1)

    # 确认对象是否真的存在，避免前端渲染一堆坏图——**一条远程命令批量查**：
    # 单条探测是串行加锁的（一条命令一次 SSH 往返），几十张图就是几十次往返、几十秒，
    # 这也是以前把检查数量卡在 6 张的原因。
    images: list[str] = []
    missing: list[str] = []
    checked = db_manager.check_seaweedfs_many(candidates[:check_limit])
    for path in candidates[:check_limit]:
        status, _ = checked.get(path, (0, ""))
        if status == 200:
            images.append(path)
        else:
            missing.append(path)

    # 回复里把"每张图出自哪条记录"一起说清楚（用户下一句就是问这个），
    # 并如实说明"取到多少条记录、去重后多少个路径、真正存在多少张"。
    located = [item for item in items if item["path"] in set(images)]
    detail_lines = [
        f"{index}. {item.get('origin') or '（这条记录没有可用的标识字段）'}"
        + (
            f"（该图片被 {item['shared_records']} 条记录共用）"
            if int(item.get("shared_records") or 1) > 1
            else ""
        )
        for index, item in enumerate(located[:10], 1)
    ]
    truncated = len(candidates) >= candidate_limit and len(rows_all) > len(candidates)
    # 清单题自带的说明（"全库图片列清点：病害记录 · 病害图片 65 张；…"）：只按列清点，
    # 不涉及"同一张图被多少条记录共用"，所以放在最前面说清口径。
    note_line = (str(result.get("note") or "").strip() + "\n") if result.get("note") else ""
    if note_line and located:
        # 清点是"库里有多少路径"，用户真正想看的是"能打开的是哪些列、各几张"——补一行，
        # 否则"清点 323 个路径 / 实际只有 89 张"之间的差额只能靠猜。
        shown: dict[str, int] = {}
        for item in located:
            key = str(item.get("route_name") or item.get("column_label") or "未标注")
            shown[key] = shown.get(key, 0) + 1
        note_line += (
            "能打开的按列统计："
            + "；".join(f"{key} {count} 张" for key, count in shown.items())
            + "。\n"
        )
    # 图片路径在库里**高度重复**时要说清楚：实测 1702 条病害只有 65 个不同路径，
    # 单条路径被 130+ 条记录共用 → 不管问哪种病害类型，能取到的都是同一批文件，
    # 用户会以为"图错了/系统坏了"。这不是查询问题，是采集端入库时路径写入的问题。
    dup_note = ""
    if len(rows_all) > len(candidates):
        # 全库口径（一次轻量统计，只在确实有重复时才查）：让用户看到"不是这一次的问题"。
        # 只对病害表说"全库 1702 条病害"——这句话套到路段/资产上就答非所问（实测"路段封面的图片"）。
        global_note = ""
        if "ai_problem" in result_tables:
            try:
                stat = db_manager.query(
                    "mysql",
                    "SELECT COUNT(*) AS total, COUNT(DISTINCT problem_url) AS distinct_url "
                    "FROM ai_problem WHERE del_flag = 0 AND problem_url IS NOT NULL "
                    "AND problem_url <> '' AND problem_url <> 'NULL'",
                )["rows"]
                if stat:
                    total = int(float(stat[0].get("total") or 0))
                    distinct_url = int(float(stat[0].get("distinct_url") or 0))
                    if total and distinct_url:
                        global_note = (
                            f"（全库 {total} 条病害记录一共只有 {distinct_url} 个不同图片路径）"
                        )
            except Exception as exc:  # noqa: BLE001
                log_error("agent", "image_dup_stat_failed", exc)
        dup_note = (
            f"⚠ 命中 {len(rows_all)} 条记录，但它们的图片路径只有 {len(candidates)} 个不同文件——"
            "多条记录指向同一张图（采集端入库时的路径写入问题），所以同一批图会反复出现"
            f"{global_note}。\n"
        )
    head = (
        note_line
        + dup_note
        +
        f"找到 {len(images)} 张可展示的图片"
        f"（本次取到 {len(rows_all)} 条记录，去重后 {len(candidates)} 个图片路径"
        f"{'，已达到取数上限，实际可能更多' if len(rows_all) >= settings.sql_max_rows else ''}"
        f"{f'，本次只展示前 {candidate_limit} 张' if truncated else ''}）。"
    )
    if detail_lines:
        head += "\n图片出处：\n" + "\n".join(detail_lines)
    if images and not missing:
        reply = head
    elif images and missing:
        reply = (
            note_line
            + dup_note
            + f"找到 {len(images)} 张可展示的图片；另有 {len(missing)} 个图片路径在存储里"
            f"找不到文件（示例：`{missing[0]}`）。"
        )
        # 位置信息一并给出：用户下一句通常就是问"这几张拍在哪"
        if detail_lines:
            reply += "\n可展示图片的出处：\n" + "\n".join(detail_lines)
    elif missing:
        reply = (
            note_line
            + dup_note
            + f"查到 {len(missing)} 条图片记录，但对应文件在 SeaweedFS 上不存在（返回 404），"
            "没有可展示的图片。\n\n"
            f"示例路径：`{missing[0]}`\n\n"
            "这批数据可能是历史记录，或入库时路径写入有误，需要在存储侧核对。"
        )
    else:
        reply = "没有找到相关图片。"

    return {
        "reply": reply,
        "source": "database",
        "images": images,
        # 每张图的出处（路段/桩号/时间）：下一句"这几张是在哪拍的"直接用，不用再查
        "image_items": [item for item in items if item["path"] in set(images)],
        "missing_images": missing,
        "columns": result.get("columns", []),
        "rows": result.get("rows", []),
        "result_id": result.get("result_id"),
        "sql": result.get("sql"),
        "references": result.get("references", []),
    }


def _card_want_images(message: str, plan: dict[str, Any] | None) -> bool:
    """摆卡时把"这次是要看图"记进 token（点确认那次请求不再判意图）。

    以前主链路摆卡一律不带这个 flag，点确认后就走普通出数：结果是**一张纯表格、
    图片一张都不渲染**（实测"看一下病害样图"选"相似已验证问法"口径）。
    判据只使用语义层给出的 shape。
    """
    return str((plan or {}).get("shape") or "") == "photo"


def _choose_capability_payload(
    need: "capabilities.ChooseCapability", want_images: bool = False
) -> dict[str, Any]:
    """能力确认卡 payload：对话主链路和图片查询**共用这一份**，免得两处各写一套、口径跑偏。

    `source` 保持 "confirm"（前端按 `await_confirm` + `capability_choices` 渲染确认卡）。
    want_images：这句是要看图的**语义结论**，跟着 token 走到点确认那一次（那一次不再问意图）。
    """
    # 「按步骤答」整条链路已删除（用户要求：要么一次答出来，要么交给 Wren）——
    # 这里不再产生"每一步选一个口径"的步骤卡；need.steps 若还带着内容也一律忽略。
    token = capabilities.stash(
        need.plan, need.candidates, tasks=need.tasks, want_images=want_images
    )
    recommended = next(
        (item["id"] for item in need.candidates if item.get("recommended")),
        need.candidates[0]["id"] if need.candidates else "",
    )
    # 复合题的整题卡：如实告诉用户"这条问题其实分几步、下面口径覆盖多少"。
    # 只有这一种卡：一次性用现成口径答，或者（选不中时）让 AI 走 Wren。
    multi_part = bool(need.tasks) and len(need.tasks) > 1
    if multi_part:
        lead = (
            f"这条问题可以拆成 {len(need.tasks)} 步；下面这些口径里能一次答对的会直接出整条结果，"
            "覆盖不全的我也会如实说明；都不合适就让 AI 用 Wren 查。"
        )
    else:
        lead = "这几个查询口径都能回答你的问题，选一个我就直接查（不重新生成 SQL）。"
    reply = lead
    if need.note:
        reply = f"{need.note}\n{reply}"
    return {
        "reply": reply,
        "source": "confirm",
        "await_confirm": True,
        "choose_token": token,
        "capability_choices": need.candidates,
        "recommended_id": recommended,
        "step_count": len(need.tasks) if multi_part else 0,
        "sql": None,
        "columns": [],
        "rows": [],
        "scope_card": nl2sql.scope_card(need.plan, []) if need.plan else "",
    }


# 「它是什么类型/在哪条路」这类只问上一条结果某个字段的追问，现在统一走正常链路
# （语义层判 subject + 字段登记表生成 SQL）。
# 以前这里有一张写死的主体→SQL 表（_SUBJECT_FIELD_SQL / _SUBJECT_FIELD_NAME），
# 没有任何调用方，属于关键词/硬编码穷举的历史残留，已删除。


def _persist_conversation_turn(
    message: str,
    session_id: str | None,
    payload: dict[str, Any],
    *,
    append_user: bool = True,
) -> None:
    """把一次用户提问和助手回复落进历史会话。

    统一放在最外层保存，确认卡、澄清、元数据查询等提前返回分支也不会漏记。
    确认同一题的后续请求只追加助手回复，避免历史里出现重复问题。
    """
    if not session_id or _is_batch_run():
        return
    ensure_conversation(session_id, message.strip()[:40])
    if append_user:
        add_conversation_message(session_id, "user", message)
    add_conversation_message(
        session_id,
        "assistant",
        str(payload.get("reply") or ""),
        str(payload.get("thinking") or ""),
        payload.get("references") or [],
        payload.get("result_id"),
    )


def handle_message(
    message: str,
    session_id: str | None = None,
    confirmed: bool = False,
    use_saved: bool | None = None,
    choose_token: str | None = None,
    capability_id: str | None = None,
    slot_values: dict[str, str] | None = None,
    force_ai: bool = False,
    on_event: Any = None,
) -> dict[str, Any]:
    """同步入口的流式事件包装；无 on_event 时与旧行为完全一致。"""
    with event_bus.capture(on_event):
        event_bus.emit_event("phase", title="开始理解问题")
        try:
            payload = _handle_message_entry(
                message,
                session_id,
                confirmed=confirmed,
                use_saved=use_saved,
                choose_token=choose_token,
                capability_id=capability_id,
                slot_values=slot_values,
                force_ai=force_ai,
            )
        except Exception as exc:
            event_bus.emit_event("error", message=f"{type(exc).__name__}: {exc}"[:300])
            event_bus.emit_event("done")
            raise
        _persist_conversation_turn(
            message,
            session_id,
            payload,
            append_user=not (
                confirmed
                or bool(choose_token)
                or bool(capability_id)
                or use_saved is not None
            ),
        )
        result_id = str(payload.get("result_id") or "")
        if result_id:
            try:
                save_result(result_id, payload)
            except Exception as exc:  # noqa: BLE001
                log_error("agent", "final_result_save_failed", exc, result_id=result_id)
        compact_payload = {
            key: value
            for key, value in payload.items()
            if key not in {"rows", "sections", "references"}
        }
        event_bus.emit_event(
            "final",
            result_id=result_id or None,
            source=payload.get("source"),
            payload=compact_payload,
        )
        event_bus.emit_event("done")
        return payload


def _handle_message_entry(
    message: str,
    session_id: str | None = None,
    confirmed: bool = False,
    use_saved: bool | None = None,
    choose_token: str | None = None,
    capability_id: str | None = None,
    slot_values: dict[str, str] | None = None,
    force_ai: bool = False,
) -> dict[str, Any]:
    """对外入口：先跑主流程；结果被校验判为"答非所问"时，自动优化一次问题重查。

    见 docs/结果校验与问题优化方案.md：
      生成 SQL → 执行 → 校验 → 对就返回；错就优化问题 → 重查一次 → 还错就如实说明。
    模板路径（写死的 SQL）不校验，省一次 LLM 往返。
    """
    single_turn = _single_turn_mode()
    if single_turn:
        # 确认卡等提前返回分支也必须先从干净状态开始，不能等进 _handle_message。
        _clear_turn_context(session_id)
    # 执行前复述确认（方案第二章）：复杂问题先问一句「我将查询：…」，用户点确认再执行。
    # 开关默认关（config.confirm_before_query / 前端开关），简单问题永不确认。
    if not confirmed and chat_settings.load().get("confirm_before_query", False):
        try:
            probe = nl2sql.build_query_plan(message, "mysql")
        except Exception:  # noqa: BLE001
            probe = None
        if probe and nl2sql.needs_confirm(probe):
            return {
                "reply": nl2sql.restate(probe) + "\n确认口径无误就点「确认查询」，不对就直接改成大白话再问我。",
                "source": "confirm",
                "await_confirm": True,
                "sql": None,
                "columns": [],
                "rows": [],
                "scope_card": nl2sql.scope_card(probe, []),
            }
    try:
        event_bus.emit_event("phase", title="分析意图与查询口径")
        payload = _handle_message(
            message,
            session_id,
            use_saved=use_saved,
            choose_token=choose_token,
            capability_id=capability_id,
            slot_values=slot_values,
            force_ai=force_ai,
        )
    except clarify.ClarifyNeeded as need:
        # 选择式澄清（reuse_mode=ask）在链路最前面抛出，这里统一转成可渲染的追问 payload
        payload = _clarify_payload(message, need, session_id)
        if single_turn:
            _clear_turn_context(session_id)
    except capabilities.ChooseCapability as need:
        # 能力确认卡：候选连同口径一起给用户选，不自动执行（方案 §四.5）
        # 组装逻辑只有一份（见 _choose_capability_payload）：图片查询那条路也要用它
        # 「这次要看图」必须随卡带走：点确认那一次不会再判意图，flag 丢了就只回表格不出图。
        payload = _choose_capability_payload(
            need, want_images=_card_want_images(message, need.plan)
        )
        if single_turn:
            _clear_turn_context(session_id)
    if not payload.get("await_confirm"):
        payload = _repair_direct_payload(message, payload)
    # 单题也要能看到"回答线路"（前端那条执行链路面板）：复合题在 _handle_composite 里已经带了
    # process（每步的问法/类型/引擎/行数/图片 + 链路回填键值），这里给单查询补一份单步的，
    # 用户就不用去翻代码看"这次到底走的模板还是 Wren、取了多少行"。
    if (
        not payload.get("process")
        and not payload.get("await_confirm")
        and (payload.get("result_id") or payload.get("sql"))
    ):
        payload["process"] = {
            "steps": [
                {
                    "id": 1,
                    "type": "data",
                    "engine": payload.get("engine"),
                    "rows": len(payload.get("rows") or []),
                    "images": len(payload.get("images") or []),
                    "question": str(payload.get("used_question") or message),
                }
            ],
            "chain_values": {},
        }
    # 确定性直出（用户确认过的口径 / 范围校准 / 内置模板结构匹配）直接返回：
    # 不再做"答非所问"校验、反思自检和后续建议（跟快捷查询 / 看板刷新同构）。
    # 光看 direct 标记不够——历史上好几个提前 return 的分支漏标过它，所以这里再用
    # engine 兜一层（docs/选模板直出提速与去除后续追问方案.md §三 P0-1）。
    if (
        (
            payload.get("direct")
            or str(payload.get("engine") or "") in _DETERMINISTIC_ENGINES
            or _sections_deterministic(payload)
        )
        # 运营概览即使走确定性模板，也必须让校验器核对任务/状态/里程/归属/病害来源。
        and not payload.get("patrol_overview")
    ) or payload.get("await_confirm"):
        return payload
    event_bus.emit_event("phase", title="生成后续建议")
    return _followup_step(message, payload)


# 确定性直出的引擎集合（一处定义、多处引用）：这些结果的 SQL 是拼好的/用户确认过的，
# 既不需要"答所问"校验，也不该再花一次大模型生成"要不要再帮你看看"。
# 命名的两套写法（template / template_match、chosen_x / x）都收进来——历史上正是因为
# 只写了 template_match、漏了 template，内置模板才会被多校验 + 多追问一次。
_DETERMINISTIC_ENGINES = {
    "template",
    "template_match",
    "preset",
    "auto",
    "generic",
    # 注：multi_field_join / multi_field_parallel 已从这份名单移除——那两个引擎靠列名匹配选表，
    # 选错也照样执行，且跳过用户确认，已停用（docs/清理不可靠引擎与通用算子修复方案.md §三）。
    # 宏观元数据查询（库/表/字段结构）：SQL 是按解析出的 JSON 确定性拼的，回复就是最终答案
    "metadata",
    "calibrated",
    "post_calc",
    # 未来/预测类问题的"数据边界"回答：是**如实说明**，别再让校验层改写成一段编的答案
    "time-boundary",
    "dialogue-state",
    "saved",
    "saved_example",
    "chosen_preset",
    "chosen_auto",
    "chosen_code_template",
    "chosen_generic",
    "chosen_example",
    "chosen_example_direct",
}


def _sections_deterministic(payload: dict[str, Any]) -> bool:
    """复合题：每个子步骤都走确定性口径时，整条回答同样不该再校验/追问。"""
    sections = payload.get("sections") or []
    if not sections:
        return False
    for section in sections:
        engine = str(section.get("engine") or "")
        if not engine or engine not in _DETERMINISTIC_ENGINES:
            return False
    return True


def _followup_step(message: str, payload: dict[str, Any]) -> dict[str, Any]:
    """后续建议：回答末尾主动提示用户接下来可能想知道什么（升级4）。"""
    if not chat_settings.load().get("followup_suggestions", True):
        return payload
    if (
        str(payload.get("source") or "") in {"clarification", "error"}
        or payload.get("partial")
        or payload.get("failures")
        or payload.get("sections")
    ):
        return payload
    # 确定性直出（模板/校准/用户确认过的口径）不生成"要不要再帮你看看"：
    # 用户要的就是那一句结果，追问既多余又要多花一次大模型往返。
    if payload.get("direct") or str(payload.get("engine") or "") in _DETERMINISTIC_ENGINES:
        return payload
    reply = str(payload.get("reply") or "")
    if reply.strip().endswith("？") or len(reply.strip()) < 10:
        return payload
    suggestion = _followup_suggestions_sync(message, reply)
    if suggestion:
        payload["reply"] = f"{reply.rstrip()}\n\n{suggestion}"
    return payload


def _followup_suggestions_sync(question: str, reply: str) -> str:
    try:
        text = chat(
            prompts.messages("followup_suggestion", question=question, reply=reply[:1500]),
            temperature=0.2,
            model=role_model("knowledge"),
            max_tokens=200,
        ).strip()
    except Exception as exc:  # noqa: BLE001
        log_error("agent", "followup_failed", exc, question=question[:120])
        return ""
    if not text or "？" not in text:
        return ""
    return text[:400]


# ── agent 侧词表外置（docs/代码结构重构与模板外置方案.md §5.3）───────────────
# `resources/lexicon/result_words.yaml` 里配了就覆盖，没配就保持上面代码里的默认值。
def _result_words(section: str, default: Any) -> Any:
    try:
        from .resource_loader import load_yaml

        data = load_yaml("lexicon/result_words.yaml", {}) or {}
    except Exception:  # noqa: BLE001
        return default
    value = data.get(section)
    if not value:
        return default
    if isinstance(default, set) and isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(default, tuple) and isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(default, dict) and isinstance(value, dict):
        # 表里允许"键 → 多档取值"（如 known_group_domains: direction: [上行, 下行]）：
        # 这种值必须保持成序列，压成 str(list) 再被遍历就会变成"逐字符一档"
        # （实测摘要里会出现「[ 0 处，' 0 处，…」这种乱码）。
        out: dict[str, Any] = {}
        for key, item in value.items():
            default_value = default.get(str(key))
            if isinstance(default_value, (tuple, list)) and isinstance(item, list):
                out[str(key)] = tuple(str(entry) for entry in item)
            else:
                out[str(key)] = str(item)
        return out
    return value


def _result_pairs(section: str, default: Any) -> Any:
    """[{words: [...], terms: [...]}] → ((words...), (terms...))，形状与代码一致。"""
    try:
        from .resource_loader import load_yaml

        rows = (load_yaml("lexicon/result_words.yaml", {}) or {}).get(section)
    except Exception:  # noqa: BLE001
        rows = None
    if not isinstance(rows, list) or not rows:
        return default
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        words, terms = item.get("words"), item.get("terms")
        if words and terms:
            out.append((tuple(str(word) for word in words), tuple(str(term) for term in terms)))
    return tuple(out) or default


_METRIC_TERM_MAP = _result_pairs("metric_term_map", _METRIC_TERM_MAP)
_RESULT_LABELS = _result_words("result_labels", _RESULT_LABELS)
_KNOWN_GROUP_DOMAINS = {
    str(key): tuple(str(item) for item in value)
    for key, value in (_result_words("known_group_domains", _KNOWN_GROUP_DOMAINS) or {}).items()
}
_DOMAIN_LOOSE_LABELS = _result_words("domain_loose_labels", _DOMAIN_LOOSE_LABELS)


def _single_turn_mode() -> bool:
    return not bool(chat_settings.load().get("inherit_context", True))


def _clear_turn_context(session_id: str | None) -> None:
    """清空所有会被后续问题复用的当轮状态。"""
    memory_service.clear_session(session_id)
    nl2sql.clear_entity_filter()
    nl2sql.clear_round_request()


def _handle_message(
    message: str,
    session_id: str | None = None,
    use_saved: bool | None = None,
    choose_token: str | None = None,
    capability_id: str | None = None,
    slot_values: dict[str, str] | None = None,
    force_ai: bool = False,
) -> dict[str, Any]:
    normalized = query_rewrite.normalize(message)
    single_turn = _single_turn_mode()
    if single_turn:
        # 单轮独立：进入本轮前先清掉上一轮所有可复用状态，绝不先读再覆盖。
        _clear_turn_context(session_id)
    # 「执行已确认的口径」是一条**纯执行**请求：SQL 已经在卡片上绑好、用户也点过确认，
    # 不需要再做上下文补全、意图分类、拆分规划这些为"理解新问题"准备的步骤
    # （实测这三步占确认后耗时的大头：补全/分类各一次大模型调用）。
    # 「让 AI 自己查」(ai/wren) 不在此列——它要继续走完整链路。
    execute_choice = bool(
        choose_token and capability_id and capability_id not in {"ai", "wren"}
    )
    # 「让 AI 自己查」的两种回执统一成一个判据：force_ai=true（新卡片）或 capability_id
    # 'ai'/'wren'（老卡片、步骤卡）。整条链路（复用闸门/图片/复合/普通数据）都用它，
    # 免得某一路径只认 force_ai，点了 AI 又弹一张卡
    # （docs/弹卡链路系统性Bug整改方案.md §二.1）。
    ai_requested = bool(force_ai or capability_id in {"ai", "wren"})
    # 元数据类问题（哪些字段没数据/空值率/完整度…）直接走画像，不进业务 plan——
    # 以前这类会被套成"缺路线"澄清（docs/通用模板扩充与全字段明细方案.md 第四节）。
    if not choose_token and capability_id is None:
        try:
            from . import generic as generic_service

            profiling = (
                generic_service.profiling_reply(message, cross_db.resolve_db_kind(message))
                if generic_service.is_profiling_question(message)
                else None
            )
        except Exception as exc:  # noqa: BLE001
            profiling = None
            log_error("agent", "profiling_route_failed", exc, question=message[:80])
        if profiling:
            return profiling
    # 复用策略（方案第七章 §5）：auto=命中直接用；ask=命中先让用户选；off=不复用。
    # ask 模式在这里就问，省掉后面全部调用（一次往返就够）。
    # 注意：这里**不再**做"命中就问一句"的短路。确定性能力统一走
    # capabilities.collect_capabilities 的候选确认卡（方案 §二、§四.6）：
    # ask 模式弹卡让人选，auto 模式直接执行，off 模式全走 Wren。
    # 新话题/纠正/补充的关系由补全模型判断；这里只清掉已经交付的待追问标记。
    memory_service.pop_pending_question(session_id)
    # 「是否沿用上一条问题」由用户开关决定：关掉后每句都独立理解，不再自动继承。
    inherit_context = not single_turn
    # 上下文补全必须在**意图分类之前**（docs/长对话上下文补全重构方案.md §5.3）：
    # "置信度多少？"这种短追问单看没有数据实体，分类器会当成知识问答（实测走成
    # "知识库里没有内容"）；补成完整问题以后再分类、再走拆分/写 SQL 的完整流程。
    inherited: list[str] = []
    # 追问里出现上一轮"唯一结果"的那个 id 时（"把那次巡检的明细调出来"），
    # 直接把上一轮记下的"字段 + 值"作为硬过滤条件传给查询计划——
    # 靠补全措辞去猜是哪一列会过滤错（实测把 patrol_id 的值过滤到 patrol_number 上 → 0 条）。
    nl2sql.clear_entity_filter()
    # 「同一句已经确认过的问题」：不补全、不分类、不生成 SQL，直接重放那条固化 SQL
    # （docs/固定看板重构与收藏固化方案.md 第七章：命中即停）。补全是给"不完整的追问"用的，
    # 完整问题再补一次反而更慢、还可能把条件改坏。
    reused_question = False
    if (
        not execute_choice
        and not ai_requested
        and str(chat_settings.load().get("reuse_mode") or "ask") != "off"
        and use_saved is not False
    ):
        try:
            _t = time.perf_counter()
            reused_question = bool(nl2sql.find_reusable_example(normalized))
            log_event(
                "agent",
                "INFO",
                "reuse_lookup_ms",
                ms=round((time.perf_counter() - _t) * 1000),
                hit=reused_question,
            )
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "example_reuse_lookup_failed", exc, question=message[:80])
    if execute_choice:
        # 已确认口径：问题原文就是当初那张卡上的问题，不再补全、不再分类
        effective_message = normalized
    elif inherit_context and not reused_question:
        _t = time.perf_counter()
        effective_message, inherited = memory_service.contextualize(normalized, session_id)
        log_event(
            "agent",
            "INFO",
            "contextualize_ms",
            ms=round((time.perf_counter() - _t) * 1000),
        )
    else:
        effective_message = normalized
    last_entity = (memory_service.load_session(session_id) or {}).get("last_entity") or {}
    if isinstance(last_entity, dict) and last_entity.get("id"):
        entity_ids = [str(item) for item in (last_entity.get("ids") or [last_entity["id"]])]
        # 追问里提到上一轮那个对象（单个 id 或并列里的任意一个）时，直接当硬条件
        if any(item and item in effective_message for item in entity_ids):
            nl2sql.set_entity_filter(
                list(last_entity.get("fields") or []),
                entity_ids[0],
                values=entity_ids,
            )
    # 注意：**不**在意图分类之前做"轻量确定性探测"。用户明确要求"语义规划不能省"——
    # 问法千变万化，用规则先猜分组/指标再摆卡，等于把"猜错口径"的风险提前到卡片上。
    # 摆卡必须基于完整语义规划（下面总闸算出来的计划会一路复用，只算一次）。
    probe_plan: dict[str, Any] | None = None
    intent = (
        "data"
        if (reused_question or execute_choice)
        else intent_service.classify_intent(effective_message)
    )
    event_bus.emit_event("phase", title=f"意图识别：{intent}")
    # 先判断意图，再在通用规划前补全“用户可能关心”的输出维度。扩写结果与来源
    # 一起挂到 plan 上，后续模板/校验/诊断都能追溯，而不是让规划器猜这段话是谁加的。
    question_optimization: dict[str, Any] | None = None
    planning_message = effective_message
    if (
        not (reused_question or execute_choice)
        and intent in {"data", "analysis", "composite"}
    ):
        try:
            question_optimization = nl2sql.expand_question_for_planning(effective_message)
            if question_optimization:
                planning_message = str(question_optimization["question"])
                log_event(
                    "agent",
                    "INFO",
                    "question_expanded_for_planning",
                    original=effective_message[:100],
                    expanded=planning_message[:180],
                )
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "question_expansion_failed", exc, question=message[:80])
    # 宏观元数据查询（docs/宏观元数据查询_语义理解方案.md）：问的是库/表/字段**本身**的结构
    # （有哪些表、哪些表是空的、哪张表记录最多、某张表有哪些字段），不是表里的业务数据。
    # 完全绕开业务 plan / 跨库拆解——查的是 information_schema，语义解析交给 metadata_service。
    if intent == "metadata" and not (reused_question or execute_choice or ai_requested):
        try:
            meta_payload = metadata_service.metadata_payload(effective_message)
        except Exception as exc:  # noqa: BLE001
            meta_payload = None
            log_error("agent", "metadata_route_failed", exc, question=message[:80])
        if meta_payload:
            return meta_payload
    if not (reused_question or execute_choice):
        try:
            probe_plan = nl2sql.build_query_plan(
                planning_message,
                cross_db.resolve_db_kind(planning_message),
            )
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "plan_failed", exc, question=message[:80])
        if probe_plan and question_optimization:
            probe_plan["question_optimization"] = question_optimization
    if probe_plan:
        semantic = probe_plan.get("semantic") or {}
        plan_overview_sql = (
            nl2sql.build_template_sql(probe_plan)
            if nl2sql.is_patrol_route_overview_plan(probe_plan)
            else None
        )
        log_event(
            "agent",
            "INFO",
            "plan_capability_check",
            subject=str(probe_plan.get("subject") or ""),
            shape=str(probe_plan.get("shape") or ""),
            output_fields="、".join(str(item) for item in semantic.get("output_fields") or []),
            metrics="、".join(str(item) for item in semantic.get("metrics") or []),
            patrol_route_overview=nl2sql.is_patrol_route_overview_plan(probe_plan),
            template_sql=bool(plan_overview_sql),
            template_is_overview=nl2sql.is_patrol_route_overview_sql(plan_overview_sql),
            question=planning_message[:100],
        )
        event_bus.emit_event(
            "phase",
            title=(
                f"语义规划：主体 {probe_plan.get('subject') or '-'}，"
                f"形状 {probe_plan.get('shape') or '-'}"
            ),
        )
        # 意图分类是预测，模板是否真的能生成完整 SQL 是执行事实。计划已命中运营概览
        # 且模板 SQL 可生成时，按单条确定性链路执行；否则仍尊重意图分类走拆解。
        if nl2sql.is_patrol_route_overview_sql(plan_overview_sql):
            if intent != "data":
                log_event(
                    "agent",
                    "INFO",
                    "intent_overridden_by_plan_template",
                    previous=intent,
                    template="patrol_route_overview",
                    question=planning_message[:80],
                )
            intent = "data"
    # 对话状态池的快捷回答（_subject_field_answer / _pool_answer）已按
    # docs/长对话上下文补全重构方案.md §5.2 废弃：一套机制更不容易互相打架——
    # 上一句已经在上面的 contextualize() 里补成完整问题，这里不再绕开完整流程。
    # 图片问题里如果还夹了别的领域诉求（GPS/轨迹/规范），也要按跨库问题拆，
    # 否则会被图片分支吃掉后半句。
    mixed_image = intent == "image" and str((probe_plan or {}).get("shape") or "") not in {
        "",
        "photo",
    }
    started = time.perf_counter()
    # 复合题整题卡确认时暂存的"原问题分了几步"，用于回答里如实说明覆盖面
    chosen_parts: list[dict[str, Any]] = []

    # 跨库问题必须走拆解分支，否则会被收敛成单库、丢掉另一半诉求。
    cross_scope: list[str] = []
    if execute_choice:
        # 已确认口径：跨库判定也不用做（要执行的那条 SQL 已经绑好、库也定了）
        cross_scope = []
    elif intent != "knowledge" and (intent != "image" or mixed_image):
        cross_scope = cross_db.db_scope(effective_message)

    # 数据 + 规范混在一句里时，即使意图分类器判成 knowledge，也必须走拆解，
    # 否则整句被当知识问答，数字根本不会去查库（H2/H3/H10 就是这么错的）。
    # 跨表多字段（plan.subject == "multi"）：一句话要了多张表的字段，单表链路只能答一半或
    # 答错表（docs/跨表多字段并列查询_系统性整改方案.md）。规划本来就要算，这里用的是同一份
    # 缓存（probe_plan 已经算过就直接复用），不额外花模型往返。
    multi_payload: dict[str, Any] | None = None
    if (
        intent in {"data", "analysis"}
        and not ai_requested
        and not (choose_token and capability_id)
    ):
        try:
            multi_plan = probe_plan or nl2sql.build_query_plan(
                planning_message, cross_db.resolve_db_kind(planning_message)
            )
            multi_fields = list((multi_plan.get("semantic") or {}).get("fields") or [])
            # subject=multi 是主判据；模型漏填 subject 但列了两个以上字段时也试一次
            # （_multi_field_payload 自己会判断"其实是一张表"并返回 None 交回原链路）
            out_fields = list((multi_plan.get("semantic") or {}).get("output_fields") or [])
            # 跨表多字段**不再走 multi_field_join / multi_field_parallel**：那套靠列名匹配
            # 选表，选错也照样执行（实测"路段长度"被 JOIN 到边坡表的 length → 结果完全错），
            # 而且跳过用户确认。这里只记一条日志，让下面的链路按常规走
            # （模板卡确认 / Wren 语义层）——见 docs/清理不可靠引擎与通用算子修复方案.md §三。
            if (
                str(multi_plan.get("subject") or "") == "multi"
                or len(multi_fields) >= 2
                or len(out_fields) >= 2
            ):
                log_event(
                    "agent",
                    "INFO",
                    "multi_field_engine_skipped",
                    fields="、".join(str(item) for item in (multi_fields or out_fields))[:60],
                    question=effective_message[:60],
                )
        except Exception as exc:  # noqa: BLE001
            log_error("agent", "multi_field_failed", exc, question=message[:120])
            multi_payload = None

    # 拆分要不要做、怎么做，交给 LLM（docs/项目智能化升级方案.md 升级2）：
    # 这里先一律标记"可能要多任务"，由下面的总闸（确定性模板能一次答对）和
    # orchestrator.decompose（LLM 优先，返回 1 条=单查询）来收敛。
    mixed_request = intent not in {"knowledge", "image"} and bool(effective_message.strip())
    # 总闸：确定性模板能一次答对就不拆（实测 F8「6月和7月平均每天各多少」、F10「两天占7月比例」
    # 被拆后每份子问题只答一半、指标反而丢；全量 88/90 → 84/90）。见拆分器一致性报告。
    # 只在"纯数据"时启用总闸：数据+规范混合的问题模板只能答一半，必须拆开（H2/H3/H10）。
    # 关键：**"模板能生成 SQL"≠"模板能答对"**。模板是关键词驱动、不看 SQL 形状的，
    # "数量最多的病害类型"它能拼出 SQL（返回明细），但那不是用户要的答案。
    # 所以总闸必须和模板快速通道用**同一个准入判断**：只有简单单条件查询才允许不拆。
    if (
        not execute_choice
        and mixed_request
        and intent == "data"
    ):
        try:
            # 这次规划的结果**存下来**给后面的执行复用：总闸为了判断"要不要拆"
            # 已经算过一遍完整计划，`_build_query_result` / `_handle_composite` 不该再算一遍
            # （方案 §6.3 P0-4：同一次请求里同一问题的计划只规划一次）。
            if probe_plan is None:
                probe_plan = nl2sql.build_query_plan(
                    planning_message, cross_db.resolve_db_kind(planning_message)
                )
            template_sql = nl2sql.build_template_sql(probe_plan)
            if template_sql:
                from . import templatizer

                # 多子问题不能因为“拼出了一条 SQL”就取消拆分：一条模板
                # 很可能只覆盖前半问，后半问会被静默丢掉。
                if (
                    not templatizer.question_is_partial(planning_message)
                    or nl2sql.is_patrol_route_overview_plan(probe_plan)
                ):
                    mixed_request = False
        except Exception:  # noqa: BLE001
            pass
    if intent == "knowledge" and not mixed_request:
        try:
            payload = _answer_knowledge(effective_message)
        except Exception as exc:
            log_error("agent", "knowledge_failed", exc, question=message[:200])
            payload = _error_payload(f"知识问答这次没成功：{exc}", source="ragflow")
    elif multi_payload is not None:
        # 跨表多字段：已经按字段关系查好了（join 一条 / 并列分段）
        payload = multi_payload
    elif intent == "image" and not mixed_image:
        # 「让 AI 自己查」有两种回执：force_ai=true（新卡片）和 capability_id='ai'/'wren'
        # （老卡片/步骤卡）。图片题原来只看 force_ai，于是老卡片点一次又弹一张卡
        # （docs/弹卡链路系统性Bug整改方案.md Bug 1）。
        # 「看图 + 别的诉求」的句子（"哪个星期平均面积最大？把那周的病害图片都调出来"）不能被
        # 整句吞成"只看图"。判据用**语义计划**：纯看图的 shape 是 photo；只要 shape 不是 photo
        # （top/distribution/trend…），说明这句还有分组/统计诉求 → 先交给拆解器，
        # 拆出 ≥2 步就按复合题走，"要图"降为某一步的渲染属性（wants_images）；
        # 只有纯看图（或拆不开）才直接走看图链路。plan 是并行预取的那份，不额外花往返。
        split_tasks: list[dict[str, Any]] | None = None
        plan_shape = str((probe_plan or {}).get("shape") or "")
        if plan_shape and plan_shape != "photo":
            try:
                split_tasks = cross_db.decompose_cross_db(effective_message, cross_scope or None)
            except Exception as exc:  # noqa: BLE001
                split_tasks = None
                log_error("agent", "image_split_decompose_failed", exc, question=effective_message[:80])
        if split_tasks and len(split_tasks) > 1:
            log_event(
                "agent",
                "INFO",
                "image_split_to_composite",
                shape=plan_shape,
                steps=len(split_tasks),
                question=effective_message[:60],
            )
            try:
                payload = _handle_composite(
                    effective_message,
                    cross_scope,
                    session_id,
                    force_ai=ai_requested,
                    plan=probe_plan,
                    tasks=split_tasks,
                    intent=intent,
                )
            except capabilities.ChooseCapability:
                raise
            except clarify.ClarifyNeeded as need:
                payload = _clarify_payload(effective_message, need, session_id)
            except Exception as exc:  # noqa: BLE001
                log_error("agent", "image_split_composite_failed", exc, question=message[:120])
                payload = _image_result(effective_message, force_ai=ai_requested)
        else:
            payload = _image_result(effective_message, force_ai=ai_requested)
    elif cross_scope == ["seaweedfs"] and intent_service.wants_images(effective_message):
        # 这条原来看的是"跨库判定说这句话是对象存储"，而那个判定内部是**关键词**表——
        # 现在再叠一道语义闸：意图分类说"要看图"才走看图（主判据是上面那句 intent == "image"，
        # 这里只是接着它兜底）。wants_images 的语义结论是缓存过的同一次调用，不额外花模型往返。
        payload = _image_result(effective_message, force_ai=ai_requested)
    elif (
        (intent == "composite" or mixed_request or len(cross_scope) > 1)
        # 用户已经在确认卡里选了口径：这次请求是"按选择执行"，不要再走一遍拆分/收集，
        # 否则会重新弹一张一模一样的卡（实测第二次点"用这口径查询"仍只回确认卡）。
        and not (choose_token and capability_id)
    ):
        try:
            payload = _handle_composite(
                planning_message,
                cross_scope,
                session_id,
                force_ai=ai_requested,
                # 整题计划如果探测时已经算过就直接复用，不重复规划
                plan=probe_plan,
                intent=intent,
                original_message=effective_message,
            )
        except capabilities.ChooseCapability:
            # 复合题的能力确认卡（子步骤候选）要冒泡到 handle_message 去组装，
            # 不是"这条没做成"——别被下面的兜底当成错误吞掉。
            raise
        except clarify.ClarifyNeeded as need:
            payload = _clarify_payload(effective_message, need, session_id)
        except DatabaseError as exc:
            payload = {
                "reply": str(exc),
                "source": "database",
                "sql": None,
                "columns": [],
                "rows": [],
                "chart_data": None,
                "result_id": None,
                "references": [],
            }
        except Exception as exc:
            log_error("agent", "composite_failed", exc, question=message[:200])
            payload = _error_payload(f"这次没能完成：{exc}")
    else:
        # 「按步骤逐条查 / 每步选口径」这条交互已经删掉（用户要求：要么一次答出来，要么靠 Wren）。
        # 复合题只剩两条路：整题卡上选一条能一次答对的口径，或者让 AI 走 Wren。
        # 用户在确认卡里选了口径：取出暂存的 plan + 候选用，不重新规划
        chosen_sql = None
        chosen_engine = ""
        # 这次用的是哪条候选（模板/示例）——反馈归因要用：只有知道是哪条模板，
        # 才能"连续 3 次答错自动降级"（docs/模板沉淀标准与复用校验_系统性方案.md §5.2）
        chosen_candidate: dict[str, Any] = {}
        chosen_plan = None
        # "这句是要看图"的语义结论随卡片一起带过来（见 _choose_capability_payload）：
        # 点确认这一次不再问意图，也不会退回关键词猜。
        chosen_want_images = False
        # 用户选的示例当前编译不出物理 SQL 时（Wren 环境不可用）：不是报错，而是
        # 回落到这条问题自己的 Wren 链路重查（候选仍然摆出来、不让确认过的答案凭空消失）
        # （docs/示例直达被generic挤占与人话推荐理由整改.md P0-2）。
        wren_replay_failed = False
        if choose_token and capability_id:
            cached = capabilities.take(choose_token)
            if cached is None and capability_id not in {"ai", "wren"}:
                # 硬保证（docs/选模板直出提速与去除后续追问方案.md §7.4 P0-A）：
                # 用户点的是确定性口径，但暂存已经取不到（重复提交/TTL 过期/进程重启）——
                # 绝不允许带着空的 forced_sql 悄悄换成 Wren 重查，必须重新摆卡让他重新选。
                log_event(
                    "agent",
                    "WARNING",
                    "capability_token_missing",
                    capability_id=str(capability_id),
                    question=message[:80],
                )
                retry_plan = nl2sql.build_query_plan(
                    effective_message, cross_db.resolve_db_kind(effective_message)
                )
                retry_candidates = capabilities.collect_capabilities(
                    effective_message, retry_plan, "mysql"
                )
                if retry_candidates:
                    raise capabilities.ChooseCapability(
                        retry_plan,
                        retry_candidates,
                        note="你刚才选的口径已经失效（可能重复提交或停留太久），请重新选一次。",
                    )
                payload = _error_payload(
                    "你选的口径已经失效了，请把问题重新发一次（我不会用别的引擎偷偷替你查）。"
                )
                return payload
            # 步骤卡已删除：暂存里不会再带 steps，这里只可能是"整题口径"那一种卡
            if cached:
                chosen_want_images = bool(cached.get("want_images"))
                cached_plan = cached.get("plan") or {}
                cached_candidates = cached.get("candidates") or []
                chosen_parts = list(cached.get("tasks") or [])
                chosen_plan = cached_plan
                if capability_id not in {"ai", "wren"}:
                    picked = next(
                        (item for item in cached_candidates if str(item.get("id")) == str(capability_id)),
                        None,
                    )
                    if picked is None:
                        # 候选 id 对不上（前端传了另一个字段/卡片过期）也算绑定失败：
                        # 记清楚是哪一次、传了什么、候选都有谁，然后重新摆卡，绝不静默换引擎。
                        log_event(
                            "agent",
                            "WARNING",
                            "capability_pick_missing",
                            got=str(capability_id),
                            have="、".join(str(item.get("id")) for item in cached_candidates)[:200],
                            question=message[:80],
                        )
                        raise capabilities.ChooseCapability(
                            cached_plan,
                            cached_candidates,
                            tasks=chosen_parts,
                            note="你选的那个口径对不上了，请重新选一次。",
                        )
                    if picked:
                        # 用户改过的槽位要写回计划，否则执行前校验还按老条件判
                        capabilities.apply_overrides(cached_plan, slot_values or {})
                        chosen_sql, note = capabilities.execute(
                            cached_plan, picked, slot_values or {}, "mysql"
                        )
                        if chosen_sql and picked.get("replay_via_wren"):
                            compiled = wren_engine.compile_mdl_sql("mysql", chosen_sql)
                            if compiled:
                                chosen_sql = str(compiled)
                            else:
                                log_event(
                                    "agent",
                                    "INFO",
                                    "example_wren_replay_failed",
                                    doc=str(picked.get("id")),
                                )
                                chosen_sql = None
                                wren_replay_failed = True
                        if chosen_sql:
                            chosen_engine = f"chosen_{picked.get('kind')}"
                            chosen_candidate = {
                                "kind": str(picked.get("kind") or ""),
                                "id": str(picked.get("id") or ""),
                            }
                            log_event(
                                "agent",
                                "INFO",
                                "capability_chosen",
                                kind=str(picked.get("kind")),
                                id=str(picked.get("id")),
                                note=note,
                            )
                        else:
                            # 选了口径但拼不出 SQL（最常见：模板还差待补条件没填全）：
                            # 把原因原样带回卡片，让用户补齐再点确认，别默默换成"重新规划"。
                            log_event(
                                "agent",
                                "INFO",
                                "capability_bind_failed",
                                kind=str(picked.get("kind")),
                                id=str(picked.get("id")),
                                reason=note,
                            )
                            raise capabilities.ChooseCapability(
                                cached_plan,
                                cached_candidates,
                                tasks=chosen_parts,
                                note=f"这条口径还不能直接查：{note}",
                            )
        try:
            result = _build_query_result(
                planning_message,
                intent,
                # 用户选定的口径已经绑好库（上面 capabilities.execute 按 mysql 拼，候选也只在
                # kind=="mysql" 时收集）：这里不能再从问题里重推库——"图片"类问题的 scope 是
                # seaweedfs（对象存储，不是数据库），重推会拿 MySQL 的 SQL 去过 PG 白名单，
                # 报 "SQL 使用的表不在白名单中：basic_facilities_sections"
                # （实测"调出来路段封面的图片"点卡后 chosen_sql_failed，每次都只回卡）。
                kind="mysql" if chosen_sql else None,
                # 用户点"让 AI 自己查"（或本条 force_ai）：确定性的示例/模板闸也一并跳过，走完整 Wren 链路
                use_saved=(
                    False
                    if (ai_requested or wren_replay_failed)
                    else use_saved
                ),
                forced_sql=chosen_sql,
                forced_engine=chosen_engine,
                # 用户选的口径优先；否则复用探测阶段已经算好的计划（不再重复规划，方案 §6.3 P0-4）
                plan=chosen_plan or probe_plan,
                # 这次别再弹候选卡：只限用户点了"让 AI 自己查"。
                # 注意不能因为"总闸已经算过计划"就跳过候选收集——那会让内置模板命中时
                # 不经用户确认直接出数，违背"模板/相似问法一律先弹卡确认"。
                skip_capabilities=ai_requested or wren_replay_failed,
                # 放弃所有确定性能力，直接走 Wren 语义层
                wren_first=ai_requested or wren_replay_failed,
            )
        except capabilities.ChooseCapability:
            # 能力确认卡要冒泡到 handle_message 去组装（不是查询失败，别当成错误吞掉）
            raise
        except clarify.ClarifyNeeded as need:
            # 继承上一轮条件后拼不出 SQL 时，用原句再试一次再决定要不要追问用户——
            # 很多时候是继承来的条件不适用新问题，不该让用户去解释字段。
            if effective_message != normalized:
                try:
                    result = _build_query_result(normalized, intent, use_saved=use_saved)
                except clarify.ClarifyNeeded as retry_need:
                    payload = _clarify_payload(normalized, retry_need, session_id)
                except Exception as exc:
                    log_error("agent", "query_retry_failed", exc, question=message[:200])
                    payload = _error_payload(f"查询执行失败：{exc}")
                else:
                    result_id = result["result_id"]
                    save_result(result_id, result)
                    payload = {
                        "reply": result.get("reply", "查询完成。"),
                        "source": result.get("source"),
                        "sql": result.get("sql"),
                        "columns": result.get("columns", []),
                        "rows": result.get("rows", []),
                        "chart_data": result.get("chart_data"),
                        "result_id": result_id,
                        "shape": result.get("shape"),
                        "required_conditions": result.get("required_conditions"),
        "tables": result.get("tables"),
                        "answer_format": result.get("answer_format"),
                        "lineage": result.get("lineage"),
                        "engine": result.get("engine"),
                        "matched_template": result.get("matched_template"),
                        "references": result.get("references", []),
                        "question": message,
                        "used_question": result.get("used_question"),
                        "reviewable": result.get("reviewable"),
                        "context": result.get("context"),
                        "spec": result.get("spec"),
                        "patrol_overview": result.get("patrol_overview"),
                        "question_optimization": result.get("question_optimization"),
                        "risk_warnings": result.get("risk_warnings") or [],
                    }
            else:
                payload = _clarify_payload(effective_message, need, session_id)
        except DatabaseError as exc:
            payload = {
                "reply": str(exc),
                "source": "database",
                "sql": None,
                "columns": [],
                "rows": [],
                "chart_data": None,
                "result_id": None,
                "references": [],
            }
        except Exception as exc:
            log_error("agent", "query_failed", exc, question=message[:200])
            payload = _error_payload(f"查询执行失败：{exc}")
        else:
            result_id = result["result_id"]
            # 用户就是要看图、而且这条口径真的取回了图片路径：先把图片渲染结果并进结果**再落盘**，
            # 否则"查看这次结果"和看板刷新里就没有图（它们都按 result_id 读缓存）。
            pictured: dict[str, Any] = {}
            if chosen_want_images:
                pictured = _images_payload(effective_message, result)
                if pictured["images"] or pictured["missing_images"]:
                    result = {**result, **pictured, "result_id": result_id}
            save_result(result_id, result)
            payload = {
                "reply": result.get("reply", "查询完成。"),
                "source": result.get("source"),
                "sql": result.get("sql"),
                "columns": result.get("columns", []),
                "rows": result.get("rows", []),
                "chart_data": result.get("chart_data"),
                "result_id": result_id,
                "shape": result.get("shape"),
                "required_conditions": result.get("required_conditions"),
        "tables": result.get("tables"),
                "answer_format": result.get("answer_format"),
                "lineage": result.get("lineage"),
                "engine": result.get("engine"),
                "matched_template": result.get("matched_template"),
                "references": result.get("references", []),
                "question": message,
                "used_question": result.get("used_question"),
                "reviewable": result.get("reviewable"),
                # 用户已选口径的直出结果：后面不再跑校验/反思/后续建议（省时也省调用）
                "direct": result.get("direct"),
                "context": result.get("context"),
                "spec": result.get("spec"),
                "patrol_overview": result.get("patrol_overview"),
                "question_optimization": result.get("question_optimization"),
                "risk_warnings": result.get("risk_warnings") or [],
                # 这次用的哪条候选（模板/示例）——"答错了"反馈据此归因、连续 3 次降级
                "candidate": chosen_candidate or None,
            }
            # 对话回复也按图渲染（前端按 images 出图）；一条图片路径都没取到时保持表格形态。
            if pictured:
                payload.update({k: v for k, v in pictured.items() if k != "result_id"})

    # 复合题整题卡确认后的如实说明：用户选的口径只覆盖其中一部分
    if len(chosen_parts) > 1 and payload.get("reply") and payload.get("result_id"):
        payload["reply"] = (
            f"（这条问题含 {len(chosen_parts)} 步，本次按你选的口径只回答了其中与它对应的部分；"
            "其余部分可以继续追问，或让 AI 用 Wren 查。）\n"
        ) + str(payload["reply"])
        payload["partial"] = True
    # 跑批（回归/脚本）不写用户的历史与检索日志：跑一遍就是几十条重复，
    # 用户在前端会看到"同一句问了几十遍"（实测 93 次）。
    if not _is_batch_run():
        add_history(
            question=message,
            intent=intent,
            sql_text=payload.get("sql"),
            result_id=payload.get("result_id"),
            summary=payload.get("reply"),
        )
    references = payload.get("references") or []
    lineage = payload.get("lineage") or {}
    dataset_names = {ref.get("dataset_name") for ref in references if ref.get("dataset_name")}
    if isinstance(lineage, dict):
        dataset_names.update(lineage.get("datasets") or [])
    if not _is_batch_run():
        add_retrieval_log(
            question=message,
            intent=intent,
            result_id=payload.get("result_id"),
            datasets=sorted(name for name in dataset_names if name),
            references=references,
        )
    if question_optimization and not payload.get("question_optimization"):
        payload["question_optimization"] = question_optimization
    payload["intent"] = intent
    # 只有真的继承了条件才在回答里写明；仅仅因为 normalize 去掉了"呢"不算改写。
    payload["effective_message"] = (
        effective_message if effective_message != normalized else None
    )
    if inherited and payload.get("reply"):
        detail = "、".join(dict.fromkeys(inherited))
        payload["reply"] = f"（已沿用上一条问题的条件：{detail}）\n{payload['reply']}"
        payload["inherited"] = inherited
    if single_turn:
        _clear_turn_context(session_id)
    else:
        memory_service.save_session(session_id, payload.get("context"))
        memory_service.set_last_question(session_id, message)
    log_event("agent", "INFO", "request_done", question=message[:200], intent=intent, result_id=payload.get("result_id"), answer_format=payload.get("answer_format"), duration_ms=round((time.perf_counter() - started) * 1000))
    return payload
