# QuerySpec 受控自救设计

## 目标

把单轮查询从“Planner 一次填错就直接失败”改成“代码定位错误字段，原 Planner 只修该字段，
其余用户范围保持不变，最多修复两轮”。该机制适用于 Planner 输出中的主体、视图结构、
字段、指标、排序、依赖和值解析问题，不把缺失的业务能力伪装成可修复问题。

## 不变量

1. 用户原问题始终是唯一语义事实源。
2. 路线、时间、病害、设备、任务、业务编号和桩号范围不得在修复中漂移。
3. LLM 只能修改 QuerySpec JSON，不得生成或改写 SQL。
4. 枚举和实体值必须继续经过统一能力注册表解析。
5. 相同证据或相同修复结果不得原样重复调用。
6. 最多两轮修复；第二轮仍失败时返回结构化原因。
7. 代码规则和正则只能在 `resources/capabilities/` 生成 `trusted=false` 候选证据。
8. 回答正确后的示例沉淀由后端结果契约驱动，不依赖前端猜测是否可保存。

## 数据流

```text
用户问题
  -> 统一能力注册表和字段触达
  -> Planner 生成原始 QuerySpec JSON
  -> 解析、字段/值/形状/依赖校验
  -> 失败时生成 RepairTicket
       - 原始问题
       - 上一版 QuerySpec JSON
       - 失败代码、路径、原因、期望值和实际值
       - 允许修改的 JSON 路径
       - 不可变化的用户范围签名
  -> 同一 Planner 返回 JSON Patch 数组
  -> 代码应用补丁并检查越权修改
  -> 重新校验并编译 SQL
  -> 成功执行，或两轮后结构化失败
```

## RepairTicket

`RepairTicket` 是 Planner 初始生成阶段的修复入口，不替代用户点击“回答错误”后的
`ReviewEvidence`。两者的共同点是都使用真实原始问题、真实计划事实和不可漂移范围。

修复补丁只允许 `set`、`add`、`remove` 三种 JSON Patch 操作。补丁应用后执行两层保护：

1. 路径保护：补丁只能落在该问题允许修改的结构路径。
2. 范围保护：原有过滤条件、节点身份和用户约束逐项比较；新增依赖节点只能复用已有范围，
   不能引入新的路线、时间、病害、设备或业务编号。

## 错误分类

- `QuerySpecUnsupported`：数据能力事实，不修复，直接说明边界。
- `QuerySpecAmbiguity`：用户口径本身不完整，不修复，返回确认或澄清。
- `LLMError`：基础设施或格式错误，按模型调用策略处理。
- 其他 `QuerySpecError`：进入受控修复，除非已经达到两轮或没有可执行修复动作。

## 主体冲突

Planner 最终主体与初始主体路由不一致时：

1. 生成 `subject_conflict` 证据，保留 Planner 最终主体和原路由证据。
2. 用该证据重新调用一次主体路由器。
3. 使用新的主体结果重新规划一次。
4. 两轮后仍冲突才返回明确失败。

主体修正只允许改变 subject 及为满足该 subject 必需的节点结构；原始路线、病害和时间
过滤必须保持。

## 最近 N 次巡检

语义辅助配置增加 `trusted=false` 的“最近 N 次巡检”候选证据。执行前契约检查：

1. 候选证据提示用户要求最近 N 次巡检。
2. 计划必须存在真实巡检批次节点，并由 `patrol_number` 驱动后续病害节点。
3. 缺失依赖时返回 `missing_recent_window_dependency` 修复票据。
4. 修复后的病害分布按真实聚合指标排序，不能使用 `create_time` 冒充指标。

## “用 AI 查”控制动作

`capability_id=ai|wren` 是控制动作，不是模板候选：

1. 在任何候选 ID 查找之前处理。
2. 丢弃当前确认卡中的模板选择，但保留卡上保存的原问题。
3. 以禁用模板候选的方式重新生成受控 QuerySpec。
4. 不再次弹出同一张模板确认卡。
5. LLM 仍然不能直接生成 SQL。

## 示例库沉淀

单轮 QuerySpec 成功结果存在可执行 SQL 时返回 `reviewable=true`。

- 单子问题：保存原问题和该条 SQL。
- 多子问题：保存复合题原问题，并逐个子问题保存问题与 SQL。
- 复合题外层不挖掘成单条多 SQL 模板。

## 代码边界

- 新修复协议：`backend/app/query/planning_repair/`
- Planner 接入：`backend/app/query/analysis.py`
- 主体重路由：`backend/app/query/capability_registry/subject_resolver.py`
- AI 控制动作：`backend/app/services/agent.py`
- 最近巡检候选证据：`resources/capabilities/assist_rules.yaml`
- 示例沉淀契约：`backend/app/services/agent.py` 和现有 `chat.py` / `nl2sql.py`

不得继续向单个巨型文件加入新的业务规则。

## 验收

1. AI 控制动作不查候选、不重复弹卡，并保留原问题。
2. 非法字段或值经过一次证据化修复后可以执行。
3. 修复不得改变用户范围，越权补丁被拒绝。
4. 相同修复结果不会重复执行。
5. 主体冲突会重新路由一次并重规划一次。
6. 最近 N 次巡检缺少真实批次依赖时在执行前修复。
7. 单子问题和多子问题的示例沉淀契约均有测试。
8. 全量查询测试、Python 编译检查、前端构建和 `git diff --check` 通过。
9. 业务代码变化后重新生成一套 `15 新题 + 5 旧题`，不复用旧考试集。
