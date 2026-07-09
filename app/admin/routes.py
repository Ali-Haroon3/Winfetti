import uuid
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, jobs, ledger
from app.auth import require_admin
from app.db import get_db
from app.fraud import log_fraud_event
from app.models import AdReceipt, FraudEvent, LedgerEntry, Redemption, User
from app.routes.redemptions import get_fulfillment
from app.schemas import AdminRedemptionResponse
from app.services import DomainError, approve_redemption, deny_redemption

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


def _load(db: Session, redemption_id: uuid.UUID) -> Redemption:
    redemption = db.get(Redemption, redemption_id)
    if redemption is None:
        raise HTTPException(status_code=404, detail="not_found")
    return redemption


@router.get("/redemptions", response_model=list[AdminRedemptionResponse])
def list_redemptions(
    status: str = Query(default="pending"),
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(Redemption)
            .where(Redemption.status == status)
            .order_by(Redemption.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [AdminRedemptionResponse.model_validate(r) for r in rows]


@router.post("/redemptions/{redemption_id}/approve", response_model=AdminRedemptionResponse)
def approve(
    redemption_id: uuid.UUID,
    db: Session = Depends(get_db),
    fulfillment=Depends(get_fulfillment),
):
    redemption = _load(db, redemption_id)
    try:
        approve_redemption(db, redemption, fulfillment)
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return AdminRedemptionResponse.model_validate(redemption)


@router.post("/redemptions/{redemption_id}/deny", response_model=AdminRedemptionResponse)
def deny(redemption_id: uuid.UUID, db: Session = Depends(get_db)):
    redemption = _load(db, redemption_id)
    try:
        deny_redemption(db, redemption)
        db.commit()
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return AdminRedemptionResponse.model_validate(redemption)


# ---------------------------------------------------------------------------
# Fraud dashboard (Phase 3): JSON now, UI later.


@router.get("/fraud/events")
def fraud_events(
    kind: str | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(get_db),
):
    stmt = select(FraudEvent).order_by(FraudEvent.created_at.desc()).limit(limit)
    if kind:
        stmt = stmt.where(FraudEvent.kind == kind)
    if user_id:
        stmt = stmt.where(FraudEvent.user_id == user_id)
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "user_id": str(e.user_id) if e.user_id else None,
            "ip": e.ip,
            "detail": e.detail,
            "created_at": e.created_at,
        }
        for e in db.execute(stmt).scalars()
    ]


@router.get("/fraud/summary")
def fraud_summary(
    hours: int = Query(default=24, ge=1, le=24 * 30),
    db: Session = Depends(get_db),
):
    since = clock.now_utc() - timedelta(hours=hours)
    by_kind = db.execute(
        select(FraudEvent.kind, func.count())
        .where(FraudEvent.created_at >= since)
        .group_by(FraudEvent.kind)
        .order_by(func.count().desc())
    ).all()
    top_ips = db.execute(
        select(FraudEvent.ip, func.count())
        .where(FraudEvent.created_at >= since, FraudEvent.ip.is_not(None))
        .group_by(FraudEvent.ip)
        .order_by(func.count().desc())
        .limit(10)
    ).all()
    risky_users = db.execute(
        select(User.id, User.risk_score, User.balance, User.status)
        .where(User.risk_score > 0)
        .order_by(User.risk_score.desc())
        .limit(10)
    ).all()
    return {
        "window_hours": hours,
        "events_by_kind": {kind: count for kind, count in by_kind},
        "top_denied_ips": [{"ip": ip, "denials": count} for ip, count in top_ips],
        "top_risk_users": [
            {
                "user_id": str(uid),
                "risk_score": score,
                "balance": balance,
                "status": status,
            }
            for uid, score, balance, status in risky_users
        ],
    }


@router.get("/users/{user_id}")
def user_detail(user_id: uuid.UUID, db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="not_found")
    credited, debited = db.execute(
        select(
            func.coalesce(
                func.sum(LedgerEntry.amount).filter(LedgerEntry.amount > 0), 0
            ),
            func.coalesce(
                func.sum(LedgerEntry.amount).filter(LedgerEntry.amount < 0), 0
            ),
        ).where(LedgerEntry.user_id == user_id)
    ).one()
    verified_ads = db.execute(
        select(func.count())
        .select_from(AdReceipt)
        .where(AdReceipt.user_id == user_id, AdReceipt.verified.is_(True))
    ).scalar_one()
    redemptions = db.execute(
        select(Redemption.status, func.count())
        .where(Redemption.user_id == user_id)
        .group_by(Redemption.status)
    ).all()
    return {
        "user_id": str(user.id),
        "status": user.status,
        "risk_score": user.risk_score,
        "balance": user.balance,
        "email": user.email,
        "email_verified": user.email_verified,
        "created_at": user.created_at,
        "checkin_streak": user.checkin_streak,
        "total_credited": credited,
        "total_debited": debited,
        "verified_ad_receipts": verified_ads,
        "redemptions_by_status": {status: count for status, count in redemptions},
    }


@router.post("/users/{user_id}/status")
def set_user_status(
    user_id: uuid.UUID,
    status: str = Body(embed=True, pattern="^(active|banned)$"),
    db: Session = Depends(get_db),
):
    from sqlalchemy.exc import NoResultFound

    try:
        user = ledger.lock_user(db, user_id)
    except NoResultFound:
        raise HTTPException(status_code=404, detail="not_found")
    previous = user.status
    user.status = status
    log_fraud_event(
        db,
        "admin_status_change",
        user_id=user.id,
        detail={"from": previous, "to": status},
    )
    db.commit()
    return {"user_id": str(user.id), "status": user.status, "previous": previous}


@router.post("/jobs/mature-cashback")
def run_mature_cashback():
    return {"matured": jobs.mature_cashback()}
