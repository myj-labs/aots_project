from dataclasses import dataclass, field
from typing import Protocol


class BrokerReadOnly(Protocol):
    """Read-only broker boundary. Live order placement is intentionally absent."""

    def get_option_chain(self, underlying: str, expiry_date: str) -> dict:
        ...

    def get_positions(self) -> dict:
        ...

    def get_orders(self) -> dict:
        ...


class ProviderError(Exception):
    """Provider failure carrying only a fixed, secret-free message."""

    safe_message = "Provider request failed."
    status_code = 502

    def __init__(self, message: str | None = None):
        self.safe_message = message or type(self).safe_message
        super().__init__(self.safe_message)


class ProviderAuthError(ProviderError):
    safe_message = "Provider rejected the credentials or access token."
    status_code = 401


class ProviderRejectedError(ProviderError):
    safe_message = "Provider rejected the request."
    status_code = 400


class ProviderRateLimitError(ProviderError):
    safe_message = "Provider rate limit reached; try again later."
    status_code = 429


class ProviderUnavailableError(ProviderError):
    safe_message = "Provider is unreachable or returned a server error."
    status_code = 503


class ProviderResponseError(ProviderError):
    safe_message = "Provider returned an unexpected response."


@dataclass(frozen=True)
class IndexQuote:
    symbol: str
    exchange: str
    last_price: float
    last_trade_time_ms: int | None
    source: str
    fetched_at: str


@dataclass(frozen=True)
class Greeks:
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    rho: float | None = None
    iv: float | None = None


@dataclass(frozen=True)
class OptionQuote:
    trading_symbol: str
    ltp: float | None
    open_interest: int | None
    volume: int | None
    greeks: Greeks | None = None


@dataclass(frozen=True)
class OptionStrike:
    strike: float
    call: OptionQuote | None = None
    put: OptionQuote | None = None


@dataclass(frozen=True)
class OptionChain:
    underlying: str
    exchange: str
    expiry: str
    underlying_ltp: float
    source: str
    fetched_at: str
    strikes: list[OptionStrike] = field(default_factory=list)


class MarketDataProvider(Protocol):
    """Provider-neutral, read-only market data. Strategy and risk code depend only on this."""

    def get_index_quote(self, symbol: str) -> IndexQuote:
        ...

    def get_option_chain_data(self, underlying: str, expiry: str) -> OptionChain:
        ...