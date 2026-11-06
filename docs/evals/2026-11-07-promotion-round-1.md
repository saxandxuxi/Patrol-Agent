# 晋级考试第 1 轮

## 结果

- 考试集：`promotion-20261107-004838`
- 随机种子：`20261107`
- 新题：`9/15`
- 旧题：`5/5`
- 总计：`14/20`
- 晋级：否
- 结论：该套题已作废，不能通过修改后重跑同一套题晋级。

## 失败题目

| 题号 | 失败层 | 主要原因 |
|---|---|---|
| fresh_lane_disease_unsupported | compiler | 车道不支持时，规划值来源先失败，返回了执行失败而不是明确的能力不支持 |
| fresh_time_trend_area | execution | 不存在的路线 `S20外环高速` 抛出 `QueryPlanError`，没有转为结构化不支持回答 |
| fresh_asset_direction_split | execution | 规划模型未返回合法 QuerySpec JSON |
| fresh_disease_hotspot_section | value | 用户要求 1 公里固定分段，计划错误地按原始 `pile_number` 分组 |
| fresh_time_comparison_additions | value | 两次周检出的“新增”被当成两次检出量对比，没有按病害唯一标识做差分 |
| fresh_maintenance_type_distribution | execution | “第二管养片区”被当成路线名称解析，抛出路线不存在异常 |

## 共同根因与修复

1. 路线、片区等范围实体解析失败时，异常没有在单轮查询边界转换成结构化
   `unsupported` 回答。修复后明确返回 `query_scope_not_found` 和具体原因。
2. 固定距离分段没有显式的 1 公里维度，规划器只能退回原始桩号。
   新增 `pile_segment_1000m` 编译能力，并在规划提示词中禁止按原始桩号字符串分组。
3. QuerySpec 规划输出预算偏小，复杂问题可能返回截断 JSON。
   规划调用统一使用至少 4096 token 正文预算。

## 诊断复核

- 不存在的 `S20外环高速` 现在返回：
  `该查询无法执行：没有找到路线“S20外环高速”...`，状态为 `unsupported`。
- “每段长 1 公里”现在生成 `pile_segment_1000m`，SQL 按 1 公里区间
  `GROUP BY` 并正确取 Top5。

## 后续规则

- 本轮代码已发生变化，因此必须生成一套全新的 15 道题重考。
- 不允许把本套题重跑结果作为晋级证据。
