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


@pytest.mark.parametrize(
    "response",
    [
        {"token": "tok-12345678"},
        {"token": "tok-12345678", "isActive": False},
    ],
)
def test_token_response_requires_explicit_active_flag(provider_env, response):
    mgr, _, _ = groww_manager_with_transport(
        [datetime(2025, 1, 3, 0, 0, tzinfo=timezone.utc)],
        responses=[(200, response)],
    )
    provider_env.install(mgr)
    with TestClient(app) as client:
        body = client.post("/api/provider/connect", json=APPROVAL).json()
    assert body["state"] == "failed"
    assert body["connected"] is False
    assert body["configured"] is False


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
    for name in ("api_key", "totp_secret", "api_secret"):
        assert f'name="{name}" type="password"' in html_text
    assert 'value="kite" disabled' in html_text
    for banned in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert banned not in js


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
    assert snapshot["afterConnect"] == ["", "", ""] and snapshot["afterDisconnect"] == ["", "", ""]
    assert MARKER in snapshot["connectBody"]
    assert all(MARKER not in url for url in snapshot["urls"])
    assert snapshot["storage"] == [0, 0, ""]
    assert snapshot["status"] == "Disconnected"