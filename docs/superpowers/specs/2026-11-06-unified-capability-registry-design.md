# 统一能力注册表与实体/枚举解析设计

> 日期：2026-11-06  
> 状态：待用户审阅  
> 范围：单轮 QuerySpec 查询链路的字段、枚举、实体、指标、派生指标、校验、编译和有限修复

## 一、问题定义

当前系统虽然存在字段目录和字段触达，但新 QuerySpec 链路没有真正把“完整事实”交给 Planner：

1. 进入规划器时，`schema` 只包含 `(表名, 列名)`，没有列注释、类型、枚举和字段角色。
2. `FieldReachSet` 默认是空集合；实际调用 `start_single_turn(message)` 时没有传入 `field_grounding.ground()` 的结果。
3. 即使传入字段触达，`FieldReachHit` 也只有表、列、命中词和分数，没有枚举 `code_map`。
4. 数据库目录实际解析出 69 个带 `code_map` 的枚举列，但 `values._CODE_FIELDS` 只能静态处理其中 11 个。
5. 指标存在两套不一致的事实源：`field_catalog.metric_names()` 认为大量数值列可形成指标，`spec_mdl` 编译器却只支持其中一部分。

结果是 Planner 看到的事实和执行器支持的能力不一致，模型会伪造指标，例如 `disease_density`。

层级实体配置也散落在多个位置：

- `route_resolver._DAMAGE_ALIASES`
- `query_rewrite.py` 的口语别名
- `nl2sql.expand_problem_types()`
- `sql_engine.expand_problem_type_filters()`
- 数据库中的 `problem_alias`

这几处分别维护病害层级，后续必然漂移。

## 二、目标

建立唯一的 `CapabilityRegistry`，让 Planner、Validator、Compiler 读取同一份能力事实：

- Planner 只能看到当前主体真实支持的字段、枚举、指标、派生指标和视图。
- Validator 遇到未知字段、未知指标或无法解析的枚举时直接拒绝。
- Compiler 只编译注册表中存在的指标和派生公式。
- 拒绝时返回结构化 `FailureEvidence`，包含原值、允许值、来源和修复建议。
- LLM 根据证据重新生成 QuerySpec，最多修复两轮。
- 两轮后仍失败，明确返回“当前数据能力不支持”，不得编造字段或猜公式。

## 三、非目标

本阶段不改：

- 长对话上下文继承。
- 前端聊天交互和结果工作台布局。
- 数据库表结构。
- 模板挖掘算法。
- 自由 SQL 模式。

## 四、事实分层

能力事实按以下优先级生成，不允许在查询代码中另建同名映射：

1. **实时物理结构**：`information_schema` 中的表、列、类型、注释。
2. **人工能力配置**：主体、逻辑字段、指标、派生指标、枚举补充、实体层级和别名。
3. **数据库动态字典**：病害类型、路线、设备等运行数据。
4. **字段触达证据**：只表示“用户这句话可能提到了某个字段”，不是物理事实，也不能直接进入 SQL。

配置与数据库冲突时：

- 人工配置引用不存在的物理列或枚举 code：启动校验失败。
- 数据库新增了枚举列但未配置补充信息：仍可作为普通字段读取，但不能作为受控枚举直接解析。
- 数据库动态实体成员不存在：配置校验失败；新成员未纳入实体族时记录待审核，不自动猜测归属。

## 五、统一配置目录

所有人工维护的语义与能力映射统一放在：

```text
resources/capabilities/
  index.yaml
  subjects.yaml
  fields.yaml
  metrics.yaml
  derived_metrics.yaml
  enums.yaml
  entities.yaml
  entity_hierarchies.yaml
  aliases.yaml
  identifiers.yaml
  query_shapes.yaml
  repair_policy.yaml
  template_capabilities.yaml
```

`auto_templates.json`、`preset_queries.json`、日志、缓存和数据库实时读取结果属于运行数据，不迁入该目录。

迁移完成后，不允许在 `.py` 文件中继续维护业务枚举、实体别名、病害层级、指标名或字段到 SQL 的映射。

## 六、配置模型

### 6.1 subjects.yaml

定义查询主体、主表、关联链路和允许的视图。

```yaml
subjects:
  disease:
    base_table: ai_problem
    joins:
      route: { table: basic_facilities_routes, on: disease.routes_id = route.id }
    allowed_views: [count, distribution, list, detail, top, trend]
```

### 6.2 fields.yaml

定义逻辑字段到物理字段或受控表达式的绑定。

```yaml
fields:
  disease:
    route_name:
      expression: disease.route_name
      label: 路线名称
      roles: [dimension, output]
    route_length_km:
      expression: route.route_length_km
      label: 路线长度
      unit: km
      roles: [metric]
      requires_join: route
```

逻辑字段必须引用真实物理列或已登记的计算表达式。

### 6.3 metrics.yaml

定义当前主体可直接查询的原子指标。

```yaml
metrics:
  disease:
    count:
      expression: COUNT(*)
      label: 数量
      unit: 处
      allowed_views: [count, distribution, top, trend]
    total_area:
      expression: SUM(disease.area)
      label: 总面积
      unit: ㎡
      allowed_views: [count, distribution, top, trend]
    route_length_km:
      expression: route.route_length_km
      label: 路线长度
      unit: km
      requires_join: route
```

Planner 只能输出这里列出的 metric id。

### 6.4 derived_metrics.yaml

定义 post_calc 派生指标。派生指标不是 SQL 可直接引用的原子列。

```yaml
derived_metrics:
  disease:
    disease_density:
      label: 病害密度
      unit: 处/km
      expression: count / route_length_km
      operands: [count, route_length_km]
      evaluator: post_calc
      lower_is_better: true
```

Compiler 不得把 `disease_density` 当 SQL 指标。Planner 应输出原子指标，再由 post_calc 计算派生指标。

### 6.5 enums.yaml

枚举必须按 `表 + 字段` 定义，不能只按列名全局注册。

```yaml
enums:
  ai_problem.up_or_down:
    label: 上下行
    source: column_comment
    values:
      "0": { label: 全部, display: 全部/未区分 }
      "1": { label: 上行 }
      "2": { label: 下行 }
```

数据库注释可自动生成 `values`；`enums.yaml` 只保存补充、同义词和显示策略。

### 6.6 entities.yaml

业务实体字典描述行级实体，不描述枚举 code。

```yaml
entities:
  disease_type:
    source:
      table: ai_problem_base
      value_column: problem_type
      code_column: problem_code
      alias_column: problem_alias
    identity: problem_code
```

### 6.7 entity_hierarchies.yaml

实体族不局限于裂缝，所有病害类型都使用同一模型。

```yaml
families:
  crack:
    entity: disease_type
    label: 裂缝
    aliases: [裂缝类, 裂缝病害]
    members:
      - 横向裂缝
      - 纵向裂缝
      - 网状裂缝
    resolution:
      default: expand_family
      ambiguity: confirm
```

成员必须存在于当前数据库实体字典，否则注册表构建失败。

### 6.8 aliases.yaml

口语别名只产生候选实体或字段证据，不能直接写入 SQL。

```yaml
aliases:
  disease_type:
    "横缝": 横向裂缝
    "纵缝": 纵向裂缝
    "龟裂": 网状裂缝
```

### 6.9 query_shapes.yaml

定义受控视图及每个视图的最小契约，替代分散在代码中的形状判断。

### 6.10 repair_policy.yaml

定义各类失败允许的修复动作。

```yaml
failures:
  unknown_metric:
    actions: [rewrite_query_spec]
    max_rounds: 2
  unresolved_enum:
    actions: [ask_confirmation]
    max_rounds: 0
```

## 七、运行模型

### 7.1 CapabilityRegistry

启动或首次请求时构建不可变 `CapabilitySnapshot`：

```text
PhysicalSchemaCatalog
  + CapabilityConfig
  + DynamicEntityDictionaries
  → CapabilitySnapshot
```

快照必须带版本或哈希，日志和 FailureEvidence 记录使用的能力快照 ID。

### 7.2 PlannerCapabilityView

Planner 不接收全部 592 列。代码根据主体和触达证据生成当前问题的能力视图：

```json
{
  "subject": "disease",
  "fields": [],
  "enums": [],
  "metrics": [],
  "derived_metrics": [],
  "allowed_views": [],
  "field_reach_evidence": []
}
```

字段触达证据只引用 registry 中的字段和能力，不复制另一份注释或枚举事实。

### 7.3 EnumResolver

输入：`kind + table + column + raw_value`。

解析顺序：

1. 精确匹配枚举 code 或 label。
2. 精确匹配注册别名。
3. 唯一模糊候选。
4. 多候选返回确认卡。
5. 无候选返回 `unresolved_enum`。

任何情况下都不能把未解析的口语直接拼进 SQL。

### 7.4 EntityResolver

输入：实体类型 + 原始文本。

解析顺序：

1. 精确实体名称或 code。
2. 精确实体别名。
3. 实体族别名。
4. 实体族明确成员展开。
5. 多候选返回确认卡。
6. 无候选返回 `unknown_entity`。

“裂缝”按实体族展开；不会把字面字符串“裂缝”直接加入 `IN`。

### 7.5 上下行策略

- 用户明确说“上行”或“下行”：精确过滤对应 code。
- 用户只说“上下行”且未说“分别”：按上行、下行分别展示。
- 字段存在 `0=全部/未区分` 时，将其作为独立类别保留，不并入上行或下行。
- 用户明确说“全部方向”：不添加上下行过滤，并按注册表策略决定是否展示方向分组。

## 八、指标伪造的彻底拦截

Planner 输出的每个 metric id 必须经过以下检查：

1. metric id 是否在 `metrics.yaml` 或 `derived_metrics.yaml`。
2. metric 是否属于当前 subject。
3. metric 是否允许当前 view。
4. 所需 join、字段和单位是否存在。
5. derived metric 的 operands 是否都已在依赖节点中产生。

失败返回：

```json
{
  "failure_code": "unknown_metric",
  "node_id": "q1",
  "original_metric": "disease_density",
  "reason": "disease_density 是派生指标，不能作为 SQL 原子指标",
  "allowed_metrics": ["count", "total_area", "average_area", "route_length_km"],
  "allowed_derived_metrics": ["disease_density"],
  "suggested_repair": "使用 count 和 route_length_km 查询，再由 post_calc 计算 disease_density"
}
```

LLM 收到结构化证据后最多修复两轮。两轮失败返回 `unsupported_capability`，不执行 SQL。

同样规则适用于：

- 未知字段。
- 字段不属于当前 subject。
- 非法 view/group/metric 组合。
- 缺少 derived metric 操作数。
- 引用了当前主体不支持的 join。

## 九、数据流

```text
用户问题
  → 用户原话与实体候选证据
  → CapabilityRegistry 生成 PlannerCapabilityView
  → Planner 生成 QuerySpec
  → 结构校验
  → 能力注册表校验
  → 枚举/实体解析
  → 依赖与视图契约校验
  → Compiler 使用同一 registry 编译
  → 执行
  → 结果验收
  → 失败：FailureEvidence
  → Planner/Controller 有界修复（最多两轮）
  → 成功或明确不支持
```

## 十、配置迁移

迁移顺序：

1. 建立 `resources/capabilities/` 和统一加载器。
2. 将现有 `resources/lexicon`、`field_meta`、`query_identifiers`、`repair_policy` 等映射迁入。
3. 将病害别名、实体族和上下行规则从 `.py` 文件迁入。
4. 将 `metric_names()` 和 `spec_mdl` 支持的指标合并为 registry。
5. 删除字段触达后对 SQL 的正则式业务改写。
6. QuerySpec Planner、Validator、Compiler 全部改为读取 registry。
7. 旧链路接入同一 registry 适配器，避免两条事实源并存。

迁移期间，一旦某个映射迁入 registry，原位置必须删除，不能双写。

## 十一、契约测试

必须新增以下测试：

1. registry 中每个物理字段都存在于实时结构。
2. registry 中每个枚举 code/label 与数据库注释或人工覆盖一致。
3. 所有带 `code_map` 的表字段都能通过通用 EnumResolver 解析，不依赖静态字段名单。
4. registry 中每个 metric 在声明的 subject/view 上都能编译。
5. 每个 derived metric 只能由 post_calc 计算。
6. 未知 metric、错误 subject metric、缺少操作数均返回结构化 FailureEvidence。
7. 修复循环严格最多两轮；失败后不执行 SQL。
8. 每个实体族成员必须存在于数据库实体字典。
9. “裂缝”及至少两个其他病害实体族能正确展开。
10. `up_or_down` 在不同表使用各自的 code_map，不能共享错误的全局映射。
11. 只说“上下行”时输出上行、下行和“全部/未区分”三个独立类别。
12. 配置目录外不得新增人工维护的业务映射。

## 十二、验收题目

- “裂缝有多少处？”→ 展开到裂缝实体族全部真实成员。
- “上下行分别有多少处？”→ 使用病害表字段的枚举定义。
- “本月巡检完成情况如何？”→ 不自动增加 `patrol_status=已完成`。
- “哪条路的健康状况最好？”→ 不使用不存在的 `disease_density` 原子指标。
- 任意模糊实体或枚举输入→ 返回确认卡或明确不支持，不直接进 SQL。

最终目标是整个能力契约通过测试，而不是逐题增加修复逻辑。
