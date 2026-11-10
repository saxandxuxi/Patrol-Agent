# NL2SQL 高难度问题答案集

> 日期：2026-09-16
> 定位：用于验证 NL2SQL（含 Wren 路径）处理复杂查询能力的测试集。所有问题均为多条件组合、多表关联、复杂聚合类型，避开简单的"XX有多少处病害"。
> 数据库：MySQL（new_four_road_base_ai）
> 关联文档：[Wren集成Bug排查报告.md](./Wren集成Bug排查报告.md)

---

## 表结构速查

| 表 | 别名 | 关键字段 |
|---|---|---|
| ai_problem | p | id, routes_id, sections_id, pile_distance(米), pile_number, area, disease_length, damage_type(code), collection_time, del_flag, problem_url, pic_position(JSON含confidence), device_code, up_or_down(1上行/2下行), patrol_id |
| basic_facilities_routes | r | id, code, short_name, full_name, distance(km), del_flag |
| basic_facilities_sections | s | id, route_id, start_pile, end_pile, surface_type, tech_grade, lane_count, surface_width, del_flag |
| ai_problem_base | b | problem_code, problem_type(名称), disease_categories_type(0路面/5路基/10沿路设施/15交安事件), del_flag |
| ai_maintain_patrol | t | id, patrol_number, patrol_type(0定期/5日常/10专项/15人工复核/20重点), patrol_status, start_time, end_time, del_flag |
| ai_work_order | w | id, problem_id, work_order_status, create_time, del_flag |

---

## 高难度问题列表（20 题）

---

### Q1：路线 + 桩号区间 + Top1 病害类型 + 总数

**问题**：宝安大道西乡段 K1+000 至 K3+000 路段中，数量最多的病害是哪种？该路段病害总数是多少？

**难点**：
- 路线 + 桩号区间双条件
- 需要同时返回 Top1 病害类型（GROUP BY + ORDER BY + LIMIT 1）和总数（COUNT(*)）
- 病害类型需要关联字典表转名称
- 字典表 code 重复，需去重否则数量翻倍

**标准 SQL**：
```sql
SELECT 
    b.problem_type AS top_damage_type,
    COUNT(*) AS top_damage_count,
    (SELECT COUNT(*) FROM ai_problem p2 
     JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
     WHERE p2.del_flag = 0 AND r2.del_flag = 0 
       AND r2.short_name = '宝安大道西乡段' 
       AND p2.pile_distance BETWEEN 1000 AND 3000) AS total_count
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND p.pile_distance BETWEEN 1000 AND 3000
GROUP BY b.problem_type
ORDER BY top_damage_count DESC
LIMIT 1
```

**预期结果格式**：top_damage_type（如"泛油"）、top_damage_count、total_count

---

### Q2：路线 + 时间范围 + 病害大类分布 + 占比

**问题**：恒明珠段 2025 年 7 月采集的病害中，各大类（路面/路基/沿路设施/交安事件）分别有多少？各占百分之多少？

**难点**：
- 路线 + 时间范围双条件
- 病害大类需要用 CASE WHEN 翻译 code（0/5/10/15）
- 需要计算每个大类的占比（数量 / 总数 * 100）
- 总数需要子查询或窗口函数

**标准 SQL**：
```sql
SELECT 
    CASE b.disease_categories_type 
        WHEN '0' THEN '路面病害' 
        WHEN '5' THEN '路基病害' 
        WHEN '10' THEN '沿路设施' 
        WHEN '15' THEN '交安事件' 
        ELSE CONCAT('其他(', b.disease_categories_type, ')') 
    END AS category_name,
    COUNT(*) AS count,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM ai_problem p2 
        JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id 
        WHERE p2.del_flag = 0 AND r2.del_flag = 0 
          AND r2.short_name = '恒明珠段' 
          AND p2.collection_time >= '2025-07-01' 
          AND p2.collection_time < '2025-08-01'), 1) AS percentage
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type, disease_categories_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
  AND p.collection_time >= '2025-07-01' AND p.collection_time < '2025-08-01'
GROUP BY b.disease_categories_type
ORDER BY count DESC
```

---

### Q3：数值阈值 + JSON 字段（置信度）+ 有图片 + 列表

**问题**：宝安大道西乡段面积大于 5 平方米、AI 识别置信度低于 0.7、且没有采集图片的病害有哪些？列出桩号、面积、置信度。

**难点**：
- 数值阈值（area > 5）
- JSON 字段提取（confidence 从 pic_position JSON 里取，需要 CAST）
- 反向条件（没有图片：problem_url IS NULL OR problem_url = ''）
- 列表查询（需要 SELECT 具体字段，不是 COUNT）
- 三个条件同时满足，容易丢条件

**标准 SQL**：
```sql
SELECT 
    p.pile_number AS 桩号,
    p.area AS 面积,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) AS 置信度,
    p.problem_number AS 病害编号
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND p.area > 5
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) < 0.7
  AND (p.problem_url IS NULL OR p.problem_url = '')
ORDER BY p.area DESC
LIMIT 500
```

---

### Q4：两条路线对比 + 同病害类型 + 差值

**问题**：宝安大道西乡段和恒明珠段的泛油数量分别是多少？相差多少？哪条路更多？

**难点**：
- 两条路线对比（需要分别统计再合并）
- 病害类型过滤（泛油，需关联字典）
- 需要计算差值（一条路的数量 - 另一条路的数量）
- 需要判断哪条路更多
- 不能只返回一条路的数量（常见错误：只统计第一条路）

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) AS 西乡段泛油数,
    SUM(CASE WHEN r.short_name = '恒明珠段' THEN 1 ELSE 0 END) AS 恒明珠段泛油数,
    ABS(SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) 
      - SUM(CASE WHEN r.short_name = '恒明珠段' THEN 1 ELSE 0 END)) AS 差值,
    CASE 
        WHEN SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) 
           > SUM(CASE WHEN r.short_name = '恒明珠段' THEN 1 ELSE 0 END) THEN '西乡段更多'
        WHEN SUM(CASE WHEN r.short_name = '宝安大道西乡段' THEN 1 ELSE 0 END) 
           < SUM(CASE WHEN r.short_name = '恒明珠段' THEN 1 ELSE 0 END) THEN '恒明珠段更多'
        ELSE '一样多'
    END AS 比较结果
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name IN ('宝安大道西乡段', '恒明珠段')
  AND b.problem_type = '泛油'
```

---

### Q5：巡检任务关联 + 路线 + 巡检类型 + 任务数和病害数

**问题**：宝安大道西乡段 2025 年 8 月有多少次定期巡查任务？这些任务共发现了多少处病害？

**难点**：
- 需要从巡检表（ai_maintain_patrol）统计任务数
- 需要从病害表（ai_problem）统计病害数
- 两表通过 patrol_id 关联
- 巡检类型过滤（patrol_type = '0' 定期巡查）
- 路线条件需要通过病害表回连路线（巡检表没有路线字段）
- 任务数需要 COUNT(DISTINCT t.id)，不能 COUNT(*)（一个任务有多条病害）

**标准 SQL**：
```sql
SELECT 
    COUNT(DISTINCT t.id) AS 定期巡查任务数,
    COUNT(p.id) AS 发现病害数
FROM ai_maintain_patrol t
JOIN ai_problem p ON p.patrol_id = t.id
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE t.del_flag = 0 AND p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND t.patrol_type = '0'
  AND t.start_time >= '2025-08-01' AND t.start_time < '2025-09-01'
```

---

### Q6：空间分段（前N米/后N米）+ 病害数量对比

**问题**：恒明珠段前 500 米和后 500 米的病害数量分别是多少？

**难点**：
- 空间分段需要知道路段的起点桩号和终点桩号
- "前500米"= 路段起点 ~ 起点+500米
- "后500米"= 终点-500米 ~ 终点
- 需要关联路段表（basic_facilities_sections）获取起止桩号
- 一条路线可能有多个路段，需要取最小起点和最大终点
- 桩号用 pile_distance（米数），不是 pile_number（文本）

**标准 SQL**：
```sql
WITH route_range AS (
    SELECT MIN(s.start_pile) AS route_start, MAX(s.end_pile) AS route_end
    FROM basic_facilities_sections s
    JOIN basic_facilities_routes r ON s.route_id = r.id
    WHERE s.del_flag = 0 AND r.del_flag = 0
      AND r.short_name = '恒明珠段'
)
SELECT 
    SUM(CASE WHEN p.pile_distance >= (SELECT route_start FROM route_range) 
              AND p.pile_distance < (SELECT route_start FROM route_range) + 500 
             THEN 1 ELSE 0 END) AS 前500米病害数,
    SUM(CASE WHEN p.pile_distance >= (SELECT route_end FROM route_range) - 500 
              AND p.pile_distance <= (SELECT route_end FROM route_range) 
             THEN 1 ELSE 0 END) AS 后500米病害数
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
```

---

### Q7：Top N 病害类型 + 各自平均面积 + 路线条件

**问题**：宝安大道西乡段数量最多的前 3 种病害分别是什么？各自的平均面积是多少？

**难点**：
- Top N（GROUP BY + ORDER BY COUNT DESC + LIMIT 3）
- 需要同时返回每种病害的平均面积（AVG(area)）
- 病害类型需关联字典转名称
- 平均面积需要处理 NULL（area 可能为 NULL）

**标准 SQL**：
```sql
SELECT 
    b.problem_type AS 病害类型,
    COUNT(*) AS 数量,
    ROUND(AVG(p.area), 2) AS 平均面积
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
GROUP BY b.problem_type
ORDER BY 数量 DESC
LIMIT 3
```

---

### Q8：工单关联 + 已转工单比例 + 路线 + 病害类型

**问题**：恒明珠段的泛油病害中，已经转成工单的有多少？占比多少？

**难点**：
- 需要关联工单表（ai_work_order）判断是否已转工单
- 工单通过 problem_id 关联病害
- 需要计算已转工单数量和占比（已转 / 总数 * 100）
- 病害类型过滤（泛油）
- 左连接（不是所有病害都有工单）

**标准 SQL**：
```sql
SELECT 
    COUNT(*) AS 泛油总数,
    SUM(CASE WHEN w.id IS NOT NULL THEN 1 ELSE 0 END) AS 已转工单数,
    ROUND(SUM(CASE WHEN w.id IS NOT NULL THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) AS 已转工单占比
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
LEFT JOIN ai_work_order w ON w.problem_id = p.id AND w.del_flag = 0
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
  AND b.problem_type = '泛油'
```

---

### Q9：上下行分布 + 路线 + 桩号区间 + 各类型数量

**问题**：宝安大道西乡段 K2+000 至 K4+000 路段中，上行和下行各有多少处病害？上下行数量最多的病害类型分别是什么？

**难点**：
- 上下行字段（up_or_down: 1上行/2下行）
- 路线 + 桩号区间双条件
- 需要分别统计上行和下行的总数
- 需要分别找出上行和下行数量最多的病害类型（需要子查询或窗口函数）
- 病害类型需关联字典

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN p.up_or_down = 1 THEN 1 ELSE 0 END) AS 上行总数,
    SUM(CASE WHEN p.up_or_down = 2 THEN 1 ELSE 0 END) AS 下行总数,
    (SELECT b2.problem_type FROM ai_problem p2 
     LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b2 ON p2.damage_type = b2.problem_code
     WHERE p2.del_flag = 0 AND p2.up_or_down = 1 
       AND p2.routes_id IN (SELECT id FROM basic_facilities_routes WHERE short_name = '宝安大道西乡段' AND del_flag = 0)
       AND p2.pile_distance BETWEEN 2000 AND 4000
     GROUP BY b2.problem_type ORDER BY COUNT(*) DESC LIMIT 1) AS 上行最多病害类型,
    (SELECT b2.problem_type FROM ai_problem p2 
     LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b2 ON p2.damage_type = b2.problem_code
     WHERE p2.del_flag = 0 AND p2.up_or_down = 2 
       AND p2.routes_id IN (SELECT id FROM basic_facilities_routes WHERE short_name = '宝安大道西乡段' AND del_flag = 0)
       AND p2.pile_distance BETWEEN 2000 AND 4000
     GROUP BY b2.problem_type ORDER BY COUNT(*) DESC LIMIT 1) AS 下行最多病害类型
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND p.pile_distance BETWEEN 2000 AND 4000
```

---

### Q10：月度趋势 + 路线 + 病害类型 + 月均数量

**问题**：宝安大道西乡段 2025 年下半年（7-12月）泛油病害每月有多少？月均多少？哪个月最多？

**难点**：
- 按月分组（DATE_FORMAT 或 YEAR/MONTH）
- 时间范围（2025-07-01 ~ 2026-01-01）
- 病害类型过滤（泛油）
- 需要计算月均（总数 / 月数）
- 需要找出最多的月份（ORDER BY + LIMIT 1，或窗口函数）
- 返回多行（每月一行）+ 汇总行

**标准 SQL**：
```sql
SELECT 
    DATE_FORMAT(p.collection_time, '%Y-%m') AS 月份,
    COUNT(*) AS 泛油数量
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND b.problem_type = '泛油'
  AND p.collection_time >= '2025-07-01' AND p.collection_time < '2026-01-01'
GROUP BY DATE_FORMAT(p.collection_time, '%Y-%m')
ORDER BY 月份
```

（月均和最多月份由应用层基于上述结果计算，或用以下汇总 SQL）

```sql
SELECT 
    COUNT(*) AS 下半年总数,
    ROUND(COUNT(*) / 6.0, 1) AS 月均数量,
    (SELECT DATE_FORMAT(p2.collection_time, '%Y-%m') 
     FROM ai_problem p2 
     JOIN basic_facilities_routes r2 ON p2.routes_id = r2.id
     LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b2 ON p2.damage_type = b2.problem_code
     WHERE p2.del_flag = 0 AND r2.del_flag = 0 
       AND r2.short_name = '宝安大道西乡段' AND b2.problem_type = '泛油'
       AND p2.collection_time >= '2025-07-01' AND p2.collection_time < '2026-01-01'
     GROUP BY DATE_FORMAT(p2.collection_time, '%Y-%m') 
     ORDER BY COUNT(*) DESC LIMIT 1) AS 最多月份
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND b.problem_type = '泛油'
  AND p.collection_time >= '2025-07-01' AND p.collection_time < '2026-01-01'
```

---

### Q11：设备号 + 时间 + 该设备采集的病害类型分布

**问题**：设备号 102367594567 在 2025 年 8 月采集的病害中，各类型分别有多少？占比多少？

**难点**：
- 设备号过滤（device_code，注意不是 vehicle_id）
- 时间范围
- 病害类型分布（GROUP BY + 占比计算）
- 设备号是字符串，需要加引号
- 常见错误：把 device_code 写成 vehicle_id

**标准 SQL**：
```sql
SELECT 
    b.problem_type AS 病害类型,
    COUNT(*) AS 数量,
    ROUND(COUNT(*) * 100.0 / (SELECT COUNT(*) FROM ai_problem p2 
        WHERE p2.del_flag = 0 AND p2.device_code = '102367594567'
          AND p2.collection_time >= '2025-08-01' AND p2.collection_time < '2025-09-01'), 1) AS 占比
FROM ai_problem p
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0
  AND p.device_code = '102367594567'
  AND p.collection_time >= '2025-08-01' AND p.collection_time < '2025-09-01'
GROUP BY b.problem_type
ORDER BY 数量 DESC
LIMIT 500
```

---

### Q12：路段关联 + 路面类型 + 病害数量 + 平均面积

**问题**：宝安大道西乡段沥青路面的路段上共有多少处病害？平均面积多大？

**难点**：
- 需要关联路段表（basic_facilities_sections）获取路面类型
- 病害通过 sections_id 关联路段
- 路面类型过滤（surface_type，如"沥青混凝土"）
- 路线条件需要同时过滤路线表和路段表
- 一条路线有多个路段，需要 JOIN 路线表和路段表

**标准 SQL**：
```sql
SELECT 
    COUNT(*) AS 病害总数,
    ROUND(AVG(p.area), 2) AS 平均面积
FROM ai_problem p
JOIN basic_facilities_sections s ON p.sections_id = s.id
JOIN basic_facilities_routes r ON s.route_id = r.id
WHERE p.del_flag = 0 AND s.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND s.surface_type LIKE '%沥青%'
```

---

### Q13：面积最大的病害 + 详细信息 + 路线 + 时间

**问题**：恒明珠段 2025 年采集的面积最大的病害是哪条？面积多大？在哪个桩号？什么类型？

**难点**：
- Top1 数值指标（ORDER BY area DESC + LIMIT 1）
- 需要返回详细信息（桩号、类型、面积、采集时间）
- 路线 + 时间双条件
- 病害类型需关联字典
- 不能退化成 COUNT(*)（常见错误：问"最大"答成总数）

**标准 SQL**：
```sql
SELECT 
    p.problem_number AS 病害编号,
    p.pile_number AS 桩号,
    p.area AS 面积,
    b.problem_type AS 病害类型,
    p.collection_time AS 采集时间,
    p.problem_url AS 图片地址
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
  AND p.collection_time >= '2025-01-01' AND p.collection_time < '2026-01-01'
  AND p.area IS NOT NULL
ORDER BY p.area DESC
LIMIT 1
```

---

### Q14：有图片 vs 无图片 + 数量对比 + 路线 + 病害类型

**问题**：宝安大道西乡段的泛油病害中，有图片的和没有图片的分别有多少？

**难点**：
- 有图片判断（problem_url IS NOT NULL AND problem_url <> ''）
- 无图片判断（problem_url IS NULL OR problem_url = ''）
- 需要分别统计（CASE WHEN）
- 病害类型过滤（泛油）
- 常见错误：只统计有图片的，漏掉无图片的

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN p.problem_url IS NOT NULL AND p.problem_url <> '' THEN 1 ELSE 0 END) AS 有图片数,
    SUM(CASE WHEN p.problem_url IS NULL OR p.problem_url = '' THEN 1 ELSE 0 END) AS 无图片数,
    COUNT(*) AS 总数
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND b.problem_type = '泛油'
```

---

### Q15：里程桩点查询 + 路线 + 桩号区间 + 数量

**问题**：宝安大道西乡段 K0+000 至 K5+000 之间有多少个里程桩点？

**难点**：
- 主体是里程桩点（basic_facilities_sections_pile_detail），不是病害
- 需要关联路段表再关联路线表（pile_detail → sections → routes）
- 桩号区间过滤（pile_detail 的桩号字段）
- 常见错误：用病害表 ai_problem 答成病害数（主体识别错误）

**标准 SQL**：
```sql
SELECT COUNT(*) AS 里程桩点数
FROM basic_facilities_sections_pile_detail d
JOIN basic_facilities_sections s ON d.sections_id = s.id
JOIN basic_facilities_routes r ON s.route_id = r.id
WHERE d.del_flag = 0 AND s.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND d.pile_distance BETWEEN 0 AND 5000
```

---

### Q16：多病害类型 + 路线 + 各类型数量 + 总数

**问题**：恒明珠段的泛油、龟裂、坑槽三种病害分别有多少？总共多少？

**难点**：
- 多病害类型 IN 过滤（泛油、龟裂、坑槽）
- 需要分别统计每种的数量（CASE WHEN 或 GROUP BY）
- 需要总数
- 病害类型需关联字典
- 常见错误：只统计第一种，漏掉其他两种

**标准 SQL**：
```sql
SELECT 
    SUM(CASE WHEN b.problem_type = '泛油' THEN 1 ELSE 0 END) AS 泛油数,
    SUM(CASE WHEN b.problem_type = '龟裂' THEN 1 ELSE 0 END) AS 龟裂数,
    SUM(CASE WHEN b.problem_type = '坑槽' THEN 1 ELSE 0 END) AS 坑槽数,
    COUNT(*) AS 三种总数
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
  AND b.problem_type IN ('泛油', '龟裂', '坑槽')
```

---

### Q17：平均病害长度 + 路线 + 病害类型 + 上下行分别统计

**问题**：宝安大道西乡段的龟裂病害中，上行和下行的平均病害长度分别是多少？

**难点**：
- 平均长度（AVG(disease_length)）
- 上下行分别统计（CASE WHEN + up_or_down）
- 病害类型过滤（龟裂）
- disease_length 可能为 NULL，需要处理
- 常见错误：只统计总体平均，不分上下行

**标准 SQL**：
```sql
SELECT 
    ROUND(AVG(CASE WHEN p.up_or_down = 1 THEN p.disease_length END), 2) AS 上行平均长度,
    ROUND(AVG(CASE WHEN p.up_or_down = 2 THEN p.disease_length END), 2) AS 下行平均长度,
    ROUND(AVG(p.disease_length), 2) AS 总体平均长度,
    SUM(CASE WHEN p.up_or_down = 1 THEN 1 ELSE 0 END) AS 上行数量,
    SUM(CASE WHEN p.up_or_down = 2 THEN 1 ELSE 0 END) AS 下行数量
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND b.problem_type = '龟裂'
  AND p.disease_length IS NOT NULL
```

---

### Q18：巡检任务 + 病害数量 + 按巡检类型分组 + 路线

**问题**：宝安大道西乡段 2025 年各类型巡检任务（定期/日常/专项/人工复核/重点）分别有多少次？各发现了多少病害？

**难点**：
- 按巡检类型分组（patrol_type: 0/5/10/15/20）
- 巡检类型需要用 CASE WHEN 翻译名称
- 任务数需要 COUNT(DISTINCT t.id)
- 病害数需要 COUNT(p.id)
- 路线条件通过病害表回连
- 时间范围

**标准 SQL**：
```sql
SELECT 
    CASE t.patrol_type 
        WHEN '0' THEN '定期巡查' 
        WHEN '5' THEN '日常巡查' 
        WHEN '10' THEN '专项巡查' 
        WHEN '15' THEN '人工复核' 
        WHEN '20' THEN '重点巡查' 
        ELSE CONCAT('其他(', t.patrol_type, ')') 
    END AS 巡检类型,
    COUNT(DISTINCT t.id) AS 任务次数,
    COUNT(p.id) AS 发现病害数
FROM ai_maintain_patrol t
JOIN ai_problem p ON p.patrol_id = t.id
JOIN basic_facilities_routes r ON p.routes_id = r.id
WHERE t.del_flag = 0 AND p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '宝安大道西乡段'
  AND t.start_time >= '2025-01-01' AND t.start_time < '2026-01-01'
GROUP BY t.patrol_type
ORDER BY 任务次数 DESC
```

---

### Q19：高置信度 + 大面积 + 路线 + 列表 + 排序

**问题**：恒明珠段 AI 识别置信度大于 0.9 且面积大于 3 平方米的病害有哪些？按面积从大到小排列，列出前 10 条。

**难点**：
- JSON 字段提取（confidence）
- 双数值阈值（confidence > 0.9 AND area > 3）
- 列表查询（SELECT 具体字段）
- 排序（ORDER BY area DESC）
- LIMIT 10
- 常见错误：条件丢失（只保留一个条件）

**标准 SQL**：
```sql
SELECT 
    p.problem_number AS 病害编号,
    p.pile_number AS 桩号,
    p.area AS 面积,
    CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) AS 置信度,
    b.problem_type AS 病害类型,
    p.collection_time AS 采集时间
FROM ai_problem p
JOIN basic_facilities_routes r ON p.routes_id = r.id
LEFT JOIN (SELECT DISTINCT problem_code, problem_type FROM ai_problem_base WHERE problem_type IS NOT NULL) b 
    ON p.damage_type = b.problem_code
WHERE p.del_flag = 0 AND r.del_flag = 0
  AND r.short_name = '恒明珠段'
  AND CAST(JSON_UNQUOTE(JSON_EXTRACT(p.pic_position, '$.confidence')) AS DECIMAL(10,6)) > 0.9
  AND p.area > 3
ORDER BY p.area DESC
LIMIT 10
```

---

### Q20：路线总长度 + 病害密度（每公里多少处）+ 多路线对比

**问题**：宝安大道西乡段和恒明珠段的路线长度分别是多少公里？每公里各有多少处病害？

**难点**：
- 需要从路线表获取长度（distance，单位 km）
- 需要统计病害数
- 需要计算病害密度（病害数 / 路线长度）
- 两条路线对比
- 单位换算（distance 已经是 km，不需要换算）
- 常见错误：忘记除以路线长度，直接返回病害数

**标准 SQL**：
```sql
SELECT 
    r.short_name AS 路线名称,
    r.distance AS 路线长度_km,
    COUNT(p.id) AS 病害数,
    ROUND(COUNT(p.id) / r.distance, 1) AS 每公里病害数
FROM basic_facilities_routes r
LEFT JOIN ai_problem p ON p.routes_id = r.id AND p.del_flag = 0
WHERE r.del_flag = 0
  AND r.short_name IN ('宝安大道西乡段', '恒明珠段')
GROUP BY r.id, r.short_name, r.distance
ORDER BY 每公里病害数 DESC
```

---

## 难度分类汇总

| 难度类型 | 题目编号 | 数量 |
|---|---|---|
| 多条件组合（路线+桩号+时间+病害+数值） | Q1, Q2, Q3, Q9, Q10, Q11, Q15, Q19 | 8 |
| 复杂聚合（TopN/占比/对比/趋势/密度） | Q1, Q2, Q4, Q7, Q10, Q16, Q17, Q20 | 8 |
| 多表关联（3表以上JOIN） | Q1, Q2, Q5, Q6, Q8, Q12, Q15, Q18 | 8 |
| JSON字段（confidence） | Q3, Q19 | 2 |
| 计算字段（有图片/上下行） | Q3, Q9, Q14, Q17 | 4 |
| 空间分段（前N米/后N米） | Q6 | 1 |
| 跨表主体（里程桩点/巡检/工单） | Q5, Q8, Q15, Q18 | 4 |
| 子查询/窗口函数 | Q1, Q2, Q4, Q9, Q10 | 5 |

---

## 验证要点

用这套测试集验证 NL2SQL 时，重点关注：

1. **条件完整性**：所有 WHERE 条件是否都保留了（尤其是多条件组合题，容易丢条件）
2. **主体正确性**：是否用对了表（如 Q15 里程桩点不能用病害表）
3. **关联正确性**：JOIN 关系是否正确（如 Q5 巡检→病害→路线）
4. **聚合正确性**：TopN/占比/对比是否正确（如 Q4 不能只返回一条路的数量）
5. **JSON字段**：confidence 是否正确提取（如 Q3、Q19）
6. **字典关联**：病害类型是否关联了字典表且去重（避免数量翻倍）
7. **del_flag**：所有表是否都加了 del_flag = 0
8. **LIMIT**：列表查询是否加了 LIMIT
