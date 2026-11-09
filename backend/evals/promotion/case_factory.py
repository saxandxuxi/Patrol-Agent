from __future__ import annotations

import json
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Callable

import yaml

from app.query.capability_registry import CapabilityRegistry
from app.services import prompts
from app.services.llm import chat_json, role_model

from .models import ExamCase


Generator = Callable[[str, set[str], int], list[ExamCase]]
_PROMOTION_DIR = Path(__file__).resolve().parent
_GENERATION_BATCH_SIZE = 5
_FRESH_FOCUS_SLOTS = (
    "病害数量与规模",
    "指定日期或单次巡检",
    "路线范围与路线对比",
    "病害类型分布与占比",
    "上下行等方向分组",
    "巡检任务次数与任务明细",
    "设备巡检次数或工作量",
    "TopN 路线或分组排名",
    "具体记录明细",
    "时间范围趋势或月度变化",
    "尺寸阈值与长度面积条件",
    "跨资产维度分析",
    "路段、桩号或分段范围",
    "跨批次新增与前后对比",
    "当前数据能力不足",
)


def _generation_capability_gaps() -> dict[str, dict[str, object]]:
    registry = CapabilityRegistry.load("mysql")
    return {
        gap_id: {
            "status": str(raw.get("status") or "unavailable"),
            "applies_to": [
                str(item) for item in raw.get("applies_to") or ()
            ],
            "reason": str(raw.get("reason") or ""),
        }
        for gap_id, raw in registry.snapshot.capability_gaps.items()
        if isinstance(raw, Mapping)
    }


def _generation_unavailable_fields() -> dict[str, dict[str, str]]:
    registry = CapabilityRegistry.load("mysql")
    return {
        f"{subject}.{field_id}": {
            "status": field.status,
            "reason": field.unavailable_reason,
        }
        for subject, fields in registry.snapshot.fields.items()
        for field_id, field in fields.items()
        if field.status in {"unavailable", "lookup_required"}
    }


def _default_generator(
    document: str,
    exclude_ids: set[str],
    count: int,
    exclude_questions: set[str],
    batch_focus: tuple[str, ...] = (),
    retry_evidence: dict[str, list[dict[str, str]]] | None = None,
) -> list[ExamCase]:
    focus = batch_focus or _FRESH_FOCUS_SLOTS[:count]
    response = chat_json(
        prompts.messages(
            "promotion_exam_generate",
            business_document=document,
            exclude_ids=json.dumps(sorted(exclude_ids), ensure_ascii=False),
            exclude_questions=json.dumps(
                sorted(exclude_questions),
                ensure_ascii=False,
            ),
            case_count=str(count),
            capability_gaps=json.dumps(
                _generation_capability_gaps(),
                ensure_ascii=False,
                sort_keys=True,
            ),
            unavailable_fields=json.dumps(
                _generation_unavailable_fields(),
                ensure_ascii=False,
                sort_keys=True,
            ),
            batch_focus=json.dumps(focus, ensure_ascii=False),
            retry_evidence=json.dumps(
                retry_evidence or {},
                ensure_ascii=False,
                sort_keys=True,
            ),
        ),
        model=role_model("planner"),
        max_tokens=4096,
    )
    raw_cases = response.get("cases") if isinstance(response, dict) else None
    if not isinstance(raw_cases, list):
        raise ValueError("promotion exam generator did not return cases")
    return [
        ExamCase(
            id=str(item.get("id") or ""),
            category=str(item.get("category") or ""),
            question=str(item.get("question") or ""),
            focus=str(item.get("focus") or ""),
            source="fresh",
            coverage_slot=str(item.get("coverage_slot") or ""),
        )
        for item in raw_cases
        if isinstance(item, dict)
    ]


def generate_exam_cases(
    document: str,
    exclude_ids: set[str],
    count: int = 15,
    generator: Generator | None = None,
    exclude_questions: set[str] | None = None,
    focus_slots: tuple[str, ...] | None = None,
) -> list[ExamCase]:
    focus_pool = tuple(
        dict.fromkeys(
            str(item).strip()
            for item in (focus_slots or _FRESH_FOCUS_SLOTS[:count])
            if str(item).strip()
        )
    )
    if len(focus_pool) < count:
        raise ValueError(
            f"focus_slots must contain at least {count} distinct slots"
        )
    excluded_questions = {
        "".join(str(item).split())
        for item in (exclude_questions or set())
        if str(item).strip()
    }
    current_ids = set(exclude_ids)
    current_questions = set(exclude_questions or set())
    collected: list[ExamCase] = []
    pending_focus = list(focus_pool[:count])
    retry_evidence: dict[str, list[dict[str, str]]] = {}
    focus_cursor = 0
    for _attempt in range(8):
        remaining = count - len(collected)
        if remaining <= 0:
            break
        request_count = min(remaining, _GENERATION_BATCH_SIZE)
        if generator is None:
            batch_focus = tuple(pending_focus[:request_count])
        else:
            batch_focus = tuple(
                _FRESH_FOCUS_SLOTS[
                    (focus_cursor + offset) % len(_FRESH_FOCUS_SLOTS)
                ]
                for offset in range(request_count)
            )
            focus_cursor += request_count
        generated = list(
            _default_generator(
                document,
                set(current_ids),
                request_count,
                set(current_questions),
                batch_focus=batch_focus,
                retry_evidence={
                    slot: retry_evidence.get(slot, [])
                    for slot in batch_focus
                    if retry_evidence.get(slot)
                },
            )
            if generator is None
            else generator(document, set(current_ids), request_count)
        )
        for item in generated:
            normalized_question = "".join(item.question.split())
            rejection_reason = ""
            if not item.id:
                rejection_reason = "missing_id"
            elif not item.question:
                rejection_reason = "missing_question"
            elif item.id in current_ids:
                rejection_reason = "duplicate_id"
            elif normalized_question in excluded_questions:
                rejection_reason = "duplicate_question"
            if (
                rejection_reason
            ):
                if (
                    generator is None
                    and item.coverage_slot in batch_focus
                ):
                    bucket = retry_evidence.setdefault(
                        item.coverage_slot,
                        [],
                    )
                    bucket.append(
                        {
                            "question": item.question,
                            "reason": rejection_reason,
                        }
                    )
                    del bucket[:-3]
                continue
            if generator is None and item.coverage_slot not in batch_focus:
                continue
            if generator is None and item.coverage_slot not in pending_focus:
                continue
            collected.append(item)
            current_ids.add(item.id)
            excluded_questions.add(normalized_question)
            if generator is None:
                pending_focus.remove(item.coverage_slot)
            if len(collected) >= count:
                break
        if len(collected) >= count:
            break
        current_questions.update(excluded_questions)
    unique = collected[:count]
    if len(unique) != count:
        missing_slots = list(pending_focus)
        rejection_evidence = {
            slot: retry_evidence.get(slot, [])
            for slot in missing_slots
            if retry_evidence.get(slot)
        }
        raise ValueError(
            f"expected {count} fresh exam cases, got {len(unique)}; "
            f"missing_slots={json.dumps(missing_slots, ensure_ascii=False)}; "
            "rejection_evidence="
            f"{json.dumps(rejection_evidence, ensure_ascii=False)}"
        )
    return unique


def load_old_cases() -> list[ExamCase]:
    path = _PROMOTION_DIR / "old_pool.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result: list[ExamCase] = []
    for raw_source in data.get("sources") or []:
        source_path = (_PROMOTION_DIR / str(raw_source)).resolve()
        source = yaml.safe_load(source_path.read_text(encoding="utf-8")) or {}
        for item in source.get("cases") or []:
            if not isinstance(item, dict):
                continue
            case_id = str(item.get("id") or "").strip()
            question = str(item.get("question") or "").strip()
            if not case_id or not question:
                continue
            result.append(
                ExamCase(
                    id=case_id,
                    category=str(item.get("category") or "历史题"),
                    question=question,
                    focus=str(item.get("focus") or ""),
                    source="old",
                )
            )
    runs_dir = _PROMOTION_DIR / "runs"
    if runs_dir.exists():
        for path in sorted(runs_dir.glob("*.yaml")):
            manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in manifest.get("cases") or []:
                if not isinstance(item, dict):
                    continue
                case_id = str(item.get("id") or "").strip()
                question = str(item.get("question") or "").strip()
                if not case_id or not question:
                    continue
                result.append(
                    ExamCase(
                        id=case_id,
                        category=str(item.get("category") or "历史考试题"),
                        question=question,
                        focus=str(item.get("focus") or ""),
                        source="old",
                    )
                )
    deduped: dict[str, ExamCase] = {}
    for item in result:
        deduped.setdefault(item.id, item)
    return list(deduped.values())


def sample_old_cases(seed: int, count: int = 5) -> list[ExamCase]:
    pool = sorted(load_old_cases(), key=lambda item: item.id)
    if len(pool) < count:
        raise ValueError(f"old pool has only {len(pool)} cases")
    return random.Random(int(seed)).sample(pool, count)
