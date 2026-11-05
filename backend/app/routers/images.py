from __future__ import annotations

import base64

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..services.db import db_manager


router = APIRouter(prefix="/api", tags=["images"])


class ImageBatchRequest(BaseModel):
    paths: list[str] = []


@router.get("/image")
def image(path: str) -> Response:
    try:
        status, content_type, body = db_manager.fetch_seaweedfs(path)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"图片服务不可用：{exc}") from exc
    if status == 404:
        raise HTTPException(status_code=404, detail="图片不存在")
    if status >= 400 or not body:
        raise HTTPException(status_code=502, detail=f"SeaweedFS 返回状态 {status}")
    return Response(content=body, media_type=content_type)


@router.post("/images/batch")
def images_batch(payload: ImageBatchRequest) -> dict:
    """一次取回多张图（data URL），给前端的图片墙用。

    每张图单独请求的话是一次 HTTP + 一次 SSH 往返（`_run` 串行加锁），42 张要十几秒；
    这里一条远程命令把这一批图全取回来（命中本地缓存的连远端都不去）。
    前端按 data URL 直接渲染，不再一张一张拉。
    """
    paths = [str(item or "").strip() for item in payload.paths if str(item or "").strip()]
    if len(paths) > 60:
        paths = paths[:60]  # 单次最多 60 张（一张约 100~300KB，再多响应体会太大）
    try:
        fetched = db_manager.fetch_seaweedfs_many(paths)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"图片服务不可用：{exc}") from exc
    items: list[dict[str, str]] = []
    missing: list[str] = []
    for path in paths:
        status, content_type, body = fetched.get(path, (404, "", b""))
        if status == 200 and body:
            encoded = base64.b64encode(body).decode("ascii")
            items.append(
                {"path": path, "data": f"data:{content_type or 'image/png'};base64,{encoded}"}
            )
        else:
            missing.append(path)
    return {"items": items, "missing": missing}
