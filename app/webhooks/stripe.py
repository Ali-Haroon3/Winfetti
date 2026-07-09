"""Stripe webhook (web purchases): grants the same entitlements as
RevenueCat, verified via the Stripe-Signature header (HMAC-SHA256 over
"{timestamp}.{raw_body}" with the endpoint's signing secret, with a replay
tolerance window). Checkout sessions carry user_id and product_id in
metadata, set when the session is created.
"""

import hashlib
import hmac
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app import clock
from app.auth import client_ip
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.services import apply_entitlement_purchase

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


def verify_stripe_signature(
    payload: bytes, header: str, secret: str, tolerance_seconds: int
) -> bool:
    timestamp = None
    candidates = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(clock.now_utc().timestamp() - ts) > tolerance_seconds:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return any(hmac.compare_digest(expected, c) for c in candidates)


@router.post("/stripe")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    settings = get_settings()
    if not settings.stripe_webhook_secret:
        raise HTTPException(status_code=503, detail="stripe_disabled")

    payload = await request.body()
    header = request.headers.get("Stripe-Signature") or ""
    if not verify_stripe_signature(
        payload,
        header,
        settings.stripe_webhook_secret,
        settings.stripe_webhook_tolerance_seconds,
    ):
        log_fraud_event(
            db, "stripe_denied:bad_signature", ip=client_ip(request), detail=None
        )
        db.commit()
        raise HTTPException(status_code=400, detail="bad_signature")

    try:
        event = json.loads(payload)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_payload")

    if event.get("type") != "checkout.session.completed":
        return {"ok": True, "ignored": event.get("type")}

    session_obj = (event.get("data") or {}).get("object") or {}
    if session_obj.get("payment_status") != "paid":
        return {"ok": True, "ignored": "unpaid"}

    metadata = session_obj.get("metadata") or {}
    product_id = metadata.get("product_id", "")
    session_id = session_obj.get("id", "")
    try:
        user_id = uuid.UUID(str(metadata.get("user_id", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_metadata_user_id")
    if not product_id or not session_id:
        raise HTTPException(status_code=400, detail="missing_metadata")

    try:
        applied = apply_entitlement_purchase(
            db, user_id, product_id, store_tx_id=f"stripe:{session_id}"
        )
        db.commit()
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=400, detail="unknown_user")

    return {"ok": True, "applied": applied}
