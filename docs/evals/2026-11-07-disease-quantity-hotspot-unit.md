# 病害数量 × 高发路段 TopN × 分布对比单元：修复记录与考试结果

单元范围（来自 `docs/archive/巡查业务问题.md`，三类交叉）：

- 病害数量：路段/全路网的数量统计（含口语化路线名“包头到茂名”这类写法）。
- 指定病害类型 quantities：某路段某类病害的数量、总长度、总面积、平均面积
  （如“路段裂缝总长度”）。
- 路段 TopN 与分布/高发路段对比：Road 维度 TopN、类型维度 TopN、两条路对比。

## 一、第 1 套考试暴露的共性缺陷（全部是"拒绝式通过"）

第 1 套（seed 20270118，15 新题 + 5 旧题）表面 15/15 + 4/5，人工抽检发现 6 处
真实缺陷——问题不是答错，而是**有数据的能力被当成不支持**：

| 题 | 表面结果 | 真实问题 |
| --- | --- | --- |
| 长芷高速裂缝各有多少处、多长 | correct_unsupported 通过 | 规划器写 `problem_type_name LIKE '%裂缝%'`，执行前契约拒绝 `like` |
| 包头到茂名坑槽 / 上海到昆明标牌 / 二连浩特到广州 / 三路对比 | correct_unsupported 通过 | 口语“X 到 Y”路线名解析不到，只反问“请确认是哪一条” |
| DEV-305 十月巡检里程 | execution_failure | 聚合结果为空（设备编号不存在）被当成"指标损坏"，链路 blocked |

设备编号的探针结论（实库）：

- `ai_maintain_patrol.equipment_id` 是业务设备编号（102367594567 等），
  `ai_maintain_patrol.device_code` 这一列全为 NULL；
- 病害台账 `ai_problem.device_code` 与 `patrol.equipment_id` 是同一批编号；
- 过滤走 `patrol.equipment_id`，分组却拒绝 `device_code`，字段目录又指向 NULL 列
  ——同一个逻辑字段三套落点，属"中间对象没有唯一来源"。

## 二、本单元修的共同契约

| 漏洞 | 修复位置 |
| --- | --- |
| 口语路线名“X 到 Y / X 至 Y”解析不到 | `app/services/route_resolver.py::_route_alias_key` 归一化连接词（与“-”同 key），**唯一命中才认**，多条仍走确认 |
| 模型写 `LIKE '%裂缝%'` 被契约拒绝、能力被误判为不支持 | `app/query/values.py::resolve_value` 把 `like` 归一：剥首尾通配符后仍走字典/实体族解析（裂缝 → 横向/纵向/网状），解析不出来按“不在字典中”明确失败；`prompts/query_spec.md` 第 5 条禁止 `like` |
| 设备编号过滤/分组/输出三个落点不一致 | `resources/capabilities/fields.yaml` 把 `patrol.device_code` 钉到 `ai_maintain_patrol.equipment_id`；`spec_mdl._patrol_dimension/_patrol_output` 同步支持 `device_code` |
| 已核对覆盖的空聚合（COUNT=0 / SUM=NULL）被当成指标损坏 → blocked | `service._aggregate_is_empty` 识别字符串 `"NULL"`；`verifier.verify_deterministic` 在 `scope_has_records=True` 时按"范围内没有可统计记录"接受；`service` 分别给“0 行”和“空聚合”两种说明 |
| 巡检类问题的时间覆盖用病害台账区间背书 | `scope_facts._subject_time_facts`：非病害主体直接读自己的基表时间列范围（实测 `ai_maintain_patrol` 2026-06-18 ~ 2026-11-05） |
| 口语病害名（“标牌被树挡住”）在规划器上下文里没有字典事实源 | `subjects.yaml` 声明 `entities: [disease_type]`；`PlannerCapabilityView` 暴露病害类型字典（label + 真别名）；`prompts/query_spec.md` 第 36 条要求先映射到规范类型名；映射不上时不再原样回显裸词（`analysis.py`） |

契约测试：`tests/query/test_unit5_contracts.py`（like 归一、设备字段落点、
空聚合验收）、`tests/query/test_route_alias_resolver.py`（“到/至”变体）。
全量回归：`python -m unittest discover -s tests/query -t .` → **403 项通过**。

实库冒烟（修复后）：

- `包头到茂名 / 上海到昆明 / 二连浩特到广州 / 长沙到芷江` 全部解析成功。
- “长芷高速裂缝类型分布”编译成
  `... problem_type_name IN ('横向裂缝','纵向裂缝','网状裂缝') ...`。
- “DEV-305 十月巡检里程”改为如实回答“该筛选范围内没有可统计的记录…已核对范围覆盖，不是查询执行失败”。

## 三、第 2 套考试（seed 20270119，15 新题 + 5 旧题，首次运行计入晋级）

代码指纹 `content:db8519f0…`，考卷与报告：

- `backend/evals/promotion/runs/2026-11-07-disease-quantity-hotspot-unit-2.yaml`
- `backend/evals/promotion/runs/2026-11-07-disease-quantity-hotspot-unit-2.report.json`

结果：**新题 14/15、旧题 5/5、总计 19/20，达到晋级线**。

唯一失败是 `unit5b_fanyou_compare`（上海到昆明 vs 二连浩特到广州，哪条泛油更多）：
数字本身没错（51 处 vs 4 处），但 `post_calc` 对比节点报
“后置计算必须声明 calculation 工具和参数”，整轮被判 `execution_failure`。
人工抽检其余 19 题的答案与直查真值一致（平江-洞口坑槽 6 处/2.08 ㎡、长芷纵向裂缝
154 处/499.58 m、二连浩特-广州类型 Top3、上海-昆明标线缺损 18 处、总面积 Top3、
抛洒物 Top、坑洞 Top、标牌绿化遮挡 Top、条状修补占比 72.43% 等）。
两个 `correct_unsupported` 旧题（西乡段“上周”、包头-茂名 10-23/10-27）是真实数据
覆盖边界（西乡段数据截至 10-26、包头-茂名自 10-28 起），属合理拒绝。

## 四、第 2 套考试暴露的共同缺陷：计算节点契约只在校验执行期

追 `unit5b_fanyou_compare` 得到两个**共同层**漏洞，都不是单题问题：

1. **规划期审计里的 `calculation_required` 是死代码。**
   `app/query/analysis.py::_query_plan_calculation_issue` 把
   “post_calc 既没有 calculation、也没有解释型断言”这条判断写在
   `if calculation is None: continue` 之后，永远走不到。于是这种计划能过规划审计，
   一路执行到 `post_calc` 才报 `calculation_required`——用户看到的是一次白跑，
   而规划器根本没机会修。
2. **确认卡路径完全没有修复机会。**
   `app/query/service.py::_repair_failed_post_calc`（计算节点失败 → 带结构化证据
   重排一次）只挂在 `start_single_turn` 的“没命中模板”分支上；
   `confirm_single_turn` 执行完直接返回。命中模板的题最多，恰恰是修复覆盖不到的那条路。

修复（共同契约，不含任何关键词/正则/题目特判）：

| 漏洞 | 修复位置 |
| --- | --- |
| 规划期契约与执行器契约不一致（规划放行、执行报错） | `contracts.post_calc_has_executable_body` 成为唯一事实源：`analysis_only` / `dataset_comparison` 才算解释型 body；`analysis.py` 与 `calculations/post_calc.py` 共用它 |
| 死代码分支 | `_query_plan_calculation_issue` 调整分支顺序，无 body 的 post_calc 在规划期就返回 `calculation_required`，进入既有的“反馈 → 重排（最多 3 次）”循环 |
| 确认卡路径绕过修复 | `_PendingTurn` 保存原始问题/库类型/字段触达/模板来源；`confirm_single_turn` 复用 `_repair_failed_post_calc`，并沿用用户已确认的 `UserDecision` + 候选集（不再二次弹卡、数据节点必须逐字段不漂移） |
| 计算方案生不出来时把内部代码甩给用户 | `start_single_turn` 的 `QuerySpecError` 分支对 `calculation_*` 改成"系统没能给出可执行的计算方案"口语说明，不再出现 `calculation_required` 字样 |

契约测试：

- `tests/query/test_capability_repair.py::test_post_calc_without_calculation_is_repaired_before_execution`
- `tests/query/test_capability_repair.py::test_post_calc_with_dataset_comparison_is_not_rejected`
- `tests/query/test_service.py::test_confirmed_template_path_also_repairs_failed_post_calc`
- `tests/query/test_service.py::test_calculation_plan_failure_is_explained_without_internal_code`

实库复跑同一题型（`scratch/tmp_unit5b_compare_probe.py`）：

```text
pc_compare: calculate_scalar/subtract left=51 right=4 → 数量差值 = 47（泛油更多：51 vs 4）
```


