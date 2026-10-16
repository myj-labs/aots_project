from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from aots.database import Database
from aots.risk import RiskEngine, RiskRejected


def setup_risk(tmp_path, *, now=None, **overrides):
    values = dict(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    values.update(overrides)
    settings = SimpleNamespace(**values)
    db = Database(str(tmp_path / "test.db"))
    clock = lambda: now or datetime(2025, 1, 3, 6, 30, tzinfo=timezone.utc)
    return settings, db, RiskEngine(settings, db, clock=clock)


def insert_trade(db, *, trade_id, status, realised_pnl, unrealised_pnl, closed_at=None):
    db.insert_trade({
        "trade_id": trade_id,
        "symbol": "NIFTY",
        "side": "BUY",
        "entry_price": 100,
        "stop_price": 95,
        "mark_price": 100,
        "quantity": 1,
        "lots": 1,
        "status": status,
        "realised_pnl": realised_pnl,
        "unrealised_pnl": unrealised_pnl,
        "created_at": "2025-01-02T17:00:00+00:00",
        "closed_at": closed_at,
    })


def test_accepts_trade_within_risk_budget(tmp_path):
    settings, db, risk = setup_risk(tmp_path)
    amount = risk.validate_entry(entry_price=100, stop_price=95, quantity=25, lots=1, market_data_age_seconds=0)
    assert amount == 125


def test_rejects_trade_over_risk_budget(tmp_path):
    settings, db, risk = setup_risk(tmp_path)
    with pytest.raises(RiskRejected, match="exceeds limit"):
        risk.validate_entry(entry_price=100, stop_price=90, quantity=100, lots=1, market_data_age_seconds=0)


def test_kill_switch_blocks_entries(tmp_path):
    settings, db, risk = setup_risk(tmp_path)
    risk.set_kill_switch(True)
    with pytest.raises(RiskRejected, match="Kill switch"):
        risk.validate_entry(entry_price=100, stop_price=99, quantity=10, lots=1, market_data_age_seconds=0)


def test_stale_data_blocks_entries(tmp_path):
    settings, db, risk = setup_risk(tmp_path)
    with pytest.raises(RiskRejected, match="stale"):
        risk.validate_entry(entry_price=100, stop_price=99, quantity=10, lots=1, market_data_age_seconds=6)


def test_three_consecutive_losses_pause_day(tmp_path):
    settings, db, risk = setup_risk(tmp_path)
    for _ in range(3):
        risk.record_closed_trade(-10)
    assert risk.paused_for_day()
    with pytest.raises(RiskRejected, match="paused"):
        risk.validate_entry(entry_price=100, stop_price=99, quantity=10, lots=1, market_data_age_seconds=0)


def test_daily_pnl_uses_kolkata_close_date_and_includes_open_positions(tmp_path):
    _, db, risk = setup_risk(
        tmp_path,
        now=datetime(2025, 1, 3, 0, 15, tzinfo=timezone.utc),
    )
    insert_trade(
        db, trade_id="prior-date-close", status="CLOSED", realised_pnl=-1000,
        unrealised_pnl=0, closed_at="2025-01-02T18:29:59+00:00",
    )
    insert_trade(
        db, trade_id="current-date-close", status="CLOSED", realised_pnl=-125,
        unrealised_pnl=0, closed_at="2025-01-02T18:30:00+00:00",
    )
    insert_trade(
        db, trade_id="open-from-prior-date", status="OPEN", realised_pnl=0,
        unrealised_pnl=300,
    )

    snapshot = risk.snapshot()

    assert snapshot.realised_pnl == -125
    assert snapshot.unrealised_pnl == 300
    assert snapshot.daily_pnl == 175
    assert snapshot.open_positions == 1


def test_session_rollover_resets_scoped_state_but_preserves_kill_switch_and_history(tmp_path):
    current_time = [datetime(2025, 1, 3, 6, 30, tzinfo=timezone.utc)]
    settings = SimpleNamespace(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    db_path = str(tmp_path / "rollover.db")
    db = Database(db_path)
    risk = RiskEngine(settings, db, clock=lambda: current_time[0])
    for _ in range(3):
        risk.record_closed_trade(-10)
    risk.set_kill_switch(True)
    insert_trade(
        db, trade_id="historical", status="CLOSED", realised_pnl=-30,
        unrealised_pnl=0, closed_at="2025-01-03T06:00:00+00:00",
    )
    event_count = len(db.events(limit=100))
    trade_count = len(db.trades())

    current_time[0] = datetime(2025, 1, 4, 6, 30, tzinfo=timezone.utc)
    restarted_risk = RiskEngine(settings, Database(db_path), clock=lambda: current_time[0])

    assert db.get_state("session_date") == "2025-01-04"
    assert restarted_risk.get_consecutive_losses() == 0
    assert not restarted_risk.paused_for_day()
    assert restarted_risk.get_kill_switch()
    assert len(db.trades()) == trade_count
    assert len(db.events(limit=100)) == event_count
