# QuerySpec Controlled Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded, evidence-driven QuerySpec self-repair without allowing SQL generation or user-scope drift.

**Architecture:** The Planner emits a draft QuerySpec. A code-owned validator creates a structured repair ticket. The same semantic model returns only JSON patches. A deterministic controller applies and validates those patches, then reruns the existing parser/compiler. AI confirmation control, subject rerouting, recent patrol dependencies and result approval are separate vertical slices using the same evidence principle.

**Tech Stack:** Python 3.11, FastAPI, unittest/pytest, Vue 3, existing capability registry and role-model LLM adapter.

**Spec:** `docs/superpowers/specs/2026-11-10-query-spec-controlled-repair-design.md`

## Global Constraints

- Preserve the user's original question, route, disease type, time range, device, task and business identifiers.
- The LLM never authors or edits SQL in this repair path.
- Maximum two repair rounds; duplicate evidence or patch signatures stop execution.
- `QuerySpecUnsupported` and `QuerySpecAmbiguity` are not silently repaired.
- Business semantics come from the semantic model; regex/keyword output is only `trusted=false` evidence.
- New business mappings belong in `resources/capabilities/`.
- Do not add one-off rules for a question ID or exact phrase.
- Preserve all existing uncommitted user changes.
- Do not clean, reset, rebase or push.

## Review Focus

- A valid result is repaired into a different route, disease or time range.
- A planner repeats the same invalid patch until the budget is exhausted.
- “用 AI 查” consumes a template token and then shows the same card again.
- A failing child section hides “存入示例库” even though executable SQL exists.
- A recent-window query ranks diseases by patrol creation time instead of disease count.

---

### Task 1: AI Control Action

**Files:**
- Modify: `backend/app/services/agent.py:4372-4476`
- Modify: `backend/app/query/service.py:2105-2125`
- Test: `backend/tests/query/test_agent_integration.py`

**Interfaces:**
- Consumes: `start_single_turn(question: str, kind: str = "mysql", *, providers=...) -> TurnStart`
- Produces: `discard_pending_confirmation(ticket_token: str) -> None`

- [ ] **Step 1: Write the failing test**

Test `capability_id="ai"` with a valid confirmation token calls `start_single_turn` with
`plan.question`, `providers=()`, never looks up an `"ai"` candidate, and returns no
`await_confirm`.

- [ ] **Step 2: Run the test**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_agent_integration.py -q`
Expected: FAIL because the current code reports `confirmation_candidate_missing`.

- [ ] **Step 3: Implement the control branch**

Handle `capability_id in {"ai", "wren"}` before candidate lookup. Discard the pending ticket,
restart from the ticket's original question with an empty provider tuple, and return the normal
compiler payload.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_agent_integration.py -q`
Expected: PASS.

### Task 2: Repair Ticket and Patch Guard

**Files:**
- Create: `backend/app/query/planning_repair/__init__.py`
- Create: `backend/app/query/planning_repair/models.py`
- Create: `backend/app/query/planning_repair/json_patch.py`
- Create: `prompts/query_spec_repair.md`
- Test: `backend/tests/query/test_planning_repair.py`

**Interfaces:**
- Produces: `RepairIssue`, `RepairTicket`, `apply_repair_patches(ticket, patches) -> dict[str, Any]`
- Produces: `scope_signature(draft: dict[str, Any]) -> str`

- [ ] **Step 1: Write the failing tests**

Tests cover a permitted `/views/0/subject` set, rejection of an unauthorized filter change,
stable scope signatures, duplicate patch fingerprints, and JSON Pointer escaping.

- [ ] **Step 2: Run the tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_planning_repair.py -q`
Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement the minimal patch protocol**

Use a deep-copied draft, RFC 6901 JSON Pointers, and only `set`, `add`, `remove`.
Reject paths not admitted by `RepairTicket.allowed_paths`. Compare normalized filter scope before
and after application unless `filters` is explicitly allowed by the ticket.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_planning_repair.py -q`
Expected: PASS.

### Task 3: Planner Repair Loop

**Files:**
- Modify: `backend/app/query/analysis.py:1302-1660,2484-2605`
- Test: `backend/tests/query/test_planner_self_repair.py`

**Interfaces:**
- Consumes: Task 2 `RepairTicket` and `apply_repair_patches`
- Produces: `_analyze_once(..., raw_draft: dict | None = None, draft_capture: dict | None = None) -> QueryPlan`

- [ ] **Step 1: Write the failing tests**

Mock the first planner response with an invalid dimension and the repair response with one local
patch. Assert `analyze_single_turn` executes the repaired plan. Add a second case where the patch
changes the original route filter; assert `QuerySpecError` and no second execution.

- [ ] **Step 2: Run the tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_planner_self_repair.py -q`
Expected: FAIL because the current retry discards the draft and has no patch protocol.

- [ ] **Step 3: Integrate bounded repair**

Capture the raw model JSON, create a `RepairTicket` for ordinary `QuerySpecError`, request patches
once per round, apply the guard, and feed the repaired draft back into `_analyze_once`. Keep
unsupported and ambiguity exceptions terminal; stop on repeated evidence/signature.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_planner_self_repair.py -q`
Expected: PASS.

### Task 4: Subject Conflict Reroute

**Files:**
- Modify: `backend/app/query/capability_registry/subject_resolver.py`
- Modify: `backend/app/query/analysis.py:339-399,1630-1645`
- Modify: `backend/app/query/service.py:1791-1840`
- Test: `backend/tests/query/test_subject_capability_view.py`

**Interfaces:**
- Produces: `SubjectResolution` evidence for Planner-final conflicting subjects
- Produces: `resolve_subject(..., conflicting_subject: str = "", previous_resolution: SubjectResolution | None = None) -> SubjectResolution`

- [ ] **Step 1: Write the failing test**

First routing returns `section`, Planner returns `disease`, second routing receives the Planner
subject as evidence and returns `disease`; the repaired plan keeps the original route and disease
filters. A repeated conflict returns a clear failure.

- [ ] **Step 2: Run the test**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_subject_capability_view.py -q`
Expected: FAIL because the initial subject is reused unchanged.

- [ ] **Step 3: Implement one evidence-backed reroute**

Catch the structured subject conflict in `start_single_turn`, call `resolve_subject` once with the
Planner subject, then call `analyze_single_turn` once with the new subject. Do not retry forever.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_subject_capability_view.py -q`
Expected: PASS.

### Task 5: Recent Patrol Evidence Contract

**Files:**
- Modify: `resources/capabilities/assist_rules.yaml`
- Modify: `backend/app/query/patrol_windows.py`
- Modify: `backend/app/query/analysis.py:1570-1630`
- Test: `backend/tests/query/test_patrol_windows.py`
- Test: `backend/tests/query/test_semantic_aids.py`

**Interfaces:**
- Produces: `missing_recent_window_dependency` repair issue when recent evidence exists without a patrol-to-disease binding.

- [ ] **Step 1: Write the failing tests**

A disease `top_groups` plan says “最近三次巡查”, omits the patrol batch node, and attempts to sort
by `create_time`. Assert analysis returns repair evidence before execution. A repaired plan contains
the batch node and sorts the disease distribution by `count`.

- [ ] **Step 2: Run the tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_patrol_windows.py backend/tests/query/test_semantic_aids.py -q`
Expected: FAIL because the current recent-window completion only runs when the plan already opted in.

- [ ] **Step 3: Implement the shared contract**

Add a `trusted=false` recent-window candidate rule, detect the missing dependency, and route it
through the Task 3 repair loop. Keep the existing batch-axis completion logic.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_patrol_windows.py backend/tests/query/test_semantic_aids.py -q`
Expected: PASS.

### Task 6: Approval Visibility and Composite Save Contract

**Files:**
- Modify: `backend/app/services/agent.py:4235-4330`
- Test: `backend/tests/query/test_agent_integration.py`
- Test: `backend/tests/query/test_result_approval.py`

**Interfaces:**
- Produces: successful QuerySpec payloads with `reviewable=true` when at least one accepted section has SQL.
- Consumes: existing `save_successful_pair` and `save_composite_pair`.

- [ ] **Step 1: Write the failing tests**

Single-section success returns `reviewable=true` and approval saves the original question plus SQL.
Multi-section success returns `reviewable=true` and approval saves the parent question plus every
child question/SQL through `save_composite_pair`.

- [ ] **Step 2: Run the tests**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_agent_integration.py backend/tests/query/test_result_approval.py -q`
Expected: FAIL because the compiler payload omits `reviewable`.

- [ ] **Step 3: Implement the result contract**

Set `reviewable` from executable accepted SQL sections. Do not duplicate the existing composite
storage implementation.

- [ ] **Step 4: Verify**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_agent_integration.py backend/tests/query/test_result_approval.py -q`
Expected: PASS.

### Task 7: Full Verification and Fresh Promotion Exam

**Files:**
- No business files expected.
- Artifact: fresh promotion run under `backend/evals/promotion/runs/`.

**Interfaces:**
- Consumes: all previous tasks.

- [ ] **Step 1: Run the query suite**

Run: `backend\.venv\Scripts\python.exe -m pytest backend/tests/query -q`
Expected: all tests pass.

- [ ] **Step 2: Run compile and frontend checks**

Run Python compilation for changed modules and the existing frontend build command.
Expected: no syntax/build errors.

- [ ] **Step 3: Run repository hygiene checks**

Run: `git diff --check`
Expected: no whitespace errors.

- [ ] **Step 4: Generate and run a new exam**

Generate 15 unseen business questions from `docs/archive/巡查业务问题.md` and randomly select 5
old questions. Do not reuse `unit-14`. Record seed, code fingerprint and per-question layers.
Expected promotion threshold: new `>=12/15`, old `>=4/5`, total `>=18/20`.
