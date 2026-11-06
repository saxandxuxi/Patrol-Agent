# 计算工具注册表与 `post_calc` 执行实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把分散在 `agent.py` 的算术迁入独立计算包，以结构化工具参数驱动 `post_calc` 和 ReAct 修复。

**Architecture:** `resources/capabilities/calculators.yaml` 定义闭集计算能力；`backend/app/calculations/` 提供 Decimal 算术、列/序列计算、证据和业务公式；三个计算工具通过现有工具注册表暴露；`post_calc` 只负责调度。

**Tech Stack:** Python 3.11、Decimal、PyYAML、unittest、现有 ToolRegistry、FastAPI

**Spec:** `docs/superpowers/specs/2026-11-06-calculation-tool-registry-design.md`

## Global Constraints

- 不允许 `eval`、`exec`、任意表达式解析或 `sympy` 动态执行。
- LLM 只能输出工具名、操作符和结构化操作数来源，不能输出最终算术结果充当执行事实。
- 人工维护的计算能力只放在 `resources/capabilities/calculators.yaml`。
- 通用算术、列计算、序列计算、表格证据和业务公式按职责拆分文件。
- 迁移后删除原函数和旧关键词触发分支，禁止双写。
- 结构化失败进入最多两轮修复，不得伪装成成功。
- 每个任务遵循失败测试、最小实现、通过测试、提交。

## Review Focus

- 除数为零、空值和非法数字不能变成静默 0。
- 两列计算必须保留原行 ID 与展示字段。
- 百分比和比例不能混用。
- `post_calc` 不能重新引入 `if "关键词" in question` 计算路由。
- QuerySpec、ReAct 和旧复合计划必须使用同一计算执行事实。

---

### Task 1: 项目约束、不可变快照与遗留注释

**Files:**
- Modify: `docs/项目原则-禁止正则与关键词硬匹配.md`
- Modify: `backend/app/services/schema_catalog.py`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Test: `backend/tests/query/test_capability_registry.py`

**Interfaces:**
- Produces: recursively immutable `CapabilitySnapshot`
- Produces: project rule requiring domain-based file placement

- [ ] **Step 1: Write a failing immutability test**

```python
def test_capability_snapshot_nested_mappings_are_read_only(self):
    registry = CapabilityRegistry.load()
    with self.assertRaises(TypeError):
        registry.snapshot.metrics["disease"]["count"] = None
```

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_registry -v`

Expected: FAIL because nested dictionaries are mutable.

- [ ] **Step 3: Implement recursive read-only snapshot and fix stale comment**

Convert nested mappings to `MappingProxyType` after registry augmentation. Replace the stale `resources/field_meta/` comment with `field_overrides.yaml`.

- [ ] **Step 4: Run registry tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_capability_registry tests.query.test_runtime_boundaries -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add docs/项目原则-禁止正则与关键词硬匹配.md backend/app/services/schema_catalog.py backend/app/query/capability_registry/models.py backend/app/query/capability_registry/registry.py backend/tests/query/test_capability_registry.py
git commit -m "refactor: freeze capability snapshots"
```

---

### Task 2: 计算配置和核心操作

**Files:**
- Create: `resources/capabilities/calculators.yaml`
- Modify: `resources/capabilities/index.yaml`
- Modify: `backend/app/query/capability_registry/loader.py`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Create: `backend/app/calculations/__init__.py`
- Create: `backend/app/calculations/models.py`
- Create: `backend/app/calculations/numbers.py`
- Create: `backend/app/calculations/scalar.py`
- Create: `backend/app/calculations/registry.py`
- Test: `backend/tests/query/test_calculations.py`

**Interfaces:**
- Produces: `CalculationOperand`, `CalculationRequest`, `CalculationResult`
- Produces: `calculate_scalar(request: CalculationRequest) -> CalculationResult`
- Produces: `CalculationRegistry.load() -> CalculationRegistry`

- [ ] **Step 1: Write failing scalar tests**

```python
def test_scalar_add_subtract_multiply_divide():
    self.assertEqual(calculate_scalar(request("add", 2, 3)).value, Decimal("5"))
    self.assertEqual(calculate_scalar(request("subtract", 5, 3)).value, Decimal("2"))
    self.assertEqual(calculate_scalar(request("multiply", 4, 3)).value, Decimal("12"))
    self.assertEqual(calculate_scalar(request("divide", 12, 4)).value, Decimal("3"))

def test_scalar_zero_denominator_is_structured_failure():
    result = calculate_scalar(request("divide", 12, 0))
    self.assertFalse(result.accepted)
    self.assertEqual(result.failure_code, "calculation_zero_denominator")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculations -v`

Expected: FAIL because calculation package does not exist.

- [ ] **Step 3: Implement config loading, Decimal parsing and scalar operations**

Use only registered operations. Do not add string-expression evaluation.

- [ ] **Step 4: Run calculation tests and registry tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculations tests.query.test_capability_registry -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add resources/capabilities backend/app/query/capability_registry backend/app/calculations backend/tests/query/test_calculations.py
git commit -m "feat: add registered scalar calculations"
```

---

### Task 3: 列计算与序列计算

**Files:**
- Create: `backend/app/calculations/columns.py`
- Create: `backend/app/calculations/series.py`
- Create: `backend/app/calculations/evidence.py`
- Test: `backend/tests/query/test_calculations.py`

**Interfaces:**
- Produces: `calculate_columns(operation, rows, left_field, right_field, output_field, precision)`
- Produces: `calculate_series(operation, labels, values)`
- Produces: `compact_table_evidence(rows)`

- [ ] **Step 1: Add failing column tests**

```python
def test_column_division_preserves_row_context():
    rows = [{"route_name": "A", "count": 10, "length_km": 5}]
    result = calculate_columns("divide", rows, "count", "length_km", "density", 4)
    self.assertEqual(result.rows[0]["route_name"], "A")
    self.assertEqual(result.rows[0]["density"], Decimal("2.0000"))

def test_column_zero_denominator_reports_row():
    result = calculate_columns("divide", [{"id": "A", "a": 1, "b": 0}], "a", "b", "ratio", 4)
    self.assertEqual(result.failure_code, "calculation_zero_denominator")
    self.assertEqual(result.evidence["row_ids"], ["A"])
```

- [ ] **Step 2: Add failing series and percentage tests**

```python
def test_percentage_share():
    self.assertEqual(calculate_scalar(request("percentage_share", 25, 200)).value, Decimal("12.5"))

def test_percentage_change():
    self.assertEqual(calculate_scalar(request("percentage_change", 80, 100)).value, Decimal("-20"))

def test_series_preserves_zero_batches():
    result = calculate_series("subtract", ["b1", "b2", "b3"], [0, 3, 15])
    self.assertEqual([row["value"] for row in result.rows], [0, 3, 15])
```

- [ ] **Step 3: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculations -v`

Expected: FAIL because column, series and percentage functions are incomplete.

- [ ] **Step 4: Implement columns, series and compact evidence**

Column calculation preserves every non-computed field in each source row. Series output keeps the original labels and order.

- [ ] **Step 5: Run calculation tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculations -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/calculations backend/tests/query/test_calculations.py
git commit -m "feat: add column and series calculations"
```

---

### Task 4: 注册三个 ReAct 计算工具

**Files:**
- Create: `backend/app/services/tools/big/calculate_scalar.py`
- Create: `backend/app/services/tools/big/calculate_columns.py`
- Create: `backend/app/services/tools/big/calculate_series.py`
- Modify: `backend/app/services/tools/big/__init__.py`
- Modify: `backend/app/services/tools/catalog.json`
- Test: `backend/tests/query/test_tool_catalog.py`

**Interfaces:**
- Produces tools: `calculate_scalar`, `calculate_columns`, `calculate_series`
- Consumes: Task 2/3 calculation functions

- [ ] **Step 1: Write failing tool registry tests**

```python
def test_calculation_tools_are_registered():
    names = {item.name for item in tools.list_tools(layer="big")}
    self.assertTrue({"calculate_scalar", "calculate_columns", "calculate_series"} <= names)

def test_calculation_tool_returns_structured_failure():
    result = tools.run_tool("calculate_scalar", operation="divide", operands=[1, 0])
    self.assertFalse(result["ok"])
    self.assertEqual(result["failure"]["failure_code"], "calculation_zero_denominator")
```

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_tool_catalog -v`

Expected: FAIL because tools are not registered.

- [ ] **Step 3: Implement one tool wrapper per file and regenerate catalog**

Run: `.\.venv\Scripts\python.exe -c "from app.services.tools.registry import write_catalog; write_catalog()"`

- [ ] **Step 4: Run tool tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_tool_catalog tests.query.test_calculations -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/tools backend/tests/query/test_tool_catalog.py
git commit -m "feat: expose calculation tools to ReAct"
```

---

### Task 5: QuerySpec 计算契约与规划提示

**Files:**
- Modify: `backend/app/query/contracts.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Modify: `backend/app/query/service.py`
- Modify: `prompts/query_spec.md`
- Modify: `prompts/query_spec_audit.md`
- Test: `backend/tests/query/test_calculation_contract.py`
- Test: `backend/tests/query/test_agent_integration.py`

**Interfaces:**
- Produces: `QuerySpec.calculation: CalculationRequest | None`
- Produces: planner capability view field `calculators`
- Consumes: calculator registry from Task 2

- [ ] **Step 1: Write failing QuerySpec tests**

```python
def test_post_calc_parses_structured_calculation():
    plan = analyze_single_turn_with_payload(post_calc_payload_with_divide())
    self.assertEqual(plan.nodes[-1].spec.calculation.tool, "calculate_columns")
    self.assertEqual(plan.nodes[-1].spec.calculation.operation, "divide")

def test_unknown_calculation_operation_is_rejected_before_execution():
    with self.assertRaisesRegex(QuerySpecError, "calculation_operation_unknown"):
        analyze_single_turn_with_payload(post_calc_payload_with_eval())
```

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculation_contract -v`

Expected: FAIL because QuerySpec has no calculation field.

- [ ] **Step 3: Add calculation model parsing and registry validation**

The planner receives calculator catalogue details. `post_calc` receives the validated serialized calculation from `QuerySpec`.

- [ ] **Step 4: Run query contract tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_calculation_contract tests.query.test_capability_repair tests.query.test_agent_integration -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/query prompts backend/tests/query/test_calculation_contract.py backend/tests/query/test_agent_integration.py
git commit -m "feat: add QuerySpec calculation contracts"
```

---

### Task 6: 迁移 `post_calc` 和业务公式

**Files:**
- Create: `backend/app/calculations/post_calc.py`
- Create: `backend/app/calculations/domain/comparison.py`
- Create: `backend/app/calculations/domain/density.py`
- Create: `backend/app/calculations/domain/ranking.py`
- Create: `backend/app/calculations/domain/trend.py`
- Create: `backend/app/calculations/domain/__init__.py`
- Modify: `backend/app/services/tools/big/post_calc.py`
- Modify: `backend/app/query/service.py`
- Modify: `backend/app/services/react_agent.py`
- Modify: `backend/app/services/agent.py`
- Test: `backend/tests/query/test_answer_experience.py`
- Test: `backend/tests/query/test_post_calc_failure.py`
- Test: `backend/tests/query/test_runtime_boundaries.py`

**Interfaces:**
- Produces: `execute_post_calc(task, results) -> dict`
- Consumes: calculator executor and domain formulas

- [ ] **Step 1: Update tests to use calculation package public seams**

Replace direct imports of `agent._density_rows`, `agent._compare_datasets` and `agent._post_calc_payload` with `app.calculations` public functions.

- [ ] **Step 2: Run tests to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_answer_experience tests.query.test_post_calc_failure tests.query.test_runtime_boundaries -v`

Expected: FAIL because public calculation seam does not exist.

- [ ] **Step 3: Move calculation implementations and delete old agent functions**

Delete the migrated functions from `agent.py`. Update service and ReAct imports to the new package.

- [ ] **Step 4: Remove keyword-based post_calc routing**

When `calculation` is absent, use compact evidence plus analysis LLM. Do not route by `"密度"`, `"占比"`, `"严重"` or `re.search("趋势")`.

- [ ] **Step 5: Run focused and full tests**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_answer_experience tests.query.test_post_calc_failure tests.query.test_runtime_boundaries -v`
Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/calculations backend/app/services/agent.py backend/app/services/react_agent.py backend/app/services/tools/big/post_calc.py backend/app/query/service.py backend/tests/query
git commit -m "refactor: move post_calc into calculation modules"
```

---

### Task 7: 失败策略、真实回归与独立评审

**Files:**
- Modify: `resources/capabilities/repair_policy.yaml`
- Modify: `prompts/react_controller.md`
- Modify: `prompts/post_calc.md`
- Modify: `backend/tests/query/test_react_repair_policy.py`
- Modify: `backend/tests/query/test_capability_repair.py`
- Create: `docs/calculation-tool-registry-regression.md`

**Interfaces:**
- Consumes: all prior calculation tools and failures
- Produces: documented real-query regression evidence

- [ ] **Step 1: Write failing repair-policy tests**

```python
def test_zero_denominator_routes_to_calculation_tools():
    names = react_repair.candidate_tool_names("calculation_zero_denominator")
    self.assertEqual(names, ["calculate_scalar", "calculate_columns", "calculate_series"])
```

- [ ] **Step 2: Run test to verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest tests.query.test_react_repair_policy -v`

Expected: FAIL because policy entries do not exist.

- [ ] **Step 3: Add calculation failures and controller instructions**

Controller may rewrite calculation tool/parameters only. It may not change user scope.

- [ ] **Step 4: Run full suite and compile**

Run: `.\.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py"`
Run: `.\.venv\Scripts\python.exe -m compileall -q app`

Expected: PASS.

- [ ] **Step 5: Real database regression**

Verify:

- 裂缝和坑槽各自在不同道路上的病害密度
- AI 识别的病害类型靠谱吗？有多少是认不出来的？
- 坑槽在最近5次巡查的数量变化
- 哪条路的健康状况最好？从哪几个维度判断？

Expected: every arithmetic result includes a registered calculation tool and structured operand evidence.

- [ ] **Step 6: Independent code review**

Use `requesting-code-review` with the plan, spec and branch diff. Fix all Critical/Important findings once.

- [ ] **Step 7: Commit**

```bash
git add resources/capabilities/repair_policy.yaml prompts backend/tests/query docs/calculation-tool-registry-regression.md
git commit -m "test: verify calculation tool workflow"
```

## Final Verification

- [ ] Run full query suite and compile.
- [ ] Search `agent.py` for deleted calculation symbols.
- [ ] Confirm no keyword/regex-based calculation dispatch remains in `post_calc`.
- [ ] Confirm `resources/capabilities/` is the only manual calculation mapping location.
- [ ] Record the independent review verdict and any deferred minors.
