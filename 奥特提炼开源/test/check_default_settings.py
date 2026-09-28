#!/usr/bin/env python3
"""Assert ConfigModel + config.yaml defaults match Settings UI snapshot."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import ConfigModel, TelegramMode  # noqa: E402

EXPECTED = {
    "rust_bot_base_url": "https://upiapi.linhtd.com",
    "telegram_enabled": False,
    "telegram_mode": "qr_only",
    "concurrency_limit": 5,
    "poll_interval_seconds": 120,
    "poll_budget": 3,
    "hard_timeout_seconds": 300,
    "pipeline_retry_limit": 1,
    "skip_session_cache": False,
    "blocklist_enabled": True,
    "blocklist_auto_threshold": 5,
    "stale_threshold_seconds": 300,
}


def main() -> int:
    cfg = ConfigModel()
    failed: list[str] = []
    for key, want in EXPECTED.items():
        got = getattr(cfg, key)
        if hasattr(got, "value"):
            got = got.value
        if got != want:
            failed.append(f"model.{key}={got!r} want {want!r}")

    if cfg.telegram_mode != TelegramMode.QR_ONLY:
        failed.append("model.telegram_mode enum")

    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    for key, want in EXPECTED.items():
        if raw.get(key) != want:
            failed.append(f"yaml.{key}={raw.get(key)!r} want {want!r}")

    if failed:
        print("FAIL:", "; ".join(failed))
        return 1
    print("OK default settings match UI snapshot")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
