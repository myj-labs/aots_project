from datetime import datetime, timezone
from uuid import uuid4
from .models import PaperTrade
from .risk import RiskEngine


class PaperBroker:
    """Local simulator only. It has no network client and cannot send broker orders."""

    def __init__(self, settings, db, risk: RiskEngine):
        self.settings, self.db, self.risk = settings, db, risk

    def open_trade(self, req):
        risk_inr = self.risk.validate_entry(
            entry_price=req.entry_price,
            stop_price=req.stop_price,
            quantity=req.quantity,
            lots=req.lots,
            market_data_age_seconds=req.market_data_age_seconds,
        )
        now = datetime.now(timezone.utc)
        trade = PaperTrade(
            trade_id=str(uuid4()),
            symbol=req.symbol,
            side=req.side,
            entry_price=req.entry_price,
            stop_price=req.stop_price,
            mark_price=req.entry_price,
            quantity=req.quantity,
            lots=req.lots,
            status="OPEN",
            realised_pnl=0,
            unrealised_pnl=0,
            created_at=now,
        )
        self.db.insert_trade(trade.model_dump(mode="json"))
        self.db.event("PAPER_TRADE_OPENED", {
            "trade_id": trade.trade_id, "symbol": trade.symbol, "estimated_risk_inr": risk_inr
        })
        return trade

    def mark(self, trade_id: str, mark_price: float):
        trade = self._get_trade(trade_id)
        if trade["status"] != "OPEN":
            raise ValueError("Trade is not open.")
        pnl = (mark_price - float(trade["entry_price"])) * int(trade["quantity"])
        trade["mark_price"] = mark_price
        trade["unrealised_pnl"] = round(pnl, 2)
        self.db.update_trade(trade)
        self.db.event("PAPER_TRADE_MARKED", {"trade_id": trade_id, "mark_price": mark_price, "unrealised_pnl": pnl})
        # Simulate a stop being triggered by the mark. Real execution can slip.
        if mark_price <= float(trade["stop_price"]):
            return self.close_trade(trade_id, exit_price=mark_price, reason="SIMULATED_STOP")
        return self._as_model(self._get_trade(trade_id))

    def close_trade(self, trade_id: str, exit_price: float, reason: str = "MANUAL"):
        trade = self._get_trade(trade_id)
        if trade["status"] != "OPEN":
            raise ValueError("Trade is not open.")
        pnl = round((exit_price - float(trade["entry_price"])) * int(trade["quantity"]), 2)
        trade["mark_price"] = exit_price
        trade["status"] = "CLOSED"
        trade["realised_pnl"] = pnl
        trade["unrealised_pnl"] = 0
        trade["closed_at"] = datetime.now(timezone.utc).isoformat()
        self.db.update_trade(trade)
        self.risk.record_closed_trade(pnl)
        self.db.event("PAPER_TRADE_CLOSED", {"trade_id": trade_id, "exit_price": exit_price, "pnl": pnl, "reason": reason})
        # Re-check portfolio loss after realised P&L; pause entries if limit breached.
        snap = self.risk.snapshot()
        if snap.daily_pnl <= -snap.max_daily_loss_inr:
            self.db.set_state("paused_for_day", True)
            self.db.event("DAILY_LOSS_LIMIT_BREACHED", {"daily_pnl": snap.daily_pnl})
        return self._as_model(self._get_trade(trade_id))

    def _get_trade(self, trade_id):
        for trade in self.db.trades():
            if trade["trade_id"] == trade_id:
                return trade
        raise KeyError("Trade not found.")

    @staticmethod
    def _as_model(trade):
        # SQLite rows serialize datetime fields as ISO strings; Pydantic parses them.
        return PaperTrade.model_validate(trade)
