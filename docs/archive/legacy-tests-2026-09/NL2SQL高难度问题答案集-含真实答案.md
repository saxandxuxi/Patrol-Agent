# NL2SQL 高难度问题答案集（含真实答案）

> 日期：2026-09-16
> 定位：15 道高难度 NL2SQL 测试题，每题附带标准 SQL 和**从数据库实际执行得到的真实答案**，可直接用于验证系统输出是否正确。
> 数据库：MySQL（new_four_road_base_ai），通过项目 db_manager 实际执行 SQL 取得答案。
> 关联文档：[提示词Bug全面排查报告.md](./提示词Bug全面排查报告.md)、[NL2SQL高难度问题答案集.md](./NL2SQL高难度问题答案集.md)（无答案版）

---

## 数据基准

| 项 | 值 |
|---|---|
| 路线1 | 宝安大道西乡段（911 处病害，起止桩号 K0+000~K5+100=5.10km，distance字段=5.60（不准），全部有图片，只有上行） |
| 路线2 | 宝安中心--恒明珠（749 处病害，起止桩号 K1+000~K20+000=19.00km，distance字段=0.00（未填），全部有图片，上行 732 / 下行 17） |
| 病害类型 | 两条路均只有"泛油"和"未识别"两种类型 |
| 西乡段泛油 | 534 处，平均面积 1.07㎡ |
| 西乡段未识别 | 377 处，平均面积 147.67㎡ |
| 恒明珠泛油 | 402 处 |
| 恒明珠未识别 | 347 处 |
| 恒明珠时间范围 | 2026-06-18 ~ 2026-07-30 |
| 巡检表时间字段 | create_time（没有 start_time 字段） |
| 工单表 | ai_work_order，通过 problem_id 关联病害，状态字段 order_status |

---

## 题目列表

---

### Q1：有图片 / 无图片分别统计

**问题**：宝安大道西乡段的病害中，有图片的和没有图片的分别有多少？

**难点**：
- "分别统计"需要用 CASE WHEN 分两列，不能退化成单条 COUNT(*)
- "有图片"条件必须同时处理 NULL 和空字符串：`problem_url IS NOT NULL AND problem_url <> ''`
- "没有图片"条件：`problem_url IS NULL OR problem_url = ''`
- 系统常见错误：只返回总数，不分别统计

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN p.problem_url IS NOT NULL AND p.problem_url <> '' THEN 1 ELSE 0 END) AS has_image,
    SUM(CASE WHEN p.problem_url IS NULL OR p.problem_url = '' THEN 1 ELSE 0 END) AS no_image,
    COUNT(*) AS total
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
```

**正确答案**：
- 有图片：**911 处**
- 没有图片：**0 处**
- 总数：911 处
- （该路线全部病害都有图片，无图片为 0）

---

### Q2：病害大类分布（code 翻译）

**问题**：宝安中心--恒明珠 2026 年 7 月采集的病害中，各大类（路面/路基/沿路设施/交安事件）分别有多少？

**难点**：
- `disease_categories_type` 是 code（0/5/10/15），必须用 CASE WHEN 翻译成中文，不能直接输出 code 数字
- 时间范围过滤（2026-07-01 ~ 2026-08-01）
- 系统常见错误：返回"类别0：259、类别NULL：184"，不翻译

**标准 SQL**：
```sql
SELECT 
    CASE b.disease_categories_type 
        WHEN '0' THEN '路面病害' WHEN '5' THEN '路基病害' 
        WHEN '10' THEN '沿路设施' WHEN '15' THEN '交安事件' 
        ELSE '未识别' 
    END AS category,
    COUNT(*) AS cnt
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type, disease_categories_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
  AND p.collection_time >= '2026-07-01' AND p.collection_time < '2026-08-01'
GROUP BY b.disease_categories_type
ORDER BY cnt DESC
```

**正确答案**：
- 路面病害：**259 处**
- 未识别：**184 处**
- 合计：443 处

---

### Q3：Top 病害类型 + 各自平均面积

**问题**：宝安大道西乡段数量最多的病害类型分别是什么？各自的平均面积是多少？

**难点**：
- "分别是什么 + 各自的平均面积"需要 GROUP BY 病害类型 + COUNT + AVG，不能退化成全表聚合（总面积/总平均/总数）
- 病害类型需要关联字典表转名称，字典表 code 重复需去重
- 未识别类型（damage_type 在字典中查不到）也要保留，用 COALESCE 处理 NULL
- 系统常见错误：返回"总面积 56241.20㎡、平均面积 61.74㎡、处数 911处"，完全没有 GROUP BY

**标准 SQL**：
```sql
SELECT 
    COALESCE(b.problem_type, '未识别') AS damage_type,
    COUNT(*) AS cnt,
    ROUND(AVG(p.area), 2) AS avg_area
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
GROUP BY b.problem_type
ORDER BY cnt DESC
```

**正确答案**：
| 病害类型 | 数量 | 平均面积 |
|---|---|---|
| 泛油 | 534 处 | 1.07 ㎡ |
| 未识别 | 377 处 | 147.67 ㎡ |

---

### Q4：两条路对比 + 差值

**问题**：宝安大道西乡段和宝安中心--恒明珠的泛油数量分别是多少？相差多少？哪条路更多？

**难点**：
- 两条路对比需要用 CASE WHEN 分两列统计，不能只返回一条路的数量
- 需要计算差值（ABS）
- 需要判断哪条路更多
- 路线名必须原样照抄："宝安中心--恒明珠"（不是"恒明珠段"）
- 系统常见错误：只返回第一条路的数量，或路线名写错查不到

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) AS xixiang,
    SUM(CASE WHEN r.short_name = '宝安中心--恒明珠' THEN 1 ELSE 0 END) AS hengmingzhu,
    ABS(SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) 
      - SUM(CASE WHEN r.short_name = '宝安中心--恒明珠' THEN 1 ELSE 0 END)) AS diff
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name IN ('宝安大道西乡段', '宝安中心--恒明珠')
  AND b.problem_type = '泛油'
```

**正确答案**：
- 宝安大道西乡段泛油：**534 处**
- 宝安中心--恒明珠泛油：**402 处**
- 相差：**132 处**
- 西乡段更多

---

### Q5：多条件组合列表（面积 + 置信度）

**问题**：宝安中心--恒明珠面积大于 3 平方米、AI 识别置信度低于 0.7 的病害有哪些？列出桩号、面积、置信度，按面积从大到小排列前 10 条。

**难点**：
- 双数值阈值（area > 3 AND confidence < 0.7）
- 置信度从 pic_position JSON 提取：`CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6))`
- 列表查询必须 SELECT 具体字段（桩号、面积、置信度），不能退化成 COUNT(*)
- ORDER BY area DESC + LIMIT 10
- 系统常见错误：条件丢失（只保留一个），或退化成"共 N 处"

**标准 SQL**：
```sql
SELECT 
    p.pile_number AS pile,
    p.area AS area,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) AS confidence,
    p.problem_number AS problem_no,
    p.up_or_down AS direction
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
  AND p.area > 3
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) < 0.7
ORDER BY p.area DESC
LIMIT 10
```

**正确答案**（前 10 条，按面积降序）：
| 桩号 | 面积 | 置信度 | 病害编号 | 方向 |
|---|---|---|---|---|
| k4+765 | 5000.00 | 0.419634 | BPLHZD202606180043 | 上行 |
| k3+100 | 5000.00 | 0.419634 | BPLHZD202607290252 | 上行 |
| k2+002 | 5000.00 | 0.419634 | BPLHZD202607290105 | 上行 |
| k1+602 | 5000.00 | 0.419634 | BPLHZD202607220031 | 上行 |
| k0+502 | 5000.00 | 0.419634 | BPLHZD202607210008 | 上行 |
| k2+202 | 5000.00 | 0.419634 | BPLHZD202607140025 | 上行 |
| k3+859 | 5000.00 | 0.419634 | BPLHZD202606180067 | 上行 |
| k1+002 | 5000.00 | 0.419634 | BPLHZD202607070006 | 上行 |
| k1+002 | 11.00 | 0.504093 | BPLHZD202606250013 | 上行 |
| k0+502 | 11.00 | 0.504093 | BPLHZD202607290361 | 上行 |

---

### Q6：定期巡查任务数 + 发现病害数

**问题**：宝安大道西乡段 2026 年有多少次定期巡查任务？这些任务共发现了多少处病害？

**难点**：
- 需要从巡检表（ai_maintain_patrol）统计任务数，从病害表统计病害数
- 两表通过 patrol_id 关联
- 巡检类型过滤（patrol_type = '0' 定期巡查）
- 任务数用 COUNT(DISTINCT t.id)，不能 COUNT(*)（一个任务有多条病害）
- 巡检表时间字段是 create_time（没有 start_time 字段）
- 系统常见错误：用不存在的 start_time 字段导致 SQL 执行失败

**标准 SQL**：
```sql
SELECT 
    COUNT(DISTINCT t.id) AS patrol_count,
    COUNT(p.id) AS problem_count
FROM ai_maintain_patrol t
JOIN ai_problem p ON p.patrol_id = t.id
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE t.del_flag = 0 AND p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND t.patrol_type = '0'
  AND t.create_time >= '2026-01-01' AND t.create_time < '2027-01-01'
```

**正确答案**：
- 定期巡查任务：**10 次**
- 发现病害：**911 处**

---

### Q7：已转工单数量 + 占比

**问题**：宝安大道西乡段的泛油病害中，已经转成工单的有多少？占比多少？

**难点**：
- 需要左连接工单表（ai_work_order）判断是否已转工单
- 工单通过 problem_id 关联病害
- 需要计算已转工单数量和占比（已转 / 总数 * 100）
- 单条聚合（SUM CASE WHEN）不需要 GROUP BY
- 系统常见错误：校验报错"分布查询需要 GROUP BY"（单条聚合不需要 GROUP BY）

**标准 SQL**：
```sql
SELECT 
    COUNT(*) AS total,
    SUM(CASE WHEN w.id IS NOT NULL THEN 1 ELSE 0 END) AS has_work_order,
    ROUND(SUM(CASE WHEN w.id IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS ratio
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
LEFT JOIN ai_work_order w ON w.problem_id = p.id AND w.del_flag = 0
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND b.problem_type = '泛油'
```

**正确答案**：
- 泛油总数：**534 处**
- 已转工单：**0 处**
- 占比：**0.0%**

---

### Q8：上下行分别统计 + 各自最多病害类型

**问题**：宝安中心--恒明珠中，上行和下行各有多少处病害？上下行数量最多的病害类型分别是什么？

**难点**：
- 上下行字段（up_or_down: 1上行/2下行）
- 需要分别统计上行和下行的总数（CASE WHEN）
- 需要分别找出上行和下行数量最多的病害类型（需要子查询）
- 病害类型需关联字典，未识别类型用 COALESCE 处理
- 系统常见错误：只统计总数不分上下行，或子查询写错

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN p.up_or_down = 1 THEN 1 ELSE 0 END) AS up_total,
    SUM(CASE WHEN p.up_or_down = 2 THEN 1 ELSE 0 END) AS down_total,
    (SELECT COALESCE(b2.problem_type,'未识别') FROM ai_problem p2 
     LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b2 
         ON p2.damage_type = b2.problem_code
     WHERE p2.del_flag = 0 AND p2.up_or_down = 1 
       AND p2.routes_id IN (SELECT id FROM basic_facilities_routes WHERE short_name = '宝安中心--恒明珠' AND del_flag = 0)
     GROUP BY b2.problem_type ORDER BY COUNT(*) DESC LIMIT 1) AS up_top_type,
    (SELECT COALESCE(b2.problem_type,'未识别') FROM ai_problem p2 
     LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b2 
         ON p2.damage_type = b2.problem_code
     WHERE p2.del_flag = 0 AND p2.up_or_down = 2 
       AND p2.routes_id IN (SELECT id FROM basic_facilities_routes WHERE short_name = '宝安中心--恒明珠' AND del_flag = 0)
     GROUP BY b2.problem_type ORDER BY COUNT(*) DESC LIMIT 1) AS down_top_type
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
```

**正确答案**：
- 上行：**732 处**，最多病害类型：**泛油**
- 下行：**17 处**，最多病害类型：**泛油**

---

### Q9：面积最大的病害 + 详细信息

**问题**：宝安中心--恒明珠面积最大的病害是哪条？面积多大？在哪个桩号？什么类型？

**难点**：
- Top1 数值指标（ORDER BY area DESC + LIMIT 1）
- 需要返回详细信息（桩号、类型、面积、采集时间、方向）
- 不能退化成 COUNT(*) 或全表聚合
- 病害类型需关联字典，未识别用 COALESCE
- 系统常见错误：返回"最大面积 5000㎡"但不返回是哪条病害

**标准 SQL**：
```sql
SELECT 
    p.problem_number AS problem_no,
    p.pile_number AS pile,
    p.area AS area,
    COALESCE(b.problem_type, '未识别') AS damage_type,
    p.collection_time AS collect_time,
    p.up_or_down AS direction
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
  AND p.area IS NOT NULL
ORDER BY p.area DESC
LIMIT 1
```

**正确答案**：
- 病害编号：**BPLHZD202606180043**
- 桩号：**k4+765**
- 面积：**5000.00 ㎡**
- 病害类型：**未识别**
- 采集时间：2026-06-19 00:03:42
- 方向：上行

---

### Q10：两种病害类型各有多少 + 总数

**问题**：宝安大道西乡段的泛油和未识别的病害分别有多少？总共多少？

**难点**：
- "分别有多少"需要用 CASE WHEN 分两列，不能只返回总数
- "未识别"对应 damage_type 在字典中查不到（b.problem_type IS NULL）
- 系统常见错误：只返回总数 911，不分别统计

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN b.problem_type = '泛油' THEN 1 ELSE 0 END) AS fanyou,
    SUM(CASE WHEN b.problem_type IS NULL THEN 1 ELSE 0 END) AS unknown,
    COUNT(*) AS total
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
```

**正确答案**：
- 泛油：**534 处**
- 未识别：**377 处**
- 总数：**911 处**

---

### Q11：高置信度 + 大面积列表

**问题**：宝安中心--恒明珠 AI 识别置信度大于 0.9 且面积大于 3 平方米的病害有哪些？按面积从大到小排列前 10 条。

**难点**：
- 双数值阈值（confidence > 0.9 AND area > 3）
- 置信度 JSON 提取
- 列表查询 SELECT 具体字段
- ORDER BY + LIMIT
- 系统常见错误：条件丢失，或置信度写法错误（写成 p.confidence）

**标准 SQL**：
```sql
SELECT 
    p.problem_number AS problem_no,
    p.pile_number AS pile,
    p.area AS area,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) AS confidence,
    COALESCE(b.problem_type, '未识别') AS damage_type
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) > 0.9
  AND p.area > 3
ORDER BY p.area DESC
LIMIT 10
```

**正确答案**（前 10 条，按面积降序）：
| 病害编号 | 桩号 | 面积 | 置信度 | 类型 |
|---|---|---|---|---|
| BXQS202606250021 | k1+802 | 4.63 | 0.945732 | 未识别 |
| BXQS202607300157 | k2+301 | 4.63 | 0.945732 | 未识别 |
| BXQS202606270124 | k1+002 | 4.63 | 0.945732 | 未识别 |
| BXQS202607300011 | k1+302 | 4.63 | 0.945732 | 未识别 |
| BXQS202606290049 | k0+902 | 4.63 | 0.945732 | 未识别 |
| BXQS202607210064 | k2+402 | 4.63 | 0.945732 | 未识别 |
| BXQS202607150000 | k0+002 | 4.63 | 0.945732 | 未识别 |
| BXQS202607210022 | k1+902 | 4.63 | 0.945732 | 未识别 |
| BXQS202607200036 | k3+302 | 4.63 | 0.945732 | 未识别 |
| BXQS202606270140 | k2+602 | 3.89 | 0.916518 | 未识别 |

---

### Q12：路线长度 + 每公里病害数（病害密度）

**问题**：宝安大道西乡段路线全长多少公里？每公里有多少处病害？

**难点**：
- 需要从路线表获取长度（distance，单位 km）
- 需要统计病害数
- 需要计算病害密度（病害数 / 路线长度）
- 不能只返回病害数，忘记除以路线长度
- 系统常见错误：只返回"911处"，不计算密度

**标准 SQL**：
```sql
SELECT 
    r.short_name AS route_name,
    r.distance AS distance_km,
    COUNT(p.id) AS problem_count,
    ROUND(COUNT(p.id) / r.distance, 1) AS per_km
FROM basic_facilities_routes r
LEFT JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = 0
WHERE r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
GROUP BY r.id, r.short_name, r.distance
```

**正确答案**：
- 路线全长：**5.60 km**
- 病害总数：**911 处**
- 每公里病害数：**162.7 处/km**

---

### Q13：各巡检类型任务数 + 病害数

**问题**：宝安大道西乡段 2026 年各类型巡检任务（定期/日常/专项/人工复核/重点）分别有多少次？各发现了多少病害？

**难点**：
- 按巡检类型分组（patrol_type: 0/5/10/15/20）
- 巡检类型需要用 CASE WHEN 翻译名称
- 任务数用 COUNT(DISTINCT t.id)
- 病害数用 COUNT(p.id)
- 巡检表时间字段是 create_time
- 系统常见错误：用不存在的 start_time 字段，或不翻译巡检类型 code

**标准 SQL**：
```sql
SELECT 
    CASE t.patrol_type 
        WHEN '0' THEN '定期巡查' WHEN '5' THEN '日常巡查' 
        WHEN '10' THEN '专项巡查' WHEN '15' THEN '人工复核' 
        WHEN '20' THEN '重点巡查' ELSE CONCAT('其他(', t.patrol_type, ')') 
    END AS patrol_type,
    COUNT(DISTINCT t.id) AS task_count,
    COUNT(p.id) AS problem_count
FROM ai_maintain_patrol t
JOIN ai_problem p ON p.patrol_id = t.id
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE t.del_flag = 0 AND p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND t.create_time >= '2026-01-01' AND t.create_time < '2027-01-01'
GROUP BY t.patrol_type
ORDER BY task_count DESC
```

**正确答案**：
| 巡检类型 | 任务次数 | 发现病害数 |
|---|---|---|
| 定期巡查 | 10 次 | 911 处 |

（该路线只有定期巡查一种类型的任务）

---

### Q14：上下行平均面积

**问题**：宝安中心--恒明珠的病害中，上行和下行的平均面积分别是多少？

**难点**：
- 平均面积（AVG(area)）
- 上下行分别统计（CASE WHEN + up_or_down）
- 需要同时返回上行数量、下行数量、总体平均
- area 可能为 NULL，需要过滤
- 系统常见错误：只返回总体平均，不分上下行

**标准 SQL**：
```sql
SELECT 
    ROUND(AVG(CASE WHEN p.up_or_down = 1 THEN p.area END), 2) AS up_avg_area,
    ROUND(AVG(CASE WHEN p.up_or_down = 2 THEN p.area END), 2) AS down_avg_area,
    ROUND(AVG(p.area), 2) AS overall_avg_area,
    SUM(CASE WHEN p.up_or_down = 1 THEN 1 ELSE 0 END) AS up_cnt,
    SUM(CASE WHEN p.up_or_down = 2 THEN 1 ELSE 0 END) AS down_cnt
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安中心--恒明珠'
  AND p.area IS NOT NULL
```

**正确答案**：
- 上行平均面积：**55.90 ㎡**（732 条）
- 下行平均面积：**1.19 ㎡**（17 条）
- 总体平均面积：**54.66 ㎡**

---

### Q15：桩号区间 + Top1 病害类型 + 总数

**问题**：宝安大道西乡段 K0+000 至 K0+887 路段中，数量最多的病害是哪种？该路段病害总数是多少？

**难点**：
- 路线 + 桩号区间双条件
- 需要同时返回 Top1 病害类型（GROUP BY + ORDER BY + LIMIT 1）和总数（子查询 COUNT(*)）
- 病害类型需关联字典，未识别用 COALESCE
- 桩号用 pile_distance（米数），K0+887 = 887 米
- 系统常见错误：只返回总数，不返回 Top1 类型；或桩号换算错误

**标准 SQL**：
```sql
SELECT 
    COALESCE(b.problem_type, '未识别') AS top_type,
    COUNT(*) AS top_count,
    (SELECT COUNT(*) FROM ai_problem p2 
     JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
     WHERE p2.del_flag = 0 AND r2.del_flag = 0 
       AND r2.short_name = '宝安大道西乡段' 
       AND p2.pile_distance BETWEEN 0 AND 887) AS total_count
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND p.pile_distance BETWEEN 0 AND 887
GROUP BY b.problem_type
ORDER BY top_count DESC
LIMIT 1
```

**正确答案**：
- 数量最多的病害：**泛油**
- 泛油数量：**534 处**
- 该路段病害总数：**911 处**

---

## 转弯题（Q16-Q25）：需要间接计算 / 多步推理

> 这 10 道题的共同特点：答案不是直接 `SELECT` 某个字段就能得到，需要先做一步或多步间接计算（起止桩号相减、子查询求平均值、环比计算、占比计算等）。
> 核心考点：LLM 能不能理解"用户问的东西不在表里，需要算出来"。

---

### Q16：路线长度（起止桩号相减，distance 字段为 0）

**问题**：宝安中心--恒明珠路线全长多少公里？

**难点**：
- 路线表的 `distance` 字段为 **0.00**（未填写），不能直接用
- 必须用 `start_num`（起始桩号）和 `end_num`（结束桩号）相减得到长度
- `start_num`/`end_num` 是字符串格式（如 `K1+000`、`K20+000`），需要解析成数字：K 后面的数字 × 1000 + `+` 后面的数字
- 系统常见错误：直接用 `distance` 字段返回 0，或不知道怎么解析桩号字符串

**标准 SQL**：
```sql
SELECT 
    short_name,
    start_num,
    end_num,
    distance AS distance_field,
    (CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(start_num, '+', -1) AS DECIMAL)) / 1000.0 AS length_by_pile_km
FROM basic_facilities_routes 
WHERE del_flag = '0' AND short_name = '宝安中心--恒明珠'
```

**正确答案**：
- 起始桩号：**K1+000**
- 结束桩号：**K20+000**
- distance 字段：0.00（未填写）
- 起止桩号相减：**19.00 km**（K20+000 − K1+000 = 19000 米）

---

### Q17：两条路长度对比（都用起止桩号算）

**问题**：宝安大道西乡段和宝安中心--恒明珠的路线长度分别是多少公里？哪条更长？长多少？

**难点**：
- 两条路的 `distance` 字段都不可靠（西乡段 5.60 但桩号算出来 5.10，恒明珠 0.00）
- 必须都用起止桩号相减，不能一条用 distance 一条用桩号
- 需要计算差值
- 系统常见错误：直接用 distance 字段（恒明珠返回 0），或只返回一条路的长度

**标准 SQL**：
```sql
SELECT 
    short_name,
    start_num,
    end_num,
    (CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(start_num, '+', -1) AS DECIMAL)) / 1000.0 AS length_km
FROM basic_facilities_routes 
WHERE del_flag = '0' AND short_name IN ('宝安大道西乡段', '宝安中心--恒明珠')
ORDER BY length_km DESC
```

**正确答案**：
| 路线 | 起始桩号 | 结束桩号 | 长度（桩号相减） |
|---|---|---|---|
| 宝安中心--恒明珠 | K1+000 | K20+000 | **19.00 km** |
| 宝安大道西乡段 | K0+000 | K5+100 | **5.10 km** |

- 恒明珠更长，比西乡段长 **13.90 km**
- （注意：西乡段 distance 字段写的 5.60，但起止桩号算出来是 5.10，以桩号为准）

---

### Q18：病害密度对比（用起止桩号算长度再算密度）

**问题**：宝安大道西乡段和宝安中心--恒明珠，哪条路的病害密度更高？每公里各有多少处病害？

**难点**：
- **转两道弯**：先算路线长度（起止桩号相减）→ 再算病害数 → 再算密度（病害数/长度）→ 最后对比
- 不能用 `distance` 字段（恒明珠为 0）
- 密度计算需要除以路线长度，不能只返回病害数
- 系统常见错误：只返回病害数（911 和 749），不计算密度；或用 distance 字段导致恒明珠密度无穷大

**标准 SQL**：
```sql
SELECT 
    r.short_name,
    (CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(r.end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(r.start_num, '+', -1) AS DECIMAL)) / 1000.0 AS length_km,
    COUNT(p.id) AS problem_count,
    ROUND(COUNT(p.id) / NULLIF((CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(r.end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(r.start_num, '+', -1) AS DECIMAL)) / 1000.0, 0), 1) AS per_km
FROM basic_facilities_routes r
LEFT JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = '0'
WHERE r.del_flag = '0' AND r.short_name IN ('宝安大道西乡段', '宝安中心--恒明珠')
GROUP BY r.id, r.short_name, r.start_num, r.end_num
ORDER BY per_km DESC
```

**正确答案**：
| 路线 | 长度（km） | 病害数 | 密度（处/km） |
|---|---|---|---|
| 宝安大道西乡段 | 5.10 | 911 | **178.6** |
| 宝安中心--恒明珠 | 19.00 | 749 | **39.4** |

- 西乡段病害密度更高，约为恒明珠的 **4.5 倍**
- （虽然恒明珠病害总数 749 也不少，但路线长 19km，摊薄后密度低很多）

---

### Q19：环比增长率（6 月 vs 7 月）

**问题**：宝安中心--恒明珠 2026 年 7 月的病害数比 6 月增长了百分之多少？

**难点**：
- **转一道弯**：先算 6 月数量 → 再算 7 月数量 → 再算增长率 `(7月-6月)/6月 × 100`
- 不能只返回两个月的数量，必须算百分比
- 时间范围过滤（6 月：2026-06-01~07-01，7 月：2026-07-01~08-01）
- 需要处理分母为 0 的情况（NULLIF）
- 系统常见错误：只返回"6月306处，7月443处"，不计算增长率；或增长率算反了（6月比7月）

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN p.collection_time >= '2026-06-01' AND p.collection_time < '2026-07-01' THEN 1 ELSE 0 END) AS june_cnt,
    SUM(CASE WHEN p.collection_time >= '2026-07-01' AND p.collection_time < '2026-08-01' THEN 1 ELSE 0 END) AS july_cnt,
    ROUND((SUM(CASE WHEN p.collection_time >= '2026-07-01' AND p.collection_time < '2026-08-01' THEN 1 ELSE 0 END)
           - SUM(CASE WHEN p.collection_time >= '2026-06-01' AND p.collection_time < '2026-07-01' THEN 1 ELSE 0 END))
          * 100.0 / NULLIF(SUM(CASE WHEN p.collection_time >= '2026-06-01' AND p.collection_time < '2026-07-01' THEN 1 ELSE 0 END), 0), 1) AS growth_rate
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安中心--恒明珠'
  AND p.collection_time >= '2026-06-01' AND p.collection_time < '2026-08-01'
```

**正确答案**：
- 6 月：**306 处**
- 7 月：**443 处**
- 环比增长率：**+44.8%**
- （7 月比 6 月多了 137 处，增长接近一半）

---

### Q20：高置信度病害占比

**问题**：宝安大道西乡段的病害中，AI 识别置信度大于 0.9 的有多少处？占百分之多少？

**难点**：
- **转一道弯**：先算总数 → 再算置信度 > 0.9 的数量（JSON 提取）→ 再算占比
- 置信度没有独立列，必须从 `pic_position` JSON 提取：`CAST(JSON_UNQUOTE(JSON_EXTRACT(...)) AS DECIMAL)`
- 不能写成 `p.confidence > 0.9`（没有这列）
- 占比计算需要除以总数
- 系统常见错误：置信度写法错误（写成 p.confidence），或只返回高置信度数量不返回占比

**标准 SQL**：
```sql
SELECT 
    COUNT(*) AS total,
    SUM(CASE WHEN CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) > 0.9 THEN 1 ELSE 0 END) AS high_conf,
    ROUND(SUM(CASE WHEN CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) > 0.9 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS ratio
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安大道西乡段'
```

**正确答案**：
- 病害总数：**911 处**
- 置信度 > 0.9：**67 处**
- 占比：**7.4%**
- （高置信度病害占比很低，说明大部分病害的 AI 识别置信度在 0.9 以下）

---

### Q21：大面积病害占比（面积 > 整条路线平均面积）

**问题**：宝安大道西乡段中，面积大于整条路线平均面积的病害有多少处？占百分之多少？

**难点**：
- **转两道弯**：先算整条路线的平均面积（子查询）→ 再筛选面积 > 平均值的病害 → 最后算占比
- 平均面积需要用子查询先算出来，不能在同一个 SELECT 里引用别名
- 需要过滤 `area IS NOT NULL`
- 系统常见错误：不知道需要子查询，试图在 WHERE 里用 `p.area > AVG(p.area)`（SQL 不允许在 WHERE 里用聚合函数）；或只返回平均面积不筛选

**标准 SQL**：
```sql
SELECT 
    COUNT(*) AS total,
    ROUND(AVG(p.area), 2) AS avg_area,
    SUM(CASE WHEN p.area > (
        SELECT AVG(area) FROM ai_problem p2 
        JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
        WHERE p2.del_flag = '0' AND r2.del_flag = '0' 
          AND r2.short_name = '宝安大道西乡段' 
          AND p2.area IS NOT NULL
    ) THEN 1 ELSE 0 END) AS above_avg,
    ROUND(SUM(CASE WHEN p.area > (
        SELECT AVG(area) FROM ai_problem p2 
        JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
        WHERE p2.del_flag = '0' AND r2.del_flag = '0' 
          AND r2.short_name = '宝安大道西乡段' 
          AND p2.area IS NOT NULL
    ) THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS ratio
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安大道西乡段'
  AND p.area IS NOT NULL
```

**正确答案**：
- 病害总数（有面积）：**911 处**
- 整条路线平均面积：**61.74 ㎡**
- 面积 > 平均面积：**11 处**
- 占比：**1.2%**
- （只有极少数病害面积超过平均值，因为大部分是泛油（平均 1.07㎡），大面积的都是未识别类型）

---

### Q22：数量最多 vs 面积最大的病害类型对比（数量和面积倒挂）

**问题**：宝安大道西乡段中，数量最多的病害类型和面积最大的病害类型分别是什么？各自的数量占比和面积占比是多少？

**难点**：
- **转一道弯 + 反直觉**：数量最多的类型和面积最大的类型**不是同一种**——泛油数量最多但面积很小，未识别类型数量少但面积极大
- 需要同时计算数量占比和面积占比（两个不同的分母）
- 未识别类型对应 `damage_type` 在字典中查不到（`b.problem_type IS NULL`），用 COALESCE 处理
- 系统常见错误：认为数量最多的就是面积最大的（泛油），只返回一种类型；或只算数量占比不算面积占比

**标准 SQL**：
```sql
SELECT 
    COALESCE(b.problem_type, '未识别') AS damage_type,
    COUNT(*) AS cnt,
    ROUND(COUNT(*) * 100.0 / (
        SELECT COUNT(*) FROM ai_problem p2 
        JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
        WHERE p2.del_flag = '0' AND r2.del_flag = '0' 
          AND r2.short_name = '宝安大道西乡段'
    ), 1) AS cnt_ratio,
    ROUND(SUM(p.area), 2) AS total_area,
    ROUND(SUM(p.area) * 100.0 / (
        SELECT SUM(area) FROM ai_problem p2 
        JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
        WHERE p2.del_flag = '0' AND r2.del_flag = '0' 
          AND r2.short_name = '宝安大道西乡段' 
          AND p2.area IS NOT NULL
    ), 1) AS area_ratio,
    ROUND(AVG(p.area), 2) AS avg_area
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安大道西乡段'
  AND p.area IS NOT NULL
GROUP BY b.problem_type
ORDER BY cnt DESC
```

**正确答案**：
| 病害类型 | 数量 | 数量占比 | 总面积 | 面积占比 | 平均面积 |
|---|---|---|---|---|---|
| 泛油 | 534 处 | **58.6%** | 569.78 ㎡ | **1.0%** | 1.07 ㎡ |
| 未识别 | 377 处 | **41.4%** | 55671.42 ㎡ | **99.0%** | 147.67 ㎡ |

- **数量最多**：泛油（534 处，占 58.6%）
- **面积最大**：未识别（55671.42 ㎡，占 99.0%）
- 典型的"数量和面积倒挂"：泛油数量多但都是小面积（平均 1.07㎡），未识别数量少但都是大面积（平均 147.67㎡）
- 这道题专门测试系统能不能发现这种反直觉的现象，而不是想当然认为"数量最多=面积最大"

---

### Q23：平均巡检间隔

**问题**：宝安大道西乡段 2026 年的定期巡查任务，平均每隔多少天巡查一次？

**难点**：
- **转一道弯**：先算首次和末次巡检时间 → 再算天数差 → 再算 `天数差 / (巡检次数 - 1)`
- 巡检次数用 `COUNT(DISTINCT t.id)`（一个任务可能对应多条病害）
- 巡检表时间字段是 `create_time`（没有 `start_time`）
- 平均间隔 = 总跨度天数 / (次数 - 1)，不是总跨度 / 次数
- 系统常见错误：用不存在的 `start_time` 字段；或算成 `天数 / 次数`（少减 1）；或只返回巡检次数不算间隔

**标准 SQL**：
```sql
SELECT 
    COUNT(DISTINCT t.id) AS patrol_count,
    MIN(t.create_time) AS first_patrol,
    MAX(t.create_time) AS last_patrol,
    DATEDIFF(MAX(t.create_time), MIN(t.create_time)) AS days_span,
    ROUND(DATEDIFF(MAX(t.create_time), MIN(t.create_time)) / NULLIF(COUNT(DISTINCT t.id) - 1, 0), 1) AS avg_interval_days
FROM ai_maintain_patrol t
JOIN ai_problem p ON p.patrol_id = t.id
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE t.del_flag = '0' AND p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安大道西乡段'
  AND t.patrol_type = '0'
  AND t.create_time >= '2026-01-01' AND t.create_time < '2027-01-01'
```

**正确答案**：
- 定期巡查任务：**10 次**
- 首次巡检：2026-07-14
- 末次巡检：2026-07-30
- 天数跨度：**16 天**
- 平均巡检间隔：**1.8 天/次**
- （16 天内巡查了 10 次，间隔非常密集）

---

### Q24：病害平均间距（用起止桩号算路线长度）

**问题**：宝安大道西乡段的病害，平均每隔多少米出现一处？

**难点**：
- **转两道弯**：先算路线长度（起止桩号相减，单位米）→ 再算病害数 → 最后算 `长度（米）/ 病害数`
- 不能用 `distance` 字段（单位是 km 且不准确），必须用起止桩号相减得到米数
- 结果单位是米，不是公里
- 系统常见错误：用 distance 字段（5.60km = 5600米）而不是起止桩号（5100米）；或忘记换算单位直接用 km / 病害数

**标准 SQL**：
```sql
SELECT 
    COUNT(p.id) AS problem_count,
    (CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(r.end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(r.start_num, '+', -1) AS DECIMAL)) AS length_m,
    ROUND((CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.end_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     + CAST(SUBSTRING_INDEX(r.end_num, '+', -1) AS DECIMAL)
     - CAST(SUBSTRING_INDEX(SUBSTRING_INDEX(r.start_num, '+', 1), 'K', -1) AS DECIMAL) * 1000 
     - CAST(SUBSTRING_INDEX(r.start_num, '+', -1) AS DECIMAL)) / COUNT(p.id), 1) AS avg_gap_m
FROM basic_facilities_routes r
JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = '0'
WHERE r.del_flag = '0' AND r.short_name = '宝安大道西乡段'
GROUP BY r.id, r.start_num, r.end_num
```

**正确答案**：
- 病害总数：**911 处**
- 路线长度（起止桩号相减）：**5100 米**（K0+000 ~ K5+100）
- 病害平均间距：**5.6 米/处**
- （平均每 5.6 米就有一处病害，密度非常高）

---

### Q25：泛油面积占比

**问题**：宝安大道西乡段的泛油病害，总面积占整条路线病害总面积的百分之多少？

**难点**：
- **转一道弯**：先算泛油总面积（SUM(area) + 病害类型过滤）→ 再算全部总面积 → 最后算占比
- 不能只返回泛油总面积，必须算百分比
- 泛油需要关联字典表筛选 `problem_type = '泛油'`
- 需要过滤 `area IS NOT NULL`
- 系统常见错误：只返回泛油总面积（569.78㎡），不计算占比；或占比的分母用病害数而不是总面积

**标准 SQL**：
```sql
SELECT 
    SUM(p.area) AS total_area,
    SUM(CASE WHEN b.problem_type = '泛油' THEN p.area ELSE 0 END) AS fanyou_area,
    ROUND(SUM(CASE WHEN b.problem_type = '泛油' THEN p.area ELSE 0 END) * 100.0 / NULLIF(SUM(p.area), 0), 1) AS fanyou_area_ratio
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = '0' AND r.del_flag = '0'
  AND r.short_name = '宝安大道西乡段'
  AND p.area IS NOT NULL
```

**正确答案**：
- 整条路线病害总面积：**56241.20 ㎡**
- 泛油总面积：**569.78 ㎡**
- 泛油面积占比：**1.0%**
- （泛油有 534 处，数量占 58.6%，但面积只占 1.0%——因为泛油平均面积只有 1.07㎡，都是小病害）

---

## 验证要点

用这套测试集验证 NL2SQL 时，逐项核对：

| 检查项 | 说明 | 对应题目 |
|---|---|---|
| 分别统计 | "分别/各有多少"必须返回多列，不能只返回总数 | Q1, Q4, Q10, Q14 |
| code 翻译 | disease_categories_type / patrol_type 必须翻译成中文，不能输出 code | Q2, Q13 |
| GROUP BY | "前N种/分布/分别"必须 GROUP BY，不能退化成全表聚合 | Q3, Q15 |
| JSON 字段 | confidence 必须用 JSON_UNQUOTE(JSON_EXTRACT(...))，不能写 p.confidence | Q5, Q11 |
| 列表查询 | "有哪些/列出"必须 SELECT 具体字段 + ORDER BY + LIMIT，不能 COUNT(*) | Q5, Q9, Q11 |
| 多条件完整 | 所有 WHERE 条件必须保留，不能遗漏 | Q5, Q11 |
| 路线名正确 | "宝安中心--恒明珠"不能写成"恒明珠段" | Q2, Q4, Q5, Q8, Q9, Q11, Q14 |
| 巡检表字段 | 用 create_time，不能用不存在的 start_time | Q6, Q13 |
| 工单左连接 | 已转工单用 LEFT JOIN ai_work_order，不能用 INNER JOIN | Q7 |
| 密度计算 | 每公里病害数 = 病害数 / 路线长度，不能只返回病害数 | Q12 |
| 子查询 | Top1 类型 + 总数需要子查询，不能只返回一个 | Q8, Q15 |
| 空结果处理 | 结果为 0 时返回"0条/0%"，不能编造原因 | Q1(无图片0), Q7(工单0) |
| 起止桩号相减 | distance字段不可靠时，必须解析K1+000字符串用结束桩号减起始桩号 | Q16, Q17, Q18, Q24 |
| 桩号字符串解析 | K5+100 = 5*1000+100，用SUBSTRING_INDEX解析，不能直接当数字 | Q16, Q17, Q18, Q24 |
| 病害密度计算 | 每公里病害数 = 病害数 / 路线长度，不能只返回病害数 | Q12, Q18 |
| 环比增长率 | (本期-上期)/上期*100，不能只返回两期数量 | Q19 |
| 占比计算 | 部分/总数*100，需要先算总数再算部分 | Q20, Q21, Q22, Q25 |
| 子查询求平均 | WHERE里不能用聚合函数，面积>平均面积需要子查询 | Q21 |
| 数量面积倒挂 | 数量最多≠面积最大，需要分别计算数量占比和面积占比 | Q22 |
| 平均巡检间隔 | 天数跨度/(次数-1)，不是天数/次数；用create_time不用start_time | Q23 |
| 平均间距 | 路线长度(米)/病害数，单位是米不是公里 | Q24 |
