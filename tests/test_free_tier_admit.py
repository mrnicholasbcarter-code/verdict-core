"""Fixtures for free-tier ∩ active-provider live admit."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from verdict.catalog_cache import get_cached_catalog
from verdict.context_hydrate import DEFAULT_CONTEXT_ROOTS
from verdict.context_pack import ContextPackSlot
from verdict.free_tier_admit import (
    FAIL_CLOSED_REASON,
    NO_ELIGIBLE_TARGET,
    REASON_METADATA_GHOST,
    REASON_OPAQUE_ROUTE_DISALLOWED,
    REASON_PROVIDER_INACTIVE,
    REASON_PROVIDER_NOT_CONNECTED,
    REASON_PROVIDER_UNHEALTHY,
    LiveAdmitError,
    admit_free_tier_active,
    build_cheap_path_context_pack,
    execute_offload_chat,
    load_omniroute_admit_snapshot,
    omniroute_endpoint_from_env,
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
    assert "anthropic/claude-3-opus-20240229" in receipt.admitted
    assert receipt.chosen == "anthropic/claude-3-opus-20240229"
    assert receipt.empty_intersection is False
    opaque = [drop for drop in receipt.exclusions if drop.reason == REASON_OPAQUE_ROUTE_DISALLOWED]
    assert any(drop.model_id.endswith("/auto") for drop in opaque)


@pytest.mark.parametrize(
    "q_connection",
    [
        None,
        {"provider": "q", "isActive": False, "testStatus": "active"},
        {"provider": "q", "isActive": True, "testStatus": "error"},
    ],
    ids=["q-unconnected", "q-inactive", "q-unhealthy"],
)
def test_free_row_never_crosses_catalog_provider_binding(
    q_connection: dict[str, object] | None,
) -> None:
    providers: list[dict[str, object]] = [
        {"provider": "p", "isActive": True, "testStatus": "active"}
    ]
    if q_connection is not None:
        providers.append(q_connection)
    snapshot = _snapshot(
        catalog=[_catalog_row("q/m", "q")],
        free_tier=[{"modelId": "m", "provider": "p", "freeType": "keyless"}],
        providers=providers,
    )

    receipt = admit_free_tier_active(snapshot)

    assert receipt.admitted == ()
    assert receipt.chosen is None
    assert any(
        drop.model_id == "p/m" and drop.reason == REASON_METADATA_GHOST
        for drop in receipt.exclusions
    )


def test_same_provider_owned_alias_remains_a_legitimate_match() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("alias/m", "p")],
        free_tier=[{"modelId": "m", "provider": "p", "freeType": "keyless"}],
        providers=[{"provider": "p", "isActive": True, "testStatus": "active"}],
    )

    receipt = admit_free_tier_active(snapshot)

    assert receipt.admitted == ("alias/m",)
    assert receipt.chosen == "alias/m"


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
    assert reasons["mistral/mistral-large-latest"] == REASON_PROVIDER_INACTIVE
    assert reasons["openrouter/auto"] == REASON_OPAQUE_ROUTE_DISALLOWED
    assert receipt.admitted == ("openrouter/poolside/laguna-s-2.1:free",)
    assert receipt.chosen == "openrouter/poolside/laguna-s-2.1:free"
    assert "mistral/mistral-large-latest" not in receipt.admitted


def test_active_nonfree_inventory_is_not_an_empty_intersection() -> None:
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
    assert receipt.admitted == ("anthropic/claude-3-opus-20240229",)
    assert receipt.chosen == "anthropic/claude-3-opus-20240229"
    assert receipt.empty_intersection is False
    named = {drop.reason for drop in receipt.exclusions}
    assert REASON_OPAQUE_ROUTE_DISALLOWED in named
    assert REASON_PROVIDER_INACTIVE in named
    assert REASON_METADATA_GHOST in named or REASON_PROVIDER_INACTIVE in named


def test_discontinued_free_metadata_does_not_exclude_active_catalog_route() -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("opencode/old-free")],
        free_tier=[{"modelId": "old-free", "provider": "opencode", "freeType": "discontinued"}],
        providers=[{"provider": "opencode", "isActive": True, "testStatus": "active"}],
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ("opencode/old-free",)
    assert receipt.free_admitted == ()
    assert receipt.exclusions == ()


def test_catalog_admission_does_not_depend_on_free_metadata() -> None:
    snapshot = _snapshot(
        catalog=[
            _catalog_row("openrouter/google/gemma-4-31b-it:free"),
            _catalog_row("openrouter/google/lyria-3-pro-preview"),
        ],
        free_tier=[{"modelId": "stealth/ox-alpha", "provider": "openrouter"}],
        providers=[{"provider": "openrouter", "isActive": True, "testStatus": "active"}],
    )
    receipt = admit_free_tier_active(snapshot)
    assert set(receipt.admitted) == {
        "openrouter/google/gemma-4-31b-it:free",
        "openrouter/google/lyria-3-pro-preview",
    }
    assert receipt.chosen == "openrouter/google/gemma-4-31b-it:free"
    assert receipt.free_admitted == ()
    assert any(drop.reason == REASON_METADATA_GHOST for drop in receipt.exclusions)


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
    snapshot, executor=None, *, passports=None, confirm_transport=None, workspace_root=None
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
        passports=passports,
        confirm_transport=confirm_transport,
        admit_now=datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
        workspace_root=workspace_root,
        context_roots=() if workspace_root is None else DEFAULT_CONTEXT_ROOTS,
        mcp_root="",
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
    confirm_calls: list[str] = []
    base_confirm = _ok_confirm_transport()

    def spy_confirm(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        confirm_calls.append(model_id)
        return base_confirm(model_id, payload, timeout)

    svc = _service(
        snapshot,
        executor=executor,
        passports={identity: _fresh_passport(identity)},
        confirm_transport=spy_confirm,
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
    # A fresh stored passport is reused without confirm probes (BOD-350).
    assert decision.admit_receipt["confirm"] == []
    assert confirm_calls == []
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
    assert REASON_PROVIDER_INACTIVE in named


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
        if request.url.path == "/api/v1/me/status":
            return httpx.Response(200, json={"accountQuotas": []})
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
                200,
                json={
                    "model": "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
                    "choices": [{"message": {"content": "hello from omniroute"}}],
                },
            )
        if request.url.path == "/api/usage/model-latency-stats":
            assert request.url.params["windowHours"] == "168"
            return httpx.Response(200, json={"entries": []})
        if request.url.path == "/api/resilience/connections":
            return httpx.Response(200, json={"connections": []})
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


def test_execute_offload_chat_rejects_wrong_served_identity() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "provider/wrong-model",
                "choices": [{"message": {"content": "must not be accepted"}}],
            },
        )

    outcome, reason = execute_offload_chat(
        "http://127.0.0.1:20128/v1",
        "provider/selected-model",
        "ping",
        transport=httpx.MockTransport(handler),
    )
    assert outcome == "error"
    assert "identity mismatch" in reason


def test_execute_offload_chat_requires_completion_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "provider/selected-model", "choices": []})

    outcome, reason = execute_offload_chat(
        "http://127.0.0.1:20128/v1",
        "provider/selected-model",
        "ping",
        transport=httpx.MockTransport(handler),
    )
    assert outcome == "error"
    assert reason == "invalid provider response: missing choices"


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


def test_cheap_path_context_pack_digest_is_stable(tmp_path: Path) -> None:
    first = build_cheap_path_context_pack(
        "format a bullet list",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=(),
        mcp_root="",
    )
    second = build_cheap_path_context_pack(
        "format a bullet list",
        candidate_id="openrouter/free-model",
        workspace_root=tmp_path,
        workspace_roots=(),
        mcp_root="",
    )
    assert first.pack_digest == second.pack_digest
    assert first.pack_digest.startswith("sha256:")
    assert "format a bullet list" in first.compiled_prompt
    assert first.omissions == ()


def test_cheap_path_context_pack_records_named_omissions(tmp_path: Path) -> None:
    noise = ContextPackSlot(
        slot_type="evidence", key="noise", content="N" * 5000, source="fixture", created_at=0.0
    )
    packed = build_cheap_path_context_pack(
        "keep the task",
        candidate_id="openrouter/free-model",
        token_budget=40,
        extra_slots=[noise],
        workspace_root=tmp_path,
        workspace_roots=(),
        mcp_root="",
    )
    assert "keep the task" in packed.compiled_prompt
    assert packed.omissions
    named = {item.name: item.reason for item in packed.omissions}
    assert any("noise" in name for name in named)


def test_intelligence_receipt_carries_pack_digest_and_packed_execute(tmp_path: Path) -> None:
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
        workspace_root=tmp_path,
    )
    decision = asyncio.run(svc.route("cheap path pack me", criticality="low"))
    assert decision.decision == "selected"
    assert decision.admit_receipt is not None
    digest = decision.admit_receipt["pack_digest"]
    assert isinstance(digest, str) and digest.startswith("sha256:")
    assert calls and "cheap path pack me" in calls[0][1]
    assert hashlib.sha256(calls[0][1].encode()).hexdigest() in {
        digest.removeprefix("sha256:"),
        decision.admit_receipt["prompt_digest"].removeprefix("sha256:"),
    }
    coverage = decision.admit_receipt["capability_coverage"]
    assert "task.requirements" in coverage["requested"]
    assert "repo.state" in coverage["requested"]
    assert decision.admit_receipt["included_sources"] == decision.admit_receipt["included"]
    assert decision.admit_receipt["pack_state"] in {"hydrated", "partial"}


def test_intelligence_execute_receives_hydrated_workspace_unit(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("README_PROVENANCE_MARKER\n", encoding="utf-8")
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
        return "sent", "hydrated-ok"

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    svc = _service(
        snapshot,
        executor=executor,
        passports={identity: _fresh_passport(identity)},
        confirm_transport=_ok_confirm_transport(),
        workspace_root=tmp_path,
    )
    decision = asyncio.run(svc.route("summarize the project readme", criticality="low"))
    assert decision.decision == "selected"
    assert calls
    assert "README_PROVENANCE_MARKER" in calls[0][1]
    assert decision.admit_receipt is not None
    assert decision.admit_receipt["pack_digest"].startswith("sha256:")
    named = {item["name"]: item["reason"] for item in decision.admit_receipt["omissions"]}
    assert named.get("docs") == "source_missing"
    assert "invented" not in calls[0][1].lower()
    included = {
        item["source_uri"]: item["source_digest"] for item in decision.admit_receipt["included"]
    }
    assert "README.md" in included
    assert included["README.md"].startswith("sha256:")
    assert decision.admit_receipt["included_sources"] == decision.admit_receipt["included"]
    assert decision.admit_receipt["pack_state"] == "hydrated"


def test_omniroute_endpoint_defaults_to_local_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset config uses the local OmniRoute origin; explicit config still wins."""
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    monkeypatch.setenv("OMNIROUTE_API_KEY", "test-key")

    assert omniroute_endpoint_from_env(None) == ("http://127.0.0.1:20128", "test-key")

    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:24000")
    assert omniroute_endpoint_from_env(None) == ("http://127.0.0.1:24000", "test-key")

    provider = ProviderConfig(base_url="http://127.0.0.1:25000/v1")
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    assert omniroute_endpoint_from_env({"omniroute": provider}) == (
        "http://127.0.0.1:25000/v1",
        "test-key",
    )


@pytest.mark.parametrize(
    "quota",
    [
        {"quotas": {"weekly": {"usedPercentage": 100, "resetAt": "2099-01-01T00:00:00Z"}}},
        {"available": False, "reason": "quota_exhausted"},
    ],
)
def test_account_quota_excludes_owned_alias(quota):
    snapshot = snapshot_from_payloads(
        catalog={"data": [_catalog_row("agy/flash", "antigravity")]},
        free_tier={"perModel": [{"provider": "antigravity", "modelId": "flash"}]},
        providers={
            "connections": [{"provider": "antigravity", "isActive": True, "testStatus": "active"}]
        },
        account_status={"accountQuotas": [{"provider": "antigravity", **quota}]},
    )
    receipt = admit_free_tier_active(snapshot)
    assert not receipt.admitted
    assert any(
        d.model_id == "agy/flash" and d.reason == "quota_exhausted" for d in receipt.exclusions
    )


@pytest.mark.parametrize(
    "quota",
    [
        {"available": False, "reason": "not_supported"},
        {"quotas": {"weekly": {"usedPercentage": 100, "resetAt": "2020-01-01T00:00:00Z"}}},
        {"quotas": {"weekly": {"usedPercentage": 99}}},
    ],
)
def test_account_quota_nonblocking_evidence(quota):
    snapshot = snapshot_from_payloads(
        catalog={"data": [_catalog_row("agy/flash", "antigravity")]},
        free_tier={"perModel": [{"provider": "antigravity", "modelId": "flash"}]},
        providers={
            "connections": [{"provider": "antigravity", "isActive": True, "testStatus": "active"}]
        },
        account_status={"accountQuotas": [{"provider": "antigravity", **quota}]},
    )
    assert admit_free_tier_active(snapshot).admitted == ("agy/flash",)


@pytest.mark.parametrize(
    "failure",
    ["invalid provider response: missing completion content", "http 429", "quota_exhausted"],
)
@pytest.mark.parametrize("recover", [True, False])
def test_offload_reselects_once_and_persists_cooling(tmp_path, monkeypatch, failure, recover):
    from verdict.orchestration.health_cache import HealthCache

    cache_path = tmp_path / "health.json"
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(cache_path))
    identities = ("openrouter/mini:free", "opencode/small-free")
    snapshot = _snapshot(
        catalog=[_catalog_row(i) for i in identities],
        free_tier=[
            {"provider": i.split("/", 1)[0], "modelId": i.split("/", 1)[1]} for i in identities
        ],
        providers=[
            {"provider": p, "isActive": True, "testStatus": "active"}
            for p in ("openrouter", "opencode")
        ],
    )
    calls = []

    def executor(model, task):
        calls.append(model)
        return ("sent", "ok") if recover and len(calls) == 2 else ("error", failure)

    svc = _service(
        snapshot,
        executor=executor,
        passports={i: _fresh_passport(i) for i in identities},
        confirm_transport=_ok_confirm_transport(),
    )
    loads = []
    original_load = svc._load_admit_snapshot

    def load():
        loads.append(True)
        return original_load()

    monkeypatch.setattr(svc, "_load_admit_snapshot", load)
    decision = asyncio.run(svc.route("summarize this paragraph", criticality="low"))
    assert len(calls) == 2 and calls[0] != calls[1]
    assert len(loads) == 2
    assert HealthCache(cache_path).lookup(calls[0], svc.admit_now).state == "negative"
    assert len(decision.admit_receipt["execution_attempts"]) == 2
    assert decision.decision == ("selected" if recover else "denied")
    if not recover:
        assert decision.model == NO_ELIGIBLE_TARGET
        assert "fail_closed" in decision.reason.lower()


@pytest.mark.parametrize("available", [True, False])
def test_quota_fence_requires_all_accounts_exhausted(available):
    snapshot = snapshot_from_payloads(
        catalog={"data": [_catalog_row("cx/code", "codex")]},
        free_tier={"perModel": [{"provider": "codex", "modelId": "code"}]},
        providers={
            "connections": [{"provider": "codex", "isActive": True, "testStatus": "active"}]
        },
        account_status={
            "accountQuotas": [
                {
                    "provider": "codex",
                    "connectionId": "exhausted",
                    "quotas": {"weekly": {"usedPercentage": 100}},
                },
                {
                    "provider": "codex",
                    "connectionId": "other",
                    "available": available,
                    "quotas": {"weekly": {"usedPercentage": 11}},
                },
            ]
        },
    )
    receipt = admit_free_tier_active(snapshot)
    assert bool(receipt.admitted) is available
    assert bool(snapshot.quota_blocks) is not available


def _two_account_snapshot(other_row):
    return snapshot_from_payloads(
        catalog={"data": [_catalog_row("cx/code", "codex")]},
        free_tier={"perModel": [{"provider": "codex", "modelId": "code"}]},
        providers={
            "connections": [{"provider": "codex", "isActive": True, "testStatus": "active"}]
        },
        account_status={
            "accountQuotas": [
                {
                    "provider": "codex",
                    "connectionId": "exhausted",
                    "quotas": {
                        "weekly": {"usedPercentage": 100, "resetAt": "2099-01-01T00:00:00Z"}
                    },
                },
                {"provider": "codex", **other_row},
            ]
        },
    )


def test_quota_fence_not_cancelled_by_not_supported_account():
    snapshot = _two_account_snapshot(
        {"connectionId": "other", "available": False, "reason": "not_supported"}
    )
    receipt = admit_free_tier_active(snapshot)
    assert not receipt.admitted
    assert snapshot.quota_blocks


def test_quota_fence_not_cancelled_by_unknown_account_row():
    snapshot = _two_account_snapshot({"connectionId": "other"})
    receipt = admit_free_tier_active(snapshot)
    assert not receipt.admitted
    assert snapshot.quota_blocks


def test_quota_fence_cancelled_by_account_with_capacity():
    snapshot = _two_account_snapshot(
        {"connectionId": "other", "quotas": {"weekly": {"usedPercentage": 11}}}
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ("cx/code",)
    assert not snapshot.quota_blocks


def test_quota_fence_cancelled_by_reset_window():
    snapshot = _two_account_snapshot(
        {
            "connectionId": "other",
            "quotas": {"weekly": {"usedPercentage": 100, "resetAt": "2020-01-01T00:00:00Z"}},
        }
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ("cx/code",)
    assert not snapshot.quota_blocks


def test_quota_fence_cancelled_by_explicit_available_flag():
    snapshot = _two_account_snapshot({"connectionId": "other", "available": True})
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ("cx/code",)
    assert not snapshot.quota_blocks


def test_quota_fence_not_cancelled_by_mixed_window_account():
    snapshot = _two_account_snapshot(
        {
            "connectionId": "other",
            "quotas": {
                "weekly": {"usedPercentage": 100, "resetAt": "2099-01-01T00:00:00Z"},
                "daily": {"usedPercentage": 10},
            },
        }
    )
    receipt = admit_free_tier_active(snapshot)
    assert not receipt.admitted
    assert snapshot.quota_blocks


# ---------------------------------------------------------------------------
# BOD-345 / S04: bounded gateway inventory and scoped telemetry (A01/A03/A05)
# ---------------------------------------------------------------------------

_S04_BASE = "http://127.0.0.1:20128"

_S04_SURFACES: dict[str, object] = {
    "/v1/models": {
        "object": "list",
        "data": [
            _catalog_row("oc/claude-small", "oc"),
            _catalog_row("bare/model", "bare"),
            _catalog_row("ghostcorp/model", "ghostcorp"),
            _catalog_row("auto/rotator", "omniroute"),
            _catalog_row("unmapped/ghost-row", "unmapped"),
        ],
    },
    "/api/free-tier/summary": {
        "perModel": [
            {"modelId": "claude-small", "provider": "oc"},
            {"modelId": "model", "provider": "bare"},
            {"modelId": "model", "provider": "ghostcorp"},
            {"modelId": "auto/rotator", "provider": "omniroute"},
        ]
    },
    "/api/providers": {
        "connections": [
            {"provider": "oc", "isActive": True, "testStatus": "active"},
            # Deployed nested analytics shape: successRate=1/CLOSED and an
            # empty MCP array are unknown observations, never health evidence.
            {
                "provider": "bare",
                "isActive": True,
                "analytics": {"successRate": 1.0, "state": "CLOSED"},
                "mcp": [],
            },
            {"provider": "omniroute", "isActive": True, "testStatus": "active"},
        ],
        "total": 3,
    },
    "/api/v1/me/status": {
        "accountQuotas": [
            {
                "provider": "oc",
                "connectionId": "acct-1",
                "quotas": {"hourly": {"usedPercentage": 0.0}},
            }
        ]
    },
    "/api/usage/model-latency-stats": {"entries": []},
    "/api/resilience/connections": {"connections": []},
}


def _s04_transport(surfaces: dict[str, object]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = surfaces.get(request.url.path)
        if payload is None:
            return httpx.Response(404)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _load_s04(
    transport: httpx.BaseTransport, api_key: str, last_good: Path, **kwargs: object
) -> object:
    return load_omniroute_admit_snapshot(
        _S04_BASE,
        api_key,
        transport=transport,
        last_good_path=last_good,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_gateway_missing_telemetry_is_unknown_and_catalog_is_bounded(tmp_path: Path) -> None:
    """S04 named aggregate regression (BOD-345 acceptance A01, A03, A05).

    Missing gateway telemetry stays unknown and never healthy; an observed
    zero stays zero; the bounded catalog walk never publishes a truncated
    inventory as complete.
    """
    last_good = tmp_path / "admit-last-good.json"

    # -- A05: missing telemetry is unknown, observed zero stays zero ----------
    snapshot = _load_s04(_s04_transport(_S04_SURFACES), "telemetry-token", last_good)
    assert snapshot.fetched_at is not None  # type: ignore[attr-defined]
    assert snapshot.inventory_complete is True  # type: ignore[attr-defined]
    assert snapshot.quota_blocks == ()  # type: ignore[attr-defined]
    # usedPercentage 0 stays an observed zero: no quota fence is invented.
    assert {"analytics", "mcp", "successRate"} <= set(snapshot.unsupported_fields)  # type: ignore[attr-defined]
    statuses = {conn.provider: conn for conn in snapshot.connections}  # type: ignore[attr-defined]
    assert statuses["bare"].test_status is None  # missing telemetry stays unknown

    receipt = admit_free_tier_active(snapshot)
    # Explicit alias mapping preserves the concrete identity (A01).
    assert receipt.chosen == "oc/claude-small"
    reasons = {drop.model_id: drop.reason for drop in receipt.exclusions}
    # Absent health telemetry is unknown, never healthy (A05).
    assert reasons["bare/model"] == REASON_PROVIDER_UNHEALTHY
    # A nonexistent provider stays unknown (A05).
    assert reasons["ghostcorp/model"] == REASON_PROVIDER_NOT_CONNECTED
    # Opaque rows never become candidates through catalog membership (A01).
    assert reasons["omniroute/auto/rotator"] == REASON_OPAQUE_ROUTE_DISALLOWED
    inventory = {row.identity_id for row in snapshot.catalog}  # type: ignore[attr-defined]
    # Opaque and unmapped rows remain internally accounted for in inventory.
    assert {"auto/rotator", "unmapped/ghost-row"} <= inventory
    assert "auto/rotator" not in receipt.admitted
    assert "unmapped/ghost-row" not in receipt.admitted

    # -- A03: a repeated cursor never publishes a truncated catalog ----------
    cursors: list[str | None] = []

    def paging_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/v1/models":
            payload = _S04_SURFACES.get(path)
            return httpx.Response(404) if payload is None else httpx.Response(200, json=payload)
        after = request.url.params.get("after")
        cursors.append(after)
        if after is None:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_catalog_row("oc/claude-small", "oc")],
                    "next_cursor": "cursor-2",
                },
            )
        # New rows but the same cursor again: an inconsistent page walk.
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [_catalog_row("zz/other", "zz")],
                "next_cursor": "cursor-2",
            },
        )

    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(httpx.MockTransport(paging_handler), "cursor-token", last_good)
    assert caught.value.code == "incomplete_source"
    assert "repeated pagination cursor" in str(caught.value)
    assert cursors == [None, "cursor-2"]
    # The truncated walk was never published as a complete catalog.
    assert get_cached_catalog(_S04_BASE, "cursor-token") is None


def test_catalog_row_overflow_is_incomplete_source(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [{"id": f"p/model-{i}", "owned_by": "p"} for i in range(50_001)],
                },
            )
        return httpx.Response(200, json=_S04_SURFACES.get(request.url.path, {}))

    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(httpx.MockTransport(handler), "rows-token", tmp_path / "lg.json")
    assert caught.value.code == "incomplete_source"
    assert "raw row limit" in str(caught.value)
    assert get_cached_catalog(_S04_BASE, "rows-token") is None


def test_catalog_page_overflow_is_incomplete_source(tmp_path: Path) -> None:
    requests = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/v1/models":
            return httpx.Response(200, json=_S04_SURFACES.get(path, {}))
        requests["count"] += 1
        return httpx.Response(
            200, json={"object": "list", "data": [], "next_cursor": f"cursor-{requests['count']}"}
        )

    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(httpx.MockTransport(handler), "pages-token", tmp_path / "lg.json")
    assert caught.value.code == "incomplete_source"
    assert "page limit" in str(caught.value)
    # The walk is bounded at the contract page cap (100 pages).
    assert requests["count"] == 100
    assert get_cached_catalog(_S04_BASE, "pages-token") is None


def test_catalog_decoded_byte_budget_is_incomplete_source(tmp_path: Path) -> None:
    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(
            _s04_transport(_S04_SURFACES),
            "bytes-token",
            tmp_path / "lg.json",
            decoded_byte_budget=128,
        )
    assert caught.value.code == "incomplete_source"
    assert "decoded byte budget" in str(caught.value)


def test_unsupported_pagination_hint_is_incomplete_source(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_catalog_row("oc/claude-small", "oc")],
                    "hasMore": True,
                },
            )
        return httpx.Response(200, json=_S04_SURFACES.get(path, {}))

    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(httpx.MockTransport(handler), "hint-token", tmp_path / "lg.json")
    assert caught.value.code == "incomplete_source"
    assert "unsupported pagination" in str(caught.value)
    assert get_cached_catalog(_S04_BASE, "hint-token") is None


def test_catalog_inconsistent_page_is_incomplete_source(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path != "/v1/models":
            return httpx.Response(200, json=_S04_SURFACES.get(path, {}))
        if request.url.params.get("after") is None:
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": [_catalog_row("oc/claude-small", "oc")],
                    "next_cursor": "cursor-2",
                },
            )
        # Every row on the followed page was already served: inconsistent.
        return httpx.Response(
            200, json={"object": "list", "data": [_catalog_row("oc/claude-small", "oc")]}
        )

    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(httpx.MockTransport(handler), "dup-token", tmp_path / "lg.json")
    assert caught.value.code == "incomplete_source"
    assert "repeated every row" in str(caught.value)
    assert get_cached_catalog(_S04_BASE, "dup-token") is None


def test_aggregate_deadline_zero_without_last_good_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(LiveAdmitError) as caught:
        _load_s04(
            _s04_transport(_S04_SURFACES),
            "deadline-token",
            tmp_path / "none.json",
            aggregate_deadline_seconds=0.0,
        )
    assert caught.value.code == "timeout"
    assert "aggregate discovery deadline" in str(caught.value)


def test_aggregate_deadline_zero_serves_original_deadline_last_good(tmp_path: Path) -> None:
    last_good = tmp_path / "lg.json"
    first = _load_s04(_s04_transport(_S04_SURFACES), "deadline-token", last_good)
    second = _load_s04(
        _s04_transport(_S04_SURFACES), "deadline-token", last_good, aggregate_deadline_seconds=0.0
    )
    # The catalog is fresh in cache; only the live surfaces went stale, served
    # from last-good within their original fetched_at deadlines.
    assert set(second.stale_served) == {"account_status", "free_tier", "providers"}  # type: ignore[attr-defined]
    assert [row.identity_id for row in second.catalog] == [  # type: ignore[attr-defined]
        row.identity_id
        for row in first.catalog  # type: ignore[attr-defined]
    ]


def test_unknown_catalog_shape_is_unknown_not_observed_zero() -> None:
    unknown = snapshot_from_payloads(
        catalog={"data": {"models": []}},  # nested wrong schema
        free_tier={"perModel": []},
        providers={"connections": []},
        account_status={"accountQuotas": []},
    )
    assert unknown.catalog == ()
    assert unknown.inventory_complete is False
    assert any(
        drop.model_id == "catalog" and drop.reason == "shape_unknown"
        for drop in unknown.inventory_diagnostics
    )
    zero = snapshot_from_payloads(
        catalog={"object": "list", "data": []},  # observed zero stays zero
        free_tier={"perModel": []},
        providers={"connections": []},
        account_status={"accountQuotas": []},
    )
    assert zero.catalog == ()
    assert zero.inventory_complete is True
    assert zero.inventory_diagnostics == ()


@pytest.mark.parametrize(
    "pricing", [{"input": 5.0, "output": 15.0}, None], ids=["subscription", "price-unknown"]
)
def test_active_catalog_route_admitted_without_free_or_price_evidence(pricing) -> None:
    row = _catalog_row("subscription/strong-code")
    if pricing is not None:
        row["pricing"] = pricing
    snapshot = _snapshot(
        catalog=[row],
        free_tier=[],
        providers=[{"provider": "subscription", "isActive": True, "testStatus": "active"}],
    )
    receipt = admit_free_tier_active(snapshot)
    assert receipt.admitted == ("subscription/strong-code",)
    assert not any(drop.reason in {"not_free_tier", "price_unknown"} for drop in receipt.exclusions)


def test_subscription_with_higher_coding_score_wins_after_fresh_confirmation() -> None:
    from verdict.metadata.records import (
        CapabilityCaps,
        FieldProvenance,
        ModelMetadataRecord,
        ProvenancedField,
        SoftScores,
    )
    from verdict.metadata.store import MetadataSnapshot

    strong, weak = "subscription/strong-code", "free/weak-code"
    fetched = "2026-09-17T18:00:00Z"

    def field(value):
        return ProvenancedField(
            value=value, provenance=FieldProvenance(source="models.dev", fetched_at=fetched)
        )

    snapshot = _snapshot(
        catalog=[
            {**_catalog_row(strong), "pricing": {"input": 5.0, "output": 15.0}},
            _catalog_row(weak),
        ],
        free_tier=[{"modelId": "weak-code", "provider": "free", "freeType": "keyless"}],
        providers=[
            {"provider": p, "isActive": True, "testStatus": "active"}
            for p in ("subscription", "free")
        ],
    )
    svc = _service(snapshot, passports={r: _fresh_passport(r) for r in (strong, weak)})
    svc.metadata_snapshot = MetadataSnapshot(
        schema_version="1",
        refreshed_at=fetched,
        sources={},
        records=tuple(
            ModelMetadataRecord(
                id=r,
                provider=r.split("/")[0],
                omniroute_ids=(r,),
                caps=CapabilityCaps(tools=field(True), context=field(128000)),
                scores=SoftScores(aa_coding=field(score)),
            )
            for r, score in ((strong, 95.0), (weak, 25.0))
        ),
    )
    decision = asyncio.run(
        svc.route(
            "Fix this Python code",
            criticality="low",
            context={"tools_required": True, "task_family": "coding"},
        )
    )
    assert decision.model == strong, decision.admit_receipt
    assert decision.admit_receipt is not None
    assert strong in decision.admit_receipt["admitted"]
    assert decision.admit_receipt["confirm"] == []


@pytest.mark.parametrize("policy", [None, "paid_allowed", "free_preferred"])
@pytest.mark.parametrize("task_class", ["ordinary", "worthy"])
def test_default_and_non_filtering_policies_do_not_filter_qualified_routes(
    policy, task_class
) -> None:
    from verdict.free_tier_admit import expand_admit_for_worthiness

    snapshot = _snapshot(
        catalog=[_catalog_row("subscription/strong-code")],
        free_tier=[],
        providers=[{"provider": "subscription", "isActive": True, "testStatus": "active"}],
    )
    base = admit_free_tier_active(snapshot)
    receipt = expand_admit_for_worthiness(
        base, snapshot, task_class=task_class, class_reasons=(), spend_policy=policy
    )
    assert receipt.admitted == ("subscription/strong-code",)
    assert receipt.exclusions == ()
    assert receipt.spend_policy == (policy or "paid_allowed")


@pytest.mark.parametrize("active,proven", [(True, True), (True, False), (False, True)])
def test_unknown_price_still_requires_active_provider_and_fresh_proof(active, proven) -> None:
    identity = "subscription/strong-code"
    snapshot = _snapshot(
        catalog=[_catalog_row(identity)],
        free_tier=[],
        providers=[{"provider": "subscription", "isActive": active, "testStatus": "active"}],
    )
    service = _service(snapshot, passports={identity: _fresh_passport(identity)} if proven else {})
    decision = asyncio.run(service.route("summarize this paragraph", criticality="low"))
    assert decision.model == (identity if active and proven else NO_ELIGIBLE_TARGET)
    if not active or not proven:
        assert decision.admit_receipt is not None
        reasons = {row["reason"] for row in decision.admit_receipt["exclusions"]}
        assert (REASON_PROVIDER_INACTIVE if not active else "no_passport") in reasons
