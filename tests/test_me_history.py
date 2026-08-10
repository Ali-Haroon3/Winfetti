"""User-facing coin history (/v1/me/ledger) and cashback visibility
(/v1/me/cashback)."""

from datetime import timedelta

from sqlalchemy import update

from app import clock
from app.jobs import mature_cashback
from app.models import CashbackCredit
from app.services import create_cashback
from tests.conftest import ADMIN_HEADERS, auth_headers


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
    # newest first: running balances 250, 200, 150
    assert [e["balance_after"] for e in page["entries"]] == [250, 200, 150]
    assert all(e["amount"] == 50 and e["kind"] == "game_win" for e in page["entries"])
    assert page["entries"][0]["ref"] == "wheel:seg_50"
    assert page["next_cursor"]  # opaque continuation token

    page2 = client.get(
        f"/v1/me/ledger?limit=3&cursor={page['next_cursor']}", headers=headers
    ).json()
    # 2 left, not 3: the other user's claim must not bleed into this history
    assert [e["balance_after"] for e in page2["entries"]] == [100, 50]
    assert page2["next_cursor"] is None


def test_history_kind_filter_with_cursor(client, db, no_happy_hour):
    headers, _ = auth_headers(client)
    for i in range(3):
        assert _claim(client, headers, i).status_code == 200
    assert client.post("/v1/checkin", headers=headers).status_code == 200

    page = client.get("/v1/me/ledger?kind=checkin", headers=headers).json()
    assert [e["kind"] for e in page["entries"]] == ["checkin"]

    # cursor and kind compose: page through game wins two at a time
    page = client.get("/v1/me/ledger?kind=game_win&limit=2", headers=headers).json()
    assert [e["balance_after"] for e in page["entries"]] == [150, 100]
    page2 = client.get(
        f"/v1/me/ledger?kind=game_win&limit=2&cursor={page['next_cursor']}",
        headers=headers,
    ).json()
    assert [e["balance_after"] for e in page2["entries"]] == [50]
    assert page2["next_cursor"] is None


def test_forged_cursor_rejected(client, db):
    headers, _ = auth_headers(client)
    resp = client.get("/v1/me/ledger?cursor=not-a-real-cursor", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_cursor"


def test_internal_refs_masked(client, db, no_happy_hour):
    """Admin notes and network tx ids must never reach the user; the user's
    own game/checkin refs must."""
    headers, user_id = auth_headers(client)
    assert _claim(client, headers, 0).status_code == 200
    resp = client.post(
        f"/admin/users/{user_id}/adjust",
        headers=ADMIN_HEADERS,
        json={
            "amount": 100,
            "reason": "clawback: suspected multi-account fraud (case #4411)",
            "idem_key": "mask-adj-0001",
        },
    )
    assert resp.status_code == 200, resp.text

    entries = client.get("/v1/me/ledger", headers=headers).json()["entries"]
    by_kind = {e["kind"]: e for e in entries}
    assert by_kind["admin_adjust"]["ref"] is None
    assert by_kind["game_win"]["ref"] == "wheel:seg_50"


def test_cashback_ledger_ref_masked(client, db):
    headers, user_id = auth_headers(client)
    create_cashback(db, user_id, "impact", "mask-tx-9", 500)
    db.commit()
    db.execute(
        update(CashbackCredit).values(
            matures_at=clock.now_utc() - timedelta(seconds=1)
        )
    )
    db.commit()
    assert mature_cashback() == 1

    entries = client.get("/v1/me/ledger", headers=headers).json()["entries"]
    (entry,) = entries
    assert entry["kind"] == "cashback"
    assert entry["amount"] == 500
    assert entry["ref"] is None  # network tx id stays server-side


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
