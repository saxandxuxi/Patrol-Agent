# 病害角度时间维度单元考试

## 最终有效结果

- 考试集：`disease-time-unit-20261107-04`
- 代码指纹：
  `content:7fd598b125d43671c74ab411c1273be8da248de7d350e9ac58cef3f21c370d61`
- 新题：`14/15`
- 旧题：`5/5`
- 总计：`19/20`
- 晋级：是（新题 ≥ 12/15，旧题 ≥ 4/5）
- 运行入口：真实 `agent.handle_message` 产品链路。
- 该指纹下第一次运行，运行期间没有修改业务代码。

题集与报告：

- `backend/evals/promotion/runs/2026-11-07-disease-time-unit-4.yaml`
- `backend/evals/promotion/runs/2026-11-07-disease-time-unit-4.report.json`

## 覆盖范围

本轮只考“病害角度的时间维度”，四类必考形状都覆盖到：

- 本月/本季度/第 N 季度等自然周期内的某种病害检出数量。
- 某种病害在最近 N 次巡查之间的数量变化。
- 某一路段上指定两个日期（两个日期或一个连续时间段）的病害对比。
- 某时间段内某种病害在不同道路上的新增与分布。

粒度覆盖：日、周、月、季度、年；相对窗口（最近三个月、最近 7 天、上周）与绝对
周期（10 月 1 日到 10 月 31 日、第三季度）都有题目。

## 诊断轮次

### 第一套 `disease-time-unit-20261107-01`

`11/15 + 5/5`，未晋级。四道失败题的共同根因是计算前后的数据流契约：

- 最近批次序列：标签节点与数值节点关联字段解析不到，标签全空、数值被静默补 0。
- 两个明确日期：被压成一个连续区间；同一句话里的“时间段”和“两次独立日期”没区分。
- 分组占比：分子与总量来自同一列时被当成“重复操作数”拒绝。
- 序列百分比的零基线：整题失败，而不是标出该点不可定义。

### 第二套 `disease-time-unit-20261107-02`

修完上述契约后 `13/15 + 4/5`，晋级。但运行之后又修了“列计算来源判定”，按考试
原则这一套作废，只能当诊断轮。

### 第三套 `disease-time-unit-20261107-03`

`13/15 + 5/5`，晋级。运行后修了日粒度用词规则，同样按原则作废。

### 第四套 `disease-time-unit-20261107-04`

最终有效证据，`14/15 + 5/5`。

## 本轮共同修复

1. 序列计算字段解析：`calculate_series` 的 labels/values 支持“节点限定名、别名、
   实际结果列名”三类写法，按规范化词元匹配。
   见 `backend/app/calculations/executor.py`（`field_label`）。
2. 标签字段解析不到结果列时，直接给出结构化失败（附 `available_columns`），
   不允许再把空标签静默补成 0。
   测试：`test_series_rejects_labels_that_miss_result_columns`。
3. 结果列映射：排序字段不再占用“逻辑字段”位置，列表查询的位置兜底映射恢复。
   见 `backend/app/query/result_projection.py`。
   测试：`test_order_by_field_does_not_break_positional_mapping`。
4. 序列百分比的零基线：该点输出 `null` 并在回答里说明“上一批次为 0，无法用百分比
   表示”，保留原始序列与差值，不再让整条依赖链失败。
   见 `backend/app/calculations/series.py`、`backend/app/calculations/domain/trend.py`。
5. 字符串相等不等于语义相等：新增 `date_range` 规则，“A 到 B”是整段，
   “A 和 B”才是两次独立对照；只有后者才要求拆成单日节点。
   见 `resources/capabilities/assist_rules.yaml`、`backend/app/query/analysis.py`。
6. 列计算来源判定按“声明视图”而不是行数：1 行的分布节点不再被误判成常量来源；
   两个多行来源会给出可执行的修复提示（改用 `calculate_series`）。
   见 `backend/app/calculations/executor.py`、`backend/app/query/service.py`
   （观察载荷补充 `view`）。
7. 自然周期不被截到今天：`本季度/今年/第 N 季度` 这类写法按完整日历区间执行，
   并保证不会顺手放大“上季度”节点的范围。
   见 `backend/app/query/periods.py`、`resources/capabilities/assist_rules.yaml`
   （`named_period`）。
   测试：`backend/tests/query/test_named_periods.py`。
8. 日粒度用词覆盖：“每一天/各天/逐天”也会要求落到日粒度字段。
   见 `resources/capabilities/assist_rules.yaml`。

回归：`python -m unittest discover -s tests/query -p "test_*.py"` → `349` 项通过。

## 指纹稳定性问题（下一轮第一件事）

第四套跑完之后重新计算指纹，发现它和报告里记录的不一致：

- 报告记录：`content:7fd598b125d43671c74ab411c1273be8da248de7d350e9ac58cef3f21c370d61`
- 跑完之后：`content:f5e0bc39b8b70f6f5b9191...`

差异来自运行期写入的 `backend/app/data/auto_templates.json`（`app/services/templatizer.py`
学到的模板缓存），它在 `backend/app` 目录里，所以被算进了“代码指纹”。
这不是人改了代码，但会让同一套题在复跑时被判成 `StaleExamError`。

下一轮在生成新题集之前，先把这种运行期产物从 `code_fingerprint` 里排除，
再开始考试；不要事后改考试工具来“保住”已经跑完的成绩。

## 当前边界（下一轮要处理）

- **跨节点汇总分母**：`第三季度和第四季度到现在，病害类型构成有什么变化？` 一类题目，
  后置计算需要先对两个季度节点的明细求和再算占比，目前直接放弃（回答“缺少计算分母”）。
  裁判认为 SQL 已经返回明细，求和是本系统该做的。
- **空窗口语义**：`平江－洞口高速公路10月26日和11月3日这两次巡查，病害总面积差多少？`
  两个节点都返回 0 行，被标成 `repairable`，依赖它的计算节点随之 `blocked`。
  需要区分“范围内确实没有记录（0 处，可以对比）”与“字段/连接写错导致查不到”。
- **缺时间桶的表达**：数据只覆盖月末几天时，日粒度问题会被标成 `unsupported`。
  数值本身是对的，但状态标签会让人误以为“查不出来”。
- **分布类问题的最终表达**：`第四季度到现在，各条路的病害总数` 这类题，答案只写
  “共 15 行，请在结果工作台查看”，没有给出任何数字或“哪条路最多”。裁判放过了，
  但用户明确要过“数据题指工作台、分析题给结论”的体验，这条要单独修。
- 管养维度按用户要求继续不纳入本单元。

## 复跑方式

```powershell
cd backend
$env:PYTHONIOENCODING='utf-8'
.\.venv\Scripts\python.exe -m evals.promotion run `
  --cases evals/promotion/runs/2026-11-07-disease-time-unit-4.yaml `
  --report evals/promotion/runs/2026-11-07-disease-time-unit-4.report.json
```
