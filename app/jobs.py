"""Scheduled jobs. Run from cron / Fly machines:

    python -m app.jobs mature-cashback

Also triggerable via POST /admin/jobs/mature-cashback.
"""

import logging
import sys

from sqlalchemy import select

from app import clock, ledger
from app.db import SessionLocal
from app.models import CashbackCredit

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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    if job == "mature-cashback":
        print(f"matured: {mature_cashback()}")
    else:
        print("usage: python -m app.jobs mature-cashback", file=sys.stderr)
        sys.exit(2)
