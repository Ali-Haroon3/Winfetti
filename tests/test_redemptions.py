"""Redemption gates, atomic holds under concurrency, and the review queue."""

import threading

from sqlalchemy import func, select

from app.db import SessionLocal
from app.models import LedgerEntry, Redemption, User
from app.services import DomainError, create_redemption
from tests.conftest import ADMIN_HEADERS, auth_headers, make_user


def _eligible_user(db, balance=200_000, **overrides):
    kwargs = dict(
        balance=balance,
        email="player@example.com",
        email_verified=True,
        age_days=30,
        verified_ads=10,
    )
    kwargs.update(overrides)
    return make_user(db, **kwargs)


def _login_as(client, db, user) -> dict:
    headers, _ = auth_headers(client, device_id=user.device_id)
    return headers


# ---------------------------------------------------------------------------
# Gates


def test_gates_reject_ineligible_users(client, db):
    cases = [
        (dict(email_verified=False), 403, "email_not_verified"),
        (dict(age_days=2), 403, "account_too_new"),
        (dict(verified_ads=3), 403, "not_enough_verified_ads"),
        (dict(balance=100), 400, "insufficient_balance"),
    ]
    for overrides, status, code in cases:
        user = _eligible_user(db, **overrides)
        headers = _login_as(client, db, user)
        resp = client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
        assert resp.status_code == status, f"{overrides}: {resp.text}"
        assert resp.json()["detail"] == code


def test_unknown_sku_404(client, db):
    user = _eligible_user(db)
    headers = _login_as(client, db, user)
    resp = client.post("/v1/redemptions", json={"sku": "yacht_9000"}, headers=headers)
    assert resp.status_code == 404


def test_second_pending_redemption_rejected(client, db):
    user = _eligible_user(db)
    headers = _login_as(client, db, user)
    assert (
        client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
        .status_code
        == 201
    )
    resp = client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "redemption_already_pending"


# ---------------------------------------------------------------------------
# The hold


def test_redemption_holds_coins_atomically(client, db):
    user = _eligible_user(db, balance=60_000)
    headers = _login_as(client, db, user)
    resp = client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["coins"] == 50_000

    db.refresh(user)
    assert user.balance == 10_000
    hold = db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "redemption_hold")
    ).scalar_one()
    assert hold.amount == -50_000
    assert hold.balance_after == 10_000


def test_concurrent_spends_only_one_wins(db):
    """Balance covers exactly one redemption; N concurrent attempts must
    produce exactly one hold and a non-negative balance."""
    user = _eligible_user(db, balance=50_000)
    db.commit()
    outcomes = []
    barrier = threading.Barrier(6)

    def worker(i):
        barrier.wait(timeout=10)
        with SessionLocal() as session:
            try:
                create_redemption(session, user.id, "amazon_5")
                session.commit()
                outcomes.append("ok")
            except DomainError as exc:
                session.rollback()
                outcomes.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert outcomes.count("ok") == 1, outcomes

    with SessionLocal() as session:
        balance = session.execute(
            select(User.balance).where(User.id == user.id)
        ).scalar_one()
        holds = session.execute(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.kind == "redemption_hold")
        ).scalar_one()
        pending = session.execute(
            select(func.count())
            .select_from(Redemption)
            .where(Redemption.user_id == user.id, Redemption.status == "pending")
        ).scalar_one()
    assert balance == 0
    assert holds == 1
    assert pending == 1


# ---------------------------------------------------------------------------
# Admin queue


def test_admin_requires_key(client, db):
    assert client.get("/admin/redemptions").status_code == 403
    assert (
        client.get("/admin/redemptions", headers={"X-Admin-Key": "wrong"}).status_code
        == 403
    )


def test_approve_sends_via_fulfillment(client, db, stub_fulfillment):
    user = _eligible_user(db)
    headers = _login_as(client, db, user)
    rid = client.post(
        "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
    ).json()["id"]

    listed = client.get("/admin/redemptions", headers=ADMIN_HEADERS).json()
    assert [r["id"] for r in listed] == [rid]

    resp = client.post(f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "sent"
    assert body["tremendous_order_id"].startswith("stub-")
    assert len(stub_fulfillment.orders) == 1
    order = stub_fulfillment.orders[0]
    assert order["email"] == "player@example.com"
    assert order["external_id"] == rid

    # approving again must not create a second order
    resp = client.post(f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS)
    assert resp.status_code == 409
    assert len(stub_fulfillment.orders) == 1


def test_deny_refunds_hold(client, db):
    user = _eligible_user(db, balance=50_000)
    headers = _login_as(client, db, user)
    rid = client.post(
        "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
    ).json()["id"]

    resp = client.post(f"/admin/redemptions/{rid}/deny", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "denied"

    db.refresh(user)
    assert user.balance == 50_000
    refund = db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "redemption_refund")
    ).scalar_one()
    assert refund.amount == 50_000

    # denying twice must not refund twice
    resp = client.post(f"/admin/redemptions/{rid}/deny", headers=ADMIN_HEADERS)
    assert resp.status_code == 409
    db.refresh(user)
    assert user.balance == 50_000


def test_fulfillment_failure_leaves_redemption_retryable(client, db, stub_fulfillment):
    user = _eligible_user(db)
    headers = _login_as(client, db, user)
    rid = client.post(
        "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
    ).json()["id"]

    stub_fulfillment.fail = True
    resp = client.post(f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS)
    assert resp.status_code == 502

    stub_fulfillment.fail = False
    resp = client.post(f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"


def test_auto_approve_after_two_manual_approvals(client, db, stub_fulfillment):
    user = _eligible_user(db, balance=500_000)
    headers = _login_as(client, db, user)

    # first two redemptions stay pending until an admin approves
    for _ in range(2):
        body = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        ).json()
        assert body["status"] == "pending"
        client.post(
            f"/admin/redemptions/{body['id']}/approve", headers=ADMIN_HEADERS
        ).raise_for_status()

    # third small redemption from a zero-risk user auto-approves
    body = client.post(
        "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
    ).json()
    assert body["status"] == "sent"
    assert len(stub_fulfillment.orders) == 3


def test_no_auto_approve_at_or_over_10_usd(client, db, stub_fulfillment):
    user = _eligible_user(db, balance=500_000)
    headers = _login_as(client, db, user)
    for _ in range(2):
        body = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        ).json()
        client.post(
            f"/admin/redemptions/{body['id']}/approve", headers=ADMIN_HEADERS
        ).raise_for_status()

    body = client.post(
        "/v1/redemptions", json={"sku": "amazon_10"}, headers=headers
    ).json()
    assert body["status"] == "pending"


def test_no_auto_approve_with_risk_score(client, db, stub_fulfillment):
    user = _eligible_user(db, balance=500_000, risk_score=1)
    headers = _login_as(client, db, user)
    # seed two prior approvals directly
    for i in range(2):
        rid = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        ).json()["id"]
        client.post(
            f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS
        ).raise_for_status()

    body = client.post(
        "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
    ).json()
    assert body["status"] == "pending"
