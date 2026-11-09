# 提示词总览

所有给大模型的提示词都在这个目录里，**改这里就直接生效**（代码从文件读，不再散落在 service 的 f-string 里）。
改完不用重启后端：加载器按文件缓存，`python -c "from app.services import prompts; prompts.clear_cache()"` 即可热更新
（或者重启后端，最省事）。

## 文件格式

```
<!-- 说明注释，加载时会剥掉，不会发给模型 -->
[system]
系统提示词正文

[user]
用户提示词正文，用 <<占位符>> 填空
```

占位符故意写成 `<<name>>` 而不是 `{name}`：提示词里有大量 JSON / SQL 花括号，用 `str.format` 会直接报错。
如果占位符忘了传值，代码会往 `logs/app/app.log` 写一条 `prompts/unfilled` 错误，不会静默把 `<<xxx>>` 发给模型。

## 清单

| 文件 | 用途 | 调用点 | 用哪个模型 | 占位符 |
|---|---|---|---|---|
| `intent.md` | 意图分类（knowledge/data/analysis/image/composite/metadata） | `intent.classify_intent` | 默认模型 | `<<message>>` |
| `metadata.md` | 宏观元数据查询解析（表/字段/库结构，只出 JSON，不写 SQL） | `metadata_service.parse_metadata_request` | `role_model("semantic")` | `<<databases>> <<tables>> <<message>>` |
| `datasource.md` | 跨库判定（mysql/postgresql/seaweedfs） | `cross_db._llm_classify_dbs` | 默认模型 | `<<candidates>> <<example_datasource>> <<example_datasource_first>> <<message>>` |
| `planning.md` | 任务规划（先想清楚几步再动手，分析类必须带 analyze 步） | `orchestrator._llm_tasks` | `role_model("planner")` | `<<question>> <<known_damage_types>>` |
| `repair_plan.md` | 执行前规划契约校准（伪枚举、混合视图、依赖和字段组合） | `orchestrator._repair_plan` | `role_model("planner")` | `<<question>> <<planning_question>> <<plan>> <<issues>> <<known_damage_types>>` |
| `react_controller.md` | ReAct 异常分支调度（重试、补证据、跳过或止损） | `react_agent._deviation_decision` | `role_model("controller")` | `<<goal>> <<current_step>> <<frozen_query>> <<observation_error>> <<failure>> <<allowed_tools>>` |
| `dialogue_understanding.md` | 对话理解（关系/多对象/补全成完整问题） | `memory._llm_complete_followup` | `role_model("semantic")` | `<<history>> <<message>>` |
| `followup_suggestion.md` | 回答末尾的后续建议 | `agent._followup_step` | `role_model("knowledge")` | `<<question>> <<reply>>` |
| `knowledge.md` | 知识问答（只依据 RAGFlow 资料） | `agent._answer_knowledge` | `role_model("knowledge")` = 知识问答模型 | `<<context>> <<message>>` |
| `result_summary.md` | 查询结果中文摘要（模板兜不住时） | `agent._summarize_result` | 默认模型 | `<<message>> <<columns>> <<preview>>` |
| `templatize_sql.md` | 成功 SQL 自动模板化（离线挖槽兜底，只识别值不改结构） | `templatizer._llm_skeleton` | `role_model("semantic")` | `<<kind>> <<schema>> <<question>> <<sql>>` |
| `example_direct_match.md` | L2 示例直达：严格同问判定（7 要素，不写 SQL） | `direct_match._score` | `role_model("semantic")` | `<<question>> <<candidates>>` |
| `promotion_exam_generate.md` | 每轮晋级考试生成 15 道全新题（不给 SQL 映射） | `evals.promotion.case_factory` | `role_model("planner")` | `<<business_document>> <<exclude_ids>> <<exclude_questions>> <<case_count>>` |

## 每条提示词的"已知坑"（改的时候别踩）

### 结构（2026-09-16 按 docs/提示词逐文件评审报告.md 重构）

- 所有提示词统一成：**system 放不可违反的硬规则，user 放动态上下文**（schema / 示例 / 槽位 / 问题）。
  以前关键规则堆在 user 中间，模型遵守度明显更差。
- 规则统一中文、编号连续、去掉重复项。
- 信息不足时输出 `AMBIGUOUS: <缺什么>`；`sql_engine.raise_if_ambiguous()` 会把它转成
  「请补充信息」的追问，而不是硬猜一个条件继续查。

**knowledge.md**
- "只依据给定资料、不编造、标注来源"这几条直接决定幻觉率，删改要慎重。
- 已删掉"联网搜索必须标注"那条：这个链路没有联网检索，留着反而可能诱导模型编造搜索结果。

**intent.md / datasource.md / planning.md / dialogue_understanding.md**
- 都只在规则判不出来时才会被调用（规则优先，省一次 10~40 秒的 LLM 往返），所以提示词里不用重复规则判断。
- `datasource` 现在带**数据源映射表**（以前没给 schema 却要求模型写"命中的表名"，只能编造），示例用真实
  数据源名而不是 `<<占位符>>`。
- `planning` 的 action（query_data/query_knowledge/analyze）由代码映射成 data/knowledge/analysis；
  analysis 步骤自动依赖前面所有步骤（规划器不返回依赖关系）。
- `dialogue_understanding` 输出 JSON（relation_type / multi_object / objects / full_question）。
  `new_topic` 会**清掉**上轮继承；`multi_object=true` 时上一轮比过的对象会被带全（两路对比不再丢一条）。
  补全后的文本还有一道代码护栏：凭空出现的年份会被摘掉（`query_rewrite.repair_fabricated_time`）。

## 相关脚本 / 测试集

- 看当前真实发给模型的文本（含替换后的值）：临时给 `chat` 打桩即可，参考 `backend/run_eval.py` 的用法。
- 回归：`cd backend && .\.venv\Scripts\python.exe run_eval.py`（90 题）
- 高难度专项：`cd backend && .\.venv\Scripts\python.exe run_hard_eval.py`
  —— 15 题（题干与真实答案来自 `docs/NL2SQL高难度问题答案集-含真实答案.md`），专门盯
  分别统计 / Top N / 列表查询 / 置信度 / 有无图片 / 大类翻译 / 跨表聚合。
