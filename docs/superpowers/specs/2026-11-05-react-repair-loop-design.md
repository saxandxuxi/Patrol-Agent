# ReAct 有界修复闭环设计

> 日期：2026-11-05
> 状态：设计待评审
> 范围：第一阶段，仅覆盖 `query_data`、`post_calc`、失败证据和工具候选策略

## 1. 背景

当前 ReAct 实际是“LLM 规划 + 代码按冻结计划执行 + 工具异常时询问一次 LLM”，不是持续监督闭环。

2026-11-05 对 7 条查询做了只读基线测试，主要问题如下：

- `西乡段泛油有多少处？` 因 Wren 生成 SQL 的中文别名语法错误而失败，重试没有拿到有效的数据库错误反馈。
- 两张按路线分组的题目已经查到正确的多行结果，但总结不完整，后续 `post_calc` 返回 0 行仍被当作成功。
- `11月2日巡检了哪些线路？` 已经查到数据，但最终回复没有列出线路，只说“去结果工作台查看”。
- `近三月有多少巡检记录？` 出现明显的 Join 放大和聚合异常，仍继续生成总结。
- `裂缝集中在哪几个月？哪些路？...` 的月份计划只取了 Top1，后续密度计算又因空结果断开。
- `数据库里各个月份的巡查次数...` 基本正确，当前数据为 83 + 62 + 41 + 20 = 206 次。仓库文档中的旧答案 147 次已经过期，不能作为当前库的硬编码期望值。

根因不是缺少一个新提示词，而是以下控制流断点：

1. `post_calc` 在“数据不足，算不出来”时仍返回成功观察。
2. ReAct 门控只检查工具异常和 `produces`，不检查空结果、后置计算失败或结果语义。
3. Wren SQL 执行失败时清空了错误，后续 SQL 重写拿不到失败证据。
4. `verify_answer()` 已经存在，但没有接入执行链。
5. 控制器没有读取工具目录，只认识 4 个硬编码别名。
6. 上游步骤失败后，下游分析和后置计算仍可能继续，并把空结果拼进最终答案。

## 2. 目标

第一阶段建立一个有界、可观察、可测试的修复闭环：

```text
执行步骤
  -> 生成结构化验收结论
  -> 通过：进入下一步
  -> 不通过：生成 FailureEvidence
  -> 根据策略筛选 2~5 个候选工具
  -> controller 选择一个工具和参数
  -> 代码校验工具名、参数和重试预算后执行
  -> 工具观察进入下一轮验收
  -> 每个失败步骤受自身预算约束，整个请求最多进行 2 轮修复
  -> 仍失败：阻断依赖步骤并返回明确原因
```

必须达到的行为：

- SQL 执行失败时，数据库原始错误能够进入下一轮 SQL 重写。
- SQL 执行成功但未回答子问题时，结果不能进入下游。
- `post_calc` 没有算出结果时，观察必须为失败。
- 独立步骤之间互不误伤；一个分支失败不删除已经成功的结果。
- 依赖失败步骤的下游任务不执行，并说明依赖链断在哪里。
- 修复次数有上限，禁止同一输入原样重跑。
- 无法回答时，用户能看到具体失败阶段、原因和已完成的部分。

## 3. 非目标

第一阶段不做以下事情：

- 不引入自由行动的通用 Agent，不让 LLM 自由改写用户问题或查询范围。
- 不把全部 24 个工具同时暴露给 controller。
- 不为每个工具配置一个独立 LLM。
- 不自动修复知识库、图片、实体解析等全部工具链。
- 不在第一阶段重做前端工作流界面。
- 不把动态数据库当前值硬编码成永久回归期望。

## 4. 总体架构

### 4.1 组件边界

新增 `backend/app/services/react_repair.py`，只负责三件事：

1. 定义和规范化 `FailureEvidence`。
2. 从修复策略加载失败类型对应的候选工具和重试预算。
3. 生成 controller 可消费的工具短名单。

修改现有组件：

- `query_data`：执行后调用验收，产出 accepted/rejected 观察。
- `post_calc`：空结果明确返回失败观察。
- `agent.py`：Wren 执行错误和语义验收反馈进入下一次 SQL 生成。
- `react_agent.py`：门控读取结构化失败，执行通用候选工具，实施依赖阻断。
- `observation.py`：统一观察字段，保持旧字段兼容。

新增 `resources/repair_policy.yaml`：

- 维护失败类型到候选工具的映射。
- 维护最大修复轮数和适用阶段。
- 不修改每个工具装饰器的 24 份元数据。

### 4.2 模型分工

不为每个工具单独配 LLM，继续复用现有角色：

- `verify_model`：判断结果是否满足子问题。
- `semantic_model`：选择修复工具和依赖处理方式。
- `sql_model`：根据结构化失败反馈修改 SQL。
- `knowledge_model`：知识查询和最终综合分析。

纯确定性工具不调用 LLM，例如时间换算、桩号换算、实体解析和枚举解析。

## 5. FailureEvidence

失败证据使用固定 JSON 结构，并同时保留在观察和最终 payload 中：

```json
{
  "accepted": false,
  "failure_code": "post_calc_empty",
  "stage": "post_calc",
  "retryable": true,
  "reason": "后置计算没有返回可用结果",
  "evidence": {
    "step_id": 2,
    "step_question": "计算每条路线的病害密度",
    "sql": "",
    "columns": [],
    "rows": [],
    "coverage": {},
    "upstream_step_ids": [1]
  },
  "candidate_tools": [
    "post_calc",
    "analyze",
    "ask_user"
  ],
  "attempt": 1,
  "max_repair_rounds": 2
}
```

第一阶段支持的失败类型：

| failure_code | 含义 | 是否可修复 |
|---|---|---|
| `sql_runtime_error` | SQL 执行失败 | 是 |
| `sql_semantic_mismatch` | SQL 成功，但主体、条件、指标、分组或口径不匹配 | 是 |
| `result_shape_mismatch` | 返回列或行结构不满足子问题 | 是 |
| `empty_unexpected` | 本应有数据却返回空结果 | 视覆盖情况 |
| `post_calc_empty` | 后置计算没有产生可用结果 | 视上游数据 |
| `missing_dependency` | 上游没有产出依赖值 | 否，除非上游可修复 |
| `tool_error` | 工具不可用或参数非法 | 否 |

所有字段必须有确定值。未知失败类型统一落到 `tool_error`，不允许控制器自由解释。

## 6. 修复策略

首版策略示例：

```yaml
sql_runtime_error:
  candidates: [query_data]
  repair_role: sql
  max_repair_rounds: 2

sql_semantic_mismatch:
  candidates: [query_data]
  repair_role: sql
  max_repair_rounds: 2

result_shape_mismatch:
  candidates: [query_data, check_coverage]
  repair_role: sql
  max_repair_rounds: 2

empty_unexpected:
  candidates: [check_coverage, query_data, ask_user]
  repair_role: semantic
  max_repair_rounds: 1

post_calc_empty:
  candidates: [post_calc, ask_user]
  repair_role: semantic
  max_repair_rounds: 2

missing_dependency:
  candidates: [ask_user]
  repair_role: null
  max_repair_rounds: 0

tool_error:
  candidates: [ask_user]
  repair_role: null
  max_repair_rounds: 0
```

策略加载失败时必须保守失败，不允许静默放行结果。

策略中的 `max_repair_rounds` 是单个失败类型的预算；执行时还要受整个请求最多 2 轮修复的全局预算约束，实际调用次数取两者剩余额度的较小值。

## 7. Controller 协议

Controller 不再接收全部工具，只接收当前失败允许的候选工具元数据：

```json
{
  "name": "check_coverage",
  "layer": "big",
  "domain": "coverage",
  "description": "核对时间、桩号和路线覆盖范围",
  "input_schema": {}
}
```

Controller 只输出动作和工具调用参数：

```json
{
  "thought": "结果为空，先核对查询范围是否超出数据覆盖",
  "action": "call_tool",
  "tool": "check_coverage",
  "arguments": {
    "plan": {}
  },
  "expected_effect": "区分确实无数据与 SQL 条件错误",
  "message": ""
}
```

允许的第一阶段动作：

- `call_tool`：调用候选工具。
- `retry_tool`：携带新反馈重试 `query_data` 或 `post_calc`。
- `ask_user`：需要用户补充信息。
- `abort`：不能继续，保留已完成部分并说明原因。

代码在执行前必须校验：

1. 工具在候选列表中。
2. 参数满足工具 `input_schema` 的必填项。
3. 当前修复轮次未超预算。
4. 新调用与上次调用不是完全相同的工具加参数。
5. 工具执行后必须产生新的观察或反馈。

## 8. 结果验收

`query_data` 执行成功后依次进行两层验收。

### 8.1 确定性验收

检查：

- SQL 是否执行成功。
- 结果是否为空。
- 返回列是否覆盖题目要求。
- 分组题是否返回分组标签。
- 聚合题是否返回单值或预期行数。
- 覆盖范围是否存在明显矛盾。
- 依赖键是否真实产生。

### 8.2 语义验收

调用现有 `nl2sql.verify_answer()`，输入：

- 当前子问题。
- 最终 SQL。
- 返回字段。
- 前 10 行样本。
- 当前步骤回复。

输出统一映射到 `FailureEvidence.failure_code`。

模板等确定性直出路径继续沿用现有跳过规则；ReAct 中由 Wren 或 LLM 生成的 SQL 必须验收。

## 9. 控制流与依赖阻断

每一步维护：

```json
{
  "step_id": 2,
  "status": "pending",
  "attempt": 1,
  "accepted": false,
  "failure": {},
  "repair_history": []
}
```

状态规则：

- `accepted` 后允许依赖它的步骤执行。
- 被拒绝的步骤不得进入下游分析或后置计算。
- 独立步骤继续执行，但最终结果标记为部分完成。
- 每个失败步骤受策略预算约束，整个请求最多执行 2 轮修复，实际额度取两者剩余值的较小值。
- 同一失败码在同一输入上第二次出现时直接终止当前分支。
- ReAct 返回失败 payload 后不得静默回退静态执行器重跑同一计划。

最终失败回复必须包含：

- 哪一步失败。
- 失败原因和证据摘要。
- 已自动修复几次。
- 哪些结果仍然有效。
- 哪些下游结果被阻断。
- 如果用户能补充什么条件，问题可能继续。

## 10. 数据流

### 10.1 SQL 语法/执行错误

```text
query_data
  -> execute_sql raises DatabaseError
  -> FailureEvidence(sql_runtime_error, database_error)
  -> policy candidates [query_data]
  -> controller selects retry_tool
  -> SQL role regenerates with previous SQL + database error
  -> execute and verify
```

Wren 执行错误不得在进入重写前被清空。若数据库错误无法反馈给生成器，直接失败，不允许原 SQL 原样重试。

### 10.2 SQL 成功但语义错误

```text
query_data
  -> SQL returns rows
  -> deterministic + semantic verification fails
  -> FailureEvidence(sql_semantic_mismatch, verdict + SQL + sample)
  -> controller selects retry_tool(query_data)
  -> SQL role regenerates with failure evidence
  -> execute and verify
```

### 10.3 后置计算为空

```text
post_calc
  -> no usable numeric result
  -> observation ok=false, failure_code=post_calc_empty
  -> controller may retry once with available upstream rows or ask_user
  -> still empty: block final conclusion
```

`post_calc` 不得再把“数据不足，算不出来”包装为 `ok=true`。

## 11. 测试设计

### 11.1 单元测试

新增 `backend/test_react_repair.py`，覆盖：

1. Wren 运行时错误进入 SQL 重写反馈。
2. SQL 成功但验收失败时生成 `FailureEvidence`。
3. `post_calc` 空结果返回失败观察。
4. 策略只返回允许的候选工具。
5. 非法工具、非法参数和重复调用被代码拒绝。
6. 上游失败时下游分析不执行。
7. 独立步骤失败时其他步骤结果保留。
8. ReAct 失败后不静默重跑整个计划。

### 11.2 第一阶段实库验收

先验收以下 4 题：

1. `西乡段泛油有多少处？`
   - 目标是识别 SQL 语法错误并携带数据库原文重写。
2. `按路线分组，统计每条路线的病害总数、病害总面积、平均单处病害面积，并关联获取该路线的长度`
   - 目标是验证 16 行路线分组结果、要求的指标列和路线长度，不因下游空结果污染最终回答。
3. `按路线和病害类型分组，统计各路线下每种病害类型的数量，找出每条路线数量最多的主要病害类型`
   - 目标是识别结果汇总不完整，避免把 237 行组合结果误报成同一路线。
4. `数据库里各个月份的巡查次数分别是多少？总共多少次？`
   - 目标是保持正向基线，验证月份分组、各月之和等于总数，以及总数单位正确。

当前数据库中第 4 题为 4 个月、83 + 62 + 41 + 20 = 206 次。实库回归验证不变量，精确数值测试使用固定快照或 mock，不依赖生产库持续不变。

### 11.3 第二阶段验收

第一阶段通过后，再纳入：

- `11月2日巡检了哪些线路？`
- `近三月有多少巡检记录？`
- `裂缝集中在哪几个月？哪些路？这些路的病害密度怎么样？`

这三题分别覆盖实体/日期、Join 放大和跨步骤分析，不与首版修复闭环混在一起。

## 12. 可观测性

结果 payload 的每一步增加：

```json
{
  "verification": {},
  "failure": {},
  "repair_history": [
    {
      "attempt": 1,
      "failure_code": "sql_runtime_error",
      "tool": "query_data",
      "accepted": false
    }
  ]
}
```

事件总线继续使用现有 `phase`、`thought`、`action`、`observation`，不新增传输协议。第一阶段只保证 payload 可查，前端展示留到后续。

## 13. 失败与降级

- 验收模型不可用：记录 `tool_error`，不得把未验收结果标记为成功。
- 策略文件缺失或格式错误：启动时记录错误，运行时直接失败并说明配置问题。
- Controller 返回坏 JSON：复用现有 JSON 修复机会；仍失败则 `abort`。
- 修复预算耗尽：返回 `accepted=false` 和完整失败证据。
- 独立分支失败：保留其他分支 section，并明确最终结果是部分完成。
- 修复后结果通过验收：替换失败尝试的结果，但保留 repair history。

## 14. 验收标准

第一阶段完成必须同时满足：

1. `post_calc` 空结果不再被标为成功。
2. ReAct 能读取结构化失败并选择策略短名单中的工具。
3. SQL 运行时错误能进入下一轮 SQL 重写。
4. SQL 成功但语义错误的步骤不能进入下游。
5. 依赖步骤被阻断时，最终回复说明具体原因。
6. 修复最多 2 轮，重复相同调用会被拒绝。
7. 第一阶段 4 题全部满足契约测试。
8. 现有工具注册、事件总线和模板直出路径不出现回归。

## 15. 风险与约束

- 语义验收仍可能误判，因此必须先做确定性结构检查，再调用 LLM 验收。
- Wren 生成的物理 SQL 可能包含中文别名，SQL 重写需要同时保留物理 SQL和 MDL 上下文。
- 修复不能扩大用户查询范围，所有新增/删除的过滤条件必须能从原问题或依赖值得到依据。
- `post_calc` 的 LLM 输出不是结构化数据；第一阶段只要求正确识别成功/失败和依赖阻断，不承诺生成完整表格。
- 当前工作区已有大量未提交改动，实施时只能触碰本规格列出的文件。
