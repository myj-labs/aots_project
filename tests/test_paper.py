from datetime import date, datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
from threading import Barrier, Event, Lock
from types import SimpleNamespace

import pytest

from aots.database import Database
from aots.models import PaperTradeRequest
from aots.paper import PaperBroker
from aots.risk import RiskEngine, RiskRejected


def paper_request(key="request-1", **overrides):
    values = dict(
        symbol="NIFTY",
        idempotency_key=key,
        entry_price=100,
        stop_price=95,
        quantity=10,
        lots=1,
        market_data_age_seconds=0,
    )
    values.update(overrides)
    return PaperTradeRequest(**values)


def test_paper_trade_open_mark_close_lifecycle_uses_long_option_pnl(tmp_path):
    current_time = [datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)]
    settings = SimpleNamespace(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    db = Database(str(tmp_path / "paper.db"))
    risk = RiskEngine(settings, db, clock=lambda: current_time[0])
    broker = PaperBroker(settings, db, risk)
    request = paper_request()

    opened = broker.open_trade(request)
    marked = broker.mark(opened.trade_id, 110, observation_sequence=1)
    assert marked.unrealised_pnl == 100
    assert risk.snapshot().unrealised_pnl == 100

    current_time[0] = datetime(2025, 1, 3, 4, 10, tzinfo=timezone.utc)
    closed = broker.close_trade(opened.trade_id, exit_price=120)

    assert closed.status == "CLOSED"
    assert closed.close_reason == "MANUAL"
    assert closed.realised_pnl == 200
    assert closed.unrealised_pnl == 0
    assert closed.closed_at == current_time[0]
    assert risk.snapshot().realised_pnl == 200
    assert risk.snapshot().open_positions == 0
    assert [event["event_type"] for event in reversed(db.events())] == [
        "PAPER_TRADE_OPENED",
        "PAPER_TRADE_MARKED",
        "PAPER_TRADE_CLOSED",
    ]


def test_stop_trigger_mark_event_matches_persisted_closed_trade(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())

    closed = broker.mark(opened.trade_id, 90, observation_sequence=1)

    persisted = next(t for t in db.trades() if t["trade_id"] == opened.trade_id)
    events = list(reversed(db.events()))
    marked_event = next(e for e in events if e["event_type"] == "PAPER_TRADE_MARKED")
    closed_event = next(e for e in events if e["event_type"] == "PAPER_TRADE_CLOSED")
    marked_detail = json.loads(marked_event["detail_json"])
    closed_detail = json.loads(closed_event["detail_json"])

    assert closed.status == "CLOSED"
    assert persisted["status"] == "CLOSED"
    assert persisted["unrealised_pnl"] == 0
    assert persisted["realised_pnl"] == -100
    assert persisted["mark_sequence"] == 1
    assert persisted["close_reason"] == "SIMULATED_STOP"
    assert marked_detail["unrealised_pnl"] == 0
    assert marked_detail["observation_sequence"] == 1
    assert closed_detail["pnl"] == -100
    assert closed_detail["reason"] == "SIMULATED_STOP"


@pytest.mark.parametrize(
    ("mark_price", "expected_status", "expected_reason", "expected_pnl"),
    [
        (109, "OPEN", None, 90),
        (110, "CLOSED", "SIMULATED_TAKE_PROFIT", 100),
        (115, "CLOSED", "SIMULATED_TAKE_PROFIT", 150),
    ],
)
def test_take_profit_closes_at_accepted_observed_mark(
    tmp_path, mark_price, expected_status, expected_reason, expected_pnl
):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(take_profit_profit_inr=100))

    result = broker.mark(opened.trade_id, mark_price, observation_sequence=1)
    persisted = db.trades()[0]
    events = list(reversed(db.events()))
    mark_event = next(e for e in events if e["event_type"] == "PAPER_TRADE_MARKED")
    close_events = [e for e in events if e["event_type"] == "PAPER_TRADE_CLOSED"]

    assert result.status == expected_status
    assert persisted["status"] == expected_status
    assert persisted["mark_price"] == mark_price
    assert persisted["take_profit_profit_inr"] == 100
    assert persisted["realised_pnl"] == (expected_pnl if expected_status == "CLOSED" else 0)
    assert persisted["unrealised_pnl"] == (0 if expected_status == "CLOSED" else expected_pnl)
    assert persisted["close_reason"] == expected_reason
    assert json.loads(mark_event["detail_json"])["close_reason"] == expected_reason
    if expected_reason is None:
        assert not close_events
        assert risk.snapshot().unrealised_pnl == expected_pnl
    else:
        assert len(close_events) == 1
        close_detail = json.loads(close_events[0]["detail_json"])
        assert close_detail["exit_price"] == mark_price
        assert close_detail["pnl"] == expected_pnl
        assert close_detail["reason"] == expected_reason
        assert risk.snapshot().realised_pnl == expected_pnl
        assert risk.snapshot().unrealised_pnl == 0


def test_ten_lots_of_65_units_use_total_quantity_once_for_profit(tmp_path):
    settings, db, risk = make_paper_services(
        tmp_path,
        capital_inr=1_000_000,
        max_trade_risk_pct=.01,
        max_lots_per_order=10,
    )
    broker = PaperBroker(settings, db, risk)
    lots, lot_size = 10, 65
    opened = broker.open_trade(
        paper_request(
            quantity=lots * lot_size,
            lots=lots,
            stop_price=99,
            take_profit_profit_inr=1500,
        )
    )

    closed = broker.mark(opened.trade_id, 102.31, observation_sequence=1)

    assert closed.quantity == 650
    assert closed.lots == 10
    assert closed.status == "CLOSED"
    assert closed.realised_pnl == 1501.5
    assert closed.close_reason == "SIMULATED_TAKE_PROFIT"
    close_event = next(
        e for e in reversed(db.events()) if e["event_type"] == "PAPER_TRADE_CLOSED"
    )
    assert json.loads(close_event["detail_json"])["pnl"] == 1501.5


@pytest.mark.parametrize(
    ("symbol", "quantity", "lots", "expected_pnl"),
    [
        ("NIFTY-CALL-CE", 65, 1, 195),
        ("NIFTY-PUT-PE", 130, 2, 390),
    ],
)
def test_long_call_and_put_pnl_scales_with_total_quantity(
    tmp_path, symbol, quantity, lots, expected_pnl
):
    settings, db, risk = make_paper_services(tmp_path, max_lots_per_order=3)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(
        paper_request(
            key=f"{symbol}-{quantity}",
            symbol=symbol,
            quantity=quantity,
            lots=lots,
            stop_price=99.9,
        )
    )

    marked = broker.mark(opened.trade_id, 103, observation_sequence=1)

    assert marked.status == "OPEN"
    assert marked.unrealised_pnl == expected_pnl


@pytest.mark.parametrize("target", [0, -1, float("inf"), float("nan")])
def test_take_profit_target_must_be_finite_and_positive(tmp_path, target):
    with pytest.raises(ValueError):
        paper_request(take_profit_profit_inr=target)


def test_stale_take_profit_observation_cannot_close_or_change_state(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(take_profit_profit_inr=100))
    broker.mark(opened.trade_id, 105, observation_sequence=2)
    before_trade = db.trades()[0]
    before_events = db.events()
    before_risk = risk.snapshot()

    for sequence in (1, 2):
        with pytest.raises(ValueError, match="stale or duplicate"):
            broker.mark(opened.trade_id, 120, observation_sequence=sequence)

    assert db.trades()[0] == before_trade
    assert db.events() == before_events
    assert risk.snapshot() == before_risk
    assert db.trades()[0]["status"] == "OPEN"


def test_manual_close_with_take_profit_keeps_manual_reason_and_closes_once(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(take_profit_profit_inr=100))

    closed = broker.close_trade(opened.trade_id, exit_price=108)
    before_events = db.events()

    with pytest.raises(ValueError, match="not open"):
        broker.close_trade(opened.trade_id, exit_price=120)
    with pytest.raises(ValueError, match="not open"):
        broker.mark(opened.trade_id, 120, observation_sequence=1)

    assert closed.close_reason == "MANUAL"
    assert db.trades()[0]["status"] == "CLOSED"
    assert db.trades()[0]["mark_price"] == 108
    assert db.trades()[0]["realised_pnl"] == 80
    assert db.trades()[0]["close_reason"] == "MANUAL"
    assert db.events() == before_events
    assert sum(e["event_type"] == "PAPER_TRADE_CLOSED" for e in db.events()) == 1
    assert risk.snapshot().realised_pnl == 80


def test_stop_has_priority_for_corrupt_overlapping_exit_thresholds(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(take_profit_profit_inr=100))
    with db.connect() as conn:
        conn.execute(
            "UPDATE trades SET take_profit_profit_inr=-100 WHERE trade_id=?",
            (opened.trade_id,),
        )

    closed = broker.mark(opened.trade_id, 94.5, observation_sequence=1)
    close_event = next(
        e for e in reversed(db.events()) if e["event_type"] == "PAPER_TRADE_CLOSED"
    )

    assert closed.status == "CLOSED"
    assert closed.close_reason == "SIMULATED_STOP"
    assert closed.mark_price == 94.5
    assert json.loads(close_event["detail_json"])["reason"] == "SIMULATED_STOP"


@pytest.mark.parametrize("failed_event", ["PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED"])
def test_take_profit_audit_failure_rolls_back_complete_transition(
    tmp_path, monkeypatch, failed_event
):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(
        paper_request(quantity=65, stop_price=99.9, take_profit_profit_inr=650)
    )
    before_events = db.events()
    original_event = db.event

    def fail_target_event(event_type, detail, conn=None):
        if event_type == failed_event:
            raise OSError("simulated take-profit event persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_target_event)
    with pytest.raises(OSError, match="take-profit event persistence failure"):
        broker.mark(opened.trade_id, 110, observation_sequence=1)

    persisted = db.trades()[0]
    assert persisted["status"] == "OPEN"
    assert persisted["mark_price"] == 100
    assert persisted["mark_sequence"] == 0
    assert persisted["realised_pnl"] == 0
    assert persisted["unrealised_pnl"] == 0
    assert persisted["close_reason"] is None
    assert risk.snapshot().daily_pnl == 0
    assert risk.get_consecutive_losses() == 0
    assert db.events() == before_events


def test_concurrent_take_profit_marks_close_once(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(take_profit_profit_inr=100))
    barrier = Barrier(2)

    def mark(price, sequence):
        barrier.wait()
        try:
            return broker.mark(opened.trade_id, price, sequence)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda args: mark(*args), ((110, 1), (120, 2))))

    assert sum(not isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    persisted = db.trades()[0]
    assert persisted["status"] == "CLOSED"
    assert persisted["close_reason"] == "SIMULATED_TAKE_PROFIT"
    assert persisted["mark_price"] in {110, 120}
    assert persisted["mark_sequence"] in {1, 2}
    assert sum(e["event_type"] == "PAPER_TRADE_CLOSED" for e in db.events()) == 1
    assert risk.get_consecutive_losses() == 0


def test_concurrent_out_of_order_marks_keep_newest_sequence(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    original_transition = db.transition_open_trade
    older_started = Event()
    newer_committed = Event()
    call_lock = Lock()
    call_count = 0

    def synchronized_transition(trade_id, prepare_transition, after_transition):
        nonlocal call_count
        with call_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            older_started.set()
            assert newer_committed.wait(timeout=5)
            return original_transition(trade_id, prepare_transition, after_transition)
        result = original_transition(trade_id, prepare_transition, after_transition)
        newer_committed.set()
        return result

    monkeypatch.setattr(db, "transition_open_trade", synchronized_transition)
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(broker.mark, opened.trade_id, 110, 1)
        assert older_started.wait(timeout=5)
        newer = pool.submit(broker.mark, opened.trade_id, 120, 2)
        with pytest.raises(ValueError, match="stale or duplicate"):
            older.result(timeout=5)
        newer_result = newer.result(timeout=5)

    persisted = db.trades()[0]
    mark_events = [
        json.loads(event["detail_json"])
        for event in reversed(db.events())
        if event["event_type"] == "PAPER_TRADE_MARKED"
    ]
    assert len(mark_events) == 1
    assert persisted["mark_sequence"] == 2
    assert persisted["mark_price"] == mark_events[-1]["mark_price"] == 120
    assert persisted["unrealised_pnl"] == mark_events[-1]["unrealised_pnl"] == 200
    assert mark_events[-1]["observation_sequence"] == 2
    assert newer_result.mark_price == 120


def test_chronological_mark_sequences_update_mark_and_pnl(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())

    first = broker.mark(opened.trade_id, 110, observation_sequence=1)
    second = broker.mark(opened.trade_id, 115, observation_sequence=2)

    assert first.unrealised_pnl == 100
    assert first.mark_sequence == 1
    assert second.mark_price == 115
    assert second.unrealised_pnl == 150
    assert second.mark_sequence == 2


def test_older_and_duplicate_mark_sequences_are_rejected_without_audit(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    broker.mark(opened.trade_id, 110, observation_sequence=2)
    event_count = sum(
        event["event_type"] == "PAPER_TRADE_MARKED" for event in db.events()
    )

    for sequence in (1, 2):
        with pytest.raises(ValueError, match="stale or duplicate"):
            broker.mark(opened.trade_id, 90, observation_sequence=sequence)

    persisted = db.trades()[0]
    assert persisted["mark_sequence"] == 2
    assert persisted["mark_price"] == 110
    assert persisted["unrealised_pnl"] == 100
    assert persisted["status"] == "OPEN"
    assert sum(
        event["event_type"] == "PAPER_TRADE_MARKED" for event in db.events()
    ) == event_count


def test_stale_below_stop_mark_cannot_close_after_newer_above_stop_mark(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    broker.mark(opened.trade_id, 110, observation_sequence=2)

    with pytest.raises(ValueError, match="stale or duplicate"):
        broker.mark(opened.trade_id, 90, observation_sequence=1)

    open_trade = db.trades()[0]
    assert open_trade["status"] == "OPEN"
    assert open_trade["mark_price"] == 110
    assert open_trade["unrealised_pnl"] == 100
    assert open_trade["mark_sequence"] == 2

    closed = broker.mark(opened.trade_id, 90, observation_sequence=3)
    events = [
        json.loads(event["detail_json"])
        for event in reversed(db.events())
        if event["event_type"] in {"PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED"}
    ]
    assert closed.status == "CLOSED"
    assert closed.realised_pnl == -100
    assert closed.unrealised_pnl == 0
    assert closed.mark_sequence == 3
    assert [event["event_type"] for event in reversed(db.events())].count(
        "PAPER_TRADE_CLOSED"
    ) == 1
    assert events[-2]["observation_sequence"] == 3


def test_mark_after_close_does_not_change_closed_trade(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    closed = broker.close_trade(opened.trade_id, exit_price=90)
    before_events = db.events()

    with pytest.raises(ValueError, match="not open"):
        broker.mark(opened.trade_id, 110, observation_sequence=1)

    assert db.trades()[0]["status"] == "CLOSED"
    assert db.trades()[0]["realised_pnl"] == closed.realised_pnl == -100
    assert db.trades()[0]["mark_price"] == 90
    assert db.events() == before_events


def test_stop_mark_wins_atomic_race_against_mark_after_close(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    original_event = db.event
    stop_mark_recorded = Event()
    release_stop = Event()
    later_mark_started = Event()
    original_transition = db.transition_open_trade
    transition_lock = Lock()
    transition_count = 0

    def hold_stop_mark_event(event_type, detail, conn=None):
        original_event(event_type, detail, conn=conn)
        if (
            event_type == "PAPER_TRADE_MARKED"
            and detail["mark_price"] == 90
        ):
            stop_mark_recorded.set()
            assert release_stop.wait(timeout=5)

    def signal_mark_started(trade_id, prepare_transition, after_transition):
        nonlocal transition_count
        with transition_lock:
            transition_count += 1
            if transition_count == 2:
                later_mark_started.set()
        return original_transition(trade_id, prepare_transition, after_transition)

    monkeypatch.setattr(db, "event", hold_stop_mark_event)
    monkeypatch.setattr(db, "transition_open_trade", signal_mark_started)

    with ThreadPoolExecutor(max_workers=2) as pool:
        stop_future = pool.submit(broker.mark, opened.trade_id, 90, 1)
        assert stop_mark_recorded.wait(timeout=5)
        later_mark_future = pool.submit(broker.mark, opened.trade_id, 110, 2)
        assert later_mark_started.wait(timeout=5)
        release_stop.set()
        stopped_trade = stop_future.result(timeout=5)
        with pytest.raises(ValueError, match="not open"):
            later_mark_future.result(timeout=5)

    persisted = db.trades()[0]
    events = list(reversed(db.events()))
    assert stopped_trade.status == "CLOSED"
    assert persisted["status"] == "CLOSED"
    assert persisted["realised_pnl"] == -100
    assert persisted["unrealised_pnl"] == 0
    assert sum(e["event_type"] == "PAPER_TRADE_MARKED" for e in events) == 1
    assert sum(e["event_type"] == "PAPER_TRADE_CLOSED" for e in events) == 1
    assert risk.get_consecutive_losses() == 1


@pytest.mark.parametrize("failed_event", ["PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED"])
def test_stop_mark_audit_failure_rolls_back_complete_transition(
    tmp_path, monkeypatch, failed_event
):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    original_event = db.event

    def fail_stop_audit(event_type, detail, conn=None):
        if event_type == failed_event:
            raise OSError(f"simulated {event_type} persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_stop_audit)

    with pytest.raises(OSError, match="persistence failure"):
        broker.mark(opened.trade_id, 90, observation_sequence=1)

    persisted = db.trades()[0]
    assert persisted["status"] == "OPEN"
    assert persisted["mark_price"] == 100
    assert persisted["realised_pnl"] == 0
    assert persisted["unrealised_pnl"] == 0
    assert persisted["mark_sequence"] == 0
    assert risk.get_consecutive_losses() == 0
    assert not any(
        event["event_type"] in {"PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED"}
        for event in db.events()
    )


def test_closed_trade_breaching_current_session_limit_pauses_entries(tmp_path):
    settings = SimpleNamespace(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    db = Database(str(tmp_path / "breach.db"))
    risk = RiskEngine(
        settings, db,
        clock=lambda: datetime(2025, 1, 3, 6, 30, tzinfo=timezone.utc),
    )
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(
        entry_price=100, stop_price=99, quantity=200,
    ))

    closed = broker.close_trade(opened.trade_id, exit_price=89)

    assert closed.realised_pnl == -2200
    assert risk.snapshot().daily_pnl == -2200
    assert risk.paused_for_day()
    with pytest.raises(RiskRejected, match="paused"):
        broker.open_trade(paper_request(
            key="request-2", entry_price=100, stop_price=99, quantity=10,
        ))
    assert db.get_state("paused_for_day") is True


def test_duplicate_entry_replays_original_trade_without_second_position(tmp_path):
    current_time = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    settings = SimpleNamespace(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=1, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    db = Database(str(tmp_path / "idempotent.db"))
    risk = RiskEngine(settings, db, clock=lambda: current_time)
    broker = PaperBroker(settings, db, risk)
    request = paper_request()

    first = broker.open_trade(request)
    replay = broker.open_trade(request)

    assert replay.trade_id == first.trade_id
    assert len(db.trades()) == 1
    assert [event["event_type"] for event in reversed(db.events())] == [
        "PAPER_TRADE_OPENED",
    ]


def test_duplicate_entry_replay_after_close_returns_original_without_duplicate_event(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    request = paper_request(key="closed-replay")
    opened = broker.open_trade(request)
    closed = broker.close_trade(opened.trade_id, exit_price=110)

    replay = broker.open_trade(request)

    assert replay.trade_id == closed.trade_id == opened.trade_id
    assert replay.status == "CLOSED"
    assert len(db.trades()) == 1
    assert sum(e["event_type"] == "PAPER_TRADE_OPENED" for e in db.events()) == 1


def test_reusing_idempotency_key_for_different_order_is_rejected(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    broker.open_trade(paper_request())

    with pytest.raises(ValueError, match="different trade"):
        broker.open_trade(paper_request(entry_price=101, stop_price=96))

    with pytest.raises(ValueError, match="different trade"):
        broker.open_trade(paper_request(take_profit_profit_inr=100))

    assert len(db.trades()) == 1


def test_atomic_entry_limit_blocks_concurrent_second_position(tmp_path):
    settings, db, risk = make_paper_services(tmp_path, max_open_positions=1)
    broker = PaperBroker(settings, db, risk)
    barrier = Barrier(2)

    def open_trade(key):
        barrier.wait()
        try:
            return broker.open_trade(paper_request(key=key))
        except RiskRejected as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(open_trade, ("first", "second")))

    assert sum(not isinstance(result, RiskRejected) for result in results) == 1
    assert sum(isinstance(result, RiskRejected) for result in results) == 1
    assert len(db.trades()) == 1


def test_concurrent_duplicate_exits_only_close_and_account_once(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    barrier = Barrier(2)

    def close_trade():
        barrier.wait()
        try:
            return broker.close_trade(opened.trade_id, exit_price=90)
        except ValueError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: close_trade(), range(2)))

    assert sum(not isinstance(result, ValueError) for result in results) == 1
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert db.trades()[0]["status"] == "CLOSED"
    assert db.trades()[0]["realised_pnl"] == -100
    assert db.trades()[0]["close_reason"] == "MANUAL"
    assert risk.get_consecutive_losses() == 1
    assert sum(e["event_type"] == "PAPER_TRADE_CLOSED" for e in db.events()) == 1


def test_opening_event_failure_rolls_back_trade_insert(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    original_event = db.event

    def fail_open_event(event_type, detail, conn=None):
        if event_type == "PAPER_TRADE_OPENED":
            raise OSError("simulated event persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_open_event)

    with pytest.raises(OSError, match="simulated"):
        broker.open_trade(paper_request())

    assert not db.trades()


def test_close_event_failure_rolls_back_trade_and_risk_state(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    original_event = db.event

    def fail_close_event(event_type, detail, conn=None):
        if event_type == "PAPER_TRADE_CLOSED":
            raise OSError("simulated event persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_close_event)

    with pytest.raises(OSError, match="simulated"):
        broker.close_trade(opened.trade_id, exit_price=90)

    assert db.trades()[0]["status"] == "OPEN"
    assert db.trades()[0]["realised_pnl"] == 0
    assert db.trades()[0]["close_reason"] is None
    assert risk.get_consecutive_losses() == 0
    assert not any(e["event_type"] == "PAPER_TRADE_CLOSED" for e in db.events())


def test_market_age_is_required_finite_and_positive(tmp_path):
    with pytest.raises(ValueError):
        PaperTradeRequest(
            symbol="NIFTY", idempotency_key="missing-age", entry_price=100,
            stop_price=95, quantity=10,
        )
    with pytest.raises(ValueError):
        paper_request(market_data_age_seconds=float("inf"))
    with pytest.raises(ValueError):
        PaperTradeRequest(
            symbol="NIFTY", entry_price=100, stop_price=95,
            quantity=10, market_data_age_seconds=0,
        )
    with pytest.raises(RiskRejected, match="finite"):
        make_paper_services(tmp_path)[2].validate_entry(
            entry_price=100, stop_price=95, quantity=1, lots=1,
            market_data_age_seconds=float("nan"),
        )


def test_idempotency_key_rejects_whitespace_only_and_surrounding_whitespace(tmp_path):
    with pytest.raises(ValueError, match="whitespace-only"):
        paper_request(key="")
    with pytest.raises(ValueError, match="whitespace-only"):
        paper_request(key="   ")
    with pytest.raises(ValueError, match="surrounding whitespace"):
        paper_request(key=" key ")

    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    with pytest.raises(ValueError, match="surrounding whitespace"):
        broker.open_trade(paper_request(key=" key "))
    assert not db.trades()
    with pytest.raises(ValueError, match="whitespace-only"):
        db.insert_trade_atomically(
            {},
            session_date=date(2025, 1, 3),
            validate=lambda _conn: 0,
            idempotency_key=" ",
            rejection_type=RiskRejected,
        )


def test_stale_session_clock_cannot_open_trade_against_newer_persisted_session(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    db.ensure_session(datetime(2025, 1, 4, tzinfo=timezone.utc).date())
    broker = PaperBroker(settings, db, risk)

    with pytest.raises(RiskRejected, match="Session date is stale"):
        broker.open_trade(paper_request())

    assert not db.trades()


def test_stale_session_clock_cannot_close_trade_out_of_current_daily_pnl(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    db.ensure_session(datetime(2025, 1, 4, tzinfo=timezone.utc).date())

    with pytest.raises(RiskRejected, match="Session date is stale"):
        broker.close_trade(opened.trade_id, exit_price=90)

    assert db.trades()[0]["status"] == "OPEN"
    assert risk.get_consecutive_losses() == 0


def test_open_position_is_restored_from_sqlite_after_broker_recreation(tmp_path):
    settings, db, risk = make_paper_services(tmp_path, max_open_positions=1)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())

    reopened_db = Database(db.path)
    restarted_risk = RiskEngine(settings, reopened_db, clock=risk.current_time)
    restarted_broker = PaperBroker(settings, reopened_db, restarted_risk)

    assert restarted_risk.snapshot().open_positions == 1
    assert restarted_broker._get_trade(opened.trade_id)["status"] == "OPEN"
    with pytest.raises(RiskRejected, match="Maximum open positions"):
        restarted_broker.open_trade(paper_request(key="second"))


def test_database_migration_preserves_legacy_trade_rows(tmp_path):
    path = str(tmp_path / "legacy-trades.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE trades (
              trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
              entry_price REAL NOT NULL, stop_price REAL NOT NULL, mark_price REAL NOT NULL,
              quantity INTEGER NOT NULL, lots INTEGER NOT NULL, status TEXT NOT NULL,
              realised_pnl REAL NOT NULL DEFAULT 0, unrealised_pnl REAL NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL, closed_at TEXT
            );
            INSERT INTO trades VALUES (
              'legacy', 'NIFTY', 'BUY', 100, 95, 101, 1, 1, 'OPEN', 0, 1,
              '2025-01-03T04:00:00+00:00', NULL
            );
        """)

    db = Database(path)

    assert db.trades()[0]["trade_id"] == "legacy"
    with db.connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(trades)")}
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert {
        "idempotency_key",
        "mark_sequence",
        "take_profit_profit_inr",
        "close_reason",
    } <= columns
    assert db.trades()[0]["mark_sequence"] == 0
    assert db.trades()[0]["take_profit_profit_inr"] is None
    assert db.trades()[0]["close_reason"] is None
    assert "strategy_decisions" in tables


def test_database_migration_preserves_legacy_premium_target_without_reinterpreting_it(
    tmp_path,
):
    path = str(tmp_path / "legacy-target.db")
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE trades (
              trade_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, side TEXT NOT NULL,
              entry_price REAL NOT NULL, stop_price REAL NOT NULL,
              take_profit_price REAL, mark_price REAL NOT NULL,
              quantity INTEGER NOT NULL, lots INTEGER NOT NULL, status TEXT NOT NULL,
              realised_pnl REAL NOT NULL DEFAULT 0,
              unrealised_pnl REAL NOT NULL DEFAULT 0,
              mark_sequence INTEGER NOT NULL DEFAULT 0,
              close_reason TEXT, created_at TEXT NOT NULL, closed_at TEXT
            );
            INSERT INTO trades VALUES (
              'legacy-target', 'NIFTY', 'BUY', 100, 95, 650, 100,
              65, 1, 'OPEN', 0, 0, 0, NULL,
              '2025-01-03T04:00:00+00:00', NULL
            );
        """)

    db = Database(path)
    migrated = db.trades()[0]

    assert migrated["trade_id"] == "legacy-target"
    assert migrated["take_profit_price"] == 650
    assert migrated["take_profit_profit_inr"] is None


def test_daily_pause_event_failure_rolls_back_close_and_pause(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(quantity=100))
    original_event = db.event

    def fail_pause_event(event_type, detail, conn=None):
        if event_type == "DAILY_LOSS_LIMIT_BREACHED":
            raise OSError("simulated pause persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_pause_event)

    with pytest.raises(OSError, match="pause persistence"):
        broker.close_trade(opened.trade_id, exit_price=70)

    assert db.trades()[0]["status"] == "OPEN"
    assert risk.snapshot().daily_pnl == 0
    assert not risk.paused_for_day()
    assert not any(
        event["event_type"] in {"PAPER_TRADE_CLOSED", "DAILY_LOSS_LIMIT_BREACHED"}
        for event in db.events()
    )


def _restart(settings, db, risk):
    reopened_db = Database(db.path)
    reopened_risk = RiskEngine(settings, reopened_db, clock=risk.current_time)
    return reopened_db, reopened_risk, PaperBroker(settings, reopened_db, reopened_risk)


def _event_types(db):
    return [e["event_type"] for e in reversed(db.events())]


def test_open_trade_mark_and_kill_switch_survive_database_reinitialisation(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    request = paper_request(key="restart-open", take_profit_profit_inr=500)
    opened = broker.open_trade(request)
    broker.mark(opened.trade_id, 104, observation_sequence=7)
    risk.set_kill_switch(True)
    before = next(t for t in db.trades() if t["trade_id"] == opened.trade_id)
    events_before = _event_types(db)

    reopened_db, reopened_risk, reopened_broker = _restart(settings, db, risk)

    after = next(t for t in reopened_db.trades() if t["trade_id"] == opened.trade_id)
    assert after == before
    assert after["status"] == "OPEN"
    assert after["idempotency_key"] == "restart-open"
    assert (after["symbol"], after["side"], after["entry_price"], after["stop_price"]) == (
        "NIFTY", "BUY", 100, 95,
    )
    assert (after["quantity"], after["lots"], after["take_profit_profit_inr"]) == (10, 1, 500)
    assert after["mark_price"] == 104
    assert after["mark_sequence"] == 7
    assert after["unrealised_pnl"] == 40
    assert after["realised_pnl"] == 0
    assert after["closed_at"] is None and after["close_reason"] is None
    snapshot = reopened_risk.snapshot()
    assert snapshot.open_positions == 1
    assert snapshot.unrealised_pnl == 40
    assert snapshot.kill_switch is True
    assert _event_types(reopened_db) == events_before

    # Restart does not re-emit events, replays the entry idempotently, and keeps sequencing.
    assert reopened_broker.open_trade(request).trade_id == opened.trade_id
    assert _event_types(reopened_db) == events_before
    with pytest.raises(ValueError, match="stale or duplicate"):
        reopened_broker.mark(opened.trade_id, 90, observation_sequence=7)
    assert next(t for t in reopened_db.trades())["mark_price"] == 104
    resumed = reopened_broker.mark(opened.trade_id, 106, observation_sequence=8)
    assert (resumed.mark_sequence, resumed.unrealised_pnl) == (8, 60)


def test_closed_trade_and_audit_events_survive_database_reinitialisation(tmp_path):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request(key="restart-closed", take_profit_profit_inr=100))
    closed = broker.mark(opened.trade_id, 115, observation_sequence=3)
    before_trades = db.trades()
    before_events = db.events()

    reopened_db, reopened_risk, reopened_broker = _restart(settings, db, risk)

    assert reopened_db.trades() == before_trades
    assert reopened_db.events() == before_events
    persisted = reopened_db.trades()[0]
    assert persisted["status"] == "CLOSED"
    assert persisted["trade_id"] == closed.trade_id
    assert (persisted["entry_price"], persisted["stop_price"], persisted["quantity"]) == (100, 95, 10)
    assert persisted["mark_price"] == 115 and persisted["mark_sequence"] == 3
    assert persisted["realised_pnl"] == 150 and persisted["unrealised_pnl"] == 0
    assert persisted["close_reason"] == "SIMULATED_TAKE_PROFIT"
    assert persisted["closed_at"] == closed.closed_at.isoformat()
    assert _event_types(reopened_db) == [
        "PAPER_TRADE_OPENED", "PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED",
    ]
    closed_detail = json.loads(reopened_db.events()[0]["detail_json"])
    assert closed_detail == {
        "trade_id": opened.trade_id, "exit_price": 115, "pnl": 150,
        "reason": "SIMULATED_TAKE_PROFIT",
    }
    snapshot = reopened_risk.snapshot()
    assert (snapshot.realised_pnl, snapshot.unrealised_pnl, snapshot.open_positions) == (150, 0, 0)

    # A repeated close or later mark after restart changes nothing and adds no events.
    with pytest.raises(ValueError, match="not open"):
        reopened_broker.close_trade(opened.trade_id, exit_price=90)
    with pytest.raises(ValueError, match="not open"):
        reopened_broker.mark(opened.trade_id, 90, observation_sequence=4)
    assert reopened_db.trades() == before_trades
    assert reopened_db.events() == before_events


@pytest.mark.parametrize(
    ("operation", "arguments", "error", "message"),
    [
        ("mark", (float("nan"), 1), ValueError, "finite"),
        ("mark", (float("inf"), 1), ValueError, "finite"),
        ("mark", (0, 1), ValueError, "greater than zero"),
        ("mark", (-5, 1), ValueError, "greater than zero"),
        ("mark", (101, 0), ValueError, "positive integer"),
        ("mark", (101, True), ValueError, "positive integer"),
        ("mark", (101, 1.5), ValueError, "positive integer"),
        ("close_trade", (float("nan"),), ValueError, "finite"),
        ("close_trade", (0,), ValueError, "greater than zero"),
    ],
)
def test_rejected_mark_or_close_leaves_trade_and_events_unchanged(
    tmp_path, operation, arguments, error, message
):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    before_trades, before_events = db.trades(), db.events()

    with pytest.raises(error, match=message):
        getattr(broker, operation)(opened.trade_id, *arguments)

    assert db.trades() == before_trades
    assert db.events() == before_events
    assert risk.get_consecutive_losses() == 0


@pytest.mark.parametrize("operation", ["mark", "close_trade"])
def test_unknown_trade_id_is_rejected_without_side_effects(tmp_path, operation):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    broker.open_trade(paper_request())
    before_trades, before_events = db.trades(), db.events()
    arguments = (101, 1) if operation == "mark" else (101,)

    with pytest.raises(KeyError):
        getattr(broker, operation)("missing-trade", *arguments)

    assert db.trades() == before_trades
    assert db.events() == before_events


def make_paper_services(tmp_path, **overrides):
    current_time = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    settings = SimpleNamespace(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    for key, value in overrides.items():
        setattr(settings, key, value)
    db = Database(str(tmp_path / "paper-test.db"))
    risk = RiskEngine(settings, db, clock=lambda: current_time)
    return settings, db, risk
