# 图片查询把"请确认口径"误报成"暂不可用"（给 codex）

## 一、现象
问"宝安大道西乡段的病害图片"，回：
> 图片查询暂不可用：请先确认查询口径

但用户本意只是要看病害图片，功能本身可用（前面已验证：查 problem_url → SeaweedFS 取图 → 返回 images，链路完整）。

## 二、根因
- "请先确认查询口径"来自 `capabilities.ChooseCapability`（capabilities.py:36，RuntimeError），这是**正常信号**——意思是"这题还差条件，弹确认卡让用户选口径"。
- 图片查询函数末尾的兜底是通用 `except Exception`（agent.py:2392-2393）：
  ```python
  except Exception as exc:
      return {"reply": f"图片查询暂不可用：{exc}", "source": "database", "images": []}
  ```
  它把 ChooseCapability 也当成"出错"接住，于是把"请确认口径"误报成"图片查询暂不可用"。

## 三、修复
在图片查询函数的 except 里，先单独接住 `ChooseCapability`，把它的 plan/candidates/steps 按确认卡正常返回（和对话主链路一致），不要走"暂不可用"兜底：
```python
except ChooseCapability as cc:
    return {"reply": str(cc), "source": "choose_capability",
            "plan": cc.plan, "candidates": cc.candidates, "steps": cc.steps, "images": []}
except Exception as exc:
    return {"reply": f"图片查询暂不可用：{exc}", ...}
```
只有真·SeaweedFS 取图失败、SQL 报错、网络问题才走"暂不可用"。

## 四、验收
1. 问"西乡段的病害图片"——若需要确认口径，应正常弹确认卡，不再说"暂不可用"；
2. 口径齐全时正常返回图片（images 非空）；
3. 真·SeaweedFS 404 / SQL 报错时，仍如实报"暂不可用"并说明原因。

---

## 五、实施记录（codex，2026-09-23）

### 5.1 主修：确认卡不再被"暂不可用"兜底吞掉

`_image_result` 在通用兜底之前单独接住 `capabilities.ChooseCapability`，按确认卡返回。
组装逻辑**抽成一份** `_choose_capability_payload(need)`（`agent.py`），对话主链路
`handle_message` 和图片查询共用——避免两处各写一套、哪天口径跑偏。

`source` 保持 `"confirm"`（**没有**按方案示例用 `"choose_capability"`）：前端是按
`payload.await_confirm` + `capability_choices` / `capability_steps` / `choose_token` 渲染确认卡的
（`ChatPanel.vue:954`），沿用主链路的 payload 才叫"和对话主链路一致"。

顺带修掉一个潜在崩溃：候选为空时 `recommended_id` 原来会 `need.candidates[0]` 抛 IndexError，
现在回空串。

### 5.2 顺手修掉"口径齐全也不出图"的真因（验收 2 原来过不了）

把列名先摘掉之后又发现：`_image_result` 认图片列**只认英文列名**
（`problem_url` / `preview_url` / `distance_capture_url` / `picture`），而语义层写 SQL 时爱给列
起中文别名（`p.problem_url AS 病害图片`）→ 一行都认不出来，回"没有找到相关病害图片"。
西乡段有 **911 条**带 `problem_url` 的病害，这个回复是错的。

新增 `_image_values_of(row)`：先认英文列名（模板/通用算子产出的就是它们），认不到再按
**值本身像不像图片路径**（后缀 `.jpg/.jpeg/.png/.webp/...`）兜底。

### 5.3 实测

```
西乡段的病害图片          → source=confirm await_confirm=True 候选=[pending_disease_list, generic_detail:ai_problem, …]
宝安大道西乡段的病害图片   → 同上（不再出现"图片查询暂不可用"）
西乡段泛油的照片          → 同上
真故障（假造 SeaweedFS 崩） → "图片查询暂不可用：SeaweedFS 连不上"（如实报错）
口径齐全（reuse_mode=off） → 把西乡段有病害的图片列出来 → images=3 ✓
```

回归集加了 1 条常驻用例 `image-query-no-false-unavailable`（断言"回复里不许出现'暂不可用'
+ 中文别名的图片列要认得出来"），当前 **PASS 19 / FAIL 0**。

### 5.4 根治：看图不再由 LLM 挑图片列

§5.3 之后还剩一个"偶发"：语义层偶尔把图片列写成 `preview_url`（该列实测大多是 `'NULL'`），
仍会回"没有找到相关病害图片"。查下来根因很直接——

**唯一会取 `problem_url` 的构造器住在死代码链 `_build_template_sql_legacy` 里**（`nl2sql.py`），
`resources/sql_templates/_index.yaml` 里 MySQL 侧**没有看图条目** → 看图题走 Wren，图片列由 LLM
自己挑 → 挑成 `preview_url` / 起中文别名全凭运气。

两处根治：

1. **`_build_photo_template` + 注册表 `photo_list`**：把死链里那份口径搬成活的构造器
   （`shape=photo` + `subject=disease`），SQL 固定 `p.problem_url` 并过滤
   `IS NOT NULL / <> '' / <> 'NULL'`。`builtin_registry_issues()` 仍是 0 条。
2. **图片查询走固定口径**：`_image_result` 先调 `nl2sql.build_photo_sql(message)`
   （建计划 → 固定口径 SQL → 直接执行，**不经过 LLM**），口径不适用（问的是资产/定距图片、
   主体不是病害）才回落到原链路——原链路上那条 `ChooseCapability` 兜底仍在（§5.1）。

实测（同一个"病害图片"问法反复问，结果不再抖）：

```
西乡段的病害图片 / 宝安大道西乡段的病害图片 / 把西乡段有病害的图片列出来
看看西乡段的病害照片 / 西乡段的泛油病害图片给我看看
   → 全部 images=3（另有 3 条记录的路径在 SeaweedFS 上确实 404，回复如实说明）
隧道台账里的图片 / 定距采集的图片 / 西乡段的资产图片
   → 固定口径不适用 → 回落原链路（不瞎套病害图片口径）
```

回归用例 `image-query-no-false-unavailable` 增加了"不许回'没有找到相关病害图片'"这一条断言，
全量 **PASS 19 / FAIL 0**。
