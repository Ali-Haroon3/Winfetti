"""RevenueCat webhook: grants the gold entitlement and boost consumables.

The client never reports its own purchases — RevenueCat does, authenticated
by the exact Authorization header value configured in their dashboard.
app_user_id is our user uuid (the client sets it after device auth).
"""

import hmac
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from app.auth import client_ip
from app.config import get_settings
from app.db import get_db
from app.fraud import log_fraud_event
from app.payouts import GOLD_PRODUCT_ID
from app.services import apply_entitlement_purchase, expire_gold

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "NON_RENEWING_PURCHASE",
}


@router.post("/revenuecat")
def revenuecat_webhook(
    request: Request,
    body: dict = Body(),
    db: Session = Depends(get_db),
):
    settings = get_settings()
    if not settings.revenuecat_webhook_auth:
        raise HTTPException(status_code=503, detail="revenuecat_disabled")

    supplied = request.headers.get("Authorization") or ""
    if not hmac.compare_digest(supplied, settings.revenuecat_webhook_auth):
        log_fraud_event(
            db, "revenuecat_denied:bad_auth", ip=client_ip(request), detail=None
        )
        db.commit()
        raise HTTPException(status_code=401, detail="bad_auth")

    event = body.get("event") or {}
    event_type = event.get("type", "")
    product_id = event.get("product_id", "")
    tx_id = event.get("transaction_id") or event.get("id")

    try:
        user_id = uuid.UUID(str(event.get("app_user_id", "")))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad_app_user_id")

    try:
        if event_type in GRANT_EVENTS and product_id and tx_id:
            applied = apply_entitlement_purchase(
                db, user_id, product_id, store_tx_id=f"revenuecat:{tx_id}"
            )
            db.commit()
            return {"ok": True, "applied": applied}
        if event_type == "EXPIRATION" and product_id == GOLD_PRODUCT_ID:
            expired = expire_gold(db, user_id)
            db.commit()
            return {"ok": True, "expired": expired}
    except NoResultFound:
        db.rollback()
        raise HTTPException(status_code=400, detail="unknown_user")

    # CANCELLATION (auto-renew off, still entitled), BILLING_ISSUE, TEST, …
    return {"ok": True, "ignored": event_type}
