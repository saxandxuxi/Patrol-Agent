from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

from evals.run_holdout_eval import _default_judge

from .case_factory import (
    generate_exam_cases,
    load_old_cases,
    sample_old_cases,
)
from .models import ExamCase, ExamManifest
from .product_runner import run_product_case
from .runner import code_fingerprint, run_exam


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DOCUMENT = PROJECT_ROOT / "docs" / "archive" / "巡查业务问题.md"


def _document_text() -> str:
    return DOCUMENT.read_text(encoding="utf-8")


def _document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_manifest(path: Path, manifest: ExamManifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "exam_id": manifest.exam_id,
        "generated_at": manifest.generated_at,
        "document_hash": manifest.document_hash,
        "seed": manifest.seed,
        "code_fingerprint": manifest.code_fingerprint,
        "fresh_case_ids": list(manifest.fresh_case_ids),
        "old_case_ids": list(manifest.old_case_ids),
        "cases": [asdict(item) for item in manifest.cases],
    }
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _load_manifest(path: Path) -> ExamManifest:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = tuple(
        ExamCase(
            id=str(item.get("id") or ""),
            category=str(item.get("category") or ""),
            question=str(item.get("question") or ""),
            focus=str(item.get("focus") or ""),
            source=str(item.get("source") or "fresh"),
        )
        for item in data.get("cases") or []
        if isinstance(item, dict)
    )
    return ExamManifest(
        exam_id=str(data.get("exam_id") or ""),
        generated_at=str(data.get("generated_at") or ""),
        document_hash=str(data.get("document_hash") or ""),
        seed=int(data.get("seed") or 0),
        code_fingerprint=str(data.get("code_fingerprint") or ""),
        fresh_case_ids=tuple(str(item) for item in data.get("fresh_case_ids") or ()),
        old_case_ids=tuple(str(item) for item in data.get("old_case_ids") or ()),
        cases=cases,
    )


def _generate(output: Path, seed: int) -> int:
    document = _document_text()
    old_cases = sample_old_cases(seed=seed, count=5)
    old_pool = load_old_cases()
    excluded_ids = {item.id for item in old_pool}
    excluded_questions = {item.question for item in old_pool}
    for previous_path in sorted(output.parent.glob("*.yaml")):
        try:
            previous = _load_manifest(previous_path)
        except Exception:
            continue
        excluded_ids.update(previous.fresh_case_ids)
        excluded_questions.update(item.question for item in previous.fresh_cases())
    fresh_cases = generate_exam_cases(
        document,
        exclude_ids=excluded_ids,
        count=15,
        exclude_questions=excluded_questions,
    )
    manifest = ExamManifest(
        exam_id=f"promotion-{datetime.now().strftime('%Y%m%d-%H%M%S')}",
        generated_at=datetime.now().astimezone().isoformat(),
        document_hash=_document_hash(document),
        seed=seed,
        code_fingerprint=code_fingerprint(PROJECT_ROOT),
        fresh_case_ids=tuple(item.id for item in fresh_cases),
        old_case_ids=tuple(item.id for item in old_cases),
        cases=tuple(fresh_cases + old_cases),
    )
    _write_manifest(output, manifest)
    print(output)
    return 0


def _run(cases_path: Path, report_path: Path) -> int:
    os.environ["CODEX_REGRESSION"] = "1"
    manifest = _load_manifest(cases_path)
    document = _document_text()
    if manifest.document_hash != _document_hash(document):
        raise RuntimeError(
            "business document changed; generate a fresh 15-question exam"
        )
    report = run_exam(
        manifest,
        runner=run_product_case,
        judge=lambda case, payload, text: _default_judge(
            asdict(case),
            payload,
            text,
        ),
        document=document,
        first_attempt=not report_path.exists(),
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.promoted else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "run"))
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--seed", type=int, default=20261107)
    args = parser.parse_args()
    if args.command == "generate":
        output = args.output or (
            Path(__file__).with_name("runs")
            / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.yaml"
        )
        return _generate(output, args.seed)
    if not args.cases:
        parser.error("run requires --cases")
    report = args.report or args.cases.with_suffix(".report.json")
    return _run(args.cases, report)


if __name__ == "__main__":
    raise SystemExit(main())
