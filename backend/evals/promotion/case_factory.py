from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Callable

import yaml

from app.services import prompts
from app.services.llm import chat_json, role_model

from .models import ExamCase


Generator = Callable[[str, set[str], int], list[ExamCase]]
_PROMOTION_DIR = Path(__file__).resolve().parent


def _default_generator(
    document: str,
    exclude_ids: set[str],
    count: int,
) -> list[ExamCase]:
    response = chat_json(
        prompts.messages(
            "promotion_exam_generate",
            business_document=document,
            exclude_ids=json.dumps(sorted(exclude_ids), ensure_ascii=False),
            case_count=str(count),
        ),
        model=role_model("planner"),
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
        )
        for item in raw_cases
        if isinstance(item, dict)
    ]


def generate_exam_cases(
    document: str,
    exclude_ids: set[str],
    count: int = 15,
    generator: Generator | None = None,
) -> list[ExamCase]:
    generated = list(
        (generator or _default_generator)(
            document,
            set(exclude_ids),
            count,
        )
    )
    filtered = [
        item
        for item in generated
        if item.id
        and item.question
        and item.id not in exclude_ids
    ]
    unique: list[ExamCase] = []
    seen: set[str] = set()
    for item in filtered:
        if item.id in seen:
            continue
        seen.add(item.id)
        unique.append(item)
    if len(unique) != count:
        raise ValueError(
            f"expected {count} fresh exam cases, got {len(unique)}"
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
    return result


def sample_old_cases(seed: int, count: int = 5) -> list[ExamCase]:
    pool = sorted(load_old_cases(), key=lambda item: item.id)
    if len(pool) < count:
        raise ValueError(f"old pool has only {len(pool)} cases")
    return random.Random(int(seed)).sample(pool, count)
