from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

KOLKATA = timezone(timedelta(hours=5, minutes=30), "Asia/Kolkata")


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
    def __init__(self, settings, db, clock: Callable[[], datetime] | None = None):
        self.settings = settings
        self.db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._ensure_session()
        if db.get_state("kill_switch") is None:
            db.set_state("kill_switch", False)

    def current_time(self) -> datetime:
        return self._as_utc(self._clock())

    @staticmethod
    def _as_utc(now: datetime) -> datetime:
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.astimezone(timezone.utc)

    def _ensure_session(self, at: datetime | None = None) -> date:
        session_time = self.current_time() if at is None else self._as_utc(at)
        session_date = session_time.astimezone(KOLKATA).date()
        self.db.ensure_session(session_date)
        return session_date

    def set_kill_switch(self, enabled: bool):
        self._ensure_session()
        self.db.set_state("kill_switch", enabled)
        self.db.event("KILL_SWITCH_CHANGED", {"enabled": enabled})

    def get_kill_switch(self) -> bool:
        self._ensure_session()
        return bool(self.db.get_state("kill_switch", False))

    def get_consecutive_losses(self) -> int:
        self._ensure_session()
        return int(self.db.get_state("consecutive_losses", 0))

    def paused_for_day(self) -> bool:
        self._ensure_session()
        return bool(self.db.get_state("paused_for_day", False))

    def record_closed_trade(self, pnl: float, *, closed_at: datetime | None = None):
        self._ensure_session(closed_at)
        streak = int(self.db.get_state("consecutive_losses", 0))
        streak = streak + 1 if pnl < 0 else 0 if pnl > 0 else streak
        self.db.set_state("consecutive_losses", streak)
        if streak >= self.settings.max_consecutive_losses:
            self.db.set_state("paused_for_day", True)
            self.db.event("LOSS_STREAK_PAUSE", {"consecutive_losses": streak})

    def pnl(self):
        session_date = self._ensure_session()
        return self._pnl_for_session(session_date, self.db.trades())

    @staticmethod
    def _pnl_for_session(session_date: date, trades: list[dict]):
        realised = sum(
            float(t["realised_pnl"])
            for t in trades
            if t["status"] == "CLOSED"
            and t["closed_at"]
            and RiskEngine._closed_on_date(t["closed_at"]) == session_date
        )
        unrealised = sum(float(t["unrealised_pnl"]) for t in trades if t["status"] == "OPEN")
        return realised, unrealised, realised + unrealised

    @staticmethod
    def _closed_on_date(closed_at: str) -> date:
        timestamp = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(KOLKATA).date()

    def open_positions(self):
        self._ensure_session()
        return sum(1 for t in self.db.trades() if t["status"] == "OPEN")

    def snapshot(self) -> RiskSnapshot:
        session_date = self._ensure_session()
        trades = self.db.trades()
        realised, unrealised, daily = self._pnl_for_session(session_date, trades)
        return RiskSnapshot(
            capital_inr=self.settings.capital_inr,
            max_daily_loss_inr=round(self.settings.capital_inr * self.settings.max_daily_loss_pct, 2),
            max_trade_risk_inr=round(self.settings.capital_inr * self.settings.max_trade_risk_pct, 2),
            realised_pnl=round(realised, 2),
            unrealised_pnl=round(unrealised, 2),
            daily_pnl=round(daily, 2),
            consecutive_losses=int(self.db.get_state("consecutive_losses", 0)),
            paused_for_day=bool(self.db.get_state("paused_for_day", False)),
            kill_switch=bool(self.db.get_state("kill_switch", False)),
            open_positions=sum(1 for t in trades if t["status"] == "OPEN"),
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
