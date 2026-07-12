from fastapi.testclient import TestClient

from llm_gate.api import app


def test_live_gateway_integration():
    with TestClient(app) as client:
        # Hit health endpoint natively
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

        # Hit parsing endpoint
        resp = client.post(
            "/v1/route", json={"task": "Deploy production", "criticality": "critical"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "model" in data
        assert data["tier"] == 0
        assert "critical" in data["reason"]

        # Hit fallback
        resp = client.post("/v1/route", json={"task": "Format JSON", "criticality": "low"})
        assert resp.status_code == 200
