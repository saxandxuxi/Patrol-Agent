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
    exclude_questions: set[str],
) -> list[ExamCase]:
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
) -> list[ExamCase]:
    excluded_questions = {
        "".join(str(item).split())
        for item in (exclude_questions or set())
        if str(item).strip()
    }
    current_ids = set(exclude_ids)
    current_questions = set(exclude_questions or set())
    collected: list[ExamCase] = []
    for _attempt in range(4):
        generated = list(
            _default_generator(
                document,
                set(current_ids),
                count,
                set(current_questions),
            )
            if generator is None
            else generator(document, set(current_ids), count)
        )
        for item in generated:
            normalized_question = "".join(item.question.split())
            if (
                not item.id
                or not item.question
                or item.id in current_ids
                or normalized_question in excluded_questions
            ):
                continue
            collected.append(item)
            current_ids.add(item.id)
            excluded_questions.add(normalized_question)
            if len(collected) >= count:
                break
        if len(collected) >= count:
            break
        current_questions.update(excluded_questions)
    unique = collected[:count]
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
