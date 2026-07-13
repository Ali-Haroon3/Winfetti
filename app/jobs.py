"""Maintenance jobs. The in-process scheduler (app/scheduler.py) runs these
automatically; they can also be run from a shell / external cron:

    python -m app.jobs mature-cashback
    python -m app.jobs retry-approved
    python -m app.jobs purge-email-tokens

or triggered via POST /admin/jobs/{mature-cashback,retry-approved,
purge-email-tokens}. Every job is safe to run concurrently from several
processes: rows are claimed with SKIP LOCKED, coins move through the
idempotent ledger, and fulfillment dedupes on external_id.
"""

import logging
import sys
from datetime import timedelta

from sqlalchemy import delete, select

from app import clock, ledger
from app.config import get_settings
from app.db import SessionLocal
from app.fulfillment import FulfillmentClient, build_fulfillment_client
from app.models import CashbackCredit, EmailVerification, Redemption
from app.services import DomainError, approve_redemption

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


def retry_stuck_redemptions(
    fulfillment: FulfillmentClient | None = None, session_factory=SessionLocal
) -> int:
    """Re-drive fulfillment for redemptions stranded in 'approved'.

    approve_redemption deliberately commits the approved state before the
    external call, so a fulfillment failure (or a crash mid-call) leaves the
    row 'approved' with the coins already held and no gift card sent. Those
    rows only needed a human to notice them before this job existed.

    Re-approving is always safe — the provider dedupes on external_id — but
    only rows approved more than redemption_retry_stuck_after_seconds ago
    are touched, so the job doesn't pointlessly double-call an order that's
    still in flight. Rows are claimed SKIP LOCKED and each retried in its
    own transaction so one bad row can't stall the batch.
    """
    settings = get_settings()
    if fulfillment is None:
        fulfillment = build_fulfillment_client()
    cutoff = clock.now_utc() - timedelta(
        seconds=settings.redemption_retry_stuck_after_seconds
    )

    with session_factory() as session:
        stuck_ids = (
            session.execute(
                select(Redemption.id).where(
                    Redemption.status == "approved",
                    Redemption.reviewed_at <= cutoff,
                )
            )
            .scalars()
            .all()
        )

    retried = 0
    for redemption_id in stuck_ids:
        with session_factory() as session:
            redemption = session.execute(
                select(Redemption)
                .where(
                    Redemption.id == redemption_id,
                    Redemption.status == "approved",
                )
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if redemption is None:  # resolved meanwhile, or another run has it
                continue
            try:
                approve_redemption(session, redemption, fulfillment)
                retried += 1
            except DomainError as exc:
                session.rollback()
                logger.warning(
                    "retry of stuck redemption %s failed: %s (will retry next run)",
                    redemption_id,
                    exc.code,
                )

    if retried:
        logger.info("re-drove %d stuck redemptions", retried)
    return retried


def purge_expired_email_verifications(session_factory=SessionLocal) -> int:
    """Delete verification rows whose tokens can never be used again.

    Rows are kept for email_token_purge_after_days past expiry (recent
    activity stays visible for support), then dropped so the table stays
    bounded. expires_at is set at creation, so consumed and superseded rows
    age out on the same clock.
    """
    settings = get_settings()
    cutoff = clock.now_utc() - timedelta(days=settings.email_token_purge_after_days)
    with session_factory() as session:
        result = session.execute(
            delete(EmailVerification).where(EmailVerification.expires_at < cutoff)
        )
        session.commit()
    purged = result.rowcount or 0
    if purged:
        logger.info("purged %d expired email verification tokens", purged)
    return purged


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    JOBS = {
        "mature-cashback": lambda: print(f"matured: {mature_cashback()}"),
        "retry-approved": lambda: print(f"retried: {retry_stuck_redemptions()}"),
        "purge-email-tokens": lambda: print(
            f"purged: {purge_expired_email_verifications()}"
        ),
    }
    job = sys.argv[1] if len(sys.argv) > 1 else ""
    run = JOBS.get(job)
    if run is None:
        print(f"usage: python -m app.jobs {{{'|'.join(JOBS)}}}", file=sys.stderr)
        sys.exit(2)
    run()
