import hashlib
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aots import api
from aots.api import app
from aots.brokers.base import ProviderAuthError
from aots.brokers.groww import GrowwRuntimeConnection, generate_checksum, totp_now
from aots.database import Database
from aots.paper import PaperBroker
from aots.provider_session import ProviderSessionManager
from test_api import _STATIC_DIR, _run_harness_page, needs_browser

MARKER = "FAKE-SECRET-MARKER-7f3a9c"
TOTP = {"provider": "groww", "auth_method": "totp", "api_key": MARKER + "-key", "totp_secret": MARKER + "-totp"}
APPROVAL = {"provider": "groww", "auth_method": "approval", "api_key": MARKER + "-key", "api_secret": MARKER + "-sec"}
ACCESS_TOKEN = {"provider": "groww", "auth_method": "access_token", "access_token": MARKER + "-access-token"}


class FakeConnection:
    def __init__(self, creds, verify=None):
        self.creds = dict(creds)
        self.closed = False
        self._verify = verify

    def verify(self):
        if self._verify:
            self._verify()

    def close(self):
        self.closed = True
        self.creds.clear()

    def get_index_quote(self, symbol):
        return {"symbol": symbol}

    def get_option_chain_data(self, underlying, expiry):
        return {"underlying": underlying, "expiry": expiry}


class StubTransport:
    def __init__(self, responses=None, error=None):
        self.calls = []
        self._responses = list(responses or [])
        self._error = error

    def request(self, method, url, headers, json_body):
        self.calls.append((method, url, dict(headers), json_body))
        if self._error is not None:
            raise self._error
        if not self._responses:
            raise RuntimeError("No stubbed response")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def provider_env(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        app_name="AOTS provider test", environment="test", broker="groww", trading_mode="paper",
        underlying="NIFTY", capital_inr=100_000, max_daily_loss_pct=.02, max_trade_risk_pct=.005,
        max_consecutive_losses=3, max_open_positions=3, max_lots_per_order=1,
        market_data_max_age_seconds=5, strategy_spot_threshold=25_000,
        database_path=str(tmp_path / "provider.db"),
    )
    db = Database(settings.database_path)
    risk = api.RiskEngine(settings, db)
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "risk", risk)
    monkeypatch.setattr(api, "paper", PaperBroker(settings, db, risk))

    def install(manager):
        monkeypatch.setattr(api, "provider_session", manager)
        return manager

    # Safe default for this fixture: no real network.
    install(ProviderSessionManager({"groww": lambda method, creds: FakeConnection(creds)}))
    return SimpleNamespace(settings=settings, db=db, install=install, tmp_path=tmp_path)


def fake_manager(created, verify=None, create_error=None, received=None):
    def factory(auth_method, creds):
        if received is not None:
            received.append((auth_method, dict(creds)))
        if create_error:
            raise create_error
        conn = FakeConnection(creds, verify)
        created.append(conn)
        return conn
    return ProviderSessionManager({"groww": factory})


def groww_manager_with_transport(clock_ref, responses=None, error=None):
    created = []
    transports = []

    def factory(auth_method, creds):
        transport = StubTransport(responses=responses, error=error)
        conn = GrowwRuntimeConnection(auth_method, creds, transport=transport, clock=lambda: clock_ref[0])
        transports.append(transport)
        created.append(conn)
        return conn

    return ProviderSessionManager({"groww": factory}), created, transports


def persisted_text(env):
    parts = []
    with sqlite3.connect(env.settings.database_path) as conn:
        for (name,) in conn.execute("select name from sqlite_master where type='table'").fetchall():
            parts.append(json.dumps(conn.execute(f'select * from "{name}"').fetchall(), default=str))
    for path in Path(env.tmp_path).rglob("*"):
        if path.is_file():
            parts.append(path.read_bytes().decode("latin-1"))
    return "\n".join(parts)


def test_checksum_generation_matches_documented_formula():
    ts = "1719830400"
    secret = "secret-value"
    assert generate_checksum(secret, ts) == hashlib.sha256((secret + ts).encode("utf-8")).hexdigest()


def test_totp_generation_uses_seed_not_literal_code():
    # RFC 6238 test vector using SHA1 and 6 digits.
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_now(seed, 59) == "287082"


def test_approval_connect_sends_checksum_and_timestamp(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    clock_ref = [now]
    mgr, _, transports = groww_manager_with_transport(clock_ref, responses=[(200, {"token": "tok-12345678", "isActive": True})])
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "connected" and body["connected"] is True
    method, url, headers, payload = transports[0].calls[0]
    assert method == "POST" and url.endswith("/v1/token/api/access")
    assert headers["Authorization"] == "Bearer " + MARKER + "-key"
    assert payload["key_type"] == "approval"
    assert payload["timestamp"] == str(int(now.timestamp()))
    assert payload["checksum"] == hashlib.sha256((MARKER + "-sec" + payload["timestamp"]).encode("utf-8")).hexdigest()


def test_totp_connect_generates_code_and_sends_it(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    clock_ref = [now]
    # Valid base32 seed decodes to bytes. This is a fake marker-only value.
    seed = "JBSWY3DPEHPK3PXP"
    payload = {"provider": "groww", "auth_method": "totp", "api_key": MARKER + "-key", "totp_secret": seed}
    mgr, _, transports = groww_manager_with_transport(clock_ref, responses=[(200, {"token": "tok-ABCDEF12", "isActive": True})])
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=payload).json()["state"] == "connected"
    sent = transports[0].calls[0][3]
    assert sent["key_type"] == "totp"
    assert sent["totp"] == totp_now(seed, now.timestamp())


def test_connect_rejected_credentials_sets_failed_without_secret_leak(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    mgr, _, _ = groww_manager_with_transport([datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)], responses=[(401, None)])
    provider_env.install(mgr)
    with TestClient(app) as client:
        res = client.post("/api/provider/connect", json=APPROVAL)
        status = client.get("/api/provider/status")
    assert res.json()["state"] == "failed" and res.json()["configured"] is False
    assert MARKER not in res.text and MARKER not in status.text and MARKER not in caplog.text
    assert MARKER not in persisted_text(provider_env)


def test_connect_handles_network_error_safely(provider_env):
    mgr, _, _ = groww_manager_with_transport([datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)], error=RuntimeError("boom"))
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "failed"
    assert body["failure_reason"] == "Provider is unreachable or returned a server error."


def test_connect_rejects_malformed_auth_response(provider_env):
    mgr, _, _ = groww_manager_with_transport([datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)], responses=[(200, {"tokenRefId": "x"})])
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "failed" and body["configured"] is False


def test_token_response_without_optional_active_flag_is_accepted(provider_env):
    mgr, _, _ = groww_manager_with_transport(
        [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)],
        responses=[(200, {"token": "tok-12345678"})],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "connected"
    assert body["connected"] is True
    assert body["configured"] is True


def test_token_response_with_inactive_flag_is_rejected(provider_env):
    mgr, _, _ = groww_manager_with_transport(
        [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)],
        responses=[(200, {"token": "tok-12345678", "isActive": False})],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "failed"
    assert body["connected"] is False
    assert body["configured"] is False


def test_existing_access_token_connects_in_memory_and_authenticates_provider_request(provider_env):
    token = ACCESS_TOKEN["access_token"]
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, created, transports = groww_manager_with_transport(
        [now],
        responses=[(200, {"status": "SUCCESS", "payload": {"expiries": ["2025-01-09"]}})],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        connected = client.post("/api/provider/connect", json=ACCESS_TOKEN)
        assert connected.status_code == 200
        assert connected.json()["state"] == "connected"
        assert connected.json()["auth_method"] == "access_token"
        assert token not in connected.text

        expiries = client.get("/api/provider/historical/expiries?year=2025&month=1")
        assert expiries.status_code == 200
        assert expiries.json() == {"expiries": ["2025-01-09"]}
        assert created[0]._credentials is None and created[0]._token == token

    assert len(transports) == 1
    assert len(transports[0].calls) == 1
    method, url, headers, body = transports[0].calls[0]
    assert method == "GET" and url.split("?", 1)[0].endswith("/v1/historical/expiries")
    assert headers["Authorization"] == f"Bearer {token}"
    assert body is None
    assert token not in persisted_text(provider_env)
    assert token not in json.dumps(mgr.status())


def test_invalid_existing_access_token_is_rejected_without_echoing_it(provider_env):
    bad_token = "short"
    payload = {**ACCESS_TOKEN, "access_token": bad_token}
    mgr, _, transports = groww_manager_with_transport(
        [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)]
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        response = client.post("/api/provider/connect", json=payload)
        assert response.status_code == 200
        assert response.json()["state"] == "failed"
        assert response.json()["failure_reason"] == "The access token is invalid."
        assert bad_token not in response.text
    assert transports[0].calls == []


def test_groww_rejected_existing_access_token_marks_session_failed(provider_env):
    mgr, _, _ = groww_manager_with_transport(
        [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)],
        responses=[(401, {"error": {"code": "GA005", "message": "sensitive upstream text"}})],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        connected = client.post("/api/provider/connect", json=ACCESS_TOKEN).json()
        assert connected["connected"] is True
        response = client.get("/api/provider/historical/expiries?year=2025&month=1")
        assert response.status_code == 401
        assert "sensitive upstream text" not in response.text
        status = client.get("/api/provider/status").json()
        assert status["state"] == "failed" and status["connected"] is False


def test_status_exposes_only_non_sensitive_metadata(provider_env):
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=TOTP)
        body = client.get("/api/provider/status").json()
    assert set(body) == {
        "provider", "auth_method", "state", "configured", "connected",
        "live_connection_implemented", "failure_reason", "paper_only", "providers",
    }
    kite = next(p for p in body["providers"] if p["id"] == "kite")
    assert kite["available"] is False and kite["auth_methods"] == []


def test_only_required_credentials_are_passed_to_the_provider(provider_env):
    created, received = [], []
    provider_env.install(fake_manager(created, received=received))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
    assert received == [("approval", {"api_key": MARKER + "-key", "api_secret": MARKER + "-sec"})]
    assert created[0].closed is True and created[0].creds == {}


def test_groww_credentials_of_1011_characters_are_accepted(provider_env):
    api_key = "K" * 1011
    api_secret = "S" * 1011
    payload = {
        "provider": "groww",
        "auth_method": "approval",
        "api_key": api_key,
        "api_secret": api_secret,
    }
    provider_env.install(ProviderSessionManager({
        "groww": lambda method, creds: FakeConnection(creds),
    }))
    with TestClient(app) as client:
        response = client.post("/api/provider/connect", json=payload)

    assert response.status_code == 200
    assert response.json()["state"] == "connected"
    assert api_key not in response.text
    assert api_secret not in response.text


def test_provider_credential_length_remains_bounded(provider_env):
    too_long = "K" * 2049
    payload = {
        "provider": "groww",
        "auth_method": "approval",
        "api_key": too_long,
        "api_secret": "S" * 1011,
    }
    with TestClient(app) as client:
        response = client.post("/api/provider/connect", json=payload)

    assert response.status_code == 422
    assert too_long not in response.text


def test_rejected_request_leaves_prior_session_untouched(provider_env):
    created = []
    manager = provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=TOTP).status_code == 200
        original = created[0]
        before = client.get("/api/provider/status").json()
        bad = {**TOTP, "totp_secret": " "}
        assert client.post("/api/provider/connect", json=bad).status_code == 400
        assert manager._connection is original
        assert original.closed is False
        assert original.creds == {"api_key": MARKER + "-key", "totp_secret": MARKER + "-totp"}
        assert len(created) == 1
        assert client.get("/api/provider/status").json() == before
    assert original.closed is True and original.creds == {}


def test_disconnect_and_shutdown_cleanup(provider_env):
    created = []
    manager = provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=TOTP)
        assert client.post("/api/provider/disconnect").json()["state"] == "disconnected"
        assert created[0].closed and created[0].creds == {}
        client.post("/api/provider/connect", json=APPROVAL)
        assert len(created) == 2 and created[1].closed is False
    assert created[1].closed is True
    assert manager.status()["configured"] is False


def test_disconnect_during_connecting_discards_the_late_session(provider_env):
    created = []
    entered, release = threading.Event(), threading.Event()

    def slow_verify():
        entered.set()
        release.wait(5)

    manager = fake_manager(created, verify=slow_verify)
    from aots.provider_session import ProviderConnectRequest
    worker = threading.Thread(target=manager.connect, args=(ProviderConnectRequest(**TOTP),))
    worker.start()
    assert entered.wait(5)
    assert manager.status()["state"] == "connecting"
    manager.disconnect()
    release.set()
    worker.join(5)
    assert manager.status()["state"] == "disconnected"
    assert created[0].closed


def test_provider_quote_endpoint_normalizes_success(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport([now], responses=[
        (200, {"token": "tok-12345678", "isActive": True}),
        (200, {"status": "SUCCESS", "payload": {"last_price": 23111.2, "last_trade_time": 1735862400000}}),
    ])
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=APPROVAL).json()["state"] == "connected"
        quote = client.get("/api/provider/quote?symbol=NIFTY")
    body = quote.json()
    assert quote.status_code == 200
    assert body["symbol"] == "NIFTY" and body["exchange"] == "NSE"
    assert body["last_price"] == 23111.2 and body["source"] == "groww"


def test_provider_option_chain_endpoint_normalizes_success(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport([now], responses=[
        (200, {"token": "tok-12345678", "isActive": True}),
        (200, {"status": "SUCCESS", "payload": {
            "underlying_ltp": 25641.7,
            "strikes": {
                "23400": {
                    "CE": {"trading_symbol": "NIFTY25N1823400CE", "ltp": 2200.0, "open_interest": 7, "volume": 5,
                           "greeks": {"delta": 0.99, "gamma": 0.0, "theta": -1.07, "vega": 0.69, "rho": 5.18, "iv": 25.34}},
                    "PE": {"trading_symbol": "NIFTY25N1823400PE", "ltp": 2.05, "open_interest": 7453, "volume": 9339,
                           "greeks": {"delta": -0.01, "gamma": 0.0, "theta": -1.07, "vega": 0.69, "rho": -0.03, "iv": 25.34}},
                }
            }
        }}),
    ])
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        res = client.get("/api/provider/option-chain?underlying=NIFTY&expiry=2025-11-27")
    body = res.json()
    assert res.status_code == 200
    assert body["underlying"] == "NIFTY" and body["expiry"] == "2025-11-27"
    assert body["strikes"][0]["strike"] == 23400.0
    assert body["strikes"][0]["call"]["trading_symbol"] == "NIFTY25N1823400CE"


def test_provider_option_chain_rejects_invalid_expiry(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport([now], responses=[(200, {"token": "tok-12345678", "isActive": True})])
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        bad = client.get("/api/provider/option-chain?underlying=NIFTY&expiry=2025-13-40")
    assert bad.status_code == 422


def account_responses(*, positions_fno=None, positions_cash=None, holdings=None, margins=None):
    return [
        (200, {"token": "tok-12345678"}),
        (200, {"status": "SUCCESS", "payload": {"positions": positions_fno or []}}),
        (200, {"status": "SUCCESS", "payload": {"positions": positions_cash or []}}),
        (200, {"status": "SUCCESS", "payload": {"holdings": holdings or []}}),
        (200, {"status": "SUCCESS", "payload": margins or {}}),
    ]


def test_account_insights_normalizes_positions_holdings_and_margins(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    fno = [{
        "trading_symbol": "NIFTY25JAN25000CE",
        "segment": "FNO",
        "exchange": "NSE",
        "product": "MIS",
        "quantity": -50,
        "net_price": 125.5,
        "net_carry_forward_quantity": 0,
        "net_carry_forward_price": 0,
        "realised_pnl": 320,
        "private_data": "must not be returned",
    }]
    holdings = [{
        "trading_symbol": "RELIANCE",
        "quantity": 10,
        "average_price": 100,
        "pledge_quantity": 2,
        "t1_quantity": 3,
        "isin": "must not be returned",
    }]
    margins = {
        "clear_cash": 5000,
        "net_margin_used": 35000,
        "collateral_available": 7000,
        "fno_margin_details": {
            "option_buy_balance_available": 11000,
            "option_sell_balance_available": 1000,
        },
    }
    mgr, _, transports = groww_manager_with_transport(
        [now],
        responses=account_responses(positions_fno=fno, holdings=holdings, margins=margins),
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=APPROVAL).json()["state"] == "connected"
        response = client.get("/api/provider/account-insights")
    data = response.json()
    assert response.status_code == 200
    assert data["refreshed_at"] == now.isoformat()
    assert data["positions"]["items"][0]["quantity"] == -50
    assert data["positions"]["items"][0]["reported_realised_pnl"] == 320
    assert data["positions"]["items"][0]["segment"] == "FNO"
    assert data["holdings"]["items"][0]["average_price"] == 100
    assert data["balances"]["values"]["clear_cash"] == 5000
    assert data["today_pnl"]["status"] == "unavailable"
    assert data["today_pnl"]["value"] is None
    assert isinstance(data["today_pnl"]["reason"], str)
    assert "does not specify a daily time scope" in data["today_pnl"]["reason"]
    assert "private_data" not in response.text and "must not be returned" not in response.text
    assert "isin" not in response.text
    assert "tok-12345678" not in response.text and "tok-12345678" not in caplog.text
    account_calls = transports[0].calls[1:]
    assert [call[1].split("?", 1)[0].removeprefix("https://api.groww.in") for call in account_calls] == [
        "/v1/positions/user", "/v1/positions/user", "/v1/holdings/user",
        "/v1/margins/detail/user",
    ]
    assert "segment=FNO" in account_calls[0][1]
    assert "segment=CASH" in account_calls[1][1]


def test_account_insights_preserves_zero_and_missing_values(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    fno = [{
        "trading_symbol": "NIFTY25JAN25000CE",
        "quantity": 0,
        "realised_pnl": 0,
    }]
    margins = {"clear_cash": 0}
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=account_responses(positions_fno=fno, margins=margins),
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        data = client.get("/api/provider/account-insights").json()
    position = data["positions"]["items"][0]
    assert position["quantity"] == 0
    assert position["reported_realised_pnl"] == 0
    assert position["net_price"] is None
    assert data["holdings"]["status"] == "available" and data["holdings"]["items"] == []
    assert data["balances"]["status"] == "available"
    assert data["balances"]["values"]["clear_cash"] == 0
    assert data["balances"]["values"]["collateral_available"] is None
    assert data["today_pnl"]["value"] is None


def test_account_insights_marks_missing_response_fields_unavailable(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (200, {"status": "SUCCESS", "payload": {}}),
            (200, {"status": "SUCCESS", "payload": {"positions": []}}),
            (200, {"status": "SUCCESS", "payload": {}}),
            (200, {"status": "SUCCESS", "payload": {}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        data = client.get("/api/provider/account-insights").json()
    assert data["positions"]["status"] == "partial"
    assert data["positions"]["items"] == []
    assert data["holdings"]["status"] == "unavailable"
    assert data["holdings"]["items"] is None
    assert data["balances"]["status"] == "unavailable"
    assert data["balances"]["values"]["clear_cash"] is None


def test_account_insights_partial_provider_failure_is_not_fabricated_as_empty(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (500, None),
            (200, {"status": "SUCCESS", "payload": {"positions": []}}),
            (200, {"status": "SUCCESS", "payload": {"holdings": []}}),
            (200, {"status": "SUCCESS", "payload": {"clear_cash": 0}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        data = client.get("/api/provider/account-insights").json()
    assert data["positions"]["status"] == "partial"
    assert data["positions"]["reason"] == "One positions segment is unavailable."
    assert data["holdings"]["status"] == "available" and data["holdings"]["items"] == []
    assert data["balances"]["values"]["clear_cash"] == 0


def test_account_insights_auth_failure_expires_session_safely(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, created, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (
                401,
                {
                    "status": "FAILURE",
                    "error": {"code": "GA005", "message": MARKER},
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get("/api/provider/account-insights")
        status = client.get("/api/provider/status").json()
    assert response.status_code == 401
    assert response.json()["detail"] == (
        "Groww rejected the access token (invalid or expired); reconnect."
    )
    assert status["state"] == "failed" and status["configured"] is False
    assert mgr._connection is None and created[0]._token is None
    assert MARKER not in response.text and MARKER not in caplog.text
    assert "tok-12345678" not in response.text and "tok-12345678" not in caplog.text


def test_provider_market_data_handles_upstream_failure(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport([now], responses=[
        (200, {"token": "tok-12345678", "isActive": True}),
        (500, None),
    ])
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        res = client.get("/api/provider/quote?symbol=NIFTY")
    assert res.status_code == 503


def test_provider_market_data_auth_error_code_fails_session_safely(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, created, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678", "isActive": True}),
            (
                400,
                {
                    "status": "FAILURE",
                    "error": {
                        "code": "GA005",
                        "message": f"Rejected credential {MARKER}",
                        "metadata": None,
                    },
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=APPROVAL).json()["state"] == "connected"
        response = client.get("/api/provider/quote?symbol=NIFTY")
        state = client.get("/api/provider/status").json()
        second_request = client.get("/api/provider/quote?symbol=NIFTY")

    assert response.status_code == 401
    assert response.json()["detail"] == (
        "Groww rejected the access token (invalid or expired); reconnect."
    )
    assert state["state"] == "failed"
    assert state["connected"] is False and state["configured"] is False
    assert mgr._connection is None
    assert created[0]._token is None and created[0]._expires_at is None
    assert second_request.status_code == 409
    for text in (response.text, caplog.text, second_request.text):
        assert MARKER not in text
        assert "tok-12345678" not in text


def test_ordinary_upstream_bad_request_keeps_session_connected(provider_env):
    now = datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)
    mgr, created, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678", "isActive": True}),
            (
                400,
                {
                    "status": "FAILURE",
                    "error": {
                        "code": "GA001",
                        "message": "Invalid trading symbol.",
                        "metadata": None,
                    },
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=APPROVAL).json()["state"] == "connected"
        response = client.get("/api/provider/quote?symbol=NIFTY")
        status = client.get("/api/provider/status").json()
        assert status["state"] == "connected" and status["configured"] is True
        assert mgr._connection is created[0]
        assert created[0]._token == "tok-12345678"
    assert response.status_code == 400
    assert response.json()["detail"] == "Groww rejected the request; check the symbol and expiry."


def test_expired_token_fails_safely_and_marks_session_failed(provider_env):
    clock = [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)]
    mgr, _, _ = groww_manager_with_transport(clock, responses=[
        (200, {"token": "tok-12345678", "isActive": True}),
    ])
    provider_env.install(mgr)
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=APPROVAL).json()["state"] == "connected"
        clock[0] = clock[0] + timedelta(hours=1)  # past 06:00 IST boundary for this date
        res = client.get("/api/provider/quote?symbol=NIFTY")
        status = client.get("/api/provider/status").json()
    assert res.status_code == 401
    assert status["state"] == "failed" and status["configured"] is False


def test_malformed_input_never_echoes_credentials(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    payload = {"provider": "groww", "auth_method": "totp", "api_key": MARKER, "api_secret": MARKER}
    with TestClient(app) as client:
        res = client.post("/api/provider/connect", json=payload)
    assert res.status_code in (400, 422)
    assert MARKER not in res.text
    assert MARKER not in caplog.text
    assert MARKER not in persisted_text(provider_env)


def test_non_json_body_does_not_echo(provider_env):
    with TestClient(app) as client:
        res = client.post("/api/provider/connect", content=f"api_key={MARKER}",
                          headers={"Content-Type": "application/json"})
    assert res.status_code == 422 and MARKER not in res.text


def test_kite_cannot_be_selected(provider_env):
    with TestClient(app) as client:
        res = client.post("/api/provider/connect",
                          json={"provider": "kite", "auth_method": "totp", "api_key": MARKER, "totp_secret": MARKER})
        status = client.get("/api/provider/status").json()
    assert res.status_code == 400 and MARKER not in res.text
    assert status["state"] == "not_configured" and status["configured"] is False


def test_provider_activity_does_not_change_paper_trading(provider_env):
    with TestClient(app) as client:
        before = client.get("/api/risk").json()
        client.post("/api/provider/connect", json=TOTP)
        client.post("/api/provider/disconnect")
        assert client.get("/api/risk").json() == before
        assert client.get("/api/status").json()["live_trading_enabled"] is False
        assert client.get("/api/trades").json() == []


def test_dashboard_has_masked_inputs_and_no_browser_storage():
    html_text = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")
    for name in ("api_key", "totp_secret", "api_secret", "access_token"):
        assert f'name="{name}" type="password"' in html_text
    assert 'value="kite" disabled' in html_text
    assert 'value="access_token">Existing access token</option>' in html_text
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert banned not in js


@needs_browser
def test_account_insights_dashboard_refresh_states_and_stale_data(tmp_path):
    snapshot = _run_harness_page(tmp_path, f"""<script>
let accountCalls = 0;
let sessionExpired = false;
const accountData = {{
  refreshed_at: "2025-01-03T00:00:00+00:00",
  today_pnl: {{status:"unavailable", value:null, reason:"Today's P&L cannot be determined."}},
  positions: {{status:"available", reason:null, items:[{{
    trading_symbol:"NIFTY-TEST-CE", segment:"FNO", exchange:"NSE", product:"MIS",
    quantity:50, net_price:10, reported_realised_pnl:0
  }}]}},
  holdings: {{status:"unavailable", reason:"Groww holdings are unavailable.", items:null}},
  balances: {{status:"available", reason:null, values:{{clear_cash:0, collateral_available:null}}}}
}};
const body = {{
  "/api/status": {{app_name:"AOTS", environment:"test", broker:"groww", mode:"paper",
    underlying:"NIFTY", live_trading_enabled:false, capital_inr:100000,
    strategy_spot_threshold:25000, trade_count:0}},
  "/api/risk": {{daily_pnl:0, realised_pnl:0, unrealised_pnl:0, open_positions:0,
    max_daily_loss_inr:2000, max_trade_risk_pct:.005, max_trade_risk_inr:500,
    consecutive_losses:0, max_consecutive_losses:3, paused_for_day:false,
    kill_switch:false}},
  "/api/trades": [],
  "/api/strategy/decisions": []
}};
window.fetch = async (path) => {{
  if (path === "/api/provider/account-insights") {{
    accountCalls++;
    if (accountCalls === 1) {{
      await new Promise(r => setTimeout(r, 180));
      return {{ok:true, status:200, json:async()=>accountData}};
    }}
    sessionExpired = true;
    return {{ok:false, status:401, json:async()=>({{detail:"Groww rejected the access token; reconnect."}})}};
  }}
  if (path === "/api/provider/status") {{
    return {{ok:true, status:200, json:async()=>({{state:sessionExpired ? "failed" : "connected"}})}};
  }}
  return {{ok:true, status:200, json:async()=>body[path]}};
}};
</script>
<script src="{(_STATIC_DIR / 'app.js').as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(100);
  const automaticCalls = accountCalls;
  const first = refreshAccountInsights();
  await sleep(20);
  const loading = {{
    state: document.getElementById("account-insights-state").innerText,
    disabled: document.getElementById("account-insights-refresh").disabled,
  }};
  await refreshAccountInsights();
  const callsWhileLoading = accountCalls;
  await first;
  const success = {{
    state: document.getElementById("account-insights-state").innerText,
    today: document.getElementById("groww-today-pnl").innerText,
    todayReason: document.getElementById("groww-today-pnl-reason").innerText,
    cash: document.getElementById("groww-clear-cash").innerText,
    collateral: document.getElementById("groww-collateral").innerText,
    positions: document.getElementById("groww-positions").innerText,
    holdings: document.getElementById("groww-holdings").innerText,
    time: document.getElementById("account-refresh-time").innerText,
  }};
  await refreshAccountInsights();
  await sleep(30);
  const expired = {{
    state: document.getElementById("account-insights-state").innerText,
    message: document.getElementById("account-insights-message").innerText,
    positions: document.getElementById("groww-positions").innerText,
    time: document.getElementById("account-refresh-time").innerText,
  }};
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify([{{automaticCalls, loading, callsWhileLoading, success, expired}}]);
  document.body.appendChild(pre);
}})();
</script>""")[0]
    assert snapshot["automaticCalls"] == 0
    assert snapshot["loading"] == {"state": "Loading", "disabled": True}
    assert snapshot["callsWhileLoading"] == 1
    assert snapshot["success"]["state"] == "Partially available"
    assert snapshot["success"]["today"] == "Unavailable"
    assert "cannot be determined" in snapshot["success"]["todayReason"]
    assert snapshot["success"]["cash"] == "₹0.00"
    assert snapshot["success"]["collateral"] == "Unavailable"
    assert "NIFTY-TEST-CE" in snapshot["success"]["positions"]
    assert "₹0.00" in snapshot["success"]["positions"]
    assert "Groww holdings are unavailable" in snapshot["success"]["holdings"]
    assert "2025" in snapshot["success"]["time"]
    assert snapshot["expired"]["state"] == "Session expired"
    assert "stale" in snapshot["expired"]["message"]
    assert "NIFTY-TEST-CE" in snapshot["expired"]["positions"]


@needs_browser
def test_browser_clears_credential_inputs_and_stores_nothing(tmp_path):
    snapshot = _run_harness_page(tmp_path, f"""<script>
const sent = [];
window.fetch = async (path, options) => {{
  sent.push({{path, body: options && options.body || null}});
  const state = path === "/api/provider/connect" ? "connected" : "disconnected";
  return {{ok: true, status: 200, json: async () => ({{state, failure_reason: null}})}};
}};
</script>
<script src="{(_STATIC_DIR / 'app.js').as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(20);
  const form = document.getElementById("provider-form");
  form.elements.api_key.value = "{MARKER}-k";
  form.elements.totp_secret.value = "{MARKER}-t";
  form.requestSubmit();
  await sleep(50);
  const afterConnect = [...form.querySelectorAll("input")].map(i => i.value);
  form.elements.api_key.value = "{MARKER}-again";
  document.getElementById("provider-disconnect").click();
  await sleep(50);
  const afterDisconnect = [...form.querySelectorAll("input")].map(i => i.value);
  const connect = sent.find(s => s.path === "/api/provider/connect");
  const out = [{{
    afterConnect, afterDisconnect, connectBody: connect.body,
    urls: sent.map(s => s.path),
    storage: [localStorage.length, sessionStorage.length, document.cookie],
    status: document.getElementById("provider-status").innerText,
  }}];
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}})();
</script>""")[0]
    assert snapshot["afterConnect"] == ["", "", "", ""] and snapshot["afterDisconnect"] == ["", "", "", ""]
    assert MARKER in snapshot["connectBody"]
    assert all(MARKER not in url for url in snapshot["urls"])
    assert snapshot["storage"] == [0, 0, ""]
    assert snapshot["status"] == "Disconnected"


def test_historical_expiry_contract_and_candle_diagnostic_is_read_only(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    symbol = "NSE-NIFTY-04Jan24-19200-CE"
    mgr, _, transports = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678", "isActive": True}),
            (200, {"status": "SUCCESS", "payload": {"expiries": ["2024-01-04"]}}),
            (200, {"status": "SUCCESS", "payload": {"contracts": [symbol]}}),
            (
                200,
                {
                    "status": "SUCCESS",
                    "payload": {
                        "candles": [
                            ["2024-01-03T09:15:00", 100, 105, 99, 103, 1000, 2000],
                            ["2024-01-03T09:30:00", 103, 106, 102, 105, 1200, 2200],
                        ]
                    },
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        before_events = provider_env.db.events()
        before_trades = provider_env.db.trades()
        before_risk = api.risk.snapshot()
        expiries = client.get(
            "/api/provider/historical/expiries?year=2024&month=1"
        )
        contracts = client.get(
            "/api/provider/historical/contracts?expiry_date=2024-01-04"
        )
        candles = client.post(
            "/api/provider/historical/candles/check",
            json={
                "groww_symbol": symbol,
                "start_time": "2024-01-03T09:15",
                "end_time": "2024-01-03T09:30",
                "candle_interval": "15minute",
            },
        )
        after_events = provider_env.db.events()
        after_trades = provider_env.db.trades()
        after_risk = api.risk.snapshot()

    assert expiries.status_code == 200 and expiries.json()["expiries"] == ["2024-01-04"]
    assert contracts.status_code == 200 and contracts.json()["contracts"] == [symbol]
    result = candles.json()
    assert candles.status_code == 200
    assert result["status"] == "available"
    assert result["candle_count"] == 2
    assert result["first_timestamp"] == "2024-01-03T09:15:00"
    assert result["last_timestamp"] == "2024-01-03T09:30:00"
    assert result["invalid_ohlc_count"] == 0
    assert "timezone is verified" in result["timestamps_timezone"]
    assert after_events == before_events
    assert after_trades == before_trades == []
    assert after_risk == before_risk

    calls = transports[0].calls
    assert [call[0] for call in calls] == ["POST", "GET", "GET", "GET"]
    assert ["/v1/historical/" in call[1] for call in calls] == [False, True, True, True]
    assert all(call[1].startswith("https://api.groww.in/") for call in calls)
    assert all(call[2].get("X-API-VERSION") == "1.0" for call in calls[1:])
    assert all(call[2].get("Authorization") == "Bearer tok-12345678" for call in calls[1:])


def test_historical_contract_list_ignores_valid_future_without_rejecting_options(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    call_symbol = "NSE-NIFTY-27Oct26-22500-CE"
    put_symbol = "NSE-NIFTY-27Oct26-22500-PE"
    future_symbol = "NSE-NIFTY-27Oct26-FUT"
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678", "isActive": True}),
            (200, {
                "status": "SUCCESS",
                "payload": {"contracts": [call_symbol, future_symbol, put_symbol]},
            }),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get("/api/provider/historical/contracts?expiry_date=2026-10-27")
        log = client.get("/api/provider/response-log").json()

    assert response.status_code == 200
    assert response.json()["contracts"] == [call_symbol, put_symbol]
    assert log["entries"][0]["body"]["payload"]["contracts"] == [call_symbol, future_symbol, put_symbol]


@pytest.mark.parametrize(
    ("candles", "expected_status", "expected_count", "warning"),
    [
        ([], "no_data", 0, None),
        (
            [["2024-01-03T09:15:00", 100, 99, 101, 98, -1, -2]],
            "available",
            1,
            "invalid OHLC",
        ),
        (
            [
                ["2024-01-03T09:30:00", 100, 105, 99, 103, 1000, None],
                ["2024-01-03T09:30:00", 103, 106, 102, 105, 1200, None],
            ],
            "available",
            2,
            "Duplicate candle timestamps",
        ),
        (
            [["2024-01-03T09:45:00", 100, 105, 99, 103, 1000, None]],
            "available",
            1,
            "outside the requested range",
        ),
    ],
)
def test_historical_candle_diagnostic_reports_empty_and_quality_warnings(
    provider_env, candles, expected_status, expected_count, warning
):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (200, {"status": "SUCCESS", "payload": {"candles": candles}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.post(
            "/api/provider/historical/candles/check",
            json={
                "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
                "start_time": "2024-01-03T09:15",
                "end_time": "2024-01-03T09:30",
                "candle_interval": "15minute",
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == expected_status
    assert result["candle_count"] == expected_count
    if warning:
        assert any(warning in item for item in result["quality_warnings"])


def test_historical_candle_malformed_timestamp_fails_closed(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (
                200,
                {
                    "status": "SUCCESS",
                    "payload": {"candles": [["not-a-timestamp", 100, 105, 99, 103, 1000, None]]},
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.post(
            "/api/provider/historical/candles/check",
            json={
                "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
                "start_time": "2024-01-03T09:15",
                "end_time": "2024-01-03T09:30",
                "candle_interval": "15minute",
            },
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "Provider returned an unexpected response."


@pytest.mark.parametrize(
    ("path", "response_payload"),
    [
        ("/api/provider/historical/expiries?year=2024&month=1", {"expiries": "not-a-list"}),
        (
            "/api/provider/historical/contracts?expiry_date=2024-01-04",
            {"contracts": ["NIFTY-UNSCOPED-CE"]},
        ),
    ],
)
def test_historical_discovery_malformed_payload_fails_closed(
    provider_env, path, response_payload
):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (200, {"status": "SUCCESS", "payload": response_payload}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get(path)
    assert response.status_code == 502
    assert response.json()["detail"] == "Provider returned an unexpected response."


def test_historical_expiry_rejects_impossible_user_date_as_client_error(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, transports = groww_manager_with_transport(
        [now], responses=[(200, {"token": "tok-12345678"})]
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get(
            "/api/provider/historical/contracts?expiry_date=2024-02-30"
        )
    assert response.status_code == 422
    assert response.json()["detail"] == "expiry must be a real calendar date."
    assert len(transports[0].calls) == 1


def test_historical_expiries_reject_impossible_upstream_date_as_response_error(
    provider_env,
):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (
                200,
                {
                    "status": "SUCCESS",
                    "payload": {
                        "expiries": ["2024-02-30"],
                        "sensitive_provider_detail": MARKER,
                    },
                },
            ),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get("/api/provider/historical/expiries?year=2024&month=2")
    assert response.status_code == 502
    assert response.json() == {"detail": "Provider returned an unexpected response."}
    assert MARKER not in response.text


@pytest.mark.parametrize(
    ("upstream_result", "expected_status", "expected_session_state"),
    [
        (
            (401, {"status": "FAILURE", "error": {"message": MARKER}}),
            401,
            "failed",
        ),
        (
            (429, {"status": "FAILURE", "error": {"message": MARKER}}),
            429,
            "connected",
        ),
        (TimeoutError(MARKER), 503, "connected"),
    ],
)
def test_historical_expiry_route_maps_auth_rate_limit_and_timeout_safely(
    provider_env, upstream_result, expected_status, expected_session_state
):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            upstream_result,
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get("/api/provider/historical/expiries?year=2024&month=1")
        status = client.get("/api/provider/status").json()
    assert response.status_code == expected_status
    assert status["state"] == expected_session_state
    assert MARKER not in response.text
    assert "tok-12345678" not in response.text


def test_historical_provider_failure_is_not_fabricated_as_empty(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (503, None),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get("/api/provider/historical/expiries?year=2024&month=1")
    assert response.status_code == 503
    assert response.json()["detail"] == "Provider is unreachable or returned a server error."


def test_historical_permission_denial_does_not_expire_connected_session(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, _ = groww_manager_with_transport(
        [now],
        responses=[
            (200, {"token": "tok-12345678"}),
            (403, {"status": "FAILURE", "error": {"message": MARKER}}),
        ],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get(
            "/api/provider/historical/expiries?year=2024&month=1"
        )
        status = client.get("/api/provider/status").json()
    assert response.status_code == 403
    assert "subscription" in response.json()["detail"]
    assert MARKER not in response.text
    assert status["state"] == "connected" and status["configured"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {
            "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
            "start_time": "2019-12-31T09:15",
            "end_time": "2019-12-31T09:30",
            "candle_interval": "15minute",
        },
        {
            "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
            "start_time": "2024-01-03T09:15",
            "end_time": "2024-01-04T09:16",
            "candle_interval": "15minute",
        },
        {
            "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
            "start_time": "2024-01-03T09:15+05:30",
            "end_time": "2024-01-03T09:30+05:30",
            "candle_interval": "15minute",
        },
        {
            "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
            "start_time": "2024-01-03T09:15",
            "end_time": "2024-01-03T09:30",
            "candle_interval": "1minute",
        },
    ],
)
def test_historical_candle_request_bounds_reject_before_provider_call(provider_env, payload):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, transports = groww_manager_with_transport(
        [now], responses=[(200, {"token": "tok-12345678"})]
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.post("/api/provider/historical/candles/check", json=payload)
    assert response.status_code == 422
    assert len(transports[0].calls) == 1


def test_historical_future_candle_window_is_rejected_without_upstream_request(provider_env):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, transports = groww_manager_with_transport(
        [now], responses=[(200, {"token": "tok-12345678"})]
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.post(
            "/api/provider/historical/candles/check",
            json={
                "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
                "start_time": "2025-01-03T09:30",
                "end_time": "2025-01-03T09:45",
                "candle_interval": "5minute",
            },
        )
    assert response.status_code == 422
    assert "future" in response.json()["detail"]
    assert len(transports[0].calls) == 1


@pytest.mark.parametrize(
    "url",
    [
        "/api/provider/historical/expiries?year=2019&month=1",
        "/api/provider/historical/expiries?year=2026&month=1",
        "/api/provider/historical/expiries?year=2024&month=13",
    ],
)
def test_historical_expiry_bounds_reject_without_upstream_request(provider_env, url):
    now = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    mgr, _, transports = groww_manager_with_transport(
        [now], responses=[(200, {"token": "tok-12345678"})]
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
        response = client.get(url)
    assert response.status_code == 422
    assert len(transports[0].calls) == 1


@needs_browser
def test_historical_diagnostic_is_user_triggered_and_stays_out_of_paper_ledger(tmp_path):
    snapshot = _run_harness_page(tmp_path, f"""<script>
const calls = [];
window.fetch = async (path, options={{}}) => {{
  const url = String(path);
  calls.push({{url, method:options.method || "GET", body:options.body || null}});
  let data = {{}};
  if (url === "/api/strategy/decisions" || url === "/api/trades" || url === "/api/replay") data = [];
  else if (url === "/api/provider/status") data = {{state:"connected"}};
  else if (url.startsWith("/api/provider/historical/expiries")) data = {{expiries:["2024-01-04"]}};
  else if (url.startsWith("/api/provider/historical/contracts")) data = {{contracts:["NSE-NIFTY-04Jan24-19200-CE"]}};
  else if (url === "/api/provider/historical/candles/check") data = {{
    status:"available", groww_symbol:"NSE-NIFTY-04Jan24-19200-CE", candle_interval:"15minute",
    candle_count:1, first_timestamp:"2024-01-03T09:15:00", last_timestamp:"2024-01-03T09:15:00",
    invalid_ohlc_count:0, quality_warnings:[], timestamps_timezone:"Timezone not verified."
  }};
  return {{ok:true,status:200,json:async()=>data}};
}};
</script>
<script src="{(_STATIC_DIR / 'app.js').as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(80);
  const automaticHistoricalCalls = calls.filter(call => call.url.includes("/api/provider/historical/")).length;
  document.querySelector('[data-tab="groww"]').click();
  const tabs = {{
    growwSelected:document.querySelector('[data-tab="groww"]').getAttribute("aria-pressed"),
    providerHidden:document.getElementById("provider-panel").hidden,
    paperHidden:document.getElementById("historical-replay").hidden,
  }};
  const expiryForm = document.getElementById("historical-expiry-form");
  expiryForm.elements.year.value = "2024";
  expiryForm.elements.month.value = "1";
  expiryForm.requestSubmit();
  await sleep(30);
  document.getElementById("historical-expiry").value = "2024-01-04";
  document.getElementById("historical-expiry").dispatchEvent(new Event("change"));
  document.getElementById("historical-contracts-load").click();
  await sleep(30);
  document.getElementById("historical-contract").value = "NSE-NIFTY-04Jan24-19200-CE";
  document.getElementById("historical-contract").dispatchEvent(new Event("change"));
  const candleForm = document.getElementById("historical-candles-form");
  candleForm.elements.start_time.value = "2024-01-03T09:15";
  candleForm.elements.end_time.value = "2024-01-03T09:30";
  candleForm.elements.candle_interval.value = "15minute";
  candleForm.requestSubmit();
  await sleep(30);
  const snapshot = {{
    automaticHistoricalCalls,
    tabs,
    historicalCalls:calls.filter(call => call.url.includes("/api/provider/historical/")),
    candleCount:document.getElementById("historical-candle-count").innerText,
    paperLedgerHidden:document.getElementById("paper-ledger").hidden,
  }};
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify([snapshot]);
  document.body.appendChild(pre);
}})();
</script>""")[0]
    assert snapshot["automaticHistoricalCalls"] == 0
    assert snapshot["tabs"] == {
        "growwSelected": "true",
        "providerHidden": False,
        "paperHidden": True,
    }
    assert [call["url"].split("?")[0] for call in snapshot["historicalCalls"]] == [
        "/api/provider/historical/expiries",
        "/api/provider/historical/contracts",
        "/api/provider/historical/candles/check",
    ]
    assert snapshot["historicalCalls"][-1]["method"] == "POST"
    assert json.loads(snapshot["historicalCalls"][-1]["body"]) == {
        "groww_symbol": "NSE-NIFTY-04Jan24-19200-CE",
        "start_time": "2024-01-03T09:15",
        "end_time": "2024-01-03T09:30",
        "candle_interval": "15minute",
    }
    assert snapshot["candleCount"] == "1"
    assert snapshot["paperLedgerHidden"] is True


@needs_browser
def test_historical_discovery_and_candle_statuses_are_terminal_and_consistent(tmp_path):
    snapshot = _run_harness_page(tmp_path, f"""<script>
let expiryMode = "empty";
let contractMode = "empty";
let candleMode = 403;
window.fetch = async path => {{
  const url = String(path);
  if (url === "/api/strategy/decisions" || url === "/api/trades" || url === "/api/replay") {{
    return {{ok:true,status:200,json:async()=>[]}};
  }}
  if (url === "/api/status") return {{ok:true,status:200,json:async()=>({{capital_inr:100000}})}};
  if (url === "/api/risk") return {{ok:true,status:200,json:async()=>({{
    daily_pnl:0,realised_pnl:0,unrealised_pnl:0,open_positions:0,
    max_daily_loss_inr:2000,max_trade_risk_pct:.005,max_trade_risk_inr:500,
    consecutive_losses:0,max_consecutive_losses:3,paused_for_day:false,kill_switch:false
  }})}};
  if (url === "/api/provider/status") return {{ok:true,status:200,json:async()=>({{state:"connected"}})}};
  let mode = "empty";
  if (url.startsWith("/api/provider/historical/expiries")) mode = expiryMode;
  else if (url.startsWith("/api/provider/historical/contracts")) mode = contractMode;
  else if (url === "/api/provider/historical/candles/check") mode = candleMode;
  if (mode === 401 || mode === 403 || mode === 500) {{
    return {{ok:false,status:mode,json:async()=>({{detail:"Safe provider failure."}})}};
  }}
  if (url.startsWith("/api/provider/historical/expiries")) {{
    return {{ok:true,status:200,json:async()=>({{expiries:mode === "success" ? ["2024-01-04"] : []}})}};
  }}
  if (url.startsWith("/api/provider/historical/contracts")) {{
    return {{ok:true,status:200,json:async()=>({{
      contracts:mode === "success" ? ["NSE-NIFTY-04Jan24-19200-CE"] : []
    }})}};
  }}
  return {{ok:true,status:200,json:async()=>({{
    status:"available",candle_count:1,first_timestamp:"2024-01-03T09:15:00",
    last_timestamp:"2024-01-03T09:15:00",invalid_ohlc_count:0,
    quality_warnings:[],timestamps_timezone:"Timezone not verified."
  }})}};
}};
</script>
<script src="{(_STATIC_DIR / 'app.js').as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const waitFor = async expected => {{
    for(let i=0; i<100; i++) {{
      if(document.getElementById("historical-check-status").innerText === expected) return;
      await sleep(10);
    }}
    throw new Error("Timed out waiting for historical status: " + expected);
  }};
  await sleep(80);
  document.querySelector('[data-tab="groww"]').click();
  const expiryForm = document.getElementById("historical-expiry-form");
  const checkExpiry = async (mode, expected) => {{
    expiryMode = mode;
    expiryForm.requestSubmit();
    await waitFor(expected);
    return document.getElementById("historical-check-message").innerText;
  }};
  const expiryEmpty = await checkExpiry("empty", "No data");
  const expirySuccess = await checkExpiry("success", "Data returned");
  const expiryDenied = await checkExpiry(403, "Access denied");
  const expiryUnauthorized = await checkExpiry(401, "Reconnect required");

  const expiry = document.getElementById("historical-expiry");
  expiry.replaceChildren(new Option("2024-01-04", "2024-01-04"));
  expiry.disabled = false;
  expiry.value = "2024-01-04";
  expiry.dispatchEvent(new Event("change"));
  const contractsButton = document.getElementById("historical-contracts-load");
  const checkContracts = async (mode, expected) => {{
    contractMode = mode;
    contractsButton.click();
    await waitFor(expected);
    return document.getElementById("historical-check-message").innerText;
  }};
  const contractsEmpty = await checkContracts("empty", "No data");
  const contractsDenied = await checkContracts(403, "Access denied");
  const contractsUnauthorized = await checkContracts(401, "Reconnect required");

  const contract = document.getElementById("historical-contract");
  contract.replaceChildren(new Option("NIFTY option", "NSE-NIFTY-04Jan24-19200-CE"));
  contract.disabled = false;
  contract.value = "NSE-NIFTY-04Jan24-19200-CE";
  contract.dispatchEvent(new Event("change"));
  const candleForm = document.getElementById("historical-candles-form");
  candleForm.elements.start_time.value = "2024-01-03T09:15";
  candleForm.elements.end_time.value = "2024-01-03T09:30";
  candleForm.elements.candle_interval.value = "15minute";
  const checkCandles = async (mode, expected) => {{
    candleMode = mode;
    candleForm.requestSubmit();
    await waitFor(expected);
    return document.getElementById("historical-check-message").innerText;
  }};
  const candlesDenied = await checkCandles(403, "Access denied");
  const candlesUnauthorized = await checkCandles(401, "Reconnect required");
  const candlesFailed = await checkCandles(500, "Check failed");
  const out = [{{
    expiryEmpty, expirySuccess, expiryDenied, expiryUnauthorized,
    contractsEmpty, contractsDenied, contractsUnauthorized,
    candlesDenied, candlesUnauthorized, candlesFailed,
    finalState:document.getElementById("historical-check-status").dataset.state
  }}];
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}})();
</script>""")[0]
    assert snapshot["expiryEmpty"] == "Request succeeded but no expiries were returned for that month."
    assert "expiry date(s) returned" in snapshot["expirySuccess"]
    assert snapshot["expiryDenied"] == (
        "Groww denied access. The specific cause may require verification."
    )
    assert snapshot["expiryUnauthorized"] == "Safe provider failure."
    assert snapshot["contractsEmpty"] == (
        "Request succeeded but no option contracts were returned for that expiry."
    )
    assert snapshot["contractsDenied"] == snapshot["expiryDenied"]
    assert snapshot["contractsUnauthorized"] == "Safe provider failure."
    assert snapshot["candlesDenied"] == snapshot["expiryDenied"]
    assert snapshot["candlesUnauthorized"] == "Safe provider failure."
    assert snapshot["candlesFailed"] == "Safe provider failure."
    assert snapshot["finalState"] == "failed"