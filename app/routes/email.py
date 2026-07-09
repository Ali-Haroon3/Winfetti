from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.emailer import EmailSender
from app.models import User
from app.rate_limit import rate_limit_user_writes
from app.schemas import EmailRequest, EmailStatusResponse, EmailVerifyRequest
from app.services import (
    DomainError,
    confirm_email_verification,
    request_email_verification,
)

router = APIRouter(prefix="/v1/me/email", tags=["email"])


def get_email_sender(request: Request) -> EmailSender:
    return request.app.state.email_sender


@router.post("", response_model=EmailStatusResponse, status_code=202)
def request_verification(
    body: EmailRequest,
    user: User = Depends(rate_limit_user_writes),
    db: Session = Depends(get_db),
    sender: EmailSender = Depends(get_email_sender),
):
    try:
        token = request_email_verification(db, user.id, body.email)
        db.commit()
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    # send after commit: a failed send leaves a valid token, and the user
    # can simply request again
    sender.send_verification(to=body.email, token=token)
    return EmailStatusResponse(
        email=user.email, email_verified=user.email_verified, pending_email=body.email
    )


@router.post("/verify", response_model=EmailStatusResponse)
def verify(
    body: EmailVerifyRequest,
    user: User = Depends(rate_limit_user_writes),
    db: Session = Depends(get_db),
):
    try:
        user = confirm_email_verification(db, user.id, body.token)
        db.commit()
    except DomainError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.code)
    return EmailStatusResponse(
        email=user.email, email_verified=user.email_verified, pending_email=None
    )
