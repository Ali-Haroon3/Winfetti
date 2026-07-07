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


def _request_with(headers: dict, client_host: str = "10.0.0.1"):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 1234),
    }
    return Request(scope)


def test_client_ip_takes_rightmost_forwarded_entry():
    """The edge proxy appends the real client; anything the client forged
    sits to the left. Spoofing must not shard the per-IP rate limit."""
    from app.auth import client_ip

    req = _request_with({"x-forwarded-for": "6.6.6.6, 1.2.3.4"})
    assert client_ip(req) == "1.2.3.4"
    assert client_ip(_request_with({})) == "10.0.0.1"


def test_client_ip_ignores_header_when_proxy_untrusted():
    from app.auth import client_ip
    from app.config import get_settings

    get_settings().trust_proxy_headers = False
    try:
        req = _request_with({"x-forwarded-for": "6.6.6.6"})
        assert client_ip(req) == "10.0.0.1"
    finally:
        get_settings().trust_proxy_headers = True


def test_auth_denials_logged_to_fraud_events(client, db):
    from sqlalchemy import select as sa_select

    from app.models import FraudEvent

    client.get("/v1/me", headers={"Authorization": "Bearer garbage"})
    kinds = [
        e.kind
        for e in db.execute(sa_select(FraudEvent)).scalars().all()
    ]
    assert "auth_denied:invalid_token" in kinds
