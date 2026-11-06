# 晋级考试第 5 轮

## 结果

- 考试集：`promotion-20261107-015658`
- 随机种子：`20261111`
- 新题：`12/15`
- 旧题：`4/5`
- 总计：`16/20`
- 晋级：是
- 路径：真实 `handle_message` 产品入口，自动确认推荐模板并包含语义疑点验收。

## 题集独立性

- 与前 4 轮新题 ID 重合：0。
- 与前 4 轮新题题干重合：0。
- 旧题抽样：
  `fresh_time_monthly_trend_multi_route`、`fresh_lane_disease_unsupported`、
  `fresh_task_completion_rate_ranking`、`fresh_disease_direction_type_diff`、
  `fresh_time_trend_area`。

## 未通过题目

| 题号 | 来源 | 失败层 | 原因 |
|---|---|---|---|
| new_time_weekly_trend | 新题 | compiler | 周趋势计划缺少消费数据结果的 post_calc 节点 |
| new_task_device_efficiency | 新题 | execution | 后置计算节点为 repairable，无法证明设备效率 |
| new_topn_route_severity | 新题 | compiler | 把“严重程度”误解释为不存在分组字段 damage_type |
| fresh_lane_disease_unsupported | 旧题 | compiler | 车道不支持场景仍以未解析 field value 的规划失败呈现 |

## 当前代码指纹

`content:43f18e013a4d06a5ead11a6505b653080482038eed0ac32934911af9efd1f544`

## 结论

本轮同时满足：

- 新题至少 `12/15`。
- 旧题至少 `4/5`。
- 总计至少 `16/20`。

因此达到晋级线。该结论绑定真实产品路径、随机种子和上述代码内容指纹。后续如修改
业务代码，必须重新生成 15 道新题考试。
