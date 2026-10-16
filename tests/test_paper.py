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
    marked = broker.mark(opened.trade_id, 110)
    assert marked.unrealised_pnl == 100
    assert risk.snapshot().unrealised_pnl == 100

    current_time[0] = datetime(2025, 1, 3, 4, 10, tzinfo=timezone.utc)
    closed = broker.close_trade(opened.trade_id, exit_price=120)

    assert closed.status == "CLOSED"
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

    closed = broker.mark(opened.trade_id, 90)

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
    assert marked_detail["unrealised_pnl"] == 0
    assert closed_detail["pnl"] == -100
    assert closed_detail["reason"] == "SIMULATED_STOP"


def test_concurrent_marks_read_the_latest_committed_trade_state(tmp_path, monkeypatch):
    settings, db, risk = make_paper_services(tmp_path)
    broker = PaperBroker(settings, db, risk)
    opened = broker.open_trade(paper_request())
    original_transition = db.transition_open_trade
    start_barrier = Barrier(2)
    initial_marks = []

    def synchronized_transition(trade_id, prepare_transition, after_transition):
        start_barrier.wait()

        def capture_current_state(trade):
            initial_marks.append(trade["mark_price"])
            return prepare_transition(trade)

        return original_transition(
            trade_id, capture_current_state, after_transition
        )

    monkeypatch.setattr(db, "transition_open_trade", synchronized_transition)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda mark: broker.mark(opened.trade_id, mark),
            (110, 120),
        ))

    persisted = db.trades()[0]
    mark_events = [
        json.loads(event["detail_json"])
        for event in reversed(db.events())
        if event["event_type"] == "PAPER_TRADE_MARKED"
    ]
    assert sorted(initial_marks) == [100, 110] or sorted(initial_marks) == [100, 120]
    assert len(mark_events) == 2
    assert persisted["mark_price"] == mark_events[-1]["mark_price"]
    assert persisted["unrealised_pnl"] == mark_events[-1]["unrealised_pnl"]
    assert {result.mark_price for result in results} == {110, 120}


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
        stop_future = pool.submit(broker.mark, opened.trade_id, 90)
        assert stop_mark_recorded.wait(timeout=5)
        later_mark_future = pool.submit(broker.mark, opened.trade_id, 110)
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
        broker.mark(opened.trade_id, 90)

    persisted = db.trades()[0]
    assert persisted["status"] == "OPEN"
    assert persisted["mark_price"] == 100
    assert persisted["realised_pnl"] == 0
    assert persisted["unrealised_pnl"] == 0
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
    assert "idempotency_key" in columns


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
