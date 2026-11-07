# ReAct 有界修复闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `query_data` 和 `post_calc` 的结构化失败验收、有限 SQL 修复、候选工具选择和依赖阻断。

**Architecture:** 新增一个集中的修复策略与失败证据模块；工具返回统一 Observation；ReAct 门控读取结构化失败，通过现有工具注册表执行候选工具；每个请求最多修复两轮，失败后保留有效结果并说明阻断原因。

**Tech Stack:** Python 3.11、FastAPI/Pydantic、现有工具注册表、现有 LLM 角色、YAML 资源加载器、脚本式测试。

**Spec:** `docs/superpowers/specs/2026-11-05-react-repair-loop-design.md`

## Global Constraints

- 第一阶段只覆盖 `query_data`、`post_calc` 和修复策略，不扩展到全部 24 个工具。
- 不为每个工具配置独立 LLM，继续使用 `verify`、`semantic`、`sql`、`knowledge` 角色。
- Controller 只能从策略生成的 2~5 个候选工具中选择。
- 不允许修改用户问题、查询主体、时间、路线、病害类型或桩号范围。
- 每个失败步骤受策略预算约束，整个请求最多执行 2 轮修复。
- Wren 或 LLM 生成的 SQL 必须验收；确定性模板直出继续沿用现有跳过规则。
- `post_calc` 返回“数据不足”时必须是失败观察，不能是 `ok=True`。
- ReAct 失败后不得静默回退静态执行器重跑相同计划。
- 当前工作区有大量未提交改动；每次提交只包含本计划列出的文件。
- 运行测试使用 `backend\.venv\Scripts\python.exe`，不安装 pytest。

## Review Focus

- SQL 成功但最终回复漏掉分组标签或指标时，必须拒绝该结果，不能仅因 `ok=True` 放行。
- `post_calc` 返回“数据不足”但没有异常时，必须产生 `post_calc_empty`。
- Wren 运行时 SQL 错误必须原样进入下一次 SQL 生成，不能在重试前清空。
- Controller 返回未授权工具、非法参数或完全相同的重试时必须拒绝。
- 上游失败且有下游依赖时不得继续分析；独立分支失败不得删除其他已成功结果。

---

### Task 1: FailureEvidence 与修复策略

**Files:**

- Create: `backend/app/services/react_repair.py`
- Create: `resources/repair_policy.yaml`
- Create: `backend/test_react_repair.py`

**Interfaces:**

- Consumes: `resource_loader.load_yaml(name: str, default: Any = None) -> Any`
- Produces: `build_failure(failure_code: str, *, stage: str, reason: str, evidence: dict[str, Any], attempt: int = 1) -> dict[str, Any]`
- Produces: `candidate_tool_names(failure_code: str) -> list[str]`
- Produces: `format_repair_feedback(failure: dict[str, Any]) -> str`
- Produces: `GLOBAL_REPAIR_LIMIT: int = 2`

- [ ] **Step 1: Write the failing policy and failure-evidence tests**

Add `backend/test_react_repair.py` with:

```python
from app.services import react_repair


def test_build_failure_uses_policy_and_keeps_evidence():
    failure = react_repair.build_failure(
        "sql_runtime_error",
        stage="execute",
        reason="ERROR 1064 (42000)",
        evidence={"sql": "SELECT 1", "rows": []},
    )
    assert failure["accepted"] is False
    assert failure["failure_code"] == "sql_runtime_error"
    assert failure["retryable"] is True
    assert failure["candidate_tools"] == ["query_data"]
    assert failure["max_repair_rounds"] == 2
    assert failure["evidence"]["sql"] == "SELECT 1"
    assert "ERROR 1064" in react_repair.format_repair_feedback(failure)


def test_unknown_failure_code_fails_closed():
    failure = react_repair.build_failure(
        "not_registered",
        stage="execute",
        reason="unknown",
        evidence={},
    )
    assert failure["failure_code"] == "tool_error"
    assert failure["retryable"] is False
    assert failure["candidate_tools"] == ["ask_user"]


def test_post_calc_candidates_are_limited():
    assert react_repair.candidate_tool_names("post_calc_empty") == [
        "post_calc",
        "ask_user",
    ]
```

- [ ] **Step 2: Run the new tests and verify failure**

Run:

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_build_failure_uses_policy_and_keeps_evidence()"
```

Expected: FAIL because `app.services.react_repair` does not exist.

- [ ] **Step 3: Implement the failure module and policy**

Create `resources/repair_policy.yaml` with the exact mapping from the spec:

```yaml
sql_runtime_error:
  candidates: [query_data]
  repair_role: sql
  max_repair_rounds: 2
  retryable: true
sql_semantic_mismatch:
  candidates: [query_data]
  repair_role: sql
  max_repair_rounds: 2
  retryable: true
result_shape_mismatch:
  candidates: [query_data, check_coverage]
  repair_role: sql
  max_repair_rounds: 2
  retryable: true
empty_unexpected:
  candidates: [check_coverage, query_data, ask_user]
  repair_role: semantic
  max_repair_rounds: 1
  retryable: true
post_calc_empty:
  candidates: [post_calc, ask_user]
  repair_role: semantic
  max_repair_rounds: 2
  retryable: true
missing_dependency:
  candidates: [ask_user]
  repair_role: null
  max_repair_rounds: 0
  retryable: false
tool_error:
  candidates: [ask_user]
  repair_role: null
  max_repair_rounds: 0
  retryable: false
```

Create `backend/app/services/react_repair.py` with these signatures
(function bodies are intentionally omitted; the tests and rules below define
their behavior):

```python
GLOBAL_REPAIR_LIMIT = 2


def candidate_tool_names(failure_code: str) -> list[str]:
    ...


def build_failure(
    failure_code: str,
    *,
    stage: str,
    reason: str,
    evidence: dict[str, Any],
    attempt: int = 1,
) -> dict[str, Any]:
    ...


def format_repair_feedback(failure: dict[str, Any]) -> str:
    ...
```

Implementation rules:

- Load the policy with `resource_loader.load_yaml("repair_policy.yaml", {})`.
- Treat an unknown code as `tool_error`.
- Reject a malformed policy entry by falling back to the explicit `tool_error` rule.
- Copy only JSON-compatible evidence values.
- `format_repair_feedback` must include failure code, reason, SQL if present, columns, and at most five result rows.

- [ ] **Step 4: Run the new tests and verify success**

Run:

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
```

Expected: PASS. Add the three test calls to `if __name__ == "__main__":`.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/react_repair.py resources/repair_policy.yaml backend/test_react_repair.py
git commit -m "feat: add structured react repair failures"
```

---

### Task 2: Observation 失败字段与 post_calc 空结果

**Files:**

- Modify: `backend/app/services/tools/observation.py`
- Modify: `backend/app/services/agent.py:2480-2533`
- Modify: `backend/app/services/tools/big/post_calc.py`
- Modify: `backend/test_react_repair.py`
- Modify: `backend/test_tool_registry.py`

**Interfaces:**

- Consumes: Task 1 `build_failure(...)` and `format_repair_feedback(...)`
- Produces: `observation(..., accepted: bool | None = None, failure: dict[str, Any] | None = None) -> dict[str, Any]`
- Produces: `_post_calc_payload(...)["accepted"]: bool`

- [ ] **Step 1: Write failing observation and post_calc tests**

Add:

```python
from unittest.mock import patch

from app.services import agent
from app.services.tools import observation
from app.services.tools.big import post_calc as post_calc_tool


def test_observation_exposes_failure_without_removing_legacy_fields():
    failure = {"failure_code": "post_calc_empty", "accepted": False}
    result = observation.observation(
        "post_calc",
        ok=False,
        summary="数据不足",
        failure=failure,
    )
    assert result["accepted"] is False
    assert result["failure"] == failure
    assert result["ok"] is False
    assert "coverage" in result


def test_post_calc_empty_is_failed_observation():
    payload = {
        "reply": "这一步需要基于前面的结果做计算，但数据不足，算不出来。",
        "source": "calc",
        "engine": "post_calc",
        "accepted": False,
        "columns": [],
        "rows": [],
    }
    with patch.object(agent, "_post_calc_payload", return_value=payload):
        result = post_calc_tool.post_calc(
            {"question": "计算密度", "depends_on": [1]},
            {1: {"rows": [{"数量": "10", "长度": "2"}]}},
        )
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["failure"]["failure_code"] == "post_calc_empty"
    assert result["failure"]["evidence"]["step_question"] == "计算密度"
```

- [ ] **Step 2: Run the tests and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_observation_exposes_failure_without_removing_legacy_fields()"
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_post_calc_empty_is_failed_observation()"
```

Expected: first test FAIL because `observation()` has no `failure`; second test FAIL because `post_calc` returns `ok=True`.

- [ ] **Step 3: Implement accepted/failure observation fields**

Add keyword-only parameters to `observation()`:

```python
accepted: bool | None = None,
failure: dict[str, Any] | None = None,
```

Return:

```python
"accepted": bool(ok) if accepted is None else bool(accepted),
"failure": dict(failure) if failure else None,
```

Do not remove existing keys.

In `_post_calc_payload`, add `"accepted": True` to successful returns and `"accepted": False` to the final fallback return.

In `post_calc.py`, map the payload result to:

```python
accepted = bool(payload.get("accepted", True))
```

When false, build `post_calc_empty` evidence with the current task question, task id, dependency ids, and upstream result summaries. Return `accepted=False` and `ok=False`.

- [ ] **Step 4: Run focused and existing tests**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe -c "import test_tool_registry as t; t.test_knowledge_profile_and_post_calc_tools()"
```

Expected: PASS. The successful two-route comparison remains `ok=True` and contains `162`.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/tools/observation.py backend/app/services/agent.py backend/app/services/tools/big/post_calc.py backend/test_react_repair.py backend/test_tool_registry.py
git commit -m "fix: fail closed when post calculation is empty"
```

---

### Task 3: query_data 结果验收

**Files:**

- Modify: `backend/app/services/tools/big/query_data.py`
- Modify: `backend/test_react_repair.py`
- Modify: `backend/test_react_agent.py`

**Interfaces:**

- Consumes: `nl2sql.verify_answer(question: str, sql: str, result: dict[str, Any], reply: str = "") -> dict[str, Any]`
- Consumes: Task 1 `build_failure(...)`
- Produces: `_verification_failure(question: str, payload: dict[str, Any]) -> dict[str, Any] | None`

- [ ] **Step 1: Write failing verifier tests**

Add tests that patch `agent._build_query_result` and `query_data_tool.nl2sql.verify_answer`:

```python
def test_query_data_rejects_semantic_mismatch():
    payload = {
        "question": "按路线分组统计病害数",
        "sql": "SELECT problem_type, COUNT(*) FROM disease GROUP BY problem_type",
        "rows": [{"病害类型": "泛油", "数量": "10"}],
        "columns": ["病害类型", "数量"],
        "reply": "分布：泛油 10 个",
        "source": "mysql",
        "engine": "wren",
        "result_id": "wrong-route",
    }
    verdict = {
        "correct": False,
        "issue_type": "missing_dimensions",
        "reason": "用户要求按路线分组，结果没有路线列",
    }
    with (
        patch.object(agent, "_fill_placeholders", return_value=("按路线分组统计病害数", [])),
        patch.object(agent, "_build_query_result", return_value=payload),
        patch.object(query_data_tool.nl2sql, "verify_answer", return_value=verdict),
        patch.object(query_data_tool, "save_result"),
    ):
        result = query_data_tool.query_data("按路线分组统计病害数")
    assert result["ok"] is False
    assert result["accepted"] is False
    assert result["failure"]["failure_code"] == "sql_semantic_mismatch"
    assert result["failure"]["evidence"]["sql"] == payload["sql"]
    assert result["failure"]["candidate_tools"] == ["query_data"]


def test_query_data_accepts_correct_result():
    payload = {
        "question": "西乡段泛油有多少处",
        "sql": "SELECT COUNT(*) AS 数量 FROM disease",
        "rows": [{"数量": "534"}],
        "columns": ["数量"],
        "reply": "共 534 处。",
        "source": "mysql",
        "engine": "wren",
        "result_id": "accepted",
    }
    with (
        patch.object(agent, "_fill_placeholders", return_value=("西乡段泛油有多少处", [])),
        patch.object(agent, "_build_query_result", return_value=payload),
        patch.object(
            query_data_tool.nl2sql,
            "verify_answer",
            return_value={"correct": True, "issue_type": "none", "reason": ""},
        ),
        patch.object(query_data_tool, "save_result"),
    ):
        result = query_data_tool.query_data("西乡段泛油有多少处")
    assert result["ok"] is True
    assert result["accepted"] is True
    assert result["failure"] is None
```

- [ ] **Step 2: Run the tests and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_query_data_rejects_semantic_mismatch()"
```

Expected: FAIL because `query_data` never calls `verify_answer`.

- [ ] **Step 3: Implement `_verification_failure` and wire it into `query_data`**

In `query_data.py`, import `nl2sql` and `build_failure`.

`_verification_failure` rules:

- Return `None` when no SQL exists or the engine is in `agent._DETERMINISTIC_ENGINES`.
- Call `verify_answer(filled_question, sql, payload, reply)` for Wren/LLM SQL.
- When `correct` is false, build `sql_semantic_mismatch` with:
  - `stage="verify"`
  - `reason=verdict["reason"]`
  - `evidence` containing `step_question`, `sql`, `columns`, first 10 `rows`, and `verdict`.
- Map `no_aggregation`, `no_grouping`, `missing_dimensions`,
  `reasoning_insufficient`, `missing_condition`, `wrong_subject`,
  `wrong_metric`, and `wrong_metric_scope` to `sql_semantic_mismatch`.

After `_build_query_result` returns and before constructing the final observation:

- Save the payload as today so diagnostics remain available.
- Set `failure = _verification_failure(filled_question, payload)`.
- Set `accepted = failure is None` and `ok = accepted` for successfully executed SQL.
- Preserve the existing failure/error handling for `ClarifyNeeded` and exceptions.
- Put `failure` in the observation and keep SQL/sample evidence inside it.

- [ ] **Step 4: Run focused and ReAct tests**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe test_react_agent.py
```

Expected: PASS. Existing `query_data` observations without a failure now include
`accepted=True` and `failure=None`.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/tools/big/query_data.py backend/test_react_repair.py backend/test_react_agent.py
git commit -m "feat: verify query_data observations"
```

---

### Task 4: SQL 修复反馈进入生成器

**Files:**

- Modify: `backend/app/services/agent.py:1711-2065`
- Modify: `backend/app/services/tools/big/query_data.py`
- Modify: `backend/test_react_repair.py`
- Modify: `backend/test_react_agent.py`

**Interfaces:**

- Consumes: Task 1 `format_repair_feedback(failure: dict[str, Any]) -> str`
- Modifies: `_build_query_result(..., repair_feedback: dict[str, Any] | None = None) -> dict[str, Any]`
- Modifies: `query_data(..., repair_feedback: dict[str, Any] | None = None) -> dict[str, Any]`

- [ ] **Step 1: Write failing pass-through and feedback tests**

Add tests:

```python
def test_query_data_passes_repair_feedback_to_builder():
    feedback = {"failure_code": "sql_runtime_error", "reason": "ERROR 1064"}
    payload = {
        "sql": "SELECT 1",
        "rows": [{"x": 1}],
        "columns": ["x"],
        "reply": "1",
        "source": "mysql",
        "engine": "wren",
        "result_id": "fixed",
    }
    with (
        patch.object(agent, "_fill_placeholders", return_value=("q", [])),
        patch.object(agent, "_build_query_result", return_value=payload) as build,
        patch.object(
            query_data_tool.nl2sql,
            "verify_answer",
            return_value={"correct": True, "issue_type": "none", "reason": ""},
        ),
        patch.object(query_data_tool, "save_result"),
    ):
        query_data_tool.query_data("q", repair_feedback=feedback)
    assert build.call_args.kwargs["repair_feedback"] == feedback


def test_format_repair_feedback_includes_runtime_error():
    failure = react_repair.build_failure(
        "sql_runtime_error",
        stage="execute",
        reason="ERROR 1064 near AS 平均密度(处/公里)",
        evidence={"sql": "SELECT 1 AS 平均密度(处/公里)"},
    )
    text = react_repair.format_repair_feedback(failure)
    assert "ERROR 1064" in text
    assert "平均密度" in text
```

- [ ] **Step 2: Run and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_query_data_passes_repair_feedback_to_builder()"
```

Expected: FAIL because `query_data` and `_build_query_result` do not accept `repair_feedback`.

- [ ] **Step 3: Implement feedback propagation**

Add `repair_feedback: dict[str, Any] | None = None` to both signatures and to
`query_data`'s `input_schema`.

In `_build_query_result`:

- Initialize `last_error = react_repair.format_repair_feedback(repair_feedback)` instead of `""`.
- Pass this value to the existing Wren `generate_mdl_sql(..., feedback=last_error)` call.
- Keep passing `last_error` to `generate_with_context(..., feedback=last_error)`.
- In the Wren execution `except` block, set:

```python
last_error = f"上一版 SQL 执行失败：{type(exc).__name__}: {exc}"
```

Do not clear the runtime error before the fallback generator runs.

In `query_data.py`, pass `repair_feedback` through to `_build_query_result`.

- [ ] **Step 4: Run focused tests**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe test_react_agent.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/agent.py backend/app/services/tools/big/query_data.py backend/test_react_repair.py backend/test_react_agent.py
git commit -m "fix: propagate sql repair evidence"
```

---

### Task 5: Controller 候选工具选择与执行

**Files:**

- Modify: `backend/app/services/react_agent.py:178-252`
- Modify: `prompts/react_controller.md`
- Modify: `backend/test_react_repair.py`
- Modify: `backend/test_react_agent.py`

**Interfaces:**

- Consumes: Task 1 `candidate_tool_names(...)`, `GLOBAL_REPAIR_LIMIT`
- Consumes: `tools.get_tool(name: str) -> Tool`
- Produces: `_allowed_tools(failure: dict[str, Any]) -> list[dict[str, Any]]`
- Produces: `_validate_decision(decision: dict[str, Any], allowed: list[str], previous_calls: set[tuple[str, str]]) -> str`
- Produces controller decision fields: `action`, `tool`, `arguments`, `expected_effect`, `message`

- [ ] **Step 1: Write failing shortlist and validation tests**

Add:

```python
def test_controller_shortlist_contains_only_policy_tools():
    failure = react_repair.build_failure(
        "post_calc_empty",
        stage="post_calc",
        reason="empty",
        evidence={},
    )
    allowed = react_agent._allowed_tools(failure)
    assert [item["name"] for item in allowed] == ["post_calc", "ask_user"]
    assert all("input_schema" in item for item in allowed)


def test_controller_rejects_unlisted_tool_and_duplicate_call():
    allowed = ["post_calc", "ask_user"]
    bad = {"action": "call_tool", "tool": "query_data", "arguments": {}}
    assert "不允许" in react_agent._validate_decision(bad, allowed, set())
    duplicate = {
        "action": "call_tool",
        "tool": "post_calc",
        "arguments": {"task": {"question": "x"}, "results": {}},
    }
    signature = ('post_calc', '{"results": {}, "task": {"question": "x"}}')
    assert "重复" in react_agent._validate_decision(
        duplicate,
        allowed,
        {signature},
    )
```

- [ ] **Step 2: Run and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_repair as t; t.test_controller_shortlist_contains_only_policy_tools()"
```

Expected: FAIL because `_allowed_tools` does not exist.

- [ ] **Step 3: Implement shortlist, validation, and prompts**

`_allowed_tools`:

- Read candidate names from the failure evidence.
- For each existing tool, return `name`, `layer`, `domain`, `description`,
  and `input_schema`.
- Skip missing tools and log a structured error.

`_validate_decision`:

- Accept `call_tool`, `retry_tool`, `ask_user`, and `abort`.
- Require `tool` for `call_tool` and `retry_tool`.
- Reject tools outside the allowed list.
- Require every `input_schema.required` argument to exist for non-step tools.
- Serialize arguments with sorted keys and reject an exact repeat in `previous_calls`.

Update `_deviation_decision` payload to include:

- `failure`
- `allowed_tools`
- `previous_attempts`
- `previous_calls`

Update `prompts/react_controller.md` to output:

```json
{
  "thought": "一句话诊断",
  "action": "call_tool",
  "tool": "check_coverage",
  "arguments": {},
  "expected_effect": "调用后预期获得的新证据",
  "message": ""
}
```

Preserve `retry_atomic` compatibility for existing tests and callers.

- [ ] **Step 4: Run controller tests**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe test_react_agent.py
```

Expected: PASS. Existing `retry_atomic` tests remain green.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/react_agent.py prompts/react_controller.md backend/test_react_repair.py backend/test_react_agent.py
git commit -m "feat: expose repair tools to react controller"
```

---

### Task 6: 有限修复重试

**Files:**

- Modify: `backend/app/services/react_agent.py:387-596`
- Modify: `backend/test_react_agent.py`

**Interfaces:**

- Consumes: Task 5 `_allowed_tools` and `_validate_decision`
- Consumes: `tools.run_tool(name: str, **kwargs: Any) -> dict[str, Any]`
- Produces: `_execute_step(..., repair_feedback: dict[str, Any] | None = None)`
- Produces: accepted sections include `accepted`, `failure`, and `attempt`
- Produces: final payload includes `repair_history: list[dict[str, Any]]`
- Produces: controller payload includes `helper_observations: list[dict[str, Any]]`

- [ ] **Step 1: Write a failing bounded-retry test**

Add to `backend/test_react_agent.py`:

```python
def test_controller_retry_uses_failure_feedback_and_accepts_second_result():
    calls = {"controller": 0, "query": 0}
    feedbacks = []
    failure = react_repair.build_failure(
        "sql_runtime_error",
        stage="execute",
        reason="ERROR 1064",
        evidence={"sql": "SELECT AS 平均密度(处/公里)"},
    )
    failed_obs = {
        "ok": False,
        "accepted": False,
        "tool": "query_data",
        "summary": "SQL 语法错误",
        "data": {
            "row_count": 0,
            "columns": [],
            "preview_rows": [],
            "produced": {},
            "truncated": False,
        },
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": "ERROR 1064",
        "next_hint": "修正 SQL",
        "failure": failure,
    }
    fixed_obs = {
        "ok": True,
        "accepted": True,
        "tool": "query_data",
        "summary": "共 534 处。",
        "data": {
            "row_count": 1,
            "columns": ["数量"],
            "preview_rows": [{"数量": "534"}],
            "produced": {},
            "truncated": False,
            "result_id": "fixed-result",
        },
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": None,
        "next_hint": "",
        "failure": None,
    }

    def fake_tool(name, **kwargs):
        if name != "query_data":
            raise AssertionError(f"unexpected tool: {name}")
        calls["query"] += 1
        feedbacks.append(kwargs.get("repair_feedback"))
        return failed_obs if calls["query"] == 1 else fixed_obs

    def controller(payload):
        calls["controller"] += 1
        assert payload["failure"]["failure_code"] == "sql_runtime_error"
        return {
            "action": "retry_tool",
            "tool": "query_data",
            "arguments": {},
            "expected_effect": "修正 SQL 语法",
        }

    cached = {
        "fixed-result": {
            "sql": "SELECT COUNT(*) AS 数量",
            "rows": [{"数量": "534"}],
            "columns": ["数量"],
            "source": "mysql",
            "engine": "wren",
            "shape": "count",
            "spec": {"shape": "count"},
            "lineage": {},
        }
    }
    plan = {"final_goal": "西乡段泛油数量", "steps": [_chain_plan()["steps"][0]]}
    with (
        patch.object(react_agent.tools, "run_tool", side_effect=fake_tool),
        patch.object(react_agent, "get_result", side_effect=lambda key: cached.get(key)),
    ):
        payload = react_agent.run("西乡段泛油有多少处？", plan, controller)
    assert calls["query"] == 2
    assert calls["controller"] == 1
    assert feedbacks[0] is None
    assert feedbacks[1]["failure_code"] == "sql_runtime_error"
    assert payload["sections"][0]["accepted"] is True
    assert payload["partial"] is False
    assert payload["repair_history"][0]["failure_code"] == "sql_runtime_error"


def test_controller_can_call_helper_before_step_retry():
    calls = {"controller": 0, "query": 0, "coverage": 0}
    failure = react_repair.build_failure(
        "result_shape_mismatch",
        stage="verify",
        reason="结果缺少路线标签",
        evidence={"step_question": "按路线统计"},
    )
    failed_obs = {
        "ok": False,
        "accepted": False,
        "tool": "query_data",
        "summary": "结果缺少路线标签",
        "data": {
            "row_count": 1,
            "columns": ["数量"],
            "preview_rows": [{"数量": "10"}],
            "produced": {},
        },
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": "结果缺少路线标签",
        "next_hint": "",
        "failure": failure,
    }
    helper_obs = {
        "ok": True,
        "accepted": True,
        "tool": "check_coverage",
        "summary": "查询范围在覆盖内",
        "data": {"row_count": 0, "columns": [], "preview_rows": [], "produced": {}},
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": None,
        "next_hint": "",
        "failure": None,
    }
    fixed_obs = {
        "ok": True,
        "accepted": True,
        "tool": "query_data",
        "summary": "路线分布已修正",
        "data": {
            "row_count": 1,
            "columns": ["路线", "数量"],
            "preview_rows": [{"路线": "西乡段", "数量": "10"}],
            "produced": {},
            "result_id": "fixed-helper-result",
        },
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": None,
        "next_hint": "",
        "failure": None,
    }

    def fake_tool(name, **kwargs):
        if name == "check_coverage":
            calls["coverage"] += 1
            return helper_obs
        if name == "query_data":
            calls["query"] += 1
            return failed_obs if calls["query"] == 1 else fixed_obs
        raise AssertionError(f"unexpected tool: {name}")

    def controller(payload):
        calls["controller"] += 1
        if calls["controller"] == 1:
            return {
                "action": "call_tool",
                "tool": "check_coverage",
                "arguments": {"plan": {"kind": "mysql", "slots": {}}},
                "expected_effect": "确认查询范围",
            }
        assert payload["helper_observations"][0]["tool"] == "check_coverage"
        return {
            "action": "retry_tool",
            "tool": "query_data",
            "arguments": {},
            "expected_effect": "修正路线分组",
        }

    cached = {
        "fixed-helper-result": {
            "sql": "SELECT route_name, COUNT(*) AS count FROM disease GROUP BY route_name",
            "rows": [{"路线": "西乡段", "数量": "10"}],
            "columns": ["路线", "数量"],
            "source": "mysql",
            "engine": "wren",
            "shape": "distribution",
            "spec": {"shape": "distribution"},
            "lineage": {},
        }
    }
    plan = {
        "final_goal": "路线分布",
        "steps": [{"id": 1, "type": "data", "question": "按路线统计"}],
    }
    with (
        patch.object(react_agent.tools, "run_tool", side_effect=fake_tool),
        patch.object(react_agent, "get_result", side_effect=lambda key: cached.get(key)),
    ):
        payload = react_agent.run("按路线统计", plan, controller)
    assert calls == {"controller": 2, "query": 2, "coverage": 1}
    assert payload["sections"][0]["accepted"] is True
    assert len(payload["repair_history"]) == 2
```

- [ ] **Step 2: Run and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_agent as t; t.test_controller_retry_uses_failure_feedback_and_accepts_second_result()"
```

Expected: FAIL because the current loop passes no `repair_feedback` and has no
`retry_tool` action.

- [ ] **Step 3: Implement the bounded attempt loop**

Refactor `_execute_step` to accept:

```python
repair_feedback: dict[str, Any] | None = None
```

When the step tool is `query_data`, include `repair_feedback` in its arguments.

In `run`:

1. Replace `retry_count` with `repair_rounds`, `repair_history`, and
   `previous_calls`.
2. Use an inner attempt loop for the current step.
3. Obtain a failure from `observation["failure"]` first; retain the current
   missing-produce/error checks as fallback evidence.
4. Stop repair when the global budget reaches `GLOBAL_REPAIR_LIMIT` or the
   failure's own budget is exhausted.
5. For `retry_tool` on the current step, run the same tool with the original
   arguments plus `repair_feedback=failure`.
6. For `call_tool`, run only an allowed helper tool and add its observation to
   `helper_observations` in the next controller payload; do not treat a
   successful helper as a successful plan step.
7. Save every attempt to `repair_history`; add `accepted`, `failure`, and
   `attempt` to accepted sections.
8. Mark a retried result as the current step result and re-run the gate.

- [ ] **Step 4: Run the full focused suite**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_agent.py
& .\.venv\Scripts\python.exe test_react_repair.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/react_agent.py backend/test_react_agent.py
git commit -m "feat: retry react steps with failure feedback"
```

---

### Task 7: 依赖阻断、部分成功与显式失败

**Files:**

- Modify: `backend/app/services/react_agent.py:387-596`
- Modify: `backend/app/services/agent.py:2719-2747`
- Modify: `backend/test_react_agent.py`

**Interfaces:**

- Consumes: Task 6 bounded attempt loop and `repair_history`
- Produces: every section has `accepted: bool`, `failure: dict | None`, and `attempt: int`
- Produces: final payload includes `partial: bool` and `failures: list[dict[str, Any]]`

- [ ] **Step 1: Write failing dependency and explicit-failure tests**

Add:

```python
def test_failed_upstream_blocks_dependent_step():
    failure = react_repair.build_failure(
        "sql_runtime_error",
        stage="execute",
        reason="ERROR 1064",
        evidence={"sql": "SELECT broken"},
    )
    failed_obs = {
        "ok": False,
        "accepted": False,
        "tool": "query_data",
        "summary": "SQL 失败",
        "data": {"row_count": 0, "columns": [], "preview_rows": [], "produced": {}},
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": "ERROR 1064",
        "next_hint": "",
        "failure": failure,
    }
    calls = {"query": 0}

    def fake_tool(name, **kwargs):
        calls["query"] += 1
        return failed_obs

    def controller(payload):
        return {"action": "abort", "message": "SQL 无法修复"}

    with patch.object(react_agent.tools, "run_tool", side_effect=fake_tool):
        payload = react_agent.run("链式题", _chain_plan(), controller)
    assert calls["query"] == 1
    assert payload["source"] == "error"
    assert payload["failed_step"]["id"] == 1
    assert payload["partial"] is True
    assert payload["sections"] == []


def test_failed_independent_step_does_not_delete_other_sections():
    failed = {
        "ok": False,
        "accepted": False,
        "tool": "query_data",
        "summary": "第一条失败",
        "data": {"row_count": 0, "columns": [], "preview_rows": [], "produced": {}},
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": "failed",
        "next_hint": "",
        "failure": react_repair.build_failure(
            "sql_semantic_mismatch",
            stage="verify",
            reason="第一条失败",
            evidence={},
        ),
    }
    accepted = {
        "ok": True,
        "accepted": True,
        "tool": "query_data",
        "summary": "第二条成功",
        "data": {"row_count": 1, "columns": ["x"], "preview_rows": [{"x": 1}], "produced": {}},
        "coverage": {"asked_range": {}, "actual_range": {}, "empty": False},
        "error": None,
        "next_hint": "",
        "failure": None,
    }

    def fake_tool(name, **kwargs):
        return failed if kwargs.get("question") == "问题A" else accepted

    def controller(payload):
        return {"action": "skip_step"}

    plan = {
        "final_goal": "两个独立问题",
        "steps": [
            {"id": 1, "type": "data", "question": "问题A"},
            {"id": 2, "type": "data", "question": "问题B"},
        ],
    }
    with patch.object(react_agent.tools, "run_tool", side_effect=fake_tool):
        payload = react_agent.run("问题A；问题B", plan, controller)
    assert len(payload["sections"]) == 1
    assert payload["partial"] is True
    assert payload["failures"][0]["step_id"] == 1


def test_react_exception_returns_explicit_failure_not_none():
    with patch.object(react_agent, "_execute_step", side_effect=RuntimeError("boom")):
        payload = react_agent.run("x", _chain_plan())
    assert payload is not None
    assert payload["source"] == "error"
    assert "boom" in payload["error"]
```

- [ ] **Step 2: Run and verify failure**

```powershell
cd backend
& .\.venv\Scripts\python.exe -c "import test_react_agent as t; t.test_react_exception_returns_explicit_failure_not_none()"
```

Expected: FAIL because `run()` returns `None` after catching the exception.

- [ ] **Step 3: Implement dependency blocking and partial results**

In `run`:

1. If a failed step has dependents, stop and return `_failure_payload` with
   `partial=True` and `failures` containing the current failure.
2. If it has no dependents, append a failed section, continue to the next
   independent plan step, and keep a cumulative `failures` list.
3. If the plan finishes with one or more independent failures, return a
   partial payload with successful sections, failures, and repair history.
4. Add `accepted`, `failure`, and `attempt` to every section.
5. Convert caught `react_agent.run` exceptions into an explicit error payload
   instead of returning `None`.

In `agent._handle_composite`, return any non-`None` ReAct payload, including an
error or partial payload. Do not continue to the static executor after a ReAct
execution failure.

- [ ] **Step 4: Run the full focused suite**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_agent.py
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe -c "import test_tool_registry as t; t.test_catalog_file_is_synchronized()"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/react_agent.py backend/app/services/agent.py backend/test_react_agent.py
git commit -m "feat: block dependent steps after repair failure"
```

---

### Task 8: 四题实库验收脚本与最终回归

**Files:**

- Create: `backend/run_react_repair_eval.py`
- Modify: `backend/test_react_repair.py`

**Interfaces:**

- Consumes: running service at `http://127.0.0.1:18730/api/chat`
- Produces: process exit code `0` only when all four first-stage contracts pass

- [ ] **Step 1: Create the live evaluation script**

Use `httpx.post` with `force_ai=True` and `use_saved=False`.

The four cases and contracts are:

1. `西乡段泛油有多少处？`
   - Must not return a SQL syntax/runtime error.
   - Must contain a numeric count in the final reply.
2. `按路线分组，统计每条路线的病害总数、病害总面积、平均单处病害面积，并关联获取该路线的长度`
   - If complete, the data section must expose the five requested columns.
   - If a downstream calculation fails, the payload must be `partial=True` or
     `source="error"` and identify `post_calc_empty`; it must not present
     “数据不足” as a successful complete answer.
3. `按路线和病害类型分组，统计各路线下每种病害类型的数量，找出每条路线数量最多的主要病害类型`
   - The first data section must contain route, disease type, and count columns.
   - A failed top-per-route calculation must set `partial=True` and include a
     failure reason; it must not silently claim the grouped result is complete.
4. `数据库里各个月份的巡查次数分别是多少？总共多少次？`
   - The month counts must sum to the total.
   - The reply must use the unit “次”, not “处”, for patrol counts.

Print one JSON object per case with:

```json
{
  "case": "simple_count",
  "ok": true,
  "source": "mysql",
  "failure_codes": [],
  "partial": false,
  "reply": "实际回复摘要"
}
```

At the end print a summary and exit nonzero if any contract fails.

- [ ] **Step 2: Run the live evaluation**

```powershell
cd backend
& .\.venv\Scripts\python.exe run_react_repair_eval.py
```

Expected: all four cases print `ok: true`. Run only against a healthy local
service and the read-only database.

- [ ] **Step 3: Fix only defects demonstrated by the four contracts**

Allowed fixes:

- Preserve failure evidence in the final partial payload.
- Correct `post_calc_empty` propagation.
- Keep the SQL runtime error in the retry feedback.
- Ensure the month-count total is computed from returned rows and uses the
  patrol unit.

Do not expand scope to the remaining three questions.

- [ ] **Step 4: Run all regression checks**

```powershell
cd backend
& .\.venv\Scripts\python.exe test_react_repair.py
& .\.venv\Scripts\python.exe test_react_agent.py
& .\.venv\Scripts\python.exe test_tool_registry.py
& .\.venv\Scripts\python.exe run_react_repair_eval.py
```

Expected: all three Python test scripts print their “checks passed” message and
the live script exits `0` with four `ok: true` cases.

- [ ] **Step 5: Commit**

```powershell
git add backend/run_react_repair_eval.py backend/test_react_repair.py
git commit -m "test: add react repair acceptance cases"
```

---

## Execution Notes

- Run all commands from the repository root unless the command starts with
  `cd backend`.
- Do not stage unrelated working-tree changes.
- If a live case reveals a behavior outside the four defined contracts, record
  it for phase two rather than expanding this plan.
- The first four cases prove control-flow correctness, not final answer quality
  for every query. Perfect multi-row prose summaries remain phase two work.
