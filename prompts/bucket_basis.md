<!--
用途：判断"这句按什么依据切分"——给 pile_bucket 做语义复核
调用：nl2sql._bucket_basis_dimension() → chat_json()
模型：role_model("semantic")
占位符：<<question>> 本轮完整问题（问题优化器补全之后）
说明：模型看到"段 / 哪一段"容易一律判成桩号分段，但"把一个月分成上、中、下三段"
      "每三个月一段"的切分依据其实是**时间刻度**（提示词写了两轮仍会被这个字带跑）。
      只在计划里**已经填了 pile_bucket** 时才跑这一次，判错才改 group_by。
-->
[system]
用户想把查询结果切成几档（"哪一段 / 哪一档最多"这类）。你只判断他**按什么依据**切。只输出 JSON。

依据只有两类：
- `pile`：**里程 / 桩号 / 空间位置**——"按桩号段""按桩号区间""每 500 米一段""这条路的哪一段病害最多"
  "集中在路的哪一段"
- `time`：**时间刻度**——上/中/下旬、上半月/下半月、月份、季度、半年、年、周、星期几、
  工作日/周末、小时/时段、每 N 天（每两周、每三个月…）

依据是 `time` 时，还要给出最贴近的分组维度名（只能取下面之一）：
ten_day / half_month / month / quarter / half_year / year / week / day / dow / workday /
hour / time_of_day / minute / every_n_days / every_n_hours / every_n_minutes
（"每两周"是 every_n_days，两周=14 天；"每三个月"是 quarter 或 every_n_days，取更贴近语义的那个）

只返回 JSON，不要解释：`{"basis":"pile|time","dim":"<时间维度名或 null>"}`

[user]
<<question>>
