"""Redis fixed-window rate limiting. Denials are recorded in fraud_events."""

import logging

import redis
from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app import clock
from app.auth import client_ip, get_current_user
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.models import User

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, redis_client: redis.Redis | None, enabled: bool, fail_open: bool):
        self.redis = redis_client
        self.enabled = enabled
        self.fail_open = fail_open

    @classmethod
    def from_settings(cls) -> "RateLimiter":
        settings = get_settings()
        client = None
        if settings.rate_limit_enabled and settings.redis_url:
            client = redis.Redis.from_url(settings.redis_url)
        return cls(client, settings.rate_limit_enabled, settings.rate_limit_fail_open)

    def allow(self, key: str, limit: int, window_s: int = 60) -> bool:
        if not self.enabled or self.redis is None:
            return True
        window = int(clock.now_utc().timestamp()) // window_s
        redis_key = f"rl:{key}:{window}"
        try:
            pipe = self.redis.pipeline()
            pipe.incr(redis_key)
            pipe.expire(redis_key, window_s * 2)
            count, _ = pipe.execute()
            return int(count) <= limit
        except redis.RedisError:
            logger.warning("rate limiter redis error for key %s", key, exc_info=True)
            return self.fail_open


def get_rate_limiter(request: Request) -> RateLimiter:
    return request.app.state.rate_limiter


def rate_limit_user_writes(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> User:
    """Shared per-user budget across all coin-writing endpoints."""
    settings = get_settings()
    if not limiter.allow(f"writes:{user.id}", settings.rate_limit_writes_per_min):
        log_fraud_event(
            db,
            "rate_limited_writes",
            user_id=user.id,
            ip=client_ip(request),
            detail={"path": request.url.path},
        )
        db.commit()
        raise HTTPException(status_code=429, detail="rate_limited")
    return user


def rate_limit_auth_ip(
    request: Request,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> None:
    settings = get_settings()
    ip = client_ip(request)
    if not limiter.allow(f"auth:{ip}", settings.rate_limit_auth_per_ip_per_min):
        log_fraud_event(
            db, "rate_limited_auth", ip=ip, detail={"path": request.url.path}
        )
        db.commit()
        raise HTTPException(status_code=429, detail="rate_limited")
