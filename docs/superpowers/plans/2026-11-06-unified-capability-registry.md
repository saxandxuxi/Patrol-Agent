# 统一能力注册表实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 Planner、Validator、Compiler 共用的统一能力注册表，修复 QuerySpec 字段触达、枚举解析、实体层级和伪造指标问题，并删除所有旧映射实现。

**Architecture:** 将人工维护的能力映射统一迁入 `resources/capabilities/`，运行时由 `CapabilityRegistry` 合并实时表结构、配置和动态实体字典，生成不可变能力快照。Planner 只拿到已确定主体的能力视图；Validator 和 Compiler 使用同一快照；未知能力通过结构化 FailureEvidence 进入最多两轮修复。

**Tech Stack:** Python 3.11、PyYAML、MySQL information_schema、unittest、FastAPI、Wren MDL

**Spec:** `docs/superpowers/specs/2026-11-06-unified-capability-registry-design.md`

## Global Constraints

- 所有人工维护的语义与能力映射只能放在 `resources/capabilities/`。
- 迁移某个映射后必须在同一任务删除原文件或原代码定义，禁止双写。
- Planner、Validator、Compiler 必须读取同一个 `CapabilitySnapshot`。
- 枚举按 `表 + 字段` 建模，不能只按列名注册。
- “裂缝”等业务实体族必须由配置和数据库字典共同校验，不能只用字符串包含关系决定 SQL。
- 用户只说“上下行”时展示上行、下行和“全部/未区分”三个独立类别。
- 未知指标、未知字段、错误主体指标和缺少派生操作数必须返回结构化 FailureEvidence。
- 修复轮数严格限制为两轮；失败后不得执行 SQL。
- `auto_templates.json`、`preset_queries.json`、日志、缓存和数据库实时结果不迁入能力配置目录。
- 所有任务遵循 TDD：失败测试、最小实现、通过测试、提交。

## Review Focus

- “工单编号中的病害”可能被解析成 disease 或 work_order 两个主体；SubjectResolver 必须唯一解析或返回确认卡。
- “裂缝”可能同时命中实体族和真实同名成员；解析必须优先精确成员，再展开实体族，不能直接把“裂缝”拼进 SQL。
- 同名 `up_or_down` 在不同表有不同 code_map；任何全局枚举缓存都可能导致错误。
- 模型输出 `disease_density` 时必须被注册表拒绝并收到派生指标修复建议，不能进入 SQL。
- 配置迁移后旧文件仍被某个工作流引用时会运行失败；每个迁移任务必须验证旧路径不存在且全量测试通过。

---

### Task 1: 能力注册表模型与加载器

**Files:**
- Create: `backend/app/query/capability_registry/__init__.py`
- Create: `backend/app/query/capability_registry/models.py`
- Create: `backend/app/query/capability_registry/loader.py`
- Create: `backend/app/query/capability_registry/registry.py`
- Create: `resources/capabilities/index.yaml`
- Create: `resources/capabilities/subjects.yaml`
- Create: `resources/capabilities/fields.yaml`
- Create: `resources/capabilities/field_overrides.yaml`
- Create: `resources/capabilities/metrics.yaml`
- Create: `resources/capabilities/derived_metrics.yaml`
- Create: `resources/capabilities/enums.yaml`
- Create: `resources/capabilities/entities.yaml`
- Create: `resources/capabilities/entity_hierarchies.yaml`
- Create: `resources/capabilities/aliases.yaml`
- Create: `resources/capabilities/identifiers.yaml`
- Create: `resources/capabilities/query_shapes.yaml`
- Create: `resources/capabilities/dimensions.yaml`
- Create: `resources/capabilities/predicates.yaml`
- Create: `resources/capabilities/time_buckets.yaml`
- Create: `resources/capabilities/result_words.yaml`
- Create: `resources/capabilities/repair_policy.yaml`
- Create: `resources/capabilities/templates.yaml`
- Test: `backend/tests/query/test_capability_registry.py`

**Interfaces:**
- Produces: `CapabilityRegistry.load(kind: str = "mysql", config_dir: Path | None = None) -> CapabilityRegistry`
- Produces: `CapabilityRegistry.snapshot -> CapabilitySnapshot`
- Produces: dataclasses `CapabilitySnapshot`, `SubjectCapability`, `FieldCapability`, `MetricCapability`, `DerivedMetricCapability`, `EnumCapability`, `EntityCapability`, `EntityFamilyCapability`
- Produces: `CapabilitySnapshot.version: str` and `CapabilitySnapshot.config_hash: str`

- [ ] **Step 1: Write failing registry loading tests**

```python
def test_registry_loads_all_capability_files():
    registry = CapabilityRegistry.load(config_dir=CAPABILITY_DIR)
    self.assertIn("disease", registry.snapshot.subjects)
    self.assertIn("count", registry.snapshot.metrics["disease"])
    self.assertIn("crack", registry.snapshot.entity_families)

def test_registry_rejects_unknown_config_key():
    with self.assertRaisesRegex(CapabilityConfigError, "unknown_key"):
        CapabilityRegistry.load(config_dir=BROKEN_CONFIG_DIR)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_registry -v
```

Expected: FAIL because `capability_registry` does not exist.

- [ ] **Step 3: Implement immutable models, strict YAML loader and registry snapshot**

The loader must reject unknown top-level keys, missing referenced files and duplicate capability IDs. `CapabilityRegistry.load()` validates that every configured physical field exists in `schema_catalog.field_catalog(kind)`.

- [ ] **Step 4: Run registry tests and existing query suite**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_registry tests.query.test_runtime_boundaries -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry resources/capabilities backend/tests/query/test_capability_registry.py
git commit -m "feat: add capability registry foundation"
```

---

### Task 2: 迁移静态资源并删除旧配置文件

**Files:**
- Modify: `backend/app/services/resource_loader.py`
- Modify all call sites using: `resources/lexicon/*`, `resources/field_meta/*`, `resources/query_*.yaml`, `resources/repair_policy.yaml`, `resources/slots/*`, `resources/sql_templates/_index.yaml`
- Modify: `resources/README.md`
- Delete: `resources/field_meta/code_map_overrides.json`
- Delete: `resources/lexicon/metrics.yaml`
- Delete: `resources/lexicon/result_words.yaml`
- Delete: `resources/lexicon/semantics.yaml`
- Delete: `resources/lexicon/trigger_words.yaml`
- Delete: `resources/query_capabilities.yaml`
- Delete: `resources/query_identifiers.yaml`
- Delete: `resources/repair_policy.yaml`
- Delete: `resources/slots/predicate_slots.yaml`
- Delete: `resources/sql_templates/_index.yaml`
- Test: `backend/tests/query/test_capability_config_migration.py`

**Interfaces:**
- Produces: `load_capability_file(name: str) -> Any`
- Consumes: `CapabilityRegistry` from Task 1
- Migration map:
  - `lexicon/semantics.yaml` → `subjects.yaml`, `fields.yaml`, `query_shapes.yaml`, `dimensions.yaml`
  - `lexicon/metrics.yaml` → `metrics.yaml`, `derived_metrics.yaml`, `dimensions.yaml`, `time_buckets.yaml`
  - `lexicon/trigger_words.yaml` → `aliases.yaml`
  - `lexicon/result_words.yaml` → `result_words.yaml`
  - `field_meta/code_map_overrides.json` → `field_overrides.yaml`, `enums.yaml`, `aliases.yaml`
  - `query_capabilities.yaml` → `templates.yaml`
  - `query_identifiers.yaml` → `identifiers.yaml`
  - `repair_policy.yaml` → `repair_policy.yaml`
  - `slots/predicate_slots.yaml` → `predicates.yaml`
  - `sql_templates/_index.yaml` → `templates.yaml`

- [ ] **Step 1: Write failing migration tests**

```python
def test_legacy_resource_files_are_deleted():
    for path in LEGACY_CAPABILITY_PATHS:
        self.assertFalse(path.exists(), str(path))

def test_legacy_resource_paths_are_not_referenced():
    output = subprocess.check_output(
        ["rg", "lexicon/|field_meta/|query_identifiers|slots/predicate_slots|sql_templates/_index", "backend/app"]
    )
    self.assertEqual(output, b"")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_config_migration -v
```

Expected: FAIL because legacy files and references still exist.

- [ ] **Step 3: Move and split configuration, update every loader call site**

Each migrated value must appear exactly once in the new directory. Do not leave compatibility copies.

- [ ] **Step 4: Run migration and query suites**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_config_migration tests.query.test_capability_registry -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS and legacy path scan returns no matches.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/resource_loader.py resources backend/tests/query/test_capability_config_migration.py
git commit -m "refactor: centralize capability configuration"
```

---

### Task 3: SubjectResolver、字段触达和 Planner 能力视图

**Files:**
- Create: `backend/app/query/capability_registry/subject_resolver.py`
- Create: `backend/app/query/capability_registry/planner_view.py`
- Modify: `backend/app/query/fields.py`
- Modify: `backend/app/query/service.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/services/agent.py`
- Test: `backend/tests/query/test_subject_capability_view.py`
- Test: `backend/tests/query/test_query_planning.py`

**Interfaces:**
- Produces: `resolve_subject(question: str, intent: str, reach: FieldReachSet, registry: CapabilityRegistry) -> SubjectResolution`
- Produces: `SubjectResolution(subject: str, confidence: float, alternatives: tuple[str, ...], evidence: tuple[str, ...])`
- Produces: `CapabilityRegistry.planner_view(subject: str, reach: FieldReachSet) -> PlannerCapabilityView`
- Produces: `PlannerCapabilityView.to_prompt_dict() -> dict[str, Any]`
- Consumes: Task 1 registry and Task 2 migrated `subjects.yaml`

- [ ] **Step 1: Write failing tests**

```python
def test_planner_view_has_comments_types_roles_and_enum_values():
    view = registry.planner_view("disease", FieldReachSet())
    up_down = view.fields["up_or_down"]
    self.assertEqual(up_down.label, "上下行")
    self.assertEqual(up_down.roles, ("code",))
    self.assertEqual(up_down.enum.values["0"].label, "全部")

def test_start_single_turn_passes_real_field_reach(monkeypatch):
    captured = {}
    monkeypatch.setattr(service, "analyze_single_turn", capture_reach)
    service.start_single_turn("西乡段泛油有多少处")
    self.assertTrue(captured["reach"].hits)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_subject_capability_view -v
```

Expected: FAIL because PlannerCapabilityView and SubjectResolver do not exist.

- [ ] **Step 3: Implement subject resolution, reach construction and closed planner view**

`start_single_turn()` must call `field_grounding.ground(kind, question)` and convert its matched columns into `FieldReachSet`. `_analyze_once()` must serialize `PlannerCapabilityView.to_prompt_dict()` instead of `sorted(schema.columns)`.

- [ ] **Step 4: Run focused tests and full query suite**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_subject_capability_view tests.query.test_query_planning -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry backend/app/query/fields.py backend/app/query/service.py backend/app/query/analysis.py backend/app/services/agent.py backend/tests/query/test_subject_capability_view.py backend/tests/query/test_query_planning.py
git commit -m "feat: pass capability-grounded context to planner"
```

---

### Task 4: 通用 EnumResolver，删除静态枚举字段名单

**Files:**
- Create: `backend/app/query/capability_registry/enum_resolver.py`
- Modify: `backend/app/query/values.py`
- Modify: `backend/app/query/validator.py`
- Modify: `backend/app/query/spec_mdl.py`
- Test: `backend/tests/query/test_enum_resolver.py`
- Test: `backend/tests/query/test_values_analysis.py`

**Interfaces:**
- Produces: `resolve_enum(registry: CapabilityRegistry, table: str, column: str, raw_value: Any) -> EnumResolution`
- Produces: `EnumResolution(status: Literal["resolved", "ambiguous", "unsupported"], values: tuple[EnumValue, ...], evidence: tuple[dict[str, Any], ...])`
- Consumes: `enums.yaml` and `field_overrides.yaml`

- [ ] **Step 1: Write failing enum tests**

```python
def test_up_or_down_resolves_per_table():
    disease = resolve_enum(registry, "ai_problem", "up_or_down", "全部")
    section = resolve_enum(registry, "basic_facilities_sections", "up_or_down", "双向")
    self.assertEqual(disease.values[0].code, "0")
    self.assertEqual(section.values[0].code, "3")

def test_all_code_map_columns_are_resolvable():
    for table, column in registry.snapshot.enums:
        sample = next(iter(registry.snapshot.enums[(table, column)].values.values()))
        result = resolve_enum(registry, table, column, sample.label)
        self.assertEqual(result.status, "resolved")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_enum_resolver -v
```

Expected: FAIL because `_CODE_FIELDS` still controls enum resolution.

- [ ] **Step 3: Implement generic resolver and remove `_CODE_FIELDS`**

`values.resolve_value()` must obtain enum definitions from `CapabilityRegistry`; no enum column names remain hardcoded in `values.py`.

- [ ] **Step 4: Run enum and full query tests**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_enum_resolver tests.query.test_values_analysis -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS, and `rg "_CODE_FIELDS" backend/app/query/values.py` returns no match.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry/enum_resolver.py backend/app/query/values.py backend/app/query/validator.py backend/app/query/spec_mdl.py backend/tests/query/test_enum_resolver.py backend/tests/query/test_values_analysis.py
git commit -m "feat: resolve enums through capability registry"
```

---

### Task 5: 实体层级与别名迁移，删除散落病害映射

**Files:**
- Create: `backend/app/query/capability_registry/entity_resolver.py`
- Modify: `backend/app/services/route_resolver.py`
- Modify: `backend/app/services/query_rewrite.py`
- Modify: `backend/app/services/nl2sql.py`
- Modify: `backend/app/services/sql_engine.py`
- Modify: `resources/capabilities/entities.yaml`
- Modify: `resources/capabilities/entity_hierarchies.yaml`
- Modify: `resources/capabilities/aliases.yaml`
- Test: `backend/tests/query/test_entity_resolver.py`
- Test: `backend/tests/query/test_decompose_consistency.py`

**Interfaces:**
- Produces: `resolve_entity(registry: CapabilityRegistry, entity: str, raw_value: Any) -> EntityResolution`
- Produces: `EntityResolution(status: Literal["resolved", "expanded", "ambiguous", "unsupported"], values: tuple[EntityValue, ...], family: str | None, evidence: tuple[dict[str, Any], ...])`
- Consumes: `entities.yaml`, `entity_hierarchies.yaml`, `aliases.yaml`

- [ ] **Step 1: Write failing entity tests**

```python
def test_crack_family_expands_to_real_dictionary_members():
    result = resolve_entity(registry, "disease_type", "裂缝")
    self.assertEqual(result.status, "expanded")
    self.assertTrue({"横向裂缝", "纵向裂缝", "网状裂缝"}.issubset(value.label for value in result.values))

def test_unknown_entity_is_not_written_to_sql():
    result = resolve_entity(registry, "disease_type", "不存在的病害")
    self.assertEqual(result.status, "unsupported")
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_entity_resolver -v
```

Expected: FAIL because entity resolution is still scattered.

- [ ] **Step 3: Implement EntityResolver and migrate aliases/hierarchies**

Delete `_DAMAGE_ALIASES`, `expand_problem_types()` and `expand_problem_type_filters()`. Replace their callers with registry-backed entity resolution. Remove duplicate alias maps from `query_rewrite.py`.

- [ ] **Step 4: Verify old mappings are gone and tests pass**

Run:

```powershell
cd backend
rg -n "_DAMAGE_ALIASES|expand_problem_types|expand_problem_type_filters|横缝.*横向裂缝" app
.\.venv\Scripts\python.exe -m unittest tests.query.test_entity_resolver tests.query.test_decompose_consistency -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: `rg` returns no matches; tests PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry/entity_resolver.py backend/app/services/route_resolver.py backend/app/services/query_rewrite.py backend/app/services/nl2sql.py backend/app/services/sql_engine.py resources/capabilities backend/tests/query/test_entity_resolver.py backend/tests/query/test_decompose_consistency.py
git commit -m "refactor: centralize entity hierarchy resolution"
```

---

### Task 6: 原子指标与派生指标统一编译

**Files:**
- Create: `backend/app/query/capability_registry/metric_registry.py`
- Create: `backend/app/query/metric_compiler.py`
- Modify: `backend/app/query/spec_mdl.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/services/field_catalog.py`
- Modify: `resources/capabilities/metrics.yaml`
- Modify: `resources/capabilities/derived_metrics.yaml`
- Test: `backend/tests/query/test_metric_registry.py`
- Test: `backend/tests/query/test_spec_mdl.py`

**Interfaces:**
- Produces: `CapabilityRegistry.metric(subject: str, metric_id: str) -> MetricCapability | None`
- Produces: `CapabilityRegistry.derived_metric(subject: str, metric_id: str) -> DerivedMetricCapability | None`
- Produces: `compile_registered_metric(subject: str, metric: MetricCapability, aliases: dict[str, str]) -> str`
- Consumes: Task 1 registry and Task 2 migrated metric files

- [ ] **Step 1: Write failing metric tests**

```python
def test_disease_density_is_derived_not_atomic():
    self.assertIsNone(registry.metric("disease", "disease_density"))
    self.assertIsNotNone(registry.derived_metric("disease", "disease_density"))

def test_every_registered_metric_compiles_for_declared_views():
    for subject, metrics in registry.snapshot.metrics.items():
        for metric in metrics.values():
            sql = compile_registered_metric(subject, metric, {"disease": "d"})
            self.assertTrue(sql)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_metric_registry -v
```

Expected: FAIL because metric metadata is still split between `field_catalog.metric_names()` and `spec_mdl`.

- [ ] **Step 3: Implement registry-backed metric compiler and remove competing metric sets**

Atomic metrics must compile only through `MetricCapability`. Derived metrics must be rejected by atomic compilation and accepted only by post_calc planning. Remove `field_catalog.metric_names()` or reduce it to a compatibility adapter that delegates to the registry.

- [ ] **Step 4: Run metric and full tests**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_metric_registry tests.query.test_spec_mdl -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry/metric_registry.py backend/app/query/metric_compiler.py backend/app/query/spec_mdl.py backend/app/query/analysis.py backend/app/services/field_catalog.py resources/capabilities backend/tests/query/test_metric_registry.py backend/tests/query/test_spec_mdl.py
git commit -m "feat: compile metrics through capability registry"
```

---

### Task 7: CapabilityFailure 与两轮修复闭环

**Files:**
- Create: `backend/app/query/capability_registry/failures.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/validator.py`
- Modify: `backend/app/services/react_repair.py`
- Test: `backend/tests/query/test_capability_repair.py`
- Test: `backend/tests/query/test_query_audit.py`

**Interfaces:**
- Produces: `CapabilityIssue(code: str, original_value: str, reason: str, allowed_values: tuple[str, ...], suggested_repair: str, source: str)`
- Produces: `to_failure_evidence(issue: CapabilityIssue, node_id: str) -> FailureEvidence`
- Consumes: `repair_policy.yaml`

- [ ] **Step 1: Write failing repair-loop tests**

```python
def test_unknown_metric_retries_once_with_allowed_values():
    plans = [plan_with_fake_metric(), plan_with_supported_metric()]
    result = analyze_single_turn_with_mock_plans(plans)
    self.assertEqual(plan_mock.call_count, 2)
    self.assertIn("allowed_metrics", second_feedback)
    self.assertEqual(result.nodes[0].spec.metrics, ("count",))

def test_unknown_metric_stops_after_two_repairs():
    with self.assertRaisesRegex(QuerySpecError, "不支持"):
        analyze_single_turn_with_mock_plans([plan_with_fake_metric()] * 3)
    self.assertEqual(plan_mock.call_count, 3)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_repair -v
```

Expected: FAIL because current compile errors are reduced to strings and the attempt count is not the specified two repair rounds.

- [ ] **Step 3: Implement structured capability failures and bounded repair**

The planner is called once initially and at most twice more. SQL execution is impossible until capability validation passes.

- [ ] **Step 4: Run repair tests and full suite**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_repair tests.query.test_query_audit -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/capability_registry/failures.py backend/app/query/analysis.py backend/app/query/validator.py backend/app/services/react_repair.py backend/tests/query/test_capability_repair.py backend/tests/query/test_query_audit.py
git commit -m "feat: add bounded capability repair"
```

---

### Task 8: QuerySpec 全链路接入与旧入口清理

**Files:**
- Modify: `backend/app/query/service.py`
- Modify: `backend/app/services/agent.py`
- Modify: `backend/app/services/field_grounding.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/spec_mdl.py`
- Test: `backend/tests/query/test_single_turn_e2e.py`
- Test: `backend/tests/query/test_agent_integration.py`

**Interfaces:**
- Consumes: Tasks 3-7 registry, SubjectResolver, EnumResolver, EntityResolver, metric compiler and repair loop
- Produces: QuerySpec payload with `capability_snapshot_id` in diagnostics

- [ ] **Step 1: Write failing end-to-end tests**

```python
def test_crack_family_query_uses_registry_expansion():
    result = start_single_turn("裂缝有多少处")
    self.assertIn("横向裂缝", result.plan.nodes[0].spec.filters[0].display_value)
    self.assertIn("纵向裂缝", result.plan.nodes[0].spec.filters[0].display_value)

def test_fake_metric_never_reaches_compiler():
    result = start_single_turn("哪条路健康状况最好")
    self.assertNotIn("disease_density", result.plan.nodes[0].spec.metrics)
    self.assertIn("count", result.plan.nodes[0].spec.metrics)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_single_turn_e2e tests.query.test_agent_integration -v
```

Expected: FAIL until all registry components are connected to the production entry point.

- [ ] **Step 3: Wire production entry point to registry-backed planning**

Remove direct schema-name planning and old compatibility calls. `start_single_turn()` must build reach, resolve subject and capability view, validate, repair and compile through the registry.

- [ ] **Step 4: Run end-to-end and full query suites**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_single_turn_e2e tests.query.test_agent_integration -v
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/query/service.py backend/app/services/agent.py backend/app/services/field_grounding.py backend/app/query/analysis.py backend/app/query/spec_mdl.py backend/tests/query/test_single_turn_e2e.py backend/tests/query/test_agent_integration.py
git commit -m "feat: route single-turn planning through registry"
```

---

### Task 9: 删除旧实现、配置误用守卫与真实回归

**Files:**
- Modify: `backend/app/services/nl2sql.py`
- Modify: `backend/app/services/route_resolver.py`
- Modify: `backend/app/services/sql_engine.py`
- Modify: `backend/app/services/query_rewrite.py`
- Modify: `backend/app/services/field_catalog.py`
- Modify: `backend/tests/query/test_runtime_boundaries.py`
- Create: `backend/tests/query/test_capability_config_guard.py`

**Interfaces:**
- Consumes: all previous tasks
- Produces: static guard tests that fail if legacy mappings or old config paths return

- [ ] **Step 1: Write failing cleanup guards**

```python
def test_no_legacy_business_mapping_symbols_remain():
    forbidden = (
        "_DAMAGE_ALIASES",
        "expand_problem_types",
        "expand_problem_type_filters",
        "_CODE_FIELDS",
    )
    for path in (BACKEND_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for symbol in forbidden:
            self.assertNotIn(symbol, text, f"{symbol} remains in {path}")

def test_no_business_mapping_yaml_outside_capability_directory():
    self.assertEqual(find_mapping_yaml_files_outside(CAPABILITY_DIR), [])
```

- [ ] **Step 2: Run guard tests to verify failure**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_config_guard -v
```

Expected: FAIL until all old mappings are deleted.

- [ ] **Step 3: Delete remaining legacy mappings and update all callers**

No compatibility aliases or old files may remain. If an old workflow cannot use the registry interface, update that workflow to the registry adapter in the same task.

- [ ] **Step 4: Run all tests, compile and real queries**

Run:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
.\.venv\Scripts\python.exe -m compileall -q app
```

Restart the backend and verify:

- “裂缝有多少处？”
- “裂缝和坑槽各自在不同道路上的病害密度”
- “上下行分别有多少处？”
- “华容－常宁高速公路在本月的巡检任务完成情况如何”
- “哪条路的健康状况最好？从哪几个维度判断？”

Expected:

- “裂缝”展开到数据库真实成员。
- 上下行按各自表的 code_map 解析。
- 完成情况不擅自增加已完成过滤。
- 健康题不出现 `disease_density` 原子指标。
- 所有失败都有结构化原因。

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/nl2sql.py backend/app/services/route_resolver.py backend/app/services/sql_engine.py backend/app/services/query_rewrite.py backend/app/services/field_catalog.py backend/tests/query/test_runtime_boundaries.py backend/tests/query/test_capability_config_guard.py
git commit -m "refactor: remove legacy capability mappings"
```

## Final Verification

- [ ] Run full query suite:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"
```

- [ ] Run compile check:

```powershell
cd backend
.\.venv\Scripts\python.exe -m compileall -q app
```

- [ ] Confirm `resources/capabilities/` is the only manual capability mapping location.
- [ ] Confirm all legacy config files and mapping symbols are deleted.
- [ ] Confirm real database queries return results or explicit unsupported failures without fabricated fields.
