"""Groww read-only adapter boundary.

This module intentionally does not instantiate a Groww SDK client and has no order
placement method. Implement read-only calls only after checking current official docs,
account permissions, and rate limits. Never add live order methods without a separate
review and explicit user authorization.
"""
import base64
import binascii
import hashlib
import hmac
import json
import math
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Mapping, Protocol


from .base import (
    Greeks,
    IndexQuote,
    OptionChain,
    OptionQuote,
    OptionStrike,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    ProviderRejectedError,
    ProviderResponseError,
    ProviderUnavailableError,
)


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


# Verified against the official Groww cURL docs
# (https://groww.in/trade-api/docs/curl and .../curl/live-data) and the growwapi SDK source.
GROWW_BASE_URL = "https://api.groww.in"
TOKEN_PATH = "/v1/token/api/access"
API_VERSION = "1.0"
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 2_000_000
MAX_STRIKES = 2_000
IST = timezone(timedelta(hours=5, minutes=30))
SUPPORTED_INDEX = "NIFTY"
SUPPORTED_EXCHANGE = "NSE"
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{8,4096}$")
_EXPIRY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TransportError(Exception):
    """Network-level failure (DNS, connection, timeout). Carries no request details."""


class HttpTransport(Protocol):
    def request(
        self, method: str, url: str, headers: Mapping[str, str], json_body: Any | None
    ) -> tuple[int, Any]:
        """Return (HTTP status, parsed JSON or None). Must not return raw bodies."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Never follow redirects: the Authorization header must not be replayed elsewhere.
    def redirect_request(self, *args, **kwargs):
        return None


class UrllibTransport:
    """HTTPS transport with a hard timeout, a response-size cap and no redirects or retries."""

    def __init__(self, timeout: float = REQUEST_TIMEOUT_SECONDS):
        self._timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(self, method, url, headers, json_body):
        if not url.startswith(GROWW_BASE_URL + "/"):
            raise TransportError()
        data = None if json_body is None else json.dumps(json_body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                status, raw = resp.status, resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            exc.close()
            return exc.code, None
        except Exception:
            raise TransportError() from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise TransportError()
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, None


def generate_checksum(secret: str, timestamp: str) -> str:
    """Documented checksum: SHA-256 hex of the API secret followed by the epoch-seconds string."""
    return hashlib.sha256((secret + timestamp).encode("utf-8")).hexdigest()


def totp_now(seed: str, now_epoch: float, digits: int = 6, interval: int = 30) -> str:
    """RFC 6238 TOTP (HMAC-SHA1, 30 s, 6 digits) from a base32 seed, as pyotp.TOTP(seed).now()."""
    cleaned = re.sub(r"[\s-]", "", seed).upper()
    try:
        key = base64.b32decode(cleaned + "=" * (-len(cleaned) % 8), casefold=True)
    except (binascii.Error, ValueError):
        raise ProviderAuthError("The TOTP secret is not valid base32.") from None
    if not key:
        raise ProviderAuthError("The TOTP secret is not valid base32.")
    counter = struct.pack(">Q", int(now_epoch // interval))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def next_6am_ist(issued: datetime) -> datetime:
    """Groww access tokens expire daily at 06:00 IST; return the next such instant."""
    local = issued.astimezone(IST)
    boundary = datetime.combine(local.date(), dtime(6, 0), tzinfo=IST)
    if local >= boundary:
        boundary += timedelta(days=1)
    return boundary.astimezone(timezone.utc)


def _number(value: Any, *, positive: bool = False, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderResponseError()
    if positive and value <= 0:
        raise ProviderResponseError()
    return float(value)


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProviderResponseError()
    if value < 0 or value != int(value):
        raise ProviderResponseError()
    return int(value)


def validate_expiry(expiry: str) -> str:
    """Strict YYYY-MM-DD calendar date; raises ValueError (safe message) otherwise."""
    if not isinstance(expiry, str) or not _EXPIRY_RE.match(expiry):
        raise ValueError("expiry must be a date in YYYY-MM-DD format.")
    try:
        date.fromisoformat(expiry)
    except ValueError:
        raise ValueError("expiry must be a real calendar date.") from None
    return expiry


class GrowwRuntimeConnection:
    """Runtime-only Groww session: documented token exchange plus read-only market data.

    Credentials are held only until the token exchange finishes; afterwards only the
    access token is kept, in memory. Nothing here places, modifies or cancels orders.
    """

    live_connection_implemented = True

    def __init__(
        self,
        auth_method: str,
        credentials: Mapping[str, str],
        transport: HttpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.auth_method = auth_method
        self._credentials: dict[str, str] | None = dict(credentials)
        self._transport = transport or UrllibTransport()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def __repr__(self) -> str:
        return f"GrowwRuntimeConnection(auth_method={self.auth_method!r})"

    def verify(self) -> None:
        creds = self._credentials
        if creds is None or self._token is not None:
            raise ProviderResponseError("Session is not in a connectable state.")
        now = self._clock()
        if self.auth_method == "approval":
            timestamp = str(int(now.timestamp()))
            body = {
                "key_type": "approval",
                "checksum": generate_checksum(creds["api_secret"], timestamp),
                "timestamp": timestamp,
            }
        elif self.auth_method == "totp":
            body = {"key_type": "totp", "totp": totp_now(creds["totp_secret"], now.timestamp())}
        else:
            raise ProviderAuthError("Unsupported authentication method.")
        headers = {
            "Authorization": "Bearer " + creds["api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        status, data = self._send("POST", GROWW_BASE_URL + TOKEN_PATH, headers, body, auth=True)
        token = data.get("token") if isinstance(data, dict) else None
        is_active = data.get("isActive") if isinstance(data, dict) else None
        if (
            status != 200
            or not isinstance(token, str)
            or not _TOKEN_RE.match(token)
            or is_active is False
            or (is_active is not None and is_active is not True)
        ):
            raise ProviderResponseError()
        self._token = token
        self._expires_at = next_6am_ist(now)
        # The long-lived secrets are no longer needed once the token exists.
        creds.clear()
        self._credentials = None

    def expired(self) -> bool:
        return self._expires_at is not None and self._clock() >= self._expires_at

    def close(self) -> None:
        if self._credentials is not None:
            self._credentials.clear()
        self._credentials = None
        self._token = None
        self._expires_at = None

    def _send(self, method, url, headers, body=None, auth=False):
        try:
            status, data = self._transport.request(method, url, headers, body)
        except ProviderError:
            raise
        except Exception:
            raise ProviderUnavailableError() from None
        error = data.get("error") if isinstance(data, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if code == "GA005":
            raise ProviderAuthError(
                "Groww rejected the credentials."
                if auth
                else "Groww rejected the access token (invalid or expired); reconnect."
            )
        if status in (401, 403):
            raise ProviderAuthError(
                "Groww rejected the credentials." if auth
                else "Groww rejected the access token (invalid or expired); reconnect."
            )
        if status == 429:
            raise ProviderRateLimitError()
        if status in (400, 404):
            raise ProviderRejectedError(
                "Groww rejected the credentials." if auth
                else "Groww rejected the request; check the symbol and expiry."
            )
        if status >= 500 or status < 200:
            raise ProviderUnavailableError()
        if status != 200:
            raise ProviderResponseError()
        return status, data

    def _get(self, path: str, query: dict[str, str]) -> dict:
        token = self._token
        if token is None or self.expired():
            raise ProviderAuthError("The access token is missing or expired; reconnect.")
        url = GROWW_BASE_URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "X-API-VERSION": API_VERSION,
        }
        _, data = self._send("GET", url, headers)
        if not isinstance(data, dict) or data.get("status") != "SUCCESS":
            if isinstance(data, dict) and data.get("status") == "FAILURE":
                raise ProviderRejectedError("Groww reported a failure for this request.")
            raise ProviderResponseError()
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ProviderResponseError()
        return payload

    def get_index_quote(self, symbol: str) -> IndexQuote:
        if symbol != SUPPORTED_INDEX:
            raise ProviderRejectedError("Only the NIFTY index is supported.")
        payload = self._get(
            "/v1/live-data/quote",
            {"exchange": SUPPORTED_EXCHANGE, "segment": "CASH", "trading_symbol": symbol},
        )
        last_time = payload.get("last_trade_time")
        return IndexQuote(
            symbol=symbol,
            exchange=SUPPORTED_EXCHANGE,
            last_price=_number(payload.get("last_price"), positive=True),
            last_trade_time_ms=_count(last_time) if last_time is not None else None,
            source="groww",
            fetched_at=self._clock().astimezone(timezone.utc).isoformat(),
        )

    def get_option_chain_data(self, underlying: str, expiry: str) -> OptionChain:
        if underlying != SUPPORTED_INDEX:
            raise ProviderRejectedError("Only the NIFTY index is supported.")
        validate_expiry(expiry)
        payload = self._get(
            f"/v1/option-chain/exchange/{SUPPORTED_EXCHANGE}/underlying/{underlying}",
            {"expiry_date": expiry},
        )
        strikes_raw = payload.get("strikes")
        if not isinstance(strikes_raw, dict) or len(strikes_raw) > MAX_STRIKES:
            raise ProviderResponseError()
        strikes = []
        for key, sides in strikes_raw.items():
            try:
                strike = float(key)
            except (TypeError, ValueError):
                raise ProviderResponseError() from None
            if not math.isfinite(strike) or strike <= 0 or not isinstance(sides, dict):
                raise ProviderResponseError()
            strikes.append(
                OptionStrike(strike, _option_quote(sides.get("CE")), _option_quote(sides.get("PE")))
            )
        strikes.sort(key=lambda s: s.strike)
        return OptionChain(
            underlying=underlying,
            exchange=SUPPORTED_EXCHANGE,
            expiry=expiry,
            underlying_ltp=_number(payload.get("underlying_ltp"), positive=True),
            source="groww",
            fetched_at=self._clock().astimezone(timezone.utc).isoformat(),
            strikes=strikes,
        )


def _option_quote(raw: Any) -> OptionQuote | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("trading_symbol"), str):
        raise ProviderResponseError()
    greeks_raw = raw.get("greeks")
    greeks = None
    if greeks_raw is not None:
        if not isinstance(greeks_raw, dict):
            raise ProviderResponseError()
        greeks = Greeks(**{
            name: _number(greeks_raw.get(name), allow_none=True)
            for name in ("delta", "gamma", "theta", "vega", "rho", "iv")
        })
    return OptionQuote(
        trading_symbol=raw["trading_symbol"],
        ltp=_number(raw.get("ltp"), allow_none=True),
        open_interest=_count(raw.get("open_interest")),
        volume=_count(raw.get("volume")),
        greeks=greeks,
    )