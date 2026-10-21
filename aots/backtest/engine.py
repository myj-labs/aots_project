from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as local_time, timedelta
from math import isfinite
from typing import Any, Sequence

from .data import BacktestConfig, Bar, KOLKATA, ValidatedData


OPENING_RANGE_START = local_time(9, 15)
OPENING_RANGE_END = local_time(9, 45)


class UnsupportedFilterError(ValueError):
    """Raised when a requested filter is not supported by defensible input data."""


@dataclass
class Signal:
    session_date: str
    timestamp: str
    decision_timestamp: str
    direction: str
    opening_range_high: float
    opening_range_low: float
    opening_range_width: float
    close_price: float
    active_filters: list[str]
    accepted: bool
    reason: str
    entry_timestamp: str | None = None
    entry_price: float | None = None
    filters_passed: bool = False
    entry_attempted: bool = False


@dataclass
class Trade:
    session_date: str
    signal_timestamp: str
    entry_timestamp: str
    direction: str
    opening_range_high: float
    opening_range_low: float
    opening_range_width: float
    entry_price: float
    stop_price: float
    target_price: float
    exit_timestamp: str
    exit_price: float
    exit_reason: str
    gross_pnl_points: float
    initial_risk_points: float
    realized_r_multiple: float
    maximum_favourable_excursion_points: float
    maximum_adverse_excursion_points: float
    execution_adjustment: str


@dataclass
class BacktestResult:
    signals: list[Signal]
    trades: list[Trade]
    metrics: dict[str, Any]
    eligible_sessions: int
    rejected_sessions: list[dict[str, str]]


def _time_at(bar: Bar) -> local_time:
    return bar.timestamp.astimezone(KOLKATA).time().replace(tzinfo=None)


def _timestamp(bar: Bar) -> str:
    return bar.timestamp.astimezone(KOLKATA).isoformat()


def _breakout_events(
    bars: Sequence[Bar],
    opening_high: float,
    opening_low: float,
) -> list[tuple[int, str]]:
    events = []
    previous_direction = None
    for index, bar in enumerate(bars):
        if _time_at(bar) < OPENING_RANGE_END:
            continue
        direction = (
            "LONG" if bar.close > opening_high
            else "SHORT" if bar.close < opening_low
            else None
        )
        if direction is not None and direction != previous_direction:
            events.append((index, direction))
        previous_direction = direction
    return events


def _true_range(current: Bar, previous_close: float | None) -> float:
    if previous_close is None:
        return current.high - current.low
    return max(
        current.high - current.low,
        abs(current.high - previous_close),
        abs(current.low - previous_close),
    )


def _ema_values(bars: Sequence[Bar], period: int = 50) -> list[float | None]:
    values: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return values
    seed = sum(bar.close for bar in bars[:period]) / period
    values[period - 1] = seed
    alpha = 2 / (period + 1)
    previous = seed
    for index in range(period, len(bars)):
        previous = alpha * bars[index].close + (1 - alpha) * previous
        values[index] = previous
    return values


def _atr_values(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    true_ranges = [
        _true_range(bar, bars[index - 1].close if index else None)
        for index, bar in enumerate(bars)
    ]
    values: list[float | None] = [None] * len(bars)
    for index in range(period - 1, len(bars)):
        values[index] = sum(true_ranges[index - period + 1:index + 1]) / period
    return values


def _signal_filter_reasons(
    direction: str,
    close: float,
    opening_width: float,
    index: int,
    ema_values: Sequence[float | None],
    atr_values: Sequence[float | None],
    config: BacktestConfig,
) -> tuple[list[str], list[str]]:
    active = []
    rejected = []
    if config.ema50_filter:
        active.append("EMA(50), SMA seed from first 50 completed 5-minute closes")
        ema = ema_values[index]
        if ema is None:
            rejected.append("EMA(50) unavailable: fewer than 50 completed bars")
        elif direction == "LONG" and close <= ema:
            rejected.append("EMA(50) alignment failed for bullish breakout")
        elif direction == "SHORT" and close >= ema:
            rejected.append("EMA(50) alignment failed for bearish breakout")
    if config.opening_range_atr_max_ratio is not None:
        active.append(
            f"opening-range width / trailing ATR(14) <= {config.opening_range_atr_max_ratio:g}"
        )
        atr = atr_values[index]
        if atr is None or atr <= 0:
            rejected.append("ATR(14) unavailable or non-positive at the signal time")
        elif opening_width / atr > config.opening_range_atr_max_ratio:
            rejected.append("opening-range width exceeded the configured ATR(14) ratio")
    return active, rejected


def run_backtest(data: ValidatedData, config: BacktestConfig) -> BacktestResult:
    if not data.quality.get("passed"):
        raise ValueError("Backtest requires data that passed the validation gate.")
    if not data.bars:
        raise ValueError("Backtest requires validated OHLC bars.")
    if not all(bar.timestamp.tzinfo is not None for bar in data.bars):
        raise ValueError("Backtest timestamps must be timezone-aware.")

    # The ^NSEI index is not a traded instrument; its volume is not treated as executed quantity.
    if config.vwap_filter or config.breakout_volume_filter:
        raise UnsupportedFilterError(
            "VWAP and breakout-volume filters are unavailable for ^NSEI because index volume "
            "has not been validated as meaningful traded-instrument volume."
        )

    bars = list(data.bars)
    session_indices: dict[date, list[int]] = defaultdict(list)
    for index, bar in enumerate(bars):
        session_indices[bar.timestamp.astimezone(KOLKATA).date()].append(index)
    ema = _ema_values(bars)
    atr = _atr_values(bars)
    signals: list[Signal] = []
    trades: list[Trade] = []
    rejected_sessions: list[dict[str, str]] = []

    for session_date, indices in sorted(session_indices.items()):
        session_bars = [bars[index] for index in indices]
        opening_bars = [bar for bar in session_bars if OPENING_RANGE_START <= _time_at(bar) < OPENING_RANGE_END]
        if len(opening_bars) != 6:
            rejected_sessions.append({
                "date": session_date.isoformat(),
                "reason": "Opening range requires six complete 5-minute candles.",
            })
            continue
        opening_high = max(bar.high for bar in opening_bars)
        opening_low = min(bar.low for bar in opening_bars)
        opening_width = opening_high - opening_low
        if not isfinite(opening_width) or opening_width <= 0:
            rejected_sessions.append({
                "date": session_date.isoformat(),
                "reason": "Opening-range width is zero or invalid.",
            })
            continue

        breakout_events = dict(_breakout_events(
            session_bars,
            opening_high,
            opening_low,
        ))
        session_trade_count = 0
        active_position: dict[str, Any] | None = None
        session_trades: list[Trade] = []
        for local_index, global_index in enumerate(indices):
            bar = bars[global_index]
            bar_time = _time_at(bar)
            position_exited_on_bar = False
            if bar_time < OPENING_RANGE_END:
                continue

            if active_position is not None:
                if bar_time >= config.time_exit:
                    _finish_trade(
                        active_position,
                        bar,
                        bar.open,
                        "TIME_EXIT",
                        "time exit at 15:15 candle open",
                        session_trades,
                    )
                    active_position = None
                    session_trade_count += 1
                    position_exited_on_bar = True
                else:
                    exit_values = _check_exit(active_position, bar)
                    if exit_values is not None:
                        price, reason, adjustment = exit_values
                        _finish_trade(
                            active_position, bar, price, reason, adjustment, session_trades
                        )
                        active_position = None
                        session_trade_count += 1
                        position_exited_on_bar = True
                    else:
                        _update_excursions(active_position, bar)

            direction = breakout_events.get(local_index)
            if direction is None:
                continue
            next_local_index = local_index + 1
            next_bar = session_bars[next_local_index] if next_local_index < len(session_bars) else None
            decision_time = bar.timestamp.astimezone(KOLKATA) + timedelta(minutes=5)
            active_filters, filter_reasons = _signal_filter_reasons(
                direction,
                bar.close,
                opening_width,
                global_index,
                ema,
                atr,
                config,
            )
            rejection_reasons = list(filter_reasons)
            if position_exited_on_bar:
                rejection_reasons.append(
                    "position exited on this candle; entry deferred because intrabar ordering is unknown"
                )
            if active_position is not None:
                rejection_reasons.append("another position is already open")
            if session_trade_count >= config.max_trades_per_session:
                rejection_reasons.append("maximum completed trades per session reached")
            if next_bar is None:
                rejection_reasons.append("no next candle is available for entry")
            elif _time_at(next_bar) > config.entry_cutoff:
                rejection_reasons.append("next candle opens after the entry cutoff")
            elif _time_at(next_bar) >= config.time_exit:
                rejection_reasons.append("next candle opens at or after the time exit")

            entry_price = next_bar.open if next_bar is not None else None
            stop_price = opening_low if direction == "LONG" else opening_high
            if entry_price is not None and (
                (direction == "LONG" and entry_price <= stop_price)
                or (direction == "SHORT" and entry_price >= stop_price)
            ):
                rejection_reasons.append("next-candle gap makes the opening-range stop unusable")
            signal = Signal(
                session_date=session_date.isoformat(),
                timestamp=_timestamp(bar),
                decision_timestamp=decision_time.isoformat(),
                direction=direction,
                opening_range_high=opening_high,
                opening_range_low=opening_low,
                opening_range_width=opening_width,
                close_price=bar.close,
                active_filters=active_filters or ["baseline breakout; no optional filters"],
                accepted=not rejection_reasons,
                reason="accepted" if not rejection_reasons else "; ".join(rejection_reasons),
                entry_timestamp=_timestamp(next_bar) if next_bar is not None else None,
                entry_price=entry_price,
                filters_passed=not filter_reasons,
            )
            signals.append(signal)
            if not signal.accepted or next_bar is None:
                continue
            risk_points = abs(entry_price - stop_price)
            if not isfinite(risk_points) or risk_points <= 0:
                signal.accepted = False
                signal.reason = "entry-to-stop risk is zero or invalid"
                continue
            target_price = (
                entry_price + 2 * risk_points if direction == "LONG"
                else entry_price - 2 * risk_points
            )
            if target_price <= 0 or not isfinite(target_price):
                signal.accepted = False
                signal.reason = "2R target is zero or invalid at the next-candle entry price"
                continue
            signal.entry_attempted = True
            active_position = {
                "signal": signal,
                "session_date": session_date.isoformat(),
                "entry_timestamp": _timestamp(next_bar),
                "direction": direction,
                "opening_range_high": opening_high,
                "opening_range_low": opening_low,
                "opening_range_width": opening_width,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "target_price": target_price,
                "risk_points": risk_points,
                "mfe": 0.0,
                "mae": 0.0,
                "execution_adjustment": "entry at next 5-minute candle open",
            }
        if active_position is not None:
            rejected_sessions.append({
                "date": session_date.isoformat(),
                "reason": "Open position had no valid 15:15 time-exit candle; session excluded.",
            })
            signals = [signal for signal in signals if signal.session_date != session_date.isoformat()]
            trades = [trade for trade in trades if trade.session_date != session_date.isoformat()]
            continue
        trades.extend(session_trades)

    validation_rejections = list(data.quality.get("rejected_sessions", []))
    all_rejections = validation_rejections + rejected_sessions
    rejected_dates = {item["date"] for item in all_rejections}
    eligible_session_dates = [
        session_date.isoformat()
        for session_date in sorted(session_indices)
        if session_date.isoformat() not in rejected_dates
    ]
    metrics = calculate_metrics(
        trades,
        len(eligible_session_dates),
        all_rejections,
        eligible_session_dates,
    )
    metrics["eligible_sessions"] = max(
        0,
        int(data.quality.get("observed_session_count", len(session_indices)))
        - len(all_rejections),
    )
    metrics["sessions_rejected_for_data_quality"] = len(all_rejections)
    metrics["rejected_session_details"] = all_rejections
    metrics["raw_breakout_events"] = len(signals)
    metrics["total_breakout_signals"] = len(signals)
    metrics["signals_rejected"] = sum(not signal.accepted for signal in signals)
    metrics["signals_accepted"] = sum(signal.accepted for signal in signals)
    metrics["filter_passing_events"] = sum(signal.filters_passed for signal in signals)
    metrics["filter_rejected_events"] = sum(not signal.filters_passed for signal in signals)
    metrics["entry_attempts"] = sum(signal.entry_attempted for signal in signals)
    metrics["entry_rejected_events"] = sum(
        signal.filters_passed and not signal.entry_attempted for signal in signals
    )
    reason_counts: dict[str, int] = defaultdict(int)
    for signal in signals:
        for reason in signal.reason.split("; "):
            if reason != "accepted":
                reason_counts[reason] += 1
    metrics["rejection_reason_counts"] = dict(sorted(reason_counts.items()))
    metrics["market_condition_breakdown"] = {
        "status": "not classified",
        "reason": "No pre-registered, defensible market-regime labels are included in this dataset.",
    }
    metrics["research_validation"] = _research_validation(trades, metrics["eligible_sessions"])
    return BacktestResult(
        signals=signals,
        trades=trades,
        metrics=metrics,
        eligible_sessions=metrics["eligible_sessions"],
        rejected_sessions=all_rejections,
    )


def _check_exit(position: dict[str, Any], bar: Bar) -> tuple[float, str, str] | None:
    direction = position["direction"]
    stop = position["stop_price"]
    target = position["target_price"]
    if direction == "LONG":
        if bar.open <= stop:
            return bar.open, "STOP", "stop gap filled at candle open, worse than stop"
        if bar.open >= target:
            return target, "TARGET", "target filled at target; no price improvement modeled"
        touched_stop = bar.low <= stop
        touched_target = bar.high >= target
        if touched_stop:
            reason = "stop and target touched in same candle; conservative stop-first rule" if touched_target else "stop touched"
            return stop, "STOP", reason
        if touched_target:
            return target, "TARGET", "target touched"
    else:
        if bar.open >= stop:
            return bar.open, "STOP", "stop gap filled at candle open, worse than stop"
        if bar.open <= target:
            return target, "TARGET", "target filled at target; no price improvement modeled"
        touched_stop = bar.high >= stop
        touched_target = bar.low <= target
        if touched_stop:
            reason = "stop and target touched in same candle; conservative stop-first rule" if touched_target else "stop touched"
            return stop, "STOP", reason
        if touched_target:
            return target, "TARGET", "target touched"
    return None


def _update_excursions(position: dict[str, Any], bar: Bar) -> None:
    entry = position["entry_price"]
    if position["direction"] == "LONG":
        position["mfe"] = max(position["mfe"], bar.high - entry)
        position["mae"] = max(position["mae"], entry - bar.low)
    else:
        position["mfe"] = max(position["mfe"], entry - bar.low)
        position["mae"] = max(position["mae"], bar.high - entry)


def _finish_trade(
    position: dict[str, Any],
    bar: Bar,
    exit_price: float,
    reason: str,
    adjustment: str,
    output: list[Trade],
) -> None:
    sign = 1 if position["direction"] == "LONG" else -1
    pnl = (exit_price - position["entry_price"]) * sign
    output.append(Trade(
        session_date=position["session_date"],
        signal_timestamp=position["signal"].decision_timestamp,
        entry_timestamp=position["entry_timestamp"],
        direction=position["direction"],
        opening_range_high=position["opening_range_high"],
        opening_range_low=position["opening_range_low"],
        opening_range_width=position["opening_range_width"],
        entry_price=position["entry_price"],
        stop_price=position["stop_price"],
        target_price=position["target_price"],
        exit_timestamp=_timestamp(bar),
        exit_price=exit_price,
        exit_reason=reason,
        gross_pnl_points=pnl,
        initial_risk_points=position["risk_points"],
        realized_r_multiple=pnl / position["risk_points"],
        maximum_favourable_excursion_points=position["mfe"],
        maximum_adverse_excursion_points=position["mae"],
        execution_adjustment=adjustment,
    ))


def calculate_metrics(
    trades: Sequence[Trade],
    eligible_sessions: int,
    rejected_sessions: Sequence[dict[str, str]],
    eligible_session_dates: Sequence[str] = (),
) -> dict[str, Any]:
    pnls = [trade.gross_pnl_points for trade in trades]
    wins = [value for value in pnls if value > 0]
    losses = [value for value in pnls if value < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnls:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    def streak(positive: bool) -> int:
        longest = current = 0
        for value in pnls:
            if (value > 0) == positive and value != 0:
                current += 1
                longest = max(longest, current)
            else:
                current = 0
        return longest

    grouped: dict[str, dict[str, Any]] = {}
    for session_date in eligible_session_dates:
        grouped[session_date] = {
            "trades": 0,
            "gross_pnl_points": 0.0,
            "directions": [],
        }
    for trade in trades:
        row = grouped.setdefault(
            trade.session_date,
            {"trades": 0, "gross_pnl_points": 0.0, "directions": []},
        )
        row["trades"] += 1
        row["gross_pnl_points"] += trade.gross_pnl_points
        row["directions"].append(trade.direction)
    by_direction = {}
    for direction in ("LONG", "SHORT"):
        selected = [trade for trade in trades if trade.direction == direction]
        by_direction[direction.lower()] = {
            "trades": len(selected),
            "gross_pnl_points": sum(trade.gross_pnl_points for trade in selected),
            "wins": sum(trade.gross_pnl_points > 0 for trade in selected),
            "losses": sum(trade.gross_pnl_points < 0 for trade in selected),
        }
    gross_profit = sum(wins)
    gross_loss_abs = abs(sum(losses))
    return {
        "result_basis": "Underlying NIFTY 50 index points; not option returns or rupees.",
        "cost_assumption": "Zero trading costs, taxes, slippage, and multiplier; gross equals net under this assumption.",
        "eligible_sessions": eligible_sessions - len(rejected_sessions),
        "sessions_rejected_for_data_quality": len(rejected_sessions),
        "rejected_session_details": list(rejected_sessions),
        "total_breakout_signals": len([]),  # Replaced by run_backtest after signal generation.
        "raw_breakout_events": len([]),  # Replaced by run_backtest after event generation.
        "signals_rejected": 0,  # Replaced by run_backtest after signal generation.
        "completed_trades": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": sum(value == 0 for value in pnls),
        "win_rate": len(wins) / len(trades) if trades else None,
        "gross_underlying_points": sum(pnls),
        "net_underlying_points": sum(pnls),
        "average_trade_points": sum(pnls) / len(pnls) if pnls else None,
        "average_win_points": sum(wins) / len(wins) if wins else None,
        "average_loss_points": sum(losses) / len(losses) if losses else None,
        "profit_factor": gross_profit / gross_loss_abs if gross_loss_abs else None,
        "profit_factor_defined": gross_loss_abs > 0,
        "average_realized_r_multiple": (
            sum(trade.realized_r_multiple for trade in trades) / len(trades) if trades else None
        ),
        "maximum_drawdown_points": max_drawdown,
        "drawdown_convention": "Trade-close cumulative underlying points, initial equity 0, running peak includes initial 0.",
        "largest_win_points": max(wins) if wins else None,
        "largest_loss_points": min(losses) if losses else None,
        "maximum_consecutive_wins": streak(True),
        "maximum_consecutive_losses": streak(False),
        "by_direction": by_direction,
        "by_session": grouped,
        "filter_passing_events": 0,
        "filter_rejected_events": 0,
        "entry_attempts": 0,
        "entry_rejected_events": 0,
        "rejection_reason_counts": {},
        "options_backtest_performed": False,
        "options_data_missing": [
            "historical NIFTY option contract identifiers and expiries",
            "historical strike/contract mapping at each signal timestamp",
            "historical option OHLC or trades/quotes including bid-ask spreads",
            "contract lot sizes and applicable costs",
        ],
    }


def _research_validation(trades: Sequence[Trade], sessions: int) -> dict[str, Any]:
    if sessions < 10 or len(trades) < 30:
        return {
            "verdict": "inconclusive",
            "development_trades": None,
            "out_of_sample_trades": None,
            "reason": "Fewer than 10 sessions or 30 trades; no credible chronological out-of-sample comparison.",
            "minimums": {"sessions": 10, "trades": 30},
        }
    ordered = sorted(trades, key=lambda trade: (trade.session_date, trade.entry_timestamp))
    session_dates = sorted({trade.session_date for trade in ordered})
    split_date_index = max(1, int(len(session_dates) * 0.7))
    split_date = session_dates[split_date_index]
    development = [trade for trade in ordered if trade.session_date < split_date]
    out_of_sample = [trade for trade in ordered if trade.session_date >= split_date]
    oos_values = [trade.gross_pnl_points for trade in out_of_sample]
    oos_gains = sum(value for value in oos_values if value > 0)
    oos_losses = abs(sum(value for value in oos_values if value < 0))
    oos_profit_factor = oos_gains / oos_losses if oos_losses else None
    if len(out_of_sample) < 10:
        verdict = "inconclusive"
        reason = "The chronological out-of-sample segment has fewer than 10 trades."
    elif sum(oos_values) <= 0 or (oos_profit_factor is not None and oos_profit_factor <= 1):
        verdict = "rejected"
        reason = "Out-of-sample gross result is non-positive or profit factor is not above 1."
    elif oos_profit_factor is None:
        verdict = "inconclusive"
        reason = "Out-of-sample profit factor is undefined because there are no losing trades."
    else:
        verdict = "promising"
        reason = "Out-of-sample gross result is positive and gross profit factor exceeds 1; not a profitability claim."
    return {
        "verdict": verdict,
        "development_trades": len(development),
        "out_of_sample_trades": len(out_of_sample),
        "split_session_date": split_date,
        "development_gross_points": sum(trade.gross_pnl_points for trade in development),
        "out_of_sample_gross_points": sum(oos_values),
        "out_of_sample_profit_factor": oos_profit_factor,
        "reason": (
            f"{reason} Chronological 70/30 split, fixed parameters, no transaction costs; "
            "this research result is not evidence of future performance."
        ),
    }


def result_as_dict(result: BacktestResult) -> dict[str, Any]:
    return {
        "signals": [asdict(signal) for signal in result.signals],
        "trades": [asdict(trade) for trade in result.trades],
        "metrics": result.metrics,
    }
