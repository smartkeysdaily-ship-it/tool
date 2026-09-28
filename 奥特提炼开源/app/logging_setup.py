from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.config import mask_secret

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_PATH = LOG_DIR / "app.log"

_SENSITIVE_KEYS = frozenset(
    {"password", "totp_secret", "cookies", "access_token", "pass", "2fa"}
)


class RedactionFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.secrets: list[str] = []

    def set_secrets(self, secrets: list[str]) -> None:
        self.secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            msg = str(record.msg)
        for s in self.secrets:
            if s and s in msg:
                msg = msg.replace(s, mask_secret(s))
        for k in _SENSITIVE_KEYS:
            if hasattr(record, k):
                try:
                    delattr(record, k)
                except Exception:
                    record.__dict__.pop(k, None)
        record.msg = msg
        record.args = ()
        return True


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "job_id": getattr(record, "job_id", None),
            "email": getattr(record, "email", None),
            "state": getattr(record, "state", None),
            "event": getattr(record, "event", None),
            "error_code": getattr(record, "error_code", None),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["stack"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


_redaction = RedactionFilter()
_configured = False


def setup_logging(secrets: list[str] | None = None) -> logging.Logger:
    global _configured
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("plus_auto")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if secrets:
        _redaction.set_secrets(list(secrets))
    if not _configured:
        handler = RotatingFileHandler(
            LOG_PATH,
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(JsonLineFormatter())
        handler.addFilter(_redaction)
        logger.addHandler(handler)
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        console.addFilter(_redaction)
        logger.addHandler(console)
        _configured = True
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("plus_auto")


def log_unclassified(message: str, exc: BaseException | None = None, **extra: Any) -> None:
    logger = get_logger()
    logger.error(
        message,
        exc_info=exc,
        extra={**extra, "error_code": "unclassified", "event": "unclassified"},
    )


def update_secrets(secrets: list[str]) -> None:
    _redaction.set_secrets(list(secrets))
