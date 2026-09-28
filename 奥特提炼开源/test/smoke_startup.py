#!/usr/bin/env python3
"""Smoke: khởi động app + GET / + GET /api/settings + parse bulk."""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ok(name: str) -> None:
    print(f"[PASS] {name}", flush=True)


def _fail(name: str, err: object) -> None:
    print(f"[FAIL] {name} — {err}", flush=True)
    raise SystemExit(1)


async def main() -> None:
    # 1. config create
    try:
        from app.config import ConfigStore, ConfigError, mask_secret
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.yaml"
            cfg = ConfigStore(p)
            assert p.exists()
            m = cfg.masked_dict()
            # UI shows full token (not masked)
            assert m["rust_bot_token"] and "***" not in m["rust_bot_token"]
            assert mask_secret("upi_abcdefghij") == "upi_***ghij" or "****" in mask_secret(
                "upi_abcdefghij"
            )
            try:
                cfg.update_field("concurrency_limit", 0)
                _fail("config reject concurrency 0", "accepted")
            except ConfigError:
                pass
            try:
                cfg.update_field("flow_mode", "bogus")
                _fail("config reject flow_mode", "accepted")
            except ConfigError:
                pass
        _ok("config create/validate/mask")
    except Exception as e:
        _fail("config", e)

    # 2. parser
    try:
        from app.job_manager import parse_bulk

        r = parse_bulk(
            "a@x.com|pass1|JBSWY3DPEHPK3PXP\n"
            "badline\n"
            "\n"
            "A@X.COM|pass2|JBSWY3DPEHPK3PXP\n"
            "b@y.com|p|NOTBASE32!!!\n"
        )
        assert r["valid_count"] == 1, r
        assert r["error_count"] >= 2, r
        _ok("parse_bulk")
    except Exception as e:
        _fail("parse_bulk", e)

    # 3. session reuse
    try:
        from app.session_cache import should_reuse

        assert should_reuse(1000, 600, 300) is True
        assert should_reuse(1000, 800, 300) is False
        _ok("session should_reuse")
    except Exception as e:
        _fail("should_reuse", e)

    # 4. SSE parse
    try:
        from app.rust_bot_client import parse_sse_block
        from app.models import BotEventType

        evt = parse_sse_block(
            'event: qr_ready\ndata: {"qr_png_base64":"abc","payment_link":"http://x","expiry_seconds":90}'
        )
        assert evt and evt.type == BotEventType.QR_READY
        assert evt.qr and evt.qr.payment_link == "http://x"
        bad = parse_sse_block("garbage\nnope")
        assert bad is None
        _ok("sse parse")
    except Exception as e:
        _fail("sse parse", e)

    # 5. FastAPI TestClient
    try:
        from httpx import ASGITransport, AsyncClient
        from app.main import create_app

        app = create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # lifespan
            async with app.router.lifespan_context(app):
                r = await client.get("/")
                assert r.status_code == 200, r.status_code
                assert "ChatGPT Plus" in r.text
                _ok("GET /")

                r = await client.get("/api/settings")
                assert r.status_code == 200
                data = r.json()
                assert "rust_bot_base_url" in data
                assert data.get("rust_bot_token") and "***" not in data.get("rust_bot_token", "")
                _ok("GET /api/settings masked")

                r = await client.post(
                    "/api/jobs/submit",
                    json={
                        "raw_text": "ok@test.com|Secret123|JBSWY3DPEHPK3PXP\nbad",
                        "dispatch_mode": "hold",
                    },
                )
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["valid_count"] == 1
                assert body["created"][0]["state"] == "held"
                jid = body["created"][0]["job_id"]
                _ok("POST submit hold")

                r = await client.post(f"/api/jobs/{jid}/start")
                assert r.status_code == 200
                assert r.json()["state"] == "queued"
                _ok("POST start held→queued")

                r = await client.delete("/api/jobs/clear?filter=all")
                assert r.status_code == 400
                assert r.json()["error_code"] == "confirm_required"
                _ok("clear all requires confirm")

                r = await client.delete("/api/jobs/clear?filter=all&confirm=true")
                assert r.status_code == 200
                _ok("clear all confirm")

                r = await client.delete("/api/session-cache/%2e%2e%2fetc%2fpasswd")
                assert r.status_code in (400, 404)
                _ok("session path traversal blocked")

        _ok("ASGI smoke complete")
    except Exception as e:
        import traceback

        traceback.print_exc()
        _fail("ASGI smoke", e)

    print("ALL SMOKE PASSED", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
