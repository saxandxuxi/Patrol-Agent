# 提示词单一事实源与可组合指标设计

> 日期：2026-11-11
> 状态：待用户审阅
> 范围：QuerySpec 提示词、语义事实、规划修复、指标语义、MDL 合同、SQL 审计、
> Wren 兜底以及历史重复定义的迁移

## 一、问题定义

当前系统已经把执行层拆成 QuerySpec、`MdlQueryContract`、确定性 MDL-SQL、统一审计和
Wren `dry-plan`，但 QuerySpec 之前的事实源仍存在多处重复和冲突。

问题不是八段提示词各自措辞不好，而是同一事实有多个 owner：

1. 链式依赖同时存在 `{{key}}`、结构化 `inputs` 和隐藏的 `DEPENDS_ON_` 三种协议。
2. 审计提示词禁止修改 `filters`，修复票据却允许修改，形成两个互相冲突的权限来源。
3. 极值问题同时受“必须使用 TopN”和“没有 N 不得使用 TopN”两条规则约束。
4. `top` 视图在代码、规划提示词和审计提示词中的公开状态不一致。
5. `disease_density` 同时表示病害数量密度和病害长度密度，且“不存在”与“可派生”
   两种描述同时生效。
6. 审计提示词声明不复制 QuerySpec 业务规则，实际却维护了第二份规则。
7. `subject_router.md` 与 `query_spec.md` 重复维护任务/病害主体规则。
8. `planning.md` 的 `analyze` 表示“不查数据”，QuerySpec 解析代码却不认识该 action，
   未识别的 action 还会静默降级成 `query_data`。

前两轮已确认但仍需纳入本轮整改的单事实源债务：

1. `field_overrides.yaml`、`fields.yaml` 与 `enums.yaml` 仍存在字段中文名分叉。
2. `categories_type` 枚举说明和 SQL CASE 仍有多份手写副本。
3. 表中文名仍由 `field_overrides.yaml` 和 Wren description 重复表达。
4. `subjects.yaml` 的 JOIN 字段已经删除并由加载器拒绝重新加入，后续必须保持关闭。
5. Wren cube 指标与 `metrics.yaml` 仍可能用不同 ID 表达同一公式。
6. `fields.yaml` 仍有手写枚举 CASE，尚未统一从 `enums.yaml` 生成。

这些问题必须在实现和验收中逐项关闭，不能只作为“长期债务”继续保留。

## 二、目标

1. 每个语义事实只有一个机器可读 owner。
2. 提示词只描述职责、输出协议和当前任务需要的收窄事实，不再复制业务规则。
3. 同一语义事实进入 Planner、审计器、合同、SQL 生成器和修复器时来自同一个快照。
4. QuerySpec 依赖只认结构化 `inputs`，不再依赖隐藏字符串协议。
5. 审计只提出结构化疑点，最终允许修改哪些字段由修复票据唯一决定。
6. 未知 action 失败关闭，不静默降级成查询。
7. 指标描述事实、聚合方式、粒度、身份和 JOIN 放大安全，不把所有指标当成同一种聚合。
8. 派生问题优先由 LLM 选择事实并使用已登记的通用加减乘除工具组合，不再逐题新增密度公式。
9. 普通业务公式只登记必要口径，不为每种对象和问法复制一套公式。
10. 失败先回流 QuerySpec 修复；只有合法合同明确返回 `compiler_not_covered` 才进入
    Wren/LLM MDL-SQL 兜底。

## 三、非目标

本设计不改变：

1. 用户问题和最终回答的语义判断仍由 LLM 完成。
2. QuerySpec 是用户范围和查询形状的唯一权威。
3. MDL-SQL 必须经过同一套合同、关系、安全和 `dry-plan` 审计。
4. 用户裁决卡、示例库和晋级考试流程。
5. 数据库物理结构。
6. 前端结果工作台布局。
7. LLM 兜底不能自由生成物理 SQL，也不能修改冻结合同。

本设计不通过关键词、正则、题目 ID 或固定问句映射修复任何问题。

## 四、事实所有权

| 事实 | 唯一 owner | 消费者 | 禁止的副本 |
|---|---|---|---|
| 用户范围、主体、视图、依赖和输出意图 | QuerySpec | 合同、修复器、执行器 | 提示词或代码自行改写用户范围 |
| 公开查询视图集合和内部归一化 | `backend/app/query/contracts.py` | QuerySpec、审计提示词、形状校验 | `query_spec.md` 和审计提示词手写 view 清单 |
| 回答操作、TopN 选择、时间语义、关系存在性、主体证据规则 | `resources/capabilities/query_shapes.yaml` | Planner、主体路由、审计器 | 各提示词中的第二份业务规则 |
| 模型、真实列、计算列和关系拓扑 | Wren MDL | 能力注册表、合同、MDL-SQL、审计器 | `subjects.yaml` 或 Python 中的 JOIN 定义 |
| 字段标签、角色、枚举引用、业务谓词 | `resources/capabilities/fields.yaml`、`enums.yaml`、`predicates.yaml` | Planner、合同、SQL 上下文 | 手写枚举 CASE 或相同事实的另一份字段映射 |
| 通用 measure 的聚合公式 | Wren MDL cube | 能力注册表、Planner、合同 | `metrics.yaml` 再复制同一公式 |
| 巡检业务指标和正式派生指标公式 | `metrics.yaml`、`derived_metrics.yaml` | 能力注册表、Planner、合同、计算契约 | Wren cube 再复制同一业务公式 |
| 指标单位、aliases、aggregation、grain、identity 和 fanout 语义 | `metrics.yaml` | Planner、合同、计算契约 | 第二份指标语义 |
| 由用户要求产生的新算术结果 | `CalculationRequest` | `post_calc` 执行器 | 为每种问法预注册一个派生指标 |
| 口语候选证据 | `assist_rules.yaml` 产生的 `trusted=false` JSON | Planner、MDL-SQL 上下文 | 直接决定主体、过滤、JOIN、SQL 或答案 |
| 修复权限 | `RepairTicket.allowed_paths` | JSON Patch 修复器 | 审计提示词 `set` 或异常文本自行授权 |

MDL 和 `resources/capabilities/` 是平等事实源。前者负责结构事实和通用 measure，后者负责
巡检业务语义和业务指标；两者不设置全局优先级。同一个事实只能有一个公式 owner：

1. Wren cube 已有的通用 measure 由 MDL 拥有，`metrics.yaml` 只能引用。
2. Wren cube 未表达的巡检业务指标由 `metrics.yaml` 拥有，MDL 不得用第二个公式重复定义。
3. 普通比例、密度等组合结果由 `CalculationRequest` 产生，不登记为原子公式。
4. 有正式业务命名的派生口径由 `derived_metrics.yaml` 拥有。

跨来源出现同一指标 ID 时，必须能证明是“公式引用”而不是两份公式。`FactConflictReport`
对重复公式 owner、公式不一致和单位不一致都失败关闭；冲突事实不得进入 LLM 上下文。

## 五、项目原则

新增：

```text
docs/项目原则-提示词与事实源一致性.md
```

现有两份原则文件必须在开头引用该文件：

```text
docs/项目原则-AI晋级考试与用户裁决.md
docs/项目原则-禁止正则与关键词硬匹配.md
```

新原则至少包含以下硬规则：

1. 一个语义事实只能有一个 owner；迁移时必须在同一变更删除旧定义。
2. 提示词之间、提示词内部不得包含互相排斥的规则。
3. 审计提示词只发现问题和引用共享事实，不复制 Planner 业务规则。
4. `planning.md` 只属于旧 OrchestratorPlan，`{{key}}`、`needs`、`produces`
   不是 QuerySpec 语法。
5. QuerySpec 链式依赖只使用 `depends_on` 和结构化 `inputs`。
6. `DEPENDS_ON_` 等隐藏兼容协议不得继续进入运行时。
7. 未识别 action 必须失败关闭。
8. MDL 与 resources 冲突必须结构化失败，不按加载顺序选边。
9. 新增或修改提示词事实时必须运行一致性测试。
10. 验收必须扫描重复、矛盾和杂糅，并关闭 `resources/README.md` 已解决的债务项。

## 六、提示词协议

### 6.1 共享占位符

所有提示词共享以下渲染事实：

```text
<<query_semantics>>
  query_shapes.yaml 中 planner_semantics 的完整结构化内容

<<answer_operations>>
  query_shapes.yaml 中 answer_operations 的同一投影

<<supported_views>>
  contracts.py 的公开查询视图集合，不含内部别名 top

<<capabilities>>
  当前主体收窄后的字段、指标、派生指标、枚举、能力缺口和计算器
```

渲染器负责从统一快照生成这些 JSON。提示词中不得手写同一份 TopN、时间窗口、枚举完整性、
关系存在性或主体选择规则。

### 6.2 `query_spec.md`

`query_spec.md` 只保留：

1. Planner 的角色和输出 JSON 协议。
2. QuerySpec 字段含义。
3. 结构化 `inputs` 的格式。
4. 必须引用 `<<query_semantics>>`、`<<answer_operations>>`、
   `<<supported_views>>` 和 `<<capabilities>>` 的说明。
5. 输出前自检和失败关闭规则。

以下内容从提示词正文删除：

1. 手写 TopN 规则。
2. 手写时间窗口和最近 N 批次规则。
3. 手写关系存在性规则。
4. 手写任务/病害主体规则。
5. 手写 view 清单。
6. 每个派生指标的固定公式清单。
7. `{{key}}`、`needs`、`produces`。

提示词可以解释如何消费共享事实，但不能重新定义共享事实。

### 6.3 `query_spec_audit.md`

审计提示词改为真正的薄审计清单，只接收：

```text
<<question>>
<<plan_json>>
<<query_semantics>>
<<answer_operations>>
<<supported_views>>
<<capabilities>>
```

审计职责：

1. 对照用户问题检查范围、主体、结果形状、依赖和计算是否完整。
2. 引用共享语义事实指出违反项。
3. 输出结构化问题代码、节点、原因、证据和建议修复方向。
4. 不重新生成 QuerySpec。
5. 不拥有修改权限。

相对时间必须已由 QuerySpec 落实为明确的半开日期区间。审计器不再单独接收
`current_date`，也不重新解释“本月、最近三个月”等自然语言。

审计输出中的 `set` 只作为建议证据，不直接进入 JSON Patch。修复器必须根据
`RepairIssue.allowed_paths` 重新判断可修改路径。

### 6.4 `subject_router.md`

主体路由提示词只保留：

1. 角色和输出协议。
2. 候选主体。
3. 字段触达和实体候选 `trusted=false` 证据。
4. 对 `<<query_semantics>>` 的引用。

任务身份与病害证据的判定只来自 `query_shapes.yaml.task_disease_evidence`。

### 6.5 `planning.md`

`planning.md` 明确标注为旧 OrchestratorPlan 协议：

1. 输出 `steps`，不输出 QuerySpec。
2. `{{key}}`、`needs` 和 `produces` 只在该旧协议中使用。
3. OrchestratorPlan 到 QuerySpec 的适配器负责转换成 `depends_on` 和 `InputBinding`。
4. QuerySpec Planner 和 QuerySpec 审计器不得把 `{{key}}` 或 `DEPENDS_ON_`
   识别为合法依赖语法。
5. `analyze` 在适配器中转换成 `post_calc + analysis_only`。

### 6.6 TopN 统一规则

TopN 只在 `query_shapes.yaml.view_selection` 维护一次：

1. 用户明确给出 N：使用 `top_groups` 或 `top_records`，必须有正整数 `limit`
   和 `order_by`。
2. 用户只说最多、最高、最严重但没有 N：按结果对象使用 `distribution` 或
   `list`，保留 `order_by`，`limit=null`。
3. `top` 只允许代码内部归一化，不进入公开视图集合。
4. 合同和审计都读取同一个 `supported_views` 投影。

### 6.7 两个密度口径

统一登记两个不同派生指标：

```text
disease_density
  expression: count / total_route_length_km
  unit: 处/km
  meaning: 每公里路线上的病害记录数

disease_length_density
  expression: sum_effective_disease_length / total_route_length_km
  unit: 米/km
  meaning: 每公里路线上的有效病害长度
```

分母必须来自路线粒度的唯一路线总长度事实，不能直接把病害 JOIN 后重复出现的
`route_length_km` 相加。

两个 ID、公式和单位只登记一次。其他对象的密度不新增同类指标，而由 Planner 使用
通用事实和计算器组合。

## 七、QuerySpec 数据流与修复

### 7.1 Action 归一化

action 只接受以下映射：

| 输入 action | 内部 node_type | 额外约束 |
|---|---|---|
| `query_data` | `query_data` | 必须形成合法查询视图 |
| `post_calc` | `post_calc` | 必须有 calculation，或允许的解释型断言 |
| `analysis` | `post_calc` | 必须补齐 `analysis_only` |
| `analyze` | `post_calc` | 必须补齐 `analysis_only` |

其他值、空值和缺省值都生成 `invalid_action` 修复票据，不得降级成 `query_data`。

### 7.2 依赖协议

QuerySpec 依赖只使用：

```json
{
  "depends_on": ["q1"],
  "inputs": [
    {
      "from_node": "q1",
      "from_field": "route_name",
      "to_field": "route_name",
      "op": "in"
    }
  ]
}
```

删除 `_dependency_placeholder_binding` 和运行时 `DEPENDS_ON_` 兼容分支。

模型把依赖误写进过滤值时，代码只按已知节点 ID、已声明输出字段和结构化分隔符识别。
例如 `q1_top_route.route_name` 可以识别为“引用了 q1 的 route_name”，因为它由以下受控
事实组成：

1. `q1_top_route` 是当前计划已声明节点；
2. `route_name` 是该节点的真实产出字段；
3. 值不是用户范围内可解析的真实路线名称。

识别后生成 `invalid_dependency_value` 修复票据：

1. 删除该错误过滤值；
2. 添加对应 `depends_on`；
3. 添加结构化 `inputs`；
4. 锁定用户原问题、路线、时间、病害、设备、任务和其他范围字段。

无法由受控节点和字段事实证明的字符串按普通值解析处理，不能靠文本猜测依赖。

### 7.3 修复权限

`RepairIssue.allowed_paths` 是修改权限的唯一来源：

1. 审计提示词 `set` 只能帮助选择修复方向。
2. 修复器必须重新生成 `RepairTicket`。
3. JSON Patch 只能修改 `allowed_paths` 覆盖的路径。
4. 应用补丁后必须逐字段比较 `preserve` 范围和 `scope_signature`。
5. 相同补丁或相同证据不得重复执行。
6. 最多两轮。

`lost_filter` 的修复票据由代码决定是否允许新增已存在关联或无关联的过滤条件，不由审计
提示词自行扩大或禁止权限。

## 八、指标语义

### 8.1 `MetricCapability`

每个指标增加：

```text
aggregation
grain
identity
fanout_safe
```

`aggregation` 闭集：

```text
row
count
count_distinct
sum
avg
min
max
```

`grain` 表示该事实天然描述的对象粒度，例如：

```text
disease
patrol
route
section
global
```

`identity` 表示该粒度下的唯一身份字段，例如 `route.id`。`fanout_safe=false` 时，
`identity` 必填。

`fanout_safe` 的含义：

1. `true`：表达式的值不会因为合法的一对多 JOIN 重复而改变。
2. `false`：必须先在自己的粒度完成聚合或去重，才能与更细粒度结果组合。
3. 关系审计仍负责具体的 JOIN 基数风险；`fanout_safe` 不是对关系的替代。

### 8.2 事实和公式的边界

`metrics.yaml` 和 MDL cube 只登记可查询事实：

1. 原始行值；
2. 单字段聚合；
3. 单字段去重计数；
4. 有明确业务范围的内置聚合；
5. 无法由通用算子组成的正式业务公式。

指标来源必须二选一，不能同时保存公式：

```yaml
# MDL 已有通用 measure，由 resources 补充业务语义
source:
  kind: mdl_cube
  reference: disease_metrics.total_area

# MDL 没有的业务指标，由 resources 拥有公式
source:
  kind: resources
  expression: SUM({alias}.effective_disease_length)
```

`source.kind=mdl_cube` 时禁止同时写 `expression`；`source.kind=resources` 时同一个指标
不得再被 Wren cube 定义。加载器把两种来源投影为同一个 `MetricCapability`，不会产生
“优先级”分支。

普通比例、密度、差值、占比和增长率由 `CalculationRequest` 组合，不逐题登记。

`derived_metrics.yaml` 只保留有正式业务命名的公式。普通病害数量密度和病害长度密度
已明确命名，因此分别登记；其他对象的普通密度不预注册。

### 8.3 示例

疾病总长度：

```yaml
sum_effective_disease_length:
  expression: SUM({alias}.effective_disease_length)
  unit: 米
  aggregation: sum
  grain: disease
  identity: disease.id
  fanout_safe: false
```

路线唯一总长度：

```yaml
total_route_length_km:
  expression: SUM({alias}.route_length_km)
  unit: km
  aggregation: sum
  grain: route
  identity: route.id
  fanout_safe: false
```

路线单行长度：

```yaml
route_length_km:
  expression: "{alias}.route_length_km"
  unit: km
  aggregation: row
  grain: route
  identity: route.id
  fanout_safe: false
```

### 8.4 视图审计

SQL 视图审计不再对所有 `contract.metrics` 强制要求聚合函数。

规则：

1. `aggregation != row` 的指标必须形成对应聚合。
2. `aggregation=row` 的指标可以是非聚合表达式。
3. 非聚合指标出现在无分组的多行结果中时拒绝。
4. 非聚合指标所在结果的分组必须覆盖其 `grain` 和 `identity`。
5. `fanout_safe=false` 的指标不能直接跨一对多 JOIN 计算。
6. 普通记录视图仍不得声明聚合指标。

### 8.5 计算组合

复用现有：

```text
calculate_scalar
calculate_columns
calculate_series
calculate_group_change
calculate_datetime_interval
```

不新增“表达式字符串”或让 LLM 直接计算最终数字。

计算契约增加：

1. 操作数结果粒度必须一致。
2. 单位运算必须形成明确输出单位。
3. 除法分母必须唯一且非粒度键。
4. 不同分组来源必须按相同 grain/identity 对齐。
5. `calculate_scalar` 只消费标量或 `row_index` 选中的唯一行。
6. `calculate_columns` 的多个多行来源必须具有相同的分组维度和身份键。
7. 无法证明粒度一致时返回 `calculation_grain_mismatch`，不执行心算或猜测。

### 8.6 省道题目标计划

用户问题：

```text
省道上的病害总长度以及密度是多少？
```

目标 QueryPlan：

```text
q1: disease 主体，省道范围
    metrics=[sum_effective_disease_length]

q2: route 主体，省道范围
    metrics=[total_route_length_km]
    required_assertions=[unique_grain]

q3: post_calc
    calculate_scalar/divide
    numerator=q1.sum_effective_disease_length
    denominator=q2.total_route_length_km
```

`disease_length_density` 只是 q3 的业务输出名，不进入 SQL 原子指标。

### 8.7 巡查任务密度

用户问“巡查任务密度”时，Planner 根据业务语义选择：

```text
q1: patrol 主体，用户时间范围，count
q2: patrol 主体，同一时间范围，active_day_count
q3: calculate_scalar/divide
```

`active_day_count` 是“有效巡查日期数”的原子业务事实，不由用户问句关键词决定。
如果业务口径是每个路线、每公里或其他分母，Planner 选择相应真实事实和范围。

## 九、执行预检与 Wren 兜底

每个 `query_data` 节点执行前按以下顺序预检：

```text
QuerySpec 结构
  → 字段、枚举和值来源
  → 主体、视图、指标、维度和形状
  → 依赖、计算和 grain 契约
  → MdlQueryContract
  → 确定性 MDL-SQL
  → 合同、关系和安全审计
  → dry-plan
```

失败处理：

1. QuerySpec 或字段/范围校验失败：生成 `RepairTicket`，回流 QuerySpec。
2. 合同、形状、审计或 `dry-plan` 失败：生成结构化证据，回流 QuerySpec 修复。
3. 修复最多两轮，范围哈希必须保持不变。
4. 只有合同合法且确定性编译器明确返回 `compiler_not_covered`，才调用一次
   Wren/LLM MDL-SQL 兜底。
5. LLM 兜底使用同一冻结合同、同一收窄 MDL 和业务能力上下文。
6. LLM SQL 同样经过统一审计和 `dry-plan`。
7. `compiler_exception`、合同冲突、安全拒绝和 QuerySpec 范围失败不进入 LLM SQL 兜底。

本设计取代上一份 QuerySpec 到 MDL-SQL 设计中的“任一定性编译失败都可兜底”规则。

## 十、事实冲突门禁

新增不可变对象：

```text
FactConflictReport
  conflicts: tuple[FactConflict, ...]
  accepted: bool
```

每个冲突至少包含：

```text
code
subject
fact_id
sources
expected
actual
evidence
```

最低检查项：

1. `subjects.yaml` 引用的 MDL 模型存在。
2. `fields.yaml` 引用的模型、列和计算列存在。
3. `requires_join` 能在 MDL 关系或直接模型路径中找到。
4. 同一指标 ID 的公式指纹一致。
5. 同一指标 ID 的单位一致。
6. 同一字段的标准 label 一致。
7. 枚举 storage code、标准 label 和 display 一致。
8. 表中文名只有一个人工 owner。
9. SQL CASE 可以从枚举 owner 确定性生成。
10. Prompt 的公开 view 集合与运行时公开集合一致。

加载或测试发现冲突时失败关闭，不把冲突事实发送给 LLM。

## 十一、历史重复与矛盾整改

### 11.1 提示词八项

| 问题 | 整改 |
|---|---|
| 三套依赖协议 | QuerySpec 只保留 `inputs`；删除 `DEPENDS_ON_`；OrchestratorPlan 由适配器转换 |
| 审计与修复权限冲突 | `RepairIssue.allowed_paths` 成为唯一权限 |
| TopN 两条规则冲突 | 收口到 `query_shapes.yaml.view_selection` |
| view 枚举不一致 | 两个提示词都使用 `<<supported_views>>` |
| 两个密度含义冲突 | 拆成 `disease_density` 和 `disease_length_density` |
| 审计复制业务规则 | `query_spec_audit.md` 改为薄清单 |
| 主体规则重复 | `subject_router.md` 引用 `task_disease_evidence` |
| `analyze` 被降级成查询 | 映射为 `post_calc + analysis_only`，未知 action 失败 |

### 11.2 单事实源六项

| 问题 | 整改 |
|---|---|
| 字段中文名分叉 | 标准 label 只由 `enums.yaml` 或非枚举字段 owner 提供，别名留在 aliases |
| `categories_type` 多份 CASE | SQL CASE 从 `enums.yaml` 生成，删除手写副本 |
| 表中文名重复 | `field_overrides.yaml` 拥有表标签；Wren description 只写业务约束 |
| `subjects.yaml` 旧 JOIN 字段 | 保持删除，加载器继续拒绝 |
| cube 与 metrics 同公式不同 ID | 使用公式指纹、单位、显式 alias 合并；公式冲突失败 |
| `fields.yaml` 手写枚举 CASE | 改为 `enum_key` 引用和生成表达式 |

### 11.3 旧执行链路问题

以下问题一并纳入回归：

1. 依赖占位符被误包装成路线不存在。
2. 混合主体链被单一主体校验误杀。
3. 审计意见无法落地到修复票据。
4. Planner 缺少当前主体的一跳关系和相邻主体能力摘要。
5. Wren 在错误合同之后才得到处理。

## 十二、代码组织

新增或调整职责：

```text
backend/app/query/
  prompt_facts.py
    渲染 query_semantics、answer_operations、supported_views

  capability_registry/
    fact_conflicts.py
      MDL 与 resources 的结构化冲突检查

  metrics/
    semantics.py
      聚合、grain、identity、fanout 和单位校验

  planning_repair/
    issues.py
      根据失败证据生成唯一 allowed_paths
```

修改：

```text
backend/app/query/contracts.py
backend/app/query/analysis.py
backend/app/query/shape_contracts.py
backend/app/query/capability_registry/models.py
backend/app/query/capability_registry/registry.py
backend/app/query/capability_registry/loader.py
backend/app/query/mdl_sql/models.py
backend/app/query/mdl_sql/contract_builder.py
backend/app/query/mdl_sql/audit/view_audit.py
backend/app/query/mdl_sql/compiler/deterministic.py
backend/app/query/mdl_sql/compiler/pipeline.py
backend/app/calculations/
prompts/query_spec.md
prompts/query_spec_audit.md
prompts/subject_router.md
prompts/planning.md
prompts/mdl_sql_generator.md
resources/capabilities/query_shapes.yaml
resources/capabilities/metrics.yaml
resources/capabilities/derived_metrics.yaml
resources/README.md
```

不在 `analysis.py`、`agent.py` 或 `nl2sql.py` 新增单题分支。

## 十三、TDD 顺序

按纵向切片依次进行，一次只让一个契约从红变绿。

### 切片 1：公开视图

失败测试：

1. 两个提示词不包含手写 view 清单。
2. 两个提示词都包含 `<<supported_views>>`。
3. `top` 不在公开集合。
4. 未知 view 返回结构化失败。

实现：

1. 建立公开视图投影。
2. 接入两个提示词渲染。
3. 删除手写清单。

### 切片 2：共享语义

失败测试：

1. TopN 规则只来自 `query_shapes.yaml`。
2. 无 N 的极值问题使用 distribution/list 且 `limit=null`。
3. 主体路由和 QuerySpec 都读取 `task_disease_evidence`。
4. 审计提示词不复制 TopN、时间窗口、枚举完整性规则。

实现：

1. 增加 `query_semantics` 和 `answer_operations` 渲染。
2. 收敛三个提示词。
3. 收紧 Prompt 契约测试。

### 切片 3：Action

失败测试：

1. `analysis` 和 `analyze` 变成 `post_calc + analysis_only`。
2. 未知 action 返回 `invalid_action`。
3. 缺省 action 不再静默变成 `query_data`。
4. `post_calc` 无 calculation 且无解释型断言时返回修复票据。

实现：

1. action 闭集解析。
2. 失败证据和修复票据。

### 切片 4：依赖

失败测试：

1. `q1_top_route.route_name` 返回 `invalid_dependency_value`。
2. 修复票据只允许删除错误 filter、添加 inputs 和 depends_on。
3. `DEPENDS_ON_` 不再有运行时兼容。
4. 合法 inputs 不改写用户范围。

实现：

1. 结构化依赖误写识别。
2. 删除隐藏兼容分支。
3. 接入最大两轮修复。

### 切片 5：修复权限

失败测试：

1. 审计 `set` 不直接进入 JSON Patch。
2. `lost_filter` 的修改路径来自 `RepairIssue.allowed_paths`。
3. 相同补丁立即停止。
4. 应用补丁后范围哈希不变。

实现：

1. 统一 RepairTicket。
2. 补丁白名单和 preserve 校验。

### 切片 6：指标语义

失败测试：

1. `route_length_km` 的非聚合表达式不会被 `view_audit` 误杀。
2. `aggregation=row` 的指标不要求 aggregate。
3. 无分组多行 row 指标被拒绝。
4. `total_route_length_km` 在路线粒度安全汇总。
5. `fanout_safe=false` 的指标不能跨一对多 JOIN 直接相加。
6. Planner 上下文暴露 aggregation、grain、identity 和 fanout_safe。

实现：

1. 扩展 MetricCapability。
2. 加载和合并规则。
3. 合同和视图审计。
4. Planner 收窄视图。

### 切片 7：计算图

失败测试：

1. 省道病害总长度和唯一路线总长度拆成两个节点，再由 post_calc 相除。
2. 巡查任务次数和有效天数使用同一计算机制。
3. grain 不一致时返回 `calculation_grain_mismatch`。
4. 单位不一致或无法组合时失败。
5. 分母是分组键或非唯一值时失败。

实现：

1. 计算契约 grain/unit 校验。
2. 多分组来源对齐。
3. 两个密度派生指标。

### 切片 8：冲突门禁

失败测试：

1. 指标公式冲突失败。
2. 指标单位冲突失败。
3. requires_join 缺失失败。
4. 字段 label、枚举和表名冲突失败。
5. 冲突事实不进入 LLM 上下文。

实现：

1. `FactConflictReport`。
2. 加载和启动门禁。
3. 六个历史债务迁移。

### 切片 9：执行预检

失败测试：

1. 合同或审计失败先生成 QuerySpec 修复票据。
2. 修复失败后不直接进入 Wren。
3. 只有 `compiler_not_covered` 触发 LLM MDL-SQL。
4. LLM SQL 使用同一合同和审计。
5. `compiler_exception` 不触发自由兜底。

实现：

1. 调整 pipeline 失败路由。
2. 接入修复器。
3. 保持 LLM 一次调用和失败关闭。

### 切片 10：端到端

1. “省道上的病害总长度以及密度是多少？”
2. “巡查任务密度是多少？”及其同义改写。
3. “病害最多的那条路是哪条，这条路具体有什么病害？”
4. “有病害且是国道的路段有哪些？”
5. “最近一次巡查以及检测出的病害是什么？”

这些题必须共享同一依赖、主体、指标和修复链路，不新增题目 ID 或问句特判。

## 十四、验收标准

功能验收：

1. 同一个语义事实在提示词、资源、代码和 Wren 中只有一个 owner。
2. 两个提示词不手写 view 清单。
3. TopN 只有一个规则来源，同义问法选择稳定。
4. QuerySpec 依赖只接受结构化 inputs。
5. `DEPENDS_ON_` 运行时分支已删除。
6. 未知 action 不降级。
7. 审计建议和修复权限不冲突。
8. 所有 metrics 不再被统一当作聚合指标。
9. 普通密度通过事实和通用计算组合。
10. 两个病害密度口径互不混用。
11. MDL 与 resources 冲突在 LLM 前失败关闭。
12. 合同或审计失败优先回流 QuerySpec。
13. 只有 `compiler_not_covered` 进入 Wren/LLM MDL-SQL 兜底。

回归验收：

1. 相关 `unittest` 全部通过。
2. 全量查询测试通过。
3. 前端构建通过。
4. `git diff --check` 通过。
5. 执行“功能杂糅、内容重复、内容矛盾”扫描并记录范围和结果。
6. 代码变更后重新生成 15 道新题和随机 5 道旧题。
7. 新题至少 `12/15`、旧题至少 `4/5`、总计至少 `18/20`。
8. 不通过关键词、正则、题目 ID 或重复考试集取得通过。

## 十五、迁移完成后的删除项

以下内容必须删除，不能保留为新旧双写：

1. `query_spec.md` 中的手写 view、TopN、时间和主体规则副本。
2. `query_spec_audit.md` 中的业务规则副本和手写 view 清单。
3. `subject_router.md` 中的重复主体规则。
4. `analysis.py::_dependency_placeholder_binding`。
5. 所有 `DEPENDS_ON_` 运行时分支。
6. 未识别 action 默认变成 `query_data` 的分支。
7. 审计 `set` 直接进入补丁执行的路径。
8. `view_audit.py` 对所有 metric 强制 aggregate 的分支。
9. `disease_density` 兼作两种口径的旧定义。
10. `fields.yaml` 手写枚举 CASE。
11. `field_overrides.yaml` 的枚举编码副本。
12. `subjects.yaml` 若重新出现的 JOIN 字段。
13. 同一原子事实的另一份手写表名、字段名或指标公式。

## 十六、完成后的边界

完成本设计后，系统仍然只回答能力注册表中真实存在的数据事实。

对于无法可靠回答的问题：

1. 明确区分能力缺失、计划错误、范围为空和基础设施失败。
2. 不猜测枚举、粒度、公式或关系。
3. 不删除用户条件改用全量代理查询。
4. 不把 LLM 兜底当作绕过 QuerySpec 或能力边界的通道。
5. 不通过新增关键词、正则或题目 ID 修复同义问法。
