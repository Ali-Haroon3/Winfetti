import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field


class DeviceAuthRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=128)


class DeviceAuthResponse(BaseModel):
    jwt: str
    user_id: uuid.UUID


class DailyState(BaseModel):
    game_win_credited_today: int
    total_credited_today: int
    daily_game_win_cap: int
    daily_total_credit_cap: int
    checkin_streak: int
    checked_in_today: bool
    happy_hour_active: bool


class MeResponse(BaseModel):
    user_id: uuid.UUID
    balance: int
    gold: bool
    boost_until: datetime | None
    email: str | None
    email_verified: bool
    daily: DailyState


class GameClaimRequest(BaseModel):
    game: str = Field(min_length=1, max_length=64)
    event: str = Field(min_length=1, max_length=64)
    idem_key: str = Field(min_length=8, max_length=128)


class GameClaimResponse(BaseModel):
    awarded: int
    balance: int
    capped: bool
    replay: bool


class CheckinResponse(BaseModel):
    streak: int
    awarded: int
    balance: int
    already_checked_in: bool


class EmailRequest(BaseModel):
    email: EmailStr


class EmailVerifyRequest(BaseModel):
    token: str = Field(min_length=16, max_length=128)


class EmailStatusResponse(BaseModel):
    email: str | None
    email_verified: bool
    pending_email: str | None


class RedemptionCreateRequest(BaseModel):
    sku: str = Field(min_length=1, max_length=64)


class RedemptionResponse(BaseModel):
    id: uuid.UUID
    sku: str
    usd: Decimal
    coins: int
    status: str
    created_at: datetime
    reviewed_at: datetime | None

    model_config = {"from_attributes": True}


class AdminRedemptionResponse(RedemptionResponse):
    user_id: uuid.UUID
    tremendous_order_id: str | None
    sent_at: datetime | None = None


class CatalogItem(BaseModel):
    sku: str
    usd: Decimal
    coins: int
    label: str


# No raw ledger id here: it's a global sequence, and exposing it would let
# any user infer platform-wide write volume. Pagination rides the opaque
# next_cursor instead (app/cursor.py).
class LedgerEntryItem(BaseModel):
    amount: int
    balance_after: int
    kind: str
    ref: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class LedgerPage(BaseModel):
    entries: list[LedgerEntryItem]
    # Opaque token — pass back as ?cursor= to fetch the next (older) page;
    # null when done.
    next_cursor: str | None


class AdminLedgerEntryItem(LedgerEntryItem):
    id: int
    idem_key: str


class AdminLedgerPage(BaseModel):
    entries: list[AdminLedgerEntryItem]
    next_cursor: int | None


class CashbackEntryItem(BaseModel):
    network: str
    coins: int
    status: str
    matures_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class CashbackListResponse(BaseModel):
    pending_coins: int
    entries: list[CashbackEntryItem]


class AdminAdjustRequest(BaseModel):
    amount: int
    reason: str = Field(min_length=3, max_length=200)
    idem_key: str = Field(min_length=8, max_length=128)


class AdminAdjustResponse(BaseModel):
    user_id: uuid.UUID
    amount: int
    balance: int
    replay: bool
