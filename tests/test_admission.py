"""Unit tests for the canonical live admission module."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict.admission import (
    CONTROLLER_IDENTITY_UNKNOWN,
    AdmissionBypassError,
    AdmissionStage,
    AdmissionUnavailableError,
    AdmittedSet,
    RuntimeEvidence,
    RuntimeObservation,
    active_controller_route,
    admit,
    canonical_route_id,
    evidence_from_health_cache,
    evidence_from_ladder_state,
    evidence_from_quota_rows,
)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def row(route_id: str, *, owned_by: str | None = None, tools: bool = True) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": owned_by or route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": tools},
    }


def conn(provider: str, *, active: bool = True, status: str = "ok", **extra: Any) -> dict:
    return {"provider": provider, "isActive": active, "testStatus": status, **extra}


def obs(key: str, state: str, category: str = "", source: str = "fixture") -> RuntimeObservation:
    return RuntimeObservation(key, state, category or state, source, NOW.isoformat())


def test_missing_inventory_connections_or_runtime_fail_closed() -> None:
    with pytest.raises(AdmissionUnavailableError, match="live_inventory_unavailable"):
        admit(None, [], RuntimeEvidence(), now=NOW)
    with pytest.raises(AdmissionUnavailableError, match="connection_evidence_unavailable"):
        admit([row("kr/a")], None, RuntimeEvidence(), now=NOW)
    with pytest.raises(AdmissionUnavailableError, match="runtime_evidence_unavailable"):
        admit([row("kr/a")], [conn("kr")], None, now=NOW)


def test_catalog_plus_connection_with_exhausted_runtime_is_rejected() -> None:
    runtime = RuntimeEvidence((obs("route:cc/dead", "exhausted", "quota_exhausted"),), ("live",))
    admitted = admit([row("cc/dead"), row("kr/ok")], [conn("cc"), conn("kr")], runtime, now=NOW)
    assert "cc/dead" not in admitted
    record = admitted.first_failure("cc/dead")
    assert record.first_failed_stage is AdmissionStage.AVAILABLE
    assert record.source == "fixture"
    assert "kr/ok" in admitted


@pytest.mark.parametrize(
    ("state", "stage"),
    [
        ("unauthorized", AdmissionStage.ENTITLED),
        ("unhealthy", AdmissionStage.HEALTHY),
        ("cooldown", AdmissionStage.AVAILABLE),
        ("exhausted", AdmissionStage.AVAILABLE),
    ],
)
def test_each_runtime_failure_names_its_first_failed_stage(
    state: str, stage: AdmissionStage
) -> None:
    runtime = RuntimeEvidence((obs("route:kr/x", state),), ("live",))
    admitted = admit([row("kr/x")], [conn("kr")], runtime, now=NOW)
    record = admitted.first_failure("kr/x")
    assert not record.admitted and record.first_failed_stage is stage


def test_provider_cooldown_drops_all_provider_routes() -> None:
    runtime = RuntimeEvidence((obs("provider:kr", "cooldown", "rate_limited"),), ("live",))
    admitted = admit([row("kr/a"), row("kr/b")], [conn("kr")], runtime, now=NOW)
    assert admitted.ids == frozenset()


def test_connection_states_are_descriptive_not_authority() -> None:
    until = (NOW + timedelta(minutes=5)).isoformat()
    admitted = admit(
        [row("aa/x"), row("bb/x"), row("cc/x"), row("dd/x")],
        [
            conn("aa", active=False),
            conn("bb", status="expired"),
            conn("cc", rate_limited_until={"cc/x": until}),
        ],
        RuntimeEvidence(),
        now=NOW,
    )
    assert admitted.ids == frozenset()
    reasons = {r.route_id: r.reason for r in admitted.records}
    assert reasons == {
        "aa/x": "no_active_account",
        "bb/x": "connection_status:expired",
        "cc/x": "provider_rate_limited",
        "dd/x": "no_connection_evidence",
    }


def test_unknown_runtime_stays_explicit_never_healthy() -> None:
    admitted = admit([row("kr/new")], [conn("kr")], RuntimeEvidence(), now=NOW)
    record = admitted.record_for("kr/new")
    assert record is not None and record.admitted
    assert record.health == "unknown"
    assert record.reason == "admitted_unverified"
    assert not admitted.proven_healthy("kr/new")


def test_healthy_evidence_is_recorded_with_source() -> None:
    runtime = RuntimeEvidence((obs("route:kr/a", "healthy", source="probe"),), ("probe",))
    admitted = admit([row("kr/a")], [conn("kr")], runtime, now=NOW)
    assert admitted.proven_healthy("kr/a")
    assert admitted.record_for("kr/a").source == "probe"  # type: ignore[union-attr]


def test_opaque_routes_are_never_admitted() -> None:
    admitted = admit(
        [row("auto/best-coding"), row("combo/x")], [conn("auto")], RuntimeEvidence(), now=NOW
    )
    assert admitted.ids == frozenset()


def test_admitted_set_cannot_be_constructed_or_replaced_directly() -> None:
    with pytest.raises(TypeError):
        AdmittedSet(records=(), generated_at="x", sources=())
    admitted = admit([row("kr/a")], [conn("kr")], RuntimeEvidence(), now=NOW)
    with pytest.raises(TypeError):
        dataclasses.replace(admitted, records=admitted.records)


def test_narrowing_only_removes_and_never_readmits() -> None:
    runtime = RuntimeEvidence((obs("route:cc/dead", "exhausted"),), ("live",))
    admitted = admit(
        [row("cc/dead"), row("kr/a"), row("kr/b")], [conn("cc"), conn("kr")], runtime, now=NOW
    )
    narrowed = admitted.narrow(AdmissionStage.DOWNSTREAM, "keep_everything", lambda _r: True)
    assert "cc/dead" not in narrowed
    assert narrowed.ids == admitted.ids
    only_a = narrowed.exclude(["kr/b"], AdmissionStage.DOWNSTREAM, "test")
    assert only_a.ids == frozenset({"kr/a"})
    # dead keeps its original first failed stage
    assert only_a.first_failure("cc/dead").first_failed_stage is AdmissionStage.AVAILABLE
    assert only_a.first_failure("kr/b").first_failed_stage is AdmissionStage.DOWNSTREAM


def test_require_subset_raises_for_outside_routes() -> None:
    admitted = admit([row("kr/a")], [conn("kr")], RuntimeEvidence(), now=NOW)
    admitted.require_subset(["kr/a", "omniroute/kr/a"], surface="t")
    with pytest.raises(AdmissionBypassError):
        admitted.require_subset(["kr/a", "cc/dead"], surface="t")


def test_worker_scope_and_controller_exclusion_are_extra_narrowing() -> None:
    admitted = admit(
        [row("kr/a"), row("kr/ctrl"), row("gc/b")],
        [conn("kr"), conn("gc")],
        RuntimeEvidence(),
        now=NOW,
    )
    scoped = admitted.restrict_prefixes(["kr/"]).exclude_controller("omniroute/kr/ctrl")
    assert scoped.ids == frozenset({"kr/a"})
    assert scoped.controller_identity == "kr/ctrl"
    assert scoped.first_failure("kr/ctrl").first_failed_stage is AdmissionStage.CONTROLLER_EXCLUDED
    assert scoped.first_failure("gc/b").first_failed_stage is AdmissionStage.WORKER_SCOPE


def test_unknown_controller_identity_is_recorded_not_guessed() -> None:
    admitted = admit([row("kr/a")], [conn("kr")], RuntimeEvidence(), now=NOW)
    out = admitted.exclude_controller(None)
    assert out.ids == admitted.ids
    assert out.receipt()["controller_identity"] == CONTROLLER_IDENTITY_UNKNOWN
    assert active_controller_route({}) is None
    assert active_controller_route({"VERDICT_ACTIVE_CONTROLLER_ROUTE": " kr/x "}) == "kr/x"


def test_receipt_lists_every_candidate_with_stage_source_and_freshness(tmp_path: Path) -> None:
    runtime = RuntimeEvidence((obs("route:cc/dead", "exhausted", source="quota:live"),), ("quota",))
    admitted = admit([row("cc/dead"), row("kr/a")], [conn("cc"), conn("kr")], runtime, now=NOW)
    path = admitted.write_receipt(tmp_path / "admission.json")
    data = json.loads(path.read_text())
    assert data["digest"].startswith("sha256:")
    by_id = {c["route_id"]: c for c in data["candidates"]}
    assert by_id["cc/dead"]["first_failed_stage"] == "AVAILABLE"
    assert by_id["cc/dead"]["source"] == "quota:live"
    assert by_id["cc/dead"]["observed_at"]
    assert by_id["kr/a"]["first_failed_stage"] is None
    assert data["admitted"] == ["kr/a"]


def test_ladder_state_and_health_cache_are_normalized(tmp_path: Path) -> None:
    state = tmp_path / "orchestration-health.json"
    state.write_text(
        json.dumps(
            {
                "health": {
                    "kr/a": {"healthy": True, "category": "", "checked_at": NOW.isoformat()}
                },
                "cooldowns": {
                    "route:cc/dead": {
                        "until": (NOW + timedelta(hours=1)).isoformat(),
                        "category": "quota_exhausted",
                    },
                    "route:kr/old": {
                        "until": (NOW - timedelta(hours=1)).isoformat(),
                        "category": "x",
                    },
                },
            }
        )
    )
    cache = tmp_path / "subagent-health.json"
    cache.write_text(
        json.dumps(
            {
                "omniroute/gc/b": {
                    "healthy": False,
                    "category": "rate_limited",
                    "observed_at": NOW.isoformat(),
                    "expires_at": (NOW + timedelta(minutes=1)).isoformat(),
                }
            }
        )
    )
    runtime = evidence_from_ladder_state(state, now=NOW).merged(
        evidence_from_health_cache(cache, now=NOW)
    )
    admitted = admit(
        [row("kr/a"), row("cc/dead"), row("kr/old"), row("gc/b")],
        [conn("kr"), conn("cc"), conn("gc")],
        runtime,
        now=NOW,
    )
    assert admitted.ids == frozenset({"kr/a", "kr/old"})
    assert admitted.proven_healthy("kr/a")
    assert not admitted.proven_healthy("kr/old")


def test_quota_rows_pass_through_as_evidence_only() -> None:
    runtime = evidence_from_quota_rows(
        [{"route_id": "kr/a", "exhausted": True}, {"route_id": "kr/b", "exhausted": None}]
    )
    admitted = admit([row("kr/a"), row("kr/b")], [conn("kr")], runtime, now=NOW)
    assert admitted.ids == frozenset({"kr/b"})
    assert admitted.record_for("kr/b").health == "unknown"  # type: ignore[union-attr]


def test_canonical_route_id_strips_gateway() -> None:
    assert canonical_route_id("omniroute/kr/x") == "kr/x"
    assert canonical_route_id("kr/x") == "kr/x"


def test_require_runtime_with_only_absent_sources_means_no_runtime_consulted(
    tmp_path: Path,
) -> None:
    from verdict.admission import default_runtime_evidence

    runtime = default_runtime_evidence(now=NOW, state_dir=tmp_path / "missing")
    assert runtime.sources and all(s.endswith(":absent") for s in runtime.sources)
    assert runtime.consulted == ()
    admitted = admit([row("cc/dead")], [conn("cc")], runtime, now=NOW, require_runtime=True)
    assert admitted.runtime_consulted == ()
    assert admitted.receipt()["runtime_consulted"] == []
    record = admitted.record_for("cc/dead")
    assert record is not None
    assert record.reason == "admitted_unverified" and record.health == "unknown"
    # Same result as an empty (no source at all) runtime evidence object.
    empty = admit([row("cc/dead")], [conn("cc")], RuntimeEvidence(), now=NOW)
    assert admitted.record_for("cc/dead") == empty.record_for("cc/dead")
    assert not admitted.proven_healthy("cc/dead")
    assert not admitted.launchable("cc/dead")
    with pytest.raises(AdmissionBypassError):
        admitted.require_launchable("cc/dead", surface="t")


def test_record_confirmation_success_and_failure() -> None:
    admitted = admit([row("kr/a"), row("kr/b")], [conn("kr")], RuntimeEvidence(), now=NOW)
    ok = admitted.record_confirmation("kr/a", healthy=True, source="probe:x", observed_at="t1")
    assert ok.launchable("kr/a") and not ok.proven_healthy("kr/a")
    assert ok.launch_authority("kr/a")["basis"] == "live_confirmation"
    bad = ok.record_confirmation(
        "omniroute/kr/b", healthy=False, source="probe:x", observed_at="t2", category="timeout"
    )
    record = bad.first_failure("kr/b")
    assert "kr/b" not in bad and record.first_failed_stage is AdmissionStage.HEALTHY
    assert (record.reason, record.source, record.observed_at) == ("timeout", "probe:x", "t2")
    # A confirmation never re-admits a dropped route.
    again = bad.record_confirmation("kr/b", healthy=True, source="probe:x", observed_at="t3")
    assert "kr/b" not in again and not again.launchable("kr/b")


def test_proven_healthy_route_is_launchable_without_confirmation() -> None:
    runtime = RuntimeEvidence((obs("route:kr/a", "healthy", source="probe"),), ("probe",))
    admitted = admit([row("kr/a")], [conn("kr")], runtime, now=NOW)
    assert admitted.runtime_consulted == ("probe",)
    assert admitted.require_launchable("kr/a", surface="t").health == "healthy"
    assert admitted.launch_authority("kr/a")["basis"] == "proven_healthy"
