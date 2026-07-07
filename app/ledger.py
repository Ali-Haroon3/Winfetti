"""Append-only coin ledger.

Every credit/debit goes through apply(). Invariants:

- The caller's transaction holds SELECT ... FOR UPDATE on the user row, so
  balance updates for one user are serialized.
- idem_key is unique-constrained; a retry (client or webhook) returns the
  original entry instead of writing a second one.
- users.balance is updated in the same transaction as the ledger insert and
  always equals the latest balance_after.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import LedgerEntry, User


class InsufficientBalance(Exception):
    pass


@dataclass
class LedgerResult:
    entry: LedgerEntry
    created: bool  # False when idem_key was already used (no-op replay)
    balance_after: int


def lock_user(session: Session, user_id: uuid.UUID) -> User:
    """Row-lock the user for the rest of the transaction."""
    return session.execute(
        select(User).where(User.id == user_id).with_for_update()
    ).scalar_one()


def find_by_idem_key(session: Session, idem_key: str) -> LedgerEntry | None:
    return session.execute(
        select(LedgerEntry).where(LedgerEntry.idem_key == idem_key)
    ).scalar_one_or_none()


def apply(
    session: Session,
    user: User,
    amount: int,
    kind: str,
    idem_key: str,
    ref: str | None = None,
) -> LedgerResult:
    """Write one ledger entry and move the balance. `user` must already be
    locked via lock_user() in this transaction.

    Replays (same idem_key) return the existing entry with created=False.
    Debits that would take the balance negative raise InsufficientBalance.
    """
    existing = find_by_idem_key(session, idem_key)
    if existing is not None:
        return LedgerResult(entry=existing, created=False, balance_after=user.balance)

    new_balance = user.balance + amount
    if new_balance < 0:
        raise InsufficientBalance(
            f"balance {user.balance} cannot cover debit of {abs(amount)}"
        )

    entry = LedgerEntry(
        user_id=user.id,
        amount=amount,
        balance_after=new_balance,
        kind=kind,
        ref=ref,
        idem_key=idem_key,
    )
    user.balance = new_balance
    session.add(entry)
    session.flush()  # surface unique-violation / assign id inside the tx
    return LedgerResult(entry=entry, created=True, balance_after=new_balance)


def apply_by_id(
    session: Session,
    user_id: uuid.UUID,
    amount: int,
    kind: str,
    idem_key: str,
    ref: str | None = None,
) -> LedgerResult:
    """Convenience wrapper: lock the user, then apply. Caller still commits."""
    user = lock_user(session, user_id)
    return apply(session, user, amount, kind, idem_key, ref=ref)


def credited_since(
    session: Session,
    user_id: uuid.UUID,
    since: datetime,
    kinds: list[str] | None = None,
) -> int:
    """Sum of positive ledger amounts for a user since `since` (used for the
    daily caps). Call while holding the user lock so concurrent claims for
    the same user can't both read a stale sum."""
    stmt = select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
        LedgerEntry.user_id == user_id,
        LedgerEntry.created_at >= since,
        LedgerEntry.amount > 0,
    )
    if kinds is not None:
        stmt = stmt.where(LedgerEntry.kind.in_(kinds))
    return session.execute(stmt).scalar_one()
