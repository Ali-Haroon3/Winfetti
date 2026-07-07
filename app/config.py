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

    # Rate limits (Redis fixed window, per minute).
    rate_limit_enabled: bool = True
    rate_limit_writes_per_min: int = 30
    rate_limit_auth_per_ip_per_min: int = 5
    # If Redis is down, allow requests through rather than hard-failing.
    rate_limit_fail_open: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
