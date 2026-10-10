"""Offline regressions for trusted agentic evidence and user service setup."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from verdict.orchestration.health_cache import HealthCache, ProbeResult
from verdict.orchestration.session_evidence import import_worker_outcomes

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)


def worker_file(tmp_path, rows):
    path = tmp_path / "workers.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


def row(outcome="SESSION_CANARY_PASS", **extra):
    return dict(route="kc/model:free", kind="session_canary_v1", child="sub-123",
                ts=NOW.isoformat(), outcome=outcome, **extra)


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
    cache.record("kc/model:free", ProbeResult("ok", True, True,
                 probe_class="agentic", agentic_ok=True), NOW - timedelta(seconds=1))
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
    cache.record("kc/model:free", ProbeResult("ok", True, True,
                 probe_class="agentic", agentic_ok=True), NOW + timedelta(seconds=1))
    cache.save()
    source = worker_file(tmp_path, [row("SESSION_FAIL"),
        {**row(), "route": "future/model", "ts": (NOW + timedelta(days=1)).isoformat()}])
    assert import_sessions(source, cache, now=NOW)["updated"] == 0
    assert cache.entry("kc/model:free").agentic_ok
    assert cache.entry("future/model") is None


def test_importer_keeps_child_and_recognizes_failures(tmp_path):
    source = worker_file(tmp_path, [row("SESSION_FAIL"),
        {**row("REAL_TASK_PASS"), "kind": "free_real_task", "route": "free/model"}])
    items = import_worker_outcomes(source)
    assert [item.outcome for item in items] == ["fail", "pass"]
    assert all(item.child_id == "sub-123" for item in items)


def test_import_sessions_cli(tmp_path, capsys, monkeypatch):
    from verdict.cli import main

    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    source = worker_file(tmp_path, [row()])
    path = tmp_path / "cache.json"
    main(["prove-at-rest", "import-sessions", str(source), "--state-path", str(path), "--json"])
    assert json.loads(capsys.readouterr().out)["updated"] == 1
    assert HealthCache(path).entry("kc/model:free").agentic_ok
