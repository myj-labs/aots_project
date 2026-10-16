from dataclasses import dataclass
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
