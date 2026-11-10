# QuerySpec to MDL-SQL Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one audited QuerySpec-to-MDL-SQL pipeline that covers all eleven registered subjects, prefers deterministic compilation, allows exactly one contract-bounded LLM fallback, and executes only physical SQL produced by Wren after all audits pass.

**Architecture:** QuerySpec remains the only authority for user intent and scope. Each `query_data` node becomes one immutable `MdlQueryContract`; a narrowed business-capability view, MDL subset, and relationship subgraph are derived from that contract. Deterministic and LLM-generated MDL-SQL share one `sqlglot` audit, one Wren `dry-plan` adapter, and one physical-SQL static check.

**Tech Stack:** Python 3.11, FastAPI, `unittest`/`pytest`, `sqlglot` 30.18.0, WrenAI 0.14.0, existing Vue 3 frontend build.

**Spec:** `docs/superpowers/specs/2026-11-10-query-spec-mdl-sql-generation-design.md`

## Global Constraints

- Read both project-principle documents before every task:
  - `docs/项目原则-禁止正则与关键词硬匹配.md`
  - `docs/项目原则-AI晋级考试与用户裁决.md`
- QuerySpec is the sole authority for subject, filters, time range, route, disease, device, task, metrics, ordering, limit, and assertions.
- The LLM receives only the contract, narrowed business capability view, narrowed MDL subset, and permitted relationship subgraph.
- The LLM never rewrites QuerySpec, SQL filters, subject, ordering, limit, physical tables, or relationships.
- Deterministic and LLM MDL-SQL must pass the same AST audit, Wren `dry-plan`, and physical-SQL static check.
- A result is successful only when SQL is non-empty, parseable, contract-audited, relationship-audited, safety-audited, dry-planned, and physically checked.
- Empty SQL, error prose, Markdown, multiple SQL statements, and `bool(reply)` are never success evidence.
- QuerySpec validation, capability failure, contract-build failure, context-budget failure, relationship ambiguity, and security rejection do not enter LLM SQL fallback.
- Deterministic compiler, deterministic audit, deterministic dry-plan, or deterministic physical-check failure may trigger exactly one LLM MDL-SQL generation.
- A failed node must not contaminate an unrelated sibling node; dependent nodes may become `blocked`.
- Business configuration belongs only under `resources/capabilities/`.
- No keyword, regex, question ID, or text-similarity rule may choose business semantics, models, fields, metrics, relationships, or SQL.
- Migrating an implementation must delete the old implementation in the same task; do not keep dual writers.
- Preserve every existing uncommitted user change. Do not reset, checkout, clean, rebase, or push.
- Before each commit, inspect `git diff -- <exact files>` and stage only hunks created by that task.
  If an existing user hunk cannot be separated safely, stop and ask the user to commit that hunk first.
- Use `backend/.venv/Scripts/python.exe` for backend tests and `npm run build` inside `frontend`.
- Do not add one-off fixes for a single exam question. Fix shared contracts, then regenerate a fresh exam.

## Review Focus

- A plan has two `query_data` nodes with different final subjects; one node fails or uses fallback and the other must remain independently executable, while only its actual dependants become `blocked`.
- Necessary context exceeds `max_context_bytes`; the pipeline must return `context_budget_exceeded` instead of truncating fields or relationships.
- The required models have more than one distinguishable relationship path; the pipeline must reject with `relationship_path_ambiguous` instead of sending the ambiguity to the LLM.
- The LLM returns SQL that changes a contract filter, adds an extra filter, changes the root model, changes ordering, or increases `limit`; every such candidate must be rejected by AST audit.
- Wren `dry-plan` returns `None`, an empty string/dictionary, or raises an exception; the pipeline must record a structured failure and must not execute or claim success.

---

## File Structure

New package responsibilities:

```text
backend/app/query/mdl_sql/
  __init__.py
  models.py
  errors.py
  catalog.py
  contract_builder.py
  context/
    __init__.py
    capabilities.py
    mdl.py
    relationships.py
    prompt_context.py
  compiler/
    __init__.py
    deterministic.py
    llm_fallback.py
    pipeline.py
  audit/
    __init__.py
    ast.py
    contract_audit.py
    relationship_audit.py
    safety_audit.py
    failures.py
  dry_plan.py
```

- `models.py` owns immutable transport/domain objects only.
- `errors.py` owns structured pipeline exceptions and converts failures without adding business logic.
- `catalog.py` wraps Wren MDL facts and computes a stable MDL fingerprint.
- `contract_builder.py` projects one validated QuerySpec into one immutable contract.
- `context/` selects only facts referenced by the contract and enforces a hard byte budget.
- `compiler/deterministic.py` compiles a contract without subject-specific Python branches.
- `compiler/llm_fallback.py` makes one MDL-SQL request and parses one strict JSON reply.
- `compiler/pipeline.py` owns fallback eligibility, stage transitions, and final candidate selection.
- `audit/` parses and validates SQL structure; it never infers business meaning from text.
- `dry_plan.py` adapts Wren compilation and physical SQL static checking into structured results.

Canonical failure-code registry:

```text
contract:      contract_build_failed
context:       context_budget_exceeded
relationship:  relationship_path_ambiguous
deterministic: compiler_not_covered, compiler_exception
audit:         root_subject_mismatch, unexpected_model, unexpected_field, missing_filter,
               extra_filter, filter_value_mismatch, illegal_relationship, cardinality_risk,
               invalid_view_shape, missing_output, order_required, invalid_limit,
               physical_table_reference, soft_delete_missing, sql_parse_failed,
               multiple_statements, forbidden_sql_type, dangerous_function, star_projection
dry_plan:      dry_plan_failed
physical:      physical_sql_invalid
fallback:      llm_sql_invalid, llm_declared_scope_mismatch, llm_fallback_failed, llm_rate_limited,
               llm_quota_exhausted, llm_transport_error, llm_provider_unavailable
```

New test files:

```text
backend/tests/query/test_mdl_catalog.py
backend/tests/query/test_mdl_contract_builder.py
backend/tests/query/test_mdl_context.py
backend/tests/query/test_mdl_sql_audit.py
backend/tests/query/test_mdl_deterministic_compiler.py
backend/tests/query/test_mdl_dry_plan.py
backend/tests/query/test_mdl_llm_fallback.py
backend/tests/query/test_mdl_sql_pipeline.py
backend/tests/query/test_mdl_sql_e2e.py
```

Existing integration points:

```text
resources/capabilities/index.yaml
resources/capabilities/subjects.yaml
resources/capabilities/mdl_compilation.yaml
backend/app/query/capability_registry/models.py
backend/app/query/capability_registry/loader.py
backend/app/query/capability_registry/registry.py
backend/app/services/wren_engine.py
backend/app/query/compiler.py
backend/app/query/service.py
backend/app/query/spec_mdl.py
prompts/mdl_sql_generator.md
```

---

### Task 1: Register MDL Compilation Facts

**Files:**
- Create: `resources/capabilities/mdl_compilation.yaml`
- Modify: `resources/capabilities/index.yaml`
- Modify: `resources/capabilities/subjects.yaml`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/loader.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Test: `backend/tests/query/test_capability_registry.py`
- Test: `backend/tests/query/test_capability_config_guard.py`

**Interfaces:**
- Produces: `MdlCompilationPolicy` with `max_context_bytes`, `max_models_per_contract`, `max_relationship_hops`, `llm_fallback_attempts`, `forbidden_sql_types`, `forbidden_functions`, and `require_soft_delete`.
- Produces: `SubjectCapability.mdl_model: str`.
- Produces: `CapabilitySnapshot.mdl_compilation: MdlCompilationPolicy`.

- [ ] **Step 1: Write the failing configuration tests**

Add assertions that every registered subject has a non-empty `mdl_model`, that every
`mdl_model` exists in `wren_engine.model_names("mysql")`, and that the exact subject mapping is:

```python
{
    "disease": "disease",
    "patrol": "patrol",
    "route": "route",
    "section": "section",
    "pile_point": "pile_point",
    "asset": "ai_asset_main",
    "work_order": "ai_work_order",
    "bridge": "basic_facilities_bridges",
    "tunnel": "basic_facilities_tunnels",
    "slope": "basic_facilities_slope",
    "project": "project_manage_project_main",
}
```

Add assertions that the policy is frozen, loaded from `mdl_compilation.yaml`,
`llm_fallback_attempts == 1`, and invalid values such as zero attempts, negative byte
limits, or unknown SQL types raise `CapabilityConfigError`.

Add a config-guard assertion that `mdl_compilation.yaml` is listed in `index.yaml` and that
no subject-to-MDL mapping exists in Python outside `resources/capabilities/`.

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_capability_registry.py backend/tests/query/test_capability_config_guard.py -q
```

Expected: FAIL because `mdl_model`, `MdlCompilationPolicy`, and the policy file do not exist.

- [ ] **Step 3: Add the policy and subject mappings**

Create `mdl_compilation.yaml` with:

```yaml
mdl_compilation:
  schema_version: 1
  max_context_bytes: 131072
  max_models_per_contract: 8
  max_relationship_hops: 4
  llm_fallback_attempts: 1
  forbidden_sql_types:
    - INSERT
    - UPDATE
    - DELETE
    - MERGE
    - CREATE
    - ALTER
    - DROP
    - TRUNCATE
    - GRANT
    - REVOKE
    - CALL
    - EXEC
  forbidden_functions:
    - sleep
    - benchmark
    - load_file
    - get_lock
    - release_lock
  require_soft_delete: true
```

Add `mdl_model` to all eleven subject entries using the exact mapping in Step 1. Extend the
loader's root-key table and manifest. Parse and validate the policy once in `CapabilityRegistry.load`;
do not read YAML from contracts, audits, or compilers.

- [ ] **Step 4: Run the focused tests**

Run the command from Step 2.

Expected: PASS, including the existing nested read-only snapshot test.

- [ ] **Step 5: Commit**

```powershell
git add resources/capabilities/index.yaml resources/capabilities/subjects.yaml resources/capabilities/mdl_compilation.yaml backend/app/query/capability_registry/models.py backend/app/query/capability_registry/loader.py backend/app/query/capability_registry/registry.py backend/tests/query/test_capability_registry.py backend/tests/query/test_capability_config_guard.py
git commit -m "feat: register mdl compilation capabilities"
```

---

### Task 2: Build an Immutable MDL Snapshot and Relationship Graph

**Files:**
- Create: `backend/app/query/mdl_sql/__init__.py`
- Create: `backend/app/query/mdl_sql/models.py`
- Create: `backend/app/query/mdl_sql/errors.py`
- Create: `backend/app/query/mdl_sql/catalog.py`
- Create: `backend/app/query/mdl_sql/context/__init__.py`
- Create: `backend/app/query/mdl_sql/context/relationships.py`
- Test: `backend/tests/query/test_mdl_catalog.py`

**Interfaces:**
- Produces: immutable `MdlColumn`, `MdlModel`, `MdlCube`, `MdlRelationship`, `MdlSnapshot`, `RelationshipPath`, `RelationshipSubgraph`, `CompilerFailure`, `MdlSqlCandidate`, `DryPlanResult`, and `MdlSqlPipelineResult`.
- Produces: `MdlSqlConfigurationError`, `MdlSqlRelationshipError`, `MdlSqlContractError`, `MdlSqlContextError`, `MdlSqlFallbackError`, and `MdlSqlPipelineError`.
- Produces: immutable `FAILURE_CODES` and `failure_message(failure: CompilerFailure) -> str`.
- Produces: `load_mdl_snapshot(kind: str, project_dir: Path | None = None) -> MdlSnapshot`.
- Produces: `build_relationship_subgraph(snapshot: MdlSnapshot, root_model: str, required_models: tuple[str, ...], required_relationship_ids: tuple[str, ...], max_hops: int) -> RelationshipSubgraph`.
- Failure: `build_relationship_subgraph` raises `MdlSqlRelationshipError("relationship_path_ambiguous", evidence)` when more than one distinguishable simple path remains.

- [ ] **Step 1: Write the failing catalog tests**

Test that the real MySQL project loads all current models, cubes, and relationships; model name
lookups are exact; calculated columns retain their expression and description; and the snapshot is
recursively read-only.

Test that a temporary MDL project changes `MdlSnapshot.fingerprint` when a model, cube,
`overrides.yml`, `relationships.yml`, or `target/mdl.json` changes.

Test relationship parsing with `sqlglot`:

```python
self.assertEqual(
    snapshot.relationships["disease_route"].field_pairs,
    (("disease.routes_id", "route.id"),),
)
```

For every current MySQL relationship, assert the snapshot's parsed `field_pairs` match the facts
returned by `wren_engine._relationship_columns()`. This is a parity test only; the new production
path parses relationship conditions with `sqlglot`.

Assert `FAILURE_CODES` exactly equals the canonical registry and that every code has one concise
Chinese explanation; unknown codes must fail closed rather than leak an English exception name.

Test a two-hop unique path, an ambiguous two-path graph, a cycle without an unbounded search,
an explicitly required relationship ID, and a required model that cannot be reached within `max_hops`.

- [ ] **Step 2: Run the catalog tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_catalog.py -q
```

Expected: FAIL because the `mdl_sql` package does not exist.

- [ ] **Step 3: Implement the snapshot wrapper**

`load_mdl_snapshot` must call `wren_engine._load_mdl()` and use the existing schema index as its
only MDL source. Preserve models, cubes, calculated columns, and relationships. Do not duplicate YAML
parsing or model lists.

Build the fingerprint from sorted relative path plus bytes for:

```text
wren_project.yml
overrides.yml
relationships.yml
models/**/*.yml
cubes/**/*.yml
target/mdl.json
```

Parse relationship equality conditions with `sqlglot`, not regex. Preserve the original condition,
join type, cardinality, and source. Freeze every mapping and tuple in the dataclasses.

- [ ] **Step 4: Implement bounded relationship-path search**

Use breadth-first search over the relationship graph. Return one deterministic subgraph when a
single simple path connects the root to each required model. Treat IDs from `required_relationship_ids`
as fixed contract edges and only search for the remaining missing connections. Deduplicate identical
edges. If two different unconstrained edge sequences remain, raise the structured ambiguity failure;
do not choose by score, name similarity, model order, or LLM inference.

- [ ] **Step 5: Run the catalog tests**

Run the command from Step 2.

Expected: PASS, including ambiguity and maximum-hop failures.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/query/mdl_sql/__init__.py backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/errors.py backend/app/query/mdl_sql/catalog.py backend/app/query/mdl_sql/context/__init__.py backend/app/query/mdl_sql/context/relationships.py backend/tests/query/test_mdl_catalog.py
git commit -m "feat: add immutable mdl snapshot and relationship graph"
```

---

### Task 3: Build One Immutable Contract per QuerySpec Node

**Files:**
- Modify: `backend/app/query/mdl_sql/models.py`
- Create: `backend/app/query/mdl_sql/contract_builder.py`
- Test: `backend/tests/query/test_mdl_contract_builder.py`

**Interfaces:**
- Produces: `MdlFieldBinding`, `MdlFilterBinding`, `MdlMetricBinding`, `MdlOutputBinding`, `MdlOrderBinding`.
- Produces: immutable `MdlQueryContract`, including `contract_id`, `plan_id`, `node_id`, all three hashes/fingerprints, subject, view, root model, bindings, outputs, ordering, limit, assertions, allowed models, required relationship IDs, mandatory predicates, and output aliases.
- Produces: `build_mdl_query_contract(spec: QuerySpec, registry: CapabilityRegistry, snapshot: MdlSnapshot, *, plan_id: str = "") -> MdlQueryContract`.
- Failure: `MdlSqlContractError` with a stable code and structured evidence.

- [ ] **Step 1: Write the failing contract tests**

Assert that each of the eleven subjects can build at least one legal `count` contract.

Assert that these two specs produce different root models, relationship sets, and contract hashes:

```python
section_spec = QuerySpec(
    id="q1",
    kind="mysql",
    subject="section",
    view="list",
    metrics=(),
    dimensions=(),
    filters=(binding("route_level", "=", "0"),),
    output_projection=("id", "route_name"),
    order_by=(),
    limit=100,
    required_assertions=("preserve_user_scope",),
    question="行政等级为国道的路段有哪些",
)

disease_spec = QuerySpec(
    id="q2",
    kind="mysql",
    subject="disease",
    view="count",
    metrics=("count",),
    dimensions=(),
    filters=(binding("route_level", "=", "0"),),
    output_projection=("count",),
    order_by=(),
    limit=None,
    required_assertions=("preserve_user_scope",),
    question="统计国道路段中检出的病害",
)
```

Assert stable hashes for identical spec/config/MDL facts, hash changes after any contract-authoritative
field changes, rejection of unavailable fields/metrics/views, and mandatory soft-delete predicates
only when the relevant role exists. Two specs with identical semantic fields but different narrative
`question` text must produce the same contract hash.

- [ ] **Step 2: Run the contract tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_contract_builder.py -q
```

Expected: FAIL because the contract models and builder do not exist.

- [ ] **Step 3: Implement contract projection**

Use `registry.snapshot.subjects[spec.subject].mdl_model` for the root. Resolve every non-root
field through its physical table and the snapshot's exact physical-table-to-model index. Resolve
metrics and dimensions from the subject capability. Parse capability expressions with `sqlglot`
and replace supported aliases with the resolved MDL model names.

Use `filter_expression` for filters and ordinary `expression` for dimensions/outputs. Capture
`source` on every binding. Build mandatory predicates from registered soft-delete roles. Use
`scope_hash(spec)` as `query_spec_hash`, `registry.snapshot.config_hash` as the capability hash,
and `snapshot.fingerprint` as the MDL fingerprint.

Derive `group_by` and `order_by` from the validated view contract. Normalize `top` with the
existing shape contract before building. Do not reference `spec.subject == "disease"` or
`spec.subject == "patrol"` anywhere.

- [ ] **Step 4: Build relationship facts and the final hash**

Collect required models and declared `requires_join` IDs from field/metric bindings. Call the Task 2
relationship subgraph builder with both sets.
Store allowed models, relationship IDs, and intermediate models in the contract. Compute
`contract_hash` from the deterministic JSON representation after all facts are complete.

- [ ] **Step 5: Run the contract tests**

Run the command from Step 2.

Expected: PASS, including both root-subject cases and stable hashes.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/contract_builder.py backend/tests/query/test_mdl_contract_builder.py
git commit -m "feat: build immutable mdl query contracts"
```

---

### Task 4: Build Narrow Context Views with Hard Budgets

**Files:**
- Modify: `backend/app/query/mdl_sql/models.py`
- Create: `backend/app/query/mdl_sql/context/capabilities.py`
- Create: `backend/app/query/mdl_sql/context/mdl.py`
- Modify: `backend/app/query/mdl_sql/context/relationships.py`
- Create: `backend/app/query/mdl_sql/context/prompt_context.py`
- Test: `backend/tests/query/test_mdl_context.py`

**Interfaces:**
- Produces: immutable `CapabilityContext`, `MdlContext`, `PromptContextBundle`.
- Produces: `build_capability_context(contract, capability_snapshot) -> CapabilityContext`.
- Produces: `build_mdl_context(contract, mdl_snapshot) -> MdlContext`.
- Produces: `build_prompt_context(contract, registry, snapshot) -> PromptContextBundle`.
- Produces: `serialize_prompt_context(context, *, max_context_bytes: int) -> str`.
- Failure: `MdlSqlContextError("context_budget_exceeded", evidence)`.

- [ ] **Step 1: Write the failing context tests**

Assert a capability context contains only referenced fields, metrics, derived operands, enum values,
entity members, and applicable assertions. Assert unrelated fields and the complete capability
registry are absent.

Assert an MDL context contains only the root, required models, referenced columns, calculated
expressions, join keys, and permitted relationships. Assert it contains no physical database or
physical table names.

Test:

```python
self.assertNotIn("basic_facilities_sections", serialized)
self.assertNotIn("new_four_road_base_ai", serialized)
```

Assert complete enum members appear only with `complete_enum_dimensions`; an entity family includes
only the referenced family and its members; and a context one byte over the configured limit raises
`context_budget_exceeded` without returning truncated JSON.

- [ ] **Step 2: Run the context tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_context.py -q
```

Expected: FAIL because the context builders do not exist.

- [ ] **Step 3: Implement contract-based selection**

Select capabilities by contract references, never by the original question. Include only the
referenced filter/dimension/metric/output/order facts plus their registered dependencies.
Preserve code versus display values. Include only the required assertion rules.

Build the MDL context from resolved model and column bindings. Include computed-column expressions
as MDL facts, but remove `physical_table`, database name, and all physical table metadata from the
prompt projection.

- [ ] **Step 4: Implement deterministic serialization and budget failure**

Serialize with `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))`.
Measure UTF-8 bytes. Allow deterministic removal of optional model descriptions in the order fixed
by the spec. Never remove or partially serialize a required contract field, enum, expression,
relationship, or binding. If required facts still exceed the limit, raise the structured failure.

- [ ] **Step 5: Run the context tests**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/context/capabilities.py backend/app/query/mdl_sql/context/mdl.py backend/app/query/mdl_sql/context/relationships.py backend/app/query/mdl_sql/context/prompt_context.py backend/tests/query/test_mdl_context.py
git commit -m "feat: build bounded mdl prompt context"
```

---

### Task 5: Parse MDL-SQL and Enforce Safety with AST

**Files:**
- Create: `backend/app/query/mdl_sql/audit/__init__.py`
- Create: `backend/app/query/mdl_sql/audit/failures.py`
- Create: `backend/app/query/mdl_sql/audit/ast.py`
- Create: `backend/app/query/mdl_sql/audit/safety_audit.py`
- Modify: `backend/app/query/mdl_sql/models.py`
- Test: `backend/tests/query/test_mdl_sql_audit.py`

**Interfaces:**
- Produces: `SqlIssue`, `SqlAuditReport`, and immutable `ParsedMdlSql`.
- Produces: `parse_mdl_sql(sql: str, kind: str) -> ParsedMdlSql`.
- Produces: `audit_safety(parsed: ParsedMdlSql, contract: MdlQueryContract, snapshot: MdlSnapshot, policy: MdlCompilationPolicy) -> tuple[SqlIssue, ...]`.
- Failure codes: `sql_parse_failed`, `multiple_statements`, `forbidden_sql_type`, `physical_table_reference`, `dangerous_function`, `star_projection`, `soft_delete_missing`.

- [ ] **Step 1: Write the failing AST/safety tests**

Cover one `SELECT`, one `WITH ... SELECT`, two statements, `INSERT`, `UPDATE`, `DELETE`, `DROP`,
a physical table reference, a dangerous function, `SELECT *`, and a missing registered soft-delete
predicate.

Assert `ParsedMdlSql` records root query type, table aliases, qualified columns, joins, projections,
group expressions, order expressions, and limit from the AST rather than string scans.

- [ ] **Step 2: Run the AST/safety tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_sql_audit.py -q
```

Expected: FAIL because the AST and safety modules do not exist.

- [ ] **Step 3: Implement strict AST parsing**

Use `sqlglot.parse()` and require exactly one `exp.Select` or one top-level query whose final
projection is a `SELECT`; reject other roots. Use `mysql` or `postgres` dialect from `kind`.
Do not strip Markdown, repair SQL, or execute anyway. Produce deterministic issue codes and JSON paths.

- [ ] **Step 4: Implement safety checks**

Compare table names to `snapshot.models` and `snapshot.physical_table_to_model`. A name that is an
allowed MDL model is permitted even when its name equals a physical table; a physical-only table is
rejected. Reject unregistered dangerous functions from the parsed policy. Detect required soft-delete
roles from contract predicates and require corresponding AST predicates.

- [ ] **Step 5: Run the AST/safety tests**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/audit/__init__.py backend/app/query/mdl_sql/audit/failures.py backend/app/query/mdl_sql/audit/ast.py backend/app/query/mdl_sql/audit/safety_audit.py backend/tests/query/test_mdl_sql_audit.py
git commit -m "feat: add mdl sql ast and safety audit"
```

---

### Task 6: Audit Contract Scope, Relationships, View Shape, Ordering, and Limit

**Files:**
- Create: `backend/app/query/mdl_sql/audit/contract_audit.py`
- Create: `backend/app/query/mdl_sql/audit/relationship_audit.py`
- Modify: `backend/app/query/mdl_sql/audit/__init__.py`
- Test: `backend/tests/query/test_mdl_sql_audit.py`

**Interfaces:**
- Produces: `audit_contract(parsed: ParsedMdlSql, contract: MdlQueryContract) -> tuple[SqlIssue, ...]`.
- Produces: `audit_relationships(parsed: ParsedMdlSql, contract: MdlQueryContract, subgraph: RelationshipSubgraph) -> tuple[SqlIssue, ...]`.
- Produces: `audit_mdl_sql(candidate: MdlSqlCandidate, contract: MdlQueryContract, subgraph: RelationshipSubgraph, snapshot: MdlSnapshot, policy: MdlCompilationPolicy) -> SqlAuditReport`.

- [ ] **Step 1: Write the failing contract/relationship tests**

For both a root-`section` contract and a root-`disease` contract, verify acceptance of matching
SQL and rejection of the other root.

Add one test for each shared rule:

- missing contract filter (`missing_filter`);
- extra business filter (`extra_filter`);
- wrong storage value (`filter_value_mismatch`);
- wrong qualified field (`unexpected_field`);
- wrong or unauthorized model (`unexpected_model`);
- illegal relationship (`illegal_relationship`);
- wrong relationship field pair (`illegal_relationship`);
- `JOIN` that can multiply rows without the permitted `EXISTS`/aggregate shape;
- aggregate metric replaced by detail (`invalid_view_shape`);
- distribution/trend/top-groups without grouping (`invalid_view_shape`);
- missing required output and wrong alias (`missing_output`);
- maximum/minimum/top without order (`order_required`);
- order direction mismatch (`invalid_view_shape`);
- contract `limit` changed or enlarged (`invalid_limit`);
- `COUNT(DISTINCT ...)` replaced by `COUNT(...)`.

- [ ] **Step 2: Run the audit tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_sql_audit.py -q
```

Expected: FAIL because the contract and relationship auditors do not exist.

- [ ] **Step 3: Implement canonical contract comparison**

Normalize predicates, qualified fields, projections, grouping, ordering, and limits into canonical
AST structures. Compare against contract bindings after separating required mandatory predicates.
Reject extra business predicates. Do not compare raw SQL text.

Implement view-specific assertions for `count`, `sum`, `avg`, `distribution`, `trend`,
`top_records`, `top_groups`, and detail/list. Support `top_per_outer_group` through a permitted
window/rank shape.

- [ ] **Step 4: Implement relationship and cardinality audit**

Map table aliases to MDL models. Every `JOIN` must use a relationship ID in the contract subgraph and
an ON field pair from that relationship. Detect cross joins and unconstrained joins. When joining a
one-to-many edge only to test existence or aggregate a child measure, require the contract-supported
aggregation or `EXISTS` shape.

- [ ] **Step 5: Compose the final report**

`audit_mdl_sql` must run parse, contract, relationship, and safety audits and return
`accepted = not issues`. It may never infer success from a non-empty SQL string alone.

- [ ] **Step 6: Run the audit tests**

Run the command from Step 2.

Expected: PASS, including the Review Focus case where LLM SQL changes contract scope.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/query/mdl_sql/audit/contract_audit.py backend/app/query/mdl_sql/audit/relationship_audit.py backend/app/query/mdl_sql/audit/__init__.py backend/tests/query/test_mdl_sql_audit.py
git commit -m "feat: audit mdl sql against query contracts"
```

---

### Task 7: Replace Subject Branches with a Contract Compiler

**Files:**
- Modify: `backend/app/query/mdl_sql/models.py`
- Create: `backend/app/query/mdl_sql/compiler/__init__.py`
- Create: `backend/app/query/mdl_sql/compiler/deterministic.py`
- Test: `backend/tests/query/test_mdl_deterministic_compiler.py`
- Test: `backend/tests/query/test_spec_mdl.py`
- Test: `backend/tests/query/test_shared_execution_contracts.py`

**Interfaces:**
- Produces: `compile_contract_to_mdl_sql(contract: MdlQueryContract, context: PromptContextBundle) -> MdlSqlCandidate`.
- Produces: `CompilerFailure` with stage `deterministic`.
- Failure codes: `compiler_not_covered` and `compiler_exception`.

- [ ] **Step 1: Write the failing generic compiler tests**

Build contracts for all eleven subjects and assert that simple `count`, `list`, `distribution`,
`top_records`, `top_groups`, and `trend` shapes produce a non-empty single-query candidate with
`source="deterministic"`.

Assert the same route-level filter creates:

```text
FROM section
```

for the section question and:

```text
FROM disease ... JOIN route ... WHERE route.level = '0'
```

for the disease question.

Assert no production source file under `mdl_sql/compiler/` contains subject equality branches.
Preserve the semantic assertions in `test_spec_mdl.py`, `test_shared_execution_contracts.py`, and
`test_segment_patrol_capability.py`; convert raw string assertions to AST assertions only where
formatting is irrelevant.

- [ ] **Step 2: Run the focused tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_deterministic_compiler.py backend/tests/query/test_spec_mdl.py backend/tests/query/test_shared_execution_contracts.py -q
```

Expected: FAIL because the deterministic contract compiler does not exist and the old compiler
still branches on `disease` and `patrol`.

- [ ] **Step 3: Implement AST-based SQL assembly**

Build select expressions, joins, predicates, grouping, ordering, and limit from contract objects.
Parse capability expressions with `sqlglot`; substitute model aliases structurally; encode literal
values as AST literals. Never concatenate untrusted values into SQL text.

Implement generic view shapes from contract metadata. Preserve output aliases and display labels.
Move remaining disease/patrol-specific formulas into `resources/capabilities/fields.yaml`,
`dimensions.yaml`, or `metrics.yaml` before deleting their Python helpers. Do not duplicate those
mappings.

- [ ] **Step 4: Run the compiler tests**

Run the command from Step 2.

Expected: PASS. If a legacy formula is not represented by a capability fact, the test must expose
that configuration gap; add the fact centrally rather than adding a subject branch.

- [ ] **Step 5: Commit**

```powershell
git add resources/capabilities/fields.yaml resources/capabilities/dimensions.yaml resources/capabilities/metrics.yaml backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/compiler/__init__.py backend/app/query/mdl_sql/compiler/deterministic.py backend/tests/query/test_mdl_deterministic_compiler.py backend/tests/query/test_spec_mdl.py backend/tests/query/test_shared_execution_contracts.py backend/tests/query/test_segment_patrol_capability.py
git commit -m "feat: compile mdl sql from contracts"
```

---

### Task 8: Reuse Wren Dry-Plan and Physical SQL Checks

**Files:**
- Modify: `backend/app/services/wren_engine.py`
- Create: `backend/app/query/mdl_sql/dry_plan.py`
- Modify: `backend/app/query/mdl_sql/models.py`
- Test: `backend/tests/query/test_mdl_dry_plan.py`

**Interfaces:**
- Produces in `wren_engine.py`: `dry_plan_mdl_sql(kind: str, mdl_sql: str) -> tuple[str | None, str]`, containing only the existing engine `dry_plan` call and raw-error capture.
- Produces: `dry_plan_candidate(kind: str, candidate: MdlSqlCandidate, snapshot: MdlSnapshot) -> DryPlanResult`.
- Produces: `check_physical_sql(kind: str, physical_sql: str) -> DryPlanResult`.
- Failure codes: `dry_plan_failed` and `physical_sql_invalid`.

- [ ] **Step 1: Write the failing Wren adapter tests**

Mock the Wren engine to return a string, `{"sql": "..."}`, `{"dialect_sql": "..."}`, `None`, `{}`,
an empty string, and an exception. Only non-empty physical SQL is success.

Assert the existing `compile_mdl_sql_verbose()` delegates its raw engine call to
`dry_plan_mdl_sql()` and preserves prior behavior for its current callers.

Assert `static_check.check_sql()` problems become `physical_sql_invalid`, and that physical SQL is
returned only after an empty problem list.

- [ ] **Step 2: Run the dry-plan tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_dry_plan.py -q
```

Expected: FAIL because the structured adapter does not exist.

- [ ] **Step 3: Extract the narrow Wren engine call**

Add `dry_plan_mdl_sql` as the single raw engine boundary. Refactor
`compile_mdl_sql_verbose()` to call it after its legacy prechecks. The new MDL pipeline will perform
AST relationship audit before this boundary, so it calls `dry_plan_mdl_sql()` directly.

- [ ] **Step 4: Implement structured dry-plan results**

Convert exceptions and empty return values into `DryPlanResult` with generated MDL-SQL, raw error,
structured allowed models/columns from the snapshot, and contract hash. Never return plain error
prose as a physical SQL result.

After success, call `static_check.check_sql(physical_sql, kind)`. Only an empty problem list yields
accepted physical SQL.

Reuse `wren_engine._schema_index()` and `columns_of()` when adding allowed model/column facts to a
failure. Reuse `compile_error_hint()` for raw unknown-model/unknown-field diagnostics, then convert
that hint into `CompilerFailure.evidence`; do not expose the hint string as the only error.

- [ ] **Step 5: Run the dry-plan tests**

Run the command from Step 2.

Expected: PASS, including all empty/exception Review Focus cases.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/wren_engine.py backend/app/query/mdl_sql/dry_plan.py backend/app/query/mdl_sql/models.py backend/tests/query/test_mdl_dry_plan.py
git commit -m "feat: wrap wren dry plan for mdl pipeline"
```

---

### Task 9: Add One Contract-Bounded LLM MDL-SQL Fallback

**Files:**
- Create: `prompts/mdl_sql_generator.md`
- Create: `backend/app/query/mdl_sql/compiler/llm_fallback.py`
- Modify: `backend/app/query/mdl_sql/models.py`
- Test: `backend/tests/query/test_mdl_llm_fallback.py`
- Test: `backend/tests/query/test_prompt_contracts.py`

**Interfaces:**
- Produces: `generate_mdl_sql_once(context: PromptContextBundle, failure: CompilerFailure, *, model: dict[str, Any] | None = None) -> MdlSqlCandidate`.
- Consumes: `prompts.messages("mdl_sql_generator", ...)` and `llm.chat(...)`.
- Failure: `MdlSqlFallbackError` with `llm_sql_invalid` or provider/configuration evidence.

- [ ] **Step 1: Write the failing fallback tests**

Assert `llm.chat` is called exactly once, never `llm.chat_json`, because `chat_json` has an internal
JSON retry.

Test accepted JSON:

```json
{
  "mdl_sql": "SELECT COUNT(*) AS 数量 FROM disease",
  "used_models": ["disease"],
  "used_relationship_ids": []
}
```

Reject Markdown fences, prose, an empty SQL string, a JSON array, a list instead of string model
names, and a payload containing two SQL statements. Add a schema-valid reply whose declared model
or relationship IDs differ from the AST; the pipeline must later reject it with
`llm_declared_scope_mismatch`.

Test LLM transport categories and require structured infrastructure codes:

```text
rate_limited        -> llm_rate_limited
quota/balance        -> llm_quota_exhausted
transport            -> llm_transport_error
provider unavailable -> llm_provider_unavailable
```

These failures must not be passed to a semantic judge as a wrong answer.

Add a prompt-contract test that requires the prompt to state QuerySpec/contract are read-only, only
the supplied MDL facts may be used, only one read-only SELECT/WITH query is allowed, and output is
strict JSON.

- [ ] **Step 2: Run the fallback tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_llm_fallback.py backend/tests/query/test_prompt_contracts.py -q
```

Expected: FAIL because the prompt and fallback generator do not exist.

- [ ] **Step 3: Create the prompt protocol**

Create `prompts/mdl_sql_generator.md` with `[system]` and `[user]` sections. The user section receives
the serialized `PromptContextBundle`. It must describe the contract as authoritative and forbids
changing or adding filters, models, metrics, ordering, or limits.

- [ ] **Step 4: Implement one-shot generation**

Call `llm.chat(..., model=model or role_model("sql"), max_tokens=4096)`. Parse using
`parse_json_lenient` once. Require exactly the three documented keys and correct value types.
Construct `MdlSqlCandidate(source="llm_fallback", contract_id=..., contract_hash=...)`.
Do not audit or execute here; the pipeline owns those stages. Convert `LLMError.category` and HTTP
status into the structured infrastructure codes from Step 1 without performing a semantic retry.

After AST audit, compare declared `used_models` and `used_relationship_ids` with the parsed sets.
Any difference is `llm_declared_scope_mismatch`; the declared JSON never overrides AST facts.

- [ ] **Step 5: Run the fallback tests**

Run the command from Step 2.

Expected: PASS and `chat` invocation count is exactly one in every malformed-output test.

- [ ] **Step 6: Commit**

```powershell
git add prompts/mdl_sql_generator.md backend/app/query/mdl_sql/compiler/llm_fallback.py backend/app/query/mdl_sql/models.py backend/tests/query/test_mdl_llm_fallback.py backend/tests/query/test_prompt_contracts.py
git commit -m "feat: add one-shot mdl sql fallback"
```

---

### Task 10: Orchestrate the Pipeline and Migrate the Production Compiler

**Files:**
- Create: `backend/app/query/mdl_sql/compiler/pipeline.py`
- Modify: `backend/app/query/mdl_sql/__init__.py`
- Modify: `backend/app/query/compiler.py`
- Modify: `backend/app/query/service.py`
- Replace: `backend/app/query/spec_mdl.py`
- Test: `backend/tests/query/test_mdl_sql_pipeline.py`
- Modify: `backend/tests/query/test_service.py`
- Modify: `backend/tests/query/test_compiler_authority.py`
- Modify: `backend/tests/query/test_legacy_query_chain_removed.py`

**Interfaces:**
- Produces: `run_mdl_sql_pipeline(spec: QuerySpec, *, plan_id: str = "", kind: str | None = None, registry: CapabilityRegistry | None = None, mdl_snapshot: MdlSnapshot | None = None, generate_mdl_sql: Callable[..., MdlSqlCandidate] | None = None) -> MdlSqlPipelineResult`.
- Produces: `MdlSqlPipelineError(RuntimeError)` carrying `failure: CompilerFailure`.
- Produces: `compile_queryspec_to_physical(spec, *, plan_id="") -> str`.
- Changes: `compile_query(..., plan_id: str = "")`; `_default_wren_executor(spec, *, plan_id="") -> str`.
- Compatibility: `spec_mdl.compile_spec_mdl(spec) -> str | None` delegates to the deterministic contract compiler and contains no business branches.

- [ ] **Step 1: Write the failing pipeline tests**

Test the exact fallback matrix:

```text
contract/context/relationship failure -> no LLM call
deterministic compile failure        -> one LLM call
deterministic audit failure          -> one LLM call
deterministic dry-plan failure       -> one LLM call
deterministic physical check failure -> one LLM call
deterministic success                -> no LLM call
LLM audit/dry-plan/physical failure  -> stop, no second free rewrite
```

Add a `post_calc` node and assert it creates no `MdlQueryContract`, performs no MDL compile, and
never calls the SQL fallback.

Add a two-node plan where one node succeeds deterministically and the other fails fallback; assert
each node has its own contract and final root. Add a failing node with an unrelated sibling and one
dependent child; assert the sibling executes and only the child is `blocked`.

Assert QuerySpec-invalid input never reaches fallback, and every final failure keeps stage, code,
contract hash, generated MDL-SQL, and safe evidence.

- [ ] **Step 2: Run the pipeline tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_sql_pipeline.py backend/tests/query/test_service.py backend/tests/query/test_compiler_authority.py backend/tests/query/test_legacy_query_chain_removed.py -q
```

Expected: FAIL because the pipeline, structured failures, and production integration do not exist.

- [ ] **Step 3: Implement stage transitions**

For one `query_data` node:

1. Build the contract.
2. Build all three narrowed context views and serialize within budget.
3. Run deterministic compile, AST audit, dry-plan, and physical check.
4. On an eligible deterministic-stage failure, call the fallback exactly once.
5. Run the same audit/dry-plan/physical stages on the fallback candidate.
6. Return only accepted physical SQL and candidate evidence.

No exception text or non-empty reply can create success.

- [ ] **Step 4: Integrate with service execution**

Compute a stable `plan_id` in `_execute_plan` and pass it through `compile_query` to
`_default_wren_executor`. Catch `MdlSqlPipelineError` before generic `RuntimeError` handling so the
section keeps `failure_code`, `failure_stage`, `contract_hash`, and safe evidence. Preserve independent
node execution and existing dependency blocking. Use `failure_message()` for the section answer and
keep the structured evidence separate; never display only the internal exception or an English view name.

- [ ] **Step 5: Migrate `spec_mdl.py` and remove the old implementation**

Move `UnsupportedQuerySpec` to a compatibility export backed by the new structured failure type.
Replace all old helper functions in `spec_mdl.py` with the compatibility wrapper defined in Interfaces.
Delete the disease/patrol filters, dimensions, metrics, outputs, ordering, and JOIN branches from the
file. Do not leave duplicate mappings in Python.

- [ ] **Step 6: Run the pipeline and migrated compiler tests**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_sql_pipeline.py backend/tests/query/test_service.py backend/tests/query/test_compiler_authority.py backend/tests/query/test_legacy_query_chain_removed.py backend/tests/query/test_spec_mdl.py -q
```

Expected: PASS. Update only obsolete mocks that encode the removed two-branch implementation; do not
weaken semantic SQL assertions.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/query/mdl_sql/compiler/pipeline.py backend/app/query/mdl_sql/__init__.py backend/app/query/compiler.py backend/app/query/service.py backend/app/query/spec_mdl.py backend/tests/query/test_mdl_sql_pipeline.py backend/tests/query/test_service.py backend/tests/query/test_compiler_authority.py backend/tests/query/test_legacy_query_chain_removed.py backend/tests/query/test_spec_mdl.py
git commit -m "feat: run query specs through audited mdl pipeline"
```

---

### Task 11: Lock Multi-Subject and Failure Isolation End to End

**Files:**
- Create: `backend/tests/query/test_mdl_sql_e2e.py`
- Modify: `backend/tests/query/test_capability_e2e.py`
- Modify: `backend/tests/query/test_shared_execution_contracts.py`
- Artifact: fresh run under `backend/evals/promotion/runs/`

**Interfaces:**
- Consumes all interfaces from Tasks 1-10.
- Produces an end-to-end contract test matrix for every registered subject and each supported basic view.

- [ ] **Step 1: Write the failing end-to-end matrix**

For each of the eleven subjects, build a legal QuerySpec and assert:

```text
MdlQueryContract.root_model == subject mdl_model
deterministic or one fallback candidate exists
AST audit accepted
dry-plan result represented explicitly
physical SQL accepted only when all checks pass
```

Add representative `count`, `list`, `distribution`, `trend`, `top_records`, and `top_groups` cases.
Add the section-versus-disease pair and assert both remain present with their own roots.

- [ ] **Step 2: Run the end-to-end tests to verify failure**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query/test_mdl_sql_e2e.py backend/tests/query/test_capability_e2e.py backend/tests/query/test_shared_execution_contracts.py -q
```

Expected: FAIL for any subject whose capability field, metric, relationship, or view still cannot
produce an audited candidate.

- [ ] **Step 3: Fix shared capability gaps**

Add missing fields, metrics, dimensions, MDL mappings, or relationship declarations only in
`resources/capabilities/` or the Wren MDL facts. Re-run the matrix by capability family. Do not add
question-specific branches or test-ID mappings.

- [ ] **Step 4: Run the complete backend query suite**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/query -q
```

Expected: all tests pass.

- [ ] **Step 5: Run syntax and frontend checks**

Run:

```powershell
backend\.venv\Scripts\python.exe -m compileall -q backend/app
npm run build
git diff --check
```

Run the frontend command with working directory `frontend`.

Expected: no syntax, build, or whitespace errors.

- [ ] **Step 6: Commit**

```powershell
git add backend/tests/query/test_mdl_sql_e2e.py backend/tests/query/test_capability_e2e.py backend/tests/query/test_shared_execution_contracts.py
git commit -m "test: cover mdl pipeline across registered subjects"
```

If Step 3 legitimately changed capability or MDL facts, add those exact changed paths to the same
commit after listing them with `git status --short`; never stage either directory wholesale.

---

### Task 12: Run a Fresh Promotion Exam

**Files:**
- No additional implementation files.
- Artifact: new manifest under `backend/evals/promotion/runs/`.
- Artifact: matching `.report.json`.

**Interfaces:**
- Consumes: the final code fingerprint.
- Produces: one first-run exam with 15 never-used fresh questions and 5 randomly selected old questions.

- [ ] **Step 1: Generate a fresh exam**

From `backend`, run the existing promotion CLI with a new timestamped output and a recorded seed:

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m evals.promotion generate --output evals/promotion/runs/2026-11-10-query-spec-mdl-sql.yaml --seed 20261110
```

Confirm the manifest contains exactly 15 fresh cases, five old cases, the business-document hash,
the current code fingerprint, and the seed.

- [ ] **Step 2: Run the first attempt once**

From `backend`, run:

```powershell
$env:PYTHONUTF8 = "1"
.venv\Scripts\python.exe -m evals.promotion run --cases evals/promotion/runs/2026-11-10-query-spec-mdl-sql.yaml --report evals/promotion/runs/2026-11-10-query-spec-mdl-sql.report.json
```

Expected promotion thresholds:

```text
fresh >= 12/15
old   >= 4/5
total >= 18/20
```

- [ ] **Step 3: Diagnose only shared failure layers**

If the first attempt misses a threshold, classify every failure by contract build, context,
relationship, deterministic compile, audit, dry-plan, physical check, execution, verification, or
expression. Fix only a repeated shared contract defect. Any code change invalidates this exam.

Do not add a question-specific rule, mapping, keyword, regex, or template for a failed case.
If the blocker is model quota, rate limiting, transport, or provider outage, mark the run as
infrastructure-blocked and do not present it as a business score.

- [ ] **Step 4: If code changed, regenerate and rerun**

After any business-code, prompt, capability, or MDL change, generate a new exam with a new filename
and seed. The prior manifest is diagnostic evidence only and cannot be reused for promotion.

- [ ] **Step 5: Record the final evidence**

Keep the passing manifest/report or the infrastructure-blocked report. Confirm they record fresh and
old IDs, seed, code fingerprint, per-question status, failure layer, and totals.

- [ ] **Step 6: Commit**

```powershell
git add backend/evals/promotion/runs/2026-11-10-query-spec-mdl-sql.yaml backend/evals/promotion/runs/2026-11-10-query-spec-mdl-sql.report.json
git commit -m "test: record fresh mdl pipeline promotion exam"
```

Do not push. Report the exam result and commit hashes to the user.

---

## Final Verification Checklist

- [ ] Every registered subject has an explicit MDL model mapping and can build a contract.
- [ ] A plan with multiple query nodes builds one contract per node and preserves different final subjects.
- [ ] Unrelated siblings continue after one node fails; only actual dependants become `blocked`.
- [ ] Context selection contains only contract-referenced business and MDL facts.
- [ ] Context overflow fails with `context_budget_exceeded` and no truncation.
- [ ] Ambiguous relationship paths fail before LLM invocation.
- [ ] Deterministic and fallback SQL use the identical audit and dry-plan path.
- [ ] LLM contract drift is rejected by AST audit.
- [ ] Empty/error/Markdown/multi-statement LLM replies never count as success.
- [ ] Wren empty output or exception never counts as success.
- [ ] Physical SQL passes `static_check.check_sql()` before execution.
- [ ] Old disease/patrol compiler branches are deleted; `spec_mdl.py` is only a compatibility wrapper.
- [ ] All query tests pass.
- [ ] Frontend build passes.
- [ ] Fresh promotion exam meets `12/15`, `4/5`, and `18/20`, or is reported as infrastructure-blocked.
