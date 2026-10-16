from dataclasses import dataclass


@dataclass
class RiskSnapshot:
    capital_inr: float
    max_daily_loss_inr: float
    max_trade_risk_inr: float
    realised_pnl: float
    unrealised_pnl: float
    daily_pnl: float
    consecutive_losses: int
    paused_for_day: bool
    kill_switch: bool
    open_positions: int


class RiskRejected(ValueError):
    pass


class RiskEngine:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        if db.get_state("kill_switch") is None:
            db.set_state("kill_switch", False)
        if db.get_state("consecutive_losses") is None:
            db.set_state("consecutive_losses", 0)
        if db.get_state("paused_for_day") is None:
            db.set_state("paused_for_day", False)

    def set_kill_switch(self, enabled: bool):
        self.db.set_state("kill_switch", enabled)
        self.db.event("KILL_SWITCH_CHANGED", {"enabled": enabled})

    def get_kill_switch(self) -> bool:
        return bool(self.db.get_state("kill_switch", False))

    def get_consecutive_losses(self) -> int:
        return int(self.db.get_state("consecutive_losses", 0))

    def paused_for_day(self) -> bool:
        return bool(self.db.get_state("paused_for_day", False))

    def record_closed_trade(self, pnl: float):
        streak = self.get_consecutive_losses()
        streak = streak + 1 if pnl < 0 else 0 if pnl > 0 else streak
        self.db.set_state("consecutive_losses", streak)
        if streak >= self.settings.max_consecutive_losses:
            self.db.set_state("paused_for_day", True)
            self.db.event("LOSS_STREAK_PAUSE", {"consecutive_losses": streak})

    def pnl(self):
        trades = self.db.trades()
        realised = sum(float(t["realised_pnl"]) for t in trades)
        unrealised = sum(float(t["unrealised_pnl"]) for t in trades if t["status"] == "OPEN")
        return realised, unrealised, realised + unrealised

    def open_positions(self):
        return sum(1 for t in self.db.trades() if t["status"] == "OPEN")

    def snapshot(self) -> RiskSnapshot:
        realised, unrealised, daily = self.pnl()
        return RiskSnapshot(
            capital_inr=self.settings.capital_inr,
            max_daily_loss_inr=round(self.settings.capital_inr * self.settings.max_daily_loss_pct, 2),
            max_trade_risk_inr=round(self.settings.capital_inr * self.settings.max_trade_risk_pct, 2),
            realised_pnl=round(realised, 2),
            unrealised_pnl=round(unrealised, 2),
            daily_pnl=round(daily, 2),
            consecutive_losses=self.get_consecutive_losses(),
            paused_for_day=self.paused_for_day(),
            kill_switch=self.get_kill_switch(),
            open_positions=self.open_positions(),
        )

    def validate_entry(self, *, entry_price, stop_price, quantity, lots, market_data_age_seconds):
        snap = self.snapshot()
        if snap.kill_switch:
            raise RiskRejected("Kill switch is active; entries are blocked.")
        if snap.paused_for_day:
            raise RiskRejected("Strategy is paused for the day after consecutive losses.")
        if snap.daily_pnl <= -snap.max_daily_loss_inr:
            self.db.set_state("paused_for_day", True)
            self.db.event("DAILY_LOSS_LIMIT_BREACHED", {"daily_pnl": snap.daily_pnl})
            raise RiskRejected("Daily loss limit breached; entries are blocked.")
        if snap.open_positions >= self.settings.max_open_positions:
            raise RiskRejected("Maximum open positions reached.")
        if lots > self.settings.max_lots_per_order:
            raise RiskRejected("Order exceeds configured max lots per order.")
        if market_data_age_seconds > self.settings.market_data_max_age_seconds:
            raise RiskRejected("Market data is stale; entry rejected.")
        if stop_price >= entry_price:
            raise RiskRejected("Stop price must be below entry for long-option trades.")
        estimated_risk = (entry_price - stop_price) * quantity
        if estimated_risk > snap.max_trade_risk_inr:
            raise RiskRejected(
                f"Estimated trade risk ₹{estimated_risk:.2f} exceeds limit ₹{snap.max_trade_risk_inr:.2f}."
            )
        return round(estimated_risk, 2)
