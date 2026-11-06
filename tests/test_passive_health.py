"""Observed OmniRoute traffic refreshes the health cache as chat-only liveness."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from test_identify import FIXTURE

from verdict.omniroute_evidence import parse_omniroute_evidence
from verdict.orchestration.health_cache import (
    CATEGORY_PAYMENT,
    EVIDENCE_SOURCE_TRAFFIC,
    EVIDENCE_SOURCE_WEBHOOK,
    STATE_FRESH,
    STATE_NEGATIVE,
    HealthCache,
    ProbeResult,
    default_cache_path,
)
from verdict.passive_health import refresh_health_from_traffic

NOW = datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc)
SEEN = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Event:
    event: str
    timestamp: datetime
    provider: str | None
    model: str | None
    latency_ms: float | None = None


def payloads():
    data = json.loads(FIXTURE.read_text())
    data["v1_models"]["data"].append({"id": "b/model", "owned_by": "beta", "root": "model"})
    data["health_matrix"]["providers"].append(
        {
            "provider": "beta",
            "accounts": [{"models": [{"model": "model", "status": "healthy", "successes": 1}]}],
        }
    )
    return data


def evidence(data=None):
    return parse_omniroute_evidence(**(data or payloads()), now=NOW)


def matrix_rows(data, provider="alpha"):
    group = next(g for g in data["health_matrix"]["providers"] if g["provider"] == provider)
    return group["accounts"][0]["models"]


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VERDICT_HEALTH_CACHE", raising=False)
    return HealthCache(default_cache_path())


def test_traffic_records_fresh_chat_only_liveness_and_keeps_agentic(cache):
    cache.merge_and_save(
        lambda c: c.record(
            "a/best",
            ProbeResult(
                category="ok",
                chat_ok=True,
                tool_ok=True,
                http_status=200,
                probe_class="agentic",
                agentic_ok=True,
            ),
            SEEN - timedelta(hours=7),
        )
    )
    before = cache.entry("a/best")
    report = refresh_health_from_traffic(evidence(), cache, now=NOW)
    entry = HealthCache(cache.path).entry("a/best")
    assert report["recorded"] == 1
    assert report["tool_proof_granted"] == report["model_calls"] == report["gateway_writes"] == 0
    assert report["newest_observed_at"] == report["oldest_observed_at"] == "2026-10-20T12:00:00Z"
    assert entry.state_at(NOW) == STATE_FRESH and entry.checked_at == SEEN
    assert entry.chat_ok and not entry.tool_ok
    assert entry.evidence_source == EVIDENCE_SOURCE_TRAFFIC
    assert (entry.agentic_ok, entry.agentic_checked_at) == (
        before.agentic_ok,
        before.agentic_checked_at,
    )


def test_repeated_refresh_writes_nothing_new(cache):
    refresh_health_from_traffic(evidence(), cache, now=NOW)
    revision = HealthCache(cache.path).entry("a/best").write_revision
    report = refresh_health_from_traffic(evidence(), cache, now=NOW)
    assert report["recorded"] == 0
    assert report["skipped"]["skipped_newer_or_equal"] == 1
    assert HealthCache(cache.path).entry("a/best").write_revision == revision


def test_newer_negative_wins_over_older_traffic(cache):
    cache.merge_and_save(
        lambda c: c.record(
            "a/best",
            ProbeResult(category=CATEGORY_PAYMENT, chat_ok=False, tool_ok=False, http_status=402),
            SEEN + timedelta(minutes=10),
        )
    )
    report = refresh_health_from_traffic(evidence(), cache, now=NOW)
    assert report["recorded"] == 0
    assert report["skipped"]["skipped_newer_or_equal"] == 1
    assert HealthCache(cache.path).lookup("a/best", NOW).state == STATE_NEGATIVE


def test_expired_transient_failure_is_not_written(cache):
    data = payloads()
    matrix_rows(data)[1].update(lastStatus=503, lastRequestAt="2026-10-20T12:10:00Z")
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    assert report["recorded_failures"] == 0
    assert report["skipped"]["skipped_expired"] == 1


@pytest.mark.parametrize("source", ["health_matrix", "providers", "cooldowns"])
def test_source_error_writes_nothing(cache, source):
    data = payloads()
    data[source] = None
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    assert source in report["source_errors"]
    assert report["recorded"] == report["recorded_failures"] == 0
    assert not cache.path.exists()


@pytest.mark.parametrize(("status", "negative"), [(503, True), (402, True), (401, False)])
def test_availability_failures_write_negatives_and_auth_does_not(cache, status, negative):
    data = payloads()
    matrix_rows(data)[1].update(lastStatus=status, lastRequestAt="2026-10-20T12:29:30Z")
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    entry = HealthCache(cache.path).entry("a/not-entitled")
    assert report["recorded_failures"] == int(negative)
    if negative:
        assert entry.state_at(NOW) == STATE_NEGATIVE and entry.http_status == status
        assert entry.evidence_source == EVIDENCE_SOURCE_TRAFFIC
    else:
        assert entry is None


def test_latest_error_on_a_proven_route_blocks_liveness(cache):
    data = payloads()
    matrix_rows(data)[0].update(lastStatus=503, lastRequestAt="2026-10-20T12:29:30Z")
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    assert report["recorded"] == 0 and report["recorded_failures"] == 1
    assert HealthCache(cache.path).lookup("a/best", NOW).state == STATE_NEGATIVE


def test_cooled_and_unusable_provider_routes_are_not_recorded(cache):
    data = payloads()
    matrix_rows(data, "beta")[0]["lastRequestAt"] = "2026-10-20T12:00:00Z"
    data["cooldowns"]["items"].append({"model": "best", "provider": "alpha"})
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    assert report["recorded"] == 0
    assert report["skipped"]["skipped_cooled"] == 1
    assert report["skipped"]["skipped_provider_unusable"] == 1
    assert HealthCache(cache.path).routes() == {}


def test_traffic_without_observation_time_is_not_recorded(cache):
    data = payloads()
    del matrix_rows(data)[0]["lastRequestAt"]
    report = refresh_health_from_traffic(evidence(data), cache, now=NOW)
    assert report["recorded"] == 0 and report["skipped"]["skipped_unknown_age"] >= 1


def test_newer_webhook_event_is_used_and_unattributed_events_are_counted(cache):
    newer = SEEN + timedelta(minutes=20)
    events = [
        Event("request.completed", newer, "alpha", "best", 850.0),
        Event("request.completed", newer, None, "best"),
        Event("request.completed", newer, "alpha", "missing-model"),
        Event("request.failed", newer, "alpha", "best"),
    ]
    report = refresh_health_from_traffic(evidence(), cache, now=NOW, webhook_events=events)
    entry = HealthCache(cache.path).entry("a/best")
    assert report["recorded"] == report["webhook_events_used"] == 1
    assert report["webhook_events_unattributed"] == 2
    assert entry.checked_at == newer and entry.latency_ms == 850.0
    assert entry.evidence_source == EVIDENCE_SOURCE_WEBHOOK and not entry.tool_ok


def test_older_webhook_event_does_not_replace_matrix_row(cache):
    events = [Event("request.completed", SEEN - timedelta(minutes=5), "alpha", "a/best")]
    report = refresh_health_from_traffic(evidence(), cache, now=NOW, webhook_events=events)
    assert report["webhook_events_used"] == 0
    assert HealthCache(cache.path).entry("a/best").evidence_source == EVIDENCE_SOURCE_TRAFFIC


def test_doctor_sees_fresh_route_after_refresh(cache):
    from verdict.doctor_diagnostics import health_cache_row

    refresh_health_from_traffic(evidence(), cache, now=NOW)
    row = health_cache_row(now=NOW)
    assert row["status"] == "ok" and row["detail"].startswith("1 fresh")


def _passive_cli(monkeypatch, data):
    from verdict import identify

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(identify, "datetime", Clock)
    monkeypatch.setattr(
        identify, "resolve_gateway_binding", lambda: ("http://127.0.0.1:20128", None)
    )
    monkeypatch.setattr(identify, "fetch_omniroute_evidence", lambda *a, **kw: evidence(data))
    monkeypatch.setattr(identify, "load_store", lambda: None)


def test_passive_cli_refresh_writes_only_with_the_flag(cache, monkeypatch, capsys):
    from verdict import cli

    _passive_cli(monkeypatch, payloads())
    monkeypatch.setattr("sys.argv", ["verdict", "identify", "--passive", "--json"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert "health_refresh" not in report and not cache.path.exists()

    monkeypatch.setattr(
        "sys.argv", ["verdict", "identify", "--passive", "--refresh-health-cache", "--json"]
    )
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "passive" and report["model_calls"] == 0
    assert report["health_refresh"]["recorded"] == 1
    assert HealthCache(cache.path).lookup("a/best", NOW).state == STATE_FRESH

    monkeypatch.setattr("sys.argv", ["verdict", "identify", "--passive", "--refresh-health-cache"])
    cli.main()
    assert "Health cache refresh: 0 liveness, 0 negatives" in capsys.readouterr().out


def test_refresh_health_cache_rejects_screen(monkeypatch):
    from verdict import cli

    monkeypatch.setattr(
        "sys.argv",
        ["verdict", "identify", "--screen", "--allow-live-probe", "--refresh-health-cache"],
    )
    with pytest.raises(SystemExit, match="requires --passive"):
        cli.main()


def test_register_webhook_command_needs_a_management_token(monkeypatch, capsys):
    from verdict import cli, credentials_store, omniroute_webhook

    calls = []
    monkeypatch.setattr(
        credentials_store, "resolve_gateway_binding", lambda: ("http://127.0.0.1:20128/v1", None)
    )
    monkeypatch.setattr(credentials_store, "resolve_management_token", lambda: None)
    monkeypatch.setattr(
        omniroute_webhook, "register_webhook", lambda **kw: calls.append(kw) or {"status": "ok"}
    )
    monkeypatch.setattr("sys.argv", ["verdict", "identify", "register-webhook", "--dry-run"])
    with pytest.raises(SystemExit, match="management token"):
        cli.main()
    assert calls == []

    monkeypatch.setattr(credentials_store, "resolve_management_token", lambda: "token-fixture")
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}
    assert calls == [
        {
            "origin": "http://127.0.0.1:20128",
            "management_token": "token-fixture",
            "url": f"http://127.0.0.1:8000{omniroute_webhook.RECEIVER_PATH}",
            "dry_run": True,
        }
    ]
