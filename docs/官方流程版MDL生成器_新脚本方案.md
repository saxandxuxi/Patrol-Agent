# 官方流程版 MDL 生成器（新脚本，与原 mdl_sync.py 并存）

> 给 codex 实施。原 `mdl_sync.py` 不动，新增 `mdl_sync_official.py`，完全按官方 `wren-generate-mdl` skill 的七阶段流程编排，集成项目特有的 override 合并、缓存清理、删表清理。

---

## 一、为什么要新写一个（而不是改原文件）
- 原 `mdl_sync.py` 已经在线上跑，改它风险大；
- 新版用官方推荐的工具链（SQLAlchemy inspect + `wren utils parse-type` + `wren context validate`），和原文件的手写类型字典/information_schema 路径差异大；
- 两个生成器并存，可以对比输出、A/B 验证，确认新版稳定后再切默认。

---

## 二、官方七阶段 vs 新脚本实现

| 官方阶段 | 官方做法 | 新脚本实现 |
|---|---|---|
| 1 Connect | 测连接 | 复用项目现有 `db_manager` / `settings`，不重复造 |
| 2 Discover | SQLAlchemy `inspect()` 拿表/列/类型/主键/**外键** | **用 SQLAlchemy inspect**（关键差异：原文件用 information_schema，拿不到外键） |
| 3 Normalize | `wren utils parse-type` 或 Python `parse_type()` | **调官方 `wren utils parse-type`**（子进程，带 dialect=mysql/postgres），失败退原文件的 `_MDL_TYPES` |
| 4 Scaffold | `wren context init` + 写 model YAML + relationships | 调 `wren context init` 初始化，然后写 `models/<表>/metadata.yml`；**外键自动生成 `relationships.yml` 草稿** |
| 5 Validate | `wren context validate` → `build` | **先 validate 再 build**，validate 失败直接返回错误 |
| 6 Index | `wren memory index` | 暂不执行（项目用 RAGFlow + field_grounding，不用 Wren memory），留接口 |
| 7 Iterate | 加描述/计算字段/视图 | **合并 overrides.yml**（项目特有） |

---

## 三、新脚本结构

```
backend/app/services/mdl_sync_official.py
├── PROJECTS = {"road_mysql": "mysql", "road_pg": "postgresql"}  # 同原文件
├── _discover(kind) -> list[table]          # SQLAlchemy inspect，含外键
├── _parse_type(physical_type, dialect) -> str  # 调 wren utils parse-type
├── _scaffold_project(project_dir, kind)     # wren context init + 写 wren_project.yml
├── _write_model(table, override, kind) -> metadata.yml
├── _write_relationships(tables) -> relationships.yml草稿  # 从外键自动生成
├── _merge_overrides(model, override)        # 同原文件逻辑：name/description/calculated/extra_columns/hide/ref_sql/primary_key
├── _clean_removed_models(models_dir, written)  # 删不再对应的表目录
├── _validate(project_dir) -> (ok, output)   # wren context validate
├── _build(project_dir) -> (ok, output)      # wren context build（带 PYTHONUTF8=1）
├── _clear_caches()                           # wren_engine.clear_caches + clarify._catalog.cache_clear
├── sync_project(kind, build=True) -> dict
└── sync_all(build=True) -> list[dict]
```

---

## 四、关键差异点（和原文件对比）

### 1. 表结构发现：SQLAlchemy inspect（拿外键）
```python
from sqlalchemy import create_engine, inspect

engine = create_engine(db_url)
insp = inspect(engine)
for table_name in insp.get_table_names():
    columns = insp.get_columns(table_name)
    pk = insp.get_pk_constraint(table_name)
    fks = insp.get_foreign_keys(table_name)   # ← 原文件拿不到这个
```
- **收益**：外键自动检测，`relationships.yml` 不用纯手写；
- **注意**：MySQL 有些表没建外键约束（靠业务逻辑关联），这种仍需 override 补；自动生成的 relationships 标 `source: auto`，人工加的标 `source: manual`，merge 时 manual 覆盖 auto。

### 2. 类型归一：官方 `wren utils parse-type`
```bash
wren utils parse-type --type "varchar(255)" --dialect mysql
# → VARCHAR(255)
```
- 优先调官方，失败（CLI 不存在/返回非 0）退原文件的 `_MDL_TYPES`；
- 加单测：对项目所有表的所有列，对比官方 parse_type 和手写字典输出，记录差异。

### 3. 先 validate 再 build
- 原文件直接 build；
- 新文件：`_validate()` 失败 → 返回 `{"valid": False, "validate_output": ...}`，不进 build；
- 接口返回加 `valid` 字段。

### 4. relationships.yml 自动生成 + merge
- 从 SQLAlchemy 拿到的外键自动生成 relationships 草稿；
- 和人工维护的 `relationships.yml` merge：同 name 的人工覆盖自动，自动的保留（标 source）；
- 这是原文件完全没有的能力。

---

## 五、项目特有逻辑（必须保留）

从原文件照搬，不丢：
1. **override 合并**：`overrides.yml` 的 name/description/calculated/extra_columns/hide/ref_sql/primary_key；
2. **删表清理**：`models/` 下不再对应任何表的目录直接清掉；
3. **缓存清理**：build 后清 `wren_engine`、`clarify._catalog` 进程内缓存；
4. **PYTHONUTF8=1**：子进程调 wren 时带这个环境变量，防中文 GBK 炸；
5. **两个项目**：road_mysql / road_pg；
6. **wren CLI 查找逻辑**：`_wren_cli()` 同原文件（优先当前解释器环境，其次 PATH，最后 .venv）。

---

## 六、接口与切换

- 新脚本暴露和原文件**完全相同的接口**：`sync_project(kind, build=True)`、`sync_all(build=True)`、`build_project(kind)`；
- 调用方（`routers/` 里的"重建语义层"按钮）暂时仍调原文件；
- 加一个环境变量 `MDL_GENERATOR=official|legacy`（默认 legacy），控制用哪个生成器；
- 前端/管理页加一个"生成器"切换选项，方便 A/B 对比。

---

## 七、验收

1. 新脚本 `sync_all()` 对两个项目都能跑通，validate + build 全通过；
2. 生成的 `models/` 和原文件生成的对比：表数一致、列数一致、类型差异有记录（官方 parse_type vs 手写字典）；
3. 外键自动生成的 `relationships.yml` 草稿合理（至少 ai_problem.routes_id → basic_facilities_routes.id 这类能自动识别）；
4. override 合并后，计算字段、extra_columns、hide 列不丢；
5. 删表清理：模拟删一张表，sync 后对应目录被清掉；
6. 缓存清理：build 后不重启也能生效；
7. `MDL_GENERATOR=official` 切换后，前端"重建语义层"按钮调新脚本，结果正确。

---

## 八、不做的（明确边界）
- 不接入 `wren memory index`（项目用 RAGFlow + field_grounding）；
- 不引入 OSI 语义模型；
- 不改原 `mdl_sync.py`；
- 不自动加业务描述/计算字段（那是 LLM 辅助的事，后续可单独做，本脚本只做确定性 scaffold）；
- 不替换 relationships.yml 的人工维护——自动生成的是草稿，人工的优先。
