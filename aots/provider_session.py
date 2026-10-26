"""Runtime-only market-data provider session manager.

Credentials and tokens live only in this process's memory, inside the active
provider connection object. They are never written to SQLite, configuration,
audit events or logs. Python cannot guarantee secure erasure of immutable
``str`` objects; disconnecting drops every reference this module holds so the
values become eligible for garbage collection, but copies may linger until then.
"""

import logging
import threading
from typing import Callable, Literal, Mapping, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .brokers.base import AccountInsightsProvider, MarketDataProvider, ProviderError

logger = logging.getLogger("aots.provider")

PROVIDER_STATES = (
    "not_configured",
    "credentials_in_memory",
    "connecting",
    "connected",
    "failed",
    "disconnected",
)

AUTH_METHODS: dict[str, dict[str, tuple[str, ...]]] = {
    "groww": {
        "totp": ("api_key", "totp_secret"),
        "approval": ("api_key", "api_secret"),
        "access_token": ("access_token",),
    },
    "kite": {},
}
PROVIDER_LABELS = {"groww": "Groww", "kite": "Zerodha Kite"}
AVAILABLE_PROVIDERS = frozenset({"groww"})
CREDENTIAL_FIELDS = ("api_key", "totp_secret", "api_secret", "access_token")


class ProviderRequestError(Exception):
    """Safe-to-return API error; its message never contains credential values."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ProviderConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["groww", "kite"]
    auth_method: Literal["totp", "approval", "access_token"]
    api_key: SecretStr | None = Field(default=None, max_length=2048)
    totp_secret: SecretStr | None = Field(default=None, max_length=2048)
    api_secret: SecretStr | None = Field(default=None, max_length=2048)
    access_token: SecretStr | None = Field(default=None, max_length=4096)


class ProviderConnection(MarketDataProvider, AccountInsightsProvider, Protocol):
    """One provider's authenticated session; owns credential and token references."""

    def verify(self) -> None:
        """Authenticate and initialize this provider session."""

    def close(self) -> None:
        """Stop provider resources and discard credential/token references."""

    def get_historical_expiries(self, year: int, month: int) -> list[str]:
        """Return documented historical derivative expiries."""

    def get_historical_contracts(self, expiry_date: str) -> list[str]:
        """Return documented historical derivative contract symbols."""

    def get_historical_future_contract(self, expiry_date: str) -> str | None:
        """Return the exact listed NIFTY future symbol for an expiry, if present."""

    def check_historical_candles(
        self, groww_symbol, start_time, end_time, candle_interval
    ) -> dict:
        """Check access and response quality for historical option candles."""

    def get_historical_candles(
        self, segment, groww_symbol, start_time, end_time, candle_interval
    ) -> list:
        """Return read-only historical candles for the NIFTY index or an option."""


ConnectionFactory = Callable[[str, Mapping[str, str]], ProviderConnection]


def default_factories() -> dict[str, ConnectionFactory]:
    from .brokers.groww import GrowwRuntimeConnection

    return {"groww": lambda auth_method, creds: GrowwRuntimeConnection(auth_method, creds)}


class ProviderSessionManager:
    def __init__(self, factories: Mapping[str, ConnectionFactory] | None = None):
        self._factories = dict(factories) if factories is not None else default_factories()
        self._lock = threading.Lock()
        self._generation = 0
        self._connection: ProviderConnection | None = None
        self._provider: str | None = None
        self._auth_method: str | None = None
        self._state = "not_configured"
        self._failure: str | None = None

    def status(self) -> dict:
        with self._lock:
            state = self._state
            return {
                "provider": self._provider,
                "auth_method": self._auth_method,
                "state": state,
                "configured": self._connection is not None,
                "connected": state == "connected",
                "live_connection_implemented": state == "connected"
                or self._provider_implements_live(),
                "failure_reason": self._failure,
                "paper_only": True,
                "providers": [
                    {
                        "id": pid,
                        "label": PROVIDER_LABELS[pid],
                        "available": pid in AVAILABLE_PROVIDERS,
                        "auth_methods": list(AUTH_METHODS[pid]),
                    }
                    for pid in PROVIDER_LABELS
                ],
            }

    def _provider_implements_live(self) -> bool:
        return bool(getattr(self._connection, "live_connection_implemented", False))

    def _active_connection(self) -> ProviderConnection:
        with self._lock:
            connection = self._connection
            state = self._state
        if connection is None or state != "connected":
            raise ProviderRequestError("Provider is not connected.", 409)
        return cast(ProviderConnection, connection)

    def connect(self, request: ProviderConnectRequest) -> dict:
        provider = request.provider
        if provider not in AVAILABLE_PROVIDERS or provider not in self._factories:
            raise ProviderRequestError(f"{PROVIDER_LABELS[provider]} is not available yet.", 400)
        required = AUTH_METHODS[provider].get(request.auth_method)
        if required is None:
            raise ProviderRequestError("Unsupported authentication method for this provider.", 400)

        creds: dict[str, str] = {}
        for field in CREDENTIAL_FIELDS:
            secret = getattr(request, field)
            value = secret.get_secret_value() if secret is not None else ""
            if field in required:
                if not value.strip():
                    missing = ", ".join(f for f in required if not _present(request, f))
                    raise ProviderRequestError(f"Missing required credential fields: {missing}", 400)
                creds[field] = value
            elif value:
                raise ProviderRequestError(f"Field {field} is not used by this authentication method.", 400)

        # Defined cleanup path: any prior session is closed before a new attempt
        # so a failed attempt never leaves, or silently keeps, stale credentials.
        with self._lock:
            old = self._detach_locked()
            self._generation += 1
            generation = self._generation
        _safe_close(old)

        try:
            connection = self._factories[provider](request.auth_method, creds)
        except Exception:
            logger.warning("Provider session could not be created (details withheld).")
            with self._lock:
                if generation == self._generation:
                    self._state, self._provider = "failed", provider
                    self._auth_method = request.auth_method
                    self._failure = "Provider session could not be created."
            return self.status()
        finally:
            creds.clear()

        with self._lock:
            if generation != self._generation:
                superseded = True
            else:
                superseded = False
                self._connection = connection
                self._provider, self._auth_method = provider, request.auth_method
                self._state, self._failure = "connecting", None
        if superseded:
            _safe_close(connection)
            return self.status()

        outcome: tuple[str, str | None]
        try:
            connection.verify()
            outcome = ("connected", None)
        except NotImplementedError:
            outcome = ("credentials_in_memory", None)
        except ProviderError as exc:
            outcome = ("failed", exc.safe_message)
        except Exception:
            logger.warning("Provider verification failed (details withheld).")
            outcome = ("failed", "Provider verification failed.")

        with self._lock:
            if generation != self._generation:
                stale = True
            else:
                stale = False
                self._state, self._failure = outcome
                if outcome[0] == "failed":
                    self._connection = None
        if stale or outcome[0] == "failed":
            _safe_close(connection)
        return self.status()

    def index_quote(self, symbol: str) -> dict:
        try:
            quote = self._active_connection().get_index_quote(symbol)
        except ProviderError as exc:
            self._on_provider_error(exc)
        return quote.__dict__

    def option_chain(self, underlying: str, expiry: str) -> dict:
        try:
            chain = self._active_connection().get_option_chain_data(underlying, expiry)
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)
        return {
            **chain.__dict__,
            "strikes": [
                {
                    "strike": s.strike,
                    "call": _option_quote_payload(s.call),
                    "put": _option_quote_payload(s.put),
                }
                for s in chain.strikes
            ],
        }

    def account_insights(self) -> dict:
        try:
            return self._active_connection().get_account_insights()
        except ProviderError as exc:
            self._on_provider_error(exc)

    def historical_expiries(self, year: int, month: int) -> list[str]:
        try:
            return self._active_connection().get_historical_expiries(year, month)
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)

    def historical_contracts(self, expiry_date: str) -> list[str]:
        try:
            return self._active_connection().get_historical_contracts(expiry_date)
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)

    def historical_future_contract(self, expiry_date: str) -> str | None:
        try:
            return self._active_connection().get_historical_future_contract(expiry_date)
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)

    def check_historical_candles(
        self, groww_symbol, start_time, end_time, candle_interval
    ) -> dict:
        try:
            return self._active_connection().check_historical_candles(
                groww_symbol, start_time, end_time, candle_interval
            )
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)

    def historical_candles(
        self, segment, groww_symbol, start_time, end_time, candle_interval
    ) -> list:
        try:
            return self._active_connection().get_historical_candles(
                segment, groww_symbol, start_time, end_time, candle_interval
            )
        except ValueError as exc:
            raise ProviderRequestError(str(exc), 422)
        except ProviderError as exc:
            self._on_provider_error(exc)

    def _on_provider_error(self, exc: ProviderError):
        if exc.status_code == 401:
            with self._lock:
                old = self._detach_locked()
                self._state = "failed"
                self._failure = exc.safe_message
            _safe_close(old)
        raise ProviderRequestError(exc.safe_message, exc.status_code)

    def disconnect(self) -> dict:
        with self._lock:
            old = self._detach_locked()
            self._generation += 1
            self._state = "disconnected" if self._provider else "not_configured"
            self._failure = None
        _safe_close(old)
        return self.status()

    def shutdown(self) -> None:
        self.disconnect()

    def _detach_locked(self) -> ProviderConnection | None:
        old, self._connection = self._connection, None
        return old


def _present(request: ProviderConnectRequest, field: str) -> bool:
    secret = getattr(request, field)
    return secret is not None and bool(secret.get_secret_value().strip())


def _option_quote_payload(quote):
    if quote is None:
        return None
    return {
        "trading_symbol": quote.trading_symbol,
        "ltp": quote.ltp,
        "open_interest": quote.open_interest,
        "volume": quote.volume,
        "greeks": None if quote.greeks is None else quote.greeks.__dict__,
    }


def _safe_close(connection: ProviderConnection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        logger.warning("Provider session cleanup raised (details withheld).")