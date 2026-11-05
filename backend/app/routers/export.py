from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from ..config import settings
from ..services.db import DatabaseError, db_manager
from ..services.exports import result_to_csv
from ..services.logging_service import log_error
from ..services.sql_engine import apply_row_limit
from ..store import get_result


router = APIRouter(prefix="/api", tags=["export"])


def _expand_result(result: dict) -> dict:
    """导出前把结果显示用的 LIMIT 换成导出上限，重新取一次全量。

    界面只显示前几百条是为了不卡，但用户下载 CSV 要的是全部数据。
    """
    sql = result.get("sql")
    if not sql:
        return result
    kind = (result.get("lineage") or {}).get("db") or "mysql"
    try:
        expanded = db_manager.query(kind, apply_row_limit(sql, settings.sql_export_max_rows))
    except DatabaseError as exc:
        # 取全量失败时退回已缓存的结果，至少能下载到界面上那些行。
        log_error("export", "expand_failed", exc, result_id=str(result.get("id")))
        return result
    return {**result, "columns": expanded["columns"], "rows": expanded["rows"]}


@router.get("/export")
def export(result_id: str) -> Response:
    result = get_result(result_id)
    if not result:
        raise HTTPException(status_code=404, detail="结果不存在或已过期")
    result = _expand_result(result)
    filename, content = result_to_csv(result)
    # 中文文件名必须用 RFC 5987 编码：HTTP 头按 latin-1 编，"病害明细.csv" 会直接 500
    # （用户看到的就是"下载失败"）。ASCII 回退名 + filename* 给浏览器挑。
    ascii_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filename) or "export.csv"
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
            )
        },
    )
