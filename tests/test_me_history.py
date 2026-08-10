"""User-facing coin history (/v1/me/ledger) and cashback visibility
(/v1/me/cashback)."""

from datetime import timedelta

from sqlalchemy import update

from app import clock
from app.jobs import mature_cashback
from app.models import CashbackCredit
from app.services import create_cashback
from tests.conftest import auth_headers


def _claim(client, headers, i):
    return client.post(
        "/v1/game/claim",
        headers=headers,
        json={"game": "wheel", "event": "seg_50", "idem_key": f"hist-{i:04d}"},
    )


def test_history_pages_newest_first_and_only_own_entries(client, db, no_happy_hour):
    headers, _ = auth_headers(client)
    other_headers, _ = auth_headers(client)
    for i in range(5):
        assert _claim(client, headers, i).status_code == 200
    assert _claim(client, other_headers, 99).status_code == 200

    page = client.get("/v1/me/ledger?limit=3", headers=headers).json()
    assert len(page["entries"]) == 3
    ids = [e["id"] for e in page["entries"]]
    assert ids == sorted(ids, reverse=True)
    assert page["next_cursor"] == ids[-1]
    assert all(e["amount"] == 50 and e["kind"] == "game_win" for e in page["entries"])
    assert page["entries"][0]["ref"] == "wheel:seg_50"

    page2 = client.get(
        f"/v1/me/ledger?limit=3&before_id={page['next_cursor']}", headers=headers
    ).json()
    # 2 left, not 3: the other user's claim must not bleed into this history
    assert len(page2["entries"]) == 2
    assert page2["next_cursor"] is None
    assert set(ids).isdisjoint({e["id"] for e in page2["entries"]})


def test_history_kind_filter(client, db, no_happy_hour):
    headers, _ = auth_headers(client)
    assert _claim(client, headers, 0).status_code == 200
    assert client.post("/v1/checkin", headers=headers).status_code == 200

    page = client.get("/v1/me/ledger?kind=checkin", headers=headers).json()
    assert [e["kind"] for e in page["entries"]] == ["checkin"]


def test_history_requires_auth(client):
    assert client.get("/v1/me/ledger").status_code == 401


def test_cashback_listing_tracks_maturation(client, db):
    headers, user_id = auth_headers(client)
    create_cashback(db, user_id, "impact", "hist-tx-1", 500)
    db.commit()

    body = client.get("/v1/me/cashback", headers=headers).json()
    assert body["pending_coins"] == 500
    (entry,) = body["entries"]
    assert entry["status"] == "pending"
    assert entry["network"] == "impact"
    assert entry["coins"] == 500
    assert entry["matures_at"] is not None

    db.execute(
        update(CashbackCredit).values(
            matures_at=clock.now_utc() - timedelta(seconds=1)
        )
    )
    db.commit()
    assert mature_cashback() == 1

    body = client.get("/v1/me/cashback", headers=headers).json()
    assert body["pending_coins"] == 0
    assert body["entries"][0]["status"] == "matured"


def test_cashback_requires_auth(client):
    assert client.get("/v1/me/cashback").status_code == 401
