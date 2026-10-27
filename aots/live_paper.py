"""Groww market-data-driven NIFTY ORB paper trading.

This service only creates and updates local simulated positions through
``PaperBroker``. Market prices never invoke Groww order APIs.
"""

from __future__ import annotations

import math
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Protocol

from pydantic import BaseModel, Field, field_validator, model_validator

from .brokers.groww_feed import (
    FeedInstrument,
    GrowwInstrumentCatalog,
    MarketFeedError,
    MarketTick,
)
from .brokers.response_log import LIVE_FEED_DIAGNOSTICS
from .brokers.base import HistoricalCandle
from .models import PaperTradeRequest
from .database import IdempotencyConflict
from .options_backtest.engine import (
    Breakout,
    OpeningRange,
    compute_opening_range,
    find_breakout,
    target_strike,
)
from .provider_session import ProviderRequestError
from .risk import KOLKATA, RiskRejected


SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)
logger = logging.getLogger("aots.live_paper")


class LivePaperStartRequest(BaseModel):
    expiry_date: date
    opening_range_minutes: int = Field(default=60, ge=15, le=60)
    candle_interval_minutes: int = Field(default=5, ge=1, le=10)
    option_side_mode: str = Field(default="both")
    entry_cutoff: time = time(15, 0)
    square_off_time: time = time(15, 15)
    strike_step: int = Field(default=50, ge=1, le=1000)
    strike_offset_steps: int = Field(default=0, ge=0, le=5)
    lots: int = Field(default=1, ge=1, le=100)
    lot_size: int = Field(ge=1, le=10_000)
    stop_loss_points: float = Field(default=5, gt=0, le=100_000, allow_inf_nan=False)
    take_profit_points: float | None = Field(default=None, gt=0, le=100_000, allow_inf_nan=False)
    max_trades_per_session: int = Field(default=1, ge=1, le=20)

    @field_validator("option_side_mode")
    @classmethod
    def validate_option_side_mode(cls, value: str) -> str:
        if value not in {"both", "ce_only", "pe_only"}:
            raise ValueError("option_side_mode must be both, ce_only or pe_only.")
        return value

    @model_validator(mode="after")
    def validate_session_rules(self):
        if self.candle_interval_minutes not in {1, 5, 10}:
            raise ValueError("candle_interval_minutes must be 1, 5 or 10.")
        if self.opening_range_minutes % self.candle_interval_minutes:
            raise ValueError("opening_range_minutes must be a multiple of candle_interval_minutes.")
        if self.entry_cutoff.tzinfo is not None or self.square_off_time.tzinfo is not None:
            raise ValueError("Session times must be local Asia/Kolkata wall-clock times.")
        if not SESSION_OPEN <= self.entry_cutoff < SESSION_CLOSE:
            raise ValueError("entry_cutoff must be between 09:15 and 15:30.")
        if not SESSION_OPEN <= self.square_off_time < SESSION_CLOSE:
            raise ValueError("square_off_time must be between 09:15 and 15:30.")
        opening_end = (
            datetime.combine(date(2000, 1, 1), SESSION_OPEN)
            + timedelta(minutes=self.opening_range_minutes)
        ).time()
        if self.entry_cutoff < opening_end:
            raise ValueError("entry_cutoff must not be earlier than the opening range end.")
        if self.square_off_time <= self.entry_cutoff:
            raise ValueError("square_off_time must be later than entry_cutoff.")
        return self


@dataclass(frozen=True)
class LiveOrbRules:
    opening_range_minutes: int
    interval_minutes: int
    option_side_mode: str
    entry_cutoff: time
    strike_step: int
    strike_offset_steps: int

    @property
    def opening_range_end(self) -> time:
        return (
            datetime.combine(date(2000, 1, 1), SESSION_OPEN)
            + timedelta(minutes=self.opening_range_minutes)
        ).time()

    @property
    def opening_range_candles(self) -> int:
        return self.opening_range_minutes // self.interval_minutes


class Feed(Protocol):
    def start(self, index_instrument: FeedInstrument) -> None: ...
    def subscribe_ltp(self, instrument: FeedInstrument) -> None: ...
    def request_stop(self) -> None: ...
    def stop(self, timeout_seconds: float = 10) -> None: ...


class LivePaperService:
    def __init__(
        self,
        settings,
        db,
        risk,
        paper,
        provider_session,
        *,
        catalog: GrowwInstrumentCatalog | None = None,
        feed_starter: Callable[[Callable[[MarketTick], None], Callable[[str], None]], Feed] | None = None,
        clock: Callable[[], datetime] | None = None,
        run_timer: bool = True,
    ):
        self.settings = settings
        self.db = db
        self.risk = risk
        self.paper = paper
        self.provider_session = provider_session
        self.catalog = catalog or GrowwInstrumentCatalog()
        self.feed_starter = feed_starter
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.run_timer = run_timer
        self._lock = threading.RLock()
        self._status = "STOPPED"
        self._entries_enabled = False
        self._feed: Feed | None = None
        self._request: LivePaperStartRequest | None = None
        self._session_date: date | None = None
        self._rules: LiveOrbRules | None = None
        self._candles: list[HistoricalCandle] = []
        self._current_candle: HistoricalCandle | None = None
        self._opening: OpeningRange | None = None
        self._opening_error: str | None = None
        self._last_signal_time: datetime | None = None
        self._pending_contract: tuple[str, str, datetime] | None = None
        self._active_trade_id: str | None = None
        self._trade_count = 0
        self._mark_sequence = 0
        self._last_option_tick: MarketTick | None = None
        self._last_index_tick: MarketTick | None = None
        self._failure_reason: str | None = None
        self._timer_stop = threading.Event()
        self._timer_thread: threading.Thread | None = None
        self._restore_state()

    def _restore_state(self) -> None:
        saved = self.db.get_state("live_paper_session")
        if not isinstance(saved, dict) or not saved.get("session_date") or not saved.get("request"):
            return
        self._session_date = date.fromisoformat(saved["session_date"])
        self._request = LivePaperStartRequest.model_validate(saved["request"])
        self._trade_count = int(saved.get("trade_count", 0))
        trade_id = saved.get("trade_id")
        trade = self._find_trade(trade_id) if isinstance(trade_id, str) else None
        if trade is not None and trade["status"] == "OPEN":
            self._status = "RECOVERY_REQUIRED"
            self._active_trade_id = trade_id
            self._failure_reason = "The application restarted with an open simulated position."
            return
        if saved.get("status") in {
            "RUNNING", "STARTING", "STOPPING_POSITION_OPEN",
            "RUNNING_POSITION_RECOVERY", "SESSION_END_EXIT_PENDING",
            "FEED_ERROR", "RECOVERY_REQUIRED",
        }:
            self._status = "RECOVERY_REQUIRED"
            self._failure_reason = (
                "The previous live-paper feed state was not cleanly stopped; "
                "start again only if no trade was opened and the opening range can be rebuilt."
            )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Feed and service timestamps must include a timezone.")
        return value.astimezone(timezone.utc)

    def _create_feed(self, index_instrument: FeedInstrument) -> Feed:
        starter = self.feed_starter
        if starter is None:
            starter = getattr(self.provider_session, "start_market_feed", None)
        if not callable(starter):
            raise ValueError("The connected provider does not support Groww market feeds.")
        return starter(self.on_tick, self.on_feed_error)

    def start(self, request: LivePaperStartRequest) -> dict:
        now = self._as_utc(self.clock())
        local_now = now.astimezone(KOLKATA)
        session_date = local_now.date()
        if not time(9, 0) <= local_now.time().replace(tzinfo=None) < request.square_off_time:
            raise ValueError("Live paper ORB can only be started between 09:00 and square-off time Asia/Kolkata.")
        if request.expiry_date < session_date:
            raise ValueError("expiry_date cannot be earlier than the current Asia/Kolkata date.")
        if request.lots > self.settings.max_lots_per_order:
            raise ValueError("lots exceeds the configured maximum per order.")
        with self._lock:
            if self._active_trade_id is not None:
                trade = self._find_trade(self._active_trade_id)
                if trade is not None and trade["status"] == "OPEN":
                    raise ValueError("Close or recover the existing live paper position before starting another session.")
                self._active_trade_id = None
            if (
                self._session_date == session_date
                and self._trade_count > 0
            ):
                raise ValueError("A live paper ORB entry has already been recorded for this Asia/Kolkata calendar date.")
            if (
                self._status == "RECOVERY_REQUIRED"
                and self._session_date is not None
                and session_date <= self._session_date
            ):
                raise ValueError("The previous live-paper state must be reconciled before starting on this date.")
            if self._status not in {"STOPPED"}:
                if (
                    self._status == "RECOVERY_REQUIRED"
                    and self._session_date is not None
                    and session_date > self._session_date
                    and self._active_trade_id is None
                ):
                    self._status = "STOPPED"
                else:
                    raise ValueError("A live paper session is already active or requires recovery.")
            self._request = request
            self._session_date = session_date
            self._rules = LiveOrbRules(
                opening_range_minutes=request.opening_range_minutes,
                interval_minutes=request.candle_interval_minutes,
                option_side_mode=request.option_side_mode,
                entry_cutoff=request.entry_cutoff,
                strike_step=request.strike_step,
                strike_offset_steps=request.strike_offset_steps,
            )
            self._candles.clear()
            self._current_candle = None
            self._opening = None
            self._opening_error = None
            self._last_signal_time = None
            self._pending_contract = None
            self._active_trade_id = None
            self._trade_count = 0
            self._failure_reason = None
            self._entries_enabled = True
            self._status = "STARTING"
            self._persist_state()

        feed: Feed | None = None
        startup_stage = "option_chain"
        LIVE_FEED_DIAGNOSTICS.record(
            "live_paper",
            "start",
            "started",
            details={
                "session_date": session_date.isoformat(),
                "expiry_date": request.expiry_date.isoformat(),
            },
        )
        try:
            chain = self.provider_session.option_chain(
                "NIFTY", request.expiry_date.isoformat()
            )
            if (
                chain.get("underlying") != "NIFTY"
                or chain.get("expiry") != request.expiry_date.isoformat()
                or not isinstance(chain.get("strikes"), list)
                or not chain["strikes"]
            ):
                raise ValueError("Groww did not return a valid option chain for the selected expiry.")
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper",
                "option_chain",
                "succeeded",
                details={"strike_count": len(chain["strikes"])},
            )
            startup_stage = "instrument_catalog"
            index_instrument = self.catalog.resolve("NSE-NIFTY", "CASH")
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper",
                "instrument_catalog",
                "succeeded",
                details={
                    "exchange": index_instrument.exchange,
                    "segment": index_instrument.segment,
                },
            )
            startup_stage = "provider_feed_create"
            feed = self._create_feed(index_instrument)
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper", "provider_feed_create", "succeeded"
            )
            startup_stage = "feed_start"
            feed.start(index_instrument)
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper", "feed_start", "succeeded"
            )
        except Exception as exc:
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper",
                startup_stage,
                "failed",
                error=exc,
            )
            if feed is not None:
                feed.stop()
            logger.warning(
                "Live-paper feed startup failed (%s); details withheld.",
                type(exc).__name__,
            )
            with self._lock:
                self._entries_enabled = False
                self._status = "STOPPED"
                self._failure_reason = (
                    str(exc)
                    if isinstance(exc, (ValueError, MarketFeedError, ProviderRequestError))
                    else "Groww live feed could not be started."
                )
                self._persist_state()
            raise ValueError(self._failure_reason) from None

        with self._lock:
            self._feed = feed
            self._status = "RUNNING"
            self._persist_state()
            self.db.event(
                "LIVE_PAPER_STARTED",
                {
                    "session_date": session_date.isoformat(),
                    "expiry_date": request.expiry_date.isoformat(),
                    "candle_interval_minutes": request.candle_interval_minutes,
                },
            )
            self._start_timer()
            return self.status()

    def resume(self) -> dict:
        with self._lock:
            if self._status != "RECOVERY_REQUIRED" or self._request is None:
                raise ValueError("No recoverable live paper position is available.")
            request = self._request
            trade = self._find_trade(self._active_trade_id)
            if trade is None or trade["status"] != "OPEN":
                raise ValueError("The live paper position is no longer open.")
            if self._session_date != self.risk.trading_date(self._as_utc(self.clock())):
                raise ValueError("A live paper position from a previous calendar date cannot be auto-resumed.")
            self._status = "STARTING"
            self._entries_enabled = False
            self._persist_state()
        feed: Feed | None = None
        try:
            index_instrument = self.catalog.resolve("NSE-NIFTY", "CASH")
            option_instrument = self.catalog.resolve(trade["symbol"], "FNO")
            feed = self._create_feed(index_instrument)
            feed.start(index_instrument)
            feed.subscribe_ltp(option_instrument)
        except Exception as exc:
            if feed is not None:
                feed.stop()
            logger.warning(
                "Live-paper position recovery failed (%s); details withheld.",
                type(exc).__name__,
            )
            with self._lock:
                self._status = "RECOVERY_REQUIRED"
                self._failure_reason = str(exc) if isinstance(exc, (ValueError, MarketFeedError)) else "The open paper position could not be recovered."
                self._persist_state()
            raise ValueError(self._failure_reason) from None
        with self._lock:
            self._feed = feed
            self._mark_sequence = int(trade["mark_sequence"])
            self._status = "RUNNING_POSITION_RECOVERY"
            self._persist_state()
            self.db.event("LIVE_PAPER_POSITION_RECOVERED", {"trade_id": trade["trade_id"]})
            self._start_timer()
            return self.status()

    def on_feed_error(self, message: str) -> None:
        with self._lock:
            self._failure_reason = message
            if self._status in {
                "RUNNING", "RUNNING_POSITION_RECOVERY", "STOPPING_POSITION_OPEN",
                "SESSION_END_EXIT_PENDING",
            }:
                self._entries_enabled = False
                self._status = (
                    "RECOVERY_REQUIRED"
                    if self._active_trade_id is not None
                    else "FEED_ERROR"
                )
            self.db.event("LIVE_PAPER_FEED_ERROR", {"message": message})
            LIVE_FEED_DIAGNOSTICS.record(
                "live_paper",
                "feed_callback",
                "failed",
                details={"message": message},
            )
            self._persist_state()

    def on_tick(self, tick: MarketTick) -> None:
        with self._lock:
            if self._status not in {
                "RUNNING", "RUNNING_POSITION_RECOVERY", "STOPPING_POSITION_OPEN",
                "SESSION_END_EXIT_PENDING",
            }:
                return
            try:
                observed_at = self._as_utc(tick.timestamp)
                now = self._as_utc(self.clock())
            except ValueError as exc:
                self.db.event("LIVE_PAPER_TICK_REJECTED", {"reason": str(exc)})
                return
            age_seconds = (now - observed_at).total_seconds()
            if (
                not math.isfinite(tick.price)
                or tick.price <= 0
                or age_seconds < -1
                or age_seconds > self.settings.market_data_max_age_seconds
            ):
                self.db.event(
                    "LIVE_PAPER_TICK_REJECTED",
                    {"symbol": tick.groww_symbol, "age_seconds": age_seconds},
                )
                return
            if self._session_date != observed_at.astimezone(KOLKATA).date():
                self._entries_enabled = False
                self._status = (
                    "RECOVERY_REQUIRED"
                    if self._active_trade_id is not None
                    else "STOPPED"
                )
                self._failure_reason = (
                    "Groww feed delivered a tick outside the active Asia/Kolkata calendar date."
                )
                self._stop_feed()
                self._persist_state()
                return

            if tick.segment == "CASH" and tick.groww_symbol == "NSE-NIFTY":
                previous = self._last_index_tick
                if previous is not None and tick.timestamp <= previous.timestamp:
                    self.db.event(
                        "LIVE_PAPER_TICK_REJECTED",
                        {
                            "symbol": tick.groww_symbol,
                            "reason": "Duplicate or out-of-order source timestamp.",
                        },
                    )
                    return
                self._last_index_tick = tick
                self._process_index_tick(tick)
            elif tick.segment == "FNO":
                self._process_option_tick(tick, age_seconds)
            self.check_time(now=now)

    def _process_index_tick(self, tick: MarketTick) -> None:
        if not self._entries_enabled or self._rules is None or self._session_date is None:
            return
        local = tick.timestamp.astimezone(KOLKATA)
        if local.time().replace(tzinfo=None) < SESSION_OPEN or local.time().replace(tzinfo=None) >= SESSION_CLOSE:
            return
        step = timedelta(minutes=self._rules.interval_minutes)
        session_open = datetime.combine(local.date(), SESSION_OPEN, tzinfo=KOLKATA)
        elapsed_minutes = int((local - session_open).total_seconds() // 60)
        bucket_offset = elapsed_minutes - elapsed_minutes % self._rules.interval_minutes
        bucket = (session_open + timedelta(minutes=bucket_offset)).replace(tzinfo=None)
        if self._current_candle is None:
            self._current_candle = HistoricalCandle(
                timestamp=bucket,
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=None,
            )
            return
        if bucket < self._current_candle.timestamp:
            return
        if bucket == self._current_candle.timestamp:
            current = self._current_candle
            self._current_candle = HistoricalCandle(
                timestamp=current.timestamp,
                open=current.open,
                high=max(current.high, tick.price),
                low=min(current.low, tick.price),
                close=tick.price,
                volume=None,
            )
            return
        closed = self._current_candle
        self._candles.append(closed)
        self._current_candle = HistoricalCandle(
            timestamp=bucket,
            open=tick.price,
            high=tick.price,
            low=tick.price,
            close=tick.price,
            volume=None,
        )
        if bucket - closed.timestamp > step:
            self.db.event(
                "LIVE_PAPER_CANDLE_GAP",
                {
                    "previous_candle": closed.timestamp.isoformat(),
                    "next_candle": bucket.isoformat(),
                },
            )
        self._process_closed_candle(closed)

    def _process_closed_candle(self, candle: HistoricalCandle) -> None:
        assert self._rules is not None and self._session_date is not None
        candle_end = candle.timestamp + timedelta(minutes=self._rules.interval_minutes)
        if (
            self._opening is None
            and self._opening_error is None
            and candle_end.time() >= self._rules.opening_range_end
        ):
            opening, error = compute_opening_range(self._candles, self._session_date, self._rules)
            if opening is None:
                self._opening_error = error
                self.db.event("LIVE_PAPER_OPENING_RANGE_REJECTED", {"reason": error})
                return
            self._opening = opening
            self.db.event(
                "LIVE_PAPER_OPENING_RANGE_READY",
                {"high": opening.high, "low": opening.low, "end": opening.end.isoformat()},
            )
        if (
            self._opening is None
            or not self._entries_enabled
            or self._pending_contract is not None
            or self._active_trade_id is not None
            or self._request is None
            or self._trade_count >= self._request.max_trades_per_session
        ):
            return
        earliest = (
            self._opening.end
            if self._last_signal_time is None
            else self._last_signal_time
        )
        breakout = find_breakout(self._candles, self._opening, earliest, self._rules)
        if breakout is None or breakout.signal_time.time() > self._rules.entry_cutoff:
            return
        if self._last_signal_time is not None and breakout.signal_time <= self._last_signal_time:
            return
        self._last_signal_time = breakout.signal_time
        self.db.event(
            "LIVE_PAPER_BREAKOUT",
            {
                "direction": breakout.direction,
                "signal_time": breakout.signal_time.isoformat(),
                "underlying_close": breakout.underlying_close,
            },
        )
        self._prepare_option_entry(breakout)

    def _prepare_option_entry(self, breakout: Breakout) -> None:
        assert self._request is not None and self._feed is not None and self._rules is not None
        option_type = "CE" if breakout.direction == "UP" else "PE"
        strike = target_strike(breakout.underlying_close, breakout.direction, self._rules)
        try:
            chain = self.provider_session.option_chain("NIFTY", self._request.expiry_date.isoformat())
            selected = next(
                (
                    row for row in chain["strikes"]
                    if abs(float(row["strike"]) - strike) < 0.01
                ),
                None,
            )
            quote = None if selected is None else selected["call" if option_type == "CE" else "put"]
            symbol = quote.get("trading_symbol") if isinstance(quote, dict) else None
            if not isinstance(symbol, str) or not symbol.startswith("NSE-NIFTY-") or not symbol.endswith("-" + option_type):
                raise ValueError("Groww option chain did not return the selected NIFTY contract.")
            instrument = self.catalog.resolve(symbol, "FNO")
            self._pending_contract = (symbol, option_type, breakout.signal_time)
            self._feed.subscribe_ltp(instrument)
            self.db.event(
                "LIVE_PAPER_OPTION_SUBSCRIBED",
                {"symbol": symbol, "strike": strike, "option_type": option_type},
            )
        except (ProviderRequestError, ValueError, MarketFeedError) as exc:
            self._pending_contract = None
            message = exc.message if isinstance(exc, ProviderRequestError) else str(exc)
            self.db.event(
                "LIVE_PAPER_ENTRY_REJECTED",
                {"signal_time": breakout.signal_time.isoformat(), "reason": message},
            )
            if isinstance(exc, ProviderRequestError) and exc.status_code == 401:
                self.on_feed_error(exc.message)

    def _process_option_tick(self, tick: MarketTick, age_seconds: float) -> None:
        previous = self._last_option_tick
        if (
            previous is not None
            and previous.groww_symbol == tick.groww_symbol
            and tick.timestamp <= previous.timestamp
        ):
            self.db.event(
                "LIVE_PAPER_TICK_REJECTED",
                {
                    "symbol": tick.groww_symbol,
                    "reason": "Duplicate or out-of-order source timestamp.",
                },
            )
            return
        if self._pending_contract is not None:
            symbol, option_type, signal_time = self._pending_contract
            if tick.groww_symbol != symbol or tick.timestamp.astimezone(KOLKATA).replace(tzinfo=None) <= signal_time:
                return
            assert self._request is not None
            quantity = self._request.lots * self._request.lot_size
            stop_price = tick.price - self._request.stop_loss_points
            if stop_price <= 0:
                self._pending_contract = None
                self.db.event(
                    "LIVE_PAPER_ENTRY_REJECTED",
                    {"symbol": symbol, "reason": "Configured stop distance is not below the entry premium."},
                )
                return
            target_profit = (
                self._request.take_profit_points * quantity
                if self._request.take_profit_points is not None
                else None
            )
            request = PaperTradeRequest(
                symbol=symbol,
                idempotency_key=(
                    f"live-orb:{self._session_date.isoformat()}:{signal_time.isoformat()}:{symbol}"
                )[:128],
                side="BUY",
                entry_price=tick.price,
                stop_price=stop_price,
                take_profit_profit_inr=target_profit,
                quantity=quantity,
                lots=self._request.lots,
                market_data_age_seconds=max(0, age_seconds),
            )
            try:
                if self.risk.snapshot().open_positions:
                    raise RiskRejected(
                        "A paper position is already open; live ORB allows one open position at a time."
                    )
                trade = self.paper.open_trade(request)
            except (RiskRejected, IdempotencyConflict, ValueError) as exc:
                self._pending_contract = None
                self.db.event("LIVE_PAPER_ENTRY_REJECTED", {"symbol": symbol, "reason": str(exc)})
                return
            self._pending_contract = None
            self._active_trade_id = trade.trade_id
            self._trade_count += 1
            self._mark_sequence = trade.mark_sequence
            self._last_option_tick = tick
            self._status = "RUNNING"
            self._persist_state()
            self.db.event(
                "LIVE_PAPER_ENTRY_FILLED",
                {
                    "trade_id": trade.trade_id,
                    "symbol": symbol,
                    "option_type": option_type,
                    "fill_price": tick.price,
                    "observed_at": tick.timestamp.isoformat(),
                },
            )
            return
        if self._active_trade_id is None:
            return
        trade = self._find_trade(self._active_trade_id)
        if trade is None or trade["status"] != "OPEN" or tick.groww_symbol != trade["symbol"]:
            return
        self._last_option_tick = tick
        self._mark_sequence = max(self._mark_sequence, int(trade["mark_sequence"])) + 1
        updated = self.paper.mark(trade["trade_id"], tick.price, self._mark_sequence)
        if updated.status == "CLOSED":
            self.db.event(
                "LIVE_PAPER_POSITION_CLOSED",
                {
                    "trade_id": updated.trade_id,
                    "exit_price": updated.mark_price,
                    "pnl": updated.realised_pnl,
                    "reason": updated.close_reason,
                },
            )
            self._active_trade_id = None
            self._persist_state()
            if not self._entries_enabled:
                self._finish_stopped_session()

    def check_time(self, *, now: datetime | None = None) -> None:
        with self._lock:
            if (
                self._request is None
                or self._session_date is None
                or self._status in {"STOPPED", "RECOVERY_REQUIRED", "FEED_ERROR"}
            ):
                return
            current = self._as_utc(now or self.clock())
            local = current.astimezone(KOLKATA)
            if local.date() != self._session_date:
                self._entries_enabled = False
                if self._active_trade_id is not None:
                    self._status = "RECOVERY_REQUIRED"
                    self._failure_reason = "Session date changed with an open simulated position; reconcile it manually."
                else:
                    self._status = "STOPPED"
                self._stop_feed()
                self._persist_state()
                return
            if local.time().replace(tzinfo=None) < self._request.square_off_time:
                return
            self._entries_enabled = False
            if self._active_trade_id is None:
                self._finish_stopped_session()
                return
            last_tick = self._last_option_tick
            if last_tick is None or (current - self._as_utc(last_tick.timestamp)).total_seconds() > self.settings.market_data_max_age_seconds:
                self._status = "SESSION_END_EXIT_PENDING"
                self._failure_reason = "No fresh option premium is available for the session-end paper exit."
                self._persist_state()
                return
            trade = self._find_trade(self._active_trade_id)
            if trade is not None and trade["status"] == "OPEN":
                closed = self.paper.close_trade(trade["trade_id"], last_tick.price, "LIVE_SESSION_END")
                self.db.event(
                    "LIVE_PAPER_POSITION_CLOSED",
                    {
                        "trade_id": closed.trade_id,
                        "exit_price": closed.mark_price,
                        "pnl": closed.realised_pnl,
                        "reason": "LIVE_SESSION_END",
                    },
                )
            self._active_trade_id = None
            self._finish_stopped_session()

    def stop(self) -> dict:
        with self._lock:
            self._entries_enabled = False
            self._pending_contract = None
            if self._active_trade_id is not None:
                self._status = "STOPPING_POSITION_OPEN"
                self._failure_reason = "New entries are disabled; the open paper position remains subscribed for exits."
                self._persist_state()
                return self.status()
            self._finish_stopped_session()
            return self.status()

    def shutdown(self) -> None:
        with self._lock:
            self._timer_stop.set()
            if self._active_trade_id is not None:
                self._status = "RECOVERY_REQUIRED"
                self._entries_enabled = False
                self._failure_reason = "Application stopped with an open simulated position."
                self._persist_state()
            else:
                self._status = "STOPPED"
                self._entries_enabled = False
                self._persist_state()
            feed = self._detach_feed()
        if feed is not None:
            feed.stop()

    def _finish_stopped_session(self) -> None:
        self._entries_enabled = False
        self._status = "STOPPED"
        self._failure_reason = None
        self._stop_feed()
        self._persist_state()

    def _stop_feed(self) -> None:
        feed = self._detach_feed()
        if feed is not None:
            feed.request_stop()

    def _detach_feed(self) -> Feed | None:
        feed = self._feed
        self._feed = None
        return feed

    def _start_timer(self) -> None:
        if not self.run_timer:
            return
        if self._timer_thread and self._timer_thread.is_alive():
            if not self._timer_stop.is_set():
                return
            self._timer_thread.join(timeout=1)
            if self._timer_thread.is_alive():
                return
        self._timer_stop.clear()

        def monitor() -> None:
            while not self._timer_stop.wait(0.5):
                self.check_time()

        self._timer_thread = threading.Thread(
            target=monitor, name="aots-live-paper-clock", daemon=True
        )
        self._timer_thread.start()

    def _persist_state(self) -> None:
        if self._request is None or self._session_date is None:
            return
        self.db.set_state(
            "live_paper_session",
            {
                "status": self._status,
                "session_date": self._session_date.isoformat(),
                "request": self._request.model_dump(mode="json"),
                "trade_id": self._active_trade_id,
                "trade_count": self._trade_count,
                "entries_enabled": self._entries_enabled,
            },
        )

    def _find_trade(self, trade_id: str | None):
        if not trade_id:
            return None
        return next((trade for trade in self.db.trades() if trade["trade_id"] == trade_id), None)

    def status(self) -> dict:
        with self._lock:
            trade = self._find_trade(self._active_trade_id)
            current_date = self.risk.trading_date(self._as_utc(self.clock()))
            open_position = trade is not None and trade["status"] == "OPEN"
            can_start = not open_position and (
                self._status == "STOPPED"
                and not (
                    self._session_date == current_date
                    and self._trade_count > 0
                )
                or (
                    self._status == "RECOVERY_REQUIRED"
                    and self._session_date is not None
                    and current_date > self._session_date
                    and self._active_trade_id is None
                )
            )
            can_resume = (
                self._status == "RECOVERY_REQUIRED"
                and open_position
                and self._session_date == current_date
            )
            return {
                "status": self._status,
                "session_date": self._session_date.isoformat() if self._session_date else None,
                "expiry_date": self._request.expiry_date.isoformat() if self._request else None,
                "entries_enabled": self._entries_enabled,
                "opening_range": (
                    {"high": self._opening.high, "low": self._opening.low,
                     "end": self._opening.end.isoformat()}
                    if self._opening else None
                ),
                "opening_range_error": self._opening_error,
                "completed_index_candles": len(self._candles),
                "last_index_price": self._last_index_tick.price if self._last_index_tick else None,
                "last_index_time": self._last_index_tick.timestamp.isoformat() if self._last_index_tick else None,
                "active_trade": trade,
                "trade_count": self._trade_count,
                "failure_reason": self._failure_reason,
                "can_start": can_start,
                "can_resume": can_resume,
            }
