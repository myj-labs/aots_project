from datetime import datetime
from math import isfinite
from uuid import uuid4
from .models import PaperTrade
from .risk import RiskEngine, RiskRejected


class PaperBroker:
    """Local simulator only. It has no network client and cannot send broker orders."""

    def __init__(self, settings, db, risk: RiskEngine):
        self.settings, self.db, self.risk = settings, db, risk

    def open_trade(self, req):
        now = self.risk.current_time()
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
        trade_data = trade.model_dump(mode="json")

        def validate(conn):
            return self.risk.validate_entry(
                entry_price=req.entry_price,
                stop_price=req.stop_price,
                quantity=req.quantity,
                lots=req.lots,
                market_data_age_seconds=req.market_data_age_seconds,
                _conn=conn,
                _session_date=self.risk.trading_date(now),
            )

        result = self.db.insert_trade_atomically(
            trade_data,
            session_date=self.risk.trading_date(now),
            validate=validate,
            idempotency_key=req.idempotency_key,
            rejection_type=RiskRejected,
        )
        return self._as_model(result)

    def mark(self, trade_id: str, mark_price: float):
        if not isfinite(mark_price) or mark_price <= 0:
            raise ValueError("Mark price must be finite and greater than zero.")

        def prepare_transition(trade):
            pnl = (mark_price - float(trade["entry_price"])) * int(trade["quantity"])
            trade["mark_price"] = mark_price
            if mark_price <= float(trade["stop_price"]):
                self._prepare_close(trade, mark_price)
            else:
                trade["unrealised_pnl"] = round(pnl, 2)
            return trade

        def after_transition(conn, trade):
            triggered = trade["status"] == "CLOSED"
            pnl = round(
                (mark_price - float(trade["entry_price"])) * int(trade["quantity"]),
                2,
            )
            self.db.event(
                "PAPER_TRADE_MARKED",
                {
                    "trade_id": trade_id,
                    "mark_price": mark_price,
                    "unrealised_pnl": 0 if triggered else round(pnl, 2),
                },
                conn=conn,
            )
            if triggered:
                self._record_close(conn, trade, "SIMULATED_STOP")

        trade = self.db.transition_open_trade(
            trade_id, prepare_transition, after_transition
        )
        if trade is None:
            raise ValueError("Trade is not open.")
        return self._as_model(trade)

    def close_trade(self, trade_id: str, exit_price: float, reason: str = "MANUAL"):
        if not isfinite(exit_price) or exit_price <= 0:
            raise ValueError("Exit price must be finite and greater than zero.")
        def prepare_transition(trade):
            self._prepare_close(trade, exit_price)
            return trade

        def after_transition(conn, trade):
            self._record_close(conn, trade, reason)

        trade = self.db.transition_open_trade(
            trade_id, prepare_transition, after_transition
        )
        if trade is None:
            raise ValueError("Trade is not open.")
        return self._as_model(trade)

    def _prepare_close(self, trade, exit_price: float):
        pnl = round(
            (exit_price - float(trade["entry_price"])) * int(trade["quantity"]),
            2,
        )
        closed_at = self.risk.current_time()
        trade["mark_price"] = exit_price
        trade["status"] = "CLOSED"
        trade["realised_pnl"] = pnl
        trade["unrealised_pnl"] = 0
        trade["closed_at"] = closed_at.isoformat()

    def _record_close(self, conn, trade, reason):
        pnl = float(trade["realised_pnl"])
        closed_at = datetime.fromisoformat(trade["closed_at"])
        self.risk.record_closed_trade(pnl, closed_at=closed_at, conn=conn)
        self.db.event(
            "PAPER_TRADE_CLOSED",
            {
                "trade_id": trade["trade_id"],
                "exit_price": trade["mark_price"],
                "pnl": pnl,
                "reason": reason,
            },
            conn=conn,
        )
        session_date = self.risk.trading_date(closed_at)
        active_session = self.db.get_state("session_date", conn=conn)
        snapshot = self.risk.snapshot(
            _conn=conn,
            _session_date=session_date,
        )
        if (
            active_session == session_date.isoformat()
            and snapshot.daily_pnl <= -snapshot.max_daily_loss_inr
        ):
            self.db.set_state("paused_for_day", True, conn=conn)
            self.db.event(
                "DAILY_LOSS_LIMIT_BREACHED",
                {"daily_pnl": snapshot.daily_pnl},
                conn=conn,
            )

    def _get_trade(self, trade_id):
        for trade in self.db.trades():
            if trade["trade_id"] == trade_id:
                return trade
        raise KeyError("Trade not found.")

    @staticmethod
    def _as_model(trade):
        # SQLite rows serialize datetime fields as ISO strings; Pydantic parses them.
        return PaperTrade.model_validate(trade)
