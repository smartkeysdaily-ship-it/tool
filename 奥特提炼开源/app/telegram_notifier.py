from __future__ import annotations

import base64
import time
from typing import Any

import httpx

from app.config import ConfigStore
from app.logging_setup import get_logger
from app.models import JobRecord, QRPayload, TelegramMode


class TelegramNotifier:
    def __init__(self, config: ConfigStore) -> None:
        self.config = config
        self._sent_qr: set[str] = set()
        self.log = get_logger()

    def _cfg(self):
        return self.config.get()

    async def send_qr(self, job: JobRecord, qr: QRPayload) -> None:
        cfg = self._cfg()
        if not cfg.telegram_enabled or cfg.telegram_mode == TelegramMode.OFF:
            return
        if cfg.telegram_mode not in (TelegramMode.QR_ONLY, TelegramMode.ALL):
            return
        if job.job_id in self._sent_qr:
            return
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            self.log.error(
                "telegram config incomplete",
                extra={"email": job.email, "error_code": "telegram_config", "event": "telegram"},
            )
            return
        caption = (
            f"{job.email}\n{qr.payment_link}\n"
            f"Expires in {qr.expiry_seconds} seconds"
        )
        try:
            png = base64.b64decode(qr.qr_png_base64)
        except Exception as exc:
            self.log.error(
                f"qr decode failed: {exc}",
                extra={"email": job.email, "error_code": "qr_decode", "event": "telegram"},
            )
            return
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendPhoto",
                    data={"chat_id": cfg.telegram_chat_id, "caption": caption},
                    files={"photo": ("qr.png", png, "image/png")},
                )
            if r.status_code // 100 != 2:
                self.log.error(
                    "telegram_send_failed",
                    extra={
                        "email": job.email,
                        "error_code": f"http_{r.status_code}",
                        "event": "telegram",
                    },
                )
            else:
                self._sent_qr.add(job.job_id)
        except Exception as exc:
            self.log.error(
                f"telegram_error: {exc}",
                extra={
                    "email": job.email,
                    "error_code": "telegram_network",
                    "event": "telegram",
                },
            )

    async def send_event(self, job: JobRecord, kind: str) -> None:
        cfg = self._cfg()
        if not cfg.telegram_enabled or cfg.telegram_mode != TelegramMode.ALL:
            return
        if not cfg.telegram_bot_token or not cfg.telegram_chat_id:
            return
        text = f"[{kind}] {job.email}"
        if job.last_error:
            text += f"\n{job.last_error}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
                    json={"chat_id": cfg.telegram_chat_id, "text": text},
                )
        except Exception as exc:
            self.log.error(
                f"telegram_event_error: {exc}",
                extra={"email": job.email, "error_code": "telegram_network", "event": "telegram"},
            )

    async def test_send(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        """Test Telegram. Optional overrides = giá trị đang gõ trên Settings UI."""
        cfg = self._cfg()
        token = (bot_token if bot_token is not None else cfg.telegram_bot_token) or ""
        token = str(token).strip()
        chat = (chat_id if chat_id is not None else cfg.telegram_chat_id) or ""
        chat = str(chat).strip()
        if not token or not chat:
            return {
                "ok": False,
                "status": 0,
                "latency_ms": 0,
                "error": "missing telegram_bot_token or telegram_chat_id",
            }
        t0 = time.perf_counter()
        text = f"Telegram connectivity test — {int(time.time())}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat, "text": text},
                )
            return {
                "ok": r.status_code // 100 == 2,
                "status": r.status_code,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": None if r.status_code // 100 == 2 else r.text[:200],
            }
        except Exception as exc:
            return {
                "ok": False,
                "status": 0,
                "latency_ms": int((time.perf_counter() - t0) * 1000),
                "error": str(exc),
            }
