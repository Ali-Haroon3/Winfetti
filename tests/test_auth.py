import uuid

from sqlalchemy import func, select

from app.models import User
from tests.conftest import auth_headers


def test_device_auth_creates_user_and_jwt_works(client, db):
    headers, user_id = auth_headers(client)
    me = client.get("/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user_id"] == str(user_id)
    assert me.json()["balance"] == 0


def test_same_device_id_maps_to_same_user(client, db):
    device = f"device-{uuid.uuid4()}"
    _, uid1 = auth_headers(client, device_id=device)
    _, uid2 = auth_headers(client, device_id=device)
    assert uid1 == uid2
    count = db.execute(select(func.count()).select_from(User)).scalar_one()
    assert count == 1


def test_requests_without_token_rejected(client, db):
    assert client.get("/v1/me").status_code == 401
    assert (
        client.get("/v1/me", headers={"Authorization": "Bearer garbage"}).status_code
        == 401
    )


def test_disabled_account_rejected(client, db):
    headers, user_id = auth_headers(client)
    user = db.get(User, user_id)
    user.status = "banned"
    db.commit()
    assert client.get("/v1/me", headers=headers).status_code == 403


def test_short_device_id_rejected(client, db):
    resp = client.post("/v1/auth/device", json={"device_id": "short"})
    assert resp.status_code == 422
