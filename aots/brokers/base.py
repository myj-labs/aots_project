from typing import Protocol


class BrokerReadOnly(Protocol):
    """Read-only broker boundary. Live order placement is intentionally absent."""

    def get_option_chain(self, underlying: str, expiry_date: str) -> dict:
        ...

    def get_positions(self) -> dict:
        ...

    def get_orders(self) -> dict:
        ...
