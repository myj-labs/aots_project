"""Groww-backed data resolution and background run management for the options backtest.

Read-only: only historical expiry, contract and candle lookups are used. Nothing here
touches the paper ledger, risk state, account balance or any order endpoint.
"""

from __future__ import annotations

import threading
import time as _time
import uuid
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..provider_session import ProviderRequestError
from .config import OptionsBacktestConfig
from .data import (
    OptionContract,
    contract_symbol,
    describe_listing,
    parse_contract_symbol,
    select_contract,
    validate_session_candles,
)
from .engine import (
    OptionUnavailable,
    ResolvedOption,
    TIMESTAMP_NOTE,
    run_session,
    summarize,
)

IST = ZoneInfo("Asia/Kolkata")
MAX_RUNS_KEPT = 10
_ERROR_CATEGORIES = {
    401: "provider_auth",
    403: "provider_denied",
    409: "provider_not_connected",
    422: "configuration",
    429: "provider_rate_limited",
    502: "provider_response",
    503: "provider_unavailable",
}
_UNDERLYING_SYMBOL = "NSE-NIFTY"


class RunRejected(Exception):
    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


class GrowwOptionResolver:
    """Resolves exact listed contracts and their validated premium candles with caching."""

    def __init__(self, source: Any, config: OptionsBacktestConfig, sleep: Callable[[float], None] = _time.sleep, pace_seconds: float = 0.0):
        self._source = source
        self._config = config
        self._sleep = sleep
        self._pace = pace_seconds
        self._expiries: dict[tuple[int, int], list[date]] = {}
        self._contracts: dict[date, list[str]] = {}
        self._candles: dict[tuple[str, date], Any] = {}

    def _pause(self) -> None:
        if self._pace:
            self._sleep(self._pace)

    def _fetch(self, label, call, *args):
        """Run one read-only provider call; add a safe stage label (never payload data) to failures."""
        self._pause()
        try:
            return call(*args)
        except ProviderRequestError as exc:
            raise ProviderRequestError(f"{exc} [stage: {label}]", exc.status_code) from None

    def underlying(self, session_date: date):
        start = datetime.combine(session_date, datetime.min.time()).replace(hour=9, minute=15)
        end = start.replace(hour=15, minute=30)
        return self._fetch(
            f"NIFTY index candles {session_date.isoformat()}",
            self._source.historical_candles, "CASH", _UNDERLYING_SYMBOL, start, end, self._config.candle_interval,
        )

    def _month_expiries(self, year: int, month: int) -> list[date]:
        key = (year, month)
        if key not in self._expiries:
            raw = self._fetch(f"expiries {year}-{month:02d}", self._source.historical_expiries, year, month)
            parsed = []
            for item in raw:
                try:
                    parsed.append(date.fromisoformat(item))
                except (TypeError, ValueError):
                    continue
            self._expiries[key] = sorted(set(parsed))
        return self._expiries[key]

    def _pick_expiry(self, session_date: date) -> date:
        earliest = session_date + timedelta(days=self._config.min_days_to_expiry)
        year, month = session_date.year, session_date.month
        for _ in range(2):
            try:
                found = [d for d in self._month_expiries(year, month) if d >= earliest]
            except ProviderRequestError as exc:
                if exc.status_code == 422:
                    found = []
                else:
                    raise
            if found:
                return found[0]
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        raise OptionUnavailable("expiry_unavailable", "Groww listed no suitable expiry for this session.")

    def _exact_contract(self, expiry: date, strike: int, option_type: str) -> OptionContract:
        """Build the one contract the strategy asked for; refuse anything that does not round-trip exactly."""
        parsed = None
        if option_type in ("CE", "PE") and isinstance(strike, int) and not isinstance(strike, bool) and strike > 0:
            parsed = parse_contract_symbol(contract_symbol(expiry, strike, option_type))
        if parsed is None or parsed.expiry != expiry or parsed.strike != float(strike) or parsed.option_type != option_type:
            raise OptionUnavailable("contract_not_listed", f"Cannot form a valid {option_type} {strike} contract for expiry {expiry.isoformat()}.")
        return parsed

    @staticmethod
    def _not_listed_note(contracts, expiry: date, strike: int, option_type: str) -> str:
        return describe_listing(contracts, expiry, strike, option_type)

    def __call__(self, session_date: date, option_type: str, strike: int, selection_time: datetime) -> ResolvedOption:
        expiry = self._pick_expiry(session_date)
        if expiry not in self._contracts:
            self._contracts[expiry] = self._fetch(
                f"contracts for expiry {expiry.isoformat()}", self._source.historical_contracts, expiry.isoformat()
            )
        contract, inconsistent = select_contract(self._contracts[expiry], expiry, strike, option_type)
        listed = contract is not None
        listing_note = ""
        if contract is None:
            # The contracts endpoint can omit contracts that have candles. Verify the exact strategy-derived
            # contract by requesting its own candles; nothing else is substituted.
            contract = self._exact_contract(expiry, strike, option_type)
            listing_note = self._not_listed_note(self._contracts[expiry], expiry, strike, option_type)
        key = (contract.groww_symbol, session_date)
        if key not in self._candles:
            start = datetime.combine(session_date, datetime.min.time()).replace(hour=9, minute=15)
            try:
                rows = self._fetch(
                    f"option candles {contract.groww_symbol} {session_date.isoformat()}",
                    self._source.historical_candles,
                    "FNO", contract.groww_symbol, start, start.replace(hour=15, minute=30), self._config.candle_interval,
                )
            except ProviderRequestError as exc:
                if listed or exc.status_code != 422:
                    raise
                raise OptionUnavailable(
                    "contract_candles_unavailable",
                    f"{contract.groww_symbol} is not listed by the Groww contracts API and Groww rejected its "
                    f"historical candle request, so it could not be verified. {listing_note}",
                ) from None
            self._candles[key] = validate_session_candles(
                rows,
                session_date,
                f"{contract.groww_symbol} option candles",
                interval_minutes=self._config.interval_minutes,
            )
        checked = self._candles[key]
        if checked.no_data:
            origin = "" if listed else " (not listed by the Groww contracts API either)"
            raise OptionUnavailable("option_no_data", f"Groww returned no historical candles for {contract.groww_symbol}{origin}.")
        if not checked.usable:
            raise OptionUnavailable("option_data_invalid", "; ".join(checked.errors))
        warnings = list(checked.warnings)
        if not listed:
            warnings.append(
                f"{contract.groww_symbol} was not in the Groww contracts list for {expiry.isoformat()}; it was used "
                "because its exact historical candles were returned and validated for this session."
            )
        if inconsistent:
            warnings.append(f"{inconsistent} returned contract symbol(s) were malformed or did not match the expiry and were ignored.")
        rule = f"Nearest Groww-listed expiry on or after session date + {self._config.min_days_to_expiry} day(s)."
        return ResolvedOption(contract, checked.candles, rule, warnings)


def weekdays(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if day.weekday() < 5:
            out.append(day)
        day += timedelta(days=1)
    return out


def execute_backtest(
    source: Any,
    config: OptionsBacktestConfig,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_progress: Callable[[int, int, date], None] = lambda done, total, day: None,
    sleep: Callable[[float], None] = _time.sleep,
    pace_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run all sessions sequentially. Provider errors propagate and fail the whole run."""
    resolver = GrowwOptionResolver(source, config, sleep, pace_seconds)
    dates = weekdays(config.start_date, config.end_date)
    equity = config.initial_capital_inr
    sessions: list[dict[str, Any]] = []
    stopped = False
    for index, day in enumerate(dates):
        if should_stop():
            stopped = True
            break
        on_progress(index, len(dates), day)
        rows = resolver.underlying(day)
        session = run_session(day, rows, config, resolver, equity)
        entry = session.as_dict()
        equity += sum(t["net_pnl"] for t in entry["trades"])
        sessions.append(entry)
    on_progress(len(sessions), len(dates), config.end_date)
    summary = summarize(sessions, config, dates)
    summary["stopped_early"] = stopped
    skipped = [
        {"session_date": s["session_date"], **sig}
        for s in sessions
        for sig in s["signals"]
        if sig["outcome"] != "traded"
    ]
    return {
        "summary": summary,
        "trades": [t for s in sessions for t in s["trades"]],
        "sessions": sessions,
        "skipped_signals": skipped,
        "config": config.model_dump(mode="json"),
        "rules": config.rules_description(),
        "data_source": {
            "provider": "Groww (read-only historical API)",
            "underlying_symbol": _UNDERLYING_SYMBOL,
            "underlying_segment": "CASH",
            "option_segment": "FNO",
            "candle_interval": config.candle_interval,
            "timestamp_note": TIMESTAMP_NOTE,
        },
        "limitations": [
            "Premiums are Groww historical candles; this run was not validated against another source.",
            "Lot size is user-supplied and applied to every session in the run.",
            "Option tick-size rounding, bid/ask spread and liquidity are not modelled.",
            "Weekdays without candles are excluded as no_data; they may be exchange holidays.",
            "Excluded or incomplete sessions can bias results.",
            "Candle timestamp semantics (start vs end) are assumed to be candle-start and are unverified.",
        ],
    }


class OptionsBacktestService:
    """One background run at a time; finished runs are kept in memory only."""

    def __init__(self, get_source: Callable[[], Any], clock: Callable[[], datetime] | None = None, pace_seconds: float = 0.2, sleep: Callable[[float], None] = _time.sleep, run_in_thread: bool = True):
        self._get_source = get_source
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pace = pace_seconds
        self._sleep = sleep
        self._threaded = run_in_thread
        self._lock = threading.Lock()
        self._runs: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._stops: dict[str, threading.Event] = {}
        self._threads: dict[str, threading.Thread] = {}

    def start(self, config: OptionsBacktestConfig) -> dict[str, Any]:
        today = self._clock().astimezone(IST).date()
        if config.end_date >= today:
            raise RunRejected("end_date must be before today (Asia/Kolkata); only completed sessions can be backtested.")
        source = self._get_source()
        with self._lock:
            if any(r["state"] == "running" for r in self._runs.values()):
                raise RunRejected("An options backtest is already running.", 409)
            run_id = uuid.uuid4().hex[:12]
            run = {
                "run_id": run_id,
                "state": "running",
                "created_at": self._clock().isoformat(),
                "finished_at": None,
                "config": config.model_dump(mode="json"),
                "progress": {"done": 0, "total": len(weekdays(config.start_date, config.end_date)), "current_date": None},
                "error": None,
                "result": None,
            }
            self._runs[run_id] = run
            while len(self._runs) > MAX_RUNS_KEPT:
                oldest = next((k for k, v in self._runs.items() if v["state"] != "running"), None)
                if oldest is None:
                    break
                self._runs.pop(oldest)
                self._stops.pop(oldest, None)
                self._threads.pop(oldest, None)
            stop = threading.Event()
            self._stops[run_id] = stop
        if self._threaded:
            thread = threading.Thread(target=self._work, args=(run_id, source, config, stop), daemon=True)
            with self._lock:
                self._threads[run_id] = thread
            thread.start()
        else:
            self._work(run_id, source, config, stop)
        return self._public(run_id, include_result=False)

    def _work(self, run_id: str, source: Any, config: OptionsBacktestConfig, stop: threading.Event) -> None:
        def progress(done: int, total: int, day: date) -> None:
            with self._lock:
                self._runs[run_id]["progress"] = {"done": done, "total": total, "current_date": day.isoformat()}

        try:
            result = execute_backtest(source, config, should_stop=stop.is_set, on_progress=progress, sleep=self._sleep, pace_seconds=self._pace)
            state, error = ("stopped" if result["summary"]["stopped_early"] else "completed"), None
        except ProviderRequestError as exc:
            result, state = None, "failed"
            error = {"category": _ERROR_CATEGORIES.get(exc.status_code, "execution_error"), "status_code": exc.status_code, "message": str(exc)}
        except Exception:
            result, state = None, "failed"
            error = {"category": "execution_error", "status_code": 500, "message": "The backtest failed unexpectedly; see server logs."}
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.update(state=state, error=error, result=result, finished_at=self._clock().isoformat())

    def _public(self, run_id: str, include_result: bool) -> dict[str, Any]:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise KeyError(run_id)
            out = {k: v for k, v in run.items() if k != "result"}
            if include_result:
                out["result"] = run["result"]
            return out

    def get(self, run_id: str) -> dict[str, Any]:
        return self._public(run_id, include_result=True)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._runs)
        return [self._public(i, include_result=False) for i in reversed(ids)]

    def stop(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(run_id)
            event = self._stops.get(run_id)
        if event is not None:
            event.set()
        return self._public(run_id, include_result=False)

    def shutdown(self) -> None:
        with self._lock:
            events = list(self._stops.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=2)


__all__ = ["GrowwOptionResolver", "OptionsBacktestService", "RunRejected", "execute_backtest", "weekdays", "OptionContract"]
