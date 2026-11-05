from __future__ import annotations

import csv
import io
from typing import Any


def result_to_csv(result: dict[str, Any]) -> tuple[str, bytes]:
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in columns})
    # 结果里存的是 result_id，不是 id；以前写死导致每次下载都叫同一个名字、互相覆盖。
    key = result.get("result_id") or result.get("id") or "export"
    filename = f"巡检结果_{str(key)[:8]}.csv"
    return filename, buffer.getvalue().encode("utf-8-sig")
