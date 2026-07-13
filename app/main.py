import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.admin.routes import router as admin_router
from app.config import get_settings
from app.emailer import build_email_sender
from app.fulfillment import build_fulfillment_client
from app.rate_limit import RateLimiter
from app.routes.auth import router as auth_router
from app.routes.checkin import router as checkin_router
from app.routes.email import router as email_router
from app.routes.game import router as game_router
from app.routes.me import router as me_router
from app.routes.redemptions import router as redemptions_router
from app.scheduler import build_scheduler
from app.webhooks import routers as webhook_routers
from app.webhooks.admob import AdMobKeyProvider


def _init_sentry() -> None:
    settings = get_settings()
    if not settings.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=settings.sentry_traces_sample_rate,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _init_sentry()
    app.state.fulfillment = build_fulfillment_client()
    app.state.rate_limiter = RateLimiter.from_settings()
    app.state.admob_keys = AdMobKeyProvider(
        settings.admob_verifier_keys_url, settings.admob_keys_cache_ttl_seconds
    )
    app.state.email_sender = build_email_sender()
    app.state.scheduler = build_scheduler(app.state.fulfillment)
    if app.state.scheduler is not None:
        app.state.scheduler.start()
    yield
    if app.state.scheduler is not None:
        await app.state.scheduler.stop()


def create_app() -> FastAPI:
    # uvicorn only configures its own loggers; without this, app-level logs
    # (including the dev email sender's verification tokens) go nowhere.
    logging.basicConfig(level=logging.INFO)

    app = FastAPI(title="Winfetti API", version="0.1.0", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(email_router)
    app.include_router(game_router)
    app.include_router(checkin_router)
    app.include_router(redemptions_router)
    for router in webhook_routers:
        app.include_router(router)
    app.include_router(admin_router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # Web frontends (web/): the player app and the ops console are static
    # no-build pages served by this process, same-origin with the API.
    # Explicit page routes rather than a "/" mount so nothing can ever
    # shadow /v1 or /admin.
    web_dir = Path(__file__).resolve().parent.parent / "web"
    app.mount("/static", StaticFiles(directory=web_dir / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def player_page():
        return FileResponse(web_dir / "index.html")

    @app.get("/console", include_in_schema=False)
    def console_page():
        return FileResponse(web_dir / "console.html")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(web_dir / "static" / "favicon.svg")

    return app


app = create_app()
