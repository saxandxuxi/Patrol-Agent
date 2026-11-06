import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from evals.promotion.case_factory import (
    generate_exam_cases,
    load_old_cases,
    sample_old_cases,
)
from evals.promotion.models import ExamCase, ExamManifest
from evals.promotion.runner import (
    StaleExamError,
    assert_promotion,
    code_fingerprint,
    run_exam,
)
from evals.promotion.product_runner import run_product_case


def cases(source, count, prefix):
    return [
        ExamCase(
            id=f"{prefix}_{index:02d}",
            category="病害",
            question=f"{prefix} 问题 {index}",
            focus="检查是否答所问",
            source=source,
        )
        for index in range(1, count + 1)
    ]


def manifest(fresh_ok=15, old_ok=5, fingerprint="code-1"):
    fresh = cases("fresh", 15, "fresh")
    old = cases("old", 5, "old")
    return ExamManifest(
        exam_id="exam-1",
        generated_at="2026-11-07T12:00:00+08:00",
        document_hash="doc-1",
        seed=7,
        code_fingerprint=fingerprint,
        fresh_case_ids=tuple(item.id for item in fresh),
        old_case_ids=tuple(item.id for item in old),
        cases=tuple(fresh + old),
        answers={
            **{item.id: fresh_ok > index for index, item in enumerate(fresh)},
            **{item.id: old_ok > index for index, item in enumerate(old)},
        },
    )


class PromotionEvalTests(unittest.TestCase):
    def test_promotes_at_exact_thresholds(self):
        item = manifest(fresh_ok=12, old_ok=4)
        results = run_exam(
            item,
            runner=lambda case: {"answer": case.question},
            judge=lambda case, payload, document: {
                "accepted": item.answers[case.id],
                "verdict": "correct_answer" if item.answers[case.id] else "wrong_value",
                "failure_layer": "" if item.answers[case.id] else "verification",
                "reason": "" if item.answers[case.id] else "答非所问",
            },
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
        )

        self.assertEqual(results.passed_fresh, 12)
        self.assertEqual(results.passed_old, 4)
        self.assertTrue(results.promoted)
        assert_promotion(results)

    def test_eleven_fresh_passes_do_not_promote(self):
        item = manifest(fresh_ok=11, old_ok=5)
        results = run_exam(
            item,
            runner=lambda case: {"answer": case.question},
            judge=lambda case, payload, document: {
                "accepted": item.answers[case.id],
                "verdict": "correct_answer" if item.answers[case.id] else "wrong_value",
                "failure_layer": "" if item.answers[case.id] else "verification",
                "reason": "",
            },
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
        )

        self.assertFalse(results.promoted)
        with self.assertRaises(AssertionError):
            assert_promotion(results)

    def test_three_old_passes_do_not_promote(self):
        item = manifest(fresh_ok=15, old_ok=3)
        results = run_exam(
            item,
            runner=lambda case: {"answer": case.question},
            judge=lambda case, payload, document: {
                "accepted": item.answers[case.id],
                "verdict": "correct_answer" if item.answers[case.id] else "wrong_value",
                "failure_layer": "",
                "reason": "",
            },
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
        )

        self.assertFalse(results.promoted)

    def test_same_exam_rerun_is_diagnostic_only(self):
        item = manifest(fresh_ok=15, old_ok=5)
        results = run_exam(
            item,
            runner=lambda case: {"answer": case.question},
            judge=lambda case, payload, document: {
                "accepted": item.answers[case.id],
                "verdict": "correct_answer",
                "failure_layer": "",
                "reason": "",
            },
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
            first_attempt=False,
        )

        self.assertFalse(results.promoted)
        self.assertIn("只能用于诊断", results.promotion_reason)

    def test_code_change_rejects_reused_exam_before_running(self):
        item = manifest()
        with self.assertRaises(StaleExamError):
            run_exam(
                item,
                runner=lambda case: self.fail("runner must not execute"),
                judge=lambda case, payload, document: self.fail("judge must not execute"),
                document="业务文档",
                fingerprint_fn=lambda: "code-2",
            )

    def test_old_case_sampling_is_seeded_and_unique(self):
        first = sample_old_cases(seed=7, count=5)
        second = sample_old_cases(seed=7, count=5)

        self.assertEqual(
            [item.id for item in first],
            [item.id for item in second],
        )
        self.assertEqual(len({item.id for item in first}), 5)

    def test_old_pool_includes_previously_frozen_exam_questions(self):
        ids = {item.id for item in load_old_cases()}
        self.assertIn("fresh_topn_route_types", ids)

    def test_code_fingerprint_ignores_docs_and_tracks_runtime_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "backend" / "app").mkdir(parents=True)
            (root / "docs").mkdir()
            code = root / "backend" / "app" / "logic.py"
            code.write_text("value = 1\n", encoding="utf-8")
            (root / "docs" / "readme.md").write_text("v1\n", encoding="utf-8")
            first = code_fingerprint(root)
            (root / "docs" / "readme.md").write_text("v2\n", encoding="utf-8")
            second = code_fingerprint(root)
            code.write_text("value = 2\n", encoding="utf-8")
            third = code_fingerprint(root)

        self.assertEqual(first, second)
        self.assertNotEqual(second, third)

    def test_fresh_generation_excludes_seen_questions(self):
        def generator(document, exclude_ids, count):
            data = cases("fresh", count + 2, "generated")
            return [item for item in data if item.id not in exclude_ids][:count]

        generated = generate_exam_cases(
            "业务文档",
            {"generated_01", "generated_02"},
            15,
            generator=generator,
        )

        self.assertEqual(len(generated), 15)
        self.assertNotIn("generated_01", {item.id for item in generated})
        self.assertNotIn("generated_02", {item.id for item in generated})

    def test_fresh_generation_excludes_duplicate_question_text(self):
        def generator(document, exclude_ids, count):
            data = cases("fresh", count + 2, "generated")
            data[1] = ExamCase(
                id="renamed_duplicate",
                category=data[1].category,
                question=data[0].question,
                focus=data[1].focus,
                source="fresh",
            )
            return data

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            generator=generator,
            exclude_questions={"generated 问题 1"},
        )

        self.assertEqual(len(generated), 15)
        self.assertNotIn(
            "generated 问题 1",
            {item.question for item in generated},
        )

    def test_fresh_generation_retries_with_expanded_exclusions(self):
        calls = []

        def generator(document, exclude_ids, count):
            calls.append(set(exclude_ids))
            if len(calls) == 1:
                return cases("fresh", 2, "retry1")
            return cases("fresh", count, "retry2")

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            generator=generator,
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(generated), 15)

    def test_product_runner_auto_confirms_recommended_capability(self):
        case = ExamCase(
            id="one",
            category="病害",
            question="泛油有多少处",
            source="fresh",
        )
        calls = []

        def fake_handle(message, **kwargs):
            calls.append((message, kwargs))
            if len(calls) == 1:
                return {
                    "reply": "请确认",
                    "await_confirm": True,
                    "choose_token": "ticket",
                    "recommended_id": "candidate-1",
                }
            return {
                "reply": "共 10 处",
                "sections": [{"status": "accepted"}],
            }

        with patch("evals.promotion.product_runner.agent.handle_message", fake_handle):
            payload = run_product_case(case)

        self.assertEqual(payload["answer"], "共 10 处")
        self.assertEqual(calls[1][1]["capability_id"], "candidate-1")


if __name__ == "__main__":
    unittest.main()

