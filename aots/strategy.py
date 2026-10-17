from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Protocol


@dataclass(frozen=True)
class MarketSnapshot:
    underlying: str
    spot: float
    timestamp_epoch: float
    option_contracts: list[dict]


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str
    entry_price: float
    stop_price: float
    quantity: int
    lots: int
    rationale: str


@dataclass(frozen=True)
class StrategyCandidate:
    symbol: str
    entry_price: float
    stop_price: float
    quantity: int
    lots: int
    take_profit_profit_inr: float | None = None


@dataclass(frozen=True)
class StrategyDecision:
    strategy_id: str
    strategy_version: str
    observation_id: str
    evaluated_at: str
    status: str
    reason: str
    inputs: dict
    signal: dict | None
    resulting_trade_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "observation_id": self.observation_id,
            "evaluated_at": self.evaluated_at,
            "status": self.status,
            "reason": self.reason,
            "inputs": self.inputs,
            "signal": self.signal,
            "resulting_trade_id": self.resulting_trade_id,
        }


class Strategy(Protocol):
    """Implement this interface for a custom strategy; no strategy is enabled by default."""

    name: str

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        ...


class NoOpStrategy:
    """Safe default: never generates trades."""

    name = "no-op"

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        return None


class SpotThresholdStrategy:
    """Demonstration rule over explicitly supplied spot and candidate inputs."""

    name = "spot-threshold-demo"
    version = "1"

    def __init__(self, threshold: float | None):
        if threshold is not None and (
            not isfinite(float(threshold)) or float(threshold) <= 0
        ):
            raise ValueError("Strategy spot threshold must be finite and positive.")
        self.threshold = threshold

    def evaluate(self, snapshot: MarketSnapshot) -> Signal | None:
        if (
            self.threshold is None
            or not isinstance(snapshot.underlying, str)
            or not snapshot.underlying.strip()
            or not isinstance(snapshot.spot, (int, float))
            or isinstance(snapshot.spot, bool)
            or not isfinite(snapshot.spot)
            or snapshot.spot <= 0
            or not isinstance(snapshot.timestamp_epoch, (int, float))
            or isinstance(snapshot.timestamp_epoch, bool)
            or not isfinite(snapshot.timestamp_epoch)
            or snapshot.timestamp_epoch <= 0
        ):
            return None
        if snapshot.spot < self.threshold:
            return None
        if not snapshot.option_contracts:
            return None
        candidate = snapshot.option_contracts[0]
        if not isinstance(candidate, dict):
            return None
        symbol = candidate.get("symbol")
        entry_price = candidate.get("entry_price")
        stop_price = candidate.get("stop_price")
        quantity = candidate.get("quantity")
        lots = candidate.get("lots")
        target = candidate.get("take_profit_profit_inr")
        candidate_prices = (entry_price, stop_price)
        if (
            not isinstance(symbol, str)
            or not symbol.strip()
            or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(value)
                or value <= 0
                for value in candidate_prices
            )
            or stop_price >= entry_price
            or not isinstance(quantity, int)
            or isinstance(quantity, bool)
            or quantity <= 0
            or not isinstance(lots, int)
            or isinstance(lots, bool)
            or lots <= 0
            or (
                target is not None
                and (
                    not isinstance(target, (int, float))
                    or isinstance(target, bool)
                    or not isfinite(target)
                    or target <= 0
                )
            )
        ):
            return None
        return Signal(
            symbol=symbol,
            side="BUY",
            entry_price=entry_price,
            stop_price=stop_price,
            quantity=quantity,
            lots=lots,
            rationale=(
                f"Supplied {snapshot.underlying} spot {snapshot.spot:g} "
                f"met configured threshold {self.threshold:g}."
            ),
        )


def evaluate_spot_threshold(
    *,
    observation_id: str,
    underlying: str,
    spot: float,
    observed_at: datetime,
    candidate: StrategyCandidate,
    threshold: float | None,
    expected_underlying: str,
    evaluated_at: datetime,
    max_age_seconds: int,
) -> StrategyDecision:
    """Pure decision evaluation; timestamps and all market inputs are explicit."""
    normalized_inputs = {
        "underlying": underlying,
        "spot": spot,
        "observed_at": observed_at.isoformat(),
        "symbol": candidate.symbol,
        "entry_price": candidate.entry_price,
        "stop_price": candidate.stop_price,
        "quantity": candidate.quantity,
        "lots": candidate.lots,
        "take_profit_profit_inr": candidate.take_profit_profit_inr,
        "spot_threshold": threshold,
        "expected_underlying": expected_underlying,
        "max_age_seconds": max_age_seconds,
    }
    evaluated_at = evaluated_at.astimezone(timezone.utc)
    age = (evaluated_at - observed_at).total_seconds()
    status = "REJECTED"
    reason = ""
    signal = None

    if threshold is None:
        reason = "Strategy is not configured; set AOTS_STRATEGY_SPOT_THRESHOLD."
    elif age < 0:
        reason = "Observation timestamp is in the future."
    elif age > max_age_seconds:
        reason = "Observation is stale; supplied data exceeds the configured age limit."
    elif underlying != expected_underlying:
        reason = (
            "This demonstration strategy supports only the configured "
            f"{expected_underlying} underlying."
        )
    else:
        snapshot = MarketSnapshot(
            underlying=underlying,
            spot=spot,
            timestamp_epoch=observed_at.timestamp(),
            option_contracts=[candidate.__dict__],
        )
        strategy = SpotThresholdStrategy(threshold)
        evaluated = strategy.evaluate(snapshot)
        if evaluated is None:
            status = "NO_SIGNAL"
            reason = f"Supplied spot {spot:g} is below configured threshold {threshold:g}."
        else:
            status = "SIGNAL"
            reason = evaluated.rationale
            signal = {
                "symbol": evaluated.symbol,
                "side": evaluated.side,
                "entry_price": evaluated.entry_price,
                "stop_price": evaluated.stop_price,
                "quantity": evaluated.quantity,
                "lots": evaluated.lots,
                "take_profit_profit_inr": candidate.take_profit_profit_inr,
            }

    return StrategyDecision(
        strategy_id=SpotThresholdStrategy.name,
        strategy_version=SpotThresholdStrategy.version,
        observation_id=observation_id,
        evaluated_at=evaluated_at.isoformat().replace("+00:00", "Z"),
        status=status,
        reason=reason,
        inputs=normalized_inputs,
        signal=signal,
    )
