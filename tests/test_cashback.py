"""Affiliate cashback: pending on postback, coins only after maturation."""

import hashlib
import hmac
from datetime import timedelta

from sqlalchemy import func, select, update

from app import clock
from app.jobs import mature_cashback
from app.models import CashbackCredit, FraudEvent, LedgerEntry
from tests.conftest import auth_headers

SECRET = "affiliate-test-secret"


def _url(network, tx_id, user_id, commission_cents, event="sale", secret=SECRET):
    message = f"{network}:{tx_id}:{user_id}:{commission_cents}:{event}"
    sig = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return (
        f"/v1/webhooks/affiliate?network={network}&tx_id={tx_id}&user_id={user_id}"
        f"&commission_cents={commission_cents}&event={event}&signature={sig}"
    )


def test_disabled_without_secret(client, db):
    assert client.get(_url("impact", "o1", "x", 100)).status_code == 503


def test_sale_creates_pending_credit_no_coins_yet(client, db, settings):
    settings.affiliate_secret = SECRET
    headers, user_id = auth_headers(client)
    # $10 commission * 10,000 coins/$ * 50% share = 50,000 coins
    resp = client.get(_url("impact", "order-1", user_id, 1000))
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending"] is True
    assert resp.json()["coins"] == 50_000

    credit = db.execute(select(CashbackCredit)).scalar_one()
    assert credit.status == "pending"
    assert client.get("/v1/me", headers=headers).json()["balance"] == 0


def test_bad_signature_rejected_and_logged(client, db, settings):
    settings.affiliate_secret = SECRET
    _, user_id = auth_headers(client)
    url = _url("impact", "order-2", user_id, 1000, secret="wrong-secret")
    assert client.get(url).status_code == 403
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "affiliate_denied:invalid_signature")
        ).scalar_one()
        == 1
    )
    assert db.execute(select(func.count()).select_from(CashbackCredit)).scalar_one() == 0


def test_replayed_sale_creates_one_credit(client, db, settings):
    settings.affiliate_secret = SECRET
    _, user_id = auth_headers(client)
    url = _url("impact", "order-dup", user_id, 1000)
    assert client.get(url).json()["pending"] is True
    assert client.get(url).json()["pending"] is False
    assert db.execute(select(func.count()).select_from(CashbackCredit)).scalar_one() == 1


def test_oversized_commission_clamped(client, db, settings):
    settings.affiliate_secret = SECRET
    settings.max_cashback_coins_per_postback = 10_000
    _, user_id = auth_headers(client)
    resp = client.get(_url("impact", "order-big", user_id, 1_000_000))
    assert resp.json()["coins"] == 10_000
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "cashback_amount_clamped")
        ).scalar_one()
        == 1
    )


def test_reversal_cancels_pending_credit(client, db, settings):
    settings.affiliate_secret = SECRET
    headers, user_id = auth_headers(client)
    client.get(_url("impact", "order-rev", user_id, 1000))
    resp = client.get(_url("impact", "order-rev", user_id, 1000, event="reversal"))
    assert resp.json()["reversed"] is True

    # maturation must now skip it forever
    db.execute(update(CashbackCredit).values(matures_at=clock.now_utc()))
    db.commit()
    assert mature_cashback() == 0
    assert client.get("/v1/me", headers=headers).json()["balance"] == 0


def test_maturation_credits_only_due_rows(client, db, settings):
    settings.affiliate_secret = SECRET
    headers, user_id = auth_headers(client)
    client.get(_url("impact", "order-due", user_id, 1000))
    client.get(_url("impact", "order-not-due", user_id, 2000))
    db.execute(
        update(CashbackCredit)
        .where(CashbackCredit.tx_id == "impact:order-due")
        .values(matures_at=clock.now_utc() - timedelta(seconds=1))
    )
    db.commit()

    assert mature_cashback() == 1
    assert mature_cashback() == 0  # idempotent rerun

    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 50_000
    entry = db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "cashback")
    ).scalar_one()
    assert entry.idem_key == "cashback:impact:order-due"
    statuses = dict(
        db.execute(select(CashbackCredit.tx_id, CashbackCredit.status)).all()
    )
    assert statuses == {
        "impact:order-due": "matured",
        "impact:order-not-due": "pending",
    }


def test_reversal_after_maturity_flags_no_clawback(client, db, settings):
    settings.affiliate_secret = SECRET
    headers, user_id = auth_headers(client)
    client.get(_url("impact", "order-late", user_id, 1000))
    db.execute(update(CashbackCredit).values(matures_at=clock.now_utc()))
    db.commit()
    assert mature_cashback() == 1

    resp = client.get(_url("impact", "order-late", user_id, 1000, event="reversal"))
    assert resp.json()["reversed"] is False
    assert client.get("/v1/me", headers=headers).json()["balance"] == 50_000
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "cashback_reversal_after_maturity")
        ).scalar_one()
        == 1
    )


def test_admin_endpoint_runs_the_job(client, db, settings):
    settings.affiliate_secret = SECRET
    _, user_id = auth_headers(client)
    client.get(_url("impact", "order-admin", user_id, 1000))
    db.execute(update(CashbackCredit).values(matures_at=clock.now_utc()))
    db.commit()
    resp = client.post(
        "/admin/jobs/mature-cashback", headers={"X-Admin-Key": "test-admin-key"}
    )
    assert resp.json() == {"matured": 1}
