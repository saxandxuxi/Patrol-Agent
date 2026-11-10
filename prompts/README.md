# 提示词总览

`prompts/` 目录里的 Markdown 是运行时提示词的唯一来源。代码通过
`backend/app/services/prompts.py` 读取文件，修改后不需要改 service 里的字符串。

当前共有 26 个提示词文件，另有本 README。文件头必须声明用途、调用点、模型和全部
`<<占位符>>`；`backend/tests/query/test_prompt_contracts.py` 会校验声明与正文一致，
并要求本文件列出每一个提示词文件。

## 文件格式

```text
<!--
用途：...
调用：...
模型：...
占位符：<<name>> ...
-->
[system]
系统提示词

[user]
用户提示词，占位符写成 <<name>>
```

占位符使用 `<<name>>`，不要改成 `{name}`。提示词里包含大量 JSON 和 SQL 花括号，
`str.format` 会把它们误当成格式字段。调用方漏传占位符时，加载器会把错误写入
`backend/logs/app/app.log`。

修改完成后，从仓库根目录执行以下命令清缓存，或者重启后端：

```powershell
cd backend
.\.venv\Scripts\python.exe -c "from app.services import prompts; prompts.clear_cache()"
```

## 当前提示词

| 文件 | 用途 | 调用点 | 模型 | 占位符 |
|---|---|---|---|---|
| `analysis.md` | 综合分析数据结果和知识库规范 | `agent._analysis_payload()` | knowledge | `question`, `data_result`, `knowledge_result`, `previous_table` |
| `bucket_basis.md` | 判断分段依据是桩号还是时间 | `nl2sql._bucket_basis_dimension()` | semantic | `question` |
| `datasource.md` | 判断问题涉及的数据源 | `cross_db._llm_classify_dbs()` | 默认模型 | `candidates`, `message` |
| `dialogue_understanding.md` | 多轮对话补全和关系理解 | `memory._llm_complete_followup()` | semantic | `history`, `message` |
| `example_direct_match.md` | 判断能否复用已验证示例 SQL | `direct_match._score()` | semantic | `question`, `candidates` |
| `fix_sql.md` | 根据校验失败原因修正 MDL SQL | `nl2sql.fix_mdl_sql()` | semantic → verify → sql | `question`, `issue_type`, `reason`, `mdl_sql`, `rows`, `required`, `models`, `errors` |
| `followup_suggestion.md` | 生成相关后续问题建议 | `agent._followup_suggestions_sync()` | knowledge | `question`, `reply` |
| `intent.md` | 意图分类 | `intent.classify_intent()` | semantic | `message`, `rule_hint`, `candidates` |
| `knowledge.md` | 仅依据 RAGFlow 片段回答知识题 | `agent._answer_knowledge()` | knowledge | `context`, `message` |
| `metadata.md` | 解析数据库元数据问题 | `metadata_service.parse_metadata_request()` | semantic | `databases`, `tables`, `message` |
| `planning.md` | 规划完整回答所需的执行步骤 | `orchestrator._llm_tasks()` | planner | `question`, `known_damage_types` |
| `promotion_exam_generate.md` | 生成晋级考试题 | `evals.promotion.case_factory` | planner | `business_document`, `exclude_ids`, `exclude_questions`, `case_count`, `capability_gaps`, `unavailable_fields`, `batch_focus`, `retry_evidence` |
| `query_holdout_judge.md` | held-out 语义验收裁判 | `evals.run_holdout_eval` | verify | `business_document`, `case_json`, `execution_json` |
| `query_spec.md` | 把单轮问题转换为 QuerySpec | `query.analysis.analyze_single_turn()` | semantic | `question`, `kind`, `current_date`, `capabilities`, `schema`, `reach`, `assist_evidence`, `planning_feedback` |
| `query_spec_audit.md` | 独立审计 QuerySpec 是否完整可执行 | `query.audit.audit_query_plan()` | verify | `question`, `current_date`, `plan_json`, `answer_operations`, `assist_evidence` |
| `query_spec_repair.md` | 按结构化票据局部修复 QuerySpec | `query.planning_repair.request_repair()` | semantic | `question`, `draft`, `issue`, `capabilities`, `assist_evidence` |
| `react_controller.md` | 异常发生时选择重试、补证据或止损 | `react_agent._deviation_decision()` | controller | `goal`, `current_step`, `frozen_query`, `observation_error`, `failure`, `allowed_tools`, `previous_attempts`, `helper_observations`, `coverage_and_values` |
| `repair_plan.md` | 规划契约失败后的有界重规划 | `orchestrator._repair_plan()` | planner | `question`, `planning_question`, `plan`, `issues`, `known_damage_types` |
| `repair_presentation.md` | 修正最终表达和单位，不改查询 | `agent._repair_result_presentation()` | semantic → verify | `question`, `reply`, `sql`, `columns`, `rows`, `reason` |
| `result_summary.md` | 模板摘要失败时生成中文结果摘要 | `agent._llm_result_summary()` | semantic | `message`, `columns`, `preview`, `examples` |
| `subject_router.md` | 从能力注册表中选择查询主体 | `capability_registry.resolve_subject()` | semantic | `question`, `subjects`, `grounding_evidence` |
| `table_analysis.md` | 仅基于紧凑证据分析已有数值 | `calculations.domain.analysis.analyze_evidence()` | semantic | `question`, `evidence`, `requirements` |
| `template_review.md` | 审核自动沉淀模板能否安全复用 | `templatizer.review_template()` | sql | `question`, `sql`, `conditions` |
| `templatize_classify.md` | 判断剩余字面值能否参数化 | `templatizer.mine_sql()` | semantic | `question`, `sql`, `suspicious`, `table_fields`, `plan`, `hints` |
| `templatize_sql.md` | 把成功 SQL 挖成参数化模板 | `templatizer._llm_template_fallback()` | semantic | `kind`, `schema`, `question`, `sql` |
| `verify_answer.md` | 判断 SQL 结果和最终回复是否答所问 | `nl2sql.verify_answer()` | verify → semantic → sql | `question`, `sql`, `columns`, `rows`, `final_reply`, `question_optimization` |

## 维护原则

- `system` 放不可违反的契约，`user` 放动态上下文、字段、示例和问题。
- 先保留完整用户问题，再由 LLM 判断实体和查询口径；不要用关键词或正则拆业务语义。
- `assist_evidence` 和代码规则候选只能作为线索，必须有用户原话或真实字段事实支撑。
- 不存在的表、字段、指标和枚举继续硬拒绝；多租户、成本和预算等业务风险应反馈或标注，
  不应在提示词层无条件阻断。
- 规则只保留一个权威来源。审计提示词检查一致性，不重复抄写 QuerySpec 的业务规则。
- 不要在示例里写可被模型复制成真实过滤条件的固定日期、周号、路线或编号。
- 修改提示词后至少运行：

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest tests.query.test_prompt_contracts -v
.\.venv\Scripts\python.exe -m unittest tests.query.test_query_audit tests.query.test_query_planning tests.query.test_answer_verifier_prompt -v
```

更大范围的质量验证使用：

```powershell
.\.venv\Scripts\python.exe -m evals.run_single_turn_eval
.\.venv\Scripts\python.exe -m evals.run_holdout_eval
```
