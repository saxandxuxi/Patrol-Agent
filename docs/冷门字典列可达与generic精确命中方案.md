# 冷门字典列（如 ai_problem_base.formula）查不到的整改（给 codex）

## 一、现象
问"病害的计算公式有哪些"，38.9 秒后报错：
"生成 SQL 缺少必须条件：用户点名的字段 area；用户点名的字段 confidence"。
连库核实：`ai_problem_base.formula` 列注释就是"计算公式"，非空 distinct 值只有 6 个——这题本该直接列出这 6 个公式。

## 二、根因链（连库 + 跑 ground() 实测）
实测 `ground("mysql","病害的计算公式有哪些")`：
- 字段目录没问题：`ai_problem_base` 在 17 张白名单表里，`formula` 列在目录里、label 就是"计算公式"；
- 但 ground() 命中的列**全是 ai_problem 主表**的（病害采集时间/病害长度/病害经纬度…），formula 一个都没出现。

真因（两层）：
1. **表级落点被通用词带偏**：问题里"病害"两个字，分词后主表 ai_problem 一堆列注释带"病害"（病害采集时间…）都拿分，落点表被锁成 ai_problem；随后 field_grounding.py:171-172"落点表确定后只保留这张表的列"，字典表 ai_problem_base 的 formula 列被整个滤掉。formula 明明被"计算公式"列名硬命中（该 +3 分），却救不回来。
2. **validate_semantics 误判**：语义层把它当主表问题，把主表默认列 area、confidence 塞进 requested_fields，硬要求 SQL 必须 SELECT 出 area、confidence（nl2sql.py:4174-4240）——这题查字典表 formula，根本不该带主表列，校验直接卡死报"缺少 area、confidence"。

Wren 没命中这个冷门列，generic 又弃权，就卡这了。

## 二补、ground() 的修法（让列级硬命中压过表级通用词）
- 现在 phrase_hit（"计算公式"整段出现在问题里）只 +3.0，压不过表级"病害"通用词把落点锁到主表。
- 改成：**某列被列名级硬命中（phrase_hit）时，它所属的表优先作为落点**——用户点了"计算公式"这种具体列名，比他顺嘴带的"病害"通用词更能说明落点。即 phrase_hit 命中列的 table 优先级高于表级通用词重合。
- 与 subject_table()（199-222）的关系：那边是"统计题别把字典当主体"（西乡段各类型多少处，主体是病害）；这里要加反向例外——**当用户问的就是字典列取值（"计算公式有哪些"），列级硬命中字典列时，落点就该是字典表**，两个规则按"有没有统计/分布诉求"区分。
- 空字典列也照常：落点 ai_problem_base、查 formula distinct，0 或 6 行由 SQL 返回，不转 knowledge。

## 三、整改（让 generic 命中更准，不放宽总量、不挤掉别的）

### 1. 【直接死因】"用户点名字段"必须是用户原话真出现的列
validate_semantics 的 requested_fields 不能用语义层默认带的主表列（area/confidence）。
改成：只把**用户 message 里真解析出的列**（named_columns 从原话解析）当成必须 SELECT 的列；用户没说"面积/置信度"就不强求。
（nl2sql.py:4174 注释已写"判据是用户的话"，但实际仍被语义层默认列带偏——按注释修齐。）

### 2. 【generic 新增高精度小入口：字典列 distinct】
generic 现在"解析不了就弃权"，但漏了一类最简单的题：**用户问某列有哪些取值**。
- 识别特征：用户问"X 有哪些 / 有几种 / 是什么 / 分别是哪些"，且 X 能在 schema 列注释里**唯一**定位到一列（"计算公式"→ai_problem_base.formula、"病害类型"→problem_type、"损坏程度"→damage_type…）；
- 这种题没有过滤条件，generic 直接 `SELECT DISTINCT 该列 FROM 该表 WHERE 该列 IS NOT NULL AND 该列<>''` 列出来；
- 一个词对应多列、或注释匹配不上 → 弃权交 Wren，不硬接。
精度极高（一对一注释匹配），不会挤掉别的题；generic 仍不占推荐名额，只在 Wren 没命中时兜这种题。

### 3. 【冷门列自动可达，不靠手写】
schema_catalog 已经从列注释建目录（_LazyFieldTable），formula="计算公式"本就在目录里。第 2 步直接复用目录，不写字段映射。
以后 ai_problem_base 其他冷门列、或别的表有数据后，只要列注释清楚，自动可达，不用改代码。

## 四、不做
- 不放宽 generic 总门槛（避免误答、挤掉别的）；
- generic 仍不进推荐名额排序；
- 不改现有 38 条内置模板。

## 五、验收
1. "病害的计算公式有哪些" → 直接列出 6 个非空公式，不再报"缺少 area/confidence"；
2. "病害类型有哪些""损坏程度有哪些"等同类字典列题都通；
3. 现有 38 条模板自匹配、确认卡候选排序不变；
4. 注释对不上/一词多列的题仍弃权交 Wren，不硬接。

## 六、改动文件
- `backend/app/services/nl2sql.py`：validate_semantics 的 requested_fields 只取用户原话真解析出的列（4174-4240）；
- `backend/app/services/field_grounding.py`：phrase_hit 列级硬命中的表优先作为落点，压过表级通用词；与 subject_table() 补"问字典列取值时落点就是字典表"的反向例外（171-172、199-222）；
- `backend/app/services/generic.py`：新增"字典列 distinct"入口，复用 schema_catalog 注释匹配；
- 不改推荐排序与模板白名单。

---

## 七、核实与实施记录（2026-09-23）

### 核实结论：方案说的现象与两层真因**都成立**（逐条实测）

| 方案的说法 | 核实结果 |
|---|---|
| `ai_problem_base.formula` 非空 distinct 只有 6 个 | ✅ 连库实测：`COUNT(*)=97`、`COUNT(DISTINCT formula)=6`、空值 0 |
| `ground()` 命中的列全是 ai_problem 主表、formula 一个不出现 | ✅ 实测 `ground("mysql","病害的计算公式有哪些")` → `table=ai_problem`，命中列全是 `列注释：病害` 的主表列；单独问"计算公式"时 `ai_problem_base.formula` 能拿 10.5 分 |
| validate_semantics 被语义层默认列 area/confidence 卡死 | ✅ 实测 `requested_fields = ['formula','problem_type_name','area','confidence']`——后三个来自 `plan.slots.semantic_fields`（语义层顺手猜的主表列） |
| 表级通用词压过列级硬命中 | ✅ 表级落点是"表名+表注释"的词元重合算出来的，"病害"命中 ai_problem 表注释 → 落点锁死 → 后面 `if top_table: scored = [只保留该表的列]` 把 formula 整个滤掉 |

一处**要修正**：方案 §三.2 说"用 `named_columns`（注释匹配）定位列"——不能直接用。`named_columns` 走的是
**Wren 模型侧注释**（`wren_engine._schema_index`），和库里列注释不一定一致：实测
`ai_problem.damage_type` 的**库注释**是"损坏程度"，模型侧对不上，于是"损坏程度有哪些"一个列都命不中。
所以第 2 步我改成**直接用 `schema_catalog` 字段目录**（也就是方案 §三.3 想要的"复用目录"）。

### 实施（三处）

1. `field_grounding.ground(kind, text, plan=None)`：**列级硬命中（列注释整段出现在问题里）的表优先当落点**，
   压过表级通用词；统计/分组题（shape=count/distribution/… 或带 group_by）除外——"各病害类型分别有多少处"
   里的"病害类型"是分组维度，主体仍是病害，不翻转。调用点 `nl2sql.py:1255` 传 `plan=plan`。
2. `validate_semantics`：语义层"顺手猜的字段"（`semantic_only`）**只有落在本轮落点表上时才算硬要求**；
   落点翻到字典表后，主表默认列 area/confidence 不再逼 SQL 必须 SELECT。
   （比方案 §三.1 的"只留用户原话解析出的列"更窄：保留了 F 题那类"用户没说、但语义层认出冷门字段"的约束。）
3. `capabilities`：新增**字典列取值清单**候选（`generic_distinct:<表>`），判据是
   "shape ∈ {list, detail} + 无过滤条件 + 字段目录里注释最长的那一列唯一命中（其余命中是它的子串算泛词噪声）"，
   SQL 复用 `generic.distinct_values`（`SELECT 取值, COUNT(*) … GROUP BY 取值`）。一词多列/注释对不上 → 弃权。

### 验收（都是实测）

```
病害的计算公式有哪些     → 卡片里出现 generic_distinct:ai_problem_base，执行返回 6 行（6 个非空公式）
病害类型有哪些          → generic_distinct → ai_problem_base.problem_type
损坏程度有哪些          → generic_distinct → ai_problem.damage_type（改用字段目录后才命中）
有哪些东西              → 弃权（无注释命中）
端到端 handle_message("病害的计算公式有哪些")：12.1s、不再报"缺少 area/confidence"，卡片三选一
内置模板抽查 6/6 通过（table_count / route_info / per_km / coverage_compare / other_routes）
mature 模板自匹配 37/37 通过；快捷查询可点查 37 条不变
```

### 遗留

- ~~字典列取值清单只覆盖"无过滤条件"~~ **已做**：distinct 入口接上了 `_generic_filters`，
  「宝安大道西乡段的损坏程度有哪些？」→ `WHERE m.routes_id = '…'`（实测执行 2 行）；条件表达不了就弃权。
- ~~`subject_table()` 的字典排除让 `generic_detail` 落到 ai_problem~~ **已做**：落点就是该字典表、
  且不是统计/分组题时，字典表就是主体（`病害的计算公式有哪些` → `ai_problem_base`）。

### 追加：对象表护栏 + 维度名落点（2026-09-23 第二轮）

排查"继续做"时发现的**错答**（比遗留更严重）：

```
各条路线分别有多少座桥梁？   → 语义 subject=disease、grounded=basic_facilities_bridges
   generic_metric 模板照旧用 ai_problem 拼出 "按路线数病害" ✗（错的答案，不是没答案）
```

三处改动：

1. `_build_generic_metric_template` 加**对象表护栏**：`field_grounding.subject_table(plan)` 指向别的业务对象表
   （桥梁/隧道/边坡/巡查资产/工单）时直接弃权——它是病害/桩点/路段专用构造器，拿主表作答就是错答。
   （用 `subject_table()` 而不是 `grounded.table`：前者已经处理"统计题别把字典当主体"，不会误伤"各病害类型分别多少处"。）
2. `generic.group_distribution` 支持**外键维度**：外键列 JOIN 目标表、按名称列分组，
   不再把 `routes_id` 这种 UUID 当类别摆给用户。
3. `capabilities._generic_candidates`：分组维度是**维度名**（route/section）而非列名时，
   按关系链落到这张表真正的那个外键列上（这不是"拿别的列顶替"，找不到仍然弃权）。

实测：

```
各条路线分别有多少座桥梁？ → generic_dist:basic_facilities_bridges:routes_id
    SQL: SELECT j0.short_name AS 类别, COUNT(*) … JOIN basic_facilities_routes j0 …（桥梁表为空 → 0 行，符合"空表如实回答"）
各条路线分别有多少个巡查资产？ → generic_dist:ai_asset_main:route_id（同样按路线名分组）
各条路线分别有多少个工单？   → 工单表没有路线关系链 → 弃权交 Wren（不硬接）
各病害类型分别有多少处？     → 仍走 generic_metric（未受影响）
回归：mature 模板自匹配 37/37、内置模板抽查 6/6、快捷查询 37 条不变、app.main 可导入
```
