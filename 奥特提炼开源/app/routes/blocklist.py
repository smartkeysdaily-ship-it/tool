from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter(tags=["blocklist"])


class AddBody(BaseModel):
    email: str
    notes: str | None = None


@router.get("/api/blocklist")
async def list_blocklist(request: Request):
    items = await request.app.state.blocklist.list_all()
    return [i.model_dump(mode="json") for i in items]


@router.post("/api/blocklist")
async def add_blocklist(body: AddBody, request: Request):
    try:
        entry = await request.app.state.blocklist.add_manual(body.email, body.notes)
        return entry.model_dump(mode="json")
    except LookupError:
        return JSONResponse({"error_code": "already_blocked"}, status_code=409)


@router.delete("/api/blocklist/{email}")
async def remove_blocklist(email: str, request: Request):
    ok = await request.app.state.blocklist.remove(email)
    if not ok:
        return JSONResponse({"error_code": "not_blocked"}, status_code=404)
    return {"deleted": True}
