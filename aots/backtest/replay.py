"""In-memory historical replay sessions using the shared ORB executor."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from threading import Event, Lock, RLock, Thread
from typing import Any, Callable
from uuid import uuid4

from .data import (
    BacktestConfig,
    DataAcquisitionError,
    DataValidationError,
    ValidatedData,
    finalize_cache_validation,
    load_or_download,
    validate_bars,
)
from .engine import BacktestResult, BacktestRunner


MAX_REPLAY_SESSIONS = 20
MAX_ACTIVE_REPLAYS = 3
MAX_REPLAY_EVENTS = 500


class ReplayNotFound(LookupError):
    """Raised when a replay ID is not present in this process."""


class ReplayCapacityError(RuntimeError):
    """Raised when the bounded replay service has no capacity."""


def load_validated_data(
    config: BacktestConfig,
    *,
    offline: bool = False,
    allow_stale_cache: bool = False,
) -> ValidatedData:
    bars, metadata = load_or_download(
        config,
        offline=offline,
        allow_stale_cache=allow_stale_cache,
    )
    validated = validate_bars(bars, config, metadata)
    try:
        finalize_cache_validation(config, validated.quality)
    except DataAcquisitionError as exc:
        raise DataAcquisitionError(
            f"Cache validation could not be finalized: {exc}"
        ) from exc
    if not validated.quality.get("passed"):
        raise DataValidationError(
            "Historical data failed the quality gate; no replay was run.",
            validated.quality,
        )
    return validated


@dataclass
class _ReplaySession:
    session_id: str
    config: BacktestConfig
    offline: bool
    allow_stale_cache: bool
    created_at: str
    status: str = "validating"
    processed_bars: int = 0
    total_bars: int = 0
    current_timestamp: str | None = None
    error: str | None = None
    quality_errors: list[str] = field(default_factory=list)
    data_quality: dict[str, Any] | None = None
    open_position: dict[str, Any] | None = None
    metrics: dict[str, Any] | None = None
    signals: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    events: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=MAX_REPLAY_EVENTS)
    )
    next_event_sequence: int = 1
    dropped_events: int = 0
    stop_requested: Event = field(default_factory=Event)
    lock: RLock = field(default_factory=RLock)
    worker: Thread | None = None


class ReplayService:
    """Runs bounded, isolated historical replays without touching paper state."""

    def __init__(
        self,
        *,
        loader: Callable[..., ValidatedData] = load_validated_data,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._loader = loader
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sessions: dict[str, _ReplaySession] = {}
        self._lock = RLock()
        self._data_load_lock = Lock()
        self._closing = False

    def start(
        self,
        config: BacktestConfig,
        *,
        offline: bool = False,
        allow_stale_cache: bool = False,
    ) -> dict[str, Any]:
        if allow_stale_cache and not offline:
            raise ValueError("Stale-cache override is only available with offline mode.")
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Replay service clock must return a timezone-aware timestamp.")
        with self._lock:
            if self._closing:
                raise ReplayCapacityError("Replay service is shutting down.")
            active = sum(
                session.status in {"validating", "running", "stopping"}
                for session in self._sessions.values()
            )
            if active >= MAX_ACTIVE_REPLAYS:
                raise ReplayCapacityError("Maximum concurrent historical replays reached.")
            self._make_room()
            session = _ReplaySession(
                session_id=str(uuid4()),
                config=config,
                offline=offline,
                allow_stale_cache=allow_stale_cache,
                created_at=now.astimezone(timezone.utc).isoformat(),
            )
            self._sessions[session.session_id] = session
            worker = Thread(
                target=self._run,
                args=(session,),
                name=f"aots-replay-{session.session_id[:8]}",
                daemon=True,
            )
            session.worker = worker
            worker.start()
            with session.lock:
                return self._snapshot(session)

    def status(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        with session.lock:
            return self._snapshot(session)

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = list(self._sessions.values())
        snapshots = []
        for session in sessions:
            with session.lock:
                snapshots.append(self._snapshot(session))
        return snapshots

    def events(self, session_id: str, *, after: int = 0) -> dict[str, Any]:
        session = self._get(session_id)
        with session.lock:
            events = [dict(event) for event in session.events if event["event_seq"] > after]
            return {
                "session_id": session.session_id,
                "events": events,
                "events_dropped": session.dropped_events,
                "next_event_sequence": session.next_event_sequence,
            }

    def results(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        with session.lock:
            return {
                "session_id": session.session_id,
                "status": session.status,
                "data_quality": session.data_quality,
                "signals": list(session.signals),
                "trades": list(session.trades),
                "open_position": session.open_position,
                "metrics": session.metrics,
            }

    def stop(self, session_id: str) -> dict[str, Any]:
        session = self._get(session_id)
        with session.lock:
            if session.status in {"validating", "running"}:
                session.stop_requested.set()
                session.status = "stopping"
            return self._snapshot(session)

    def shutdown(self, *, join_timeout: float = 2.0) -> None:
        with self._lock:
            self._closing = True
            sessions = list(self._sessions.values())
        workers = []
        for session in sessions:
            with session.lock:
                if session.status in {"validating", "running"}:
                    session.stop_requested.set()
                    session.status = "stopping"
                if session.worker is not None:
                    workers.append(session.worker)
        for worker in workers:
            worker.join(timeout=join_timeout)

    def _make_room(self) -> None:
        if len(self._sessions) < MAX_REPLAY_SESSIONS:
            return
        for session_id, session in list(self._sessions.items()):
            if session.status in {"completed", "stopped", "failed"}:
                del self._sessions[session_id]
                return
        raise ReplayCapacityError("Replay session history is full; stop an active replay first.")

    def _get(self, session_id: str) -> _ReplaySession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            raise ReplayNotFound("Historical replay session was not found in this process.")
        return session

    def _run(self, session: _ReplaySession) -> None:
        try:
            with self._data_load_lock:
                with session.lock:
                    if session.stop_requested.is_set():
                        session.status = "stopped"
                        return
                data = self._loader(
                    session.config,
                    offline=session.offline,
                    allow_stale_cache=session.allow_stale_cache,
                )
            if not data.quality.get("passed"):
                raise DataValidationError(
                    "Historical data failed the quality gate; no replay was run.",
                    data.quality,
                )
            runner = BacktestRunner(data.quality, session.config)
            with session.lock:
                if session.stop_requested.is_set():
                    session.status = "stopped"
                    return
                session.data_quality = _quality_summary(data.quality)
                session.total_bars = len(data.bars)
                session.status = "running"
            for bar in data.bars:
                with session.lock:
                    if session.stop_requested.is_set():
                        session.status = "stopped"
                        return
                    events = runner.process_bar(bar)
                    session.processed_bars += 1
                    session.current_timestamp = bar.timestamp.isoformat()
                    session.open_position = runner.open_position
                    self._record_events(session, events)
            with session.lock:
                if session.stop_requested.is_set():
                    session.status = "stopped"
                    return
                result: BacktestResult = runner.finish()
                self._record_events(session, runner.drain_events())
                session.signals = [
                    _as_mapping(value) for value in result.signals
                ]
                session.trades = [
                    _as_mapping(value) for value in result.trades
                ]
                session.metrics = result.metrics
                session.open_position = None
                session.status = "completed"
        except Exception as exc:
            with session.lock:
                session.error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, DataValidationError):
                    session.quality_errors = list(exc.report.get("errors", []))
                session.status = "failed"

    @staticmethod
    def _record_events(
        session: _ReplaySession,
        events: list[dict[str, Any]],
    ) -> None:
        for event in events:
            if len(session.events) == MAX_REPLAY_EVENTS:
                session.dropped_events += 1
            session.events.append({
                "event_seq": session.next_event_sequence,
                **event,
            })
            session.next_event_sequence += 1

    @staticmethod
    def _snapshot(session: _ReplaySession) -> dict[str, Any]:
        percent = (
            session.processed_bars / session.total_bars * 100
            if session.total_bars else 0.0
        )
        return {
            "session_id": session.session_id,
            "status": session.status,
            "requested_start": session.config.start.isoformat(),
            "requested_end_exclusive": session.config.end.isoformat(),
            "created_at": session.created_at,
            "processed_bars": session.processed_bars,
            "total_bars": session.total_bars,
            "progress_percent": round(percent, 2),
            "current_timestamp": session.current_timestamp,
            "error": session.error,
            "quality_errors": list(session.quality_errors),
            "data_quality": session.data_quality,
            "open_position": session.open_position,
            "event_count": session.next_event_sequence - 1,
            "events_dropped": session.dropped_events,
            "metrics": session.metrics,
        }


def _as_mapping(value: Any) -> dict[str, Any]:
    return asdict(value)


def _quality_summary(quality: dict[str, Any]) -> dict[str, Any]:
    fields = (
        "passed",
        "retrieval_mode",
        "retrieved_at_utc",
        "actual_first_timestamp",
        "actual_last_timestamp",
        "row_count",
        "eligible_session_count",
        "coverage_status",
        "cache_freshness_status",
        "cache_age_days",
        "stale_cache_override",
        "timestamp_convention",
    )
    return {field: quality.get(field) for field in fields}
