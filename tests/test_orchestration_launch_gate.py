"""Launch gate on the orchestration ladder: an unverified route needs a live confirm."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_eligibility import NOW, REQ, FakeProbe, conn, row
from verdict.admission import AdmissionBypassError, AdmissionStage, RuntimeEvidence, admit
from verdict.orchestration.contracts import EligibilityStage
from verdict.orchestration.eligibility import LADDER_CONFIRMATION_SOURCE, EligibilityLadder
from verdict.subagent_selection import HealthResult

DEAD = "cc/dead"
OK = "kr/claude-sonnet-ok"


def _rows() -> list[dict[str, Any]]:
    return [row(DEAD, owned_by="cc"), row(OK, owned_by="kr")]


def _conns() -> list[dict[str, Any]]:
    return [conn("cc"), conn("kr")]


def _fresh_admitted() -> Any:
    # Fresh host: no runtime source consulted, every route admitted_unverified.
    admitted = admit(_rows(), _conns(), RuntimeEvidence((), ("ladder_state:x:absent",)), now=NOW)
    assert admitted.runtime_consulted == ()
    assert not admitted.launchable(DEAD) and not admitted.launchable(OK)
    return admitted


def _cached_healthy(path: Path, *routes: str) -> None:
    health = {r: {"healthy": True, "checked_at": NOW.isoformat(), "category": ""} for r in routes}
    path.write_text(json.dumps({"health": health, "cooldowns": {}}))


def _ladder(tmp_path: Path, probe: FakeProbe, state: Path, **kwargs: Any) -> EligibilityLadder:
    return EligibilityLadder(
        _rows(),
        _conns(),
        probe,
        state,
        harness_visible=lambda _r: True,
        admitted=_fresh_admitted(),
        prefer_providers=("cc",),
        admission_receipt=tmp_path / "admission-latest.json",
        **kwargs,
    )


def test_cached_healthy_ladder_state_is_not_confirmation(tmp_path: Path) -> None:
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD, OK)
    probe = FakeProbe()
    ladder = _ladder(tmp_path, probe, state)
    chosen, _ = ladder.select(REQ, now=NOW)
    assert chosen is not None and chosen.route_id == DEAD
    assert probe.calls == [DEAD]  # confirmed live despite the cached healthy hit
    assert ladder.admitted is not None
    record = ladder.admitted.record_for(DEAD)
    assert record is not None
    assert record.confirmation_source == LADDER_CONFIRMATION_SOURCE
    assert record.confirmed_at == NOW.isoformat()
    assert ladder.admitted.launch_authority(DEAD)["basis"] == "live_confirmation"
    receipt = json.loads((tmp_path / "admission-latest.json").read_text())
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id[DEAD]["confirmation_source"] == LADDER_CONFIRMATION_SOURCE
    assert by_id[DEAD]["confirmed_at"] == NOW.isoformat()


def test_failed_confirmation_drops_route_and_never_readmits(tmp_path: Path) -> None:
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD, OK)
    probe = FakeProbe({DEAD: HealthResult(False, "rate_limited")})
    ladder = _ladder(tmp_path, probe, state)
    chosen, _ = ladder.select(REQ, now=NOW)
    assert chosen is not None and chosen.route_id == OK
    assert probe.calls == [DEAD, OK]
    assert ladder.admitted is not None
    assert DEAD not in ladder.admitted
    dead = ladder.admitted.first_failure(DEAD)
    assert dead.first_failed_stage is AdmissionStage.HEALTHY
    assert dead.reason == "rate_limited"
    # A later healthy ladder result cannot put the dropped route back.
    _cached_healthy(state, DEAD)
    probe.results[DEAD] = HealthResult(True, "")
    again, verdicts = ladder.select(REQ, now=NOW)
    assert again is not None and again.route_id == OK
    by_id = {v.route_id: v for v in verdicts}
    assert by_id[DEAD].failed_stage is EligibilityStage.HEALTHY
    assert by_id[DEAD].reason.startswith("admission:")
    assert probe.calls == [DEAD, OK]  # OK is now confirmed: no second probe


def test_confirmation_counts_against_probe_budget(tmp_path: Path) -> None:
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD, OK)
    probe = FakeProbe({DEAD: HealthResult(False, "rate_limited")})
    ladder = _ladder(tmp_path, probe, state, max_probes_per_select=1)
    chosen, verdicts = ladder.select(REQ, now=NOW)
    assert chosen is None
    assert probe.calls == [DEAD]
    by_id = {v.route_id: v for v in verdicts}
    assert by_id[OK].reason == "probe_budget_exhausted"


def test_fresh_host_every_node_selection_is_confirmed_or_proven(tmp_path: Path) -> None:
    state = tmp_path / "orchestration-health.json"  # does not exist yet
    probe = FakeProbe()
    ladder = _ladder(tmp_path, probe, state)
    for _node in range(3):
        chosen, _ = ladder.select(REQ, now=NOW)
        assert chosen is not None
        assert ladder.admitted is not None
        assert ladder.admitted.launchable(chosen.route_id)
    assert probe.calls == [DEAD]


def test_select_asserts_launchable_not_just_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD)
    ladder = _ladder(tmp_path, FakeProbe(), state)
    # Simulate a code path that skips the confirm: the assertion must still fire.
    monkeypatch.setattr(ladder, "_record_confirmation", lambda *_a, **_k: None)
    with pytest.raises(AdmissionBypassError):
        ladder.select(REQ, now=NOW)
    with pytest.raises(AdmissionBypassError):
        ladder.require_launchable(DEAD, surface="test")
