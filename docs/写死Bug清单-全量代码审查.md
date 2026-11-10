# 写死 Bug 清单 —— 全量代码审查

> 审查范围：`backend/app` 下全部 36 个 Python 文件（services 13 + routers 9 + 根 3 + 其余）
> 审查方式：逐文件全量通读 + 关键词交叉核对
> 结论性质：**只审查、不改代码**。以下所有"写死点"按严重程度分级，供 Codex 统一修复。
> 关联文档：[长对话Bug分析-LD1两边加起来.md](./长对话Bug分析-LD1两边加起来.md) —— LD1 的 6 个根因环节里，环节①②③⑤ 本质就是本清单里的"写死词表 / 只查缺不查多"问题，本文档是它的全量展开版。

---

## 0. 总览

| 级别 | 数量 | 说明 |
|---|---|---|
| 🔴 P0 架构级 | 6 | 配置与代码口径不一致、库名/表名/URL 写死、白名单写死 |
| 🟠 P1 功能级 | 22 | 词表缺词导致答错、口径写死、模板 hack、校验不全 |
| 🟡 P2 维护级 | 30+ | 文案写死、常量写死、映射表写死、与 config 不联动 |

**病根一句话**：这个项目的"智能"是**堆规则堆出来的**，几乎所有规则（词表、映射、口径、列名）都硬编码在代码里，且**没有一条统一的数据流**——config 里定义了一份，intent.py / agent.py / nl2sql.py / cross_db.py / orchestrator.py 又各自写死一份，新增一个病害类型 / 数据集 / 表 / 比较词，要同时改 3~5 个文件，漏一个就出"看着对、其实错"的 bug。这就是"发现一个问题修一个模板"永远修不完的根本原因。

---

## 1. 🔴 P0 架构级写死

### 1.1 SeaweedFS URL 两处口径矛盾（最严重）

- `backend/app/services/db.py` 共 **4 处**写死 `http://127.0.0.1:28888`（分布在 4 个函数里）：
  - L155：`_buckets()` 列桶请求
  - L193：`fetch_seaweedfs()` 取文件
  - L207：`check_seaweedfs()` 探测文件
  - L222：`list_seaweedfs()` 列目录
- 而 `config.py` L57 定义了 `seaweedfs_base_url = "http://10.168.210.78:28888"`，**db.py 从头到尾没引用过它**。
- **后果**：代码实际依赖"本机 SSH 隧道把 28888 转发到 127.0.0.1"这个隐含前提。一旦换机器、换部署方式，改 config 里的 `seaweedfs_base_url` 是**无效的**（没人读），必须改 db.py 里 5 处字面量。`/api/storage`、`/api/image`、`_image_result` 全部受影响。

### 1.2 数据库连接绕过端口直连、写死 docker 容器名

- `db.py` L64：`docker exec -e MYSQL_PWD={password} mysql mysql -h127.0.0.1 -P{port} ...`
- `db.py` L76：`docker exec -e PGPASSWORD={password} postgres psql ...`
- **两处写死**：
  1. 容器名写死 `mysql` / `postgres`（服务器上重建容器、改名字就挂）；
  2. 连接方式写死"docker exec 进容器再连 127.0.0.1"，而 MySQL 3307 / PostgreSQL 5436 明明直接监听宿主端口——**ssh 隧道一旦断开，所有查询全挂，且无法用隧道之外的方式直连**。换云上托管数据库（无 docker exec）整个 db.py 不可用。

### 1.3 MySQL 库名写死在检索标记里

- `nl2sql.py` L1546-1550：
  ```python
  _DB_MARKERS = {
      "mysql": ("mysql", "new_four_road_base_ai"),
      "postgresql": ("postgresql", "gpsdb"),
      "seaweedfs": ("seaweedfs",),
  }
  ```
- `new_four_road_base_ai`、`gpsdb` 写死在字段字典检索的过滤逻辑里。换库名（比如测试库）就要改代码。

### 1.4 表白名单写死（新增表必须改代码）

- `config.py` L111-129：`mysql_allowed_tables` 17 张表。
- `config.py` L131-134：`pg_allowed_tables` 只有 2 张（`gps_points`、`inclination_points`）。
- `validate_sql`（sql_engine.py L105-108）依赖它做安全校验，**但这是"名单式"而不是"排除式"**：库里实际 200+ 张表（new_four_road_base_ai 下光业务表就 100+），用户问一张没进名单的表（如 `uav_patrol`、`maintenance_contract`、`basic_facilities_slope` 之外的新表），SQL 直接判"不在白名单"，而不是"支持任何只读 SELECT + 危险词拦截"。边界写死 = 覆盖不全。

### 1.5 数据集名多处各自写死，与 config 不联动

config.py L136-144 定义 `knowledge_datasets` 7 项，但检索/问答代码**没有统一从 config 读**：
- `agent.py` L108-113 `_knowledge_datasets()`：写死 `["养护行业规范"]`，计量词叠加 `"工程量计算文件"`；
- `intent.py` L139-146 `knowledge_datasets_for_intent()`：写死 4 个数据集名（`养护行业规范` / `mysql语句知识` / `postgresql语句知识` / `seaweedfs语句知识`）；
- `nl2sql.py` L1687-1691 `_SQL_DATASET_BY_KIND`：写死 3 个语法数据集名；
- `agent.py` L63：`use_kg` 判断用 `settings.ragflow_graphrag_datasets`（config 有 3 个），与上述写死名单是**三套并存**。
- **后果**：新增一个数据集（如"养护定额库"）要改 config + agent.py + intent.py + nl2sql.py 四处，漏一处就"知识库里有但检索不到"。

### 1.6 CORS 配置违规

- `main.py` L14-18：`allow_origins=["*"]` 与 `allow_credentials=True` 并存。浏览器规范**禁止** credentials 与通配符来源组合，前端只要带 cookie/凭证的请求会被浏览器直接拦截。要么去掉 credentials，要么列具体 origin。

---

## 2. 🟠 P1 功能级写死（直接导致答错）

### 2.1 比较词表缺"加起来/合计/总和/两边"（LD1 环节③）

- `nl2sql.py` `_COMPARISON_HINTS` 只有约 10 个比较词（含"对比、哪个多、分别…"），**没有"加起来 / 合计 / 总和 / 两边 / 总共 / 一共"**。
- 这就是"宝安大道西乡段 + 恒明珠段 + 两边加起来"被答成 911/0 的环节③根因：模板和 `is_comparison` 都识别不出"加起来"语义，走了对比分支，只输出一个单值。
- 修复方向：不是加一个词，而是把"求和/汇总类词"和"比较类词"分成**两组独立的语义槽**（sum_hint vs compare_hint）。

### 2.2 validate_semantics 只查"缺"不查"多"（LD1 环节⑤）

- `nl2sql.py` L1465-1543：`validate_semantics` 只检查 SQL **少了**哪些必须条件（缺路线 JOIN、缺桩号 BETWEEN、缺时间列…）。
- **从不检查 SQL 多了不该有的条件**。多轮继承时，上一轮的"泛油"条件被拼进新问题（而新问题其实是别的对象），SQL 会多一个 `problem_type IN ('泛油')`，校验器看不出来，结果答 0 或答错主体。
- 修复方向：校验器增加"多余条件检测"——`plan["slots"]` 里没有的过滤词（尤其是继承来的 damage/route/pile），如果在 SQL 里出现且不是用户本句要求的，应报警。

### 2.3 generate_sql 提示词编号重复（两条 "10."）

- `sql_engine.py` L167-168：
  ```
  10. If joining ai_problem_base, use (SELECT DISTINCT problem_code, problem_type ...) b ...
  10. Fix the previous issue if provided: {feedback}
  ```
- 编号从 10 直接跳了两条 10，后面 11-15 顺延。模型读到两个 "10." 容易混淆顺序，且 14 条规则里只有 8.1/8.2 是中文，其余英文，混排。

### 2.4 桩号口径写死

- `parsers.py` L47-48：`parse_pile_range` **单桩号默认向后查 999 米**。"K1+000 附近""K1+000 以内"会被当成 K1+000~K1+999 整段。
- `nl2sql.py` L1247-1275 `_build_spatial_split_template`：写死"前 N 米 = [0, N)、后 N 米 = [N, 2N)"，**从路线起点算**。如果用户说"K1+000 往前 500 米"这种带基准桩号的，模板不认（`parse_spatial_split` 只认"前 N 米"纯词）。
- `parsers.py` L157-166 `parse_pile_side`：只认 `K\d+(+\d+)?(以东西)`，"K1 之前 / K1 之后 / K1 以北"全不认。

### 2.5 病害词表与真实字典不联动

- `parsers.py` L260-263 `_DAMAGE_TOKENS` 正则**写死 17 个病害词**（龟裂/网状裂缝/横向裂缝/纵向裂缝/裂缝/坑槽/坑洞/车辙/沉陷/波浪拥包/拥包/松散/泛油/标线缺损/块状修补/路面积水/剥落/露骨/修补——实际 19 个）。
- 真实字典在 MySQL `ai_problem_base` 表（42 类，含"防眩板缺损""声屏障缺损""减速带损坏""边坡落石""路框差"等），**与这个正则完全独立**。
- 后果：
  - 字典里存在、但正则没有的词（如"防眩板缺损"）→ `unknown_damage_token` 不会误报，OK；
  - 字典里不存在、但正则有的词（如"龟裂"）→ 报"病害字典里没有'龟裂'"。这个行为本身合理，**但字典词源应该是数据库，而不是代码里的正则**——字典增删类型要同步改正则，迟早漏。

### 2.6 病害/巡查/资产字典的 code→名称映射写死

- `nl2sql.py` L1165-1169 `_PATROL_TYPE_CASE`：`0/5/10/15/20 → 定期巡查/日常巡查/专项巡查/人工复核/重点巡查` 写死在 SQL 的 CASE WHEN 里；
- `nl2sql.py` L552-557：`ai_problem_base.group_labels` `0/5/10/15 → 路面病害/路基病害/沿路设施/交安事件` 写死；
- `nl2sql.py` L570：`ai_asset_base` 注释写明"字段注释说 0 条状/1 点状，实际值是 10/20/30/40/60/70/80 七组"——**连数据字典的注释都是错的**，而代码里 `group_labels: {}` 只能裸输出数字。
- 这些字典值（代码/名称/大类）都该从 `ai_problem_base` / `ai_asset_base` / 字典表动态读，写死 = 字典一变就错。

### 2.7 多指标模板的"别名字符串替换" hack

- `nl2sql.py` L1010-1032 `_build_multi_metric_template` 的类型分布子查询：
  ```python
  for source, target in (("p.", "p2."), ("r.", "r2.")):
      sub_joins = sub_joins.replace(source, target)
  ```
- 用字符串替换把 `p.` → `p2.`、`r.` → `r2.`。**一旦 JOIN 链里出现别的别名（比如将来加 `s.` sections 或 `w.` 工单），替换会漏或误伤**。这是纯字符串 hack，不是结构化 AST 处理，极其脆弱。

### 2.8 "桥"字误命中病害字典的 hack

- `nl2sql.py` L609-611：`_build_table_count_template` 注释自曝"「桥梁」里的'桥'会被病害字典的模糊匹配当成病害类型"，靠 `plan["slots"].pop("damage_matches")` 补救。
- 这是"打补丁"式修法：病害类型的模糊匹配本身就该用**最长词优先 + 完整词边界**，而不是事后 pop。

### 2.9 巡检/工单等非病害主体的模板缺口

- `_SUBJECT_SOURCES`（nl2sql.py L448-461）只支持 3 个主体（disease/pile_point/section），但 `SUBJECT_MAIN_TABLE`（parsers.py L296-302）声明了 5 个（还有 patrol/work_order）。
- `_build_patrol_top_template` / `_build_patrol_group_template` 单独硬编了 2 个巡查模板，但**工单主体没有模板**，`build_template_sql` L1390 直接 `return None` 交给 LLM——LLM 又容易答错（用户已实测"几次巡检"答成病害数）。

### 2.10 时间解析缺"上个月/去年/今年"

- `parsers.py parse_time_range`（L187-246）支持：今天/昨天/最近N天/具体日/年月/裸月/年份。
- **不支持"上个月""上上周""去年""今年""上季度"**，这些问法全部静默解析失败 → 变成全库查询。

### 2.11 "平均每天"被 orchestrator 特判成按月

- `orchestrator.py` 注释自曝"每天都会被按天分组误判"，于是加特判：出现"平均每天/日均"强制按月维度。
- 这是**为单个测试题打的补丁**：如果用户真的想按天看"7月每天的平均值"，会被强制按月。语义上"平均每天"到底按天还是按月应取决于上下文（通常按月日均，但"这个月每天各多少"就是按天），写死必错一侧。

### 2.12 LLM 对话里的"K100+K200"单一样例写死

- `sql_engine.py` L159：`4. K100+K200 means pile_distance values 100000 and 200000.`
- 只教了这一个桩号样例格式，没教 `K1+000~K3+000` 区间、`K4+765` 等格式；模型看到 `K1+000至K3+000` 时可能把 + 号理解为拼接运算符。

### 2.13 连接/查询强依赖 SSH 隧道（隐含写死）

- 见 1.2。`config.py` 有 `ssh_host/ssh_port/ssh_user/ssh_password`（L39-42）说明设计上承认走 SSH 隧道，但 `db.py` 直接 `docker exec` 而非显式建隧道、再连宿主端口。隧道断了无重连、无提示（超时 120s），用户体验是"卡死"。

### 2.14 多轮上下文"继承"词表写死

- `memory.py`：
  - L12 `POINTING_WORDS` 10 词；
  - L15-28 `_FOLLOWUP_ASKS` 11 词；L30 `_GLOBAL_HINTS` 6 词；
  - L84 `_FRESH_QUESTION_TERMS` 8 词；L95-106 `_CORRECTION_HINTS` 10 词；L109-126 `_FRESH_TOPIC_HINTS` 16 个完整短语；
  - **长度阈值写死**：L39 `len(text) > 30`、L90 `> 40`、L137 `> 60`。
- "两边加起来呢？"这种 6 字短句能命中 followup，但"西乡段和恒明珠段两边加起来一共有多少处病害"（>30 字）会被判成新问题 → 不继承路线 → 全库查。LD1 场景的 R3 正是这种长句。

### 2.15 摘要渲染写死列名

- `agent.py` L467-487 `_summarize_result`：写死 `avg_metric` / `above_avg_count` / `total_count` / `ratio_percent` / `part_count` / `first_time` / `last_time` / `unknown_type_count` / `no_direction_count` / `no_section_attr_count` 等列名分支。
- `agent.py` L484：`aggregate_keys = ("avg_area","average_area","max_area","min_area","total_area","avg","max","min","sum")` 9 个键名写死。
- 这些键名是模板/LLM"约定"生成的别名；一旦 LLM 起的别名不一样（如 `average` 而非 `avg`），渲染直接掉进 raw_fallback 输出一堆 `key=value`。

### 2.16 中英混排的"业务词→列名"映射写死

- `agent.py` L244-255 `_METRIC_TERM_MAP` 10 组，其中：
  ```python
  (("泛油",), ("pan_you", "bleeding")),
  ```
  **`pan_you` 是拼音，不是任何真实列名**。这条映射的存在意义是"用户问泛油 → 列名里含 pan_you 的列"，纯属给模型起别名兜底，但拼音词出现在"权威映射"里本身就是脏数据。

### 2.17 图片查询逻辑写死

- `agent.py` L914：`image_columns = ("problem_url", "preview_url", "distance_capture_url", "picture")` 4 个列名写死；
- L917 `[:50]` 候选上限、L928 `>= 9`、L934 `[:6]` 展示上限——三个数字写死；
- `_IMAGE_AS_FILTER`（parsers.py L344）只认 `(有|带|含|包含|没有|无)\s*(照片|图片|现场图|病害图|影像)`，"有图无图"问法覆盖不全（如"全部带图"）。

### 2.18 单位推断写死

- `agent.py` L349-368 `unit_of`：按 label 里的子串猜单位（"面积"→㎡、"长度"→m、"车道"→条、默认"处"）。"高度"→m、"宽度"→m 与"面积"→㎡ 并存，但**"体积/数量/金额"等没有规则，默认落"处"**——问"工单金额 50000"会渲染成"50000 处"。

### 2.19 意图分类词表写死且含重复

- `intent.py`：
  - L8-25 `_KNOWLEDGE_HINTS` 16 词、L27-41 `_DATA_HINTS` 13 词；
  - L46-67 `_DATA_ENTITY_HINTS` 20 个词，**"路段"重复出现两次**（L58、L59）。
- 词表与 config/parsers/nl2sql 各写一套，"怎么修泛油"（knowledge）vs "西乡段泛油怎么样"（data）的边界完全靠这几十个词硬顶，换说法就翻车。

### 2.20 图表推断的列名/词表写死

- `analysis.py`：
  - L22/L32：时间列只认列名含 `time/date/时间/日期`；
  - L87：类别列只认 `type/damage_type/problem_type` 或含"类型"；
  - L95/L148：`<= 8` 用 pie 否则 bar 的阈值写死。
- LLM 起的别名不在这些名单里 → 直接不画图。

### 2.21 前/后 N 米口径说明写死

- `agent.py` L734-739：`（按「前N米 = 起点~N米、后N米 = N~2N米」计算）`——口径硬编码进回答，如果某天改口径，连回答文案都要跟着改。

### 2.22 导出文件名恒为固定值

- `exports.py` L16：`f"query_result_{result.get('id', 'export')}.csv"`。
- **结果里没有 `id` 字段（是 `result_id`）**，所以每次导出文件名都是 `query_result_export.csv`，浏览器连续下载会互相覆盖。

---

## 3. 🟡 P2 维护级写死（不影响单次正确性，但全是雷）

| 位置 | 写死内容 | 说明 |
|---|---|---|
| `config.py` L20-27 | `app_host=127.0.0.1`、`app_port=18730`、qwen 默认模型/URL | 默认值合理，但"模型名元组"L101-109 7 个模型写死，新增模型要改代码 |
| `config.py` L39-55 | ssh/mysql/pg 的 host/port/user 全部默认值写死 | 部署换环境只靠 .env 覆盖，一旦 .env 缺项就静默用默认 IP |
| `config.py` L73-76 | `ragflow_page_ranges` 写死两个数据集从第 6 页起 | 换 PDF（封面页数变化）就切错页；第 6 页这个数字是拍脑袋的 |
| `config.py` L91 | `confidence_threshold=0.40` | 亿维字典 `ai_problem_base.confidence` 每类病害各有阈值，业务侧过滤阈值应是"字典驱动"，不是全局一个 0.40 |
| `db.py` L64/76 | 容器名 `mysql`/`postgres` | 见 1.2 |
| `query_rewrite.py` | `SYNONYMS` 6 组同义词 | "龟裂→网状裂缝"这类同义映射写死，与字典不联动 |
| `cross_db.py` | `MYSQL_TERMS` 34 / `POSTGRESQL_TERMS` 8 / `SEAWEEDFS_TERMS` 11 | 跨库分流词表写死；`_CLAUSE_SPLIT` 连接词正则写死 |
| `orchestrator.py` | `DATA_TERMS` 24 / `KNOWLEDGE_TERMS` 13 / `_task_type` 词表 | 拆题词表写死 |
| `clarify.py` | `_CORE_TABLES`：mysql 只给 3 张表、pg 只给 2 张表配中文注释 | 其余 100+ 表在澄清/字段提示里没有注释，用户追问字段时无从提示 |
| `rag.py` L208-226 | `list_documents` 翻页上限 `while page <= 50`（每页 100 条，最多 5000 文档） | 文档超 5000 个就列不全 |
| `rag.py` L230-240 | `_vector_weight` 按问题长度写死返回 0.35 / 0.7 | 检索向量/关键词权重硬编码，与 config 的 `ragflow_vector_similarity_weight` 并存但不一致 |
| `store.py` L460-463 | `retrieval_log` **只保留 100 条**写死（`LIMIT 100`） | config 有 `result_cache_limit=200` 但 retrieval_log 是独立写死的 100；日志只留 100 条，排障时历史检索记录很快被清光 |
| `vanna_engine.py` L60 | `_UserResolver` 写死 `User(id="local-user", group_memberships=["admin","user"])` | 所有用户身份一样，无隔离 |
| `vanna_engine.py` L179/181 | `DemoAgentMemory(max_items=1000)`、`max_tool_iterations=4` | 容量/迭代上限写死 |
| `llm.py` L102-105 | `enable_thinking` 报错重试 hack | 靠异常字符串匹配 `"enable_thinking"` 判断，脆 |
| `llm.py` L138-139 | `chat_json` 剥 ` ```json ` 前缀 | 只剥首尾，模型输出注释行会 JSON 解析失败（有兜底但脆弱） |
| `calibrate.py` L17 | `_FACTS_TTL_SECONDS = 300` | 事实缓存 5 分钟；刚入库的数据 5 分钟内查不到会答"0 处" |
| `calibrate.py` L245 | `explain_zero` 文案写死"现有记录只有泛油与未识别类型" | **数据写死**：库里出现第 3 种类型时这句话就是错的 |
| `calibrate.py` L47-57 | `route_facts` 写死 3 张表名（routes/ai_problem/sections） | 与 1.4 白名单问题同源 |
| `sql_engine.py` L11-23 | `DANGEROUS_WORDS` 13 词 + `_DANGEROUS_PATTERN` | `\b` 词边界对中文/反引号场景有漏网可能（如 `load_file(`） |
| `sql_engine.py` L30-45 | MySQL/PG 方言标记两组 | 靠关键词猜方言，`str_to_date` 等缺漏会漏检 |
| `sql_engine.py` L109-110 | MySQL 强制 `del_flag` | 全表都必须有 del_flag 才合法；字典表/无该列的表查询会被拒 |
| `agent.py` L37-40 | `_COMPARISON_FEEDBACK` 文案 | 反馈话术写死 |
| `agent.py` L83-96 | `_OTHER_DOMAIN_SIGNALS` 10 词、`_MEASURE_WORDS` 10 词 | 词表写死 |
| `agent.py` L749-751 | `_task_title` 4 个标签写死 | 子任务标题映射 |
| `agent.py` L996 | `inherit_context` 默认 True | 行为开关写死默认值 |
| `memory.py` L52-53 | session key 前缀 `session:` | 存储键格式 |
| `storage.py` L12 | `_REFERENCE_KEYWORDS = ("url","pic","image","photo","path","file")` 6 词 | 找图片字段靠关键词猜 |
| `analysis.py` L9 | `_PILE_PATTERN` 只认 `K\d+\+\d+` | 桩号格式变了（如 `K1+000.5`）解析失败 |
| `chat_settings.py` L33 | `model_choices` 直接透传 config | 与 3.1 同源 |
| `main.py` L11 | `version="0.1.0"` | 无关紧要但写死 |
| `knowledge_sync.py` L32-38 | `_mime_type` 只认 pdf/md | 其他类型文件（docx/xlsx）按 text/plain 传，RAGFlow 解析可能失败 |
| `agent.py` L1165-1166 | `add_conversation_message(session_id, ...)` | 会话标题取 `message[:40]` 写死截断 |

---

## 4. 写死点的集中分布图（修复优先级依据）

```
config.py          ← 写死的"总源头"（IP/端口/用户名/白名单/数据集/阈值/模型名单）
   │
   ├─ db.py         ── 5×127.0.0.1:28888、容器名（P0，改 config 无效）
   ├─ intent.py     ── 49 个意图词、4 个数据集名（P1，与 config 不联动）
   ├─ agent.py      ── 10 组指标映射、8 个分组列名、9 个聚合键、500字/3~6条、4 个图片列
   ├─ nl2sql.py     ── 10 比较词、17 表对象、9 维度、4 数值字段、16 操作符、3 主体链路、6 字段词
   ├─ parsers.py    ── 19 病害词、13 停用词、5 主体、999m 默认、前/后N米口径
   ├─ cross_db.py / orchestrator.py ── 34+8+11 分流词、24+13 拆题词、日均特判
   ├─ memory.py     ── 61 个词/短语、3 个长度阈值
   ├─ sql_engine.py ── prompt 双 10.、K100+K200、13 危险词、del_flag 强制
   ├─ calibrate.py  ── "只有泛油与未识别类型"、3 表名、300s TTL
   ├─ rag.py / store.py / analysis.py / vanna_engine.py ── 权重、100 条、图表词、max_items
   └─ exports.py    ── 文件名恒为 query_result_export.csv
```

## 5. 给 Codex 的统一修复方向（不是逐个打补丁）

1. **词表集中化**：所有词表（病害词、比较词、意图词、跨库词、主体词、单位词）收敛成一份配置（YAML/JSON/数据库字典表），运行时加载，**禁止散落在各 py 文件顶部**。新增词 = 改配置，不是改代码。
2. **字典驱动**：病害类型（42 类）、code→名称、大类分组（0/5/10/15）、巡查类型（0/5/10/15/20）、资产大类（10~80）、每类 confidence 阈值——全部从 `ai_problem_base` / `ai_asset_base` / 字典表动态读取，代码里只留"读字典"的逻辑。
3. **单一数据流**：数据集名、表白名单、库名、模型名单——只从 config 读，`intent.py` / `agent.py` / `nl2sql.py` 不得再写第二份。
4. **连接层抽象**：db.py 的 MySQL/PG 访问抽象成"连接器接口"，实现 SSH 隧道 + 宿主端口直连（默认）和 docker exec（可选），容器名/URL 全部走 config。SeaweedFS 统一用 `settings.seaweedfs_base_url`。
5. **语义槽补齐**：比较词拆分"求和(sum) / 对比(compare)"两组；`validate_semantics` 增加"多余条件检测"；桩号解析支持"以X为基准前/后 N 米"。
6. **渲染层列名解耦**：`_summarize_result` / `_grouped_summary` 不依赖约定别名，改为按"指标含义 + 数值类型"推断（`_numeric_columns` 已是好方向，扩展开即可），或让模板/LLM 输出标准化 JSON 结构。
7. **prompt 修复**：编号去重、桩号样例给全格式（区间/单点/带基准前后）。
8. **清理 hack**：`_build_multi_metric_template` 的字符串别名替换改为结构化构建（显式写 p2/r2 别名）；`_build_table_count_template` 的"桥"误命中改为完整词匹配。

## 6. 遗留说明

- 本次审查为**静态通读**，未运行任何 SQL/未连库复算；写死点均为代码层面可确认的事实。
- `db.py` / `calibrate.py` / `rag.py` 的写死点已在 KEY_FACTS 中列全，但 `rag.py` 的 `list_documents` 翻页与 `_vector_weight` 的具体行号以实际文件为准（本次未逐行重读 rag.py，属上一轮已确认结论）。
- 修复顺序建议：P0 的 1.1 / 1.2 / 1.4 / 1.5 先做（影响面最大），再做 2.1 / 2.2（LD1 直接相关），其余按文档分类逐项消化。
