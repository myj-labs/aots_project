from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from threading import Event

import pytest

import aots.backtest.replay as replay_module
from aots.backtest.data import BacktestConfig, Bar, KOLKATA, ValidatedData
from aots.backtest.replay import ReplayService


def make_data(session_date: date, direction: str = "LONG") -> ValidatedData:
    bars = []
    start = datetime.combine(session_date, time(9, 15), tzinfo=KOLKATA)
    for index in range(75):
        stamp = start + timedelta(minutes=index * 5)
        values = (100.0, 101.0, 99.0, 100.0)
        if index == 6 and direction == "LONG":
            values = (100.5, 102.5, 100.0, 102.0)
        elif index == 6 and direction == "SHORT":
            values = (99.5, 100.0, 97.5, 98.0)
        elif index == 7 and direction == "LONG":
            values = (101.5, 102.5, 100.5, 102.0)
        elif index == 7 and direction == "SHORT":
            values = (98.5, 99.5, 97.5, 98.0)
        elif index == 8 and direction == "LONG":
            values = (102.0, 107.0, 101.0, 106.0)
        elif index == 8 and direction == "SHORT":
            values = (98.0, 99.0, 93.0, 94.0)
        bars.append(Bar(stamp, *values))
    return ValidatedData(
        bars=bars,
        metadata={},
        quality={
            "passed": True,
            "observed_session_count": 1,
            "rejected_sessions": [],
        },
    )


def config_for(session_date: date, tmp_path) -> BacktestConfig:
    return BacktestConfig(
        start=session_date,
        end=session_date + timedelta(days=1),
        data_dir=tmp_path / "cache",
        output_dir=tmp_path / "reports",
    )


def join_worker(service: ReplayService, session_id: str) -> None:
    session = service._get(session_id)
    assert session.worker is not None
    session.worker.join(timeout=5)
    assert not session.worker.is_alive()


def test_replay_sessions_are_isolated_and_emit_chronological_trade_events(tmp_path):
    first_date = date(2026, 10, 20)
    second_date = date(2026, 10, 21)

    def loader(config, **_kwargs):
        return make_data(
            config.start,
            "LONG" if config.start == first_date else "SHORT",
        )

    service = ReplayService(
        loader=loader,
        clock=lambda: datetime(2026, 10, 22, tzinfo=timezone.utc),
    )
    first = service.start(config_for(first_date, tmp_path))
    second = service.start(config_for(second_date, tmp_path))
    join_worker(service, first["session_id"])
    join_worker(service, second["session_id"])

    first_result = service.results(first["session_id"])
    second_result = service.results(second["session_id"])
    first_status = service.status(first["session_id"])
    first_events = service.events(first["session_id"])["events"]
    second_events = service.events(second["session_id"])["events"]

    assert first_result["status"] == second_result["status"] == "completed"
    assert first_status["data_quality"]["passed"] is True
    assert first_result["signals"][0]["direction"] == "LONG"
    assert second_result["signals"][0]["direction"] == "SHORT"
    assert [event["type"] for event in first_events] == ["signal", "entry", "exit"]
    assert [event["event_seq"] for event in first_events] == [1, 2, 3]
    assert first_events[0]["timestamp"] < first_events[1]["timestamp"] < first_events[2]["timestamp"]

    service.shutdown()


def test_stop_during_validation_prevents_any_candle_processing(tmp_path):
    entered_loader = Event()
    release_loader = Event()
    session_date = date(2026, 10, 20)

    def loader(_config, **_kwargs):
        entered_loader.set()
        assert release_loader.wait(timeout=5)
        return make_data(session_date)

    service = ReplayService(loader=loader)
    started = service.start(config_for(session_date, tmp_path))
    assert entered_loader.wait(timeout=5)

    stopped = service.stop(started["session_id"])
    assert stopped["status"] == "stopping"
    release_loader.set()
    join_worker(service, started["session_id"])

    final = service.status(started["session_id"])
    assert final["status"] == "stopped"
    assert final["processed_bars"] == 0
    assert service.results(started["session_id"])["metrics"] is None
    service.shutdown()


def test_event_history_is_bounded_and_reports_dropped_events(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_module, "MAX_REPLAY_EVENTS", 2)
    session_date = date(2026, 10, 20)
    service = ReplayService(loader=lambda _config, **_kwargs: make_data(session_date))
    started = service.start(config_for(session_date, tmp_path))
    join_worker(service, started["session_id"])

    batch = service.events(started["session_id"])
    assert batch["events_dropped"] == 1
    assert [event["event_seq"] for event in batch["events"]] == [2, 3]
    service.shutdown()


def test_failed_data_load_is_visible_and_never_returns_results(tmp_path):
    session_date = date(2026, 10, 20)

    def broken_loader(_config, **_kwargs):
        raise RuntimeError("validated cache unavailable")

    service = ReplayService(loader=broken_loader)
    started = service.start(config_for(session_date, tmp_path))
    join_worker(service, started["session_id"])

    status = service.status(started["session_id"])
    assert status["status"] == "failed"
    assert "validated cache unavailable" in status["error"]
    assert service.results(started["session_id"])["metrics"] is None
    service.shutdown()


def test_quality_failure_prevents_runner_and_exposes_validation_errors(tmp_path):
    session_date = date(2026, 10, 20)
    invalid = ValidatedData(
        bars=make_data(session_date).bars,
        metadata={},
        quality={
            "passed": False,
            "errors": ["configured regular session has no complete bars"],
            "rejected_sessions": [],
        },
    )
    service = ReplayService(loader=lambda _config, **_kwargs: invalid)
    started = service.start(config_for(session_date, tmp_path))
    join_worker(service, started["session_id"])

    status = service.status(started["session_id"])
    assert status["status"] == "failed"
    assert status["quality_errors"] == ["configured regular session has no complete bars"]
    assert service.results(started["session_id"])["metrics"] is None
    service.shutdown()


def test_stale_cache_override_is_rejected_without_offline_mode(tmp_path):
    session_date = date(2026, 10, 20)
    service = ReplayService(loader=lambda _config, **_kwargs: make_data(session_date))

    with pytest.raises(ValueError, match="only available with offline mode"):
        service.start(
            config_for(session_date, tmp_path),
            allow_stale_cache=True,
        )

    service.shutdown()


def test_replay_stop_does_not_change_paper_database_or_use_provider(tmp_path):
    from aots.database import Database
    from aots.paper import PaperBroker
    from aots.risk import RiskEngine
    from types import SimpleNamespace

    settings = SimpleNamespace(
        capital_inr=100_000,
        max_daily_loss_pct=.02,
        max_trade_risk_pct=.005,
        max_consecutive_losses=3,
        max_open_positions=3,
        max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    database = Database(str(tmp_path / "paper.db"))
    risk = RiskEngine(settings, database, clock=lambda: datetime(2026, 10, 20, tzinfo=KOLKATA))
    PaperBroker(settings, database, risk)
    before = (database.trades(), database.events(100), risk.snapshot())

    service = ReplayService(loader=lambda _config, **_kwargs: make_data(date(2026, 10, 20)))
    started = service.start(config_for(date(2026, 10, 20), tmp_path))
    join_worker(service, started["session_id"])
    after = (database.trades(), database.events(100), risk.snapshot())

    assert after == before
    service.shutdown()


@pytest.mark.parametrize(
    ("offline", "allow_stale", "expected"),
    [(False, False, False), (True, False, True), (True, True, True)],
)
def test_replay_passes_explicit_cache_policy_to_loader(
    tmp_path, offline, allow_stale, expected
):
    session_date = date(2026, 10, 20)
    observed = []

    def loader(_config, **kwargs):
        observed.append(kwargs)
        return make_data(session_date)

    service = ReplayService(loader=loader)
    started = service.start(
        config_for(session_date, tmp_path),
        offline=offline,
        allow_stale_cache=allow_stale,
    )
    join_worker(service, started["session_id"])
    assert observed == [{
        "offline": expected,
        "allow_stale_cache": allow_stale,
    }]
    service.shutdown()
