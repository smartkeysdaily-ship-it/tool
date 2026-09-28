from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.job_manager import ActionNotAllowed, JobNotFound

router = APIRouter(tags=["jobs"])


class SubmitBody(BaseModel):
    raw_text: str
    dispatch_mode: str = "default"
    skip_session_cache: bool | None = None


def _jm(request: Request):
    return request.app.state.job_manager


def _owns(request: Request, job_id: str) -> bool:
    """校验该 job 是否属于当前会话（未归属的旧 job 放行）。"""
    sid = getattr(request.state, "session_id", "")
    try:
        job = request.app.state.job_manager.get(job_id)
    except JobNotFound:
        return True  # 交给后续逻辑返回 404
    owner = getattr(job, "session_id", "") or ""
    return (not owner) or owner == sid


@router.get("/api/jobs/stream")
async def jobs_stream(request: Request):
    sse = request.app.state.sse
    sid = getattr(request.state, "session_id", "")

    async def gen():
        async for chunk in sse.subscribe(sid):
            if await request.is_disconnected():
                break
            yield chunk

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/jobs")
async def list_jobs(request: Request):
    sid = getattr(request.state, "session_id", "")
    jobs = _jm(request).list_public(sid)
    seq = request.app.state.sse.seq
    return JSONResponse(
        jobs,
        headers={
            "X-Jobs-Seq": str(seq),
            "Cache-Control": "no-store",
        },
    )


@router.post("/api/jobs/submit")
async def submit_jobs(body: SubmitBody, request: Request):
    sid = getattr(request.state, "session_id", "")
    result = await _jm(request).submit(
        body.raw_text,
        dispatch_mode=body.dispatch_mode,
        skip_cache=body.skip_session_cache,
        session_id=sid,
    )
    return result


@router.post("/api/jobs/{job_id}/start")
async def start_job(job_id: str, request: Request):
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    try:
        job = await _jm(request).start_job(job_id)
        return job.public_dict()
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    except ActionNotAllowed as e:
        return JSONResponse(
            {
                "error_code": "action_not_allowed",
                "current_state": e.state.value,
                "action": e.action,
            },
            status_code=409,
        )


@router.post("/api/jobs/{job_id}/stop")
async def stop_job(job_id: str, request: Request):
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    try:
        job = await _jm(request).stop_job(job_id)
        return job.public_dict()
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    except ActionNotAllowed as e:
        return JSONResponse(
            {
                "error_code": "action_not_allowed",
                "current_state": e.state.value,
                "action": e.action,
            },
            status_code=409,
        )


@router.post("/api/jobs/{job_id}/rerun")
async def rerun_job(job_id: str, request: Request):
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    try:
        return await _jm(request).rerun_job(job_id)
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    except ActionNotAllowed as e:
        return JSONResponse(
            {
                "error_code": "rerun_not_allowed_in_active_state"
                if e.action == "rerun"
                else "action_not_allowed",
                "current_state": e.state.value,
                "action": e.action,
            },
            status_code=409,
        )


@router.post("/api/jobs/{job_id}/check-plan")
async def check_plan(job_id: str, request: Request):
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    try:
        return await _jm(request).check_plan(job_id)
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    except ActionNotAllowed as e:
        return JSONResponse(
            {
                "error_code": "action_not_allowed",
                "current_state": e.state.value,
                "action": e.action,
            },
            status_code=409,
        )


@router.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str, request: Request):
    # reserved bulk path segments
    if job_id in {
        "clear",
        "stream",
        "submit",
        "stop-all",
        "start-all-held",
        "rerun-failed",
        "pause",
        "resume",
        "pause-status",
    }:
        return JSONResponse({"error_code": "invalid_job_id"}, status_code=400)
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    try:
        await _jm(request).remove_job(job_id)
        return {"deleted": True}
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)


@router.get("/api/jobs/{job_id}/qr.png")
async def job_qr(job_id: str, request: Request):
    if not _owns(request, job_id):
        return JSONResponse({"error_code": "qr_not_found"}, status_code=404)
    try:
        data = _jm(request).get_qr_png(job_id)
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    if not data:
        return JSONResponse({"error_code": "qr_not_found"}, status_code=404)
    return Response(content=data, media_type="image/png")


@router.get("/api/jobs/{job_id}")
async def job_detail(job_id: str, request: Request):
    try:
        job = _jm(request).get(job_id)
    except JobNotFound:
        return JSONResponse({"error_code": "job_not_found"}, status_code=404)
    return {
        "state": job.state.value,
        "timeline": [t.model_dump(mode="json") for t in job.timeline],
        "last_error": job.last_error,
        "cookies_summary": job.cookies_summary,
        "attempt_login": job.attempt_login,
        "attempt_pipeline": job.attempt_pipeline,
        "origin_job_id": job.origin_job_id,
        "email": job.email,
        "job_id": job.job_id,
    }
