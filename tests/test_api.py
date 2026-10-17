from fastapi.testclient import TestClient
from aots import api
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
