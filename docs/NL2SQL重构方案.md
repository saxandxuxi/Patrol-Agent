# NL2SQL 重构方案：spec + 编译器（含 Vanna 的定位）

> 背景：靠"每遇到一个问法就加一个模板"已经修了 12 个模板，仍未收敛。
> 结论：问题不在模型，在于**让模型直接写 SQL**、让**正则承担语义理解**、以及**缺少答案自检**。

---

## 一、目标架构

```
自然语言
  ↓ ① 快路径（正则/缓存）：只做加速，命中即用，不做兜底
  ↓ ② LLM 产出【结构化查询意图 spec】(JSON，字段取自闭集)
  ↓ ③ 校验 spec（字段闭集 / 组合合法性 / 条件完整性）——不过就重试一次，再不过就追问
  ↓ ④ 编译器把 spec 编译成 SQL（唯一写 SQL 的地方）
  ↓ ⑤ 安全校验（只读、白名单、del_flag、LIMIT）
  ↓ ⑥ 执行
  ↓ ⑦ 结果自检（形状 / 空值 / 自相矛盾）——不过就如实说，绝不编数字
  ↓ ⑧ 渲染（按数据类型识别标签列与数值列，不依赖列名）
```

职责边界：

| 层 | 谁来做 | 备注 |
|---|---|---|
| 语义理解 | LLM | 只选逻辑字段，不写 SQL 语法 |
| 编译 SQL | 代码 | 一个编译器替代现有 12 个模板 |
| 安全与完整性 | 代码 | 复用现有 `validate_sql` / `validate_semantics` |
| 结果自检 | 代码 | 本轮已实现（见第五节） |
| 答案渲染 | 代码 | 本轮已改成不认列名 |

---

## 二、spec 长什么样

```json
{
  "subject": "disease",
  "filters": [
    {"field": "route",           "op": "=",       "value": "宝安大道西乡段"},
    {"field": "damage_type",     "op": "in",      "value": ["泛油"]},
    {"field": "pile_distance",   "op": "between", "value": [800, 900]},
    {"field": "collection_time", "op": "between", "value": ["2026-07-01", "2026-08-01"]},
    {"field": "area",            "op": "=",       "value": 5000},
    {"field": "confidence",      "op": ">=",      "value": 0.7},
    {"field": "device_code",     "op": "=",       "value": "102367594567"},
    {"field": "has_image",       "op": "=",       "value": true}
  ],
  "metrics": [
    {"kind": "count",          "alias": "count"},
    {"kind": "count_if",       "condition": "collection_time in 6月", "alias": "june_count"},
    {"kind": "sum",            "field": "area", "alias": "total_area"},
    {"kind": "avg",            "field": "area", "alias": "avg_area"},
    {"kind": "max",            "field": "area", "alias": "max_area"},
    {"kind": "count_distinct", "field": "pile_number", "alias": "distinct_pile_count"},
    {"kind": "rank_top",       "field": "area", "alias": "metric"}
  ],
  "group_by": ["route"],
  "order_by": [{"alias": "count", "direction": "desc"}],
  "assumptions": ["前 500 米按路线起点算"]
}
```

要点：**LLM 只填逻辑字段名（闭集），不碰真实列名**。真实列名、JOIN、方言全部由编译器负责。

---

## 三、编译器：一张映射表替代 12 个模板

编译器持有三样东西：

1. **主体 → 主表 + 关联链路**
   `disease → ai_problem p`；`pile_point → basic_facilities_sections_pile_detail d JOIN sections s ON d.sections_id=s.id JOIN routes r ON s.route_id=r.id`；`patrol → ai_maintain_patrol t JOIN ai_problem p ON p.patrol_id=t.id`；`work_order`、`section`、`route_attribute` 同理。
2. **逻辑字段 → 真实列/表达式**
   `route → r.id`（名称先解析成 id）；`damage_type → b.problem_type`（名称先解析成 code，经去重字典 `(SELECT DISTINCT problem_code, problem_type)`）；`confidence → CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position,'$.confidence')) AS DECIMAL(10,6))`；`has_image → p.problem_url <> ''`；`pile_distance`、`area`、`disease_length`、`collection_time`、`device_code` 直接映射。
3. **指标 → 聚合表达式**
   `count/ count_if / sum / avg / max / count_distinct / rank_top(并列用 RANK() OVER)`。

于是：

- 「面积等于 5000 的记录有多少条、分布在哪」= `count` + `group_by: [route]` + `filters: [area=5000]`
- 「6月/7月/7月29日/K1以西各多少」= 4 个 `count_if`，同一条 SQL 四列（**天然不互斥**）
- 「置信度最高的是哪条，并列都列出来」= `rank_top(confidence)`
- 「泛油和识别不出来的各多少」= `count_if(泛油)` + `count_if(未知类型)`

以上现在分别是 5 个模板，重构后是**同一段编译逻辑的 5 组输入**。

校验器（编译前）：
- 字段必须在闭集内；主体与字段必须兼容（例如 `patrol_type` 只对 `patrol` 主体）
- 用户明确说出的条件必须出现在 spec 里（防丢条件，沿用现有 `validate_semantics` 思路）
- 越界即失败 → 重试一次 → 仍失败就**追问**，不再静默降级

---

## 四、Vanna 怎么用上（你问的重点）

**代码编译 SQL 完全可行**，而且比让模型写 SQL 更可靠：模型输出的是受限 JSON，SQL 只是模板化拼接；真正的难点是"让模型输出合法 spec"，这靠 JSON schema + 闭集校验 + 重试解决，不靠祈祷。

在这个前提下，Vanna 有三个可用位置，建议**同时用 2 和 3，按需上 4**：

**① 主链路（默认不走 Vanna）**
spec → 编译器 → SQL。确定、可复现、可自检。

**② 兜底：自由查询模式（保留 Vanna Agent）**
当问题超出 spec 词汇表（例如临时探索性分析、编译器明确拒绝的组合）时，才交给 Vanna 的多轮 Agent。
它产出的 SQL **必须走同一套安全校验 + 结果自检 + 渲染**，并在前端标注「自由生成（Vanna）」，让你一眼知道这条不是确定链路。
现在的 `vanna_engine.generate_and_execute` 基本就是这个形态，接上去成本最低。

**③ 可视化：继续用 Vanna 的绘图能力**
`VisualizeDataTool` / `PlotlyChartGenerator` 与"谁写 SQL"无关。spec 里带 `viz: bar|line|none`，出图仍由 Vanna 的 Plotly 生成器完成。这是你当初想接 Vanna 的初衷，保留。

**④ 进阶（推荐）：把编译器注册成 Vanna 的工具**
Vanna 的 `ToolRegistry.register_local_tool` 允许注册自定义工具。把 `build_query(spec)` 注册进去，让 Vanna 的 agent loop **调用我们的编译器**而不是自己写 SQL：

```
Vanna Agent（负责多轮推理、追问、选口径）
   ├─ tool: build_query(spec)   ← 我们的编译器，产出确定性 SQL
   ├─ tool: run_sql(...)        ← 现有执行器
   └─ tool: visualize_data(...) ← 出图
```

这样既有 agent 的灵活性，又保证 SQL 是机械正确的，还顺手解决了"Vanna 抄错列名"这一类问题（列名解析被编译器接管）。

**对拍口径**：②③ 保持现状即可；④ 属于增强，做不做都不影响主线。

---

## 五、已完成：结果自检 + 渲染去列名（本轮）

- 渲染层改为按数据识别"标签列 / 数值列"，不再写死 `count` / `type` / `area`
  （此前模型把列名写成 `count_per_route`，答案就渲染成空值 + "合计 0 处"，而 SQL 本身是对的）
- 新增 `_result_problems()`：计数/分布/TOP 类问题如果找不到数值列，或分组列出现空值，**不猜、不编数字**，如实说明并在结果工作台保留原始表格与 SQL
- 新增两个回归测试固化这两条（`test_summary_is_column_name_agnostic`、`test_result_self_check_flags_broken_shape`）

---

## 六、迁移步骤与验收

| 步骤 | 内容 | 验收 |
|---|---|---|
| 1 | 结果自检 + 渲染去列名 | ✅ 已上线，回归测试通过 |
| 2 | spec schema + 编译器（覆盖现有 12 个模板的能力）+ 对拍脚本 | 用 `docs/评测问题集与标准答案.md` 逐题对拍：准确率 ≥ 现链路，耗时 ≤ 现链路 |
| 3 | 主链路切到 spec；旧模板降级为快路径缓存（命中即用，仍参与对拍） | 切流后逐题复测，异常可一键回退开关 |
| 4 | Vanna 定位调整：兜底（②）+ 出图（③）；可选工具化（④） | 兜底路径的答案带「自由生成」标注；出图不回退 |

回滚：三步各留开关（`NL2SQL_SPEC_ENABLED`），任何一步异常可单独关掉回到现链路。

**必须一起验收的一条**：所有"丢条件 / 空值 / 自相矛盾"的答案都要被自检拦住——这是这轮暴露出来最贵的一类错误（SQL 对、话说错）。
