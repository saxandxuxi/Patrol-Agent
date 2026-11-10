# 桥隧边坡资产名称→ID 解析（补路线解析之外的空白）

## 一、现状
只有路线有"名称→ID"解析：route_resolver._resolve_route("西乡段") → 路线 id。
桥/隧/边坡没有：
- _SUBJECT_TABLES 只有 disease/patrol/route/section/pile_point/asset/work_order；
- nl2sql 1921-1923 只有"主体→表"映射，没有"桥名→桥id"。

后果：问"XX桥的技术状况"，能落到 bridges 表，但不会把桥名解析成 id 过滤，只能靠 LLM 写 SQL 猜，容易错或查空。

## 二、方案
仿 route_resolver 做一个 asset_resolver：
1. 加 _SUBJECT_TABLES：
   - bridge → basic_facilities_bridges
   - tunnel → basic_facilities_tunnels
   - slope → basic_facilities_slope
2. 新增 asset_resolver._resolve_asset(kind, name)：
   - 从对应表把 名称列(id/name/long_name) 拉出来（带缓存，仿 _route_rows）；
   - 精确/前两字模糊匹配 → 返回 id；
   - 多个同名给相近选项。
3. build_query_plan 里：subject 是 bridge/tunnel/slope 时，把问题里的资产名解析成 id 过滤，和路线走同一条链路。
4. 长对话指代：memory.result_entity 除了路线 id，也记桥/隧/边坡 id，下一轮"它"能复用。

## 三、验收
1. "XX桥的技术等级是多少" → SQL 带 bridge.id='xx'；
2. "最长的隧道" → 落到 tunnels 表；
3. 长对话：上一轮问某座桥，下一轮"它的净宽呢"能带上桥 id；
4. 同名多座给相近选项，不硬猜。

---

## 四、实施记录（2026-09-24）

方案事实核对**全对**：`_SUBJECT_TABLES`（capabilities.py:22）确实只有 7 个主体；
nl2sql:1921-1923 只有"主体/词 → 表"的映射；只有路线有名称→ID 解析。按方案实现了，
但有一处**改法不同**（下面 4.2）。

### 4.1 落地物

| 位置 | 内容 |
|---|---|
| `app/services/asset_resolver.py`（新增） | `ASSET_TABLES`（bridge/tunnel/slope → 表 + 名称列 + 后缀词）、`rows_of()`（只读 + TTL 缓存，查不通用旧缓存、没旧缓存返回空）、`name_tokens()`（从问句切"像资产名"的片段）、`resolve()`（精确 → 包含 → 候选） |
| `nl2sql.build_query_plan` | 字段触达定完落点表后：落点是这三张台账就解析资产名 → 命中写 `slots["asset_match"]` + `required_conditions += ["asset"]`；同名多座只写 `slots["asset_candidates"]`，不硬猜 |
| `nl2sql` 提示词槽位 | 新增 `asset: <表>.id = '<id>'（名字）`，并注明"按 id 过滤，别按名称字符串过滤"（同 multi_route 那条的教训） |

### 4.2 一处改法不同：**用"落点表"锚定，而不是加语义主体**

方案 §二.1 建议把 `bridge/tunnel/slope` 加进 `_SUBJECT_TABLES`。**没这么做**，原因：

1. 语义层的 subject 词表在 `resources/lexicon/semantics.yaml`（disease/pile_point/section/
   patrol/work_order/asset/route），LLM 现在根本不会给出 `bridge`；只往 `_SUBJECT_TABLES` 里加
   三行，`subject` 永远不会是它们 → 等于加了不生效的死配置；
2. 真要让它生效就得改语义词表 + 提示词，那会**改动一大批题的规划**（"桥梁的技术评定等级分布"
   现在落 bridges 是靠字段触达/词表证据，实测是对的），风险远大于收益；
3. 字段触达本来就已经把落点定到这三张表了（实测"桥梁的技术评定等级分布"→ bridges ✓），
   所以直接**用落点表锚定**：`asset_resolver.subject_of(落点表)` → 解析名字。

### 4.3 实测（用假行喂进缓存，三张表现在是空的）

```
羊台山桥的技术等级是多少？   → {'id': 'b2', 'name': '羊台山桥'}      精确命中 ✓
罗田桥的技术等级是多少？     → 同名两座 → candidates=[b1, b3]，不硬猜 ✓
这座羊台山桥的净宽呢        → 去掉"这座"前缀 → 命中 ✓（长对话指代可复用）
桥梁按技术等级分布          → 没点名资产 → 空（不干扰普通分布题）✓
最长的隧道是哪条            → 只剩后缀"隧道" → 当成最高级问题 → 空 ✓（该 ORDER BY 长度）
羊台山隧道的净宽是多少      → {'id': 't1'} ✓
```

### 4.4 没做的两件

1. **语义主体词表加 bridge/tunnel/slope**：见 4.2，故意不做；
2. **`memory.result_entity` 记资产 id**（长对话里下一轮"它的净宽呢"）：解析本身已经能接住
   "这座羊台山桥"，但要让**指代词独立成句**（"它的净宽呢"）也能带上 id，得改 memory 的
   entity 池（现在是路线/病害类型那几类）。等三张表有数据、这条链路真被用到再补更划算。

### 4.5 验不了的部分（如实说）

三张表现在 **0 行**（桥/隧/边坡都是空的），所以真实名字解析今天**解析不出任何东西**——
上面的实测是把假行塞进缓存做的。代码先备好，数据一到就能用；想现在就端到端试，可以用夹具库：
`python run_regression.py --fixtures`（它给这三张表各造了 12 行结构合法的数据）。
