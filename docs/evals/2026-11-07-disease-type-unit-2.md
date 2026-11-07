# 病害类型单元（第二套）：修复记录与考试结果

## 一、用户裁决指出的六个问题

| 现象 | 根因 | 修复 |
| --- | --- | --- |
| 两次问“全库面积最大的病害在哪条路”答案不一致（一次怀化绕城 5881，一次宝安恒明珠 0.46） | 极值问法被规划成不带 `ORDER BY` 的明细查询，`LIMIT` 后返回的是任意一行 | 执行前契约：极值问法 + 明细视图 + 无排序 → 拒绝并反馈给规划器重排（`analysis._spec_extreme_order_issue`） |
| 点“答错了”后端报 `'dict' object has no attribute 'hits'` | `field_grounding.ground()` 返回命中字典，被当成 `FieldReachSet` 直接取 `.hits` | `review_repair._field_reach_from_ground` 做类型转换，并补回归测试 |
| 链式依赖的结果丢条件（只给桩号分段，不说是哪条路/哪种病害） | 依赖查询把上游结果变成过滤条件后，回答里看不到这些条件 | `service._inherited_note`：明细/分组回答都写明“本次结果沿用了上一节点筛选出的条件” |
| “无法可靠规划该查询：不支持的查询视图：analysis” | 模型自造视图名被当成硬失败 | `shape_contracts.canonical_view` 归一化 + `service` 区分“计划内部视图名不合法”和“数据里查不到” |
| 分析类问题回答把明细行原样倒进正文 | 分析模式没有结论节点时，回退用了节点级明细文本 | `service.assemble_answer`：数据节点一律走数据回答规则（摘要 + 指路结果工作台） |
| 依赖值直接把中文路线名当成可执行值 | 上游结果是展示视图（路线中文名、枚举中文标签），过滤条件要的是存储值（路线 id、枚举 code） | `service._materialize_inputs` 对 `route_name` 和已登记枚举字段先过字典解析，解析不了就明确失败 |

## 二、依赖传值契约（本单元核心）

一个字段在不同阶段有不同视图，不能互相替代：

- 用户输入阶段：`values.resolve_value` 把“长沙－芷江高速公路”解析成路线存储值。
- 上游结果阶段：结果行里的值是**展示值**（路线中文名、`上行/下行` 标签）。
- 过滤执行阶段：SQL 要的是**存储值**（`disease.route_name` 的中文名，或 `up_or_down` 的 `1/2`）。

修复前，依赖传值跳过了解析这一步：

- 路线：生成 `route.id = '长沙－芷江高速公路'`，永远 0 行。
- 上下行：生成 `disease.up_or_down = '上行'`，而库里存的是 `1/2`，同样 0 行。

修复后保留展示视图的过滤方式（`disease.route_name IN (...)`）或解析回存储 code
（`disease.up_or_down IN ('1')`）；解析不了的值（例如分组里的“未知路线”）直接
返回结构化失败原因，不生成必然为 0 的 SQL。

契约测试：

- `tests/query/test_service.py::test_route_name_dependency_keeps_display_view_filter`
- `tests/query/test_service.py::test_enum_dependency_resolves_label_to_storage_code`
- `tests/query/test_service.py::test_unresolvable_route_dependency_stops_with_reason`
- `tests/query/test_review_repair.py::test_grounding_dict_becomes_field_reach_set`

## 三、实库冒烟验证

| 问题 | 修复前 | 修复后 |
| --- | --- | --- |
| 全库面积最大的病害在哪条路上？ | 无排序，返回任意一条（宝安恒明珠 0.46） | `ORDER BY area DESC LIMIT 1` → 怀化市绕城高速公路 5881.00（声屏障缺损，k22+520） |
| AI 认不出来的病害主要集中在哪条路上？有多少处？ | q2 生成 `route.id = '长沙－芷江高速公路'`，返回 0 | q2 生成 `disease.route_name IN ('长沙－芷江高速公路')`，返回 7631 |

## 四、考试结果

### 第二套（disease-type-unit-20261107-02）

- 种子：`20270106`；旧题：`disease_changzhi_two_types_two_dates`、
  `new_time_inter_task_gap`、`type_changzhi_last_month_types`、
  `changsha_zhijiang_trend`、`holdout_19`
- 结果：新题 `14/15`，旧题 `3/5`，总计 `17/20`，**未晋级**

三道失败题都是共同层，不是单题：

| 题 | 失败层 | 根因 | 已修 |
| --- | --- | --- | --- |
| 坑槽的平均长度是多少米？最长的一处有多长？（新题） | execution | `avg_disease_length`/`max_disease_length` 由物理列自动生成，取的是原始 `disease_length`（坑槽 44 条全为空），SQL 算出 NULL；语义字段 `effective_disease_length` 被绕过 | `resources/capabilities/metrics.yaml` 显式登记三个长度指标走语义字段；验证器把“指标全空”的原因写清楚 |
| 长沙－芷江高速10月27日和10月31日两次检出中，坑槽和泛油分别变化多少？（旧题） | execution | 计算节点用 `calculate_columns` 对接两个多行来源，失败后没有任何修复闭环，直接把错误文本当答案 | `service._repair_failed_post_calc`：把结构化失败反馈给规划器重排一次，数据节点范围哈希必须逐字段一致，重排后仍有失败就保留原回答 |
| 长沙－芷江高速公路病害数量，最近是在增加还是在减少？（旧题） | value | 窗口 2026-10-08 ~ 2026-11-07 按月分桶，首尾月天数不同（23 天 vs 7 天），却直接得“下降 84%”的结论 | `time_coverage._partial_window_note`：首尾时间桶不完整时明确说明“不能直接比较大小” |

新增契约测试：

- `tests/query/test_metric_registry.py::test_length_metrics_use_semantic_length_field`
- `tests/query/test_time_coverage.py::test_partial_month_trend_window_is_flagged`
- `tests/query/test_service.py::test_failed_post_calc_is_replanned_once`
- `tests/query/test_service.py::test_post_calc_repair_keeps_original_when_data_scope_drifts`
- `tests/query/test_service.py::test_post_calc_repair_keeps_original_when_repaired_answer_still_fails`
- `tests/query/test_service.py::test_successful_answer_never_triggers_replan`

### 第三套（disease-type-unit-20261107-03）

代码已修改，第二套立即作废，按规则重新生成 15 道新题（种子 `20270107`）。

- 种子：`20270107`；旧题：`oil_severity_route`、`time_day_patrol_tasks_october`、
  `fresh_task_device_recent_detail`、`type_changzhi_last_month_types`、
  `time_week_range_1023_1031`
- 结果：新题 `13/15`，旧题 `5/5`，总计 `18/20`，**达到晋级线**
  （新题 ≥12、旧题 ≥4、总计 ≥16，首次运行）

仍然失败的两题属于下一轮的共同层，不是单题特判：

| 题 | 失败层 | 根因 | 下一步 |
| --- | --- | --- | --- |
| 全路网上行和下行各检出多少种病害类型？（新题） | value | 「多少**种**」要的是去重计数（`COUNT(DISTINCT problem_type_name)`），能力注册表里没有去重计数指标，模型只能拿 `COUNT(*)` 顶上，把 21413 处病害说成 21413 种类型 | 在 `metrics.yaml` 登记去重计数指标，规划器只能选注册指标 |
| 许昌-广州这条路上检出的病害类型有哪些？（新题） | verification | 该路线只检出 1 类病害，回答没说明“这就是全部”，判分认为无法排除漏报 | 分组结果行数很少时，回答要明确“共 N 类，已全部列出” |
