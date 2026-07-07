from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.rate_limit import rate_limit_user_writes
from app.schemas import CheckinResponse
from app.services import do_checkin

router = APIRouter(prefix="/v1", tags=["checkin"])


@router.post("/checkin", response_model=CheckinResponse)
def checkin(
    user: User = Depends(rate_limit_user_writes),
    db: Session = Depends(get_db),
):
    result = do_checkin(db, user.id)
    db.commit()
    return CheckinResponse(
        streak=result.streak,
        awarded=result.awarded,
        balance=result.balance,
        already_checked_in=result.already_checked_in,
    )
