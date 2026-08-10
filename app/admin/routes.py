import uuid
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app import clock, jobs, ledger
from app.auth import require_admin
from app.db import get_db
from app.fraud import log_fraud_event
from app.models import (
    AdReceipt,
    CashbackCredit,
    FraudEvent,
    LedgerEntry,
    Redemption,
    User,
)
from app.routes.redemptions import get_fulfillment
from app.schemas import (
    AdminAdjustRequest,
    AdminAdjustResponse,
    AdminLedgerEntryItem,
    AdminLedgerPage,
    AdminRedemptionResponse,
)
from app import services
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


@router.get("/users/{user_id}/ledger", response_model=AdminLedgerPage)
def user_ledger(
    user_id: uuid.UUID,
    limit: int = Query(default=100, ge=1, le=500),
    before_id: int | None = Query(default=None, ge=1),
    kind: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
):
    """Full ledger for one user, newest first, idem keys included — the
    audit view for support disputes ("where did these coins come from?")."""
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="not_found")
    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user_id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(LedgerEntry.id < before_id)
    if kind is not None:
        stmt = stmt.where(LedgerEntry.kind == kind)
    rows = db.execute(stmt).scalars().all()
    return AdminLedgerPage(
        entries=[AdminLedgerEntryItem.model_validate(r) for r in rows],
        next_cursor=rows[-1].id if len(rows) == limit else None,
    )


@router.post("/users/{user_id}/adjust", response_model=AdminAdjustResponse)
def adjust(
    user_id: uuid.UUID,
    body: AdminAdjustRequest,
    db: Session = Depends(get_db),
):
    try:
        result = services.adjust_balance(
            db, user_id, body.amount, body.reason, body.idem_key
        )
        db.commit()
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=404, detail="not_found")
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return AdminAdjustResponse(
        user_id=user_id,
        amount=result.applied,
        balance=result.balance,
        replay=result.replay,
    )


@router.get("/stats/economy")
def economy_stats(db: Session = Depends(get_db)):
    """One screen of the numbers that run the business: outstanding coin
    liability, committed redemption dollars, and today's credit flow."""
    day_start = clock.day_start_utc()

    coins_outstanding = db.execute(
        select(func.coalesce(func.sum(User.balance), 0))
    ).scalar_one()
    total_users, new_users_today = db.execute(
        select(
            func.count(),
            func.coalesce(func.count().filter(User.created_at >= day_start), 0),
        ).select_from(User)
    ).one()
    active_users_today = db.execute(
        select(func.count(func.distinct(LedgerEntry.user_id))).where(
            LedgerEntry.created_at >= day_start
        )
    ).scalar_one()

    credited_by_kind = dict(
        db.execute(
            select(LedgerEntry.kind, func.sum(LedgerEntry.amount))
            .where(LedgerEntry.created_at >= day_start, LedgerEntry.amount > 0)
            .group_by(LedgerEntry.kind)
        ).all()
    )
    debited_today = db.execute(
        select(func.coalesce(func.sum(-LedgerEntry.amount), 0)).where(
            LedgerEntry.created_at >= day_start, LedgerEntry.amount < 0
        )
    ).scalar_one()

    # The two open states are different liabilities: pending coins are
    # refundable holds (deny gives them back), approved coins are already
    # burned and it's the dollars that are committed (order may be in
    # flight). Don't blend them into one number.
    open_by_status = {
        status: {"count": count, "coins": coins, "usd": usd}
        for status, count, coins, usd in db.execute(
            select(
                Redemption.status,
                func.count(),
                func.coalesce(func.sum(Redemption.coins), 0),
                func.coalesce(func.sum(Redemption.usd), 0),
            )
            .where(Redemption.status.in_(["pending", "approved"]))
            .group_by(Redemption.status)
        ).all()
    }
    pending = open_by_status.get("pending", {"count": 0, "coins": 0, "usd": 0})
    approved = open_by_status.get("approved", {"count": 0, "coins": 0, "usd": 0})
    # sent_at, not reviewed_at: approval and fulfillment can land on
    # different days (approve commits before the external call and retries).
    sent_today_usd = db.execute(
        select(func.coalesce(func.sum(Redemption.usd), 0)).where(
            Redemption.status == "sent", Redemption.sent_at >= day_start
        )
    ).scalar_one()

    pending_cashback = db.execute(
        select(func.coalesce(func.sum(CashbackCredit.coins), 0)).where(
            CashbackCredit.status == "pending"
        )
    ).scalar_one()

    return {
        "coins_outstanding": coins_outstanding,
        "pending_cashback_coins": pending_cashback,
        "users": {
            "total": total_users,
            "new_today": new_users_today,
            "active_today": active_users_today,
        },
        "today": {
            "credited_by_kind": credited_by_kind,
            "credited_total": sum(credited_by_kind.values()),
            "debited_total": debited_today,
        },
        "redemption_liability": {
            "pending": {
                "count": pending["count"],
                "coins_on_hold": pending["coins"],
                "usd_if_approved": pending["usd"],
            },
            "approved_unsent": {
                "count": approved["count"],
                "usd_committed": approved["usd"],
            },
        },
        "sent_today_usd": sent_today_usd,
    }


@router.post("/jobs/mature-cashback")
def run_mature_cashback():
    return {"matured": jobs.mature_cashback()}


@router.post("/jobs/audit-ledger")
def run_audit_ledger():
    return jobs.audit_ledger()
