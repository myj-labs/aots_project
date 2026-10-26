"""Deterministic NIFTY options ORB backtest engine.

Pure computation: no network, clock or database access. Option data is obtained
through an injected ``resolver``, so every replay is reproducible from its inputs.
Signals use only NIFTY index candles that have closed; fills use option premiums
from later candles; nothing here can place an order.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Sequence

from ..brokers.base import HistoricalCandle
from .config import SESSION_OPEN, CostConfig, OptionsBacktestConfig
from .data import OptionContract, validate_session_candles
from .forensics import build_trade_loss_report

LOT_SIZE_WARNING = (
    "Lot size is user-supplied for this run; Groww's historical endpoints do not return "
    "historical lot sizes, so it is unverified for this contract and date."
)
TIMESTAMP_NOTE = (
    "Groww timestamps are timezone-unspecified. They are shown as supplied and checked against "
    "the selected interval's 09:15-to-session-close grid; this grid check does not verify timezone semantics."
)


class OptionUnavailable(Exception):
    """Raised by a resolver when the historical contract or its premiums cannot be used."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedOption:
    contract: OptionContract
    candles: list[HistoricalCandle]
    expiry_rule: str
    warnings: list[str] = field(default_factory=list)


Resolver = Callable[[date, str, int, datetime], ResolvedOption]


@dataclass(frozen=True)
class OpeningRange:
    high: float
    low: float
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Breakout:
    candle_start: datetime
    signal_time: datetime
    direction: str
    underlying_close: float


@dataclass
class SessionResult:
    session_date: date
    status: str
    reason_code: str | None = None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    opening_range: dict[str, Any] | None = None
    vwap_symbol: str | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    comparison_trades: list[dict[str, Any]] = field(default_factory=list)
    loss_reports: list[dict[str, Any]] = field(default_factory=list)
    underlying_candles: int = 0
    underlying_missing_candles: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date.isoformat(),
            "status": self.status,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "warnings": list(self.warnings),
            "opening_range": self.opening_range,
            "vwap_symbol": self.vwap_symbol,
            "signals": list(self.signals),
            "trades": list(self.trades),
            "comparison_trades": list(self.comparison_trades),
            "loss_reports": list(self.loss_reports),
            "underlying_candles": self.underlying_candles,
            "underlying_missing_candles": self.underlying_missing_candles,
        }


def compute_opening_range(
    candles: Sequence[HistoricalCandle], session_date: date, config: OptionsBacktestConfig
) -> tuple[OpeningRange | None, str | None]:
    start = datetime.combine(session_date, SESSION_OPEN)
    end = datetime.combine(session_date, config.opening_range_end)
    window = [candle for candle in candles if start <= candle.timestamp < end]
    if len(window) != config.opening_range_candles:
        return None, (
            f"Opening range needs {config.opening_range_candles} candles but "
            f"{len(window)} were available."
        )
    high = max(candle.high for candle in window)
    low = min(candle.low for candle in window)
    if not high > low:
        return None, "Opening range has zero width."
    return OpeningRange(high, low, start, end), None


def find_breakout(
    candles: Sequence[HistoricalCandle],
    opening: OpeningRange,
    not_before: datetime,
    config: OptionsBacktestConfig,
) -> Breakout | None:
    """Find the next ORB breakout event, matching the underlying-index ORB transition rule."""
    earliest = max(opening.end, not_before)
    previous_direction: str | None = None
    for candle in candles:
        if candle.timestamp < opening.end:
            continue
        if candle.close > opening.high:
            direction = "UP"
        elif candle.close < opening.low:
            direction = "DOWN"
        else:
            previous_direction = None
            continue
        is_new_event = direction != previous_direction
        previous_direction = direction
        if not is_new_event or candle.timestamp < earliest:
            continue
        if direction == "UP" and config.option_side_mode == "pe_only":
            continue
        if direction == "DOWN" and config.option_side_mode == "ce_only":
            continue
        step = timedelta(minutes=config.interval_minutes)
        return Breakout(candle.timestamp, candle.timestamp + step, direction, candle.close)
    return None


def target_strike(underlying_close: float, direction: str, config: OptionsBacktestConfig) -> int:
    atm = int(math.floor(underlying_close / config.strike_step + 0.5)) * config.strike_step
    offset = config.strike_offset_steps * config.strike_step
    return atm + offset if direction == "UP" else atm - offset


def compute_costs(entry: float, exit_price: float, quantity: int, costs: CostConfig) -> dict[str, Any]:
    buy_value = entry * quantity
    sell_value = exit_price * quantity
    brokerage = 2 * costs.brokerage_per_order_inr
    exchange = (buy_value + sell_value) * costs.exchange_txn_pct / 100
    parts = {
        "brokerage": round(brokerage, 2),
        "stt": round(sell_value * costs.stt_sell_pct / 100, 2),
        "exchange_charges": round(exchange, 2),
        "stamp_duty": round(buy_value * costs.stamp_duty_buy_pct / 100, 2),
        "gst": round((brokerage + exchange) * costs.gst_pct / 100, 2),
    }
    charges = round(sum(parts.values()), 2)
    slippage = round(2 * costs.slippage_per_unit_inr * quantity, 2)
    return {
        **parts,
        "charges_total": charges,
        "slippage": slippage,
        "total_costs": round(charges + slippage, 2),
        "configured": costs.configured,
    }


def _check_exit(candle: HistoricalCandle, stop: float, target: float, stop_reason: str = "STOP_LOSS"):
    if candle.open <= stop:
        return candle.open, stop_reason, "Stop gapped through; filled at the candle open."
    touched_stop = candle.low <= stop
    touched_target = candle.high >= target
    if touched_stop:
        note = (
            "Stop and target were both touched in one candle; stop-first assumed."
            if touched_target
            else None
        )
        return stop, stop_reason, note
    if candle.open >= target:
        return target, "TARGET", "Target filled at the target level; no price improvement modelled."
    if touched_target:
        return target, "TARGET", None
    return None


def _wilder_atr(
    candles: Sequence[HistoricalCandle],
    period: int,
    step: timedelta,
) -> list[float | None]:
    """Return ATR known at each candle close; missing intervals restart warm-up."""
    values: list[float | None] = [None] * len(candles)
    true_ranges: list[float] = []
    previous_close: float | None = None
    previous_time: datetime | None = None
    atr: float | None = None

    for index, candle in enumerate(candles):
        if previous_time is not None and candle.timestamp - previous_time != step:
            true_ranges.clear()
            previous_close = None
            atr = None

        if previous_close is None:
            true_range = candle.high - candle.low
        else:
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )

        if atr is None:
            true_ranges.append(true_range)
            if len(true_ranges) == period:
                atr = sum(true_ranges) / period
        else:
            atr = ((period - 1) * atr + true_range) / period

        values[index] = atr
        previous_close = candle.close
        previous_time = candle.timestamp

    return values


def _session_vwap(
    candles: Sequence[HistoricalCandle],
    step: timedelta,
) -> dict[datetime, tuple[datetime, float, float]]:
    """Return session VWAP keyed by candle close time, using volume-weighted typical price."""
    cumulative_volume = 0.0
    cumulative_value = 0.0
    values: dict[datetime, tuple[datetime, float, float]] = {}
    for candle in candles:
        if candle.volume is None:
            raise ValueError("NIFTY candle volume is required to calculate VWAP.")
        if candle.volume > 0:
            typical_price = (candle.high + candle.low + candle.close) / 3
            cumulative_value += typical_price * candle.volume
            cumulative_volume += candle.volume
        if cumulative_volume > 0:
            values[candle.timestamp + step] = (
                candle.timestamp,
                candle.close,
                cumulative_value / cumulative_volume,
            )
    return values


def _skip(code: str, message: str, *, data: bool) -> dict[str, Any]:
    return {"skip": True, "code": code, "message": message, "data_driven": data}


def _false_breakout_close(
    candles: Sequence[HistoricalCandle],
    opening: OpeningRange,
    entry_time: datetime,
    exit_time: datetime,
    step: timedelta,
) -> datetime | None:
    """Find a completed NIFTY candle that closes back inside the OR during the trade."""
    for candle in candles:
        close_time = candle.timestamp + step
        if candle.timestamp < entry_time or close_time > exit_time:
            continue
        if opening.low <= candle.close <= opening.high:
            return close_time
    return None


def _simulate_trade(
    *,
    session_date: date,
    sequence: int,
    breakout: Breakout,
    opening: OpeningRange,
    option_type: str,
    strike: int,
    resolved: ResolvedOption,
    config: OptionsBacktestConfig,
    equity: float,
    vwap_by_close: dict[datetime, tuple[datetime, float, float]] | None = None,
) -> dict[str, Any]:
    entry_start = breakout.signal_time
    step = timedelta(minutes=config.interval_minutes)
    all_candles = list(resolved.candles)
    entry_index = next(
        (index for index, candle in enumerate(all_candles) if candle.timestamp >= entry_start),
        None,
    )
    if entry_index is None or all_candles[entry_index].timestamp != entry_start:
        return _skip(
            "entry_candle_missing",
            f"No option premium candle starts at {entry_start:%H:%M}; the entry was not simulated.",
            data=True,
        )
    candles = all_candles[entry_index:]
    entry_candle = candles[0]
    if entry_candle.volume <= 0:
        return _skip(
            "no_entry_volume",
            "The option traded no volume in the entry candle; the entry was not simulated.",
            data=False,
        )
    quantity = config.lots * config.lot_size
    entry_premium = entry_candle.open
    if entry_premium * quantity > equity:
        return _skip(
            "insufficient_simulated_capital",
            "Premium outlay exceeds the available simulated capital.",
            data=False,
        )
    if config.exit_level_mode == "percentage":
        stop = entry_premium * (1 - config.stop_loss_pct / 100)
        target = entry_premium * (1 + config.target_pct / 100)
        stop_loss_input = config.stop_loss_pct
        target_input = config.target_pct
    else:
        if config.stop_loss_points >= entry_premium:
            return _skip(
                "invalid_absolute_stop",
                f"The ₹{config.stop_loss_points:g} stop distance must be less than the "
                f"₹{entry_premium:g} entry premium so the stop remains above zero.",
                data=False,
            )
        stop = entry_premium - config.stop_loss_points
        target = entry_premium + config.target_points
        stop_loss_input = config.stop_loss_points
        target_input = config.target_points
    if config.exit_strategy == "trailing_stop" and config.initial_stop_loss is not None:
        if config.initial_stop_loss >= entry_premium:
            return _skip(
                "invalid_initial_stop",
                f"The ₹{config.initial_stop_loss:g} initial stop distance must be less than the "
                f"₹{entry_premium:g} entry premium so the stop remains above zero.",
                data=False,
            )
        stop = entry_premium - config.initial_stop_loss
    atr_values = (
        _wilder_atr(all_candles, config.atr_period, step)
        if config.exit_strategy == "atr"
        else [None] * len(all_candles)
    )
    prior_atr = atr_values[entry_index - 1] if config.exit_strategy == "atr" and entry_index > 0 else None
    trailing_stop = (
        max(stop, entry_premium - prior_atr * config.atr_multiplier)
        if prior_atr is not None
        else None
    )
    highest_premium = entry_premium
    trailing_peak = entry_premium
    trailing_stop: float | None = None
    trailing_active = config.trailing_stop_activation is None
    square_off = datetime.combine(session_date, config.square_off_time)

    warnings = list(resolved.warnings) + [LOT_SIZE_WARNING]
    previous: HistoricalCandle | None = None
    missing_during_hold = 0
    zero_volume = 0
    exit_candle: HistoricalCandle | None = None
    exit_premium = 0.0
    exit_reason = ""
    vwap_exit_value: float | None = None
    vwap_exit_signal_time: datetime | None = None
    held_candles: list[HistoricalCandle] = []
    stop_timeline: list[dict[str, Any]] = []
    for candle_index, candle in enumerate(candles, start=entry_index):
        held_candles.append(candle)
        if previous is not None:
            missing_during_hold += max(0, int((candle.timestamp - previous.timestamp) / step) - 1)
        previous = candle
        if candle.volume <= 0:
            zero_volume += 1
        if config.exit_on_session_end and candle.timestamp >= square_off:
            exit_candle, exit_premium, exit_reason = candle, candle.open, "TIME_EXIT"
            delay = int((candle.timestamp - square_off) / step)
            if delay:
                warnings.append(
                    f"Time exit executed {delay * config.interval_minutes} minute(s) after the configured "
                    "square-off because those candles are missing."
                )
            break
        if config.exit_strategy == "trailing_stop" and candle_index == entry_index:
            # The candle's range may include prices printed before the open fill.
            stop_timeline.append({
                "timestamp": candle.timestamp.isoformat(),
                "event_type": "INITIAL_STOP_SET_AT_ENTRY",
                "stop_price": round(stop, 4),
                "target_price": round(target, 4),
                "running_peak": entry_premium,
                "note": "Entry-candle extremes excluded because their sequence relative to fill is unknown.",
            })
            continue
        if config.exit_strategy == "trailing_stop":
            active_stop = max(stop, trailing_stop) if trailing_stop is not None else stop
            stop_reason = (
                "TRAILING_STOP"
                if trailing_stop is not None and trailing_stop > stop
                else "STOP_LOSS"
            )
            stop_timeline.append({
                "timestamp": candle.timestamp.isoformat(),
                "event_type": "STOP_CHECK_BEFORE_CANDLE_RANGE",
                "stop_price": round(active_stop, 4),
                "stop_reason": stop_reason,
                "target_price": round(target, 4),
                "running_peak": round(trailing_peak, 4),
                "candle_open": candle.open,
                "candle_low": candle.low,
                "candle_high": candle.high,
            })
            if candle.open <= active_stop:
                exit_candle, exit_premium, exit_reason = candle, candle.open, stop_reason
                warnings.append("Stop gapped through; filled at the candle open.")
                break
            if candle.low <= active_stop:
                exit_candle, exit_premium, exit_reason = candle, active_stop, stop_reason
                if candle.high >= target:
                    warnings.append(
                        "An active stop and target were both touched in one candle; stop-first assumed."
                    )
                break
            if candle.open >= target or candle.high >= target:
                exit_candle, exit_premium, exit_reason = candle, target, "TARGET"
                warnings.append(
                    "Target filled at the target level; no price improvement modelled."
                )
                break

            if candle.high > trailing_peak:
                trailing_peak = candle.high
            if (
                not trailing_active
                and trailing_peak - entry_premium >= config.trailing_stop_activation
            ):
                trailing_active = True
            if trailing_active:
                candidate_stop = trailing_peak - config.trailing_stop_distance
                trailing_stop = max(stop, trailing_stop or stop, candidate_stop)
                stop_timeline.append({
                    "timestamp": candle.timestamp.isoformat(),
                    "event_type": "TRAIL_RAISED_FOR_NEXT_CANDLE",
                    "stop_price": round(trailing_stop, 4),
                    "running_peak": round(trailing_peak, 4),
                    "trailing_distance": config.trailing_stop_distance,
                    "effective_next_stop": round(max(stop, trailing_stop), 4),
                })
            highest_premium = max(highest_premium, candle.high)
            continue
        effective_stop = (
            max(stop, trailing_stop)
            if config.exit_strategy == "atr" and trailing_stop is not None
            else stop
        )
        stop_reason = "ATR_TRAILING_STOP" if trailing_stop is not None and trailing_stop > stop else "STOP_LOSS"
        stop_timeline.append({
            "timestamp": candle.timestamp.isoformat(),
            "event_type": "STOP_CHECK_BEFORE_CANDLE_RANGE",
            "stop_price": round(effective_stop, 4),
            "stop_reason": stop_reason,
            "target_price": round(target, 4),
            "running_peak": round(highest_premium, 4),
            "candle_open": candle.open,
            "candle_low": candle.low,
            "candle_high": candle.high,
        })
        vwap_at_close = vwap_by_close.get(candle.timestamp) if vwap_by_close is not None else None
        vwap_breach = (
            config.exit_strategy == "vwap"
            and vwap_at_close is not None
            and vwap_at_close[0] >= entry_start
            and (
                (breakout.direction == "UP" and vwap_at_close[1] < vwap_at_close[2])
                or (breakout.direction == "DOWN" and vwap_at_close[1] > vwap_at_close[2])
            )
        )
        if vwap_breach:
            vwap_exit_value = vwap_at_close[2]
            vwap_exit_signal_time = candle.timestamp
            if candle.open <= effective_stop:
                exit_premium, exit_reason = candle.open, stop_reason
                warnings.append("The option gapped through its active stop on the VWAP-exit candle.")
            elif candle.open >= target:
                exit_premium, exit_reason = target, "TARGET"
                warnings.append("The option opened at/above target on the VWAP-exit candle; target filled.")
            else:
                exit_premium, exit_reason = candle.open, "VWAP_EXIT"
            exit_candle = candle
            break
        hit = _check_exit(candle, effective_stop, target, stop_reason)
        if hit is not None:
            exit_premium, exit_reason, note = hit
            exit_candle = candle
            if note:
                warnings.append(note)
            break
        highest_premium = max(highest_premium, candle.high)
        close_atr = atr_values[candle_index]
        if config.exit_strategy == "atr" and close_atr is not None:
            candidate_stop = highest_premium - close_atr * config.atr_multiplier
            trailing_stop = max(stop, trailing_stop or stop, candidate_stop)
            stop_timeline.append({
                "timestamp": (candle.timestamp + step).isoformat(),
                "event_type": "ATR_TRAIL_RAISED_AFTER_CANDLE_CLOSE",
                "stop_price": round(trailing_stop, 4),
                "running_peak": round(highest_premium, 4),
                "atr": round(close_atr, 4),
                "atr_multiplier": config.atr_multiplier,
            })
    if exit_candle is None:
        return _skip(
            "exit_data_missing",
            (
                "No option candle exists at or after the square-off time and no stop/target was hit; "
                "the trade is unresolved and excluded from P&L."
                if config.exit_on_session_end
                else "No stop/target exit occurred before the available session data ended; the position "
                "is unresolved, no overnight carry is modelled, and it is excluded from P&L."
            ),
            data=True,
        )
    if missing_during_hold:
        warnings.append(
            f"{missing_during_hold} option candle(s) were missing while the position was open; "
            "stop/target touches inside the gaps could not be evaluated."
        )
    if zero_volume:
        warnings.append(f"{zero_volume} held option candle(s) had zero traded volume.")

    gross = round((exit_premium - entry_premium) * quantity, 2)
    completed_holding_candles = [
        candle for candle in candles
        if entry_candle.timestamp < candle.timestamp < exit_candle.timestamp
    ]
    peak_candle = max(completed_holding_candles, key=lambda candle: candle.high, default=None)
    highest_since_entry = max(
        entry_premium,
        peak_candle.high if peak_candle is not None else entry_premium,
    )
    highest_since_entry_time = (
        peak_candle.timestamp
        if peak_candle is not None and peak_candle.high > entry_premium
        else entry_candle.timestamp
    )
    lowest_before_exit = min(
        [entry_premium, *(candle.low for candle in completed_holding_candles)]
    )
    mfe = round(max(0.0, highest_since_entry - entry_premium), 4)
    mae = round(max(0.0, entry_premium - min(lowest_before_exit, exit_premium)), 4)
    costs = compute_costs(entry_premium, exit_premium, quantity, config.costs)
    net = round(gross - costs["total_costs"], 2)
    peak_gross_pnl = round((highest_since_entry - entry_premium) * quantity, 2)
    peak_costs = compute_costs(entry_premium, highest_since_entry, quantity, config.costs)
    peak_net_pnl = round(peak_gross_pnl - peak_costs["total_costs"], 2)
    contract = resolved.contract
    return {
        "skip": False,
        "trade_id": f"{session_date.isoformat()}-{sequence}",
        "session_date": session_date.isoformat(),
        "signal_candle_start": breakout.candle_start.isoformat(),
        "signal_time": breakout.signal_time.isoformat(),
        "signal_direction": breakout.direction,
        "underlying_close_at_signal": breakout.underlying_close,
        "opening_range_high": opening.high,
        "opening_range_low": opening.low,
        "opening_range_start": opening.start.isoformat(),
        "opening_range_end": opening.end.isoformat(),
        "contract": {
            "groww_symbol": contract.groww_symbol,
            "option_type": contract.option_type,
            "strike": contract.strike,
            "expiry": contract.expiry.isoformat(),
            "expiry_rule": resolved.expiry_rule,
            "strike_rule": (
                f"ATM from signal-candle close {breakout.underlying_close:g} "
                f"(step {config.strike_step}, offset {config.strike_offset_steps}); "
                f"selected at {breakout.signal_time:%H:%M} using only data available then."
            ),
            "selected_at": breakout.signal_time.isoformat(),
            "lot_size": config.lot_size,
            "lot_size_source": "user-supplied (unverified)",
        },
        "entry_time": entry_candle.timestamp.isoformat(),
        "entry_premium": entry_premium,
        "max_premium_reached": highest_since_entry,
        "max_premium_time": highest_since_entry_time.isoformat(),
        "highest_premium_between_entry_and_exit": highest_since_entry,
        "highest_premium_between_entry_and_exit_time": highest_since_entry_time.isoformat(),
        "max_premium_basis": (
            "Entry fill or highest high from fully completed holding candles strictly after entry and "
            "strictly before the exit candle. Entry-candle pre-fill prices and exit-candle highs are excluded "
            "because their intrabar path ordering relative to the simulated fill is unknown."
        ),
        "exit_time": exit_candle.timestamp.isoformat(),
        "exit_premium": exit_premium,
        "exit_reason": exit_reason,
        "exit_strategy": config.exit_strategy,
        "exit_level_mode": config.exit_level_mode,
        "stop_loss_input": stop_loss_input,
        "target_input": target_input,
        "stop_premium": round(stop, 4),
        "target_premium": round(target, 4),
        "atr_period": config.atr_period,
        "atr_multiplier": config.atr_multiplier,
        "atr_trailing_stop_premium": round(trailing_stop, 4) if trailing_stop is not None else None,
        "highest_premium_since_entry": round(highest_since_entry, 4),
        "highest_premium_time": highest_since_entry_time.isoformat(),
        "trailing_stop_distance": (
            config.trailing_stop_distance if config.exit_strategy == "trailing_stop" else None
        ),
        "trailing_stop_at_exit": (
            round(trailing_stop, 4) if config.exit_strategy == "trailing_stop" and trailing_stop is not None
            else None
        ),
        "stop_timeline": stop_timeline,
        "maximum_favourable_excursion": mfe,
        "maximum_adverse_excursion": mae,
        "excursion_basis": (
            "Entry candle OHLC and exit-candle extremes are excluded where their intrabar order relative "
            "to entry/exit is unknown; excursion includes entry premium and observed completed holding candles."
        ),
        "vwap_at_exit": round(vwap_exit_value, 4) if vwap_exit_value is not None else None,
        "vwap_exit_signal_time": (
            vwap_exit_signal_time.isoformat() if vwap_exit_signal_time is not None else None
        ),
        "lots": config.lots,
        "lot_size": config.lot_size,
        "quantity": quantity,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": net,
        "hypothetical_peak_exit_gross_pnl": peak_gross_pnl,
        "hypothetical_peak_exit_costs": peak_costs,
        "hypothetical_peak_exit_net_pnl": peak_net_pnl,
        "hypothetical_peak_exit_basis": (
            "Hypothetical exit at the highest premium between entry and exit, using the same quantity and "
            "configured cost model. It is not the realized result and does not model executable bid/ask prices."
        ),
        "net_pnl_basis": "gross minus modeled costs" if costs["configured"] else "no costs configured; net equals gross",
        "warnings": warnings,
    }


def run_session(
    session_date: date,
    underlying_rows: Sequence[HistoricalCandle],
    config: OptionsBacktestConfig,
    resolver: Resolver,
    equity: float,
    *,
    vwap_rows: Sequence[HistoricalCandle] | None = None,
    vwap_symbol: str | None = None,
    comparison_equities: dict[str, float] | None = None,
    run_id: str = "direct",
) -> SessionResult:
    """Replay one session. ``equity`` is the simulated equity available at the session start."""
    validation = validate_session_candles(
        underlying_rows,
        session_date,
        "NIFTY index candles",
        volume_required=False,
        interval_minutes=config.interval_minutes,
    )
    result = SessionResult(
        session_date=session_date,
        status="excluded",
        underlying_candles=len(underlying_rows),
        underlying_missing_candles=validation.missing_count,
    )
    if not validation.usable:
        result.reason_code = "no_data" if validation.no_data else "invalid_underlying_data"
        result.reason = "; ".join(validation.errors)
        return result
    candles = validation.candles
    vwap_by_close: dict[datetime, tuple[datetime, float, float]] | None = None
    if config.exit_strategy == "vwap" or config.compare_vwap_and_trailing:
        if not vwap_symbol or not vwap_rows:
            result.reason_code = "vwap_future_unavailable"
            result.reason = (
                "VWAP exit needs an exact listed NIFTY futures contract and its historical candles "
                "for the selected expiry; Groww did not provide both."
            )
            return result
        vwap_validation = validate_session_candles(
            vwap_rows,
            session_date,
            f"{vwap_symbol} VWAP candles",
            volume_required=True,
            interval_minutes=config.interval_minutes,
        )
        if not vwap_validation.usable:
            result.reason_code = "invalid_vwap_future_data"
            result.reason = "; ".join(vwap_validation.errors)
            return result
        if vwap_validation.missing_count:
            result.reason_code = "vwap_data_incomplete"
            result.reason = (
                f"NIFTY futures VWAP exit requires a complete {config.interval_minutes}-minute session; "
                f"{vwap_validation.missing_count} candle(s) were missing, so the session was not used."
            )
            return result
        vwap_by_close = _session_vwap(vwap_validation.candles, timedelta(minutes=config.interval_minutes))
        if not vwap_by_close:
            result.reason_code = "vwap_volume_unavailable"
            result.reason = "NIFTY futures VWAP exit requires positive cumulative volume; no VWAP could be calculated."
            return result
    opening, why = compute_opening_range(candles, session_date, config)
    if opening is None:
        result.reason_code = "opening_range_unavailable"
        result.reason = why
        return result

    result.warnings.extend(validation.warnings)
    result.opening_range = {
        "high": opening.high,
        "low": opening.low,
        "start": opening.start.isoformat(),
        "end": opening.end.isoformat(),
    }
    result.vwap_symbol = (
        vwap_symbol if config.exit_strategy == "vwap" or config.compare_vwap_and_trailing else None
    )
    cutoff = datetime.combine(session_date, config.entry_cutoff)
    not_before = opening.end
    data_skips = 0
    while len(result.trades) < config.max_trades_per_session:
        breakout = find_breakout(candles, opening, not_before, config)
        if breakout is None:
            break
        option_type = "CE" if breakout.direction == "UP" else "PE"
        strike = target_strike(breakout.underlying_close, breakout.direction, config)
        record = {
            "signal_candle_start": breakout.candle_start.isoformat(),
            "signal_time": breakout.signal_time.isoformat(),
            "direction": breakout.direction,
            "underlying_close": breakout.underlying_close,
            "option_type": option_type,
            "target_strike": strike,
            "outcome": "traded",
            "reason": None,
        }
        result.signals.append(record)
        if breakout.signal_time > cutoff:
            record.update(outcome="skipped_rule", reason="Entry candle would start after the entry cutoff.")
            break
        try:
            resolved = resolver(session_date, option_type, strike, breakout.signal_time)
        except OptionUnavailable as exc:
            record.update(outcome="skipped_data", reason=exc.message, reason_code=exc.code)
            data_skips += 1
            break
        sequence = len(result.trades) + 1
        comparison_outcomes: list[tuple[str, dict[str, Any]]] = []
        if config.compare_vwap_and_trailing:
            distances = list(dict.fromkeys(
                [config.trailing_stop_distance, *config.trailing_stop_comparison_distances]
            ))
            variants = [("VWAP", config.model_copy(update={"exit_strategy": "vwap"}))]
            variants.extend(
                (
                    f"TRAILING_STOP ₹{distance:g}",
                    config.model_copy(update={
                        "exit_strategy": "trailing_stop",
                        "trailing_stop_distance": distance,
                    }),
                )
                for distance in distances
            )
            for variant_name, variant_config in variants:
                variant_outcome = _simulate_trade(
                    session_date=session_date,
                    sequence=sequence,
                    breakout=breakout,
                    opening=opening,
                    option_type=option_type,
                    strike=strike,
                    resolved=resolved,
                    config=variant_config,
                    equity=(comparison_equities or {}).get(variant_name, equity),
                    vwap_by_close=vwap_by_close,
                )
                comparison_outcomes.append((variant_name, variant_outcome))
            primary_name = (
                "VWAP" if config.exit_strategy == "vwap"
                else f"TRAILING_STOP ₹{config.trailing_stop_distance:g}"
            )
            outcome = next(value for name, value in comparison_outcomes if name == primary_name)
            failed_variant = next(
                ((name, value) for name, value in comparison_outcomes if value["skip"]),
                None,
            )
            if failed_variant is not None:
                variant_name, failed_outcome = failed_variant
                outcome = failed_outcome
                record.update(
                    outcome="skipped_data" if failed_outcome["data_driven"] else "skipped_rule",
                    reason=f"Comparison pair excluded: {variant_name}: {failed_outcome['message']}",
                    reason_code=failed_outcome["code"],
                )
                if failed_outcome["data_driven"]:
                    data_skips += 1
                break
            if comparison_equities is not None:
                for variant_name, variant_outcome in comparison_outcomes:
                    comparison_equities[variant_name] = (
                        comparison_equities.get(variant_name, equity) + variant_outcome["net_pnl"]
                    )
        else:
            outcome = _simulate_trade(
                session_date=session_date,
                sequence=sequence,
                breakout=breakout,
                opening=opening,
                option_type=option_type,
                strike=strike,
                resolved=resolved,
                config=config,
                equity=equity,
                vwap_by_close=vwap_by_close,
            )
        if outcome["skip"]:
            kind = "skipped_data" if outcome["data_driven"] else "skipped_rule"
            record.update(outcome=kind, reason=outcome["message"], reason_code=outcome["code"])
            if outcome["data_driven"]:
                data_skips += 1
                break
            if outcome["code"] == "no_entry_volume":
                not_before = breakout.candle_start + timedelta(minutes=config.interval_minutes)
                continue
            break
        false_breakout_time = _false_breakout_close(
            candles,
            opening,
            datetime.fromisoformat(outcome["entry_time"]),
            datetime.fromisoformat(outcome["exit_time"]),
            timedelta(minutes=config.interval_minutes),
        )
        outcome["false_breakout"] = false_breakout_time is not None
        outcome["false_breakout_time"] = (
            false_breakout_time.isoformat() if false_breakout_time is not None else None
        )
        outcome["false_breakout_definition"] = (
            "NIFTY closed back inside the opening range after entry and no later than the simulated exit time."
        )
        if config.compare_vwap_and_trailing:
            for variant_name, variant_outcome in comparison_outcomes:
                variant_outcome["comparison_variant"] = variant_name
                variant_outcome["paired_entry_id"] = outcome["trade_id"]
                variant_false_breakout_time = _false_breakout_close(
                    candles,
                    opening,
                    datetime.fromisoformat(variant_outcome["entry_time"]),
                    datetime.fromisoformat(variant_outcome["exit_time"]),
                    timedelta(minutes=config.interval_minutes),
                )
                variant_outcome["false_breakout"] = variant_false_breakout_time is not None
                variant_outcome["false_breakout_time"] = (
                    variant_false_breakout_time.isoformat()
                    if variant_false_breakout_time is not None else None
                )
                result.comparison_trades.append(variant_outcome)
        result.trades.append(outcome)
        if float(outcome["net_pnl"]) < 0:
            result.loss_reports.append(build_trade_loss_report(
                outcome,
                run_id=run_id,
                config=config,
                underlying_candles=candles,
                option_candles=resolved.candles,
            ))
        equity += outcome["net_pnl"]
        exit_start = max(
            datetime.fromisoformat(value["exit_time"])
            for _, value in comparison_outcomes
        ) if config.compare_vwap_and_trailing else datetime.fromisoformat(outcome["exit_time"])
        not_before = exit_start + timedelta(minutes=config.interval_minutes)

    if data_skips:
        result.warnings.append(
            f"{data_skips} signal(s) could not be simulated because historical data was unavailable "
            "or unusable; results for this session are incomplete."
        )
    result.status = "warnings" if result.warnings else "complete"
    return result


def summarize(
    sessions: Sequence[dict[str, Any]],
    config: OptionsBacktestConfig,
    requested_dates: Sequence[date],
) -> dict[str, Any]:
    """Aggregate metrics strictly from the simulated trades in ``sessions``."""
    trades = [trade for session in sessions for trade in session["trades"]]
    processed = [s for s in sessions if s["status"] in ("complete", "warnings")]
    excluded = [s for s in sessions if s["status"] == "excluded"]
    reasons: dict[str, int] = {}
    for session in excluded:
        reasons[session["reason_code"]] = reasons.get(session["reason_code"], 0) + 1
    signals = [signal for session in sessions for signal in session["signals"]]

    nets = [trade["net_pnl"] for trade in trades]
    gross_total = round(sum(trade["gross_pnl"] for trade in trades), 2)
    net_total = round(sum(nets), 2)
    cost_total = round(sum(trade["costs"]["total_costs"] for trade in trades), 2)
    charges_total = round(sum(trade["costs"]["charges_total"] for trade in trades), 2)
    slippage_total = round(sum(trade["costs"]["slippage"] for trade in trades), 2)
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    exit_reason_counts: dict[str, int] = {}
    for trade in trades:
        reason = str(trade.get("exit_reason", "UNKNOWN"))
        exit_reason_counts[reason] = exit_reason_counts.get(reason, 0) + 1
    consecutive_losses = 0
    current_losses = 0
    for net in nets:
        current_losses = current_losses + 1 if net < 0 else 0
        consecutive_losses = max(consecutive_losses, current_losses)
    favourable_excursions = [
        float(trade["maximum_favourable_excursion"])
        for trade in trades
        if trade.get("maximum_favourable_excursion") is not None
    ]
    adverse_excursions = [
        float(trade["maximum_adverse_excursion"])
        for trade in trades
        if trade.get("maximum_adverse_excursion") is not None
    ]
    givebacks = [
        max(
            0.0,
            (float(trade["highest_premium_since_entry"]) - float(trade["entry_premium"]))
            * int(trade["quantity"])
            - float(trade["gross_pnl"]),
        )
        for trade in trades
        if (
            trade.get("highest_premium_since_entry") is not None
            and trade.get("entry_premium") is not None
            and trade.get("quantity") is not None
        )
    ]
    daily_pnl = []
    for session in sessions:
        session_trades = session["trades"]
        session_nets = [float(trade["net_pnl"]) for trade in session_trades]
        session_wins = sum(1 for pnl in session_nets if pnl > 0)
        session_losses = sum(1 for pnl in session_nets if pnl < 0)
        daily_pnl.append({
            "session_date": session["session_date"],
            "session_status": session["status"],
            "trade_count": len(session_trades),
            "winning_trades": session_wins,
            "losing_trades": session_losses,
            "win_rate_pct": (
                round(session_wins / len(session_trades) * 100, 2)
                if session_trades else None
            ),
            "gross_pnl": round(
                sum(float(trade["gross_pnl"]) for trade in session_trades),
                2,
            ),
            "net_pnl": round(sum(session_nets), 2),
        })

    equity = config.initial_capital_inr
    peak = equity
    max_dd = 0.0
    max_dd_pct = 0.0
    curve = [round(equity, 2)]
    for net in nets:
        equity += net
        peak = max(peak, equity)
        drawdown = peak - equity
        max_dd = max(max_dd, drawdown)
        max_dd_pct = max(max_dd_pct, drawdown / peak * 100 if peak > 0 else 0.0)
        curve.append(round(equity, 2))

    if losses:
        profit_factor = round(sum(wins) / abs(sum(losses)), 4)
        pf_note = "Net winning trades divided by the absolute sum of net losing trades."
    else:
        profit_factor = None
        pf_note = "Undefined because there were no net losing trades."

    data_skipped = sum(1 for s in signals if s["outcome"] == "skipped_data")
    if not processed:
        kind = (
            "no_historical_data"
            if excluded and all(s["reason_code"] == "no_data" for s in excluded)
            else "invalid_or_incomplete_data"
        )
    elif not trades:
        kind = "zero_trades"
    else:
        kind = "trades"

    return {
        "result_kind": kind,
        "requested_start": config.start_date.isoformat(),
        "requested_end": config.end_date.isoformat(),
        "processed_start": processed[0]["session_date"] if processed else None,
        "processed_end": processed[-1]["session_date"] if processed else None,
        "sessions_requested": len(requested_dates),
        "daily_pnl": daily_pnl,
        "sessions_evaluated": len(sessions),
        "sessions_processed": len(processed),
        "sessions_complete": sum(1 for s in sessions if s["status"] == "complete"),
        "sessions_with_warnings": sum(1 for s in sessions if s["status"] == "warnings"),
        "sessions_excluded": len(excluded),
        "excluded_reasons": reasons,
        "signals_total": len(signals),
        "signals_skipped_data": data_skipped,
        "signals_skipped_rule": sum(1 for s in signals if s["outcome"] == "skipped_rule"),
        "trade_count": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "maximum_consecutive_losses": consecutive_losses,
        "breakeven_trades": len(nets) - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "gross_pnl": gross_total,
        "net_pnl": net_total,
        "total_costs": cost_total,
        "total_charges": charges_total,
        "total_slippage": slippage_total,
        "costs_configured": config.costs.configured,
        "net_pnl_basis": (
            "Gross minus the configured charges and slippage."
            if config.costs.configured
            else "No costs were configured, so net P&L equals gross P&L and is not net of real charges."
        ),
        "max_drawdown_inr": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 4),
        "max_drawdown_method": (
            "Largest peak-to-trough fall of simulated equity (initial capital plus cumulative "
            "net P&L), sampled after each closed trade; intra-trade excursions are not included."
        ),
        "profit_factor": profit_factor,
        "profit_factor_note": pf_note,
        "average_trade_pnl": round(net_total / len(trades), 2) if trades else None,
        "average_profit": round(sum(wins) / len(wins), 2) if wins else None,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "exit_reason_breakdown": exit_reason_counts,
        "average_favourable_excursion": (
            round(sum(favourable_excursions) / len(favourable_excursions), 4)
            if favourable_excursions else None
        ),
        "maximum_favourable_excursion": round(max(favourable_excursions), 4) if favourable_excursions else None,
        "average_adverse_excursion": (
            round(sum(adverse_excursions) / len(adverse_excursions), 4)
            if adverse_excursions else None
        ),
        "maximum_adverse_excursion": round(max(adverse_excursions), 4) if adverse_excursions else None,
        "average_profit_giveback_from_peak": (
            round(sum(givebacks) / len(givebacks), 2) if givebacks else None
        ),
        "maximum_profit_giveback_from_peak": round(max(givebacks), 2) if givebacks else None,
        "initial_equity": round(config.initial_capital_inr, 2),
        "ending_equity": round(config.initial_capital_inr + net_total, 2),
        "equity_curve": curve,
        "results_may_be_incomplete": bool(excluded or data_skipped),
    }
