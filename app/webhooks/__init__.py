"""Verified credit paths (Phase 2): AdMob SSV, Tapjoy offerwall postbacks,
RevenueCat and Stripe purchase webhooks. Every endpoint verifies a
third-party signature or shared secret before anything touches the ledger,
and returns 503 until its secret is configured."""

from app.webhooks.admob import router as admob_router
from app.webhooks.revenuecat import router as revenuecat_router
from app.webhooks.stripe import router as stripe_router
from app.webhooks.tapjoy import router as tapjoy_router

routers = [admob_router, tapjoy_router, revenuecat_router, stripe_router]
