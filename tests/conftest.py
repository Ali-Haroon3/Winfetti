import os
import uuid
from datetime import timedelta

# Must be set before any app import: Settings caches at first use.
# DATABASE_URL is force-overwritten (not setdefault): the schema fixture
# drops every table, and inheriting a stray DATABASE_URL from the shell
# must never point that at a real database. Override only via
# WINFETTI_TEST_DATABASE_URL, and the database name must end in "_test".
_TEST_DB_URL = os.environ.get(
    "WINFETTI_TEST_DATABASE_URL",
    "postgresql+psycopg://winfetti:winfetti@localhost:5432/winfetti_test",
)
if not _TEST_DB_URL.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
    raise RuntimeError(
        f"refusing to run tests against {_TEST_DB_URL!r}: database name must "
        "end in '_test' (tests drop all tables)"
    )
os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["JWT_SECRET"] = "test-secret-0123456789abcdef0123456789abcdef"
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["ADMIN_API_KEY"] = "test-admin-key"
os.environ["TREMENDOUS_API_KEY"] = ""
os.environ["REDEMPTION_COOLDOWN_SECONDS"] = "0"  # re-enabled per-test

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app import clock
from app.db import Base, SessionLocal, engine
from app.fulfillment import StubFulfillment
from app.main import app
from app.models import AdReceipt, FraudEvent, LedgerEntry, Purchase, Redemption, User

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key"}


def run_threads(n, target):
    """Start n threads on a barrier; return the list of escaped exceptions.
    Tests must assert the result is empty — a swallowed thread exception
    would otherwise turn a real failure into a silent pass."""
    import threading

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


@pytest.fixture(scope="session", autouse=True)
def _schema():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture(autouse=True)
def _clean_tables():
    yield
    with SessionLocal() as db:
        for model in (FraudEvent, AdReceipt, Purchase, Redemption, LedgerEntry, User):
            db.execute(delete(model))
        db.commit()


@pytest.fixture
def db():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def stub_fulfillment():
    return StubFulfillment()


@pytest.fixture
def settings():
    """Yields the (cached) Settings object; restores every field after."""
    from app.config import get_settings

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


@pytest.fixture
def client(stub_fulfillment):
    with TestClient(app) as c:
        # lifespan already set real clients; override with test doubles
        app.state.fulfillment = stub_fulfillment
        yield c


def make_user(
    db,
    *,
    balance: int = 0,
    email: str | None = None,
    email_verified: bool = False,
    age_days: int = 0,
    verified_ads: int = 0,
    risk_score: int = 0,
) -> User:
    user = User(
        device_id=f"device-{uuid.uuid4()}",
        email=email,
        email_verified=email_verified,
        balance=balance,
        risk_score=risk_score,
    )
    db.add(user)
    db.flush()
    if age_days:
        user.created_at = clock.now_utc() - timedelta(days=age_days)
    if balance:
        # keep the ledger consistent with the seeded balance
        db.add(
            LedgerEntry(
                user_id=user.id,
                amount=balance,
                balance_after=balance,
                kind="admin_adjust",
                idem_key=f"seed:{user.id}",
                ref="test-seed",
            )
        )
    for i in range(verified_ads):
        db.add(
            AdReceipt(
                user_id=user.id,
                network="admob",
                tx_id=f"tx-{user.id}-{i}",
                verified=True,
            )
        )
    db.commit()
    return user


def auth_headers(client, device_id: str | None = None) -> tuple[dict, uuid.UUID]:
    device_id = device_id or f"device-{uuid.uuid4()}"
    resp = client.post("/v1/auth/device", json={"device_id": device_id})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    return {"Authorization": f"Bearer {data['jwt']}"}, uuid.UUID(data["user_id"])
