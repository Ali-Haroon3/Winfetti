"""Email verification: gift cards go to this address, so it must be proven."""

from datetime import timedelta

import pytest

from app import clock
from app.emailer import StubEmailSender
from tests.conftest import auth_headers


@pytest.fixture
def mailbox(client):
    stub = StubEmailSender()
    client.app.state.email_sender = stub
    return stub


def _request(client, headers, email="player@example.com"):
    return client.post("/v1/me/email", json={"email": email}, headers=headers)


def _verify(client, headers, token):
    return client.post("/v1/me/email/verify", json={"token": token}, headers=headers)


def test_request_then_verify_marks_email_verified(client, db, mailbox):
    headers, _ = auth_headers(client)
    resp = _request(client, headers)
    assert resp.status_code == 202, resp.text
    assert resp.json()["email_verified"] is False
    assert len(mailbox.sent) == 1
    assert mailbox.sent[0]["to"] == "player@example.com"

    resp = _verify(client, headers, mailbox.sent[0]["token"])
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "email": "player@example.com",
        "email_verified": True,
        "pending_email": None,
    }
    me = client.get("/v1/me", headers=headers).json()
    assert me["email_verified"] is True


def test_verified_email_passes_redemption_gate(client, db, mailbox, settings):
    settings.redemption_min_verified_ad_receipts = 0
    headers, _ = auth_headers(client)
    _request(client, headers)
    _verify(client, headers, mailbox.sent[0]["token"])
    resp = client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
    # email gate cleared; the next gate (account age) fires instead
    assert resp.json()["detail"] == "account_too_new"


def test_wrong_token_rejected(client, db, mailbox):
    headers, _ = auth_headers(client)
    _request(client, headers)
    resp = _verify(client, headers, "not-the-right-token-at-all")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "invalid_or_expired_token"


def test_token_is_single_use(client, db, mailbox):
    headers, _ = auth_headers(client)
    _request(client, headers)
    token = mailbox.sent[0]["token"]
    assert _verify(client, headers, token).status_code == 200
    assert _verify(client, headers, token).status_code == 400


def test_expired_token_rejected(client, db, mailbox, monkeypatch):
    headers, _ = auth_headers(client)
    _request(client, headers)
    token = mailbox.sent[0]["token"]

    real_now = clock.now_utc()
    monkeypatch.setattr(clock, "now_utc", lambda: real_now + timedelta(hours=25))
    assert _verify(client, headers, token).status_code == 400


def test_new_request_supersedes_old_token(client, db, mailbox):
    headers, _ = auth_headers(client)
    _request(client, headers, "first@example.com")
    _request(client, headers, "second@example.com")
    old, new = mailbox.sent[0]["token"], mailbox.sent[1]["token"]
    assert _verify(client, headers, old).status_code == 400
    resp = _verify(client, headers, new)
    assert resp.json()["email"] == "second@example.com"


def test_another_users_token_does_not_work(client, db, mailbox):
    headers_a, _ = auth_headers(client)
    headers_b, _ = auth_headers(client)
    _request(client, headers_a)
    token = mailbox.sent[0]["token"]
    assert _verify(client, headers_b, token).status_code == 400
    assert _verify(client, headers_a, token).status_code == 200


def test_email_verified_by_another_account_is_rejected(client, db, mailbox):
    headers_a, _ = auth_headers(client)
    _request(client, headers_a, "shared@example.com")
    _verify(client, headers_a, mailbox.sent[0]["token"])

    headers_b, _ = auth_headers(client)
    resp = _request(client, headers_b, "Shared@Example.com")  # case-insensitive
    assert resp.status_code == 409
    assert resp.json()["detail"] == "email_in_use"


def test_bad_email_rejected(client, db, mailbox):
    headers, _ = auth_headers(client)
    assert _request(client, headers, "not-an-email").status_code == 422
    assert mailbox.sent == []
