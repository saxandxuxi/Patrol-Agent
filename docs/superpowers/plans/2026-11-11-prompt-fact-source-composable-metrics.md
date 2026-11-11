# 提示词单一事实源与可组合指标实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 QuerySpec 提示词、能力配置、修复票据和 MDL 之间的重复事实源，让普通比例和密度由可审计的事实节点加通用计算组合，并且只让明确的 `compiler_not_covered` 进入一次 Wren/LLM MDL-SQL 兜底。

**Architecture:** QuerySpec 继续拥有用户范围、主体、视图、依赖和输出意图；`query_shapes.yaml` 拥有共享业务语义；Wren MDL 拥有模型、列、计算列、关系拓扑和通用 measure；`resources/capabilities/` 拥有巡检业务字段、枚举、指标语义和正式派生指标。所有消费者读取同一份投影，所有冲突在 LLM 之前失败关闭，所有范围变更只能通过 `RepairIssue.allowed_paths` 约束的 JSON Patch 完成。

**Tech Stack:** Python 3、`unittest`、YAML、SQLGlot、Wren MDL / `dry-plan`、Vue 3、Vite。

**Spec:** `docs/superpowers/specs/2026-11-11-prompt-fact-source-composable-metrics-design.md`

## Global Constraints

- 一个机器可读语义事实只能有一个 owner；迁移时必须同步删除旧位置，禁止新旧双写。
- 不通过关键词、正则、题目 ID、固定问句映射或新增单题分支修复问题。
- QuerySpec 链式依赖只允许 `depends_on` 加结构化 `inputs`；不得恢复 `DEPENDS_ON_`、`{{key}}` 等 QuerySpec 隐藏协议。
- 审计提示词的 `set` 只能作为建议；修改权限的唯一来源是 `RepairIssue.allowed_paths`。
- QuerySpec 或合同失败最多修复两轮；相同证据或相同补丁立即停止；用户原始范围哈希必须保持不变。
- MDL 与 resources 是平等事实源，不设置全局优先级；同一事实冲突时失败关闭。
- 只有合法合同下确定性编译器明确返回 `compiler_not_covered` 才允许一次 LLM MDL-SQL 兜底。
- 禁止把 `compiler_exception`、合同冲突、安全拒绝、范围失败、确定性 SQL 审计失败或 `dry-plan` 失败送入自由 LLM 兜底。
- 测试命令统一在 `backend/` 目录运行，使用 `backend/.venv`，项目测试框架是 `unittest`。
- 不运行 `git reset`、`git checkout --`、`git clean`，不覆盖当前工作区中已有的未提交修改。
- 每个任务只暂存该任务列出的文件；禁止使用 `git add -A` 或把未跟踪诊断脚本带入提交。

## Execution Precondition

当前 `main` 上有大量尚未提交的业务修改和未跟踪文件，而计划与设计稿已经提交。实施前必须先由用户明确选择一种基线：

1. 在现有脏工作区继续实施，并先获得在 `main` 直接实施的明确同意；或
2. 把当前工作区保存为一个经用户确认的 WIP checkpoint，再在 `codex/prompt-fact-source-composable-metrics` 隔离 worktree 中实施。

无论选择哪一种，Task 1 之前的验证基线都必须是“当前工作区代码”，不能从 `HEAD` 新建一个丢失现有修改的 worktree。若用户选择隔离 worktree，先确认其中的五个相关测试文件与当前工作区行为一致。

## File Structure

| 路径 | 单一职责 |
| --- | --- |
| `backend/app/query/contracts.py` | 公开视图集合、内部别名和 QuerySpec 数据类型 |
| `backend/app/query/prompt_facts.py` | 从能力注册表和运行时视图构建提示词共享事实 JSON |
| `backend/app/query/analysis.py` | QuerySpec 解析、范围校验、语义审计和有限修复编排 |
| `backend/app/query/planning_repair/issues.py` | 把错误或合同失败转换成唯一修复票据 |
| `backend/app/query/planning_repair/json_patch.py` | JSON Patch 权限、范围和重复补丁强制校验 |
| `backend/app/query/capability_registry/models.py` | 能力快照和指标语义数据类型 |
| `backend/app/query/capability_registry/mdl_facts.py` | MDL cube measure 到能力指标的投影 |
| `backend/app/query/capability_registry/fact_conflicts.py` | MDL 与 resources 的结构化冲突门禁 |
| `backend/app/query/metrics/semantics.py` | 指标聚合、grain、identity、fanout 和来源规则 |
| `backend/app/calculations/semantics.py` | 计算图的来源粒度、单位和对齐规则 |
| `backend/app/query/mdl_sql/audit/view_audit.py` | SQL 计划形状与指标聚合一致性 |
| `backend/app/query/mdl_sql/compiler/pipeline.py` | 确定性预检、审计、`dry-plan` 和唯一兜底入口 |
| `resources/capabilities/query_shapes.yaml` | TopN、时间、主体证据、关系存在性等共享业务语义 |
| `resources/capabilities/metrics.yaml` | 原子指标事实、单位、聚合、grain、identity、fanout 和来源 |
| `resources/capabilities/derived_metrics.yaml` | 有正式业务命名的派生公式 |
| `prompts/query_spec.md` | QuerySpec 输出协议和共享事实消费方式 |
| `prompts/query_spec_audit.md` | 薄审计清单和结构化问题输出 |
| `prompts/subject_router.md` | 主体路由协议和共享主体语义消费 |
| `docs/项目原则-提示词与事实源一致性.md` | 提示词、事实 owner、冲突门禁和验收原则 |

---

### Task 1: 公开视图与提示词事实投影

**Files:**
- Create: `backend/app/query/prompt_facts.py`
- Create: `backend/tests/query/test_prompt_facts.py`
- Modify: `backend/app/query/contracts.py`
- Modify: `backend/app/query/shape_contracts.py`
- Modify: `backend/app/query/validator.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/audit.py`
- Modify: `backend/app/query/capability_registry/subject_resolver.py`
- Modify: `prompts/query_spec.md`
- Modify: `prompts/query_spec_audit.md`
- Modify: `prompts/subject_router.md`
- Modify: `backend/tests/query/test_prompt_contracts.py`

**Interfaces:**
- Produces: `PUBLIC_QUERY_VIEWS: tuple[str, ...]`，不包含内部别名 `top`。
- Produces: `PromptFacts.as_prompt_values() -> dict[str, str]`，至少包含 `query_semantics`、`answer_operations`、`supported_views`、`capabilities` 四个 JSON 字符串。
- Produces: `build_prompt_facts(registry: CapabilityRegistry, capabilities: Mapping[str, Any]) -> PromptFacts`。
- Consumers: `analysis.py`、`audit.py` 和 `subject_resolver.py` 都通过同一函数生成共享占位符值。

**Review Focus:** 用户只要求排序全部对象时，不得因提示词手抄清单漂移而变成 TopN；内部 `top` 不得暴露给 Planner。

- [ ] **Step 1: 写失败测试**

在 `test_prompt_facts.py` 增加以下断言：

```python
def test_public_views_exclude_internal_top_alias(self):
    self.assertNotIn("top", PUBLIC_QUERY_VIEWS)
    self.assertIn("top_groups", PUBLIC_QUERY_VIEWS)

def test_prompt_facts_render_the_same_supported_views(self):
    facts = build_prompt_facts(CapabilityRegistry.load(), {"subject": "disease"})
    rendered = facts.as_prompt_values()
    self.assertEqual(
        json.loads(rendered["supported_views"]),
        list(PUBLIC_QUERY_VIEWS),
    )
    self.assertNotIn("top", json.loads(rendered["supported_views"]))
```

在 `test_prompt_contracts.py` 增加：

```python
def test_query_prompts_use_runtime_supported_views(self):
    for name in ("query_spec.md", "query_spec_audit.md"):
        _, body = body_and_header(name)
        self.assertIn("<<supported_views>>", body)
        self.assertNotIn("count / sum / avg / distribution / list / detail", body)

def test_unknown_view_is_rejected_against_public_projection(self):
    spec = replace(valid_spec(), view="invented_view")
    issues = validate_shape_contract(spec, CapabilityRegistry.load())
    self.assertEqual(issues[0].code, "unsupported_view")
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_prompt_facts tests.query.test_prompt_contracts -v
```

Expected: FAIL，因为 `PUBLIC_QUERY_VIEWS`、`prompt_facts` 或 `<<supported_views>>` 尚不存在。

- [ ] **Step 3: 实现公开视图和共享投影**

在 `contracts.py` 定义 `PUBLIC_QUERY_VIEWS`，把 `top` 单独放入 `INTERNAL_QUERY_VIEW_ALIASES`。`shape_contracts.py` 和 `validator.py` 使用公开集合校验外部 QuerySpec，`normalize_top_view()` 仍可在代码内部消费 `top`。

在 `prompt_facts.py` 中用不可变 `PromptFacts` 封装：

```python
@dataclass(frozen=True)
class PromptFacts:
    query_semantics: Mapping[str, Any]
    answer_operations: Mapping[str, Any]
    supported_views: tuple[str, ...]
    capabilities: Mapping[str, Any]

    def as_prompt_values(self) -> dict[str, str]: ...
```

序列化统一使用 `ensure_ascii=False`、`default=str`。三个提示词调用点删除手写 view 清单并传入投影值。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_prompt_facts tests.query.test_prompt_contracts -v
```

Expected: PASS，且未填充占位符日志为 0。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/query/prompt_facts.py backend/app/query/contracts.py backend/app/query/shape_contracts.py backend/app/query/validator.py backend/app/query/analysis.py backend/app/query/audit.py backend/app/query/capability_registry/subject_resolver.py backend/tests/query/test_prompt_facts.py backend/tests/query/test_prompt_contracts.py prompts/query_spec.md prompts/query_spec_audit.md prompts/subject_router.md
git commit -m "refactor: centralize prompt facts and public views"
```

---

### Task 2: 共享查询语义与薄审计提示词

**Files:**
- Modify: `resources/capabilities/query_shapes.yaml`
- Modify: `prompts/query_spec.md`
- Modify: `prompts/query_spec_audit.md`
- Modify: `prompts/subject_router.md`
- Modify: `prompts/planning.md`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/audit.py`
- Modify: `backend/app/query/capability_registry/subject_resolver.py`
- Modify: `backend/tests/query/test_prompt_contracts.py`
- Modify: `backend/tests/query/test_subject_capability_view.py`

**Interfaces:**
- Consumes: Task 1 的 `PromptFacts`。
- Produces: `query_shapes.yaml.planner_semantics` 是 TopN、时间范围、主体证据和关系存在性的唯一机器可读 owner。
- Produces: `audit_query_plan(question, plan, *, capabilities, registry=None)` 不再接收 `current_date` 或 `assist_evidence`。

**Review Focus:** 审计器可能再次手抄 Planner 规则；测试必须证明审计提示词只引用共享事实。

- [ ] **Step 1: 写失败测试**

在 `test_prompt_contracts.py` 增加：

```python
def test_query_spec_and_audit_share_topn_semantics(self):
    for name in ("query_spec.md", "query_spec_audit.md"):
        _, body = body_and_header(name)
        self.assertIn("<<query_semantics>>", body)
        self.assertIn("<<answer_operations>>", body)
        self.assertNotIn("用户明确给出 N", body)
        self.assertNotIn("没有 N", body)

def test_subject_router_consumes_shared_task_evidence(self):
    _, body = body_and_header("subject_router.md")
    self.assertIn("<<query_semantics>>", body)
    self.assertNotIn("问任务自身状态、设备、里程、次数", body)

def test_extreme_without_explicit_n_is_not_topn(self):
    spec = replace(valid_spec(), view="top_groups", limit=None, dimensions=("route_name",))
    normalized = normalize_top_view(spec)
    self.assertEqual(normalized.view, "distribution")
    self.assertIsNone(normalized.limit)
```

在 `test_subject_capability_view.py` 增加：

```python
def test_planner_and_router_receive_the_same_task_disease_evidence(self):
    registry = CapabilityRegistry.load()
    planner = registry.planner_view("disease", FieldReachSet()).to_prompt_dict()
    router = build_prompt_facts(registry, {}).as_prompt_values()
    self.assertEqual(
        planner["query_semantics"]["task_disease_evidence"],
        json.loads(router["query_semantics"])["task_disease_evidence"],
    )
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_prompt_contracts tests.query.test_subject_capability_view -v
```

Expected: FAIL，因为提示词仍手写规则，主体路由仍复制任务判定。

- [ ] **Step 3: 收敛共享语义**

把 `query_spec.md` 中重复的 TopN、排序全部对象、最近 N 次/最近一次、关系存在性、主体身份规则删除；缺失的“最近 N 次巡检候选证据”写入 `query_shapes.yaml.planner_semantics`。

把 `query_spec_audit.md` 收敛为薄清单，只接收：

```text
<<question>>
<<plan_json>>
<<query_semantics>>
<<answer_operations>>
<<supported_views>>
<<capabilities>>
```

`subject_router.md` 只保留路由职责、候选主体、`trusted=false` 证据和 `<<query_semantics>>`。`planning.md` 文件头明确标注为旧 OrchestratorPlan 协议，`{{key}}`、`needs`、`produces` 不得进入 QuerySpec。

更新 `audit.py` 和两个调用点，删除 `current_date` 与 `assist_evidence` 参数。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_prompt_contracts tests.query.test_subject_capability_view tests.query.test_query_audit -v
```

Expected: PASS，审计正文不包含第二份 TopN、时间或主体规则。

- [ ] **Step 5: 提交**

```powershell
git add resources/capabilities/query_shapes.yaml prompts/query_spec.md prompts/query_spec_audit.md prompts/subject_router.md prompts/planning.md backend/app/query/analysis.py backend/app/query/audit.py backend/app/query/capability_registry/subject_resolver.py backend/tests/query/test_prompt_contracts.py backend/tests/query/test_subject_capability_view.py
git commit -m "refactor: share query semantics across prompts"
```

---

### Task 3: Action 闭集与失败关闭

**Files:**
- Create: `backend/tests/query/test_action_contract.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/planning_repair/issues.py`

**Interfaces:**
- Produces: `normalize_action(action: object) -> tuple[str, tuple[str, ...]]`。
- Mapping: `query_data -> query_data`；`post_calc -> post_calc`；`analysis|analyze -> post_calc` 加 `analysis_only`。
- Invalid values raise a `QuerySpecError` starting with `invalid_action:`.

**Review Focus:** 缺省 action 不能被静默当成 SQL 查询；`analyze` 不能被执行成 `query_data`。

- [ ] **Step 1: 写失败测试**

```python
def test_analyze_maps_to_non_querying_post_calc(self):
    node_type, assertions = normalize_action("analyze")
    self.assertEqual(node_type, "post_calc")
    self.assertIn("analysis_only", assertions)

def test_missing_action_is_not_query_data(self):
    with self.assertRaisesRegex(QuerySpecError, "^invalid_action:"):
        normalize_action(None)

def test_unknown_action_generates_action_only_repair_issue(self):
    issue = repair_issue_from_error(
        QuerySpecError("invalid_action: draw_chart"),
        {"views": [{"id": "q1", "action": "draw_chart"}]},
    )
    self.assertEqual(issue.code, "invalid_action")
    self.assertIn("/views/*/action", issue.allowed_paths)
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_action_contract -v
```

Expected: FAIL，因为函数尚不存在，且当前代码把缺省 action 变成 `query_data`。

- [ ] **Step 3: 实现闭集归一化**

在 `analysis.py` 用 `normalize_action()` 替换当前 `action = str(raw.get("action") or "query_data")` 分支。`analysis`/`analyze` 的 `analysis_only` 断言与模型已有断言去重合并。未知值立即抛出结构化错误。

在 `issues.py` 增加 `invalid_action` 分支，只允许 `/views/*/action`；若修复为 `post_calc` 且需要 `analysis_only`，额外允许 `/views/*/required_assertions`。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_action_contract tests.query.test_planner_self_repair tests.query.test_calculation_contract -v
```

Expected: PASS，未知 action 不会进入执行器。

- [ ] **Step 5: 提交**

```powershell
git add backend/tests/query/test_action_contract.py backend/app/query/analysis.py backend/app/query/planning_repair/issues.py
git commit -m "fix: fail closed on unknown query actions"
```

---

### Task 4: 结构化依赖与误写占位符修复

**Files:**
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/planning_repair/issues.py`
- Modify: `backend/tests/query/test_planner_self_repair.py`
- Modify: `backend/tests/query/test_planning_repair.py`

**Interfaces:**
- Produces: `structured_dependency_binding(raw_value, target_field, op, outputs_by_node) -> InputBinding | None`。
- Produces: `invalid_dependency_value` repair issue，允许 `/views/*/filters`、`/views/*/depends_on`、`/views/*/inputs`。
- Removes: `_dependency_placeholder_binding()` 和所有运行时 `DEPENDS_ON_` 分支。

**Review Focus:** 真实路线名即使包含点号也不能被误判为依赖；只有节点 ID 和已声明输出字段同时命中时才进入修复票据。

- [ ] **Step 1: 写失败测试**

```python
def test_query_spec_dot_path_is_repaired_into_structured_input(self):
    draft = draft_with_filter("q1_top_route.route_name")
    issue = repair_issue_from_error(
        QuerySpecError("invalid_dependency_value: q1_top_route.route_name"),
        draft,
    )
    self.assertEqual(issue.code, "invalid_dependency_value")
    self.assertIn("/views/*/filters", issue.allowed_paths)
    self.assertIn("/views/*/inputs", issue.allowed_paths)

def test_depends_on_secret_protocol_is_no_longer_accepted(self):
    with self.assertRaisesRegex(QuerySpecError, "invalid_dependency_value"):
        parse_dependency_value(
            "DEPENDS_ON_q1_route_name",
            node_ids={"q1"},
            outputs_by_node={"q1": {"route_name"}},
        )

def test_unknown_dot_string_remains_a_normal_user_value(self):
    self.assertIsNone(
        structured_dependency_binding(
            "G65.west",
            "route_name",
            "=",
            {"q1": {"route_name"}},
        )
    )
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_planner_self_repair tests.query.test_planning_repair -v
```

Expected: FAIL，当前只识别 `DEPENDS_ON_`。

- [ ] **Step 3: 实现受控依赖识别**

删除隐藏兼容函数。解析过滤值时先按“前缀等于当前计划节点 ID + 已声明输出字段”匹配；命中后生成 `invalid_dependency_value` 票据，目标是删除错误 filter 并新增 `depends_on` 和 `InputBinding`。未命中受控事实的字符串继续交给普通值解析。

在 `_parse` 完成后，把票据交给现有最多两轮的修复器，应用补丁时继续校验范围哈希和依赖图。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_planner_self_repair tests.query.test_planning_repair tests.query.test_query_planning -v
```

Expected: PASS，修复后只有结构化 `inputs`，且原路线、时间、病害条件不变。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/query/analysis.py backend/app/query/planning_repair/issues.py backend/tests/query/test_planner_self_repair.py backend/tests/query/test_planning_repair.py
git commit -m "fix: repair query dependencies through structured inputs"
```

---

### Task 5: 审计建议与修复权限单一来源

**Files:**
- Create: `backend/tests/query/test_audit_repair_authority.py`
- Modify: `backend/app/query/planning_repair/issues.py`
- Modify: `backend/app/query/planning_repair/json_patch.py`
- Modify: `backend/app/query/analysis.py`

**Interfaces:**
- Produces: `repair_issue_from_audit(issue: Mapping[str, Any], draft: dict[str, Any], registry: CapabilityRegistry | None = None) -> RepairIssue`。
- Removes: 审计 `set` 直接调用 `_apply_audit_patches()` 的路径。
- Invariant: `apply_repair_patches()` 是唯一修改 QuerySpec 草稿的入口。

**Review Focus:** 模型在审计 `set` 中提出修改 `filters` 或 `subject` 时，代码必须重新发放票据；不得因为审计建议看起来合理就直接应用。

- [ ] **Step 1: 写失败测试**

```python
def test_audit_set_is_not_applied_without_repair_ticket(self):
    draft = two_node_draft()
    with patch(
        "app.query.analysis.request_repair_once",
        return_value=draft,
    ):
        repaired = repair_audit_issues(
            draft,
            [{"code": "lost_filter", "set": {"routes/0": "x"}}],
            registry=CapabilityRegistry.load(),
        )
    self.assertEqual(repaired, draft)

def test_lost_filter_repair_cannot_change_other_scope_fields(self):
    issue = repair_issue_from_audit(
        {"code": "lost_filter", "node_id": "q1"},
        draft_with_route_and_time_filters(),
    )
    with self.assertRaisesRegex(ValueError, "repair changed the user scope"):
        apply_repair_patches(
            RepairTicket("q", issue=issue, draft=draft_with_route_and_time_filters()),
            [{"op": "remove", "path": "/views/0/filters/0"}],
        )
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_audit_repair_authority -v
```

Expected: FAIL，因为审计问题仍可绕过票据直接补丁。

- [ ] **Step 3: 统一修复授权**

删除 `_apply_audit_patches()` 和 `_audit_issue_changes_plan()` 中绕过票据的路径。把审计 issue 转成 `RepairIssue`：`lost_filter` 只允许与用户已表达范围兼容的 filter 路径；`subject_conflict` 只允许 subject；其余结构问题使用显式路径集合。

`apply_repair_patches()` 保留并强化：

1. `allowed_paths` 路径检查；
2. 相同补丁签名检查；
3. 依赖自依赖和环检查；
4. `_scope_preserved()` 逐视图范围检查。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_audit_repair_authority tests.query.test_planning_repair tests.query.test_planner_self_repair -v
```

Expected: PASS，审计建议未经票据验证不会改变草稿。

- [ ] **Step 5: 提交**

```powershell
git add backend/tests/query/test_audit_repair_authority.py backend/app/query/planning_repair/issues.py backend/app/query/planning_repair/json_patch.py backend/app/query/analysis.py
git commit -m "fix: make repair tickets the only planned mutation authority"
```

---

### Task 6: 指标语义、视图审计和 fanout 安全

**Files:**
- Create: `backend/app/query/metrics/__init__.py`
- Create: `backend/app/query/metrics/semantics.py`
- Create: `backend/tests/query/test_metric_semantics.py`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Modify: `backend/app/query/capability_registry/mdl_facts.py`
- Modify: `backend/app/query/mdl_sql/models.py`
- Modify: `backend/app/query/mdl_sql/contract_builder.py`
- Modify: `backend/app/query/mdl_sql/audit/view_audit.py`
- Modify: `backend/tests/query/test_metric_registry.py`
- Modify: `backend/tests/query/test_mdl_sql_audit.py`

**Interfaces:**
- Extends: `MetricCapability` with `aggregation`, `grain`, `identity`, `fanout_safe`, `source_kind`, `source_reference`。
- Produces: `AGGREGATIONS = frozenset({"row", "count", "count_distinct", "sum", "avg", "min", "max"})`。
- Produces: `validate_metric_semantics(metric, *, subject) -> tuple[MetricSemanticsIssue, ...]`。
- Extends: `MdlMetricBinding` with the same aggregation、grain、identity、fanout 字段。

**Review Focus:** `route_length_km` 是 row 指标，不能被 `contains_aggregate` 错杀；`fanout_safe=false` 的指标不能跨一对多 JOIN 直接相加。

- [ ] **Step 1: 写失败测试**

```python
def test_route_length_is_a_row_metric_not_an_aggregate(self):
    metric = CapabilityRegistry.load().snapshot.metrics["disease"]["route_length_km"]
    self.assertEqual(metric.aggregation, "row")
    self.assertEqual(metric.grain, "route")
    self.assertFalse(metric.fanout_safe)

def test_row_metric_does_not_require_aggregate_function(self):
    issues = audit_view_shape(
        parse_one("SELECT route.route_length_km AS route_length_km FROM route"),
        aliases={"route": "route"},
        contract=contract_with_metric("route_length_km", aggregation="row"),
        dialect="mysql",
    )
    self.assertNotIn("aggregate_metric_missing", {item.evidence.get("reason") for item in issues})

def test_sum_metric_missing_aggregate_still_fails(self):
    issues = audit_view_shape(
        parse_one("SELECT disease.effective_disease_length AS total FROM disease"),
        aliases={"disease": "disease"},
        contract=contract_with_metric(
            "sum_effective_disease_length",
            aggregation="sum",
        ),
        dialect="mysql",
    )
    self.assertIn("aggregate_metric_missing", {item.evidence.get("reason") for item in issues})

def test_unsafe_metric_cannot_be_aggregated_across_disease_join(self):
    metric = disease_route_length_metric()
    issues = validate_metric_semantics(metric, subject="disease")
    self.assertEqual(issues[0].code, "metric_requires_own_grain")
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_metric_semantics tests.query.test_metric_registry tests.query.test_mdl_sql_audit -v
```

Expected: FAIL，因为 `MetricCapability` 尚无这些字段，view audit 对所有 metric 强制聚合。

- [ ] **Step 3: 实现指标语义**

在 `metrics/semantics.py` 集中实现来源闭集、聚合闭集、非法 grain/identity、`fanout_safe=false` 且需要异构 JOIN 的拒绝规则。`registry.py` 读取 YAML 字段并验证；生成指标必须显式给出聚合语义，不能默认假设为 sum。

在 `view_audit.py` 只对 `aggregation != "row"` 要求投影包含聚合；实际表达式仍必须与合同完全一致。`aggregation="row"` 的指标出现在多行分组时，必须能证明每个分组唯一对应 identity，否则返回 `metric_grain_unsafe`。

Planner 投影的 `metric_details` 增加 `aggregation`、`grain`、`identity`、`fanout_safe`、`source_kind`、`source_reference`。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_metric_semantics tests.query.test_metric_registry tests.query.test_mdl_sql_audit tests.query.test_mdl_contract_builder tests.query.test_subject_capability_view -v
```

Expected: PASS，row 指标可执行，sum/count 等仍严格要求聚合，fanout 风险在 SQL 前失败。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/query/metrics/__init__.py backend/app/query/metrics/semantics.py backend/tests/query/test_metric_semantics.py backend/app/query/capability_registry/models.py backend/app/query/capability_registry/registry.py backend/app/query/capability_registry/mdl_facts.py backend/app/query/mdl_sql/models.py backend/app/query/mdl_sql/contract_builder.py backend/app/query/mdl_sql/audit/view_audit.py backend/tests/query/test_metric_registry.py backend/tests/query/test_mdl_sql_audit.py
git commit -m "feat: add auditable metric grain semantics"
```

---

### Task 7: 可组合计算图与两个密度口径

**Files:**
- Create: `backend/app/calculations/semantics.py`
- Create: `backend/tests/query/test_calculation_semantics.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `resources/capabilities/metrics.yaml`
- Modify: `resources/capabilities/derived_metrics.yaml`
- Modify: `backend/tests/query/test_calculation_contract.py`
- Modify: `backend/tests/query/test_metric_registry.py`

**Interfaces:**
- Produces: `CalculationSourceShape(result_grain: str, dimensions: tuple[str, ...], identity: str, unit: str)`。
- Produces: `validate_calculation_sources(tool: str, operation: str, sources: tuple[CalculationSourceShape, ...], output_unit: str) -> tuple[CalculationSemanticIssue, ...]`。
- Registers: `route.total_route_length_km: SUM({alias}.route_length_km)`，`aggregation=sum`、`grain=route`、`identity=route.id`。
- Registers: `disease_length_density = sum_effective_disease_length / total_route_length_km`。

**Review Focus:** 两个不同主体的全局标量可以在同一用户范围内相除，但不能因为主体不同就误拒；分组来源必须按相同维度和 identity 对齐。

- [ ] **Step 1: 写失败测试**

```python
def test_density_uses_two_fact_nodes_and_one_scalar_division(self):
    plan = province_road_density_plan()
    issue = _query_plan_calculation_issue(plan, CapabilityRegistry.load())
    self.assertIsNone(issue)
    self.assertEqual([node.spec.subject for node in plan.nodes], ["disease", "route", "disease"])
    self.assertEqual(plan.nodes[-1].spec.calculation.operation, "divide")
    self.assertEqual(plan.nodes[-1].spec.calculation.output.unit, "米/km")

def test_patrol_density_uses_count_and_active_days(self):
    plan = patrol_density_plan()
    self.assertIsNone(_query_plan_calculation_issue(plan, CapabilityRegistry.load()))
    self.assertEqual(
        [item.source["field"] for item in plan.nodes[-1].spec.calculation.operands],
        ["count", "active_day_count"],
    )

def test_grouped_sources_with_different_grain_are_rejected(self):
    issue = _query_plan_calculation_issue(
        grouped_density_with_mismatched_identity(),
        CapabilityRegistry.load(),
    )
    self.assertEqual(issue.code, "calculation_grain_mismatch")

def test_disease_density_and_length_density_are_distinct(self):
    derived = CapabilityRegistry.load().snapshot.derived_metrics["disease"]
    self.assertEqual(derived["disease_density"].unit, "处/km")
    self.assertEqual(derived["disease_length_density"].unit, "米/km")
    self.assertEqual(
        derived["disease_density"].operands,
        ("count", "total_route_length_km"),
    )
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_calculation_semantics tests.query.test_calculation_contract tests.query.test_metric_registry -v
```

Expected: FAIL，因为路线总长度事实、长度密度和 grain/unit 语义尚不存在。

- [ ] **Step 3: 实现计算语义和指标**

在 `calculations/semantics.py` 实现纯规则：

1. 标量或 `row_index` 来源必须产生唯一值；
2. 多行来源的 dimensions 和 identity 必须一致；
3. `divide|ratio|percentage_share` 分母必须是唯一非分组键；
4. `divide` 的 output unit 必须等于分子单位除以分母单位；
5. 无法证明时返回 `calculation_grain_mismatch`、`calculation_unit_mismatch` 或 `calculation_denominator_not_unique`。

`analysis.py` 从 registry 的指标语义和上游 QuerySpec 生成 `CalculationSourceShape`，再把 issue 转成现有 `CapabilityIssue` 修复票据。

更新两个指标文件：

```text
count / total_route_length_km      -> disease_density, 处/km
sum_effective_disease_length /
total_route_length_km              -> disease_length_density, 米/km
```

巡查任务密度不新增 `patrol_density` 固定公式；使用 `patrol.count` 与业务事实 `active_day_count` 组合。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_calculation_semantics tests.query.test_calculation_contract tests.query.test_metric_registry tests.query.test_post_calc_failure -v
```

Expected: PASS，省道题形成三个节点，两个密度口径不再混用。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/calculations/semantics.py backend/tests/query/test_calculation_semantics.py backend/app/query/analysis.py resources/capabilities/metrics.yaml resources/capabilities/derived_metrics.yaml backend/tests/query/test_calculation_contract.py backend/tests/query/test_metric_registry.py
git commit -m "feat: compose density calculations from typed facts"
```

---

### Task 8: 事实冲突门禁与历史债务清理

**Files:**
- Create: `backend/app/query/capability_registry/fact_conflicts.py`
- Create: `backend/tests/query/test_fact_conflicts.py`
- Modify: `backend/app/query/capability_registry/models.py`
- Modify: `backend/app/query/capability_registry/registry.py`
- Modify: `backend/app/query/capability_registry/loader.py`
- Modify: `backend/app/query/capability_registry/mdl_facts.py`
- Modify: `resources/capabilities/metrics.yaml`
- Modify: `resources/capabilities/fields.yaml`
- Modify: `resources/capabilities/field_overrides.yaml`
- Modify: `resources/capabilities/enums.yaml`
- Modify: `resources/capabilities/dimensions.yaml`
- Modify: `resources/capabilities/time_buckets.yaml`
- Modify: `resources/capabilities/predicates.yaml`
- Modify: `backend/app/services/nl2sql.py`
- Modify: `wren_projects/road_mysql/cubes/disease_metrics/metadata.yml`
- Modify: `wren_projects/road_mysql/overrides.yml`
- Modify: `resources/README.md`
- Modify: `backend/tests/query/test_capability_config_guard.py`
- Modify: `backend/tests/query/test_mdl_capability_inheritance.py`
- Modify: `docs/项目数据流讲解.md`
- Modify: `docs/项目原则-AI晋级考试与用户裁决.md`
- Modify: `docs/项目原则-禁止正则与关键词硬匹配.md`
- Create: `docs/项目原则-提示词与事实源一致性.md`

**Interfaces:**
- Produces: `FactConflictReport(conflicts: tuple[FactConflict, ...], accepted: bool)`。
- Produces: `validate_fact_conflicts(snapshot: CapabilitySnapshot, mdl: MdlSnapshot) -> FactConflictReport`。
- Loader invariant: `accepted=False` 时抛出 `CapabilityConfigError`，证据写入异常。
- Metric source invariant: `source_kind=mdl_cube` 时必须提供 `source_reference` 且表达式为空；`source_kind=resources` 时必须有表达式且不得被 Wren cube 重复定义。

**Review Focus:** 两个人眼看起来等价的公式可能语义不同；门禁只能按标准化公式指纹、单位和显式来源判断，不能靠 label 相似度。

- [ ] **Step 1: 写失败测试**

```python
def test_conflicting_metric_owners_fail_before_context_build(self):
    report = validate_fact_conflicts(
        snapshot_with_metric(expression="SUM(area)", source_kind="resources"),
        mdl_with_cube_measure("total_area", "SUM(area)"),
    )
    self.assertFalse(report.accepted)
    self.assertEqual(report.conflicts[0].code, "duplicate_metric_formula_owner")

def test_missing_join_fails_as_structured_conflict(self):
    report = validate_fact_conflicts(
        snapshot_with_requires_join("missing_relationship"),
        empty_mdl_snapshot(),
    )
    self.assertEqual(report.conflicts[0].code, "missing_join")

def test_enum_case_is_generated_only_from_enums_yaml(self):
    cfg = load_capability_config()
    for subject_fields in cfg.documents["fields"].values():
        for field in subject_fields.values():
            if isinstance(field, dict) and field.get("enum_key"):
                self.assertNotIn("CASE ", str(field.get("expression") or "").upper())

def test_metrics_do_not_own_dimension_or_time_lexicons(self):
    metrics = yaml.safe_load(
        (CAPABILITY_ROOT / "metrics.yaml").read_text(encoding="utf-8")
    )["metrics"]
    self.assertNotIn("_lexicon", metrics)
```

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_fact_conflicts tests.query.test_capability_config_guard tests.query.test_mdl_capability_inheritance -v
```

Expected: FAIL，因为冲突报告尚不存在，且 `metrics.yaml` 仍有混合职责的 `_lexicon`。

- [ ] **Step 3: 实现冲突门禁和历史迁移**

`FactConflict` 至少包含 `code`、`subject`、`fact_id`、`sources`、`expected`、`actual`、`evidence`。门禁检查设计中的十项最低条件，并在 `CapabilityRegistry.load()` 完成 MDL 投影后、任何 Planner 上下文构建前执行。

迁移 MDL 已有的公式：

```yaml
count:
  source:
    kind: mdl_cube
    reference: disease_metrics.disease_count
total_area:
  source:
    kind: mdl_cube
    reference: disease_metrics.total_area
average_area:
  source:
    kind: mdl_cube
    reference: disease_metrics.avg_area
```

同步关闭前两轮债务：

1. 枚举 label、storage code、display 和 CASE 只来自 `enums.yaml`；
2. 表中文名只由 `field_overrides.yaml` 拥有，Wren description 只保留业务约束；
3. `subjects.yaml` 不得重新出现 `joins`；
4. 同公式指标必须共享标准 ID 或显式 alias；
5. `fields.yaml` 不得出现手写枚举 CASE；
6. `metrics.yaml` 的 `_lexicon` 中 dimensions 迁到 `dimensions.yaml`，time buckets 迁到 `time_buckets.yaml`，numeric fields/ops 迁到 `predicates.yaml`；重复的 `metric_expressions` 从同一数字字段来源派生，不再保存第二份字段映射。

更新 `nl2sql.py` 的 `_lexicon_section()` 只从对应 owner 文件读取。更新 `resources/README.md` 删除已解决债务，只保留真实未解决项。

新增项目原则并让两份现有原则在开头引用它，明确：事实唯一 owner、提示词内外不得矛盾、迁移必须删旧位置、MDL/resources 平等、冲突失败关闭、验收必须扫描重复和杂糅。

在 `docs/项目数据流讲解.md` 写清 Wren 能力的复用边界：继续复用 MDL 的模型、真实列、计算列、关系子图、`dry-plan` 和物理方言展开；QuerySpec、冻结合同、统一 SQL 审计和 range 校验仍是前置权威。Wren 是执行器与结构验证器，不是绕过业务语义的入口。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_fact_conflicts tests.query.test_capability_config_guard tests.query.test_mdl_capability_inheritance tests.query.test_capability_registry tests.query.test_prompt_contracts -v
```

Expected: PASS，冲突事实不进入 LLM 上下文，职责清单与磁盘一致。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/query/capability_registry/fact_conflicts.py backend/tests/query/test_fact_conflicts.py backend/app/query/capability_registry/models.py backend/app/query/capability_registry/registry.py backend/app/query/capability_registry/loader.py backend/app/query/capability_registry/mdl_facts.py resources/capabilities/metrics.yaml resources/capabilities/fields.yaml resources/capabilities/field_overrides.yaml resources/capabilities/enums.yaml resources/capabilities/dimensions.yaml resources/capabilities/time_buckets.yaml resources/capabilities/predicates.yaml backend/app/services/nl2sql.py wren_projects/road_mysql/cubes/disease_metrics/metadata.yml wren_projects/road_mysql/overrides.yml resources/README.md backend/tests/query/test_capability_config_guard.py backend/tests/query/test_mdl_capability_inheritance.py docs/项目数据流讲解.md docs/项目原则-AI晋级考试与用户裁决.md docs/项目原则-禁止正则与关键词硬匹配.md docs/项目原则-提示词与事实源一致性.md
git commit -m "fix: enforce single-source facts and remove duplicate mappings"
```

---

### Task 9: 预检优先与唯一兜底条件

**Files:**
- Modify: `backend/app/query/mdl_sql/compiler/pipeline.py`
- Modify: `backend/app/query/spec_mdl.py`
- Modify: `backend/app/query/analysis.py`
- Modify: `backend/app/query/service.py`
- Modify: `backend/tests/query/test_mdl_sql_pipeline.py`
- Modify: `backend/tests/query/test_mdl_llm_fallback.py`
- Modify: `backend/tests/query/test_planner_self_repair.py`
- Modify: `backend/tests/query/test_service.py`

**Interfaces:**
- Produces: `preflight_mdl_sql(spec: QuerySpec, *, registry=None, mdl_snapshot=None) -> MdlSqlPipelineResult | None`。
- Contract: `None` 只表示确定性编译器返回 `compiler_not_covered`；其他失败抛 `MdlSqlPipelineError`。
- Consumer: `run_mdl_sql_pipeline()` 与 QuerySpec 规划修复都复用该预检。

**Review Focus:** 确定性 SQL 审计或 `dry-plan` 失败必须回流 QuerySpec；这些失败不能偷偷交给 LLM 重写 SQL。

- [ ] **Step 1: 写失败测试**

```python
def test_only_compiler_not_covered_calls_llm_fallback(self):
    fallback = Mock()
    with patch(
        "app.query.mdl_sql.compiler.pipeline.preflight_mdl_sql",
        return_value=None,
    ):
        result = self.run_pipeline(fallback)
    fallback.assert_called_once()
    self.assertEqual(result.candidate.source, "llm_fallback")

def test_deterministic_audit_failure_does_not_call_fallback(self):
    fallback = Mock()
    with patch(
        "app.query.mdl_sql.compiler.pipeline.preflight_mdl_sql",
        side_effect=MdlSqlPipelineError(
            CompilerFailure(code="invalid_view_shape", stage="audit")
        ),
    ):
        with self.assertRaises(MdlSqlPipelineError):
            self.run_pipeline(fallback)
    fallback.assert_not_called()

def test_dry_plan_failure_does_not_call_fallback(self):
    fallback = Mock()
    with patch(
        "app.query.mdl_sql.compiler.pipeline.preflight_mdl_sql",
        side_effect=MdlSqlPipelineError(
            CompilerFailure(code="dry_plan_failed", stage="dry_plan")
        ),
    ):
        with self.assertRaises(MdlSqlPipelineError):
            self.run_pipeline(fallback)
    fallback.assert_not_called()
```

在 `test_planner_self_repair.py` 增加：预检返回 `invalid_view_shape` 时生成修复票据并再次规划；修复两轮失败后抛出结构化错误，不调用 LLM SQL 生成。

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_mdl_sql_pipeline tests.query.test_mdl_llm_fallback tests.query.test_planner_self_repair tests.query.test_service -v
```

Expected: FAIL，因为当前任何 deterministic 后置失败都会进入 fallback。

- [ ] **Step 3: 实现预检与失败路由**

把 pipeline 拆成：

```text
build contract
-> build narrowed context
-> build relationship subgraph
-> deterministic MDL-SQL
-> contract/relationship/view audit
-> dry-plan
```

`preflight_mdl_sql()` 执行上述步骤但不调用 LLM。只有 deterministic candidate 的 failure code 恰好为 `compiler_not_covered` 时返回 `None`。

`run_mdl_sql_pipeline()`：

1. 预检成功则直接返回；
2. 预检返回 `None` 才调用一次 `generate_mdl_sql_once()`；
3. fallback candidate 继续经过同一合同、审计、声明范围和 `dry-plan`；
4. 其他预检失败立即抛结构化错误。

`_query_plan_compile_error()` 改用同一预检；合同、视图审计和 `dry-plan` 失败转成 `CapabilityIssue`，进入现有 QuerySpec 修复器。

- [ ] **Step 4: 运行测试并确认绿灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_mdl_sql_pipeline tests.query.test_mdl_llm_fallback tests.query.test_planner_self_repair tests.query.test_service tests.query.test_mdl_sql_e2e -v
```

Expected: PASS，`compiler_not_covered` 是唯一 fallback 入口。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/query/mdl_sql/compiler/pipeline.py backend/app/query/spec_mdl.py backend/app/query/analysis.py backend/app/query/service.py backend/tests/query/test_mdl_sql_pipeline.py backend/tests/query/test_mdl_llm_fallback.py backend/tests/query/test_planner_self_repair.py backend/tests/query/test_service.py
git commit -m "fix: route deterministic failures back to query repair"
```

---

### Task 10: 纵向端到端回归与交付验收

**Files:**
- Create: `backend/tests/query/test_fact_source_e2e.py`
- Modify: `backend/tests/query/test_single_turn_e2e.py`
- Modify: `backend/tests/query/test_analysis_contracts.py`
- Modify: `backend/tests/query/test_shared_execution_contracts.py`

**Interfaces:**
- Consumes: Tasks 1-9 的全部稳定接口。
- Produces: 五条真实业务问题在无单题特判下共享同一规划、修复、合同和计算链路。

**Review Focus:** 复合问题最终主体必须服从用户真正询问的对象；上游前置节点可以属于另一主体，不能再次被“单一最终主体”校验误杀。

- [ ] **Step 1: 写失败测试**

在 `test_fact_source_e2e.py` 写五个测试：

```python
def test_province_disease_length_and_density_use_three_nodes(self):
    plan = build_plan_from_mocked_planner(province_density_payload())
    self.assertEqual([node.id for node in plan.nodes], ["q1", "q2", "q3"])
    self.assertEqual(plan.nodes[2].spec.calculation.operation, "divide")
    self.assertEqual(plan.nodes[2].spec.calculation.output.unit, "米/km")

def test_patrol_density_reuses_the_same_calculation_contract(self):
    plan = build_plan_from_mocked_planner(patrol_density_payload())
    self.assertEqual(
        [item.source["field"] for item in plan.nodes[-1].spec.calculation.operands],
        ["count", "active_day_count"],
    )

def test_top_route_then_disease_details_uses_structured_inputs(self):
    plan = build_plan_from_mocked_planner(top_route_then_details_payload())
    binding = plan.nodes[1].spec.input_bindings[0]
    self.assertEqual((binding.from_node, binding.from_field, binding.to_field), ("q1", "route_name", "route_name"))

def test_guodao_sections_with_disease_use_relation_exists(self):
    plan = build_plan_from_mocked_planner(guodao_sections_payload())
    self.assertEqual(plan.nodes[0].spec.filters[1].op, "exists")
    self.assertNotIn("linked_disease_count", plan.nodes[0].spec.metrics)

def test_latest_patrol_and_diseases_keep_patrol_then_disease_chain(self):
    plan = build_plan_from_mocked_planner(latest_patrol_and_diseases_payload())
    self.assertEqual([node.spec.subject for node in plan.nodes], ["patrol", "disease"])
    self.assertEqual(plan.nodes[1].spec.input_bindings[0].from_node, "q1")
```

测试不得断言固定提示词文本，只断言规划后的结构化 QueryPlan、失败码和范围哈希。

- [ ] **Step 2: 运行测试并确认红灯**

Run:

```powershell
.venv\Scripts\python.exe -m unittest tests.query.test_fact_source_e2e tests.query.test_single_turn_e2e tests.query.test_analysis_contracts tests.query.test_shared_execution_contracts -v
```

Expected: FAIL，至少省道双密度、巡检密度或混合主体链仍有一个契约未接上。

- [ ] **Step 3: 只修共享契约，不加题目分支**

根据失败码回到对应 owner 修复；禁止在 `analysis.py`、`agent.py`、`nl2sql.py` 增加问句匹配、题目 ID 或固定业务名称。每条修复必须已经由 Tasks 1-9 定义的共享接口承载。

- [ ] **Step 4: 运行查询全量回归**

Run:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests\query -p "test_*.py" -v
```

Expected: `OK`，无 failed 或 error。

- [ ] **Step 5: 构建前端并检查工作区**

Run:

```powershell
Set-Location ..\frontend
npm run build
Set-Location ..\backend
git diff --check
```

Expected: Vite build 成功；`git diff --check` 无输出。

- [ ] **Step 6: 提交端到端测试**

```powershell
git add backend/tests/query/test_fact_source_e2e.py backend/tests/query/test_single_turn_e2e.py backend/tests/query/test_analysis_contracts.py backend/tests/query/test_shared_execution_contracts.py
git commit -m "test: cover shared fact-source query flows"
```

---

## Final Acceptance

- [ ] Task 1-10 全部完成，每个任务都有独立的 RED→GREEN 输出和提交。
- [ ] 公开 view 只有运行时投影；`top` 只用于内部归一化。
- [ ] 两个 QuerySpec 提示词不手写 view、TopN、时间窗口、关系存在性或主体规则。
- [ ] QuerySpec 只接受 `depends_on` 加 `inputs`，`DEPENDS_ON_` 无可执行分支。
- [ ] 未知 action 失败关闭，`analysis|analyze` 转成 `post_calc + analysis_only`。
- [ ] 审计 `set` 不直接修改草稿，所有修改经过 `RepairIssue.allowed_paths`。
- [ ] `route_length_km` 不再被聚合审计误杀，sum/count 等指标仍要求聚合。
- [ ] `disease_density` 与 `disease_length_density` 口径、单位和操作数分离。
- [ ] 普通巡查密度能由事实和计算器组合，不需要新增固定公式。
- [ ] MDL 与 resources 的公式、单位、关系、字段和枚举冲突在上下文构建前失败。
- [ ] 只有 `compiler_not_covered` 调用一次 LLM MDL-SQL 兜底。
- [ ] `backend/tests/query` 全量 unittest 通过。
- [ ] 前端 `npm run build` 通过。
- [ ] `git diff --check` 通过。
- [ ] 执行“功能杂糅、内容重复、内容矛盾”扫描，并在 `resources/README.md` 只保留真实未解决债务。
- [ ] 代码完成后生成一套全新的 `15 新题 + 5 旧题` 晋级考试，不复用旧题集；达到新题至少 `12/15`、旧题至少 `4/5`、总计至少 `18/20`。

