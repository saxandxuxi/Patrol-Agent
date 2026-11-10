# 目前能答对的问题示例

> 存到 RAGFlow 做 few-shot 参考用。

---

西乡段泛油一共多少条？
答：534 处。
SQL：SELECT COUNT(*) FROM disease d JOIN route r ON ... JOIN damage_dict dd ON ... WHERE r.short_name = '宝安大道西乡段' AND dd.problem_type = '泛油'

---

西乡段所有病害的面积加起来有多大？
答：56241.20 ㎡。
SQL：SELECT SUM(p.area) FROM disease d JOIN route r ON ... WHERE r.short_name = '宝安大道西乡段'

---

宝安大道西乡段各病害类型的数量和面积分别是多少？
答：泛油 534 处，未识别 377 处。
SQL：SELECT dd.problem_type, COUNT(*) AS cnt, SUM(p.area) AS total_area FROM disease d JOIN route r ON ... JOIN damage_dict dd ON ... WHERE r.short_name = '宝安大道西乡段' GROUP BY dd.problem_type ORDER BY cnt DESC

---

宝安大道西乡段泛油病害占全部病害的百分之多少？
答：58.6%。
SQL：SELECT ROUND(SUM(CASE WHEN dd.problem_type = '泛油' THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 2) FROM disease d JOIN route r ON ... JOIN damage_dict dd ON ... WHERE r.short_name = '宝安大道西乡段'

---

西乡段病害密度是多少？
答：178.6 处/公里。
拆两步：先查病害总数 911，再查路线长度 5.10 km，然后 911 ÷ 5.10 = 178.6。
注意：路线长度用 route_length_km 字段，不要用 distance。

---

西乡段病害平均每隔多少米出现一处？
答：5.6 米/处。
拆两步：先查路线长度 5100 米，再查病害总数 911，然后 5100 ÷ 911 = 5.6。

---

西乡段和恒明珠的病害总数分别是多少？哪条更多？
答：西乡段 911 处，恒明珠 749 处，西乡段更多。
SQL：SELECT r.short_name, COUNT(*) AS cnt FROM disease d JOIN route r ON ... WHERE r.short_name IN ('宝安大道西乡段', '宝安中心--恒明珠') GROUP BY r.short_name

---

西乡段面积大于 5 平方米的病害有哪些？列出桩号、面积。
SQL：SELECT d.pile_number, d.area, dd.problem_type FROM disease d JOIN route r ON ... JOIN damage_dict dd ON ... WHERE r.short_name = '宝安大道西乡段' AND d.area > 5 ORDER BY d.area DESC

---

宝安大道西乡段的路线长度是多少公里？
答：5.10 公里。
SQL：SELECT r.route_length_km FROM route r WHERE r.short_name = '宝安大道西乡段'
注意：用 route_length_km 字段，不要用 distance。

---

西乡段 2026 年 7 月有多少次定期巡查任务？
答：10 次。
SQL：SELECT COUNT(DISTINCT t.id) FROM patrol t JOIN route r ON ... WHERE r.short_name = '西乡段' AND t.create_time BETWEEN '2026-07-01' AND '2026-07-31'
