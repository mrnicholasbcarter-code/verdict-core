"""BOD-107 / BOD-100 / BOD-109 spend-preservation gates on the cheap path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from verdict.capability_gate import derive_requirements, gate_capability
from verdict.free_tier_admit import (
    admit_free_tier_active,
    expand_admit_for_worthiness,
    snapshot_from_payloads,
)
from verdict.intelligence import IntelligenceService
from verdict.metadata.records import (
    SOURCE_MODELS_DEV,
    CapabilityCaps,
    FieldProvenance,
    ModelMetadataRecord,
    ProvenancedField,
)
from verdict.metadata.store import MetadataSnapshot
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig
from verdict.worthiness import ORDINARY, WORTHY, classify_worthiness

NOW = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)
FETCHED = "2026-09-18T18:00:00Z"
FREE_NO_TOOLS = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
PAID_TOOLS = "groq/llama-3.3-70b-versatile"
FRONTIER = "anthropic/claude-3-opus-20240229"
PROVEN_FREE = "opencode/hy3-free"


def _prov(value: bool | int) -> ProvenancedField:
    return ProvenancedField(
        value=value, provenance=FieldProvenance(source=SOURCE_MODELS_DEV, fetched_at=FETCHED)
    )


def _snapshot_store() -> MetadataSnapshot:
    return MetadataSnapshot(
        schema_version="1",
        refreshed_at=FETCHED,
        sources={},
        records=(
            ModelMetadataRecord(
                id=FREE_NO_TOOLS,
                caps=CapabilityCaps(tools=_prov(False), vision=_prov(False)),
                omniroute_ids=(FREE_NO_TOOLS,),
            ),
            ModelMetadataRecord(
                id=PAID_TOOLS,
                caps=CapabilityCaps(tools=_prov(True), vision=_prov(False), context=_prov(131072)),
                omniroute_ids=(PAID_TOOLS,),
            ),
            ModelMetadataRecord(
                id=FRONTIER,
                caps=CapabilityCaps(tools=_prov(True), vision=_prov(True), context=_prov(200000)),
                omniroute_ids=(FRONTIER,),
            ),
            ModelMetadataRecord(
                id=PROVEN_FREE,
                caps=CapabilityCaps(tools=_prov(True), vision=_prov(False)),
                omniroute_ids=(PROVEN_FREE,),
            ),
        ),
    )


def _live_snapshot(*, include_paid: bool = True, include_free: bool = True):
    catalog = []
    free_tier = []
    connections = [
        {"provider": "openrouter", "isActive": True, "testStatus": "active"},
        {"provider": "opencode", "isActive": True, "testStatus": "active"},
        {"provider": "groq", "isActive": True, "testStatus": "active"},
        {"provider": "anthropic", "isActive": True, "testStatus": "active"},
    ]
    if include_free:
        catalog.append({"id": FREE_NO_TOOLS, "owned_by": "openrouter"})
        catalog.append({"id": PROVEN_FREE, "owned_by": "opencode"})
        free_tier.extend(
            [
                {
                    "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                    "provider": "openrouter",
                    "freeType": "recurring-daily",
                },
                {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            ]
        )
    if include_paid:
        catalog.append({"id": PAID_TOOLS, "owned_by": "groq"})
        catalog.append({"id": FRONTIER, "owned_by": "anthropic"})
    return snapshot_from_payloads(
        catalog={"data": catalog},
        free_tier={"perModel": free_tier},
        providers={"connections": connections},
    )


def _passport(identity_id: str) -> ModelPassport:
    qualified = NOW - timedelta(minutes=1)
    return ModelPassport(
        provider=identity_id.split("/", 1)[0],
        model_id=identity_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=NOW + timedelta(minutes=10),
        latency_p95=40.0,
    )


def _ok_transport():
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def _service(snapshot, *, passports, metadata=None, **kwargs) -> IntelligenceService:
    return IntelligenceService(
        primary_model=FRONTIER,
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        execute_offload=False,
        passports=passports,
        confirm_transport=_ok_transport(),
        admit_now=NOW,
        context_roots=(),
        mcp_root="",
        metadata_snapshot=metadata if metadata is not None else _snapshot_store(),
        **kwargs,
    )


def test_architecture_security_task_is_worthy() -> None:
    result = classify_worthiness(
        "Design a distributed authentication architecture and security threat model"
    )
    assert result.task_class == WORTHY
    assert result.class_reasons
    assert any(
        "architecture" in item or "security" in item or "escalation" in item
        for item in result.class_reasons
    )


def test_ordinary_coding_is_ordinary() -> None:
    result = classify_worthiness("summarize this paragraph")
    assert result.task_class == ORDINARY
    assert result.class_reasons


def test_worthy_selects_frontier_not_free() -> None:
    snapshot = _live_snapshot()
    passports = {
        FREE_NO_TOOLS: _passport(FREE_NO_TOOLS),
        PROVEN_FREE: _passport(PROVEN_FREE),
        PAID_TOOLS: _passport(PAID_TOOLS),
        FRONTIER: _passport(FRONTIER),
    }
    svc = _service(snapshot, passports=passports)
    decision = asyncio.run(
        svc.route(
            "Design a distributed authentication architecture and security threat model",
            criticality="high",
        )
    )
    assert decision.task_class == WORTHY
    assert decision.model == FRONTIER
    assert decision.model != FREE_NO_TOOLS
    assert decision.admit_receipt is not None
    assert decision.admit_receipt["task_class"] == WORTHY
    assert decision.admit_receipt["class_reasons"]
    named = {item["reason"] for item in decision.admit_receipt["exclusions"]}
    assert "worthy_excludes_free_for_cost" in named
    assert "chooser_ranked_admitted" in decision.safety_flags
    assert decision.degraded_mode is False


def test_ordinary_prefers_free_when_available() -> None:
    snapshot = _live_snapshot()
    passports = {
        PROVEN_FREE: _passport(PROVEN_FREE),
        PAID_TOOLS: _passport(PAID_TOOLS),
        FRONTIER: _passport(FRONTIER),
    }
    svc = _service(snapshot, passports=passports)
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert decision.task_class == ORDINARY
    assert decision.model == PROVEN_FREE
    assert "free-first among admitted" in (decision.admit_receipt or {}).get("selected_because", "")


def test_tools_required_drops_non_tool_free_and_selects_paid() -> None:
    snapshot = _live_snapshot()
    passports = {
        FREE_NO_TOOLS: _passport(FREE_NO_TOOLS),
        PAID_TOOLS: _passport(PAID_TOOLS),
        FRONTIER: _passport(FRONTIER),
    }
    svc = _service(snapshot, passports=passports)
    decision = asyncio.run(
        svc.route(
            "call tools to implement this",
            criticality="low",
            context={"tools_required": True, "task": "call tools to implement this"},
        )
    )
    assert decision.model == PAID_TOOLS
    assert decision.model != FREE_NO_TOOLS
    receipt = decision.admit_receipt or {}
    named = {item["reason"] for item in receipt.get("exclusions", [])}
    assert "capability_mismatch" in named or "required_unknown" in named
    assert receipt.get("requirements")
    assert receipt.get("capability_matches")
    because = receipt.get("selected_because") or ""
    assert "lesser-paid fallback" in because or "paid" in because


def test_unknown_required_cap_is_named_drop() -> None:
    snapshot = _live_snapshot(include_paid=False)
    receipt = admit_free_tier_active(snapshot)
    classified = classify_worthiness("summarize this paragraph")
    receipt = expand_admit_for_worthiness(
        receipt, snapshot, task_class=classified.task_class, class_reasons=classified.class_reasons
    )
    requirements = derive_requirements("x", {"min_context": 10_000_000})
    gated = gate_capability(receipt, requirements, snapshot=_snapshot_store())
    assert gated.empty_intersection is True
    assert any(
        item.reason in {"required_unknown", "capability_mismatch"} for item in gated.exclusions
    )
    assert gated.capability_matches


def test_free_empty_paid_fallback() -> None:
    snapshot = _live_snapshot(include_free=False, include_paid=True)
    passports = {PAID_TOOLS: _passport(PAID_TOOLS), FRONTIER: _passport(FRONTIER)}
    svc = _service(snapshot, passports=passports)
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert decision.decision == "selected"
    assert decision.model in {PAID_TOOLS, FRONTIER}
    assert decision.model != FREE_NO_TOOLS
    because = (decision.admit_receipt or {}).get("selected_because") or ""
    assert "lesser-paid fallback" in because
    assert decision.task_class == ORDINARY


def test_gate_capability_unit_drops_false_tools() -> None:
    snapshot = _live_snapshot()
    receipt = admit_free_tier_active(snapshot)
    receipt = expand_admit_for_worthiness(
        receipt, snapshot, task_class=ORDINARY, class_reasons=("ordinary:summarize",)
    )
    requirements = derive_requirements("x", {"tools_required": True})
    gated = gate_capability(receipt, requirements, snapshot=_snapshot_store())
    assert FREE_NO_TOOLS not in gated.admitted
    assert PAID_TOOLS in gated.admitted
    match = next(item for item in gated.capability_matches if item["model"] == FREE_NO_TOOLS)
    assert match["admitted"] is False
    assert match["drop"]["reason"] in {"capability_mismatch", "required_unknown"}
    assert match["provenance"] or match["drop"]


def test_task_that_exceeds_pack_budget_is_denied_not_executed(tmp_path) -> None:
    """BOD-110: when the task itself does not fit the pack, deny — never send a pack without it."""
    snapshot = _live_snapshot()
    passports = {PROVEN_FREE: _passport(PROVEN_FREE), PAID_TOOLS: _passport(PAID_TOOLS)}
    calls: list[tuple[str, str]] = []

    def executor(model_id: str, packed: str) -> tuple[str, str | None]:
        calls.append((model_id, packed))
        return "success", "OK"

    svc = _service(
        snapshot, passports=passports, workspace_root=tmp_path, offload_executor=executor
    )
    huge = "summarize this paragraph. " * 20_000
    decision = asyncio.run(svc.route(huge, criticality="low"))
    assert decision.decision == "denied"
    assert decision.transport_outcome == "not_sent"
    assert "task_instructions_omitted" in decision.safety_flags
    assert calls == [], "a pack without the task must never reach an upstream"
    receipt = decision.admit_receipt or {}
    assert receipt["task_complete"] is False
    assert receipt["pack_state"] == "failed"
    assert {item["name"]: item["reason"] for item in receipt["omissions"]}[
        "urn:verdict:task"
    ] == "task_instructions_omitted"
