# resources/ —— 会变的业务知识都放这里

依据 `docs/代码结构重构与模板外置方案.md`：

- `prompts/`（项目根）：给 LLM 的提示词，已经在外置，原位不动。
- `resources/`（本目录）：**确定性知识 / 配置**，随 git 管理。
- `backend/data/`：运行期产物（agent.db、schema_catalog.json 缓存），不进 git 语义。

## 目录

所有人工维护的语义与能力映射统一放在 `capabilities/`。下表是逐文件职责的唯一权威清单；
项目原则只引用本表，不重复维护另一份职责映射。

| 文件 | 唯一职责 | 禁止内容 |
| --- | --- | --- |
| `index.yaml` | 能力文件清单和配置版本 | 字段、指标、枚举或业务规则 |
| `subjects.yaml` | 查询主体、主表、MDL 模型名和允许视图 | JOIN 关系；表间关系只以 Wren MDL 为准 |
| `mdl_compilation.yaml` | 确定性编译与 LLM 兜底的策略 | 字段、指标或业务枚举定义 |
| `fields.yaml` | 逻辑字段到物理字段或受控表达式的绑定 | 手写枚举值到中文标签的映射 |
| `field_overrides.yaml` | 表中文名、非枚举字段的标签例外、字段别名和角色 | `code_map`、枚举标签副本、MDL 关系 |
| `metrics.yaml` | 原子指标的标准 ID、表达式、单位和别名 | 时间分桶、维度词表或口语候选规则 |
| `derived_metrics.yaml` | post_calc 派生指标 | 可登记为原子指标的公式 |
| `enums.yaml` | 表 + 字段的编码、标准 label、display 和 SQL CASE 生成依据 | 另一份 `code_map` 或手写枚举 CASE |
| `entities.yaml` | 业务实体字典来源 | 字段绑定或指标公式 |
| `entity_hierarchies.yaml` | 病害等实体族层级 | 实体原始取值副本 |
| `aliases.yaml` | 实体、字段和触发词别名 | 枚举编码、指标公式或 JOIN |
| `identifiers.yaml` | XJJL、XCBH 等业务编号定义 | 具体工单 ID 或 SQL |
| `query_shapes.yaml` | 查询形状和语义键 | 具体业务数据条件 |
| `dimensions.yaml` | 分组维度词表 | 指标公式或时间窗口规则 |
| `predicates.yaml` | SQL 谓词槽位规则 | 自然语言意图判断 |
| `time_buckets.yaml` | 时间分桶表达式 | 字段别名或查询结果模板 |
| `result_words.yaml` | 结果展示词与领域标签 | 业务执行口径 |
| `repair_policy.yaml` | 失败修复策略 | 单题修复特判 |
| `calculators.yaml` | 已登记计算工具的输入、来源和参数约束 | 工具实现代码或自然语言意图判断 |
| `templates.yaml` | 内置模板与模板能力声明 | 运行期自动沉淀模板 |
| `capability_gaps.yaml` | 已确认不可用或不支持的能力边界 | 可用字段或指标定义 |
| `preset_bindings.yaml` | QuerySpec 字段到预置模板参数名的绑定 | SQL、指标公式或业务过滤值 |
| `assist_rules.yaml` | 口语关键词和固定格式候选证据 | 直接决定语义、过滤或 SQL；输出必须 `trusted=false` |

## 怎么改

- **加一个实体别名**：改 `capabilities/aliases.yaml`。
- **加一个实体族或成员**：改 `capabilities/entity_hierarchies.yaml`，成员必须存在于数据库实体字典。
- **加/改字段编码映射**：只改 `capabilities/enums.yaml`；`COLUMN_COMMENT` 只能作为发现线索，
  `field_overrides.yaml` 禁止保存 `code_map`。
- **生成枚举中文显示**：只从 `capabilities/enums.yaml` 生成 CASE；禁止在 `fields.yaml`、
  `overrides.yml` 或代码常量中手抄一遍。
- **表中文名**：只由 `field_overrides.yaml` 提供；Wren 描述只写业务约束，不得再维护另一份表名。
- **加指标**：原子指标写 `metrics.yaml`，派生指标写 `derived_metrics.yaml`。
- **指标身份**：同一公式必须使用同一标准 ID 和显式别名；不得用两个 ID 各存一份相同公式。
- **加内置模板**：在 `capabilities/templates.yaml` 登记，SQL 正文写同名 `.yaml`。
- **新增人工配置文件**：先在职责表登记，再同步 `index.yaml` 和加载器；不得出现未登记配置。
- **独立加载入口**：统一能力注册表读取核心能力文件；`assist_rules.yaml` 由语义辅助引擎读取，
  `preset_bindings.yaml` 由预置模板绑定器读取。文件有独立消费者也必须在本表登记，
  不能因为不在统一注册表中就变成无主配置。
- **生成物**：`models/*/metadata.yml`、`target/mdl.json`、缓存和运行期模板由 owner 生成，
  禁止手工修改后当作人工事实源。
- 禁止在 `.py` 文件中新增业务枚举、实体别名、病害层级、指标名或字段到 SQL 的映射。

## 当前职责债务

当前没有已核实的同类职责债务。新增或迁移配置时仍需先登记职责，并同步删除旧位置；
发现问题时重新登记，不能把本清单当作永久例外。

每次验收都必须检查本清单是否缩小；修改完成后同步删除已解决项，禁止把债务清单当永久例外。

## 热更新

`resource_loader` 按文件 `(mtime, size)` 判版本，改完文件下一次读取即生效，不用重启；
启动时会做一次资源自检（文件存在、YAML 可解析、模板 id 唯一），结果写日志。
