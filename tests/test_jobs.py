"""Maintenance jobs: stuck-redemption retries and email-token purges.
(mature_cashback is covered alongside the webhook flow in test_cashback.py.)"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.db import SessionLocal
from app.jobs import purge_expired_email_verifications, retry_stuck_redemptions
from app.models import EmailVerification, Redemption, User
from app.services import DomainError, approve_redemption, create_redemption
from tests.conftest import ADMIN_HEADERS, make_user


def _stuck_redemption(db, stub, email="stuck@example.com"):
    """A redemption whose fulfillment call failed after the approved state
    was committed — exactly the state a crash or provider outage leaves."""
    user = make_user(
        db,
        balance=200_000,
        email=email,
        email_verified=True,
        age_days=30,
        verified_ads=10,
    )
    with SessionLocal() as s:
        redemption = create_redemption(s, user.id, "amazon_5")
        s.commit()
        rid = redemption.id

    stub.fail = True
    with SessionLocal() as s:
        with pytest.raises(DomainError):
            approve_redemption(s, s.get(Redemption, rid), stub)
        s.rollback()
    stub.fail = False

    with SessionLocal() as s:
        row = s.get(Redemption, rid)
        assert row.status == "approved" and row.tremendous_order_id is None
    return user, rid


def test_retry_redrives_stuck_approved_row(db, stub_fulfillment, settings):
    settings.redemption_retry_stuck_after_seconds = 0
    _, rid = _stuck_redemption(db, stub_fulfillment)

    assert retry_stuck_redemptions(stub_fulfillment) == 1
    with SessionLocal() as s:
        row = s.get(Redemption, rid)
        assert row.status == "sent"
        assert row.tremendous_order_id.startswith("stub-")
    assert [o["external_id"] for o in stub_fulfillment.orders] == [str(rid)]

    # nothing left to do; a rerun must not touch the sent row
    assert retry_stuck_redemptions(stub_fulfillment) == 0
    assert len(stub_fulfillment.orders) == 1


def test_retry_ignores_rows_inside_the_grace_window(db, stub_fulfillment, settings):
    """A just-approved row may still have its fulfillment call in flight in
    another process; the job must not double-drive it."""
    settings.redemption_retry_stuck_after_seconds = 900
    _, rid = _stuck_redemption(db, stub_fulfillment)

    assert retry_stuck_redemptions(stub_fulfillment) == 0
    with SessionLocal() as s:
        assert s.get(Redemption, rid).status == "approved"
    assert stub_fulfillment.orders == []


def test_retry_ignores_pending_and_denied_rows(db, stub_fulfillment, settings):
    settings.redemption_retry_stuck_after_seconds = 0
    user = make_user(
        db,
        balance=200_000,
        email="pending@example.com",
        email_verified=True,
        age_days=30,
        verified_ads=10,
    )
    with SessionLocal() as s:
        create_redemption(s, user.id, "amazon_5")
        s.commit()

    assert retry_stuck_redemptions(stub_fulfillment) == 0
    assert stub_fulfillment.orders == []


def test_one_bad_row_does_not_stall_the_batch(db, stub_fulfillment, settings):
    settings.redemption_retry_stuck_after_seconds = 0
    bad_user, bad_rid = _stuck_redemption(db, stub_fulfillment, email="bad@example.com")
    _, good_rid = _stuck_redemption(db, stub_fulfillment, email="good@example.com")

    # approve_redemption refuses a user with no email; the bad row must be
    # skipped and the good one still re-driven.
    with SessionLocal() as s:
        s.get(User, bad_user.id).email = None
        s.commit()

    assert retry_stuck_redemptions(stub_fulfillment) == 1
    with SessionLocal() as s:
        assert s.get(Redemption, bad_rid).status == "approved"
        assert s.get(Redemption, good_rid).status == "sent"


def test_admin_endpoint_runs_retry(client, db, stub_fulfillment, settings):
    settings.redemption_retry_stuck_after_seconds = 0
    _, rid = _stuck_redemption(db, stub_fulfillment)

    resp = client.post("/admin/jobs/retry-approved", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"retried": 1}
    with SessionLocal() as s:
        assert s.get(Redemption, rid).status == "sent"


# ---------------------------------------------------------------------------
# Email-token purge


def _verification(db, user, token_hash, expires_delta):
    row = EmailVerification(
        user_id=user.id,
        email="p@example.com",
        token_hash=token_hash,
        expires_at=clock.now_utc() + expires_delta,
    )
    db.add(row)
    db.commit()
    return row


def test_purge_drops_only_rows_past_the_grace_window(client, db, settings):
    settings.email_token_purge_after_days = 7
    user = make_user(db)
    _verification(db, user, "hash-old", timedelta(days=-8))  # purgeable
    _verification(db, user, "hash-recent", timedelta(days=-1))  # expired, in grace
    _verification(db, user, "hash-live", timedelta(hours=12))  # still valid

    assert purge_expired_email_verifications() == 1
    remaining = {
        row.token_hash
        for row in db.execute(select(EmailVerification)).scalars()
    }
    assert remaining == {"hash-recent", "hash-live"}

    resp = client.post("/admin/jobs/purge-email-tokens", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"purged": 0}
