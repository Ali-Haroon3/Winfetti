"""Domain operations. Each function runs inside the caller's transaction and
takes the user row lock itself; the caller (route or test) commits.

Raising DomainError rolls the whole transaction back, so a denied claim or
redemption never leaves a partial write behind. Fraud events for denials are
written by the route layer after the rollback.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, ledger
from app.config import get_settings
from app.fraud import log_fraud_event
from app.fulfillment import FulfillmentClient, FulfillmentError
from app.models import AdReceipt, Purchase, Redemption, User
from app.payouts import (
    BOOST_PRODUCTS,
    GAME_PAYOUTS,
    GOLD_PRODUCT_ID,
    MAX_SINGLE_WIN_BASE,
    REDEMPTION_CATALOG,
    checkin_reward,
)


class DomainError(Exception):
    def __init__(self, status_code: int, code: str, message: str = ""):
        self.status_code = status_code
        self.code = code
        self.message = message or code
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Multipliers


def happy_hour_active() -> bool:
    s = get_settings()
    return s.happy_hour_start_utc <= clock.now_utc().hour < s.happy_hour_end_utc


def gold_active(session: Session, user: User) -> bool:
    return (
        session.execute(
            select(func.count())
            .select_from(Purchase)
            .where(
                Purchase.user_id == user.id,
                Purchase.product_id == GOLD_PRODUCT_ID,
                Purchase.status == "active",
            )
        ).scalar_one()
        > 0
    )


def boost_active(user: User) -> bool:
    return user.boost_until is not None and user.boost_until > clock.now_utc()


def current_multiplier(session: Session, user: User) -> float:
    s = get_settings()
    mult = 1.0
    if happy_hour_active():
        mult *= s.happy_hour_multiplier
    if gold_active(session, user):
        mult *= s.gold_multiplier
    if boost_active(user):
        mult *= s.boost_multiplier
    return mult


# ---------------------------------------------------------------------------
# Game claims


@dataclass
class ClaimResult:
    awarded: int
    balance: int
    capped: bool
    replay: bool  # idem_key already seen; nothing new was credited


def claim_game_win(
    session: Session, user_id: uuid.UUID, game: str, event: str, idem_key: str
) -> ClaimResult:
    """Client-claimed win, server-priced and server-capped.

    The user row lock makes the cap reads race-free: two concurrent claims
    for one user serialize here, so the second sees the first's ledger rows.
    """
    settings = get_settings()
    payout = GAME_PAYOUTS.get(game, {}).get(event)
    if payout is None:
        raise DomainError(404, "unknown_game_event")

    # Scope the client-supplied key to this user: two users innocently
    # sending the same key must not collide on the global unique constraint,
    # and no client string can squat on a system key like "checkin:...".
    scoped_key = f"game:{user_id}:{idem_key}"

    user = ledger.lock_user(session, user_id)

    existing = ledger.find_by_idem_key(session, scoped_key)
    if existing is not None:
        return ClaimResult(
            awarded=0, balance=user.balance, capped=False, replay=True
        )

    base = min(payout, MAX_SINGLE_WIN_BASE.get(game, payout))
    amount = int(base * current_multiplier(session, user))

    day_start = clock.day_start_utc()
    capped = False

    game_today = ledger.credited_since(session, user.id, day_start, kinds=["game_win"])
    game_room = max(0, settings.daily_game_win_cap - game_today)
    if amount > game_room:
        amount, capped = game_room, True

    total_today = ledger.credited_since(
        session, user.id, day_start, kinds=ledger.EARNING_KINDS
    )
    total_room = max(0, settings.daily_total_credit_cap - total_today)
    if amount > total_room:
        amount, capped = total_room, True

    if capped:
        user.risk_score += 1
        log_fraud_event(
            session,
            "daily_cap_hit",
            user_id=user.id,
            detail={"game": game, "event": event, "credited": amount},
        )

    # A zero-amount entry still burns the idem_key, so a capped claim can't
    # be replayed for coins after midnight.
    result = ledger.apply(
        session, user, amount, "game_win", scoped_key, ref=f"{game}:{event}"
    )
    return ClaimResult(
        awarded=amount if result.created else 0,
        balance=result.balance_after,
        capped=capped,
        replay=not result.created,
    )


# ---------------------------------------------------------------------------
# Check-in


@dataclass
class CheckinResult:
    streak: int
    awarded: int
    balance: int
    already_checked_in: bool


def do_checkin(session: Session, user_id: uuid.UUID) -> CheckinResult:
    """Streak is computed purely from the server clock and server state."""
    user = ledger.lock_user(session, user_id)
    today = clock.today_utc()

    if user.last_checkin_date == today:
        return CheckinResult(
            streak=user.checkin_streak,
            awarded=0,
            balance=user.balance,
            already_checked_in=True,
        )

    if user.last_checkin_date == today - timedelta(days=1):
        user.checkin_streak += 1
    else:
        user.checkin_streak = 1
    user.last_checkin_date = today

    amount = checkin_reward(user.checkin_streak)
    result = ledger.apply(
        session,
        user,
        amount,
        "checkin",
        idem_key=f"checkin:{user.id}:{today.isoformat()}",
        ref=f"streak:{user.checkin_streak}",
    )
    return CheckinResult(
        streak=user.checkin_streak,
        awarded=amount if result.created else 0,
        balance=result.balance_after,
        already_checked_in=not result.created,
    )


# ---------------------------------------------------------------------------
# Redemptions


def _lock_redemption(session: Session, redemption_id: uuid.UUID) -> Redemption:
    """Row-lock a redemption so approve/deny can't race each other.
    populate_existing for the same reason as ledger.lock_user."""
    return session.execute(
        select(Redemption)
        .where(Redemption.id == redemption_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one()


def create_redemption(session: Session, user_id: uuid.UUID, sku: str) -> Redemption:
    """Gate, then atomically hold the coins and queue the redemption."""
    settings = get_settings()
    item = REDEMPTION_CATALOG.get(sku)
    if item is None:
        raise DomainError(404, "unknown_sku")

    user = ledger.lock_user(session, user_id)

    if not user.email_verified or not user.email:
        raise DomainError(403, "email_not_verified")

    min_age = timedelta(days=settings.redemption_min_account_age_days)
    if clock.now_utc() - user.created_at < min_age:
        raise DomainError(403, "account_too_new")

    verified_ads = session.execute(
        select(func.count())
        .select_from(AdReceipt)
        .where(AdReceipt.user_id == user.id, AdReceipt.verified.is_(True))
    ).scalar_one()
    if verified_ads < settings.redemption_min_verified_ad_receipts:
        raise DomainError(403, "not_enough_verified_ads")

    pending = session.execute(
        select(func.count())
        .select_from(Redemption)
        .where(Redemption.user_id == user.id, Redemption.status == "pending")
    ).scalar_one()
    if pending > 0:
        raise DomainError(409, "redemption_already_pending")

    # POST /v1/redemptions carries no idem_key, so a network-level retry
    # would otherwise create (and auto-approve-ship) a second redemption.
    if settings.redemption_cooldown_seconds > 0:
        last_created = session.execute(
            select(func.max(Redemption.created_at)).where(
                Redemption.user_id == user.id
            )
        ).scalar_one()
        if last_created is not None and (
            clock.now_utc() - last_created
        ).total_seconds() < settings.redemption_cooldown_seconds:
            raise DomainError(429, "redemption_cooldown")

    if user.balance < item["coins"]:
        raise DomainError(400, "insufficient_balance")

    redemption = Redemption(
        user_id=user.id, sku=sku, usd=item["usd"], coins=item["coins"]
    )
    session.add(redemption)
    session.flush()  # assign redemption.id for the idem_key

    ledger.apply(
        session,
        user,
        -item["coins"],
        "redemption_hold",
        idem_key=f"redemption_hold:{redemption.id}",
        ref=str(redemption.id),
    )
    return redemption


def auto_approvable(session: Session, redemption: Redemption, user: User) -> bool:
    """First N redemptions per user are always manual; after that, small
    orders from zero-risk users skip the queue."""
    settings = get_settings()
    if user.risk_score != 0:
        return False
    if redemption.usd >= Decimal(str(settings.auto_approve_max_usd)):
        return False
    approved_before = session.execute(
        select(func.count())
        .select_from(Redemption)
        .where(
            Redemption.user_id == user.id,
            Redemption.status.in_(["approved", "sent"]),
            Redemption.id != redemption.id,
        )
    ).scalar_one()
    return approved_before >= settings.auto_approve_after_n_approved


def approve_redemption(
    session: Session, redemption: Redemption, fulfillment: FulfillmentClient
) -> Redemption:
    """pending -> approved -> (Tremendous order) -> sent.

    The status check happens under a row lock so approve can't race deny
    (both re-read the row FOR UPDATE before acting). The approved state is
    committed before the external call — releasing the lock so it isn't held
    across network I/O — and a crash mid-fulfillment leaves an 'approved'
    row to retry. The provider dedupes on external_id, so neither a retry
    nor two concurrent approves can pay twice.
    """
    redemption = _lock_redemption(session, redemption.id)
    if redemption.status not in ("pending", "approved"):
        session.rollback()
        raise DomainError(409, "not_approvable", f"status is {redemption.status}")

    user = session.get(User, redemption.user_id)
    if user is None or not user.email:
        session.rollback()
        raise DomainError(409, "user_has_no_email")
    email = user.email

    if redemption.status == "pending":
        redemption.status = "approved"
        redemption.reviewed_at = clock.now_utc()
    session.commit()  # releases the row lock before the external call

    try:
        order_id = fulfillment.create_order(
            email=email,
            usd=redemption.usd,
            external_id=str(redemption.id),
        )
    except FulfillmentError as exc:
        raise DomainError(502, "fulfillment_failed", str(exc))

    redemption = _lock_redemption(session, redemption.id)
    redemption.tremendous_order_id = order_id
    redemption.status = "sent"
    session.commit()
    return redemption


def deny_redemption(session: Session, redemption: Redemption) -> Redemption:
    """Refund the hold and close the redemption, atomically.

    Locks the redemption row before the status check so a deny can never
    race an approve into refunding a fulfilled redemption. Lock order is
    redemption -> user everywhere.
    """
    redemption = _lock_redemption(session, redemption.id)
    if redemption.status != "pending":
        session.rollback()
        raise DomainError(409, "not_deniable", f"status is {redemption.status}")

    user = ledger.lock_user(session, redemption.user_id)
    ledger.apply(
        session,
        user,
        redemption.coins,
        "redemption_refund",
        idem_key=f"redemption_refund:{redemption.id}",
        ref=str(redemption.id),
    )
    redemption.status = "denied"
    redemption.reviewed_at = clock.now_utc()
    return redemption


# ---------------------------------------------------------------------------
# Webhook credit paths (Phase 2): every caller has already verified a
# third-party signature. tx ids are stored network-prefixed so two networks
# can never collide on the global unique constraint.


@dataclass
class VerifiedRewardResult:
    awarded: int
    replay: bool


def credit_verified_reward(
    session: Session,
    user_id: uuid.UUID,
    network: str,
    tx_id: str,
    amount: int,
    kind: str,
    payload: dict | None = None,
) -> VerifiedRewardResult:
    """Record the receipt and move the coins, idempotent on (network, tx_id).
    Raises NoResultFound for an unknown user — callers turn that into a 4xx."""
    scoped_tx = f"{network}:{tx_id}"
    user = ledger.lock_user(session, user_id)

    existing = session.execute(
        select(AdReceipt).where(AdReceipt.tx_id == scoped_tx)
    ).scalar_one_or_none()
    if existing is not None:
        return VerifiedRewardResult(awarded=0, replay=True)

    session.add(
        AdReceipt(
            user_id=user.id,
            network=network,
            tx_id=scoped_tx,
            payload=payload,
            verified=True,
        )
    )
    result = ledger.apply(
        session, user, amount, kind, idem_key=f"{kind}:{scoped_tx}", ref=scoped_tx
    )
    return VerifiedRewardResult(
        awarded=amount if result.created else 0, replay=not result.created
    )


def apply_entitlement_purchase(
    session: Session, user_id: uuid.UUID, product_id: str, store_tx_id: str
) -> bool:
    """Grant an IAP entitlement (gold subscription or boost consumable),
    idempotent on store_tx_id. Returns False on replay. The user row lock
    serializes boost_until math."""
    user = ledger.lock_user(session, user_id)

    existing = session.execute(
        select(Purchase).where(Purchase.store_tx_id == store_tx_id)
    ).scalar_one_or_none()
    if existing is not None:
        return False

    session.add(
        Purchase(
            user_id=user.id,
            product_id=product_id,
            store_tx_id=store_tx_id,
            status="active",
        )
    )
    if product_id in BOOST_PRODUCTS:
        now = clock.now_utc()
        base = user.boost_until if (user.boost_until and user.boost_until > now) else now
        user.boost_until = base + BOOST_PRODUCTS[product_id]
    session.flush()
    return True


def expire_gold(session: Session, user_id: uuid.UUID) -> int:
    """Mark the user's active gold purchases expired (subscription lapsed)."""
    ledger.lock_user(session, user_id)
    rows = (
        session.execute(
            select(Purchase).where(
                Purchase.user_id == user_id,
                Purchase.product_id == GOLD_PRODUCT_ID,
                Purchase.status == "active",
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = "expired"
    return len(rows)
