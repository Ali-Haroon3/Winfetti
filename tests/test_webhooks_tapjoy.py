"""Tapjoy offerwall postbacks: shared-secret hash, unique tx, clamped credit."""

import hashlib

from sqlalchemy import func, select

from app.models import FraudEvent, LedgerEntry
from tests.conftest import auth_headers

SECRET = "tapjoy-test-secret"


def _url(tx_id, snuid, currency, secret=SECRET, verifier=None):
    verifier = verifier or hashlib.md5(
        f"{tx_id}:{snuid}:{currency}:{secret}".encode()
    ).hexdigest()
    return (
        f"/v1/webhooks/tapjoy?id={tx_id}&snuid={snuid}"
        f"&currency={currency}&verifier={verifier}"
    )


def test_disabled_without_secret(client, db):
    resp = client.get(_url("t1", "x", 100))
    assert resp.status_code == 503


def test_valid_postback_credits(client, db, settings):
    settings.tapjoy_secret = SECRET
    headers, user_id = auth_headers(client)
    resp = client.get(_url("t-100", user_id, 500))
    assert resp.status_code == 200, resp.text
    assert resp.json()["credited"] == 500
    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 500
    entry = db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "offer")
    ).scalar_one()
    assert entry.idem_key == "offer:tapjoy:t-100"


def test_wrong_verifier_rejected_and_logged(client, db, settings):
    settings.tapjoy_secret = SECRET
    _, user_id = auth_headers(client)
    resp = client.get(_url("t-bad", user_id, 500, verifier="0" * 32))
    assert resp.status_code == 403
    assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 0
    assert (
        db.execute(
            select(func.count())
            .select_from(FraudEvent)
            .where(FraudEvent.kind == "tapjoy_denied:invalid_verifier")
        ).scalar_one()
        == 1
    )


def test_replay_credits_once(client, db, settings):
    settings.tapjoy_secret = SECRET
    headers, user_id = auth_headers(client)
    url = _url("t-dup", user_id, 500)
    assert client.get(url).json()["credited"] == 500
    assert client.get(url).json() == {"ok": True, "credited": 0, "replay": True}
    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 500


def test_oversized_amount_clamped_and_logged(client, db, settings):
    settings.tapjoy_secret = SECRET
    settings.max_offer_coins_per_postback = 1_000
    headers, user_id = auth_headers(client)
    resp = client.get(_url("t-big", user_id, 999_999))
    assert resp.json()["credited"] == 1_000
    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 1_000
    event = db.execute(
        select(FraudEvent).where(FraudEvent.kind == "offer_amount_clamped")
    ).scalar_one()
    assert event.detail["requested"] == 999_999


def test_bad_snuid_and_amount_rejected(client, db, settings):
    settings.tapjoy_secret = SECRET
    assert client.get(_url("t-x", "not-a-uuid", 100)).status_code == 400
    _, user_id = auth_headers(client)
    assert client.get(_url("t-y", user_id, 0)).status_code == 400
    assert client.get(_url("t-z", user_id, -5)).status_code == 400
