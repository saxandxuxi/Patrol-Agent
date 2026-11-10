# 确定性模板是怎么被选中、又是怎么拼出 SQL 的（讲解）

这份文档只讲一件事：用户问一句话，系统里那批"确定性模板/构造器"是怎么判断"这题归不归我管"，以及判断通过后 SQL 是怎么拼出来的。不涉及 Wren 自由生成那条路。

## 一、它在整条链路里的位置

一句话进来后，主干是：

1. 长对话补全（把"那恒明珠段呢"补成完整问题）；
2. 规划模型把问题解析成结构化的 **plan**（这一步要调大模型）；
3. **确定性模板匹配**：拿 plan 去对一批模板的"适用声明"，看有没有模板能直接答（不调大模型）；
4. 有候选 → 弹确认卡给用户点；用户确认 → 直接执行模板 SQL，不再重新规划、不进 Wren；
5. 没有模板能接 → 才交给 Wren / 大模型自由生成 SQL。

所以模板的价值是：规划只做一次"理解"，理解完只要能对上模板，后面就是确定性执行，又快又稳、结果可复现。

## 二、plan 是什么：把一句话压成结构化字段

模板不看用户原话，看的是规划模型产出的 plan。关键字段：

- `shape`（查询形态）：count 计数 / distribution 分布 / top 排名 / list 明细 / sum、avg / bucket_compare 分档对比 / density 密度 / 台账信息 等；
- `subject`（主体）：disease 病害 / route 路线 / section 路段 / patrol 巡检任务 / asset 资产 / work_order 工单 等；
- `slots.group_by`（分组维度）：route 路线、damage_type 病害类型、pile_bucket 桩号分段、direction 上下行、month 月份 等；
- `semantic.metrics`（指标）：count、total_area、avg_area、max_area、avg_length 等；
- `semantic.derived`（派生指标）：ratio 占比、density 密度 等；
- `slots` 里解析好的具体值：route_match / routes（路线 id+名称）、damage_matches（病害类型）、months / time_range（时间）、pile_range（桩号范围）、semantic_conditions（面积/置信度阈值和比较符）、device_code 等；
- 一组布尔特征 `flags`（由 plan 现算，见 preset_lib._plan_flags）：
  - multi_route 两条路对比、comparison 比较题、multi_metric 一问多指标、
  - numeric_condition 带数值比较、explicit_threshold 用户明确给了阈值线、
  - multi_period 多个并列时间段、spatial_split 空间前后段拆分、
  - has_damage_filter 带病害类型、has_image 要看图、pile_range 带桩号范围、device_code 带设备号。

同一句话，无论用户怎么换说法，只要规划模型理解对了，plan 就是同一套结构——这是"不靠穷举问法"的基础。

## 三、模板有两种存放形式

1. **代码构造器**：`backend/app/services/nl2sql.py` 里一堆 `_build_xxx_template(plan)` 函数，每个函数内部做判断并用 Python 拼 SQL。例如 `_build_route_info_template`（路线台账）、`_build_threshold_count_template`（阈值计数）、`_build_per_km_template`（每公里密度）、`_build_multi_route_template`（多路对比）等。
2. **外置模板**：
   - `backend/app/data/preset_queries.json`：36 条内置快速通道模板，SQL 里写 `{占位}`，参数从库里动态查（有数据的路线、月份、病害类型）；
   - `resources/sql_templates/_index.yaml`：模板注册表，声明每个内置模板"适用什么 plan"，并指定由哪个代码构造器（builder）来拼。

## 四、第一步：按 plan 语义选模板（不靠原话关键词）

`preset_lib.match_builtin_templates`（preset_lib.py）读取 `_index.yaml`，对每个模板的 `match` 声明逐条核对。声明可以约束：

- `kinds`：mysql 还是 postgresql；
- `shapes`：哪些查询形态能用；
- `subjects`：哪些主体能用；
- `group_by`：按什么分组；
- `metrics` / `derived`：包含哪些指标/派生指标（与 plan 取交集，非空才算）；
- `flags.require`：必须成立的布尔特征；`flags.require_any`：至少一个成立；`flags.forbid`：成立就不许用；
- `match_alt`：同一模板的第二套适用条件（有些题没被打上 multi_route 标记，但本质也是多对象比指标）。

只有这些规则**全部满足**，模板才入围。然后打分排序：形态、主体、分组、必填特征对得上加分；外置模板还会因为"多算了用户没要的指标"扣分（只问"多少处"却连总面积一起算的模板往后排），让"刚好对上"的模板排第一。

选中后，`build_template_sql_for(plan, builder)` **只调用得分最高的那一个构造器**，不再像旧版 `_build_template_sql_legacy` 那样按顺序把二十几个构造器全试一遍。

`_index.yaml` 里的真实条目长这样（人读的 trigger 是说明，机器看的是 match）：

```yaml
- id: route_info
  title: 路线台账信息
  builder: _build_route_info_template
  match:
    subjects: [route]

- id: bucket
  title: 按指标中位数分两档对比
  builder: _build_bucket_template
  match:
    shapes: [bucket_compare]
    flags:
      forbid: [numeric_condition, spatial_split]

- id: multi_route
  title: 多路线对比（每条路一行）
  builder: _build_multi_route_template
  match:
    group_by: [route]
    metrics: [avg_area, total_area, max_area, avg_length, count]
    flags:
      require: [multi_route]
```

## 五、第二步：构造器自己再判断一遍（不能接就 return None）

被点名后，构造器开头还有几行硬性自我保护，任一不满足就返回 None（"这题我不接"），交回给匹配流程/ Wren。真实例子：

- 阈值计数 `_build_threshold_count_template`：
  - 必须 `shape == count` 且 `subject == disease`；
  - 原话出现"分布 / 各多少 / 分别 / 占 / 比例 / 拆开"就放弃（那是分组题，纯计数模板会丢维度）；
  - 从 plan 解析不出数值阈值（面积>5 这类）就放弃。
- 路线台账 `_build_route_info_template`：
  - 槽位里必须有路线 id；
  - 原话要含台账类词（车道、全长、起终点等）；
  - 出现"病害 / 泛油 / 裂缝"且不是问"基本情况"就放弃（问的是病害，不是台账）。
- 每公里密度 `_build_per_km_template`：要有"每公里/密度"这类词、主体是病害、且有路线 id。

这里出现的少量词语（"每公里""病害""分别"）是**排除用的安全护栏，不是端到端触发器**。触发权在第四步的 plan 语义匹配；这些词只负责"别答错领域、别丢掉分组"。这正是"不靠穷举用户说法"的边界：决定接不接靠结构，词只做最后一道保险。

## 六、第三步：判断通过后怎么拼 SQL（纯确定性，不调模型）

构造器从 plan 里**已经解析好的槽位**取值，套进写死的 SQL 骨架：

1. `_metric_filters(plan)` 统一生成 JOIN 和 WHERE：路线 `r.id`、病害类型、时间范围、桩号范围、面积/置信度阈值，以及 del_flag、租户等固定业务条件都在这里拼；
2. 路线 id 等值做单引号转义，防注入；
3. 按题型套骨架：
   - 纯计数：`SELECT COUNT(*) AS count FROM ai_problem p {JOIN} WHERE {条件全 AND} LIMIT 1`；
   - 每公里密度：`ROUND(COUNT(*) / NULLIF(路线长度公式,0), 2)`，路线长度一律用台账起止桩号算（不信 distance 字段，以前用它答出过离谱值）；
   - 两条路：`r.id IN (...) ... GROUP BY r.id`，每条路一行；单条路：`r.id = '...' LIMIT 1`；
   - 问"多少米"就把公里乘 1000；
4. 写 `metric_labels`（如 count→处数、per_km→每公里处数、lane_count→车道数），告诉结果总结每列的中文名，保证回答用词和表格一致。

整个过程是字符串拼接，没有大模型，所以快、可复现。

### 副本隔离（修过的一个关键 bug）

构造器只在 plan 的**深拷贝副本**上跑（`_template_scope`），拼完用 `_merge_template_annotations` 只把四类"结果注解"合并回原 plan：指标中文名 metric_labels、分档说明 boundary_note、口径备注 group_note、空列说明 empty_column_note。

`subject / shape / comparison` 这类"这句话到底问什么"的身份字段，构造器一个字都不许改。以前有构造器直接把原 plan 的 subject 从 route 涂改成 section，导致同一问题第二次匹配匹配不到路线模板、落点从路线表被带到路段表（两张表都有"车道数"列），表现为"同一句两次推荐不一样"。构造器是翻译器，不是理解器。

## 七、两个端到端例子

**例 1："西乡段是几车道"**
- plan：subject=route、台账形态、route_match 带西乡段 id；
- 病害计数类模板要求 subject=disease → 出局；
- route_info 模板声明 subjects 含 route → 入围、选中 `_build_route_info_template`；
- 构造器复核：有路线 id、原话有"车道"、无"病害" → 通过；
- 拼出 `路线表 r LEFT JOIN 路段表 s ON s.route_id=r.id ... WHERE r.id=... GROUP BY r.id`，取 `MAX(s.lane_count)`；
- 弹卡 → 用户确认 → 直接执行，出"6 车道"。

**例 2："西乡段泛油有多少处"**
- plan：subject=disease、shape=count、带 damage 过滤（泛油）、有路线；
- route_info 因主体不符、又踩"泛油"排除词，出局；
- 病害规模计数模板（形态 count + 主体 disease + 类型过滤）命中；
- `_metric_filters` 拼好 r.id 和泛油条件 → `SELECT COUNT(*) ... WHERE ... AND 类型='泛油'`；
- 确认后直接出 534，引擎标记为预置模板（已确认），零大模型。

## 八、边界：为什么有些题模板不接

模板只覆盖"结构说得清、槽位填得齐"的题。出现下面情况就不命中、交给 Wren：

- 模板声明表达不了的指标或字段（比如冷门字段、模糊的"尺寸"）；
- 过滤条件对不齐（用户多了一个模板没有的条件，硬接会把结果放大/缩小）；
- 复合了规范知识、需要归因分析（"为什么这么多、该怎么养护"）；
- 形态/主体落在所有声明之外。

设计取舍很明确：**宁可少接、交 Wren，也不许模板在条件不全时硬答一个错口径**；接不接由结构和条件覆盖决定，不由用户用了哪个词决定。

## 九、想查看或新增模板时看哪里

- 看"现在注册了哪些模板、各自适用什么 plan"：`resources/sql_templates/_index.yaml`；
- 看内置快速通道的参数化 SQL：`backend/app/data/preset_queries.json`；
- 看某个模板具体怎么判断、怎么拼：`_index.yaml` 里的 builder 名 → nl2sql.py 里同名 `_build_xxx_template` 函数；
- 匹配与打分逻辑：`backend/app/services/preset_lib.py` 的 `match_builtin_templates` / `match_templates` / `_plan_flags`；
- 新增一个确定性模板的常规做法：在 `_index.yaml` 加 match 声明 + 写对应构造器（或在 preset_queries.json 加参数化 SQL），让它靠 plan 语义命中，而不是靠加关键词。
