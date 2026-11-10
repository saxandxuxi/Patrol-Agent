# 道路AI巡检养护智能体 · 整体方案（v6）

> 版本：v6　|　日期：2026-09-11　|　状态：数据探查已完成，待进入开发

## 一、方案变更记录（相对 v5）

| 项 | 变更 | 原因 |
|---|---|---|
| **查询引擎** | ✅ 新增第四章 | 明确三数据库查询能力、查询类型、语义→SQL 混合机制（代码逻辑+LLM 协同）、SQL 正确性保障五道关卡 |
| **输出层** | ✅ 新增 | markdown 表格 + **CSV 文件下载** |
| **数据分析与可视化** | ✅ 新增 | 分析引擎（聚合/趋势/分布）+ 前端**双栏布局**（左对话 / 右图表展示） |
| **记忆功能** | ✅ 新增 | 短期对话记忆 + 长期用户偏好/历史查询记忆 |
| **混合架构** | ✅ 明确 | 确定性代码（模板/解析/校验/测算）为主干，LLM 只做意图理解、参数抽取、SQL 生成与自然语言输出，**不是全靠 LLM** |
| Python 直连 / config.yaml | ✅ 保留 | v5 已定 |
| 三存储 / 移除接入服务 | ✅ 保留 | v4/v5 已定 |

---

## 二、系统架构（v6）

```
【数据现状 · 已跑通，智能体只读消费】
亿维推送(HTTP) ─▶ RabbitMQ(disease/asset/distance-capture-topic) ─▶ MySQL业务表(ai_problem/ai_asset_main/ai_distance_capture)
亿维GPS(HTTP) ───────────────────────────────────────────────────▶ PostgreSQL(gpsdb.gps_points)
病害图片 ────────────────────────────────────────────────────────▶ SeaweedFS(problem_url 路径)

【智能体 · 零接入，直连三存储，代码逻辑+LLM 混合】
用户 ─▶ 前端(左对话 | 右展示) ─▶ 智能体服务(FastAPI + LangGraph)
                                    ├─▶ 需求判定引擎(规则+LLM)
                                    ├─▶ 查询引擎 ◀─ 确定性层:桩号/时间解析·SQL模板·白名单校验
                                    │        │        LLM层:意图分类·字段映射·SQL生成·报错修正
                                    │        └─▶ 远程直连 MySQL(3307)/PG(5436) [只读]
                                    ├─▶ 图片访问 ──▶ SeaweedFS HTTP(28888) [只读]
                                    ├─▶ 分析引擎(pandas:聚合/趋势/分布/对比)
                                    ├─▶ RAG问答 ──▶ RAGFlow v0.27.1(向量+内置图谱+查询手册)
                                    ├─▶ 测算引擎(规则+formula+定额)
                                    ├─▶ 记忆(短期对话 / 长期偏好·历史查询 → SQLite)
                                    └─▶ 血缘记录 ──▶ data_lineage表

输出：markdown 表格 / CSV 下载 / 图表数据(JSON) → 前端渲染
```

---

## 三、数据层设计（三存储）

### 3.1 数据源定位（实测确认）

| 存储 | 端口 | 定位 | 智能体角色 |
|---|---|---|---|
| **MySQL** `new_four_road_base_ai` | 3307 | **主业务库**：病害(ai_problem)、资产(ai_asset_main)、定距(ai_distance_capture)、台账(basic_facilities_*)、工程(project_manage_*) | 只读 SQL 查询 |
| **PostgreSQL** `gpsdb` | 5436 | **GPS 轨迹库**：gps_points / inclination_points（当前 0 行，按有数据设计） | 只读 SQL 查询 |
| **SeaweedFS** | 28888(filer) | **对象存储**：病害图片（problem_url，免认证） | 只读 HTTP 取图 |
| RabbitMQ / Redis / ES / EMQX / Nacos / mediamtx | - | 管道/缓存/检索/消息/配置/视频 | **不接入** |

### 3.2 核心业务表（字段字典已收录）

| 数据需求 | 表 | 关键字段 |
|---|---|---|
| 病害 | `ai_problem`（1681行） | pile_number/pile_distance(桩号)、damage_type(编码)、pic_position(JSON含confidence)、area/area_width/area_height、lng/lat、collection_time、routes_id/sections_id、problem_url |
| 病害字典 | `ai_problem_base` | problem_code→problem_type、confidence(阈值0.40)、formula、disease_categories_type |
| 资产 | `ai_asset_main` | pile_number、coordinate、abnormal_type、picture、lng/lat |
| 定距 | `ai_distance_capture` | pile_number、distance_capture_url、collection_time、device_code |
| GPS | PG `gps_points` | device_id、lat/lon、speed、direct、create_time(索引) |
| 路线台账 | `basic_facilities_routes`(3592) | code、full_name、level、start_num/end_num、distance、manage_dept |
| 路段/桩号 | `basic_facilities_sections` / `sections_pile_detail` | 68492 行桩号明细 |
| 工程记录 | `project_manage_project_main` | project_name、project_total_investment、design_estimate_amount、build_start/end_date |

### 3.3 数据访问规则
1. **远程直连只读**：MySQL `10.168.210.78:3307`、PG `10.168.210.78:5436`、SeaweedFS `http://10.168.210.78:28888`（外部直连授权待验证）；
2. **SQL 白名单**：仅 SELECT、仅业务表、强制 `del_flag=0`、默认时间范围、行数上限；
3. **中文编码**：MySQL 连接 `charset=utf8mb4`；
4. **置信度过滤**：`JSON_EXTRACT(pic_position,'$.confidence')` ≥ 阈值（默认 0.40），剔除 offRoad=true；
5. **图片容错**：URL 拼接 + 404 显示占位。

### 3.4 数据血缘
记录 `{结果ID, 用户问题, 意图, 来源库, 来源表, 筛选字段, 计算逻辑, 原始快照, 时间}`（SQLite 起步）。

### 3.5 连接配置模板（config.yaml，填密码后交给 Codex）

```yaml
# 填写规则：用户名/密码按实际填写；没有的用户名留空 ""，没有的密码留空 ""
mysql:                        # ✅ 主数据源
  host: 10.168.210.78
  port: 3307
  database: new_four_road_base_ai
  user: Yiviewmysql           # 用户名（有）
  password: ""                # ← 必填
  charset: utf8mb4

postgresql:                   # ✅ GPS 轨迹库
  host: 10.168.210.78
  port: 5436
  database: gpsdb
  user: postgres              # 用户名（有）
  password: ""                # ← 必填

seaweedfs:                    # ✅ 图片存储（HTTP 无认证）
  base_url: "http://10.168.210.78:28888"
  user: ""                    # 无，留空
  password: ""                # 无，留空

redis:                        # 可选（缓存，当前空）
  host: 10.168.210.78
  port: 6376
  user: ""                    # Redis 无用户名，留空
  password: ""                # ← 有则填

rabbitmq:                     # 可选（管道，默认不接入）
  host: 10.168.210.78
  port: 5672
  user: ywrabbitmq            # 用户名（有）
  password: ""                # ← 有则填

elasticsearch:                # 暂不接入
  enabled: false
  host: 10.168.210.78
  port: 19200
  user: elastic
  password: ""                # 待管理员

ragflow:                      # 本机 RAGFlow
  base_url: "http://127.0.0.1"
  api_key: ""                 # ← 界面生成后填
```

```python
# MySQL
engine = create_engine("mysql+pymysql://Yiviewmysql:密码@10.168.210.78:3307/new_four_road_base_ai?charset=utf8mb4")
# PostgreSQL
conn = psycopg2.connect(host="10.168.210.78", port=5436, user="postgres", password="密码", dbname="gpsdb")
# SeaweedFS
resp = requests.get("http://10.168.210.78:28888/" + problem_url, timeout=10)
```

---

## 四、查询引擎设计（核心新增）

### 4.1 三数据库查询能力清单（"增删改查"中的"查"全覆盖）

| 能力 | MySQL（业务主库） | PostgreSQL（gpsdb） | SeaweedFS（HTTP） |
|---|---|---|---|
| 单表查询 | SELECT + WHERE + ORDER BY + LIMIT | 同左 | - |
| 多表 JOIN | ai_problem ⋈ ai_problem_base（类型翻译）、⋈ basic_facilities_routes（道路挂接） | - | - |
| 聚合统计 | COUNT/GROUP BY 按病害类型/路段/时间/上下行 | 按 device_id/时间桶统计 | - |
| 时间范围 | `collection_time BETWEEN` | `create_time BETWEEN`（有 DESC 索引） | - |
| JSON 字段 | `JSON_EXTRACT(pic_position,'$.confidence')` | `data->>'字段'`（data 预留） | - |
| 桩号/里程区间 | `pile_distance BETWEEN 起  AND 止`（K20+000→20000） | - | - |
| 模糊/前缀 | `pile_number LIKE 'k20%'`、`full_name LIKE` | device_id 精确/前缀 | - |
| 分页 | LIMIT/OFFSET | 同左 | - |
| 图片/文件取回 | - | - | `GET base_url + problem_url`（无 SQL） |

**MySQL 与 PG 语法差异（LLM 提示与模板必须区分）**：
- 时间函数：MySQL `DATE_FORMAT(collection_time,'%Y-%m')` vs PG `to_char(create_time,'YYYY-MM')` / `date_trunc('month', create_time)`；
- JSON：MySQL `JSON_EXTRACT(...,'$.k')` vs PG `->>`；
- 模糊：PG 大小写不敏感用 `ILIKE`（MySQL 用 `LIKE` + 字符集决定）；
- 连接参数：MySQL 必须 utf8mb4；PG 需指定 dbname。

### 4.2 查询类型与语义匹配（用户要什么 → 生成什么）

**确定性代码优先，LLM 辅助**。流程：规则层抽取结构化参数 → LLM 补语义 → 模板组装 → 校验 → 执行。

| 用户语义特征 | 查询类型 | 实现方式 |
|---|---|---|
| "查/列出/有哪些 XX" | 单表查询 | SQL 模板 + LLM 填字段/条件 |
| "XX道路的病害 / 病害是什么类型" | 多表 JOIN | 预置 JOIN 模板（病害+字典+台账） |
| "多少处/多少个/分布" | 聚合统计 | COUNT/GROUP BY 模板 |
| "K100-K200" | 里程区间 | **正则桩号解析器（确定性代码）** → pile_distance BETWEEN |
| "最近/2026年/6月" | 时间范围 | **时间解析器（确定性）** → BETWEEN |
| "高置信/有效病害" | 置信度过滤 | JSON_EXTRACT 阈值 |
| "照片/图片" | 图片取回 | SeaweedFS URL 拼接 |
| 组合语义 | 综合查询 | 多条件拼接执行 |

**LLM 的职责边界（明确，不是全靠 LLM）**：
1. 意图分类（可用规则兜底）；
2. 自然语言 → 字段映射（基于字段字典提示）；
3. 生成候选 SQL（有模板时只填参数，无模板时全文生成）；
4. 报错信息回喂修正（最多 2 次）。

**确定性代码的职责（不依赖 LLM，保证稳定）**：
1. 桩号解析：`K20+000` → 20000（正则）；
2. 时间解析：中文时间 → 日期区间；
3. SQL 模板库：常用查询（病害统计、路线挂接、桩号区间）预置参数化模板；
4. 白名单校验、EXPLAIN 预检、结果行数限制；
5. 测算引擎、血缘记录。

### 4.3 SQL 正确性保障（五道关卡）

1. **字段字典注入**：LLM 提示带表结构/字段/枚举/桩号格式/同义词（依据《数据库字段字典》）；
2. **白名单校验**：仅 SELECT、仅业务表、强制 `del_flag=0`、强制 LIMIT 上限（默认 500 行）；
3. **语法预检**：先 `EXPLAIN` 或 `LIMIT 1` 试执行，失败即拦截；
4. **结果校验**：空结果区分"无数据"与"条件过严"；字段缺失/类型错误提示；
5. **报错回喂**：执行错误信息 → LLM 修正 SQL → 重试（最多 2 次），仍失败返回可读错误并记录血缘。

### 4.4 知识库补充查询知识（回答你的问题：需要）

**需要补充**，但定位是"LLM 生成 SQL 的参考示例"（few-shot），不是替代确定性代码：

| 知识库文档 | 内容 | 作用 |
|---|---|---|
| 《数据库查询手册》 | 三数据库常用查询语法示例、MySQL/PG 差异对照 | LLM 生成 SQL 时检索参考 |
| 《业务查询模板集》 | 病害统计/里程查询/台账查询/工程查询 每类 3-5 条带注释示例 | 提高生成准确率 |
| 《字段字典速查》 | 核心表字段+枚举+桩号格式 | 字段映射依据 |

实现：这些文档上传 RAGFlow（与养护资料同库或分库），智能体生成 SQL 前先检索相关示例注入提示词。**同时本地维护 `query_templates` 表**（确定性模板库，高频查询直接走模板，不调 LLM）。

---

## 五、知识层设计（RAGFlow v0.27.1）

### 5.1 知识库内容清单

| 类别 | 资料 | 用途 |
|---|---|---|
| 技术规范 | 《公路养护技术规范》JTG H10-2009 | 养护工艺、质量标准 |
| 评定标准 | 《公路技术状况评定标准》JTG 5210-2018 | 病害分类、评定等级 |
| 养护规范 | JTG 5142 / JTG/T 5521 | 裂缝/坑槽处置方案 |
| 桥涵养护 | 《公路桥涵养护规范》JTG H11 | 桥梁病害 |
| 造价定额 | JTG/T 5610-2020、预算编制办法 | 工程量→资金测算 |
| 管理办法 | 养护工程管理办法及细则 | 立项、验收流程 |
| 案例库 | 历年养护工程案例 | 问答示例 |
| 手册 | 亿维操作手册/推送协议 | 数据口径 |
| **查询手册** | **4.4 的三份查询知识文档** | **SQL 生成参考** |

### 5.2 RAGFlow 知识图谱（GraphRAG）配置
1. 数据集配置 → 实体类型改养护业务实体（道路/病害/养护工艺/定额子目/材料/资金），方法 **Light**；
2. 文件页 → 生成 → 知识图谱（分批上传控制 token）；
3. 聊天/检索打开"使用知识图谱"开关，问答走"向量+图谱"双通道。

注意：图谱存 RAGFlow 文档引擎，不支持导出；文件更新后需重新生成。

---

## 六、智能体层设计

### 6.1 需求判定引擎（意图路由）

| 意图 | 路由目标 | 示例 |
|---|---|---|
| 知识问答 | RAGFlow（向量+图谱） | "纵向裂缝的养护规范是什么" |
| 数据查询 | 查询引擎 → MySQL/PG | "G72 K100-K200 有多少处横向裂缝" |
| 分析统计 | 查询引擎 + 分析引擎 | "按病害类型统计最近一个月的分布" |
| 测算 | 测算引擎 | "这段路坑槽修补需要多少预算" |
| 图片 | SeaweedFS | "K20+000 病害现场照片" |
| 综合 | 多链路拼接 | "K100-K200 病害+建议+预算" |

判定：LLM 分类 + 规则兜底，输出结构化路由。

### 6.2 查询引擎
见第四章。执行后统一产出结构化结果：`{columns, rows, chart_data?, source_db, sql, cost_ms}`。

### 6.3 输出层：markdown + CSV 下载
- **markdown**：对话内渲染表格（结果 ≤ 50 行直接展示，多则摘要+提示下载）；
- **CSV 下载**：结果转 CSV 文件，接口 `GET /api/export?result_id=xxx` 返回下载（文件名含时间戳，如 `病害统计_20260911_1530.csv`）；
- 血缘：每次导出记录 result_id，保证可追溯。

### 6.4 数据分析引擎
- 输入：查询引擎结果（DataFrame）；
- 能力：聚合汇总、趋势（按时间）、分布（按类型/路段）、对比（年度/上下行）、TopN；
- 输出：统计表 + 图表数据 JSON（`{type: bar/line/pie, x, y, series}`），前端直接渲染。

### 6.5 前端布局与可视化（双栏）
- **布局**：左栏对话（约 45% 宽，markdown 渲染 + 下载按钮），右栏展示区（约 55%，主屏幕）：
  - 图表区：ECharts（柱状/折线/饼图，来自 chart_data JSON）；
  - 数据表区：查询结果表格（可排序、分页）；
  - 图片区：SeaweedFS 病害图；
  - 血缘/来源角标：显示"数据来源：MySQL.ai_problem + 筛选条件"。
- 交互：对话中提问 → 右栏自动切换到对应图表/表格；点击历史消息可回看。

### 6.6 记忆功能
- **短期记忆**：多轮对话上下文（LangGraph checkpoint，存会话变量/Redis）；
- **长期记忆**：SQLite 表 `user_memory`：
  - `user_pref`：用户偏好（常用路线/路段、常用时间窗、输出偏好）；
  - `query_history`：历史查询（问题、SQL、结果摘要、时间），支持"上次查的"、"继续查下一段"等指代；
- 记忆注入：新问题进来先检索相关记忆（规则匹配 + 可选 LLM 摘要），补充到提示词。

### 6.7 数据血缘
见 3.4；与输出层联动：每次查询/导出/图表都挂 result_id。

### 6.8 模型可插拔（LLMProvider 抽象）
统一 `chat()/embedding()` 接口：智能体侧可切换 DeepSeek/Qwen/Ollama；RAGFlow 侧模型提供商页配置。

---

## 七、技术栈

| 层 | 选型 | 说明 |
|---|---|---|
| 知识引擎 | RAGFlow v0.27.1（本机已有） | 向量 + 内置 GraphRAG + 查询手册 |
| 后端 | Python 3.11 + FastAPI | 智能体服务 + CSV 导出接口 |
| 智能体编排 | LangGraph / LangChain | 路由、工具调用、短期记忆 |
| 查询引擎 | SQLAlchemy + pymysql/psycopg2 + 自研模板/解析器 | 混合架构：确定性代码 + LLM |
| 分析 | pandas + 自研聚合逻辑 | 数据分析、chart_data 生成 |
| 对象存储 | requests（HTTP） | SeaweedFS 取图 |
| 记忆 | SQLite（user_memory）+ LangGraph checkpoint | 短期+长期 |
| 前端 | Vue3 + Vite + ECharts + Element Plus | 双栏：对话 | 图表/表格/图片 |
| 模型 | DeepSeek/Qwen（OpenAI 兼容），可换 Ollama | 可插拔 |

---

## 八、实施里程碑

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0 | RAGFlow 建库 + 图谱 + 查询手册上传 | ⬜ 待开始（等你提供资料） |
| M1 | 数据探查 + 字段字典 | ✅ 已完成 |
| M2 | 智能体核心：需求判定 + RAG + **查询引擎**（模板/解析器/白名单/五道关卡）+ CSV 输出 | ⬜ 下一步 |
| M3 | 分析引擎 + 前端双栏（对话/图表表格图片）+ 可视化 | ⬜ 待开始 |
| M4 | 记忆功能 + 测算引擎 + 血缘完善 | ⬜ 待开始 |
| M5 | 联调：真实数据问答/导出/图表/记忆回看 | ⬜ 待开始 |

---

## 九、已确认决策

- ✅ 数据源只有 MySQL / PostgreSQL(gpsdb) / SeaweedFS，其余不接入；
- ✅ 智能体不接推送、直接远程连库只读；
- ✅ **混合架构**：确定性代码（模板/解析器/校验/测算）为主干，LLM 做意图/映射/SQL 生成，**不是全靠 LLM**；
- ✅ 输出：markdown 表格 + **CSV 下载**；
- ✅ 前端**双栏**：左对话、右图表/表格/图片主屏展示；
- ✅ **记忆功能**：短期对话 + 长期偏好/历史查询；
- ✅ RAGFlow v0.27.1 + 内置图谱 + 查询手册知识，不用 Neo4j；
- ✅ 业务数据只查库不进知识库；
- ✅ 不做地图、不做平台对接；模型可插拔；血缘可追溯；
- ✅ 空库按"以后会有数据"设计。

## 十、待办

1. 填写 config.yaml 密码（3.5 模板）；
2. 外部直连授权验证（`10.168.210.78:3307/5436`，失败则 GRANT 或找管理员）；
3. 养护资料 PDF + 查询手册 → RAGFlow（M0）；
4. RAGFlow API Key 生成填入；
5. 亿维推送稳定性确认；
6. 智能体代码开发（M2 起，交给 Codex，输入见附录）。

## 十一、风险

| 风险 | 影响 | 对策 |
|---|---|---|
| 服务器网络（Tailscale 断） | 查不到数据 | 监控 VPN；申请内网白名单 |
| 外部直连授权缺失 | Python 直连被拒 | GRANT 或找管理员开只读账号 |
| **LLM 生成 SQL 错误率** | 查询结果不对 | 五道关卡 + 模板优先 + 查询手册 few-shot + 报错回喂 |
| 桩号/时间解析歧义 | 里程区间错 | 确定性解析器 + 解析结果回显给用户确认 |
| RAGFlow 图谱 token 大 | 构建慢/贵 | 分批、Light、先小资料 |
| 前端双栏开发量大 | M3 延期 | 先 API + 简单双栏，图表逐步加 |
| gpsdb/SeaweedFS 空 | 功能空转 | 按有数据设计，接入后生效 |
| ES 未接入 | 检索类需求缺失 | 需要时向管理员索取 |

---

## 附录：开发输入清单（交给 Codex）

| 输入 | 文件 |
|---|---|
| 整体方案 | 本文件（v6，重点第四章查询引擎） |
| 字段字典 | 《数据库字段字典.md》 |
| 连接配置 | config.yaml（填好密码后） |
| 推送协议 | 亿维道路AI巡检_HTTP_V2.pdf |
| RAGFlow 对接 | RAGFlow REST API 文档 |

**开发范围（Codex）**：FastAPI 服务、需求判定、查询引擎（模板+解析器+校验）、RAG 对接、分析引擎、CSV 导出、记忆、血缘、模型抽象、前端双栏。**不包含**：接入服务、写业务库。
