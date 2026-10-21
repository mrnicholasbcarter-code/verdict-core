"""Tests for BOD-350 census_passports passive refresh and census timer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.health_cache import EVIDENCE_FRESH_SECONDS, HealthCache, ProbeResult
from verdict.prove_at_rest_legacy import load_healthy_passports

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")


def _free_result(*, tool_ok: bool = True) -> ProbeResult:
    return ProbeResult(
        "ok",
        chat_ok=True,
        tool_ok=tool_ok,
        capacity_evidence="free",
        probe_class="agentic",
        agentic_ok=tool_ok,
    )


def _sub_result() -> ProbeResult:
    return ProbeResult(
        "ok",
        chat_ok=True,
        tool_ok=True,
        capacity_evidence="subscription",
        probe_class="single_call",
    )


def _bad_result() -> ProbeResult:
    return ProbeResult("timeout", False, False)


# ── passive_refresh tests ────────────────────────────────────────────────────


def test_passive_refresh_derives_passport_from_fresh_free_healthy_entry(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/gemini-flash:free", _free_result(), NOW)
    cache.save()

    store_path = tmp_path / "state.json"
    result = passive_refresh(cache, max_passports=10, passport_store_path=store_path, now=NOW)

    assert result["updated"] == 1
    assert result["skipped_budget"] == 0
    passports = load_healthy_passports(store_path)
    assert "gc/gemini-flash:free" in passports
    passport = passports["gc/gemini-flash:free"]
    assert passport.auth_state == "authorized"
    assert passport.availability_state == "eligible"
    assert passport.expires_at > NOW


def test_passive_refresh_skips_subscription_routes(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("kc/claude:free", _free_result(), NOW)
    cache.record("kc/claude-sub", _sub_result(), NOW)
    cache.save()

    result = passive_refresh(
        cache, max_passports=10, passport_store_path=tmp_path / "state.json", now=NOW
    )
    assert result["updated"] == 1
    passports = load_healthy_passports(tmp_path / "state.json")
    assert "kc/claude:free" in passports
    assert "kc/claude-sub" not in passports


def test_passive_refresh_skips_unhealthy_routes(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/gemini:free", _free_result(), NOW)
    cache.record("gc/broken:free", _bad_result(), NOW)
    cache.save()

    result = passive_refresh(
        cache, max_passports=10, passport_store_path=tmp_path / "state.json", now=NOW
    )
    assert result["updated"] == 1
    passports = load_healthy_passports(tmp_path / "state.json")
    assert "gc/gemini:free" in passports
    assert "gc/broken:free" not in passports


def test_passive_refresh_respects_max_passports_budget(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    for i in range(5):
        cache.record(f"gc/model{i}:free", _free_result(), NOW)
    cache.save()

    result = passive_refresh(
        cache, max_passports=3, passport_store_path=tmp_path / "state.json", now=NOW
    )
    assert result["updated"] == 3
    assert result["skipped_budget"] == 2
    passports = load_healthy_passports(tmp_path / "state.json")
    assert len(passports) == 3


def test_passive_refresh_skips_already_fresh_passports(tmp_path):
    """Routes with a fresh passport should not count against the budget."""
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/model0:free", _free_result(), NOW)
    cache.record("gc/model1:free", _free_result(), NOW)
    cache.save()

    store_path = tmp_path / "state.json"
    # First run: both updated
    result1 = passive_refresh(cache, max_passports=10, passport_store_path=store_path, now=NOW)
    assert result1["updated"] == 2

    # Second run at same time: passports are fresh, not re-written
    result2 = passive_refresh(cache, max_passports=10, passport_store_path=store_path, now=NOW)
    assert result2["updated"] == 0
    assert result2["skipped_fresh"] == 2


def test_passive_refresh_stale_passport_is_refreshed(tmp_path):
    """A stale passport for a still-healthy route is overwritten."""
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/model0:free", _free_result(), NOW - timedelta(hours=1))
    cache.save()

    store_path = tmp_path / "state.json"
    result1 = passive_refresh(
        cache, max_passports=10, passport_store_path=store_path, now=NOW - timedelta(hours=1)
    )
    assert result1["updated"] == 1

    # Move time forward past PASSPORT_TTL; run again with a fresh cache entry
    cache2 = HealthCache(tmp_path / "health.json")
    cache2.record("gc/model0:free", _free_result(), NOW)
    cache2.save()

    result2 = passive_refresh(cache2, max_passports=10, passport_store_path=store_path, now=NOW)
    assert result2["updated"] == 1


def test_passive_refresh_dead_provider_skipped(tmp_path):
    """A route whose pool is dead (negative cache, auth failure) is not refreshed."""
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    # healthy=False (auth failure stored)
    cache.record(
        "dead/model:free", ProbeResult("unauthorized", False, False, probe_class="single_call"), NOW
    )
    cache.save()

    result = passive_refresh(
        cache, max_passports=10, passport_store_path=tmp_path / "state.json", now=NOW
    )
    assert result["updated"] == 0


def test_passive_refresh_rejects_stale_source_evidence(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "gc/stale:free", _free_result(), NOW - timedelta(seconds=EVIDENCE_FRESH_SECONDS + 1)
    )
    result = passive_refresh(cache, 10, tmp_path / "state.json", NOW)
    assert result["updated"] == 0
    assert not load_healthy_passports(tmp_path / "state.json")


def test_passive_refresh_empty_cache_is_noop(tmp_path):
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    result = passive_refresh(
        cache, max_passports=10, passport_store_path=tmp_path / "state.json", now=NOW
    )
    assert result == {"updated": 0, "skipped_budget": 0, "skipped_fresh": 0}


# ── census_passports_service tests ──────────────────────────────────────────


def test_census_service_dry_run_renders_files(tmp_path):
    from verdict.census_passports_service import manage_census_service

    report = manage_census_service(unit_dir=tmp_path / "units", interval=1800, dry_run=True)
    assert report["dry_run"]
    assert not (tmp_path / "units").exists()
    service_body = report["files"]["verdict-census.service"]
    timer_body = report["files"]["verdict-census.timer"]
    assert "census" in service_body
    assert "1800" in timer_body
    assert "verdict-census.service" in timer_body


def test_census_service_default_interval_is_less_than_passport_ttl(tmp_path):
    from verdict.census_passports_service import CENSUS_INTERVAL_SECONDS, manage_census_service
    from verdict.model_passports import PASSPORT_TTL_SECONDS

    assert CENSUS_INTERVAL_SECONDS < PASSPORT_TTL_SECONDS
    assert CENSUS_INTERVAL_SECONDS == 240
    report = manage_census_service(unit_dir=tmp_path / "units", dry_run=True)
    assert "240" in report["files"]["verdict-census.timer"]


def test_census_service_install_and_idempotent(tmp_path):
    from verdict.census_passports_service import manage_census_service

    calls = []
    first = manage_census_service(unit_dir=tmp_path / "u", interval=1800, run=calls.append)
    assert first["changed"]
    second = manage_census_service(unit_dir=tmp_path / "u", interval=1800, run=calls.append)
    assert not second["changed"]
    assert any("enable" in str(c) and "--now" in str(c) for c in calls)


def test_census_service_uninstall(tmp_path):
    from verdict.census_passports_service import manage_census_service

    calls = []
    manage_census_service(unit_dir=tmp_path / "u", run=calls.append)
    manage_census_service(unit_dir=tmp_path / "u", uninstall=True, run=calls.append)
    assert not list((tmp_path / "u").iterdir())


def test_census_service_rejects_invalid_interval(tmp_path):
    from verdict.census_passports_service import manage_census_service

    with pytest.raises(ValueError):
        manage_census_service(unit_dir=tmp_path / "u", interval=0)
    with pytest.raises(ValueError):
        manage_census_service(unit_dir=tmp_path / "u", interval=float("nan"))
    assert not (tmp_path / "u").exists()


def test_census_service_cli_dry_run_json(tmp_path, monkeypatch, capsys):
    from verdict.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["verdict", "census", "--install", "--dry-run", "--json"])
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"]
    assert report["files"]
    assert not (tmp_path / "systemd").exists()


# ── APC health-cache fast path tests ────────────────────────────────────────


def test_apc_uses_fresh_health_cache_as_passport(tmp_path):
    """gate_admit_prove_confirm uses a FRESH health cache entry as a passport,
    so confirm transport is not needed and fresh_ids is populated."""
    from verdict.admit_prove_confirm import gate_admit_prove_confirm
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/gemini:free", _free_result(), NOW)
    cache.save()

    receipt = FreeTierAdmitReceipt(
        admitted=("gc/gemini:free",),
        chosen="gc/gemini:free",
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"gc"}),
        free_admitted=frozenset({"gc/gemini:free"}),
        free_tier_providers=frozenset({"gc"}),
    )

    result = gate_admit_prove_confirm(
        receipt, health_cache_path=tmp_path / "health.json", confirm_transport=None, now=NOW
    )
    # Should be admitted without inline confirm
    assert "gc/gemini:free" in result.admitted


def test_apc_stale_health_cache_does_not_pass_fast_path(tmp_path):
    """A STALE (not FRESH) health entry does not satisfy the fast path."""
    from verdict.admit_prove_confirm import gate_admit_prove_confirm
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    # A healthy entry exactly at the evidence deadline is expired.
    stale_time = NOW - timedelta(seconds=EVIDENCE_FRESH_SECONDS)
    cache = HealthCache(tmp_path / "health.json")
    cache.record("gc/gemini:free", _free_result(), stale_time)
    cache.save()

    receipt = FreeTierAdmitReceipt(
        admitted=("gc/gemini:free",),
        chosen="gc/gemini:free",
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"gc"}),
        free_admitted=frozenset({"gc/gemini:free"}),
        free_tier_providers=frozenset({"gc"}),
    )

    result = gate_admit_prove_confirm(
        receipt, health_cache_path=tmp_path / "health.json", confirm_transport=None, now=NOW
    )
    # Stale entry → no fast path → no transport → drop (fail-closed)
    assert "gc/gemini:free" not in result.admitted


def test_apc_fast_path_absent_when_no_health_cache(tmp_path):
    """Without health_cache_path, existing behaviour is unchanged."""
    from verdict.admit_prove_confirm import gate_admit_prove_confirm
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    receipt = FreeTierAdmitReceipt(
        admitted=("gc/gemini:free",),
        chosen="gc/gemini:free",
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"gc"}),
        free_admitted=frozenset({"gc/gemini:free"}),
        free_tier_providers=frozenset({"gc"}),
    )

    result = gate_admit_prove_confirm(
        receipt, health_cache_path=None, confirm_transport=None, now=NOW
    )
    assert "gc/gemini:free" not in result.admitted


def _free_result_no_evidence(*, tool_ok: bool = True) -> ProbeResult:
    """Agentic PASS exactly like _free_result, but capacity_evidence is None.

    Mirrors ollama-cloud/* entries in the real health-cache: fresh, chat_ok,
    tool_ok, but the OmniRoute connection-metadata-derived capacity_evidence
    never resolves to the literal string "free" (BOD-431).
    """
    return ProbeResult(
        "ok",
        chat_ok=True,
        tool_ok=tool_ok,
        capacity_evidence=None,
        probe_class="agentic",
        agentic_ok=tool_ok,
    )


def test_apc_admits_free_admitted_route_with_no_capacity_evidence(tmp_path):
    """A health-cache entry with capacity_evidence=None still becomes a passport
    when its route is in receipt.free_admitted (the free-tier-summary authority),
    without any confirm transport call (BOD-431)."""
    from verdict.admit_prove_confirm import gate_admit_prove_confirm
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    cache = HealthCache(tmp_path / "health.json")
    cache.record("ollama-cloud/gemma4:31b", _free_result_no_evidence(), NOW)
    cache.save()

    receipt = FreeTierAdmitReceipt(
        admitted=("ollama-cloud/gemma4:31b",),
        chosen="ollama-cloud/gemma4:31b",
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"ollama-cloud"}),
        free_admitted=("ollama-cloud/gemma4:31b",),
        free_tier_providers=frozenset({"ollama-cloud"}),
    )

    result = gate_admit_prove_confirm(
        receipt, health_cache_path=tmp_path / "health.json", confirm_transport=None, now=NOW
    )
    assert "ollama-cloud/gemma4:31b" in result.admitted


def test_apc_admits_fresh_eligible_route_without_free_membership(tmp_path):
    """A fresh eligible agentic-PASS route does not require free-tier membership."""
    from verdict.admit_prove_confirm import gate_admit_prove_confirm
    from verdict.free_tier_admit import FreeTierAdmitReceipt

    cache = HealthCache(tmp_path / "health.json")
    cache.record("ollama-cloud/gemma4:31b", _free_result_no_evidence(), NOW)
    cache.save()

    receipt = FreeTierAdmitReceipt(
        admitted=("ollama-cloud/gemma4:31b",),
        chosen="ollama-cloud/gemma4:31b",
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"ollama-cloud"}),
        free_admitted=(),
        free_tier_providers=frozenset({"ollama-cloud"}),
    )

    result = gate_admit_prove_confirm(
        receipt, health_cache_path=tmp_path / "health.json", confirm_transport=None, now=NOW
    )
    assert "ollama-cloud/gemma4:31b" in result.admitted


def test_passive_refresh_writes_passport_for_eligible_route_with_no_capacity_evidence(tmp_path):
    """census passive_refresh writes a passport for a fresh agentic-PASS route
    with capacity_evidence=None when it is in the caller-supplied eligible_routes
    set (already free-tier-summary-authoritative), mirroring run_census's own
    call with eligible_routes=set(candidates) derived from receipt.free_admitted
    (BOD-431)."""
    from verdict.census_passports import passive_refresh

    cache = HealthCache(tmp_path / "health.json")
    cache.record("ollama-cloud/gemma4:31b", _free_result_no_evidence(), NOW)
    cache.save()

    store_path = tmp_path / "state.json"
    result = passive_refresh(
        cache,
        max_passports=10,
        passport_store_path=store_path,
        now=NOW,
        eligible_routes={"ollama-cloud/gemma4:31b"},
    )

    assert result["updated"] == 1
    passports = load_healthy_passports(store_path)
    assert "ollama-cloud/gemma4:31b" in passports


def test_census_active_candidates_filtered_and_budgeted(tmp_path):
    from dataclasses import replace

    from verdict.census_passports import run_census
    from verdict.free_tier_admit import snapshot_from_payloads
    from verdict.prove_at_rest import ProbeExchange

    ids = ["good/a", "good/b", "dead/c", "paid/d"]
    snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": model} for model in ids]},
        free_tier={
            "perModel": [
                {"provider": "good", "modelId": "a", "freeType": "keyless"},
                {"provider": "good", "modelId": "b", "freeType": "keyless"},
                {"provider": "dead", "modelId": "c", "freeType": "keyless"},
            ]
        },
        providers={
            "connections": [
                {"provider": "good", "isActive": True, "testStatus": "active"},
                {"provider": "dead", "isActive": False},
                {"provider": "paid", "isActive": True, "testStatus": "active"},
            ]
        },
    )
    from verdict.admission import RuntimeEvidence, RuntimeObservation

    snapshot = replace(
        snapshot,
        passive_health=RuntimeEvidence(
            tuple(
                RuntimeObservation(
                    key="route:" + model,
                    state="healthy",
                    category="passive_success",
                    source="fixture",
                    observed_at=NOW.isoformat(),
                    until=(NOW + timedelta(minutes=5)).isoformat(),
                )
                for model in ids
            )
        ),
    )
    calls = []

    def transport(model, phase, timeout):
        calls.append((model, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    result = run_census(
        snapshot,
        transport,
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        max_candidates=1,
        max_tokens=256,
    )
    assert result["probed"] == 1
    assert {model for model, _ in calls} == {"good/a"}
    assert len(calls) == 2
    assert set(load_healthy_passports(tmp_path / "state.json")) == {"good/a"}


def test_census_token_budget_prevents_dispatch(tmp_path):
    from unittest.mock import Mock

    from verdict.census_passports import run_census
    from verdict.free_tier_admit import OmniRouteAdmitSnapshot

    transport = Mock(side_effect=AssertionError("must not probe"))
    snapshot = OmniRouteAdmitSnapshot(catalog=(), free_tier=(), connections=())
    result = run_census(
        snapshot,
        transport,
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        max_candidates=10,
        max_tokens=1,
    )
    assert result["probed"] == 0
    transport.assert_not_called()


def test_census_fake_gateway_integration(tmp_path):
    import httpx

    from verdict.census_passports import run_census
    from verdict.free_tier_admit import load_omniroute_admit_snapshot
    from verdict.prove_at_rest import ProbeExchange

    def gateway(request):
        path = request.url.path
        data = {
            "/v1/models": {"data": [{"id": "good/a", "owned_by": "good"}]},
            "/api/v1/me/status": {"accountQuotas": []},
            "/api/free-tier/summary": {
                "perModel": [{"provider": "good", "modelId": "a", "freeType": "keyless"}]
            },
            "/api/providers": {
                "connections": [{"provider": "good", "isActive": True, "testStatus": "active"}]
            },
            "/api/usage/model-latency-stats": {
                "entries": [
                    {
                        "key": "good/a",
                        "provider": "good",
                        "model": "a",
                        "totalRequests": 5,
                        "successRate": 1,
                        "windowHours": 168,
                    }
                ]
            },
            "/api/resilience/connections": {
                "connections": [
                    {
                        "provider": "good",
                        "isActive": True,
                        "connectionStatus": "healthy",
                        "cooldownRemainingMs": 0,
                        "breaker": {"state": "CLOSED"},
                        "lockouts": [],
                    }
                ]
            },
        }
        return httpx.Response(200, json=data[path])

    snapshot = load_omniroute_admit_snapshot(
        "http://gateway", transport=httpx.MockTransport(gateway)
    )
    now = datetime.now(timezone.utc)
    result = run_census(
        snapshot,
        lambda *args: ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True),
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=now,
        max_candidates=1,
        max_tokens=256,
    )
    assert result["updated"] == 1
    passport = load_healthy_passports(tmp_path / "state.json")["good/a"]
    assert passport.expires_at > now


def test_census_passport_copy_never_extends_source_ttl(tmp_path):
    from verdict.census_passports import passive_refresh
    from verdict.model_passports import PASSPORT_TTL_SECONDS

    cache = HealthCache(tmp_path / "health.json")
    cache.record("good/a", _free_result(), NOW - timedelta(seconds=PASSPORT_TTL_SECONDS + 1))
    result = passive_refresh(cache, 1, tmp_path / "state.json", NOW)
    assert result["updated"] == 0


def test_census_passport_reaches_route_admission_fresh_and_stale(tmp_path):
    """Census passport in passport_store_path reaches route admission; fresh admitted, stale rejected."""
    from verdict.admit_prove_confirm import REASON_PASSPORT_STALE, gate_admit_prove_confirm
    from verdict.census_passports import passive_refresh
    from verdict.free_tier_admit import FreeTierAdmitReceipt
    from verdict.model_passports import PASSPORT_TTL_SECONDS

    store_path = tmp_path / "state.json"
    cache = HealthCache(tmp_path / "health.json")
    model_id = "good/a"
    cache.record(model_id, _free_result(), NOW)
    cache.save()

    refresh_res = passive_refresh(cache, 1, store_path, NOW)
    assert refresh_res["updated"] == 1

    receipt = FreeTierAdmitReceipt(
        admitted=(model_id,),
        chosen=model_id,
        empty_intersection=False,
        exclusions=(),
        active_providers=frozenset({"good"}),
        free_admitted=frozenset({model_id}),
        free_tier_providers=frozenset({"good"}),
    )

    def ok_transport(model_id, payload, timeout):
        return {
            "status_code": 200,
            "body": {
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        }

    # 1. Fresh passport reaches route admission
    admit_fresh = gate_admit_prove_confirm(
        receipt,
        passport_store_path=store_path,
        confirm_transport=ok_transport,
        now=NOW + timedelta(seconds=10),
    )
    assert model_id in admit_fresh.admitted
    assert admit_fresh.chosen == model_id

    # 2. Stale passport rejected (fails closed)
    stale_now = NOW + timedelta(seconds=PASSPORT_TTL_SECONDS + 10)
    admit_stale = gate_admit_prove_confirm(
        receipt, passport_store_path=store_path, confirm_transport=ok_transport, now=stale_now
    )
    assert model_id not in admit_stale.admitted
    assert any(drop.reason == REASON_PASSPORT_STALE for drop in admit_stale.exclusions)


def test_census_matches_aliased_passive_health(tmp_path):
    """Census candidates match even when passive health uses canonical pool prefix."""
    from verdict.admission import RuntimeEvidence, RuntimeObservation
    from verdict.census_passports import run_census
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import ProbeExchange

    now = datetime(2026, 10, 14, 10, 0, tzinfo=timezone.utc)
    until = (now + timedelta(hours=1)).isoformat()
    from dataclasses import replace

    from verdict.free_tier_admit import snapshot_from_payloads

    snapshot = replace(
        snapshot_from_payloads(
            catalog={"data": [{"id": "agy/gemini-3.1-flash-lite", "owned_by": "antigravity"}]},
            free_tier={
                "perModel": [
                    {
                        "modelId": "gemini-3.1-flash-lite",
                        "provider": "antigravity",
                        "freeType": "keyless",
                    }
                ]
            },
            providers={
                "connections": [
                    {"provider": "antigravity", "isActive": True, "testStatus": "active"}
                ]
            },
        ),
        passive_health=RuntimeEvidence(
            observations=(
                RuntimeObservation(
                    "route:antigravity/gemini-3.1-flash-lite",
                    "healthy",
                    "passive_success",
                    "fixture",
                    now.isoformat(),
                    until=until,
                ),
            )
        ),
    )

    result = run_census(
        snapshot,
        lambda *args: ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True),
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "state.json",
        now=now,
        max_candidates=1,
        max_tokens=256,
    )
    assert result["candidates"] == 1
    assert result["updated"] == 1


def test_census_service_working_directory_is_unquoted(tmp_path):
    """WorkingDirectory= takes a literal path, not a shell-quoted one (unlike
    ExecStart=): a quoted value is parsed as part of the path itself and
    systemd refuses to start the unit ("path is not absolute")."""
    from verdict.census_passports_service import manage_census_service

    report = manage_census_service(unit_dir=tmp_path / "units", dry_run=True)
    service_body = report["files"]["verdict-census.service"]
    working_dir_line = next(
        line for line in service_body.splitlines() if line.startswith("WorkingDirectory=")
    )
    value = working_dir_line.removeprefix("WorkingDirectory=")
    assert not value.startswith('"')
    assert Path(value).is_absolute()


def test_census_service_unit_files_pass_systemd_analyze_verify(tmp_path):
    """Real systemd parser must accept the rendered units, not just our own
    string checks."""
    import shutil
    import subprocess

    from verdict.census_passports_service import manage_census_service

    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not available on this host")

    unit_dir = tmp_path / "units"
    report = manage_census_service(unit_dir=unit_dir, dry_run=True)
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name, body in report["files"].items():
        (unit_dir / name).write_text(body)

    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit_dir / "verdict-census.service")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "WorkingDirectory" not in result.stderr
    assert "not absolute" not in result.stderr


def test_census_service_working_directory_escapes_percent_specifier(monkeypatch, tmp_path):
    """WorkingDirectory= has no quoting, but systemd still expands "%"
    specifiers in it; a literal "%" in the path must be doubled."""
    from verdict import census_passports_service

    # The runtime copy path carries the "%"; the source path only feeds the copy.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "a%b"))

    report = census_passports_service.manage_census_service(
        unit_dir=tmp_path / "units", dry_run=True
    )
    service_body = report["files"]["verdict-census.service"]
    working_dir_line = next(
        line for line in service_body.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert "%%" in working_dir_line
    assert "a%b" not in working_dir_line.replace("%%", "")


def test_census_six_free_active_routes_and_provider_cooldown(tmp_path):
    import httpx

    from verdict.census_passports import load_census_cooldowns, run_census
    from verdict.free_tier_admit import snapshot_from_payloads
    from verdict.prove_at_rest import ProbeExchange

    ids = ["kiro/cooled"] + [f"good/model-{i}" for i in range(7)] + ["paid/model", "dead/model"]
    snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": model, "owned_by": model.split("/")[0]} for model in ids]},
        free_tier={
            "perModel": [
                {
                    "provider": model.split("/")[0],
                    "modelId": model.split("/")[1],
                    "freeType": "keyless",
                }
                for model in ids
                if not model.startswith("paid/")
            ]
        },
        providers={
            "connections": [
                {"provider": provider, "isActive": provider != "dead", "testStatus": "active"}
                for provider in ("kiro", "good", "paid", "dead")
            ]
        },
    )
    requests = []

    def gateway(request):
        requests.append(request.url.path)
        assert request.url.path == "/api/resilience/model-cooldowns"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "provider": "kiro",
                        "model": "another-model",
                        "reason": "rate_limit",
                        "remainingMs": 60000,
                    },
                    {
                        "provider": "good",
                        "model": "model-0",
                        "reason": "rate_limit",
                        "remainingMs": 0,
                    },
                ]
            },
        )

    cooldowns = load_census_cooldowns("http://gateway", transport=httpx.MockTransport(gateway))
    calls = []

    def probe(model, phase, timeout):
        calls.append((model, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    report = run_census(
        snapshot,
        probe,
        cache=HealthCache(tmp_path / "health.json"),
        passport_store_path=tmp_path / "passports.json",
        now=NOW,
        max_candidates=6,
        max_tokens=1536,
        max_per_provider=None,
        cooling_providers=cooldowns,
    )
    # S05 contract caps: 4 routes, 2/provider (BOD-297/S05-A). The shared
    # coordinator's maintenance contract now bounds the actual probe count,
    # even though census's own max_per_provider=None lifts its
    # candidate-selection cap.
    assert requests == ["/api/resilience/model-cooldowns"]
    assert report["probed"] == 2
    assert report["updated"] == 2
    assert report["reserved_tokens"] == 512
    assert {model for model, phase in calls} <= {f"good/model-{i}" for i in range(6)}
    assert len(calls) == 4
    assert len(load_healthy_passports(tmp_path / "passports.json")) == 2


def test_census_probe_loop_is_owned_by_the_shared_coordinator(tmp_path):
    """BOD-297/S05-A: census joins the shared RefreshCoordinator, not its own loop.

    While another producer holds the coordinator's flock, a census tick must
    make ZERO direct transport calls (it joins/lock-times-out like every other
    producer). Once the lock is free, a 9,000-candidate tick is bounded by the
    maintenance contract: at most 4 routes, 16 requests, 2 per provider, and
    the coordinator's own marker records the "census" consumer.
    """
    import multiprocessing

    from verdict.census_passports import run_census
    from verdict.free_tier_admit import snapshot_from_payloads
    from verdict.orchestration.verified_refresh import _JobLock
    from verdict.prove_at_rest import ProbeExchange

    cache_path = tmp_path / "health.json"
    lock_path = cache_path.parent / "verified-refresh.lock"
    marker_path = cache_path.parent / "verified-refresh.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    def _hold_lock_in_child(path_str: str, started: Any, release: Any) -> None:
        lock = _JobLock(Path(path_str))
        lock.acquire()
        started.set()
        release.wait(timeout=5.0)
        lock.release()

    ids = [f"good/m{i}" for i in range(6)]
    snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": model, "owned_by": "good"} for model in ids]},
        free_tier={
            "perModel": [
                {"provider": "good", "modelId": f"m{i}", "freeType": "keyless"} for i in range(6)
            ]
        },
        providers={"connections": [{"provider": "good", "isActive": True, "testStatus": "active"}]},
    )
    calls = []

    def transport(model, phase, timeout):
        calls.append((model, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    ctx = multiprocessing.get_context("fork")
    started = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_lock_in_child, args=(str(lock_path), started, release))
    holder.start()
    try:
        assert started.wait(timeout=5.0)
        report = run_census(
            snapshot,
            transport,
            cache=HealthCache(cache_path),
            passport_store_path=tmp_path / "state.json",
            now=NOW,
            max_candidates=6,
            max_tokens=4096,
        )
    finally:
        release.set()
        holder.join(timeout=5.0)

    assert calls == []
    assert report["probed"] == 0
    assert report.get("coordinator_outcome") in {"lock_timeout", "joined"}

    # -- lock free: 9,000 candidates bounded by the maintenance contract --
    many_ids = [f"p{i % 4500}/m{i}" for i in range(9_000)]
    many_providers = sorted({model.split("/", 1)[0] for model in many_ids})
    big_snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": model, "owned_by": model.split("/", 1)[0]} for model in many_ids]},
        free_tier={
            "perModel": [
                {
                    "provider": model.split("/", 1)[0],
                    "modelId": model.split("/", 1)[1],
                    "freeType": "keyless",
                }
                for model in many_ids
            ]
        },
        providers={
            "connections": [
                {"provider": provider, "isActive": True, "testStatus": "active"}
                for provider in many_providers
            ]
        },
    )
    calls2 = []

    def transport2(model, phase, timeout):
        calls2.append((model, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    run_census(
        big_snapshot,
        transport2,
        cache=HealthCache(cache_path),
        passport_store_path=tmp_path / "state2.json",
        now=NOW,
    )
    probed_routes = {model for model, _phase in calls2}
    assert len(probed_routes) <= 4
    assert len(calls2) <= 16
    per_family: dict[str, int] = {}
    for model in probed_routes:
        family = model.split("/", 1)[0]
        per_family[family] = per_family.get(family, 0) + 1
    assert all(count <= 2 for count in per_family.values())
    marker_payload = json.loads(marker_path.read_text(encoding="utf-8"))
    assert marker_payload.get("owner") == "census"


def test_census_install_updates_runtime_and_reports_last_result(tmp_path, monkeypatch):
    from verdict.census_passports_service import census_status, manage_census_service

    source = tmp_path / "installed"
    (source / "verdict").mkdir(parents=True)
    (source / "verdict" / "__init__.py").write_text('VERSION = "one"\n')
    (source / "pyproject.toml").write_text('[project]\nname="verdict"\nversion="1"\n')
    (source / "uv.lock").write_text("version = 1\n")
    runtime = tmp_path / "runtime"
    calls = []
    first = manage_census_service(
        unit_dir=tmp_path / "units", source_root=source, runtime_root=runtime, run=calls.append
    )
    assert (runtime / "repo/verdict/__init__.py").read_text() == 'VERSION = "one"\n'
    assert str(runtime / "repo") in first["files"]["verdict-census.service"]
    (runtime / "last-result.json").write_text(json.dumps({"updated": 4, "probed": 6}))
    (source / "verdict/__init__.py").write_text('VERSION = "two"\n')
    second = manage_census_service(
        unit_dir=tmp_path / "units", source_root=source, runtime_root=runtime, run=calls.append
    )
    assert (runtime / "repo/verdict/__init__.py").read_text() == 'VERSION = "two"\n'
    assert second["runtime_version"] != first["runtime_version"]
    status = census_status(runtime_root=runtime)
    assert status["version"] == second["runtime_version"]
    assert status["last_result"] == {"updated": 4, "probed": 6}


def test_doctor_census_section_marks_retired_runtime_without_install_advice():
    from verdict.doctor_diagnostics import census_runtime_section

    for status in (
        {"version": "0.5.0+abc123", "last_result": {"updated": 4, "probed": 6}},
        {"version": None, "last_result": None},
    ):
        row = census_runtime_section(status)
        assert row[0] == "Census runtime"
        assert row[1] == "note"
        assert "retired" in row[2]
        assert "identify" in row[2]
        assert "--install" not in row[2]


def test_census_command_records_last_result_for_doctor(tmp_path, monkeypatch, capsys):
    # Import the consumer before patching its source module; otherwise its
    # module-level import retains this test double after monkeypatch teardown.
    from verdict import census_passports, free_tier_admit, intelligence, prove_at_rest
    from verdict.census_passports import cmd_census
    from verdict.census_passports_service import census_status

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Initialize and patch both bindings so a first lazy import cannot retain
    # this fixture endpoint after monkeypatch teardown.
    monkeypatch.setattr(
        free_tier_admit, "omniroute_endpoint_from_env", lambda *a, **k: ("http://gw", "k")
    )
    monkeypatch.setattr(
        intelligence, "omniroute_endpoint_from_env", lambda *a, **k: ("http://gw", "k")
    )
    keyfile = tmp_path / ".config/verdict/omniroute-api-key"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text("k")
    monkeypatch.setattr(free_tier_admit, "load_omniroute_admit_snapshot", lambda *a, **k: object())
    monkeypatch.setattr(prove_at_rest, "live_transport", lambda *a, **k: object())
    monkeypatch.setattr(
        census_passports,
        "run_census",
        lambda *a, **k: {"updated": 4, "probed": 6, "candidates": 6, "reserved_tokens": 1536},
    )
    monkeypatch.setattr(census_passports, "load_census_cooldowns", lambda *a, **k: frozenset())

    cmd_census(health_cache_path=tmp_path / "h.json", passport_store_path=tmp_path / "p.json")

    status = census_status()
    assert status["version"] is None
    assert status["last_result"]["updated"] == 4
    assert status["last_result"]["probed"] == 6
    assert status["last_result"]["finished_at"]


def test_census_command_survives_cooldown_endpoint_failures(tmp_path, monkeypatch):
    """A cooldown-endpoint outage or malformed payload must not kill the census."""
    import httpx

    # Import the consumer before patching its source module; otherwise its
    # module-level import retains this test double after monkeypatch teardown.
    from verdict import census_passports, free_tier_admit, intelligence, prove_at_rest
    from verdict.census_passports import cmd_census

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    # Initialize and patch both bindings so a first lazy import cannot retain
    # this fixture endpoint after monkeypatch teardown.
    monkeypatch.setattr(
        free_tier_admit, "omniroute_endpoint_from_env", lambda *a, **k: ("http://gw", "k")
    )
    monkeypatch.setattr(
        intelligence, "omniroute_endpoint_from_env", lambda *a, **k: ("http://gw", "k")
    )
    keyfile = tmp_path / ".config/verdict/omniroute-api-key"
    keyfile.parent.mkdir(parents=True, exist_ok=True)
    keyfile.write_text("k")
    monkeypatch.setattr(free_tier_admit, "load_omniroute_admit_snapshot", lambda *a, **k: object())
    monkeypatch.setattr(prove_at_rest, "live_transport", lambda *a, **k: object())
    seen = []
    monkeypatch.setattr(
        census_passports,
        "run_census",
        lambda *a, **k: (
            seen.append(k["cooling_providers"])
            or {"updated": 0, "probed": 0, "candidates": 0, "reserved_tokens": 0}
        ),
    )

    failures = [
        httpx.ConnectError("down"),
        httpx.HTTPStatusError("500", request=None, response=None),
        KeyError("items"),
        ValueError("invalid json"),
        TypeError("wrong shape"),
    ]
    for failure in failures:
        monkeypatch.setattr(
            census_passports,
            "load_census_cooldowns",
            lambda *a, _failure=failure, **k: (_ for _ in ()).throw(_failure),
        )
        cmd_census(health_cache_path=tmp_path / "h.json", passport_store_path=tmp_path / "p.json")
    assert seen == [frozenset()] * len(failures)


def test_census_install_refuses_runtime_copy_as_source(tmp_path):
    """Installing from the runtime copy itself must not delete its own source."""
    from verdict.census_passports_service import manage_census_service

    runtime = tmp_path / "runtime"
    repo = runtime / "repo"
    (repo / "verdict").mkdir(parents=True)
    (repo / "verdict" / "__init__.py").write_text('VERSION = "x"\n')
    with pytest.raises(ValueError, match="runtime copy"):
        manage_census_service(
            unit_dir=tmp_path / "units", source_root=repo, runtime_root=runtime, run=lambda *a: None
        )
    assert (repo / "verdict" / "__init__.py").is_file()


def test_census_install_copies_only_the_verdict_package(tmp_path):
    """A site-packages source must not duplicate every dependency into runtime."""
    from verdict.census_passports_service import manage_census_service

    source = tmp_path / "site-packages"
    (source / "verdict").mkdir(parents=True)
    (source / "verdict" / "__init__.py").write_text('VERSION = "new"\n')
    (source / "httpx").mkdir()
    (source / "httpx" / "__init__.py").write_text("")
    runtime = tmp_path / "runtime"
    old_repo = runtime / "repo"
    (old_repo / "verdict").mkdir(parents=True)
    (old_repo / "verdict" / "__init__.py").write_text('VERSION = "old"\n')

    manage_census_service(
        unit_dir=tmp_path / "units", source_root=source, runtime_root=runtime, run=lambda *a: None
    )

    assert (runtime / "repo/verdict/__init__.py").read_text() == 'VERSION = "new"\n'
    assert not (runtime / "repo/httpx").exists()
    assert not (runtime / ".repo.staging").exists()
    assert not (runtime / ".repo.old").exists()
    with pytest.raises(ValueError, match="no verdict package"):
        manage_census_service(
            unit_dir=tmp_path / "units",
            source_root=tmp_path / "elsewhere",
            runtime_root=runtime,
            run=lambda *a: None,
        )


def test_load_census_cooldowns_tolerates_payload_shapes():
    """Unknown keys, mapping values and non-mapping rows mean no hints, never a crash."""
    import httpx

    from verdict.census_passports import load_census_cooldowns

    payloads = [
        ({"items": {"a": {"provider": "p1", "remainingMs": 5}}}, {"p1"}),
        ({"lockouts": [{"provider": "p2", "remainingMs": 10}]}, {"p2"}),
        ({"data": [{"provider": "p3", "remainingMs": True}]}, set()),
        ({"items": ["x", {"provider": "p4", "remainingMs": 1}]}, {"p4"}),
        ({"unexpected": 1}, set()),
        (["x"], set()),
    ]
    for payload, expected in payloads:
        transport = httpx.MockTransport(
            lambda request, _payload=payload: httpx.Response(200, json=_payload)
        )
        assert load_census_cooldowns("http://gw", transport=transport) == expected


def test_doctor_census_section_is_retired_regardless_of_old_result_age():
    from verdict.doctor_diagnostics import census_runtime_section

    for finished_at in (
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) - timedelta(hours=2),
    ):
        row = census_runtime_section(
            {"version": "v", "last_result": {"finished_at": finished_at.isoformat()}}
        )
        assert row[1] == "note"
        assert "retired" in row[2]
        assert "verdict-census.timer" not in row[2]


def test_census_skips_routes_with_unexpired_negative_health(tmp_path):
    from verdict.census_passports import run_census
    from verdict.free_tier_admit import snapshot_from_payloads
    from verdict.prove_at_rest import ProbeExchange

    ids = ["bad/model-0", "bad/model-1", "good/model-0", "good/model-1"]
    snapshot = snapshot_from_payloads(
        catalog={"data": [{"id": model, "owned_by": model.split("/")[0]} for model in ids]},
        free_tier={
            "perModel": [
                {
                    "provider": model.split("/")[0],
                    "modelId": model.split("/")[1],
                    "freeType": "keyless",
                }
                for model in ids
            ]
        },
        providers={
            "connections": [
                {"provider": provider, "isActive": True, "testStatus": "active"}
                for provider in ("bad", "good")
            ]
        },
    )
    calls = []

    def probe(model, phase, timeout):
        calls.append((model, phase))
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    cache = HealthCache(tmp_path / "health.json")
    cache.record("bad/model-0", _bad_result(), NOW)
    cache.record("bad/model-1", _bad_result(), NOW)

    report = run_census(
        snapshot,
        probe,
        cache=cache,
        passport_store_path=tmp_path / "passports.json",
        now=NOW,
        cooling_providers=frozenset(),
    )
    assert {model for model, phase in calls} == {"good/model-0", "good/model-1"}
    assert report["probed"] == 2
    assert report["updated"] == 2


# ── BOD-387: keep worker-capable passports fresh across census ticks ─────────


def _census_snapshot(models):
    from verdict.free_tier_admit import snapshot_from_payloads

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


def _ok_transport(calls):
    from verdict.prove_at_rest import ProbeExchange

    def transport(model, phase, timeout):
        calls.append(model)
        return ProbeExchange(http_status=200, ok=True, chat_exact=True, tool_called=True)

    return transport


def test_passport_ttl_outlives_two_census_intervals():
    """OnUnitInactiveSec counts from run end, so one TTL must cover a late tick."""
    from verdict.census_passports_service import CENSUS_INTERVAL_SECONDS
    from verdict.model_passports import PASSPORT_TTL_SECONDS

    assert PASSPORT_TTL_SECONDS >= 2 * CENSUS_INTERVAL_SECONDS


def test_census_does_not_reprobe_a_passport_within_evidence_lifetime(tmp_path):
    from verdict.census_passports import passive_refresh, run_census
    from verdict.census_passports_service import CENSUS_INTERVAL_SECONDS

    route = "n/agnes"
    proved_at = NOW - timedelta(seconds=CENSUS_INTERVAL_SECONDS)
    cache = HealthCache(tmp_path / "health.json")
    cache.record(route, _free_result(), proved_at)
    store = tmp_path / "state.json"
    passive_refresh(cache, 1, store, proved_at)
    assert load_healthy_passports(store)[route].expires_at > NOW

    calls: list[str] = []
    run_census(
        _census_snapshot([route]),
        _ok_transport(calls),
        cache=cache,
        passport_store_path=store,
        now=NOW,
    )

    assert route not in calls


def test_census_spends_budget_on_previously_proven_routes_across_families(tmp_path):
    """Recently proven routes are kept alive first, one per family before seconds."""
    from verdict.census_passports import run_census

    proven = ["n/agnes", "n/laguna", "v/gemma"]
    unprobed = [f"a/x{i}" for i in range(6)]
    cache = HealthCache(tmp_path / "health.json")
    from verdict.orchestration.health_cache import EVIDENCE_FRESH_SECONDS

    for route in proven:
        cache.record(
            route, _free_result(), NOW - timedelta(seconds=EVIDENCE_FRESH_SECONDS + 12 * 60)
        )
    calls: list[str] = []
    result = run_census(
        _census_snapshot(proven + unprobed),
        _ok_transport(calls),
        cache=cache,
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        max_candidates=2,
        max_tokens=512,
    )

    assert result["probed"] == 2
    assert set(calls) == {"n/agnes", "v/gemma"}


def _proven_cache(tmp_path, routes):
    from verdict.orchestration.health_cache import EVIDENCE_FRESH_SECONDS

    cache = HealthCache(tmp_path / "health.json")
    for route in routes:
        # Outside fresh lifetime but within usable-stale grace: passport is due.
        cache.record(
            route, _free_result(), NOW - timedelta(seconds=EVIDENCE_FRESH_SECONDS + 12 * 60)
        )
    return cache


TEN_PROVEN = [f"{family}/m" for family in "abcdefgh"] + ["y/worker", "z/worker"]


def test_census_reserves_an_exploration_slot_for_a_never_proven_route(tmp_path):
    from verdict.census_passports import run_census

    calls: list[str] = []
    run_census(
        _census_snapshot([*TEN_PROVEN, "q/new"]),
        _ok_transport(calls),
        cache=_proven_cache(tmp_path, TEN_PROVEN),
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        max_candidates=6,
        max_tokens=1536,
    )

    # S05 contract caps: 4 routes, 2/provider (BOD-297/S05-A). The shared
    # coordinator's maintenance contract now bounds the actual probe count
    # below census's own ask.
    assert len(set(calls)) == 4
    assert "q/new" in calls


def test_census_probes_due_worker_capable_routes_before_other_proven_routes(tmp_path):
    from verdict.census_passports import run_census

    calls: list[str] = []
    run_census(
        _census_snapshot([*TEN_PROVEN, "q/new"]),
        _ok_transport(calls),
        cache=_proven_cache(tmp_path, TEN_PROVEN),
        passport_store_path=tmp_path / "state.json",
        now=NOW,
        max_candidates=6,
        max_tokens=1536,
        role_routes=frozenset({"y/worker", "z/worker"}),
    )

    assert {"y/worker", "z/worker"} <= set(calls)
    assert "q/new" in calls
