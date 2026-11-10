# 弹卡链路系统性 Bug 整改方案

> 覆盖"确认卡/候选卡"全链路的 bug。核心问题：图片题点「让 AI 自己查」后重复弹卡；候选质量差；弹卡慢；引擎标签错。

---

## 一、Bug 清单与根因

### Bug 1（P0）：图片题点「让 AI 自己查」后重复弹卡

**复现**：问"病害图标可以调出来看看长什么样吗"→ 弹卡 → 点「让 AI 自己查」→ 又弹一张一模一样的卡。

**根因**：`agent.py` 图片题路径（3670-3676 行）调用 `_image_result(effective_message, force_ai=force_ai)`，**只传了 force_ai，没有把 capability_id in {"ai","wren"} 转成 force_ai**。

对比普通 data 题路径（3893-3895 行）：
```python
skip_capabilities=force_ai or capability_id in {"ai", "wren"} or wren_replay_failed,
wren_first=force_ai or capability_id in {"ai", "wren"} or wren_replay_failed,
```
普通题有 `or capability_id in {"ai","wren"}`，图片题漏了。

**结果**：用户点「让 AI 自己查」→ 前端发 capability_id='ai' → 后端图片题路径 force_ai 仍为 False → _image_result 里 skip_capabilities=False → _build_query_result 收集候选 → 又弹卡。

**修复**：3671 行和 3676 行改成：
```python
payload = _image_result(effective_message, force_ai=force_ai or capability_id in {"ai", "wren"})
```

---

### Bug 2（P0）：图片题固定口径不适用时，走 _build_query_result 又弹卡，但弹的卡和图片无关

**复现**：问"病害图标可以调出来看看长什么样吗"——"图标"可能在 ai_problem_base（字典表）的某列，不是 ai_problem.problem_url。_photo_result 固定口径不适用（返回 None），走 _build_query_result，但收集的候选可能是"病害明细"等和图片无关的口径。

**根因**：_image_result 里 _photo_result 返回 None 后，直接调 _build_query_result(want_images=True)，但 capabilities.collect_capabilities 收集候选时没有过滤"只收能取到图片列的候选"。

**修复**：_build_query_result 里 want_images=True 时，collect_capabilities 的候选过滤条件加一条"候选 SQL 的 SELECT 列里必须包含 role=image 的列"，不含图片列的候选不摆出来。

---

### Bug 3（P1）：弹卡后结果工作台显示引擎标签错误

**复现**：用户点了模板/示例口径查询，结果工作台显示"执行引擎：Wren 语义层"，而不是"预置模板/示例直达"。

**根因**：结果工作台的引擎标签取自 result 的 engine 字段，但某些路径（如 chosen_example 走 wren 重放后）engine 字段被覆盖成了 wren。

**修复**：执行 chosen_* 口径时，把原始引擎类型（chosen_preset/chosen_auto/chosen_example/chosen_generic 等）写进 result.engine，不被后续 wren 重放覆盖。前端按 engine 字段显示标签。

---

### Bug 4（P1）：弹卡速度慢（输入到弹卡约 9 秒）

**根因**：弹卡前的链路：上下文补全（1次LLM）→ 意图分类（1次LLM）→ 语义规划（1次LLM）→ collect_capabilities（可能调 LLM 重排）。4 次 LLM 调用串行，每次 2-3 秒。

**优化方向**（按性价比排序）：
1. collect_capabilities 里的 LLM 重排加缓存（同问题+同候选集合只排一次）；
2. 语义规划结果复用（probe_plan 已经在总闸阶段算过，collect_capabilities 不要再算一次）；
3. 候选收集的 RAGFlow 检索和模板匹配并行（现在可能是串行）；
4. 加阶段耗时打点（log_event 记每步毫秒数），用真实数据定位最慢的一步再优化，不瞎猜。

---

### Bug 5（P1）：候选卡上的"查看 SQL"对自动模板/示例不友好

**现象**：自动模板的 SQL 是参数化的（含占位符），用户点"查看 SQL"看到的是带 `{route_id}` 的模板 SQL，不是可执行 SQL，用户看不懂。

**修复**：候选卡的"查看 SQL"显示**回填后的可执行 SQL**（用本轮 plan 的参数值替换占位符），而不是参数化模板。如果参数缺失（待补槽），显示"参数补齐后可执行"并列出缺哪些参数。

---

### Bug 6（P2）：复合题整题卡和步骤卡的切换逻辑容易让用户困惑

**现象**：复合题先弹整题卡（推荐一个模板能一次答对），用户点「按步骤逐条查」后又弹步骤卡，步骤卡上每步又有候选，用户要点好几次。

**修复**：
1. 整题卡上明确标注"这个模板能一次答对，覆盖全部 X 步"；
2. 「按步骤逐条查」按钮旁加说明"每步单独选口径，适合模板覆盖不全时"；
3. 步骤卡上如果某步没有候选（只能走 Wren），直接标注"这步走 AI 查询"，不摆空候选列表。

---

## 二、统一修复原则

1. **force_ai 的转换必须全链路一致**：任何调用 _build_query_result / _image_result / _handle_composite 的地方，都要用 `force_ai or capability_id in {"ai", "wren"}`，不能只传 force_ai。全局搜 `force_ai=` 检查每一处。
2. **弹卡必须基于完整语义规划**：不在规划前用规则猜候选（用户明确要求）。
3. **候选必须和问题相关**：want_images 时只收含图片列的候选；候选 SQL 必须能执行（不摆注定失败的口径）。
4. **用户选了「让 AI 自己查」就绝不弹第二次卡**：这是硬约束，全链路检查。

---

## 三、验收

1. 问"病害图标可以调出来看看长什么样吗"→ 弹卡 → 点「让 AI 自己查」→ 直接出结果，不弹第二次卡；
2. 问"看一下病害样图"→ 弹卡 → 点「让 AI 自己查」→ 直接出结果，不弹第二次卡；
3. 普通 data 题点「让 AI 自己查」→ 不弹卡（回归）；
4. 点模板口径查询后，结果工作台引擎标签显示"预置模板/示例直达"，不是"Wren 语义层"；
5. 候选卡"查看 SQL"显示回填后的可执行 SQL；
6. 弹卡耗时从 9 秒降到 ≤5 秒（加打点后用真实数据验证）。

---

## 四、实施顺序
1. **第一步**：Bug 1（图片题 force_ai 转换）——一行修复，立即验证；
2. **第二步**：Bug 2（图片候选过滤）+ Bug 3（引擎标签）；
3. **第三步**：Bug 4（弹卡耗时打点+优化）；
4. **第四步**：Bug 5（查看 SQL 回填）+ Bug 6（复合题卡说明）。
