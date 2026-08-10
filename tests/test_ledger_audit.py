"""The maintained balance column must always be provable from the ledger;
the audit job is that proof."""

from sqlalchemy import func, select, update

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
    # balance still equals the sum, but the running balance_after chain lies
    user = make_user(db, balance=1000)
    db.execute(
        update(LedgerEntry)
        .where(LedgerEntry.user_id == user.id)
        .values(balance_after=999)
    )
    db.commit()

    report = audit_ledger()
    (mismatch,) = report["mismatches"]
    assert mismatch["balance"] == 1000
    assert mismatch["ledger_sum"] == 1000
    assert mismatch["last_balance_after"] == 999


def test_admin_endpoint_runs_audit(client, db):
    make_user(db, balance=250)
    resp = client.post("/admin/jobs/audit-ledger", headers=ADMIN_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == {"checked": 1, "mismatches": []}
