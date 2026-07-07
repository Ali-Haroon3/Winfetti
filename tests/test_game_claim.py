"""Game claims: server-owned payouts, multipliers by server clock, hard caps."""

import uuid

from sqlalchemy import select

import pytest

from app.config import get_settings
from app.models import FraudEvent, User
from tests.conftest import auth_headers


@pytest.fixture
def settings():
    s = get_settings()
    snapshot = dict(s.__dict__)
    yield s
    for key, value in snapshot.items():
        setattr(s, key, value)


@pytest.fixture
def no_happy_hour(settings):
    settings.happy_hour_start_utc = 0
    settings.happy_hour_end_utc = 0
    return settings


def _claim(client, headers, game="wheel", event="seg_500", idem_key=None):
    return client.post(
        "/v1/game/claim",
        json={
            "game": game,
            "event": event,
            "idem_key": idem_key or f"claim-{uuid.uuid4()}",
        },
        headers=headers,
    )


def test_claim_pays_from_server_table(client, db, no_happy_hour):
    headers, _ = auth_headers(client)
    resp = _claim(client, headers, event="seg_500")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"awarded": 500, "balance": 500, "capped": False, "replay": False}


def test_unknown_game_or_event_rejected(client, db, no_happy_hour):
    headers, user_id = auth_headers(client)
    assert _claim(client, headers, game="poker").status_code == 404
    assert _claim(client, headers, event="seg_999999").status_code == 404
    # denials land in fraud_events
    events = db.execute(select(FraudEvent)).scalars().all()
    assert len(events) == 2
    assert all(e.kind == "game_claim_denied:unknown_game_event" for e in events)


def test_claim_replay_same_idem_key_credits_once(client, db, no_happy_hour):
    headers, user_id = auth_headers(client)
    first = _claim(client, headers, idem_key="fixed-idem-key-1").json()
    second = _claim(client, headers, idem_key="fixed-idem-key-1").json()
    assert first["awarded"] == 500
    assert second["awarded"] == 0
    assert second["replay"] is True
    assert second["balance"] == 500


def test_happy_hour_multiplier(client, db, settings):
    settings.happy_hour_start_utc = 0
    settings.happy_hour_end_utc = 24
    settings.happy_hour_multiplier = 2.0
    headers, _ = auth_headers(client)
    body = _claim(client, headers, event="seg_500").json()
    assert body["awarded"] == 1000


def test_daily_game_win_cap_clips_and_flags(client, db, no_happy_hour):
    settings = no_happy_hour
    settings.daily_game_win_cap = 1200
    headers, user_id = auth_headers(client)

    assert _claim(client, headers, event="seg_1000").json()["awarded"] == 1000
    body = _claim(client, headers, event="seg_500").json()
    assert body["awarded"] == 200  # clipped to the remaining room
    assert body["capped"] is True

    body = _claim(client, headers, event="seg_500").json()
    assert body["awarded"] == 0  # cap exhausted; idem_key still burned
    assert body["capped"] is True

    user = db.get(User, user_id)
    assert user.balance == 1200
    assert user.risk_score == 2
    flags = (
        db.execute(select(FraudEvent).where(FraudEvent.kind == "daily_cap_hit"))
        .scalars()
        .all()
    )
    assert len(flags) == 2


def test_daily_total_credit_cap_counts_all_credit_kinds(client, db, no_happy_hour):
    settings = no_happy_hour
    settings.daily_total_credit_cap = 600
    headers, user_id = auth_headers(client)

    # checkin credits 100, leaving 500 of total room
    assert client.post("/v1/checkin", headers=headers).json()["awarded"] == 100
    body = _claim(client, headers, event="seg_1000").json()
    assert body["awarded"] == 500
    assert body["capped"] is True
    user = db.get(User, user_id)
    assert user.balance == 600


def test_me_reflects_daily_state(client, db, no_happy_hour):
    headers, _ = auth_headers(client)
    _claim(client, headers, event="seg_1000")
    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 1000
    assert me["daily"]["game_win_credited_today"] == 1000
    assert me["daily"]["total_credited_today"] == 1000
    assert me["gold"] is False
