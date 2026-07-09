"""AdMob Server-Side Verification (SSV).

Google calls this endpoint after a rewarded ad completes, with an ECDSA
signature over the query string. No valid SSV callback, no coins — even if
the client swears the ad played. The client passes our user uuid in
custom_data. The reward amount is server-owned (settings.ad_reward_coins);
the reward params in the callback are logged but never trusted for pricing.
"""

import base64
import logging
import threading
import uuid

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app import clock
from app.auth import client_ip
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.services import credit_verified_reward

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


class AdMobKeyProvider:
    """Fetches and caches Google's SSV verifier keys. An unknown key_id
    forces a refresh (Google rotates keys)."""

    def __init__(self, url: str, ttl_seconds: int):
        self._url = url
        self._ttl = ttl_seconds
        self._keys: dict[int, ec.EllipticCurvePublicKey] = {}
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    def get(self, key_id: int) -> ec.EllipticCurvePublicKey | None:
        with self._lock:
            now = clock.now_utc().timestamp()
            stale = now - self._fetched_at > self._ttl
            if stale or key_id not in self._keys:
                self._refresh()
                self._fetched_at = now
            return self._keys.get(key_id)

    def _refresh(self) -> None:
        try:
            resp = httpx.get(self._url, timeout=10)
            resp.raise_for_status()
            self._keys = {
                int(k["keyId"]): serialization.load_pem_public_key(k["pem"].encode())
                for k in resp.json()["keys"]
            }
        except (httpx.HTTPError, KeyError, ValueError):
            logger.warning("failed to refresh AdMob verifier keys", exc_info=True)


class StaticKeyProvider:
    """Test double: a fixed key_id -> public key map."""

    def __init__(self, keys: dict[int, ec.EllipticCurvePublicKey]):
        self._keys = keys

    def get(self, key_id: int) -> ec.EllipticCurvePublicKey | None:
        return self._keys.get(key_id)


def get_admob_keys(request: Request):
    return request.app.state.admob_keys


def _b64decode_websafe(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@router.get("/admob-ssv")
def admob_ssv(
    request: Request,
    db: Session = Depends(get_db),
    keys=Depends(get_admob_keys),
):
    settings = get_settings()
    params = dict(request.query_params)
    signature = params.get("signature")
    key_id = params.get("key_id")
    tx_id = params.get("transaction_id")
    if not signature or not key_id or not tx_id:
        raise HTTPException(status_code=400, detail="missing_params")

    # Google signs the raw query string up to (not including) `&signature=`;
    # signature and key_id are always the last two parameters.
    raw_query = request.url.query
    message, sep, _ = raw_query.partition("&signature=")
    if not sep:
        raise HTTPException(status_code=400, detail="missing_params")

    def deny(code: str) -> HTTPException:
        log_fraud_event(
            db, f"admob_ssv_denied:{code}", ip=client_ip(request), detail=params
        )
        db.commit()
        return HTTPException(status_code=403, detail=code)

    try:
        public_key = keys.get(int(key_id))
    except ValueError:
        raise deny("bad_key_id")
    if public_key is None:
        raise deny("unknown_key_id")

    try:
        public_key.verify(
            _b64decode_websafe(signature),
            message.encode(),
            ec.ECDSA(hashes.SHA256()),
        )
    except (InvalidSignature, ValueError):
        raise deny("invalid_signature")

    # Signature is good from here on: bad user data is Google-side config,
    # not fraud — 400 so it surfaces in AdMob's callback logs.
    try:
        user_id = uuid.UUID(params.get("custom_data", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_custom_data")

    try:
        result = credit_verified_reward(
            db,
            user_id,
            network="admob",
            tx_id=tx_id,
            amount=settings.ad_reward_coins,
            kind="ad_reward",
            payload=params,
        )
        db.commit()
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=400, detail="unknown_user")

    return {"ok": True, "credited": result.awarded, "replay": result.replay}
