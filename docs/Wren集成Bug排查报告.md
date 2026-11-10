# Wren 集成 Bug 排查报告

> 日期：2026-09-16
> 定位：Wren AI 集成后的代码审查与实测验证，列出所有已确认的 bug、复现方式和修复方案。
> 关联文档：[WrenAI集成方案.md](./WrenAI集成方案.md)、[RAGFlow知识库精简方案.md](./RAGFlow知识库精简方案.md)

---

## 一、排查背景与环境确认

### 1.1 排查范围

Wren AI 集成涉及的文件：
- `backend/app/services/wren_engine.py` — Wren 引擎封装（MDL 上下文 + MDL-SQL 编译）
- `backend/app/services/mdl_sync.py` — MDL 自动生成与构建
- `backend/app/services/sql_engine.py` — `generate_mdl_sql`（LLM 生成 MDL-SQL）
- `backend/app/services/nl2sql.py` — `mdl_condition_lines`（MDL 必须条件）、`validate_semantics`
- `backend/app/services/agent.py` — Wren 路径调用逻辑
- `backend/app/config.py` — Wren 相关配置
- `wren_projects/road_mysql/` — MySQL MDL 项目（models/overrides/relationships/cubes/target）
- `wren_projects/road_pg/` — PostgreSQL MDL 项目

### 1.2 环境确认

| 项 | 值 | 状态 |
|---|---|---|
| 虚拟环境 Python 版本 | 3.11.15 | ✅ 满足 Wren ≥3.11 要求 |
| wrenai 包版本 | 0.14.0 | ✅ 已安装 |
| wren CLI | 可用（backend/.venv/Scripts/wren.exe） | ✅ |
| `import wren` | 成功，`WrenEngine` 可用 | ✅ |
| `WrenEngine.dry_plan()` | 能正常编译 | ✅ |
| `wren_projects/road_mysql/target/mdl.json` | 存在 | ✅ |
| `wren_projects/road_pg/target/mdl.json` | 存在 | ✅ |

> 注：系统 Python 是 3.10.9，但项目使用 `backend/.venv` 虚拟环境（Python 3.11.15），不存在版本不兼容问题。

### 1.3 实测验证方法

用虚拟环境 Python 直接实例化 `WrenEngine`，调用 `dry_plan()` 测试不同 MDL-SQL 写法的编译结果，验证编译产物是否可执行。

---

## 二、P0 致命 Bug（Wren 路径实际完全走不通）

### Bug 1：prompt 错误引导 LLM "编译器自动 JOIN"，但 Wren 不支持隐式关系引用

**严重程度**：P0 — 导致 Wren 路径永远走不通，等于白做

**位置**：
- `wren_engine.py` L114-120（`mdl_context()` 输出的关系说明）
- `sql_engine.py` L162-168（`generate_mdl_sql` 的业务规则 prompt）

**问题描述**：

`mdl_context()` 输出给 LLM 的文本里写了：
```
关系（编译器自动 JOIN，不需要你写）：
disease_route disease.routes_id = route.id；disease_section disease.sections_id = section.id；...
```

`generate_mdl_sql` 的 prompt 里写了：
```
- 路线用 route.short_name / route.full_name；桩号区间用 disease.pile_distance BETWEEN a AND b
```

LLM 按照引导写了隐式关系引用：
```sql
SELECT COUNT(*) FROM disease 
WHERE route.short_name = '宝安大道西乡段' 
  AND disease.del_flag = 0
```

**但 Wren 实际不支持隐式关系引用！** 实测 4 种写法的结果：

| 写法 | 编译结果 | 可执行 |
|---|---|---|
| `FROM disease JOIN route ON disease.routes_id = route.id WHERE route.short_name = ...` | ✅ 正确生成 disease + route 两个 CTE + JOIN | ✅ |
| `FROM disease WHERE disease.del_flag = 0 AND disease.area > 10`（只查自己字段） | ✅ 正确 | ✅ |
| `FROM disease WHERE route.short_name = ...`（隐式引用，不写 JOIN） | ⚠️ 编译"成功"，但 CTE 只有 disease，WHERE 引用 `route.short_name` 但没 JOIN route | ❌ 执行报 `Unknown column 'route.short_name'` |
| `FROM disease WHERE disease.route.short_name = ...`（点号穿透） | ❌ 直接报错 `Schema error: No field named disease.route` | ❌ |

**Bug 1 的编译产物（写法3）**：
```sql
WITH disease AS (
  SELECT `wren_src_disease`.del_flag AS del_flag 
  FROM (...) AS `wren_src_disease`
) 
SELECT COUNT(*) AS cnt FROM disease 
WHERE route.short_name = '宝安大道西乡段'   -- ← route 表根本没 JOIN！
  AND disease.del_flag = 0
```

**后果链路**：
1. LLM 按照 prompt 引导写隐式关系引用（不写 JOIN）
2. Wren `dry_plan()` 编译"成功"（不报错），但产物缺少 JOIN
3. `compile_mdl_sql()` 返回这个有逻辑错误的 SQL
4. `validate_semantics()` 可能通过（因为 SQL 文本里有 `route.short_name` 字符串）
5. `execute_sql()` 执行报 `Unknown column 'route.short_name'`
6. Wren 路径失败，`last_error` 被设置，静默降级到内置 LLM
7. **Wren 路径实际上永远走不通**

**修复方案**：

1. **`wren_engine.py` L114-120**：把"关系（编译器自动 JOIN，不需要你写）"改为明确说明必须显式写 JOIN：
```python
if mdl["relationships"]:
    lines.append(
        "关系（必须显式写 JOIN，不能直接在 WHERE 里引用其他模型字段）："
        + "；".join(
            f"{r.get('models')} {r.get('condition')}" for r in mdl["relationships"]
        )
    )
    lines.append("JOIN 写法示例：FROM disease JOIN route ON disease.routes_id = route.id")
```

2. **`sql_engine.py` L162-168**：业务规则里增加显式 JOIN 的强制要求和示例：
```
- 必须显式写 JOIN，不能直接在 WHERE 里引用其他模型的字段
- 路线过滤写法：FROM disease JOIN route ON disease.routes_id = route.id WHERE route.short_name = 'xxx'
- 病害类型过滤写法：FROM disease JOIN damage_dict ON disease.damage_type = damage_dict.problem_code WHERE damage_dict.problem_type = '泛油'
- 只查 disease 自己的字段（area/pile_distance/collection_time 等）时不需要 JOIN
```

3. **`nl2sql.py` `mdl_condition_lines()`**：条件写法也要包含 JOIN，不能只说字段名。

---

### Bug 2：`generate_mdl_sql` 没有调用 `extract_sql`

**严重程度**：P0 — LLM 返回 markdown 时编译必然失败

**位置**：`sql_engine.py` L178

**问题描述**：

```python
def generate_mdl_sql(...):
    ...
    return chat([...], temperature=0, model=sql_model())  # ← 直接返回原始响应
```

对比 `generate_with_context`（nl2sql.py L2613）的正确写法：
```python
def generate_with_context(...):
    response = generate_sql(...)
    return extract_sql(response)  # ← 提取纯 SQL
```

`generate_mdl_sql` 直接返回 LLM 的原始响应。如果 LLM 返回了 markdown 代码块（```sql ... ```）、前置解释文字、或后置说明，`compile_mdl_sql` 收到的不是纯 SQL，Wren 编译必然失败。

虽然 prompt 里写了"只返回 SQL，不要解释、不要 markdown"，但 LLM 不总是遵守，尤其是在复杂查询或有 feedback 修正时。

**修复方案**：
```python
return extract_sql(chat([...], temperature=0, model=sql_model()))
```

---

### Bug 3：`wren_enabled` 和 `wren_use_mdl_context` 默认都是 False

**严重程度**：P0 — 功能没开，等于没集成

**位置**：`config.py` L158-160

**问题描述**：

```python
wren_enabled: bool = False          # 打开后才尝试用 Wren 编译 MDL-SQL
wren_projects_dir: Path = Path("wren_projects")
wren_use_mdl_context: bool = False  # 把 MDL 的结构化模型/字段注进提示词
```

项目没有 `.env` 文件覆盖默认值，所以实际运行时：
- `wren_engine.available()` 第一行 `if not settings.wren_enabled: return False`，Wren 编译功能直接关闭
- `nl2sql.py` L2227 `if settings.wren_use_mdl_context` 为 False，`mdl_context()` 的返回值不会注入 prompt，LLM 看不到 MDL 模型清单

两个开关都关着，Wren 集成的代码虽然写了，但实际完全不生效。

**修复方案**：
- `wren_use_mdl_context` 建议默认改为 `True`——这层不依赖 wren 包，纯读 YAML，Python 3.10 也能用，现在就能生效，且没有风险
- `wren_enabled` 在 Bug 1（prompt 引导）修复后改为 `True`——Bug 1 不修就打开会导致 Wren 路径持续失败降级，反而增加延迟

---

## 三、P1 代码逻辑 Bug

### Bug 4：`mdl_condition_lines` 路线条件用 `short_name`，可能查不到

**严重程度**：P1 — 路线条件可能查空

**位置**：`nl2sql.py` L2576-2579

**问题描述**：

```python
if "route" in required and slots.get("route_match"):
    route = slots["route_match"]
    name = str(route.get("short_name") or route.get("full_name") or route.get("code") or "")
    lines.append(f"路线：route.short_name = '{name}'（原样照抄，不要加「段」或改写）")
```

问题有两个：

1. **short_name/full_name 混淆**：如果路线匹配用的是 `full_name`（如"宝安大道西乡段"是 full_name，short_name 可能是"西乡段"），这里会把 full_name 的值塞进 `route.short_name = '...'`，导致查不到数据。

2. **没说要写 JOIN**：结合 Bug 1，这里只说了 `route.short_name = 'xxx'`，没说要写 `JOIN route ON disease.routes_id = route.id`，LLM 会写成隐式引用，Wren 编译出的 SQL 缺少 JOIN。

**修复方案**：

优先用 `route.id`（确定的，不会有 short_name/full_name 混淆），并明确写出 JOIN 写法：
```python
route_id = str(route.get("id") or "")
lines.append(
    f"路线：FROM disease JOIN route ON disease.routes_id = route.id "
    f"WHERE route.id = '{route_id}'（用 id 过滤，不要用名称，避免 short_name/full_name 混淆）"
)
```

---

### Bug 5：Wren 编译产物可能通不过 `validate_semantics`

**严重程度**：P1 — 正确 SQL 可能被误拦截

**位置**：`agent.py` L634

**问题描述**：

```python
if physical:
    missing = nl2sql.validate_semantics(plan, physical)
```

`validate_semantics` 是为手写物理 SQL 设计的，用正则匹配物理表名/列名（如 `basic_facilities_routes`、`r.short_name`、`pile_distance`）。

Wren 编译出的 SQL 格式不同：
- 用 `WITH disease AS (...)` CTE 包裹
- 表别名是 Wren 自动生成的（`wren_src_disease`、`__source`）
- 物理表名是全限定名（`new_four_road_base_ai.ai_problem`）

`validate_semantics` 的正则可能匹配不到 Wren 编译产物中的条件，误判为"缺少必须条件"，导致 Wren 编译出的正确 SQL 被拦截。

**需要验证**：用 Wren 编译一条含路线条件的 MDL-SQL（显式 JOIN 写法，Bug 1 修复后），看产物能否通过 `validate_semantics`。

**修复方向**（二选一）：
1. `validate_semantics` 增加对 Wren CTE 格式的适配（识别 `WITH ... AS (...)` 格式，在 CTE 定义和主查询中都搜索条件）
2. Wren 路径跳过 `validate_semantics`——因为 MDL-SQL 阶段已经通过 `mdl_condition_lines` 强制了条件，且 Wren 编译是确定性的，不会凭空增加或丢失条件

---

### Bug 6：Wren 路径失败后 `last_error` 反馈给内置 LLM 可能不合理

**严重程度**：P1 — 降级重试效果差

**位置**：`agent.py` L620-658

**问题描述**：

Wren 路径有多个失败点，每个失败点设置的 `last_error` 不同：
- L636：`validate_semantics` 失败 → `last_error = "Wren 编译结果缺少必须条件：..."`
- L643：`execute_sql` 失败 → `last_error = str(exc)`（如 `Unknown column 'route.short_name'`）
- 编译失败（`compile_mdl_sql` 返回 None）→ 不设置 `last_error`

然后进入内置 LLM 重试循环（L647），用 `last_error` 作为 feedback：
```python
candidate = nl2sql.generate_with_context(plan, context, feedback=last_error)
```

问题：
1. Wren 执行失败的错误（如 `Unknown column 'route.short_name'`）是 MDL-SQL 写法问题，对内置 LLM 修正物理 SQL 没有帮助，反而可能干扰
2. Wren 编译失败时不设置 `last_error`，内置 LLM 重试时没有 feedback，等于白重试一次
3. `validate_semantics` 失败的错误信息（"Wren 编译结果缺少必须条件"）对内置 LLM 来说语义不清晰

**修复方向**：
- Wren 路径失败后，清空或重置 `last_error`，不要把 Wren 特有的错误传给内置 LLM
- 或者区分错误类型：如果是 SQL 执行错误（如 Unknown column），可以保留；如果是 Wren 特有的错误，清空

---

## 四、P2 优化项

### Bug 7：disease 模型 50+ 字段，MDL 上下文过长

**位置**：`wren_engine.py` `mdl_context()` L90-103

**问题描述**：

`disease` 模型有 50+ 字段，其中很多是业务上不用的（`tenant_id`、`create_by`、`update_time`、`luge_name`、`inspection_obj_id`、`file_name` 等），且很多字段描述是"（库侧没写 COMMENT，待补）"。

6 个 core 模型（disease、damage_dict、route、section、pile_point、patrol）的字段加起来可能超过 `generate_mdl_sql` 中 `models_text[:12000]` 的截断限制，导致后面的模型（如 damage_dict、patrol）被截断，LLM 看不到关键字段。

**修复方案**：
在 `mdl_context` 中过滤掉"（库侧没写 COMMENT，待补）"的字段，只显示有业务描述的字段：
```python
for column in model.get("columns") or []:
    label = (column.get("properties") or {}).get("description") or ""
    if "待补" in label or not label.strip():
        continue  # 跳过无业务描述的字段
    ...
```

---

### Bug 8：`mdl_sync.py` 中 wren CLI 路径写死

**位置**：`mdl_sync.py` L132-133

**问题描述**：

```python
def _wren_cli() -> Path:
    return Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "wren.exe"
```

写死了 Windows 路径和 `.venv` 目录名。如果虚拟环境不在 `backend/.venv`，或者用的是其他环境管理工具（conda/poetry），或者部署到 Linux 服务器，`wren context build` 会失败。

**修复方案**：
优先用 `shutil.which("wren")` 从 PATH 查找，找不到再回退到默认路径：
```python
import shutil

def _wren_cli() -> Path | None:
    found = shutil.which("wren")
    if found:
        return Path(found)
    default = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "wren.exe"
    return default if default.exists() else None
```

---

### Bug 9：`_load_mdl` 用 `lru_cache` 缓存，MDL 更新后不生效

**位置**：`wren_engine.py` L33

**问题描述**：

```python
@lru_cache(maxsize=8)
def _load_mdl(project: str) -> dict[str, Any]:
```

`_load_mdl` 用 `lru_cache` 缓存了 MDL 解析结果。如果通过 `/api/schema/mdl/sync` 接口重新生成了 MDL（改了 overrides.yml 或 relationships.yml），缓存不会自动失效，`mdl_context()` 返回的还是旧的 MDL。

**修复方案**：
- 增加一个 `clear_mdl_cache()` 函数，在 `mdl_sync.sync_project()` 成功后调用
- 或者去掉 `lru_cache`，改用基于文件修改时间的缓存（检查 mtime，变了就重新加载）

---

## 五、汇总表

| 优先级 | Bug | 位置 | 影响 |
|---|---|---|---|
| **P0** | prompt 错误引导"自动 JOIN"，Wren 实际不支持隐式关系引用 | wren_engine.py L114 + sql_engine.py L162 | Wren 路径永远走不通 |
| **P0** | generate_mdl_sql 没调 extract_sql | sql_engine.py L178 | LLM 返回 markdown 时编译失败 |
| **P0** | wren_enabled / wren_use_mdl_context 默认 False | config.py L158-160 | 功能没开，等于没集成 |
| **P1** | mdl_condition_lines 用 short_name 可能查不到 | nl2sql.py L2578 | 路线条件查空 |
| **P1** | Wren 编译产物可能通不过 validate_semantics | agent.py L634 | 正确 SQL 被误拦截 |
| **P1** | Wren 失败后 last_error 反馈给内置 LLM 不合理 | agent.py L620-658 | 降级重试效果差 |
| **P2** | disease 50+ 字段导致 MDL 上下文过长 | wren_engine.py mdl_context | 截断丢关键模型 |
| **P2** | wren CLI 路径写死 | mdl_sync.py L132 | MDL build 可能失败 |
| **P2** | _load_mdl lru_cache 不失效 | wren_engine.py L33 | MDL 更新后不生效 |

---

## 六、建议修复顺序

### 第一批（立即修，不依赖外部验证，约 30 分钟）

1. **Bug 2**：`generate_mdl_sql` 加 `extract_sql`，一行代码
2. **Bug 3a**：`wren_use_mdl_context` 默认改为 `True`（不依赖 wren 包，立即生效）
3. **Bug 7**：`mdl_context` 过滤"待补"字段，减少上下文长度

### 第二批（核心修复，约 1 小时）

4. **Bug 1**：修改 `mdl_context` 和 `generate_mdl_sql` 的 prompt，明确告诉 LLM 必须显式写 JOIN，给出写法示例。这是 Wren 路径能否走通的前提。
5. **Bug 4**：`mdl_condition_lines` 改用 route.id + 显式 JOIN 写法
6. **Bug 3b**：Bug 1 修复后，`wren_enabled` 默认改为 `True`

### 第三批（验证后修，约 1 小时）

7. **Bug 5**：实际跑一条 Wren 编译测试（显式 JOIN 写法），验证 `validate_semantics` 是否需要适配，按需修复
8. **Bug 6**：Wren 路径失败后重置 `last_error`，避免干扰内置 LLM 重试

### 第四批（优化，有空再做）

9. **Bug 8**：wren CLI 路径用 `shutil.which` 查找
10. **Bug 9**：`_load_mdl` 缓存失效机制

---

## 七、验证标准

修复完成后，用以下场景验证 Wren 路径是否真正走通：

| 测试场景 | 期望结果 |
|---|---|
| "宝安大道西乡段一共多少处病害？" | engine=wren，SQL 含 JOIN route，结果=911 |
| "宝安大道西乡段泛油有多少处？" | engine=wren，SQL 含 JOIN route + JOIN damage_dict，结果正确 |
| "面积超过10平方米的病害有多少处？" | engine=wren，SQL 含 area > 10，结果正确 |
| "置信度大于0.7的病害有多少处？" | engine=wren，SQL 引用 confidence 计算字段，编译后含 JSON_EXTRACT |
| "K1+000到K3+000有多少处病害？" | engine=wren，SQL 含 pile_distance BETWEEN 1000 AND 3000 |
| 模板能命中的简单查询 | engine=template（模板优先，不经过 Wren） |
| Wren 编译失败的复杂查询 | 静默降级到内置 LLM，engine=llm，不报错 |

**关键验证点**：
1. `result["engine"] == "wren"`（说明 Wren 路径真的走通了，不是静默降级）
2. 编译出的 SQL 可执行（不报错）
3. 结果数字正确（与探针数据一致）
4. agent.log 中有 `compile_success` 事件（wren_engine.py L172）
