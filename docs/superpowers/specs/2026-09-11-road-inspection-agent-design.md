# 道路 AI 巡检养护智能体 MVP 设计文档

> 日期：2026-09-11
> 状态：设计已确认，待进入实现计划
> 范围：第一轮 MVP，不包含推送接入、写业务库、生产级多服务编排

## 1. 背景与目标

用户已具备道路巡检数据与知识资料：

- MySQL `new_four_road_base_ai`：病害、资产、定距、路线台账、工程等业务数据；
- PostgreSQL `gpsdb`：GPS 轨迹数据，当前为空，按未来有数据设计；
- SeaweedFS：病害图片对象存储，通过 `problem_url` 访问；
- RAGFlow：本机知识引擎，用于养护规范和 SQL 查询规范问答；
- 本地知识库文件：养护行业规范、MySQL/PG/SeaweedFS 查询知识。

目标是实现一个语义化 AI 助手：用户用自然语言提问，系统自动判断意图，执行只读查询或知识检索，并返回表格、图表、图片或 CSV。

第一阶段交付端到端闭环：

```text
自然语言问题 -> 意图路由 -> 数据库查询 / RAG 检索 -> 结果渲染 / CSV / 图表
```

## 2. 非目标

- 不接入亿维 HTTP 推送、RabbitMQ 消费或任何写业务库逻辑；
- 不实现地图、视频流、ES、EMQX、Nacos 等非核心服务；
- 第一轮不实现完整测算引擎、完整数据血缘、复杂长期记忆；
- 暂不完成亿维平台实际嵌入，只预留 embed 模式。

## 3. 总体架构

采用单仓库、单 FastAPI 服务、Vue3 前端由 FastAPI 静态托管。

```text
浏览器（Vue3：左对话 / 右图表·表格·图片）
        │  /api/*
        ▼
FastAPI 应用
  ├─ 配置与密钥管理（.env，模型可前端配置）
  ├─ 连接层（SSH 隧道 -> MySQL/PG；SeaweedFS HTTP）
  ├─ 语义路由（Qwen：意图识别、字段映射、SQL 生成/纠错）
  ├─ 查询引擎（桩号/时间解析、SQL 模板、白名单、校验、只读执行）
  ├─ RAG 层（RAGFlow 同步 + 检索）
  ├─ 分析层（pandas 聚合/趋势/分布，输出表格与 chart JSON）
  ├─ 导出层（CSV 下载）
  ├─ 模型管理层（默认 Qwen，前端可增删模型、填 key、测试连接）
  └─ 本地持久层（SQLite：同步映射、模型配置、记忆/历史查询）
```

数据流：

```text
用户提问
 -> 意图路由：知识问答 / 数据查询 / 分析统计 / 图片查看 / 综合
 -> 需要数据库时：解析参数 -> 检索 SQL few-shot -> LLM 生成或套模板 -> 五道校验 -> SSH 隧道只读执行
 -> 结果统一为 { columns, rows, chart_data?, source, sql, cost_ms }
 -> 前端按类型渲染为表格/图表/图片，并提供 CSV 下载
```

## 4. 技术栈

- 后端：Python 3.10+、FastAPI、SQLAlchemy、PyMySQL、psycopg2、pandas、sshtunnel/paramiko、httpx；
- 前端：Vue3、Vite、Element Plus、ECharts；
- 大模型：默认 Qwen，使用 `QWEN_API_KEY`，通过 OpenAI 兼容接口调用，支持前端配置模型和 API Key；
- 知识引擎：RAGFlow REST API；
- 本地存储：SQLite；
- 配置：`.env` 保存敏感信息，`config.yaml` 保存可读默认配置。

## 5. 数据库连接与查询安全

### 5.1 连接策略

- 先尝试直连 `10.168.210.78:3307` 和 `5436`；
- 直连超时或失败时，自动建立 SSH 隧道，把远端 MySQL/PG 端口转发到本机临时端口；
- SSH 隧道和数据库连接对象做缓存，健康检查失败后重建；
- SeaweedFS 先尝试 `http://10.168.210.78:28888` 直读，失败时通过 SSH 端口转发或 HTTP 代理访问；
- 所有敏感信息只从 `.env` 读取，代码中不硬编码。

### 5.2 查询安全

五道关卡：

1. 只允许 `SELECT`，拒绝写操作和危险语句；
2. 表名和库名白名单；
3. 自动追加 `del_flag=0`、`LIMIT` 上限和默认时间范围；
4. 先 `EXPLAIN` 或 `LIMIT 1` 试执行；
5. 执行错误回喂模型修正，最多 2 次。

### 5.3 空库设计

`gpsdb.gps_points`、SeaweedFS 图片等当前可能为空，但按未来有数据设计：字段字典照常生成，查询时返回友好提示，不把空库当作错误。

## 6. RAGFlow 知识库同步与检索

### 6.1 数据集划分

按本地目录新建独立数据集：

```text
养护行业规范      -> 知识库文件/养护行业规范/*.pdf
mysql语句知识     -> 知识库文件/mysql语句知识/*.md
postgresql语句知识 -> 知识库文件/postgresql语句知识/*.md
seaweedfs语句知识 -> 知识库文件/seaweedfs语句知识/*.md
数据库字段字典     -> 后续连库后自动生成并上传
```

### 6.2 镜像同步

维护 `knowledge_sync` 映射表：

```text
知识库名 + 本地相对路径 + 内容哈希 + chunk 序号 -> RAGFlow document_id
```

同步规则：

- 本地新增文件：切分后上传；
- 本地内容变化：删除旧文档，重新上传；
- 本地删除文件：删除对应 RAGFlow 文档；
- 前端提供知识库管理页，可查看状态和手动触发重新同步。

### 6.3 SQL/API 文档原子预切分

SQL/API 类 Markdown 文件不直接交给 RAGFlow 自动切分，而是先在本地做原子化切分：

- 一个标题及其正文作为一个逻辑单元；
- 一个 fenced code block 永远不切开，并与其说明文字放在同一单元；
- 表格尽量整体保留；
- 每个单元作为独立 RAGFlow 文档上传，文档名带来源路径和序号；
- 养护行业 PDF 走 RAGFlow 默认解析。

## 7. 语义理解与查询引擎

核心原则：确定性代码为主干，Qwen 只做语义补充。

### 7.1 意图路由

```text
知识问答 -> RAGFlow
数据查询 -> 查询引擎 -> MySQL/PG
分析统计 -> 查询引擎 + pandas
图片查看 -> SeaweedFS
综合任务 -> 多步骤组合
```

### 7.2 数据查询流程

1. 确定性解析器抽取桩号区间、时间范围、上下行、置信度阈值、行数上限；
2. 高频查询优先使用 SQL 模板；
3. 无模板时从 RAGFlow 检索对应 SQL few-shot，并注入字段字典；
4. Qwen 生成候选 SQL；
5. SQL 通过白名单、表名、`del_flag=0`、`LIMIT`、试执行五道关卡后运行；
6. 报错回喂修正，最多 2 次；
7. 输出统一结构化结果。

### 7.3 字段字典注入

连库后自动生成每个数据库的字段字典，内容包括：表名、字段名、类型、说明、主外键、索引、样例值、枚举映射、桩号格式和 MySQL/PG 语法差异。字段字典会上传 `数据库字段字典` 知识库，并在生成 SQL 前注入提示词。

## 8. 分析、导出与记忆

### 8.1 分析引擎

- 输入：查询结果 DataFrame；
- 能力：聚合汇总、时间趋势、类型/路段分布、对比、TopN；
- 输出：统计表 + `chart_data` JSON，类型为 `bar/line/pie`。

### 8.2 CSV 导出

- 每次查询生成 `result_id`；
- `GET /api/export?result_id=...` 返回 CSV；
- 文件名带业务主题和时间戳。

### 8.3 记忆

- 短期记忆：当前会话上下文；
- 长期记忆：SQLite 保存用户偏好、历史查询和结果摘要；
- 支持“上次查的”“继续查下一段”等指代。

## 9. 前端设计

- 左栏对话，支持 Markdown、表格、下载按钮；
- 右栏展示图表、表格、图片；
- 模型配置页：默认 Qwen，可增删模型、填写 API Key、测试连接；
- 知识库管理页：查看同步状态、手动重同步；
- 预留 `embed` 模式，未来可嵌入亿维平台，第一轮只做模式开关和接口预留。

## 10. 配置项

敏感信息通过 `.env` 读取，至少包括：

```text
QWEN_API_KEY
QWEN_BASE_URL
SSH_HOST
SSH_PORT
SSH_USER
SSH_PASSWORD
MYSQL_HOST
MYSQL_PORT
MYSQL_DATABASE
MYSQL_USER
MYSQL_PASSWORD
PG_HOST
PG_PORT
PG_DATABASE
PG_USER
PG_PASSWORD
SEAWEEDFS_BASE_URL
RAGFLOW_BASE_URL
RAGFLOW_API_KEY
```

前端模型配置保存到 SQLite，避免每次手动填 key。

## 11. 错误处理

- VPN 未连接、SSH 不可达：返回明确提示，指导用户连接 VPN 后重试；
- 数据库为空：返回“当前暂无数据，但功能已就绪”；
- SQL 非法或执行失败：返回可读错误，最多自动修正两次；
- SeaweedFS 图片 404：返回占位信息；
- RAGFlow API 不可用：返回同步失败状态，不影响数据库查询主流程。

## 12. 测试策略

- 单元测试：桩号解析、时间解析、SQL 白名单、字段字典生成、原子切分；
- 集成测试：MySQL/PG 只读查询、SSH 隧道连接、RAGFlow 同步、CSV 导出；
- 前端人工验证：对话、表格、图表、图片、模型配置、知识库管理；
- 真库联调放在 VPN 和凭据就绪后进行。

## 13. 里程碑

```text
MVP-1 后端骨架与配置
MVP-2 数据库连接层与 SSH 隧道
MVP-3 字段字典生成
MVP-4 语义路由与查询引擎
MVP-5 RAGFlow 知识库同步与检索
MVP-6 分析、CSV、记忆基础能力
MVP-7 Vue3 前端与模型/知识库配置
MVP-8 VPN 真库联调与端到端验收
```

## 14. 风险与对策

| 风险 | 对策 |
|---|---|
| VPN 未连接或 SSH 不可达 | 启动/查询时健康检查，返回明确提示 |
| LLM 生成 SQL 错误 | 模板优先、字段字典注入、五道关卡、最多两次纠错 |
| SQL 示例代码块被切断 | SQL/API 文件本地原子预切分 |
| RAGFlow 后端暂时 502 | 同步模块失败隔离，不阻塞数据库查询 |
| 空库影响联调 | 按有数据设计，返回友好提示，保留字段字典 |
| 后续嵌入亿维平台 | 前端预留 embed 模式和外部上下文参数 |

## 15. 决策记录

- 第一轮范围：MVP，不实现全部 v6 能力；
- 架构：方案一，单 FastAPI 服务 + Vue3 前端；
- 数据库连接：直连失败自动 SSH 隧道；
- 默认模型：Qwen + `QWEN_API_KEY`；
- RAGFlow：新建独立数据集，本地目录镜像同步，SQL 文件原子预切分；
- 前端：Vue3 + Vite + Element Plus + ECharts；
- 空库：按未来有数据设计。
