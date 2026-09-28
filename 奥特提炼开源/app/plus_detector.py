from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from app.config import ConfigStore
from app.models import JobRecord, SessionEntry
from app.session_cache import SessionCache


class PollError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        seg = token.split(".")[1]
        pad = "=" * (-len(seg) % 4)
        raw = base64.urlsafe_b64decode(seg + pad)
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def plan_from_at(access_token: str) -> str:
    """从 AT (JWT) payload 解析 plan。返回 'plus' | 'free' | ''（未知）。"""
    payload = _decode_jwt_payload(access_token or "")
    if not payload:
        return ""
    auth = payload.get("https://api.openai.com/auth")
    plan = ""
    if isinstance(auth, dict):
        plan = str(auth.get("chatgpt_plan_type") or "").lower()
    if not plan:
        plan = str(payload.get("chatgpt_plan_type") or "").lower()
    if "plus" in plan:
        return "plus"
    if plan in ("free", "basic"):
        return "free"
    return ""


class PlusDetector:
    def __init__(self, config: ConfigStore, session_cache: SessionCache) -> None:
        self.config = config
        self.session_cache = session_cache

    async def poll_plus(self, job: JobRecord, entry: SessionEntry) -> str:
        cfg = self.config.get()
        for _ in range(cfg.poll_budget):
            await asyncio.sleep(cfg.poll_interval_seconds)
            result = await self.check_plan(job, entry)
            if result == "plus":
                return "plus"
        return "free"

    async def check_plan(self, job: JobRecord, entry: SessionEntry) -> str:
        # AT 模式（无 cookies）：直接从 JWT 解析 plan
        if entry.access_token and not entry.cookies:
            plan = plan_from_at(entry.access_token)
            if plan:
                return plan
            return "pending"
        return "pending"
