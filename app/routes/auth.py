from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import issue_token
from app.db import get_db
from app.models import User
from app.rate_limit import rate_limit_auth_ip
from app.schemas import DeviceAuthRequest, DeviceAuthResponse

router = APIRouter(prefix="/v1/auth", tags=["auth"])


@router.post(
    "/device",
    response_model=DeviceAuthResponse,
    dependencies=[Depends(rate_limit_auth_ip)],
)
def device_auth(body: DeviceAuthRequest, db: Session = Depends(get_db)):
    """Anonymous device auth: get-or-create the user for this device_id."""
    user = db.execute(
        select(User).where(User.device_id == body.device_id)
    ).scalar_one_or_none()
    if user is None:
        user = User(device_id=body.device_id)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            # Concurrent first-auth from the same device; take the winner.
            db.rollback()
            user = db.execute(
                select(User).where(User.device_id == body.device_id)
            ).scalar_one()
    return DeviceAuthResponse(jwt=issue_token(user.id), user_id=user.id)
