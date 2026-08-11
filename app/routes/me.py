from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, ledger, services
from app.auth import get_current_user
from app.config import get_settings
from app.cursor import decode_cursor, encode_cursor
from app.db import get_db
from app.models import CashbackCredit, LedgerEntry, User
from app.schemas import (
    CashbackEntryItem,
    CashbackListResponse,
    DailyState,
    LedgerEntryItem,
    LedgerPage,
    MeResponse,
)

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings()
    day_start = clock.day_start_utc()
    return MeResponse(
        user_id=user.id,
        balance=user.balance,
        gold=services.gold_active(db, user),
        boost_until=user.boost_until if services.boost_active(user) else None,
        email=user.email,
        email_verified=user.email_verified,
        daily=DailyState(
            game_win_credited_today=ledger.credited_since(
                db, user.id, day_start, kinds=["game_win"]
            ),
            total_credited_today=ledger.credited_since(
                db, user.id, day_start, kinds=ledger.EARNING_KINDS
            ),
            daily_game_win_cap=settings.daily_game_win_cap,
            daily_total_credit_cap=settings.daily_total_credit_cap,
            checkin_streak=user.checkin_streak,
            checked_in_today=user.last_checkin_date == clock.today_utc(),
            happy_hour_active=services.happy_hour_active(),
        ),
    )


# Kinds whose ref is the user's own data (game outcome, streak day, their
# redemption id). Everything else — admin notes, ad/offer/cashback network
# tx ids — is internal and never leaves the server through this endpoint.
USER_VISIBLE_REF_KINDS = {"game_win", "checkin", "redemption_hold", "redemption_refund"}


@router.get("/me/ledger", response_model=LedgerPage)
def my_ledger(
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=256),
    kind: str | None = Query(default=None, max_length=64),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Coin history, newest first. The cursor keeps pages stable while new
    entries land on top; it's opaque so the global ledger sequence stays
    server-side."""
    before_id = None
    if cursor is not None:
        before_id = decode_cursor(cursor)
        if before_id is None:
            raise HTTPException(status_code=400, detail="invalid_cursor")

    stmt = (
        select(LedgerEntry)
        .where(LedgerEntry.user_id == user.id)
        .order_by(LedgerEntry.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(LedgerEntry.id < before_id)
    if kind is not None:
        stmt = stmt.where(LedgerEntry.kind == kind)
    rows = db.execute(stmt).scalars().all()
    return LedgerPage(
        entries=[
            LedgerEntryItem(
                amount=r.amount,
                balance_after=r.balance_after,
                kind=r.kind,
                ref=r.ref if r.kind in USER_VISIBLE_REF_KINDS else None,
                created_at=r.created_at,
            )
            for r in rows
        ],
        next_cursor=encode_cursor(rows[-1].id) if len(rows) == limit else None,
    )


@router.get("/me/cashback", response_model=CashbackListResponse)
def my_cashback(
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Cashback credits with their maturation dates, newest first. Pending
    coins are not in the balance yet — they land when the return window
    passes."""
    rows = (
        db.execute(
            select(CashbackCredit)
            .where(CashbackCredit.user_id == user.id)
            .order_by(CashbackCredit.id.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    pending = db.execute(
        select(func.coalesce(func.sum(CashbackCredit.coins), 0)).where(
            CashbackCredit.user_id == user.id, CashbackCredit.status == "pending"
        )
    ).scalar_one()
    return CashbackListResponse(
        pending_coins=pending,
        entries=[CashbackEntryItem.model_validate(r) for r in rows],
    )
