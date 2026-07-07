from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.auth import client_ip
from app.db import get_db
from app.fraud import log_fraud_event
from app.models import User
from app.rate_limit import rate_limit_user_writes
from app.schemas import GameClaimRequest, GameClaimResponse
from app.services import DomainError, claim_game_win

router = APIRouter(prefix="/v1/game", tags=["game"])


@router.post("/claim", response_model=GameClaimResponse)
def claim(
    body: GameClaimRequest,
    request: Request,
    user: User = Depends(rate_limit_user_writes),
    db: Session = Depends(get_db),
):
    try:
        result = claim_game_win(db, user.id, body.game, body.event, body.idem_key)
        db.commit()
    except DomainError as exc:
        db.rollback()
        log_fraud_event(
            db,
            f"game_claim_denied:{exc.code}",
            user_id=user.id,
            ip=client_ip(request),
            detail={"game": body.game, "event": body.event},
        )
        db.commit()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return GameClaimResponse(
        awarded=result.awarded,
        balance=result.balance,
        capped=result.capped,
        replay=result.replay,
    )
