# resources/ —— 会变的业务知识都放这里

依据 `docs/代码结构重构与模板外置方案.md`：

- `prompts/`（项目根）：给 LLM 的提示词，已经在外置，原位不动。
- `resources/`（本目录）：**确定性知识 / 配置**，随 git 管理。
- `backend/data/`：运行期产物（agent.db、schema_catalog.json 缓存），不进 git 语义。

## 目录

所有人工维护的语义与能力映射统一放在 `capabilities/`：

| 文件 | 放什么 |
| --- | --- |
| `subjects.yaml` | 查询主体、主表和关联链路 |
| `fields.yaml` | 逻辑字段到物理字段或受控表达式的绑定 |
| `field_overrides.yaml` | 数据库字段元数据的例外覆盖 |
| `metrics.yaml` | 原子指标和旧语义词表 |
| `derived_metrics.yaml` | post_calc 派生指标 |
| `enums.yaml` | 按表 + 字段登记的枚举补充 |
| `entities.yaml` | 业务实体字典来源 |
| `entity_hierarchies.yaml` | 病害等实体族层级 |
| `aliases.yaml` | 实体、字段和触发词别名 |
| `identifiers.yaml` | XJJL、XCBH 等业务编号定义 |
| `query_shapes.yaml` | 查询形状和语义键 |
| `dimensions.yaml` | 分组维度词表 |
| `predicates.yaml` | SQL 谓词槽位规则 |
| `time_buckets.yaml` | 时间分桶表达式 |
| `result_words.yaml` | 结果展示词与领域标签 |
| `repair_policy.yaml` | 失败修复策略 |
| `templates.yaml` | 内置模板与模板能力声明 |

## 怎么改

- **加一个实体别名**：改 `capabilities/aliases.yaml`。
- **加一个实体族或成员**：改 `capabilities/entity_hierarchies.yaml`，成员必须存在于数据库实体字典。
- **加/改字段编码映射**：优先改数据库 `COLUMN_COMMENT`；解析不全时改 `capabilities/enums.yaml` 或 `field_overrides.yaml`。
- **加指标**：原子指标写 `metrics.yaml`，派生指标写 `derived_metrics.yaml`。
- **加内置模板**：在 `capabilities/templates.yaml` 登记，SQL 正文写同名 `.yaml`。
- 禁止在 `.py` 文件中新增业务枚举、实体别名、病害层级、指标名或字段到 SQL 的映射。

## 热更新

`resource_loader` 按文件 `(mtime, size)` 判版本，改完文件下一次读取即生效，不用重启；
启动时会做一次资源自检（文件存在、YAML 可解析、模板 id 唯一），结果写日志。
