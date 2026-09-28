from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["bulk"])


def _jm(request: Request):
    return request.app.state.job_manager


@router.post("/api/jobs/stop-all")
async def stop_all(request: Request):
    return await _jm(request).stop_all()


@router.post("/api/jobs/pause")
async def pause_jobs(request: Request):
    return await _jm(request).pause_dispatch()


@router.post("/api/jobs/resume")
async def resume_jobs(request: Request):
    return await _jm(request).resume_dispatch()


@router.get("/api/jobs/pause-status")
async def pause_status(request: Request):
    return _jm(request).pause_status()


@router.post("/api/jobs/start-all-held")
async def start_all_held(request: Request):
    return await _jm(request).start_all_held()


@router.post("/api/jobs/rerun-failed")
async def rerun_failed(request: Request):
    return await _jm(request).rerun_failed()


@router.delete("/api/jobs/clear")
async def clear_jobs(
    request: Request,
    filter: str = Query(...),
    confirm: bool = Query(False),
):
    try:
        return await _jm(request).clear(filter, confirm=confirm)
    except ValueError:
        return JSONResponse({"error_code": "invalid_filter"}, status_code=400)
    except PermissionError:
        return JSONResponse({"error_code": "confirm_required"}, status_code=400)
