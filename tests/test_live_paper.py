from datetime import date, datetime, time, timezone
import base64
import asyncio
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from aots.brokers.groww_feed import (
    FeedInstrument,
    GrowwInstrumentCatalog,
    GrowwMarketFeed,
    MarketFeedError,
    MarketTick,
)
from aots.brokers.response_log import LiveFeedDiagnosticLog, LIVE_FEED_DIAGNOSTICS
from aots.database import Database
from aots.live_paper import LivePaperService, LivePaperStartRequest
from aots.paper import PaperBroker
from aots.provider_session import ProviderRequestError
from aots.risk import RiskEngine


IST = ZoneInfo("Asia/Kolkata")


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FakeFeed:
    def __init__(self, on_tick, on_error):
        self.on_tick = on_tick
        self.on_error = on_error
        self.started = False
        self.stopped = False
        self.subscriptions = []

    def start(self, index_instrument):
        self.started = True
        self.index_instrument = index_instrument

    def subscribe_ltp(self, instrument):
        self.subscriptions.append(instrument)

    def request_stop(self):
        self.stopped = True

    def stop(self, timeout_seconds=10):
        self.stopped = True

    def publish(self, symbol, segment, price, observed_at):
        self.on_tick(MarketTick(symbol, segment, price, observed_at))


class FakeProvider:
    def __init__(self):
        self.feeds = []
        self.requested_expiries = []

    def start_market_feed(self, on_tick, on_error):
        feed = FakeFeed(on_tick, on_error)
        self.feeds.append(feed)
        return feed

    def option_chain(self, underlying, expiry):
        self.requested_expiries.append((underlying, expiry))
        return {
            "underlying": underlying,
            "expiry": expiry,
            "strikes": [
                {
                    "strike": 100,
                    "call": {"trading_symbol": "NSE-NIFTY-09Jan25-100-CE"},
                    "put": {"trading_symbol": "NSE-NIFTY-09Jan25-100-PE"},
                }
            ],
        }


class FakeCatalog:
    def resolve(self, symbol, segment):
        return FeedInstrument(symbol, "NSE", segment, "12345")


class FakeNatsClient:
    def __init__(self):
        self.connected = False
        self.closed = False
        self.subscriptions = {}
        self.connect_options = None

    @property
    def is_connected(self):
        return self.connected

    async def connect(self, **kwargs):
        self.connect_options = kwargs
        self.connected = True

    async def subscribe(self, topic, cb):
        self.subscriptions[topic] = cb

    async def flush(self, timeout=None):
        return None

    async def close(self):
        self.connected = False
        self.closed = True


def make_service(tmp_path, *, start_time=None, settings_overrides=None):
    local_now = start_time or datetime(2025, 1, 3, 9, 10, tzinfo=IST)
    clock = MutableClock(local_now.astimezone(timezone.utc))
    settings = SimpleNamespace(
        capital_inr=1000,
        max_daily_loss_pct=0.02,
        max_trade_risk_pct=0.03,
        max_consecutive_losses=3,
        max_open_positions=3,
        max_lots_per_order=2,
        market_data_max_age_seconds=5,
    )
    if settings_overrides:
        for key, value in settings_overrides.items():
            setattr(settings, key, value)
    db = Database(str(tmp_path / "live-paper.db"))
    risk = RiskEngine(settings, db, clock=clock)
    paper = PaperBroker(settings, db, risk)
    provider = FakeProvider()
    service = LivePaperService(
        settings,
        db,
        risk,
        paper,
        provider,
        catalog=FakeCatalog(),
        clock=clock,
        run_timer=False,
    )
    return service, provider, db, risk, clock


def local_time(hour, minute, second=0):
    return datetime(2025, 1, 3, hour, minute, second, tzinfo=IST)


def push(feed, clock, symbol, segment, price, at):
    clock.value = at.astimezone(timezone.utc)
    feed.publish(symbol, segment, price, at)


def start_request(**kwargs):
    return LivePaperStartRequest(
        expiry_date=date(2025, 1, 9),
        opening_range_minutes=15,
        candle_interval_minutes=5,
        lot_size=10,
        stop_loss_points=2,
        **kwargs,
    )


def create_breakout(service, provider, clock, *, direction="UP"):
    service.start(start_request())
    feed = provider.feeds[0]
    # Three completed 5-minute bars form a 09:15-09:30 opening range [99, 101].
    push(feed, clock, "NSE-NIFTY", "CASH", 100, local_time(9, 15, 5))
    push(feed, clock, "NSE-NIFTY", "CASH", 100.5, local_time(9, 20, 5))
    push(feed, clock, "NSE-NIFTY", "CASH", 99.5, local_time(9, 25, 5))
    push(feed, clock, "NSE-NIFTY", "CASH", 100, local_time(9, 30, 5))
    signal_price = 102 if direction == "UP" else 98
    push(feed, clock, "NSE-NIFTY", "CASH", signal_price, local_time(9, 30, 10))
    push(feed, clock, "NSE-NIFTY", "CASH", signal_price, local_time(9, 35, 1))
    return feed


def test_instrument_catalog_resolves_exact_nse_segment_without_network():
    payload = (
        "groww_symbol,exchange,segment,exchange_token\n"
        "NSE-NIFTY,NSE,CASH,NIFTY\n"
        "NSE-NIFTY-09Jan25-100-CE,NSE,FNO,501\n"
    ).encode()
    catalog = GrowwInstrumentCatalog(loader=lambda: payload)

    assert catalog.resolve("NSE-NIFTY", "CASH") == FeedInstrument(
        "NSE-NIFTY", "NSE", "CASH", "NIFTY"
    )
    assert catalog.resolve("NSE-NIFTY-09Jan25-100-CE", "FNO").exchange_token == "501"
    with pytest.raises(MarketFeedError, match="not in Groww"):
        catalog.resolve("NSE-NIFTY-09Jan25-100-PE", "FNO")


def test_provider_feed_start_error_is_reported_without_hiding_safe_reason(tmp_path):
    service, _provider, _db, _risk, _clock = make_service(tmp_path)

    def fail_feed_start(_on_tick, _on_error):
        raise ProviderRequestError("Groww access token is unavailable or expired; reconnect.", 401)

    service.feed_starter = fail_feed_start

    with pytest.raises(ValueError, match="access token is unavailable or expired"):
        service.start(start_request())

    assert "access token is unavailable or expired" in service.status()["failure_reason"]
    assert service.status()["status"] == "STOPPED"


def test_live_feed_diagnostics_are_bounded_and_redact_credentials():
    diagnostics = LiveFeedDiagnosticLog(capacity=2)
    diagnostics.record(
        "groww_feed",
        "socket_token_request",
        "failed",
        error=RuntimeError(
            "Authorization: Bearer do-not-show token=also-do-not-show"
        ),
    )
    diagnostics.record("groww_feed", "nats_connect", "started")
    diagnostics.record("groww_feed", "nats_connect", "failed", details={"errno": 110})

    entries = diagnostics.snapshot()
    rendered = str(entries)
    assert len(entries) == 2
    assert entries[0]["stage"] == "nats_connect"
    assert "do-not-show" not in rendered
    assert "also-do-not-show" not in rendered


def test_feed_start_failure_is_available_in_live_diagnostics(tmp_path):
    LIVE_FEED_DIAGNOSTICS.clear()
    service, _provider, _db, _risk, _clock = make_service(tmp_path)

    def fail_feed_start(_on_tick, _on_error):
        class FailedFeed(FakeFeed):
            def start(self, _instrument):
                raise RuntimeError("NATS handshake rejected")

        return FailedFeed(_on_tick, _on_error)

    service.feed_starter = fail_feed_start

    with pytest.raises(ValueError, match="could not be started"):
        service.start(start_request())

    entries = LIVE_FEED_DIAGNOSTICS.snapshot()
    failure = next(
        entry for entry in entries
        if entry["component"] == "live_paper" and entry["outcome"] == "failed"
    )
    assert failure["stage"] == "feed_start"
    assert failure["details"]["exception_type"] == "RuntimeError"
    assert failure["details"]["exception"] == "NATS handshake rejected"


def test_feed_decoder_normalizes_index_and_option_timestamps():
    from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto

    index_instrument = FeedInstrument("NSE-NIFTY", "NSE", "CASH", "26000")
    index_message = StocksSocketResponseProtoDto()
    index_message.stocksLiveIndices.value = 23123.5
    index_message.stocksLiveIndices.tsInMillis = 1_735_872_300_000
    index_tick = GrowwMarketFeed.decode_message(
        index_instrument, index_message.SerializeToString()
    )
    assert index_tick is not None
    assert index_tick.price == 23123.5
    assert index_tick.timestamp.tzinfo == timezone.utc
    assert index_tick.segment == "CASH"

    option_instrument = FeedInstrument(
        "NSE-NIFTY-09Jan25-100-CE", "NSE", "FNO", "501"
    )
    option_message = StocksSocketResponseProtoDto()
    option_message.stockLivePrice.ltp = 105.25
    option_message.stockLivePrice.tsInMillis = 1_735_872_300_000
    option_tick = GrowwMarketFeed.decode_message(
        option_instrument, option_message.SerializeToString()
    )
    assert option_tick is not None
    assert option_tick.price == 105.25
    assert option_tick.groww_symbol == option_instrument.groww_symbol


def test_groww_feed_uses_memory_auth_and_stops_managed_nats_client(monkeypatch):
    client = FakeNatsClient()
    option_instrument = FeedInstrument(
        "NSE-NIFTY-09Jan25-100-CE", "NSE", "FNO", "501"
    )
    callback_complete = threading.Event()
    feed = None

    def on_tick(_tick):
        feed.subscribe_ltp(option_instrument)
        callback_complete.set()

    feed = GrowwMarketFeed(
        "not-a-real-access-token",
        on_tick,
        lambda _message: None,
        transport=object(),
        client_factory=lambda: client,
    )
    monkeypatch.setattr(
        feed,
        "_socket_credentials",
        lambda _token: ("test-jwt", lambda _nonce: b"signature"),
    )
    instrument = FeedInstrument("NSE-NIFTY", "NSE", "CASH", "26000")

    feed.start(instrument)

    assert client.connected is True
    assert client.subscriptions
    assert client.connect_options["user_jwt_cb"]() == b"test-jwt"
    assert "user_credentials" not in client.connect_options
    from growwapi.groww.proto.stocks_socket_response_pb2 import StocksSocketResponseProtoDto

    message = StocksSocketResponseProtoDto()
    message.stocksLiveIndices.value = 23123
    message.stocksLiveIndices.tsInMillis = 1_735_872_300_000
    index_topic = next(iter(client.subscriptions.values()))
    asyncio.run(index_topic(SimpleNamespace(data=message.SerializeToString())))
    assert callback_complete.wait(2)
    assert len(client.subscriptions) == 2

    feed.stop()

    assert client.closed is True


def test_socket_token_request_uses_canonical_url_without_redirect():
    class TokenTransport:
        def __init__(self, response):
            self.response = response
            self.calls = []

        def request(self, method, url, headers, body):
            self.calls.append((method, url, headers, body))
            return self.response

    transport = TokenTransport((200, {"token": "test-socket-jwt"}))
    feed = GrowwMarketFeed(
        "test-access-token",
        lambda _tick: None,
        lambda _error: None,
        transport=transport,
    )

    token, signature = feed._socket_credentials("test-access-token")

    assert token == "test-socket-jwt"
    assert callable(signature)
    encoded_signature = signature("nats-server-nonce")
    assert isinstance(encoded_signature, bytes)
    assert len(base64.b64decode(encoded_signature, validate=True)) == 64
    assert encoded_signature.decode("utf-8").isascii()
    method, url, headers, body = transport.calls[0]
    assert method == "POST"
    assert url == "https://api.groww.in/v1/api/apex/v1/socket/token/create"
    assert headers["Authorization"] == "Bearer test-access-token"
    assert body["socketKey"]
    assert feed.is_running is False


@pytest.mark.parametrize(
    ("direction", "expected_symbol"),
    [
        ("UP", "NSE-NIFTY-09Jan25-100-CE"),
        ("DOWN", "NSE-NIFTY-09Jan25-100-PE"),
    ],
)
def test_live_orb_uses_same_long_option_entry_and_local_paper_stop(
    tmp_path, direction, expected_symbol
):
    service, provider, db, risk, clock = make_service(tmp_path)
    feed = create_breakout(service, provider, clock, direction=direction)

    assert service.status()["opening_range"] == {
        "high": 100.5,
        "low": 99.5,
        "end": "2025-01-03T09:30:00",
    }
    assert provider.requested_expiries == [
        ("NIFTY", "2025-01-09"),
        ("NIFTY", "2025-01-09"),
    ]
    assert [item.groww_symbol for item in feed.subscriptions] == [expected_symbol]

    push(feed, clock, expected_symbol, "FNO", 10, local_time(9, 35, 2))
    position = service.status()["active_trade"]
    assert position["symbol"] == expected_symbol
    assert position["entry_price"] == 10
    assert position["status"] == "OPEN"
    assert risk.snapshot().open_positions == 1

    push(feed, clock, expected_symbol, "FNO", 12, local_time(9, 36, 2))
    assert risk.snapshot().unrealised_pnl == 20
    push(feed, clock, expected_symbol, "FNO", 7, local_time(9, 37, 2))

    closed = next(
        trade for trade in db.trades()
        if trade["symbol"] == expected_symbol
    )
    assert closed["status"] == "CLOSED"
    assert closed["close_reason"] == "SIMULATED_STOP"
    assert closed["realised_pnl"] == -30
    assert risk.snapshot().paused_for_day is True
    assert service.status()["active_trade"] is None
    assert service.status()["trade_count"] == 1
    service.stop()
    with pytest.raises(ValueError, match="already been recorded"):
        service.start(start_request())


def test_missing_opening_range_candle_is_not_synthesized(tmp_path):
    service, provider, _db, _risk, clock = make_service(tmp_path)
    service.start(start_request())
    feed = provider.feeds[0]
    push(feed, clock, "NSE-NIFTY", "CASH", 100, local_time(9, 15, 5))
    push(feed, clock, "NSE-NIFTY", "CASH", 99.5, local_time(9, 25, 5))
    push(feed, clock, "NSE-NIFTY", "CASH", 100, local_time(9, 30, 5))

    assert service.status()["opening_range"] is None
    assert "needs 3 candles" in service.status()["opening_range_error"]
    assert not feed.subscriptions


def test_duplicate_and_out_of_order_source_ticks_do_not_change_candles_or_marks(tmp_path):
    service, provider, db, _risk, clock = make_service(tmp_path)
    feed = create_breakout(service, provider, clock)
    push(feed, clock, "NSE-NIFTY", "CASH", 102, local_time(9, 35, 1))
    push(feed, clock, "NSE-NIFTY", "CASH", 999, local_time(9, 35, 1))
    push(feed, clock, "NSE-NIFTY", "CASH", 998, local_time(9, 35, 0))
    assert service.status()["last_index_price"] == 102

    symbol = "NSE-NIFTY-09Jan25-100-CE"
    push(feed, clock, symbol, "FNO", 10, local_time(9, 35, 2))
    push(feed, clock, symbol, "FNO", 7, local_time(9, 35, 2))
    assert service.status()["active_trade"]["mark_price"] == 10
    assert service.status()["active_trade"]["mark_sequence"] == 0
    assert sum(
        event["event_type"] == "LIVE_PAPER_TICK_REJECTED"
        for event in db.events(100)
    ) == 4


def test_session_end_closes_at_last_fresh_option_mark(tmp_path):
    service, provider, db, _risk, clock = make_service(tmp_path)
    feed = create_breakout(service, provider, clock)
    symbol = "NSE-NIFTY-09Jan25-100-CE"
    push(feed, clock, symbol, "FNO", 10, local_time(9, 35, 2))
    push(feed, clock, symbol, "FNO", 10, local_time(15, 15, 1))

    trade = next(row for row in db.trades() if row["symbol"] == symbol)
    assert trade["status"] == "CLOSED"
    assert trade["close_reason"] == "LIVE_SESSION_END"
    assert trade["realised_pnl"] == 0
    assert service.status()["status"] == "STOPPED"


def test_session_end_waits_for_a_fresh_quote_instead_of_filling_a_stale_price(tmp_path):
    service, provider, db, _risk, clock = make_service(tmp_path)
    feed = create_breakout(service, provider, clock)
    symbol = "NSE-NIFTY-09Jan25-100-CE"
    push(feed, clock, symbol, "FNO", 10, local_time(9, 35, 2))

    service.check_time(now=local_time(15, 15, 1))
    assert service.status()["status"] == "SESSION_END_EXIT_PENDING"
    assert db.trades()[0]["status"] == "OPEN"

    push(feed, clock, symbol, "FNO", 9, local_time(15, 15, 2))

    assert db.trades()[0]["status"] == "CLOSED"
    assert db.trades()[0]["close_reason"] == "LIVE_SESSION_END"
    assert db.trades()[0]["mark_price"] == 9


def test_restart_requires_explicit_recovery_and_monitors_existing_position(tmp_path):
    service, provider, db, risk, clock = make_service(tmp_path)
    feed = create_breakout(service, provider, clock)
    symbol = "NSE-NIFTY-09Jan25-100-CE"
    push(feed, clock, symbol, "FNO", 10, local_time(9, 35, 2))
    trade_id = service.status()["active_trade"]["trade_id"]

    recovered, recovered_provider, _db, _risk, recovered_clock = make_service(
        tmp_path, start_time=local_time(9, 36, 0)
    )
    assert recovered.status()["status"] == "RECOVERY_REQUIRED"
    assert recovered.status()["active_trade"]["trade_id"] == trade_id
    result = recovered.resume()
    assert result["status"] == "RUNNING_POSITION_RECOVERY"
    assert result["entries_enabled"] is False
    assert [item.groww_symbol for item in recovered_provider.feeds[0].subscriptions] == [symbol]

    push(
        recovered_provider.feeds[0],
        recovered_clock,
        symbol,
        "FNO",
        7,
        local_time(9, 37, 1),
    )
    assert db.trades()[0]["status"] == "CLOSED"
    assert recovered.status()["status"] == "STOPPED"
