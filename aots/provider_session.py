"""Runtime-only market-data provider session manager.

Credentials live only in this process's memory, inside the active provider
connection object. They are never written to SQLite, configuration, audit
events or logs. Python cannot guarantee secure erasure of immutable ``str``
objects; disconnecting drops every reference this module holds so the values
become eligible for garbage collection, but copies may linger until then.
"""
import logging
import threading
from typing import Callable, Literal, Mapping, Protocol
from pydantic import BaseModel, ConfigDict, Field, SecretStr

logger = logging.getLogger("aots.provider")

PROVIDER_STATES = (
    "not_configured",
    "credentials_in_memory",
    "connecting",
    "connected",
    "failed",
    "disconnected",
)

# Required credential fields per provider and authentication method.
# Groww: derived from secondary sources describing the Groww Trade API; the
# official documentation could not be read in this environment (see README).
AUTH_METHODS: dict[str, dict[str, tuple[str, ...]]] = {
    "groww": {
        "totp": ("api_key", "totp_secret"),
        "approval": ("api_key", "api_secret"),
    },
    "kite": {},
}
PROVIDER_LABELS = {"groww": "Groww", "kite": "Zerodha Kite"}
AVAILABLE_PROVIDERS = frozenset({"groww"})
CREDENTIAL_FIELDS = ("api_key", "totp_secret", "api_secret")


class ProviderRequestError(Exception):
    """Safe-to-return error; its message never contains credential values."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ProviderConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["groww", "kite"]
    auth_method: Literal["totp", "approval"]
    api_key: SecretStr | None = Field(default=None, max_length=512)
    totp_secret: SecretStr | None = Field(default=None, max_length=512)
    api_secret: SecretStr | None = Field(default=None, max_length=512)


class ProviderConnection(Protocol):
    """One provider's authenticated session; owns the credential references."""

    def verify(self) -> None:
        """Perform a documented verification request.

        Return normally only after a real successful verification. Raise
        NotImplementedError when the transport is not implemented.
        """

    def close(self) -> None:
        """Stop provider resources and discard credential references."""


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

    def connect(self, request: ProviderConnectRequest) -> dict:
        provider = request.provider
        if provider not in AVAILABLE_PROVIDERS or provider not in self._factories:
            raise ProviderRequestError(
                f"{PROVIDER_LABELS[provider]} is not available yet.", 400
            )
        required = AUTH_METHODS[provider].get(request.auth_method)
        if required is None:
            raise ProviderRequestError("Unsupported authentication method for this provider.", 400)
        creds: dict[str, str] = {}
        for field in CREDENTIAL_FIELDS:
            secret = getattr(request, field)
            value = secret.get_secret_value() if secret is not None else ""
            if field in required:
                if not value.strip():
                    raise ProviderRequestError(
                        "Missing required credential fields: "
                        + ", ".join(f for f in required if not _present(request, f)),
                        400,
                    )
                creds[field] = value
            elif value:
                raise ProviderRequestError(
                    f"Field {field} is not used by this authentication method.", 400
                )

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


def _safe_close(connection: ProviderConnection | None) -> None:
    if connection is None:
        return
    try:
        connection.close()
    except Exception:
        logger.warning("Provider session cleanup raised (details withheld).")
