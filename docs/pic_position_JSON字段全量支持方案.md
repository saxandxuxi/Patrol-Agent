# pic_position JSON 字段全量支持方案

> 生成时间：2026-10-10
> 目标：让 `ai_problem.pic_position` JSON 字段里的所有信息都能被查询、过滤、调出图片，不能只关注置信度

---

## 一、问题背景

### 1.1 pic_position 字段存储的内容

`ai_problem.pic_position` 是一个 JSON 字段，存储了病害图片的标注信息：

```json
{
  "confidence": "0.666667",   // AI 识别置信度
  "height": "2.32",             // 病害高度（米）
  "lat": "22.556322171925583", // 纬度
  "lng": "113.90995921297466", // 经度
  "type": "纵向裂缝",            // 病害类型（AI 识别的类型）
  "width": "0.2",               // 病害宽度（米）
  "x0": "1477.5",               // bounding box 左上角 x
  "x1": "1790.39",              // bounding box 右下角 x
  "y0": "942.47",               // bounding box 左上角 y
  "y1": "1199.71"               // bounding box 右下角 y
}
```

### 1.2 当前只支持了 confidence

代码里只注册了 `confidence` 这一个 JSON 字段：

| 位置 | 注册情况 |
|---|---|
| `field_catalog.py` 的 `_SPECIAL_FIELDS` | 只有 `confidence` |
| `nl2sql.py` 的逻辑字段映射 | 只有 `confidence` |
| 语义层提示词 | 只提到了置信度 |
| 模板构造器 | 只识别 confidence 的 JSON_EXTRACT |

其他 9 个字段（type、width、height、lat、lng、x0、x1、y0、y1）**完全没有注册**。

### 1.3 导致的问题

用户问"调出横向裂缝的图片"时：
- 系统不知道 `type` 字段在 JSON 里
- 不知道用 `JSON_EXTRACT(p.pic_position, '$.type')` 来查询
- 结果：要么调不出来，要么走 Wren 生成错误的 SQL

类似的问题：
- "调出宽度大于 1 米的病害图片" → `width` 字段没注册
- "调出高度大于 2 米的病害图片" → `height` 字段没注册
- "调出经度在 113.9 到 114.0 之间的病害图片" → `lng` 字段没注册
- "调出 AI 识别为坑槽的病害图片" → `type` 字段没注册

---

## 二、改造方案

### 2.1 第一步：在 _SPECIAL_FIELDS 里注册所有 JSON 字段

修改 `field_catalog.py` 的 `_SPECIAL_FIELDS` 字典，增加以下字段：

```python
_SPECIAL_FIELDS: dict[str, tuple[str, bool, str]] = {
    # 已有
    "confidence": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6))",
        True, "AI 置信度",
    ),
    "problem_type_name": ("b.problem_type", False, "病害类型"),
    "route_name": ("r.short_name", False, "路线名"),
    "task_number": ("t.task_number", False, "巡检任务编号"),
    "lng_lat": ("CONCAT(p.lng, ',', p.lat)", False, "经纬度"),
    "image": (...),
    "has_image": (...),
    "problem_id": ("p.id", False, "病害id"),

    # 新增：pic_position JSON 里的其他字段
    "pic_type": (
        "JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type'))",
        False, "图片标注病害类型",
    ),
    "pic_width": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.width')) AS DECIMAL(10,2))",
        True, "图片标注宽度（米）",
    ),
    "pic_height": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.height')) AS DECIMAL(10,2))",
        True, "图片标注高度（米）",
    ),
    "pic_lng": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.lng')) AS DECIMAL(12,8))",
        True, "图片标注经度",
    ),
    "pic_lat": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.lat')) AS DECIMAL(12,8))",
        True, "图片标注纬度",
    ),
    "pic_x0": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.x0')) AS DECIMAL(10,2))",
        True, "标注框左上角X",
    ),
    "pic_x1": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.x1')) AS DECIMAL(10,2))",
        True, "标注框右下角X",
    ),
    "pic_y0": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.y0')) AS DECIMAL(10,2))",
        True, "标注框左上角Y",
    ),
    "pic_y1": (
        "CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.y1')) AS DECIMAL(10,2))",
        True, "标注框右下角Y",
    ),
}
```

**命名约定**：
- JSON 里的字段统一加 `pic_` 前缀，避免和物理表的列名冲突（比如 `type` 和 `pic_type`、`lat` 和 `pic_lat`）
- 数值字段（width、height、lng、lat、x0、x1、y0、y1）用 `CAST AS DECIMAL`，支持比较和排序
- 文本字段（type）用 `JSON_UNQUOTE`，支持等值和模糊查询

### 2.2 第二步：在 nl2sql.py 里注册这些字段

nl2sql.py 里有多处需要同步注册：

1. **逻辑字段映射**（第82-83行附近）：
   ```python
   "confidence": ("confidence", "pic_position"),
   "pic_type": ("pic_type", "pic_position"),
   "pic_width": ("pic_width", "pic_position"),
   # ... 其他字段
   ```

2. **SQL 表达式生成**（第2226行、第3580行、第4145行、第4715行附近）：
   - 这些地方有 `confidence` 的 JSON_EXTRACT 表达式，需要同步增加其他字段的表达式
   - 最好抽成一个公共函数，避免每处都手写

3. **字段触达/语义检索**（第303行、第324行、第337行附近）：
   - 这些地方有 `confidence` 的字段名列表，需要同步增加其他字段

4. **指标/维度关键词映射**（第4049行、第4074-4076行附近）：
   - 增加"标注类型""标注宽度""标注高度"等关键词到语义字段映射

### 2.3 第三步：语义层提示词里加入这些字段

修改 `prompts/query_semantic.md`（或相关提示词文件），在字段清单里加入：

```
## pic_position JSON 标注字段（AI 识别时写入，可能为空）

| 语义字段名 | 物理表达式 | 类型 | 说明 |
|---|---|---|---|
| pic_type | JSON_EXTRACT(pic_position, '$.type') | 文本 | AI 识别的病害类型（如"纵向裂缝""横向裂缝""坑槽"） |
| pic_width | CAST(JSON_EXTRACT(pic_position, '$.width') AS DECIMAL) | 数值 | 病害宽度（米） |
| pic_height | CAST(JSON_EXTRACT(pic_position, '$.height') AS DECIMAL) | 数值 | 病害高度（米） |
| pic_lng | CAST(JSON_EXTRACT(pic_position, '$.lng') AS DECIMAL) | 数值 | 标注经度 |
| pic_lat | CAST(JSON_EXTRACT(pic_position, '$.lat') AS DECIMAL) | 数值 | 标注纬度 |
| confidence | CAST(JSON_EXTRACT(pic_position, '$.confidence') AS DECIMAL) | 数值 | AI 识别置信度 |

注意：
- 这些字段只在 pic_position 不为空时才有值，查询时自动加 pic_position IS NOT NULL
- pic_type 是 AI 识别的类型，和病害表的 damage_type（人工/字典类型）可能不一致
- 用户问"横向裂缝的图片"时，用 pic_type = '横向裂缝' 过滤
```

### 2.4 第四步：JSON 字段查询时自动加非空过滤

所有涉及 JSON 字段的查询（WHERE/SELECT/GROUP BY），自动加 `p.pic_position IS NOT NULL AND p.pic_position <> ''` 条件。

当前代码里已经有这个逻辑（nl2sql.py 第3603-3604行、第4168-4169行），但只针对 `confidence`。需要扩展到所有 JSON 字段。

伪代码：
```python
_JSON_FIELDS = {"confidence", "pic_type", "pic_width", "pic_height", "pic_lng", "pic_lat",
                 "pic_x0", "pic_x1", "pic_y0", "pic_y1"}

def needs_pic_position_check(expr):
    return any(f"$.{field.replace('pic_', '')}" in expr for field in _JSON_FIELDS)

# 在生成 WHERE 条件时
if needs_pic_position_check(expression):
    where.append("p.pic_position IS NOT NULL AND p.pic_position <> ''")
```

### 2.5 第五步：图片查询时支持按 JSON 字段过滤

当前图片查询的固定口径（agent.py 第2815行附近）只查 `problem_url IS NOT NULL` 的行，过滤条件走正常的 SQL 生成。

需要确保：
- 图片查询时，用户的过滤条件（如"横向裂缝""宽度大于1米"）能正确生成 JSON_EXTRACT 的 SQL
- 过滤条件和图片查询的固定口径（只查有图的）能正确组合

伪代码：
```sql
-- 用户问"调出横向裂缝的图片"
SELECT p.problem_url AS 病害图片, p.pile_number, ...
FROM ai_problem p
WHERE p.del_flag = 0
  AND p.problem_url IS NOT NULL AND p.problem_url <> ''
  AND p.pic_position IS NOT NULL AND p.pic_position <> ''
  AND JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) = '横向裂缝'
ORDER BY p.collection_time DESC
LIMIT 500
```

### 2.6 第六步：模板挖掘时识别 JSON 字段

模板挖掘时（templatizer.py 的 `mine_sql` 函数），需要识别 WHERE 条件里的 JSON_EXTRACT 表达式，把 JSON 字段的值参数化。

比如：
```sql
WHERE JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) = '横向裂缝'
```
应该被挖掘为：
```sql
WHERE JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.type')) = '{{pic_type}}'
```
参数槽位：
```python
"pic_type": {
    "type": "enum",
    "options": [{"label": "纵向裂缝", "value": "纵向裂缝"}, ...],
    # 或者从数据里 DISTINCT 查
}
```

---

## 三、字段分类与优先级

### 3.1 高优先级（用户最可能问的）

| 字段 | 语义名 | 典型问法 |
|---|---|---|
| type | pic_type | "调出横向裂缝的图片""AI 识别为坑槽的病害图片" |
| width | pic_width | "宽度大于1米的病害图片""宽度排名前三的病害" |
| height | pic_height | "高度大于2米的病害图片""高度排名前三的病害" |
| confidence | confidence（已有） | "置信度高于0.7的病害图片" |

### 3.2 中优先级（可能问的）

| 字段 | 语义名 | 典型问法 |
|---|---|---|
| lng | pic_lng | "经度在113.9到114.0之间的病害" |
| lat | pic_lat | "纬度在22.5到22.6之间的病害" |

### 3.3 低优先级（不太可能问，但应该支持）

| 字段 | 语义名 | 典型问法 |
|---|---|---|
| x0, x1, y0, y1 | pic_x0, pic_x1, pic_y0, pic_y1 | "标注框宽度大于100像素的病害"（基本不会问） |

**建议**：先做高优先级和中优先级的 6 个字段（type、width、height、lng、lat + 已有的 confidence），低优先级的 4 个字段（x0、x1、y0、y1）可以以后再做，或者一次性都注册上（代码量差不多）。

---

## 四、需要注意的坑

### 4.1 pic_type 和 damage_type 的区别

- `pic_type`：AI 识别时写入 JSON 的类型，可能为空、可能不准确
- `damage_type`：病害表的字段，是病害代码（如'10'），关联字典表得到中文名
- 用户问"横向裂缝的病害"时，应该用 `damage_type`（字典类型）
- 用户问"横向裂缝的图片"或"AI 识别为横向裂缝"时，应该用 `pic_type`（JSON 标注类型）

语义层需要区分这两种问法，提示词里要说明。

### 4.2 JSON 字段可能为空

不是所有病害都有 `pic_position`（没有图片的病害可能为空），查询时必须加非空过滤，否则 JSON_EXTRACT 返回 NULL，比较结果不符合预期。

### 4.3 JSON 字段的值是字符串

JSON 里的数值（width、height、confidence 等）存储时是字符串（如 `"2.32"`），需要用 `CAST AS DECIMAL` 才能正确比较和排序。

### 4.4 字段命名冲突

JSON 里的 `type`、`lat`、`lng` 等字段名可能和物理表的列名或其他语义字段冲突，所以统一加 `pic_` 前缀。

### 4.5 模板挖掘时的 JSON_EXTRACT 识别

模板挖掘时，需要识别 `JSON_EXTRACT(p.pic_position, '$.type')` 这类表达式，不能只识别裸列名。当前 `_classify_expression` 函数（templatizer.py 第970行）已经有识别"函数/JSON 包裹的裸列"的逻辑，可以扩展支持所有 JSON 字段。

---

## 五、验收标准

1. "调出横向裂缝的图片" → 生成正确的 SQL，用 `JSON_EXTRACT(pic_position, '$.type') = '横向裂缝'` 过滤，返回图片
2. "调出宽度大于 1 米的病害图片" → 用 `CAST(JSON_EXTRACT(pic_position, '$.width') AS DECIMAL) > 1` 过滤
3. "调出高度大于 2 米的病害图片" → 用 `CAST(JSON_EXTRACT(pic_position, '$.height') AS DECIMAL) > 2` 过滤
4. "调出经度在 113.9 到 114.0 之间的病害图片" → 用 `pic_lng BETWEEN 113.9 AND 114.0` 过滤
5. "AI 识别为坑槽的病害有哪些" → 用 `pic_type = '坑槽'` 过滤（注意和 damage_type 区分）
6. 所有 JSON 字段查询自动加 `pic_position IS NOT NULL` 条件
7. 模板挖掘时，JSON 字段的值能被参数化（如 `pic_type = '{{pic_type}}'`）
8. 字段触达能命中 pic_type、pic_width 等字段（用户问"标注类型"时能找到 pic_type）
9. 语义层提示词里有 pic_position JSON 字段的说明
10. 回归测试：原有的 confidence 相关查询不受影响

---

## 六、实施顺序建议

### 第一步：注册字段（最快见效）

1. 在 `field_catalog.py` 的 `_SPECIAL_FIELDS` 里注册所有 JSON 字段
2. 在 `nl2sql.py` 的相关位置同步注册
3. 抽公共函数，避免每处手写 JSON_EXTRACT 表达式

**预计工作量**：小到中等（主要是复制粘贴 + 抽函数）

### 第二步：提示词和字段触达

1. 在语义层提示词里加入 JSON 字段的说明
2. 在字段触达/语义检索里加入这些字段
3. 增加关键词映射（"标注类型"→pic_type、"标注宽度"→pic_width 等）

**预计工作量**：小

### 第三步：JSON 字段查询的非空过滤和模板挖掘

1. 扩展非空过滤逻辑，覆盖所有 JSON 字段
2. 模板挖掘时识别 JSON_EXTRACT 表达式，参数化 JSON 字段的值

**预计工作量**：中等

### 第四步：测试和回归

1. 用验收标准里的 10 条测试用例验证
2. 回归测试原有的 confidence 相关查询
3. 测试 pic_type 和 damage_type 的区分

**预计工作量**：小到中等

---

## 七、不做的边界

1. **不修改 pic_position 的存储格式**——还是 JSON，不改成独立列
2. **不做 JSON 字段的自动补全**——用户输入"横"时自动提示"横向裂缝"，这个以后做
3. **不做 pic_type 和 damage_type 的自动对齐**——AI 识别的类型和字典类型可能不一致，不自动映射
4. **不做 bounding box 的可视化**——x0、x1、y0、y1 只支持查询，不在前端画框
5. **不删除已有的 confidence 逻辑**——在原有基础上扩展，不替换
