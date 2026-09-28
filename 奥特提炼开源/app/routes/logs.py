from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from app.logging_setup import LOG_PATH

router = APIRouter(tags=["logs"])


@router.get("/api/logs/export")
async def export_logs():
    if not LOG_PATH.exists():
        return JSONResponse({"error_code": "log_not_found"}, status_code=404)
    return FileResponse(
        path=str(LOG_PATH),
        filename="app.log",
        media_type="application/octet-stream",
    )
