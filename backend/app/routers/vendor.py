from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


router = APIRouter(prefix="/vendor", tags=["vendor"])


def _plotly_bundle() -> Path | None:
    try:
        import plotly
    except Exception:
        return None
    path = Path(plotly.__file__).resolve().parent / "package_data" / "plotly.min.js"
    return path if path.exists() else None


@router.get("/plotly.min.js")
def plotly_bundle() -> FileResponse:
    path = _plotly_bundle()
    if path is None:
        raise HTTPException(status_code=404, detail="plotly.min.js 不可用，请确认后端已安装 plotly。")
    return FileResponse(path, media_type="application/javascript")
