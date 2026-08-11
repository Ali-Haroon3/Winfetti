"""Scheduled jobs. Run from cron / Fly machines:

    python -m app.jobs mature-cashback
    python -m app.jobs audit-ledger

Also triggerable via POST /admin/jobs/{mature-cashback,audit-ledger}.
"""

import logging
import sys

from sqlalchemy import func, select

from app import clock, ledger
from app.db import SessionLocal
from app.fraud import log_fraud_event
from app.models import CashbackCredit, LedgerEntry, User

logger = logging.getLogger(__name__)


def mature_cashback(session_factory=SessionLocal) -> int:
    """Credit every pending cashback whose return window has passed.

    SKIP LOCKED makes concurrent runs safe (two schedulers can't grab the
    same row), and the ledger idem_key makes a crashed run safe to rerun.
    Each credit commits individually so one bad row can't stall the batch.
    """
    matured = 0
    with session_factory() as session:
        due_ids = (
            session.execute(
                select(CashbackCredit.id).where(
                    CashbackCredit.status == "pending",
                    CashbackCredit.matures_at <= clock.now_utc(),
                )
            )
            .scalars()
            .all()
        )

    for credit_id in due_ids:
        with session_factory() as session:
            credit = session.execute(
                select(CashbackCredit)
                .where(
                    CashbackCredit.id == credit_id,
                    CashbackCredit.status == "pending",
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if credit is None:  # reversed meanwhile, or another run has it
                continue
            user = ledger.lock_user(session, credit.user_id)
            ledger.apply(
                session,
                user,
                credit.coins,
                "cashback",
                idem_key=f"cashback:{credit.tx_id}",
                ref=credit.tx_id,
            )
            credit.status = "matured"
            session.commit()
            matured += 1

    if matured:
        logger.info("matured %d cashback credits", matured)
    return matured


def audit_ledger(session_factory=SessionLocal) -> dict:
    """Prove the maintained balance column against the append-only ledger:
    for every user, users.balance == SUM(ledger.amount) and every entry's
    balance_after == previous balance_after + amount (base 0).

    Per-user id order IS chain order: writes for one user serialize under
    the row lock, so a later entry always gets a larger id. Checking the
    whole chain (not just the newest entry) matters because ledger.apply
    computes balance_after from users.balance — a later legitimate write
    "rebases" onto the balance column, so a corrupted row can sink into the
    interior of the chain, where only a full walk finds it. Both invariants
    are permanent for committed rows: a legitimate write can neither create
    nor heal a violation, so nothing a concurrent writer does can mask
    corruption from the next run.

    Pass 1 scans without locks, so a claim landing mid-scan can throw false
    positives; every candidate is re-checked under the user row lock — the
    lock every writer holds — before being reported. Confirmed mismatches
    are logged to fraud_events as ledger_integrity_mismatch.
    """
    with session_factory() as session:
        checked = session.execute(
            select(func.count()).select_from(User)
        ).scalar_one()
        sum_mismatch = (
            session.execute(
                select(User.id)
                .outerjoin(LedgerEntry, LedgerEntry.user_id == User.id)
                .group_by(User.id, User.balance)
                .having(
                    User.balance != func.coalesce(func.sum(LedgerEntry.amount), 0)
                )
            )
            .scalars()
            .all()
        )
        chain = select(
            LedgerEntry.user_id.label("user_id"),
            (LedgerEntry.balance_after - LedgerEntry.amount).label("expected_prev"),
            func.lag(LedgerEntry.balance_after)
            .over(partition_by=LedgerEntry.user_id, order_by=LedgerEntry.id)
            .label("prev_after"),
        ).subquery()
        chain_mismatch = (
            session.execute(
                select(chain.c.user_id)
                .where(chain.c.expected_prev != func.coalesce(chain.c.prev_after, 0))
                .distinct()
            )
            .scalars()
            .all()
        )

    mismatches = []
    for user_id in sorted(set(sum_mismatch) | set(chain_mismatch)):
        with session_factory() as session:
            user = ledger.lock_user(session, user_id)
            rows = session.execute(
                select(LedgerEntry.id, LedgerEntry.amount, LedgerEntry.balance_after)
                .where(LedgerEntry.user_id == user_id)
                .order_by(LedgerEntry.id)
            ).all()
            ledger_sum = sum(r.amount for r in rows)
            prev = 0
            break_ids = []
            for r in rows:
                if r.balance_after != prev + r.amount:
                    break_ids.append(r.id)
                prev = r.balance_after
            if user.balance == ledger_sum and not break_ids:
                continue  # pass-1 caught a mid-write snapshot; consistent under the lock
            detail = {
                "balance": user.balance,
                "ledger_sum": ledger_sum,
                "last_balance_after": rows[-1].balance_after if rows else None,
                "chain_break_entry_ids": break_ids[:10],
            }
            log_fraud_event(
                session, "ledger_integrity_mismatch", user_id=user_id, detail=detail
            )
            session.commit()
            mismatches.append({"user_id": str(user_id), **detail})

    if mismatches:
        logger.error("ledger integrity: %d mismatched users", len(mismatches))
    return {"checked": checked, "mismatches": mismatches}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "mature-cashback":
        print(f"matured: {mature_cashback()}")
    elif job == "audit-ledger":
        report = audit_ledger()
        print(f"checked: {report['checked']}, mismatches: {report['mismatches']}")
        sys.exit(1 if report["mismatches"] else 0)
    else:
        print(
            "usage: python -m app.jobs {mature-cashback,audit-ledger}",
            file=sys.stderr,
        )
        sys.exit(2)
