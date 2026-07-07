from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import client_ip, get_current_user
from app.db import get_db
from app.fraud import log_fraud_event
from app.fulfillment import FulfillmentClient
from app.models import Redemption, User
from app.payouts import REDEMPTION_CATALOG
from app.rate_limit import rate_limit_user_writes
from app.schemas import CatalogItem, RedemptionCreateRequest, RedemptionResponse
from app.services import (
    DomainError,
    approve_redemption,
    auto_approvable,
    create_redemption,
)

router = APIRouter(prefix="/v1/redemptions", tags=["redemptions"])


def get_fulfillment(request: Request) -> FulfillmentClient:
    return request.app.state.fulfillment


@router.post("", response_model=RedemptionResponse, status_code=201)
def create(
    body: RedemptionCreateRequest,
    request: Request,
    user: User = Depends(rate_limit_user_writes),
    db: Session = Depends(get_db),
    fulfillment: FulfillmentClient = Depends(get_fulfillment),
):
    try:
        redemption = create_redemption(db, user.id, body.sku)
        db.commit()
    except DomainError as exc:
        db.rollback()
        log_fraud_event(
            db,
            f"redemption_denied:{exc.code}",
            user_id=user.id,
            ip=client_ip(request),
            detail={"sku": body.sku},
        )
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)

    if auto_approvable(db, redemption, user):
        try:
            approve_redemption(db, redemption, fulfillment)
        except DomainError:
            # Fulfillment hiccup: leave it for the manual queue.
            db.rollback()
    return RedemptionResponse.model_validate(redemption)


@router.get("", response_model=list[RedemptionResponse])
def list_mine(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(Redemption)
            .where(Redemption.user_id == user.id)
            .order_by(Redemption.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [RedemptionResponse.model_validate(r) for r in rows]


@router.get("/catalog", response_model=list[CatalogItem])
def catalog():
    return [
        CatalogItem(sku=sku, usd=item["usd"], coins=item["coins"], label=item["label"])
        for sku, item in REDEMPTION_CATALOG.items()
    ]
