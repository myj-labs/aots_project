from types import SimpleNamespace
import pytest
from aots.database import Database
from aots.risk import RiskEngine, RiskRejected


def setup_risk(tmp_path, **overrides):
    values = dict(
        capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5,
    )
    values.update(overrides)
    settings = SimpleNamespace(**values)
    db = Database(str(tmp_path / "test.db"))
    return settings, db, RiskEngine(settings, db)


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
