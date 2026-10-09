"""BOD-300: real eligibility selection reads probe and session evidence."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import conn, row
from verdict.actions.registry import _action_eligibility
from verdict.orchestration import eligibility_report
from verdict.orchestration import run as orch_run
from verdict.orchestration.health_cache import CATEGORY_OK, HealthCache, ProbeResult
from verdict.orchestration.session_evidence import SessionLedger, SessionOutcome
from verdict.subagent_selection import HealthResult

ROUTE = "gl/glm-5"


@pytest.fixture
def evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[HealthCache, SessionLedger]:
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("VERDICT_HEALTH_CACHE", str(tmp_path / "cache.json"))
    monkeypatch.setenv("VERDICT_SESSION_EVIDENCE", str(tmp_path / "sessions.jsonl"))
    rows = [row(ROUTE, owned_by="glm", pricing={"input": 0.0, "output": 0.0})]
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda *a, **kw: rows)
    monkeypatch.setattr(
        orch_run,
        "fetch_connections",
        lambda *a, **kw: [conn("glm", auth="apikey", plan="free", free_only=True)],
    )
    import verdict.subagent_selection as selection

    monkeypatch.setattr(
        selection, "openai_health_probe", lambda *a, **kw: lambda _: HealthResult(True, "healthy")
    )
    return HealthCache(tmp_path / "cache.json"), SessionLedger(tmp_path / "sessions.jsonl")


def qualify(cache: HealthCache) -> None:
    cache.record(
        ROUTE,
        ProbeResult(CATEGORY_OK, True, True, probe_class="agentic", agentic_ok=True),
        datetime.now(timezone.utc),
    )
    cache.save()


def select() -> dict[str, Any]:
    return _action_eligibility(
        gateway="http://127.0.0.1:1", probe=True, sync_visibility=True, refresh_hook=lambda *a: {}
    ).data


def test_real_action_selects_agentic_free_route_read_only(
    evidence: tuple[HealthCache, SessionLedger],
) -> None:
    cache, ledger = evidence
    qualify(cache)
    ledger.append(
        SessionOutcome(
            ROUTE, "pass", "implement", None, True, datetime.now(timezone.utc), "controller"
        )
    )
    before = (cache.path.read_bytes(), ledger.path.read_bytes())
    payload = select()
    assert payload["selected"]["route_id"] == ROUTE
    assert payload["selected"]["rank_components"]["session_passes"] == 1
    assert before == (cache.path.read_bytes(), ledger.path.read_bytes())


def test_missing_cache_is_empty_not_detached(evidence: tuple[HealthCache, SessionLedger]) -> None:
    cache, ledger = evidence
    payload = select()
    assert payload["selected"] is None
    assert payload["verdicts"][0]["reason"] == "no_agentic_probe"
    assert not cache.path.exists()
    assert not ledger.path.exists()


def test_build_selector_injects_read_only_evidence(
    evidence: tuple[HealthCache, SessionLedger],
) -> None:
    cache, ledger = evidence
    ladder = eligibility_report.build_selector(
        "http://127.0.0.1:1",
        scope="",
        prefer="",
        capacity=("free",),
        health_cache=cache,
        session_ledger=ledger,
    )
    assert ladder._health_cache is cache
    assert ladder._session_ledger is ledger
    assert not cache.path.exists()
    assert not ledger.path.exists()
