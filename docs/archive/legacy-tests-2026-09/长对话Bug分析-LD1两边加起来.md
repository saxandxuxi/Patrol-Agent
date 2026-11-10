# 长对话 Bug 分析报告：LD1「两边加起来呢？」答成 911/0

> 文档定位：面向 Codex 的根因分析 + 修复指引。本次分析**没有**停留在"发现一个 bug 修一个模板"，而是把整条多轮链路逐环节钉死，给出通用修复方向。
> 所有结论均来自真实现场证据（agent.db 的 query_history / conversation_messages / example_pairs + agent.log），可复核。

---

## 1. 现场复现（2026-09-16 09:32~09:33，会话 9eda8eaa）

| 轮次 | 用户提问 | 系统回答 | 期望 |
|---|---|---|---|
| R1 | 宝安大道西乡段一共多少处病害？ | 共 911 处 ✅ | 911 |
| R2 | 那恒明珠段呢？ | （已沿用上一条问题的条件：有多少处）共 749 处 ✅ | 749 |
| R3 | 两边加起来呢？ | （已沿用上一条问题的条件）查询结果显示，上行数量为 911，下行数量为 0，两者相加的总数量为 911… ❌ | **1660（911+749）** |
| R4 | 哪边泛油多？各多少？ | （未实测） | 西乡 534 > 恒明珠 402 |
| R5 | 多多少？ | （未实测） | 多 132（534−402） |

**用户的疑问**：这是模型能力问题，还是代码问题？

**结论先行**：既不是"模型笨"，也不是"一个 SQL 模板写错"，而是 **6 个工程缺陷叠加**导致的。R3 从"继承上下文"到"生成 SQL"到"校验放行"到"结果摘要"每一环都出了问题。修复必须按系统级方案做，不能打补丁。

---

## 2. 现场铁证（从 agent.db / agent.log 提取，可复核）

### 证据 1：R3 实际执行的 SQL（query_history id=1406）

```sql
SELECT 
    SUM(CASE WHEN p.up_or_down = 1 THEN 1 ELSE 0 END) AS 上行数量,
    SUM(CASE WHEN p.up_or_down = 2 THEN 1 ELSE 0 END) AS 下行数量,
    COUNT(*) AS 总数量
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 
    AND r.del_flag = 0
    AND r.full_name = '宝安大道西乡段'          -- ← 查的是西乡段！不是恒明珠！
LIMIT 500
```

- "两边"被翻译成**上行/下行**（up_or_down=1/2 的 SUM）；
- 路线条件凭空出现**宝安大道西乡段**（R1 的路线），而 R3 用户输入里根本没有"西乡段"；
- 实测数据库：西乡段 up_or_down 分布 = 上行 911 / 下行 0（**正好是这条 SQL 的结果**）；恒明珠 = 上行 732 / 下行 17（749）。

### 证据 2：R3 的 Vanna 路径实际没生成 SQL（agent.log 09:33:13）

```
vanna | no_sql_executed | question: 两边加起来？ | agent_output: 您的问题比较简略，
我不太清楚具体是指哪"两边"需要加起来……（Vanna 拒绝生成，返回 None）
```

### 证据 3：R3 时 RAG 检索的问题是原始句（agent.log 09:32:59）

```
ragflow | retrieve_success | question: 两边加起来？
```

**"两边加起来？"里没有"宝安中心--恒明珠"、没有"有多少处"——contextualize 什么都没继承到。**

### 证据 4：会话上下文在 R3 时点为空（user_memory）

- `session:9eda8eaa` 的 context 记录是 **09:34:14**（R4 之后）才写入的 `{route: 宝安大道西乡段, shape: count, damage_type: 泛油}`；
- R1/R2/R3（09:32~09:33）期间会话 context **从未被保存**。

### 证据 5：few-shot 示例污染（example_pairs 共 20 条）

- 带"宝安大道西乡段"的 SQL 示例占 **11 条**（id 13/14/15/16/20/23/29/30/31/…）；
- id 24 恰好是"病害大多是上行还是下行方向采集的？"→ **GROUP BY up_or_down + CASE WHEN '上行'/'下行' 的示例**；
- LLM 生成 R3 SQL 时，从示例里抄了"上行/下行"模式 + 出现频率最高的路线名"宝安大道西乡段"。

---

## 3. 根因链条（6 个环节，逐个钉死）

### 环节 ① 会话上下文保存缺失 → 多轮继承彻底失效（最底层根因）

`agent.py` 的 `handle_message` 正常回答分支（约 1096~1119 行）构造的 payload **没有 `context` 字段**：

```python
else:
    result_id = result["result_id"]
    save_result(result_id, result)
    payload = {
        "reply": ...,
        "sql": ...,
        "columns": ..., "rows": ..., "chart_data": ...,
        # ← 没有 "context": result.get("context")
    }
```

随后 `memory_service.save_session(session_id, payload.get("context"))`（约 1147 行）收到 `None`，`save_session` 里 `if not session_id or not context: return` **直接不保存**。

结果：**R1、R2、R3 全部没有把"西乡段 / 恒明珠"写进会话上下文**。R3 时 `load_session` 返回空 dict → `contextualize` 第 227 行 `if not context: return message, []` → 什么条件都没继承。

> 对照：retry 分支（约 1059~1079 行）和 calibrate 分支（约 588 行）的 payload 是带 `context` 的，所以**有些路径能存、有些路径不能存**，行为不一致。这就是为什么偶尔继承有效、偶尔完全失效。

### 环节 ② 误导性前缀「已沿用上一条问题的条件」（假继承）

`query_rewrite.normalize("两边加起来呢？")` 去掉了"呢"，得到 `"两边加起来？"`。

`agent.py` 约 1141~1145 行：

```python
payload["effective_message"] = effective_message if effective_message != message else None
if payload["effective_message"] and payload.get("reply"):
    detail = f"：{'、'.join(dict.fromkeys(inherited))}" if inherited else ""
    payload["reply"] = f"（已沿用上一条问题的条件{detail}）\n{payload['reply']}"
```

`effective_message`（"两边加起来？"）≠ 原始 `message`（"两边加起来呢？"）——**仅仅因为 normalize 去掉了"呢"**，系统就误判"消息被改写过"，给回答加了前缀。此时 `inherited=[]`，所以前缀没有明细，正好是用户看到的"（已沿用上一条问题的条件）"。

**危害**：这个前缀让用户（和开发者）误以为系统正确继承了上一条的条件，掩盖了环节 ① 的继承失效。**前缀文案"已沿用上一条问题的条件"是假象，实际什么都没沿用。**

### 环节 ③ 「两边加起来」无任何语义识别 → shape 退化成 detail、模板弃权

`nl2sql.py`：

- `is_comparison`（约 190 行）的 `_COMPARISON_HINTS = ("各有多少","分别有多少","各多少","分别多少","哪个多","哪个更","对比","差别","区别","差异")` —— **没有"加起来/合计/总共/两边/一共"**；
- `parsers.parse_query_shape`（parsers.py 约 366 行）没有 sum 类别，"两边加起来呢？"无任何 shape 词 → 返回默认 `"detail"`；
- `build_template_sql`：`shape == "detail" and not plan["required_conditions"]` → 模板弃权返回 None。

于是"两边加起来呢？"（无路线、无病害、无时间）变成一个**空条件 detail 查询**，完全交给 LLM 自由发挥。

### 环节 ④ few-shot 示例污染 → LLM 抄出"西乡段 + 上行/下行"

`agent.py` 约 604~608 行：`agent_loop_useful = not (comparison or multi_route or template_abstained)` —— 本问题三者皆否 → `agent_loop_useful=True` → 先跑 Vanna → **Vanna 拒绝生成（证据 2）返回 None** → 落入约 622 行内置 LLM 循环。

内置 LLM `generate_with_context` 收到的上下文里包含 `examples`（few-shot，来自 example_pairs 检索）：
- "上行/下行"模式 ← 示例 id 24；
- "宝安大道西乡段" ← 示例里出现频率最高的路线。

LLM 把两者拼起来：`SUM(CASE WHEN up_or_down=1/2) WHERE r.full_name='宝安大道西乡段'`。

### 环节 ⑤ validate_semantics 单向校验 → 放行"多出来的西乡段"

`nl2sql.py` 约 1435 行 `validate_semantics` **只检查"该有的条件缺没缺"，不检查"SQL 里多出来的条件是不是用户要的"**。

R3 的 plan：`required_conditions=[]`（因为 normalized 里没有任何路线/病害/时间词）→ 路线校验分支根本不触发 → 带 `r.full_name='宝安大道西乡段'` 的 SQL **零拦截通过**。

### 环节 ⑥ 结果摘要美化错误 → 把错误结果包装成"看起来合理"的解读

`_summarize_result` 的 LLM 摘要把 [上行 911, 下行 0, 总 911] 解读成"上行数量为 911，下行数量为 0，表明所有流量或记录均集中在上行方向"——让一个错误结果显得像是有依据的分析。

---

## 4. 结论：是模型能力问题还是代码问题？

**都不是单一问题，是工程缺陷。** 逐环节看：

| 环节 | 性质 | 责任方 |
|---|---|---|
| ① 会话上下文保存缺失 | 代码 bug（payload 漏字段） | 代码 |
| ② 误导性前缀 | 代码 bug（normalize 副作用） | 代码 |
| ③ 无 sum/comparison 语义 | 设计缺失 | 方案 |
| ④ few-shot 污染 | 数据/设计问题（示例集偏斜 + LLM 抄写） | 数据+方案 |
| ⑤ 校验单向 | 设计缺失（校验不验"多"） | 方案 |
| ⑥ 摘要美化 | 设计缺失（错误结果不该被解读成分析） | 方案 |

**LLM 只是忠实执行了被错误引导的任务**。模型能力不是瓶颈——把正确的语义 JSON 给它，它就能做对。

---

## 5. 同类 bug 清单（由本次链路外推，均已在代码/现场中确认风险）

1. **任何"跨两轮汇总/对比"问题**都会踩同一坑：两边加起来、总共多少、哪个多、多多少、差多少——目前全部没有识别，全部会退化成 detail/单主体查询。
2. **normalize 改动原文触发假前缀**：任何被 normalize 改写（去"呢/吗/的"、替换标点）的句子都可能显示"已沿用上一条问题的条件"，无论是否真继承。
3. **会话上下文全空**：由于 payload 缺 context 字段，**所有正常回答都不保存上下文**——目前的多轮继承只在 retry/calibrate 等少数路径碰巧生效，整体继承能力形同虚设。
4. **few-shot 路线名偏斜**：example_pairs 里西乡段占 55%，LLM 生成 SQL 时会把示例里的路线名抄进新查询（本次已实测发生）。
5. **validate_semantics 不验"多出来的条件"**：SQL 里凭空出现的路线/病害/时间条件不会被拦截，会把用户没问的范围悄悄加进结果。
6. **R2 正确掩盖了继承 bug**：R2 之所以对，是因为问题里自带"恒明珠"（`_known_route_in_message` 命中，不依赖继承）。一旦问题不含主体词（"两边""加起来""那条路"），继承失效立刻暴露。

---

## 6. 修复指引（面向 Codex，按优先级）

### P0（止血，立即改）

1. **修复 payload 漏 context 字段**：`agent.py` 正常回答分支（约 1096~1119 行）补上 `"context": result.get("context")`，与 retry 分支保持一致。这是让多轮继承恢复工作的前提。
2. **修复假前缀**：`payload["effective_message"]` 的判定改为 `effective_message != normalized`（与 normalize 后的值比较），而不是与原始 `message` 比较；同时**仅当 `inherited` 非空时才加"已沿用上一条问题的条件"前缀**，空继承一律不加。
3. **"加起来"类识别**：`_COMPARISON_HINTS` 增加 `("加起来", "合计", "总共", "一共", "两边", "总和", "汇总")`；`parse_query_shape` 增加 `sum` 类别（"加起来/合计/总共/总和"）。命中后**不再走内置 LLM 自由生成**，进入对比/汇总专用处理。

### P1（系统级，解决根本）

4. **会话上下文模型升级（对应语义 JSON 中间层）**：
   - 上下文不再只存"上一条的 route/shape"，改为存**对话状态**：`{subject_pool: [{route: 西乡段, value: 911}, {route: 恒明珠, value: 749}], last_plan: {...}}`；
   - `context_from_plan` 增加 `subject_pool` 维护：每轮成功查询后，把"问题里的主体 + 结果值"追加进池子；
   - "两边加起来呢？"→ 语义识别为 `shape=sum, scope=[subject_pool 全部主体]` → **优先内存计算**（直接用池子里 R1=911、R2=749 相加，不查库）或生成 `routes_id IN (两个 id)` 的确定性 SQL。
5. **validate_semantics 增加"多条件"校验**：plan 里没有 route/damage/time 条件时，**禁止 SQL 的 WHERE 里出现 `basic_facilities_routes` 相关的路线过滤**（或要求校验函数显式确认"SQL 的过滤条件集合 ⊆ 用户声明的条件集合"）。
6. **few-shot 示例治理**：
   - 示例入库时做**条件归一化**（路线名/ID 用占位符如 `{route_id}`，防止路线名被抄）；
   - 检索示例时按**结构相似度**（shape/主体数）匹配，而不是按文本相似度；
   - 或者干脆：模板能覆盖的场景（count/distribution/list）**一律不进 LLM**，LLM 只处理模板弃权的少数场景，并强制带上 `required_conditions` 校验。

### P2（体验）

7. **结果摘要与结果解耦**：`_summarize_result` 只能基于真实 rows 做确定性渲染；LLM 自由摘要仅用于"多行解释"，单值/分组结果用模板渲染，避免把错误结果包装成分析。
8. **"已沿用上一条问题的条件"前缀改为真实清单**：只展示真继承到的条件（inherited 列表），空继承不展示，避免误导。

---

## 7. 回归验收（LD1 完整场景，连真实库）

| 轮次 | 提问 | 期望回答 | 验收方式 |
|---|---|---|---|
| R1 | 宝安大道西乡段一共多少处病害？ | 共 911 处 | 直接提问 |
| R2 | 那恒明珠段呢？ | 共 749 处（前缀：已沿用上一条问题的条件：有多少处） | 直接提问 |
| R3 | 两边加起来呢？ | 共 1660 处（911+749） | 直接提问 |
| R4 | 哪边泛油多？各多少？ | 西乡段 534 处 > 恒明珠 402 处 | 直接提问 |
| R5 | 多多少？ | 多 132 处（534−402） | 直接提问 |

补充验收（同类场景）：
- "总共多少" / "合计多少" → 走 sum 语义；
- "那两条路加起来呢" → 继承 subject_pool 两个主体；
- 新会话（clear_session 后）直接问"两边加起来呢？" → 必须**追问用户"哪两边"**，而不是乱查库；
- 所有轮次回答数字需与 `data/probe_v5_*.py` 探针数字一致（911/749/534/402/132）。

---

## 8. 本次分析留档

- 探针脚本：`data/probe_ld1_bug.py`（查 query_history）、`data/probe_ld1_scene.py`（查会话现场）、`data/probe_ld1_updown.py`（上下行分布，见文内数据）；
- 证据来源：`backend/data/agent.db`（query_history / conversation_messages / example_pairs / user_memory）、`backend/data/agent.log`（vanna/ragflow/agent 事件）；
- 相关代码位置：`agent.py`（payload 构造 ~1096-1119 / 前缀 ~1141-1145 / Vanna 选择 ~604-608 / retry ~1046-1051）、`memory.py`（contextualize 223-269 / save_session 63-69）、`nl2sql.py`（is_comparison 190 / build_query_plan 195 / validate_semantics 1435 / build_template_sql）、`parsers.py`（parse_query_shape 366）。
