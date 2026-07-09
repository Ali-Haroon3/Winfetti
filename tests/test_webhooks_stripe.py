"""Stripe webhook: signed payloads grant entitlements for web purchases."""

import hashlib
import hmac
import json

from sqlalchemy import func, select

from app import clock
from app.models import FraudEvent, Purchase
from tests.conftest import auth_headers

SECRET = "whsec_test_secret"


def _checkout_event(user_id, product="gold", session_id="cs_test_1", paid=True):
    return {
        "id": f"evt_{session_id}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "payment_status": "paid" if paid else "unpaid",
                "metadata": {"user_id": str(user_id), "product_id": product},
            }
        },
    }


def _post(client, event, secret=SECRET, timestamp=None):
    payload = json.dumps(event).encode()
    ts = timestamp if timestamp is not None else int(clock.now_utc().timestamp())
    mac = hmac.new(
        secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/v1/webhooks/stripe",
        content=payload,
        headers={
            "Stripe-Signature": f"t={ts},v1={mac}",
            "Content-Type": "application/json",
        },
    )


def test_disabled_without_secret(client, db):
    resp = client.post("/v1/webhooks/stripe", content=b"{}")
    assert resp.status_code == 503


def test_valid_checkout_grants_gold(client, db, settings):
    settings.stripe_webhook_secret = SECRET
    headers, user_id = auth_headers(client)
    resp = _post(client, _checkout_event(user_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert client.get("/v1/me", headers=headers).json()["gold"] is True
    purchase = db.execute(select(Purchase)).scalar_one()
    assert purchase.store_tx_id == "stripe:cs_test_1"


def test_bad_signature_rejected_and_logged(client, db, settings):
    settings.stripe_webhook_secret = SECRET
    _, user_id = auth_headers(client)
    resp = _post(client, _checkout_event(user_id), secret="whsec_wrong")
    assert resp.status_code == 400
    assert db.execute(select(func.count()).select_from(Purchase)).scalar_one() == 0
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "stripe_denied:bad_signature")
        ).scalar_one()
        == 1
    )


def test_stale_timestamp_rejected(client, db, settings):
    settings.stripe_webhook_secret = SECRET
    _, user_id = auth_headers(client)
    old = int(clock.now_utc().timestamp()) - 3600
    resp = _post(client, _checkout_event(user_id), timestamp=old)
    assert resp.status_code == 400


def test_replayed_session_applies_once(client, db, settings):
    settings.stripe_webhook_secret = SECRET
    _, user_id = auth_headers(client)
    assert _post(client, _checkout_event(user_id)).json()["applied"] is True
    assert _post(client, _checkout_event(user_id)).json()["applied"] is False
    assert db.execute(select(func.count()).select_from(Purchase)).scalar_one() == 1


def test_unpaid_and_foreign_events_ignored(client, db, settings):
    settings.stripe_webhook_secret = SECRET
    _, user_id = auth_headers(client)
    assert (
        _post(client, _checkout_event(user_id, paid=False)).json()["ignored"]
        == "unpaid"
    )
    other = {"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}}
    assert _post(client, other).json()["ignored"] == "invoice.paid"
    assert db.execute(select(func.count()).select_from(Purchase)).scalar_one() == 0
