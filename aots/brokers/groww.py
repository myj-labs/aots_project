"""Groww read-only adapter boundary.

This module intentionally does not instantiate a Groww SDK client and has no order
placement method. Implement read-only calls only after checking current official docs,
account permissions, and rate limits. Never add live order methods without a separate
review and explicit user authorization.
"""
from dataclasses import dataclass


@dataclass
class GrowwReadOnlyConfig:
    access_token: str | None = None


class GrowwReadOnlyAdapter:
    def __init__(self, config: GrowwReadOnlyConfig):
        self.config = config

    def _require_token(self):
        if not self.config.access_token:
            raise RuntimeError("Groww read-only adapter is not configured; no credentials supplied.")

    def get_option_chain(self, underlying: str, expiry_date: str) -> dict:
        self._require_token()
        raise NotImplementedError(
            "Read-only option-chain API call is a future integration task. "
            "Consult current Groww API docs and implement with timeouts, schema validation, "
            "rate-limit handling and redacted logs."
        )

    def get_positions(self) -> dict:
        self._require_token()
        raise NotImplementedError("Read-only positions sync is not implemented yet.")

    def get_orders(self) -> dict:
        self._require_token()
        raise NotImplementedError("Read-only order sync is not implemented yet.")


class GrowwRuntimeConnection:
    """Runtime-only Groww session holding credentials in memory.

    The transport is a placeholder: ``verify`` makes no network call and
    raises NotImplementedError, so the session is never reported connected.
    """

    live_connection_implemented = False

    def __init__(self, auth_method: str, credentials):
        self.auth_method = auth_method
        self._credentials: dict[str, str] | None = dict(credentials)

    def __repr__(self) -> str:
        return f"GrowwRuntimeConnection(auth_method={self.auth_method!r})"

    def verify(self) -> None:
        if self._credentials is None:
            raise RuntimeError("Session is closed.")
        raise NotImplementedError("Groww token exchange is not implemented.")

    def close(self) -> None:
        if self._credentials is not None:
            self._credentials.clear()
        self._credentials = None
