"""Server clock. Every time-based rule (streaks, happy hour, daily caps)
reads through here so tests can monkeypatch a single seam and so no code
path ever trusts a client-supplied timestamp."""

from datetime import date, datetime, timezone


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def today_utc() -> date:
    return now_utc().date()


def day_start_utc(d: date | None = None) -> datetime:
    d = d or today_utc()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
