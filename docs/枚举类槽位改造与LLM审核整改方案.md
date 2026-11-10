# 枚举类槽位改造与 LLM 审核整改方案

> 生成时间：2026-10-10
> 目标：①解决模板挖掘时枚举值（如行政等级、上下行、技术等级）没有被参数化的问题；②根据 LLM 审核不通过的问题进行整改，降低误报率；③确认审核结果的前端渲染

---

## 一、问题背景

### 1.1 枚举类槽位没有被参数化

用户问："列出行政等级为'国道'的所有路线，显示每条路线的具体名称和编号。"

Wren 生成的 SQL：
```sql
SELECT r.full_name AS 具体名称, r.code AS 编号
FROM route AS r
WHERE r.del_flag = 0 AND r.level = '0'  -- '0' 是写死的，没有参数化
ORDER BY r.full_name LIMIT 500
```

问题：`r.level = '0'` 里的 `'0'` 是枚举值（国道），应该被参数化为 `{{admin_level}}`，这样用户问"省道""县道"时也能复用这个模板。

但当前模板挖掘时，这个枚举值没有被识别为参数槽位，导致：
- 模板只能查"国道"，不能查"省道""县道"
- 同类问题（不同枚举值）不能复用，每次都要走 Wren

### 1.2 字段注释里已经有枚举信息

`basic_facilities_routes.level` 列的注释：
> 行政级别（0国道 1省道 2县道 3乡道 4村道）

字段目录（field_catalog）已经从注释里解析出了 `code_map`：
```python
{"0": "国道", "1": "省道", "2": "县道", "3": "乡道", "4": "村道"}
```

`_slot_spec` 函数（templatizer.py 第954-962行）已经能识别有 `code_map` 的列，生成 `enum` 类型的槽位：
```python
code_map = col.get("code_map") or {}
if code_map:
    return {
        "type": "enum",
        "options": [{"label": label, "value": code} for code, label in code_map.items()],
    }
```

**但问题是**：模板挖掘时，WHERE 条件里的枚举字面值（如 `'0'`）没有被正确识别为"这是一个枚举列的值，应该参数化"。

### 1.3 LLM 审核误报率高

当前 40 条模板中，已审核 11 条，被降级 8 条。但分析发现：
- **真问题（2条）**：单花括号参数格式错误（`{route_id}` 应为 `{{route_id}}`）
- **误报（4条）**：把"参数正常用法"当成"参数错位"——如"route_id 参数与西乡段不匹配""参数直接用于 WHERE 过滤"
- **部分合理/待验证（2条）**：参数名不一致、硬编码距离值

误报率约 55%。LLM 对"参数格式错误"这类确定性问题判断准确，但对"参数正常用法""固定条件"这类语义问题误报率很高。

### 1.4 审核结果的前端渲染

前端 AdminPanel.vue 已经有审核结果的渲染（第118-127行）：
- 有 high 问题但未降级：显示"审核存疑"标签（warning，tooltip 列问题）
- 被降级：显示"审核未过（已降级）"标签（danger）
- 有"LLM 审核（3条）"按钮可以触发审核

但可能存在的问题：
- 审核结果的详细信息（issues 列表）展示不够清晰
- 用户无法快速判断哪些是误报、哪些是真问题
- 没有"恢复误报模板"的一键操作

---

## 二、枚举类槽位改造方案

### 2.1 根因分析

模板挖掘时，WHERE 条件里的字面值识别逻辑（`mine_sql` 函数）可能只识别了以下几类参数：
- 实体主键（route_id、section_id 等）
- 时间范围（time_start、time_end）
- 病害类型（damage_type）
- 数值阈值（area > 10）

但对于**枚举列的字面值**（如 `level = '0'`、`up_or_down = '1'`、`built = '1'`），没有被识别为参数槽位。

原因可能是：
1. 挖掘时只看"这张表的主键列"和"常见过滤列"，没有遍历所有有 `code_map` 的列
2. 或者识别了，但因为 `code_map` 里没有这个值（比如注释里写的是"0国道"，但 SQL 里是 `'0'`，匹配不上）而放弃

### 2.2 改造方案

#### 步骤 1：挖掘时识别所有枚举列的字面值

在 `mine_sql` 函数的 WHERE 条件解析阶段，增加逻辑：
- 遍历 WHERE 条件里的所有字面值（`= '0'`、`IN ('0', '1')`）
- 检查这个字面值所在的列是否有 `code_map`
- 如果有，且字面值在 `code_map` 的 key 里（或能通过 label 反查到 key），就识别为枚举参数槽位

伪代码：
```python
for condition in where_conditions:
    column = condition.column  # e.g. "level"
    value = condition.value    # e.g. "0"
    col_info = field_catalog.get_column(table, column)
    code_map = col_info.get("code_map") if col_info else {}
    if code_map and value in code_map:
        # 这是一个枚举值，参数化
        param_name = _name_for("enum", col_info, column)
        slot = _slot_spec(col_info, column, {"type": "enum"})
        add_param(param_name, slot, value)
        replace_literal_with_placeholder(sql, column, value, param_name)
```

#### 步骤 2：枚举值的双向映射

挖掘时，SQL 里的字面值可能是 code（如 `'0'`），也可能是 label（如 `'国道'`）。需要支持双向映射：
- 如果字面值是 code（在 code_map 的 key 里），直接识别
- 如果字面值是 label（在 code_map 的 value 里），反查到 code，再识别

伪代码：
```python
def match_enum_value(value, code_map):
    # 先按 code 匹配
    if value in code_map:
        return value, code_map[value]
    # 再按 label 匹配
    for code, label in code_map.items():
        if label == value or label in value or value in label:
            return code, label
    return None, None
```

#### 步骤 3：枚举槽位的 options 动态生成

枚举槽位的 options 从 `code_map` 生成，**不写死**：
```python
"options": [{"label": "国道", "value": "0"}, {"label": "省道", "value": "1"}, ...]
```

这样用户在卡片上可以下拉选择"国道/省道/县道/乡道/村道"。

#### 步骤 4：复用时的参数替换

复用时，`coerce` 函数需要支持枚举值的中文→code 映射：
- 用户问"省道" → 查 options → 映射成 `'1'` → 替换 `{{admin_level}}`
- 用户问"level=1" → 直接用 `'1'`

### 2.3 需要覆盖的枚举列

根据字段目录，以下列有 `code_map`，需要支持枚举参数化：

| 表 | 列 | 注释 | code_map 示例 |
|---|---|---|---|
| basic_facilities_routes | level | 行政级别 | 0=国道, 1=省道, 2=县道, 3=乡道, 4=村道 |
| basic_facilities_routes | built | 是否内置 | 0=否, 1=是 |
| basic_facilities_routes | route_type | 所在路线类型 | 1=左线, 2=右线, 3=跨线, 4=匝道, 5=线外 |
| ai_problem | up_or_down | 上下行 | 1=上行, 2=下行 |
| ai_problem | categories_type | 所属大类 | 0=路面病害, 5=路基病害, 10=沿路设施, 15=交安事件 |
| ai_problem_base | enable_type | 病害基础状态 | 0=启用, 1=禁用 |
| ai_problem_base | disease_categories_type | 所属大类 | 0=路面病害, 5=路基病害, 10=沿路设施, 15=交安事件 |
| ai_problem_base | main_focus | 是否主要关注类型 | 0=否, 1=是 |
| basic_facilities_bridges | drive_direction | 桥梁行车方向 | 1=上行, 2=下行, 3=双向 |
| basic_facilities_bridges | span_category | 跨径分类 | 1=特大桥, 2=大桥, 3=中桥, 4=小桥 |
| basic_facilities_bridges | use_years_category | 使用年限分类 | 1=永久性, 2=半永久性, 3=临时性 |
| basic_facilities_bridges | is_rail_road | 是否公铁两用 | 0=否, 1=是 |
| basic_facilities_bridges | crossing_type | 跨越地物类型 | 1=河流, 2=跨海, 3=沟壑, ... |
| ai_work_order | order_type | 工单类型 | (从注释解析) |
| ai_work_order | order_status | 工单状态 | (从注释解析) |

### 2.4 验收标准

1. "列出行政等级为国道的所有路线" → 挖掘出的模板 SQL 里 `level = '{{admin_level}}'`，不是 `level = '0'`
2. 模板的 params 里有 `admin_level` 槽位，type=enum，options 包含国道/省道/县道/乡道/村道
3. 用户问"列出行政等级为省道的所有路线" → 弹卡，参数替换为省道，结果正确
4. 用户问"列出内置的路线" → 弹卡，`built = '{{built}}'`，options 包含是/否
5. 所有有 code_map 的列，在 WHERE 条件里出现时都能被参数化
6. 枚举值是 label（如"国道"）时也能正确识别和映射

---

## 三、LLM 审核整改方案

### 3.1 当前审核问题分析

| 问题类型 | 数量 | 是否真问题 | 说明 |
|---|---|---|---|
| 单花括号参数格式错误 | 2 | ✅ 真问题 | `{route_id}` 应为 `{{route_id}}`，会导致参数替换失败 |
| 参数与问法不匹配 | 3 | ❌ 误报 | "route_id 参数与西乡段不匹配"——参数本来就是用来替换的 |
| 参数直接用于 WHERE 过滤 | 1 | ❌ 误报 | "参数直接用于 j0.route_id = '{route_id}'"——这是模板的正常用法 |
| 参数名不一致 | 2 | ⚠️ 部分合理 | `route_id` vs `route`，可能导致回填错误，但应建议修复而非直接降级 |
| 硬编码距离值 | 1 | ⚠️ 待验证 | 1802 可能是 k1+802 的米数换算，需要看具体 SQL |
| del_flag=0 硬编码 | 1 | ❌ 误报 | `del_flag=0` 是所有查询的固定条件，不是硬编码参数 |
| 占位符硬编码 | 1 | ❌ 误报 | `{route_id}` 是参数占位符，不是硬编码值 |

**误报率约 55%**。

### 3.2 整改方案

#### 3.2.1 收窄 LLM 审核的判定范围

LLM 审核只对以下两类问题自动降级（已经做了）：
- `parameter_misaligned`（参数错位）：多个同类参数与列别名/子查询的语义对不上
- `partial_answer`（只答一半）：用户问了多个子问题，SQL 只答了一部分

其余问题（hardcoded_condition、hardcoded_alias、wrong_unit）只记录+提示，不自动降级。

#### 3.2.2 修复 parameter_misaligned 的误报

当前 `parameter_misaligned` 的误报主要是：
- "route_id 参数与西乡段不匹配"——参数本来就是用来替换的，模板问法写"西乡段"，参数填西乡段 id，这是正常的
- "参数直接用于 WHERE 过滤"——这是模板的正常用法

**整改**：在提示词里明确说明：
- 参数占位符（`{{route_id}}`、`{{damage_type}}`）用于 WHERE 过滤是**正常用法**，不算 parameter_misaligned
- 参数名与模板问法里的实体名称不一致是**正常的**（问法写"西乡段"，参数叫 `route_id`），不算 parameter_misaligned
- 只有当**多个同类参数的语义角色对不上**时（如 avg_area CTE 里的路线和列别名写的路线不一致），才算 parameter_misaligned

#### 3.2.3 增加确定性检查（不依赖 LLM）

对于"单花括号参数格式错误"这类确定性问题，不需要 LLM 判断，直接用代码检查：
- 检查 SQL 里是否有单花括号参数（`{param}` 而不是 `{{param}}`）
- 如果有，直接标记为"参数格式错误"，自动降级

这样可以避免 LLM 对这类问题的误判，也更快。

#### 3.2.4 审核结果的分类展示

前端审核结果展示时，按问题类型分类：
- **确定性问题（代码检查）**：单花括号格式、SQL 语法错误、引用不存在的表/列——这些是 100% 真问题
- **LLM 审核问题**：parameter_misaligned、partial_answer——这些可能有误报，需要人工确认
- **提示性问题**：hardcoded_condition、hardcalse_alias、wrong_unit——这些不降级，只提示

让用户能快速区分哪些是必须修的、哪些是可能误报的。

#### 3.2.5 一键恢复误报模板

前端增加"恢复为 mature"按钮：
- 对于被降级但用户认为是误报的模板，一键恢复为 mature
- 恢复时记录"人工恢复"原因，避免 LLM 再次降级时覆盖
- 被人工恢复的模板，LLM 复审时不再自动降级（只记录问题）

### 3.3 验收标准

1. 单花括号参数格式错误 → 代码检查直接发现，自动降级（不依赖 LLM）
2. "route_id 参数与西乡段不匹配" → 不再被误报为 parameter_misaligned
3. "参数直接用于 WHERE 过滤" → 不再被误报为 parameter_misaligned
4. "del_flag=0 硬编码" → 只提示，不降级
5. 被误降的模板可以一键恢复为 mature
6. 审核结果按"确定性问题/LLM 审核问题/提示性问题"分类展示

---

## 四、审核结果前端渲染确认与完善

### 4.1 当前渲染情况

前端 AdminPanel.vue 已经有：
- 第118-127行：审核结果标签（"审核存疑"/"审核未过（已降级）"）
- 第297-303行：`reviewText()` 函数，把 high 级问题拼出来给 tooltip
- 第343-352行："LLM 审核（3条）"按钮

### 4.2 需要完善的地方

#### 4.2.1 审核结果详情展示

当前只有 tooltip 展示 high 问题，不够清晰。建议：
- 点击模板行展开，显示完整的审核结果（issues 列表、suggested_fixes）
- 每个 issue 显示：类型、严重程度、描述、位置
- 区分"确定性问题"和"LLM 审核问题"

#### 4.2.2 一键恢复误报模板

增加"恢复为 mature"按钮：
- 只有被降级（status=candidate + demote_reason 以"LLM 审核发现"开头）的模板显示
- 点击后恢复为 mature，记录"人工恢复"原因
- 被人工恢复的模板，LLM 复审时不再自动降级

#### 4.2.3 审核进度展示

当前"LLM 审核（3条）"按钮只显示本次审了多少条，建议：
- 显示总模板数、已审核数、待审核数、被降级数
- 显示审核进度条
- 支持"审核全部待审"按钮（限量，避免花费过高）

#### 4.2.4 模板参数 options 的展示

对于枚举类槽位，在模板详情里展示 options 列表：
- 让用户能看到这个模板支持哪些枚举值
- 如果 options 不全（比如只有国道，没有省道），能快速发现问题

---

## 五、实施顺序建议

### 第一步：枚举类槽位改造（优先级最高）

这是用户当前遇到的直接问题，改动相对独立：
1. 修改 `mine_sql` 函数，识别 WHERE 条件里所有有 `code_map` 的列的字面值
2. 支持枚举值的双向映射（code↔label）
3. 枚举槽位的 options 从 code_map 动态生成
4. 复用时的 coerce 函数支持枚举值的中文→code 映射

**预计工作量**：中等（需要改挖掘逻辑和绑定逻辑，但有现成的 code_map 数据）

### 第二步：LLM 审核整改（优先级高）

降低误报率，提高审核质量：
1. 增加确定性检查（单花括号格式、SQL 语法、引用不存在的表/列）
2. 收窄 parameter_misaligned 的判定范围，修复误报
3. 审核结果按问题类型分类展示

**预计工作量**：中等（主要改提示词和前端展示）

### 第三步：前端渲染完善（优先级中）

完善审核结果的展示和操作：
1. 审核结果详情展开
2. 一键恢复误报模板
3. 审核进度展示
4. 模板参数 options 展示

**预计工作量**：小到中等（主要是前端改动）

---

## 六、不做的边界

1. **不改变 preset_queries.json（手写模板）的枚举逻辑**——手写模板的参数选项已经是动态从数据库查的，不需要改
2. **不做枚举值的自动补全**——用户输入"国"时自动提示"国道"，这个可以以后做
3. **不做 LLM 审核的自动修复**——LLM 只审核不修改，修复靠人工或确定性代码
4. **不改变模板挖掘的整体流程**——只增加枚举列的识别，不改变其他参数的识别逻辑
5. **不删除历史备份文件**——auto_templates.backup.json 等 4 个备份文件，用户确认后再删
