from typing import ClassVar

from fastapi.testclient import TestClient

import llm_gate.api as api
from llm_gate.intelligence import ReadinessReport
from llm_gate.models import RoutingDecision


class FakeProxy:
    base_url = "http://fake-upstream/v1"

    async def models(self):
        return type("Resp", (), {"status_code": 200})()

    async def chat(self, payload):
        raise AssertionError("chat should not be called in this integration test")


class FakeIntelligence:
    primary_model = "primary-model"
    providers: ClassVar[dict[str, object]] = {}
    log_path = ""
    log_full_task = False
    discovery_ttl = 60
    profile = "production"

    def readiness(self) -> ReadinessReport:
        return ReadinessReport(
            status="ready",
            production_ready=True,
            profile="production",
            managed_backend_status="healthy",
            degraded_mode=False,
            policy_version="policy-2026-07-13.1",
            reason="ready",
            adapter_versions={"ruflo": "ruflo", "ruvector": "ruvector"},
        )

    def route(
        self, task: str, criticality: str = "medium", context: dict[str, object] | None = None
    ) -> RoutingDecision:
        if criticality == "critical":
            return RoutingDecision(
                model="primary-model",
                provider="primary",
                tier=0,
                reason="managed intelligence unavailable in production profile",
                decision="denied",
                request_id="route-critical",
                managed_backend_status="unavailable",
            )
        return RoutingDecision(
            model="primary-model",
            provider="primary",
            tier=2,
            reason="test fallback",
            decision="fallback",
            request_id="route-low",
            managed_backend_status="healthy",
        )


def test_live_gateway_integration(monkeypatch):
    monkeypatch.setattr(api, "_build_intelligence", lambda: FakeIntelligence())
    monkeypatch.setattr(api, "_build_proxy", lambda: FakeProxy())
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")

    with TestClient(api.app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

        resp = client.post(
            "/v1/route", json={"task": "Deploy production", "criticality": "critical"}
        )
        assert resp.status_code == 503
        data = resp.json()
        assert data["decision"] == "denied"
        assert data["tier"] == 0
        assert "managed intelligence unavailable" in data["reason"]

        resp = client.post("/v1/route", json={"task": "Format JSON", "criticality": "low"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "primary-model"
        assert data["decision"] == "fallback"
