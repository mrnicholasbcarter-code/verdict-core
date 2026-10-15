"""Tests for BOD-350 census_passports passive refresh and census timer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.health_cache import FRESH_SECONDS, HealthCache, ProbeResult
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
    cache.record("gc/stale:free", _free_result(), NOW - timedelta(seconds=FRESH_SECONDS + 1))
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

    # record before FRESH_SECONDS ago → stale
    stale_time = NOW - timedelta(seconds=FRESH_SECONDS + 60)
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

    fake_module_dir = tmp_path / "a%b" / "verdict"
    fake_module_dir.mkdir(parents=True)
    monkeypatch.setattr(census_passports_service, "__file__", str(fake_module_dir / "mod.py"))

    report = census_passports_service.manage_census_service(
        unit_dir=tmp_path / "units", dry_run=True
    )
    service_body = report["files"]["verdict-census.service"]
    working_dir_line = next(
        line for line in service_body.splitlines() if line.startswith("WorkingDirectory=")
    )
    assert "%%" in working_dir_line
    assert "a%b" not in working_dir_line.replace("%%", "")
