"""Validation of Groww historical candles and contract metadata for the options backtest.

Groww candle timestamps carry no timezone. They are compared only as provider-supplied
wall-clock values. A session is usable only when its candles sit on the NSE 09:15-15:25
selected-interval grid for the requested date; a data-shape check, not authoritative proof
of the provider's timezone.
"""

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Sequence

from ..brokers.base import HistoricalCandle
from .config import INTERVAL_MINUTES, SESSION_CLOSE, SESSION_OPEN

SESSION_MINUTES = (SESSION_CLOSE.hour * 60 + SESSION_CLOSE.minute) - (
    SESSION_OPEN.hour * 60 + SESSION_OPEN.minute
)
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_CONTRACT_RE = re.compile(
    r"^NSE-NIFTY-(\d{2})([A-Za-z]{3})(\d{2})-(\d{1,6}(?:\.\d{1,2})?)-(CE|PE)$"
)


@dataclass(frozen=True)
class OptionContract:
    groww_symbol: str
    expiry: date
    strike: float
    option_type: str


@dataclass
class CandleValidation:
    candles: list[HistoricalCandle]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    no_data: bool = False
    missing_count: int = 0

    @property
    def usable(self) -> bool:
        return not self.errors and bool(self.candles)


def format_expiry_token(expiry: date) -> str:
    return f"{expiry.day:02d}{_MONTHS[expiry.month - 1]}{expiry.year % 100:02d}"


def contract_symbol(expiry: date, strike: int, option_type: str) -> str:
    return f"NSE-NIFTY-{format_expiry_token(expiry)}-{strike}-{option_type}"


def parse_contract_symbol(symbol: str) -> OptionContract | None:
    match = _CONTRACT_RE.fullmatch(symbol)
    if match is None:
        return None
    day, month_text, year, strike, option_type = match.groups()
    try:
        month = _MONTHS.index(month_text.title()) + 1
        expiry = date(2000 + int(year), month, int(day))
        strike_value = float(strike)
    except ValueError:
        return None
    if strike_value <= 0:
        return None
    return OptionContract(symbol, expiry, strike_value, option_type)


def select_contract(
    contracts: Sequence[str], expiry: date, strike: int, option_type: str
) -> tuple[OptionContract | None, int]:
    """Return the exact listed contract and a count of returned symbols inconsistent with ``expiry``.

    Never substitutes a different expiry or strike.
    """
    found: OptionContract | None = None
    inconsistent = 0
    for symbol in contracts:
        parsed = parse_contract_symbol(symbol)
        if parsed is None or parsed.expiry != expiry:
            inconsistent += 1
            continue
        if parsed.strike == float(strike) and parsed.option_type == option_type:
            found = parsed
    return found, inconsistent


def describe_listing(contracts: Sequence[str], expiry: date, strike: int, option_type: str) -> str:
    """Market-identifier-only facts explaining why an exact contract was not found."""
    parsed = [parse_contract_symbol(symbol) for symbol in contracts]
    unparsed = [symbol for symbol, item in zip(contracts, parsed) if item is None]
    same_expiry = [item for item in parsed if item is not None and item.expiry == expiry]
    same_type = sorted({item.strike for item in same_expiry if item.option_type == option_type})
    other_expiries = sorted({item.expiry.isoformat() for item in parsed if item is not None and item.expiry != expiry})
    sample = [f"{value:g}" for value in same_type[:5] + same_type[-5:]] if len(same_type) > 10 else [f"{v:g}" for v in same_type]
    text = (
        f"[diagnostic: requested {option_type} {strike} expiry {expiry.isoformat()} "
        f"(expected symbol {contract_symbol(expiry, strike, option_type)}); Groww returned {len(contracts)} symbol(s), "
        f"{len(same_expiry)} for that expiry, {len(same_type)} {option_type} strike(s) for it"
    )
    if sample:
        text += f" (lowest/highest sample: {', '.join(sample)})"
    if other_expiries:
        text += f"; other expiries present: {', '.join(other_expiries[:5])}"
    if unparsed:
        text += f"; {len(unparsed)} unparseable symbol(s), e.g. {unparsed[0][:80]!r}"
    return text + "; see the Groww log tab for the raw response]"


def validate_session_candles(
    rows: Sequence[HistoricalCandle],
    session_date: date,
    label: str,
    *,
    volume_required: bool = True,
    interval_minutes: int = INTERVAL_MINUTES,
) -> CandleValidation:
    """Validate one session of interval-aligned candles without sorting or filling them."""
    if interval_minutes not in (1, 5, 10):
        raise ValueError("interval_minutes must be 1, 5 or 10.")
    result = CandleValidation(candles=list(rows))
    if not rows:
        result.no_data = True
        result.errors.append(f"{label}: no candles were returned.")
        return result

    if _is_final_1530_row(rows, session_date):
        result.warnings.append(
            f"{label}: excluded the final {SESSION_CLOSE.strftime('%H:%M')} candle by the backtest "
            "session boundary; only candles on the 09:15-15:25 grid are used."
        )
        rows = rows[:-1]
        result.candles = list(rows)

    stamps = [row.timestamp for row in rows]
    if any(stamp.tzinfo is not None for stamp in stamps):
        result.errors.append(f"{label}: timestamps carried an unexpected timezone offset.")
        return result
    if len(set(stamps)) != len(stamps):
        result.errors.append(f"{label}: duplicate candle timestamps.")
    if stamps != sorted(stamps):
        result.errors.append(f"{label}: candles are not in chronological order.")
    if any(stamp.date() != session_date for stamp in stamps):
        result.errors.append(f"{label}: candles fall outside the requested session date.")
    if any(not _on_session_grid(stamp, interval_minutes) for stamp in stamps):
        interval_label = {1: "one-minute", 5: "five-minute", 10: "ten-minute"}[interval_minutes]
        result.errors.append(
            f"{label}: timestamps are not on the {interval_label} grid from 09:15 through the last "
            "tradable candle before 15:30; the provider's "
            "timezone/semantics cannot be confirmed, so the session was not used. "
            + _grid_diagnostic(rows, interval_minutes)
        )

    invalid = 0
    for row in rows:
        prices = (row.open, row.high, row.low, row.close)
        if (
            any(not math.isfinite(value) or value <= 0 for value in prices)
            or (row.volume is None and volume_required)
            or (row.volume is not None and (not math.isfinite(row.volume) or row.volume < 0))
            or row.high < max(row.open, row.close, row.low)
            or row.low > min(row.open, row.close, row.high)
            or (
                row.open_interest is not None
                and (not math.isfinite(row.open_interest) or row.open_interest < 0)
            )
        ):
            invalid += 1
    if invalid:
        result.errors.append(f"{label}: {invalid} candle(s) had invalid or non-positive OHLC/volume values.")

    expected_candles = (SESSION_MINUTES + interval_minutes - 1) // interval_minutes
    unique_on_grid = {stamp for stamp in stamps if _on_session_grid(stamp, interval_minutes)}
    result.missing_count = max(0, expected_candles - len(unique_on_grid))
    if result.missing_count and not result.errors:
        result.warnings.append(
            f"{label}: {result.missing_count} of {expected_candles} expected "
            f"{interval_minutes}-minute candles are missing; gaps were not filled."
        )
    return result


def _is_final_1530_row(rows: Sequence[HistoricalCandle], session_date: date) -> bool:
    """Recognize the final 15:30 row for exclusion by the explicit backtest session boundary."""
    return (
        bool(rows)
        and rows[-1].timestamp.time() == SESSION_CLOSE
        and rows[-1].timestamp.date() == session_date
    )


def _grid_diagnostic(rows: Sequence[HistoricalCandle], interval_minutes: int = INTERVAL_MINUTES) -> str:
    """Compact raw-candle sample (market data only) to diagnose provider timestamp conventions."""

    def show(row: HistoricalCandle) -> str:
        return f"{row.timestamp.isoformat()} O={row.open} H={row.high} L={row.low} C={row.close} V={row.volume}"

    off_grid = [row for row in rows if not _on_session_grid(row.timestamp, interval_minutes)]
    return (
        f"[diagnostic: {len(rows)} candles; first 3: {[show(r) for r in rows[:3]]}; "
        f"last 3: {[show(r) for r in rows[-3:]]}; "
        f"{len(off_grid)} off-grid, first 5: {[r.timestamp.isoformat() for r in off_grid[:5]]}]"
    )


def _on_session_grid(stamp: datetime, interval_minutes: int = INTERVAL_MINUTES) -> bool:
    if stamp.second or stamp.microsecond:
        return False
    clock = stamp.time()
    if clock < SESSION_OPEN or clock >= SESSION_CLOSE:
        return False
    offset = (clock.hour * 60 + clock.minute) - (SESSION_OPEN.hour * 60 + SESSION_OPEN.minute)
    return offset % interval_minutes == 0
