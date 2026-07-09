"""AdMob SSV: no valid Google signature, no coins."""

import base64
import urllib.parse

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import func, select

from app.models import AdReceipt, FraudEvent, LedgerEntry
from app.webhooks.admob import StaticKeyProvider
from tests.conftest import auth_headers

KEY_ID = 3335741209


@pytest.fixture
def signer(client):
    private_key = ec.generate_private_key(ec.SECP256R1())
    client.app.state.admob_keys = StaticKeyProvider(
        {KEY_ID: private_key.public_key()}
    )

    def build_url(user_id, tx_id, *, tamper=False, key_id=KEY_ID):
        params = [
            ("ad_network", "5450213213286189855"),
            ("ad_unit", "1234567890"),
            ("custom_data", str(user_id)),
            ("reward_amount", "1"),
            ("reward_item", "coins"),
            ("timestamp", "1750000000000"),
            ("transaction_id", tx_id),
            ("user_id", str(user_id)),
        ]
        query = urllib.parse.urlencode(params)
        sig = private_key.sign(query.encode(), ec.ECDSA(hashes.SHA256()))
        if tamper:
            query = query.replace("reward_amount=1", "reward_amount=999999")
        sig_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return f"/v1/webhooks/admob-ssv?{query}&signature={sig_b64}&key_id={key_id}"

    return build_url


def test_valid_ssv_credits_server_owned_amount(client, db, signer, settings):
    settings.ad_reward_coins = 250
    headers, user_id = auth_headers(client)
    resp = client.get(signer(user_id, "tx-100"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["credited"] == 250  # not the callback's reward_amount

    me = client.get("/v1/me", headers=headers).json()
    assert me["balance"] == 250
    receipt = db.execute(select(AdReceipt)).scalar_one()
    assert receipt.network == "admob"
    assert receipt.verified is True


def test_replayed_transaction_credits_once(client, db, signer):
    headers, user_id = auth_headers(client)
    url = signer(user_id, "tx-dup")
    assert client.get(url).json()["credited"] > 0
    replay = client.get(url).json()
    assert replay["replay"] is True
    assert replay["credited"] == 0
    count = db.execute(
        select(func.count())
        .select_from(LedgerEntry)
        .where(LedgerEntry.kind == "ad_reward")
    ).scalar_one()
    assert count == 1


def test_tampered_query_rejected_and_logged(client, db, signer):
    headers, user_id = auth_headers(client)
    resp = client.get(signer(user_id, "tx-tampered", tamper=True))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "invalid_signature"
    assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 0
    event = db.execute(
        select(FraudEvent).where(FraudEvent.kind == "admob_ssv_denied:invalid_signature")
    ).scalar_one()
    assert event.detail["transaction_id"] == "tx-tampered"


def test_unknown_key_id_rejected(client, db, signer):
    _, user_id = auth_headers(client)
    resp = client.get(signer(user_id, "tx-badkey", key_id=999))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "unknown_key_id"


def test_no_signature_no_coins(client, db):
    _, user_id = auth_headers(client)
    resp = client.get(
        f"/v1/webhooks/admob-ssv?custom_data={user_id}&transaction_id=tx-nosig"
    )
    assert resp.status_code == 400
    assert db.execute(select(func.count()).select_from(LedgerEntry)).scalar_one() == 0


def test_bad_custom_data_rejected(client, db, signer):
    resp = client.get(signer("not-a-uuid", "tx-baduser"))
    assert resp.status_code == 400
    assert resp.json()["detail"] == "bad_custom_data"


def test_verified_ads_count_toward_redemption_gate(client, db, signer, settings):
    settings.redemption_min_verified_ad_receipts = 2
    headers, user_id = auth_headers(client)
    client.get(signer(user_id, "gate-tx-1"))
    client.get(signer(user_id, "gate-tx-2"))
    # gate check happens after ads: failure must now be about something else
    resp = client.post("/v1/redemptions", json={"sku": "amazon_5"}, headers=headers)
    assert resp.json()["detail"] != "not_enough_verified_ads"
