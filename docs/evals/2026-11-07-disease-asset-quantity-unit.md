# 病害数量与规模单元：修复记录与考试结果

## 一、本单元修复（全部为共同契约，不是单题特判）

### 1. 代码指纹排除运行期产物

`backend/app/data/auto_templates.json` 是运行时写出的模板缓存，之前被算进
`code_fingerprint`，导致同一套题跑完就指纹漂移、复跑直接报 `StaleExamError`。

现在 `backend/evals/promotion/runner.py` 显式排除运行期产物：
`auto_templates.json`、`*.log`、`*.cache`。

### 2. 跨节点汇总分母

两个季度节点算占比时，规划器没给 `total` 就直接放弃计算。现在
`app/calculations/executor.py` 的 `calculate_columns` 与 `percentage_share`
在缺 `total` 时，先对明细列求和当分母；求和不了才失败。

测试：`tests/query/test_post_calc_failure.py::test_group_share_computes_total_when_planner_omits_it`。

### 3. 空窗口语义

两次巡查都 0 行时被一律标 `repairable`，依赖步骤随之 `blocked`。现在
`app/query/scope_facts.py` 区分：

- 范围内确实没有记录（INSIDE）：0 条是事实，可加指标给 0，平均/占比仍为空，
  写入 `empty_scope_nodes`，不阻断依赖。
- 超出这份台账的真实覆盖（OUTSIDE）：状态 `unsupported` + `out_of_coverage`，
  回答里写清覆盖区间。

### 4. 考试实测暴露并修掉的系统性漏洞

| 漏洞 | 修复位置 |
| --- | --- |
| 模型自造 `analysis/analysis_only` 等视图名被当成能力缺失 | `shape_contracts.canonical_view` 统一归一化 |
| 分布视图丢分组维度、静默退化成一行总量 | `validator.missing_dimension` 执行前拦截 |
| 规划器看不到 `route_level`/`tech_grade` | `resources/capabilities/fields.yaml`、`enums.yaml`、`assist_rules.yaml` |
| 枚举解析按主体基表找字段，跨表枚举取不到 | `app/query/values.py::_enum_location` |
| 模型把路线名塞进 `luge_name`（实际是里程区段） | `field_overrides.yaml` + `prompts/query_spec.md` 第 31 条 |
| 按枚举分组时缺少的取值不出现 | `service._completed_enum_rows` 只补计数与可加指标 |
| 车道能力不足时只说“没产出视图” | `analysis._unavailable_assist_reason` 给出具体能力缺失原因 |

## 二、第三套考试（资产数量与规模）

- 考试集：`disease-asset-quantity-unit-20261107-03`
- 随机种子：`20261220`
- 旧题：`disease_hengmingzhu_type_change_two_dates`、
  `disease_last_week_top_longitudinal_routes`、`fresh_time_trend_area`、
  `route_top_type`、`fresh_topn_route_area`
- 结果：新题 `13/15`，旧题 `3/5`，总计 `16/20`，**未晋级**

失败的 4 题全部落在同一个共同层：**“计划说的”和“SQL/结果实际做的”不一致**。

| 题 | 失败层 | 根因 |
| --- | --- | --- |
| XCBH202610270002 总面积与平均单处面积 | verification | 声明了 `total_area`/`average_area`，但视图是 `data`，编译器把指标丢了，SQL 只是一段明细 |
| 按公路技术等级分组的病害总面积 | value | 分组里有分组无有效数值（NULL），回复却写“已按 0 计入”，与结果自相矛盾 |
| 上周纵向裂缝最多的路线（旧题） | value | “新增”口径没有实现，系统按存量统计，答的是另一个指标 |
| 每条路线的主要病害类型（旧题） | presentation | 237 行结果只回一句“无法逐条列出”，没有结论 |

## 三、本单元之后立刻修的共同契约

1. **指标必须真的被聚合**：`shape_contracts.canonical_view` 把
   `data/list/detail + metrics` 归一化成聚合视图；`validator` 增加
   `metrics_require_aggregate_view` 执行前拦截；`repair.patch_view_shape`、
   `patch_metric` 走同一套归一化，不再让补丁把非法视图写回计划。
2. **极值/排名必须有排序**：`assist_rules.yaml` 新增 `superlative_hint`
   （trusted=false 候选证据），规划前校验“极值问法 + 无排序字段”直接拒绝并
   写明原因；`prompts/query_spec.md` 第 32 条要求 `top_records`/`top_groups`。
3. **空值不等于 0**：分组结果里出现空值时，回答追加“有个别分组没有有效数值
   （不是 0）”；只有真正补齐的枚举分组才说“已按 0 计入”。
4. **数据题给事实，分析题给结论**：分组结果先给一行确定性的“排序前列”摘要，
   再指路结果工作台；明细结果在 1～3 条时直接把值写进回答，不再只让用户去工作台。
5. **链式依赖带上条件**：依赖上一节点取值的查询，回答会写明“本次结果沿用了
   上一节点筛选出的条件”，结构化字段落在 `inherited_filters`，观察载荷也带上。
6. **答错重规划崩溃**：`review_repair` 把 `field_grounding.ground()` 的命中字典
   当 `FieldReachSet` 用，取 `.hits` 直接崩（`'dict' object has no attribute
   'hits'`）。现在先转换成 `FieldReachSet` 再规划。
7. **拒绝原因说清楚**：不支持视图时回答解释“这是查询计划内部的视图名不合法，
   不代表数据里查不到”，并给出下一步建议。

## 四、指纹稳定性

unit-3 报告记录的代码指纹与跑完后重新计算的指纹一致（运行期产物已排除），
复跑不会再因为模板缓存被判 `StaleExamError`。

## 五、回归

`python -m unittest discover -s tests/query -p "test_*.py"` → `350` 项通过。

## 六、下一步

1. 继续按业务单元考试：病害类型（路段各类病害、上下行主病害、上下行差异、
   车道能力缺失、尺寸阈值）。
2. 代码一旦再改动，当前考试集立即作废，必须重新生成 15 道新题。
