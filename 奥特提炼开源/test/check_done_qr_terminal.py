#!/usr/bin/env python3
"""Verify SSE done extracts QR and JobState.DONE is terminal."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.models import (  # noqa: E402
    ACTIVE_STATES,
    CHECK_PLAN_STATES,
    CONCLUDED_STATES,
    BotEventType,
    JobState,
)
from app.rust_bot_client import parse_sse_block  # noqa: E402


def test_parse_done_with_qr() -> None:
    block = (
        "event: done\n"
        "data: {"
        '"job_id": 42, "ok": true, "code": "ok", '
        '"qr_png_base64": "iVBORw0KGgo=", '
        '"payment_link": "https://pay.example/x", '
        '"payment_url": "https://pay.example/x"'
        "}\n"
    )
    evt = parse_sse_block(block)
    assert evt is not None, "done event must parse"
    assert evt.type == BotEventType.DONE
    assert evt.qr is not None
    assert evt.qr.qr_png_base64.startswith("iVBORw")
    assert evt.qr.payment_link.startswith("https://pay.example")
    print("OK parse done+qr")


def test_parse_done_link_only() -> None:
    block = (
        "event: done\n"
        'data: {"ok": true, "payment_url": "https://pay.example/y"}\n'
    )
    evt = parse_sse_block(block)
    assert evt is not None
    assert evt.type == BotEventType.DONE
    assert evt.qr is not None
    assert evt.qr.payment_link == "https://pay.example/y"
    print("OK parse done+link")


def test_state_machine() -> None:
    assert JobState.DONE in CONCLUDED_STATES
    assert JobState.DONE not in ACTIVE_STATES
    assert JobState.DONE in CHECK_PLAN_STATES
    assert JobState.POLLING_PLUS not in ACTIVE_STATES
    assert JobState.FREE_TIMEOUT not in CHECK_PLAN_STATES
    print("OK state sets")


def main() -> int:
    test_parse_done_with_qr()
    test_parse_done_link_only()
    test_state_machine()
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
