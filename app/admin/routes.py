import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import Redemption
from app.routes.redemptions import get_fulfillment
from app.schemas import AdminRedemptionResponse
from app.services import DomainError, approve_redemption, deny_redemption

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


def _load(db: Session, redemption_id: uuid.UUID) -> Redemption:
    redemption = db.get(Redemption, redemption_id)
    if redemption is None:
        raise HTTPException(status_code=404, detail="not_found")
    return redemption


@router.get("/redemptions", response_model=list[AdminRedemptionResponse])
def list_redemptions(
    status: str = Query(default="pending"),
    limit: int = Query(default=100, le=500),
    db: Session = Depends(get_db),
):
    rows = (
        db.execute(
            select(Redemption)
            .where(Redemption.status == status)
            .order_by(Redemption.created_at.asc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [AdminRedemptionResponse.model_validate(r) for r in rows]


@router.post("/redemptions/{redemption_id}/approve", response_model=AdminRedemptionResponse)
def approve(
    redemption_id: uuid.UUID,
    db: Session = Depends(get_db),
    fulfillment=Depends(get_fulfillment),
):
    redemption = _load(db, redemption_id)
    try:
        approve_redemption(db, redemption, fulfillment)
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return AdminRedemptionResponse.model_validate(redemption)


@router.post("/redemptions/{redemption_id}/deny", response_model=AdminRedemptionResponse)
def deny(redemption_id: uuid.UUID, db: Session = Depends(get_db)):
    redemption = _load(db, redemption_id)
    try:
        deny_redemption(db, redemption)
        db.commit()
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return AdminRedemptionResponse.model_validate(redemption)
