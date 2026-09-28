from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["results"])


@router.get("/api/results/plus")
async def results_plus(request: Request):
    content, count = await request.app.state.results.read_result("plus")
    return {"content": content, "count": count}


@router.get("/api/results/done")
async def results_done(request: Request):
    content, count = await request.app.state.results.read_result("done")
    return {"content": content, "count": count}


@router.get("/api/results/free")
async def results_free(request: Request):
    # Legacy endpoint — UI không còn dùng
    content, count = await request.app.state.results.read_result("free")
    return {"content": content, "count": count}


@router.get("/api/stats")
async def stats(request: Request):
    """站点统计：总成功/失败 + 24 小时成功/失败。"""
    try:
        data = await request.app.state.db.stats_summary()
    except Exception:
        data = {"done_total": 0, "fail_total": 0, "done_24h": 0, "fail_24h": 0, "success_rate": 0.0}
    # 补充累计出链文件行数（重启/清理后仍保留）
    try:
        _, done_lines = await request.app.state.results.read_result("done")
        data["done_file_lines"] = int(done_lines)
    except Exception:
        data["done_file_lines"] = 0
    return data
