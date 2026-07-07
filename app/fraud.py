import uuid

from sqlalchemy.orm import Session

from app.models import FraudEvent


def log_fraud_event(
    session: Session,
    kind: str,
    user_id: uuid.UUID | None = None,
    ip: str | None = None,
    detail: dict | None = None,
) -> None:
    """Record a denial or anomaly. Caller commits (or it rides along with the
    surrounding transaction)."""
    session.add(FraudEvent(user_id=user_id, ip=ip, kind=kind, detail=detail))
