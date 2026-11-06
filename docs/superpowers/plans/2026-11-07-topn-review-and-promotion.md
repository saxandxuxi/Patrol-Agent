# TopN 契约、用户裁决与晋级考试实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 TopN 规划与编译断点，加入用户裁决和有界修复闭环，并建立每轮 15 新题 + 5 旧随机题的 80% 晋级考试。

**Architecture:** QuerySpec 进入编译器前统一通过独立 shape contract。用户裁决状态和证据进入 `query/review.py`，修复只消费结构化 ReviewEvidence。晋级考试拆成出题、抽题、执行、语义判定和报告五个独立模块，不使用关键词通过条件。

**Tech Stack:** Python 3.11、FastAPI、unittest、Vue 3、YAML、现有 LLM 角色模型。

**Spec:** `docs/superpowers/specs/2026-11-07-topn-review-and-promotion-design.md`

## Global Constraints

- 禁止用关键词、正则、中文短语包含关系或题目 ID 决定业务语义。
- 所有人工维护的能力映射只放 `resources/capabilities/`。
- 新增业务逻辑不得继续堆进 `agent.py` 或 `nl2sql.py`。
- 用户拥有答案正确性的最终判断权。
- 修复不得改变原问题、范围、时间、对象和业务编号。
- 每轮晋级考试固定 15 道新题和 5 道随机旧题。
- 新题首次至少 12/15、旧题至少 4/5、总计至少 16/20 才能通过。
- 代码指纹变化后复用旧考试集，考试运行器必须失败。

## Review Focus

- `top_records` 没有输出字段：必须执行前拒绝，不能生成空 SELECT。
- `top_records` 混入聚合指标：必须执行前拒绝，不能把前五条记录变成 SUM。
- 用户点击“回答错误”后重复提交同一裁决：必须拒绝重复消费，不能重复执行修复。
- 修复后范围被扩大或缩小：必须由范围哈希拦截。
- 考试集在代码变化后继续复用：必须被代码指纹校验拒绝。

---

### Task 1: TopN 视图契约

**Files:**
- Create: `backend/app/query/shape_contracts.py`
- Modify: `backend/app/query/contracts.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/spec_mdl.py`
- Test: `backend/tests/query/test_shape_contracts.py`
- Test: `backend/tests/query/test_spec_mdl.py`

**Interfaces:**
- Produces: `normalize_top_view(spec: QuerySpec) -> QuerySpec`
- Produces: `validate_shape_contract(spec: QuerySpec, registry: CapabilityRegistry) -> tuple[ContractIssue, ...]`
- Produces: `ContractIssue(code: str, reason: str, node_id: str)`
- Consumes: `QuerySpec`, `CapabilityRegistry`

- [ ] **Step 1: Write failing contract tests**

Tests assert:

```python
assert normalize_top_view(top_spec(metrics=(), dimensions=(), output_projection=("area",), order_by=(order("area"),))).view == "top_records"
assert normalize_top_view(top_spec(metrics=("count",), dimensions=("route",), output_projection=())).view == "top_groups"
assert issue_codes(top_spec(view="top_records", metrics=(), output_projection=(), order_by=(order("area"),), limit=5)) == ("missing_output_projection",)
assert issue_codes(top_spec(view="top_records", metrics=("total_area",), output_projection=("area",), order_by=(order("area"),), limit=5)) == ("metrics_not_allowed",)
assert issue_codes(top_spec(view="top_records", metrics=(), output_projection=("area",), order_by=(), limit=5)) == ("missing_order_by",)
assert issue_codes(top_spec(view="top_groups", metrics=(), dimensions=("route",), output_projection=(), order_by=(), limit=5)) == ("missing_metrics",)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python -m unittest tests.query.test_shape_contracts -v`
Expected: FAIL because `shape_contracts` does not exist.

- [ ] **Step 3: Implement the shape contract module**

`normalize_top_view` only normalizes raw `top`; an already normalized view is returned unchanged.
`validate_shape_contract` validates `top_records` and `top_groups` against the registry. Unknown
views return `unsupported_view`.

- [ ] **Step 4: Run contract tests and verify GREEN**

Run: `python -m unittest tests.query.test_shape_contracts -v`
Expected: PASS.

- [ ] **Step 5: Add compiler RED tests**

Add tests asserting `top_records` SQL contains:

```python
self.assertIn("disease.area AS 面积", sql)
self.assertIn("ORDER BY disease.area DESC", sql)
self.assertIn("LIMIT 5", sql)
self.assertNotIn("GROUP BY", sql)
self.assertNotIn("SELECT FROM", sql)
```

Add a `top_groups` test asserting COUNT, GROUP BY, ORDER BY and LIMIT.

- [ ] **Step 6: Run compiler tests and verify RED**

Run: `python -m unittest tests.query.test_spec_mdl.SpecMdlTests.test_top_records_compiles_detail_order_and_limit tests.query.test_spec_mdl.SpecMdlTests.test_top_groups_compiles_group_order_and_limit -v`
Expected: FAIL because the compiler does not handle the new views.

- [ ] **Step 7: Implement compiler branches and planner normalization**

In `analysis.py`, normalize each node before audit and compilation. In `spec_mdl.py`, implement
`top_records` as detail projection plus ORDER BY and LIMIT; implement `top_groups` as grouped
metrics plus ORDER BY and LIMIT. Call `validate_shape_contract` before building SQL.

- [ ] **Step 8: Run focused and full query tests**

Run: `python -m unittest tests.query.test_shape_contracts tests.query.test_spec_mdl -v`
Expected: PASS.

Run: `python -m unittest discover -s tests/query -p "test_*.py"`
Expected: all tests PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/app/query/shape_contracts.py backend/app/query/contracts.py backend/app/query/analysis.py backend/app/query/spec_mdl.py backend/tests/query/test_shape_contracts.py backend/tests/query/test_spec_mdl.py
git commit -m "fix: separate top records and top groups contracts"
```

### Task 2: ReviewSuspicion and ReviewEvidence

**Files:**
- Create: `backend/app/query/review.py`
- Modify: `backend/app/query/service.py`
- Modify: `backend/app/query/verifier.py`
- Test: `backend/tests/query/test_review.py`
- Test: `backend/tests/query/test_service.py`

**Interfaces:**
- Produces: `ReviewSuspicion`
- Produces: `ReviewGate`
- Produces: `ReviewEvidence`
- Produces: `issue_review_gate(answer: AnswerPackage, suspicions: Sequence[ReviewSuspicion]) -> ReviewGate | None`
- Produces: `consume_review_gate(token: str, decision: Literal["accept", "reject"]) -> ReviewEvidence | None`
- Consumes: `AnswerPackage`, `VerificationReport`

- [ ] **Step 1: Write failing review evidence tests**

Tests assert:

```python
gate = issue_review_gate(answer, [suspicion("q1", "结果像聚合值，但问题要 Top5 明细")])
assert gate.review_token
assert gate.nodes[0].node_id == "q1"
assert consume_review_gate(gate.review_token, "accept") is not None
assert consume_review_gate(gate.review_token, "reject") is None
evidence = consume_review_gate(other_token, "reject")
assert evidence.original_question == question
assert evidence.scope_hash == answer_spec.immutable_scope_hash
assert evidence.suspect_node_ids == ("q1",)
assert evidence.sections[0].sql
assert evidence.sections[0].result_sample
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.query.test_review -v`
Expected: FAIL because `review.py` does not exist.

- [ ] **Step 3: Implement review data objects and token registry**

Use random opaque tokens, TTL cleanup, one-time consumption and in-memory cache consistent with
the existing confirmation ticket pattern. ReviewEvidence must store only backend-cached truth.

- [ ] **Step 4: Run review tests and verify GREEN**

Run: `python -m unittest tests.query.test_review -v`
Expected: PASS.

- [ ] **Step 5: Add service integration RED tests**

Tests assert an AnswerPackage with semantic suspicion carries `review_gate`, while an accepted
deterministic answer has no gate.

- [ ] **Step 6: Run service tests and verify RED**

Run: `python -m unittest tests.query.test_service`
Expected: the new review-gate tests FAIL.

- [ ] **Step 7: Implement review gate attachment**

Attach gates at the query service boundary after verification and before answer return.
Do not create a gate for ordinary accepted answers with no suspicion.

- [ ] **Step 8: Run focused and full query tests**

Run: `python -m unittest tests.query.test_review tests.query.test_service -v`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/app/query/review.py backend/app/query/service.py backend/app/query/verifier.py backend/tests/query/test_review.py backend/tests/query/test_service.py
git commit -m "feat: add structured user review evidence"
```

### Task 3: Review API and User Decision UI

**Files:**
- Modify: `backend/app/routers/chat.py`
- Modify: `frontend/src/api.js`
- Modify: `frontend/src/components/ChatPanel.vue`
- Test: `backend/tests/query/test_agent_integration.py`

**Interfaces:**
- Produces: `POST /api/query/review`
- Consumes: `{"review_token": "...", "decision": "accept" | "reject"}`
- Produces response:

```json
{
  "accepted": true,
  "decision": "accept"
}
```

- [ ] **Step 1: Write failing API tests**

Tests assert:

- valid accept returns `accepted=true`.
- invalid token returns `review_token_missing`.
- repeated reject returns `review_token_consumed`.
- no user-supplied SQL or QuerySpec is accepted by the request model.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.query.test_agent_integration -v`
Expected: FAIL because the endpoint does not exist.

- [ ] **Step 3: Implement endpoint and frontend review card**

The Vue card displays original question, suspect node question, SQL summary, result summary and
reason. Buttons call the new API. Keep existing good/bad feedback buttons separate.

- [ ] **Step 4: Run backend and frontend checks**

Run: `python -m unittest tests.query.test_agent_integration -v`
Expected: PASS.

Run: `npm run build`
Expected: Vite build succeeds.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/chat.py frontend/src/api.js frontend/src/components/ChatPanel.vue backend/tests/query/test_agent_integration.py
git commit -m "feat: add user review gate"
```

### Task 4: Bounded Review Repair

**Files:**
- Create: `backend/app/query/review_repair.py`
- Modify: `backend/app/query/review.py`
- Modify: `backend/app/query/service.py`
- Modify: `backend/app/routers/chat.py`
- Test: `backend/tests/query/test_review_repair.py`

**Interfaces:**
- Produces: `repair_review_evidence(evidence: ReviewEvidence) -> AnswerPackage`
- Consumes: `ReviewEvidence`
- Produces: `ReviewRepairResult(answer: AnswerPackage, attempts: int, stopped_reason: str)`

- [ ] **Step 1: Write failing repair tests**

Tests assert:

- repair receives the original question and suspect node IDs.
- only suspect nodes can change.
- unchanged scope hash succeeds.
- changed scope hash returns `scope_drift`.
- first repair failure retries once.
- second identical failure stops with `repair_exhausted`.
- accepted repair produces no new review gate.

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.query.test_review_repair -v`
Expected: FAIL because the repair module does not exist.

- [ ] **Step 3: Implement bounded repair**

Use structured planning feedback containing ReviewEvidence. Preserve immutable scope. Reuse the
existing compile/execute/verify pipeline. Never retry the same QuerySpec fingerprint unchanged.

- [ ] **Step 4: Run repair and regression tests**

Run: `python -m unittest tests.query.test_review_repair tests.query.test_query_repair -v`
Expected: PASS.

Run: `python -m unittest discover -s tests/query -p "test_*.py"`
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/query/review_repair.py backend/app/query/review.py backend/app/query/service.py backend/app/routers/chat.py backend/tests/query/test_review_repair.py
git commit -m "feat: repair rejected answers within node scope"
```

### Task 5: Promotion Exam Harness

**Files:**
- Create: `backend/evals/promotion/__init__.py`
- Create: `backend/evals/promotion/models.py`
- Create: `backend/evals/promotion/case_factory.py`
- Create: `backend/evals/promotion/runner.py`
- Create: `backend/evals/promotion/__main__.py`
- Create: `backend/evals/promotion/old_pool.yaml`
- Create: `backend/tests/query/test_promotion_eval.py`

**Interfaces:**
- Produces: `ExamCase`
- Produces: `ExamManifest`
- Produces: `ExamReport`
- Produces: `generate_exam_cases(document: str, exclude_ids: set[str], count: int = 15) -> list[ExamCase]`
- Produces: `sample_old_cases(seed: int, count: int = 5) -> list[ExamCase]`
- Produces: `code_fingerprint(root: Path) -> str`
- Produces: `run_exam(manifest: ExamManifest) -> ExamReport`
- Produces: `assert_promotion(report: ExamReport) -> None`

- [ ] **Step 1: Write failing harness tests**

Tests use fake runners and fake judges and assert:

```python
assert report.passed_new == 12 and report.promoted
assert report.passed_new == 11 and not report.promoted
assert report.passed_old == 3 and not report.promoted
assert reused_cases_after_code_change_fails(previous, current)
assert sample_old_cases(seed=7, count=5) == sample_old_cases(seed=7, count=5)
assert len({case.id for case in sample_old_cases(seed=7)}) == 5
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.query.test_promotion_eval -v`
Expected: FAIL because the promotion package does not exist.

- [ ] **Step 3: Implement manifest, sampling, fingerprint and report**

The manifest records generated time, document hash, seed, case IDs, code fingerprint and whether
the set is fresh. Reused sets whose code fingerprint differs from their creation fingerprint fail
before execution.

- [ ] **Step 4: Implement fresh case generation boundary**

The generator accepts the business document and excluded IDs and returns 15 structured cases.
It must not map case IDs to SQL, fields or answers. Network/model calls are injected for tests.

- [ ] **Step 5: Implement CLI**

Commands:

```bash
python -m evals.promotion generate --output evals/promotion/runs/2026-11-07.yaml
python -m evals.promotion run --cases evals/promotion/runs/2026-11-07.yaml
```

- [ ] **Step 6: Run tests and verify GREEN**

Run: `python -m unittest tests.query.test_promotion_eval -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/evals/promotion backend/tests/query/test_promotion_eval.py
git commit -m "feat: add promotion exam harness"
```

### Task 6: Code Principles and First Exam

**Files:**
- Create: `docs/项目原则-AI晋级考试与用户裁决.md`
- Modify: `docs/项目原则-禁止正则与关键词硬匹配.md`
- Create: `docs/evals/2026-11-07-promotion-round-1.md`

**Interfaces:**
- Consumes: passed promotion harness and implemented review/TopN flows.
- Produces: mandatory coding policy and first exam record.

- [ ] **Step 1: Write the principle document**

Include all rules from the spec's section 8 and require every code-changing task to read the
principle before modifying query semantics.

- [ ] **Step 2: Link it from the existing mandatory principle**

Add a short mandatory section to `docs/项目原则-禁止正则与关键词硬匹配.md` linking the new policy.

- [ ] **Step 3: Generate and run the first 15-question exam**

Freeze the 15 questions before running the system. Record the random seed and 5 sampled old cases.
Run once without modifying code between cases.

- [ ] **Step 4: Record the report**

The report includes per-case verdicts, failure layers, fresh-set first-pass rate, old-set pass rate,
promotion decision and code fingerprint.

- [ ] **Step 5: If below 80%, diagnose common root causes only**

Do not modify individual case logic. Classify failures by contract, value, compiler, execution,
verification and presentation. Record the diagnosis in the report.

- [ ] **Step 6: If code changes, generate a new 15-question set**

The old set is marked burned. Reusing it as promotion evidence is forbidden.

- [ ] **Step 7: Commit**

```bash
git add docs/项目原则-AI晋级考试与用户裁决.md docs/项目原则-禁止正则与关键词硬匹配.md docs/evals/2026-11-07-promotion-round-1.md
git commit -m "docs: codify promotion exam policy and first round"
```
