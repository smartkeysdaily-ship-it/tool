"""扫码站点（masi.cc.cd）对接客户端。

提炼成功（Done）后，把支付链接自动提交到扫码站点，由工人扫码支付。
- 纯 CDK 认证：CDK 放在请求体 ticket 字段
- 必须带 User-Agent（前置 Cloudflare，缺 UA 会被 403 拦截）
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://masi.cc.cd"
USER_AGENT = "UpiPlusTool/1.0"


class ScanSiteClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, cdk: str = "") -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.cdk = (cdk or "").strip()
        self.log = logging.getLogger("plus_auto")

    def configure(self, base_url: str, cdk: str) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.cdk = (cdk or "").strip()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if extra:
            h.update(extra)
        return h

    async def health(self, timeout: float = 10.0) -> dict[str, Any]:
        """GET /health — 查在线工人数等。"""
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{self.base_url}/health", headers=self._headers())
            data = r.json() if r.status_code == 200 else {}
            if not isinstance(data, dict):
                data = {}
            data.setdefault("ok", r.status_code == 200)
            data["status"] = r.status_code
            return data
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 0, "error": str(exc)}

    async def redeem(self, cdk: str | None = None, timeout: float = 15.0) -> dict[str, Any]:
        """POST /api/tickets/redeem — 查 CDK 剩余额度。"""
        ticket = (cdk or self.cdk or "").strip()
        if not ticket:
            return {"ok": False, "error": "cdk_empty"}
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(
                    f"{self.base_url}/api/tickets/redeem",
                    json={"ticket": ticket},
                    headers=self._headers(),
                )
            data = r.json() if r.content else {}
            if not isinstance(data, dict):
                data = {}
            data["status"] = r.status_code
            return data
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 0, "error": str(exc)}

    async def submit_order(
        self,
        link: str,
        email: str = "",
        access_token: str = "",
        *,
        require_online_workers: bool = True,
        cdk: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """POST /api/scan-orders — 提交单条支付链接。

        返回 dict，至少含 ok / status；成功时含 order / order_secret。
        """
        ticket = (cdk or self.cdk or "").strip()
        if not ticket:
            return {"ok": False, "status": 0, "error": "cdk_empty"}
        if not link or not (link.startswith("https://") or link.startswith("upi://")):
            return {"ok": False, "status": 0, "error": "invalid_link", "link": link}

        body: dict[str, Any] = {"ticket": ticket, "link": link}
        if email:
            body["email"] = email
        if access_token:
            body["access_token"] = access_token
        if require_online_workers:
            body["require_online_workers"] = True

        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post(
                    f"{self.base_url}/api/scan-orders",
                    json=body,
                    headers=self._headers(),
                )
            data: dict[str, Any] = {}
            try:
                parsed = r.json()
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
            data["status"] = r.status_code
            data["ok"] = r.status_code < 400 and bool(data.get("ok", r.status_code < 400))
            if not data["ok"] and "error" not in data:
                if r.status_code == 409:
                    data["error"] = "no_online_workers_or_no_quota"
                elif r.status_code == 403:
                    data["error"] = "cloudflare_1010_missing_ua"
                else:
                    data["error"] = f"http_{r.status_code}"
            return data
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": 0, "error": str(exc)}
