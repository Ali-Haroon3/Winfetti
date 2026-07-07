"""The two invariants everything else rests on:

1. A duplicate idem_key never double-credits, even under concurrent requests.
2. Debits are atomic: concurrent spends can never drive a balance negative.
"""

import threading

import pytest
from sqlalchemy import func, select

from app import ledger
from app.db import SessionLocal
from app.ledger import InsufficientBalance
from app.models import LedgerEntry, User
from tests.conftest import make_user

N_THREADS = 12


def _run_threads(n, target):
    barrier = threading.Barrier(n)
    errors = []

    def wrapped(i):
        try:
            barrier.wait(timeout=10)
            target(i)
        except Exception as exc:  # noqa: BLE001 - collected and asserted on
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


def test_credit_updates_balance_and_ledger(db):
    user = make_user(db)
    result = ledger.apply_by_id(db, user.id, 500, "game_win", "k1")
    db.commit()
    assert result.created is True
    assert result.balance_after == 500

    db.refresh(user)
    assert user.balance == 500
    entry = db.execute(select(LedgerEntry)).scalar_one()
    assert entry.amount == 500
    assert entry.balance_after == 500


def test_duplicate_idem_key_sequential_is_noop(db):
    user = make_user(db)
    first = ledger.apply_by_id(db, user.id, 500, "game_win", "dup")
    db.commit()
    second = ledger.apply_by_id(db, user.id, 500, "game_win", "dup")
    db.commit()

    assert first.created and not second.created
    assert second.entry.id == first.entry.id
    db.refresh(user)
    assert user.balance == 500
    assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 1


def test_duplicate_idem_key_concurrent_never_double_credits(db):
    user = make_user(db)
    db.commit()

    def worker(_):
        with SessionLocal() as session:
            ledger.apply_by_id(session, user.id, 500, "game_win", "race-key")
            session.commit()

    errors = _run_threads(N_THREADS, worker)
    assert errors == []

    with SessionLocal() as session:
        count = session.execute(
            select(func.count()).select_from(LedgerEntry)
        ).scalar_one()
        balance = session.execute(
            select(User.balance).where(User.id == user.id)
        ).scalar_one()
    assert count == 1, f"expected exactly one ledger entry, got {count}"
    assert balance == 500, f"double credit: balance is {balance}"


def test_concurrent_distinct_credits_all_land_consistently(db):
    user = make_user(db)
    db.commit()

    def worker(i):
        with SessionLocal() as session:
            ledger.apply_by_id(session, user.id, 100, "game_win", f"k-{i}")
            session.commit()

    errors = _run_threads(N_THREADS, worker)
    assert errors == []

    with SessionLocal() as session:
        balance = session.execute(
            select(User.balance).where(User.id == user.id)
        ).scalar_one()
        entries = (
            session.execute(
                select(LedgerEntry).order_by(LedgerEntry.balance_after.asc())
            )
            .scalars()
            .all()
        )
    assert balance == N_THREADS * 100
    # balance_after forms a strict chain: 100, 200, ... with no gaps or dupes
    assert [e.balance_after for e in entries] == [
        100 * (i + 1) for i in range(N_THREADS)
    ]


def test_debit_insufficient_balance_raises(db):
    user = make_user(db, balance=100)
    with pytest.raises(InsufficientBalance):
        ledger.apply_by_id(db, user.id, -200, "redemption_hold", "hold-1")
    db.rollback()
    db.refresh(user)
    assert user.balance == 100


def test_concurrent_debits_never_go_negative(db):
    user = make_user(db, balance=500)
    db.commit()
    successes = []

    def worker(i):
        with SessionLocal() as session:
            try:
                ledger.apply_by_id(
                    session, user.id, -100, "redemption_hold", f"debit-{i}"
                )
                session.commit()
                successes.append(i)
            except InsufficientBalance:
                session.rollback()

    errors = _run_threads(N_THREADS, worker)
    assert errors == []
    assert len(successes) == 5  # 500 / 100: exactly five debits fit

    with SessionLocal() as session:
        balance = session.execute(
            select(User.balance).where(User.id == user.id)
        ).scalar_one()
        min_after = session.execute(
            select(func.min(LedgerEntry.balance_after))
        ).scalar_one()
    assert balance == 0
    assert min_after >= 0, "a debit drove the running balance negative"


def test_balance_always_matches_ledger_sum(db):
    user = make_user(db, balance=1000)
    ledger.apply_by_id(db, user.id, 250, "checkin", "c1")
    ledger.apply_by_id(db, user.id, -300, "redemption_hold", "h1")
    ledger.apply_by_id(db, user.id, 300, "redemption_refund", "r1")
    db.commit()

    db.refresh(user)
    ledger_sum = db.execute(
        select(func.sum(LedgerEntry.amount)).where(LedgerEntry.user_id == user.id)
    ).scalar_one()
    assert user.balance == ledger_sum == 1250
