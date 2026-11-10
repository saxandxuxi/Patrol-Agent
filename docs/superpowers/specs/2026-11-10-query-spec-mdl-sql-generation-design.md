# QuerySpec 到 MDL-SQL 的统一受控生成设计

> 日期：2026-11-10
> 状态：待用户审阅
> 范围：单轮查询中，QuerySpec 合法之后的 MDL 能力视图、执行合同、确定性编译、LLM 兜底、SQL 审计和 Wren 复用

## 一、问题定义

`resources/capabilities/subjects.yaml` 登记了 11 个查询主体，但当前
`backend/app/query/spec_mdl.py` 只实际编译 `disease` 和 `patrol`。其余九个主体会返回
“受控编译器不支持当前表、字段或查询视图”。

问题不只是缺少九段 `if/elif`：

1. 九个主体的业务字段、角色、枚举和指标还不完整。
2. 能力注册表虽然从物理目录补出了字段，但 `route`、`section`、`asset`、`bridge`、
   `tunnel`、`slope`、`project` 等主体在 `subjects.yaml` 中没有完整 JOIN 声明。
3. QuerySpec 编译器把主体、字段解析、JOIN、指标、展示表达式和 SQL 拼装集中在同一个
   1161 行文件中，继续增加主体会形成不可维护的大型文件。
4. 同一个“国道路段”条件可能服务于不同最终答案：
   - “行政等级为国道的路段有哪些”的最终主体是 `section`；
   - “统计国道路段中检出的病害”的最终主体是 `disease`，国道只是通过路线关联形成的范围条件。
   如果两条查询共用主体的答案或 SQL 形状，就会答非所问。
5. Wren MDL 已经比当前手写编译器更完整：当前项目有 18 个模型和 23 条关系，模型、字段、
   计算列和关系均已有机器可读事实；这些能力没有统一进入 QuerySpec 的编译链路。

## 二、目标

建立以下受控链路：

```text
用户问题
  → LLM 生成 QuerySpec
  → QuerySpec 语义、字段、范围和形状校验
  → 每个 query_data 节点生成 MdlQueryContract
  → 构建收窄的业务能力视图、MDL 子集和关系子图
  → 确定性 MDL-SQL 编译器优先
  → 确定性路径失败时，使用同一合同调用 LLM 生成一次 MDL-SQL
  → 两条路径共用 SQL 审计
  → Wren dry-plan
  → 物理 SQL 安全检查
  → 执行和结果验收
```

实现目标：

1. QuerySpec 始终是用户意图和范围的唯一权威。
2. LLM 只能在冻结合同、相关 MDL 和业务能力范围内补写 MDL-SQL。
3. 确定性编译器和 LLM 兜底必须经过同一套主体、范围、关系、形状和 `limit` 审计。
4. 十一个主体通过统一合同和通用编译器覆盖，不继续手写十一个主体分支。
5. 只向 LLM 提供本题需要的三份收窄视图，不发送完整业务注册表或完整 MDL。
6. 复用 Wren 已有的 MDL 读取、关系事实、`dry-plan`、错误定位和物理 SQL 安全检查。
7. 编译、审计或 `dry-plan` 失败必须结构化返回，不能把非空错误文本当成成功。
8. 禁止使用关键词、正则或文本相似度决定主体、字段、JOIN、指标或 SQL。

## 三、非目标

本设计不改变：

1. QuerySpec 之前的用户问题语义理解、主体路由和 QuerySpec 修复协议。
2. `post_calc` 的计算工具和计算语义。
3. 模板确认卡、内置模板和已确认模板的执行规则。
4. 前端结果工作台布局。
5. 长对话上下文继承。
6. 数据库表结构。
7. 用户裁决卡和晋级考试流程。

LLM 兜底不恢复已经废弃的“直接写物理 SQL”旧链路，也不允许模型重新生成 QuerySpec。

## 四、事实源与权限边界

不同阶段使用不同视图，但事实来源必须唯一。

| 事实 | 唯一来源 | 消费者 | 不允许的行为 |
|---|---|---|---|
| 用户要查什么、最终主体、范围、视图 | QuerySpec | 合同构建器、审计器 | LLM SQL 生成器修改合同 |
| 业务字段标签、角色、枚举、指标、规则 | 能力注册表 | Planner、合同构建器、LLM 上下文、审计器 | Python 中另建业务映射 |
| 模型名、字段名、类型、计算列、MDL 关系 | Wren MDL 与 `relationships.yml` | 合同构建器、SQL 生成器、审计器 | LLM 猜模型、猜字段、写物理表 |
| 实际执行 SQL | Wren `dry-plan` | 数据库执行器 | LLM 直接生成物理 SQL |
| SQL 是否表达合同 | 统一 SQL 审计器 | 两条 SQL 生成路径 | 依据回答文本是否非空判断成功 |

### 4.1 权威优先级

发生冲突时：

1. QuerySpec 决定“查什么”，LLM 不得改主体、过滤、指标、时间、路线或数量范围。
2. 能力注册表决定业务字段、枚举、指标公式和规则。
3. MDL 决定可用模型、真实列名、计算列和关系字段。
4. 关系子图决定哪些 JOIN 合法。
5. Wren `dry-plan` 决定 MDL-SQL 能否展开成物理 SQL。
6. SQL 审计器决定生成的 SQL 是否仍然表达 QuerySpec。

冲突不能被提示词静默解决，必须返回结构化失败证据。

## 五、QuerySpec 与 MdlQueryContract

### 5.1 QuerySpec

QuerySpec 是用户意图、范围和查询形状的权威。

例如“统计国道路段中检出的病害”：

```text
subject = disease
filters = route.level = 0
view = count
```

“国道”是病害查询通过路线关联得到的范围条件，不是最终结果主体。

### 5.2 MdlQueryContract

`MdlQueryContract` 是某一个 `query_data` QuerySpec 的可执行投影，回答“这个合法查询应如何
映射到模型、字段、关系和 SQL 形状”。

一个 `query_data` 节点对应一个合同：

```text
QueryPlan
  q1 QuerySpec → q1 MdlQueryContract
  q2 QuerySpec → q2 MdlQueryContract
  post_calc     → 不生成 SQL 合同
```

如果一个计划拆成病害总数和病害明细，两个节点各自生成合同，但共享原始问题和计划范围哈希。
两个节点可以有不同最终主体，不能为了“统一主体”删除其中一个结果。

### 5.3 合同内容

合同至少包含：

```text
contract_id
plan_id
node_id
query_spec_hash
capability_snapshot_hash
mdl_fingerprint
subject
view
root_model
field_bindings
filters
dimensions
metrics
outputs
group_by
order_by
limit
required_assertions
allowed_models
required_relationship_ids
mandatory_predicates
output_aliases
```

每个绑定必须记录来源：

- `query_spec`
- `capability_registry`
- `mdl`

合同只读，确定性编译器和 LLM 兜底都不得修改合同。合同哈希进入 SQL 审计和失败证据。

## 六、三份收窄视图

上下文选择完全由合同、能力注册表和 MDL 的结构化引用决定，不重新解释用户原话。

### 6.1 业务能力视图

输入是当前合同的字段、指标、断言和计算引用。

选择规则：

1. 加入 `filters`、`dimensions`、`metrics`、`output_projection` 和 `order_by`
   实际引用的字段。
2. 按字段的 `enum_key`、`unit`、`roles` 和 `requires_join` 展开最小依赖。
3. 只过滤枚举值时，只加入当前使用的存储值和显示标签。
4. 声明 `complete_enum_dimensions` 时，才加入该维度的完整枚举成员。
5. 引用实体族时，只加入该实体族、别名和实际成员。
6. 引用指标时，只加入该指标公式、单位和依赖。
7. 引用派生指标时，加入其操作数，但说明派生值由 `post_calc` 计算。
8. 根据 `required_assertions` 加入对应业务规则，例如 `recent_window`、
   `complete_enum_dimensions`、`top_per_outer_group`。

不加入：

- 未被合同引用的字段；
- 未被引用的指标和派生指标；
- 全部病害类型或全部枚举成员；
- 不适用于当前主体或视图的规则；
- 完整能力注册表文本。

### 6.2 MDL 子集

输入是合同的根模型和字段绑定。

选择规则：

1. 在 `subjects.yaml` 中增加明确的 `mdl_model` 映射，例如：

   ```yaml
   section:
     base_table: basic_facilities_sections
     mdl_model: section
   ```

2. 根模型固定为当前主体的 `mdl_model`。
3. 通过字段能力的 `physical_table` 和 `physical_column` 映射到对应 MDL 模型和字段。
4. 加入指标 `requires_join` 声明的模型。
5. 只输出模型名、业务说明、被引用列、JOIN 键、主键和被引用计算列。
6. 计算列必须带 MDL 中的表达式和说明，不能只给一个无法解释的列名。
7. 提示词中不提供物理库名或物理表名，避免 LLM 绕过 MDL 写物理 SQL。
8. 完整字段目录、完整物理目录和 Planner 展示字段仍是不同视图，不能互相替代。

### 6.3 关系子图

关系事实来自 Wren `relationships.yml`，不在 Python 和 QuerySpec 编译器里重复维护。

每条关系边至少包含：

```text
relationship_id
left_model
left_fields
right_model
right_fields
join_type
cardinality
condition
source
```

选择规则：

1. 从合同根模型和已选模型开始。
2. 优先使用能力字段或指标声明的 `requires_join`。
3. 要求相关模型时，只加入连接根模型和这些模型所需的关系边。
4. 多跳关系递归加入中间模型，但不得加入无关边。
5. 唯一合法路径才自动使用。
6. 存在多个语义不同且无法根据合同区分的路径时，返回
   `relationship_path_ambiguous`，不交给 LLM 猜。
7. 关系基数必须进入提示词和审计；从一方 JOIN 多条子记录时，必须使用合同允许的
   `EXISTS` 或聚合，避免行数放大。

示例一：“行政等级为国道的路段”

```text
root_model = section
models = section, route
relationships = section_route
condition = section.route_id = route.id
```

示例二：“国道路段中检出的病害”

```text
root_model = disease
models = disease, route
relationships = disease_route
condition = disease.routes_id = route.id
```

两题都使用国道条件，但根模型、关系边和结果主体不同。

### 6.4 上下文预算

上下文超限时不能从中间截断 JSON、MDL 字段或关系。

处理顺序：

1. 删除非必要模型说明和示例。
2. 删除非必要相邻模型和未引用列。
3. 保留合同、必用字段、计算依赖和关系边。
4. 仍超限时返回 `context_budget_exceeded`，不调用 LLM。

合同事实不得通过截断而变得不完整。

## 七、LLM 提示词协议

新增 `prompts/mdl_sql_generator.md`。

提示词分为固定的 `system` 和结构化 `user JSON`。

### 7.1 system

系统提示只规定流程和权限：

1. 根据合同生成一条 MDL-SQL，不重新理解或修改用户问题。
2. 只使用给定的模型、字段、指标、枚举和关系 ID。
3. 使用 MDL 模型名，不写物理库名或物理表名。
4. QuerySpec 和合同是只读权威。
5. 合同与 MDL 或业务能力冲突时停止并报告，不自行选边。
6. 只执行一条只读 `SELECT` 或 `WITH ... SELECT`。
7. 只输出规定 JSON，不输出 Markdown、思考过程或解释。

### 7.2 user JSON

输入结构：

```json
{
  "schema_version": 1,
  "question_context": {
    "original_question": "行政等级为“国道”的路段有哪些",
    "node_question": "列出行政等级为国道的路段",
    "authority": "context_only"
  },
  "contract": {},
  "business_capability": {},
  "mdl_subset": {},
  "relationship_subgraph": [],
  "compiler_failure": null
}
```

`question_context` 只帮助模型理解字段含义，不能授权模型增加合同之外的过滤条件。

`compiler_failure` 在确定性路径失败时出现，至少包含：

```text
stage
failure_code
reason
generated_mdl_sql
allowed_models
allowed_relationship_ids
preserve_contract_hash
```

不把 Wren 或 Python 的原始异常字符串直接作为唯一反馈；必须转换成结构化证据。

### 7.3 输出

只接受：

```json
{
  "mdl_sql": "SELECT ...",
  "used_models": ["section", "route"],
  "used_relationship_ids": ["section_route"]
}
```

`used_models` 和 `used_relationship_ids` 只用于交叉检查，最终以 SQL AST 审计结果为准。

空 SQL、失败文本、Markdown、多条 SQL 和无法解析的 JSON 都视为 LLM 兜底失败。

## 八、确定性优先与 LLM 兜底

### 8.1 前置条件

只有 QuerySpec 已通过以下校验，才允许构建合同：

1. JSON 结构和闭集字段校验；
2. 字段、枚举、实体和值来源校验；
3. 能力注册表校验；
4. 主体、视图、指标、维度、依赖和输入绑定校验；
5. 规划审计通过。

QuerySpec 不合格、主体冲突、范围有歧义或能力明确不可用时，不进入 MDL 编译器，也不进入
LLM SQL 兜底。

### 8.2 确定性失败后的兜底

合同构建成功后，以下任一步失败均可触发一次 LLM 兜底：

1. 确定性编译器明确不支持；
2. 确定性编译器内部抛出受控异常；
3. 确定性 SQL 未通过统一审计；
4. 确定性 SQL 未通过 Wren `dry-plan`；
5. 确定性物理 SQL 未通过安全检查。

所有失败都转换为 `CompilerFailure`，使用同一个合同进入 LLM 兜底。

本设计将“让 LLM 试一次”定义为：

1. LLM 只接收一次初始 MDL-SQL 生成任务；
2. LLM SQL 进入同一审计和 `dry-plan`；
3. 若仍失败，本次兜底结束并返回结构化原因；
4. 不在本轮自动进行第二次自由重写；
5. 后续如需修复，必须由独立的 `RepairTicket` 明确允许范围和最大轮次。

### 8.3 失败关闭

以下情况不能触发 LLM 兜底：

- QuerySpec 未通过语义或范围校验；
- 主体无法唯一确定；
- 字段能力为 `unavailable`；
- 请求超出注册的数据能力；
- 合同构建发现能力配置错误；
- 安全策略拒绝执行。

LLM 兜底不是绕过 QuerySpec 或权限校验的通道。

## 九、统一 SQL 审计

确定性和 LLM MDL-SQL 都先转换为 `MdlSqlCandidate`，再进入同一审计器。

审计使用已安装的 `sqlglot` 解析 AST，不依靠字符串包含关系判断 SQL 语义。

### 9.1 安全

1. 只允许单条 `SELECT` 或 `WITH ... SELECT`。
2. 禁止 DML、DDL、多语句和危险函数。
3. 禁止引用物理库名和物理表名。
4. 所有模型必须在 `allowed_models`。
5. 所有普通字段必须来自 `mdl_subset` 或合同允许的计算列。
6. 关系字段不能当普通输出字段。

### 9.2 最终主体

1. 根 `FROM` 必须等于合同 `root_model`。
2. JOIN 不能替换最终主体。
3. “国道路段”必须从 `section` 返回结果。
4. “国道路段检出的病害”必须从 `disease` 返回结果。

### 9.3 范围与过滤

1. 合同中的每个过滤条件必须存在。
2. 字段、比较符和存储值必须完全一致。
3. 不得增加合同之外的业务过滤。
4. 时间范围必须保持合同的半开区间。
5. 软删除等强制安全谓词必须存在。
6. `NULL`、空字符串、枚举 code 和显示标签不能混用。

### 9.4 JOIN

1. JOIN 只能使用关系子图中的边。
2. ON 条件必须匹配登记字段对。
3. 禁止笛卡尔积、猜测外键和隐式关系。
4. 关系基数需要子表存在性判断时，使用允许的 `EXISTS` 或聚合。
5. JOIN 不得产生静默行数放大。

### 9.5 视图、指标和分组

1. `count`、`sum`、`avg` 必须真的形成对应聚合。
2. `distribution`、`trend`、`top_groups` 必须真的存在分组。
3. 明细视图不得悄悄聚合。
4. 聚合指标不得退化成明细行。
5. `COUNT(DISTINCT ...)` 不得写成普通 `COUNT`。
6. 指标公式必须与能力注册表一致。
7. 派生指标不能在 SQL 中伪装成原子指标。

### 9.6 输出

1. SQL 输出列必须覆盖合同要求的字段。
2. 展示别名必须与业务能力视图一致。
3. 链式依赖必须带出路线、病害、方向、设备或任务等条件字段。
4. 不能输出合同未要求的分析分数、健康指数或伪造指标。
5. 分组结果必须保留排序依据。

### 9.7 排序与 limit

1. 最大、最小、最多、最少、前 N 必须有明确排序。
2. 单个极值结果按合同使用 `limit=1`。
3. `top_records` 和 `top_groups` 不能混用。
4. 有 `limit` 但无排序的明细查询直接失败。
5. LLM 不能扩大合同 `limit`。
6. `top_groups` 的聚合维度、分组维度和 limit 语义必须一致。

### 9.8 成功判定

只有以下条件全部满足才算成功：

```text
SQL 非空
AND AST 解析成功
AND 合同审计通过
AND 关系审计通过
AND 安全审计通过
AND Wren dry-plan 成功
AND 物理 SQL 静态安全检查通过
```

不得使用 `accepted = bool(reply)`、非空错误文本或其他宽松条件把失败算作通过。

## 十、Wren 能力复用

Wren 已经是比当前手写 QuerySpec 编译器更完整的模型和关系事实源。复用 Wren 时只复用底层能力，
不恢复已废弃的自由 SQL 链路。

### 10.1 MDL 读取

复用 `backend/app/services/wren_engine.py` 的 `_load_mdl()` 读取能力，封装成只读
`MdlSnapshot`：

1. 模型；
2. 字段类型；
3. 字段说明；
4. 计算列；
5. cube；
6. `relationships.yml`；
7. MDL 文件指纹。

不在新模块里重新解析 YAML 或复制模型清单。

### 10.2 模型和字段定位

复用 `_schema_index()` 和 `columns_of()` 提供的模型/字段事实，用于：

1. 物理字段定位到 MDL 模型；
2. 生成 MDL 子集的真实列名；
3. 将 `dry-plan` 错误中的未知模型和未知字段转换为允许值。

`related_models()` 和 `search_models()` 可以继续产生 `trusted=false` 候选证据，但不得用于
最终上下文选择、JOIN 选择或 SQL 生成。

### 10.3 关系复用

复用 `relationships.yml` 的关系定义和 `_relationship_columns()` 提取的合法字段对：

1. 关系模型；
2. 合法左右字段；
3. 关系方向；
4. 基数；
5. 人工或自动来源。

现有 `join_contract_issue()` 的行为目标可以复用，但新审计器应使用 `sqlglot` AST 获取
FROM、JOIN、ON 和字段，而不是依赖新的业务正则扫描。关系事实来自 MDL，JOIN 判断来自 AST。

### 10.4 dry-plan

复用 `compile_mdl_sql_verbose()` 的 Wren 编译能力，并由新适配器负责：

1. 调用 `WrenEngine.dry_plan(mdl_sql)`；
2. 支持返回值字符串或字典；
3. 将异常转换成 `DryPlanFailure`；
4. 记录原始错误；
5. 用 `model_names()`、`columns_of()` 和关系事实补充允许值；
6. 返回物理 SQL或结构化失败，不直接执行。

### 10.5 编译错误提示

复用 `compile_error_hint()` 的未知字段和未知模型定位能力，但输出改为结构化：

```text
stage
failure_code
model
column
allowed_columns
allowed_models
reason
preserve_contract_hash
```

### 10.6 物理 SQL 静态检查

`dry-plan` 成功后继续复用 `backend/app/services/static_check.py` 的 `check_sql()`：

1. 表在字段目录；
2. 物理列存在；
3. 编码值在字典；
4. CTE 输出列成立；
5. 派生表字段契约成立。

MDL 合同审计负责“SQL 是否答对这道题”，Wren 负责“MDL-SQL 能否展开”，
`static_check` 负责“最终物理 SQL 是否站得住”。

### 10.7 明确不复用

以下旧能力不进入新主链路：

1. LLM 直接生成物理 SQL；
2. 由文本相似度决定最终模型和 JOIN；
3. 编译失败后用关键词剥离过滤条件；
4. 空回复或错误文本作为成功结果；
5. 已删除的 `query_semantic`、`table_selector`、`sql_mdl` 和 `sql_physical` 旧工作流。

## 十一、执行时序

```text
for each query_data node:
    validate QuerySpec and capabilities
    build MdlQueryContract
    load MdlSnapshot by fingerprint
    build CapabilityContext
    build MdlContext
    build RelationshipSubgraph
    try deterministic compiler:
        compile
        audit
        dry-plan
        physical static check
    if any deterministic stage fails:
        create CompilerFailure
        build LLM prompt context
        call LLM once
        audit LLM MDL-SQL
        dry-plan
        physical static check
    execute physical SQL
    verify result against QuerySpec

after query_data nodes:
    run post_calc and final answer assembly
```

每个 `query_data` 节点独立选择执行路径。一个节点使用确定性路径、另一个节点使用 LLM 兜底是
允许的；失败节点不能把自己的错误范围传播给无关节点。依赖失败节点的下游节点必须明确
`blocked`。

## 十二、目录与职责

新增目录：

```text
backend/app/query/mdl_sql/
  __init__.py
  models.py
  catalog.py
  contract_builder.py
  context/
    capabilities.py
    mdl.py
    relationships.py
    prompt_context.py
  compiler/
    deterministic.py
    llm_fallback.py
    pipeline.py
  audit/
    ast.py
    contract_audit.py
    relationship_audit.py
    safety_audit.py
    failures.py
  dry_plan.py
```

职责：

- `models.py`：只放不可变数据对象，不放业务执行逻辑。
- `catalog.py`：只读 MDL、模型索引、字段索引和指纹。
- `contract_builder.py`：把合法 QuerySpec 投影成合同。
- `context/`：只负责三份收窄视图和提示词 JSON。
- `compiler/deterministic.py`：只按合同组装 MDL-SQL。
- `compiler/llm_fallback.py`：一次受控 LLM 调用，不重新规划 QuerySpec。
- `compiler/pipeline.py`：主路径、失败切换、统一候选输出。
- `audit/`：解析 AST 和检查合同、关系、安全。
- `dry_plan.py`：Wren 编译适配和结构化错误。

配套修改：

```text
prompts/mdl_sql_generator.md
resources/capabilities/subjects.yaml
resources/capabilities/mdl_compilation.yaml
resources/capabilities/index.yaml
```

`mdl_compilation.yaml` 只保存上下文预算、LLM 兜底次数、允许的 SQL 形状和安全策略，
不得包含业务关键词或字段别名映射。

现有 `backend/app/query/spec_mdl.py` 迁移后只保留兼容入口，不再承载主体分支和 SQL 业务逻辑：

```python
def compile_spec_mdl(spec):
    return mdl_sql_pipeline.compile_deterministically(spec)
```

迁移某个函数后必须删除旧实现，禁止双写。

## 十三、核心数据对象

### 13.1 MdlQueryContract

一个 `query_data` 节点的不可变执行合同，包含主体、根模型、字段绑定、过滤、指标、维度、输出、
排序、limit、断言、允许模型、关系 ID 和三个指纹。

### 13.2 CapabilityContext

只包含合同引用字段、枚举、实体族、指标、派生指标操作数和适用业务规则。

### 13.3 MdlContext

只包含根模型、相关模型、被引用字段、计算字段、JOIN 键和说明，不包含物理表名。

### 13.4 RelationshipSubgraph

本题允许的模型节点和关系边。

### 13.5 CompilerFailure

确定性路径失败的统一证据，包含阶段、代码、原因、生成 SQL、允许值和合同哈希。

### 13.6 MdlSqlCandidate

```text
mdl_sql
source = deterministic | llm_fallback
contract_id
contract_hash
used_models
used_relationship_ids
```

### 13.7 SqlAuditReport

```text
accepted
issues
root_model
used_models
used_fields
used_relationships
normalized_structure
```

## 十四、确定性编译器如何覆盖十一个主体

编译器不再根据主体名称执行十一个分支，而是消费已经解析完成的合同：

1. `root_model` 决定 `FROM`。
2. `outputs` 和 `metrics` 决定 SELECT。
3. `filters` 决定 WHERE。
4. `relationship_subgraph` 决定 JOIN。
5. `dimensions` 决定 GROUP BY。
6. `order_by` 和 `limit` 决定排序与截断。
7. `mandatory_predicates` 决定软删除等安全条件。

疾病和巡检的历史特殊口径迁移到能力注册表的字段表达式、指标公式、关系和断言中，不再写成：

```python
if subject == "disease":
    ...
elif subject == "patrol":
    ...
```

主体差异只允许通过配置事实和 MDL 事实表达。

## 十五、错误模型

至少登记以下失败代码：

| 阶段 | failure_code | 含义 |
|---|---|---|
| contract | `contract_build_failed` | 合法 QuerySpec 无法投影成合同 |
| context | `context_budget_exceeded` | 必要事实无法完整放入上下文 |
| relationship | `relationship_path_ambiguous` | 存在多个无法由合同区分的 JOIN 路径 |
| deterministic | `compiler_not_covered` | 确定性编译器未覆盖当前合同 |
| deterministic | `compiler_exception` | 确定性编译器内部受控异常 |
| audit | `root_subject_mismatch` | 根模型与 QuerySpec 主体不一致 |
| audit | `unexpected_model` | 使用未授权模型 |
| audit | `unexpected_field` | 使用未授权字段 |
| audit | `missing_filter` | 丢失合同过滤 |
| audit | `extra_filter` | 增加合同外过滤 |
| audit | `filter_value_mismatch` | 过滤值不一致 |
| audit | `illegal_relationship` | JOIN 不在关系子图 |
| audit | `cardinality_risk` | JOIN 可能放大结果 |
| audit | `invalid_view_shape` | 聚合、分组或明细形状错误 |
| audit | `missing_output` | 缺少合同输出 |
| audit | `order_required` | 极值或排名无排序 |
| audit | `invalid_limit` | limit 与合同不一致 |
| audit | `physical_table_reference` | MDL-SQL 引用物理表 |
| audit | `soft_delete_missing` | 缺少强制软删除条件 |
| dry_plan | `dry_plan_failed` | Wren 无法展开 MDL-SQL |
| physical | `physical_sql_invalid` | 展开后的物理 SQL 未通过静态检查 |
| fallback | `llm_sql_invalid` | LLM 输出不是合法单条 MDL-SQL |
| fallback | `llm_fallback_failed` | LLM SQL 未通过审计或 dry-plan |

用户的最终拒绝说明必须区分：

- 用户问题超出数据能力；
- QuerySpec 不合法；
- MDL 能力或关系配置缺失；
- 确定性编译器失败；
- LLM 兜底失败；
- 范围内确实没有数据。

不得只显示内部英文异常名。

## 十六、配置变更

### 16.1 subjects.yaml

每个主体增加：

```yaml
mdl_model: <model_name>
```

不允许在 Python 中维护“主体 → 模型”的第二份映射。

### 16.2 mdl_compilation.yaml

保存：

```text
schema_version
max_context_bytes
max_models_per_contract
max_relationship_hops
llm_fallback_attempts
forbidden_sql_types
require_soft_delete
```

该文件走统一能力加载器、哈希和配置守护测试。

### 16.3 relationships.yml

继续作为 Wren 技术关系事实源。业务能力配置通过 `requires_join` 引用关系名或目标模型，
不在能力文件中复制完整 ON 条件。

## 十七、迁移策略

1. 先增加合同、上下文和审计的契约测试。
2. 建立 `MdlSnapshot`，复用现有 Wren 读取能力，不复制 MDL 解析。
3. 为所有主体补齐 `mdl_model` 映射。
4. 把现有疾病和巡检查询编译逻辑迁移到合同构建器和通用编译器。
5. 删除 `spec_mdl.py` 中的旧业务分支，只保留兼容入口。
6. 接入确定性 pipeline，保持现有 `single_turn_compiler_enabled` 行为。
7. 接入一次 LLM 兜底。
8. 将 `relationships.yml` 和 `dry-plan` 接入统一审计。
9. 运行全量查询测试和前端构建。
10. 生成全新 `15 道新题 + 随机 5 道旧题` 做回归考试。

每一步都必须删除被迁移的旧实现，不保留新旧双写。

## 十八、契约测试

### 18.1 主体与范围

1. “行政等级为国道的路段有哪些”根模型必须是 `section`。
2. “统计国道路段中检出的病害”根模型必须是 `disease`。
3. 两个问题不得共用 SQL 合同或最终主体。
4. 十一个主体都能生成合同。
5. 多节点计划为每个 `query_data` 节点生成独立合同。

### 18.2 上下文

1. 业务能力视图不包含未引用字段和指标。
2. MDL 子集不包含无关模型和物理表名。
3. 关系子图只包含合同所需关系。
4. 完整枚举只在声明完整性断言时出现。
5. 实体层级只在引用对应实体族时出现。
6. 上下文超限时不发生静默截断。

### 18.3 编译与兜底

1. 确定性编译成功时不调用 LLM。
2. QuerySpec 校验失败时不调用 LLM SQL 兜底。
3. QuerySpec 合法但确定性编译失败时调用 LLM 一次。
4. LLM 不能修改合同主体、过滤、指标、排序或 limit。
5. LLM 空回复、Markdown、多 SQL 和非法 JSON 均失败关闭。
6. LLM 失败后不进入第二次自由重写。

### 18.4 审计

1. 根主体错误被拒绝。
2. 额外过滤条件被拒绝。
3. 丢失时间、路线、病害或任务范围被拒绝。
4. 未登记 JOIN、错误字段对和笛卡尔积被拒绝。
5. 聚合指标被写成明细被拒绝。
6. 分布查询没有 GROUP BY 被拒绝。
7. 最大、最小和前 N 缺少排序被拒绝。
8. 极值 limit 不是合同值被拒绝。
9. 物理表引用和危险 SQL 被拒绝。
10. 缺少软删除条件被拒绝。
11. Wren `dry-plan` 失败被转换为结构化证据。
12. 物理 SQL 检查失败不能被当成成功。

### 18.5 稳定性

1. 相同 QuerySpec、能力快照和 MDL 指纹生成相同合同哈希。
2. 同义问法在 QuerySpec 相同时使用相同合同和 SQL 审计规则。
3. 十个主体之外新增模型时，不需要增加 Python 主体分支。
4. 不允许新增业务关键词表和业务正则。

## 十九、验收标准

功能验收：

1. 十一个已登记主体中，QuerySpec 合法的普通 count、list、detail、distribution、
   top_records、top_groups 和 trend 查询能生成可审计 MDL-SQL。
2. 确定性编译器失败后，LLM 只使用同一合同和收窄视图兜底一次。
3. 确定性 SQL 和 LLM SQL 的全部审计结果一致。
4. Wren `dry-plan` 成功后才生成物理 SQL。
5. 物理 SQL 通过 `static_check` 后才执行。
6. 失败结果结构化，不把错误文本当 accepted。

回归验收：

1. 现有查询测试和新增合同测试全部通过。
2. 前端构建通过。
3. 重新生成 15 道从未见过的新题和随机 5 道旧题。
4. 新题至少 `12/15`、旧题至少 `4/5`、总计至少 `18/20`。
5. 考试不得复用已曝光题集，代码变化后必须重新生成。
6. 不允许靠题目 ID、关键词、正则或 `required_contains` 通过考试。

## 二十、完成后的明确边界

完成后，系统仍然只回答注册了真实字段、实体、指标和关系能力的数据问题。

对于不存在的数据能力：

1. 明确说明缺少哪项能力；
2. 说明当前可用的替代指标；
3. 不为迎合问题而修改范围；
4. 不伪造字段、指标或关系；
5. 不把 LLM 兜底当作能力缺失的绕过手段。
