# 晋级考试第 3 轮

## 结果

- 考试集：`promotion-20261107-012543`
- 随机种子：`20261109`
- 新题：`12/15`
- 旧题：`4/5`
- 总计：`16/20`
- 晋级：是
- 说明：第三轮使用修改后代码生成的全新题，首次运行达到晋级线。

## 题集独立性

- 与第 1、2 轮新题 ID 重合：0。
- 与第 1、2 轮新题题干重合：0。
- 旧题随机抽样：`fresh_task_completion_rate_ranking`、
  `fresh_asset_road_grade_comparison`、`route_metrics`、`holdout_03`、
  `fresh_equipment_mileage_agg`。

## 未通过题目

| 题号 | 来源 | 失败层 | 原因 |
|---|---|---|---|
| fresh_asset_road_grade_trend | 新题 | compiler | 趋势题计划缺少消费数据结果的 post_calc 节点 |
| fresh_task_device_monthly_mileage | 新题 | verification | SUM 结果因设备不存在返回 NULL，被验收器判为不可用数值 |
| fresh_topn_disease_size_filter | 新题 | verification | 要求按长度取前 5 条，SQL 有 LIMIT 5 但缺少 ORDER BY |
| route_metrics | 旧题 | execution | 平均单处面积后置计算遇到 NULL 路线值，计算类型校验失败 |

## 结论

本轮同时满足：

- 新题至少 `12/15`。
- 旧题至少 `4/5`。
- 总计至少 `16/20`。

因此达到晋级线。已知仍有 4 类失败，但本轮没有继续修改代码；后续任何修复都必须
生成新的 15 道题重新考试，不能把当前试卷重跑到 100% 作为晋级依据。
