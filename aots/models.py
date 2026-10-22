from datetime import date, datetime, timedelta, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperTradeRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(max_length=128)
    side: Literal["BUY"] = "BUY"
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    take_profit_profit_inr: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )
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
    observation_sequence: int = Field(
        gt=0,
        strict=True,
        description="Caller-provided increasing per-trade source observation sequence.",
    )


class KillSwitchRequest(BaseModel):
    enabled: bool


class StrategyEvaluationRequest(BaseModel):
    observation_id: str = Field(min_length=1, max_length=128)
    underlying: str = Field(min_length=1, max_length=32)
    spot: float = Field(gt=0, allow_inf_nan=False)
    observed_at: datetime
    symbol: str = Field(min_length=1, max_length=80)
    entry_price: float = Field(gt=0, allow_inf_nan=False)
    stop_price: float = Field(gt=0, allow_inf_nan=False)
    quantity: int = Field(gt=0)
    lots: int = Field(ge=1)
    take_profit_profit_inr: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )

    @field_validator("observation_id", "underlying", "symbol")
    @classmethod
    def require_non_whitespace(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("Value must not be empty or have surrounding whitespace.")
        return value

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone.")
        return value.astimezone(timezone.utc)

    @field_validator("stop_price")
    @classmethod
    def stop_below_entry(cls, value: float, info):
        entry = info.data.get("entry_price")
        if entry is not None and value >= entry:
            raise ValueError("For long-option candidates, stop_price must be below entry_price.")
        return value


class HistoricalReplayRequest(BaseModel):
    start: date
    end: date
    offline: bool = False
    allow_stale_cache: bool = False
    ema50_filter: bool = False
    opening_range_atr_max_ratio: float | None = Field(
        default=None, gt=0, allow_inf_nan=False
    )

    @model_validator(mode="after")
    def validate_replay_window(self):
        if self.end <= self.start:
            raise ValueError("end must be later than start; end is exclusive.")
        if (self.end - self.start).days > 60:
            raise ValueError("Historical replay is limited to a 60-calendar-day request window.")
        if self.allow_stale_cache and not self.offline:
            raise ValueError("allow_stale_cache requires offline mode.")
        return self


class HistoricalCandlesCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    groww_symbol: str = Field(min_length=1, max_length=80)
    start_time: datetime
    end_time: datetime
    candle_interval: Literal["5minute", "15minute"]

    @field_validator("groww_symbol")
    @classmethod
    def require_trimmed_symbol(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("groww_symbol must not be empty or have surrounding whitespace.")
        return value

    @field_validator("start_time", "end_time")
    @classmethod
    def require_local_wall_clock(cls, value: datetime) -> datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("Historical timestamps must be local exchange times without a timezone offset.")
        return value

    @model_validator(mode="after")
    def validate_candle_window(self):
        if self.start_time < datetime(2020, 1, 1):
            raise ValueError("Historical data requests must start on or after 2020-01-01.")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time.")
        if self.end_time - self.start_time > timedelta(days=1):
            raise ValueError("Historical candle checks are limited to a one-day request window.")
        return self


class PaperTrade(BaseModel):
    trade_id: str
    symbol: str
    side: str
    entry_price: float
    stop_price: float
    take_profit_profit_inr: float | None = None
    mark_price: float
    quantity: int
    lots: int
    status: Literal["OPEN", "CLOSED"]
    realised_pnl: float = 0
    unrealised_pnl: float = 0
    mark_sequence: int = 0
    close_reason: str | None = None
    created_at: datetime
    closed_at: datetime | None = None
