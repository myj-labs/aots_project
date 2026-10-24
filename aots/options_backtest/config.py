"""Explicit configuration for the historical NIFTY options ORB backtest.

Defaults reuse the existing Yahoo ORB conventions where they exist (opening range
09:15-10:15, entry cutoff 15:00, time exit 15:15, one trade per session, 5-minute
candles). Stop-loss/target percentages, strike step, simulated capital and the cost
model are *proposed defaults or user inputs* and are echoed in every result.
"""

from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
INTERVAL_MINUTES = 5
SUPPORTED_CANDLE_INTERVALS = {"1minute": 1, "5minute": 5, "10minute": 10}
MAX_WINDOW_DAYS = 31


class CostConfig(BaseModel):
    """Every rate is supplied by the user; nothing is assumed about historical tax rates."""

    model_config = ConfigDict(extra="forbid")

    brokerage_per_order_inr: float = Field(default=0, ge=0, le=10_000, allow_inf_nan=False)
    stt_sell_pct: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)
    exchange_txn_pct: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)
    stamp_duty_buy_pct: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)
    gst_pct: float = Field(default=0, ge=0, le=50, allow_inf_nan=False)
    slippage_per_unit_inr: float = Field(default=0, ge=0, le=1_000, allow_inf_nan=False)

    @property
    def configured(self) -> bool:
        return any(
            value > 0
            for value in (
                self.brokerage_per_order_inr,
                self.stt_sell_pct,
                self.exchange_txn_pct,
                self.stamp_duty_buy_pct,
                self.gst_pct,
                self.slippage_per_unit_inr,
            )
        )


class OptionsBacktestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: Literal["orb"] = "orb"
    underlying: Literal["NIFTY"] = "NIFTY"
    start_date: date
    end_date: date
    candle_interval: Literal["1minute", "5minute", "10minute"] = "5minute"
    option_side_mode: Literal["both", "ce_only", "pe_only"] = "both"
    opening_range_minutes: int = Field(default=60, ge=15, le=60)
    entry_cutoff: time = time(15, 0)
    square_off_time: time = time(15, 15)
    strike_step: int = Field(default=50, ge=1, le=1000)
    strike_offset_steps: int = Field(default=0, ge=0, le=5)
    min_days_to_expiry: int = Field(default=0, ge=0, le=7)
    exit_level_mode: Literal["percentage", "premium_points"] = "percentage"
    stop_loss_pct: float = Field(default=25, gt=0, lt=100, allow_inf_nan=False)
    target_pct: float = Field(default=50, gt=0, le=1000, allow_inf_nan=False)
    stop_loss_points: float = Field(default=5, gt=0, le=100_000, allow_inf_nan=False)
    target_points: float = Field(default=5, gt=0, le=100_000, allow_inf_nan=False)
    max_trades_per_session: int = Field(default=1, ge=1, le=1000)
    lots: int = Field(default=1, ge=1, le=100)
    lot_size: int = Field(ge=1, le=10_000)
    initial_capital_inr: float = Field(default=100_000, gt=0, le=1e9, allow_inf_nan=False)
    costs: CostConfig = Field(default_factory=CostConfig)

    @model_validator(mode="after")
    def validate_rules(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be earlier than start_date.")
        if self.start_date < date(2020, 1, 1):
            raise ValueError("Historical data is documented from 2020-01-01.")
        if (self.end_date - self.start_date).days + 1 > MAX_WINDOW_DAYS:
            raise ValueError(f"A backtest is limited to {MAX_WINDOW_DAYS} calendar days per run.")
        interval_minutes = self.interval_minutes
        if self.opening_range_minutes % interval_minutes:
            raise ValueError("opening_range_minutes must be a multiple of the selected candle interval.")
        for name in ("entry_cutoff", "square_off_time"):
            value = getattr(self, name)
            if value.second or value.microsecond:
                raise ValueError(f"{name} must have minute precision.")
            if not (SESSION_OPEN <= value < SESSION_CLOSE):
                raise ValueError(f"{name} must be between 09:15 and 15:30.")
        square_off_offset = (
            self.square_off_time.hour * 60 + self.square_off_time.minute
            - SESSION_OPEN.hour * 60 - SESSION_OPEN.minute
        )
        if square_off_offset % interval_minutes:
            raise ValueError("square_off_time must fall on a selected candle boundary.")
        if self.entry_cutoff >= self.square_off_time:
            raise ValueError("entry_cutoff must be earlier than square_off_time.")
        if self.entry_cutoff < self.opening_range_end:
            raise ValueError("entry_cutoff must not be earlier than the end of the opening range.")
        return self

    @property
    def opening_range_end(self) -> time:
        start = datetime.combine(date(2000, 1, 1), SESSION_OPEN)
        return (start + timedelta(minutes=self.opening_range_minutes)).time()

    @property
    def opening_range_candles(self) -> int:
        return self.opening_range_minutes // self.interval_minutes

    @property
    def interval_minutes(self) -> int:
        return SUPPORTED_CANDLE_INTERVALS[self.candle_interval]

    def rules_description(self) -> list[str]:
        return [
            "Signal source: NIFTY index candles (Groww CASH segment, symbol NSE-NIFTY); option "
            "premiums are never used to form the breakout signal.",
            f"Opening range: highest high / lowest low of the NIFTY candles from 09:15 for "
            f"{self.opening_range_minutes} minutes; every range candle must be present.",
            "Signal: a NIFTY candle close crossing from inside/no breakout to above the range high "
            "(buy CE), below the range low (buy PE), or directly to the opposite side. Consecutive "
            "closes beyond the same range boundary are one breakout, not repeated signals; another "
            "same-side signal requires a close back inside the range first.",
            "Timing: the signal is known only when the signal candle closes. The simulated entry "
            f"fills at the OPEN of the selected option's next {self.interval_minutes}-minute candle, "
            "never at the signal close. Index signals and option fills use the same interval.",
            f"Contract: nearest Groww-listed expiry on/after session date + {self.min_days_to_expiry} day(s); "
            f"strike = ATM from the signal-candle close (step {self.strike_step}, ties round up)"
            + (
                f", {self.strike_offset_steps} step(s) out of the money."
                if self.strike_offset_steps
                else "."
            )
            + " The symbol must be returned by Groww for that expiry; no fallback contract is used.",
            f"Entry cutoff: entry candle must start at or before {self.entry_cutoff:%H:%M}.",
            (
                f"Stop-loss: {self.stop_loss_pct:g}% below entry premium; target: "
                f"{self.target_pct:g}% above it."
                if self.exit_level_mode == "percentage"
                else f"Stop-loss: ₹{self.stop_loss_points:g} below entry premium; target: "
                f"₹{self.target_points:g} above it."
            ),
            "Exit evaluation uses option candles from the entry candle onward. Stop gaps fill at the "
            "candle open when it is at/below the stop; a candle touching both stop and target exits "
            "at the stop (conservative stop-first). Touching a level is assumed to fill, with no queue modelling.",
            f"Time exit: open of the first option candle at/after {self.square_off_time:%H:%M}. If no such "
            "candle exists the trade is reported as unresolved and excluded from P&L; no price is invented.",
            f"At most {self.max_trades_per_session} trade(s) per session and one open position at a time; "
            "each distinct breakout event is considered only after the previous exit candle ends.",
            "P&L: gross = (exit premium - entry premium) x quantity; net = gross - modeled charges "
            "- modeled slippage. Costs not configured are reported as such.",
            "Maximum premium reached is the highest option candle high from entry through the exit candle, "
            "inclusive. Candle data does not establish the intrabar order of that high relative to an exit.",
        ]
