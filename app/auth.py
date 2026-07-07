import hmac
import uuid
from datetime import timedelta

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.models import User

_bearer = HTTPBearer(auto_error=False)


def issue_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    now = clock.now_utc()
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(days=settings.jwt_expires_days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def _deny(db: Session, request: Request, status: int, code: str) -> HTTPException:
    log_fraud_event(
        db, f"auth_denied:{code}", ip=client_ip(request), detail={"path": request.url.path}
    )
    db.commit()
    return HTTPException(status_code=status, detail=code)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise _deny(db, request, 401, "missing_token")
    try:
        payload = jwt.decode(
            credentials.credentials,
            get_settings().jwt_secret,
            algorithms=["HS256"],
        )
        user_id = uuid.UUID(payload["sub"])
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise _deny(db, request, 401, "invalid_token")

    user = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if user is None:
        raise _deny(db, request, 401, "unknown_user")
    if user.status != "active":
        raise _deny(db, request, 403, "account_disabled")
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> None:
    settings = get_settings()
    if not settings.admin_api_key:
        raise HTTPException(status_code=503, detail="admin_disabled")
    supplied = request.headers.get("X-Admin-Key") or ""
    if not hmac.compare_digest(supplied, settings.admin_api_key):
        raise _deny(db, request, 403, "forbidden")


def client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting and fraud logs.

    With trust_proxy_headers on (the Fly/Railway deployment shape: exactly
    one edge proxy that APPENDS the real client address), take the
    right-most X-Forwarded-For entry — anything the client forged sits to
    the left of it. When exposed directly, turn the flag off so a spoofed
    header can't shard the per-IP rate limit.
    """
    if get_settings().trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.rsplit(",", 1)[-1].strip()
    return request.client.host if request.client else "unknown"
