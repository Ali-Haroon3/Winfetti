"""Affiliate network postbacks (Impact / Rakuten Advertising style).

Sales credit as *pending* cashback — coins only reach the ledger when the
maturation job runs after the return window. Reversals (returned orders)
cancel the pending credit. Authenticated with an HMAC-SHA256 over the
payload fields with a shared secret:

    signature = HMAC_SHA256(secret, "{network}:{tx_id}:{user_id}:{commission_cents}:{event}")
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
from app.services import create_cashback, reverse_cashback

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def _expected_signature(
    secret: str, network: str, tx_id: str, user_id: str, commission_cents: int, event: str
) -> str:
    message = f"{network}:{tx_id}:{user_id}:{commission_cents}:{event}"
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


@router.get("/affiliate")
def affiliate_postback(
    request: Request,
    network: str = Query(min_length=1, max_length=32),
    tx_id: str = Query(min_length=1),
    user_id: str = Query(min_length=1),
    commission_cents: int = Query(ge=0),
    event: str = Query(pattern="^(sale|reversal)$"),
    signature: str = Query(min_length=1),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.affiliate_secret:
        raise HTTPException(status_code=503, detail="affiliate_disabled")

    expected = _expected_signature(
        settings.affiliate_secret, network, tx_id, user_id, commission_cents, event
    )
    if not hmac.compare_digest(expected, signature):
        log_fraud_event(
            db,
            "affiliate_denied:invalid_signature",
            ip=client_ip(request),
            detail={"network": network, "tx_id": tx_id, "event": event},
        )
        db.commit()
        raise HTTPException(status_code=403, detail="invalid_signature")

    if event == "reversal":
        credit = reverse_cashback(db, network, tx_id)
        db.commit()
        if credit is None:
            return {"ok": True, "reversed": False, "reason": "unknown_tx"}
        return {"ok": True, "reversed": credit.status == "reversed"}

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_user_id")

    coins = int(
        commission_cents / 100 * settings.coins_per_usd * settings.cashback_share
    )
    if coins <= 0:
        return {"ok": True, "pending": False, "reason": "zero_coins"}
    clamp = settings.max_cashback_coins_per_postback
    if coins > clamp:
        log_fraud_event(
            db,
            "cashback_amount_clamped",
            user_id=uid,
            ip=client_ip(request),
            detail={"network": network, "requested": coins, "credited": clamp},
        )
        coins = clamp

    try:
        credit, created = create_cashback(
            db,
            uid,
            network=network,
            tx_id=tx_id,
            coins=coins,
            payload=dict(request.query_params),
        )
        db.commit()
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=400, detail="unknown_user")

    return {
        "ok": True,
        "pending": created,
        "coins": credit.coins,
        "matures_at": credit.matures_at.isoformat(),
    }
