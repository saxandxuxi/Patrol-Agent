# 模板 spec 与现行语义漂移核查（2026-09-23）

## 一、为什么查这个

上一轮我按"推荐卡里出现自己"的口径复测 auto 模板自匹配，得到 **19/37**，而之前一次报的是 37/37
（口径不同：那更像是"可点查模板能自绑定"）。17 条模板"拿自己的题面都召不回自己"这件事实在太可疑，
所以单独开一轮查**每条模板卡在哪个闸门、为什么**。

## 二、方法

对每条 `status=mature` 的 auto 模板：用它记录的 `question` 重新过一遍
`nl2sql.build_query_plan()`，再用**严格复刻 `templatizer.card_for_template` 闸门顺序**的判定器
找出第一个不通过的闸门。闸门顺序（与源码一致）：

```
① _plan_allows_template / 状态  →  ③ 条件覆盖(plan_keys ⊆ 模板covered)  →  ④ top_n
→ ⑤ shapes  →  ⑥ subjects  →  ⑦ group_by  →  ⑧ metrics ⊆  →  ⑨ 指标与字段都空
→ ⑩⑪⑫ 槽位  →  ⑬ bind / 点名列必须在 SQL 里
```

## 三、结果：37 条里命中 19~20、未命中 17~18

（跑了两遍，命中数分别是 19 和 20——语义层对个别题目给什么 `group_by`/`shape` 会变，
所以这组数字本身有 ±1~2 的抖动。下面按未命中的**原因**分类，分类结论两轮一致。）

| 闸门 | 条数 | 说明 |
|---|---|---|
| 命中（无漂移） | 19~20 | 简单计数/明细类模板（"西乡段7月30日有多少处泛油""泛油桩号有哪些"…）都对得上 |
| ⑤ shape 漂移 | 7 | 记录的形状与今天语义层给的形状不是一类 |
| ③ 条件覆盖 | 7 | 今天的 plan 多出模板没声明的条件 |
| ⑧ metrics 漂移 | 1~2 | 记录指标 ⊆ 今天的指标不成立 |
| ① 闸门 | 1 | 多条件+分组（或分组维度对不上）的复合题，本来就不该摆模板（设计如此） |
| ⑦ group_by | 1 | 模板不分维度，今天却给了分组维度 |

### 3.1 ⑤ shape 漂移的 7 条明细

```
auto_169df5f91aac034a「宝安大道西乡段病害的最大宽度和最大高度分别是多少？」
     记录 shapes=['aggregate']   今天 count      ← "aggregate" 不是合法取值（见 3.4，死记录）
auto_3baba2e4b002286a「西乡段的经度范围」
     记录 shapes=['list']        今天 distribution
auto_b1d19f05421bf716 / auto_b0c8be788b9d9452「有图片的和没有图片的分别有多少」
     记录 shapes=['count']       今天 distribution
auto_bb74d372164d0308「西乡段的病害类型，哪几种最常见？」
     记录 shapes=['count']       今天 top
auto_c23a7fb238aaf9d9「宝安大道西乡段和宝安中心--恒明珠，哪个病害密度更高？」
     记录 shapes=['count']       今天 distribution
auto_01487d891fb15ab6「…泛油病害中，面积大于 1 平方米且置信度…」
     记录 shapes=['list']        今天 top
```

规律很清楚：**记录时是"粗形状"（count / list），今天语义层给的是"细形状"（distribution / top）**。
上一轮刚把语义层做细（指标/字段白名单扩到字段目录、prompt 加列清单、硬推荐修正），
于是这批"按旧语义挖出来的"模板集体对不上——它们是按当时的语义层画的像。

### 3.2 ③ 条件覆盖的 7 条明细

```
pile_range 多出来（4 条 —— 收益最大的一类）
  auto_d2ffd9c130b9d94a「k1+802 附近还有其他高置信度的病害记录吗？」
  auto_f59d4da972443198「宝安中心--恒明珠的泛油：6月多少？7月多少？…」
  auto_0bde395f0551befe「宝安大道西乡段 K0+800 到 K0+900 有多少…」
  auto_1df497baedc7a855「宝安大道西乡段 K0+800-K0+900 泛油 总面积…」
       今天 plan_keys 里有 pile_distance **和** pile_range 两个；
       模板只声明了 pile_distance → 覆盖不住 → 弃权（设计上"宁可不匹配"）
unknown_type 多出来（1 条）
  auto_83d0377e8a6a5a93「宝安大道西乡段的 377 处"未识别"病害，按桩号分布…」
area 多出来（1 条）
  auto_8b8f1e46bb2dfb18「…面积大于10平方米的病害有多少处？占比…」
       模板的条件集只有 ['route']，连阈值条件都没记进去 → 这条记录本身不完整
键口径错位（1 条）
  auto_41ed6faee9be6caf「…泛油病害，在哪些桩号段最集中？」
       模板记的是 ['comparison','top_n']，今天 plan 给的是 ['damage_type','route'] —— 两边不是一套键
```

`pile_range` 这 4 条值得单独看：同一句话里"K0+800 到 K0+900"被抽成了 `pile_distance` + `pile_range`
两个条件，而模板侧只认识前者。这更像是**语义层的冗余抽取**（区间条件和它的端点用同一个字段表达），
不是模板记错了。

### 3.3 ⑧ metrics 漂移的 2 条

```
auto_a5fed6c64ecc8906「西乡段病害密度是多少？」
     记录 metrics=['count','max_route_length_km']  今天 ['density']   ← 记录侧把"每公里"记成"最长路线长度"
auto_3889cd6f50e194aa「6月和7月平均每天各发现多少条病害？」
     记录 metrics=['count']                         今天 ['count','rate']  ← 今天多抽了 rate
```

### 3.4 一个确凿的 bug：模板记录里的 `aggregate`

`templatizer._sql_profile()`（挖掘侧）在"指标既不是 count/distinct，也不是 total_/avg_ 前缀"时写
`shape = "aggregate"`；而**语义层的 shape 词表**（`nl2sql._SEMANTIC_SHAPES`）只有
`count / sum / avg / distribution / list / top / trend / bucket_compare` —— 永远不会给出 `aggregate`。
于是这类模板**永远匹配不上**（现库 1 条：`auto_169df5f91aac034a`）。

修法（两行，未做，等确认）：

1. 挖掘侧：`shape = "aggregate"` 改成按语义词表记 `shapes = ["count","sum","avg"]`
   —— 真正拦住"只问 count 的问题"的是**指标覆盖闸**（plan metrics ⊆ 模板 metrics），不是 shape；
2. 匹配侧：`card_for_template` 碰到旧记录里的 `aggregate` 时，按 `count/sum/avg` 认，
   否则已经存下来的那条记录永远是死的。

## 四、结论与建议

1. **不是本轮改动引入的**：`templatizer` / `capabilities` 都不读 `resources/sql_templates/_index.yaml`。
   （本轮在 `card_for_template` 里**新加了一道具名列表闸**，但它只会否掉"问题点名 lat、
   模板 SQL 里没有 lat"这种，不影响自匹配命中数——见第五节。）
2. **根因是"模板 spec 冻结在挖掘那一刻"**：模板是按*当时*的语义层画的像；语义层一旦做细
   （上一轮就是），旧模板就成批对不上。这不会自愈——只会随着语义层迭代越积越多。
3. **优先做的三件事**（按收益排）：
   - `aggregate` 那两行（1 条死记录 + 以后不再产生死记录）；
   - `pile_range` 冗余抽取对齐（4 条模板卡在这里，收益最大，但要先确认"区间条件要不要再单独出一个键"）；
   - 给模板库一个**按当前语义层重新校准**的入口（重跑 `_sql_profile` 更新 spec，而不是重新挖），
     否则形状从 count→distribution 这类漂移只能靠人工发现。
4. 一句话口径：**这 18 条不是"匹配逻辑坏了"，而是"模板记录的是上一版语义的指纹"。**

## 五、顺带修掉的一条（本轮已改）

查这一轮时发现一个**现网的硬推荐**问题，和 spec 漂移同源（问题点名了列，模板里没有那一列）：

`card_for_template` 里"问题点名了字段 → 模板 SQL 里必须有那一列"这道闸，原来只在
**槽位齐全、SQL 拼得出来**的那条路上生效；**缺槽的弱候选绕过了它**。于是：

```
「西乡段的纬度范围」连跑 5 次（每次新进程，语义层重新判）：
  3 次 → 前三名 = auto_1dfe118d5007203b / auto_2920a2fcddfe0c29 / auto_04a40f387375301b
         （都是"宝安大道西乡段有多少处泛油"这类 count 模板，而且缺 damage_type，是弱卡）
  2 次 → 第一名 = generic_geo:basic_facilities_sections_pile_detail ✓
```

差别只在语义层把主体判成 `route` 还是 `asset`（判成 route 时那几个模板的 `subjects` 命中了）。
修法：把那道闸提到槽位判定**之前**，用模板自带的 SQL 判（`sql_covers_fields`）。修完再跑 4 次：

```
shape=count subject=route       → 第一名 generic_geo ✓
shape=count subject=route       → 第一名 generic_geo ✓
shape=count subject=route       → 第一名 generic_geo ✓
shape=distribution subject=route → 第一名 generic_geo ✓
```

37 条自匹配命中数不受影响（19→20，仍在抖动区间内）。

## 六、实施记录：§四.3 的三件事都做了（2026-09-23）

### 6.1 `aggregate` 两行：已改

- **挖掘侧**（`_ast_profile`）：指标既不是 count/distinct、也不是 `total_/avg_` 前缀时，原来记私有
  取值 `aggregate`（语义层永远不会给），现在按语义词表记 `shapes=["count","sum","avg"]`
  —— 真正拦住"只问 count 的问题"的是**指标覆盖闸**，不是 shape。
- **匹配侧**（`card_for_template`）：老记录里留着 `aggregate` 的，按 `count/sum/avg` 认，
  否则那条存量记录永远是死的。

### 6.2 `pile_range` 归一：已改（4 条模板复活）

`templatizer.plan_condition_keys` 里：桩号条件只报一个键——若 `pile_distance` 与 `pile_range`
同时出现（语义层在 pile_distance 列上抽的区间条件 + 规则按 slots 加的 pile_range，说的是同一件事），
丢掉 `pile_range`（模板记录用的是 `pile_distance`）。

### 6.3 模板 spec 重校准入口：已做（而且真有效）

`templatizer.recalibrate_specs(dry_run=True)`：按**当前**的 `_ast_profile` 重算每条模板的
shapes/主体/分组/指标，**不重挖 SQL、不动条件与槽位**；`dry_run=False` 才写回。
入口：`POST /api/templates/auto/recalibrate?apply=0|1`，管理页「自动沉淀模板」多了一个
**spec 重校准** 按钮（先看差异，有差异才写回）。

> 注：一开始担心"重算 = 空操作"。实测**不是**——先补齐两处识别能力，重算才有内容：
> ① `_GROUP_BY_NAMES` 加 `problem_type_name → damage_type`（disease 模型 ref_sql 抽出来的列名，
> LLM 写 `GROUP BY COALESCE(d.problem_type_name,'未识别')` 时分组维度被漏掉）；
> ② `_ast_profile` 把 `LIMIT 'zzslotxxtop_nxxzz'`（挖槽自己留下的占位）认成"取前 N 名"。
> 补完这两处后重校准命中 3 条，其中 2 条是能从"永远匹配不上"变回可用：

```
auto_169df5f91aac034a  shapes ['aggregate']        → ['count','sum','avg']   （死记录复活）
auto_bb74d372164d0308  count/无分组                → top + damage_type       （哪几种病害类型最常见）
auto_41ed6faee9be6caf  count/无分组                → top                     （哪些桩号段最集中）
```

37 条里其余 34 条重算结果与记录完全一致（说明记录确实是 SQL 的忠实画像，剩下的漂移都在
"问题 → plan"那一侧，只能靠放宽匹配或重挖，不在这条路的射程内）。
