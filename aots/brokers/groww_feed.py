"""Managed, read-only Groww market-data feed.

The Groww SDK convenience feed writes NATS credentials to fixed files in the
working directory and does not expose a public shutdown method. This adapter
uses Groww's protocol definitions with a bounded token request and handles NATS
credentials in memory, with a managed event-loop thread.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import logging
import math
import os
import queue
import re
import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import urlopen
from typing import Callable

from google.protobuf.message import DecodeError
from nacl.signing import SigningKey
from nats.aio.client import Client
import nkeys

from .response_log import LIVE_FEED_DIAGNOSTICS


GROWW_FEED_SOCKET_URL = "wss://socket-api.groww.in"
GROWW_SOCKET_TOKEN_URL = "https://api.groww.in/v1/api/apex/v1/socket/token/create"
INSTRUMENT_MASTER_TIMEOUT_SECONDS = 10
MAX_INSTRUMENT_MASTER_BYTES = 32_000_000
logger = logging.getLogger("aots.groww_feed")


class MarketFeedError(RuntimeError):
    """A safe, secret-free feed or instrument-resolution error."""


@dataclass(frozen=True)
class FeedInstrument:
    groww_symbol: str
    exchange: str
    segment: str
    exchange_token: str

    def as_subscription(self) -> dict[str, str]:
        return {
            "exchange": self.exchange,
            "segment": self.segment,
            "exchange_token": self.exchange_token,
        }


@dataclass(frozen=True)
class MarketTick:
    groww_symbol: str
    segment: str
    price: float
    timestamp: datetime


class GrowwInstrumentCatalog:
    """Resolve exact Groww symbols from the public instrument master."""

    def __init__(self, loader: Callable[[], bytes] | None = None):
        self._loader = loader or self._download_master
        self._rows: dict[str, dict[str, str]] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _download_master() -> bytes:
        from growwapi.groww.client import GrowwAPI

        try:
            with urlopen(
                GrowwAPI.INSTRUMENT_CSV_URL,
                timeout=INSTRUMENT_MASTER_TIMEOUT_SECONDS,
            ) as response:
                payload = response.read(MAX_INSTRUMENT_MASTER_BYTES + 1)
        except (OSError, URLError, TimeoutError):
            raise MarketFeedError("Groww instrument master could not be downloaded.") from None
        if len(payload) > MAX_INSTRUMENT_MASTER_BYTES:
            raise MarketFeedError("Groww instrument master exceeded the allowed size.")
        return payload

    def _load_rows(self) -> dict[str, dict[str, str]]:
        with self._lock:
            if self._rows is not None:
                return self._rows
            payload = self._loader()
            try:
                text = payload.decode("utf-8-sig")
                reader = csv.DictReader(io.StringIO(text))
                required = {"groww_symbol", "exchange", "segment", "exchange_token"}
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    raise MarketFeedError("Groww instrument master has an unsupported schema.")
                rows: dict[str, dict[str, str]] = {}
                for row in reader:
                    symbol = row.get("groww_symbol", "")
                    if symbol:
                        rows.setdefault(symbol, row)
            except (UnicodeDecodeError, csv.Error):
                raise MarketFeedError("Groww instrument master could not be parsed.") from None
            self._rows = rows
            return rows

    def resolve(self, groww_symbol: str, segment: str) -> FeedInstrument:
        if not groww_symbol.startswith("NSE-NIFTY-") and groww_symbol != "NSE-NIFTY":
            raise MarketFeedError("Only exact NSE NIFTY instruments are supported.")
        if segment not in {"CASH", "FNO"}:
            raise MarketFeedError("Unsupported Groww instrument segment.")
        row = self._load_rows().get(groww_symbol)
        if row is None:
            raise MarketFeedError("The requested instrument is not in Groww's instrument master.")
        exchange = row.get("exchange", "")
        row_segment = row.get("segment", "")
        token = row.get("exchange_token", "")
        valid_token = (
            segment == "CASH" and token == "NIFTY"
        ) or (
            segment == "FNO"
            and token.isascii()
            and token.isdigit()
        )
        if (
            exchange != "NSE"
            or row_segment != segment
            or not token
            or len(token) > 32
            or not valid_token
        ):
            raise MarketFeedError("Groww instrument metadata did not match the requested instrument.")
        return FeedInstrument(groww_symbol, exchange, segment, token)


class GrowwMarketFeed:
    """One NATS connection; callbacks contain only validated LTP/index observations."""

    def __init__(
        self,
        access_token: str,
        on_tick: Callable[[MarketTick], None],
        on_error: Callable[[str], None],
        *,
        transport,
        client_factory: Callable[[], Client] = Client,
        startup_timeout_seconds: float = 15,
    ):
        self._access_token = access_token
        self._transport = transport
        self._on_tick = on_tick
        self._on_error = on_error
        self._client_factory = client_factory
        self._startup_timeout = startup_timeout_seconds
        self._client: Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._startup_error: str | None = None
        self._subscriptions: dict[str, str] = {}
        self._lock = threading.RLock()
        self._tick_queue: queue.Queue[MarketTick | str | None] = queue.Queue(maxsize=10_000)
        self._dispatch_thread: threading.Thread | None = None

    def _socket_credentials(self, access_token: str):
        from growwapi.groww.client import GrowwAPI

        LIVE_FEED_DIAGNOSTICS.record(
            "groww_feed", "socket_token_request", "started"
        )
        seed = os.urandom(32)
        key_pair = nkeys.KeyPair(
            keys=SigningKey(seed),
            seed=nkeys.encode_seed(seed, nkeys.PREFIX_BYTE_USER),
        )
        try:
            status, response = self._transport.request(
                "POST",
                GROWW_SOCKET_TOKEN_URL,
                GrowwAPI._build_headers(access_token),
                {"socketKey": key_pair.public_key.decode("utf-8")},
            )
        except Exception as exc:
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "socket_token_request",
                "failed",
                error=exc,
            )
            raise MarketFeedError("Groww socket authentication could not be initialized.") from None
        payload = response.get("payload", response) if isinstance(response, dict) else None
        socket_jwt = payload.get("token") if status == 200 and isinstance(payload, dict) else None
        if not isinstance(socket_jwt, str) or not socket_jwt:
            error = response.get("error") if isinstance(response, dict) else None
            provider_code = error.get("code") if isinstance(error, dict) else None
            code = (
                provider_code
                if isinstance(provider_code, (str, int))
                and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", str(provider_code))
                else None
            )
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "socket_token_response",
                "failed",
                details={
                    "http_status": status,
                    "provider_error_code": code,
                    "response_type": type(response).__name__,
                    "response_keys": sorted(response.keys())[:20]
                    if isinstance(response, dict)
                    else [],
                },
            )
            raise MarketFeedError("Groww did not return a valid socket token.")
        LIVE_FEED_DIAGNOSTICS.record(
            "groww_feed",
            "socket_token_response",
            "succeeded",
            details={"http_status": status},
        )
        return socket_jwt, lambda nonce: base64.b64encode(
            key_pair.sign(nonce.encode("utf-8"))
        )

    def start(self, index_instrument: FeedInstrument) -> None:
        from growwapi.groww.constants import FeedConstants

        if index_instrument.segment != "CASH":
            raise ValueError("The initial Groww feed instrument must be the NIFTY CASH index.")
        with self._lock:
            if self._thread is not None:
                raise MarketFeedError("Groww market feed is already started.")
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "nats_start",
                "started",
                details={
                    "host": "socket-api.groww.in",
                    "exchange": index_instrument.exchange,
                    "segment": index_instrument.segment,
                },
            )
            jwt, signature_callback = self._socket_credentials(self._access_token)
            topic = FeedConstants.get_live_index_topic(
                index_instrument.segment,
                index_instrument.exchange,
                index_instrument.exchange_token,
            ).get_topic()
            self._thread = threading.Thread(
                target=self._run,
                args=(jwt, signature_callback, [(topic, index_instrument)]),
                name="aots-groww-market-feed",
                daemon=True,
            )
            self._dispatch_thread = threading.Thread(
                target=self._dispatch_ticks,
                name="aots-groww-market-data-dispatch",
                daemon=True,
            )
            self._dispatch_thread.start()
            self._thread.start()
        if not self._ready.wait(self._startup_timeout):
            self.stop()
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "nats_start",
                "failed",
                details={"reason": "startup_timeout"},
            )
            raise MarketFeedError("Timed out while starting the Groww market feed.")
        if self._startup_error:
            self.stop()
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "nats_start",
                "failed",
                details={"reason": self._startup_error},
            )
            raise MarketFeedError(self._startup_error)
        LIVE_FEED_DIAGNOSTICS.record(
            "groww_feed", "nats_start", "succeeded"
        )

    def subscribe_ltp(self, instrument: FeedInstrument) -> None:
        from growwapi.groww.constants import FeedConstants

        if instrument.segment != "FNO":
            raise ValueError("Option LTP subscriptions must use the FNO segment.")
        with self._lock:
            if self._thread is None or not self._ready.is_set():
                raise MarketFeedError("Groww market feed is not running.")
            if instrument.groww_symbol in self._subscriptions.values():
                return
            topic = FeedConstants.get_live_price_topic(
                instrument.segment,
                instrument.exchange,
                instrument.exchange_token,
            ).get_topic()
            loop = self._loop
        if loop is None:
            raise MarketFeedError("Groww market feed is not ready.")
        future = asyncio.run_coroutine_threadsafe(
            self._subscribe(topic, instrument), loop
        )
        try:
            future.result(timeout=10)
        except Exception:
            raise MarketFeedError("Groww option-price subscription failed.") from None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested.is_set()

    def request_stop(self) -> None:
        with self._lock:
            loop = self._loop
            stop_event = self._stop_event
            self._stop_requested.set()
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)

    async def _subscribe(self, topic: str, instrument: FeedInstrument) -> None:
        client = self._client
        if client is None:
            raise MarketFeedError("Groww market feed is not connected.")
        await client.subscribe(topic, cb=self._message_callback(instrument))
        await client.flush(timeout=5)
        with self._lock:
            self._subscriptions[topic] = instrument.groww_symbol

    def _message_callback(self, instrument: FeedInstrument):
        async def receive(message):
            try:
                tick = self.decode_message(instrument, message.data)
                if tick is not None:
                    self._tick_queue.put_nowait(tick)
            except (DecodeError, ValueError):
                self._notify_error("Groww sent an invalid market-data message.")
                self.request_stop()
            except queue.Full:
                self._notify_error("Live market-data queue is full; the feed has been stopped.")
                self.request_stop()
            except Exception:
                self._notify_error("The live-paper market-data handler failed.")
                self.request_stop()

        return receive

    def _dispatch_ticks(self) -> None:
        while True:
            item = self._tick_queue.get()
            if item is None:
                self._tick_queue.task_done()
                return
            try:
                if isinstance(item, str):
                    self._on_error(item)
                else:
                    self._on_tick(item)
            except Exception:
                logger.error("Live-paper market-data callback failed; details withheld.")
                self.request_stop()
            finally:
                self._tick_queue.task_done()

    @staticmethod
    def decode_message(instrument: FeedInstrument, payload: bytes) -> MarketTick | None:
        from growwapi.groww.proto.stocks_socket_response_pb2 import (
            StocksSocketResponseProtoDto,
        )

        response = StocksSocketResponseProtoDto()
        response.ParseFromString(payload)
        if response.HasField("stocksLiveIndices"):
            price = float(response.stocksLiveIndices.value)
            timestamp_ms = response.stocksLiveIndices.tsInMillis
        elif response.HasField("stockLivePrice"):
            price = float(response.stockLivePrice.ltp)
            timestamp_ms = response.stockLivePrice.tsInMillis
        else:
            return None
        if not math.isfinite(price) or price <= 0 or timestamp_ms <= 0:
            raise ValueError("Invalid Groww price or timestamp.")
        return MarketTick(
            groww_symbol=instrument.groww_symbol,
            segment=instrument.segment,
            price=price,
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc),
        )

    def _notify_error(self, message: str) -> None:
        try:
            self._tick_queue.put_nowait(message)
        except queue.Full:
            logger.error("Live-feed error queue is full; details withheld.")

    def _run(self, jwt: str, signature_callback, initial_subscriptions) -> None:
        try:
            asyncio.run(self._run_async(jwt, signature_callback, initial_subscriptions))
        except Exception as exc:
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "nats_connection",
                "failed",
                error=exc,
            )
            if not self._ready.is_set():
                self._startup_error = "Groww market feed could not be connected."
            else:
                self._notify_error("Groww market feed disconnected unexpectedly.")
        finally:
            self._ready.set()
            self._stop_dispatcher()

    async def _run_async(self, jwt: str, signature_callback, initial_subscriptions) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        client = self._client_factory()
        self._client = client
        async def disconnected():
            if not self._stop_requested.is_set():
                self._notify_error("Groww market feed disconnected.")

        async def closed():
            if not self._stop_requested.is_set():
                self._notify_error("Groww market feed closed unexpectedly.")

        try:
            await client.connect(
                servers=GROWW_FEED_SOCKET_URL,
                user_jwt_cb=lambda: jwt.encode("utf-8"),
                signature_cb=signature_callback,
                tls=ssl.create_default_context(),
                connect_timeout=10,
                max_reconnect_attempts=5,
                disconnected_cb=disconnected,
                closed_cb=closed,
            )
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed", "nats_connect", "succeeded"
            )
            for topic, instrument in initial_subscriptions:
                await self._subscribe(topic, instrument)
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "index_subscription",
                "succeeded",
                details={"exchange": "NSE", "segment": "CASH"},
            )
            self._ready.set()
            if self._stop_requested.is_set():
                self._stop_event.set()
            await self._stop_event.wait()
        except Exception as exc:
            LIVE_FEED_DIAGNOSTICS.record(
                "groww_feed",
                "nats_connect_or_subscribe",
                "failed",
                error=exc,
            )
            raise
        finally:
            if client.is_connected:
                await client.close()

    def _stop_dispatcher(self) -> None:
        thread = self._dispatch_thread
        if thread is None:
            return
        discarded = 0
        while True:
            try:
                self._tick_queue.put_nowait(None)
                break
            except queue.Full:
                try:
                    self._tick_queue.get_nowait()
                    self._tick_queue.task_done()
                    discarded += 1
                except queue.Empty:
                    continue
        if discarded:
            logger.warning(
                "Discarded %s queued market observation(s) during feed shutdown.",
                discarded,
            )
        if thread is not threading.current_thread():
            thread.join(timeout=10)

    def stop(self, timeout_seconds: float = 10) -> None:
        self.request_stop()
        with self._lock:
            thread = self._thread
        if thread is None:
            self._stop_dispatcher()
            return
        if thread is threading.current_thread():
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise MarketFeedError("Groww market feed did not stop within the allowed time.")
        dispatch_thread = self._dispatch_thread
        if dispatch_thread is not None and dispatch_thread is not threading.current_thread():
            dispatch_thread.join(timeout_seconds)
            if dispatch_thread.is_alive():
                raise MarketFeedError("Groww market-data callbacks did not stop within the allowed time.")
        with self._lock:
            self._thread = None
            self._client = None
            self._loop = None
            self._stop_event = None
            self._subscriptions.clear()
            self._ready.clear()
            self._stop_requested.clear()
            self._access_token = ""
            self._transport = None
            self._dispatch_thread = None
