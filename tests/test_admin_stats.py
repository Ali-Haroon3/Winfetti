"""Economy overview and the per-user ledger audit view."""

import uuid
from datetime import timedelta
from decimal import Decimal

from app import clock, ledger
from app.models import Redemption
from tests.conftest import ADMIN_HEADERS, auth_headers, make_user


def test_economy_stats(client, db, no_happy_hour, monkeypatch):
    # Pin the request-time clock before seeding: every row created below is
    # stamped after t0, so "today" (>= day_start(t0)) always contains them —
    # no flake when the suite straddles midnight UTC.
    t0 = clock.now_utc()
    monkeypatch.setattr(clock, "now_utc", lambda: t0)

    headers, _ = auth_headers(client)
    resp = client.post(
        "/v1/game/claim",
        headers=headers,
        json={"game": "wheel", "event": "seg_100", "idem_key": "stats-claim-1"},
    )
    assert resp.json()["awarded"] == 100

    seeded = make_user(db, balance=1000)
    db.add(
        Redemption(
            user_id=seeded.id,
            sku="amazon_5",
            usd=Decimal("5.00"),
            coins=50_000,
            status="pending",
        )
    )
    # approved-but-unsent: coins already burned, dollars committed
    db.add(
        Redemption(
            user_id=seeded.id,
            sku="visa_10",
            usd=Decimal("10.00"),
            coins=100_000,
            status="approved",
            reviewed_at=t0,
        )
    )
    # sent today from a yesterday approval: must count via sent_at,
    # not reviewed_at
    db.add(
        Redemption(
            user_id=seeded.id,
            sku="amazon_25",
            usd=Decimal("25.00"),
            coins=250_000,
            status="sent",
            reviewed_at=t0 - timedelta(days=1),
            sent_at=t0,
        )
    )
    # sent yesterday: excluded
    db.add(
        Redemption(
            user_id=seeded.id,
            sku="amazon_5",
            usd=Decimal("5.00"),
            coins=50_000,
            status="sent",
            reviewed_at=t0 - timedelta(days=2),
            sent_at=t0 - timedelta(days=1),
        )
    )
    db.commit()

    stats = client.get("/admin/stats/economy", headers=ADMIN_HEADERS).json()
    assert stats["coins_outstanding"] == 1100
    assert stats["users"] == {"total": 2, "new_today": 2, "active_today": 2}
    assert stats["today"]["credited_by_kind"] == {
        "game_win": 100,
        "admin_adjust": 1000,
    }
    assert stats["today"]["credited_total"] == 1100
    assert stats["today"]["debited_total"] == 0
    assert stats["redemption_liability"] == {
        "pending": {"count": 1, "coins_on_hold": 50_000, "usd_if_approved": 5.0},
        "approved_unsent": {"count": 1, "usd_committed": 10.0},
    }
    assert stats["sent_today_usd"] == 25.0
    assert stats["pending_cashback_coins"] == 0


def test_admin_user_ledger_with_pagination_and_kind(client, db):
    user = make_user(db, balance=1000)  # seed entry, kind admin_adjust
    ledger.apply_by_id(db, user.id, 100, "checkin", idem_key=f"al-1:{user.id}")
    ledger.apply_by_id(db, user.id, 50, "game_win", idem_key=f"al-2:{user.id}")
    db.commit()

    resp = client.get(f"/admin/users/{user.id}/ledger?limit=2", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert [e["balance_after"] for e in page["entries"]] == [1150, 1100]
    assert page["entries"][1]["idem_key"] == f"al-1:{user.id}"
    assert page["next_cursor"] == page["entries"][-1]["id"]

    page2 = client.get(
        f"/admin/users/{user.id}/ledger?limit=2&before_id={page['next_cursor']}",
        headers=ADMIN_HEADERS,
    ).json()
    assert [e["balance_after"] for e in page2["entries"]] == [1000]
    assert page2["entries"][0]["idem_key"] == f"seed:{user.id}"
    assert page2["next_cursor"] is None

    filtered = client.get(
        f"/admin/users/{user.id}/ledger?kind=checkin", headers=ADMIN_HEADERS
    ).json()
    assert [e["kind"] for e in filtered["entries"]] == ["checkin"]


def test_admin_user_ledger_unknown_user(client):
    resp = client.get(
        f"/admin/users/{uuid.uuid4()}/ledger", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 404
