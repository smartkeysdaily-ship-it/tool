from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import ValidationError

from app.models import ConfigModel, FlowMode, TelegramMode

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.yaml"

_URL_RE = re.compile(r"^https?://", re.I)


def mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    prefix = secret[:4] if secret.startswith("upi_") else secret[:4]
    return f"{prefix}***{secret[-4:]}"


class ConfigError(Exception):
    pass


class ConfigStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._cfg = self._load_or_create()

    @property
    def data(self) -> ConfigModel:
        with self._lock:
            return self._cfg.model_copy(deep=True)

    def get(self) -> ConfigModel:
        return self.data

    def _load_or_create(self) -> ConfigModel:
        if not self.path.exists():
            cfg = ConfigModel()
            self._write(cfg)
            return cfg
        try:
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(
                f"config.yaml parse error at {self.path}: {exc}"
            ) from exc
        except OSError as exc:
            raise ConfigError(f"cannot read config.yaml: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config.yaml must be a mapping: {self.path}")
        try:
            cfg = ConfigModel.model_validate(raw)
        except ValidationError as exc:
            raise ConfigError(f"config.yaml invalid: {exc}") from exc
        self._validate_business(cfg)
        return cfg

    def _write(self, cfg: ConfigModel) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = cfg.model_dump(mode="json")
        text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        self.path.write_text(text, encoding="utf-8")

    @staticmethod
    def _validate_business(cfg: ConfigModel) -> None:
        if not _URL_RE.match(cfg.rust_bot_base_url or ""):
            raise ConfigError("rust_bot_base_url must be http(s) URL")
        parsed = urlparse(cfg.rust_bot_base_url)
        if not parsed.netloc:
            raise ConfigError("rust_bot_base_url missing host")
        if not (cfg.rust_bot_token or "").strip():
            raise ConfigError("rust_bot_token must not be empty")
        if cfg.flow_mode not in (FlowMode.AUTO, FlowMode.MANUAL):
            raise ConfigError("flow_mode must be auto|manual")
        if cfg.telegram_mode not in (
            TelegramMode.OFF,
            TelegramMode.QR_ONLY,
            TelegramMode.ALL,
        ):
            raise ConfigError("telegram_mode must be off|qr_only|all")
        if not (1 <= cfg.concurrency_limit <= 50):
            raise ConfigError("concurrency_limit must be in [1, 50]")
        if not (1 <= cfg.blocklist_auto_threshold <= 10):
            raise ConfigError("blocklist_auto_threshold must be in [1, 10]")
        if not (0 <= cfg.session_cache_ttl_buffer_seconds <= 3600):
            raise ConfigError("session_cache_ttl_buffer_seconds must be in [0, 3600]")
        if not (60 <= cfg.stale_threshold_seconds <= 3600):
            raise ConfigError("stale_threshold_seconds must be in [60, 3600]")
        if not cfg.login_backoff_seconds:
            raise ConfigError("login_backoff_seconds must be non-empty list")

    def masked_dict(self) -> dict[str, Any]:
        """Trả full settings cho UI local — không che API key (theo yêu cầu operator)."""
        return self.data.model_dump(mode="json")

    def update_field(self, key: str, value: Any) -> dict[str, Any]:
        with self._lock:
            if key not in ConfigModel.model_fields:
                raise ConfigError(f"unknown key: {key}")
            payload = self._cfg.model_dump(mode="json")
            if key in ("rust_bot_token", "telegram_bot_token"):
                value = str(value or "").strip()
            payload[key] = value
            try:
                new_cfg = ConfigModel.model_validate(payload)
            except ValidationError as exc:
                raise ConfigError(str(exc)) from exc
            self._validate_business(new_cfg)
            self._write(new_cfg)
            self._cfg = new_cfg
            return {
                "key": key,
                "value": value,
                "value_masked": value,  # backward-compat alias
                "applied_at": time.time(),
            }

    def update_bulk(self, updates: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            payload = self._cfg.model_dump(mode="json")
            for k, v in updates.items():
                if k not in ConfigModel.model_fields:
                    raise ConfigError(f"unknown key: {k}")
                if k in ("rust_bot_token", "telegram_bot_token"):
                    s = str(v or "").strip()
                    if not s:
                        continue  # ô trống = giữ token cũ
                    payload[k] = s
                else:
                    payload[k] = v
            try:
                new_cfg = ConfigModel.model_validate(payload)
            except ValidationError as exc:
                raise ConfigError(str(exc)) from exc
            self._validate_business(new_cfg)
            self._write(new_cfg)
            self._cfg = new_cfg
            return self.masked_dict()
