"""Doctor reports the passive health source: cache rows by evidence source and the webhook row."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import doctor_diagnostics as dd
from verdict.orchestration.health_cache import (
    CATEGORY_OK,
    EVIDENCE_SOURCE_TRAFFIC,
    HealthCache,
    ProbeResult,
)

NOW = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def cache_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / ".verdict"))
    path = tmp_path / "health-cache.json"
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(path))
    return path


def _status(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    status = {
        "configured": False,
        "webhook_id": None,
        "url": None,
        "registration": None,
        "last_event_at": None,
        "events_total": 0,
        "receiver_path": "/v1/omniroute/webhook",
        **values,
    }
    monkeypatch.setattr("verdict.omniroute_webhook.webhook_status", lambda **_: status)


def test_empty_cache_fails_naming_the_passive_source(cache_path: Path) -> None:
    HealthCache(cache_path).save()
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "failed"
    assert (
        "verdict-identify.service (OmniRoute health matrix) last refreshed never" in row["detail"]
    )
    assert "repair: systemctl --user start verdict-identify.service" in row["detail"]


def test_failure_names_the_last_passive_refresh_age(cache_path: Path) -> None:
    from verdict.identify_service import latest_report_path

    HealthCache(cache_path).save()
    latest_report_path().parent.mkdir(parents=True)
    latest_report_path().write_text(
        json.dumps({"health_refresh": {"refreshed_at": (NOW - timedelta(hours=1)).isoformat()}})
    )
    assert "last refreshed 3600s ago" in dd.health_cache_row(now=NOW)["detail"]


def test_passive_entries_count_by_source(cache_path: Path) -> None:
    cache = HealthCache(cache_path)
    observed = NOW - timedelta(minutes=10)
    for route in ("a/one", "b/two"):
        outcome = cache.record_passive_traffic(
            route, observed_at=observed, now=NOW, source=EVIDENCE_SOURCE_TRAFFIC
        )
        assert outcome == "recorded"
    cache.record(
        "c/probe",
        ProbeResult(category=CATEGORY_OK, chat_ok=True, tool_ok=True, http_status=200),
        NOW - timedelta(minutes=5),
    )
    cache.save()
    row = dd.health_cache_row(now=NOW)
    assert row["status"] == "ok"
    assert row["detail"] == (
        "3 fresh, 0 stale route(s); passive traffic 2 (newest age 600s); "
        "webhook 0; probes 1 (newest age 300s)"
    )


def test_webhook_row_warns_with_registration_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch, registration={"status": "failed", "reason": "blocked_private"})
    row = dd.omniroute_webhook_row(now=NOW)
    assert row["status"] == "warning"
    assert "blocked_private" in row["detail"]
    assert "repair: verdict identify register-webhook" in row["detail"]


def test_webhook_row_warns_when_never_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch)
    assert dd.omniroute_webhook_row(now=NOW)["status"] == "warning"


@pytest.mark.parametrize(
    "last_event,expected",
    [(None, "warning"), (timedelta(hours=7), "warning"), (timedelta(0), "ok")],
)
def test_registered_webhook_needs_a_recent_event(
    monkeypatch: pytest.MonkeyPatch, last_event: timedelta | None, expected: str
) -> None:
    _status(
        monkeypatch,
        configured=True,
        url="https://verdict.example/v1/omniroute/webhook",
        last_event_at=None if last_event is None else (NOW - last_event).isoformat(),
        events_total=3,
    )
    assert dd.omniroute_webhook_row(now=NOW)["status"] == expected


def test_webhook_row_is_an_installed_service_row(monkeypatch: pytest.MonkeyPatch) -> None:
    _status(monkeypatch)
    rows = dd.installed_service_rows(now=NOW, timer_state=lambda unit: {"state": "active"})
    row = next(r for r in rows if r["label"] == "OmniRoute webhook")
    assert row["status"] == "warning"
