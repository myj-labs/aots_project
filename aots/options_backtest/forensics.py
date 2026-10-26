"""Evidence-grounded diagnostics for completed losing options-backtest trades."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Sequence

from ..brokers.base import HistoricalCandle
from .config import OptionsBacktestConfig

ANALYSIS_VERSION = "1"


def _stamp(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _timeline_row(
    timestamp: datetime,
    event: str,
    **facts: Any,
) -> dict[str, Any]:
    return {"timestamp": timestamp.isoformat(), "event": event, **facts}


def _add_finding(
    findings: list[dict[str, str]],
    code: str,
    confidence: str,
    evidence: str,
) -> None:
    findings.append({
        "code": code,
        "confidence": confidence,
        "evidence": evidence,
    })


def build_trade_loss_report(
    trade: dict[str, Any],
    *,
    run_id: str,
    config: OptionsBacktestConfig,
    underlying_candles: Sequence[HistoricalCandle],
    option_candles: Sequence[HistoricalCandle],
) -> dict[str, Any]:
    """Build one report using only records already used by the completed simulation."""
    if float(trade["net_pnl"]) >= 0:
        raise ValueError("Forensic loss reports require a completed trade with negative net P&L.")

    entry_time = _stamp(trade["entry_time"])
    exit_time = _stamp(trade["exit_time"])
    signal_time = _stamp(trade["signal_time"])
    if entry_time is None or exit_time is None or signal_time is None:
        raise ValueError("Completed trades must contain entry, exit, and signal timestamps.")

    contract = trade["contract"]
    quantity = int(trade["quantity"])
    entry_premium = float(trade["entry_premium"])
    exit_premium = float(trade["exit_premium"])
    peak = float(trade["highest_premium_between_entry_and_exit"])
    peak_time = _stamp(trade.get("highest_premium_between_entry_and_exit_time"))
    gross = float(trade["gross_pnl"])
    net = float(trade["net_pnl"])
    best_unrealized = round(max(0.0, peak - entry_premium) * quantity, 2)
    giveback = round(max(0.0, best_unrealized - gross), 2)
    holding_minutes = round((exit_time - entry_time).total_seconds() / 60, 2)
    option_rows = [
        candle for candle in option_candles
        if entry_time <= candle.timestamp <= exit_time
    ]
    signal_candle_start = datetime.fromisoformat(trade["signal_candle_start"])
    index_rows = [
        candle for candle in underlying_candles
        if signal_candle_start <= candle.timestamp
        and candle.timestamp + timedelta(minutes=config.interval_minutes) <= exit_time
    ]
    signal_candle = next(
        (candle for candle in underlying_candles
         if candle.timestamp.isoformat() == trade["signal_candle_start"]),
        None,
    )
    step_minutes = config.interval_minutes
    range_start = datetime.fromisoformat(trade["opening_range_start"])
    range_end = datetime.fromisoformat(trade["opening_range_end"])

    range_rows = [
        candle for candle in underlying_candles
        if range_start <= candle.timestamp < range_end
    ]
    range_recomputed_high = max((candle.high for candle in range_rows), default=None)
    range_recomputed_low = min((candle.low for candle in range_rows), default=None)
    range_valid = (
        len(range_rows) == config.opening_range_candles
        and range_recomputed_high == float(trade["opening_range_high"])
        and range_recomputed_low == float(trade["opening_range_low"])
    )

    timeline: list[dict[str, Any]] = [
        _timeline_row(
            range_start,
            "ORB_RANGE_START",
            range_high=trade["opening_range_high"],
            range_low=trade["opening_range_low"],
        ),
        _timeline_row(
            range_end,
            "ORB_RANGE_COMPLETE",
            range_high=trade["opening_range_high"],
            range_low=trade["opening_range_low"],
            candles_used=len(range_rows),
            recomputed_high=range_recomputed_high,
            recomputed_low=range_recomputed_low,
            calculation_matches=range_valid,
        ),
        _timeline_row(
            signal_time,
            "ORB_BREAKOUT_SIGNAL",
            direction=trade["signal_direction"],
            signal_candle_start=trade["signal_candle_start"],
            underlying_close=trade["underlying_close_at_signal"],
            range_high=trade["opening_range_high"],
            range_low=trade["opening_range_low"],
            condition=(
                "completed NIFTY close crossed above OR high"
                if trade["signal_direction"] == "UP"
                else "completed NIFTY close crossed below OR low"
            ),
        ),
        _timeline_row(
            entry_time,
            "SIMULATED_ENTRY_FILL",
            order_timestamp=signal_time.isoformat(),
            fill_timestamp=entry_time.isoformat(),
            fill_price=entry_premium,
        ),
    ]

    for candle in index_rows:
        timeline.append(_timeline_row(
            candle.timestamp,
            "NIFTY_CASH_CANDLE",
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            volume_availability="unavailable" if candle.volume is None else "reported",
        ))
    for candle in option_rows:
        timeline.append(_timeline_row(
            candle.timestamp,
            "OPTION_CANDLE",
            open=candle.open,
            high=candle.high,
            low=candle.low,
            close=candle.close,
            volume=candle.volume,
            open_interest=candle.open_interest,
        ))
    for stop_event in trade.get("stop_timeline", []):
        event_time = _stamp(stop_event.get("timestamp"))
        if event_time is not None:
            timeline.append({
                **stop_event,
                "timestamp": event_time.isoformat(),
                "event": "STOP_STATE",
            })
    exit_signal_time = trade.get("vwap_exit_signal_time")
    if exit_signal_time:
        timeline.append(_timeline_row(
            _stamp(exit_signal_time),
            "VWAP_EXIT_SIGNAL",
            signal="VWAP condition confirmed by completed underlying candle",
            exit_fill_timestamp=exit_time.isoformat(),
        ))
    if trade["exit_reason"] in {"STOP_LOSS", "ATR_TRAILING_STOP", "TRAILING_STOP"}:
        timeline.append(_timeline_row(
            exit_time,
            "STOP_TRIGGER_AND_SIMULATED_FILL",
            exit_reason=trade["exit_reason"],
            fill_price=exit_premium,
            trigger_timestamp_precision="candle interval only; sub-candle time unavailable",
        ))
    else:
        timeline.append(_timeline_row(
            exit_time,
            "SIMULATED_EXIT_FILL",
            exit_reason=trade["exit_reason"],
            fill_price=exit_premium,
            signal_timestamp=exit_signal_time or exit_time.isoformat(),
        ))
    timeline.sort(key=lambda row: (row["timestamp"], row["event"]))

    findings: list[dict[str, str]] = []
    if trade.get("false_breakout"):
        _add_finding(
            findings,
            "FALSE_BREAKOUT",
            "confirmed from recorded data",
            f"NIFTY closed back inside the recorded opening range at {trade.get('false_breakout_time')}.",
        )
    underlying_at_end = index_rows[-1].close if index_rows else None
    underlying_change = (
        float(underlying_at_end) - float(trade["underlying_close_at_signal"])
        if underlying_at_end is not None else None
    )
    moved_against = (
        underlying_change < 0 if trade["signal_direction"] == "UP"
        else underlying_change > 0
    ) if underlying_change is not None else None
    if moved_against:
        _add_finding(
            findings,
            "UNDERLYING_MOVED_AGAINST_POSITION",
            "strongly supported",
            f"NIFTY close changed by {underlying_change:.2f} points from signal close to the last "
            "available candle in the trade window, against the position direction. This association "
            "does not by itself establish causation.",
        )
    if best_unrealized > 0 and giveback > 0:
        _add_finding(
            findings,
            "EXIT_GAVE_BACK_PROFIT",
            "confirmed from recorded data",
            f"Observed peak premium implied ₹{best_unrealized:.2f} gross unrealized profit; gross P&L "
            f"at the simulated exit was ₹{gross:.2f}, a ₹{giveback:.2f} gross giveback.",
        )
    if gross >= 0 and net < 0:
        _add_finding(
            findings,
            "COSTS_ERODED_PROFIT",
            "confirmed from recorded data",
            f"Gross P&L was ₹{gross:.2f}, modeled costs were ₹{trade['costs']['total_costs']:.2f}, "
            f"and net P&L was ₹{net:.2f}.",
        )
    quality_warnings = [
        warning for warning in trade.get("warnings", [])
        if any(word in warning.lower() for word in ("missing", "zero traded volume", "gap", "unavailable"))
    ]
    if quality_warnings:
        _add_finding(
            findings,
            "DATA_QUALITY_ISSUE",
            "possible explanation",
            "Recorded data warnings: " + " ".join(quality_warnings),
        )
    if any("time exit executed" in warning.lower() for warning in trade.get("warnings", [])):
        _add_finding(
            findings,
            "SESSION_BOUNDARY_ISSUE",
            "possible explanation",
            "The simulated square-off occurred later than configured because intervening option candles "
            "were missing; no price was invented for the missing interval.",
        )
    if trade["exit_reason"] in {"STOP_LOSS", "ATR_TRAILING_STOP", "TRAILING_STOP"}:
        _add_finding(
            findings,
            "STOP_LOSS_EXECUTION_ISSUE",
            "unable to determine",
            "The backtest records a candle-based simulated stop fill; broker execution, queue position, "
            "bid/ask spread, and sub-candle trigger time are not available for verification.",
        )
    if not range_valid:
        _add_finding(
            findings,
            "DATA_QUALITY_ISSUE",
            "possible explanation",
            f"Recomputed opening range used {len(range_rows)} candles; expected "
            f"{config.opening_range_candles}, high/low match={range_valid}.",
        )
    _add_finding(
        findings,
        "INSUFFICIENT_DATA",
        "confirmed from available schema",
        "Historical IV/Greeks, bid/ask quotes, market depth, and true transaction costs are unavailable; "
        "their effect on the option premium and executable exit cannot be measured.",
    )

    classifications = {finding["code"] for finding in findings}
    recommendations: list[dict[str, str]] = []
    if "FALSE_BREAKOUT" in classifications:
        recommendations.append({
            "test": "Evaluate an additional completed-candle breakout confirmation or retest rule.",
            "tradeoff": "May filter failed breaks but can delay entries or miss fast moves; validate out of sample.",
            "area": "entry rules",
        })
    if "EXIT_GAVE_BACK_PROFIT" in classifications:
        recommendations.append({
            "test": "Compare the selected exit with a predeclared protective/trailing exit on the same entry.",
            "tradeoff": "A tighter exit may reduce giveback while increasing premature exits; this report is not proof it improves results.",
            "area": "exit rules",
        })
    if "STOP_LOSS_EXECUTION_ISSUE" in classifications:
        recommendations.append({
            "test": "Validate candle-based stop assumptions against finer-resolution trades and broker fill data.",
            "tradeoff": "Higher-resolution or quote data may be unavailable or costly; current fills remain simulated.",
            "area": "execution logic",
        })
    if "DATA_QUALITY_ISSUE" in classifications:
        recommendations.append({
            "test": "Recheck provider timestamps, missing candles, and volume using a second validated source.",
            "tradeoff": "Cross-provider differences may be timestamp conventions rather than actual data errors.",
            "area": "data quality",
        })
    if not recommendations:
        recommendations.append({
            "test": "Review this trade with additional intraday quote/volatility data before changing rules.",
            "tradeoff": "Current candles cannot identify a causal parameter change.",
            "area": "data quality",
        })

    holding_option_rows = [
        candle for candle in option_rows
        if entry_time < candle.timestamp < exit_time
    ]
    option_peak = max((candle.high for candle in holding_option_rows), default=None)
    option_low = min((candle.low for candle in holding_option_rows), default=None)
    option_candle_gaps = []
    for previous, current in zip(option_rows, option_rows[1:]):
        missing = int((current.timestamp - previous.timestamp).total_seconds() // (step_minutes * 60)) - 1
        if missing > 0:
            option_candle_gaps.append({
                "after": previous.timestamp.isoformat(),
                "before": current.timestamp.isoformat(),
                "missing_count": missing,
            })
    underlying_gap_count = 0
    for previous, current in zip(index_rows, index_rows[1:]):
        underlying_gap_count += max(
            0,
            int((current.timestamp - previous.timestamp).total_seconds() // (step_minutes * 60)) - 1,
        )
    actual_peak_gross = round((peak - entry_premium) * quantity, 2)
    dte = (
        (datetime.fromisoformat(contract["expiry"]).date()
         - datetime.fromisoformat(trade["session_date"]).date()).days
    )
    range_close_check = (
        signal_candle.close > float(trade["opening_range_high"])
        if trade["signal_direction"] == "UP"
        else signal_candle.close < float(trade["opening_range_low"])
    ) if signal_candle else None
    assessment = "completed-candle close confirms configured ORB direction" if range_close_check else (
        "signal candle unavailable for independent verification" if range_close_check is None
        else "signal close does not independently confirm the stored ORB boundary"
    )

    return {
        "report_id": f"{run_id}:{trade['trade_id']}:loss-forensics-v{ANALYSIS_VERSION}",
        "analysis_version": ANALYSIS_VERSION,
        "run_id": run_id,
        "trade_id": trade["trade_id"],
        "generated_for_completed_loss": True,
        "summary": {
            "trade_id": trade["trade_id"],
            "run_id": run_id,
            "trading_date": trade["session_date"],
            "instrument": contract["groww_symbol"],
            "option_type": contract["option_type"],
            "strike": contract["strike"],
            "expiry": contract["expiry"],
            "days_to_expiry": dte,
            "entry_timestamp": trade["entry_time"],
            "entry_fill_price": entry_premium,
            "exit_timestamp": trade["exit_time"],
            "exit_fill_price": exit_premium,
            "quantity": quantity,
            "lot_size": trade["lot_size"],
            "lot_size_status": trade["contract"]["lot_size_source"],
            "gross_pnl": gross,
            "costs": trade["costs"],
            "net_pnl": net,
            "entry_strategy": "NIFTY CASH ORB",
            "exit_strategy": trade["exit_strategy"],
            "exit_reason": trade["exit_reason"],
            "holding_duration_minutes": holding_minutes,
            "maximum_favourable_excursion_premium_points": trade.get("maximum_favourable_excursion"),
            "maximum_adverse_excursion_premium_points": trade.get("maximum_adverse_excursion"),
            "best_unrealized_gross_pnl": best_unrealized,
            "peak_unrealized_gross_pnl": actual_peak_gross,
            "maximum_profit_given_back_gross": giveback,
            "peak_premium": peak,
            "peak_timestamp": peak_time.isoformat() if peak_time else None,
            "false_breakout": bool(trade.get("false_breakout")),
            "false_breakout_timestamp": trade.get("false_breakout_time"),
        },
        "timeline": timeline,
        "entry_quality": {
            "opening_range_recalculated": {
                "passed": range_valid,
                "expected_candles": config.opening_range_candles,
                "observed_candles": len(range_rows),
                "stored_high": trade["opening_range_high"],
                "stored_low": trade["opening_range_low"],
                "recomputed_high": range_recomputed_high,
                "recomputed_low": range_recomputed_low,
            },
            "signal": {
                "signal_timestamp": trade["signal_time"],
                "signal_candle_start": trade["signal_candle_start"],
                "completed_candle_close": signal_candle.close if signal_candle else None,
                "direction": trade["signal_direction"],
                "rule_check": assessment,
                "entry_fill_timestamp": trade["entry_time"],
                "entry_delay_minutes": round((entry_time - signal_time).total_seconds() / 60, 2),
                "buffer_or_retest_required": False,
                "breakout_reversed_inside_range": bool(trade.get("false_breakout")),
                "stale_data": "not measurable from the historical response schema",
                "duplicate_timestamps": "rejected by candle validation before execution",
                "fill_realism": (
                    "open of selected option candle is a simulated fill; bid/ask, queue and market impact "
                    "are unavailable"
                ),
            },
            "data_validation": {
                "option_candle_gaps": option_candle_gaps,
                "underlying_missing_intervals_in_trade_window": underlying_gap_count,
                "trade_warnings": list(trade.get("warnings", [])),
                "timestamp_timezone": "provider wall-clock timestamps; timezone semantics are unverified",
            },
        },
        "exit_analysis": {
            "initial_stop_price": trade.get("stop_premium"),
            "target_price": trade.get("target_premium"),
            "stop_timeline": list(trade.get("stop_timeline", [])),
            "exit_signal_timestamp": trade.get("vwap_exit_signal_time") or (
                trade["exit_time"] if trade["exit_reason"] in {"STOP_LOSS", "ATR_TRAILING_STOP", "TRAILING_STOP"}
                else None
            ),
            "simulated_fill_timestamp": trade["exit_time"],
            "exit_reason": trade["exit_reason"],
            "fill_assessment": (
                "Exit price is the value recorded by the backtest execution model; it is not independently "
                "verified against a broker fill or executable market quote."
            ),
            "gap_or_slippage_notes": list(trade.get("warnings", [])),
            "lookahead_review": (
                "Entry uses the next option candle open after the completed signal. Exit simulation reads "
                "candles in chronological order and halts at the exit candle; entry/exit candle high-low "
                "ordering is not inferred."
            ),
            "intrabar_ambiguity": [
                warning for warning in trade.get("warnings", [])
                if "both touched" in warning.lower() or "candle" in warning.lower() and "stop" in warning.lower()
            ],
            "broker_orders_or_errors": "No broker orders are placed by this backtester.",
        },
        "underlying_vs_option": {
            "underlying_signal_close": trade["underlying_close_at_signal"],
            "underlying_last_close_in_trade_window": underlying_at_end,
            "underlying_change_points": _round(underlying_change, 2),
            "underlying_moved_against_option_direction": moved_against,
            "option_peak_premium": _round(option_peak),
            "option_low_premium": _round(option_low),
            "moneyness_at_signal_points": _round(
                float(trade["underlying_close_at_signal"]) - float(contract["strike"]), 2
            ),
            "time_to_expiry_days": dte,
            "volume": "per-candle provider-reported volume appears in timeline; missing values remain null",
            "implied_volatility": "unavailable; not inferred",
            "greeks": "unavailable historically; not inferred",
            "bid_ask_and_liquidity": "unavailable; not inferred",
        },
        "profit_opportunity": {
            "maximum_favourable_excursion_points": trade.get("maximum_favourable_excursion"),
            "maximum_adverse_excursion_points": trade.get("maximum_adverse_excursion"),
            "peak_unrealized_gross_pnl": actual_peak_gross,
            "peak_timestamp": peak_time.isoformat() if peak_time else None,
            "gross_profit_given_back": giveback,
            "ever_profitable_before_costs": actual_peak_gross > 0,
            "minutes_from_peak_to_exit": (
                round((exit_time - peak_time).total_seconds() / 60, 2)
                if peak_time is not None else None
            ),
            "counterfactuals": {
                "highest_premium_mark": {
                    "gross_pnl": trade.get("hypothetical_peak_exit_gross_pnl"),
                    "net_pnl": trade.get("hypothetical_peak_exit_net_pnl"),
                    "status": "hindsight mark only; not an executable alternative exit",
                },
                "other_exit_modes": "not simulated in this report; separate paired backtests must be run on a common entry set",
            },
        },
        "root_causes": findings,
        "recommendations": recommendations,
        "data_sources_and_limits": {
            "underlying": "Groww historical NIFTY CASH candles used for this backtest",
            "option": "Groww historical FNO option candles used for this backtest",
            "vwap": (
                "NIFTY futures volume-bearing VWAP source"
                if trade["exit_strategy"] == "vwap"
                else "not used for this trade"
            ),
            "lot_size": trade["contract"]["lot_size_source"],
            "timezone": "Provider timestamps are timezone-unspecified; validated against the session grid only.",
            "chart": "No charting library is present in the existing application; synchronized timeline rows are provided instead.",
            "persisted_history": "Reports are retained with the in-memory backtest run (existing run retention limit); they are not persisted across server restarts.",
            "cost_model": trade["net_pnl_basis"],
        },
    }


def build_loss_dashboard(
    trades: Sequence[dict[str, Any]],
    reports: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate evidence and sample sizes across completed trades without implying causality."""
    losses = [trade for trade in trades if float(trade.get("net_pnl", 0)) < 0]
    if not losses:
        return {
            "losing_trade_count": 0,
            "combined_net_loss": 0.0,
            "average_loss": None,
            "median_loss": None,
            "largest_loss": None,
            "sample_sizes": {"losing_trades": 0, "all_completed_trades": len(trades)},
            "by_month": {},
            "by_weekday": {},
            "by_entry_hour": {},
            "by_option_type": {},
            "by_expiry": {},
            "by_days_to_expiry": {},
            "by_exit_reason": {},
            "by_exit_strategy": {},
            "false_breakout": {"yes": 0, "no": 0, "sample_size": 0},
            "profitable_before_loss": {"count": 0, "sample_size": 0},
            "average_gross_profit_giveback": None,
            "root_causes": {},
            "losses_vs_winners_by_side_and_exit": {},
        }

    negative_values = [float(trade["net_pnl"]) for trade in losses]
    def bucket(field: Any) -> dict[str, Any]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in losses:
            groups[str(field(trade))].append(trade)
        return {
            key: {
                "count": len(rows),
                "net_loss": round(sum(float(row["net_pnl"]) for row in rows), 2),
                "sample_size": len(rows),
            }
            for key, rows in sorted(groups.items())
        }

    root_counts: Counter[str] = Counter()
    for report in reports:
        for finding in report["root_causes"]:
            root_counts[finding["code"]] += 1
    weekday_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    false_count = sum(bool(trade.get("false_breakout")) for trade in losses)
    profitable_count = sum(
        float(trade.get("hypothetical_peak_exit_gross_pnl", 0)) > 0
        for trade in losses
    )
    givebacks = [
        float(report["summary"]["maximum_profit_given_back_gross"])
        for report in reports
    ]
    comparison_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        key = f"{trade['contract']['option_type']}|{trade['exit_strategy']}"
        comparison_groups[key].append(trade)
    comparison = {}
    for key, rows in sorted(comparison_groups.items()):
        group_losses = [row for row in rows if float(row["net_pnl"]) < 0]
        comparison[key] = {
            "all_trade_sample_size": len(rows),
            "loser_sample_size": len(group_losses),
            "loser_rate_pct": round(len(group_losses) / len(rows) * 100, 2) if rows else None,
            "average_net_pnl": round(sum(float(row["net_pnl"]) for row in rows) / len(rows), 2),
        }
    dtes = {
        report["trade_id"]: report["summary"]["days_to_expiry"]
        for report in reports
    }
    return {
        "losing_trade_count": len(losses),
        "combined_net_loss": round(sum(negative_values), 2),
        "average_loss": round(sum(negative_values) / len(negative_values), 2),
        "median_loss": round(statistics.median(negative_values), 2),
        "largest_loss": round(min(negative_values), 2),
        "sample_sizes": {"losing_trades": len(losses), "all_completed_trades": len(trades)},
        "by_month": bucket(lambda trade: trade["session_date"][:7]),
        "by_weekday": bucket(
            lambda trade: weekday_names[datetime.fromisoformat(trade["session_date"]).weekday()]
        ),
        "by_entry_hour": bucket(lambda trade: trade["entry_time"][11:13] + ":00"),
        "by_option_type": bucket(lambda trade: trade["contract"]["option_type"]),
        "by_expiry": bucket(lambda trade: trade["contract"]["expiry"]),
        "by_days_to_expiry": bucket(lambda trade: dtes.get(trade["trade_id"], "unavailable")),
        "by_exit_reason": bucket(lambda trade: trade["exit_reason"]),
        "by_exit_strategy": bucket(lambda trade: trade["exit_strategy"]),
        "false_breakout": {
            "yes": false_count,
            "no": len(losses) - false_count,
            "sample_size": len(losses),
        },
        "profitable_before_loss": {
            "count": profitable_count,
            "sample_size": len(losses),
        },
        "average_gross_profit_giveback": (
            round(sum(givebacks) / len(givebacks), 2) if givebacks else None
        ),
        "root_causes": {
            code: {"count": count, "sample_size": len(reports)}
            for code, count in sorted(root_counts.items())
        },
        "losses_vs_winners_by_side_and_exit": comparison,
        "interpretation_note": (
            "Descriptive sample counts only; associations do not establish causes or statistical significance. "
            "Small samples should not be treated as reliable patterns."
        ),
    }
