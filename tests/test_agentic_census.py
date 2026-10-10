"""Offline regressions for trusted agentic evidence and user service setup."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.orchestration.session_evidence import import_worker_outcomes

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("VERDICT_SKIP_SETUP", "1")


def worker_file(tmp_path, rows):
    path = tmp_path / "workers.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def row(outcome="SESSION_CANARY_PASS", **extra):
    return dict(
        route="kc/model:free",
        kind="session_canary_v1",
        child="sub-123",
        ts=NOW.isoformat(),
        outcome=outcome,
        **extra,
    )


def test_import_sessions_exact_route_provenance_and_idempotence(tmp_path):
    from verdict.prove_at_rest import import_sessions

    source = worker_file(tmp_path, [row()])
    cache = HealthCache(tmp_path / "health.json")
    assert import_sessions(source, cache, now=NOW)["updated"] == 1
    entry = HealthCache(cache.path).entry("kc/model:free")
    assert entry.agentic_ok and entry.agentic_checked_at == NOW
    assert entry.agentic_source == str(source)
    assert entry.agentic_child_id == "sub-123"
    assert HealthCache(cache.path).entry("openrouter/model:free") is None
    revision = entry.write_revision
    assert import_sessions(source, cache, now=NOW)["updated"] == 0
    assert HealthCache(cache.path).entry(entry.route_id).write_revision == revision


@pytest.mark.parametrize("failure", ["FALSE_CLAIM", "SESSION_FAIL", "SESSION_CANARY_FAIL"])
def test_session_failure_revokes_qualification_without_rewriting_health(tmp_path, failure):
    from verdict.prove_at_rest import import_sessions

    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "kc/model:free",
        ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=True),
        NOW - timedelta(seconds=1),
    )
    cache.save()
    original = cache.entry("kc/model:free")
    import_sessions(worker_file(tmp_path, [row(failure)]), cache, now=NOW)
    entry = HealthCache(cache.path).entry("kc/model:free")
    assert not entry.agentic_ok
    assert entry.checked_at == original.checked_at
    assert entry.healthy == original.healthy


def test_import_does_not_replace_newer_evidence_or_accept_future(tmp_path):
    from verdict.prove_at_rest import import_sessions

    cache = HealthCache(tmp_path / "health.json")
    cache.record(
        "kc/model:free",
        ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=True),
        NOW + timedelta(seconds=1),
    )
    cache.save()
    source = worker_file(
        tmp_path,
        [
            row("SESSION_FAIL"),
            {**row(), "route": "future/model", "ts": (NOW + timedelta(days=1)).isoformat()},
        ],
    )
    assert import_sessions(source, cache, now=NOW)["updated"] == 0
    assert cache.entry("kc/model:free").agentic_ok
    assert cache.entry("future/model") is None


def test_importer_keeps_child_and_recognizes_failures(tmp_path):
    source = worker_file(
        tmp_path,
        [
            row("SESSION_FAIL"),
            {**row("REAL_TASK_PASS"), "kind": "free_real_task", "route": "free/model"},
        ],
    )
    items = import_worker_outcomes(source)
    assert [item.outcome for item in items] == ["fail", "pass"]
    assert all(item.child_id == "sub-123" for item in items)


def test_import_sessions_cli(tmp_path, capsys, monkeypatch):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    source = worker_file(tmp_path, [row()])
    path = tmp_path / "cache.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "prove-at-rest",
            "import-sessions",
            str(source),
            "--state-path",
            str(path),
            "--json",
        ],
    )
    main()
    assert json.loads(capsys.readouterr().out)["updated"] == 1
    assert HealthCache(path).entry("kc/model:free").agentic_ok


def test_service_dry_run_idempotence_and_uninstall(tmp_path):
    from verdict.prove_at_rest_service import manage_service

    units = tmp_path / "units"
    calls = []

    def run(args):
        calls.append(args)

    report = manage_service(unit_dir=units, interval=90, max_requests=12, dry_run=True, run=run)
    assert not units.exists() and not calls
    service = report["files"]["verdict-prove-at-rest.service"]
    assert "prove-at-rest daemon --allow-live-probe" in service
    assert "--interval 90" in service and "--max-requests 12" in service
    assert "--max-wall-seconds 600" in service
    assert "verdict-prove-at-rest.service" in report["files"]["verdict-prove-at-rest.timer"]
    first = manage_service(unit_dir=units, interval=90, max_requests=12, run=run)
    assert first["changed"]
    second = manage_service(unit_dir=units, interval=90, max_requests=12, run=run)
    assert not second["changed"]
    assert any("enable" in call and "--now" in call for call in calls)
    manage_service(unit_dir=units, uninstall=True, run=run)
    assert not list(units.iterdir())
    manage_service(unit_dir=units, uninstall=True, run=run)


@pytest.mark.parametrize(
    "options", [{"interval": 0}, {"max_requests": 0}, {"interval": float("nan")}]
)
def test_service_rejects_unbounded_options_before_writes(tmp_path, options):
    from verdict.prove_at_rest_service import manage_service

    with pytest.raises(ValueError):
        manage_service(unit_dir=tmp_path / "units", **options)
    assert not (tmp_path / "units").exists()


def test_service_cli_dry_run_json(tmp_path, monkeypatch, capsys):
    from verdict.cli import main

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "sys.argv",
        [
            "verdict",
            "prove-at-rest",
            "install-service",
            "--interval",
            "45",
            "--max-requests",
            "8",
            "--dry-run",
            "--json",
        ],
    )
    main()
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] and report["files"]
    assert not (tmp_path / "systemd").exists()


def test_full_cache_eligibility_counts_not_refresh_sample(tmp_path):
    from verdict.orchestration.eligibility_report import eligibility_payload
    from verdict.prove_at_rest import status_report

    cache = HealthCache(tmp_path / "health.json")
    for i in range(45):
        cache.record(
            f"free/m{i}",
            ProbeResult(
                "ok", True, True, capacity_evidence="free", probe_class="agentic", agentic_ok=True
            ),
            NOW,
        )
    cache.record(
        "sub/m",
        ProbeResult(
            "ok",
            True,
            True,
            capacity_evidence="subscription",
            probe_class="agentic",
            agentic_ok=True,
        ),
        NOW,
    )
    cache.record(
        "stale/m",
        ProbeResult(
            "ok", True, True, capacity_evidence="free", probe_class="agentic", agentic_ok=True
        ),
        NOW - timedelta(minutes=11),
    )
    cache.record("bad/m", ProbeResult("timeout", False, False), NOW)
    report = status_report(cache, now=NOW)
    assert report["counts_by_state"] == {"fresh": 46, "stale": 1, "negative": 1, "unprobed": 0}
    assert report["agentic_qualified_by_capacity_class"] == {
        "free": 45,
        "subscription": 1,
        "metered": 0,
        "unknown": 0,
    }
    payload = eligibility_payload([], {}, None, {}, health_cache=cache, now=NOW)
    assert payload["health_cache"]["route_count"] == 48
    assert payload["health_cache"]["counts_by_state"] == report["counts_by_state"]
    from verdict.orchestration.cli import render_eligibility_text

    assert "free=45" in render_eligibility_text(payload)


def test_liveness_refresh_preserves_session_qualification_timestamp(tmp_path):
    from verdict.prove_at_rest import import_sessions

    cache = HealthCache(tmp_path / "health.json")
    import_sessions(worker_file(tmp_path, [row()]), cache, now=NOW)
    cache.record_liveness(
        "kc/model:free",
        latency_ms=3,
        pool="kc",
        capacity_evidence="free",
        now=NOW + timedelta(seconds=10),
    )
    cache.save()
    entry = HealthCache(cache.path).entry("kc/model:free")
    assert entry.agentic_checked_at == NOW
    assert entry.agentic_source and entry.agentic_child_id == "sub-123"


@pytest.mark.parametrize(
    "age,live_age,failure,eligible",
    [(2, 0, False, True), (2, 11, False, False), (2, 0, True, False), (8, 0, False, False)],
)
def test_session_capability_and_liveness_are_separate(tmp_path, age, live_age, failure, eligible):
    from tests.test_orch_eligibility import REQ, conn, make_ladder
    from tests.test_orch_eligibility import row as model_row
    from verdict.prove_at_rest import import_sessions

    route = "gl/glm-5"
    source = worker_file(
        tmp_path, [{**row(), "route": route, "ts": (NOW - timedelta(days=age)).isoformat()}]
    )
    cache = HealthCache(tmp_path / "health.json")
    import_sessions(source, cache, now=NOW)
    if failure:
        source.write_text(json.dumps({**row("FALSE_CLAIM"), "route": route}) + "\n")
        import_sessions(source, cache, now=NOW)
    cache.record(route, ProbeResult("ok", True, True), NOW - timedelta(minutes=live_age))
    ladder, probe = make_ladder(
        tmp_path,
        [model_row(route, owned_by="glm")],
        [conn("glm", auth="apikey", plan="free", free_only=True)],
        health_cache=cache,
    )
    # Read-only evaluation cannot silently refresh stale liveness.
    verdict = ladder.evaluate(REQ, now=NOW)[0]
    assert (verdict.reached is not None and verdict.reached.value == "TASK_ELIGIBLE") == eligible
    assert not probe.calls
    evidence = verdict.to_dict()["agentic_capability"]
    assert evidence["source"] == "session_evidence"
    assert evidence["child_id"] == "sub-123"
    assert evidence["checked_at"]
    if eligible:
        assert ladder.select(REQ, now=NOW)[0] is not None


def test_session_capability_ttl_is_configurable(tmp_path, monkeypatch):
    from verdict.orchestration.health_cache import agentic_capability
    from verdict.prove_at_rest import import_sessions

    source = worker_file(tmp_path, [{**row(), "ts": (NOW - timedelta(days=2)).isoformat()}])
    cache = HealthCache(tmp_path / "health.json")
    import_sessions(source, cache, now=NOW)
    assert agentic_capability(cache.entry("kc/model:free"), NOW)["qualified"]
    monkeypatch.setenv("VERDICT_AGENTIC_CAPABILITY_TTL_S", "86400")
    assert not agentic_capability(cache.entry("kc/model:free"), NOW)["qualified"]


def test_session_capability_survives_failed_liveness_but_does_not_authorize_health(tmp_path):
    from verdict.orchestration.health_cache import agentic_capability
    from verdict.prove_at_rest import import_sessions

    cache = HealthCache(tmp_path / "health.json")
    import_sessions(worker_file(tmp_path, [row()]), cache, now=NOW)
    cache.record("kc/model:free", ProbeResult("timeout", False, False), NOW + timedelta(seconds=1))
    cache.save()
    entry = HealthCache(cache.path).entry("kc/model:free")
    assert not entry.healthy
    assert agentic_capability(entry, NOW + timedelta(seconds=1))["qualified"]


def test_newer_agentic_probe_failure_revokes_session_capability(tmp_path):
    from verdict.orchestration.health_cache import agentic_capability
    from verdict.prove_at_rest import import_sessions

    route = "kc/model:free"
    source = worker_file(tmp_path, [{**row(), "ts": (NOW - timedelta(days=2)).isoformat()}])
    cache = HealthCache(tmp_path / "health.json")
    import_sessions(source, cache, now=NOW)
    assert agentic_capability(cache.entry(route), NOW)["qualified"] is True
    cache.record(
        route,
        ProbeResult("ok", True, True, probe_class="agentic", agentic_ok=False),
        NOW - timedelta(minutes=1),
    )
    capability = agentic_capability(cache.entry(route), NOW)
    assert capability["qualified"] is False
    # Durable: a later single-call healthy probe must not resurrect it (review).
    cache.record(route, ProbeResult("ok", True, True), NOW - timedelta(seconds=30))
    cache.save()
    reloaded = HealthCache(cache.path).entry(route)
    assert agentic_capability(reloaded, NOW)["qualified"] is False
    # Provenance names the revoking probe, not the old session child.
    assert reloaded.agentic_source == "agentic_probe"
    assert reloaded.agentic_child_id is None


def test_agentic_transport_failure_does_not_revoke_session_capability(tmp_path):
    from verdict.orchestration.health_cache import agentic_capability
    from verdict.prove_at_rest import import_sessions

    route = "kc/model:free"
    source = worker_file(tmp_path, [{**row(), "ts": (NOW - timedelta(days=2)).isoformat()}])
    cache = HealthCache(tmp_path / "health.json")
    import_sessions(source, cache, now=NOW)
    cache.record(
        route,
        ProbeResult("rate_limited", False, False, probe_class="agentic", agentic_ok=False),
        NOW - timedelta(minutes=1),
    )
    assert agentic_capability(cache.entry(route), NOW)["qualified"] is True


@pytest.mark.parametrize("value", ["0", "-5", "nan", "not-a-number"])
def test_invalid_capability_ttl_fails_closed(tmp_path, monkeypatch, value):
    from verdict.orchestration.health_cache import agentic_capability
    from verdict.prove_at_rest import import_sessions

    monkeypatch.setenv("VERDICT_AGENTIC_CAPABILITY_TTL_S", value)
    source = worker_file(tmp_path, [{**row(), "ts": (NOW - timedelta(seconds=30)).isoformat()}])
    cache = HealthCache(tmp_path / "health.json")
    import_sessions(source, cache, now=NOW)
    assert agentic_capability(cache.entry("kc/model:free"), NOW)["qualified"] is False
