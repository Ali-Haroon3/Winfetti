"""The manual credit path: goes through the ledger, capped, idempotent,
and every real move leaves an audit row."""

import uuid

from sqlalchemy import func, select

from app import clock, ledger
from app.models import FraudEvent, LedgerEntry, User
from tests.conftest import ADMIN_HEADERS, make_user


def _adjust(client, user_id, amount, idem="adjust-key-1", reason="support: test"):
    return client.post(
        f"/admin/users/{user_id}/adjust",
        headers=ADMIN_HEADERS,
        json={"amount": amount, "reason": reason, "idem_key": idem},
    )


def test_credit_then_replay(client, db):
    user = make_user(db)
    resp = _adjust(client, user.id, 500)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "user_id": str(user.id),
        "amount": 500,
        "balance": 500,
        "replay": False,
    }

    replay = _adjust(client, user.id, 500).json()
    assert replay == {
        "user_id": str(user.id),
        "amount": 0,
        "balance": 500,
        "replay": True,
    }

    entry = db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "admin_adjust")
    ).scalar_one()
    assert entry.idem_key == f"admin_adjust:{user.id}:adjust-key-1"
    assert entry.ref == "support: test"
    # one audit row, not two: the replay moved nothing
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "admin_adjust")
        ).scalar_one()
        == 1
    )


def test_debit(client, db):
    user = make_user(db, balance=1000)
    resp = _adjust(client, user.id, -400, idem="debit-key-1")
    assert resp.status_code == 200, resp.text
    assert resp.json()["balance"] == 600


def test_debit_cannot_go_negative(client, db):
    user = make_user(db, balance=100)
    resp = _adjust(client, user.id, -500, idem="debit-key-2")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "insufficient_balance"
    db.expire_all()
    assert db.get(User, user.id).balance == 100
    assert (
        db.execute(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.amount < 0)
        ).scalar_one()
        == 0
    )


def test_over_cap_rejected(client, db, settings):
    user = make_user(db)
    resp = _adjust(client, user.id, settings.admin_adjust_max_coins + 1, idem="cap-key-1")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "adjustment_too_large"


def test_zero_amount_rejected(client, db):
    user = make_user(db)
    assert _adjust(client, user.id, 0).status_code == 400


def test_unknown_user_404(client, db):
    assert _adjust(client, uuid.uuid4(), 100).status_code == 404


def test_requires_admin_key(client, db):
    user = make_user(db)
    resp = client.post(
        f"/admin/users/{user.id}/adjust",
        json={"amount": 100, "reason": "no key", "idem_key": "no-key-here"},
    )
    assert resp.status_code == 403


def test_adjust_does_not_consume_daily_earning_room(client, db):
    user = make_user(db)
    _adjust(client, user.id, 500)
    assert (
        ledger.credited_since(
            db, user.id, clock.day_start_utc(), kinds=ledger.EARNING_KINDS
        )
        == 0
    )
