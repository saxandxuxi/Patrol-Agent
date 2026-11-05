# resources/ —— 会变的业务知识都放这里

依据 `docs/代码结构重构与模板外置方案.md`：

- `prompts/`（项目根）：给 LLM 的提示词，已经在外置，原位不动。
- `resources/`（本目录）：**确定性知识 / 配置**，随 git 管理。
- `backend/data/`：运行期产物（agent.db、schema_catalog.json 缓存），不进 git 语义。

## 目录

| 目录 | 放什么 | 事实源 |
| --- | --- | --- |
| `sql_templates/` | 内置确定性 SQL 模板的注册表与（逐步迁入的）SQL 正文；`_index.yaml` 是总表 | 本目录 |
| `lexicon/` | 触发词 / 语义键→SQL 映射 / 结果话术等词表 | 本目录 |
| `field_meta/` | 字段元数据的**静态覆盖层**（`code_map_overrides.json`） | 数据库 `COLUMN_COMMENT` + 本目录覆盖 |
| `slots/` | SQL 谓词 → 语义槽位的归类规则（`predicate_slots.yaml`）：哪些列是固定常量、哪些是自由文本、数值比较符白名单、实体槽的取值域来源 | 本目录（字段本身仍以 `schema_catalog` 读 `COLUMN_COMMENT` 为准） |

## 怎么改

- **加一个问法同义词**：改 `lexicon/trigger_words.yaml`（不用动 `.py`，下次请求自动生效）。
- **加/改一个字段的中文名或编码映射**：优先改库里的 `COLUMN_COMMENT`；注释解析不全时改 `field_meta/code_map_overrides.json`。
- **加一个内置模板**：在 `sql_templates/_index.yaml` 登记，SQL 正文写同名 `.yaml`（当前阶段：登记 + 只读展示，判定逻辑仍在代码里，按方案 §6 分阶段迁移）。

## 热更新

`resource_loader` 按文件 `(mtime, size)` 判版本，改完文件下一次读取即生效，不用重启；
启动时会做一次资源自检（文件存在、YAML 可解析、模板 id 唯一），结果写日志。
