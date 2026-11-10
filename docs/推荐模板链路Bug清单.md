# 推荐模板链路 Bug 清单（2026-09-22 实测）

范围：**自动沉淀模板（L1）从挖掘 → 匹配推荐 → 确认卡绑定执行**这条链路，外加作为候选来源的
L2 示例直达（相似已验证问法）在推荐卡上的执行环节。

审查基线（哈希为写文档时的文件指纹，注意 `templatizer.py` / `capabilities.py` 正被另一个
工作流并发修改，行号可能漂移）：

| 文件 | sha256 前 16 位 | 时间 |
|---|---|---|
| `backend/app/services/templatizer.py` | `C90B0D199CDCF210` → `388393F3CA5F1B5D` | 15:47 → 15:58（本次修改后） |
| `backend/app/services/capabilities.py` | `9BC301406800C784` | 2026-09-22 15:40 |
| `backend/app/services/nl2sql.py` | `C81EE66348569959` | 2026-09-22 15:44 |
| `backend/app/data/auto_templates.json` | `6635439FF40D7AA4` → `B2BCEBC02CE6E37A` | 15:36 → 15:48（并发重挖） |

所有结论都用当前代码/当前库里数据跑过，证据见每条的「实测」段。文末附一次跑完的复现脚本。

## 本次修改（2026-09-22 15:58，只动 `backend/app/services/templatizer.py`）

目标：**匹配判定不再靠措辞/关键词命中，改成结构 + 语义槽一致**，并把几处"看起来命中、
其实对不上口径"的漏判堵掉（对应下面 P0-1 / P1-5 / P1-6）。改动 5 处：

1. 新增条件键规范化（`condition_key` / `plan_condition_value` / `plan_condition_keys` /
   `condition_agrees`）：挖掘侧与计划侧的同一个条件先翻成同一套规范名，再做集合判定
   （`num_area` ↔ `area`、`route_id` ↔ `route`、`problem_url` ↔ `has_image`…）。
2. `match()` 的条件覆盖改成 `plan_condition_keys(plan) ⊆ 模板声明键`，删掉原来
   "required 与 semantic_fields 分别比、两边键名各写一套"的写法。
3. `match()` 的 `fixed` 冲突判定改为**直接对计划取值**：模板写死的业务条件必须和问题给的一致，
   问题没提这个条件 → 不命中（绝不替用户加条件）。以前这段遍历 `params`，
   而被降级的槽不在 `params` 里，等于不校验。
4. `_ast_profile()` 的 shape/维度词表对齐语义层：时间维度分组 → `["trend","distribution"]`、
   分档 CASE 分组 → `bucket_compare`、不分组 `ORDER BY+LIMIT` → `top` 并按结构推断 `max_/min_` 指标、
   日/周/时粒度按 SQL 函数识别（`day`/`week`/`hour`）。
5. `bind()` 的占位符形态与挖掘侧 `_fill_fixed()` 对齐（字符串类走 `'…'`，数值/名次类走裸占位）；
   顺带修掉 `_plan_param_value()` 里 `str(...).get(...)` 的必崩写法（只要有阈值槽，
   异常会被 `capabilities` 吞掉 → **整类自动模板候选消失**）。

验证（43 项断言全过，临时脚本已删）：

- 自画像：按月/按天 → `trend`+`distribution`；按类型 → `distribution`；分组 TopN / 不分组极值 → `top`；
  分档 → `bucket_compare`；普通计数 → `count`。
- 线上 9 条 mature 模板**自匹配全部仍然命中**（没有把合法模板判掉）。
- 写死 `up_or_down='1'` 的模板：问题=上行 → 命中；问题=下行 → **不命中**；问题没提方向 → **不命中**。
- `num_area` 阈值模板：问题带 `area` 条件 → 命中且卡片 SQL 拼成 `p.area > 1000`；
  问题没带阈值 → 按 P0-3 仍作为"待补"候选出现，但**不预先拼 SQL**。
- `ORDER BY … LIMIT 5` 的 TopN 模板现在能 `bind`、能过 `_merge_cluster`、能过 `_verify` 回环验证
  （即下一次重挖会真的产出 TopN 模板）。

仍未处理（见下文对应条目）：P0-2 / P0-3（示例库归一化）、P0-4（CTE 判定）、P1-7 / P1-8 / P1-9、
P2-10 / P2-11。

## 〇、模板库现状（为什么值得查这些）

- 注册表在审查期间被另一个工作流并发重挖过一次（15:36 的 5 条 → 15:48 的 10 条），
  但槽位仍然只有 `route_id / damage_type / time_start / time_end` 四种，
  **没有一条带 TopN、数值阈值、枚举（上下行/有无图）、状态类槽**——与下面 P1-5/P1-9 吻合。
- 本地示例池 61 条 trusted，其中 **28 条以 `WITH` 开头（Wren 产出的物理 SQL 带 CTE）**、
  **29 条是 `ORDER BY + LIMIT` 形态**、4 条带 `BETWEEN`、2 条带 `LIKE`。
- 重挖审计（`auto_templates.json` 的 `rejects`）从 8 条涨到 39 条，分类是：
  **`表不在白名单` 28 条**（对应下面 P0-4 的 CTE 问题，占 72%）、
  `有不可泛化的条件` 8 条（`BETWEEN` 3、`LIKE` 2、其余 3）、`没有可替换的槽位` 3 条。

也就是说：示例池里最主流的两种形态（CTE、TopN）**一种挖不出来、一种绑不出来**。

---

## P0-1　枚举槽被降级成固定值后，冲突检查失效 → 推荐卡静默答错（✅ 已修判定，`promote` 待改）

**现象**：模板里"上下行/有无图片/状态/来源"这类枚举槽，只要在样本里只出现过一次，就会被
`_merge_cluster` 从 `params` 移到 `match.fixed`（值直接焊进 SQL）。之后模板照样是 `mature`、
照样被推荐，但**用户问的枚举值和模板里焊死的值不一致时没有任何拦截**。

**实测**（复现脚本 V3b）：模板

```sql
SELECT COUNT(*) AS c FROM ai_problem p
WHERE p.del_flag = 0 AND p.routes_id = '{route_id}' AND p.up_or_down = '1'
```

`params = ['route_id']`、`match.fixed = {'up_or_down': '1'}`、`status = mature`。
问「恒明珠段**下行**有多少处」：`match()` 返回 1 条候选，`bind(..., route_id='R2')` 执行出的 SQL 是

```sql
... AND p.routes_id = 'R2' AND p.up_or_down = '1'   -- 把"下行"答成"上行"
```

**定位**：`templatizer.match()` 的 fixed 冲突循环是
`for condition, want in fixed.items(): for name, param_spec in params.items(): if param_spec.get("declare") != condition: continue`
——被降级的槽恰恰**不在 `params` 里**，循环永远进不到它，等于没有校验。
`promote()`（`templatizer.py:119-138`）只改 `status`，也没把该槽恢复成可替换参数，
和 `docs/复杂模板匹配与推荐改造方案.md` P2-9「管理员确认这个条件本来就该换值后转正」的语义相反。

**已修**：fixed 校验改成直接对 plan 取值（`plan_condition_value` + `condition_agrees`），
问题没提该条件也判不命中。**未修**：`promote()` 仍只改 `status`，没把该槽恢复成带 `options` 的
param——现在转正后的模板只会在"问题明确给出同一条件"时命中，不会再答错，但也复用不了新值。

---

## P0-2　示例库里多值路线没编号 → 对比题两条路线塌成一条

**实测**（复现脚本 V8，直接读线上 RAGFlow 示例库）：

```
问题: 宝安大道西乡段和宝安中心--恒明珠，在病害总数、泛油数量、平均面积三个维度上分别是多少？
归一化 SQL 的 IN 片段 : IN ({route_name}, {route_name})
spec.routes           : ['宝安中心--恒明珠', '宝安大道西乡段']
回填后 IN 片段        : IN ('宝安中心--恒明珠', '宝安中心--恒明珠')
```

两条路线被填成同一条 → 用户点这条口径，得到的是"同一条路比两次"的结果。

**定位**：写入侧 `nl2sql._normalize_sql_for_example` 已经有编号规则（`{route_id}` / `{route_id_2}`），
但**存量文档没被重写**；回填侧 `nl2sql.fill_example_slots` 对同名占位符是 `str.replace`，
两处一定填同一个值。同一现象在示例库里还有 `pair-708974e7f940b5aa.md`（`{damage_type}` × 2）、
`pair-1978f491e80b3b1e.md` 等，粗查 12 份文档存在同名占位重复。

**建议**：对存量跑一次编号化重写；回填时若 SQL 里同一占位名出现多次且 spec 里对应多值，
直接判"不可重放"（宁可不出这条候选，也不要给一条答案错的）。

---

## P0-3　桩号归一化用裸字符串替换，把示例 SQL 里的数字 0 全换掉了

**实测**（复现脚本 V9，线上文档 `pair-381957f5285a5fec.md`，问题「朝朝线 K0+000-K1+000 有多少处横向裂缝」）：

```
CAST(... AS DECIMAL(1{pile_start}, 6))                  ← 原本是 DECIMAL(10,6)
'1{pile_start}'                                        ← 原本是 '10'
... * 1{pile_start}{pile_start}{pile_start} + ...       ← 原本是 * 1000
1{pile_start}{pile_start}{pile_start}.{pile_start} AS route_length_km  ← 原本是 1000.0
```

全库这类"占位符被引号/数字包住"的命中 **63 处**。

**定位**：`nl2sql.py:5204-5205`

```python
normalized = normalized.replace(str(int(pile[0])), "{pile_start}")
normalized = normalized.replace(str(int(pile[1])), "{pile_end}")
```

`pile[0] == 0` 时等于把整条 SQL 里所有字符 `0` 都换成占位符。

**影响**：用同一条示例换桩号区间复用（`pile_start=1000`）会把 `1000` 拼成
`1100010001000` → 报错或算出荒谬值；`DECIMAL(10,6)`、`1000.0` 这些无关常量也被牵连。

**建议**：只替换桩号谓词里的完整字面量（走 AST / `pile_distance BETWEEN a AND b` 的位置），
不要对整段 SQL 做裸文本替换。

---

## P0-4　CTE 名被当成物理表 → Wren 产出的成功 SQL 全部挖不成模板

**实测**（库内「西乡段泛油病害有多少处」那条 trusted 示例）：

```
原 SQL 开头            : WITH disease AS (SELECT `wren_src_disease`.routes_id AS routes_id, ...
references_physical_table: True
physical_sql_of 是否原样 : True        ← 正确的：这条 SQL 本来就能直接跑
mine_sql 结果            : 拒绝：表不在白名单：disease
```

注册表 `rejects` 里 4 条同类拒绝（`表不在白名单：disease`、`pile_point`）就是这么来的。

**定位**：`templatizer._precheck`（`templatizer.py:777-784`）遍历 `tree.find_all(exp.Table)`，
把 `WITH disease AS (...)` 的 **CTE 名**（以及派生表别名）也当成了物理表做白名单校验。
Wren 产出的物理 SQL 一律形如 `WITH <subject> AS (...)`，所以这一类**永远进不了模板库**，
「approve 即模板化」对 Wren 路径等同失效。

**建议**：白名单校验前先收集 CTE 名集合（`tree.args["with"].expressions` 的 alias）并跳过；
`wren_src_*` 只作为派生表别名出现时也不该拒（那段逻辑对真·MDL-SQL 仍然要保留）。

---

## P1-5　TopN 模板绑定必失败 → 排名类模板永远入不了库（✅ 已修）

**实测**（复现脚本 V1）：

```
挖出的骨架: ... ORDER BY COUNT(*) DESC LIMIT {top_n}      ← 占位符是裸的
params    : {'time_start': 'time', 'top_n': 'int'}
bind(样本值) -> 失败（模板还有未绑定的参数）
```

`_verify` 的回环验证第一步就是 `bind`，所以这类模板在挖掘阶段就被判"回填失败"丢掉——
库里 29/61 条 `ORDER BY + LIMIT` 示例一条都没沉淀成模板，与此吻合。

**定位**：`templatizer.bind`（`templatizer.py:1710` / `1720`）里 `time` 走裸占位替换，
`int`（`top_n`）却落到最后一行的**带引号替换** `sql.replace("'{name}'", ...)`，
而 `LIMIT {top_n}` 没有引号；`_fill_fixed`（`templatizer.py:960` 附近）用的是裸替换，
两边规则不一致。

**已修**：`bind()` 与挖掘侧 `_fill_fixed()` 用同一规则（字符串类 `'…'`、数值/名次类裸占位）；
实测 TopN 模板现在能 `bind`、能过 `_merge_cluster` 与 `_verify`。顺带修掉
`_plan_param_value()` 里 `str(...).get(...)` 必崩的写法——只要有阈值槽，
`match()` 会抛异常并被 `capabilities._auto_candidates` 吞掉，**整类自动模板候选都会消失**。

---

## P1-6　模板自画像的 shape 词表与语义层对不上（✅ 已修词表，bucket 还需看计划侧字段）

**实测**（复现脚本 V2，`_ast_profile` 直接跑）：

| SQL 形态 | `_ast_profile` 判出 | 语义层（`prompts/query_semantic.md:20-27`）会判 |
|---|---|---|
| 不分组取极值（`ORDER BY area DESC LIMIT 1`） | `list` | `top` |
| 按月趋势（`GROUP BY DATE_FORMAT(collection_time,'%Y-%m')`） | `distribution` | `trend` |
| 分档对比（`GROUP BY CASE WHEN …`） | `count` | `bucket_compare` |
| 分组 TopN（`GROUP BY + ORDER BY + LIMIT 5`） | `top` | `top` ✅ |

`match()` 有 `if spec.get("shapes") and shape not in list(spec["shapes"]): continue`，
所以前三种模板**永远匹配不上**对应形状的问题；反过来 `trend`/`bucket_compare` 的问题会拿到
`distribution` 的模板。

**定位**：`templatizer._ast_profile`（`templatizer.py` 约 1020-1120）自造了一套 shape 词表，
和提示词/计划的词表没对齐。

**建议**：以 `prompts/query_semantic.md` 的词表为准（含 `trend`、非分组 `top`、
`bucket_compare`、`photo`），或把两边收敛到同一个枚举常量。

---

## P1-7　人工"转正"会被下一次重挖抹掉

**实测**（复现脚本 V3）：

```
重挖前后的 status: mature -> candidate
promoted_at      : 2026-09-22T11:00:00 -> None
```

**定位**：`_merge_cluster`（`templatizer.py` 约 1300-1345）重建记录时只继承
`title / question / created_at / enabled / usage`，`status` 与 `promoted_at` 一律按规则重算。

**建议**：继承 `promoted_at`（或"人工转正过的记录不再自动降级"），并在维护页如实显示。

---

## P1-8　挖掘开关关闭时，管理页提示 undefined；approve 后的异步模板化空转

**实测**（复现脚本 V4，把开关临时置 False）：

```
mine_all() -> {'skipped': 'auto_template_mine_enabled=false'}
前端提示   -> 重挖完成：可模板化 None/None 条，入库 None 条（可推荐 None）
```

**定位**：`templatizer.mine_all`（`templatizer.py:1397-1401`）早退时只返回 `{"skipped": ...}`，
而 `frontend/src/components/AdminPanel.vue:270-281` 直接拼 `stats.mined / stats.pairs /
stats.templates / stats.mature`。另外 `chat.py` 的 `_templatize_in_background()` 无条件调
`mine_all()`，而 `chat_settings` 里 `auto_template_mine_enabled` **默认 False**（当前这台机器
被手动打开过），所以"approve 即模板化"（方案 §2.5.2）在默认配置下其实是空转；
`mine_all(force=...)` 这个形参既没被任何调用方使用、函数体里也没读。

**建议**：早退也回全字段（各计数给 0）并在前端提示"挖掘开关未开"；`approve` 后的补挖走
`force=True` 或者显式提示"需要先在设置里打开自动挖掘"。

---

## P1-9　一个谓词挖不动 → 整条成功 SQL 不沉淀

**实测**（注册表 `rejects`，2026-09-22T15:36 那次重挖的记录）：

| 拒绝原因 | 出处问题 |
|---|---|
| `有不可泛化的条件：r.id IN ('d74bf5e5…', '1b0b4a9f…')` | 两条路线对比 |
| `有不可泛化的条件：p.pile_distance BETWEEN 1802 AND 2801` | `k1+802 附近还有…` |
| `有不可泛化的条件：t.rnk = 1` | 置信度最高的病害是哪个 |
| `有不可泛化的条件：r.full_name LIKE '%恒明珠%'` | 恒明珠泛油面积对比 |

**定位**：`_mine_predicate` 对「列不在字段目录的 IN」「`BETWEEN`（op 为空）」「列不是裸列且
不是登记表达式」一律返回 `None`，`mine_sql` 收到 `None` 就
`return None, f"有不可泛化的条件：…"` **整条放弃**——`docs/复杂模板匹配与推荐改造方案.md`
P1-4 要的"单谓词降级、其余照挖"只对 CAST/JSON 那一类生效，这几类仍是整条连坐。

**建议**：`BETWEEN` 挖成数值区间槽（桩号区间场景本来就要它）；目录外的列（主键、窗口函数别名）
按 fixed 保留；`LIKE '%…%'` 若是路线口径，应归一到路线槽而不是整条拒。

---

## P2-10　确认卡里数值阈值槽改不了；步骤卡根本不显示槽位

- `frontend/src/components/ChatPanel.vue:254-263`：所有可编辑槽都渲染成 `el-select`，
  选项只有 `slot.options`。`templatizer._options` 对 `numeric` 类型返回空数组
  → 一个**空下拉**：当前值看不到（被 select 顶掉）、也没法输入新阈值。
  方案 P0-3 要求的"数值阈值用输入框补填"没落地，"还差 N 个条件待补"的候选点确认只会
  反复弹回 `缺少参数`。
- `ChatPanel.vue:352-368`：复合题的步骤卡只渲染标题 + 口径 chip，**不渲染 slots**；
  `confirmSteps`（`ChatPanel.vue:743-761`）恒发 `step_slots: {}`，后端
  `composite_choice["slots"]` 永远为空 → 用户既看不到这一步用什么条件查，也无法补填缺槽。

**建议**：`slot.options` 为空时渲染数字输入框；步骤卡复用同一套槽位编辑组件并把
`step_slots` 带上。

---

## P2-11　零散问题

1. **示例直达改路线不生效**：`capabilities.execute` 对 `example/example_direct` 的 spec 组装
   （`capabilities.py:900-925`）来自 `spec.update(candidate["spec"])`，用户改路线只覆盖
   `spec["route_id"]`，`spec["route"]` 仍是示例自己的路线名；`fill_example_slots` 用
   `spec["route"]` 填 `{route_name}` → 卡片上选了新路线，实际仍按老路线查。线上 `{route_name}`
   是裸占位（`r.short_name = {route_name}`），若某条 SQL 把占位符写在引号里
   （`'%{route_name}%'`），回填还会拼出坏引号 `'%'宝安大道西乡段'%'`（复现脚本 V7 就是这一形态）。
2. **槽位清不掉**：`capabilities._slot_overrides` 用 `values.get(name) or slot.get("value")`，
   用户在卡上清空某个条件时会静默回退成原值。
3. **`execute(kind=...)` 形参没用**：`capabilities.execute` 收了 `kind` 但函数体没读，
   调用点硬编码传 `"mysql"`（`agent.py` 确认卡路径）——将来 postgres 模板会踩。
4. **`_declared_conditions` 不含 `has_image`/收藏/状态**：通用算子侧靠
   `catalog.resolve_column` 失败来整体弃权（不会答错，但会少给候选），与 P0-1 要求的
   "语义层认出的条件必须全部表达"不完全一致。

---

## P1-12　跨库示例被一律按 mysql 判定（✅ 已修）

**现象**：每次启动/对账都会在 stderr 打一行
`Failed to create logical plan: Error during planning: table 'wren.public.gps_points' not found`，
同时那条 PG 示例补不上物理 SQL。

**根因**：`nl2sql.physical_sql_of(sql)` 里把库类型写死成 `"mysql"`——
`references_physical_table("mysql", …)` + `_explains("mysql", …)` 的"本来就是物理 SQL"快捷判断
对 PG 必然失败，于是退到 `compile_mdl_sql("mysql", …)`，拿 MySQL 的模型去编译 PG 的 SQL。
实测这条 SQL（`pair-38eccadb0ad4d3fc.md`「gps_points 表的记录数量是多少？」）：

```
references_physical_table(mysql)=False   references_physical_table(postgresql)=True
_explains(mysql)=False                   _explains(postgresql)=True     ← 本来就能直接跑
compile_mdl_sql(postgresql) → wren.public.gps_points 不是本项目的模型
```

（物理表在 PG 里是存在的：`public.gps_points` ✓，不是缺表。）

**已修**：`physical_sql_of(sql, kind="mysql")` 透传库类型，并加 `example_kind(spec|row)`
统一从 `spec.kind` 取（认不出来才回 mysql）；缓存键改成 `(kind, sql)`，避免跨库串结果。
8 个调用点（`capabilities._executable_sql`、`templatizer._compile_wren`、启动预热、
`_reuse_candidates`、`backfill_physical_sql`、`_example_document`）全部按各自示例的库类型传。

**验证**：PG 示例现在被判定为"可直接跑的物理 SQL"且**全程不调用 Wren 编译**（噪音消失）；
模拟启动预热 66 条示例 → 66 条都能拿到物理 SQL、无任何 `Failed to create logical plan`；
MySQL 侧 30 条带 `wren_src_` 的示例照旧全部解析成功；缓存按 (kind, sql) 分开。

---

## P1-13　不该喂给 Wren 的 SQL 在 stderr 刷屏（✅ 已修）

**现象**（真机日志，两类混在一起刷）：

```
Failed to create logical plan: SQL error: ParserError("Expected: :, found: } at Line: 1, Column: 1921")
Failed to create logical plan: Error during planning: table 'wren.new_four_road_base_ai.ai_problem' not found
```

**根因**：`wren_engine.compile_mdl_sql_verbose` 的护栏（"SQL 里没有本项目的模型就跳过"）当初只给
`kind == "postgresql"` 加了；mysql 侧把两类**根本不是 MDL-SQL**的东西直接送进了引擎：

- **归一化 SQL**（还带 `{route_id}` 这种占位符）→ 引擎解析到 `{` 就 `ParserError("Expected: :, found: }")`；
- **物理 SQL**（`new_four_road_base_ai.ai_problem` 是库表名）→ 引擎按模型名找不到 → `table 'wren.…' not found`。

**已修**：

1. `wren_engine.compile_mdl_sql_verbose`：护栏对**所有库**生效；新增"还是归一化 SQL（带 {占位符}）"判定，
   两种情况都在进引擎**之前**返回原因，不再产生引擎日志。
2. `nl2sql.physical_sql_of`：带 `{占位符}` 的归一化 SQL 直接原样返回（它本来就要先 `fill_example_slots`
   填槽再执行），不再走 Wren 编译分支。

**验证**：6 条带占位符的归一化 SQL 逐个过 `physical_sql_of` → 全部原样返回、**一行 `Failed to create
logical plan` 都没有**；护栏冒烟：带占位符 → "还是归一化 SQL（带 {占位符}），先填槽再编译"，
物理 SQL → "SQL 里没有本项目的模型（可用：ai_asset_base、ai_asset_main、…）"。

---

## P1-14　查库失败把"空结果"钉进 5 分钟缓存（✅ 已修）

**现象/根因**：`_route_rows()` / `_known_damage_types()` 在 `except` 里写
`rows = 旧缓存 if 有 else []`，然后**无条件** `_ROUTE_ROWS = (now, rows)`——
一次 SSH 隧道抖动就会把"没有路线/没有病害类型"当成事实缓存 5 分钟：这期间所有问题都解析不出路线，
而且不会自愈（要等缓存过期或重启）。

**已修**（`route_resolver.py` 与 `templatizer.py` 里各自的 `_route_rows`）：

- 有旧缓存（哪怕已过期）→ 继续用旧的，不打断正在跑的问题；
- **没有旧缓存 → 直接返回空，但绝不写缓存** → 下次调用重新查库；
- 查库成功仍然照常写缓存（含"表确实为空"的正常空结果）。

**验证**：模拟隧道断开——无缓存时连续两次调用 → 返回空、缓存仍为 `None`、DB 被查了 2 次（会重试）；
有过期缓存时 → 返回旧数据且缓存保留；成功路径 → 写缓存、第二次命中缓存不再查库；
`_known_damage_types` 同样三态验证通过；真库回归 路线 3623 条 / 病害类型 55 条。

**同类的还有**（没动，属不同域，待定）：`field_catalog._catalog()`（失败会缓存 10 分钟空字段目录）、
`field_catalog.value_samples()`（失败会缓存空样例）。
logical plan` 都没有**；护栏冒烟：带占位符 → "还是归一化 SQL（带 {占位符}），先填槽再编译"，
物理 SQL → "SQL 里没有本项目的模型（可用：ai_asset_base、ai_asset_main、…）"。

---

## 附：一次跑完的复现脚本

放在 `backend/` 下执行（只读：不写注册表、不改设置）：

```python
import io, sys, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
import sqlglot
from app.services import templatizer as T, capabilities as C, chat_settings

# V1 TopN 绑不出来
top = ("SELECT p.damage_type, COUNT(*) AS cnt FROM ai_problem p "
       "WHERE p.del_flag = 0 AND p.collection_time >= '2026-07-01 00:00:00' "
       "GROUP BY p.damage_type ORDER BY COUNT(*) DESC LIMIT 5")
skel, reason = T.mine_sql("7月病害最多的类型前5名", top, "mysql")
print("V1", skel and skel["sql"], "| bind ->", T.bind(skel, dict(skel["values"])) if skel else reason)

# V2 自画像 shape
for label, sql in [
    ("非分组极值", "SELECT p.id FROM ai_problem p WHERE p.del_flag=0 ORDER BY p.area DESC LIMIT 1"),
    ("按月趋势", "SELECT DATE_FORMAT(p.collection_time,'%Y-%m') m, COUNT(*) c FROM ai_problem p WHERE p.del_flag=0 GROUP BY m"),
    ("分档对比", "SELECT CASE WHEN p.area > 10 THEN 'a' ELSE 'b' END b, COUNT(*) c FROM ai_problem p WHERE p.del_flag=0 GROUP BY b"),
]:
    print("V2", label, T._ast_profile(sqlglot.parse(sql, read="mysql")[0])["shape"])

# V3/V3b 枚举槽降级后不校验 + 转正丢状态
item = {
    "question": "西乡段上行的病害有多少处",
    "sql": ("SELECT COUNT(*) AS c FROM ai_problem p WHERE p.del_flag = 0 "
            "AND p.routes_id = '{route_id}' AND p.up_or_down = '{up_or_down}'"),
    "params": {
        "route_id": {"label": "路线", "type": "entity", "column": "routes_id", "role": "route",
                     "form": "value", "options": [{"label": "恒明珠段", "value": "R2"}]},
        "up_or_down": {"label": "上下行", "type": "enum", "column": "up_or_down",
                       "options": [{"label": "上行", "value": "1"}, {"label": "下行", "value": "2"}]},
    },
    "values": {"route_id": "R1", "up_or_down": "1"},
    "declares": {"route_id": "route", "up_or_down": "up_or_down"},
    "fingerprint": "demo",
    "profile": {"shape": "count", "shapes": ["count"], "subjects": ["disease"], "group_by": [], "metrics": ["count"]},
}
promoted = {"id": "auto_demo", "status": "mature", "promoted_at": "T", "enabled": True, "usage": 1}
merged = T._merge_cluster([item], promoted)
print("V3 status", promoted["status"], "->", merged["status"], "| promoted_at ->", merged.get("promoted_at"))
T.list_templates = lambda: [merged]
plan = {"kind": "mysql", "shape": "count", "subject": "disease",
        "slots": {"route_match": {"id": "R2"}, "up_or_down": "2", "group_by": "",
                  "semantic_conditions": [{"field": "up_or_down", "op": "=", "value": "2"}]},
        "semantic": {"metrics": ["count"]}, "required_conditions": ["route", "up_or_down"]}
hits = T.match(plan)
print("V3b 命中候选", len(hits), "->", C.execute(plan, hits[0], {"route_id": "R2"})[0] if hits else "")

# V4 开关关闭时的返回体
orig = chat_settings.load
chat_settings.load = lambda: {**orig(), "auto_template_mine_enabled": False}
print("V4", T.mine_all())
chat_settings.load = orig
```

示例库侧（V8/V9）用 `app.services.rag` 读线上文档即可复现，脚本里用的是
`pair-8f3ee34646e6e2e2.md` 与 `pair-381957f5285a5fec.md` 两份。
