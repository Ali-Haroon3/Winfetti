from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://winfetti:winfetti@localhost:5432/winfetti"
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: str = "dev-secret-change-me"
    jwt_expires_days: int = 30

    # Admin routes are disabled (503) until this is set.
    admin_api_key: str = ""

    # Tremendous fulfillment. With no API key the app falls back to the
    # stub client so `docker compose up` works out of the box.
    tremendous_api_key: str = ""
    tremendous_base_url: str = "https://testflight.tremendous.com/api/v2"
    tremendous_funding_source_id: str = "balance"
    tremendous_campaign_id: str = ""

    # Multipliers (server-owned; the client never sends amounts).
    happy_hour_start_utc: int = 19  # inclusive hour, server clock
    happy_hour_end_utc: int = 21  # exclusive hour
    happy_hour_multiplier: float = 2.0
    gold_multiplier: float = 1.5
    boost_multiplier: float = 2.0

    # Hard caps (coins).
    daily_game_win_cap: int = 60_000
    daily_total_credit_cap: int = 100_000

    # Redemption gates.
    redemption_min_account_age_days: int = 7
    redemption_min_verified_ad_receipts: int = 10
    auto_approve_after_n_approved: int = 2
    auto_approve_max_usd: float = 10.0
    # POST /v1/redemptions has no idem_key; the cooldown stops a network
    # retry from creating (and possibly auto-shipping) a second order.
    redemption_cooldown_seconds: int = 60

    # Phase 2 webhooks. Each endpoint returns 503 until its secret is set —
    # a credit path with no signature to verify must not exist.
    ad_reward_coins: int = 250  # server-owned; the SSV reward params are ignored
    admob_verifier_keys_url: str = (
        "https://www.gstatic.com/admob/reward/verifier-keys.json"
    )
    admob_keys_cache_ttl_seconds: int = 86_400
    tapjoy_secret: str = ""
    # Defensive clamp on offerwall payout sizes (the amount rides in the
    # postback; the hash proves origin, not sanity).
    max_offer_coins_per_postback: int = 50_000
    revenuecat_webhook_auth: str = ""
    stripe_webhook_secret: str = ""
    stripe_webhook_tolerance_seconds: int = 300

    # Email verification (Phase 3).
    email_token_ttl_hours: int = 24

    # Affiliate cashback (Phase 3). Sales credit as pending and mature after
    # the return window; the endpoint 503s until the secret is set.
    affiliate_secret: str = ""
    cashback_maturation_days: int = 30
    coins_per_usd: int = 10_000
    cashback_share: float = 0.5  # user's cut of the affiliate commission
    max_cashback_coins_per_postback: int = 100_000

    # Observability (Phase 3). Sentry is off until a DSN is set.
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1

    # Rate limits (Redis fixed window, per minute).
    rate_limit_enabled: bool = True
    rate_limit_writes_per_min: int = 30
    rate_limit_auth_per_ip_per_min: int = 5
    # If Redis is down, allow requests through rather than hard-failing.
    rate_limit_fail_open: bool = True
    # True when running behind exactly one edge proxy that appends the real
    # client IP to X-Forwarded-For (Fly/Railway). Set false if the app is
    # exposed directly, or the header becomes client-spoofable.
    trust_proxy_headers: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
