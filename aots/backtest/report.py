from __future__ import annotations

import csv
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping

from .data import BacktestConfig, ValidatedData
from .engine import BacktestResult


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    key: json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value
                    for key, value in row.items()
                })
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _display(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def _markdown_table(rows: list[tuple[str, Any]]) -> str:
    return "\n".join(
        ["| Measure | Result |", "|---|---:|"]
        + [f"| {name} | {_display(value)} |" for name, value in rows]
    )


def write_data_quality(output_dir: Path, report: Mapping[str, Any]) -> Path:
    path = output_dir / "orb_data_quality.json"
    _atomic_text(path, json.dumps(report, indent=2, sort_keys=True) + "\n")
    return path


def write_results(
    config: BacktestConfig,
    data: ValidatedData,
    result: BacktestResult,
    experiments: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Path]:
    output = config.output_dir
    output.mkdir(parents=True, exist_ok=True)
    signals = [asdict(signal) for signal in result.signals]
    trades = [asdict(trade) for trade in result.trades]
    signal_columns = list(signals[0]) if signals else list(asdict_placeholder_signal())
    trade_columns = list(trades[0]) if trades else list(asdict_placeholder_trade())
    _write_csv(output / "orb_signals.csv", signals, signal_columns)
    _write_csv(output / "orb_trades.csv", trades, trade_columns)
    metrics = dict(result.metrics)
    metrics["filter_experiments"] = dict(experiments or {})
    _atomic_text(output / "orb_metrics.json", json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    _atomic_text(output / "orb_data_quality.json", json.dumps(data.quality, indent=2, sort_keys=True) + "\n")
    report = _build_report(config, data, result, experiments or {})
    _atomic_text(output / "orb_backtest_report.md", report)
    return [
        output / "orb_backtest_report.md",
        output / "orb_trades.csv",
        output / "orb_signals.csv",
        output / "orb_data_quality.json",
        output / "orb_metrics.json",
    ]


def asdict_placeholder_signal() -> dict[str, Any]:
    return {
        "session_date": "",
        "timestamp": "",
        "decision_timestamp": "",
        "direction": "",
        "opening_range_high": "",
        "opening_range_low": "",
        "opening_range_width": "",
        "close_price": "",
        "active_filters": "",
        "accepted": "",
        "reason": "",
        "entry_timestamp": "",
        "entry_price": "",
        "filters_passed": "",
        "entry_attempted": "",
    }


def asdict_placeholder_trade() -> dict[str, Any]:
    return {
        "session_date": "",
        "signal_timestamp": "",
        "entry_timestamp": "",
        "direction": "",
        "opening_range_high": "",
        "opening_range_low": "",
        "opening_range_width": "",
        "entry_price": "",
        "stop_price": "",
        "target_price": "",
        "exit_timestamp": "",
        "exit_price": "",
        "exit_reason": "",
        "gross_pnl_points": "",
        "initial_risk_points": "",
        "realized_r_multiple": "",
        "maximum_favourable_excursion_points": "",
        "maximum_adverse_excursion_points": "",
        "execution_adjustment": "",
    }


def _build_report(
    config: BacktestConfig,
    data: ValidatedData,
    result: BacktestResult,
    experiments: Mapping[str, Mapping[str, Any]],
) -> str:
    quality = data.quality
    metrics = result.metrics
    research = metrics["research_validation"]
    lines = [
        "# NIFTY 50 30-minute Opening Range Breakout — Research Backtest",
        "",
        f"**Verdict: {research['verdict'].upper()}** — {research['reason']}",
        "",
        "## Executive summary",
        "",
        f"- Valid complete sessions: {quality['eligible_session_count']}; incomplete/invalid sessions excluded: {len(quality['rejected_sessions'])}.",
        f"- Raw breakout events: {metrics['raw_breakout_events']}; filter-passing: {metrics['filter_passing_events']}; filter-rejected: {metrics['filter_rejected_events']}; entry attempts: {metrics['entry_attempts']}; entry-rejected: {metrics['entry_rejected_events']}; completed underlying trades: {metrics['completed_trades']}.",
        f"- Gross underlying result: {_display(metrics['gross_underlying_points'])} index points.",
        "- This is an underlying-price simulation, **not actual NIFTY options P&L** and not a live or paper order.",
        "",
        "## Strategy and execution rules",
        "",
        "- Opening range is the six completed 5-minute candles labeled 09:15 through 09:40 IST; levels freeze at 09:45 IST.",
        "- A completed candle closing strictly above the opening-range high signals LONG; strictly below its low signals SHORT.",
        "- A breakout event is a transition from inside/no breakout to a close strictly outside the range, or a direct transition to the opposite side. Consecutive closes on the same side are not new events; price must close back inside the range before a same-side event can recur.",
        "- Entry is the next 5-minute candle open. The stop is the opposite range boundary; target is 2R from entry.",
        "- An entry attempt is counted only when a filter-passing event also passes entry constraints and opens a simulated position; blocked events are reported separately with their rejection reasons.",
        f"- Time exit uses the {_display(config.time_exit.strftime('%H:%M'))} IST candle open; no entries are accepted after the {_display(config.entry_cutoff.strftime('%H:%M'))} IST cutoff.",
        "- At most one position at a time and one completed trade per session by default. Same-candle stop/target ambiguity is resolved stop-first; a stop gap fills at the worse candle open.",
        "- No volume filters are used: ^NSEI index volume is not treated as meaningful traded-instrument volume. Optional VWAP and breakout-volume filters are unavailable.",
        "- EMA(50), when enabled in a comparison, is initialized with the first 50 completed closes using an SMA seed. ATR(14), when enabled, is the simple average of the last 14 completed true ranges available at signal time.",
        "",
        "## Data provenance and quality",
        "",
        f"- Provider: {data.metadata.get('source', 'unknown')}; ticker `{config.ticker}`; interval `{config.interval}`.",
        f"- Requested: `{config.start.isoformat()}` through `{config.end.isoformat()}` (end exclusive).",
        f"- Actual: `{quality.get('actual_first_timestamp')}` through `{quality.get('actual_last_timestamp')}`; {quality['row_count']} normalized rows.",
        f"- Retrieval: `{data.metadata.get('retrieved_at_utc')}` UTC; mode: `{data.metadata.get('retrieval_mode')}`.",
        f"- Timestamp rule: `{quality['timestamp_convention']}`",
        f"- Quality gate: **{'PASSED' if quality['passed'] else 'FAILED'}**; missing candle count: {quality['missing_candle_count']}; duplicate timestamps: {quality['duplicate_timestamp_count']}; invalid OHLC: {quality['invalid_ohlc_count']}; off-session rows: {quality['off_session_row_count']}.",
        f"- Weekdays in the requested range with no returned data (possibly holidays or provider gaps): {', '.join(quality.get('weekday_dates_without_bars', [])) or 'none'}.",
        f"- Calendar limitation: {quality['calendar_note']}",
        "",
        "### Excluded sessions",
        "",
    ]
    if quality["rejected_sessions"]:
        lines.extend(f"- {item['date']}: {item['reason']}" for item in quality["rejected_sessions"])
    else:
        lines.append("- None.")
    lines += [
        "",
        "## Performance metrics",
        "",
        _markdown_table([
            ("Eligible sessions", metrics["eligible_sessions"]),
            ("Rejected sessions", metrics["sessions_rejected_for_data_quality"]),
            ("Raw breakout events", metrics["raw_breakout_events"]),
            ("Filter-passing events", metrics["filter_passing_events"]),
            ("Filter-rejected events", metrics["filter_rejected_events"]),
            ("Entry attempts", metrics["entry_attempts"]),
            ("Entry-rejected events", metrics["entry_rejected_events"]),
            ("Completed trades", metrics["completed_trades"]),
            ("Wins / losses / breakeven", f"{metrics['winning_trades']} / {metrics['losing_trades']} / {metrics['breakeven_trades']}"),
            ("Win rate", _display(metrics["win_rate"])),
            ("Gross / net underlying points", f"{_display(metrics['gross_underlying_points'])} / {_display(metrics['net_underlying_points'])}"),
            ("Average trade points", metrics["average_trade_points"]),
            ("Average win / loss points", f"{_display(metrics['average_win_points'])} / {_display(metrics['average_loss_points'])}"),
            ("Gross profit factor", metrics["profit_factor"] if metrics["profit_factor_defined"] else "undefined (no gross losses)"),
            ("Average realized R", metrics["average_realized_r_multiple"]),
            ("Maximum drawdown points", metrics["maximum_drawdown_points"]),
            ("Largest win / loss points", f"{_display(metrics['largest_win_points'])} / {_display(metrics['largest_loss_points'])}"),
            ("Maximum consecutive wins / losses", f"{metrics['maximum_consecutive_wins']} / {metrics['maximum_consecutive_losses']}"),
        ]),
        "",
        f"Drawdown definition: {metrics['drawdown_convention']}",
        "",
        "Gross and net match only because this research simulation assumes zero brokerage, exchange fees, taxes, slippage, and contract multiplier. Results are index points only.",
        "",
        "### Long and short results",
        "",
        _markdown_table([
            ("Long trades / gross points", f"{metrics['by_direction']['long']['trades']} / {_display(metrics['by_direction']['long']['gross_pnl_points'])}"),
            ("Short trades / gross points", f"{metrics['by_direction']['short']['trades']} / {_display(metrics['by_direction']['short']['gross_pnl_points'])}"),
        ]),
        "",
        "### Results by session",
        "",
        "| Session date | Trades | Direction(s) | Gross underlying points |",
        "|---|---:|---|---:|",
    ]
    for session, values in sorted(metrics["by_session"].items()):
        lines.append(
            f"| {session} | {values['trades']} | {', '.join(values['directions'])} | {values['gross_pnl_points']:.4f} |"
        )
    lines += [
        "",
        "### Event rejection reasons",
        "",
        "| Reason | Events |",
        "|---|---:|",
    ]
    for reason, count in metrics["rejection_reason_counts"].items():
        lines.append(f"| {reason} | {count} |")
    lines += ["", "## Filter experiments (one at a time)", ""]
    if experiments:
        lines += [
            "| Configuration | Raw events | Filter rejected | Filter passing | Entry attempts | Trades | Gross points | OOS verdict |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
            f"| Baseline | {metrics['raw_breakout_events']} | {metrics['filter_rejected_events']} | {metrics['filter_passing_events']} | {metrics['entry_attempts']} | {metrics['completed_trades']} | {metrics['gross_underlying_points']:.4f} | {research['verdict']} |",
        ]
        for name, values in experiments.items():
            lines.append(
                f"| {name} | {values['raw_breakout_events']} | {values['filter_rejected_events']} | {values['filter_passing_events']} | {values['entry_attempts']} | {values['completed_trades']} | {values['gross_underlying_points']:.4f} | {values['research_validation']['verdict']} |"
            )
    else:
        lines.append("No optional filter experiment was requested. Baseline parameters were not tuned.")
    reason_sets = [("Baseline", metrics["rejection_reason_counts"])]
    reason_sets.extend(
        (name, values["rejection_reason_counts"])
        for name, values in experiments.items()
    )
    if any(reasons for _, reasons in reason_sets):
        lines += [
            "",
            "### Rejection reasons by configuration",
            "",
            "| Configuration | Rejection reason | Events |",
            "|---|---|---:|",
        ]
        for name, reasons in reason_sets:
            if reasons:
                lines.extend(
                    f"| {name} | {reason} | {count} |"
                    for reason, count in reasons.items()
                )
            else:
                lines.append(f"| {name} | None | 0 |")
    lines += [
        "",
        "## Research interpretation and limitations",
        "",
        f"- Chronological out-of-sample assessment: `{research['verdict']}`. {research['reason']}",
        "- The sample may be too short for statistical conclusions; results are sensitive to the selected date window and Yahoo data revisions/availability.",
        "- 5-minute OHLC bars do not reveal intrabar ordering. When both stop and target are touched in one candle, stop-first is applied. MFE/MAE omit the exit candle to avoid claiming an unknowable intrabar path.",
        "- Market-condition results are not classified; no pre-registered, defensible market-regime labels are included.",
        "- Gaps through a stop fill at the candle open; targets fill at their limit level without modeled price improvement.",
        "- Yahoo timestamps are treated as candle-start labels and the expected session is 09:15–15:25 IST. Any provider end-stamp convention would require separate verification before using these results.",
        "- Calendar dates with no provider rows cannot be distinguished from NSE holidays without checking official exchange notices; special sessions are not modeled.",
        "- **Options backtest: NOT PERFORMED.** The ^NSEI index series contains no option premiums. Required data includes historical contracts/expiries, strike mapping, option OHLC/trades or bid/ask quotes, lot sizes, and transaction costs. No option premium, Greeks, or option return was fabricated.",
        "- This is research code. It does not connect to live order execution, does not alter AOTS paper trading, and does not establish investment merit.",
        "",
        "## Reproduction",
        "",
        "Run `python -m aots.backtest --start YYYY-MM-DD --end YYYY-MM-DD` to download then validate and run; `--offline` reuses the matching CSV+manifest cache and does not claim a fresh download. Outputs are written to the configured report directory.",
        "",
        "Yahoo Finance / yfinance access is unofficial and subject to Yahoo terms. yfinance documents the 60-calendar-day limit for intraday bars; requested dates outside that provider window require a separate licensed historical dataset.",
        "",
    ]
    return "\n".join(lines)
