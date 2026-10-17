import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from aots.database import Database, IdempotencyConflict
from aots.models import StrategyEvaluationRequest
from aots.paper import PaperBroker
from aots.risk import RiskEngine
from aots.strategy import (
    MarketSnapshot,
    SpotThresholdStrategy,
    StrategyCandidate,
    evaluate_spot_threshold,
)
from aots.strategy_service import StrategyService


NOW = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)


def strategy_services(tmp_path, **overrides):
    values = dict(
        capital_inr=100_000,
        max_daily_loss_pct=.02,
        max_trade_risk_pct=.005,
        max_consecutive_losses=3,
        max_open_positions=3,
        max_lots_per_order=1,
        market_data_max_age_seconds=5,
        strategy_spot_threshold=25_000,
        underlying="NIFTY",
    )
    values.update(overrides)
    settings = SimpleNamespace(**values)
    db = Database(str(tmp_path / "strategy.db"))
    risk = RiskEngine(settings, db, clock=lambda: NOW)
    paper = PaperBroker(settings, db, risk)
    return settings, db, risk, paper, StrategyService(settings, db, risk, paper)


def strategy_request(observation_id="obs-1", **overrides):
    values = dict(
        observation_id=observation_id,
        underlying="NIFTY",
        spot=25_100,
        observed_at=NOW.isoformat(),
        symbol="DEMO-OPTION",
        entry_price=100,
        stop_price=95,
        quantity=10,
        lots=1,
        take_profit_profit_inr=100,
    )
    values.update(overrides)
    return StrategyEvaluationRequest(**values)


def test_threshold_strategy_returns_signal_and_no_signal_deterministically():
    candidate = StrategyCandidate("DEMO", 100, 95, 10, 1, 100)
    kwargs = dict(
        observation_id="obs",
        underlying="NIFTY",
        observed_at=NOW,
        candidate=candidate,
        threshold=25_000,
        expected_underlying="NIFTY",
        evaluated_at=NOW,
        max_age_seconds=5,
    )
    signal = evaluate_spot_threshold(spot=25_100, **kwargs)
    same_signal = evaluate_spot_threshold(spot=25_100, **kwargs)
    no_signal = evaluate_spot_threshold(spot=24_999, **kwargs)

    assert signal.status == "SIGNAL"
    assert signal.signal["side"] == "BUY"
    assert signal.signal["entry_price"] == 100
    assert signal.signal["take_profit_profit_inr"] == 100
    assert signal.as_dict() == same_signal.as_dict()
    assert no_signal.status == "NO_SIGNAL"
    assert no_signal.signal is None


@pytest.mark.parametrize(
    "snapshot",
    [
        MarketSnapshot("NIFTY", float("nan"), NOW.timestamp(), []),
        MarketSnapshot("NIFTY", 25_100, float("inf"), []),
        MarketSnapshot("NIFTY", 25_100, NOW.timestamp(), [{}]),
        MarketSnapshot("NIFTY", 25_100, NOW.timestamp(), [{
            "symbol": "DEMO",
            "entry_price": 100,
            "stop_price": 100,
            "quantity": 1,
            "lots": 1,
        }]),
    ],
)
def test_strategy_protocol_fails_closed_for_invalid_snapshot_candidate(snapshot):
    assert SpotThresholdStrategy(25_000).evaluate(snapshot) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"spot": 0},
        {"spot": float("nan")},
        {"entry_price": float("inf")},
        {"quantity": 0},
        {"lots": 0},
        {"take_profit_profit_inr": 0},
        {"stop_price": 100},
        {"observed_at": "2025-01-03T04:00:00"},
        {"observation_id": " obs"},
    ],
)
def test_invalid_or_missing_strategy_inputs_fail_validation(overrides):
    with pytest.raises(ValidationError):
        strategy_request(**overrides)


def test_missing_required_strategy_input_fails_validation():
    values = strategy_request().model_dump()
    values.pop("spot")
    with pytest.raises(ValidationError):
        StrategyEvaluationRequest(**values)


def test_evaluation_is_pure_and_rejects_stale_future_or_unconfigured_inputs(tmp_path):
    settings, db, risk, paper, service = strategy_services(tmp_path)
    before_events = db.events()
    before_trades = db.trades()

    result = service.evaluate(strategy_request())
    stale = service.evaluate(
        strategy_request(observed_at=(NOW - timedelta(seconds=6)).isoformat())
    )
    future = service.evaluate(
        strategy_request(observed_at=(NOW + timedelta(seconds=1)).isoformat())
    )
    unconfigured = StrategyService(
        SimpleNamespace(**{**vars(settings), "strategy_spot_threshold": None}),
        db,
        risk,
        paper,
    ).evaluate(strategy_request())

    assert result["status"] == "SIGNAL"
    assert stale["status"] == "REJECTED"
    assert "stale" in stale["reason"]
    assert future["status"] == "REJECTED"
    assert "future" in future["reason"]
    assert unconfigured["status"] == "REJECTED"
    assert db.events() == before_events
    assert db.trades() == before_trades
    assert db.strategy_decisions() == []


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("kill", "Kill switch"),
        ("paused", "paused"),
        ("daily_loss", "Daily loss limit"),
        ("positions", "Maximum open positions"),
        ("lots", "max lots"),
        ("trade_risk", "exceeds limit"),
    ],
)
def test_authoritative_risk_controls_reject_strategy_entries(
    tmp_path, case, expected_reason
):
    settings, db, risk, paper, service = strategy_services(
        tmp_path,
        max_open_positions=1 if case == "positions" else 3,
    )
    request = strategy_request(
        lots=2 if case == "lots" else 1,
        quantity=100 if case == "trade_risk" else 10,
        stop_price=90 if case == "trade_risk" else 95,
    )
    if case == "kill":
        risk.set_kill_switch(True)
    elif case == "paused":
        for _ in range(settings.max_consecutive_losses):
            risk.record_closed_trade(-1)
    elif case == "daily_loss":
        db.insert_trade({
            "trade_id": "loss",
            "symbol": "DEMO",
            "side": "BUY",
            "entry_price": 100,
            "stop_price": 95,
            "mark_price": 100,
            "quantity": 1,
            "lots": 1,
            "status": "CLOSED",
            "realised_pnl": -2_000,
            "unrealised_pnl": 0,
            "created_at": NOW.isoformat(),
            "closed_at": NOW.isoformat(),
        })
    elif case == "positions":
        from aots.models import PaperTradeRequest

        paper.open_trade(PaperTradeRequest(
            symbol="EXISTING",
            idempotency_key="existing",
            entry_price=100,
            stop_price=95,
            quantity=1,
            lots=1,
            market_data_age_seconds=0,
        ))

    decision = service.process(request)

    assert decision["status"] == "RISK_REJECTED"
    assert expected_reason.lower() in decision["reason"].lower()
    assert len(db.strategy_decisions()) == 1
    assert sum(t["symbol"] == request.symbol for t in db.trades()) == 0
    if case in {"kill", "paused"}:
        assert risk.snapshot().kill_switch is (case == "kill")


def test_unsupported_underlying_and_below_threshold_process_without_trade(tmp_path):
    _, db, _, _, service = strategy_services(tmp_path)

    unsupported = service.process(
        strategy_request(observation_id="unsupported", underlying="BANKNIFTY")
    )
    no_signal = service.process(
        strategy_request(observation_id="below-threshold", spot=24_999)
    )

    assert unsupported["status"] == "REJECTED"
    assert "configured NIFTY" in unsupported["reason"]
    assert no_signal["status"] == "NO_SIGNAL"
    assert "below configured threshold" in no_signal["reason"]
    assert db.trades() == []
    assert len(db.strategy_decisions()) == 2


def test_stale_strategy_observation_is_persisted_as_rejected_without_trade(tmp_path):
    _, db, _, _, service = strategy_services(tmp_path)

    decision = service.process(
        strategy_request(observed_at=(NOW - timedelta(seconds=6)).isoformat())
    )

    assert decision["status"] == "REJECTED"
    assert not db.trades()
    assert db.strategy_decisions() == [decision]


def test_processing_same_observation_is_idempotent_and_conflicting_input_rejected(
    tmp_path,
):
    _, db, _, _, service = strategy_services(tmp_path)
    request = strategy_request()

    first = service.process(request)
    replay = service.process(request)
    assert replay == first
    assert first["status"] == "TRADE_CREATED"
    assert len(db.trades()) == 1
    assert sum(event["event_type"] == "PAPER_TRADE_OPENED" for event in db.events()) == 1
    assert sum(
        event["event_type"] == "STRATEGY_DECISION_RECORDED"
        for event in db.events()
    ) == 1

    with pytest.raises(IdempotencyConflict, match="different inputs"):
        service.process(strategy_request(spot=25_200))
    assert len(db.trades()) == 1
    assert len(db.strategy_decisions()) == 1


def test_concurrent_identical_observation_processing_creates_one_trade(tmp_path):
    _, db, _, _, service = strategy_services(tmp_path)
    barrier = Barrier(2)
    request = strategy_request()

    def process():
        barrier.wait()
        return service.process(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process(), range(2)))

    assert results[0] == results[1]
    assert results[0]["status"] == "TRADE_CREATED"
    assert len(db.trades()) == 1
    assert len(db.strategy_decisions()) == 1
    assert sum(
        event["event_type"] == "STRATEGY_DECISION_RECORDED"
        for event in db.events()
    ) == 1


@pytest.mark.parametrize(
    "failed_event", ["PAPER_TRADE_OPENED", "STRATEGY_DECISION_RECORDED"]
)
def test_strategy_processing_failure_rolls_back_decision_trade_and_events(
    tmp_path, monkeypatch, failed_event
):
    _, db, risk, _, service = strategy_services(tmp_path)
    before_events = db.events()
    original_event = db.event

    def fail_event(event_type, detail, conn=None):
        if event_type == failed_event:
            raise OSError("simulated strategy persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_event)
    with pytest.raises(OSError, match="strategy persistence failure"):
        service.process(strategy_request())

    assert db.trades() == []
    assert db.strategy_decisions() == []
    assert db.events() == before_events
    assert risk.snapshot().daily_pnl == 0


def test_processed_decision_and_trade_survive_database_reopening(tmp_path):
    settings, db, risk, paper, service = strategy_services(tmp_path)
    first = service.process(strategy_request())
    reopened_db = Database(db.path)
    reopened_risk = RiskEngine(settings, reopened_db, clock=lambda: NOW)
    reopened_paper = PaperBroker(settings, reopened_db, reopened_risk)
    reopened_service = StrategyService(
        settings, reopened_db, reopened_risk, reopened_paper
    )

    replay = reopened_service.process(strategy_request())

    assert replay == first
    assert reopened_db.strategy_decisions() == [first]
    assert reopened_db.trades()[0]["trade_id"] == first["resulting_trade_id"]
    assert reopened_risk.snapshot().open_positions == 1
    decision_events = [
        json.loads(event["detail_json"])
        for event in reopened_db.events()
        if event["event_type"] == "STRATEGY_DECISION_RECORDED"
    ]
    assert decision_events[0]["resulting_trade_id"] == first["resulting_trade_id"]
