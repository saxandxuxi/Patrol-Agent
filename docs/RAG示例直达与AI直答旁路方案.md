# 分层复用省钱链路方案：成功 SQL 自动模板化 + RAG 示例直达 + AI 直答旁路（给 codex）

> 本版在原《RAG 示例直达与 AI 直答旁路方案》上合并了"成功 SQL 自动模板化"，一次给全，只交付这一份。
> 目标只有一个：**链路上最贵、最慢、最容易错的是"用大模型写 SQL（qwen3-coder-plus）+ 语义校验 + 失败 refine 多轮"，能不写就不写。**
> 不改 Wren 语义层、不改已验证取数逻辑；大量复用现有 `preset_lib / capabilities / nl2sql 示例子域 / rag / schema_catalog / sql_engine / chat_settings`。

---

## 〇、一页总览：四级复用阶梯，命中即停

按"成本从低到高"逐级匹配，命中即停；用户可一键跳到最贵但最灵活的 L3。

| 级别 | 名称 | 怎么来的 | 问答时成本 | 触发/确认 | 状态 |
|---|---|---|---|---|---|
| **L0** | 内置确定性模板 | 代码 / `resources/sql_templates` YAML 手写 | **0 次 LLM** | 结构匹配后**必须弹卡，用户确认** | 已有 |
| **L1** | **自动模板化（auto 模板）** | trusted 成功 SQL **离线**把字面值挖成类型化占位符 | **0 次 LLM** | 结构+槽位全覆盖匹配后**必须弹卡，用户确认** | **本方案新增（省钱主力）** |
| **L2** | RAG 示例直达 | 同义问法向量召回 + **便宜模型**严格同问打分 | **1 次便宜模型** | 高门槛 + **必须弹卡，用户确认** | 本方案新增 |
| **L3** | Wren / LLM 完整生成 | 现有完整链路（补全→意图→语义→写 SQL→校验→修复） | 完整成本（最贵） | 用户点「让 AI 回答」(`force_ai`)，或上面全未命中 | 已有 |

> **最高原则（凌驾于所有省钱/自动化设计之上）：模板/示例"这次用不用、用哪一条"，确认权 100% 在用户。**
> 代码（结构匹配、槽位覆盖、回环验证）和大模型（向量召回、相似度打分、排序）只负责**找候选、排顺序、讲清口径**，**绝不允许自动选定某条模板直接出数**。哪怕匹配分再高、哪怕是 0 LLM 的确定性模板，也必须先把"准备用哪条模板、什么过滤口径、哪些参数、对应 SQL"摆成卡片，由用户点「确认查询」才执行；用户可以改槽位、换候选、或点「让 AI 回答」走 L3。
>
> 唯一例外：**固定看板卡片 / 快捷查询 / 预置查询的"刷新"按钮**。这些是用户**主动点击某张已绑定 SQL 的卡片**，点击动作本身就是显式选定模板，直接实时取数、不再二次弹卡。对话问答流里的自动推荐不在此列，一律要确认。

要点：

- **L1 是省钱主力**：用户每确认一条成功问答，就离线沉淀一个"换个路线 / 换种病害 / 换个阈值也能跑"的参数化模板，之后同类问题**推荐给用户一键确认**即可，零写 SQL。模板越用越多，L3 占比越来越低，**越用越省、越用越准**。
- **L2 兜底同义问法**：L0/L1 是"结构匹配"，承接不了"换个说法但语义同构"；L2 用 RAGFlow 混合检索 + 便宜模型严格判定，承接这部分，门槛高、仍要用户确认。
- **L3 永远可用**：输入框「智能回答」或确认卡「让 AI 回答」置 `force_ai=true`，本条消息跳过 L0/L1/L2 所有卡片，直接走 Wren。
- 自动挖槽是**离线/异步**做的，不占问答链路的钱和时间；它只"准备候选"，不决定"采用"。

全链路：

```
用户消息（长对话先补全成 standalone 问题）
   │
   ├─ force_ai=true？──是──▶ 【L3 完整 Wren 链路】，且不弹任何选择卡 → 结束
   │
   ├─ 否：【L0/L1 确定性模板】结构匹配（零 LLM，只找候选、不自动采用）
   │        统一模板注册表（builtin + preset + auto）按 shape/主体/分组/指标/槽位全覆盖匹配
   │        ├─ 命中任意候选 → 一律弹"口径确认卡"（推荐项 + 全部候选 + 参数 + 可折叠 SQL）
   │        │                  ├─ 用户「确认查询」→ 绑定白名单参数 → execute_sql 实时取数 → 出答案
   │        │                  ├─ 用户改槽位 / 换候选 → 重新绑定再确认
   │        │                  └─ 用户「让 AI 回答」→ L3
   │        └─ 未命中 / 槽位覆盖不全 ↓
   │
   ├─【L2 RAG 示例直达】（开关开、单查库问题）
   │        expand_for_retrieval → rag.retrieve 混合召回 trusted 示例(TopK)
   │        → 便宜模型严格同问打分 → 代码硬门槛（阈值+唯一分差+槽位齐+SQL可信）
   │        ├─ 高置信唯一 → 弹"直达确认卡" → 用户确认 → 绑定执行实时取数 → 出答案
   │        │                          └─ 用户点"让 AI 回答" → L3
   │        └─ 未达门槛（静默不打扰）↓
   │
   └─【L3 完整 Wren 链路】意图→规划/语义→写 SQL→执行→校验/反思
            （L0/L1/L2 绑定或执行报错，也自动降到 L3 重试一次，不硬答）
```

---

## 一、现状与问题（已读代码确认）

现在已有两套"模板/复用"，但参数化槽位太窄，导致大量成功 SQL 复用不了：

1. **手写预置模板 `services/preset_lib.py`**：唯一事实源 `data/preset_queries.json`，参数走 `params + options 白名单 + bind()`（白名单动态连库现查：`routes_with_data / months / damage_types / top_n`），匹配走 `match_templates()` 的"shape + subject + group_by + metrics + 槽位全覆盖"。但**参数只有 route_id / damage_type / month / up_or_down / top_n 几个**，且要人工一条条写。
2. **成功示例归一化 `nl2sql.py`**：`save_successful_pair` 存"问题+意图 JSON+SQL"；`infer_example_spec()` 只识别 route / damage_type / time_range / pile_range / device_code 五类维度；`_normalize_sql_for_example()` 也只替换这几类字面值；`_example_spec_usable()` 规定**归一化后只要还残留任何 `='某某'` 或 `=数字` 就整条判不可复用**。

**瓶颈**：一条成功 SQL 只要带了"上下行 / 面积阈值 / 置信度阈值 / 大类 / 来源 / 工单状态 / 收藏 / 关注 / 具体 TopN"等任一条件，归一化后就残留字面值 → 直接弃用。结果就是问答库里躺着大量带 SQL 的成功问题，却只能"整句相似"时复用，换个参数就得重新花钱写 SQL。

**本方案做的事**：把槽位体系做全，做一个**离线模板化挖掘器（templatizer）**，把 trusted 成功 SQL 自动提升为 L1 参数化模板，纳入和手写模板同一套注册表与匹配；再配 L2 同义直达与 L3 旁路。

---

## 二、能力 A：成功 SQL 自动模板化（L1，省钱主力）

### 2.1 定义与铁律

- **模板化 = 只把 SQL 里的"确定性字面值"替换成类型化占位符**（WHERE 的过滤值、LIMIT 的 N、IN 列表、可选的分组维度值）。
- **绝对不改结构**：表、JOIN、JOIN 顺序、WHERE 逻辑与括号、聚合函数、GROUP BY 列、子查询、ORDER BY 指标，一个都不许动。改了结构就不是"同一条查询换参数"，必须丢弃。
- 产出四件套：**参数化 SQL + 槽位声明 + 结构指纹 + match 声明**（见 2.4）。

### 2.2 哪些能模板化、哪些不能（明确判据）

**能模板化**：单条只读 SELECT；涉及的表都在白名单；所有字面过滤条件都能映射到 2.3 的已知槽位类型；回环验证通过；来源 trusted。

**不能模板化（命中任一条就不生成 auto 模板）**：

1. **零过滤的全库/全表明细**：如 `SELECT * FROM ai_problem ... LIMIT 500`（"全库的病害有哪些"）。它没有可替换维度，模板化无意义，且已被通用明细算子覆盖，**不要产模板**。
2. **一次性业务主键 / 自由文本**：过滤值是具体 `problem_number`、病害 `id`、`memo / problem_name / problem_describe LIKE '某词'` 这类不可泛化、没有稳定取值域的条件。
   - 例外：若该列本身是可枚举实体（patrol、vehicle、device、user、section），应做成实体槽位泛化，而不是当一次性值。
3. **运行时才算出来的动态分界**：分档中位数、NTILE、PERCENTILE、由数据驱动的 `CASE WHEN <聚合> <阈值>`（"新老病害按采集时间中位数分"）。这类必须保留为**代码算子**，不能静态模板化。
4. **复合 / 知识 / 看图 / 多语句 / 写操作 / 跨库**：需要多步、要结合规范文本、主诉求是看图、非 SELECT、一条 SQL 跨 mysql/postgresql 的，不做单模板。
5. **参数位置无法稳定定位的复杂结构**：多层相关子查询、SQL 解析后无法把某字面值唯一对应到某个列的，默认不自动模板化（可保留人工 preset）。
6. **回环验证失败**（2.6）：回填后与原 SQL 不等价或结果对不上。
7. **非 trusted**：用户没确认过的 pair 不挖。

### 2.3 槽位类型体系（基于 `ai_problem` 真实字段注释，第一批要支持）

> 实体槽位的下拉取值一律**连库现查去重**（沿用 `preset_lib._dynamic_options` 模式），编码类一律给"中文↔code"枚举映射（沿用并外置现有 `_CONDITION_VALUE_HINTS`），用户说中文、库里存 code。

| 槽位 key | 物理列 / 表达式 | 类型 | 取值来源 / 枚举（中文↔code） |
|---|---|---|---|
| `route_id` | `routes_id` / `r.id` | entity 单选/多选 | `routes_with_data` 现查；多选用 IN |
| `section_id` | `sections_id` | entity | 该路线下路段现查 |
| `patrol_id` / `patrol_number` | `ai_maintain_patrol.id` / `p.patrol_number`（需 JOIN patrol） | entity | 巡查任务现查；注意编号里的数字不是时间 |
| `vehicle_id` / `device_code` | `vehicle_id` / `device_code` | entity | 车辆/设备现查 |
| `inspection_user_id` | `inspection_user_id` | entity | 巡检用户现查 |
| `damage_type` | `damage_type`=字典 `problem_code` | enum/entity | `damage_types` 现查（显示 `problem_type`，存 code）；多选 IN |
| `category` | `categories_type` / 字典 `disease_categories_type` | enum | 0路面病害 / 5路基病害 / 10沿路设施 / 15交安事件 |
| `up_or_down` | `up_or_down` | enum | 0全部 / 1上行 / 2下行 |
| `problem_status` | `problem_status` | enum | 0待转工单 / 10已转工单 / 15忽略病害 |
| `operate_type` | `operate_type` | enum | 0待转工单 / 3病害恢复 / 5删除工单 / 10转工单 / 15忽略病害 |
| `problem_source` | `problem_source` | enum | 0手动 / 5车辆巡检 / 10人工巡检 / 15历史工单 / 20路长转养护 / 25路政转养护 |
| `mission_flag` | `mission_flag` | bool enum | 0未绑定 / 1已绑定巡查任务 |
| `collect` | `collect` | bool enum | 0否 / 1是（收藏） |
| `track_problem` | `track_problem` | bool enum | 0否 / 1是（关注病害） |
| `time_month` / `time_range` | `collection_time`（主）/ `create_time` | time | `months` 现查 或 [起,止]；支持相对时间（本月/上月/7月） |
| `num_area` | `area` | numeric 阈值 | op∈{>,>=,<,<=,=,between} + 数值，数值域校验 |
| `num_confidence` | `CAST(JSON_UNQUOTE(JSON_EXTRACT(pic_position,'$.confidence')) AS DECIMAL(10,6))` | numeric 阈值 | 同上；**注意是 JSON 里的 confidence，不是裸列** |
| `num_length` / `num_width` / `num_height` / `num_pile_distance` / `num_pile_meter` | `disease_length / area_width / area_height / pile_distance / pile_meter` | numeric 阈值 | 同上 |
| `pile_range` | `pile_distance BETWEEN a AND b`（或桩号解析） | spatial | 米数区间 |
| `luge_name` | `luge_name` | entity/string | 路格现查 |
| `has_image` | `problem_url`（默认，原始采集图）空否；可选 `preview_url`（画框缓存图） | bool 派生 | 1有图 / 0无图（`IS NULL OR TRIM(...)=''`）；**两个字段口径要可区分，默认 problem_url** |
| `top_n` / `limit` | `LIMIT n` | int | 白名单 1..50（沿用 preset） |
| `group_by` | GROUP BY 的列 | dimension | 仅允许白名单可分组列：problem_type / up_or_down / problem_status / problem_source / category / DATE_FORMAT(collection_time) / sections_id / luge_name / vehicle_id 等 |

实现要求：

- numeric 槽位要把"比较符"和"值"分开存（`op` + `value`），这样"面积大于 5"和"面积大于 10"是同一模板换值；`between` 存两个值。
- 多选实体（多路线、多病害）用 IN，槽位标 `multi=true`。
- 枚举槽位回填时，用户给中文要翻成 code；模板里可以直接存 code 占位，展示层显示中文。
- 字段→槽位的映射**以 `schema_catalog`（读 COLUMN_COMMENT 的单一事实源）为准**，不要再在 nl2sql 里新写一份硬编码表；枚举映射表外置到资源文件（呼应《代码结构重构与模板外置方案》）。

### 2.4 auto 模板记录结构（对齐 preset，纳入统一注册表）

建议存 `backend/data/auto_templates.json`（或 agent.db 一张表，随注册表方案统一），单条：

```json
{
  "id": "auto_disease_count_route_damage",
  "source": "auto",
  "title": "某路线某病害类型数量",
  "question": "西乡段泛油有多少处",
  "variants": ["西乡大道有多少泛油病害", "恒明珠坑槽有几处"],
  "kind": "mysql",
  "dialect": "physical",
  "sql": "SELECT COUNT(*) AS cnt FROM ai_problem p WHERE p.del_flag=0 AND p.routes_id='{route_id}' AND p.damage_type='{damage_type}'",
  "params": {
    "route_id": {"label": "路线", "column": "routes_id", "table": "ai_problem", "type": "entity", "value_source": "routes_with_data", "required": true},
    "damage_type": {"label": "病害类型", "column": "damage_type", "table": "ai_problem", "type": "enum", "value_source": "damage_types", "required": true}
  },
  "match": {"shape": ["count"], "subject": ["disease"], "group_by": [], "metrics": ["count"], "slots": ["route", "damage_type"], "fixed": {}, "flags": {}},
  "fingerprint": "select count(*) from ai_problem where del_flag and routes_id and damage_type",
  "origin_doc": "pair-xxxx",
  "status": "mature",
  "trusted": true,
  "enabled": true,
  "usage": 0,
  "created_at": "2026-09-21T..."
}
```

`match` 字段语义与 `preset_lib.match_templates` 完全一致（shape/subject/group_by/metrics/slots/fixed/flags），这样 auto 模板能被同一套匹配逻辑直接消费。

`status` 取值见 2.5.4：`mature` 才参与对话推荐与弹卡，`candidate`（仅单条、未经跨样本证实）只在预置维护页"待确认"区累积，不进对话候选；簇内始终同值的位置写入 `match.fixed` 而不是 `params`。

### 2.5 离线挖掘逻辑（不花问答链路的钱）

#### 2.5.1 两个核心理念

**理念一：列驱动，不做"值驱动"的字符串替换。**
现有 `_normalize_sql_for_example` 用 `str.replace` 把具体值换成占位符，很脆——值在子查询里出现多次、值与别的列撞车、大小写/引号不同都会换错或漏换。模板化必须用 **sqlglot 解析成 AST**：遍历 WHERE / HAVING 的每个谓词，先定位**左值是哪一列**（`表.列` 或固定 JSON 表达式），经 `schema_catalog` 查到它的语义类型，**右值变成什么槽完全由列决定**。值长什么样不重要，列才决定槽类型、比较符和填值方式。

**理念二：槽位的金标准是"同一骨架、跨样本发生变化的位置"，让数据自己证明这里可变。**
单看一条 SQL，无法区分 `'西乡段'` 是"可换参数"还是"这条查询本来就写死的常量"。因此：

- 把 trusted SQL 按"结构指纹"聚类成簇；同簇里某位置在 **≥2 个变体中取过不同值** → 几乎必然是槽（confirmed slot）。
- 同簇多条、某位置**始终是同一个值** → 它是该模板的 fixed（例如某模板固定只看"泛油"），**不暴露成可填槽**。
- 只在单条 SQL 里出现、没有第二个变体佐证 → 标 **candidate（待观察）**，先沉淀但不推荐，等第二个变体自动证实、或用户在维护页手动转正。
- 规则负责把谓词**归类成槽类型**，跨样本比对负责**确认这里真的可变**；两者分工，不靠 LLM 猜。

#### 2.5.2 触发时机（全部异步，不在问答请求内）

- 用户在对话里 **approve 一条成功 pair** 后，排队跑一次模板化；
- 后台 reconcile（沿用 `main._reconcile_examples_forever`）批量扫所有 trusted 但尚未模板化的 pair，并对已有簇重跑"跨样本确认"，让 candidate 自动升级。

#### 2.5.3 单条 SQL 挖掘 pipeline

0. **预检**：套用 2.2 判据（单条只读 SELECT、表白名单、无写操作/多语句、无动态分档/自由文本/一次性主键），不满足直接放弃。
1. **AST 解析 + 抽骨架**：sqlglot 解析，抽出 shape（count/distribution/top/list…）、subject 主表、JOIN 集合、GROUP BY、聚合指标、投影列；解析失败就放弃，不回落脆弱正则硬挖。
2. **谓词分类（列驱动，0 LLM）**：遍历等值 / IN / BETWEEN / 大小于 / IS NULL / LIMIT，按 2.3 槽位表归类为 fixed / entity / enum / time / numeric（op 与 value 分开）/ bool 派生 / top_n：
   - `del_flag=0`、`tenant_id` 等 → fixed；
   - 落在 JOIN 表上的谓词（如 patrol_number 在 `ai_maintain_patrol`）→ 槽位记录"所属表 + 所需 JOIN"，**JOIN 结构原样保留，换值不增删 JOIN**；
   - numeric 槽必须同时存比较符与数值，"面积>5"和"面积>10"才是同模板换值；
   - **GROUP BY 列、ORDER BY 指标第一期视为结构的一部分，不挖成"分组维度槽"**（"按类型分布"和"按上下行分布"是两个模板，避免互串，这是踩过的坑）。
3. **残留处理**：归不进任何槽、也不是 fixed 的业务谓词 → 调一次便宜模型（附录 A `templatize_sql.md`）归类；仍归不出或判为不可模板化 → **放弃**，该条只留作普通示例（L2 仍可能用到）。
4. **产出**：参数化 SQL（值→`{slot}`）、`params`、`match` 声明、结构指纹。

#### 2.5.4 跨样本聚类与槽位确认（关键步骤）

- **结构指纹**：把 AST 规范化（小写关键字、统一别名与空白、字面量统一替换为占位）后取哈希。同指纹归为一簇，问题原文累加进 `variants`（复用 `append_example_variant`），不重复产模板。
- 对簇内每个候选槽位置，收集各样本在该位置的值集合：
  - 出现 **≥2 个不同值** → `confirmed` 槽；
  - 始终只有 1 个值 → 判为该模板 **fixed**；
  - 值形态/类型互相冲突、无法稳定对应同一列 → 不挖该位置。
- 模板分两个状态：
  - **`mature`（成熟，可推荐）**：簇内 ≥2 个变体且关键槽被跨样本证实；或虽是单条但用户在预置维护页手动转正。
  - **`candidate`（待观察，不推荐）**：仅单条 trusted、槽位未经跨样本证实。入库沉淀并继续累积变体，但**不进对话候选、不弹卡**；一旦出现同指纹第二变体自动升级为 mature，或由人工转正。
- 这样既不放过可复用模板，又不让"只见过一次、可能本就写死"的条件被错误泛化后反复答错。

#### 2.5.5 回环验证与入库

- mature 与 candidate 都必须过 **2.6 回环验证**（回填后 AST 结构等价 + 实跑结果一致），不过不入库。
- 通过后写 auto 模板注册表：`source=auto`、`status=mature/candidate`、`trusted=true`；mature 纳入 `capabilities` 候选 / 固定看板 / 预置维护页，candidate 仅在维护页"待确认"区可见。
- 失败只保留为普通示例，并 `log_event("templatize","rejected", reason=...)`，便于后续补槽位类型。

#### 2.5.6 LLM 的使用边界

- 便宜模型只用于：归类规则认不出的谓词、给模板起标题 / 复核 shape。
- **绝不改写 SQL、不增删结构、不决定某条模板采不采用**——采用权始终在用户确认（见 §〇 最高原则）。

### 2.6 回环验证（自动模板的可信硬门槛）

每条 auto 模板入库前必须过两道：

1. **SQL 等价**：用挖掘时记录的原始字面值回填模板，经 sqlglot 规范化后与原 SQL 做结构比对（表/JOIN/WHERE/聚合/GROUP BY/投影列一致，忽略空白与别名）。不一致直接丢弃。
2. **结果一致**：用 `sql_engine.execute_sql` 实际跑"原 SQL"和"回填后 SQL"，比对结果——count/单值聚合必须相等；明细比"行数 + 关键列校验和（如面积总和、id 哈希计数）"。不一致丢弃。

任一失败：不进 L0/L1，降级为普通 RAG 示例（L2 仍可能用到），并 `log_event("templatize","rejected", reason=...)`。

### 2.7 安全红线

- 只参数化"值"，永不改结构；占位符强类型（entity/enum/time/numeric/int/bool）。
- 回填沿用 `preset_lib.bind` 的双保险：**先白名单校验（动态 options / 枚举 code 集 / 数值域 / 正则），再 `_SAFE_VALUE` 字符校验**；entity 值必须来自连库现查的 id 集合，numeric 限定数值，禁止把用户原文拼进 SQL。
- 只读 SELECT、表白名单、强制 LIMIT、kind/方言锁定（mysql 模板不会跑去 postgresql）。
- RAGFlow 文档里的自由 SQL 不得直接执行，必须经过"模板化 + 回环验证 + 白名单绑定"后才成为可执行模板。

### 2.8 统一模板注册表与匹配

- 把三类模板收敛到一个注册表（呼应《代码结构重构与模板外置方案》的 TemplateRegistry）：
  - `builtin`（resources YAML）、`preset`（手写 json）、`auto`（本方案挖掘）。
- 匹配统一走"shape + subject + group_by + metrics + flags + **槽位全覆盖**"：模板声明能表达的槽位必须**覆盖问题的全部过滤条件**，问题里多了模板表达不了的条件（如模板没有"面积阈值"槽）就不命中——沿用 `match_templates` 现有"宁可不匹配，不可错匹配"。
- auto 模板与 builtin/preset 一样进入 `capabilities` 候选、固定看板卡片、预置查询维护列表。
- **是否采用一律由用户确认**：对话问答流里，L0/L1 哪怕唯一高置信命中也只作为卡片上的"推荐项"，必须用户点「确认查询」才执行，不做任何后台自动直出。新增总开关 `auto_template_enabled` 只控制"要不要把 auto 模板纳入候选"，不改变"必须确认"这一行为。

### 2.9 生命周期、自动同步与前端

- approve 即挖、后台批量补挖；后端或前端手工新增/编辑模板后，注册表按 mtime/版本自动重载（沿用 preset 的热加载），**自动同步到可被匹配/检索的库，无需重启**。
- "预置查询维护"页新增一组"自动生成模板"：展示标题、来源问题、槽位、命中次数、最近取值、启用开关；支持 **停用 / 编辑槽位与口径 / 转正为手写 preset / 删除**。auto 模板在卡片上打"自动生成"标签，和手写模板区分。
- auto 模板与 RAGFlow 示例用 `fingerprint` 去重，不重复沉淀。

---

## 三、能力 B：RAG 示例直达（L2，承接模板覆盖不到的同义问法）

L1 已经能覆盖"同构换参"（换路线/病害/月份/阈值），L2 主要承接**结构匹配没命中、但语义其实同构**的问法（说法差异大、或条件组合没被任何模板覆盖）。机制沿用原方案，简述如下（完整判定与提示词见附录 B）：

1. **时机**：长对话补全成 standalone 问题之后、意图/规划之前；且 `rag_direct_enabled=true`、非 `force_ai`、预判为"一条 SQL 能答完的查库题"。
2. **召回**：`query_rewrite.expand_for_retrieval` 扩词 → `rag.retrieve` 混合检索（向量+全文，带 `vector_similarity_weight`），限定 trusted 示例/问答数据集，TopK=`rag_direct_topk`。
3. **打分**：便宜模型按附录 B 的 7 要素输出每候选 score / same_query / 槽位映射 / missing_slots / differences / verdict。
4. **代码硬门槛**：`score>=rag_direct_threshold(0.92)` 且 Top1−Top2 `>=rag_direct_margin(0.08)`；结构一致（`infer_example_spec + _same_structure`）；必需槽位可解析（路线名→id、病害中文→code、时间、枚举）；SQL trusted/参数化；kind/方言一致。任一不过静默走 L3。
5. **确认与执行**：高置信唯一才弹一张直达卡（标准问法+口径+参数+可折叠 SQL）；用户确认后复用 `capabilities.stash/take` + `fill_example_slots / preset_lib.bind` + `sql_engine.execute_sql` **实时取数**（刷新语义，不返回旧缓存），结果打 `source="example_direct"`，跳过规划/写 SQL/校验。
6. **降级**：绑定或执行失败不硬答，自动转 L3 一次（带错误记忆）。
7. L1 已能给出确定性候选时不再调用 L2（省一次打分），候选统一进确认卡等用户确认；完全相同的问法先用 `find_existing_example` 本地精确命中，跳过 LLM 打分；打分结果按归一化问题短时缓存。任何候选都不得绕过确认卡直接执行。

---

## 四、能力 C："让 AI 回答"旁路（force_ai）

- 入口：直达卡「让 AI 回答」按钮、输入框「智能回答」按钮、或关闭复用开关。
- `routers/chat.py` 的 `ChatRequest` 增 `force_ai: bool=False`，**只对本条消息生效**，不改全局设置。
- `force_ai=true` 时在 `agent._handle_message / _build_query_result` 中：跳过 L0/L1 模板匹配与卡、跳过 L2 直达卡、跳过 plan 级 `collect_capabilities` 弹卡（等价本条 `reuse_mode=off`，引擎以 Wren 为主力），直接走完整链路。

---

## 五、多级命中只出一张卡

同一问题可能同时命中 L0/L1/L2，为避免重复打断：

- 统一由 `capabilities` 收集并排序：**确定性模板（builtin/preset/auto）优先于 L2 相似示例**；同一张确认卡里分组展示"确定性模板（推荐）/ 相似已验证问法"，默认勾选确定性最优项，用户可展开选别的或点「让 AI 回答」。
- 槽位全覆盖原则对所有候选一致：候选表达不了问题里的某个条件，就不进候选。
- **只要存在候选就必须弹卡，不存在"高置信自动出数"**：L0/L1/L2 在对话流里统一为"推荐 → 用户确认 → 执行"，代码和模型都不能替用户点确认。用户主动点固定看板/快捷查询卡片的"刷新"属于显式选定，不二次弹卡。
- 复合题：本期 L0/L1/L2 只处理"一条 SQL 答完"的单查库问题；复合/知识/看图整体走 L3。二期可对复合题逐子步骤匹配（命中子步骤用模板、未命中走 Wren，`capabilities.collect_steps` 已有雏形），但**每个子步骤采用哪条模板同样要在确认卡里逐条列给用户确认**，本期留接口不实现。

---

## 六、设置项（合并进 `chat_settings.load/save` + 前端设置页）

| key | 含义 | 默认 |
|---|---|---|
| `auto_template_enabled` | 是否把自动模板（L1）纳入候选推荐 | `true` |
| `reuse_mode` | 复用策略：`ask`=命中必须弹卡由用户确认（默认、且对话流强制）；`off`=完全不推荐模板/示例，全走 L3。**移除原 `auto`（自动直出）语义**，历史上存了 `auto` 的一律按 `ask` 处理 | `ask` |
| `rag_direct_enabled` | 是否启用 RAG 示例直达（L2） | `true` |
| `rag_direct_threshold` | L2 同问分阈值（高门槛） | `0.92` |
| `rag_direct_margin` | Top1−Top2 最小分差 | `0.08` |
| `rag_direct_topk` | 混合检索召回数 | `8` |
| `rag_direct_confirm` | L2 是否必须确认（本期恒 true） | `true` |
| `force_ai`（请求级，不入持久设置） | 本条强制走 L3 | `false` |

模型约束：**离线挖槽兜底（L1）与 L2 打分都用便宜模型**（`role_model("semantic")`/flash），**禁止用 `sql_model()`**；这两处只做判定/抽取，不写 SQL。

---

## 七、成本与耗时（为什么省钱）

| 级别 | 问答时新增 LLM 调用 | 命中后省掉 | 大致耗时 |
|---|---|---|---|
| L0/L1 | 无（结构匹配是代码） | 补全后的意图/语义/写 SQL/校验/refine 全部 | 与看板刷新同构，亚秒~1 秒取数 |
| L2 | 1 次便宜模型打分 + 1 次检索 | 最贵的写 SQL 及其校验重试 | 秒级 |
| L3 | 完整链路（含 qwen3-coder-plus 写 SQL，可能多轮） | — | 最慢、最贵 |

- L1 的挖槽在 approve 时**离线一次性**完成，成本被后续无数次复用摊薄；模板量随使用增长，L3 占比单调下降。
- L2 未命中只多一次便宜打分；精确同句、短时缓存可连这次都省掉。
- **交互成本是刻意保留的**：对话流里每次模板/示例命中都多一张确认卡、一次点击。这是用"一次确认点击"换掉"最贵的写 SQL 调用 + 模型猜错口径的风险"，确认权必须在人；固定看板/快捷查询的主动刷新不增加这一步。
- 所有新行为都有开关，关闭即回到现状。

---

## 八、落地步骤（分三期，每期跑 `run_eval.py`(90) + `run_hard_eval.py`(15) 回归，准确率不许下降）

**期 1：L1 规则版（最高性价比，先做）**

1. 外置枚举/槽位元数据：把字段→槽位、中文↔code 枚举从 nl2sql 常量迁到资源文件，以 `schema_catalog` 为单一事实源。
2. 新增 `services/templatizer.py`：sqlglot 解析 → 结构指纹 → 规则挖槽（覆盖 2.3 第一批槽位）→ 参数化 SQL + params + match。
3. 回环验证（SQL 等价 + 结果一致），auto 模板写 `data/auto_templates.json`（热加载）。
4. 统一注册表：让 preset 的 `match_templates / bind` 同时消费 auto 模板（source 区分），纳入 `capabilities` 候选与看板。
5. approve 后异步触发 + 后台批量补挖；`auto_template_enabled` 开关。

**期 2：挖槽兜底与模板管理**

6. 落盘 `prompts/templatize_sql.md`（附录 A），规则挖不全时便宜模型兜底。
7. 预置查询维护页展示/停用/编辑/转正/删除 auto 模板，显示命中次数；自动同步。
8. 统计 trusted pair 可模板化率、L3 占比，补槽位类型提升覆盖。

**期 3：L2 + force_ai**

9. 落盘 `prompts/example_direct_match.md`（附录 B）；新增 `services/direct_match.py`（或 `capabilities.direct_match`）。
10. `_handle_message` 补全后插入 L2 闸 + 硬门槛 + 直达卡；确认走 stash/take/绑定/execute_sql，`source=example_direct`。
11. `ChatRequest.force_ai` 与前端「智能回答」，跳过 L0/L1/L2 全部卡片直走 Wren；失败逐级降级一次。
12. 多级候选合并为一张卡。

---

## 九、验收标准

**L1 自动模板化**

- "西乡段泛油有多少处"被挖成含 `{route_id}{damage_type}` 的模板；换成"恒明珠段坑槽有多少"能零 LLM 正确出数。
- 带上下行 / 面积阈值 / 置信度阈值 / 大类 / 来源 / 工单状态 / 收藏 / TopN 的 trusted 成功 SQL，从现在的"归一化残留字面值→不可复用"变为可复用模板，且换阈值/换方向结果正确。
- "全库病害有哪些"这类零过滤全表明细**不生成**模板；中位数分档类**不被**静态模板化（保留算子）；具体 problem_number / memo LIKE 不被错误泛化。
- **跨样本证据生效**：同一指纹只有单条变体时模板为 `candidate`，不进对话推荐、不弹卡；出现第二条同指纹、仅路线/类型/阈值等取值不同的成功 SQL 后自动升级 `mature`，且只有"取值发生变化的位置"被开成槽，簇内始终同值的位置进 `match.fixed` 而非 `params`。
- 回环验证不一致的模板绝不入库；注入尝试（路线名里带引号/`;`）被白名单与字符校验拦截；mysql 模板不会跑去 postgresql。
- **人工确认红线**：对话问答流里，L0/L1/L2 任何候选命中后，未经用户在卡片上点「确认查询」，后端不得执行该模板/示例 SQL（日志中不允许出现"自动选定模板并直出"的记录）；代码匹配分和 LLM 打分只用于排序与默认推荐，不构成执行授权。唯一可直接执行的是用户主动点击固定看板/快捷查询卡片的"刷新"。
- 指标：抽样 trusted pair 的"可模板化率"给出统计；回归集通过率不下降；用户确认后命中 L0/L1 的请求日志中**无 sql_model 写 SQL 调用**。

**L2 / force_ai**

- 已成功问题及其同义改写（仅换参数）首条即弹直达卡，确认后秒级出数且与 L3 答案一致；形态/维度/条件不同的问题不弹卡（误直达率目标 0）。
- `force_ai` 全程不弹任何选择卡；关闭开关行为回到现状；长对话指代先补全再匹配；复合/知识/看图不被错误直达。

---

# 附录 A：`prompts/templatize_sql.md`（离线挖槽兜底，可直接落盘）

> 仅在规则挖槽后仍有"无法归类的字面值"时调用一次；用便宜模型。只做抽取与判定，不重写 SQL、不回答问题。
> 占位符 `<<name>>`，分段 `[system]/[user]`，由 `prompts.messages("templatize_sql", ...)` 调用，`chat_json` 解析。

```markdown
<!-- 把一条"已验证可跑通"的成功 SQL 挖成参数化模板：只识别哪些字面值可换成槽位，不改查询结构。 -->
[system]
你是道路巡检数据问答系统的"SQL 模板化分析器"。给你一条已经验证能跑通的 SELECT（MySQL 或 PostgreSQL），
你要判断它能否沉淀为"只换参数值就能复用"的确定性模板，并把其中的字面值识别为带类型的槽位。

铁律：
1. 只识别"值"，绝不能改动表、JOIN、WHERE 逻辑与括号、聚合、GROUP BY、ORDER BY、子查询结构。
2. 不能为了凑槽位而改变查询含义；拿不准某个字面值能不能泛化，就把它放进 unresolved_literals 并令 templatizable=false。
3. 以下情况直接 templatizable=false 并说明 reason：
   - 没有任何业务过滤、本质是全库/全表明细导出（如 SELECT * ... LIMIT 500）；
   - 过滤值是一次性主键或自由文本（具体 problem_number、病害 id、memo/name/describe LIKE 某个词）；
   - 含运行时才算的动态分界（中位数分档、NTILE、PERCENTILE、由聚合结果决定的 CASE 阈值）；
   - 非 SELECT、多语句、写操作、跨库、复合多步、看图/知识类；
   - 字面值无法稳定对应到某个列，或不在下面给出的可识别槽位/字段范围内。

可识别的槽位类型（value_source / enum 必须照抄系统给你的字段字典，不要自己造列）：
- entity：route_id(路线)、section_id(路段)、patrol(巡查任务)、vehicle_id/device_code(车辆设备)、inspection_user_id；
- enum（中文↔code）：damage_type(病害类型)、category(大类)、up_or_down(0全部/1上行/2下行)、
  problem_status(0待转工单/10已转工单/15忽略)、operate_type、problem_source(0手动/5车辆/10人工/15历史/20路长/25路政)、
  mission_flag(0/1)、collect(收藏0/1)、track_problem(关注0/1)；
- time：time_month(按月份)、time_range(起止)；
- numeric：area(面积)、confidence(pic_position JSON 里的 $.confidence)、disease_length、area_width、area_height、
  pile_distance、pile_meter；必须给出比较符 op（>,>=,<,<=,=,between）和原始值；
- spatial：pile_range(桩号米数区间)；
- bool 派生：has_image（problem_url 原始采集图是否为空；若用的是 preview_url 要注明 image_field=preview_url）；
- int：top_n/limit（LIMIT 的数字）；
- dimension：group_by（仅当分组列属于可分组白名单：病害类型/上下行/状态/来源/大类/采集月份/路段/路格/车辆）。

对每个识别到的槽位输出：name、column(物理列或固定表达式)、table、type、op(数值类必填)、
value_source 或 enum_map、multi(是否 IN 多值)、required、original_value(这条 SQL 里的原始值)、quoted(原值是否字符串)。
param_sql：把原始值替换成 {name} 占位后的 SQL（字符串值占位由绑定层负责加引号，你按原样把 '值' 换成 {name} 即可；
LIMIT/数值类同样换成 {name} 裸占位）。不要遗漏任何仍残留在 WHERE/IN/LIMIT 里的具体值——
凡是没换成占位、又无法解释为固定业务常量（如 del_flag=0）的字面值，都要进 unresolved_literals。

只输出 JSON：
{
  "templatizable": true/false,
  "reason": "一句话结论（不可模板化时写明命中哪条判据）",
  "shape": "count|list|distribution|comparison|topn|trend|ratio|sum|aggregate|other",
  "subject": "disease|patrol|route|section|asset|work_order|other",
  "kind": "mysql|postgresql",
  "group_by": "",
  "metrics": [],
  "slots": [
    {"name":"", "column":"", "table":"", "type":"", "op":"", "value_source":"", "enum_map":{},
     "multi":false, "required":true, "original_value":"", "quoted":true}
  ],
  "fixed": {"del_flag":"0"},
  "unresolved_literals": [{"expr":"", "why":""}],
  "param_sql": "带 {name} 占位的完整 SQL",
  "match": {"shape":[], "subject":[], "group_by":[], "metrics":[], "slots":[]}
}

[user]
数据库方言：<<kind>>
字段字典（表.列 注释、枚举取值）：
<<schema>>
原问题：<<question>>
已验证 SQL：
<<sql>>

请输出 JSON。只要有一个业务字面值既没换成占位也没在 fixed 中给出合理解释，templatizable 必须为 false。
```

few-shot（建议固定附带）：

- "西乡段泛油有多少处" + `... routes_id='1b0b..'(32位hex) AND damage_type='1101'` → 槽位 route_id(32位hex按路线实体)、damage_type；templatizable=true。
- "全库的病害有哪些" + `SELECT * FROM ai_problem WHERE del_flag=0 LIMIT 500` → 无业务过滤 → templatizable=false（全表明细，走通用算子）。
- "西乡段收藏但没转工单的病害" + `collect='1' AND problem_status='0'` → 两个 enum 槽位；templatizable=true。
- "西乡段新老病害哪个多（按采集时间中位数分）" + 含 `PERCENTILE_CONT/中位数 CASE` → 动态分界 → templatizable=false（保留算子）。

---

# 附录 B：`prompts/example_direct_match.md`（L2 同问打分，可直接落盘）

```markdown
<!-- L2 示例直达：判断用户问题能否直接复用某条"已验证标准问法"的参数化 SQL。
     只做判定与槽位对齐，严禁写 SQL、严禁回答问题；拿不准就给低分。 -->
[system]
你是道路巡检数据问答系统的"查询复用判定器"。给你 1 个用户问题和若干"已验证标准问法"（各绑定一条参数化 SQL）。
唯一任务：判断用户问题能否用某条标准问法的【同一条 SQL（仅参数取值不同）】直接回答。
不是看"话题像不像"，而是看"查询是否同构"；话题相同但查法不同，一律不可复用。

逐项核对 7 要素，任一结构性要素不一致则 same_query=false：
1. 查询形态：计数/明细/分布分组/多对象对比/TopN/时间趋势/占比/最大最小均值，是否一致。
2. 查询主体：病害/巡检任务/路线/路段/资产/工单/GPS 轨迹，是否一致。
3. 实体范围：路线、路段、桩号区间是否一致（仅取值不同算一致）。
4. 分类过滤：病害类型/大类/上下行/来源/工单状态/收藏等维度是否一致；多一个或少一个过滤都算不一致。
5. 数值条件：面积/置信度/长度等阈值与比较符（>,<,=,between,TopN 的 N）；数值不同算换参数，维度或比较符不同算不一致。
6. 时间范围：月份/日期区间/相对时间，仅取值不同算换参数。
7. 分组维度与输出列：按什么分组、返回哪些指标列；"各类型数量"对"总数"算不一致。

打分（0~1 两位小数）：
- 0.95~1.00 同一查询仅换参数值，且必需参数都能从用户问题明确取到；
- 0.85~0.94 形态相同但有需谨慎的差异（多了可选过滤、时间口径需推断、说法简略）；
- 0~0.84 不是同一条查询（形态/维度/条件/主体不同，或需多步/知识/看图）。

槽位对齐（score≥0.85 才做）：route(路线原文，不要自行翻 id)、damage_type、time_range、up_or_down、
category、problem_status、problem_source、collect、area_op+area_value、confidence_threshold、
pile_range、top_n。必需槽取不到值→写入 missing_slots 且 same_query=false、ready=false；
用户多出 SQL 表达不了的条件→写入 differences 且 same_query=false。

硬性要求：宁缺毋滥；看图/照片/轨迹、养护规范与处置建议、先查数再结合规范分析、一句话含两个以上独立查询，
全部 ready=false。只输出 JSON，不解释、不写 SQL、不回答问题。

输出 JSON：
{
  "matches": [
    {"candidate_id":"", "score":0.0, "same_query":true,
     "shape":"count|list|distribution|comparison|topn|trend|ratio|aggregate|other",
     "reason":"", "slots":{}, "missing_slots":[], "differences":[]}
  ],
  "verdict": {"best_id":"", "ready":false}
}

[user]
用户问题：
<<question>>

候选标准问法（含 id、标准问题、形态、分组/指标/过滤、所需槽位、参数化 SQL 预览）：
<<candidates>>

逐条核对 7 要素后输出 JSON；ready=true 只允许"同一条 SQL、仅换参数值、必需槽位齐全"。
```

few-shot：

- "恒明珠段 7 月泛油有多少处" 对 "某路线某月某病害类型数量"（槽 route/damage_type/time）→ 0.97、same=true。
- "西乡段各病害类型分别多少处" 对 "西乡段泛油有多少处" → 0.30、same=false（分布对单计数）。
- "西乡段面积>5、置信度<0.7 且没图的病害明细" 对 "西乡段泛油明细" → 0.40、same=false（多三个过滤且形态不同）。
- "西乡段路况好不好、该怎么养护" 对 任意查数问法 → ready=false（数据+规范复合分析）。

---

# 附录 C：复杂 SQL 自动模板化端到端示例 + sqlglot 说明

## C.1 依赖准备

- 已验证 `backend/.venv` 内可直接 `import sqlglot`（当前 **30.18.0**）。但仓库根 / backend 下未找到标准依赖清单（requirements.txt / pyproject.toml），落地时**必须把 `sqlglot` 显式登记进依赖文件并锁版本**，避免换环境 / 部署时缺失。
- 解析 MySQL / PostgreSQL 时分别传 `read="mysql"` / `read="postgres"`，与模板记录里的 `kind/dialect` 对齐。

## C.2 sqlglot 是什么、为什么不用正则

sqlglot 是纯 Python 的 SQL **解析 / 转译 / 改写**库：把 SQL 字符串解析成抽象语法树（AST），可以遍历、定位"表 / 列 / WHERE 谓词 / 聚合 / GROUP BY / LIMIT"，把某个字面量替换成占位后再重新生成 SQL，还能在 MySQL、PostgreSQL 等方言间转译。

现有 `_normalize_sql_for_example` 用 `str.replace` 换值，在复杂 SQL 上很脆：同一值在子查询里出现多次会误换、值和别的列撞车、大小写 / 换行 / 别名不同就漏换，也认不出 IN / BETWEEN / IS NULL / JSON 表达式。AST 能精确知道"这是 WHERE 里第几个谓词、左值是哪张表的哪一列、右值是字面量还是另一个列、比较符是什么"，所以**列驱动的槽位识别必须基于 AST，不允许用字符串替换**。

## C.3 原始成功问答（trusted）

问题："查一下西乡段 2026 年 7 月、上行方向，面积大于 5 ㎡、AI 置信度低于 0.7 的病害，按病害类型分组，统计每种类型的处数和总面积，按处数取前 5 种。"

```sql
SELECT b.problem_type AS 病害类型,
       COUNT(*)             AS 处数,
       ROUND(SUM(p.area),2) AS 总面积
FROM ai_problem p
JOIN basic_facilities_routes r
     ON p.routes_id = r.id AND r.del_flag = 0
LEFT JOIN (SELECT DISTINCT problem_code, problem_type, disease_categories_type
           FROM ai_problem_base WHERE del_flag = 0) b
     ON p.damage_type = b.problem_code
WHERE p.del_flag = 0
  AND r.id = '1b0b4a9f700477b1d96119caac87c168'
  AND p.up_or_down = '1'
  AND p.collection_time >= '2026-07-01 00:00:00'
  AND p.collection_time <  '2026-08-01 00:00:00'
  AND p.area > 5
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position,'$.confidence')) AS DECIMAL(10,6)) < 0.7
GROUP BY b.problem_type
ORDER BY 处数 DESC
LIMIT 5;
```

## C.4 预检 + AST 抽骨架（只认结构，不碰值）

- 预检：单条只读 SELECT、表全在白名单、无动态分档 / 自由文本 / 写操作 → 可挖。
- sqlglot 解析得到：shape=`distribution`（含 LIMIT+ORDER BY 计数，细分 `topn`）；subject=`disease`；JOIN 集合=`[routes, ai_problem_base 字典]`；GROUP BY=`b.problem_type`；metrics=`[count, total_area]`；排序=`count DESC`。
- **GROUP BY 列、聚合指标、ORDER BY 指标、JOIN 全部算结构，第一期不开槽**（防止"按类型分布"被套成"按上下行分布"）。

## C.5 谓词分类（列驱动）

| 谓词 | 左值 | 判定 | 处理 |
|---|---|---|---|
| `p.del_flag=0`、两处 `r.del_flag=0`、字典子查询 `del_flag=0` | del_flag | 固定常量 | `fixed`，不挖 |
| `r.id='1b0b…(32位hex)'` | routes_id | entity | `{route_id}`，下拉连库现查 |
| `p.up_or_down='1'` | up_or_down | enum | `{up_or_down}`，中文"上行"→code `1` |
| `collection_time >= … AND … < …` | collection_time | time | 用户可见槽为"月份"，后端把 `2026-07` 派生为 `{time_start}/{time_end}` |
| `p.area > 5` | area | numeric | 拆两槽：`{area_op}`=`>`、`{area_value}`=`5` |
| `CAST(JSON_EXTRACT(pic_position,'$.confidence')…) < 0.7` | confidence（固定 JSON 表达式） | numeric | 表达式写死，只开 `{conf_op}`=`<`、`{conf_value}`=`0.7` |
| `LIMIT 5` | — | int | `{top_n}`，白名单 1..50 |

要点：比较符与数值**分开存**（"大于 5 / 大于 10"才是同模板换值）；`{area_op}` 由后端从白名单 `{>,>=,<,<=,=,between}` 映射成固定 SQL 片段，`between` 取两个值，**不接受用户自由拼接比较符**；confidence 的整段 JSON 提取作为"列定义"固定，只暴露阈值。

## C.6 参数化 SQL 与 match 声明

```sql
SELECT b.problem_type AS 病害类型,
       COUNT(*) AS 处数,
       ROUND(SUM(p.area),2) AS 总面积
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id AND r.del_flag = 0
LEFT JOIN (SELECT DISTINCT problem_code, problem_type, disease_categories_type
           FROM ai_problem_base WHERE del_flag=0) b
     ON p.damage_type = b.problem_code
WHERE p.del_flag = 0
  AND r.id = '{route_id}'
  AND p.up_or_down = '{up_or_down}'
  AND p.collection_time >= '{time_start}' AND p.collection_time < '{time_end}'
  AND p.area {area_op} {area_value}
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position,'$.confidence')) AS DECIMAL(10,6))
      {conf_op} {conf_value}
GROUP BY b.problem_type
ORDER BY 处数 DESC
LIMIT {top_n};
```

```json
"match": {"shape": ["distribution","topn"], "subject": ["disease"],
          "group_by": ["problem_type"], "metrics": ["count","total_area"],
          "slots": ["route","up_or_down","time_range","num_area","num_confidence","top_n"],
          "fixed": {"del_flag": "0"}}
```

## C.7 指纹与跨样本确认（candidate → mature）

- 值全部抹成占位、AST 规范化后取哈希作为指纹。
- 第一次见只有一条 → `status=candidate`，只进预置维护页"待确认"区，**不进对话推荐、不弹卡**。
- 之后又成功一条"恒明珠段 6 月、下行、面积大于 10、置信度低于 0.8、按类型取前 3"，指纹相同；逐位置比对：route / up_or_down / 月份 / area 的 op 与值 / confidence 值 / top_n **都出现过不同值 → confirmed 槽**；GROUP BY、metrics、JOIN 始终不变 → 留在结构。自动升级 `mature`，进入推荐。

## C.8 回环验证

用第一条原始值（西乡段 id / 1 / 7 月边界 / >5 / <0.7 / 5）回填：先比 AST 与原 SQL 结构等价，再实际执行比对每组处数与总面积；一致才入库，不一致丢弃且只留普通示例。

## C.9 运行时命中

新问题"恒明珠段 6 月下行、面积大于 10、置信度低于 0.8，按病害类型处数排前 3"：shape / group_by / metrics 一致，6 个过滤条件恰好被模板 slots 全覆盖 → 成为推荐候选，弹卡显示"模板名 + 参数（路线=恒明珠、月份=2026-06、方向=下行、面积>10、置信度<0.8、前 3）+ 可折叠 SQL"；用户点「确认查询」后，中文 / 月份被解析成白名单 id、code、时间边界与数值，绑定实时执行，全程不调写 SQL 模型。

## C.10 三个边界（为什么不会乱套）

1. 改成"按上下行分组" → group_by 不同、指纹不同 → **不命中**，走别的模板或 L3。
2. 多了"且没采集图片" → 本模板无 `has_image` 槽、覆盖不全 → **不命中**（不悄悄丢条件）。
3. 少了"面积"条件 → 第一期模板槽**全必填**，不做"可选 WHERE 块动态拼接"；"不带面积过滤"是另一个更简单指纹、另一条模板。模板里不藏分支逻辑。

## C.11 这套逻辑多少靠代码、多少靠语义模型

| 环节 | 靠什么 |
|---|---|
| 预检（只读 / 表白名单 / 动态分档 / 自由文本 / 一次性主键） | **代码**（规则 + schema） |
| AST 解析，抽 shape / subject / JOIN / GROUP BY / 指标 | **代码**（sqlglot） |
| 谓词归类（路线、枚举、时间、数值、IS NULL、LIMIT、IN） | **代码为主**（`schema_catalog` + 枚举映射），覆盖常见绝大多数 |
| op/value 拆分、占位生成、JOIN 原样保留 | **代码** |
| 结构指纹、跨样本聚类、candidate/mature 状态机 | **代码** |
| 回环验证（AST 等价 + 实跑结果比对） | **代码** |
| 安全（白名单 / 强类型 / 转义 / 只读 / LIMIT / kind 锁定） | **代码** |
| 规则认不出的列或表达式（自定义函数、特殊 CASE、JSON 其他字段）归类、判断是否根本不可模板化 | **便宜模型兜底 1 次**，结论仍须过代码规则与回环验证 |
| 模板起人类可读标题、复核 shape | **便宜模型** |
| 本次问答到底用不用这条模板 | **用户在确认卡点确认**（最高原则，代码与模型都不能代决） |

结论：**默认零 LLM，约九成槽位由"sqlglot + 字段目录 + 枚举映射"确定性挖出**；语义模型只在长尾认不出时兜底，且不允许改 SQL、不决定采用；离线挖错的代价由"跨样本证据 + 回环验证 + 用户确认"三重挡住。
