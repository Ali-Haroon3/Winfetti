"""Economy overview and the per-user ledger audit view."""

import uuid
from decimal import Decimal

from app.models import Redemption
from tests.conftest import ADMIN_HEADERS, auth_headers, make_user


def test_economy_stats(client, db, no_happy_hour):
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
        "open_count": 1,
        "coins_held": 50_000,
        "usd_committed": 5.0,
    }
    assert stats["sent_today_usd"] == 0
    assert stats["pending_cashback_coins"] == 0


def test_admin_user_ledger(client, db):
    user = make_user(db, balance=1000)
    resp = client.get(f"/admin/users/{user.id}/ledger", headers=ADMIN_HEADERS)
    assert resp.status_code == 200, resp.text
    (entry,) = resp.json()["entries"]
    assert entry["idem_key"] == f"seed:{user.id}"
    assert entry["amount"] == 1000
    assert resp.json()["next_cursor"] is None


def test_admin_user_ledger_unknown_user(client):
    resp = client.get(
        f"/admin/users/{uuid.uuid4()}/ledger", headers=ADMIN_HEADERS
    )
    assert resp.status_code == 404
