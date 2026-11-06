# 计算工具注册表真实数据库回归

> 日期：2026-11-06  
> 数据源：MySQL 巡检业务库  
> 执行链：QuerySpec → `post_calc` → registered calculation tool

## 验收结果

### 1. 病害密度

问题：

```text
裂缝和坑槽各自在不同道路上的病害密度
```

结果：

- 使用 `calculate_columns:divide`。
- 对 39 个“路线 × 病害类型”分组逐行计算。
- 保留路线名称、病害类型、数量、路线长度和病害密度。
- “未知路线”长度为 0，被 `skip_rows` 排除，并在计算证据中列为
  `excluded_row_ids=["未知路线"]`。
- 西乡段纵向裂缝密度 `92.9412 处/km`。

### 2. AI 识别可靠性

问题：

```text
AI识别的病害类型，靠谱吗？有多少是认不出来的？
```

结果：

- 总病害数：`34889`。
- 低置信度病害数：`17623`。
- 低置信度口径来自注册业务规则 `confidence:low_confidence`，阈值为 `<0.7`。
- 使用 `calculate_scalar:percentage_share`。
- 占比：`50.51%`。
- 公式：`17623 / 34889 * 100`。

### 3. 最近5次巡查变化

问题：

```text
坑槽在最近5次巡查的数量变化
```

结果：

- 批次按 `patrol.create_time DESC` 获取。
- 标签来自批次节点，数值来自病害统计节点。
- 序列：`1、5、15、3、0`。
- 使用 `calculate_series:percentage_change`。
- 相邻变化：`+400%、+200%、-80%、-100%`。
- 零数量批次被保留。

### 4. 健康综合评价

问题：

```text
哪条路的健康状况最好？从哪几个维度判断？
```

结果：

- 使用 `calculate_columns:divide` 计算病害密度。
- 计算操作数来自依赖节点真实产出的 `count` 和 `route_length_km`。
- 使用 `multi_metric_comparison` 对数量、总面积、平均面积、病害长度和密度综合分析。
- 最终结论明确标注为代理指标，不冒充正式业务评分。

## 失败与修复验证

- 两个数字除法支持零分母结构化失败：
  `calculation_zero_denominator`。
- 列计算支持 `skip_rows`，但必须报告被排除的行。
- 计算工具失败进入 ReAct 修复策略，候选工具限定为：
  `calculate_scalar`、`calculate_columns`、`calculate_series`。
- QuerySpec 在执行前校验工具、操作符、命名角色、上游字段和输出契约。
