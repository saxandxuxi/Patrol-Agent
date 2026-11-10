import json
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from evals.promotion.case_factory import (
    _default_generator,
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
from evals.run_holdout_eval import _default_judge


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
    @patch("evals.run_holdout_eval.chat_json")
    def test_default_judge_receives_authoritative_field_capabilities(
        self,
        chat_json,
    ):
        chat_json.return_value = {
            "accepted": True,
            "verdict": "correct_answer",
        }

        _default_judge(
            {
                "id": "severity",
                "question": "按严重程度统计病害",
                "focus": "核对损坏程度字段",
                "source": "fresh",
            },
            {
                "answer": "按损坏程度统计完成",
                "sections": [
                    {
                        "subject": "disease",
                        "spec": {
                            "dimensions": ["damage_type"],
                            "metrics": ["count", "total_area"],
                            "output_projection": ["damage_type"],
                        },
                    }
                ],
            },
            "",
        )

        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn('"damage_type"', prompt)
        self.assertIn("损坏程度", prompt)
        self.assertIn("轻度", prompt)
        self.assertIn('"history_damage_interval"', prompt)
        self.assertIn('"status": "unavailable"', prompt)

    @patch("evals.promotion.case_factory.chat_json")
    def test_default_generator_receives_actual_capability_gaps(self, chat_json):
        chat_json.return_value = {"cases": []}

        _default_generator(
            "业务文档",
            set(),
            15,
            set(),
        )

        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn("management_zone", prompt)
        self.assertIn("lane_level_distribution", prompt)
        self.assertIn("history_damage_interval", prompt)
        self.assertIn("最多 1 道", prompt)

    @patch("evals.promotion.case_factory.chat_json")
    def test_default_generator_receives_field_level_capability_facts(
        self,
        chat_json,
    ):
        chat_json.return_value = {"cases": []}

        _default_generator(
            "业务文档",
            set(),
            1,
            set(),
            batch_focus=("当前数据能力不足",),
        )

        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn("当前字段级别能力事实", prompt)
        self.assertIn('"disease.damage_type"', prompt)
        self.assertIn('"label": "损坏程度"', prompt)
        self.assertIn('"status": "available"', prompt)
        self.assertIn('"disease.history_damage_interval"', prompt)
        self.assertIn('"status": "unavailable"', prompt)

    @patch("evals.promotion.case_factory.chat_json")
    def test_default_generator_uses_nonzero_temperature(self, chat_json):
        chat_json.return_value = {"cases": []}

        _default_generator(
            "业务文档",
            set(),
            1,
            set(),
            batch_focus=("具体记录明细",),
        )

        self.assertGreater(
            chat_json.call_args.kwargs["temperature"],
            0,
        )

    @patch("evals.promotion.case_factory.chat_json")
    def test_default_generator_receives_structured_retry_evidence(
        self,
        chat_json,
    ):
        chat_json.return_value = {"cases": []}

        _default_generator(
            "业务文档",
            set(),
            1,
            set(),
            batch_focus=("具体记录明细",),
            retry_evidence={
                "具体记录明细": [
                    {
                        "question": "已经出现过的明细题",
                        "reason": "duplicate_question",
                    }
                ]
            },
        )

        prompt = "\n".join(
            str(item.get("content") or "")
            for item in chat_json.call_args.args[0]
        )
        self.assertIn("已经出现过的明细题", prompt)
        self.assertIn("duplicate_question", prompt)

    @patch("evals.promotion.case_factory._default_generator")
    def test_fresh_generation_assigns_distinct_focus_slots(self, generator):
        focus_batches = []

        def generate(
            document,
            exclude_ids,
            count,
            exclude_questions,
            batch_focus=(),
            retry_evidence=None,
        ):
            focus_batches.append(tuple(batch_focus))
            return [
                ExamCase(
                    id=f"focus{len(focus_batches)}_{index:02d}",
                    category=slot,
                    question=f"focus{len(focus_batches)} 问题 {index}",
                    focus=f"{slot}：检查是否答所问",
                    source="fresh",
                    coverage_slot=slot,
                )
                for index, slot in enumerate(batch_focus, start=1)
            ]

        generator.side_effect = generate

        generated = generate_exam_cases("业务文档", set(), 15)

        self.assertEqual(len(generated), 15)
        flattened = [item for batch in focus_batches for item in batch]
        self.assertEqual(len(flattened), 15)
        self.assertEqual(len(set(flattened)), 15)

    @patch("evals.promotion.case_factory._default_generator")
    def test_fresh_generation_honors_business_unit_focus_slots(self, generator):
        slot_names = tuple(f"位置专项槽 {index}" for index in range(1, 16))
        focus_batches = []

        def generate(
            document,
            exclude_ids,
            count,
            exclude_questions,
            batch_focus=(),
            retry_evidence=None,
        ):
            focus_batches.append(tuple(batch_focus))
            return [
                ExamCase(
                    id=f"location_{len(focus_batches)}_{index:02d}",
                    category="位置与分布",
                    question=f"位置问题 {len(focus_batches)}-{index}",
                    focus=f"{slot}：检查位置数据链路",
                    source="fresh",
                    coverage_slot=slot,
                )
                for index, slot in enumerate(batch_focus, start=1)
            ]

        generator.side_effect = generate

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            focus_slots=slot_names,
        )

        self.assertEqual(len(generated), 15)
        self.assertEqual(
            tuple(item for batch in focus_batches for item in batch),
            slot_names,
        )

    def test_fresh_generation_requires_enough_business_unit_focus_slots(self):
        with self.assertRaisesRegex(ValueError, "focus_slots"):
            generate_exam_cases(
                "业务文档",
                set(),
                15,
                focus_slots=("只有一个专项槽",),
            )

    @patch("evals.promotion.case_factory.chat_json")
    def test_default_generation_rejects_case_with_wrong_coverage_slot(
        self,
        chat_json,
    ):
        calls = []

        def generate(messages, **_kwargs):
            calls.append(messages)
            prompt = "\n".join(
                str(item.get("content") or "") for item in messages
            )
            marker = "本次小批必须覆盖的结构化职责槽：\n"
            batch_focus = json.loads(
                prompt.split(marker, 1)[1].split("\n\n", 1)[0]
            )
            cases = [
                {
                    "id": f"slot_{len(calls)}_{index}",
                    "category": slot,
                    "question": f"第 {len(calls)} 批第 {index} 题：{slot}",
                    "focus": f"{slot}：检查对应能力",
                    "coverage_slot": slot,
                }
                for index, slot in enumerate(batch_focus, start=1)
            ]
            if len(calls) == 1:
                cases[-1]["coverage_slot"] = batch_focus[0]
                cases[-1]["question"] = "错误的覆盖槽题目"
            return {"cases": cases}

        chat_json.side_effect = generate

        generated = generate_exam_cases("业务文档", set(), 15)

        self.assertEqual(len(generated), 15)
        self.assertNotIn(
            "错误的覆盖槽题目",
            {item.question for item in generated},
        )
        self.assertEqual(
            {item.coverage_slot for item in generated},
            {
                item.coverage_slot
                for item in generated
                if item.coverage_slot
            },
        )
        self.assertEqual(
            len({item.coverage_slot for item in generated}),
            15,
        )
        self.assertIn(
            "当前数据能力不足",
            {item.coverage_slot for item in generated},
        )

    def test_promotes_at_exact_thresholds(self):
        item = manifest(fresh_ok=14, old_ok=4)
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

        self.assertEqual(results.passed_fresh, 14)
        self.assertEqual(results.passed_old, 4)
        self.assertEqual(results.passed_total, 18)
        self.assertTrue(results.promoted)
        assert_promotion(results)

    def test_sixteen_total_does_not_promote(self):
        item = manifest(fresh_ok=12, old_ok=4)
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

        self.assertEqual(results.passed_total, 16)
        self.assertFalse(results.promoted)

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

    def test_infrastructure_failure_stops_without_semantic_judge(self):
        item = manifest()

        results = run_exam(
            item,
            runner=lambda case: {
                "answer": "模型额度不足",
                "infrastructure": {
                    "kind": "quota_exhausted",
                    "code": "llm_quota_exhausted",
                    "reason": "模型账户余额不足",
                },
            },
            judge=lambda case, payload, document: self.fail(
                "infrastructure failures must not call the semantic judge"
            ),
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
        )

        self.assertEqual(len(results.results), 20)
        first = results.results[0]
        self.assertEqual(first.verdict, "infrastructure_blocked")
        self.assertEqual(first.failure_layer, "infrastructure")
        self.assertEqual(
            {item.verdict for item in results.results[1:]},
            {"not_run"},
        )
        self.assertFalse(results.promoted)

    def test_infrastructure_failure_during_judge_stops_exam(self):
        item = manifest()

        class JudgeQuotaError(RuntimeError):
            category = "quota_exhausted"

        def judge(case, payload, document):
            raise JudgeQuotaError("judge model quota exhausted")

        results = run_exam(
            item,
            runner=lambda case: {"answer": case.question},
            judge=judge,
            document="业务文档",
            fingerprint_fn=lambda: "code-1",
        )

        self.assertEqual(len(results.results), 20)
        first = results.results[0]
        self.assertEqual(first.verdict, "infrastructure_blocked")
        self.assertEqual(first.failure_layer, "infrastructure")
        self.assertIn("quota_exhausted", first.reason)
        self.assertEqual(
            {item.verdict for item in results.results[1:]},
            {"not_run"},
        )
        self.assertFalse(results.promoted)

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
        calls = []

        def generator(document, exclude_ids, count):
            calls.append(count)
            data = cases("fresh", count + 2, f"generated{len(calls)}")
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
        calls = []

        def generator(document, exclude_ids, count):
            calls.append(count)
            data = cases("fresh", count + 2, f"generated{len(calls)}")
            data[1] = ExamCase(
                id=f"renamed_duplicate_{len(calls)}",
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
            return cases("fresh", count, f"retry{len(calls)}")

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            generator=generator,
        )

        self.assertEqual(len(calls), 4)
        self.assertEqual(len(generated), 15)

    def test_fresh_generation_limits_the_final_batch_to_the_remaining_count(self):
        calls = []

        def generator(document, exclude_ids, count):
            calls.append(count)
            amount = 2 if len(calls) == 1 else min(count, 5)
            return cases("fresh", amount, f"batch{len(calls)}")

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            generator=generator,
        )

        self.assertEqual(calls, [5, 5, 5, 3])
        self.assertEqual(len(generated), 15)

    def test_fresh_generation_uses_small_reliable_batches(self):
        calls = []

        def generator(document, exclude_ids, count):
            calls.append(count)
            amount = min(count, 5)
            return cases("fresh", amount, f"batch{len(calls)}")

        generated = generate_exam_cases(
            "业务文档",
            set(),
            15,
            generator=generator,
        )

        self.assertEqual(calls, [5, 5, 5])
        self.assertEqual(len(generated), 15)

    @patch("evals.promotion.case_factory._default_generator")
    def test_generation_failure_names_the_missing_coverage_slot(
        self,
        generator,
    ):
        attempts = 0

        def generate(
            document,
            exclude_ids,
            count,
            exclude_questions,
            batch_focus=(),
            retry_evidence=None,
        ):
            nonlocal attempts
            attempts += 1
            return [
                ExamCase(
                    id=f"partial_{attempts}_{index}",
                    category=slot,
                    question=f"partial {attempts} 问题 {index}",
                    source="fresh",
                    coverage_slot=slot,
                )
                for index, slot in enumerate(
                    item
                    for item in batch_focus
                    if item != "具体记录明细"
                )
            ]

        generator.side_effect = generate

        with self.assertRaisesRegex(ValueError, "具体记录明细"):
            generate_exam_cases("业务文档", set(), 15)

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

    def test_product_runner_marks_provider_failure_as_infrastructure(self):
        case = ExamCase(
            id="one",
            category="病害",
            question="泛油有多少处",
            source="fresh",
        )

        with patch(
            "evals.promotion.product_runner.agent.handle_message",
            return_value={
                "reply": "模型额度不足",
                "error": "模型额度不足",
                "sections": [
                    {
                        "status": "error",
                        "failure_code": "llm_quota_exhausted",
                        "answer": "模型额度不足",
                    }
                ],
            },
        ):
            payload = run_product_case(case)

        self.assertEqual(
            payload["infrastructure"]["kind"],
            "quota_exhausted",
        )
        self.assertIn("模型额度不足", payload["infrastructure"]["reason"])


if __name__ == "__main__":
    unittest.main()

