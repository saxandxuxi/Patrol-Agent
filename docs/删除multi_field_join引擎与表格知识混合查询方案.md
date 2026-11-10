# 删除 multi_field_join 引擎与表格知识混合查询方案

> 背景：multi_field_join 引擎会错误选择 JOIN 表（如把里程桩点表选成边坡表），且跳过用户确认直接执行，导致查询结果错误。同时，"拱起是怎么计算的"这类表格与知识混合问题（查询 ai_problem_base.formula 字段）没有得到很好的支持。
> 核心思路：删除 multi_field_join 引擎，所有查询要么走模板（用户确认后执行），要么走 Wren 语义层；新增表格与知识混合问题的处理逻辑。
> 生成时间：2026-10-12

---

## 一、问题诊断

### 1.1 multi_field_join 引擎的问题

#### Bug 1：错误选择 JOIN 表

**案例："路段名称以及对应的桩号以及对应的路段长度调出来看看"**

生成的 SQL：
```sql
SELECT s.full_name AS '路段全称', s.start_num AS '起点桩号', s.end_num AS '终点桩号', 
       s.section_num AS '路段序号', ..., j1.length AS '长度' 
FROM basic_facilities_sections s 
LEFT JOIN basic_facilities_slope j1 ON j1.sections_id = s.id AND j1.del_flag = 0 
WHERE s.del_flag = 0 LIMIT 500
```

**问题：**
- 用户问的是"路段长度"，应该 JOIN `basic_facilities_sections_pile_detail`（里程桩点表）
- 实际 JOIN 了 `basic_facilities_slope`（边坡表）
- 边坡表的 `length` 字段是边坡长度，不是路段长度
- 虽然 SQL 能执行，但结果完全错误

**根因：**
- `multi_field.py` 的字段解析逻辑靠字段名匹配，"长度"这个词在多张表里都有（边坡长度、桥梁长度、路段长度等）
- 它没有足够的语义理解能力来判断用户到底要哪张表的"长度"
- 选错表后直接执行，没有用户确认环节

#### Bug 2：跳过用户确认直接执行

**问题：**
- multi_field_join 引擎在 `agent.py:4332` 被调用后，直接执行 SQL 并返回结果
- 没有弹卡让用户确认查询口径
- 用户不知道系统选错了表，只能看到错误的结果

**调用链路：**
```
用户输入 → 语义规划 → 判断 subject=multi 或 fields>=2 → _multi_field_payload() → 直接执行 SQL → 返回结果
```

#### Bug 3：与 Wren 语义层功能重叠

**问题：**
- multi_field_join 试图解决"跨表多字段查询"的问题
- 但 Wren 语义层本身就支持跨表 JOIN 查询
- 两套逻辑并存，导致维护成本高，且容易出现不一致
- Wren 基于 MDL（语义模型），能更准确地理解表之间的关系和字段含义

### 1.2 表格与知识混合问题

**案例："拱起是怎么计算的？"**

用户问的是病害类型"拱起"的计算公式，需要查询 `ai_problem_base` 表的 `formula` 字段。

**formula 字段的格式：**
```json
[{"label":"长","value":1},{"label":"*","value":6},{"label":"宽","value":2}]
```

这是一个 JSON 数组，描述了计算公式的组成（如"长 * 宽 * 系数"）。

**当前问题：**
- 系统可能把这个问题当成普通的业务查询，去 `ai_problem` 表里找数据
- 没有识别出这是一个"知识查询"，应该去 `ai_problem_base`（病害字典表）里查
- formula 字段是 JSON 格式，需要特殊解析和展示
- 用户需要的是"解释这个公式是什么意思"，而不是简单地返回 JSON

---

## 二、删除 multi_field_join 引擎方案

### 2.1 核心思路

**删除 multi_field_join 引擎，所有查询只走两条路径：**

1. **模板路径**：匹配到模板 → 弹卡用户确认 → 执行
2. **Wren 路径**：没有匹配到模板 → Wren 语义层生成 SQL → 执行

**不再有第三条"自动执行"的路径。**

### 2.2 具体改动

#### 改动 1：移除 multi_field_join 的调用

**文件：** `backend/app/services/agent.py`

**位置：** line 4320-4340 附近

**改动：**
- 删除 `_multi_field_payload()` 的调用
- 删除 `multi_field_plan` 的构建逻辑
- 当 `subject=multi` 或 `fields>=2` 时，直接走 Wren 语义层

**改前：**
```python
if (
    str(multi_plan.get("subject") or "") == "multi"
    or len(multi_fields) >= 2
    or len(out_fields) >= 2
):
    multi_payload = _multi_field_payload(...)
    if multi_payload:
        return multi_payload
```

**改后：**
```python
# 跨表多字段题统一走 Wren 语义层，不再用 multi_field_join
# Wren 基于 MDL 能更准确地理解表关系和字段含义
```

#### 改动 2：移除 multi_field_join 的确定性引擎标记

**文件：** `backend/app/services/agent.py`

**位置：** line 3522 附近

**改动：**
- 从 `_DETERMINISTIC_ENGINES` 列表中移除 `"multi_field_join"` 和 `"multi_field_parallel"`

#### 改动 3：保留 multi_field.py 但标记为废弃

**文件：** `backend/app/services/multi_field.py`

**改动：**
- 在文件头增加废弃说明
- 保留代码但不再被调用（防止其他地方还引用）
- 后续版本可以彻底删除

#### 改动 4：回归测试清理

**文件：** `backend/run_regression.py`

**改动：**
- 移除或修改依赖 multi_field_join 的测试用例
- 新增测试用例验证跨表多字段题走 Wren 路径

### 2.3 风险与应对

| 风险 | 应对 |
|---|---|
| 某些跨表多字段题 Wren 处理不好 | Wren 基于 MDL，只要 MDL 里表关系定义正确，就能处理；如果有问题，优化 MDL 而不是加新引擎 |
| 删除后某些查询变慢 | Wren 生成 SQL 可能比确定性拼接慢，但结果更准确；可以通过缓存 Wren 编译结果来优化 |
| 其他地方还引用 multi_field | 全局搜索确认没有其他引用；保留代码一个版本，确认没问题后再彻底删除 |

---

## 三、表格与知识混合查询方案

### 3.1 问题分类

表格与知识混合问题分为两类：

#### 类型 A：查询字典表的知识字段

**例子：**
- "拱起是怎么计算的？" → 查询 `ai_problem_base.formula`
- "泛油的单位是什么？" → 查询 `ai_problem_base.length_unit`
- "边坡落石属于哪一类病害？" → 查询 `ai_problem_base.disease_categories_type`
- "横向裂缝的病害代码是什么？" → 查询 `ai_problem_base.problem_code`

**特点：**
- 查询对象是字典表（`*_base`）
- 返回的是知识/描述性信息，不是业务数据统计
- 需要解释字段含义（特别是 formula 这种 JSON 格式）

#### 类型 B：查询业务数据 + 字典知识

**例子：**
- "西乡段泛油病害的计算公式是什么？" → 先查西乡段有多少泛油病害，再查泛油的计算公式
- "每种病害类型的计算公式分别是什么？" → 按病害类型分组，查每种的计算公式

**特点：**
- 既需要业务数据查询，又需要字典知识查询
- 需要把两部分结果结合起来回答

### 3.2 解决方案

#### 方案 A：新增字典知识查询意图分类

**文件：** `prompts/intent.md`

**改动：**
- 新增 `dict_knowledge` 意图类别
- 边界例子：
  - "XX是怎么计算的" → dict_knowledge
  - "XX的单位是什么" → dict_knowledge
  - "XX属于哪一类" → dict_knowledge
  - "XX的病害代码是什么" → dict_knowledge
  - "XX有多少处" → data（业务查询）

**文件：** `backend/app/services/intent.py`

**改动：**
- 在允许集合中增加 `dict_knowledge`

#### 方案 B：新增字典知识查询执行器

**文件：** `backend/app/services/dict_knowledge.py`（新建）

**功能：**
1. 识别用户问的是哪个字典表、哪个字段、哪条记录
2. 执行查询，返回原始数据
3. 特殊字段解析：
   - `formula`：解析 JSON 数组，转换成人类可读的公式描述
   - `code_map` 字段：把编码翻译成中文
   - `problem_sample_image`：返回图片 URL
4. 生成自然语言解释

**formula 解析逻辑：**
```python
def parse_formula(formula_json: str) -> str:
    """把 formula 的 JSON 数组转换成人类可读的公式描述。
    
    输入：[{"label":"长","value":1},{"label":"*","value":6},{"label":"宽","value":2}]
    输出："计算公式：长 × 6 × 宽（其中 6 是系数）"
    """
    items = json.loads(formula_json)
    parts = []
    for item in items:
        label = item.get("label", "")
        value = item.get("value", "")
        if label in ("*", "×", "x"):
            parts.append("×")
        elif label == "+":
            parts.append("+")
        elif label == "-":
            parts.append("-")
        elif label == "/":
            parts.append("÷")
        elif label == "输入框":
            num = item.get("num", "")
            parts.append(f"[输入值，默认 {num}]")
        else:
            parts.append(label)
    return " ".join(parts)
```

#### 方案 C：在 agent.py 中接入字典知识查询

**文件：** `backend/app/services/agent.py`

**改动：**
- 在意图分类后，如果是 `dict_knowledge`，调用 `dict_knowledge.query()`
- 不走业务查询链路
- 结果直接返回，不需要弹卡确认（因为是知识查询，不是业务数据统计）

### 3.3 实施步骤

#### 阶段 1：基础功能（P0）

1. 新增 `dict_knowledge` 意图分类
2. 新建 `dict_knowledge.py`，实现基本查询功能
3. 支持 `formula` 字段的解析和展示
4. 在 agent.py 中接入

#### 阶段 2：增强功能（P1）

1. 支持更多字典表（`ai_problem_base`、`basic_facilities_routes`、`basic_facilities_bridges` 等）
2. 支持类型 B（业务数据 + 字典知识混合查询）
3. 优化 formula 解析，支持更复杂的公式结构
4. 新增病害图标展示（`problem_sample_image` 字段）

#### 阶段 3：优化（P2）

1. 把字典知识查询结果存入 RAGFlow，作为相似问法的参考
2. 支持用户追问（"那这个公式里的系数6是什么意思？"）
3. 优化自然语言解释质量

---

## 四、确保所有查询都经过用户确认

### 4.1 当前问题

当前系统有多个执行引擎，其中一些会跳过用户确认直接执行：
- `multi_field_join`：直接执行
- `chosen_generic`（通用算子）：直接执行
- `metadata`：直接执行（元数据查询，可以接受）

### 4.2 解决方案

**原则：所有业务数据查询都必须经过用户确认。**

#### 改动 1：通用算子也弹卡

**文件：** `backend/app/services/agent.py`

**改动：**
- 通用算子（`chosen_generic`）匹配到后，也弹卡让用户确认
- 卡片上显示：查询口径、涉及的表、SQL 预览
- 用户确认后才执行

#### 改动 2：明确哪些查询可以不弹卡

**可以不弹卡的查询类型：**
1. 元数据查询（`metadata`）：查询库/表/字段结构，不涉及业务数据
2. 字典知识查询（`dict_knowledge`）：查询字典表的知识字段，不涉及业务数据统计
3. 高置信度模板匹配：相似度 ≥ 阈值且结构同型，可以直接弹卡（但仍然需要用户点确认）

**必须弹卡的查询类型：**
1. 所有业务数据查询（统计、明细、分组等）
2. 通用算子生成的查询
3. Wren 生成的查询（除非匹配到高置信度模板）

### 4.3 弹卡内容标准化

所有弹卡必须包含以下信息：
1. **查询口径**：用户问的是什么，系统理解的是什么
2. **涉及的表**：查询会用到哪些表
3. **过滤条件**：有哪些 WHERE 条件，特别是用户没提到的默认条件
4. **SQL 预览**：可以展开查看完整 SQL
5. **确认按钮**：用户点确认后才执行
6. **"让 AI 重新查"按钮**：用户觉得不对，可以让 AI 用 Wren 重新生成

---

## 五、给 codex 的具体修改清单

### 后端

1. **删除 multi_field_join 调用**（`agent.py` line 4320-4340）
2. **从 `_DETERMINISTIC_ENGINES` 移除 multi_field_join 和 multi_field_parallel**
3. **`multi_field.py` 标记为废弃，保留代码但不再被调用**
4. **新增 `dict_knowledge.py`**：字典知识查询执行器
5. **新增 `dict_knowledge` 意图分类**（`intent.md` + `intent.py`）
6. **在 agent.py 中接入 dict_knowledge 查询**
7. **通用算子也弹卡确认**（不再直接执行）
8. **回归测试清理和新增**

### 提示词

1. **修改 `intent.md`**：新增 `dict_knowledge` 类别和边界例子
2. **新增 `dict_knowledge.md`**：字典知识查询的语义解析提示词（如果需要 LLM 辅助解析）

### 前端

1. **弹卡内容标准化**：确保所有弹卡都包含查询口径、涉及的表、过滤条件、SQL 预览
2. **字典知识查询结果展示**：支持 formula 字段的格式化展示、病害图标展示
3. **移除 multi_field_join 相关的 UI 逻辑**（如果有）

### 配置

1. 新增 `ENABLE_MULTI_FIELD_JOIN=false`：是否启用 multi_field_join（默认 false，保留开关一个版本）
2. 新增 `ENABLE_DICT_KNOWLEDGE=true`：是否启用字典知识查询

---

## 六、验收标准

### 删除 multi_field_join

1. ✅ "路段名称以及对应的桩号以及对应的路段长度" → 走 Wren 语义层，正确 JOIN 里程桩点表，不再 JOIN 边坡表
2. ✅ 全局搜索确认没有其他地方调用 `_multi_field_payload()`
3. ✅ 跨表多字段题（"养护任务编号和病害描述"）走 Wren 能正确查询
4. ✅ 回归测试全部通过

### 字典知识查询

1. ✅ "拱起是怎么计算的？" → 查询 `ai_problem_base.formula`，返回人类可读的公式描述
2. ✅ "泛油的单位是什么？" → 查询 `ai_problem_base.length_unit`
3. ✅ "边坡落石属于哪一类病害？" → 查询 `ai_problem_base.disease_categories_type`，返回中文分类
4. ✅ formula 字段的 JSON 能正确解析和展示

### 用户确认

1. ✅ 所有业务数据查询都弹卡让用户确认
2. ✅ 弹卡包含查询口径、涉及的表、过滤条件、SQL 预览
3. ✅ 元数据查询和字典知识查询可以不弹卡
4. ✅ 用户可以点"让 AI 重新查"来换查询口径

---

## 七、风险与应对

| 风险 | 应对 |
|---|---|
| 删除 multi_field_join 后，某些跨表多字段题 Wren 处理不好 | 优化 MDL 里的表关系定义；Wren 是基于语义模型的，只要 MDL 正确就能处理 |
| 所有查询都弹卡会降低效率 | 高置信度模板匹配可以直接弹卡（减少一次 LLM 调用）；用户确认后下次相似问题可以更快 |
| 字典知识查询意图分类不准确 | 收集 bad case，持续优化意图分类提示词；可以先用关键词规则兜底 |
| formula 字段格式多样，解析不完整 | 先支持常见格式，遇到不支持的格式返回原始 JSON + 提示"格式待解析" |
