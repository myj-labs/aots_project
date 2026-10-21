from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as local_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


KOLKATA = ZoneInfo("Asia/Kolkata")
YAHOO_INTRADAY_LIMIT_DAYS = 60
EXPECTED_OHLC = ("open", "high", "low", "close")
CSV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "volume_invalid")


class DataAcquisitionError(RuntimeError):
    """A safe, actionable failure before any strategy is run."""


class DataValidationError(ValueError):
    def __init__(self, message: str, report: dict[str, Any]):
        super().__init__(message)
        self.report = report


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    data_dir: Path = Path("data/backtest")
    output_dir: Path = Path("reports/backtest")
    ticker: str = "^NSEI"
    interval: str = "5m"
    timezone_name: str = "Asia/Kolkata"
    timeout_seconds: float = 15.0
    max_retries: int = 2
    ema50_filter: bool = False
    opening_range_atr_max_ratio: float | None = None
    vwap_filter: bool = False
    breakout_volume_filter: bool = False
    max_trades_per_session: int = 1
    entry_cutoff: local_time = local_time(15, 0)
    time_exit: local_time = local_time(15, 15)

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("End date must be later than start date (end is exclusive).")
        if self.interval != "5m":
            raise ValueError("The ORB implementation requires the 5m interval.")
        if self.ticker != "^NSEI":
            raise ValueError("This research implementation only supports Yahoo ticker ^NSEI.")
        if self.timezone_name != "Asia/Kolkata":
            raise ValueError("NIFTY 50 session handling requires Asia/Kolkata.")
        if self.timeout_seconds <= 0 or self.max_retries < 0:
            raise ValueError("Timeout must be positive and retries must be non-negative.")
        if self.opening_range_atr_max_ratio is not None and self.opening_range_atr_max_ratio <= 0:
            raise ValueError("The opening-range/ATR ratio must be positive.")
        if self.max_trades_per_session < 1:
            raise ValueError("At least one completed trade per session must be allowed.")
        if self.entry_cutoff >= self.time_exit:
            raise ValueError("Entry cutoff must be earlier than the time exit.")

    @property
    def cache_stem(self) -> str:
        return f"nsei_5m_{self.start.isoformat()}_{self.end.isoformat()}"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None
    volume_invalid: bool = False


@dataclass
class ValidatedData:
    bars: list[Bar]
    metadata: dict[str, Any]
    quality: dict[str, Any]


def _atomic_write(path: Path, writer: Callable[[Any], None], *, binary: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        mode = "wb" if binary else "w"
        kwargs = {} if binary else {"encoding": "utf-8", "newline": ""}
        with os.fdopen(fd, mode, **kwargs) as handle:
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write(path, lambda handle: json.dump(payload, handle, indent=2, sort_keys=True))


def _column_for(columns: Sequence[Any], field_name: str, ticker: str) -> Any | None:
    candidates = []
    for column in columns:
        parts = tuple(str(part) for part in column) if isinstance(column, tuple) else (str(column),)
        if any(part.casefold() == field_name.casefold() for part in parts):
            candidates.append((column, parts))
    if not candidates:
        return None
    for column, parts in candidates:
        if ticker in parts:
            return column
    return candidates[0][0]


def _as_local_timestamp(value: Any) -> tuple[datetime, str]:
    if pd.isna(value):
        raise ValueError("Historical data contains an invalid timestamp.")
    stamp = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    if not isinstance(stamp, datetime):
        stamp = pd.Timestamp(stamp).to_pydatetime()
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        return stamp.replace(tzinfo=KOLKATA), "naive interpreted in Asia/Kolkata exchange time"
    return stamp.astimezone(KOLKATA), "timezone-aware converted to Asia/Kolkata"


def normalize_frame(frame: Any, ticker: str = "^NSEI") -> tuple[list[Bar], str]:
    if frame is None or getattr(frame, "empty", False):
        return [], "empty response"
    if not hasattr(frame, "columns") or not hasattr(frame, "index"):
        raise ValueError("Downloaded market data must be a table with timestamps and OHLC columns.")
    mapping = {
        field_name: _column_for(list(frame.columns), field_name, ticker)
        for field_name in (*EXPECTED_OHLC, "volume")
    }
    missing = [name for name in EXPECTED_OHLC if mapping[name] is None]
    if missing:
        raise ValueError(f"Downloaded market data is missing required fields: {', '.join(missing)}.")
    bars: list[Bar] = []
    timestamp_conventions: set[str] = set()
    for index, row in frame.iterrows():
        stamp, convention = _as_local_timestamp(index)
        timestamp_conventions.add(convention)
        values = {}
        for field_name in EXPECTED_OHLC:
            raw = row[mapping[field_name]]
            if pd.isna(raw):
                values[field_name] = float("nan")
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                value = float("nan")
            values[field_name] = value
        volume = None
        volume_invalid = False
        if mapping["volume"] is not None:
            raw_volume = row[mapping["volume"]]
            if not pd.isna(raw_volume):
                try:
                    volume = float(raw_volume)
                except (TypeError, ValueError):
                    volume = None
                    volume_invalid = True
                if volume is not None and (not math.isfinite(volume) or volume < 0):
                    volume = None
                    volume_invalid = True
        bars.append(Bar(
            timestamp=stamp,
            volume=volume,
            volume_invalid=volume_invalid,
            **values,
        ))
    convention = "; ".join(sorted(timestamp_conventions)) or "no timestamps"
    return bars, convention


def _read_cache(csv_path: Path) -> list[Bar]:
    bars = []
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != CSV_FIELDS:
            raise DataAcquisitionError("Cached data has an unsupported CSV schema; download it again.")
        for row in reader:
            stamp, _ = _as_local_timestamp(datetime.fromisoformat(row["timestamp"]))
            values = {name: float(row[name]) for name in EXPECTED_OHLC}
            volume = float(row["volume"]) if row["volume"] else None
            bars.append(Bar(
                timestamp=stamp,
                volume=volume,
                volume_invalid=row.get("volume_invalid", "False").casefold() == "true",
                **values,
            ))
    return bars


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_cache(csv_path: Path, bars: Iterable[Bar]) -> None:
    def write(handle: Any) -> None:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for bar in bars:
            writer.writerow({
                "timestamp": bar.timestamp.isoformat(),
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": "" if bar.volume is None else bar.volume,
                "volume_invalid": str(bar.volume_invalid),
            })
    _atomic_write(csv_path, write)


def load_or_download(
    config: BacktestConfig,
    *,
    offline: bool = False,
    downloader: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Bar], dict[str, Any]]:
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    age_days = (now.astimezone(KOLKATA).date() - config.start).days
    if not offline and age_days > YAHOO_INTRADAY_LIMIT_DAYS:
        raise DataAcquisitionError(
            f"5m Yahoo Finance history is limited to the last {YAHOO_INTRADAY_LIMIT_DAYS} calendar days; "
            "choose a more recent start date or use a matching validated cache."
        )
    if config.end > now.astimezone(KOLKATA).date() + timedelta(days=1):
        raise DataAcquisitionError("The requested end date is in the future.")
    csv_path = config.data_dir / f"{config.cache_stem}.csv"
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    request = {
        "ticker": config.ticker,
        "interval": config.interval,
        "start": config.start.isoformat(),
        "end_exclusive": config.end.isoformat(),
        "timezone": config.timezone_name,
    }
    if offline:
        if not csv_path.is_file() or not manifest_path.is_file():
            raise DataAcquisitionError("Offline mode requested, but no matching cached data and manifest exist.")
        try:
            metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise DataAcquisitionError("The cached metadata is unreadable; download the data again.") from None
        if metadata.get("request") != request or metadata.get("source") != "Yahoo Finance via yfinance":
            raise DataAcquisitionError("Cached data does not match the requested ticker, interval, and date range.")
        if metadata.get("validation_status") != "passed":
            raise DataAcquisitionError("Cached data has not passed validation; download and validate it first.")
        if metadata.get("csv_sha256") != _file_sha256(csv_path):
            raise DataAcquisitionError("Cached data checksum does not match its manifest; download it again.")
        raw_name = metadata.get("original_download_csv")
        raw_path = config.data_dir / raw_name if isinstance(raw_name, str) else None
        if (
            raw_path is None
            or not raw_path.is_file()
            or metadata.get("original_download_sha256") != _file_sha256(raw_path)
        ):
            raise DataAcquisitionError("Original Yahoo response cache is missing or does not match its manifest.")
        try:
            bars = _read_cache(csv_path)
        except (OSError, KeyError, ValueError, TypeError):
            raise DataAcquisitionError("Cached CSV data is unreadable; download it again.") from None
        metadata = {**metadata, "retrieval_mode": "offline cached reuse"}
        return bars, metadata

    if downloader is None:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise DataAcquisitionError("yfinance is not installed; install the project dependencies first.") from exc
        downloader = yf.download
    last_error = "empty response"
    frame = None
    for attempt in range(config.max_retries + 1):
        try:
            frame = downloader(
                config.ticker,
                start=config.start.isoformat(),
                end=config.end.isoformat(),
                interval=config.interval,
                auto_adjust=False,
                actions=False,
                prepost=False,
                repair=False,
                keepna=True,
                ignore_tz=False,
                progress=False,
                threads=False,
                timeout=config.timeout_seconds,
                multi_level_index=False,
            )
            if frame is not None and not getattr(frame, "empty", False):
                break
            last_error = "Yahoo Finance returned no rows for the requested range."
        except Exception as exc:
            last_error = f"download failed ({type(exc).__name__}); check network access and retry later."
        if attempt < config.max_retries:
            sleep(min(2 ** attempt, 4))
    if frame is None or getattr(frame, "empty", False):
        raise DataAcquisitionError(last_error)
    raw_path = config.data_dir / f"{config.cache_stem}.yahoo.csv"
    try:
        _atomic_write(raw_path, lambda handle: frame.to_csv(handle))
    except Exception:
        raise DataAcquisitionError("Yahoo Finance returned data, but the raw response could not be cached safely.") from None
    try:
        bars, timestamp_convention = normalize_frame(frame, config.ticker)
    except (TypeError, ValueError, KeyError) as exc:
        raise DataAcquisitionError(
            f"Yahoo Finance response could not be normalized: {exc}; original rows are preserved at {raw_path}."
        ) from None
    if not bars:
        raise DataAcquisitionError("Yahoo Finance response contained no usable OHLC rows.")
    retrieved_at = now.isoformat()
    timestamps = [bar.timestamp.isoformat() for bar in bars]
    metadata = {
        "source": "Yahoo Finance via yfinance",
        "request": request,
        "retrieved_at_utc": retrieved_at,
        "actual_first_timestamp_ist": timestamps[0],
        "actual_last_timestamp_ist": timestamps[-1],
        "row_count": len(bars),
        "original_download_csv": raw_path.name,
        "original_download_sha256": _file_sha256(raw_path),
        "validation_status": "pending",
        "timestamp_convention": timestamp_convention,
        "transformation": (
            "Column names normalized to lowercase; timestamps converted to Asia/Kolkata. "
            "Timezone-naive provider timestamps, if returned, are interpreted as exchange-local time. "
            "OHLC prices are not adjusted, filled, resampled, or fabricated."
        ),
        "yfinance_version": _package_version("yfinance"),
        "retrieval_mode": "downloaded now",
    }
    _write_cache(csv_path, bars)
    metadata["csv_sha256"] = _file_sha256(csv_path)
    _write_json(manifest_path, metadata)
    return bars, metadata


def finalize_cache_validation(
    config: BacktestConfig,
    quality: Mapping[str, Any],
) -> None:
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    if not manifest_path.is_file():
        return
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise DataAcquisitionError("Downloaded cache manifest could not be finalized.") from None
    metadata["validation_status"] = "passed" if quality.get("passed") else "failed"
    metadata["validation_summary"] = {
        "eligible_session_count": quality.get("eligible_session_count", 0),
        "missing_candle_count": quality.get("missing_candle_count", 0),
        "duplicate_timestamp_count": quality.get("duplicate_timestamp_count", 0),
        "invalid_ohlc_count": quality.get("invalid_ohlc_count", 0),
        "off_session_row_count": quality.get("off_session_row_count", 0),
        "invalid_ohlc_count": quality.get("invalid_ohlc_count", 0),
        "invalid_volume_count": quality.get("invalid_volume_count", 0),
    }
    _write_json(manifest_path, metadata)


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


def validate_bars(
    bars: Sequence[Bar],
    config: BacktestConfig,
    metadata: Mapping[str, Any] | None = None,
) -> ValidatedData:
    details: list[str] = []
    duplicate_count = 0
    invalid_ohlc_count = 0
    invalid_volume_count = sum(1 for bar in bars if bar.volume_invalid)
    off_session_count = 0
    invalid_dates: set[date] = set()
    timestamps = [bar.timestamp for bar in bars]
    if not bars:
        details.append("No market-data bars were available.")
    if any(stamp.tzinfo is None or stamp.utcoffset() is None for stamp in timestamps):
        details.append("One or more timestamps are timezone-naive after normalization.")
    if any(left >= right for left, right in zip(timestamps, timestamps[1:])):
        details.append("Timestamps are not strictly chronological.")
    seen: set[datetime] = set()
    for stamp in timestamps:
        if stamp in seen:
            duplicate_count += 1
        seen.add(stamp)
    if duplicate_count:
        details.append(f"{duplicate_count} duplicate timestamps were found.")

    grouped: dict[date, list[Bar]] = defaultdict(list)
    outside_requested_range = 0
    for bar in bars:
        if bar.timestamp.tzinfo is None or bar.timestamp.utcoffset() is None:
            continue
        stamp = bar.timestamp.astimezone(KOLKATA)
        if not config.start <= stamp.date() < config.end:
            outside_requested_range += 1
            continue
        if not (stamp.time() >= local_time(9, 15) and stamp.time() < local_time(15, 30)):
            off_session_count += 1
            continue
        if stamp.minute % 5 != 0 or stamp.second or stamp.microsecond:
            off_session_count += 1
            continue
        numeric = (bar.open, bar.high, bar.low, bar.close)
        valid = all(math.isfinite(value) and value > 0 for value in numeric)
        valid = valid and bar.high >= max(bar.open, bar.close) and bar.low <= min(bar.open, bar.close)
        valid = valid and bar.high >= bar.low
        if not valid:
            invalid_ohlc_count += 1
            invalid_dates.add(stamp.date())
        grouped[stamp.date()].append(bar)
    if invalid_ohlc_count:
        details.append(f"{invalid_ohlc_count} rows have invalid OHLC values or relationships.")
    if off_session_count:
        details.append(f"{off_session_count} rows fall outside the 09:15–15:30 IST 5-minute session.")
    if outside_requested_range:
        details.append(f"{outside_requested_range} rows fall outside the requested date range.")

    session_start = datetime.combine(date(2000, 1, 1), local_time(9, 15))
    expected_times = [
        (session_start + timedelta(minutes=5 * index)).time()
        for index in range(75)
    ]
    session_rows: list[dict[str, Any]] = []
    eligible_dates: list[str] = []
    excluded_dates: list[dict[str, str]] = []
    missing_total = 0
    for session_date, rows in sorted(grouped.items()):
        by_time: dict[local_time, list[Bar]] = defaultdict(list)
        for row in rows:
            by_time[row.timestamp.astimezone(KOLKATA).time()].append(row)
        missing = [at.strftime("%H:%M") for at in expected_times if len(by_time[at]) == 0]
        repeated = [at.strftime("%H:%M") for at in expected_times if len(by_time[at]) > 1]
        missing_total += len(missing)
        reasons = []
        if session_date in invalid_dates:
            reasons.append("session contains invalid or missing OHLC values")
        if missing:
            reasons.append(f"incomplete session; missing {len(missing)} expected candles")
        if repeated:
            reasons.append(f"duplicate candle times: {', '.join(repeated[:5])}")
        ordered = sorted(rows, key=lambda row: row.timestamp)
        opening = [
            row for row in ordered
            if local_time(9, 15) <= row.timestamp.astimezone(KOLKATA).time() < local_time(9, 45)
        ]
        opening_is_valid = all(
            all(math.isfinite(value) for value in (row.open, row.high, row.low, row.close))
            for row in opening
        )
        if (
            len(opening) == 6
            and opening_is_valid
            and max(row.high for row in opening) <= min(row.low for row in opening)
        ):
            reasons.append("opening-range width is zero")
        if not reasons:
            eligible_dates.append(session_date.isoformat())
        else:
            excluded_dates.append({"date": session_date.isoformat(), "reason": "; ".join(reasons)})
        session_rows.append({
            "date": session_date.isoformat(),
            "rows": len(rows),
            "missing_candles": len(missing),
            "missing_candle_times": missing,
            "eligible": not reasons,
            "reason": "; ".join(reasons) if reasons else None,
        })
    if not eligible_dates:
        details.append("No complete, valid 09:15–15:30 IST trading sessions were available.")
    weekday_dates_without_bars = []
    candidate = config.start
    while candidate < config.end:
        if candidate.weekday() < 5 and candidate not in grouped:
            weekday_dates_without_bars.append(candidate.isoformat())
        candidate += timedelta(days=1)
    # A Yahoo-data date is not enough to distinguish an exchange holiday from a provider outage.
    calendar_note = (
        "Sessions are identified from observed Yahoo timestamps and checked against the regular "
        "NSE 09:15–15:30 schedule. The tested exchange_calendars 4.13.2 release does not register XNSE, "
        "so no reliable NSE holiday calendar is available here. Weekdays with no returned bars are "
        "listed as ambiguous (holiday versus provider gap); verify them and special sessions against "
        "official NSE notices."
    )
    has_global_error = bool(
        duplicate_count or off_session_count
        or outside_requested_range
        or any("strictly chronological" in item or "timezone-naive" in item for item in details)
    )
    validated_bars = [
        bar for bar in bars
        if bar.timestamp.astimezone(KOLKATA).date().isoformat() in eligible_dates
    ]
    if config.ema50_filter and len(validated_bars) < 50:
        details.append("Insufficient completed candles for the configured EMA(50) experiment.")
    if config.opening_range_atr_max_ratio is not None and len(validated_bars) < 14:
        details.append("Insufficient completed candles for the configured ATR(14) experiment.")
    passed = (
        bool(eligible_dates)
        and not has_global_error
        and not (config.ema50_filter and len(validated_bars) < 50)
        and not (config.opening_range_atr_max_ratio is not None and len(validated_bars) < 14)
    )
    fatal_errors = [
        item for item in details
        if (
            "No market-data" in item
            or "timezone-naive" in item
            or "not strictly chronological" in item
            or "duplicate timestamps" in item
            or "fall outside the 09:15" in item
            or "outside the requested date range" in item
            or "No complete, valid" in item
            or "Insufficient completed candles" in item
        )
    ]
    quality = {
        "passed": passed,
        "ticker": config.ticker,
        "interval": config.interval,
        "timezone": config.timezone_name,
        "requested_start": config.start.isoformat(),
        "requested_end_exclusive": config.end.isoformat(),
        "actual_first_timestamp": timestamps[0].astimezone(KOLKATA).isoformat() if timestamps else None,
        "actual_last_timestamp": timestamps[-1].astimezone(KOLKATA).isoformat() if timestamps else None,
        "row_count": len(bars),
        "observed_session_count": len(grouped),
        "weekday_dates_without_bars": weekday_dates_without_bars,
        "eligible_session_count": len(eligible_dates),
        "eligible_sessions": eligible_dates,
        "rejected_sessions": excluded_dates,
        "missing_candle_count": missing_total,
        "duplicate_timestamp_count": duplicate_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "off_session_row_count": off_session_count,
        "invalid_volume_count": invalid_volume_count,
        "ohlc_validation": (
            "passed"
            if invalid_ohlc_count == 0
            else "passed with affected sessions excluded" if passed
            else "failed"
        ),
        "chronological": not any(left >= right for left, right in zip(timestamps, timestamps[1:])),
        "timestamp_convention": (
            "Yahoo 5-minute index labels are treated as candle start times; expected bars run "
            "09:15 through 15:25 IST. No end-stamp conversion is inferred."
        ),
        "volume": {
            "field_available": any(bar.volume is not None for bar in bars),
            "missing_value_count": sum(
                1 for bar in bars if bar.volume is None and not bar.volume_invalid
            ),
            "positive_value_count": sum(1 for bar in bars if bar.volume is not None and bar.volume > 0),
            "invalid_value_count": invalid_volume_count,
            "interpretation": (
                "Not considered meaningful traded volume for the ^NSEI index. Volume-dependent "
                "VWAP and breakout-volume filters are unavailable and are not computed."
            ),
        },
        "calendar_note": calendar_note,
        "session_details": session_rows,
        "warnings": [
            item for item in details if item not in fatal_errors
        ],
        "errors": fatal_errors,
        "provider_limitations": [
            "yfinance is an unofficial client of Yahoo Finance; Yahoo access and data rights are subject to Yahoo terms.",
            "Yahoo intraday data is limited to the most recent 60 calendar days.",
            "Intraday candles may be delayed, incomplete, revised, or unavailable.",
        ],
        "retrieved_at_utc": (metadata or {}).get("retrieved_at_utc"),
        "retrieval_mode": (metadata or {}).get("retrieval_mode", "provided to validator"),
    }
    return ValidatedData(
        bars=validated_bars,
        metadata=dict(metadata or {}),
        quality=quality,
    )


def validate_or_raise(
    bars: Sequence[Bar],
    config: BacktestConfig,
    metadata: Mapping[str, Any] | None = None,
) -> ValidatedData:
    validated = validate_bars(bars, config, metadata)
    if not validated.quality["passed"]:
        raise DataValidationError(
            "Historical data failed the quality gate; no backtest was run.",
            validated.quality,
        )
    return validated
