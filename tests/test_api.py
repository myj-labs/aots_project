import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

# The API module constructs its database at import time. Keep that initialization
# isolated too; API integration tests install their own per-test database below.
_import_database_dir = tempfile.TemporaryDirectory(prefix="aots-api-import-")
_original_database_path = os.environ.get("AOTS_DATABASE_PATH")
os.environ["AOTS_DATABASE_PATH"] = str(
    Path(_import_database_dir.name) / "aots.db"
)
from aots import api
from aots.api import app
from aots.database import Database
from aots.paper import PaperBroker
from aots.risk import RiskEngine


def test_health_is_paper_only():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["mode"] == "paper"
    assert r.json()["live_trading_enabled"] is False


def test_dashboard_loads():
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "AOTS" in r.text
    assert "PAPER MODE" in r.text
    for label in (
        "Realised today",
        "Open unrealised",
        "Daily entry pause",
        "Position side",
        "Option type",
        "Stop premium",
        "Total profit target",
        "Entered (IST)",
        "Closed (IST)",
        "Closure reason",
        "mark freshness is unavailable",
    ):
        assert label in r.text

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "Open unrealised" in script.text
    assert "Closed realised" in script.text
    assert 'timeZone:"Asia/Kolkata"' in script.text


def test_mark_endpoint_requires_observation_sequence():
    client = TestClient(app)
    response = client.post(
        "/api/paper/mark",
        json={"trade_id": "test", "mark_price": 100},
    )
    assert response.status_code == 422


def test_mark_endpoint_rejects_non_integer_observation_sequence():
    client = TestClient(app)
    response = client.post(
        "/api/paper/mark",
        json={
            "trade_id": "test",
            "mark_price": 100,
            "observation_sequence": 1.5,
        },
    )
    assert response.status_code == 422


def test_mark_endpoint_maps_stale_sequence_to_conflict(monkeypatch):
    received = []

    class StalePaper:
        def mark(self, trade_id, mark_price, observation_sequence):
            received.append((trade_id, mark_price, observation_sequence))
            raise ValueError(
                "Mark observation is stale or duplicate; sequence must be greater."
            )

    monkeypatch.setattr(api, "paper", StalePaper())
    client = TestClient(app)
    response = client.post(
        "/api/paper/mark",
        json={
            "trade_id": "test",
            "mark_price": 100,
            "observation_sequence": 2,
        },
    )

    assert response.status_code == 409
    assert "stale or duplicate" in response.json()["detail"]
    assert received == [("test", 100, 2)]


@pytest.fixture
def paper_api(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        app_name="AOTS API test",
        environment="test",
        broker="broker-independent",
        trading_mode="paper",
        underlying="NIFTY",
        capital_inr=100_000,
        max_daily_loss_pct=.02,
        max_trade_risk_pct=.005,
        max_consecutive_losses=3,
        max_open_positions=3,
        max_lots_per_order=1,
        market_data_max_age_seconds=5,
        strategy_spot_threshold=25_000,
        database_path=str(tmp_path / "api-test.db"),
    )
    fixed_time = datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc)
    db = Database(settings.database_path)
    risk = RiskEngine(settings, db, clock=lambda: fixed_time)
    paper = PaperBroker(settings, db, risk)
    monkeypatch.setattr(api, "settings", settings)
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "risk", risk)
    monkeypatch.setattr(api, "paper", paper)

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, settings, db, risk


def trade_payload(key="api-trade"):
    return {
        "symbol": "NIFTY-DEMO-CE",
        "idempotency_key": key,
        "side": "BUY",
        "entry_price": 100,
        "stop_price": 95,
        "quantity": 10,
        "lots": 1,
        "market_data_age_seconds": 0,
    }


def strategy_payload(observation_id="api-observation", **overrides):
    payload = {
        "observation_id": observation_id,
        "underlying": "NIFTY",
        "spot": 25_100,
        "observed_at": "2025-01-03T04:00:00+00:00",
        "symbol": "NIFTY-DEMO-CE",
        "entry_price": 100,
        "stop_price": 95,
        "quantity": 10,
        "lots": 1,
        "take_profit_profit_inr": 100,
    }
    payload.update(overrides)
    return payload


def decoded_events(client):
    return [
        {
            **event,
            "detail": json.loads(event["detail_json"]),
        }
        for event in client.get("/api/events").json()
    ]


def test_api_paper_lifecycle_observation_rejection_stop_and_closed_state(paper_api):
    client, _, db, _ = paper_api
    created = client.post("/api/paper/trade", json=trade_payload())
    assert created.status_code == 200
    trade = created.json()
    trade_id = trade["trade_id"]
    assert trade["status"] == "OPEN"
    assert trade["mark_sequence"] == 0
    assert client.get("/api/positions").json()[0]["trade_id"] == trade_id
    assert client.get("/api/risk").json()["open_positions"] == 1

    accepted = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 110,
            "observation_sequence": 2,
        },
    )
    assert accepted.status_code == 200
    marked = accepted.json()
    assert marked["mark_price"] == 110
    assert marked["unrealised_pnl"] == 100
    assert marked["mark_sequence"] == 2
    risk_before_rejections = client.get("/api/risk").json()
    events_before_rejections = decoded_events(client)
    trades_before_rejections = client.get("/api/trades").json()

    for sequence in (2, 1):
        rejected = client.post(
            "/api/paper/mark",
            json={
                "trade_id": trade_id,
                "mark_price": 90,
                "observation_sequence": sequence,
            },
        )
        assert rejected.status_code == 409
        assert "stale or duplicate" in rejected.json()["detail"]
        assert client.get("/api/trades").json() == trades_before_rejections
        assert client.get("/api/risk").json() == risk_before_rejections
        assert decoded_events(client) == events_before_rejections
        assert db.trades()[0]["status"] == "OPEN"

    stale_stop = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 90,
            "observation_sequence": 1,
        },
    )
    assert stale_stop.status_code == 409
    assert db.trades()[0]["status"] == "OPEN"
    assert client.get("/api/risk").json() == risk_before_rejections

    stopped = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 90,
            "observation_sequence": 3,
        },
    )
    assert stopped.status_code == 200
    closed = stopped.json()
    assert closed["status"] == "CLOSED"
    assert closed["mark_price"] == 90
    assert closed["mark_sequence"] == 3
    assert closed["realised_pnl"] == -100
    assert closed["unrealised_pnl"] == 0

    risk_after_stop = client.get("/api/risk").json()
    assert risk_after_stop["realised_pnl"] == -100
    assert risk_after_stop["unrealised_pnl"] == 0
    assert risk_after_stop["daily_pnl"] == -100
    assert risk_after_stop["open_positions"] == 0
    assert client.get("/api/positions").json() == []

    events = decoded_events(client)
    mark_events = [e for e in events if e["event_type"] == "PAPER_TRADE_MARKED"]
    close_events = [e for e in events if e["event_type"] == "PAPER_TRADE_CLOSED"]
    assert len(mark_events) == 2
    assert len(close_events) == 1
    stop_mark = next(e for e in mark_events if e["detail"]["observation_sequence"] == 3)
    assert stop_mark["detail"]["unrealised_pnl"] == 0
    assert close_events[0]["detail"]["pnl"] == -100
    assert close_events[0]["detail"]["reason"] == "SIMULATED_STOP"

    closed_trade_before = client.get("/api/trades").json()
    events_before_closed_mark = decoded_events(client)
    after_close = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 120,
            "observation_sequence": 4,
        },
    )
    assert after_close.status_code == 409
    assert client.get("/api/trades").json() == closed_trade_before
    assert decoded_events(client) == events_before_closed_mark
    assert client.get("/api/risk").json() == risk_after_stop


def test_strategy_api_evaluation_is_pure_and_process_is_idempotent(paper_api):
    client, _, db, _ = paper_api
    before_events = decoded_events(client)
    evaluation = client.post(
        "/api/strategy/evaluate", json=strategy_payload()
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["status"] == "SIGNAL"
    assert db.trades() == []
    assert db.strategy_decisions() == []
    assert decoded_events(client) == before_events

    processed = client.post(
        "/api/strategy/process", json=strategy_payload()
    )
    assert processed.status_code == 200
    result = processed.json()
    assert result["status"] == "TRADE_CREATED"
    assert result["resulting_trade_id"]
    assert len(db.trades()) == 1
    assert len(db.strategy_decisions()) == 1
    assert db.trades()[0]["idempotency_key"].startswith("strategy:")
    assert len(db.trades()[0]["idempotency_key"]) < 128

    replay = client.post(
        "/api/strategy/process", json=strategy_payload()
    )
    assert replay.status_code == 200
    assert replay.json() == result
    assert len(db.trades()) == 1
    assert len(db.strategy_decisions()) == 1

    conflict = client.post(
        "/api/strategy/process",
        json=strategy_payload(spot=25_200),
    )
    assert conflict.status_code == 409
    assert "different inputs" in conflict.json()["detail"]
    assert len(db.trades()) == 1
    assert client.get("/api/strategy/decisions").json() == [result]


def test_strategy_api_validation_risk_rejection_and_dashboard(paper_api):
    client, _, db, _ = paper_api
    invalid = client.post(
        "/api/strategy/evaluate",
        json=strategy_payload(observed_at="2025-01-03T04:00:00"),
    )
    assert invalid.status_code == 422
    assert db.trades() == []

    enabled = client.post("/api/risk/kill-switch", json={"enabled": True})
    assert enabled.status_code == 200
    blocked = client.post(
        "/api/strategy/process", json=strategy_payload("blocked-by-kill")
    )
    assert blocked.status_code == 200
    assert blocked.json()["status"] == "RISK_REJECTED"
    assert "Kill switch" in blocked.json()["reason"]
    assert db.trades() == []
    assert client.get("/api/strategy/decisions").json() == [blocked.json()]

    page = client.get("/")
    script = client.get("/static/app.js")
    assert "Deterministic strategy demonstration" in page.text
    assert "Evaluate only" in page.text
    assert "Process paper entry" in page.text
    assert "Recent persisted strategy decisions" in page.text
    assert "mark freshness" in page.text
    assert "/api/strategy/evaluate" in script.text
    assert "/api/strategy/process" in script.text


@pytest.mark.parametrize(
    "payload",
    [
        {"trade_id": "missing-sequence", "mark_price": 100},
        {"trade_id": "zero-sequence", "mark_price": 100, "observation_sequence": 0},
        {"trade_id": "fractional-sequence", "mark_price": 100, "observation_sequence": 1.5},
        {"trade_id": "boolean-sequence", "mark_price": 100, "observation_sequence": True},
        {
            **trade_payload("invalid-stop"),
            "stop_price": 100,
        },
        {
            **trade_payload("target-at-entry"),
            "take_profit_profit_inr": 0,
        },
        {
            **trade_payload("target-below-entry"),
            "take_profit_profit_inr": -1,
        },
    ],
)
def test_api_validation_errors_leave_isolated_ledger_unchanged(paper_api, payload):
    client, _, db, risk = paper_api
    initial_events = decoded_events(client)
    initial_risk = risk.snapshot()

    if "idempotency_key" in payload:
        response = client.post("/api/paper/trade", json=payload)
    else:
        response = client.post("/api/paper/mark", json=payload)

    assert response.status_code == 422
    assert db.trades() == []
    assert decoded_events(client) == initial_events
    assert risk.snapshot() == initial_risk


def test_api_sqlite_reinitialization_restores_trade_risk_and_kill_switch(
    paper_api, monkeypatch
):
    client, settings, db, _ = paper_api
    created = client.post("/api/paper/trade", json=trade_payload("restart"))
    trade_id = created.json()["trade_id"]
    marked = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 105,
            "observation_sequence": 4,
        },
    )
    assert marked.status_code == 200
    switched = client.post("/api/risk/kill-switch", json={"enabled": True})
    assert switched.status_code == 200

    reopened_db = Database(db.path)
    reopened_risk = RiskEngine(
        settings,
        reopened_db,
        clock=lambda: datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(api, "db", reopened_db)
    monkeypatch.setattr(api, "risk", reopened_risk)
    monkeypatch.setattr(api, "paper", PaperBroker(settings, reopened_db, reopened_risk))

    persisted = client.get("/api/trades")
    assert persisted.status_code == 200
    assert persisted.json()[0]["trade_id"] == trade_id
    assert persisted.json()[0]["mark_sequence"] == 4
    assert persisted.json()[0]["unrealised_pnl"] == 50
    assert client.get("/api/risk").json()["unrealised_pnl"] == 50
    assert client.get("/api/risk").json()["kill_switch"] is True
    assert any(
        event["event_type"] == "PAPER_TRADE_MARKED"
        for event in client.get("/api/events").json()
    )


def test_api_take_profit_persists_and_closes_with_observed_mark(paper_api, monkeypatch):
    client, settings, db, _ = paper_api
    payload = {
        **trade_payload("api-take-profit"),
        "quantity": 65,
        "take_profit_profit_inr": 650,
    }
    created = client.post("/api/paper/trade", json=payload)
    assert created.status_code == 200
    trade_id = created.json()["trade_id"]
    assert created.json()["take_profit_profit_inr"] == 650
    assert created.json()["side"] == "BUY"
    assert created.json()["quantity"] == 65
    assert created.json()["lots"] == 1
    assert created.json()["created_at"]
    assert created.json()["stop_price"] == 95

    reopened_db = Database(db.path)
    reopened_risk = RiskEngine(
        settings,
        reopened_db,
        clock=lambda: datetime(2025, 1, 3, 4, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(api, "db", reopened_db)
    monkeypatch.setattr(api, "risk", reopened_risk)
    monkeypatch.setattr(api, "paper", PaperBroker(settings, reopened_db, reopened_risk))

    under_target = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 109,
            "observation_sequence": 1,
        },
    )
    assert under_target.status_code == 200
    assert under_target.json()["status"] == "OPEN"
    assert under_target.json()["unrealised_pnl"] == 585
    before_stale_trades = client.get("/api/trades").json()
    before_stale_risk = client.get("/api/risk").json()
    before_stale_events = decoded_events(client)

    stale_target = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 120,
            "observation_sequence": 1,
        },
    )
    assert stale_target.status_code == 409
    assert client.get("/api/trades").json() == before_stale_trades
    assert client.get("/api/risk").json() == before_stale_risk
    assert decoded_events(client) == before_stale_events

    target_hit = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 110,
            "observation_sequence": 2,
        },
    )
    assert target_hit.status_code == 200
    closed = target_hit.json()
    assert closed["status"] == "CLOSED"
    assert closed["take_profit_profit_inr"] == 650
    assert closed["mark_price"] == 110
    assert closed["close_reason"] == "SIMULATED_TAKE_PROFIT"
    assert closed["realised_pnl"] == 650
    assert closed["unrealised_pnl"] == 0
    assert closed["closed_at"]
    stored_trade = client.get("/api/trades").json()[0]
    assert stored_trade["close_reason"] == "SIMULATED_TAKE_PROFIT"
    stored_closed_at = datetime.fromisoformat(
        stored_trade["closed_at"].replace("Z", "+00:00")
    )
    response_closed_at = datetime.fromisoformat(
        closed["closed_at"].replace("Z", "+00:00")
    )
    assert stored_closed_at == response_closed_at
    assert stored_trade["mark_price"] == 110
    assert client.get("/api/positions").json() == []
    risk_after_close = client.get("/api/risk").json()
    assert risk_after_close["realised_pnl"] == 650
    assert risk_after_close["unrealised_pnl"] == 0
    assert risk_after_close["daily_pnl"] == 650
    assert risk_after_close["open_positions"] == 0

    closed_again = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 130,
            "observation_sequence": 3,
        },
    )
    assert closed_again.status_code == 409
    assert reopened_db.trades()[0]["realised_pnl"] == 650
    close_events = [
        event for event in decoded_events(client)
        if event["event_type"] == "PAPER_TRADE_CLOSED"
    ]
    assert len(close_events) == 1
    assert close_events[0]["detail"]["exit_price"] == 110
    assert close_events[0]["detail"]["reason"] == "SIMULATED_TAKE_PROFIT"


def test_api_invalid_mark_sequences_cannot_trigger_take_profit(paper_api):
    client, _, db, _ = paper_api
    created = client.post(
        "/api/paper/trade",
        json={
            **trade_payload("invalid-sequence-target"),
            "take_profit_profit_inr": 50,
        },
    )
    trade_id = created.json()["trade_id"]
    accepted = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 101,
            "observation_sequence": 2,
        },
    )
    assert accepted.status_code == 200
    before_trade = db.trades()[0]
    before_risk = client.get("/api/risk").json()
    before_events = decoded_events(client)

    invalid_sequences = (None, 0, 2.5, True)
    for sequence in invalid_sequences:
        payload = {"trade_id": trade_id, "mark_price": 110}
        if sequence is not None:
            payload["observation_sequence"] = sequence
        response = client.post("/api/paper/mark", json=payload)
        assert response.status_code == 422
        assert db.trades()[0] == before_trade
        assert client.get("/api/risk").json() == before_risk
        assert decoded_events(client) == before_events


@pytest.mark.parametrize("failed_event", ["PAPER_TRADE_MARKED", "PAPER_TRADE_CLOSED"])
def test_api_take_profit_persistence_failure_rolls_back_everything(
    paper_api, monkeypatch, failed_event
):
    client, _, db, risk = paper_api
    created = client.post(
        "/api/paper/trade",
        json={
            **trade_payload("take-profit-rollback"),
            "quantity": 65,
            "take_profit_profit_inr": 650,
        },
    )
    trade_id = created.json()["trade_id"]
    before_risk = client.get("/api/risk").json()
    before_events = decoded_events(client)
    original_event = db.event

    def fail_event(event_type, detail, conn=None):
        if event_type == failed_event:
            raise OSError("simulated event persistence failure")
        return original_event(event_type, detail, conn=conn)

    monkeypatch.setattr(db, "event", fail_event)
    response = client.post(
        "/api/paper/mark",
        json={
            "trade_id": trade_id,
            "mark_price": 110,
            "observation_sequence": 1,
        },
    )
    assert response.status_code == 500

    persisted = db.trades()[0]
    assert persisted["status"] == "OPEN"
    assert persisted["mark_price"] == 100
    assert persisted["mark_sequence"] == 0
    assert persisted["realised_pnl"] == 0
    assert persisted["unrealised_pnl"] == 0
    assert client.get("/api/risk").json() == before_risk
    assert decoded_events(client) == before_events
    assert risk.get_consecutive_losses() == 0


def test_dashboard_serves_trade_controls_and_sequence_aware_script(paper_api):
    client, _, _, _ = paper_api
    page = client.get("/")
    script = client.get("/static/app.js")

    assert page.status_code == 200
    assert 'id="trade-form"' in page.text
    assert 'id="trades-body"' in page.text
    assert script.status_code == 200
    assert 'data-sequence="${Number(t.mark_sequence)+1}"' in script.text
    assert 'observation_sequence:Number(b.dataset.sequence)' in script.text
    assert 'name="take_profit_profit_inr"' in page.text
    assert "Total quantity (all lots)" in page.text
    assert 't.close_reason==="SIMULATED_TAKE_PROFIT"' in script.text
    assert 'const target = data.take_profit_profit_inr.trim();' in script.text
    assert 'api("/api/paper/mark"' in script.text


def test_core_endpoints_stay_healthy_when_strategy_history_fails(paper_api, monkeypatch):
    client, _, db, _ = paper_api
    assert client.post("/api/paper/trade", json=trade_payload("partial-refresh")).status_code == 200

    def broken_history():
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(db, "strategy_decisions", broken_history)

    assert client.get("/api/strategy/decisions").status_code == 500
    assert client.get("/api/status").status_code == 200
    risk = client.get("/api/risk")
    assert risk.status_code == 200
    assert risk.json()["open_positions"] == 1
    trades = client.get("/api/trades")
    assert trades.status_code == 200
    assert [(t["symbol"], t["status"]) for t in trades.json()] == [("NIFTY-DEMO-CE", "OPEN")]


def _find_headless_browser():
    for name in ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome"):
        found = shutil.which(name)
        if found:
            return found
    for path in (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        if Path(path).exists():
            return path
    return None


_BROWSER = _find_headless_browser()
_STATIC_DIR = Path(api.__file__).parent / "static"


def run_dashboard_refresh(tmp_path, steps, app_js=None):
    """Run the real index.html/app.js in a headless browser with a stubbed fetch.

    Each step maps an API path to (status, JSON body). The dashboard is refreshed once per
    step and a snapshot of the rendered sections is returned for each.
    """
    script_path = Path(app_js) if app_js else _STATIC_DIR / "app.js"
    return _run_harness_page(tmp_path, f"""<script>
const STEPS = {json.dumps(steps)};
let current = 0;
window.fetch = async (path) => {{
  const route = STEPS[current][path];
  if (!route) throw new TypeError("no stub for " + path);
  return {{ok: route[0] < 400, status: route[0], json: async () => route[1]}};
}};
</script>
<script src="{script_path.as_uri()}"></script>
<script>
(async () => {{
  const ids = ["health-label","capital","pnl","positions-count","risk-badge","trades-body","strategy-decisions"];
  const out = [];
  for (let i = 0; i < STEPS.length; i++) {{
    current = i;
    await new Promise(r => setTimeout(r, 50));
    await refresh();
    out.push(Object.fromEntries(ids.map(id => [id, {{text: document.getElementById(id).innerText,
      html: document.getElementById(id).innerHTML}}])));
  }}
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}})();
</script>""")


def _run_harness_page(tmp_path, harness):
    html_text = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    page = html_text.replace('<script src="/static/app.js"></script>', harness)
    assert harness in page
    page_path = tmp_path / "dashboard-harness.html"
    page_path.write_text(page, encoding="utf-8")
    result = subprocess.run(
        [_BROWSER, "--headless=new", "--disable-gpu", "--no-first-run", "--dump-dom",
         "--virtual-time-budget=10000", f"--user-data-dir={tmp_path / 'browser-profile'}",
         page_path.as_uri()],
        capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
    )
    match = re.search(r'<pre id="snapshot">(.*?)</pre>', result.stdout, re.S)
    assert match, f"dashboard harness produced no snapshot: {result.stderr[-500:]}"
    return json.loads(html.unescape(match.group(1)))


def run_overlapping_refreshes(tmp_path, older, newer):
    """Start two refreshes, answer the newer one first, then the older one, and snapshot."""
    script_path = _STATIC_DIR / "app.js"
    return _run_harness_page(tmp_path, f"""<script>
const RESPONSES = {json.dumps({"older": older, "newer": newer})};
const pending = [];
window.fetch = (path) => new Promise(resolve => pending.push({{path, resolve}}));
const answer = (entry, which) => {{
  const route = RESPONSES[which][entry.path];
  entry.resolve({{ok: route[0] < 400, status: route[0], json: async () => route[1]}});
}};
</script>
<script src="{script_path.as_uri()}"></script>
<script>
(async () => {{
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  await sleep(20);
  const olderRequests = pending.splice(0);   // the refresh started when the page loaded
  refresh();                                  // a second, newer refresh
  await sleep(20);
  const newerRequests = pending.splice(0);
  newerRequests.forEach(e => answer(e, "newer"));
  await sleep(50);
  olderRequests.forEach(e => answer(e, "older"));
  await sleep(50);
  const ids = ["health-label","capital","positions-count","trades-body","strategy-decisions"];
  const out = [Object.fromEntries(ids.map(id => [id, {{text: document.getElementById(id).innerText}}]))];
  const pre = document.createElement("pre");
  pre.id = "snapshot";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}})();
</script>""")


def dashboard_steps(client, **overrides):
    """Real API payloads (open trade seeded by the caller) plus per-step failure overrides."""
    routes = {
        path: (200, client.get(path).json())
        for path in ("/api/status", "/api/risk", "/api/trades")
    }
    routes["/api/strategy/decisions"] = (200, [])
    routes.update(overrides)
    return routes


needs_browser = pytest.mark.skipif(_BROWSER is None, reason="no headless Chrome/Edge available")


def _older_and_newer_dashboard_state(client):
    older = dashboard_steps(client)
    older["/api/trades"] = (200, [])
    older["/api/status"] = (200, {**older["/api/status"][1], "capital_inr": 111_111})
    older["/api/risk"] = (200, {**older["/api/risk"][1], "open_positions": 0})
    newer = dashboard_steps(client)
    newer["/api/status"] = (200, {**newer["/api/status"][1], "capital_inr": 222_222})
    return older, newer


@needs_browser
def test_older_refresh_response_cannot_overwrite_newer_dashboard_state(paper_api, tmp_path):
    client, _, _, _ = paper_api
    assert client.post("/api/paper/trade", json=trade_payload("overlap")).status_code == 200
    older, newer = _older_and_newer_dashboard_state(client)

    (snapshot,) = run_overlapping_refreshes(tmp_path, older, newer)

    assert "₹2,22,222" in snapshot["capital"]["text"]
    assert snapshot["positions-count"]["text"] == "1"
    assert "NIFTY-DEMO-CE" in snapshot["trades-body"]["text"]
    assert "No paper trades yet" not in snapshot["trades-body"]["text"]
    assert snapshot["health-label"]["text"] == "System healthy · paper only"


@needs_browser
def test_older_failed_refresh_cannot_overwrite_newer_successful_dashboard_state(
    paper_api, tmp_path
):
    client, _, _, _ = paper_api
    assert client.post("/api/paper/trade", json=trade_payload("overlap-fail")).status_code == 200
    older, newer = _older_and_newer_dashboard_state(client)
    older["/api/trades"] = (503, {"detail": "late failure"})
    older["/api/strategy/decisions"] = (503, {"detail": "late history failure"})

    (snapshot,) = run_overlapping_refreshes(tmp_path, older, newer)

    assert snapshot["health-label"]["text"] == "System healthy · paper only"
    assert "NIFTY-DEMO-CE" in snapshot["trades-body"]["text"]
    assert "unavailable" not in snapshot["trades-body"]["text"]
    assert "unavailable" not in snapshot["strategy-decisions"]["text"]


@needs_browser
def test_dashboard_renders_core_sections_when_strategy_history_returns_503(paper_api, tmp_path):
    client, _, _, _ = paper_api
    assert client.post("/api/paper/trade", json=trade_payload("browser-open")).status_code == 200
    healthy = dashboard_steps(client)
    failed_history = dashboard_steps(
        client,
        **{"/api/strategy/decisions": (503, {"detail": "history <b>down</b>"})},
    )

    normal, partial = run_dashboard_refresh(tmp_path, [healthy, failed_history])

    for snapshot in (normal, partial):
        assert snapshot["health-label"]["text"] == "System healthy · paper only"
        assert "API unavailable" not in snapshot["health-label"]["text"]
        assert "₹1,00,000" in snapshot["capital"]["text"]
        assert snapshot["positions-count"]["text"] == "1"
        assert snapshot["risk-badge"]["text"] == "Active"
        assert "NIFTY-DEMO-CE" in snapshot["trades-body"]["text"]
        assert "OPEN" in snapshot["trades-body"]["text"]
        assert "Loading trades" not in snapshot["trades-body"]["text"]
    assert "No processed observations yet" in normal["strategy-decisions"]["text"]
    history = partial["strategy-decisions"]
    assert "Strategy history unavailable: history <b>down</b>" in history["text"]
    assert "Loading decisions" not in history["text"]
    # The error message is escaped, not injected as markup.
    assert "<b>" not in history["html"]
    assert "&lt;b&gt;down&lt;/b&gt;" in history["html"]


@needs_browser
@pytest.mark.parametrize("failing_path", ["/api/status", "/api/risk", "/api/trades"])
def test_dashboard_reports_core_request_failure_without_claiming_fresh_data(
    paper_api, tmp_path, failing_path
):
    client, _, _, _ = paper_api
    assert client.post("/api/paper/trade", json=trade_payload("browser-core")).status_code == 200
    healthy = dashboard_steps(client)
    failed_core = dashboard_steps(
        client, **{failing_path: (503, {"detail": "core <i>down</i>"})}
    )

    before, after = run_dashboard_refresh(tmp_path, [healthy, failed_core])

    assert before["health-label"]["text"] == "System healthy · paper only"
    assert after["health-label"]["text"] == "API unavailable: core <i>down</i>"
    assert "System healthy" not in after["health-label"]["text"]
    assert after["risk-badge"]["text"] == "Data unavailable"
    trades = after["trades-body"]["text"]
    assert "Trades unavailable" in trades and "out of date" in trades
    assert "NIFTY-DEMO-CE" not in trades
    assert "Loading trades" not in trades
    # The optional history section is independent and still renders.
    assert "No processed observations yet" in after["strategy-decisions"]["text"]
    assert "<i>" not in after["health-label"]["html"]


@needs_browser
def test_dashboard_formats_validation_error_details_instead_of_object_object(paper_api, tmp_path):
    client, _, _, _ = paper_api
    detail = [{"loc": ["body", "spot"], "msg": "Input should be greater than 0", "type": "x"}]
    steps = [dashboard_steps(client, **{"/api/status": (422, {"detail": detail})})]

    (snapshot,) = run_dashboard_refresh(tmp_path, steps)

    label = snapshot["health-label"]["text"]
    assert label == "API unavailable: spot: Input should be greater than 0"
    assert "[object Object]" not in label


def teardown_module():
    if _original_database_path is None:
        os.environ.pop("AOTS_DATABASE_PATH", None)
    else:
        os.environ["AOTS_DATABASE_PATH"] = _original_database_path
    _import_database_dir.cleanup()
