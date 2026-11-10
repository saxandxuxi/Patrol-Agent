# 同句第二遍没推已验证示例、反被 generic 挤占 + generic 理由看不懂（给 codex）

## 一、现象
1. 同一个问题问两遍：第一遍走 Wren、结果对、存进了 RAGFlow；第二遍按理说该直接推荐这条已验证问法，结果卡片第一条还是"通用算子"，已验证示例根本没出现。
2. 通用算子的推荐理由看不懂，是"字段目录解析（编码值翻中文）、不经过大模型"这类内部话术，不是"这个问题为什么这么查"的人话。

## 二、根因（已读代码定位）
排序本身没错：`collect_capabilities`（capabilities.py:542-555）分层 tier，preset/auto=0、example/example_direct=1、generic/code_template=2，同类上限 generic≤2。也就是只要已验证示例进了候选，它一定排在 generic 前面。

问题出在**示例根本进不了候选**：
- 第一遍走 Wren 存进 RAGFlow 的是 **MDL-SQL**（含 wren_src_）；
- 第二遍 `_example_candidates`（capabilities.py:137-140）用 `direct_match.gate(only_exact=True)` 已经零成本召回了这条同句已验证问法；
- 但 147 行调 `_executable_sql(hit.sql)` → `nl2sql.physical_sql_of`，要把 MDL-SQL 当场编译成物理 SQL；
- 当前环境 Wren 项目名对不上 / 不可用，编译失败返回 None，于是 148-155 行 `continue` 把这条命中的示例**整条丢弃**；
- 没有 preset/auto 模板时，候选里就只剩 generic（tier=2），它自然成了 recommended 第一条。

代码里其实已经踩过一次这个坑（capabilities.py:204-208 注释："西乡段里程桩点那条示例第二遍问就不见了，只剩错的通用算子"），那次只修了"Wren 物理 SQL 也带 wren_src_ 被误判"，没解决"Wren 真编译不出来"的情形。

generic 的理由看不懂：`_generic_candidate_card`（capabilities.py:500-501）和 generic_detail（421 行）的 reasons 是写死的通用话术，不看具体 SQL 内容。

## 三、修复要求

### P0-1 自动补物理 SQL（主解，承接上一份方案）
- 启动时 / `import_ragflow_examples`（立即同步）后，自动把 MDL 示例编译出物理 SQL 写进 `# 物理SQL` 段；Wren 不可用就跳过、记日志、下次再试，不报错不删原始 MDL。
- 补好后 `_executable_sql` 直接命中物理版，已验证示例正常进候选（tier=1），第二遍就会排在 generic 前面。这是治本。

### P0-2 同句精确命中的示例，物理 SQL 暂缺时不要直接丢弃
- `only_exact=True` 命中、且是 trusted 同句（score=1.0）的已验证问法，即使 `physical_sql_of` 暂时编译不出，也**保留为候选**：
  - scope 注明"这条第一遍是走 Wren 跑通的，本次将走 Wren 重放"；
  - 排序仍为 example_direct（tier=1），压过 generic；
  - 执行时回落到 Wren 链路（反正第一遍就是 Wren 出的），不要因为"现在编译不出物理 SQL"就把用户自己确认过的答案藏起来。
- 只有"相似度召回（非精确同句）"的示例，才保留"编译不出就不摆"的保守策略，避免摆一个点了跑不通的近似题。

### P0-3 generic 不抢"默认推荐"
- 当候选里没有 preset / auto / 任何 example_direct，只剩下 generic 时，`recommended`（index==0）不要直接给 generic：默认推荐项改为"让 AI 自己查 / 走 Wren"，generic 作为兜底项排在后面，或明确标"兜底模板，口径需核对"。
- 目的：generic 是最便宜的兜底，不该因为"别的都没命中"就被当成权威答案顶在第一位（尤其它现在 count 路径还会丢过滤条件，见上一份方案）。
- 保留同类≤2 的限制（已有 _KIND_CAP）。

### P0-4 generic 的理由改成人话
- 不要再写死"字段目录解析（编码翻中文）、不经过大模型"。
- 从它实际生成的 sql_preview / scope 反推一句用户看得懂的：会查哪张表、按哪些列过滤、做什么聚合，例如"按病害表统计：时间=2026年7月、路线=全库、数行数"。
- 这样用户一眼能判断这张卡片答的是不是自己问的那件事。

### P0-5 同句直达的零成本性保持
- 继续保持 `direct_match.gate(only_exact=True)` 本地精确匹配、不调模型（capabilities.py:137）；不要为了排序再额外加一次 LLM 调用把同句示例压下去。

## 四、验收
1. 同一句 trusted 问题（第一遍走 Wren 存过）第二遍问：候选第一条是"相似已验证问法 / 按这条已确认过的问法口径"（example_direct），generic 排其后或不出现；
2. Wren 不可用、物理 SQL 暂缺时，同句精确命中的示例仍出现并标注"走 Wren 重放"，而不是凭空消失只剩 generic；
3. 没有任何已验证来源时，generic 不再独占 recommended 第一位，而是"走 Wren / 让 AI 查"优先；
4. generic 卡片理由是一句人话（查哪表、按什么过滤、做什么统计），不是内部话术；
5. 同一句问三次，候选顺序稳定（已有 rerank 缓存，不退化）。

## 五、改动文件（参考）
- `backend/app/services/capabilities.py`：`_example_candidates` 物理 SQL 暂缺时保留同句精确命中（144-155）；generic 不抢 recommended、generic 理由人话化（488-504、419-421）；`collect_capabilities` 默认推荐位逻辑（538-571）。
- `backend/app/services/nl2sql.py`：`physical_sql_of` 对 MDL 的自动补物理 SQL（与上一份方案合并）。
- 不动 `direct_match` 的零成本同句匹配。
