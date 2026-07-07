from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import clock, ledger, services
from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.schemas import DailyState, MeResponse

router = APIRouter(prefix="/v1", tags=["me"])


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    settings = get_settings()
    day_start = clock.day_start_utc()
    return MeResponse(
        user_id=user.id,
        balance=user.balance,
        gold=services.gold_active(db, user),
        boost_until=user.boost_until if services.boost_active(user) else None,
        email=user.email,
        email_verified=user.email_verified,
        daily=DailyState(
            game_win_credited_today=ledger.credited_since(
                db, user.id, day_start, kinds=["game_win"]
            ),
            total_credited_today=ledger.credited_since(db, user.id, day_start),
            daily_game_win_cap=settings.daily_game_win_cap,
            daily_total_credit_cap=settings.daily_total_credit_cap,
            checkin_streak=user.checkin_streak,
            checked_in_today=user.last_checkin_date == clock.today_utc(),
            happy_hour_active=services.happy_hour_active(),
        ),
    )
