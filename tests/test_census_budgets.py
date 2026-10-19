"""Census probe budgets, passive refresh and eligibility (census S5)."""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from verdict.admission import RuntimeEvidence, RuntimeObservation
from verdict.free_tier_admit import snapshot_from_payloads
from verdict.orchestration.health_cache import HealthCache
from verdict.prove_at_rest import ProbeExchange
from verdict.prove_at_rest_legacy import load_healthy_passports

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)


def _snapshot(models: list[str]) -> object:
    providers = sorted({m.split("/", 1)[0] for m in models})
    return snapshot_from_payloads(
        catalog={"data": [{"id": m} for m in models]},
        free_tier={
            "perModel": [
                {
                    "provider": m.split("/", 1)[0],
                    "modelId": m.split("/", 1)[1],
                    "freeType": "keyless",
                }
                for m in models
            ]
        },
        providers={
            "connections": [
                {"provider": p, "isActive": True, "testStatus": "active"} for p in providers
            ]
        },
    )


def _transport(calls: list[str]):  # type: ignore[no-untyped-def]
    def transport(model, phase, timeout):  # type: ignore[no-untyped-def]
        calls.append(model)
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    return transport


def _run(
    tmp_path: Path, snapshot: object, calls: list[str], **budgets: object
) -> dict[str, object]:
    from verdict.census_passports import run_census

    return run_census(
        snapshot,
        _transport(calls),
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        **budgets,  # type: ignore[arg-type]
    )


def test_token_budget_bounded_by_shared_coordinator_maintenance_cap(tmp_path: Path) -> None:
    """Census's own max_candidates is no longer the binding cap: the shared
    coordinator's maintenance contract (S05 contract caps: 4 routes,
    2/provider, BOD-297/S05-A) is, and it binds below census's own ask.
    """
    models = [f"p{i}/m" for i in range(10)]
    calls: list[str] = []
    _run(tmp_path, _snapshot(models), calls, max_candidates=10)
    assert set(calls) <= set(models)
    assert len(set(calls)) == 4


def test_default_budget_is_per_provider_not_a_global_cap(tmp_path: Path) -> None:
    """S05 contract caps (4 routes, 2/provider; BOD-297/S05-A) now bound the
    actual probe count below census's own ask.
    """
    models = [f"a/m{i}" for i in range(5)] + [f"p{i}/m" for i in range(8)]
    calls: list[str] = []
    report = _run(tmp_path, _snapshot(models), calls)
    probed = set(calls)
    assert len([m for m in probed if m.startswith("a/")]) <= 2
    assert len(probed) == 4
    assert report["probed"] == 4


def test_explicit_token_budget_still_applies_on_its_own(tmp_path: Path) -> None:
    calls: list[str] = []
    _run(tmp_path, _snapshot([f"p{i}/m" for i in range(10)]), calls, max_tokens=512)
    assert len(set(calls)) == 2


def test_passively_healthy_routes_are_refreshed_without_probes(tmp_path: Path) -> None:
    models = [f"p{i}/m" for i in range(10)]
    evidence = RuntimeEvidence(
        tuple(
            RuntimeObservation(
                key=f"route:{m}",
                state="healthy",
                category="recent_success",
                source="test",
                observed_at=(NOW - timedelta(minutes=5)).isoformat(),
                until=(NOW + timedelta(minutes=5)).isoformat(),
            )
            for m in models
        ),
        ("test",),
    )
    calls: list[str] = []
    report = _run(tmp_path, replace(_snapshot(models), passive_health=evidence), calls)  # type: ignore[type-var]
    assert calls == []
    assert report["probed"] == 0
    assert report["passive_refreshed"] == 10
    assert set(load_healthy_passports(tmp_path / "state.json")) == set(models)


def _evidence(
    models: list[str], category: str = "recent_success", source: str = "test"
) -> RuntimeEvidence:
    return RuntimeEvidence(
        tuple(
            RuntimeObservation(
                key=f"route:{m}",
                state="healthy",
                category=category,
                source=source,
                observed_at=(NOW - timedelta(minutes=5)).isoformat(),
                until=(NOW + timedelta(minutes=5)).isoformat(),
            )
            for m in models
        ),
        (source,),
    )


def test_latency_only_evidence_never_refreshes(tmp_path: Path) -> None:
    models = [f"p{i}/m" for i in range(3)]
    evidence = _evidence(
        models, "passive_success", "omniroute:model-latency-stats+resilience/connections"
    )
    calls: list[str] = []
    report = _run(tmp_path, replace(_snapshot(models), passive_health=evidence), calls)  # type: ignore[type-var]
    assert report["passive_refreshed"] == 0
    assert set(calls) == set(models)


def test_success_evidence_renews_liveness_but_leaves_agentic_pass_untouched(tmp_path: Path) -> None:
    from verdict.prove_at_rest_legacy import (
        STATUS_HEALTHY,
        ProofResult,
        ProveAtRestCycle,
        ProveAtRestStore,
    )

    store_path = tmp_path / "state.json"
    agentic = replace(_passport("p0/agentic", NOW - timedelta(hours=1)), tool_support=True)
    ProveAtRestStore(path=store_path).write(
        ProveAtRestCycle(
            cycle_id="seed",
            started_at=NOW,
            finished_at=NOW,
            admitted=(),
            results=(
                ProofResult(
                    identity_id="p0/agentic",
                    provider="p0",
                    status=STATUS_HEALTHY,
                    proved_at=NOW - timedelta(hours=1),
                    latency_ms=None,
                    passport=agentic,
                ),
            ),
        )
    )
    models = ["p0/agentic", "p1/live"]
    snapshot = replace(_snapshot(models), passive_health=_evidence(models))  # type: ignore[type-var]
    from verdict.census_passports import refresh_from_passive_evidence

    renewed = refresh_from_passive_evidence(snapshot, store_path, NOW, set(models))
    passports = load_healthy_passports(store_path)
    assert renewed == {"p1/live"}
    assert passports["p1/live"].tool_support is False
    assert passports["p0/agentic"] == agentic


def test_stale_latency_stats_surface_renews_nothing(tmp_path: Path) -> None:
    models = [f"p{i}/m" for i in range(3)]
    evidence = _evidence(models, source="omniroute:model-latency-stats+call-logs")
    snapshot = replace(  # type: ignore[type-var]
        _snapshot(models), passive_health=evidence, stale_served=("latency_stats",)
    )
    calls: list[str] = []
    report = _run(tmp_path, snapshot, calls)
    assert report["passive_refreshed"] == 0
    assert report["stale_served"] == ["latency_stats"]


def test_banned_or_unavailable_connection_is_never_census_eligible(tmp_path: Path) -> None:
    models = ["banned/m", "gone/m", "ok/m"]
    snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": m} for m in models]},
        free_tier={
            "perModel": [
                {"provider": m.split("/")[0], "modelId": "m", "freeType": "keyless"} for m in models
            ]
        },
        providers={
            "connections": [
                {"provider": "banned", "isActive": True, "testStatus": "banned"},
                {"provider": "gone", "isActive": True, "testStatus": "unavailable"},
                {"provider": "ok", "isActive": True, "testStatus": "active"},
            ]
        },
    )
    snapshot = replace(snapshot, passive_health=_evidence(models))
    calls: list[str] = []
    report = _run(tmp_path, snapshot, calls)
    assert set(calls) <= {"ok/m"}
    assert not {"banned/m", "gone/m"} & set(load_healthy_passports(tmp_path / "state.json"))
    assert report["candidates"] == 1


def test_tick_reports_probe_budget_total(tmp_path: Path, caplog: object) -> None:
    import logging

    models = [f"a/m{i}" for i in range(3)] + ["b/m"]
    calls: list[str] = []
    with caplog.at_level(logging.INFO, logger="verdict.census_passports"):  # type: ignore[attr-defined]
        report = _run(tmp_path, _snapshot(models), calls)
    assert report["probe_budget"] == 4
    assert "census probe budget: 4 (2 providers x max_per_provider=2)" in caplog.text  # type: ignore[attr-defined]


def _passport(route: str, at: datetime):  # type: ignore[no-untyped-def]
    from verdict.probes import ProbeObservation
    from verdict.prove_at_rest_legacy import passport_from_probe

    return passport_from_probe(
        provider=route.split("/")[0],
        identity_id=route,
        observation=ProbeObservation(
            model_id=route, availability_state="ready", status="ok", observed_at=at
        ),
    )


def test_passive_evidence_older_than_30_minutes_does_not_refresh(tmp_path: Path) -> None:
    evidence = RuntimeEvidence(
        (
            RuntimeObservation(
                key="route:p0/m",
                state="healthy",
                category="recent_success",
                source="test",
                observed_at=(NOW - timedelta(minutes=31)).isoformat(),
                until=(NOW + timedelta(minutes=5)).isoformat(),
            ),
        ),
        ("test",),
    )
    calls: list[str] = []
    report = _run(tmp_path, replace(_snapshot(["p0/m"]), passive_health=evidence), calls)  # type: ignore[type-var]
    assert report["passive_refreshed"] == 0
    assert calls == ["p0/m", "p0/m"] or set(calls) == {"p0/m"}


def test_non_chat_routes_are_not_census_eligible(tmp_path: Path) -> None:
    calls: list[str] = []
    report = _run(tmp_path, _snapshot(["p0/text-embedding-3-small", "p1/chat-m"]), calls)
    assert set(calls) == {"p1/chat-m"}
    assert report["non_chat_skipped"] == 1


def test_census_service_command_carries_no_candidate_cap() -> None:
    from verdict import census_passports_service

    source = inspect.getsource(census_passports_service)
    assert "--max-candidates 6" not in source
    assert "--max-tokens 1536" not in source
    assert "--max-per-provider" in source
