"""Fixtures for free-tier ∩ active-provider live admit."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from verdict.context_pack import ContextPackSlot
from verdict.free_tier_admit import (
    FAIL_CLOSED_REASON,
    NO_ELIGIBLE_TARGET,
    REASON_INACTIVE_UNCONNECTED,
    REASON_METADATA_GHOST,
    REASON_NOT_FREE_TIER,
    REASON_OPAQUE_AUTO,
    admit_free_tier_active,
    build_cheap_path_context_pack,
    execute_offload_chat,
    load_omniroute_admit_snapshot,
    snapshot_from_payloads,
)
from verdict.intelligence import IntelligenceService
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig


def _snapshot(
    *,
    catalog: list[dict[str, object]],
    free_tier: list[dict[str, object]],
    providers: list[dict[str, object]],
):
    return snapshot_from_payloads(
        catalog={"data": catalog},
        free_tier={"perModel": free_tier},
        providers={"connections": providers, "total": len(providers)},
    )


def _catalog_row(identity_id: str, owned_by: str | None = None) -> dict[str, object]:
    return {
        "id": identity_id,
        "owned_by": owned_by or identity_id.split("/", 1)[0],
        "object": "model",
    }


def test_free_intersect_active_wins_concrete_identity() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free"),
            _catalog_row("openrouter/auto"),
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
            _catalog_row("oc/hy3-free", "opencode"),
            _catalog_row("opencode/hy3-free"),
        ],
        free_tier=[
            {
                "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            },
            {"modelId": "hy3-free", "provider": "opencode", "freeType": "keyless"},
            {"modelId": "auto", "provider": "openrouter", "freeType": "recurring-uncapped"},
        ],
        providers=[
            {"provider": "openrouter", "isActive": True, "testStatus": "active"},
            {"provider": "opencode", "isActive": True, "testStatus": "active"},
            {"provider": "claude", "isActive": True, "testStatus": "active"},
        ],
    )
    receipt = admit_free_tier_active(snapshot)
    assert "openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in receipt.admitted
    assert "opencode/hy3-free" in receipt.admitted
    assert "openrouter/auto" not in receipt.admitted
    assert "anthropic/claude-3-opus-20240229" not in receipt.admitted
    assert receipt.chosen == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert receipt.empty_intersection is False
    opaque = [drop for drop in receipt.exclusions if drop.reason == REASON_OPAQUE_AUTO]
    assert any(drop.model_id.endswith("/auto") for drop in opaque)


def test_metadata_ghosts_and_inactive_providers_are_named_drops() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/poolside/laguna-s-2.1:free"),
            _catalog_row("mistral/mistral-large-latest"),
        ],
        free_tier=[
            {
                "modelId": "poolside/laguna-s-2.1:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            },
            {
                "modelId": "ghost-model-not-in-catalog",
                "provider": "openrouter",
                "freeType": "keyless",
            },
            {
                "modelId": "mistral-large-latest",
                "provider": "mistral",
                "freeType": "recurring-monthly",
            },
            {"modelId": "auto", "provider": "openrouter"},
        ],
        providers=[
            {"provider": "openrouter", "isActive": True, "testStatus": "active"},
            {"provider": "mistral", "isActive": False, "testStatus": "active"},
        ],
    )
    receipt = admit_free_tier_active(snapshot)
    reasons = {drop.model_id: drop.reason for drop in receipt.exclusions}
    assert reasons["openrouter/ghost-model-not-in-catalog"] == REASON_METADATA_GHOST
    assert reasons["mistral/mistral-large-latest"] == REASON_INACTIVE_UNCONNECTED
    assert reasons["openrouter/auto"] == REASON_OPAQUE_AUTO
    assert receipt.admitted == ("openrouter/poolside/laguna-s-2.1:free",)
    assert receipt.chosen == "openrouter/poolside/laguna-s-2.1:free"
    assert "mistral/mistral-large-latest" not in receipt.admitted


def test_empty_intersection_fails_closed() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
            _catalog_row("openrouter/auto"),
        ],
        free_tier=[
            {"modelId": "auto", "provider": "openrouter"},
            {
                "modelId": "mistral-large-latest",
                "provider": "mistral",
                "freeType": "recurring-monthly",
            },
            {"modelId": "ghost", "provider": "openrouter", "freeType": "keyless"},
        ],
        providers=[
            {"provider": "openrouter", "isActive": False, "testStatus": "active"},
            {"provider": "mistral", "isActive": False, "testStatus": "active"},
            {"provider": "claude", "isActive": True, "testStatus": "active"},
        ],
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ()
    assert receipt.chosen is None
    assert receipt.empty_intersection is True
    named = {drop.reason for drop in receipt.exclusions}
    assert REASON_OPAQUE_AUTO in named
    assert REASON_INACTIVE_UNCONNECTED in named
    assert REASON_METADATA_GHOST in named or REASON_INACTIVE_UNCONNECTED in named


def test_discontinued_free_tier_is_not_free_tier_drop() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("opencode/old-free")],
        free_tier=[{"modelId": "old-free", "provider": "opencode", "freeType": "discontinued"}],
        providers=[{"provider": "opencode", "isActive": True, "testStatus": "active"}],
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.empty_intersection is True
    assert any(drop.reason == REASON_NOT_FREE_TIER for drop in receipt.exclusions)


def test_catalog_free_suffix_on_active_free_provider_is_admitted() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/google/gemma-4-31b-it:free"),
            _catalog_row("openrouter/google/lyria-3-pro-preview"),
        ],
        free_tier=[{"modelId": "stealth/ox-alpha", "provider": "openrouter"}],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    receipt = admit_free_tier_active(snapshot)
    assert "openrouter/google/gemma-4-31b-it:free" in receipt.admitted
    assert "openrouter/google/lyria-3-pro-preview" not in receipt.admitted
    assert receipt.chosen == "openrouter/google/gemma-4-31b-it:free"


def _fresh_passport(identity_id: str) -> ModelPassport:
    now = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)
    qualified = now - timedelta(minutes=1)
    return ModelPassport(
        provider=identity_id.split("/", 1)[0],
        model_id=identity_id,
        auth_state="authorized",
        availability_state="eligible",
        qualified_at=qualified,
        last_verified_timestamp=qualified,
        expires_at=now + timedelta(minutes=10),
    )


def _ok_confirm_transport(fail: set[str] | None = None):
    failed = fail or set()

    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        if model_id in failed:
            return {"status_code": 500, "body": {"error": {"message": "boom"}}}
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def _service(
    snapshot, executor=None, *, passports=None, confirm_transport=None
) -> IntelligenceService:
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus-20240229",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        offload_executor=executor,
        execute_offload=executor is not None,
        ruflo_command="nonexistent_ruflo",
        passports=passports,
        confirm_transport=confirm_transport,
        admit_now=datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
    )


def test_intelligence_offload_selects_free_active_not_opus() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free"),
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
        ],
        free_tier=[
            {
                "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            }
        ],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    calls: list[tuple[str, str]] = []

    def executor(model_id: str, task: str) -> tuple[str, str]:
        calls.append((model_id, task))
        return "sent", "ok from offload"

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    svc = _service(
        snapshot,
        executor=executor,
        passports={identity: _fresh_passport(identity)},
        confirm_transport=_ok_confirm_transport(),
    )
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert decision.model == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert decision.decision == "selected"
    assert decision.transport_outcome == "sent"
    assert decision.model != "anthropic/claude-3-opus-20240229"
    assert decision.admit_receipt is not None
    assert decision.admit_receipt["chosen"] == decision.model
    assert calls[0][0] == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert "summarize this paragraph" in calls[0][1]
    assert calls[0][1] != "summarize this paragraph"
    assert decision.admit_receipt["pack_digest"]
    assert decision.admit_receipt["pack_digest"].startswith("sha256:")
    assert isinstance(decision.admit_receipt["omissions"], list)
    assert decision.admit_receipt["passport"]
    assert any(row["fresh"] for row in decision.admit_receipt["passport"])
    assert decision.admit_receipt["confirm"]
    assert any(row["confirmed"] for row in decision.admit_receipt["confirm"])
    assert decision.admit_receipt["selected_because"]
    assert str(decision.admit_receipt["selected_because"]).startswith("selected because")
    assert "chooser_ranked_admitted" in decision.safety_flags


def test_intelligence_no_passport_fail_closed_not_opus() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free"),
            _catalog_row("anthropic/claude-3-opus-20240229", "claude"),
        ],
        free_tier=[
            {
                "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            }
        ],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    svc = _service(snapshot, passports={}, confirm_transport=_ok_confirm_transport())
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert decision.model == NO_ELIGIBLE_TARGET
    assert decision.model != "anthropic/claude-3-opus-20240229"
    assert decision.decision == "denied"
    assert decision.reason == FAIL_CLOSED_REASON
    assert decision.admit_receipt is not None
    named = {item["reason"] for item in decision.admit_receipt["exclusions"]}
    assert "no_passport" in named


def test_intelligence_empty_intersection_does_not_select_primary() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("anthropic/claude-3-opus-20240229", "claude")],
        free_tier=[{"modelId": "ghost", "provider": "mistral", "freeType": "keyless"}],
        providers=[{"provider": "mistral", "isActive": False, "testStatus": "active"}],
    )
    svc = _service(snapshot)
    decision = asyncio.run(svc.route("format docs", criticality="low"))
    assert decision.model == NO_ELIGIBLE_TARGET
    assert decision.model != "anthropic/claude-3-opus-20240229"
    assert decision.decision == "denied"
    assert decision.reason == FAIL_CLOSED_REASON
    assert decision.transport_outcome == "not_sent"
    assert decision.admit_receipt is not None
    assert decision.admit_receipt["empty_intersection"] is True
    assert decision.admit_receipt["pack_digest"] is None
    assert decision.admit_receipt["omissions"] == []
    assert decision.admit_receipt["chosen"] is None
    named = {item["reason"] for item in decision.admit_receipt["exclusions"]}
    assert REASON_INACTIVE_UNCONNECTED in named


def test_load_snapshot_and_execute_use_omniroute_paths() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            return httpx.Response(
                200, json={"data": [_catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free")]}
            )
        if request.url.path == "/api/free-tier/summary":
            return httpx.Response(
                200,
                json={
                    "perModel": [
                        {"modelId": "nvidia/nemotron-3-nano-30b-a3b:free", "provider": "openrouter"}
                    ]
                },
            )
        if request.url.path == "/api/providers":
            return httpx.Response(
                200,
                json={
                    "connections": [
                        {"provider": "openrouter", "isActive": True, "testStatus": "active"}
                    ],
                    "total": 1,
                },
            )
        if request.url.path == "/v1/chat/completions":
            assert request.headers.get("authorization") == "Bearer test-token"
            body = request.read()
            assert b"openrouter/nvidia/nemotron-3-nano-30b-a3b:free" in body
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "hello from omniroute"}}]}
            )
        raise AssertionError(request.url.path)

    transport = httpx.MockTransport(handler)
    snapshot = load_omniroute_admit_snapshot(
        "http://127.0.0.1:20128", "test-token", transport=transport
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.chosen == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    outcome, preview = execute_offload_chat(
        "http://127.0.0.1:20128/v1",
        receipt.chosen,
        "ping",
        api_key="test-token",
        transport=transport,
    )
    assert outcome == "sent"
    assert preview == "hello from omniroute"
    assert "/v1/models" in seen
    assert "/api/free-tier/summary" in seen
    assert "/api/providers" in seen
    assert "/v1/chat/completions" in seen


def test_parsers_accept_live_omniroute_shapes() -> None:
    snapshot = snapshot_from_payloads(
        catalog={
            "object": "list",
            "data": [
                {
                    "id": "openrouter/poolside/laguna-s-2.1:free",
                    "owned_by": "openrouter",
                    "object": "model",
                }
            ],
        },
        free_tier={
            "headline": "documented free tokens",
            "modelCount": 1,
            "perModel": [
                {
                    "modelId": "poolside/laguna-s-2.1:free",
                    "provider": "openrouter",
                    "displayName": "Laguna",
                    "freeType": "recurring-daily",
                    "poolKey": "openrouter",
                }
            ],
            "uncappedProviders": ["agnes"],
        },
        providers={
            "total": 1,
            "connections": [
                {
                    "id": "abc",
                    "name": "OpenRouter",
                    "provider": "openrouter",
                    "isActive": True,
                    "testStatus": "active",
                    "authType": "apikey",
                }
            ],
        },
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.chosen == "openrouter/poolside/laguna-s-2.1:free"


def test_critical_work_still_never_offloads() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free")],
        free_tier=[{"modelId": "nvidia/nemotron-3-nano-30b-a3b:free", "provider": "openrouter"}],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    svc = _service(snapshot)
    decision = asyncio.run(svc.route("deploy production infrastructure", criticality="critical"))
    assert decision.model == "anthropic/claude-3-opus-20240229"
    assert decision.protected is True
    assert "never offload" in decision.reason


def test_cli_route_gate_merges_omniroute_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.cli import _build_route_gate

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    gate = _build_route_gate(allow_offline=True)
    assert "omniroute" in gate.providers
    assert gate.providers["omniroute"].base_url.endswith("/v1")
    assert gate.providers["omniroute"].api_key_env == "OMNIROUTE_API_KEY"


def test_cheap_path_context_pack_digest_is_stable() -> None:
    first = build_cheap_path_context_pack(
        "format a bullet list", candidate_id="openrouter/free-model"
    )
    second = build_cheap_path_context_pack(
        "format a bullet list", candidate_id="openrouter/free-model"
    )
    assert first.pack_digest == second.pack_digest
    assert first.pack_digest.startswith("sha256:")
    assert "format a bullet list" in first.compiled_prompt
    assert first.omissions == ()


def test_cheap_path_context_pack_records_named_omissions() -> None:
    noise = ContextPackSlot(
        slot_type="evidence", key="noise", content="N" * 5000, source="fixture", created_at=0.0
    )
    packed = build_cheap_path_context_pack(
        "keep the task", candidate_id="openrouter/free-model", token_budget=40, extra_slots=[noise]
    )
    assert "keep the task" in packed.compiled_prompt
    assert packed.omissions
    named = {item.name: item.reason for item in packed.omissions}
    assert any("noise" in name for name in named)


def test_intelligence_receipt_carries_pack_digest_and_packed_execute() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("openrouter/nvidia/nemotron-3-nano-30b-a3b:free")],
        free_tier=[
            {
                "modelId": "nvidia/nemotron-3-nano-30b-a3b:free",
                "provider": "openrouter",
                "freeType": "recurring-daily",
            }
        ],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    calls: list[tuple[str, str]] = []

    def executor(model_id: str, task: str) -> tuple[str, str]:
        calls.append((model_id, task))
        return "sent", "packed-ok"

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    svc = _service(
        snapshot,
        executor=executor,
        passports={identity: _fresh_passport(identity)},
        confirm_transport=_ok_confirm_transport(),
    )
    decision = asyncio.run(svc.route("cheap path pack me", criticality="low"))
    assert decision.decision == "selected"
    assert decision.admit_receipt is not None
    digest = decision.admit_receipt["pack_digest"]
    assert isinstance(digest, str) and digest.startswith("sha256:")
    assert decision.admit_receipt["omissions"] == []
    assert calls and "cheap path pack me" in calls[0][1]
    expected = build_cheap_path_context_pack("cheap path pack me", candidate_id=decision.model)
    assert digest == expected.pack_digest
    assert calls[0][1] == expected.compiled_prompt
