# 模板挖槽位与LLM审核逻辑重构方案

> 背景：当前挖槽位纯靠 SQL AST 解析，枚举值（如 disease_categories_type='15'、level='0'）经常挖不出来；LLM 审核误报率极高，大量正确模板被降级。
> 核心思路：挖槽位改为"语义理解 + AST 精确提取"两阶段；LLM 审核改为"只做参考不自动降级"或彻底移除。
> 生成时间：2026-10-12

---

## 一、问题诊断（连库+读代码验证）

### 1.1 挖槽位的具体 Bug

#### Bug 1：枚举值没有被挖成参数

**案例 1："看交安事件的病害样图"**
```sql
SELECT m.problem_sample_image AS 取值, COUNT(*) AS 数量 
FROM ai_problem_base AS m 
WHERE m.del_flag = 0 AND m.disease_categories_type = '15' 
GROUP BY 取值 ORDER BY 数量 DESC LIMIT 200
```
- 期望：`disease_categories_type = '15'` 挖成枚举参数（可换路面病害/路基病害/沿路设施/交安事件）
- 实际：显示"（无可换参数）"，`'15'` 被当成固定值

**案例 2："列出行政等级为国道的所有路线"**
```sql
SELECT r.full_name AS 具体名称, r.code AS 编号 
FROM route AS r 
WHERE r.del_flag = 0 AND r.level = '0' 
ORDER BY r.full_name LIMIT 500
```
- 期望：`level = '0'` 挖成枚举参数（可换国道/省道/县道/乡道/村道）
- 实际：显示"（无可换参数）"，`'0'` 被当成固定值

**根因分析：**
- 字段目录里这两个字段都有 `code_map`（`disease_categories_type: {'0':'路面病害','5':'路基病害','10':'沿路设施','15':'交安事件'}`，`level: {'0':'国道',...}`）
- `_classify_plain` 理论上能识别为 `type: "enum"`
- 但 `_mine_predicate` 里 enum 分支要求 `quoted=True`，且别名解析（`m.` 前缀）可能有问题
- 纯 AST 解析不理解"交安事件"和 `'15'` 的语义对应关系，无法判断这个值是否可替换

#### Bug 2：槽位描述不准确

**案例："西乡段7月份巡检了几次"**
- SQL 里有 `r.id = '{route_id}'` 和 `p.collection_time >= '{time_start}' AND p.collection_time < '{time_end}'`
- 前端只显示"病害采集时间·domain 病害采集时间·domain"，**没有显示路线槽**
- 根因：前端渲染槽位时可能只显示了部分参数，或者参数的 label 映射有问题

#### Bug 3：固定条件和可替换参数混淆

- `del_flag = 0` 是硬性隐含条件（所有查询都带），不应该挖成参数
- `disease_categories_type = '15'` 是用户指定的业务条件，应该挖成参数
- 纯 AST 解析无法区分这两者，只能靠字段目录的 `fixed_columns` 配置，但配置可能不全

### 1.2 LLM 审核的具体问题

#### 问题 1：误报率极高

用户提供的模板列表里，**几乎所有模板**都显示"待观察 审核未过（已降级）"：
- 宝安大道西乡段的泛油病害，统计总处数、总面积和平均面积 → 审核未过
- 宝安大道西乡段的病害中，有图片的和没有图片的分别有多少 → 审核未过
- 宝安大道西乡段病害的最大宽度和最大高度分别是多少 → 审核未过
- 西乡段的经度范围 → 审核未过
- 恒明珠段按巡检时间看，是越往后病害越多还是越少 → 审核未过
- ...（共 10+ 条全部审核未过）

**这些模板的 SQL 都是用户确认过"答对了"的，不应该被降级。**

#### 问题 2：审核口径不可靠

虽然提示词里明确说：
> 只有 parameter_misaligned / partial_answer 算 high（会触发降级）；hardcoded_condition / hardcoded_alias / wrong_unit 只算提示

但 LLM（qwen3-coder-plus）仍然会：
- 把 `{route_id}` 占位符当成"硬编码"
- 把模板问法里本来就有的条件（如"7月30日"、"泛油"）当成"多余条件"
- 把正常的单参数模板判成"参数错位"

实测之前已经发现 3/3 误报，现在看来误报率更高。

#### 问题 3：审核结果不可解释

- 前端只显示"审核未过（已降级）"，不显示具体原因
- 用户无法判断是真的有问题还是误报
- 也无法手动纠正审核结果

---

## 二、新的挖槽位逻辑（语义理解 + AST 精确提取）

### 2.1 核心思路

**当前逻辑（纯 AST）：**
```
SQL → sqlglot 解析 → 遍历 WHERE 谓词 → 按字段目录判断类型 → 挖槽
```
问题：不理解语义，枚举值经常挖不出来，固定条件和可替换参数混淆。

**新逻辑（两阶段）：**
```
阶段1（语义理解）：
  SQL → 提取涉及的表 → 查字段目录/MDL 获取字段注释 → 
  LLM 输出 JSON：每个 WHERE 条件是"可替换参数"还是"固定条件"，如果是参数，参数名/类型/可选值是什么

阶段2（AST 精确提取）：
  根据阶段1的 JSON 结果 → sqlglot 精确替换对应谓词的值为占位符 → 
  生成参数化 SQL + params 元数据
```

**关键原则：**
- LLM 只做**语义理解和分类**，不直接修改 SQL（不可靠）
- sqlglot 做**精确的文本替换**（可靠）
- 两者结合：LLM 告诉代码"哪个条件该挖、挖成什么参数"，代码负责精确执行

### 2.2 阶段1：语义理解（LLM 输出 JSON）

#### 输入给 LLM 的信息

```
1. 原始问题（用户问法）
2. 原始 SQL
3. SQL 涉及的表及其字段注释（从字段目录/MDL 取）
   例如：
   - ai_problem_base.disease_categories_type: 所属大类（0路面病害，5路基病害，10沿路设施，15交安事件）
   - ai_problem_base.del_flag: 删除标记
   - basic_facilities_routes.level: 行政级别（0国道 1省道 2县道 3乡道 4村道）
4. 固定条件白名单（del_flag、tenant_id 等系统字段）
```

#### LLM 输出的 JSON 格式

```json
{
  "conditions": [
    {
      "sql_fragment": "m.disease_categories_type = '15'",
      "decision": "parameter",
      "param_name": "disease_category",
      "param_type": "enum",
      "param_label": "所属大类",
      "options": [
        {"label": "路面病害", "value": "0"},
        {"label": "路基病害", "value": "5"},
        {"label": "沿路设施", "value": "10"},
        {"label": "交安事件", "value": "15"}
      ],
      "original_value": "15",
      "reason": "用户问的是交安事件，这个值可以换成其他病害大类"
    },
    {
      "sql_fragment": "m.del_flag = 0",
      "decision": "fixed",
      "reason": "删除标记是系统隐含条件，所有查询都带，不可替换"
    },
    {
      "sql_fragment": "r.id = '1b0b4a9f700477b1d96119caac87c168'",
      "decision": "parameter",
      "param_name": "route_id",
      "param_type": "entity",
      "param_label": "路线",
      "original_value": "1b0b4a9f700477b1d96119caac87c168",
      "reason": "用户指定了宝安大道西乡段，可以换成其他路线"
    }
  ],
  "limit_decision": "fixed",
  "limit_reason": "LIMIT 200 是平台行数上限，不是用户指定的 TopN"
}
```

#### LLM 提示词设计要点（已改进，见 prompts/templatize_classify.md）

**原提示词的问题：**
1. 没有把 SQL 涉及的表和字段描述放进上下文 → LLM 只能猜字段含义
2. 没有完整的成功/失败示例 → LLM 不知道什么样的输出是对的
3. 没有分步思考逻辑 → LLM 直接给结果，容易漏判或误判
4. 只有一个反面例子（ABS(pile_distance - 1802)），不够全面

**改进后的提示词结构：**

```
1. 角色定义：SQL 模板参数化助手
2. 输入信息说明：
   - 用户问法
   - 参数化 SQL
   - 可疑字面值
   - SQL 涉及的表和字段描述（新增！重点！）
   - 审核发现的问题
3. 判断步骤（5步）：
   - 步骤1：理解用户问法，找出业务实体和条件
   - 步骤2：对照字段描述，理解每个可疑值的含义
   - 步骤3：判断"换了它答案会不会变"
   - 步骤4：排除已经是参数的
   - 步骤5：拿不准就不要报
4. 成功示例（3个）：
   - 示例1：枚举值挖槽（交安事件 disease_categories_type='15'）
   - 示例2：多参数都已挖好（返回空 slots）
   - 示例3：外键实体挖槽（damage_type='10' 对应泛油）
5. 注意事项：
   - 同一值多处出现要逐条报
   - fragment 必须逐字匹配
   - value 照抄不带引号
   - column_hint 用真实列名
   - param_type 只能是 numeric/enum/entity/time
6. 输出格式：JSON
```

**关键改进：字段描述上下文**

在调用 LLM 之前，代码需要先提取 SQL 涉及的表，然后从字段目录查出每个字段的：
- 中文名（label）
- 注释（comment）
- 枚举值映射（code_map，如 "0=国道,1=省道..."）
- 外键关联（join，如 damage_type 关联 ai_problem_base）

然后把这些信息格式化后放进提示词的 `<<table_fields>>` 占位符。

**示例字段描述格式：**
```
ai_problem_base.disease_categories_type: 所属大类（0路面病害，5路基病害，10沿路设施，15交安事件）
ai_problem_base.del_flag: 删除标记
ai_problem_base.problem_sample_image: 病害样图
basic_facilities_routes.level: 行政级别（0国道 1省道 2县道 3乡道 4村道）
ai_problem.damage_type: 损坏程度（外键，关联 ai_problem_base.problem_code）
```

这样 LLM 看到 `disease_categories_type = '15'` 时，对照字段描述就知道"15=交安事件"，是用户指定的业务条件，应该挖成枚举参数。

### 2.3 阶段2：AST 精确提取（代码执行）

#### 流程

```python
def mine_sql_v2(question, sql, kind):
    # 1. sqlglot 解析 SQL
    tree = sqlglot.parse(sql, read="mysql")[0]
    
    # 2. 提取涉及的表，查字段目录
    tables = extract_tables(tree)
    field_comments = get_field_comments(tables)  # 从 schema_catalog 取
    
    # 3. 阶段1：LLM 语义理解，输出 conditions JSON
    conditions_json = llm_classify_conditions(question, sql, field_comments)
    
    # 4. 阶段2：根据 JSON，sqlglot 精确替换
    params = {}
    values = {}
    declares = {}
    for cond in conditions_json["conditions"]:
        if cond["decision"] == "fixed":
            continue  # 固定条件，不处理
        
        # 用 sqlglot 找到对应的谓词节点，精确替换值为占位符
        predicate = find_predicate_by_fragment(tree, cond["sql_fragment"])
        if predicate:
            placeholder = f"{{{cond['param_name']}}}"
            replace_literal(predicate, placeholder)
            params[cond["param_name"]] = build_slot_spec(cond)
            values[cond["param_name"]] = cond["original_value"]
            declares[cond["param_name"]] = cond["param_name"]
    
    # 5. 处理 LIMIT
    if conditions_json.get("limit_decision") == "parameter":
        # 挖 TopN 槽
        ...
    
    # 6. 生成参数化 SQL
    parameterized_sql = tree.sql(dialect="mysql")
    
    return {
        "question": clean_question(question),
        "sql": parameterized_sql,
        "params": params,
        "values": values,
        "declares": declares,
        ...
    }
```

#### 关键实现细节

1. **精确匹配谓词**：
   - 用 `cond["sql_fragment"]` 在 AST 里找到对应的谓词节点
   - 匹配方式：规范化 SQL 文本后比对（去空格、统一引号）
   - 找不到时 fallback：按列名+操作符匹配

2. **保留现有 AST 逻辑作为兜底**：
   - 如果 LLM 输出的 JSON 有问题（格式错误、条件不匹配），fallback 到现有的纯 AST 挖槽逻辑
   - 保证不会因为 LLM 抖动导致挖槽完全失败

3. **缓存 LLM 结果**：
   - 按 SQL 指纹缓存 LLM 分类结果，同一条 SQL 不重复调用
   - 挖槽是后台操作（用户点"答对了"时触发），慢一点可以接受

### 2.4 与现有逻辑的对比

| 维度 | 现有逻辑（纯 AST） | 新逻辑（语义+AST） |
|---|---|---|
| 枚举值识别 | 靠字段目录 code_map，经常挖不出来 | LLM 看字段注释，能识别枚举值 |
| 固定条件判断 | 靠 fixed_columns 配置，可能不全 | LLM 理解语义，能区分系统字段和业务条件 |
| 参数名/label | 代码自动生成，可能不准确 | LLM 根据字段注释生成，更准确 |
| 可靠性 | 高（纯代码） | 中（LLM 可能出错，但有 fallback） |
| 成本 | 0 LLM 调用 | 1 次 LLM 调用（后台，可缓存） |

---

## 三、LLM 审核的改进方案

### 3.1 问题本质

LLM 审核的核心问题：**让一个代码模型（qwen3-coder-plus）去判断"模板复用会不会错"，但它不理解项目的模板复用机制**。

它不知道：
- `{route_id}` 是占位符，复用时会被替换
- 模板问法里的条件（如"7月30日"）是用户指定的，不是多余条件
- 弹卡时有确定性闸（条件对不上不摆卡），所以"SQL 里写了过滤值"本身不是问题

### 3.2 方案 A：保留 LLM 审核，但只做参考不自动降级（推荐）

#### 改动

1. **LLM 审核结果只显示，不自动降级**
   - 移除"high 级 issue 自动降级"的逻辑
   - 审核结果显示为"审核建议"，用户可以看具体原因
   - 是否降级由用户手动决定

2. **审核结果可解释**
   - 前端显示具体的 issue 列表（类型、严重程度、描述、位置）
   - 不是只显示"审核未过（已降级）"

3. **增加"忽略审核建议"按钮**
   - 用户可以标记"审核误报"，该模板的审核结果不再显示
   - 收集误报数据，用于后续优化提示词

4. **审核提示词进一步收窄**
   - 只审 `parameter_misaligned` 和 `partial_answer` 两类
   - 明确告诉 LLM："如果你不确定，就报 pass=true"
   - 增加更多正面示例（正确的模板应该长什么样）

#### 优点
- 保留了 LLM 审核的参考价值
- 不会因为误报导致好模板被降级
- 用户有最终决定权

#### 缺点
- 用户需要手动判断审核建议是否合理
- 仍然有 LLM 调用成本

### 3.3 方案 B：彻底移除 LLM 审核，改用确定性规则审核

#### 改动

1. **移除 LLM 审核功能**
   - 删除 `review_template()`、`review_pending()` 函数
   - 删除 `template_review.md` 提示词
   - 前端移除"审核未过"标签和审核按钮

2. **改用确定性规则审核**
   - `parameter_misaligned`：检查同类参数的多个槽是否与列别名/子查询语义对得上（可以用代码检查）
   - `partial_answer`：检查模板问法里的问号/并列指标数量 vs SQL 输出列数量
   - 其他类型不审（误报率太高）

3. **规则审核结果只做提示**
   - 不自动降级
   - 显示"可能存在的问题"，用户手动判断

#### 优点
- 0 LLM 调用成本
- 结果可解释、可复现
- 不会误报

#### 缺点
- 规则只能覆盖简单情况，复杂问题发现不了
- 需要维护规则

### 3.4 方案 C：保留 LLM 审核，但用更强的模型

#### 改动
- 把审核模型从 `qwen3-coder-plus` 换成更强的模型（如 qwen3.7-plus 或 gpt-4o）
- 优化提示词，增加更多上下文（模板复用机制的说明）

#### 优点
- 可能降低误报率

#### 缺点
- 成本更高
- 不一定能解决根本问题（模型还是不理解项目机制）

### 3.5 推荐方案

**推荐方案 A（保留 LLM 审核，但只做参考不自动降级）**，原因：
1. LLM 审核确实能发现一些问题（比如真的参数错位、只答了一部分）
2. 但自动降级的代价太大（好模板被误报降级，退出推荐）
3. 用户是最终决策者，应该由用户判断审核建议是否合理
4. 改动量小，风险低

如果后续发现 LLM 审核建议大部分都是误报，可以再升级到方案 B（彻底移除）。

---

## 四、具体实施步骤

### 阶段 1：挖槽位重构（P0）

#### 步骤 1：新增 LLM 语义分类函数

文件：`backend/app/services/templatizer.py`

```python
def llm_classify_conditions(question: str, sql: str, tables: list[str]) -> dict:
    """阶段1：LLM 语义理解，输出每个 WHERE 条件的分类 JSON。"""
    # 1. 查字段目录，获取涉及表的字段注释
    field_comments = []
    for table in tables:
        columns = schema_catalog.list_columns("mysql", table)
        for col in columns:
            info = schema_catalog.resolve_column("mysql", table, col)
            if info:
                field_comments.append({
                    "table": table,
                    "column": col,
                    "label": info.get("label"),
                    "comment": info.get("comment"),
                    "code_map": info.get("code_map"),
                })
    
    # 2. 调用 LLM，输出 conditions JSON
    prompt = build_classify_prompt(question, sql, field_comments)
    result = chat_json(prompt, model=role_model("sql"))
    
    return result
```

#### 步骤 2：新增挖槽位 V2 函数

文件：`backend/app/services/templatizer.py`

```python
def mine_sql_v2(question: str, sql: str, kind: str = "mysql") -> tuple[dict | None, str]:
    """新的挖槽位逻辑：语义理解 + AST 精确提取。"""
    # 1. sqlglot 解析
    # 2. 提取涉及的表
    # 3. LLM 语义分类
    # 4. 根据分类结果，sqlglot 精确替换
    # 5. fallback：LLM 失败时用现有 mine_sql
    ...
```

#### 步骤 3：修改挖槽入口

文件：`backend/app/services/templatizer.py`

- `mine_sql()` 改为调用 `mine_sql_v2()`
- `mine_all()` 里的挖槽逻辑也改用 V2
- 保留 `mine_sql_v1()` 作为 fallback

#### 步骤 4：前端槽位显示修复

文件：`frontend/src/components/`（模板管理页）

- 确保所有参数都显示（包括路线槽）
- 参数 label 用 LLM 生成的 `param_label`，更准确

### 阶段 2：LLM 审核改进（P0）

#### 步骤 1：移除自动降级逻辑

文件：`backend/app/services/templatizer.py`

- `apply_review()` 里，移除"high 级 issue 自动降级"的逻辑
- 审核结果只存储，不改变模板状态

#### 步骤 2：前端显示审核详情

文件：`frontend/src/components/`（模板管理页）

- 点击"审核建议"展开具体 issue 列表
- 显示类型、严重程度、描述、位置
- 增加"忽略此建议"按钮

#### 步骤 3：优化审核提示词

文件：`prompts/template_review.md`

- 增加更多正面示例
- 明确告诉 LLM："不确定就报 pass=true"
- 增加项目模板复用机制的说明

### 阶段 3：验证与回归（P1）

#### 测试用例

1. **枚举值挖槽测试**：
   - "看交安事件的病害样图" → `disease_categories_type` 应该挖成枚举参数
   - "列出国道的所有路线" → `level` 应该挖成枚举参数

2. **固定条件识别测试**：
   - 所有带 `del_flag = 0` 的 SQL → 不应该挖成参数
   - `tenant_id` 等系统字段 → 不应该挖成参数

3. **槽位显示测试**：
   - "西乡段7月份巡检了几次" → 应该显示路线槽 + 时间槽

4. **LLM 审核误报测试**：
   - 现有 10+ 条被降级的模板 → 重新审核，应该大部分 pass
   - 审核结果只显示，不自动降级

---

## 五、验收标准

### 挖槽位

1. ✅ "看交安事件的病害样图" → `disease_categories_type` 挖成枚举参数，options 包含 4 种病害大类
2. ✅ "列出国道的所有路线" → `level` 挖成枚举参数，options 包含 5 种行政级别
3. ✅ `del_flag = 0` 不被挖成参数（固定条件）
4. ✅ "西乡段7月份巡检了几次" → 前端显示路线槽 + 时间槽（2个参数）
5. ✅ LLM 失败时 fallback 到现有逻辑，不会完全失败
6. ✅ 同一条 SQL 的挖槽结果可缓存，不重复调用 LLM

### LLM 审核

1. ✅ 审核结果不自动降级模板状态
2. ✅ 前端显示具体的审核 issue 列表（不是只显示"审核未过"）
3. ✅ 用户可以"忽略审核建议"
4. ✅ 现有被误降级的模板重新审核后，大部分应该 pass
5. ✅ 真的有问题的模板（参数错位、只答一部分）仍然能被发现

---

## 六、给 codex 的具体修改清单

### 后端（`templatizer.py`）

1. **新增 `llm_classify_conditions()` 函数**：输入问题+SQL+涉及表，输出条件分类 JSON
2. **新增 `mine_sql_v2()` 函数**：两阶段挖槽位（语义+AST）
3. **修改 `mine_sql()` 入口**：优先调用 V2，失败 fallback 到 V1
4. **修改 `apply_review()` 函数**：移除 high 级 issue 自动降级逻辑
5. **新增 `get_review_details()` 函数**：返回模板的审核详情（供前端展示）

### 提示词（`prompts/`）

1. **新增 `templatize_classify.md`**：条件分类提示词（阶段1用）
2. **修改 `template_review.md`**：优化审核提示词，增加正面示例，明确"不确定就 pass"

### 前端（模板管理页）

1. **槽位显示修复**：确保所有参数都显示，label 用 LLM 生成的
2. **审核详情展示**：点击展开具体 issue 列表
3. **"忽略审核建议"按钮**：用户可以标记误报
4. **移除"审核未过（已降级）"标签**：改为"审核建议：N 条"

### 配置（`.env`）

1. 新增 `TEMPLATE_MINE_USE_LLM=true/false`：是否启用 LLM 辅助挖槽（默认 true）
2. 新增 `TEMPLATE_REVIEW_AUTO_DEMOTE=false`：是否自动降级（默认 false）

---

## 七、风险与应对

| 风险 | 应对 |
|---|---|
| LLM 分类结果不稳定 | 有 fallback 到纯 AST 逻辑；结果按 SQL 指纹缓存 |
| LLM 分类增加挖槽耗时 | 挖槽是后台操作（用户点"答对了"时触发），慢一点可接受 |
| 新逻辑可能引入新 bug | 保留 V1 作为 fallback；先小范围测试，再全量切换 |
| 移除自动降级后，真的有问题的模板可能留在推荐里 | 审核建议仍然显示，用户可以手动降级；连续答错仍然自动降级 |
