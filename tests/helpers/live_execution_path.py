"""A real IntelligenceService on the live execution-path route branch, with no network.

Only the external reads are replaced: the OmniRoute admit snapshot fetch, the
gateway inventory and connections reads, OmniRoute health, and the one-token
probe factory. Admission, the eligibility ladder, the health cache, the
candidate pool, confirmation and the optimizer all run for real. Because the
service has no ``admit_snapshot`` and a configured OmniRoute endpoint, it takes
the same branch as ``verdict serve``: ``build_selector`` then
``selector._assess_all``, which is where the live HTTP 500 came from
(``HealthCacheError: now must be a timezone-aware datetime``).
"""

from __future__ import annotations

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

ROUTE = "kr/claude-sonnet-5"
GATEWAY = "http://127.0.0.1:20128"


def _field(value: bool | int | float) -> ProvenancedField:
    return ProvenancedField(
        value=value,
        provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at="2026-11-17T00:00:00Z"),
    )


def _metadata() -> MetadataSnapshot:
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at="2026-11-17T00:00:00Z",
        sources={},
        records=(
            ModelMetadataRecord(
                id=ROUTE,
                provider="kr",
                omniroute_ids=(ROUTE,),
                caps=CapabilityCaps(tools=_field(True), context=_field(200_000)),
                scores=SoftScores(aa_coding=_field(90.0)),
            ),
        ),
    )


def _snapshot() -> Any:
    return snapshot_from_payloads(
        catalog={"data": [{"id": ROUTE, "owned_by": "kr"}]},
        free_tier={
            "perModel": [{"modelId": "claude-sonnet-5", "provider": "kr", "freeType": "keyless"}]
        },
        providers={"connections": [{"provider": "kr", "isActive": True, "testStatus": "active"}]},
    )


def _passport() -> ModelPassport:
    now = datetime.now(timezone.utc)
    return ModelPassport(
        provider="kr",
        model_id=ROUTE,
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


def public_request_body() -> dict[str, Any]:
    """The public ``POST /v1/route`` JSON shape, with no caller-supplied time."""
    return {
        "task": "fix the failing unit test",
        "criticality": "low",
        "spend_policy": "free_only",
        "tools_required": True,
        "execution_path_request": {
            "schema_version": "1",
            "trajectory_id": "traj-aware-clock",
            "slice_id": "slice-aware-clock",
            "acceptance_criteria": ["tests pass"],
            "proof_criteria": ["pytest green"],
            "candidates": [
                {
                    "route_id": ROUTE,
                    "gateway": "omniroute",
                    "provider": "kr",
                    "model": "claude-sonnet-5",
                    "certification_state": "ready",
                    "certification_freshness": "fresh",
                    "eligible": True,
                    "is_free": True,
                }
            ],
        },
    }


def install_live_fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Replace only the external reads; point every state file at ``tmp_path``."""
    import verdict.intelligence as intelligence
    import verdict.omniroute_health as omniroute_health
    import verdict.orchestration.run as orch_run
    import verdict.subagent_selection as selection
    from verdict.admission import RuntimeEvidence
    from verdict.orchestration.health_cache import HealthCache, ProbeResult

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "health-cache.json"))
    # A fresh agentic pass is what lets the ladder keep the route; it also makes
    # the ladder read this cache through HealthCache.lookup(route, now).
    cache = HealthCache(tmp_path / "health-cache.json")
    cache.record(
        ROUTE,
        ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=True),
        datetime.now(timezone.utc) - timedelta(seconds=30),
    )
    cache.save()
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setattr(intelligence, "load_omniroute_admit_snapshot", lambda *a, **k: _snapshot())
    monkeypatch.setattr(
        orch_run,
        "fetch_inventory",
        lambda *a, **k: [
            {
                "id": ROUTE,
                "object": "model",
                "context_length": 200_000,
                "owned_by": "kr",
                "capabilities": {"tool_calling": True},
            }
        ],
    )
    monkeypatch.setattr(
        orch_run,
        "fetch_connections",
        lambda *a, **k: [
            {"id": "conn-kr", "name": "kr", "provider": "kr", "status": "active", "isActive": True}
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


def live_service(admit_now: datetime | None = None) -> IntelligenceService:
    """The serve-path service shape: live OmniRoute endpoint, no fixture snapshot."""
    return IntelligenceService(
        primary_model="kr/claude-sonnet-5",
        providers={"omniroute": ProviderConfig(base_url=GATEWAY + "/v1")},
        profile="production",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        metadata_snapshot=_metadata(),
        passports={ROUTE: _passport()},
        confirm_transport=_confirm_transport,
        admit_now=admit_now,
        require_execution_path_authority=True,
        context_roots=(),
        mcp_root="",
        persist_routing_receipts=False,
    )
