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
    signals: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
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
            "signals": list(self.signals),
            "trades": list(self.trades),
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


def _check_exit(candle: HistoricalCandle, stop: float, target: float):
    if candle.open <= stop:
        return candle.open, "STOP_LOSS", "Stop gapped through; filled at the candle open."
    touched_stop = candle.low <= stop
    touched_target = candle.high >= target
    if touched_stop:
        note = (
            "Stop and target were both touched in one candle; stop-first assumed."
            if touched_target
            else None
        )
        return stop, "STOP_LOSS", note
    if candle.open >= target:
        return target, "TARGET", "Target filled at the target level; no price improvement modelled."
    if touched_target:
        return target, "TARGET", None
    return None


def _skip(code: str, message: str, *, data: bool) -> dict[str, Any]:
    return {"skip": True, "code": code, "message": message, "data_driven": data}


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
) -> dict[str, Any]:
    entry_start = breakout.signal_time
    step = timedelta(minutes=config.interval_minutes)
    candles = [candle for candle in resolved.candles if candle.timestamp >= entry_start]
    if not candles or candles[0].timestamp != entry_start:
        return _skip(
            "entry_candle_missing",
            f"No option premium candle starts at {entry_start:%H:%M}; the entry was not simulated.",
            data=True,
        )
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
    square_off = datetime.combine(session_date, config.square_off_time)

    warnings = list(resolved.warnings) + [LOT_SIZE_WARNING]
    previous: HistoricalCandle | None = None
    missing_during_hold = 0
    zero_volume = 0
    exit_candle: HistoricalCandle | None = None
    exit_premium = 0.0
    exit_reason = ""
    held_candles: list[HistoricalCandle] = []
    for candle in candles:
        held_candles.append(candle)
        if previous is not None:
            missing_during_hold += max(0, int((candle.timestamp - previous.timestamp) / step) - 1)
        previous = candle
        if candle.volume <= 0:
            zero_volume += 1
        if candle.timestamp >= square_off:
            exit_candle, exit_premium, exit_reason = candle, candle.open, "TIME_EXIT"
            delay = int((candle.timestamp - square_off) / step)
            if delay:
                warnings.append(
                    f"Time exit executed {delay * config.interval_minutes} minute(s) after the configured "
                    "square-off because those candles are missing."
                )
            break
        hit = _check_exit(candle, stop, target)
        if hit is not None:
            exit_premium, exit_reason, note = hit
            exit_candle = candle
            if note:
                warnings.append(note)
            break
    if exit_candle is None:
        return _skip(
            "exit_data_missing",
            "No option candle exists at or after the square-off time and no stop/target was hit; "
            "the trade is unresolved and excluded from P&L.",
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
    max_premium_candle = max(held_candles, key=lambda candle: candle.high)
    costs = compute_costs(entry_premium, exit_premium, quantity, config.costs)
    net = round(gross - costs["total_costs"], 2)
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
        "max_premium_reached": max_premium_candle.high,
        "max_premium_time": max_premium_candle.timestamp.isoformat(),
        "max_premium_basis": (
            "highest candle high from entry through exit, inclusive; intrabar path ordering is unknown"
        ),
        "exit_time": exit_candle.timestamp.isoformat(),
        "exit_premium": exit_premium,
        "exit_reason": exit_reason,
        "exit_level_mode": config.exit_level_mode,
        "stop_loss_input": stop_loss_input,
        "target_input": target_input,
        "stop_premium": round(stop, 4),
        "target_premium": round(target, 4),
        "lots": config.lots,
        "lot_size": config.lot_size,
        "quantity": quantity,
        "gross_pnl": gross,
        "costs": costs,
        "net_pnl": net,
        "net_pnl_basis": "gross minus modeled costs" if costs["configured"] else "no costs configured; net equals gross",
        "warnings": warnings,
    }


def run_session(
    session_date: date,
    underlying_rows: Sequence[HistoricalCandle],
    config: OptionsBacktestConfig,
    resolver: Resolver,
    equity: float,
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
        outcome = _simulate_trade(
            session_date=session_date,
            sequence=len(result.trades) + 1,
            breakout=breakout,
            opening=opening,
            option_type=option_type,
            strike=strike,
            resolved=resolved,
            config=config,
            equity=equity,
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
        result.trades.append(outcome)
        equity += outcome["net_pnl"]
        exit_start = datetime.fromisoformat(outcome["exit_time"])
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
        "initial_equity": round(config.initial_capital_inr, 2),
        "ending_equity": round(config.initial_capital_inr + net_total, 2),
        "equity_curve": curve,
        "results_may_be_incomplete": bool(excluded or data_skipped),
    }
