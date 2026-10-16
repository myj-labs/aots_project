from fastapi.testclient import TestClient
from aots.api import app


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
