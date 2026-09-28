"""Orchestration ladder / build_selector consume the canonical admitted set."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import NOW, REQ, FakeProbe, conn, row
from verdict.admission import (
    AdmissionBypassError,
    AdmissionStage,
    RuntimeEvidence,
    RuntimeObservation,
    admit,
)
from verdict.orchestration import cli as orch_cli
from verdict.orchestration import run as orch_run
from verdict.orchestration.contracts import EligibilityStage
from verdict.orchestration.eligibility import LADDER_CONFIRMATION_SOURCE, EligibilityLadder
from verdict.subagent_selection import HealthResult

DEAD = "cc/dead"
OK = "kr/claude-sonnet-ok"
CTRL = "kr/claude-controller"


def _rows() -> list[dict[str, Any]]:
    return [row(DEAD, owned_by="cc"), row(OK, owned_by="kr"), row(CTRL, owned_by="kr")]


def _conns() -> list[dict[str, Any]]:
    return [conn("cc"), conn("kr")]


def _exhausted() -> RuntimeEvidence:
    return RuntimeEvidence(
        (
            RuntimeObservation(
                f"route:{DEAD}", "exhausted", "quota_exhausted", "fixture:live", NOW.isoformat()
            ),
        ),
        ("fixture:live",),
    )


def test_ladder_rejects_admission_excluded_route_before_probe(tmp_path: Path) -> None:
    admitted = admit(_rows(), _conns(), _exhausted(), now=NOW)
    probe = FakeProbe()
    ladder = EligibilityLadder(
        [row(DEAD, owned_by="cc")],
        _conns(),
        probe,
        tmp_path / "state.json",
        harness_visible=lambda _r: True,
        admitted=admitted,
    )
    selected, verdicts = ladder.select(REQ, now=NOW)
    assert selected is None
    assert probe.calls == []
    assert verdicts[0].failed_stage is EligibilityStage.AVAILABLE
    assert verdicts[0].reason.startswith("admission:")


def test_ladder_without_admission_would_select_dead_route(tmp_path: Path) -> None:
    probe = FakeProbe()
    ladder = EligibilityLadder(
        [row(DEAD, owned_by="cc")],
        _conns(),
        probe,
        tmp_path / "s.json",
        harness_visible=lambda _r: True,
    )
    selected, _ = ladder.select(REQ, now=NOW)
    assert selected is not None and selected.route_id == DEAD


def test_ladder_bypass_guard_raises_if_admitted_set_is_swapped(tmp_path: Path) -> None:
    admitted = admit(_rows(), _conns(), _exhausted(), now=NOW)
    ladder = EligibilityLadder(
        [row(DEAD, owned_by="cc")],
        _conns(),
        FakeProbe(),
        tmp_path / "s.json",
        harness_visible=lambda _r: True,
        admitted=admit(_rows(), _conns(), RuntimeEvidence(), now=NOW),
    )
    ladder._admitted = admitted  # simulate a downstream swap after assessment
    ladder._assess_all = lambda req, now: (  # type: ignore[method-assign]
        EligibilityLadder._assess_all(
            EligibilityLadder(
                [row(DEAD, owned_by="cc")],
                _conns(),
                FakeProbe(),
                tmp_path / "t.json",
                harness_visible=lambda _r: True,
            ),
            req,
            now,
        )
    )
    with pytest.raises(AdmissionBypassError):
        ladder.select(REQ, now=NOW)


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gw, *, api_key, timeout=30: _rows())
    monkeypatch.setattr(orch_run, "fetch_connections", lambda gw, *, api_key, timeout=30: _conns())
    monkeypatch.setattr(orch_run, "resolve_api_key", lambda *a, **k: None)
    state = tmp_path / "orchestration-health.json"
    until = (NOW + timedelta(days=3650)).isoformat()
    state.write_text(
        json.dumps(
            {
                "health": {},
                "cooldowns": {f"route:{DEAD}": {"until": until, "category": "quota_exhausted"}},
            }
        )
    )
    return state


def test_build_selector_admits_before_scope_and_excludes_active_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch_gateway(monkeypatch, tmp_path)
    monkeypatch.setenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", CTRL)
    probe_calls: list[str] = []
    monkeypatch.setattr(
        "verdict.subagent_selection.openai_health_probe",
        lambda *a, **k: lambda c: probe_calls.append(c.route_id) or pytest.fail("no live probe"),
    )
    ladder = orch_cli.build_selector("http://127.0.0.1:1", scope="", prefer="kr", state_file=state)
    admitted = ladder.admitted
    assert admitted is not None
    assert admitted.ids == frozenset({OK})
    assert admitted.first_failure(DEAD).first_failed_stage is AdmissionStage.AVAILABLE
    assert admitted.first_failure(CTRL).first_failed_stage is AdmissionStage.CONTROLLER_EXCLUDED
    verdicts = {v.route_id: v for v in ladder.evaluate(REQ, now=NOW)}
    assert verdicts[DEAD].failed_stage is not None
    assert verdicts[CTRL].failed_stage is not None
    receipt = json.loads((tmp_path / "admission-latest.json").read_text())
    assert receipt["controller_identity"] == CTRL
    dead = next(c for c in receipt["candidates"] if c["route_id"] == DEAD)
    assert dead["first_failed_stage"] == "AVAILABLE" and dead["source"].startswith("ladder_state")
    assert probe_calls == []


def test_build_selector_scope_drop_reasons_are_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch_gateway(monkeypatch, tmp_path)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    ladder = orch_cli.build_selector("http://127.0.0.1:1", scope="cc/", prefer="", state_file=state)
    admitted = ladder.admitted
    assert admitted is not None and admitted.ids == frozenset()
    # Scope narrowing cannot re-admit the live-exhausted route.
    assert admitted.first_failure(DEAD).first_failed_stage is AdmissionStage.AVAILABLE
    assert admitted.first_failure(OK).first_failed_stage is AdmissionStage.WORKER_SCOPE
    assert admitted.receipt()["controller_identity"] == "unknown"


def test_build_selector_records_capability_drops_in_canonical_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _patch_gateway(monkeypatch, tmp_path)
    rows = [*_rows(), row("kr/no-tools", owned_by="kr", tools=False)]
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gw, *, api_key, timeout=30: rows)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    ladder = orch_cli.build_selector(
        "http://127.0.0.1:1",
        scope="",
        prefer="",
        state_file=state,
        required_capabilities=frozenset({"tools"}),
        min_context_tokens=32_000,
    )
    admitted = ladder.admitted
    assert admitted is not None and "kr/no-tools" not in admitted
    record = admitted.first_failure("kr/no-tools")
    assert record.first_failed_stage is AdmissionStage.CAPABILITY
    assert record.reason == "missing_capability:tools"
    receipt = json.loads((tmp_path / "admission-latest.json").read_text())
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id["kr/no-tools"]["first_failed_stage"] == "CAPABILITY"
    # Later narrowing still applies as defence in depth.
    verdicts = {v.route_id: v for v in ladder.evaluate(REQ, now=NOW)}
    assert verdicts["kr/no-tools"].failed_stage is not None


def test_eligibility_command_passes_task_requirements_into_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    captured: dict[str, Any] = {}

    class StopError(Exception):
        pass

    def fake_build(gateway: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise StopError

    import verdict.orchestration.eligibility_report as elig_report
    monkeypatch.setattr(elig_report, "build_selector", fake_build)
    args = argparse.Namespace(
        gateway="http://127.0.0.1:1",
        scope="",
        prefer="",
        provider_family=[],
        reasoning=False,
        frontier=False,
        probe=False,
        json=True,
    )
    result_code = orch_cli._eligibility(args)
    # run_action catches the StopError, so we check the result and captured params
    assert result_code == 0  # JSON output path returns 0
    assert captured["required_capabilities"] == frozenset({"tools"})
    assert captured["min_context_tokens"] == 32_000


def test_build_selector_reads_evidence_from_the_state_file_it_uses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # --state-file / --inject shape: the ladder reads chaos-health.json, so
    # admission must read the same file, not only orchestration-health.json.
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gw, *, api_key, timeout=30: _rows())
    monkeypatch.setattr(orch_run, "fetch_connections", lambda gw, *, api_key, timeout=30: _conns())
    monkeypatch.setattr(orch_run, "resolve_api_key", lambda *a, **k: None)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    now = datetime.now(timezone.utc)
    until = (now + timedelta(days=1)).isoformat()
    state = tmp_path / "run" / "chaos-health.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "health": {OK: {"healthy": True, "checked_at": now.isoformat(), "category": ""}},
                "cooldowns": {f"route:{DEAD}": {"until": until, "category": "quota_exhausted"}},
            }
        )
    )
    ladder = orch_cli.build_selector("http://127.0.0.1:1", scope="", prefer="kr", state_file=state)
    admitted = ladder.admitted
    assert admitted is not None
    assert "ladder_state:chaos-health.json" in admitted.runtime_consulted
    dead = admitted.first_failure(DEAD)
    assert dead.first_failed_stage is AdmissionStage.AVAILABLE
    assert dead.source == "ladder_state:chaos-health.json"
    assert not admitted.proven_healthy(OK)
    assert not admitted.launchable(OK)
    assert admitted.launch_authority(OK)["basis"] == "none"
    receipt = json.loads((state.parent / "admission-latest.json").read_text())
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id[DEAD]["source"] == "ladder_state:chaos-health.json"


def test_ladder_confirmations_are_persisted_to_admission_latest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(orch_run, "fetch_inventory", lambda gw, *, api_key, timeout=30: _rows())
    monkeypatch.setattr(orch_run, "fetch_connections", lambda gw, *, api_key, timeout=30: _conns())
    monkeypatch.setattr(orch_run, "resolve_api_key", lambda *a, **k: None)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    probed: list[str] = []

    def fake_probe(*a: Any, **k: Any) -> Any:
        def run(candidate: Any) -> HealthResult:
            probed.append(candidate.route_id)
            ok = candidate.route_id != DEAD
            return HealthResult(ok, "" if ok else "rate_limited")

        return run

    monkeypatch.setattr("verdict.subagent_selection.openai_health_probe", fake_probe)
    state = tmp_path / "orchestration-health.json"  # fresh host: absent
    ladder = orch_cli.build_selector("http://127.0.0.1:1", scope="", prefer="cc", state_file=state)
    ladder._harness_visible = lambda _r: True
    chosen, _ = ladder.select(REQ, now=datetime.now(timezone.utc))
    assert chosen is not None and chosen.route_id != DEAD
    assert probed[0] == DEAD
    receipt = json.loads((tmp_path / "admission-latest.json").read_text())
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id[DEAD]["first_failed_stage"] == "HEALTHY"
    assert by_id[chosen.route_id]["confirmation_source"] == LADDER_CONFIRMATION_SOURCE
    assert by_id[chosen.route_id]["confirmed_at"]
