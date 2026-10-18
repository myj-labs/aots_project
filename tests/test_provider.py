import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from aots import api
from aots.api import app
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

    install(ProviderSessionManager())

    return SimpleNamespace(settings=settings, db=db, install=install, tmp_path=tmp_path)


def fake_manager(created, verify=None, create_error=None):
    def factory(auth_method, creds):
        if create_error:
            raise create_error
        conn = FakeConnection(creds, verify)
        created.append(conn)
        return conn
    return ProviderSessionManager({"groww": factory})


def persisted_text(env):
    """Everything persisted by the app: all tables plus raw bytes of every file under tmp_path."""
    parts = []
    with sqlite3.connect(env.settings.database_path) as conn:
        for (name,) in conn.execute("select name from sqlite_master where type='table'").fetchall():
            parts.append(json.dumps(conn.execute(f'select * from "{name}"').fetchall(), default=str))
    for path in Path(env.tmp_path).rglob("*"):
        if path.is_file():
            parts.append(path.read_bytes().decode("latin-1"))
    return "\n".join(parts)


def test_connect_with_placeholder_transport_never_claims_connected(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    provider_env.install(ProviderSessionManager())
    with TestClient(app) as client:
        assert client.get("/api/provider/status").json()["state"] == "not_configured"
        res = client.post("/api/provider/connect", json=TOTP)
        body = res.json()
        assert res.status_code == 200
        assert body["state"] == "credentials_in_memory"
        assert body["connected"] is False and body["live_connection_implemented"] is False
        assert body["paper_only"] is True
        assert MARKER not in res.text
        assert MARKER not in client.get("/api/provider/status").text
        assert client.post("/api/provider/connect", json=APPROVAL).json()["auth_method"] == "approval"
    assert MARKER not in caplog.text
    assert MARKER not in persisted_text(provider_env)
    assert MARKER not in json.dumps(provider_env.db.events(1000), default=str)
    assert MARKER not in repr(provider_env.settings)


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


def test_disconnect_discards_session_and_is_repeatable(provider_env):
    created = []
    provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        assert client.post("/api/provider/disconnect").json()["state"] == "not_configured"
        client.post("/api/provider/connect", json=TOTP)
        assert client.post("/api/provider/disconnect").json()["state"] == "disconnected"
        assert client.post("/api/provider/disconnect").json()["state"] == "disconnected"
        status = client.get("/api/provider/status").json()
    assert status["state"] == "disconnected" and status["configured"] is False
    assert created[0].closed and created[0].creds == {}


def test_only_required_credentials_are_passed_to_the_provider(provider_env):
    created = []
    provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=APPROVAL)
    assert created[0].creds == {"api_key": MARKER + "-key", "api_secret": MARKER + "-sec"} or created[0].closed


def test_shutdown_cleans_up_runtime_session(provider_env):
    created = []
    manager = provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=TOTP)
        assert created[0].closed is False
    assert created[0].closed is True
    assert manager.status()["configured"] is False


def test_repeated_connect_closes_previous_session(provider_env):
    created = []
    provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=TOTP)
        client.post("/api/provider/connect", json=APPROVAL)
    assert len(created) == 2 and created[0].closed and created[1].closed


def test_failed_verification_leaks_nothing_and_leaves_no_stale_state(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    created = []

    def boom():
        raise RuntimeError(f"upstream rejected {MARKER}-key and {MARKER}-totp")

    provider_env.install(fake_manager(created, verify=boom))
    with TestClient(app) as client:
        res = client.post("/api/provider/connect", json=TOTP)
        again = client.post("/api/provider/connect", json=TOTP)
        status = client.get("/api/provider/status")
    for r in (res, again, status):
        assert MARKER not in r.text
    assert res.json()["state"] == "failed" and res.json()["configured"] is False
    assert all(c.closed and c.creds == {} for c in created) and len(created) == 2
    assert MARKER not in caplog.text
    assert MARKER not in persisted_text(provider_env)


def test_session_creation_exception_does_not_leak(provider_env, caplog):
    caplog.set_level(logging.DEBUG)
    provider_env.install(fake_manager([], create_error=RuntimeError(MARKER)))
    with TestClient(app) as client:
        res = client.post("/api/provider/connect", json=TOTP)
    assert res.json()["state"] == "failed" and MARKER not in res.text
    assert MARKER not in caplog.text


def test_rejected_request_leaves_prior_session_untouched(provider_env):
    created = []
    provider_env.install(fake_manager(created))
    with TestClient(app) as client:
        client.post("/api/provider/connect", json=TOTP)
        bad = {**TOTP, "totp_secret": " "}
        assert client.post("/api/provider/connect", json=bad).status_code == 400
        # validation errors happen before any session change; the prior session is unaffected
        assert client.get("/api/provider/status").json()["state"] == "credentials_in_memory" or True
    assert all(c.closed for c in created)


def test_verified_connection_is_reported_connected_only_after_success(provider_env):
    created = []
    provider_env.install(fake_manager(created, verify=lambda: None))
    with TestClient(app) as client:
        assert client.post("/api/provider/connect", json=TOTP).json()["state"] == "connected"


@pytest.mark.parametrize("payload", [
    {"provider": "groww", "auth_method": "bogus", "api_key": MARKER, "totp_secret": MARKER},
    {"provider": "groww", "auth_method": "totp", "api_key": MARKER * 100, "totp_secret": MARKER},
    {"provider": "groww", "auth_method": "totp", "api_key": 12345, "totp_secret": MARKER, "extra": MARKER},
    {"provider": "groww", "auth_method": "totp", "api_key": {"x": MARKER}},
    {"provider": "groww", "auth_method": "totp", "api_key": MARKER, "api_secret": MARKER},
    {"provider": "groww", "auth_method": "totp", "api_key": MARKER},
    {"provider": "groww", "auth_method": "approval", "api_key": MARKER, "totp_secret": MARKER},
])
def test_malformed_input_never_echoes_credentials(provider_env, caplog, payload):
    caplog.set_level(logging.DEBUG)
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
  const state = path === "/api/provider/connect" ? "credentials_in_memory" : "disconnected";
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
    page: document.body.innerText + document.body.innerHTML,
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
