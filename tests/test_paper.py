from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from aots.database import Database
from aots.models import PaperTradeRequest
from aots.paper import PaperBroker
from aots.risk import RiskEngine, RiskRejected


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
    request = PaperTradeRequest(
        symbol="NIFTY",
        entry_price=100,
        stop_price=95,
        quantity=10,
        lots=1,
    )

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
    opened = broker.open_trade(PaperTradeRequest(
        symbol="NIFTY",
        entry_price=100,
        stop_price=99,
        quantity=200,
        lots=1,
    ))

    closed = broker.close_trade(opened.trade_id, exit_price=89)

    assert closed.realised_pnl == -2200
    assert risk.snapshot().daily_pnl == -2200
    assert risk.paused_for_day()
    with pytest.raises(RiskRejected, match="paused"):
        broker.open_trade(PaperTradeRequest(
            symbol="NIFTY",
            entry_price=100,
            stop_price=99,
            quantity=10,
            lots=1,
        ))
    assert db.get_state("paused_for_day") is True
