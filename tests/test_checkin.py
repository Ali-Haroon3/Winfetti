"""Check-in streaks are computed from the server clock only."""

from datetime import date, timedelta

from sqlalchemy import func, select

from app import clock
from app.models import LedgerEntry
from tests.conftest import auth_headers

BASE_DAY = date(2026, 3, 10)


def _freeze_day(monkeypatch, day: date):
    monkeypatch.setattr(clock, "today_utc", lambda: day)


def test_first_checkin_starts_streak(client, db):
    headers, _ = auth_headers(client)
    body = client.post("/v1/checkin", headers=headers).json()
    assert body["streak"] == 1
    assert body["awarded"] == 100
    assert body["already_checked_in"] is False


def test_same_day_checkin_is_idempotent(client, db, monkeypatch):
    _freeze_day(monkeypatch, BASE_DAY)
    headers, _ = auth_headers(client)
    first = client.post("/v1/checkin", headers=headers).json()
    second = client.post("/v1/checkin", headers=headers).json()
    assert first["awarded"] == 100
    assert second["already_checked_in"] is True
    assert second["awarded"] == 0
    assert second["balance"] == first["balance"]

    count = db.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.kind == "checkin")
    ).scalar_one()
    assert count == 1


def test_consecutive_days_grow_streak_and_reward(client, db, monkeypatch):
    headers, _ = auth_headers(client)
    rewards = []
    for offset in range(3):
        _freeze_day(monkeypatch, BASE_DAY + timedelta(days=offset))
        rewards.append(client.post("/v1/checkin", headers=headers).json())
    assert [r["streak"] for r in rewards] == [1, 2, 3]
    assert [r["awarded"] for r in rewards] == [100, 200, 300]


def test_missed_day_resets_streak(client, db, monkeypatch):
    headers, _ = auth_headers(client)
    _freeze_day(monkeypatch, BASE_DAY)
    client.post("/v1/checkin", headers=headers)
    _freeze_day(monkeypatch, BASE_DAY + timedelta(days=1))
    client.post("/v1/checkin", headers=headers)
    # skip a day
    _freeze_day(monkeypatch, BASE_DAY + timedelta(days=3))
    body = client.post("/v1/checkin", headers=headers).json()
    assert body["streak"] == 1
    assert body["awarded"] == 100


def test_streak_reward_caps_at_day_seven(client, db, monkeypatch):
    headers, _ = auth_headers(client)
    for offset in range(9):
        _freeze_day(monkeypatch, BASE_DAY + timedelta(days=offset))
        body = client.post("/v1/checkin", headers=headers).json()
    assert body["streak"] == 9
    assert body["awarded"] == 1000  # day 7+ pays the day-7 reward
