from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.blocklist import BlocklistService
from app.config import ConfigError, ConfigStore
from app.db import Database
from app.job_manager import JobManager
from app.logging_setup import setup_logging, update_secrets
from app.plus_detector import PlusDetector
from app.result_writer import ResultWriter
from app.routes import blocklist as blocklist_routes
from app.routes import bulk as bulk_routes
from app.routes import jobs as jobs_routes
from app.routes import logs as logs_routes
from app.routes import results as results_routes
from app.routes import sessions as sessions_routes
from app.routes import settings as settings_routes
from app.routes import ui as ui_routes
from app.rust_bot_client import RustBotClient
from app.session_cache import SessionCache
from app.sse import JobStream
from app.telegram_notifier import TelegramNotifier

STATIC_DIR = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        config = ConfigStore()
    except ConfigError as exc:
        print(f"FATAL config: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    cfg = config.get()
    logger = setup_logging([cfg.rust_bot_token, cfg.telegram_bot_token])
    update_secrets([cfg.rust_bot_token, cfg.telegram_bot_token])

    db = Database()
    await db.connect()

    session_cache = SessionCache()
    rust_bot = RustBotClient(cfg.rust_bot_base_url, cfg.rust_bot_token)
    telegram = TelegramNotifier(config)
    plus = PlusDetector(config, session_cache)
    results = ResultWriter()
    blocklist = BlocklistService(db, config)
    sse = JobStream()
    jm = JobManager(
        config=config,
        db=db,
        sse=sse,
        session_cache=session_cache,
        rust_bot=rust_bot,
        telegram=telegram,
        plus=plus,
        results=results,
        blocklist=blocklist,
    )
    await jm.start()

    app.state.config = config
    app.state.db = db
    app.state.session_cache = session_cache
    app.state.rust_bot = rust_bot
    app.state.telegram = telegram
    app.state.plus = plus
    app.state.results = results
    app.state.blocklist = blocklist
    app.state.sse = sse
    app.state.job_manager = jm
    app.state.logger = logger

    logger.info("app started")
    try:
        yield
    finally:
        await jm.stop()
        await db.close()
        logger.info("app stopped")


def create_app() -> FastAPI:
    app = FastAPI(title="ChatGPT Plus QR Automation", lifespan=lifespan)

    @app.middleware("http")
    async def session_middleware(request, call_next):
        """为每个访客分配隔离的 session_id（cookie）。"""
        sid = request.cookies.get("upi_sid") or ""
        if not sid or len(sid) < 16:
            import uuid as _uuid

            sid = _uuid.uuid4().hex
            request.state.new_sid = sid
        request.state.session_id = sid
        response = await call_next(request)
        if getattr(request.state, "new_sid", None):
            response.set_cookie(
                "upi_sid",
                request.state.new_sid,
                max_age=60 * 60 * 24 * 30,
                httponly=True,
                samesite="lax",
            )
        return response

    app.include_router(ui_routes.router)
    # bulk trước jobs — tránh DELETE /api/jobs/{job_id} nuốt path "clear"
    app.include_router(bulk_routes.router)
    app.include_router(jobs_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(results_routes.router)
    app.include_router(sessions_routes.router)
    app.include_router(blocklist_routes.router)
    app.include_router(logs_routes.router)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


app = create_app()


def main() -> None:
    import uvicorn

    # Uvicorn logs "Uvicorn running on http://0.0.0.0:8787" — that is the BIND
    # address, not a browser URL. Opening 0.0.0.0 in Edge/Chrome → ERR_ADDRESS_INVALID.
    print()
    print("=" * 64)
    print("  Open the UI in your browser:")
    print("    http://127.0.0.1:8787")
    print()
    print("  Many people might face error if they open http://0.0.0.0:8787")
    print("  (ERR_ADDRESS_INVALID). Always use http://127.0.0.1:8787")
    print("=" * 64)
    print()

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8787,
        reload=False,
        app_dir=str(ROOT),
    )


if __name__ == "__main__":
    main()
