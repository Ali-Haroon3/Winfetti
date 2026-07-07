"""Redemption gates, atomic holds under concurrency, and the review queue."""

from sqlalchemy import func, select

from app.db import SessionLocal
from app.fulfillment import StubFulfillment
from app.models import LedgerEntry, Redemption, User
from app.services import (
    DomainError,
    approve_redemption,
    create_redemption,
    deny_redemption,
)
from tests.conftest import ADMIN_HEADERS, auth_headers, make_user, run_threads


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

    def worker(i):
        with SessionLocal() as session:
            try:
                create_redemption(session, user.id, "amazon_5")
                session.commit()
                outcomes.append("ok")
            except DomainError as exc:
                session.rollback()
                outcomes.append(exc.code)

    errors = run_threads(6, worker)
    assert errors == []
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


def test_concurrent_approve_and_deny_never_both_win(db):
    """Regression: without a row lock on the redemption, an approve and a
    deny could both pass the status check — shipping the gift card AND
    refunding the hold. Exactly one may win."""
    for _ in range(5):  # the race needs a few attempts to interleave badly
        user = _eligible_user(db, balance=50_000)
        with SessionLocal() as s:
            redemption = create_redemption(s, user.id, "amazon_5")
            s.commit()
            rid = redemption.id
        stub = StubFulfillment()
        outcomes = {}

        def approver(_):
            with SessionLocal() as s:
                r = s.get(Redemption, rid)
                try:
                    approve_redemption(s, r, stub)
                    outcomes["approve"] = "ok"
                except DomainError as exc:
                    s.rollback()
                    outcomes["approve"] = exc.code

        def denier(_):
            with SessionLocal() as s:
                r = s.get(Redemption, rid)
                try:
                    deny_redemption(s, r)
                    s.commit()
                    outcomes["deny"] = "ok"
                except DomainError as exc:
                    s.rollback()
                    outcomes["deny"] = exc.code

        errors = run_threads(2, lambda i: approver(i) if i == 0 else denier(i))
        assert errors == []
        assert sorted(outcomes.values()).count("ok") == 1, outcomes

        with SessionLocal() as s:
            status = s.get(Redemption, rid).status
            refunds = s.execute(
                select(func.count())
                .select_from(LedgerEntry)
                .where(
                    LedgerEntry.kind == "redemption_refund",
                    LedgerEntry.ref == str(rid),
                )
            ).scalar_one()
        card_shipped = len(stub.orders) > 0
        refunded = refunds > 0
        assert not (card_shipped and refunded), (
            f"double payout: card shipped AND hold refunded (status={status})"
        )
        assert (status == "sent") == card_shipped
        assert (status == "denied") == refunded


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


def test_cooldown_blocks_rapid_resubmit(client, db, stub_fulfillment):
    """POST /v1/redemptions has no idem_key; the cooldown is what stops a
    network retry from shipping a second auto-approved gift card."""
    from app.config import get_settings

    user = _eligible_user(db, balance=500_000)
    headers = _login_as(client, db, user)
    for _ in range(2):  # unlock auto-approval
        rid = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        ).json()["id"]
        client.post(
            f"/admin/redemptions/{rid}/approve", headers=ADMIN_HEADERS
        ).raise_for_status()

    # push the setup redemptions out of the cooldown window
    from datetime import timedelta

    from app import clock

    db.execute(
        Redemption.__table__.update()
        .where(Redemption.user_id == user.id)
        .values(created_at=clock.now_utc() - timedelta(hours=2))
    )
    db.commit()

    get_settings().redemption_cooldown_seconds = 3600
    try:
        first = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        )
        assert first.status_code == 201
        assert first.json()["status"] == "sent"  # auto-approved
        retry = client.post(
            "/v1/redemptions", json={"sku": "amazon_5"}, headers=headers
        )
        assert retry.status_code == 429
        assert retry.json()["detail"] == "redemption_cooldown"
    finally:
        get_settings().redemption_cooldown_seconds = 0
    assert len(stub_fulfillment.orders) == 3  # not 4


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
