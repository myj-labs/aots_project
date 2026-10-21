from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date, datetime, time as local_time, timedelta
from pathlib import Path

from .data import (
    BacktestConfig,
    DataAcquisitionError,
    KOLKATA,
    finalize_cache_validation,
    load_or_download,
    validate_bars,
)
from .engine import UnsupportedFilterError, run_backtest
from .report import write_data_quality, write_failure_report, write_results


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Dates must use YYYY-MM-DD.") from None


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Value must be a positive number.") from None
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Value must be a positive number.")
    return parsed


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.") from None
    if parsed < 0:
        raise argparse.ArgumentTypeError("Value must be a non-negative integer.")
    return parsed


def _defaults() -> tuple[date, date]:
    today = datetime.now(KOLKATA)
    end = today.date()
    if today.time().replace(tzinfo=None) >= local_time(15, 30):
        end += timedelta(days=1)
    return today.date() - timedelta(days=30), end


def _parser() -> argparse.ArgumentParser:
    default_start, default_end = _defaults()
    parser = argparse.ArgumentParser(
        description="Fetch, validate, and backtest a NIFTY 50 5-minute ORB strategy."
    )
    parser.add_argument("--start", type=_date, default=default_start, help="Inclusive YYYY-MM-DD (default: recent 30 days).")
    parser.add_argument("--end", type=_date, default=default_end, help="Exclusive YYYY-MM-DD.")
    parser.add_argument("--offline", action="store_true", help="Use only the exact matching cached CSV+manifest.")
    parser.add_argument(
        "--allow-stale-cache",
        action="store_true",
        help="Allow offline reuse beyond --max-cache-age-days; reports label the override.",
    )
    parser.add_argument(
        "--max-cache-age-days",
        type=_positive_float,
        default=7.0,
        help="Maximum cache age for offline reuse before an explicit override is required (default: 7).",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data/backtest"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports/backtest"))
    parser.add_argument(
        "--session-calendar",
        type=Path,
        help="Optional JSON file classifying regular sessions, holidays, and special sessions.",
    )
    parser.add_argument("--timeout", type=_positive_float, default=15.0)
    parser.add_argument("--retries", type=_nonnegative_int, default=2)
    parser.add_argument("--ema50", action="store_true", help="Run a separate EMA(50) experiment against baseline.")
    parser.add_argument("--atr-max-ratio", type=_positive_float, help="Run a separate OR-width / ATR(14) threshold experiment.")
    parser.add_argument("--vwap", action="store_true", help="Explicitly request unsupported index-volume VWAP (fails closed).")
    parser.add_argument("--breakout-volume", action="store_true", help="Explicitly request unsupported index volume filter (fails closed).")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.allow_stale_cache and not args.offline:
        parser.error("--allow-stale-cache requires --offline.")
    try:
        config = BacktestConfig(
            start=args.start,
            end=args.end,
            data_dir=args.data_dir,
            output_dir=args.output_dir,
            timeout_seconds=args.timeout,
            max_retries=args.retries,
            max_cache_age_days=args.max_cache_age_days,
            session_calendar=args.session_calendar,
            vwap_filter=args.vwap,
            breakout_volume_filter=args.breakout_volume,
        )
    except ValueError as exc:
        parser.error(str(exc))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        bars, metadata = load_or_download(
            config,
            offline=args.offline,
            allow_stale_cache=args.allow_stale_cache,
        )
    except DataAcquisitionError as exc:
        report = {
            "passed": False,
            "validation_status": "not_validated",
            "cache_integrity_status": "not_verified",
            "cache_freshness_status": "unknown",
            "cache_age_days": None,
            "cache_max_age_days": config.max_cache_age_days,
            "stale_cache_override": False,
            "ticker": config.ticker,
            "interval": config.interval,
            "requested_start": config.start.isoformat(),
            "requested_end_exclusive": config.end.isoformat(),
            "retrieval_mode": "offline cached reuse" if args.offline else "download requested",
            "actual_first_timestamp": None,
            "actual_last_timestamp": None,
            "errors": [str(exc)],
            "backtest_run": False,
        }
        write_failure_report(config.output_dir, report)
        print(f"Data acquisition stopped: {exc}", file=sys.stderr)
        print(f"Quality report: {config.output_dir / 'orb_data_quality.json'}", file=sys.stderr)
        return 2

    validated = validate_bars(bars, config, metadata)
    try:
        finalize_cache_validation(config, validated.quality)
    except DataAcquisitionError as exc:
        failure = dict(validated.quality)
        failure["passed"] = False
        failure["validation_status"] = "not_finalized"
        failure["backtest_run"] = False
        failure["errors"] = [*failure.get("errors", []), str(exc)]
        write_failure_report(config.output_dir, failure)
        print(f"Cache validation could not be finalized: {exc}", file=sys.stderr)
        return 2
    validated.quality["backtest_run"] = False
    write_data_quality(config.output_dir, validated.quality)
    if not validated.quality["passed"]:
        print("Historical data failed validation; no backtest was run.", file=sys.stderr)
        write_failure_report(config.output_dir, validated.quality)
        print(f"Quality report: {config.output_dir / 'orb_data_quality.json'}", file=sys.stderr)
        return 2
    try:
        baseline = run_backtest(validated, config)
        experiments = {}
        if args.ema50:
            experiment_config = replace(config, ema50_filter=True)
            experiment_data = validate_bars(bars, experiment_config, metadata)
            if not experiment_data.quality["passed"]:
                raise ValueError("EMA(50) experiment failed its data-quality gate.")
            experiments["EMA(50) alignment"] = run_backtest(
                experiment_data, experiment_config
            ).metrics
        if args.atr_max_ratio is not None:
            experiment_config = replace(config, opening_range_atr_max_ratio=args.atr_max_ratio)
            experiment_data = validate_bars(bars, experiment_config, metadata)
            if not experiment_data.quality["passed"]:
                raise ValueError("ATR(14) experiment failed its data-quality gate.")
            experiments[f"OR width / ATR(14) <= {args.atr_max_ratio:g}"] = run_backtest(
                experiment_data, experiment_config
            ).metrics
    except (UnsupportedFilterError, ValueError) as exc:
        failure = dict(validated.quality)
        failure["errors"] = [*failure.get("errors", []), str(exc)]
        failure["backtest_run"] = False
        write_failure_report(config.output_dir, failure)
        print(f"Backtest stopped: {exc}", file=sys.stderr)
        return 2

    validated.quality["backtest_run"] = True
    paths = write_results(config, validated, baseline, experiments)
    print(
        f"Completed baseline research run: {len(baseline.trades)} trades across "
        f"{validated.quality['eligible_session_count']} eligible sessions."
    )
    print(f"Data mode: {metadata.get('retrieval_mode')}; no claim of a fresh download in offline mode.")
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
