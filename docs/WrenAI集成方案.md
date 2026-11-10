# Wren AI 集成方案

> 日期：2026-09-16
> 定位：面向 Codex 的集成实施指南，包含 Wren 优势分析、现有资产保留边界、整体架构、分阶段实施步骤、MDL 建模示例、风险与注意事项。
> 关联文档：[NL2SQL重构方案.md](./NL2SQL重构方案.md)、[写死Bug清单-全量代码审查.md](./写死Bug清单-全量代码审查.md)、[长对话Bug分析-LD1两边加起来.md](./长对话Bug分析-LD1两边加起来.md)、[待补充需求清单.md](./待补充需求清单.md)

---

## 一、为什么要引入 Wren AI

### 1.1 现有 NL2SQL 的核心痛点

当前项目的 NL2SQL 链路是：**自然语言 → LLM 直接生成物理 SQL → 校验 → 执行**。

这个架构的根本问题是：**让 LLM 一次性搞定所有细节**，包括表名、字段名、JOIN 关系、字典翻译、方言语法、del_flag 过滤、LIMIT 等。任何一个细节猜错，SQL 就错了，而且很多错误是"SQL 能执行但业务含义错了"，更难发现。

具体表现（来自评测和现场 bug）：

| 痛点分类 | 具体案例 | 根因 |
|---|---|---|
| 字段映射错误 | "设备 102367594567"被映射到 `vehicle_id`（正确是 `device_code`） | LLM 猜列名 |
| JSON 字段查询 | "置信度≥0.7"条件被丢弃，答全库 1702（正确 893） | LLM 不会写 `JSON_EXTRACT` |
| 数值阈值条件 | "面积超过10㎡"条件被丢弃，答 534（正确 0） | parsers 不识别数值阈值 + LLM 丢条件 |
| 主体识别错误 | "里程桩点多少个"答成病害数 911（正确 52） | 模板硬编码 `FROM ai_problem`，无主体槽位 |
| top 查询错误 | "面积最大的桩号"被拆成 2 个子任务，MAX+定位全错 | 无 top 形状识别，orchestrator 误判 composite |
| 多指标只取第一列 | "不是泛油的有多少"SQL 算出 non_bleeding_count=766，reply 答总数 1702 | 结果摘要只取第一列 |
| list 降级为 count | "7月29日泛油明细"生成 `COUNT(*)` 聚合，答"共1条明细" | 形状识别错误 |
| 分布类型标签丢失 | "各类型占比"reply 全显示"未知类型 处"且数量空白 | 渲染层依赖约定列名 |
| "两边加起来"答错 | 被翻译成上行/下行，路线条件凭空出现西乡段 | 6 个工程缺陷叠加（见 LD1 分析文档） |
| few-shot 示例污染 | 示例里西乡段占 55%，LLM 把路线名抄进新查询 | 示例未做条件归一化 |

### 1.2 架构级问题：规则写死在代码里

全量代码审查发现（见《写死Bug清单》）：项目的"智能"是**堆规则堆出来的**，几乎所有规则（词表、映射、口径、列名）都硬编码在代码里，且没有统一数据流。

- config 里定义了一份，intent.py / agent.py / nl2sql.py / cross_db.py / orchestrator.py 又各自写死一份
- 新增一个病害类型 / 数据集 / 表 / 比较词，要同时改 3~5 个文件，漏一个就出"看着对、其实错"的 bug
- 这就是"发现一个问题修一个模板"永远修不完的根本原因

### 1.3 已有的正确方向：spec + 编译器架构

《NL2SQL重构方案.md》已经设计了正确的架构方向：

```
自然语言 → spec(JSON，逻辑字段闭集) → 校验 → 编译器 → SQL → 安全校验 → 执行
```

核心思想：**不让 LLM 写物理 SQL，让 LLM 填结构化 JSON，代码把 JSON 确定性编译成 SQL**。

这个架构需要三样基础设施：
1. **逻辑字段→真实列/表达式映射表**（主体→主表+关联链路、字段映射、指标定义）
2. **编译器**（把 spec 编译成物理 SQL，处理 JOIN、字典、方言、del_flag 等）
3. **schema 检索**（给 LLM 提供相关的表/字段信息）

**Wren AI 就是这三样基础设施的成熟实现。**

---

## 二、Wren AI 是什么，核心优势是什么

### 2.1 Wren AI 的核心概念

Wren AI 是一个开源的语义层 + Text-to-SQL 引擎，GitHub 17.6K+ stars。它的核心是 **MDL（Modeling Definition Language）**——用 YAML 配置文件定义业务模型、字段、指标、关系，然后由 Wren Engine 确定性地把引用 MDL 模型的 SQL 编译成物理 SQL。

```
自然语言 → MDL-SQL（引用模型名/字段名/指标名）→ Wren Engine 编译 → 物理 SQL → 执行
```

### 2.2 MDL 示例（基于本项目的真实表）

```yaml
models:
  - name: disease
    table: ai_problem
    description: 病害记录
    columns:
      - name: area
        expression: p.area
        type: number
        description: 面积
      - name: disease_length
        expression: p.disease_length
        type: number
        description: 病害长度
      - name: pile_distance
        expression: p.pile_distance
        type: number
        description: 桩号距离(米)
      - name: pile_number
        expression: p.pile_number
        type: string
        description: 桩号
      - name: collection_time
        expression: p.collection_time
        type: datetime
        description: 采集时间
      - name: confidence
        expression: CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6))
        type: number
        description: AI识别置信度（JSON字段提取）
      - name: has_image
        expression: p.problem_url IS NOT NULL AND p.problem_url <> ''
        type: boolean
        description: 是否有图片
      - name: up_or_down
        expression: p.up_or_down
        type: string
        description: 上下行
      - name: device_code
        expression: p.device_code
        type: string
        description: 设备号
    metrics:
      - name: disease_count
        expression: COUNT(*)
        description: 病害数量
      - name: total_area
        expression: SUM(p.area)
        description: 总面积
      - name: avg_area
        expression: AVG(p.area)
        description: 平均面积
    relations:
      - name: route
        type: many_to_one
        model: route
        join_on: p.routes_id = r.id
      - name: section
        type: many_to_one
        model: section
        join_on: p.sections_id = s.id
      - name: damage_dict
        type: many_to_one
        model: damage_dict
        join_on: p.damage_type = d.problem_code
      - name: patrol
        type: many_to_one
        model: patrol
        join_on: p.patrol_id = t.id

  - name: route
    table: basic_facilities_routes
    description: 路线
    columns:
      - name: code
        expression: r.code
      - name: short_name
        expression: r.short_name
        description: 路线简称
      - name: full_name
        expression: r.full_name
        description: 路线全称
      - name: distance
        expression: r.distance
        description: 路线长度(km)

  - name: damage_dict
    table: ai_problem_base
    description: 病害类型字典
    columns:
      - name: problem_code
        expression: d.problem_code
      - name: problem_type
        expression: d.problem_type
        description: 病害类型名称
      - name: category_name
        expression: CASE d.disease_categories_type WHEN '0' THEN '路面病害' WHEN '5' THEN '路基病害' WHEN '10' THEN '沿路设施' WHEN '15' THEN '交安事件' ELSE CONCAT('其他(', d.disease_categories_type, ')') END
        description: 病害大类名称
```

### 2.3 Wren Engine 编译示例

LLM 生成的 MDL-SQL（很简单，LLM 只需要选对模型和字段）：

```sql
SELECT disease_count
FROM disease
WHERE route.short_name = '宝安大道西乡段'
  AND damage_dict.problem_type = '泛油'
  AND area > 10
  AND confidence >= 0.7
```

Wren Engine 自动编译成物理 SQL（确定性，不会错）：

```sql
SELECT COUNT(*) AS disease_count
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (
  SELECT DISTINCT problem_code, problem_type
  FROM ai_problem_base
  WHERE problem_type IS NOT NULL
) d ON p.damage_type = d.problem_code
WHERE p.del_flag = 0
  AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND d.problem_type = '泛油'
  AND p.area > 10
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) >= 0.7
```

注意：JOIN、字典子查询、JSON 提取、del_flag，**全是 Wren 自动加的**，LLM 不需要管。

### 2.4 Wren 的三大核心优势

#### 优势一：MDL 语义层——解决"规则写死"问题

把表/字段/关系/指标/字典映射从 Python 代码里抽离出来，变成 YAML 配置文件。

- 新增表/字段/指标：只改 MDL YAML，不改代码
- 字典映射（code→名称、大类分组）：在 MDL 里用 calculated column 定义，不再写死在代码的 CASE WHEN 里
- JSON 字段（置信度）：在 MDL 里定义为计算字段，LLM 只需引用 `confidence >= 0.7`，不需要写 `JSON_EXTRACT`
- 业务口径统一：所有查询复用同一套 MDL 定义，不会出现同一个指标在不同问题里口径不一致

#### 优势二：确定性 SQL 编译——解决"LLM 写 SQL 不可靠"问题

LLM 不再直接写物理 SQL，而是写引用 MDL 模型名的 MDL-SQL。Wren Engine 确定性编译为物理 SQL。

- LLM 不需要猜 JOIN 关系（Wren 根据 MDL relation 自动加）
- LLM 不需要猜字典翻译（Wren 根据 MDL 关系自动关联字典表）
- LLM 不需要写 JSON_EXTRACT（MDL 里已经定义为计算字段）
- LLM 不需要记 del_flag（MDL 里可以定义为默认过滤条件）
- 编译结果每次都一样，可复现、可测试

#### 优势三：结构化语义检索——解决"schema 检索不精准"问题

`wren_fetch_context(question)` 基于 MDL 模型定义做结构化检索，返回相关的模型/字段/指标/关系，而不是 RAGFlow 的文本向量匹配。

| 对比维度 | RAGFlow 文本检索 | Wren 结构化检索 |
|---|---|---|
| 检索对象 | 547KB 字段字典的文本片段 | MDL 定义的模型/字段/指标 |
| 检索方式 | 向量相似度匹配（有随机性） | 结构化语义匹配（确定性） |
| 返回内容 | 8 段文字（可能含不相关表、可能漏关键表） | 结构化 JSON（只含相关模型，信息完整） |
| LLM 要做的事 | 阅读理解文字 → 判断哪些表相关 → 猜 JOIN → 写完整 SQL | 选对模型和字段 → 写简单 MDL-SQL |
| 每次结果一致性 | 可能不一样 | 完全一致 |

---

## 三、Wren 能解决什么、不能解决什么

### 3.1 Wren 能解决的（核心价值）

| 痛点 | Wren 如何解决 | 对应文档案例 |
|---|---|---|
| LLM 直接写物理 SQL 不可靠 | LLM 写 MDL-SQL，Wren Engine 确定性编译 | 字段映射错、JOIN 错、方言错 |
| 字段映射写死在代码里 | MDL 定义逻辑字段→真实列映射，配置化 | 设备→vehicle_id（正确 device_code） |
| JSON 字段查询困难 | MDL 把 confidence 定义为计算字段，LLM 只需引用 | 置信度条件被丢弃（D1） |
| 规则写死、新增要改多个文件 | MDL YAML 统一管理，新增只改配置 | 写死Bug清单全部 P0/P1 |
| schema 检索不精准、召回不相关表 | wren_fetch_context 结构化检索 | few-shot 示例污染、RAG 召回无关表 |
| 字典映射写死在 CASE WHEN | MDL calculated column 定义字典映射 | 巡查类型/病害大类 code→名称写死 |
| 跨数据库方言差异 | Wren Engine 适配不同数据库方言 | MySQL/PG 双库 |

### 3.2 Wren 不能解决的（必须保留现有逻辑或自行修复）

| 痛点分类 | 具体内容 | 为什么 Wren 不解决 | 处理方式 |
|---|---|---|---|
| **业务实体解析** | 桩号解析（K1+000~K3+000→BETWEEN 1000 AND 3000）、路线名称模糊匹配、病害类型识别、时间解析（上个月/去年）、数值阈值识别（面积>10㎡）、空间分段（前500米/后500米） | 这些是业务领域的自然语言理解，Wren 不涉及 | **全部保留现有 parsers.py / nl2sql.py 的解析逻辑**，这是项目核心资产 |
| **多轮对话状态管理** | 会话上下文继承、subject_pool（"两边加起来"要知道是哪两边）、指代消解 | Wren 是单轮查询引擎，不维护对话状态 | **保留现有 memory.py，修复 payload 漏 context 字段的 bug**（LD1 环节①） |
| **查询形状识别** | count/list/distribution/top/aggregate/sum 等形状识别 | Wren 不做意图分类 | **保留现有 parsers.parse_query_shape，增加 sum/top 等缺失类别** |
| **意图分类** | data/knowledge/image/composite 意图判断 | Wren 只处理数据查询 | **保留现有 intent.py** |
| **跨库分流** | MySQL/PostgreSQL/SeaweedFS 数据源判定和拆解 | Wren 一个 project 绑定一个数据源 | **保留现有 cross_db.py**，MySQL/PG 各建一个 Wren project |
| **结果渲染** | 多列输出、图表生成、摘要渲染、列名识别 | Wren 只负责生成 SQL 和执行，不负责结果展示 | **保留现有 agent.py / analysis.py，修复多列只取第一列等渲染 bug** |
| **安全校验** | 表白名单、危险词拦截、del_flag 强制、LIMIT、单条语句 | Wren 内部执行不经过现有校验 | **保留现有 sql_engine.validate_sql，Wren 编译出的物理 SQL 仍然走这层校验** |
| **数据库连接** | SSH 隧道、连接池、docker exec | Wren datasource 需要直连端口 | **保留现有 db.py，Wren 只做编译不做执行（Direct API 模式），或配置 Wren 连隧道端口** |
| **代码 bug** | payload 漏 context、假前缀、validate_semantics 单向校验 | 纯代码 bug，与引擎无关 | **按 LD1 分析文档和写死Bug清单修复** |

### 3.3 一句话边界

**Wren 替换的是"spec→SQL 编译器"和"逻辑字段映射表"和"schema 检索"，不是替换整个项目。**

你的业务解析逻辑（桩号、路线、病害字典、多轮对话、意图分类、跨库分流、结果渲染、安全校验）都是核心资产，全部保留。

---

## 四、整体集成架构

### 4.1 目标架构

```
自然语言
  │
  ├─ ① 意图分类（intent.py，保留）
  │    ├─ knowledge → RAGFlow 知识问答（保留）
  │    ├─ image → SeaweedFS 图片查询（保留）
  │    └─ data → 进入数据查询链路
  │
  ├─ ② 跨库分流（cross_db.py，保留）
  │    ├─ MySQL → Wren project: road_mysql
  │    └─ PostgreSQL → Wren project: road_pg
  │
  ├─ ③ 业务实体解析（parsers.py / nl2sql.py，保留）
  │    ├─ 路线匹配 → route_id
  │    ├─ 桩号解析 → pile_start/pile_end
  │    ├─ 病害类型识别 → damage_type
  │    ├─ 时间解析 → time_start/time_end
  │    ├─ 数值阈值识别 → numeric_conditions
  │    ├─ 空间分段 → spatial_split
  │    └─ 查询形状 → shape（count/list/distribution/top/sum）
  │
  ├─ ④ 会话上下文管理（memory.py，保留并修复 bug）
  │    ├─ 继承上一轮的路线/桩号/病害条件
  │    └─ subject_pool（多轮汇总/对比用）
  │
  ├─ ⑤ 确定性模板快路径（nl2sql.py build_template_sql，保留）
  │    └─ 模板命中 → 直接生成物理 SQL → 跳到 ⑧
  │
  ├─ ⑥ LLM 基于 Wren MDL 上下文生成 MDL-SQL（新增）
  │    ├─ wren_fetch_context(question) → 结构化模型/字段/指标信息
  │    ├─ 现有 parsers 解析出的 slots 注入 prompt
  │    └─ LLM 生成 MDL-SQL（引用模型名，不写物理表名）
  │
  ├─ ⑦ Wren Engine 编译为物理 SQL（新增）
  │    ├─ toolkit.dry_plan(mdl_sql) → physical_sql
  │    ├─ 编译失败 → 重试一次 → 仍失败 → 追问用户（不静默降级）
  │    └─ 物理 SQL
  │
  ├─ ⑧ 安全校验（sql_engine.validate_sql，保留）
  │    ├─ 只读 SELECT
  │    ├─ 危险词拦截
  │    ├─ 表白名单
  │    ├─ del_flag 强制（MySQL）
  │    ├─ 方言检测
  │    └─ LIMIT 补全
  │
  ├─ ⑨ 语义校验（nl2sql.validate_semantics，保留并增强）
  │    ├─ 必须条件检查（缺路线/桩号/病害/时间）
  │    └─ 多余条件检查（新增：SQL 里不能出现用户没声明的条件）
  │
  ├─ ⑩ 执行（db_manager.query，保留 SSH 隧道）
  │
  └─ ⑪ 结果后处理（agent.py / analysis.py，保留并修复渲染 bug）
       ├─ 多列输出（修复只取第一列）
       ├─ 图表生成
       ├─ 结果自检（形状/空值/自相矛盾）
       ├─ 摘要渲染（不美化错误结果）
       └─ 范围校准（calibrate.py，保留）
```

### 4.2 三级降级策略

```
用户提问
  → ① 确定性模板（快路径，命中即用）
  → ② Wren MDL-SQL + Wren Engine 编译（主路径，模板弃权时走）
  → ③ 内置 LLM 直接生成物理 SQL（兜底路径，Wren 编译失败时走）
```

每一级都有明确的开关（config 配置），可单独关闭回退。

---

## 五、分阶段实施步骤

### 阶段一：止血（必须先做，与 Wren 无关，1-2 天）

这些是纯代码 bug，不修好 Wren 也救不了你。按优先级排列：

#### 1.1 修复 payload 漏 context 字段（LD1 环节①，P0）

**文件**：`backend/app/services/agent.py` 正常回答分支（约 1096~1119 行）

**问题**：构造的 payload 没有 `"context"` 字段，导致 `memory_service.save_session()` 收到 None 直接不保存，多轮继承彻底失效。

**修复**：在 payload 中补上 `"context": result.get("context")`，与 retry 分支（约 1059~1079 行）和 calibrate 分支保持一致。

**验收**：连续提问 R1（西乡段多少处）→ R2（恒明珠段呢）→ R3（两边加起来呢），R3 能继承到 R1/R2 的路线条件。

#### 1.2 修复假前缀（LD1 环节②，P0）

**文件**：`backend/app/services/agent.py` 约 1141~1145 行

**问题**：`effective_message != message` 的判定仅仅因为 normalize 去掉了"呢"就误判"消息被改写过"，给回答加了"已沿用上一条问题的条件"前缀，但实际什么都没继承。

**修复**：
- `effective_message` 与 `normalized`（normalize 后的值）比较，而不是与原始 `message` 比较
- 仅当 `inherited` 非空时才加"已沿用上一条问题的条件"前缀，空继承一律不加

#### 1.3 增加 sum/comparison 语义槽（LD1 环节③，P0）

**文件**：`backend/app/services/nl2sql.py` 的 `is_comparison` 和 `parsers.py` 的 `parse_query_shape`

**问题**：`_COMPARISON_HINTS` 没有"加起来/合计/总共/两边/一共/总和/汇总"，`parse_query_shape` 没有 sum 类别，"两边加起来呢？"退化成 detail 空条件查询。

**修复**：
- `_COMPARISON_HINTS` 增加 `("加起来", "合计", "总共", "一共", "两边", "总和", "汇总")`
- `parse_query_shape` 增加 `sum` 类别（触发词：加起来/合计/总共/总和）
- 命中 sum 后进入对比/汇总专用处理，不走内置 LLM 自由生成

#### 1.4 validate_semantics 增加"多条件"校验（LD1 环节⑤，P1）

**文件**：`backend/app/services/nl2sql.py` 的 `validate_semantics`

**问题**：只检查"该有的条件缺没缺"，不检查"SQL 里多出来的条件是不是用户要的"。plan 里没有 route 条件时，带 `r.full_name='宝安大道西乡段'` 的 SQL 零拦截通过。

**修复**：增加"多余条件检测"——plan 的 slots 里没有的过滤词（尤其是继承来的 damage/route/pile），如果在 SQL 的 WHERE 里出现且不是用户本句要求的，应报警。

#### 1.5 few-shot 示例治理（LD1 环节④，P1）

**文件**：`backend/app/services/nl2sql.py` 的 example pair 相关函数

**问题**：示例入库时路线名是真实值，LLM 生成 SQL 时会把示例里的路线名抄进新查询（西乡段占示例 55%）。

**修复**：
- 示例入库时做条件归一化（路线名/ID 用占位符如 `{route_id}`，病害类型用 `{damage_type}`）
- 检索示例时按结构相似度（shape/主体数）匹配，而不是纯文本相似度
- 模板能覆盖的场景一律不进 LLM

### 阶段二：Wren 环境搭建与 MDL 建模（核心工作量，3-5 天）

#### 2.1 安装 Wren CLI 并初始化项目

```bash
# 安装
pip install wrenai

# 初始化 MySQL project
wren profile add road_mysql --datasource mysql
wren context init
wren context set-profile road_mysql
# 配置数据库连接（SSH 隧道后的本地端口）
# 编辑 .wren/profiles/road_mysql.yml

# 初始化 PostgreSQL project
wren profile add road_pg --datasource postgres
wren context init
wren context set-profile road_pg

# 构建 MDL
wren context build  # 生成 target/mdl.json
```

**注意**：Wren datasource 需要直连数据库端口。由于项目走 SSH 隧道，需要在 Wren profile 中配置隧道后的本地端口（如 127.0.0.1:3307），并保持隧道常驻。

#### 2.2 用 wren-generate-mdl 生成初始 MDL

Wren 提供了 `wren-generate-mdl` skill，可以自动扫描数据库 schema 生成初始 MDL：

```bash
wren generate-mdl --project road_mysql --output models/
```

这会自动生成所有表的基础模型定义（表名、字段名、字段类型）。

#### 2.3 人工补充业务语义（核心工作量）

自动生成的 MDL 只有物理表结构，需要人工补充业务语义。按以下优先级建模：

**P0 必须建模的模型（核心查询链路）：**

| MDL 模型 | 物理表 | 关键计算字段 | 关键关系 |
|---|---|---|---|
| disease | ai_problem | confidence（JSON提取）、has_image、damage_type_name（经字典） | route、section、damage_dict、patrol |
| route | basic_facilities_routes | - | - |
| section | basic_facilities_sections | surface_type_name、tech_grade_name（code→名称） | route |
| damage_dict | ai_problem_base | category_name（大类名称） | - |
| pile_point | basic_facilities_sections_pile_detail | - | section、route |
| patrol | ai_maintain_patrol | patrol_type_name、patrol_status_name（code→名称） | - |

**P1 建议建模的模型（扩展查询能力）：**

| MDL 模型 | 物理表 | 说明 |
|---|---|---|
| work_order | ai_work_order | AI巡检工单 |
| maintenance_work_order | maintenance_work_order | 养护工单 |
| maintenance_project | maintenance_project | 养护项目 |
| bridge | basic_facilities_bridges | 桥梁 |
| tunnel | basic_facilities_tunnels | 隧道 |
| slope | basic_facilities_slope | 边坡 |
| asset | ai_asset_main | 巡查资产 |

**P2 PostgreSQL 模型：**

| MDL 模型 | 物理表 | 说明 |
|---|---|---|
| gps_point | gps_points | GPS轨迹点 |
| inclination_point | inclination_points | 倾角监测点 |

#### 2.4 MDL 建模关键规则

**规则一：计算字段（Calculated Column）**

把复杂表达式封装成计算字段，LLM 只需引用字段名：

```yaml
columns:
  - name: confidence
    expression: CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6))
    type: number
    description: AI识别置信度
  - name: has_image
    expression: p.problem_url IS NOT NULL AND p.problem_url <> ''
    type: boolean
    description: 是否有图片
  - name: damage_type_name
    expression: COALESCE(d.problem_type, '未知类型')
    type: string
    description: 病害类型名称（经字典关联，空值显示未知类型）
```

**规则二：字典映射（Code → 名称）**

不再写死在代码的 CASE WHEN 里，在 MDL 中定义：

```yaml
# patrol 模型
columns:
  - name: patrol_type_name
    expression: CASE t.patrol_type WHEN '0' THEN '定期巡查' WHEN '5' THEN '日常巡查' WHEN '10' THEN '专项巡查' WHEN '15' THEN '人工复核' WHEN '20' THEN '重点巡查' ELSE t.patrol_type END
    description: 巡查类型名称
```

**规则三：指标（Metric）**

把常用聚合定义为指标：

```yaml
metrics:
  - name: disease_count
    expression: COUNT(*)
    description: 病害数量
  - name: total_area
    expression: SUM(p.area)
    description: 总面积
  - name: avg_area
    expression: AVG(p.area)
    description: 平均面积
```

**规则四：关系（Relation）**

定义模型之间的关联，Wren 编译时自动加 JOIN：

```yaml
relations:
  - name: route
    type: many_to_one
    model: route
    join_on: p.routes_id = r.id
  - name: damage_dict
    type: many_to_one
    model: damage_dict
    join_on: p.damage_type = d.problem_code
```

#### 2.5 验证 MDL 正确性

```bash
# 构建 MDL
wren context build

# 列出模型
wren list-models

# 测试编译（dry_plan，不执行）
wren dry-plan "SELECT disease_count FROM disease WHERE route.short_name = '宝安大道西乡段'"

# 测试执行
wren query "SELECT disease_count FROM disease WHERE route.short_name = '宝安大道西乡段'"
```

验收标准：
- 所有 P0 模型都能正确编译和执行
- 计算字段（confidence、has_image）返回正确值
- 字典映射（damage_type_name）返回正确名称
- 关系 JOIN 自动生成，不需要手写

### 阶段三：Wren 集成代码改动（2-3 天）

#### 3.1 新增 wren_engine.py

**文件**：`backend/app/services/wren_engine.py`（新增）

封装 WrenToolkit，提供以下接口：

```python
from wren_pydantic import WrenToolkit

class WrenEngine:
    def __init__(self):
        self._mysql_toolkit = None
        self._pg_toolkit = None

    def _get_toolkit(self, kind: str) -> WrenToolkit:
        if kind == "mysql":
            if self._mysql_toolkit is None:
                self._mysql_toolkit = WrenToolkit.from_project("./wren_projects/road_mysql")
            return self._mysql_toolkit
        else:
            if self._pg_toolkit is None:
                self._pg_toolkit = WrenToolkit.from_project("./wren_projects/road_pg")
            return self._pg_toolkit

    def compile_mdl_sql(self, kind: str, mdl_sql: str) -> str:
        """把 MDL-SQL 编译为物理 SQL（dry_plan）"""
        toolkit = self._get_toolkit(kind)
        return toolkit.dry_plan(mdl_sql)

    def fetch_context(self, kind: str, question: str) -> dict:
        """获取结构化语义上下文（替代 RAGFlow schema 检索）"""
        toolkit = self._get_toolkit(kind)
        return toolkit.memory.fetch(question)

    def recall_queries(self, kind: str, question: str, limit: int = 3) -> list:
        """召回相似的历史 NL→SQL 对"""
        toolkit = self._get_toolkit(kind)
        return toolkit.memory.recall(question, limit=limit)

    def store_query(self, kind: str, nl: str, sql: str, tags: list = None):
        """存储确认正确的 NL→SQL 对（条件归一化后存储）"""
        toolkit = self._get_toolkit(kind)
        # 存储前做条件归一化：路线名/ID 用占位符，防止示例污染
        normalized_sql = self._normalize_sql(sql)
        toolkit.memory.store(nl=nl, sql=normalized_sql, tags=tags or [])

wren_engine = WrenEngine()
```

**注意**：使用 Direct API 模式（`toolkit.dry_plan` / `toolkit.query` / `toolkit.memory.*`），不引入 Pydantic AI 的 Agent 循环。这样 LLM 调用仍然走现有的 `llm.py` 封装，风险最小。

#### 3.2 修改 retrieve_query_context

**文件**：`backend/app/services/nl2sql.py` 的 `retrieve_query_context`

在 RAGFlow 检索结果之外，追加 Wren 的结构化语义上下文：

```python
def retrieve_query_context(message, kind, plan):
    # 现有 RAGFlow 检索（保留，作为补充）
    schema = _ragflow_schema_retrieval(message, kind)
    docs = _ragflow_docs_retrieval(message, kind)
    examples = _ragflow_examples_retrieval(message, kind)

    # 新增：Wren 结构化语义上下文（主路径）
    try:
        wren_context = wren_engine.fetch_context(kind, message)
        # 把 Wren 返回的模型/字段/指标信息格式化为 prompt 文本
        wren_schema_text = _format_wren_context(wren_context)
        # Wren 上下文优先，RAGFlow 作为补充
        schema = wren_schema_text + "\n\n" + schema
    except Exception as e:
        log_error("wren", "fetch_context_failed", e)
        # Wren 失败时降级为纯 RAGFlow

    return {"schema": schema, "docs": docs, "examples": examples}
```

#### 3.3 修改 generate_sql 的 prompt

**文件**：`backend/app/services/sql_engine.py` 的 `generate_sql`

prompt 中增加 MDL 模型/字段列表，要求 LLM 生成 MDL-SQL：

```python
prompt = f"""
You are a data query expert for a road inspection and maintenance database.
Generate one MDL-SQL query that answers the question exactly.

===可用业务模型（MDL，优先依据）
{wren_models_text}

===业务规则
- 引用模型名和字段名，不要写物理表名（如 ai_problem、basic_facilities_routes）
- 病害类型用 damage_dict.problem_type（名称），不要用 damage_type（code）
- 置信度用 confidence 字段（已自动提取 JSON）
- 有没有图片用 has_image 字段（布尔值）
- 路线名称用 route.short_name 或 route.full_name

===必须条件
{required_text}

===已解析的业务槽位
{slots}

Question: {question}
"""
```

#### 3.4 修改 agent.py 的 SQL 生成链路

**文件**：`backend/app/services/agent.py` 的 `_build_query_result`

在 LLM 生成 MDL-SQL 后，增加 Wren 编译步骤：

```python
# 模板命中 → 直接用模板 SQL
if template_sql:
    sql = template_sql
    from_template = True
# 模板弃权 → Wren 路径
elif settings.nl2sql_engine in {"wren", "hybrid"}:
    # LLM 生成 MDL-SQL
    mdl_sql = nl2sql.generate_with_context(plan, context, feedback=last_error)
    # Wren Engine 编译为物理 SQL
    try:
        physical_sql = wren_engine.compile_mdl_sql(kind, mdl_sql)
        sql = physical_sql
    except Exception as e:
        # 编译失败 → 重试一次
        last_error = str(e)
        try:
            mdl_sql = nl2sql.generate_with_context(plan, context, feedback=last_error)
            physical_sql = wren_engine.compile_mdl_sql(kind, mdl_sql)
            sql = physical_sql
        except Exception as e2:
            # 仍失败 → 降级为内置 LLM 直接生成物理 SQL
            last_error = str(e2)
            sql = None
# 兜底 → 内置 LLM 直接生成物理 SQL
if not sql:
    sql = nl2sql.generate_with_context(plan, context, feedback=last_error)
```

**关键**：Wren 编译出的物理 SQL 仍然走现有 `validate_sql` + `validate_semantics` + `db_manager.query`，安全校验和执行层不变。

#### 3.5 修改 config.py 和 requirements.txt

**config.py 增加：**
```python
# Wren AI 配置
nl2sql_engine: str = "hybrid"  # 可选: builtin / wren / hybrid
wren_mysql_project_path: str = "wren_projects/road_mysql"
wren_pg_project_path: str = "wren_projects/road_pg"
wren_enabled: bool = True
```

**requirements.txt 增加：**
```
wren-pydantic[mysql,postgres,memory]>=0.1.0
```

### 阶段四：记忆与示例治理（1-2 天）

#### 4.1 用 Wren 记忆替代 example_pairs

- 现有 RAGFlow "SQL问答示例" 数据集逐步迁移到 Wren 记忆
- 存储时做条件归一化（路线名/ID 用占位符，病害类型用占位符）
- 召回时按结构相似度（shape/主体数）匹配

#### 4.2 模板能覆盖的场景一律不进 LLM

- 现有 20+ 确定性模板保留作为快路径
- LLM（+Wren）只处理模板弃权的复杂场景
- 在 `agent.py` 中明确模板优先的逻辑

### 阶段五：验收与对拍（持续）

用 `docs/评测问题集与标准答案.md` 逐题对拍：

| 验收维度 | 标准 |
|---|---|
| 准确率 | Wren 路径 ≥ 现链路（模板+Vanna+内置LLM） |
| 耗时 | Wren 路径 ≤ 现链路（Wren 编译是确定性的，比 LLM 多轮 Agent 快） |
| 复杂查询 | 数值阈值、JSON字段、多指标、top查询、主体识别等历史失败案例通过率显著提升 |
| 可复现性 | 同一问题多次执行结果一致（Wren 编译确定性） |
| 回退能力 | 关闭 Wren 开关后能正常回退到现链路 |

---

## 六、关键风险与注意事项

### 6.1 MDL 建模是最大工作量

20+ 张表的业务语义定义（尤其是病害类型字典、路线关系、桩号口径、置信度 JSON 字段）需要人工投入。Wren 的 `wren-generate-mdl` 只能生成初始版本（物理表结构），业务语义必须人工补充。

**建议**：先建 P0 的 6 个核心模型（disease/route/section/damage_dict/pile_point/patrol），覆盖 80% 的查询场景，再逐步扩展。

### 6.2 SSH 隧道问题

Wren datasource 需要直连数据库端口。项目走 SSH 隧道，有两种处理方式：

**方式一（推荐）：Wren 只做编译，不做执行**
- 使用 Direct API 模式：`toolkit.dry_plan(mdl_sql)` 只编译不执行
- 执行仍然走现有 `db_manager.query`（保留 SSH 隧道、连接池）
- Wren 不需要连数据库，只需要 MDL 定义
- 风险最小，推荐先用这个模式

**方式二：Wren 连隧道端口执行**
- 在 Wren profile 中配置隧道后的本地端口（如 127.0.0.1:3307）
- 保持 SSH 隧道常驻
- Wren 的 `wren_query` 工具可以直接执行
- 但 Wren 内部执行不经过现有 `validate_sql`，需要额外注意安全

### 6.3 MDL-SQL 的表达能力边界

Wren 的 MDL-SQL 主要支持标准查询（SELECT/JOIN/WHERE/GROUP BY/ORDER BY/LIMIT）。对于项目现有的复杂模板，需要验证是否能表达：

| 复杂场景 | MDL-SQL 能否表达 | 处理方式 |
|---|---|---|
| 多口径 SUM(CASE WHEN...) | 可以（用多个指标或计算字段） | 在 MDL 中定义为指标 |
| RANK() OVER（并列 top） | 可以（窗口函数） | 验证 Wren 是否支持 |
| 空间分段（前N米/后N米） | 可以（WHERE 条件） | parsers 解析后注入条件 |
| 类型分布子查询（GROUP_CONCAT） | 可能受限 | 保留现有模板，不走 Wren |
| UNION ALL（多表计数） | 可能受限 | 保留现有模板，不走 Wren |
| 复杂子查询嵌套 | 可能受限 | 保留现有模板，不走 Wren |

**策略**：MDL-SQL 能表达的场景走 Wren，表达不了的复杂场景保留现有模板，不走 Wren。模板优先，Wren 处理模板弃权的标准查询。

### 6.4 通义千问兼容性

使用 Direct API 模式时，LLM 调用仍然走现有的 `llm.py` 封装（OpenAI 兼容接口调通义千问），无兼容性问题。

如果后续要使用 Pydantic AI 的 Agent 循环模式，需要验证 Pydantic AI 的 OpenAI 兼容客户端能否正常调用通义千问（base_url + api_key）。

### 6.5 不要一次性全量切换

- 先在少数复杂查询场景（模板弃权的场景）试点 Wren，对比准确率
- 逐步扩大覆盖范围
- 保留现有模板和内置 LLM 作为兜底，可随时回退
- config 中保留 `nl2sql_engine` 开关（builtin/wren/hybrid）

### 6.6 Wren 记忆的并发问题

Wren 文档明确说明：**不要在 Agent 使用同一 project 的同时运行 `wren memory index`**，因为索引操作会删除并重建 LanceDB schema 表，并发读取可能瞬时失败。

记忆索引操作应在低峰期或维护窗口执行。

---

## 七、文件改动清单

### 新增文件

| 文件 | 说明 |
|---|---|
| `backend/app/services/wren_engine.py` | Wren 引擎封装（compile_mdl_sql / fetch_context / recall / store） |
| `wren_projects/road_mysql/` | MySQL Wren project（MDL YAML + profile + target/mdl.json） |
| `wren_projects/road_pg/` | PostgreSQL Wren project |

### 修改文件

| 文件 | 改动内容 | 阶段 |
|---|---|---|
| `backend/app/services/agent.py` | 修复 payload 漏 context、修复假前缀、增加 Wren 编译步骤 | 一、三 |
| `backend/app/services/nl2sql.py` | 增加 sum 语义、validate_semantics 多条件校验、retrieve_query_context 增加 Wren 上下文 | 一、三 |
| `backend/app/services/parsers.py` | parse_query_shape 增加 sum/top 类别 | 一 |
| `backend/app/services/sql_engine.py` | generate_sql prompt 增加 MDL 模型列表，要求生成 MDL-SQL | 三 |
| `backend/app/config.py` | 增加 Wren 相关配置 | 三 |
| `requirements.txt` | 增加 wren-pydantic 依赖 | 三 |

### 保留不变的文件（核心资产）

| 文件 | 为什么保留 |
|---|---|
| `backend/app/services/parsers.py` | 业务实体解析（桩号/路线/病害/时间/数值阈值），Wren 不涉及 |
| `backend/app/services/cross_db.py` | 跨库分流，Wren 一个 project 绑一个数据源 |
| `backend/app/services/intent.py` | 意图分类，Wren 只处理数据查询 |
| `backend/app/services/memory.py` | 多轮对话状态管理，Wren 是单轮引擎 |
| `backend/app/services/db.py` | 数据库连接（SSH 隧道/连接池），Wren Direct API 模式不接管执行 |
| `backend/app/services/calibrate.py` | 范围校准，与 Wren 无关 |
| `backend/app/services/analysis.py` | 图表分析，与 Wren 无关 |
| `backend/app/services/clarify.py` | 澄清追问，与 Wren 无关 |
| `backend/app/services/rag.py` | RAGFlow 知识问答，Wren 只替代 schema 检索部分 |
| `backend/app/services/vanna_engine.py` | 已移除（见 [RAGFlow知识库精简方案.md](./RAGFlow知识库精简方案.md) §3.1） |

---

## 八、总结

### Wren 集成的核心价值

1. **解决"LLM 写物理 SQL 不可靠"**：LLM 写 MDL-SQL，Wren Engine 确定性编译，JOIN/字典/JSON/del_flag 自动处理
2. **解决"规则写死在代码里"**：MDL YAML 统一管理表/字段/关系/指标/字典映射，新增只改配置
3. **解决"schema 检索不精准"**：wren_fetch_context 结构化检索，替代 RAGFlow 文本向量匹配
4. **解决"few-shot 示例污染"**：Wren 记忆支持条件归一化，路线名不再被抄进新查询

### 必须保留的现有资产

- 业务实体解析（桩号/路线/病害/时间/数值阈值/空间分段）
- 多轮对话状态管理（修复 bug 后）
- 意图分类、跨库分流
- 安全校验（validate_sql）
- 数据库连接（SSH 隧道）
- 结果渲染、范围校准、图表分析
- 20+ 确定性模板（作为快路径）

### 实施节奏

| 阶段 | 内容 | 工作量 | 优先级 |
|---|---|---|---|
| 一 | 止血（5 个纯代码 bug） | 1-2 天 | P0 必须先做 |
| 二 | Wren 环境搭建 + MDL 建模 | 3-5 天 | P0 核心工作量 |
| 三 | Wren 集成代码改动 | 2-3 天 | P1 |
| 四 | 记忆与示例治理 | 1-2 天 | P2 |
| 五 | 验收与对拍 | 持续 | P1 |

**总工作量约 1-2 周**，其中 MDL 建模占一半。

### 最终目标架构

```
确定性模板（快路径）→ 命中即用
    ↓ 弃权
Wren MDL-SQL + Wren Engine 编译（主路径）→ 确定性 SQL
    ↓ 编译失败
内置 LLM 直接生成物理 SQL（兜底路径）
    ↓
统一安全校验 → 统一执行 → 统一结果后处理
```

三级降级，每级可独立开关，可随时回退。
