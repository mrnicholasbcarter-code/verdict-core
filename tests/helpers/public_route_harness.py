"""Real FastAPI app + real IntelligenceService for several routes, with no network.

Generalises ``tests/helpers/live_execution_path.py`` (one route) to a small set
of routes so budget, shortlist and invariance behaviour can be observed through
``POST /v1/route``. Only external reads are replaced: the OmniRoute admit
snapshot, the gateway inventory/connections reads, OmniRoute health and the
probe factory. Admission, the eligibility ladder, the candidate pool,
confirmation and the optimizer all run for real.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.free_tier_admit import snapshot_from_payloads
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
    SoftScores,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig

PROVIDER = "kr"
ROUTES: tuple[str, ...] = ("kr/claude-sonnet-5", "kr/claude-haiku-5", "kr/claude-opus-5")
GATEWAY = "http://127.0.0.1:20128"


def _field(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value,
        provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at="2026-11-17T00:00:00Z"),
    )


def _metadata(routes: Sequence[str]) -> MetadataSnapshot:
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at="2026-11-17T00:00:00Z",
        sources={},
        records=tuple(
            ModelMetadataRecord(
                id=route,
                provider=PROVIDER,
                omniroute_ids=(route,),
                caps=CapabilityCaps(tools=_field(True), context=_field(200_000)),
                scores=SoftScores(aa_coding=_field(90.0 - index)),
            )
            for index, route in enumerate(routes)
        ),
    )


def _snapshot(routes: Sequence[str]) -> Any:
    return snapshot_from_payloads(
        catalog={"data": [{"id": route, "owned_by": PROVIDER} for route in routes]},
        free_tier={
            "perModel": [
                {"modelId": route.split("/", 1)[1], "provider": PROVIDER, "freeType": "keyless"}
                for route in routes
            ]
        },
        providers={
            "connections": [{"provider": PROVIDER, "isActive": True, "testStatus": "active"}]
        },
    )


def _passport(route: str) -> ModelPassport:
    now = datetime.now(timezone.utc)
    return ModelPassport(
        provider=PROVIDER,
        model_id=route,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=now - timedelta(minutes=1),
        last_verified_timestamp=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=10),
        latency_p95=500.0,
        tool_support=True,
    )


def _confirm_transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
    return {
        "status_code": 200,
        "body": {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    }


def candidate(route: str, **overrides: Any) -> dict[str, Any]:
    """One public execution-path candidate for ``route`` (eligible, fresh, ready)."""
    body: dict[str, Any] = {
        "route_id": route,
        "gateway": "omniroute",
        "provider": PROVIDER,
        "model": route.split("/", 1)[1],
        "certification_state": "ready",
        "certification_freshness": "fresh",
        "eligible": True,
    }
    body.update(overrides)
    return body


def priced(usd_per_mtok: str) -> dict[str, Any]:
    """Caller price evidence: the same rate for input and output tokens."""
    return {
        "input_usd_per_mtok": usd_per_mtok,
        "output_usd_per_mtok": usd_per_mtok,
        "evidence_id": f"test-price-{usd_per_mtok}",
    }


def route_body(
    candidates: Sequence[dict[str, Any]], *, budget: Any = None, omit_budget: bool = True
) -> dict[str, Any]:
    """The public ``POST /v1/route`` JSON shape. ``omit_budget`` drops the key."""
    ep: dict[str, Any] = {
        "schema_version": "1",
        "trajectory_id": "traj-budget",
        "slice_id": "slice-budget",
        "acceptance_criteria": ["tests pass"],
        "proof_criteria": ["pytest green"],
        "candidates": list(candidates),
    }
    if not omit_budget:
        ep["hard_spend_budget"] = budget
    return {
        "task": "fix the failing unit test",
        "criticality": "low",
        "spend_policy": "free_only",
        "tools_required": True,
        "execution_path_request": ep,
    }


def install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, routes: Sequence[str] = ROUTES
) -> IntelligenceService:
    """Fake only external reads; return the real service the app will use."""
    import verdict.api as api
    import verdict.intelligence as intelligence
    import verdict.omniroute_health as omniroute_health
    import verdict.orchestration.run as orch_run
    import verdict.subagent_selection as selection
    from verdict.actions.verified_models import PublicModelsUnavailableError
    from verdict.admission import RuntimeEvidence
    from verdict.orchestration.health_cache import HealthCache, ProbeResult

    def no_socket(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"this test must not open a connection: {args!r}")

    # TestClient is in-process and opens no socket; any real gateway read would.
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    cache = HealthCache(tmp_path / "health-cache.json")
    for route in routes:
        cache.record(
            route,
            ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=True),
            datetime.now(timezone.utc) - timedelta(seconds=30),
        )
    cache.save()
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    snapshot = _snapshot(routes)
    monkeypatch.setattr(intelligence, "load_omniroute_admit_snapshot", lambda *a, **k: snapshot)
    monkeypatch.setattr(
        orch_run,
        "fetch_inventory",
        lambda *a, **k: [
            {
                "id": route,
                "object": "model",
                "context_length": 200_000,
                "owned_by": PROVIDER,
                "capabilities": {"tool_calling": True},
            }
            for route in routes
        ],
    )
    monkeypatch.setattr(
        orch_run,
        "fetch_connections",
        lambda *a, **k: [
            {
                "id": "conn-kr",
                "name": PROVIDER,
                "provider": PROVIDER,
                "status": "active",
                "isActive": True,
            }
        ],
    )
    monkeypatch.setattr(
        omniroute_health,
        "load_omniroute_health",
        lambda *a, **k: RuntimeEvidence((), ("omniroute_health:absent",)),
    )
    monkeypatch.setattr(
        selection,
        "openai_health_probe",
        lambda *a, **k: lambda candidate: selection.HealthResult(False, "not_probed"),
    )
    service = IntelligenceService(
        primary_model=routes[0],
        providers={"omniroute": ProviderConfig(base_url=GATEWAY + "/v1")},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        metadata_snapshot=_metadata(routes),
        passports={route: _passport(route) for route in routes},
        confirm_transport=_confirm_transport,
        require_execution_path_authority=True,
        context_roots=(),
        mcp_root="",
        persist_routing_receipts=False,
    )
    monkeypatch.setattr(api, "_build_intelligence", lambda: service)
    monkeypatch.setattr(api, "_build_availability_cache", lambda: None)

    def no_public_models(**_: object) -> None:
        raise PublicModelsUnavailableError("inventory_absent")

    monkeypatch.setattr(api, "load_public_models", no_public_models)
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    return service


class Spy:
    """Records the real optimizer calls and pool inputs; never alters results."""

    def __init__(self) -> None:
        self.optimizer: list[tuple[Any, Any]] = []
        self.pool_inputs: list[tuple[str, ...]] = []
        self.snapshot_loads = 0


def install_spy(monkeypatch: pytest.MonkeyPatch, service: IntelligenceService) -> Spy:
    """Wrap the optimizer, candidate-pool and admit-snapshot reads with recorders."""
    from verdict import serve_path

    spy = Spy()
    real_optimize = serve_path.optimize_execution_path
    real_pool = service._apply_candidate_pool
    real_load = service._load_admit_snapshot

    def optimize(request: Any) -> Any:
        decision = real_optimize(request)
        spy.optimizer.append((request, decision))
        return decision

    def pool(receipt: Any, **kwargs: Any) -> Any:
        spy.pool_inputs.append(tuple(receipt.admitted))
        return real_pool(receipt, **kwargs)

    def load() -> Any:
        spy.snapshot_loads += 1
        return real_load()

    monkeypatch.setattr(serve_path, "optimize_execution_path", optimize)
    monkeypatch.setattr(service, "_apply_candidate_pool", pool)
    monkeypatch.setattr(service, "_load_admit_snapshot", load)
    return spy


def post_raw(test_client: Any, body: dict[str, Any], budget_literal: str, query: str = "") -> Any:
    """POST ``body`` with ``hard_spend_budget`` set to a raw JSON literal.

    Raw text is needed for ``NaN``, ``Infinity`` and ``1e400``, which a JSON
    encoder either refuses or normalises before the server can see them.
    """
    import json

    ep = dict(body["execution_path_request"])
    ep["hard_spend_budget"] = "__BUDGET__"
    text = json.dumps({**body, "execution_path_request": ep}).replace(
        '"__BUDGET__"', budget_literal
    )
    return test_client.post(
        "/v1/route" + query, content=text.encode(), headers={"content-type": "application/json"}
    )


@contextmanager
def client() -> Iterator[Any]:
    from fastapi.testclient import TestClient

    import verdict.api as api

    with TestClient(api.app, raise_server_exceptions=False) as test_client:
        yield test_client
