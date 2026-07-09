"""RevenueCat webhook: gold entitlement and boost consumables."""

from sqlalchemy import func, select

from app.models import FraudEvent, Purchase, User
from tests.conftest import auth_headers

AUTH = "Bearer rc-test-secret"


def _event(user_id, type="INITIAL_PURCHASE", product="gold", tx="rc-tx-1"):
    return {
        "event": {
            "type": type,
            "app_user_id": str(user_id),
            "product_id": product,
            "transaction_id": tx,
            "id": f"evt-{tx}",
        }
    }


def _post(client, body, auth=AUTH):
    return client.post(
        "/v1/webhooks/revenuecat", json=body, headers={"Authorization": auth}
    )


def test_disabled_without_configured_auth(client, db):
    resp = client.post("/v1/webhooks/revenuecat", json={})
    assert resp.status_code == 503


def test_bad_auth_rejected_and_logged(client, db, settings):
    settings.revenuecat_webhook_auth = AUTH
    _, user_id = auth_headers(client)
    resp = _post(client, _event(user_id), auth="Bearer wrong")
    assert resp.status_code == 401
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "revenuecat_denied:bad_auth")
        ).scalar_one()
        == 1
    )


def test_gold_purchase_activates_multiplier(client, db, settings, no_happy_hour):
    settings.revenuecat_webhook_auth = AUTH
    headers, user_id = auth_headers(client)
    assert client.get("/v1/me", headers=headers).json()["gold"] is False

    resp = _post(client, _event(user_id))
    assert resp.status_code == 200, resp.text
    assert resp.json()["applied"] is True
    assert client.get("/v1/me", headers=headers).json()["gold"] is True

    # gold multiplier (1.5x) applies to the next claim
    claim = client.post(
        "/v1/game/claim",
        json={"game": "wheel", "event": "seg_500", "idem_key": "rc-gold-claim-1"},
        headers=headers,
    ).json()
    assert claim["awarded"] == 750


def test_expiration_deactivates_gold(client, db, settings):
    settings.revenuecat_webhook_auth = AUTH
    headers, user_id = auth_headers(client)
    _post(client, _event(user_id))
    resp = _post(client, _event(user_id, type="EXPIRATION"))
    assert resp.json()["expired"] == 1
    assert client.get("/v1/me", headers=headers).json()["gold"] is False
    purchase = db.execute(select(Purchase)).scalar_one()
    assert purchase.status == "expired"


def test_boost_purchase_sets_boost_until_and_stacks(client, db, settings):
    settings.revenuecat_webhook_auth = AUTH
    headers, user_id = auth_headers(client)
    _post(
        client,
        _event(user_id, type="NON_RENEWING_PURCHASE", product="boost_1h", tx="b1"),
    )
    me = client.get("/v1/me", headers=headers).json()
    assert me["boost_until"] is not None

    # a second boost extends from the current boost_until, not from now
    _post(
        client,
        _event(user_id, type="NON_RENEWING_PURCHASE", product="boost_1h", tx="b2"),
    )
    user = db.get(User, user_id)
    db.refresh(user)
    first_until = me["boost_until"]
    assert user.boost_until.isoformat() > first_until


def test_replayed_transaction_applies_once(client, db, settings):
    settings.revenuecat_webhook_auth = AUTH
    _, user_id = auth_headers(client)
    body = _event(user_id, type="NON_RENEWING_PURCHASE", product="boost_1h", tx="dup")
    assert _post(client, body).json()["applied"] is True
    assert _post(client, body).json()["applied"] is False
    assert db.execute(select(func.count()).select_from(Purchase)).scalar_one() == 1


def test_irrelevant_events_ignored(client, db, settings):
    settings.revenuecat_webhook_auth = AUTH
    _, user_id = auth_headers(client)
    resp = _post(client, _event(user_id, type="CANCELLATION"))
    assert resp.json() == {"ok": True, "ignored": "CANCELLATION"}
    assert db.execute(select(func.count()).select_from(Purchase)).scalar_one() == 0
