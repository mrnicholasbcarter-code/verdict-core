"""Harness path: Verdict /v1/chat/completions → free∩active admit → OmniRoute."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi.testclient import TestClient

import verdict.api as api
from verdict.free_tier_admit import FAIL_CLOSED_REASON, NO_ELIGIBLE_TARGET, snapshot_from_payloads
from verdict.intelligence import IntelligenceService
from verdict.model_passports import ModelPassport
from verdict.models import ProviderConfig
from verdict.proxy import UpstreamProxy


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
    provider = owned_by or identity_id.split("/", 1)[0]
    return {"id": identity_id, "owned_by": provider, "object": "model"}


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw_body = await request.aread()
        body = json.loads(raw_body) if raw_body else None
        self.requests.append(
            {
                "method": request.method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": body,
            }
        )
        host = request.url.host or ""
        # Fail closed: never pretend public Anthropic/OpenAI answered.
        if host in {"api.anthropic.com", "api.openai.com"}:
            return httpx.Response(599, json={"error": "public fallback forbidden"})
        if request.url.path.endswith("/chat/completions"):
            model = (body or {}).get("model", "unknown")
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": f"ok:{model}"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        return httpx.Response(404, json={"error": "missing"})


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


def _ok_confirm_transport():
    def transport(model_id: str, payload: object, timeout: float) -> dict[str, object]:
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    return transport


def _admit_service(
    snapshot, *, passports=None, confirm_transport=None, log_path: str = ""
) -> IntelligenceService:
    return IntelligenceService(
        primary_model="anthropic/claude-3-opus-20240229",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path=log_path,
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        execute_offload=False,
        passports=passports if passports is not None else {},
        confirm_transport=confirm_transport,
        admit_now=datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
        context_roots=(),
        mcp_root="",
    )


def _configure(monkeypatch, transport: RecordingTransport, intelligence) -> None:
    monkeypatch.setattr(api, "_build_intelligence", lambda: intelligence)
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://127.0.0.1:20128/v1",
            api_key="omni-secret",
            transport=transport,
            allow_private_hosts={"127.0.0.1", "localhost"},
        ),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)


def test_chat_completions_admits_then_forwards_to_omniroute(monkeypatch) -> None:
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
    transport = RecordingTransport()
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    _configure(
        monkeypatch,
        transport,
        _admit_service(
            snapshot,
            passports={identity: _fresh_passport(identity)},
            confirm_transport=_ok_confirm_transport(),
        ),
    )

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "summarize this paragraph"}],
                "max_tokens": 16,
                "criticality": "low",
            },
        )

    assert response.status_code == 200
    assert response.headers["x-verdict-model"] == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert response.headers["x-verdict-decision"] == "selected"
    assert len(transport.requests) == 1
    forwarded = transport.requests[0]
    assert forwarded["url"] == "http://127.0.0.1:20128/v1/chat/completions"
    assert forwarded["body"]["model"] == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert "api.anthropic.com" not in forwarded["url"]
    assert "api.openai.com" not in forwarded["url"]
    assert forwarded["headers"].get("authorization") == "Bearer omni-secret"


def test_empty_intersection_fails_closed_without_upstream(monkeypatch) -> None:
    snapshot = _snapshot(
        catalog=[_catalog_row("anthropic/claude-3-opus-20240229", "claude")],
        free_tier=[{"modelId": "ghost", "provider": "mistral", "freeType": "keyless"}],
        providers=[{"provider": "mistral", "isActive": False, "testStatus": "active"}],
    )
    transport = RecordingTransport()
    _configure(monkeypatch, transport, _admit_service(snapshot))

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "format docs"}],
                "criticality": "low",
            },
        )

    assert response.status_code == 503
    body = response.json()
    assert FAIL_CLOSED_REASON in body["error"]["message"]
    assert body.get("decision", {}).get("model") == NO_ELIGIBLE_TARGET
    assert transport.requests == []


def test_upstream_down_does_not_fallback_to_public(monkeypatch) -> None:
    class BoomTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.requests: list[str] = []

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(str(request.url))
            raise httpx.ConnectError("omniroute down", request=request)

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
    transport = BoomTransport()
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    monkeypatch.setattr(
        api,
        "_build_intelligence",
        lambda: _admit_service(
            snapshot,
            passports={identity: _fresh_passport(identity)},
            confirm_transport=_ok_confirm_transport(),
        ),
    )
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy(
            "http://127.0.0.1:20128/v1",
            api_key="omni-secret",
            transport=transport,
            allow_private_hosts={"127.0.0.1"},
        ),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("LLMGATE_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")

    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "hello world"}],
                "criticality": "low",
            },
        )

    assert response.status_code == 502
    assert transport.requests == ["http://127.0.0.1:20128/v1/chat/completions"]
    assert all(
        "api.anthropic.com" not in url and "api.openai.com" not in url for url in transport.requests
    )


def test_resolve_upstream_prefers_omniroute_base_url(monkeypatch) -> None:
    monkeypatch.delenv("LLMGATE_UPSTREAM_BASE_URL", raising=False)
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("OMNIROUTE_API_KEY", "from-omni")
    proxy = api._build_proxy()
    assert proxy.base_url == "http://127.0.0.1:20128/v1"
    assert proxy.api_key == "from-omni"


def test_resolve_upstream_explicit_llmgate_wins(monkeypatch) -> None:
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    monkeypatch.setenv("LLMGATE_UPSTREAM_BASE_URL", "http://127.0.0.1:20132/v1")
    monkeypatch.setenv("LLMGATE_UPSTREAM_API_KEY", "llmgate-key")
    proxy = api._build_proxy()
    assert proxy.base_url == "http://127.0.0.1:20132/v1"
    assert proxy.api_key == "llmgate-key"


def test_build_intelligence_disables_inline_execute_offload(monkeypatch) -> None:
    monkeypatch.setenv("LLMGATE_LOG_PATH", "")
    svc = api._build_intelligence()
    assert svc.execute_offload is False
    assert svc.managed_backend_status == "not_used"
    assert not hasattr(svc, "ruflo_command")


# --- BOD-111: hydrated pack is injected into the actual upstream request ------


def _admit_service_with_workspace(snapshot, *, passports, confirm_transport, workspace_root):
    from verdict.context_hydrate import DEFAULT_CONTEXT_ROOTS

    return IntelligenceService(
        primary_model="anthropic/claude-3-opus-20240229",
        providers={"omniroute": ProviderConfig(base_url="http://127.0.0.1:20128/v1")},
        profile="development",
        log_path="",
        log_full_task=False,
        discovery_ttl=60,
        admit_snapshot=snapshot,
        execute_offload=False,
        passports=passports,
        confirm_transport=confirm_transport,
        admit_now=datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc),
        context_roots=DEFAULT_CONTEXT_ROOTS,
        workspace_root=workspace_root,
        mcp_root="",
    )


def _free_snapshot():
    return _snapshot(
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


def test_hydrated_pack_is_injected_and_digest_matches_receipt(monkeypatch, tmp_path) -> None:
    import hashlib

    (tmp_path / "docs" / "adr").mkdir(parents=True)
    (tmp_path / "docs" / "architecture").mkdir(parents=True)
    (tmp_path / "docs" / "adr" / "ADR-001-ledger.md").write_text(
        "# ADR-001 Ledger\n\nADR-TOKEN-7731 governs the ledger.\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "architecture" / "overview.md").write_text(
        "# Architecture\n\nARCH-TOKEN-9920 single control plane.\n", encoding="utf-8"
    )
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    transport = RecordingTransport()
    _configure(
        monkeypatch,
        transport,
        _admit_service_with_workspace(
            _free_snapshot(),
            passports={identity: _fresh_passport(identity)},
            confirm_transport=_ok_confirm_transport(),
            workspace_root=tmp_path,
        ),
    )
    user_messages = [{"role": "user", "content": "summarize the ledger ADR"}]
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": user_messages, "criticality": "low"},
        )

    assert response.status_code == 200
    assert response.headers["x-verdict-pack-injected"] == "true"
    assert response.headers["x-verdict-pack-state"] == "hydrated"
    forwarded = transport.requests[0]["body"]
    assert forwarded["messages"][1:] == user_messages, "client messages are never rewritten"
    envelope = forwarded["messages"][0]
    assert envelope["role"] == "system"
    assert "ADR-TOKEN-7731" in envelope["content"]
    assert "ARCH-TOKEN-9920" in envelope["content"]
    assert "summarize the ledger ADR" in envelope["content"]
    digest = f"sha256:{hashlib.sha256(envelope['content'].encode('utf-8')).hexdigest()}"
    assert response.headers["x-verdict-pack-digest"] == digest
    # Verdict-local controls still never reach the upstream.
    assert "criticality" not in forwarded


def test_task_contract_pack_is_injected_without_workspace_files(monkeypatch, tmp_path) -> None:
    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    transport = RecordingTransport()
    _configure(
        monkeypatch,
        transport,
        _admit_service_with_workspace(
            _free_snapshot(),
            passports={identity: _fresh_passport(identity)},
            confirm_transport=_ok_confirm_transport(),
            workspace_root=tmp_path,  # nothing to hydrate from
        ),
    )
    user_messages = [{"role": "user", "content": "summarize this paragraph"}]
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": user_messages, "criticality": "low"},
        )
    assert response.status_code == 200
    assert response.headers["x-verdict-pack-injected"] == "true"
    assert response.headers["x-verdict-pack-state"] == "hydrated"
    assert response.headers["x-verdict-pack-digest"].startswith("sha256:")
    forwarded = transport.requests[0]["body"]["messages"]
    assert forwarded[1:] == user_messages
    assert forwarded[0]["role"] == "system"
    assert "summarize this paragraph" in forwarded[0]["content"]


# --- BOD-117: serving writes a post-execution outcome receipt --------------------


class CostReportingTransport(RecordingTransport):
    """Spy upstream that returns OmniRoute cost/usage headers like the real gateway."""

    def __init__(self, *, with_cost: bool) -> None:
        super().__init__()
        self.with_cost = with_cost

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await super().handle_async_request(request)
        if response.status_code != 200 or not self.with_cost:
            return response
        headers = dict(response.headers)
        headers.update(
            {
                "X-OmniRoute-Request-Id": "exec-777",
                "X-OmniRoute-Model": "openrouter/nvidia/nemotron-3-nano-30b-a3b:free",
                "X-OmniRoute-Response-Cost": "0.0031",
                "X-OmniRoute-Tokens-In": "640",
                "X-OmniRoute-Tokens-Out": "80",
                "X-OmniRoute-Cache-Hit": "false",
            }
        )
        return httpx.Response(200, headers=headers, content=response.content)


def _serve_once(monkeypatch, tmp_path, *, with_cost: bool):
    from verdict.outcome_log import load_outcomes

    identity = "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    transport = CostReportingTransport(with_cost=with_cost)
    decisions = tmp_path / "verdict-decisions.jsonl"
    _configure(
        monkeypatch,
        transport,
        _admit_service(
            _free_snapshot(),
            passports={identity: _fresh_passport(identity)},
            confirm_transport=_ok_confirm_transport(),
            log_path=str(decisions),
        ),
    )
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 200
    request_id = response.headers["x-verdict-request-id"]
    decision_rows = [
        json.loads(line) for line in decisions.read_text(encoding="utf-8").splitlines() if line
    ]
    assert [row["request_id"] for row in decision_rows] == [request_id]
    return request_id, decision_rows[0], load_outcomes(decisions)


def test_serve_writes_outcome_receipt_with_observed_cost(monkeypatch, tmp_path) -> None:
    request_id, decision, outcomes = _serve_once(monkeypatch, tmp_path, with_cost=True)
    assert "observed_cost_usd" not in decision, "pre-execution decision never carries cost"
    assert len(outcomes[request_id]["attempts"]) == 1
    outcome = outcomes[request_id]["final"]
    assert outcome["observed_cost_usd"] == 0.0031
    assert outcome["cost_source"] == "x-omniroute-response-cost"
    assert outcome["observed_tokens_total"] == 720
    assert outcome["completed_with"] == "openrouter/nvidia/nemotron-3-nano-30b-a3b:free"
    assert outcome["execution_id"] == "exec-777"
    assert outcome["status_code"] == 200
    assert outcome["surface"] == "chat"


def test_serve_without_cost_header_records_unmeasured_outcome(monkeypatch, tmp_path) -> None:
    request_id, _decision, outcomes = _serve_once(monkeypatch, tmp_path, with_cost=False)
    outcome = outcomes[request_id]["final"]
    assert outcome["observed_cost_usd"] is None, "no header means unmeasured, never $0"
    assert outcome["cost_source"] is None
    assert outcome["status_code"] == 200
