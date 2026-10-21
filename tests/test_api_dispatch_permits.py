"""API relay permits, exercised through real preparation and stub transport."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

import verdict.api as api
from tests import test_live_candidate_shortlist as live
from tests.test_proxy import FixedIntelligence
from verdict.models import RoutingDecision
from verdict.orchestration.health_cache import (
    PERMIT_STATE_MAYBE_SENT,
    HealthCache,
    default_cache_path,
)
from verdict.proxy import UpstreamProxy


@pytest.mark.parametrize(
    "case",
    [
        "positive",
        "missing",
        "expired",
        "revoked",
        "duplicate",
        "post_send",
        "alternative",
        "unproven_alternative",
        "future",
        "wrong_origin",
        "post_commit_revocation",
        "server_error",
        "auth",
        "identity_mismatch",
        "replayed_old_passport",
        "equal_revocation_passport",
        "newer_revocation_passport",
        "foreign_provider",
        "foreign_profile",
        "model_not_found_404",
        "free_text_404",
    ],
)
def test_dispatch_permit_gates_every_send_and_spawn(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Worker/spawn vertical is explicitly phase 2b; this aggregate covers API sends.
    current = datetime.now(timezone.utc)
    monkeypatch.setattr(live, "NOW", current)
    service = live._service([], top_k=2)
    service.admit_now = current
    service.passports = {
        rid: live._passport(rid, latency_ms=1.0) for rid in (live.STRONG, live.NEW)
    }
    prepared_decisions: list[RoutingDecision] = []
    requests: list[str] = []
    attempt_ids: list[str] = []
    serial = iter(
        ["duplicate-attempt"] * 4 if case == "duplicate" else [f"attempt-{i}" for i in range(8)]
    )
    monkeypatch.setattr(api, "_dispatch_attempt_id", lambda: next(serial), raising=False)

    async def transport(request: httpx.Request) -> httpx.Response:
        import json

        model = json.loads(await request.aread())["model"]
        requests.append(model)
        if case == "post_send":
            raise httpx.ReadTimeout("reply lost", request=request)
        if case == "server_error":
            return httpx.Response(503, json={"error": {"message": "server lost reply"}})
        if case == "auth":
            return httpx.Response(403, json={"error": {"message": "forbidden"}})
        if case == "model_not_found_404":
            return httpx.Response(
                404, json={"error": {"code": "model_not_found", "message": "gone"}}
            )
        if case == "free_text_404":
            return httpx.Response(404, json={"error": {"message": "no such model path /foo"}})
        if case == "post_commit_revocation":
            proof = next(
                p for p in prepared_decisions[0].admission_proofs.values() if p.model == model
            )
            HealthCache(default_cache_path()).record_revocation(
                proof.scope,
                route_id=proof.route_id,
                event_id="denied-after-commit",
                source_revision="rev3",
                observed_at=current,
            )
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        return httpx.Response(
            200,
            json={
                "model": "substituted-model" if case == "identity_mismatch" else model,
                "choices": [],
            },
        )

    class PreparedIntelligence(FixedIntelligence):
        log_path = ""
        profile = "production"

        async def route(
            self,
            task: str,
            criticality: str = "medium",
            context: dict[str, Any] | None = None,
            *,
            request_id: str | None = None,
        ) -> RoutingDecision:
            # The relay recomputes the profile digest from the request it sends,
            # so preparation must see that same request context.
            assert context is not None
            ctx = {**context, **live._context(live._request(live.STRONG, live.NEW))}
            ctx["_dispatch_gateway_origin"] = "http://upstream.test/v1"
            decision = await service.route(
                "fix code", criticality="low", context=ctx, request_id=request_id
            )
            if case in {
                "replayed_old_passport",
                "equal_revocation_passport",
                "newer_revocation_passport",
            }:
                primary = next(
                    p for p in decision.admission_proofs.values() if p.model == decision.model
                )
                revoked_at = current - timedelta(seconds=30)
                HealthCache(default_cache_path()).record_revocation(
                    primary.scope,
                    route_id=primary.route_id,
                    event_id="denied-before-reroute",
                    source_revision="rev2",
                    observed_at=revoked_at,
                )
                if case != "replayed_old_passport":
                    qualified_at = revoked_at + timedelta(
                        seconds=15 if case == "newer_revocation_passport" else 0
                    )
                    service.passports[primary.route_id] = replace(
                        service.passports[primary.route_id],
                        qualified_at=qualified_at,
                        last_verified_timestamp=qualified_at,
                    )
                decision = await service.route(
                    "fix code", criticality="low", context=ctx, request_id=request_id
                )
                if case == "newer_revocation_passport":
                    # A positive during active scoped backoff cannot reauthorize.
                    assert primary.route_id not in decision.admission_proofs
            elif case != "missing":
                assert len(decision.admission_proofs) == 2
                primary = next(
                    p for p in decision.admission_proofs.values() if p.model == decision.model
                )
                if case == "revoked":
                    HealthCache(default_cache_path()).record_revocation(
                        primary.scope,
                        route_id=primary.route_id,
                        event_id="denied-between-route-send",
                        source_revision="rev2",
                        observed_at=current,
                    )
                if case in {"future", "wrong_origin", "foreign_provider", "foreign_profile"}:
                    proofs = dict(decision.admission_proofs)
                    if case == "foreign_provider":
                        # Self-consistent proof for another provider serving the same model.
                        proofs[primary.route_id] = replace(
                            primary,
                            provider="otherco",
                            scope=api.dispatch_scope(
                                gateway_origin=primary.scope.gateway_origin,
                                provider="otherco",
                                model=primary.model,
                                route_id=primary.route_id,
                                profile_digest=primary.scope.profile_digest,
                            ),
                        )
                    elif case == "foreign_profile":
                        proofs[primary.route_id] = replace(
                            primary,
                            scope=replace(primary.scope, profile_digest="sha256:" + "f" * 64),
                        )
                    elif case == "future":
                        proofs[primary.route_id] = replace(
                            primary, qualified_at=current + timedelta(minutes=1)
                        )
                    else:
                        proofs[primary.route_id] = replace(
                            primary,
                            scope=replace(primary.scope, gateway_origin="http://wrong.test"),
                        )
                    decision = replace(decision, admission_proofs=proofs)
                if case in {"expired", "alternative", "unproven_alternative"}:
                    proofs = dict(decision.admission_proofs)
                    proofs[primary.route_id] = replace(
                        primary, expires_at=current - timedelta(seconds=1)
                    )
                    if case == "unproven_alternative":
                        proofs = {primary.route_id: proofs[primary.route_id]}
                    decision = replace(decision, admission_proofs=proofs)
            elif case == "missing":
                # Bare decisions are selection only, never send authority.
                decision = RoutingDecision(
                    model=decision.model,
                    provider=decision.provider,
                    tier=decision.tier,
                    reason="missing proof",
                )
            if case not in {
                "alternative",
                "unproven_alternative",
                "post_send",
                "post_commit_revocation",
                "server_error",
                "auth",
                "identity_mismatch",
            }:
                decision = replace(decision, alternatives=[])
            prepared_decisions.append(decision)
            return decision

    monkeypatch.setattr(api, "_build_intelligence", PreparedIntelligence)
    monkeypatch.setattr(
        api,
        "_build_proxy",
        lambda: UpstreamProxy("http://upstream.test/v1", transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setenv("LLMGATE_ALLOW_ANONYMOUS", "true")
    request_body = {
        "messages": [{"role": "user", "content": "fix code"}],
        "tools_required": True,
        "task_family": "coding",
        "spend_policy": "free_only",
    }
    with TestClient(api.app) as client:
        response = client.post(
            "/v1/chat/completions",
            json=request_body,
            headers={} if case == "server_error" else {"idempotency-key": "permit-test"},
        )
        if case == "duplicate":
            assert response.status_code == 200
            response = client.post(
                "/v1/chat/completions",
                json=request_body,
                headers={"idempotency-key": "permit-test"},
            )
    if case in {"positive", "alternative"}:
        assert response.status_code == 200
        assert len(requests) == 1
        if case == "alternative":
            assert requests[0] != prepared_decisions[0].model
    elif case in {
        "missing",
        "expired",
        "revoked",
        "unproven_alternative",
        "future",
        "wrong_origin",
        "replayed_old_passport",
        "equal_revocation_passport",
        "newer_revocation_passport",
        "foreign_provider",
        "foreign_profile",
    }:
        # Expired/revoked primary must not silently select another route here.
        assert response.status_code == 503
        assert requests == []
    elif case == "duplicate":
        assert response.status_code == 503
        assert len(requests) == 1
    elif case in {
        "auth",
        "server_error",
        "post_commit_revocation",
        "model_not_found_404",
        "free_text_404",
    }:
        assert (
            response.status_code
            == {
                "auth": 403,
                "server_error": 503,
                "post_commit_revocation": 502,
                "model_not_found_404": 404,
                "free_text_404": 404,
            }[case]
        )
        assert len(requests) == 1
    else:
        assert response.status_code == 502
        assert len(requests) == 1
    cache = HealthCache(default_cache_path())
    if case in {"auth", "identity_mismatch", "post_commit_revocation", "model_not_found_404"}:
        proof = next(
            p for p in prepared_decisions[0].admission_proofs.values() if p.model == requests[0]
        )
        assert cache.revocation_epoch(proof.scope) == proof.epoch + 1
    if case == "free_text_404":
        proof = next(
            p for p in prepared_decisions[0].admission_proofs.values() if p.model == requests[0]
        )
        assert cache.revocation_epoch(proof.scope) == proof.epoch
    if requests:
        if case == "duplicate":
            attempt_ids = ["duplicate-attempt"]
        else:
            attempt_ids = ["attempt-1" if case == "alternative" else "attempt-0"]
        for attempt_id in attempt_ids:
            assert cache.dispatch_attempt_state(attempt_id) == PERMIT_STATE_MAYBE_SENT
