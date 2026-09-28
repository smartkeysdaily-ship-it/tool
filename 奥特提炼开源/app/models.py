from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class JobState(str, Enum):
    QUEUED = "queued"
    HELD = "held"
    LOGGING_IN = "logging_in"
    SESSION_READY = "session_ready"
    SUBMITTING_BOT = "submitting_bot"
    AWAITING_QR = "awaiting_qr"
    QR_READY = "qr_ready"
    DONE = "done"
    # Legacy (DB cũ) — không còn dùng trong pipeline mới
    POLLING_PLUS = "polling_plus"
    PLUS_CONFIRMED = "plus_confirmed"
    FREE_TIMEOUT = "free_timeout"
    RETRY_SCHEDULED = "retry_scheduled"
    FAILED = "failed"
    STOPPED = "stopped"
    STALE = "stale"


ACTIVE_STATES = {
    JobState.LOGGING_IN,
    JobState.SESSION_READY,
    JobState.SUBMITTING_BOT,
    JobState.AWAITING_QR,
    JobState.QR_READY,
}

WAITING_STATES = {
    JobState.QUEUED,
    JobState.HELD,
    JobState.RETRY_SCHEDULED,
}

CONCLUDED_STATES = {
    JobState.DONE,
    JobState.PLUS_CONFIRMED,  # legacy
    JobState.FREE_TIMEOUT,  # legacy
    JobState.FAILED,
    JobState.STOPPED,
    JobState.STALE,
}

RERUNNABLE_STATES = CONCLUDED_STATES

CHECK_PLAN_STATES = {
    JobState.DONE,
    JobState.QR_READY,
}

STATE_LABELS: dict[JobState, str] = {
    JobState.QUEUED: "Queued",
    JobState.HELD: "Held",
    JobState.LOGGING_IN: "Logging in",
    JobState.SESSION_READY: "Session ready",
    JobState.SUBMITTING_BOT: "Submitting bot",
    JobState.AWAITING_QR: "Waiting for QR",
    JobState.QR_READY: "QR ready",
    JobState.DONE: "Done",
    JobState.POLLING_PLUS: "Checking Plus",
    JobState.PLUS_CONFIRMED: "Plus confirmed",
    JobState.FREE_TIMEOUT: "Still Free",
    JobState.RETRY_SCHEDULED: "Retry scheduled",
    JobState.FAILED: "Failed",
    JobState.STOPPED: "Stopped",
    JobState.STALE: "Stale",
}

# Backward-compat alias
STATE_LABELS_VI = STATE_LABELS


class FlowMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class TelegramMode(str, Enum):
    OFF = "off"
    QR_ONLY = "qr_only"
    ALL = "all"


class DispatchOverride(str, Enum):
    DEFAULT = "default"
    IMMEDIATE = "immediate"
    HOLD = "hold"


class BotEventType(str, Enum):
    QR_READY = "qr_ready"
    DONE = "done"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


HARD_ERRORS = {"credits_required", "queue_full"}
SOFT_ERRORS = {
    "rate_limit",
    "max_concurrent",
    "cooldown",
    "spacing",
    "duplicate_email",
    "invalid_session",
    "approve_blocked",
}

SOFT_ERROR_DELAYS: dict[str, int] = {
    "rate_limit": 0,
    "max_concurrent": 0,
    "cooldown": 0,
    "spacing": 0,
    "duplicate_email": 0,
    "sse_stalled": 0,
    "bot_timeout": 0,
    "bot_cancelled": 0,
    "proxy_exhausted": 0,
    "checkout_flagged": 0,
    "qr_incomplete": 0,
    "approve_blocked": 2,
}


class AccountRecord(BaseModel):
    email: str = ""
    password: str = ""
    totp_secret: str = ""
    access_token: str = ""


class SessionEntry(BaseModel):
    email: str
    cookies: dict[str, str]
    access_token: str
    expires_at: int
    created_at: int
    last_used_at: Optional[int] = None


class QRPayload(BaseModel):
    qr_png_base64: str
    payment_link: str = ""
    expiry_seconds: int = 0
    received_at: int = 0


class BotEvent(BaseModel):
    type: BotEventType
    error_code: Optional[str] = None
    qr: Optional[QRPayload] = None
    raw: Optional[dict[str, Any]] = None


class TimelineEntry(BaseModel):
    state: JobState
    at: float
    note: Optional[str] = None


class JobRecord(BaseModel):
    job_id: str
    email: str
    password: str = ""
    totp_secret: str = ""
    access_token: str = ""
    state: JobState = JobState.QUEUED
    attempt_login: int = 0
    attempt_pipeline: int = 0
    held_flag: bool = False
    origin_job_id: Optional[str] = None
    last_error: Optional[str] = None
    qr: Optional[QRPayload] = None
    plan_result: Optional[str] = None  # plus|free|pending from side Check Plus
    scan_order_id: str = ""
    scan_order_secret: str = ""
    session_id: str = ""  # 访客会话隔离标识
    timeline: list[TimelineEntry] = Field(default_factory=list)
    created_at: float = 0.0
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    last_progress_at: float = 0.0
    cookies_summary: list[dict[str, str]] = Field(default_factory=list)

    def to_account(self) -> AccountRecord:
        return AccountRecord(
            email=self.email,
            password=self.password,
            totp_secret=self.totp_secret,
            access_token=self.access_token,
        )

    def public_dict(self) -> dict[str, Any]:
        qr_pub = None
        if self.qr:
            qr_pub = {
                "payment_link": self.qr.payment_link,
                "expiry_seconds": self.qr.expiry_seconds,
                "received_at": self.qr.received_at,
                "has_image": bool(self.qr.qr_png_base64),
                "thumbnail_b64": (
                    self.qr.qr_png_base64[:200] + "…"
                    if len(self.qr.qr_png_base64) > 200
                    else self.qr.qr_png_base64
                ),
            }
        return {
            "job_id": self.job_id,
            "email": self.email,
            "state": self.state.value,
            "state_label": STATE_LABELS.get(self.state, self.state.value),
            "attempt_login": self.attempt_login,
            "attempt_pipeline": self.attempt_pipeline,
            "held_flag": self.held_flag,
            "origin_job_id": self.origin_job_id,
            "queue_position": 0,
            "last_error": self.last_error,
            "plan_result": self.plan_result,
            "scan_order_id": self.scan_order_id,
            "session_id": self.session_id,
            "qr": qr_pub,
            "timeline": [t.model_dump(mode="json") for t in self.timeline],
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_progress_at": self.last_progress_at,
        }


class BlocklistEntry(BaseModel):
    email: str
    reason: str
    created_at: int
    fail_count_at_add: int = 0
    notes: Optional[str] = None


class SessionCacheEntry(BaseModel):
    email: str
    expires_at: int
    created_at: int
    last_used_at: Optional[int] = None
    file_size_bytes: int = 0
    is_expired: bool = False


class ConfigModel(BaseModel):
    """Default khớp Settings UI: concurrency 5, poll 120/3, hard TO 300, retry 1, blocklist on/threshold 5."""

    rust_bot_base_url: str = "https://upiapi.linhtd.com"
    rust_bot_token: str = "upi_changeme"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_enabled: bool = False
    telegram_mode: TelegramMode = TelegramMode.QR_ONLY
    concurrency_limit: int = Field(10, ge=1, le=50)
    poll_interval_seconds: int = 120
    poll_budget: int = 3
    hard_timeout_seconds: int = 300
    pipeline_retry_limit: int = 5
    login_backoff_seconds: list[int] = Field(default_factory=lambda: [2, 5, 15])
    skip_session_cache: bool = False
    flow_mode: FlowMode = FlowMode.AUTO
    blocklist_enabled: bool = True
    blocklist_auto_threshold: int = Field(5, ge=1, le=10)
    session_cache_ttl_buffer_seconds: int = Field(300, ge=0, le=3600)
    stale_threshold_seconds: int = Field(300, ge=60, le=3600)
    # 代理池：每行一个代理；失败重试时自动轮换到下一条
    proxy_list: str = ""
    # 是否把代理随任务一起提交给 UPI Bot
    proxy_enabled: bool = True
    # 扫码站点：Done 后自动提交支付链接
    scan_site_url: str = "https://masi.cc.cd"
    scan_site_cdk: str = ""
    scan_site_enabled: bool = False
    scan_site_require_workers: bool = True
    # 提取引擎：local = 本地引擎（不依赖外部 UPI Bot）；bot = 外部 Rust UPI Bot
    extraction_mode: str = "local"
    # 备用站点（游客可切换）
    backup_site_url: str = "https://dasaobi.online/upi/"
