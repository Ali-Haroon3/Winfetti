"""Tapjoy offerwall server-to-server postback.

Tapjoy calls with the completed offer's tx id (`id`), our user uuid
(`snuid`), the coin amount (`currency`), and `verifier` =
MD5("{id}:{snuid}:{currency}:{secret}"). Same pattern as every credit path:
verify the shared-secret hash, dedupe on tx id, credit through the ledger —
with a defensive clamp on the amount since it rides in the request.
"""

import hashlib
import hmac
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.auth import client_ip
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.services import credit_verified_reward

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.get("/tapjoy")
def tapjoy_postback(
    request: Request,
    id: str = Query(min_length=1),
    snuid: str = Query(min_length=1),
    currency: int = Query(),
    verifier: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.tapjoy_secret:
        raise HTTPException(status_code=503, detail="tapjoy_disabled")

    expected = hashlib.md5(
        f"{id}:{snuid}:{currency}:{settings.tapjoy_secret}".encode()
    ).hexdigest()
    if not hmac.compare_digest(expected, verifier):
        log_fraud_event(
            db,
            "tapjoy_denied:invalid_verifier",
            ip=client_ip(request),
            detail={"id": id, "snuid": snuid, "currency": currency},
        )
        db.commit()
        raise HTTPException(status_code=403, detail="invalid_verifier")

    try:
        user_id = uuid.UUID(snuid)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_snuid")
    if currency <= 0:
        raise HTTPException(status_code=400, detail="bad_amount")

    amount = min(currency, settings.max_offer_coins_per_postback)
    if amount < currency:
        log_fraud_event(
            db,
            "offer_amount_clamped",
            user_id=user_id,
            ip=client_ip(request),
            detail={"network": "tapjoy", "requested": currency, "credited": amount},
        )

    try:
        result = credit_verified_reward(
            db,
            user_id,
            network="tapjoy",
            tx_id=id,
            amount=amount,
            kind="offer",
            payload=dict(request.query_params),
        )
        db.commit()
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=400, detail="unknown_user")

    return {"ok": True, "credited": result.awarded, "replay": result.replay}
