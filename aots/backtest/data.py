from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import tempfile
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as local_time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

import pandas as pd


KOLKATA = ZoneInfo("Asia/Kolkata")
YAHOO_INTRADAY_LIMIT_DAYS = 60
EXPECTED_OHLC = ("open", "high", "low", "close")
CSV_FIELDS = ("timestamp", "open", "high", "low", "close", "volume", "volume_invalid")
MANIFEST_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 1
DEFAULT_MAX_CACHE_AGE_DAYS = 7.0


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
    max_cache_age_days: float = DEFAULT_MAX_CACHE_AGE_DAYS
    session_calendar: Path | None = None
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
        if (
            isinstance(self.max_cache_age_days, bool)
            or not math.isfinite(self.max_cache_age_days)
            or self.max_cache_age_days <= 0
        ):
            raise ValueError("Maximum cache age must be a positive number of days.")
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


def _utc_timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DataAcquisitionError(f"{label} must be a timezone-aware timestamp.")
    return value.astimezone(timezone.utc)


def _parse_manifest_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DataAcquisitionError("Cache retrieval timestamp is missing or malformed.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise DataAcquisitionError("Cache retrieval timestamp is malformed.") from None
    return _utc_timestamp(parsed, label="Cache retrieval timestamp")


def _manifest_compatibility(metadata: Mapping[str, Any]) -> tuple[int, int]:
    manifest_version = metadata.get("manifest_schema_version", 0)
    cache_version = metadata.get("cache_schema_version", 0)
    if (
        not isinstance(manifest_version, int)
        or isinstance(manifest_version, bool)
        or not isinstance(cache_version, int)
        or isinstance(cache_version, bool)
        or manifest_version < 0
        or cache_version < 0
        or manifest_version > MANIFEST_SCHEMA_VERSION
        or cache_version > CACHE_SCHEMA_VERSION
    ):
        raise DataAcquisitionError("Cache manifest or CSV schema version is incompatible; refresh the cache.")
    if manifest_version == MANIFEST_SCHEMA_VERSION and cache_version != CACHE_SCHEMA_VERSION:
        raise DataAcquisitionError("Cache manifest and CSV schema versions do not match.")
    return manifest_version, cache_version


def _load_session_calendar(path: Path | None) -> tuple[dict[str, set[date]], str | None, str | None]:
    if path is None:
        return {"holidays": set(), "special_sessions": set(), "regular_sessions": set()}, None, None
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return (
            {"holidays": set(), "special_sessions": set(), "regular_sessions": set()},
            None,
            "Configured session calendar is unreadable; use a valid JSON calendar file.",
        )
    if not isinstance(payload, dict) or set(payload) - {
        "source",
        "holidays",
        "special_sessions",
        "regular_sessions",
    }:
        return (
            {"holidays": set(), "special_sessions": set(), "regular_sessions": set()},
            hashlib.sha256(raw).hexdigest(),
            "Configured session calendar has an unsupported schema.",
        )
    parsed: dict[str, set[date]] = {}
    try:
        for key in ("holidays", "special_sessions", "regular_sessions"):
            values = payload.get(key, [])
            if not isinstance(values, list):
                raise ValueError
            dates = set()
            for value in values:
                if not isinstance(value, str):
                    raise ValueError
                parsed_date = date.fromisoformat(value)
                if parsed_date.isoformat() != value:
                    raise ValueError
                dates.add(parsed_date)
            if len(dates) != len(values):
                raise ValueError
            parsed[key] = dates
    except ValueError:
        return (
            {"holidays": set(), "special_sessions": set(), "regular_sessions": set()},
            hashlib.sha256(raw).hexdigest(),
            "Configured session calendar dates must be ISO YYYY-MM-DD arrays.",
        )
    if (
        parsed["holidays"] & parsed["special_sessions"]
        or parsed["holidays"] & parsed["regular_sessions"]
        or parsed["special_sessions"] & parsed["regular_sessions"]
    ):
        return (
            parsed,
            hashlib.sha256(raw).hexdigest(),
            "Configured session calendar assigns conflicting classifications to a date.",
        )
    source = payload.get("source")
    if source is not None and not isinstance(source, str):
        return (
            parsed,
            hashlib.sha256(raw).hexdigest(),
            "Configured session calendar source must be a string.",
        )
    return parsed, hashlib.sha256(raw).hexdigest(), None


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
    allow_stale_cache: bool = False,
    downloader: Callable[..., Any] | None = None,
    clock: Callable[[], datetime] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[list[Bar], dict[str, Any]]:
    try:
        now = _utc_timestamp(
            (clock or (lambda: datetime.now(timezone.utc)))(),
            label="Clock",
        )
    except DataAcquisitionError:
        raise
    except Exception:
        raise DataAcquisitionError("Clock did not provide a valid UTC timestamp.") from None
    if allow_stale_cache and not offline:
        raise DataAcquisitionError("Stale-cache override is only available with offline mode.")
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
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise DataAcquisitionError("The cached metadata is unreadable; download the data again.") from None
        if not isinstance(metadata, dict):
            raise DataAcquisitionError("The cached manifest has an unsupported structure; download it again.")
        manifest_version, cache_version = _manifest_compatibility(metadata)
        if metadata.get("request") != request or metadata.get("source") != "Yahoo Finance via yfinance":
            raise DataAcquisitionError("Cached data does not match the requested ticker, interval, and date range.")
        if manifest_version == MANIFEST_SCHEMA_VERSION and metadata.get("instrument") != config.ticker:
            raise DataAcquisitionError("Cache instrument metadata does not match the requested ticker.")
        if metadata.get("validation_status") != "passed":
            raise DataAcquisitionError("Cached data has not passed validation; download and validate it first.")
        if (
            manifest_version == MANIFEST_SCHEMA_VERSION
            and (
                not isinstance(metadata.get("retrieval_id"), str)
                or not metadata.get("retrieval_id")
            )
        ):
            raise DataAcquisitionError("Cache provenance is incomplete; refresh the cache.")
        if metadata.get("csv_sha256") != _file_sha256(csv_path):
            raise DataAcquisitionError("Cached data checksum does not match its manifest; download it again.")
        raw_name = metadata.get("original_download_csv")
        raw_path = config.data_dir / raw_name if isinstance(raw_name, str) else None
        if (
            raw_path is None
            or raw_path.name != f"{config.cache_stem}.yahoo.csv"
            or not raw_path.is_file()
            or metadata.get("original_download_sha256") != _file_sha256(raw_path)
        ):
            raise DataAcquisitionError("Original Yahoo response cache is missing or does not match its manifest.")
        try:
            bars = _read_cache(csv_path)
        except (OSError, KeyError, ValueError, TypeError):
            raise DataAcquisitionError("Cached CSV data is unreadable; download it again.") from None
        retrieval_time = _parse_manifest_timestamp(metadata.get("retrieved_at_utc"))
        if retrieval_time > now:
            raise DataAcquisitionError("Cache retrieval timestamp is in the future; check the system clock.")
        age = now - retrieval_time
        age_days = age.total_seconds() / 86400
        stale = age_days > config.max_cache_age_days
        if stale and not allow_stale_cache:
            raise DataAcquisitionError(
                f"Cached data is stale ({age_days:.2f} days old; maximum is "
                f"{config.max_cache_age_days:g} days). Use --allow-stale-cache with --offline "
                "to explicitly reuse it."
            )
        if (
            not isinstance(metadata.get("row_count"), int)
            or isinstance(metadata.get("row_count"), bool)
            or metadata.get("row_count") != len(bars)
        ):
            raise DataAcquisitionError("Cached row count does not match its manifest.")
        if bars:
            if (
                metadata.get("actual_first_timestamp_ist") != bars[0].timestamp.isoformat()
                or metadata.get("actual_last_timestamp_ist") != bars[-1].timestamp.isoformat()
            ):
                raise DataAcquisitionError("Cached timestamp range does not match its manifest.")
        if (
            manifest_version == MANIFEST_SCHEMA_VERSION
            and (
                metadata.get("validated_csv_sha256") != metadata.get("csv_sha256")
                or metadata.get("validated_original_download_sha256")
                != metadata.get("original_download_sha256")
                or metadata.get("validated_retrieval_id") != metadata.get("retrieval_id")
            )
        ):
            raise DataAcquisitionError("Cache validation is not bound to the current cached data.")
        metadata = {
            **metadata,
            "retrieval_mode": "offline cached reuse",
            "manifest_schema_version": manifest_version,
            "cache_schema_version": cache_version,
            "cache_provenance": (
                "legacy manifest verified by request and checksums"
                if manifest_version == 0
                else "versioned manifest"
            ),
            "cache_integrity_status": "verified",
            "cache_age_seconds": age.total_seconds(),
            "cache_age_days": age_days,
            "cache_max_age_days": config.max_cache_age_days,
            "cache_freshness_status": "stale" if stale else "fresh",
            "stale_cache_override": stale and allow_stale_cache,
        }
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
    try:
        bars, timestamp_convention = normalize_frame(frame, config.ticker)
    except (TypeError, ValueError, KeyError) as exc:
        raise DataAcquisitionError(f"Yahoo Finance response could not be normalized: {exc}.") from None
    if not bars:
        raise DataAcquisitionError("Yahoo Finance response contained no usable OHLC rows.")
    raw_path = config.data_dir / f"{config.cache_stem}.yahoo.csv"
    try:
        _atomic_write(raw_path, lambda handle: frame.to_csv(handle))
    except Exception:
        raise DataAcquisitionError("Yahoo Finance returned data, but the raw response could not be cached safely.") from None
    retrieved_at = now.isoformat()
    timestamps = [bar.timestamp.isoformat() for bar in bars]
    metadata = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "retrieval_id": uuid.uuid4().hex,
        "source": "Yahoo Finance via yfinance",
        "instrument": config.ticker,
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
    try:
        _write_cache(csv_path, bars)
        metadata["csv_sha256"] = _file_sha256(csv_path)
        metadata.update({
            "cache_integrity_status": "verified",
            "cache_age_seconds": 0.0,
            "cache_age_days": 0.0,
            "cache_max_age_days": config.max_cache_age_days,
            "cache_freshness_status": "fresh",
            "stale_cache_override": False,
            "cache_provenance": "downloaded in this run",
        })
        _write_json(manifest_path, metadata)
    except OSError:
        raise DataAcquisitionError(
            "Downloaded data could not be committed as a complete cache; refresh and validate it again."
        ) from None
    return bars, metadata


def finalize_cache_validation(
    config: BacktestConfig,
    quality: Mapping[str, Any],
) -> None:
    manifest_path = config.data_dir / f"{config.cache_stem}.json"
    if not manifest_path.is_file():
        raise DataAcquisitionError("Cache manifest is missing; validation status was not saved.")
    try:
        metadata = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        raise DataAcquisitionError("Downloaded cache manifest could not be finalized.") from None
    if not isinstance(metadata, dict):
        raise DataAcquisitionError("Downloaded cache manifest has an unsupported structure.")
    manifest_version, _ = _manifest_compatibility(metadata)
    request = {
        "ticker": config.ticker,
        "interval": config.interval,
        "start": config.start.isoformat(),
        "end_exclusive": config.end.isoformat(),
        "timezone": config.timezone_name,
    }
    if metadata.get("request") != request:
        raise DataAcquisitionError("Cache request changed before validation could be finalized.")
    csv_path = config.data_dir / f"{config.cache_stem}.csv"
    original_name = metadata.get("original_download_csv")
    if (
        not isinstance(original_name, str)
        or Path(original_name).name != f"{config.cache_stem}.yahoo.csv"
    ):
        raise DataAcquisitionError("Cache provenance is incomplete; validation status was not saved.")
    original_path = config.data_dir / original_name
    try:
        csv_hash = _file_sha256(csv_path)
        original_hash = _file_sha256(original_path)
    except OSError:
        raise DataAcquisitionError("Cache files are missing; validation status was not saved.") from None
    if (
        csv_hash != metadata.get("csv_sha256")
        or original_hash != metadata.get("original_download_sha256")
        or quality.get("cache_csv_sha256") != csv_hash
        or quality.get("cache_original_download_sha256") != original_hash
        or quality.get("cache_retrieval_id") != metadata.get("retrieval_id")
    ):
        raise DataAcquisitionError(
            "Cache changed after validation; validation status was not transferred to different data."
        )
    if manifest_version == MANIFEST_SCHEMA_VERSION and not metadata.get("retrieval_id"):
        raise DataAcquisitionError("Cache provenance is incomplete; validation status was not saved.")
    metadata["validation_status"] = "passed" if quality.get("passed") else "failed"
    metadata["validation_summary"] = {
        "eligible_session_count": quality.get("eligible_session_count", 0),
        "missing_candle_count": quality.get("missing_candle_count", 0),
        "duplicate_timestamp_count": quality.get("duplicate_timestamp_count", 0),
        "invalid_ohlc_count": quality.get("invalid_ohlc_count", 0),
        "off_session_row_count": quality.get("off_session_row_count", 0),
        "invalid_volume_count": quality.get("invalid_volume_count", 0),
    }
    metadata["validated_csv_sha256"] = csv_hash
    metadata["validated_original_download_sha256"] = original_hash
    metadata["validated_retrieval_id"] = metadata.get("retrieval_id")
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
    session_calendar, calendar_sha256, calendar_error = _load_session_calendar(
        config.session_calendar
    )
    if calendar_error:
        details.append(calendar_error)
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
        calendar_type = (
            "holiday" if session_date in session_calendar["holidays"]
            else "special_session" if session_date in session_calendar["special_sessions"]
            else "configured_regular_session"
            if session_date in session_calendar["regular_sessions"]
            else "observed_session_calendar_unconfirmed"
        )
        by_time: dict[local_time, list[Bar]] = defaultdict(list)
        for row in rows:
            by_time[row.timestamp.astimezone(KOLKATA).time()].append(row)
        missing = (
            []
            if calendar_type in {"holiday", "special_session"}
            else [at.strftime("%H:%M") for at in expected_times if len(by_time[at]) == 0]
        )
        repeated = (
            []
            if calendar_type in {"holiday", "special_session"}
            else [at.strftime("%H:%M") for at in expected_times if len(by_time[at]) > 1]
        )
        missing_total += len(missing)
        reasons = []
        if calendar_type == "holiday":
            reasons.append("provider returned rows for a date configured as an exchange holiday")
        if calendar_type == "special_session":
            reasons.append("special session excluded; regular-session ORB schedule is not applicable")
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
            calendar_type not in {"holiday", "special_session"}
            and len(opening) == 6
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
            "calendar_classification": calendar_type,
        })
    if not eligible_dates:
        details.append("No complete, valid 09:15–15:30 IST trading sessions were available.")
    weekday_dates_without_bars = []
    missing_session_classifications = []
    candidate = config.start
    while candidate < config.end:
        if candidate.weekday() < 5 and candidate not in grouped:
            weekday_dates_without_bars.append(candidate.isoformat())
            if candidate in session_calendar["holidays"]:
                classification = "configured_exchange_holiday"
            elif candidate in session_calendar["special_sessions"]:
                classification = "configured_special_session_no_data"
            elif candidate in session_calendar["regular_sessions"]:
                classification = "configured_regular_session_provider_gap"
            else:
                classification = "unresolved_holiday_special_session_or_provider_gap"
            missing_session_classifications.append({
                "date": candidate.isoformat(),
                "classification": classification,
            })
        candidate += timedelta(days=1)
    calendar_note = (
        "Session calendar classifications use the optional user-supplied JSON file when configured. "
        "Otherwise observed dates are checked against the regular 09:15–15:30 Asia/Kolkata schedule, "
        "and weekdays without rows remain unresolved; they are not presumed holidays or provider failures. "
        "Special sessions require explicit calendar entries and are excluded from this regular-session strategy."
    )
    has_global_error = bool(
        duplicate_count or off_session_count
        or outside_requested_range
        or calendar_error
        or any(
            session_date in session_calendar["holidays"]
            for session_date in grouped
        )
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
            or "Configured session calendar" in item
            or "Configured session calendar assigns" in item
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
        "missing_session_classifications": missing_session_classifications,
        "session_calendar": {
            "path": str(config.session_calendar) if config.session_calendar else None,
            "sha256": calendar_sha256,
            "holidays": sorted(value.isoformat() for value in session_calendar["holidays"]),
            "special_sessions": sorted(value.isoformat() for value in session_calendar["special_sessions"]),
            "regular_sessions": sorted(value.isoformat() for value in session_calendar["regular_sessions"]),
            "error": calendar_error,
        },
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
        "manifest_schema_version": (metadata or {}).get("manifest_schema_version"),
        "cache_schema_version": (metadata or {}).get("cache_schema_version"),
        "cache_provenance": (metadata or {}).get("cache_provenance"),
        "cache_integrity_status": (metadata or {}).get("cache_integrity_status", "not_verified"),
        "cache_age_seconds": (metadata or {}).get("cache_age_seconds"),
        "cache_age_days": (metadata or {}).get("cache_age_days"),
        "cache_max_age_days": (
            (metadata or {}).get("cache_max_age_days", config.max_cache_age_days)
        ),
        "cache_freshness_status": (
            (metadata or {}).get("cache_freshness_status", "not_applicable")
        ),
        "stale_cache_override": (metadata or {}).get("stale_cache_override", False),
        "cache_csv_sha256": (metadata or {}).get("csv_sha256"),
        "cache_original_download_sha256": (
            (metadata or {}).get("original_download_sha256")
        ),
        "cache_retrieval_id": (metadata or {}).get("retrieval_id"),
        "validation_status": "passed" if passed else "failed",
        "coverage_status": (
            "complete_for_observed_sessions"
            if passed and not missing_total
            else "partial_or_failed"
        ),
        "backtest_run": False,
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
