# 计算工具注册表与 `post_calc` 执行设计

> 日期：2026-11-06  
> 状态：用户已确认实施  
> 范围：单轮 QuerySpec、ReAct 与旧复合计划共用的结构化计算层

## 一、问题定义

当前 `post_calc` 主要依赖 `agent.py` 中的字符串判断和分散函数完成算术。

具体问题：

1. 加减乘除、比例、密度、平均、趋势等计算散落在多个函数中。
2. 找不到现成函数时，会退化为让 LLM 直接心算。
3. 参数来源、公式、工具名和失败原因没有统一契约。
4. ReAct 控制器无法区分“选错计算工具”“操作数缺失”“除数为零”和“结果无效”。
5. 大量计算代码继续堆积在 `agent.py`，可读性和迁移成本持续恶化。

## 二、目标

建立统一的 `CalculationRegistry` 和结构化计算执行器：

- LLM 只选择计算工具、操作符和操作数来源，不直接给最终算术结果。
- 通用代码使用 `Decimal` 执行标量、列和序列计算。
- 每个计算返回输入来源、公式、工具名、输出、精度和失败证据。
- `post_calc` 调用计算执行器，不再自行维护大量业务函数。
- ReAct 可以把计算失败作为结构化观察，并在允许范围内选择修复工具。
- 原有 `agent.py` 中的计算实现迁移后删除，不保留双写兼容函数。

## 三、非目标

本阶段不实现：

- 任意表达式解析器。
- `eval`、`exec` 或动态 Python 执行。
- 用户自定义公式编辑器。
- SQL 内聚合指标替代。
- 完整迁移 `nl2sql.py` 的所有业务逻辑。

## 四、目录结构

```text
backend/app/calculations/
  __init__.py
  models.py
  numbers.py
  evidence.py
  scalar.py
  columns.py
  series.py
  registry.py
  executor.py
  post_calc.py
  domain/
    __init__.py
    comparison.py
    density.py
    ranking.py
    trend.py

backend/app/services/tools/big/
  calculate_scalar.py
  calculate_columns.py
  calculate_series.py

resources/capabilities/
  calculators.yaml
```

职责：

- `models.py`：操作数、操作符、结果和失败证据的数据模型。
- `numbers.py`：十进制解析、舍入、空值和零分母策略。
- `evidence.py`：行标签补齐、紧凑表格证据、数字列识别。
- `scalar.py`：两个或多个标量之间的算术。
- `columns.py`：同一行两列或多列之间的算术。
- `series.py`：批次、时间序列和分组序列之间的差异与变化率。
- `registry.py`：加载 `calculators.yaml`，校验操作符和工具能力。
- `executor.py`：根据计算工具和结构化参数执行，并统一返回结果。
- `post_calc.py`：`post_calc` 总入口；只负责调度计算或有界分析，不实现具体算术。
- `domain/*`：业务公式编排，例如密度和趋势；复用通用计算工具。
- `services/tools/big/calculate_*.py`：把计算包包装为 ReAct 可调用工具。

## 五、计算配置

`resources/capabilities/calculators.yaml` 定义：

```yaml
calculators:
  calculate_scalar:
    input_shapes: [scalar_list]
    operations: [add, subtract, multiply, divide, ratio, percentage_share, percentage_change]
    precision: 6
    zero_denominator: error
  calculate_columns:
    input_shapes: [rows]
    operations: [add, subtract, multiply, divide, ratio, percentage_share, percentage_change]
    precision: 6
    zero_denominator: row_error
  calculate_series:
    input_shapes: [series]
    operations: [subtract, percentage_change]
    precision: 6
    zero_denominator: error
```

配置是允许工具和操作符的闭集。未登记的工具、操作符或精度策略必须拒绝执行。

## 六、结构化计算契约

QuerySpec 的 `post_calc` 节点新增可选 `calculation` 对象：

```json
{
  "tool": "calculate_columns",
  "operation": "divide",
  "operands": [
    {"name": "numerator", "source": {"node": "q1", "field": "count"}},
    {"name": "denominator", "source": {"node": "q1", "field": "route_length_km"}}
  ],
  "output": {
    "name": "disease_density",
    "label": "病害密度",
    "unit": "处/km"
  },
  "precision": 4,
  "zero_denominator": "row_error"
}
```

禁止：

- 字符串表达式作为执行参数。
- LLM 提供未经计算工具验证的最终数值。
- 在 `calculation` 中修改用户路线、时间、类型或任务范围。

## 七、操作语义

- `add`：`a + b`
- `subtract`：`a - b`
- `multiply`：`a * b`
- `divide`：`a / b`
- `ratio`：`a / b`
- `percentage_share`：`numerator / total * 100`
- `percentage_change`：`(new - old) / old * 100`

比例、占比和百分比变化必须使用命名操作数：

- `divide` / `ratio`：`numerator`、`denominator`
- `percentage_share`：`numerator`、`total`
- `percentage_change`：`old`、`new`

所有输入先转为 `Decimal`。空值、不可解析数字、除数为零和输出列缺失均返回结构化失败，不做静默零值替换。

## 八、结果契约

成功结果：

```json
{
  "accepted": true,
  "tool": "calculate_columns",
  "operation": "divide",
  "formula": "count / route_length_km",
  "columns": ["route_name", "count", "route_length_km", "disease_density"],
  "rows": [],
  "calculation": {
    "operands": [],
    "precision": 4,
    "unit": "处/km"
  }
}
```

失败证据：

```json
{
  "accepted": false,
  "failure_code": "calculation_zero_denominator",
  "tool": "calculate_columns",
  "operation": "divide",
  "operand_indexes": [1],
  "reason": "第 3 行 route_length_km 为 0",
  "allowed_actions": ["rewrite_calculation", "ask_user"]
}
```

## 九、`post_calc` 新流程

```text
post_calc 收到 task + 上游 results
  → 有 calculation：校验工具、操作符、来源和类型
  → 调用对应计算工具
  → 成功：返回结果与公式
  → 失败：返回结构化 FailureEvidence
  → 无 calculation：只把紧凑证据交给分析 LLM，不自行猜算术
```

旧的 `if "密度"`、`if "占比"`、`if "严重"`、`re.search("趋势")` 等计算触发分支删除。

## 十、ReAct 接入

新增工具：

- `calculate_scalar`
- `calculate_columns`
- `calculate_series`

Planner 能力视图增加 `calculators` 详情。Controller 只可改变计算工具和参数，不可改用户范围。

新增失败码：

- `calculation_tool_unknown`
- `calculation_operation_unknown`
- `calculation_operand_missing`
- `calculation_type_mismatch`
- `calculation_zero_denominator`
- `calculation_output_invalid`

修复策略候选工具为上述三个计算工具；具体候选由 failure evidence 中的 `input_shape` 决定。

## 十一、旧代码迁移

从 `agent.py` 删除并在计算包重建：

| 旧实现 | 新位置 |
|---|---|
| `_numeric_value` | `calculations/numbers.py` |
| `_row_with_group_labels` | `calculations/evidence.py` |
| `_compact_table_evidence` | `calculations/evidence.py` |
| `_difference_conclusion` | `calculations/domain/comparison.py` |
| `_compare_datasets` | `calculations/domain/comparison.py` |
| `_per_group_max_rows` | `calculations/domain/ranking.py` |
| `_density_rows` | `calculations/domain/density.py` |
| `_trend_conclusion`、`_sequence_change_conclusion` | `calculations/domain/trend.py` |
| `_ai_reliability_conclusion`、`_per_group_average_rows`、`_ratio_and_average_conclusion` | 通用计算工具或对应 domain 模块 |
| `_deterministic_table_conclusion` | `calculations/domain/ranking.py` |
| `_post_calc_payload` | `calculations/post_calc.py` |

迁移后允许保留的唯一兼容入口是 `agent._post_calc_payload` 的单行委托；若所有调用方已更新，则直接删除。

## 十二、代码组织约束

- 新业务公式必须进入独立 domain 模块并登记到 `calculators.yaml`。
- 计算工具注册文件不得包含业务判断。
- `post_calc.py` 不得包含加减乘除实现。
- `agent.py` 不得新增任何计算业务函数。
- 每个迁移完成后删除原函数，禁止兼容双写。

## 十三、测试与验收

必须覆盖：

1. 两个数字加、减、乘、除。
2. 除数为零返回结构化失败。
3. 两列逐行除法保留原行 ID、路线和展示字段。
4. 比例、占比、百分比变化分别测试。
5. 空值、非法数字、精度和舍入。
6. 序列变化包含零值批次。
7. 未登记工具和操作符被拒绝。
8. 不执行任意表达式或 `eval`。
9. `post_calc` 只传结构化工具参数。
10. ReAct 计算失败能进入有界修复。
11. 原有查询测试全部通过。
12. 真实数据库复测密度、AI 识别占比、最近5次变化和健康综合评价。

## 十四、完成标准

- 所有新增计算通过工具执行。
- `agent.py` 中原计算函数已删除。
- 配置目录外不存在人工维护的计算公式。
- 失败有结构化原因，不把 LLM 心算当成功。
- 旧计算测试全部迁移到新公共接口。
