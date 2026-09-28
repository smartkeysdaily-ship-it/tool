from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, AsyncIterator, Optional

import httpx

from app.models import BotEvent, BotEventType, QRPayload

OnEvent = Callable[[BotEvent], Awaitable[None]]
_STALL_SENTINEL = object()


def _map_http(status: int) -> str:
    if status == 402:
        return "credits_required"
    if status == 429:
        return "rate_limit"
    if status == 409:
        return "duplicate_email"
    if status == 503:
        return "queue_full"
    if status in (401, 403):
        return "invalid_session"
    return f"http_{status}"


def parse_sse_block(block: str) -> Optional[BotEvent]:
    event_type: str | None = None
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            event_type = line[len("event:") :].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:") :].strip())
    if not event_type:
        return None
    payload: dict[str, Any] = {}
    if data_lines:
        try:
            payload = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            payload = {"raw": "\n".join(data_lines)}
    if not isinstance(payload, dict):
        payload = {"raw": payload}

    def _qr_from(data: dict[str, Any]) -> QRPayload | None:
        png = str(
            data.get("qr_png_base64")
            or data.get("qr_base64")
            or ""
        )
        link = str(
            data.get("payment_link")
            or data.get("payment_url")
            or data.get("pay_url")
            or ""
        )
        if not png and not link:
            return None
        return QRPayload(
            qr_png_base64=png,
            payment_link=link,
            expiry_seconds=int(
                data.get("expiry_seconds")
                or data.get("expires_in")
                or 900
            ),
            received_at=int(time.time()),
        )

    if event_type == "qr_ready":
        return BotEvent(
            type=BotEventType.QR_READY,
            qr=_qr_from(payload),
            raw=payload,
        )
    if event_type == "done":
        return BotEvent(
            type=BotEventType.DONE,
            qr=_qr_from(payload),
            raw=payload,
        )
    if event_type == "failed":
        return BotEvent(
            type=BotEventType.FAILED,
            error_code=str(payload.get("error_code") or payload.get("code") or "failed"),
            raw=payload,
        )
    if event_type == "timeout":
        return BotEvent(
            type=BotEventType.TIMEOUT, error_code="bot_timeout", raw=payload
        )
    if event_type == "cancelled":
        return BotEvent(
            type=BotEventType.CANCELLED, error_code="bot_cancelled", raw=payload
        )
    return None


async def _iter_with_timeout(
    aiter: AsyncIterator[bytes], stall: float
) -> AsyncIterator[Any]:
    it = aiter.__aiter__()
    while True:
        try:
            chunk = await asyncio.wait_for(it.__anext__(), timeout=stall)
            yield chunk
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            yield _STALL_SENTINEL
            return


class RustBotClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    def configure(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    async def me(self, timeout: float = 10.0) -> dict[str, Any]:
        """GET /api/v1/me — validate API key + return credits/account info."""
        t0 = time.perf_counter()
        url = f"{self.base_url}/api/v1/me"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {self.token}"},
                )
            latency = int((time.perf_counter() - t0) * 1000)
            data: dict[str, Any] = {}
            try:
                parsed = r.json()
                if isinstance(parsed, dict):
                    data = parsed
            except ValueError:
                data = {}
            ok = r.status_code < 400 and bool(data.get("ok", r.status_code < 400))
            return {
                "ok": ok,
                "status": r.status_code,
                "latency_ms": latency,
                "user_id": data.get("user_id"),
                "balance": data.get("balance"),
                "reserved": data.get("reserved"),
                "key_prefix": data.get("key_prefix"),
                "error": None
                if ok
                else str(data.get("code") or data.get("error") or f"http_{r.status_code}"),
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(exc),
            }

    async def run_bot_job(
        self,
        access_token: str,
        email: str,
        on_event: OnEvent,
        *,
        proxy: str | None = None,
        stall: float = 300.0,
    ) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        body: dict[str, Any] = {"access_token": access_token, "email": email}
        if proxy:
            # 同时带上多种常见字段名，最大兼容不同 Bot 实现
            body["proxy"] = proxy
            body["proxy_url"] = proxy
            body["proxy_ip"] = proxy
        url = f"{self.base_url}/api/v1/jobs"

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST", url, json=body, headers=headers
            ) as resp:
                if resp.status_code >= 300:
                    await on_event(
                        BotEvent(
                            type=BotEventType.FAILED,
                            error_code=_map_http(resp.status_code),
                        )
                    )
                    return

                buffer = ""
                async for chunk in _iter_with_timeout(resp.aiter_bytes(), stall=stall):
                    if chunk is _STALL_SENTINEL:
                        await on_event(
                            BotEvent(
                                type=BotEventType.FAILED,
                                error_code="sse_stalled",
                            )
                        )
                        return
                    if not chunk:
                        continue
                    buffer += chunk.decode("utf-8", "replace")
                    while "\n\n" in buffer:
                        raw_event, buffer = buffer.split("\n\n", 1)
                        evt = parse_sse_block(raw_event)
                        if not evt:
                            continue
                        await on_event(evt)
                        if evt.type in (
                            BotEventType.DONE,
                            BotEventType.FAILED,
                            BotEventType.TIMEOUT,
                            BotEventType.CANCELLED,
                        ):
                            return
