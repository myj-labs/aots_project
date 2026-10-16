from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperTradeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(max_length=128)
    side: Literal["BUY"] = "BUY"
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    quantity: int = Field(gt=0)
    lots: int = Field(default=1, ge=1)
    market_data_age_seconds: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("idempotency_key must not be empty or whitespace-only.")
        if value != value.strip():
            raise ValueError("idempotency_key must not have surrounding whitespace.")
        return value

    @field_validator("stop_price")
    @classmethod
    def stop_below_entry(cls, v, info):
        entry = info.data.get("entry_price")
        if entry is not None and v >= entry:
            raise ValueError("For long-option paper trades, stop_price must be below entry_price.")
        return v


class MarkPriceRequest(BaseModel):
    trade_id: str
    mark_price: float = Field(gt=0, allow_inf_nan=False)


class KillSwitchRequest(BaseModel):
    enabled: bool


class PaperTrade(BaseModel):
    trade_id: str
    symbol: str
    side: str
    entry_price: float
    stop_price: float
    mark_price: float
    quantity: int
    lots: int
    status: Literal["OPEN", "CLOSED"]
    realised_pnl: float = 0
    unrealised_pnl: float = 0
    created_at: datetime
    closed_at: datetime | None = None
