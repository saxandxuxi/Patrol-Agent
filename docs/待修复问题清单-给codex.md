# NL2SQL 待修复问题清单（给 codex）

> 整理了3个问题：全部走Wren、上下行分组bug、图片查询bug。
> 每个问题都写了：现象、根因、修复方案。

---

## 问题1：全部走 Wren，废弃模板快速通道

### 现状
现在是"模板快速通道 → Wren → 内置 LLM"三级降级，简单查询先走模板。

### 用户决定
**模板太蠢了，结果不对。宁愿多花点钱调用 LLM，也要结果对。**

### 修复方案
**全部走 Wren，模板只做兜底：**

1. **取消 `is_simple_single_query` 判断**，所有问题都走 Wren 路径
2. **模板只做 Wren 失败后的兜底**，不做快速通道
3. **子问题也全部走 Wren**，不用判断简单不简单

**改法：**
```python
# 现在的逻辑
if engine_mode != "wren" and not wren_first and is_simple_single_query(plan):
    template_sql = build_template_sql(plan)

# 改成：全部走 Wren
# template_sql = build_template_sql(plan)  # 注释掉，不走模板
# 直接走 Wren
```

**好处：**
- 所有问题都走 LLM 语义理解，准确率高
- 不用维护模板了
- 新场景不用写模板，改 MDL 就行

---

## 问题2：Wren 生成 SQL 时没听进去分组要求

### 现象

用户问："宝安大道西乡段上行和下行各有多少处病害？"
系统答："共 911 处。"

正确答案应该是：上行 911 处，下行 0 处（当前数据全部上行）。

### 根因

1. **parse_semantics 识别对了：** `group_by = direction` ✅
2. **semantic_summary 传了：** "必须按上下行分组" ✅
3. **但 LLM 还是没写 GROUP BY** ❌

**为什么 LLM 没听进去？**
- `semantic_summary` 里的提示是："必须按「上下行」分组（GROUP BY），每个分组各占一行"
- 写在 `required_conditions` 里，和其他条件混在一起
- LLM 可能当成了"建议"，不是"强制"

### 修复方案

**改 `nl2sql.py` 的 `semantic_summary` 函数：**

把 group_by 的提示写得更强制，放在最前面，用【强制】标记：

```python
if group_by:
    parts.insert(0,
        f"【强制】必须按「{_SEMANTIC_LABELS.get(group_by, group_by)}」分组，"
        f"SQL 里必须有 GROUP BY 子句，每个分组各占一行，禁止只写 COUNT(*)"
    )
```

**另外，在 `sql_mdl.md` 提示词里加一条更明确的规则：**

```
12. 用户问「各/分别/按...分」时，SQL 里必须有 GROUP BY，禁止只写一个总数。
    如果 SQL 里没有 GROUP BY，就是错的，必须重写。
```

---

## 问题3：图片查询 bug

### 现象

用户问："给我看看宝安大道西乡段面积最大的病害图片。"
系统答："没有找到相关病害图片。"

### 根因

看 `_image_result` 函数（agent.py:1319）：

```python
# 1. 直接把整个问题丢给 _build_query_result
result = _build_query_result(message, "data", kind="mysql")

# 2. 从结果里找图片字段
for row in result.get("rows", [])[:50]:
    for column in image_columns:  # problem_url, preview_url, ...
        value = row.get(column)
        ...
```

**问题出在哪？**

"给我看看面积最大的病害图片"这个问题：
- `_build_query_result` 可能查出来的是**聚合结果**（比如 COUNT(*) 或者 ORDER BY area DESC LIMIT 1）
- 如果是聚合结果，SELECT 里**根本没有 problem_url 字段**
- 自然找不到图片

**举个例子：**
- 正确的 SQL 应该是：
  ```sql
  SELECT problem_number, pile_number, area, problem_url
  FROM ai_problem ...
  ORDER BY area DESC LIMIT 1
  ```
- 但 `_build_query_result` 可能查成了：
  ```sql
  SELECT MAX(area) FROM ai_problem ...
  ```
  这样结果里只有一个数，没有 problem_url。

### 修复方案

**改 `_image_result` 函数：**

不要直接把整个问题丢给 `_build_query_result`，而是：

1. **先解析问题，提取过滤条件**（路线、桩号、病害类型、时间等）
2. **明确告诉 `_build_query_result`：要列表查询，SELECT 里必须包含 problem_url**
3. **或者直接写死 SQL：** 先查面积最大的那条，再取它的 problem_url

**具体改法：**

```python
def _image_result(message: str) -> dict[str, Any]:
    # 1. 先把"给我看看...图片"去掉，提取核心查询条件
    # 比如："给我看看宝安大道西乡段面积最大的病害图片"
    # 提取成："宝安大道西乡段面积最大的病害有哪些？列出 problem_url"
    
    # 2. 明确要求 SELECT 里包含 problem_url
    # 可以在调用 _build_query_result 时，加一个 output_fields 参数
    # 或者在问题里加上"列出图片路径"
    
    clean_message = re.sub(r"给我看看|看看|图片|照片", "", message).strip()
    clean_message += " 列出 problem_url 字段"
    
    result = _build_query_result(clean_message, "data", kind="mysql")
    ...
```

**另外还要注意：**
- 如果是"面积最大的病害图片"，必须是**列表查询**（SELECT problem_url ... ORDER BY area DESC LIMIT 1）
- 不能是聚合查询（SELECT MAX(area) ...）

---

## 修复优先级

| 优先级 | 问题 | 预期效果 |
|--------|------|---------|
| P0 | 全部走 Wren，废弃模板快速通道 | 准确率提升，不用维护模板了 |
| P0 | 上下行分组没生效 | 分组类问题准确率提升 |
| P0 | 图片查询找不到图 | 图片查询能用 |
