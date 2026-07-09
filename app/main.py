from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.admin.routes import router as admin_router
from app.config import get_settings
from app.fulfillment import build_fulfillment_client
from app.rate_limit import RateLimiter
from app.routes.auth import router as auth_router
from app.routes.checkin import router as checkin_router
from app.routes.game import router as game_router
from app.routes.me import router as me_router
from app.routes.redemptions import router as redemptions_router
from app.webhooks import routers as webhook_routers
from app.webhooks.admob import AdMobKeyProvider


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.fulfillment = build_fulfillment_client()
    app.state.rate_limiter = RateLimiter.from_settings()
    app.state.admob_keys = AdMobKeyProvider(
        settings.admob_verifier_keys_url, settings.admob_keys_cache_ttl_seconds
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="Winfetti API", version="0.1.0", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(me_router)
    app.include_router(game_router)
    app.include_router(checkin_router)
    app.include_router(redemptions_router)
    for router in webhook_routers:
        app.include_router(router)
    app.include_router(admin_router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


app = create_app()
