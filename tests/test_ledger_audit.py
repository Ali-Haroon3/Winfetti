"""The maintained balance column must always be provable from the ledger;
the audit job is that proof — sum AND full balance_after chain."""

from sqlalchemy import func, select, update

from app import ledger
from app.jobs import audit_ledger
from app.models import FraudEvent, LedgerEntry, User
from tests.conftest import ADMIN_HEADERS, make_user


def test_clean_ledger_passes(db):
    make_user(db, balance=1000)
    make_user(db)  # no ledger rows at all is also consistent

    report = audit_ledger()
    assert report["checked"] == 2
    assert report["mismatches"] == []
    assert db.execute(select(func.count()).select_from(FraudEvent)).scalar_one() == 0


def test_corrupted_balance_detected(db):
    user = make_user(db, balance=1000)
    db.execute(update(User).where(User.id == user.id).values(balance=1500))
    db.commit()

    report = audit_ledger()
    assert report["mismatches"] == [
        {
            "user_id": str(user.id),
            "balance": 1500,
            "ledger_sum": 1000,
            "last_balance_after": 1000,
            "chain_break_entry_ids": [],
        }
    ]
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "ledger_integrity_mismatch")
        ).scalar_one()
        == 1
    )


def test_broken_running_balance_detected(db):
    # balance still equals the sum, but the balance_after chain lies
    user = make_user(db, balance=1000)
    entry_id = db.execute(
        select(LedgerEntry.id).where(LedgerEntry.user_id == user.id)
    ).scalar_one()
    db.execute(
        update(LedgerEntry)
        .where(LedgerEntry.id == entry_id)
        .values(balance_after=999)
    )
    db.commit()

    report = audit_ledger()
    (mismatch,) = report["mismatches"]
    assert mismatch["balance"] == 1000
    assert mismatch["ledger_sum"] == 1000
    assert mismatch["last_balance_after"] == 999
    assert mismatch["chain_break_entry_ids"] == [entry_id]


def test_interior_corruption_survives_later_writes(db):
    """ledger.apply computes balance_after from users.balance, so a
    legitimate write after a corruption 'rebases' the tail and hides the bad
    row from any newest-entry check. The full chain walk must still find it."""
    user = make_user(db, balance=1000)
    ledger.apply_by_id(db, user.id, 100, "checkin", idem_key=f"audit-mid:{user.id}")
    db.commit()

    middle_id = db.execute(
        select(func.max(LedgerEntry.id)).where(LedgerEntry.user_id == user.id)
    ).scalar_one()
    db.execute(
        update(LedgerEntry)
        .where(LedgerEntry.id == middle_id)
        .values(balance_after=1099)  # should be 1100
    )
    db.commit()

    # a later legitimate write buries the corrupt row in the interior
    ledger.apply_by_id(db, user.id, 50, "game_win", idem_key=f"audit-tail:{user.id}")
    db.commit()

    # sum matches balance (1150) and the newest entry agrees — only the
    # chain walk can see the lie
    report = audit_ledger()
    (mismatch,) = report["mismatches"]
    assert mismatch["user_id"] == str(user.id)
    assert mismatch["balance"] == 1150
    assert mismatch["ledger_sum"] == 1150
    assert mismatch["chain_break_entry_ids"] != []


def test_admin_endpoint_runs_audit(client, db):
    make_user(db, balance=250)
    resp = client.post("/admin/jobs/audit-ledger", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"checked": 1, "mismatches": []}
