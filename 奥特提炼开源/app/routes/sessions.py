from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from app.session_cache import SessionCacheError

router = APIRouter(tags=["sessions"])


@router.get("/api/session-cache")
async def list_sessions(request: Request):
    cfg = request.app.state.config.get()
    entries = request.app.state.session_cache.list_entries(
        buffer=cfg.session_cache_ttl_buffer_seconds
    )
    return [e.model_dump(mode="json") for e in entries]


@router.delete("/api/session-cache/{email}")
async def delete_session(email: str, request: Request):
    try:
        ok = request.app.state.session_cache.delete(email)
    except SessionCacheError as e:
        return JSONResponse({"error_code": e.code}, status_code=400)
    if not ok:
        return JSONResponse({"error_code": "session_not_found"}, status_code=404)
    return {"deleted": True, "email": email}


@router.delete("/api/session-cache")
async def delete_all_sessions(request: Request, confirm: bool = Query(False)):
    if not confirm:
        return JSONResponse({"error_code": "confirm_required"}, status_code=400)
    sc = request.app.state.session_cache
    entries = sc.list_entries()
    n = 0
    for e in entries:
        try:
            if sc.delete(e.email):
                n += 1
        except SessionCacheError:
            continue
    return {"deleted": n}
