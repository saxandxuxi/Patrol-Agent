# Single-Turn Query Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a production-safe single-turn inspection-query core where a QuerySpec is the only source of truth, every template match requires user confirmation, execution facts feed a three-layer verifier, and bounded repair cannot change the user's scope.

**Architecture:** Add a new `app/query` package that converts a single-turn question into typed contracts, validates field and value provenance, resolves only fully compatible templates, requires an explicit confirmation decision, compiles and executes SQL, verifies deterministic facts before semantic and presentation checks, and exposes a feature-flagged facade to the existing chat entry point. Existing Wren, SQL linting, database access, template data, and result storage remain the underlying implementations.

**Tech Stack:** Python 3.11, standard-library `unittest`, existing FastAPI/Agent services, Wren/SQL execution, MySQL first, PostgreSQL-compatible boundaries.

**Spec:** `docs/superpowers/specs/2026-11-05-single-turn-query-compiler-design.md`

## Global Constraints

- Scope is one user query with no previous-turn context. Confirmation is authorization for the same query, not a new semantic turn.
- One user query may produce multiple internal nodes, but all nodes share one immutable scope hash.
- LLM output never executes directly. No SQL is compiled until QuerySpec validation passes.
- `FieldReachSet`, `PhysicalSchemaView`, and `OutputProjection` remain separate types and cannot substitute for one another.
- Every SQL filter and output field must carry provenance. Model-inferred enum values require dictionary resolution.
- Storage values and display labels are separate fields and require round-trip tests.
- A SQL success is not a task success. Execution facts come only from `ExecutionObservation`.
- All template families use the same `TemplateContract` and `QueryCandidate` eligibility rules.
- Every template hit requires confirmation, including a single complete candidate. There is no auto-execute switch.
- Confirmation must revalidate the QuerySpec fingerprint, template version, parameter provenance, and schema snapshot.
- Detail queries may exceed 500 displayed rows; exact count, fetched rows, truncation, and verification scope stay separate.
- Presentation errors never trigger a database re-query.
- Business failures use typed failure results; exceptions are reserved for infrastructure and programming errors.
- A solvable case that explicitly fails is a failed regression test.
- Do not modify long-dialogue, knowledge, image, metadata, write, or production deployment flows.
- Do not add dependencies or change the database schema.

## Review Focus

- A preset or built-in template supports only one requested metric while another requested metric is missing; the expected behavior is rejection before the confirmation card.
- A user changes a template parameter to a value absent from the dictionary; the expected behavior is confirmation failure and a refreshed card, never SQL execution.
- A controller proposes a template during repair; the expected behavior is a new confirmation card, not automatic execution.
- A detail query matches more than 500 rows; the expected behavior is exact count plus explicit truncation, never claiming the sample is complete.
- `post_calc` produces no usable numeric result; the expected behavior is `accepted=false` with typed failure, never a non-empty fallback treated as success.

---

## File Structure

**Create**

- `backend/app/query/__init__.py`: public facade exports only.
- `backend/app/query/contracts.py`: IntentContract, ValueBinding, QuerySpec, plan nodes, failures, observations, verification results.
- `backend/app/query/fields.py`: FieldReachSet, PhysicalSchemaView, OutputProjection.
- `backend/app/query/validator.py`: deterministic QuerySpec validation.
- `backend/app/query/values.py`: filter-value and provenance resolution adapters.
- `backend/app/query/templates.py`: TemplateContract, QueryCandidate, CandidateSet, TemplateProvider, TemplateResolver.
- `backend/app/query/template_sources.py`: adapters for built-in, preset, auto, and verified-example templates.
- `backend/app/query/analysis.py`: single-turn QuestionAnalysis-to-QueryPlan adapter.
- `backend/app/query/planner.py`: deterministic QuerySpec view splitting and query-node construction.
- `backend/app/query/confirmations.py`: candidate confirmation tokens and decisions.
- `backend/app/query/compiler.py`: selected-template and Wren compilation adapters.
- `backend/app/query/executor.py`: SQL execution and ExecutionObservation creation.
- `backend/app/query/verifier.py`: deterministic, semantic, and presentation verification.
- `backend/app/query/repair.py`: bounded RepairPatch application.
- `backend/app/query/service.py`: feature-flagged facade used by the existing agent.
- `prompts/query_spec.md`: JSON-only QuerySpec proposal prompt.
- `backend/tests/query/__init__.py`: test package marker.
- `backend/tests/query/factories.py`: shared QuerySpec fixtures for focused tests.
- `backend/tests/query/*.py`: focused unit and integration tests.
- `backend/evals/__init__.py`: evaluator package marker.
- `backend/evals/single_turn/cases.yaml`: versioned real-answer and invariant cases.
- `backend/evals/run_single_turn_eval.py`: evaluator that distinguishes correct answers, honest unsupported results, partial results, and failures.

**Modify**

- `backend/app/services/chat_settings.py`: add a disabled-by-default single-turn feature flag.
- `backend/app/services/agent.py`: route enabled single-turn data questions into the new service and handle confirmation payloads.
- `backend/app/services/capabilities.py`: keep the existing card transport but allow confirmation payloads to carry the new compiler context.
- `backend/app/services/agent.py:_post_calc_payload`: stop treating a fallback sentence as success.
- `backend/app/services/tools/big/post_calc.py`: preserve typed failure conversion.

**Do not edit in this plan**

- Long-dialogue contextualization algorithms.
- Knowledge, image, metadata, prompt-template administration, or unrelated frontend layout.
- Existing untracked diagnostics, logs, screenshots, or scratch scripts.

### Task 1: Query Contract Primitives

**Files:**
- Create: `backend/app/query/__init__.py`
- Create: `backend/app/query/contracts.py`
- Create: `backend/tests/query/__init__.py`
- Create: `backend/tests/query/factories.py`
- Test: `backend/tests/query/test_contracts.py`

**Interfaces:**
- Consumes: standard-library `dataclasses`, `hashlib`, `json`, and `typing`.
- Produces: `ValueBinding`, `IntentContract`, `QuerySpec`, `PlanNode`, `QueryPlan`, `FailureEvidence`, `ExecutionObservation`, `VerificationIssue`, `VerificationReport`, `AnswerPackage(text, sections=())`, `scope_hash(spec)`, and `plan_scope_hash(plan)`.

- [ ] **Step 1: Add shared fixture factories and failing contract tests**

Create `count_spec(**overrides)` and `detail_spec(**overrides)` in `tests/query/factories.py`. Both return valid `QuerySpec` objects; each accepts keyword overrides, and `detail_spec` uses `view="detail"` with an `id` output field.

```python
import unittest

from app.query.contracts import QuerySpec, ValueBinding, plan_scope_hash, scope_hash
from tests.query.factories import count_plan


class QueryContractTests(unittest.TestCase):
    def test_storage_and_display_values_are_not_interchangeable(self):
        value = ValueBinding(
            field="up_or_down",
            op="=",
            storage_value="2",
            display_value="下行",
            source="user_explicit",
            resolver="enum:up_or_down",
            raw_text="下行",
        )
        self.assertEqual(value.storage_value, "2")
        self.assertEqual(value.display_value, "下行")

    def test_scope_hash_changes_with_user_scope(self):
        first = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        second = QuerySpec(**{**first.__dict__, "filters": (
            ValueBinding(
                field="problem_type_name",
                op="=",
                storage_value="泛油",
                display_value="泛油",
                source="dictionary",
                resolver="damage_type",
                raw_text="泛油",
            ),
        )})
        self.assertNotEqual(scope_hash(first), scope_hash(second))

    def test_plan_hash_covers_every_node(self):
        plan = count_plan()
        changed = QuerySpec(**{**plan.nodes[0].spec.__dict__, "limit": 10})
        changed_plan = plan.__class__(
            question=plan.question,
            nodes=(plan.nodes[0].__class__(id=plan.nodes[0].id, spec=changed),),
        )
        self.assertNotEqual(plan_scope_hash(plan), plan_scope_hash(changed_plan))
```

- [ ] **Step 2: Run tests to confirm the module does not exist**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_contracts -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.query'`.

- [ ] **Step 3: Implement immutable dataclasses and canonical hashing**

Use frozen dataclasses. `scope_hash(spec)` serializes subject, view, metrics, dimensions, filters, order, limit, and required assertions with sorted JSON keys and returns a SHA-256 hex digest. `plan_scope_hash(plan)` hashes the ordered node IDs and each node's scope hash. Do not include display-only labels in canonical hashes.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_contracts -v
```

Expected: `Ran 3 tests ... OK`.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/__init__.py backend/app/query/contracts.py backend/tests/query/__init__.py backend/tests/query/factories.py backend/tests/query/test_contracts.py
git commit -m "feat: add single-turn query contracts"
```

### Task 2: Separate Field Views and Validate QuerySpec

**Files:**
- Create: `backend/app/query/fields.py`
- Create: `backend/app/query/validator.py`
- Test: `backend/tests/query/test_fields_validator.py`

**Interfaces:**
- Consumes: `QuerySpec`, `ValueBinding`, `VerificationIssue`.
- Produces: `FieldRef(table: str, column: str, display_label: str)`, `FieldReachHit`, `FieldReachSet`, `PhysicalSchemaView`, `OutputProjection`, and `validate_query_spec(spec, schema, projection) -> tuple[VerificationIssue, ...]`.

- [ ] **Step 1: Write failing tests for view separation and source validation**

```python
import unittest

from app.query.contracts import QuerySpec, ValueBinding
from app.query.fields import (
    FieldReachHit,
    FieldReachSet,
    FieldRef,
    OutputProjection,
    PhysicalSchemaView,
)
from app.query.validator import validate_query_spec


class FieldValidatorTests(unittest.TestCase):
    def test_reach_hit_does_not_prove_physical_column(self):
        reach = FieldReachSet(
            hits=(FieldReachHit("disease", "missing_col", "缺失列", "用户原话", 3.0),)
        )
        schema = PhysicalSchemaView(columns=frozenset({("disease", "id")}))
        self.assertFalse(reach.contains_physical("disease", "missing_col"))
        self.assertFalse(schema.has_column("disease", "missing_col"))

    def test_unknown_physical_field_is_rejected(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        schema = PhysicalSchemaView(columns=frozenset({("disease", "id")}))
        projection = OutputProjection(fields=(FieldRef("disease", "missing_col", "缺失列"),))
        issues = validate_query_spec(spec, schema, projection)
        self.assertIn("unknown_physical_field", {item.code for item in issues})

    def test_model_inferred_value_without_resolver_is_rejected(self):
        spec = QuerySpec(
            id="q1",
            kind="mysql",
            subject="disease",
            view="count",
            metrics=("count",),
            dimensions=(),
            filters=(ValueBinding(
                field="problem_type_name",
                op="=",
                storage_value="其他",
                display_value="其他",
                source="model_inference",
                resolver="",
                raw_text="其他",
            ),),
            output_projection=(),
            order_by=(),
            limit=None,
            required_assertions=(),
        )
        issues = validate_query_spec(
            spec,
            PhysicalSchemaView(columns=frozenset({("disease", "problem_base_id")})),
            OutputProjection(fields=()),
        )
        self.assertIn("unresolved_provenance", {item.code for item in issues})
```

- [ ] **Step 2: Run tests to confirm failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_fields_validator -v
```

Expected: FAIL because `app.query.fields` does not exist.

- [ ] **Step 3: Implement the three field views and deterministic validator**

Keep the classes separate and immutable. `validate_query_spec` checks physical columns, projection labels, non-empty resolvers for non-user-explicit values, supported view values, and valid metric/dimension pairing. Return all issues; do not stop at the first issue.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_fields_validator -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/fields.py backend/app/query/validator.py backend/tests/query/test_fields_validator.py
git commit -m "feat: validate query fields and provenance"
```

### Task 3: Resolve Values Through Existing Dictionaries

**Files:**
- Create: `backend/app/query/values.py`
- Create: `backend/app/query/analysis.py`
- Create: `prompts/query_spec.md`
- Test: `backend/tests/query/test_values_analysis.py`

**Interfaces:**
- Consumes: existing `parsers`, `route_resolver`, `dictionary`, and `nl2sql.known_problem_types`.
- Produces: `resolve_value(field, op, raw_value, question, kind) -> ValueBinding`, internal `build_query_plan(contract, schema, reach) -> QueryPlan`, and public `analyze_single_turn(question, kind, schema, reach) -> QueryPlan`.

- [ ] **Step 1: Write failing tests with injected LLM output**

```python
import unittest
from unittest.mock import patch

from app.query.analysis import analyze_single_turn
from app.query.fields import FieldReachSet, PhysicalSchemaView


class ValueAnalysisTests(unittest.TestCase):
    @patch("app.query.analysis.chat_json")
    def test_analysis_returns_structured_query_plan(self, chat_json):
        chat_json.return_value = {
            "final_goal": "统计泛油数量",
            "intent_family": "count",
            "views": [{
                "id": "q1",
                "subject": "disease",
                "view": "count",
                "metrics": ["count"],
                "dimensions": [],
                "filters": [{
                    "field": "problem_type_name",
                    "op": "=",
                    "raw_value": "泛油",
                }],
                "output_projection": [],
                "order_by": [],
                "limit": None,
                "required_assertions": ["preserve_user_scope"],
            }],
        }
        plan = analyze_single_turn(
            "泛油有多少处？",
            "mysql",
            PhysicalSchemaView(columns=frozenset({("disease", "problem_base_id")})),
            FieldReachSet(hits=()),
        )
        spec = plan.nodes[0].spec
        self.assertEqual(spec.view, "count")
        self.assertEqual(spec.filters[0].display_value, "泛油")
        self.assertTrue(spec.filters[0].resolver)
```

- [ ] **Step 2: Run test to verify code generation fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_values_analysis -v
```

Expected: FAIL because the analysis module and prompt are absent.

- [ ] **Step 3: Implement the JSON-only prompt and resolver**

`prompts/query_spec.md` must output only an intent contract plus one or more QuerySpec views and may not output SQL. `analyze_single_turn` calls `chat_json` with role `semantic`, resolves every filter through `resolve_value`, rejects unresolved scopes, and creates a frozen `IntentContract`. It then calls `planner.build_query_plan`, which splits mixed summary/detail requests into separate `PlanNode` values, assigns stable node IDs, preserves dependencies, and computes each node's `scope_hash`. `analyze_single_turn` returns the resulting `QueryPlan`.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_values_analysis -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/values.py backend/app/query/analysis.py backend/app/query/planner.py prompts/query_spec.md backend/tests/query/test_values_analysis.py
git commit -m "feat: produce provenance-aware query specs"
```

### Task 4: Add Unified Template Contracts and Candidate Resolution

**Files:**
- Create: `backend/app/query/templates.py`
- Test: `backend/tests/query/test_template_resolution.py`

**Interfaces:**
- Consumes: `QuerySpec`, `PhysicalSchemaView`.
- Produces: `TemplateContract`, `TemplateCandidate`, `ConfirmationCard`, `CandidateSet`, `TemplateProvider.declared_contracts(spec, schema) -> tuple[TemplateContract, ...]`, and `resolve_templates(spec, schema, providers) -> CandidateSet`.

- [ ] **Step 1: Write failing eligibility tests**

```python
import unittest

from app.query.contracts import QuerySpec
from app.query.fields import PhysicalSchemaView
from app.query.templates import TemplateContract, TemplateProvider, resolve_templates


class FixedProvider(TemplateProvider):
    def __init__(self, contracts):
        self.contracts = contracts

    def declared_contracts(self, spec, schema):
        return self.contracts


class TemplateResolutionTests(unittest.TestCase):
    def test_partial_metric_support_is_rejected(self):
        spec = QuerySpec(
            id="q1", kind="mysql", subject="disease", view="distribution",
            metrics=("count", "length"), dimensions=("route",), filters=(),
            output_projection=(), order_by=(), limit=None,
            required_assertions=(),
        )
        contract = TemplateContract(
            id="partial", version=1, source="preset", status="active", kind="mysql",
            subject="disease", view="distribution", metrics=("count",),
            dimensions=("route",), filters=(), fixed_conditions=(),
        )
        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )
        self.assertEqual(result.candidates, ())
        self.assertIn("missing_metric:length", result.rejections[0])

    def test_unique_complete_candidate_still_requires_confirmation(self):
        spec = QuerySpec(
            id="q1", kind="mysql", subject="disease", view="count",
            metrics=("count",), dimensions=(), filters=(), output_projection=(),
            order_by=(), limit=None, required_assertions=(),
        )
        contract = TemplateContract(
            id="complete", version=1, source="builtin", status="active", kind="mysql",
            subject="disease", view="count", metrics=("count",),
            dimensions=(), filters=(), fixed_conditions=(),
        )
        result = resolve_templates(
            spec,
            PhysicalSchemaView(columns=frozenset()),
            [FixedProvider([contract])],
        )
        self.assertEqual(len(result.candidates), 1)
        self.assertTrue(result.candidates[0].requires_confirmation)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_resolution -v
```

Expected: FAIL because `TemplateContract` is absent.

- [ ] **Step 3: Implement hard-gated template resolution**

Use exact set inclusion: every QuerySpec metric, dimension, filter, and output field must be supported by the contract. `TemplateContract` includes `adapter_id` and `native_id`, which the compiler uses to dispatch to the matching source-specific implementation. Collect all eligible candidates without truncation. Build `ConfirmationCard` data containing source, title, version, scope summary, parameter display values, fixed conditions, warnings, and SQL summary. Always set `requires_confirmation=True`.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_resolution -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/templates.py backend/tests/query/test_template_resolution.py
git commit -m "feat: gate template candidates by query contract"
```

### Task 5: Adapt Built-In and Preset Templates

**Files:**
- Create: `backend/app/query/template_sources.py`
- Test: `backend/tests/query/test_template_sources.py`

**Interfaces:**
- Consumes: `resources/sql_templates/_index.yaml`, `backend/app/data/preset_queries.json`, `preset_lib`.
- Produces: `BuiltinTemplateProvider` and `PresetTemplateProvider`, each implementing `TemplateProvider.declared_contracts(spec, schema)`.

- [ ] **Step 1: Write failing adapter tests**

```python
import unittest

from app.query.template_sources import contract_from_builtin, contract_from_preset


class TemplateSourceTests(unittest.TestCase):
    def test_builtin_metrics_become_exact_support_set(self):
        contract = contract_from_builtin({
            "id": "multi_metric",
            "source": "builtin",
            "match": {"metrics": ["count", "total_area"]},
        })
        self.assertEqual(contract.metrics, ("count", "total_area"))

    def test_preset_fixed_conditions_keep_values(self):
        contract = contract_from_preset({
            "id": "oil_count",
            "match": {"fixed": {"damage_type": "泛油"}},
        })
        self.assertEqual(contract.fixed_conditions[0].display_value, "泛油")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_sources -v
```

Expected: FAIL because adapters do not exist.

- [ ] **Step 3: Implement adapters without changing template SQL**

Read existing declarations and convert only metadata. A declaration without enough support metadata is excluded with a typed `incomplete_template_contract` rejection. Keep existing SQL binding functions as the compilation implementation.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_sources -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/template_sources.py backend/tests/query/test_template_sources.py
git commit -m "feat: adapt built-in and preset template contracts"
```

### Task 6: Adapt Auto and Verified-Example Templates

**Files:**
- Modify: `backend/app/query/template_sources.py`
- Test: `backend/tests/query/test_template_sources_auto_examples.py`

**Interfaces:**
- Consumes: `templatizer.list_templates`, `direct_match` result shape.
- Produces: `AutoTemplateProvider` and `VerifiedExampleTemplateProvider`.

- [ ] **Step 1: Write failing tests for missing declarations**

```python
import unittest

from app.query.template_sources import contract_from_auto, contract_from_example


class AutoExampleTemplateTests(unittest.TestCase):
    def test_auto_template_without_output_contract_is_rejected(self):
        result = contract_from_auto({"id": "t1", "sql": "SELECT 1"})
        self.assertFalse(result.ok)
        self.assertEqual(result.code, "incomplete_template_contract")

    def test_example_contract_preserves_provenance_requirements(self):
        result = contract_from_example({
            "doc_name": "doc-1",
            "spec": {"metrics": ["count"], "conditions": {"damage_type": ["泛油"]}},
        })
        self.assertTrue(result.ok)
        self.assertTrue(all(item.source for item in result.contract.fixed_conditions))
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_sources_auto_examples -v
```

Expected: FAIL because the adapters do not exist.

- [ ] **Step 3: Implement strict adapters**

An auto template participates only when it declares shape, subject, metrics, dimensions, filters, output columns, and parameter mapping. A verified example participates only when its stored spec can produce those declarations without guesses. Otherwise exclude it and emit a structured rejection in debug logs.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_template_sources_auto_examples -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/template_sources.py backend/tests/query/test_template_sources_auto_examples.py
git commit -m "feat: adapt auto and verified example templates"
```

### Task 7: Add Confirmation Tokens and User Decisions

**Files:**
- Create: `backend/app/query/confirmations.py`
- Test: `backend/tests/query/test_confirmations.py`

**Interfaces:**
- Consumes: `QueryPlan`, `CandidateSet`, schema snapshot identifier.
- Produces: `issue_confirmation(plan, candidate_set, schema_snapshot) -> ConfirmationTicket`, `consume_confirmation(token, choices, overrides, value_resolver=resolve_value, scope_hash=None) -> UserDecision`, and `ConfirmationError`.

- [ ] **Step 1: Write failing confirmation tests**

Add a local `candidate_set_with_one_template()` helper returning one candidate whose `candidate_id` is `c1` for node `q1`.

```python
import unittest
from unittest.mock import patch

from app.query.confirmations import ConfirmationError, consume_confirmation, issue_confirmation
from app.query.analysis import QuerySpecError
from tests.query.factories import count_plan
from app.query.templates import CandidateSet


class ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.plan = count_plan()

    def test_token_is_single_use(self):
        ticket = issue_confirmation(self.plan, CandidateSet(candidates=(), rejections=()), "schema-1")
        consume_confirmation(ticket.token, choices=(), overrides={})
        with self.assertRaises(ConfirmationError):
            consume_confirmation(ticket.token, choices=(), overrides={})

    def test_scope_hash_mismatch_is_rejected(self):
        ticket = issue_confirmation(self.plan, CandidateSet(candidates=(), rejections=()), "schema-1")
        with self.assertRaisesRegex(ConfirmationError, "scope"):
            consume_confirmation(ticket.token, choices=(), overrides={}, scope_hash="tampered")

    def test_invalid_override_is_rejected_before_execution(self):
        ticket = issue_confirmation(
            self.plan,
            candidate_set_with_one_template(),
            "schema-1",
        )
        with patch(
            "app.query.confirmations.resolve_value",
            side_effect=QuerySpecError("unknown enum"),
        ):
            with self.assertRaisesRegex(ConfirmationError, "override"):
                consume_confirmation(
                    ticket.token,
                    choices={"q1": "c1"},
                    overrides={"q1": {"problem_type_name": "其他"}},
                )
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_confirmations -v
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement in-memory single-use confirmations**

Store the plan scope hash, selected candidate ID for every template-bearing node, template versions, parameter bindings, schema snapshot, issue time, and expiry. Match the existing capability-token TTL. A successful consume removes the token before returning. Expose no API that executes directly from a candidate without a consumed decision.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_confirmations -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/confirmations.py backend/tests/query/test_confirmations.py
git commit -m "feat: require explicit template confirmation"
```

### Task 8: Compile Confirmed Templates and Wren Queries

**Files:**
- Create: `backend/app/query/compiler.py`
- Test: `backend/tests/query/test_compiler.py`

**Interfaces:**
- Consumes: `UserDecision`, `QuerySpec`, `TemplateCandidate`.
- Produces: `CompiledQuery(sql: str, engine: str, source: str, template_id: str | None, template_version: int | None)` and `compile_query(spec, decision, candidate, template_executor, wren_executor) -> CompiledQuery`.

- [ ] **Step 1: Write failing tests**

```python
import unittest

from app.query.compiler import compile_query
from app.query.contracts import QuerySpec, UserDecision


class CompilerTests(unittest.TestCase):
    def test_template_compilation_requires_consumed_decision(self):
        spec = QuerySpec(
            id="q1", kind="mysql", subject="disease", view="count",
            metrics=("count",), dimensions=(), filters=(), output_projection=(),
            order_by=(), limit=None, required_assertions=(),
        )
        with self.assertRaisesRegex(ValueError, "confirmation"):
            compile_query(
                spec,
                UserDecision(token="", choices={"q1": "x"}, overrides={}),
                candidate=template_candidate(),
                template_executor=None,
                wren_executor=None,
            )

    def test_no_candidate_compiles_through_wren(self):
        spec = QuerySpec(
            id="q1", kind="mysql", subject="disease", view="count",
            metrics=("count",), dimensions=(), filters=(), output_projection=(),
            order_by=(), limit=None, required_assertions=(),
        )
        decision = UserDecision(token="issued", choices={}, overrides={})
        result = compile_query(
            spec,
            decision,
            candidate=None,
            template_executor=None,
            wren_executor=lambda current: "SELECT COUNT(*) FROM ai_problem",
        )
        self.assertEqual(result.engine, "wren")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_compiler -v
```

Expected: FAIL because the compiler module and `UserDecision` are absent.

- [ ] **Step 3: Implement compiler adapters**

Add a local `template_candidate()` test helper and add `UserDecision(token: str, choices: dict[str, str], overrides: dict[str, dict[str, Any]])` to `contracts.py`. `choices` maps plan node ID to confirmed candidate ID. A template cannot compile without a non-empty consumed token and a matching candidate ID for that node. Wren compilation is used only when no template candidate was selected for the node. The compiler does not execute SQL.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_compiler -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/contracts.py backend/app/query/compiler.py backend/tests/query/test_compiler.py
git commit -m "feat: compile only confirmed query decisions"
```

### Task 9: Execute SQL and Record Observations

**Files:**
- Create: `backend/app/query/executor.py`
- Test: `backend/tests/query/test_executor.py`

**Interfaces:**
- Consumes: `CompiledQuery`, an injectable `execute_sql(kind, sql)` callable.
- Produces: `execute_compiled(compiled, executor, display_limit=500, count_sql=None) -> ExecutionObservation`.

- [ ] **Step 1: Write failing observation tests**

```python
import unittest

from app.query.compiler import CompiledQuery
from app.query.executor import execute_compiled


class ExecutorTests(unittest.TestCase):
    def test_detail_observation_separates_fetched_and_total(self):
        compiled = CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "mysql", "wren", None, None)
        def fake_execute(kind, sql):
            if "COUNT(*)" in sql:
                return {"columns": ["n"], "rows": [{"n": "20108"}]}
            return {"columns": ["id"], "rows": [{"id": str(i)} for i in range(500)]}
        observation = execute_compiled(
            compiled,
            fake_execute,
            display_limit=500,
            count_sql="SELECT COUNT(*) AS n FROM ai_problem",
        )
        self.assertEqual(observation.rows_fetched, 500)
        self.assertEqual(observation.total_count, 20108)
        self.assertTrue(observation.truncated)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_executor -v
```

Expected: FAIL because the module does not exist.

- [ ] **Step 3: Implement executor and count separation**

The injected executor returns columns and rows. For list views, accept an optional precompiled count query and record its exact total. Preserve database error text in `runtime_error`. Do not call an LLM in this module.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_executor -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/executor.py backend/tests/query/test_executor.py
git commit -m "feat: record exact single-turn execution observations"
```

### Task 10: Make Post-Calculation Empty Results Typed Failures

**Files:**
- Modify: `backend/app/services/agent.py:_post_calc_payload`
- Modify: `backend/app/services/tools/big/post_calc.py`
- Test: `backend/tests/query/test_post_calc_failure.py`

**Interfaces:**
- Consumes: the existing post-calculation tool and payload.
- Produces: payloads with `accepted=False`, `source="error"`, and a non-empty `failure` object whenever no usable calculation exists.

- [ ] **Step 1: Write the failing regression test**

```python
import unittest
from unittest.mock import patch

from app.services import agent
from app.services.tools.big import post_calc


class PostCalcFailureTests(unittest.TestCase):
    def test_fallback_sentence_is_not_success(self):
        with patch("app.services.agent.chat", side_effect=RuntimeError("model unavailable")):
            payload = agent._post_calc_payload(
                {"question": "计算密度", "depends_on": [1]},
                {1: {"rows": [], "columns": []}},
            )
            result = post_calc.post_calc(
                {"question": "计算密度", "depends_on": [1]},
                {1: {"rows": [], "columns": []}},
            )
        self.assertFalse(payload["accepted"])
        self.assertEqual(payload["source"], "error")
        self.assertFalse(result["ok"])
        self.assertEqual(result["failure"]["failure_code"], "post_calc_empty")
```

- [ ] **Step 2: Run the test to reproduce the defect**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_post_calc_failure -v
```

Expected: FAIL because the fallback reply is currently accepted as non-empty text.

- [ ] **Step 3: Replace the success-shaped fallback**

Keep user-facing fallback text only inside a typed failed payload. Remove `accepted=bool(reply)` for missing calculation data. Keep exception capture for infrastructure failures and map both empty-result and exception cases through `react_repair.build_failure`.

- [ ] **Step 4: Run focused and existing repair tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_post_calc_failure -v
.\.venv\Scripts\python.exe test_react_repair.py
```

Expected: both commands pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/agent.py backend/app/services/tools/big/post_calc.py backend/tests/query/test_post_calc_failure.py
git commit -m "fix: fail closed on empty post calculations"
```

### Task 11: Implement Three-Layer Verification

**Files:**
- Create: `backend/app/query/verifier.py`
- Test: `backend/tests/query/test_verifier.py`

**Interfaces:**
- Consumes: `QuerySpec`, `CompiledQuery`, `ExecutionObservation`, and injectable semantic/presentation checkers.
- Produces: `verify_deterministic(...) -> tuple[VerificationIssue, ...]`, `verify_result(...) -> VerificationReport`, and `verify_presentation(...) -> VerificationReport`.

- [ ] **Step 1: Write failing tests for detail and presentation behavior**

Add local factory helpers `compiled_detail()`, `detail_observation(...)`, `count_sql()`, and `count_observation()` in the test module. They construct the real contract objects with one preview row; the detail observation records `rows_fetched=500`, `total_count=20108`, and `truncated=True`.

```python
import unittest

from app.query.verifier import verify_result
from tests.query.factories import count_spec, detail_spec


class VerifierTests(unittest.TestCase):
    def test_large_detail_uses_metadata_not_full_rows(self):
        report = verify_result(
            spec=detail_spec(),
            compiled=compiled_detail(),
            observation=detail_observation(rows_fetched=500, total_count=20108, truncated=True),
            semantic_checker=lambda *_: (_ for _ in ()).throw(AssertionError("must not scan all rows")),
            presentation_checker=lambda *_: (),
        )
        self.assertEqual(report.status, "accepted")

    def test_presentation_failure_requests_presentation_only(self):
        report = verify_result(
            spec=count_spec(),
            compiled=count_sql(),
            observation=count_observation(),
            semantic_checker=lambda *_: (),
            presentation_checker=lambda *_: ("missing_unit",),
        )
        self.assertEqual(report.status, "repairable")
        self.assertEqual(report.allowed_actions, ("repair_presentation",))
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_verifier -v
```

Expected: FAIL because verification functions do not exist.

- [ ] **Step 3: Implement verification order**

Run deterministic checks first. Stop semantic checks when deterministic facts fail. Run semantic checks with metadata and at most ten preview rows. Run presentation checks only after data passes. Return `accepted`, `repairable`, `unsupported`, or `ambiguous`, with node IDs and allowed actions.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_verifier -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/verifier.py backend/tests/query/test_verifier.py
git commit -m "feat: add layered query verification"
```

### Task 12: Apply Bounded RepairPatches

**Files:**
- Create: `backend/app/query/repair.py`
- Test: `backend/tests/query/test_query_repair.py`

**Interfaces:**
- Consumes: `QuerySpec`, `FailureEvidence`, previous patch signatures.
- Produces: `RepairPatch`, `apply_repair(spec, patch, previous_signatures) -> QuerySpec`, and `request_template_confirmation(candidate_id) -> RepairPatch`.

- [ ] **Step 1: Write failing repair tests**

```python
import unittest

from app.query.contracts import QuerySpec
from app.query.repair import RepairPatch, apply_repair, request_template_confirmation
from tests.query.factories import count_spec


class RepairTests(unittest.TestCase):
    def test_repair_cannot_change_scope_hash(self):
        spec = count_spec()
        patch = RepairPatch(action="patch_filter_value", node_id="q1", payload={"field": "problem_type_name", "value": "坑槽"})
        with self.assertRaisesRegex(ValueError, "scope"):
            apply_repair(spec, patch, set())

    def test_same_patch_is_rejected(self):
        spec = count_spec()
        patch = RepairPatch(action="patch_metric", node_id="q1", payload={"add": "length"})
        first = apply_repair(spec, patch, set())
        with self.assertRaisesRegex(ValueError, "duplicate"):
            apply_repair(first, patch, {patch.signature})

    def test_template_repair_only_requests_confirmation(self):
        patch = request_template_confirmation("candidate-2")
        self.assertEqual(patch.action, "request_template_confirmation")
        self.assertEqual(patch.payload["candidate_id"], "candidate-2")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_query_repair -v
```

Expected: FAIL because repair contracts do not exist.

- [ ] **Step 3: Implement local patches and the no-scope-change rule**

Allow only the action list from the spec. Recompute the scope hash after each patch. Permit adding a missing non-filter metric/dimension or fixing an unresolved stored value with a dictionary-backed binding. Reject any patch that changes user filters, entities, time, or explicit thresholds. A template proposal returns `request_template_confirmation`; it never executes.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_query_repair -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/repair.py backend/tests/query/test_query_repair.py
git commit -m "feat: constrain query repair to local patches"
```

### Task 13: Build the Single-Turn Facade

**Files:**
- Create: `backend/app/query/service.py`
- Modify: `backend/app/query/__init__.py`
- Test: `backend/tests/query/test_service.py`

**Interfaces:**
- Consumes: all prior query modules.
- Produces: `start_single_turn(question, kind="mysql") -> TurnStart` and `confirm_single_turn(ticket_token, choices, overrides) -> AnswerPackage | TurnStart`.
- `TurnStart` contains either `candidate_set: CandidateSet` with confirmation data or `answer: AnswerPackage`.

- [ ] **Step 1: Write failing facade tests**

Add local factories `count_plan()`, `candidate_set_with_one_template()`, `empty_candidate_set()`, and `compiled_count_query()` to this test module. They return real contract objects with one count node and no database access.

```python
import unittest
from unittest.mock import patch

from app.query.service import start_single_turn
from tests.query.factories import count_plan


class ServiceTests(unittest.TestCase):
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_template_match_returns_cards_without_execution(self, analyze, resolve):
        analyze.return_value = count_plan()
        resolve.return_value = candidate_set_with_one_template()
        result = start_single_turn("泛油有多少处？")
        self.assertIsNotNone(result.candidate_set)
        self.assertIsNone(result.answer)

    @patch("app.query.service.compile_query")
    @patch("app.query.service.resolve_templates")
    @patch("app.query.service.analyze_single_turn")
    def test_no_template_can_continue_to_wren(self, analyze, resolve, compile_query):
        analyze.return_value = count_plan()
        resolve.return_value = empty_candidate_set()
        compile_query.return_value = compiled_count_query()
        result = start_single_turn("泛油有多少处？")
        self.assertIsNotNone(result.answer)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_service -v
```

Expected: FAIL because the service does not exist.

- [ ] **Step 3: Implement the orchestration**

Perform analysis, validation, per-node template resolution, confirmation, compilation, execution, verification, bounded repair, and answer assembly in that order. If any template candidate exists in any node, return the combined `candidate_set` and do not execute any node. If none exists, continue through Wren. Preserve partial successful sections when a later node fails.

- [ ] **Step 4: Run tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_service -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/__init__.py backend/app/query/service.py backend/tests/query/test_service.py
git commit -m "feat: add confirmed single-turn query facade"
```

### Task 14: Integrate Behind a Feature Flag

**Files:**
- Modify: `backend/app/services/chat_settings.py`
- Modify: `backend/app/services/agent.py`
- Modify: `backend/app/services/capabilities.py`
- Test: `backend/tests/query/test_agent_integration.py`

**Interfaces:**
- Consumes: `app.query.service.start_single_turn`, `confirm_single_turn(ticket_token, choices, overrides)`, and existing `ChooseCapability` payload.
- Produces: `chat_settings.load()["single_turn_compiler_enabled"]`, a data-query route controlled by the flag, and confirmation payloads carrying `compiler_context`.

- [ ] **Step 1: Write a failing integration test**

Add a local `turn_with_confirmation_card()` helper returning a real `TurnStart` with an empty answer and one confirmation candidate.

```python
import unittest
from unittest.mock import patch

from app.services import agent


class AgentIntegrationTests(unittest.TestCase):
    @patch("app.services.agent.chat_settings.load", return_value={"single_turn_compiler_enabled": True})
    @patch("app.query.service.start_single_turn")
    def test_enabled_data_question_uses_new_service(self, start, _settings):
        start.return_value = turn_with_confirmation_card()
        payload = agent.handle_message("泛油有多少处？", session_id="s1")
        self.assertTrue(payload["await_confirm"])
        self.assertEqual(payload["source"], "confirm")
        self.assertIn("compiler_context", payload)
```

- [ ] **Step 2: Run test to confirm the flag is absent**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_agent_integration -v
```

Expected: FAIL because neither the setting nor the route exists.

- [ ] **Step 3: Add flag routing and confirmation round-trip**

Default the flag to false. When enabled and the question is a single-turn data intent, call `start_single_turn` before the legacy optimizer/planner. Convert `CandidateSet` to the existing `ChooseCapability` card shape and stash `compiler_context`. On a confirmation request, route through `confirm_single_turn`, never through the legacy `capabilities.execute`.

- [ ] **Step 4: Run focused integration tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_agent_integration -v
.\.venv\Scripts\python.exe test_tool_registry.py
```

Expected: both pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/chat_settings.py backend/app/services/agent.py backend/app/services/capabilities.py backend/tests/query/test_agent_integration.py
git commit -m "feat: gate single-turn compiler integration"
```

### Task 15: Add the Real-Answer Regression Harness

**Files:**
- Create: `backend/evals/__init__.py`
- Create: `backend/evals/single_turn/cases.yaml`
- Create: `backend/evals/run_single_turn_eval.py`
- Test: `backend/tests/query/test_eval_harness.py`

**Interfaces:**
- Consumes: the public chat or facade response and versioned YAML cases.
- Produces: `run_case(case, runner) -> CaseResult` and an exit code of zero only when every case meets its declared expected status.

- [ ] **Step 1: Write failing evaluator tests**

```python
import unittest

from evals.run_single_turn_eval import run_case


class EvalHarnessTests(unittest.TestCase):
    def test_explicit_failure_is_failure_for_solvable_case(self):
        result = run_case(
            {"id": "count", "question": "泛油有多少处？", "expected_status": "answered"},
            lambda _: {"answer": None, "verification": {"status": "repairable"}},
        )
        self.assertFalse(result.ok)

    def test_unsupported_case_requires_specific_reason(self):
        result = run_case(
            {"id": "missing", "question": "病害的坡度分布", "expected_status": "unsupported"},
            lambda _: {"answer": "系统暂不支持", "verification": {"status": "unsupported", "reason": "数据库没有坡度字段"}},
        )
        self.assertTrue(result.ok)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_eval_harness -v
```

Expected: FAIL because the evaluator does not exist.

- [ ] **Step 3: Implement the evaluator and initial cases**

Write all 15 questions from the spec into `cases.yaml` with expected status, required filters, required output fields, exact values or invariants, and allowed truncation. The runner must report `pass`, `fail`, and `partial` separately. A solver failure is never converted to pass.

- [ ] **Step 4: Run the offline harness**

Run:

```powershell
$env:CODEX_REGRESSION='1'
.\.venv\Scripts\python.exe -m unittest tests.query.test_eval_harness -v
.\.venv\Scripts\python.exe evals\run_single_turn_eval.py --dry-run
```

Expected: unit tests pass and the dry run parses all 15 cases without executing queries.

- [ ] **Step 5: Commit**

```powershell
git add backend/evals/__init__.py backend/evals/single_turn/cases.yaml backend/evals/run_single_turn_eval.py backend/tests/query/test_eval_harness.py
git commit -m "test: add single-turn answer regression suite"
```

### Task 16: Verify the Full Single-Turn Path with Fake Adapters

**Files:**
- Modify: `backend/app/query/service.py`
- Test: `backend/tests/query/test_single_turn_e2e.py`

**Interfaces:**
- Consumes: the completed core, template confirmation, and injectable compiler/executor/verifier adapters.
- Produces: an end-to-end service orchestration proven before live database evaluation.

- [ ] **Step 1: Write failing end-to-end contract tests**

Extend `tests/query/factories.py` with `length_query_plan()`. Add local fixtures `count_observation()`, `detail_observation(...)`, and `accepted_report()` to the test module. Patch the service's analyzer, template resolver, compiler, executor, verifier, and answer assembler so the test crosses every orchestration boundary without a real database.

```python
import unittest
from unittest.mock import patch

from app.query.compiler import CompiledQuery
from app.query.contracts import AnswerPackage
from app.query.service import start_single_turn
from tests.query.factories import count_plan, length_query_plan


class SingleTurnEndToEndTests(unittest.TestCase):
    @patch("app.query.service.assemble_answer", return_value=AnswerPackage("共 20108 处；明细展示前 500 条。"))
    @patch("app.query.service.verify_result", side_effect=[accepted_report(), accepted_report()])
    @patch("app.query.service.execute_compiled", side_effect=[count_observation(), detail_observation(500, 20108)])
    @patch("app.query.service.compile_query", side_effect=[
        CompiledQuery("SELECT COUNT(*) FROM ai_problem", "mysql", "wren", None, None),
        CompiledQuery("SELECT id FROM ai_problem LIMIT 500", "mysql", "wren", None, None),
    ])
    @patch("app.query.service.resolve_templates", return_value=empty_candidate_set())
    @patch("app.query.service.analyze_single_turn", return_value=length_query_plan())
    def test_length_query_separates_summary_count_and_detail_page(
        self,
        _analyze,
        _resolve,
        _compile,
        execute,
        _verify,
        _assemble,
    ):
        result = start_single_turn("病害长度大于 2 米的有多少处？把明细列出来")
        self.assertEqual(execute.call_count, 2)
        self.assertIn("20108", result.answer.text)
        self.assertIn("前 500", result.answer.text)

    @patch("app.query.service.execute_compiled")
    @patch("app.query.service.resolve_templates", return_value=candidate_set_with_one_template())
    @patch("app.query.service.analyze_single_turn", return_value=count_plan())
    def test_template_query_does_not_execute_before_confirmation(self, _analyze, _resolve, execute):
        result = start_single_turn("泛油有多少处？")
        self.assertIsNotNone(result.candidate_set)
        execute.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm end-to-end gaps**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_single_turn_e2e -v
```

Expected: FAIL because the service cannot yet run a multi-node plan through injected adapters.

- [ ] **Step 3: Wire injectable end-to-end adapters**

Inject SQL compilation, execution, verification, and answer assembly into `service.py`. Iterate plan nodes in dependency order, compile and execute each node, preserve accepted sections, and stop dependent nodes after a rejected dependency. Do not change legacy branches in this task.

- [ ] **Step 4: Run full offline verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/query -v
.\.venv\Scripts\python.exe test_react_agent.py
.\.venv\Scripts\python.exe test_react_repair.py
.\.venv\Scripts\python.exe test_tool_registry.py
.\.venv\Scripts\python.exe -m compileall -q app
```

Expected: all tests pass and compileall exits with no errors.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/service.py backend/tests/query/test_single_turn_e2e.py
git commit -m "test: verify single-turn query orchestration"
```

### Task 17: Run Live Single-Turn Evaluation

**Files:**
- Modify: `backend/evals/single_turn/cases.yaml` only if the database schema changes; never hardcode volatile presentation text.
- Modify: `backend/evals/run_single_turn_eval.py` only for evaluator defects.
- Test: existing live cases; no new production code.

- [ ] **Step 1: Start from the confirmed specification and backend**

Run:

```powershell
D:\Code\巡检智能体\backend\.venv\Scripts\python.exe run.py --kill
```

Expected: `Uvicorn running on http://127.0.0.1:18730`.

- [ ] **Step 2: Run the 15-case live evaluation**

Run:

```powershell
$env:CODEX_REGRESSION='1'
.\.venv\Scripts\python.exe evals\run_single_turn_eval.py
```

Expected: every case is `pass`; report counts for exact answer, invariant pass, unsupported, partial, and failed.

- [ ] **Step 3: Diagnose failures by boundary**

For each failure, identify whether it belongs to QuerySpec, field/value resolution, template eligibility, confirmation, compilation, execution, deterministic verification, semantic verification, presentation, or repair. Add a focused failing test before changing that boundary.

- [ ] **Step 4: Re-run focused and full suites**

Run:

```powershell
$env:CODEX_REGRESSION='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests/query -v
.\.venv\Scripts\python.exe evals\run_single_turn_eval.py
```

Expected: all focused tests and all 15 live cases pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/evals/single_turn/cases.yaml backend/evals/run_single_turn_eval.py
git commit -m "test: verify single-turn query baseline"
```

### Task 18: Remove Replaced Legacy Branches After Live Validation

**Files:**
- Modify: `backend/app/services/agent.py`
- Modify: `backend/app/services/nl2sql.py`
- Modify: `backend/app/services/capabilities.py`
- Test: `backend/tests/query/test_agent_integration.py`
- Test: existing full regression scripts

**Interfaces:**
- Consumes: a passing 15-case live evaluation from Task 17.
- Produces: a feature-flagged path with only the legacy branches that remain reachable when the flag is false or the new path declines the request.

- [ ] **Step 1: Add a flag-off regression test**

```python
    @patch("app.services.agent.chat_settings.load", return_value={"single_turn_compiler_enabled": False})
    @patch("app.query.service.start_single_turn")
    def test_disabled_flag_never_calls_single_turn_service(self, start, _settings):
        with patch.object(agent, "_handle_message", return_value={"reply": "legacy"}):
            payload = agent.handle_message("泛油有多少处？", session_id="s1")
        start.assert_not_called()
        self.assertEqual(payload["reply"], "legacy")
```

- [ ] **Step 2: Run the flag-off test**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.query.test_agent_integration -v
```

Expected: PASS; this proves the legacy path remains available while the new path is disabled.

- [ ] **Step 3: Remove only replaced branches**

Trace calls from `agent.py` with the feature flag enabled. Remove optimizer, planner, capability execution, and direct repair branches only when no enabled single-turn call can reach them. Keep shared helpers used by knowledge, image, metadata, long-dialogue, or flag-off paths.

- [ ] **Step 4: Run all regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests/query -v
.\.venv\Scripts\python.exe test_react_agent.py
.\.venv\Scripts\python.exe test_react_repair.py
.\.venv\Scripts\python.exe test_plan_contracts.py
.\.venv\Scripts\python.exe test_field_contracts.py
.\.venv\Scripts\python.exe test_tool_registry.py
.\.venv\Scripts\python.exe -m compileall -q app
$env:CODEX_REGRESSION='1'
.\.venv\Scripts\python.exe evals\run_single_turn_eval.py
```

Expected: all unit checks pass and all live cases remain successful.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/agent.py backend/app/services/nl2sql.py backend/app/services/capabilities.py backend/tests/query/test_agent_integration.py
git commit -m "refactor: remove replaced single-turn branches"
```
