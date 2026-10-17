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


def test_token_budget_no_longer_clamps_candidates_to_six(tmp_path: Path) -> None:
    models = [f"p{i}/m" for i in range(10)]
    calls: list[str] = []
    _run(tmp_path, _snapshot(models), calls, max_candidates=10)
    assert sorted(set(calls)) == sorted(models)


def test_default_budget_is_per_provider_not_a_global_cap(tmp_path: Path) -> None:
    models = [f"a/m{i}" for i in range(5)] + [f"p{i}/m" for i in range(8)]
    calls: list[str] = []
    report = _run(tmp_path, _snapshot(models), calls)
    probed = set(calls)
    assert len([m for m in probed if m.startswith("a/")]) == 2
    assert {f"p{i}/m" for i in range(8)} <= probed
    assert report["probed"] == 10


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
                category="passive_success",
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


def test_passive_evidence_older_than_30_minutes_does_not_refresh(tmp_path: Path) -> None:
    evidence = RuntimeEvidence(
        (
            RuntimeObservation(
                key="route:p0/m",
                state="healthy",
                category="passive_success",
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
