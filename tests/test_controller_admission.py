"""Root-controller selection consumes the canonical admitted set before ranking."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from tests.test_controller_launch import NOW, _ctrl_offer, _ctrl_route, _hooks, _mission
from verdict.admission import AdmissionUnavailableError, RuntimeEvidence, RuntimeObservation, admit
from verdict.controller_launch import ControllerLaunchError
from verdict.controller_selection import (
    build_production_controller_selection_hooks,
    select_controller_launch,
)
from verdict.execution_path import optimize_execution_path

DEAD = "cc/dead"  # catalog-advertised, connected, live-exhausted (fixture only)
OK = "kr/claude-ok"
CATALOG = [
    {"id": DEAD, "owned_by": "cc", "context_length": 200_000},
    {"id": OK, "owned_by": "kr", "context_length": 200_000},
]
CONNECTIONS = [
    {"provider": "cc", "isActive": True, "testStatus": "ok"},
    {"provider": "kr", "isActive": True, "testStatus": "ok"},
]
EXHAUSTED = RuntimeEvidence(
    (
        RuntimeObservation(
            f"route:{DEAD}", "exhausted", "quota_exhausted", "fixture:live", NOW.isoformat()
        ),
    ),
    ("fixture:live",),
)


def _offer(route_id: str, digest: str) -> Any:
    return _ctrl_offer(
        _ctrl_route(f"omniroute/{route_id}", provider="omniroute", model=route_id),
        ctx_digest=digest,
    )


def _admission(when: datetime) -> Any:
    return admit(CATALOG, CONNECTIONS, EXHAUSTED, now=when)


def _pool_receipt(route_ids: list[str], confirmed: dict[str, bool]) -> Any:
    """Candidate pool receipt carrying the budgeted confirm rows from prepare."""
    from verdict.candidate_pool import CandidatePoolReceipt, ShortlistEntry, TaskFingerprint

    return CandidatePoolReceipt(
        task_fingerprint=TaskFingerprint(digest="sha256:task", task_family="controller"),
        discovered_count=len(route_ids),
        hard_drops=(),
        probes=(),
        shortlist=tuple(
            ShortlistEntry(r, None, "omniroute", r, 1.0, 1.0, {}, "fixture") for r in route_ids
        ),
        uncertainty=(),
        evidence_digest="sha256:evidence",
        shortlist_digest="sha256:shortlist",
        confirmation={
            "receipt_id": "confirm-fixture",
            "confirm": [
                {"identity_id": r, "confirmed": ok, "status": "confirmed" if ok else "HTTP 429"}
                for r, ok in confirmed.items()
            ],
        },
    )


def _confirming_prepare(confirmed: dict[str, bool], calls: list[Any] | None = None) -> Any:
    """prepare that narrows nothing and reports a budgeted confirm per route."""

    def prepare(task: str, criticality: str, context: dict, request: Any) -> Any:
        if calls is not None:
            calls.append(request)
        ids = [o.route.route_id for o in request.offers]
        return replace(request, pool_receipt=_pool_receipt(ids, confirmed))

    return prepare


class Spy:
    """Records every stage that would lead towards a launch."""

    def __init__(self) -> None:
        self.calls: list[str] = []


def _spied(hooks: Any, spy: Spy) -> Any:
    prepare, optimize, bind, persist = (
        hooks.prepare_execution_request,
        hooks.optimize,
        hooks.bind_prime_target,
        hooks.persist_receipt,
    )

    def p(*a: Any) -> Any:
        spy.calls.append("prepare")
        return prepare(*a)

    def o(request: Any) -> Any:
        spy.calls.append("optimize")
        return optimize(request)

    def b(route: Any) -> Any:
        spy.calls.append("bind")
        return bind(route)

    def s(store: Any, receipt: Any) -> Any:
        spy.calls.append("persist")
        return persist(store, receipt)

    return replace(
        hooks, prepare_execution_request=p, optimize=o, bind_prime_target=b, persist_receipt=s
    )


def test_proof_root_controller_rejects_dead_route_before_any_launch_call() -> None:
    hooks, _, _ = _hooks(seed=[_offer(DEAD, "ctx-dead")])
    spy = Spy()
    hooks = replace(_spied(hooks, spy), admission=_admission, require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert "cc/dead:AVAILABLE" in exc.value.detail and "fixture:live" in exc.value.detail
    assert spy.calls == []


def test_without_admission_dead_route_would_be_selected() -> None:
    """Control: the passport/catalog-only bypass the admitted set closes."""
    hooks, _, _ = _hooks(seed=[_offer(DEAD, "ctx-dead")])
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert decision.prime_target.prime_model == DEAD


def test_controller_selects_only_from_admitted_seeds_and_records_receipt() -> None:
    # OK has no runtime evidence (unknown), so it needs a successful live
    # confirmation before it may launch.
    prepared: list[Any] = []
    hooks, _, persisted = _hooks(
        seed=[_offer(DEAD, "ctx-dead"), _offer(OK, "ctx-ok")],
        prepare=_confirming_prepare({f"omniroute/{OK}": True}, prepared),
    )
    hooks = replace(hooks, admission=_admission, require_live_admission=True)
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert decision.prime_target.prime_model == OK
    assert [o.route.model for o in prepared[0].offers] == [OK]
    ext = persisted[0].kwargs["extensions"]["controller_launch"]["admission"]
    assert ext["digest"].startswith("sha256:")
    dropped = ext["dropped_seeds"][0]
    assert dropped["route_id"] == DEAD
    assert dropped["first_failed_stage"] == "AVAILABLE"
    assert dropped["source"] == "fixture:live"


def test_controller_fails_closed_when_live_admission_required_but_absent() -> None:
    hooks, _, _ = _hooks(seed=[_offer(OK, "ctx-ok")])
    spy = Spy()
    hooks = replace(_spied(hooks, spy), require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "live_admission_required"
    assert spy.calls == []


def test_controller_fails_closed_when_live_admission_source_errors() -> None:
    def broken(when: datetime) -> Any:
        raise AdmissionUnavailableError("connection_evidence_unavailable", "gateway down")

    hooks, _, _ = _hooks(seed=[_offer(OK, "ctx-ok")])
    spy = Spy()
    hooks = replace(_spied(hooks, spy), admission=broken, require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "live_admission_unavailable"
    assert spy.calls == []


def test_prepare_cannot_reintroduce_an_excluded_route() -> None:
    dead = _offer(DEAD, "ctx-dead")

    def readmitting_prepare(task: str, criticality: str, context: dict, request: Any) -> Any:
        return replace(request, offers=(*request.offers, dead))

    hooks, _, _ = _hooks(seed=[dead, _offer(OK, "ctx-ok")], prepare=readmitting_prepare)
    hooks = replace(hooks, admission=_admission, require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "admission_bypass"


def test_prepare_cannot_add_a_route_even_without_admission() -> None:
    extra = _offer("gc/injected", "ctx-x")

    def widening_prepare(task: str, criticality: str, context: dict, request: Any) -> Any:
        return replace(request, offers=(*request.offers, extra))

    hooks, _, _ = _hooks(seed=[_offer(OK, "ctx-ok")], prepare=widening_prepare)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "admission_bypass"


def test_optimizer_cannot_select_outside_admitted_set() -> None:
    dead_route = _ctrl_route(f"omniroute/{DEAD}", provider="omniroute", model=DEAD)

    def rogue_optimize(request: Any) -> Any:
        return replace(optimize_execution_path(request), selected_route=dead_route)

    hooks, _, _ = _hooks(
        seed=[_offer(DEAD, "ctx-dead"), _offer(OK, "ctx-ok")],
        optimize=rogue_optimize,
        prepare=_confirming_prepare({f"omniroute/{OK}": True}),
    )
    hooks = replace(hooks, admission=_admission, require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "admission_bypass"


class _AuthorityService:
    require_execution_path_authority = True

    def prepare_controller_execution_request(self, *a: Any) -> Any:
        raise AssertionError("not reached")


def test_factory_in_authoritative_mode_requires_live_admission() -> None:
    with pytest.raises(ControllerLaunchError) as exc:
        build_production_controller_selection_hooks(
            intelligence_service=_AuthorityService(),
            seed_offers=lambda mission, when: (),
            bind_prime_target=lambda route: None,  # type: ignore[arg-type,return-value]
            compile_context_digests=lambda d, p: {},
            cost_state_factory=lambda a, b, c: None,  # type: ignore[arg-type,return-value]
            task_state_factory=lambda m: None,  # type: ignore[arg-type,return-value]
        )
    assert exc.value.reason_code == "live_admission_required"


def test_factory_with_admission_wires_hooks_and_records_last_admission() -> None:
    bundle = build_production_controller_selection_hooks(
        intelligence_service=_AuthorityService(),
        seed_offers=lambda mission, when: (_offer(DEAD, "ctx-dead"),),
        bind_prime_target=lambda route: None,  # type: ignore[arg-type,return-value]
        compile_context_digests=lambda d, p: {},
        cost_state_factory=lambda a, b, c: None,  # type: ignore[arg-type,return-value]
        task_state_factory=lambda m: None,  # type: ignore[arg-type,return-value]
        admission=_admission,
    )
    assert bundle.hooks.require_live_admission is True
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=bundle.hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert bundle.artifacts.last_admission is not None
    assert DEAD not in bundle.artifacts.last_admission


# ---------------------------------------------------------------- launch gate
FRESH_CATALOG = [{"id": OK, "owned_by": "kr", "context_length": 200_000}]


def _fresh_host_admission(tmp_path: Any) -> Any:
    """Real default runtime loader on a fresh host: both state files absent."""
    from verdict.admission import default_runtime_evidence

    def load(when: datetime) -> Any:
        runtime = default_runtime_evidence(now=when, state_dir=tmp_path / "no-state")
        return admit(FRESH_CATALOG, CONNECTIONS, runtime, now=when)

    return load


def test_no_runtime_evidence_and_failed_confirm_never_launches(tmp_path: Any) -> None:
    recorded: list[Any] = []
    hooks, _, persisted = _hooks(
        seed=[_offer(OK, "ctx-ok")], prepare=_confirming_prepare({f"omniroute/{OK}": False})
    )
    spy = Spy()
    hooks = replace(
        _spied(hooks, spy),
        prepare_execution_request=hooks.prepare_execution_request,
        admission=_fresh_host_admission(tmp_path),
        require_live_admission=True,
        record_admission=recorded.append,
    )
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert f"{OK}:HEALTHY:HTTP 429@controller_confirm" in exc.value.detail
    assert spy.calls == [] and persisted == []
    # The persisted admission state names the stage and the confirmation source.
    record = recorded[-1].receipt()["candidates"][0]
    assert record["first_failed_stage"] == "HEALTHY"
    assert record["source"].startswith("controller_confirm:admit_prove_confirm")
    assert record["observed_at"] == NOW.isoformat()
    assert recorded[-1].runtime_consulted == ()


def test_no_runtime_evidence_and_no_confirm_never_launches(tmp_path: Any) -> None:
    hooks, _, persisted = _hooks(seed=[_offer(OK, "ctx-ok")])
    hooks = replace(hooks, admission=_fresh_host_admission(tmp_path), require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "no_eligible_route"
    assert "admitted_unverified" in exc.value.detail
    assert persisted == []


def test_unknown_plus_successful_confirm_launches_with_confirmation_source(tmp_path: Any) -> None:
    recorded: list[Any] = []
    hooks, _, persisted = _hooks(
        seed=[_offer(OK, "ctx-ok")], prepare=_confirming_prepare({f"omniroute/{OK}": True})
    )
    hooks = replace(
        hooks,
        admission=_fresh_host_admission(tmp_path),
        require_live_admission=True,
        record_admission=recorded.append,
    )
    decision = select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert decision.prime_target.prime_model == OK
    ext = persisted[0].kwargs["extensions"]["controller_launch"]["admission"]
    authority = ext["launch_authority"]
    assert authority["basis"] == "live_confirmation"
    assert authority["source"].startswith("controller_confirm:admit_prove_confirm")
    assert authority["observed_at"] == NOW.isoformat()
    assert ext["confirmations"][0]["confirmed"] is True
    record = recorded[-1].record_for(OK)
    assert record.health == "unknown" and record.confirmation_source == authority["source"]
    assert not recorded[-1].proven_healthy(OK) and recorded[-1].launchable(OK)


def test_optimizer_cannot_select_an_unconfirmed_route_after_select(tmp_path: Any) -> None:
    ok_route = _ctrl_route(f"omniroute/{OK}", provider="omniroute", model=OK)
    other = "kr/claude-other"
    catalog = [*FRESH_CATALOG, {"id": other, "owned_by": "kr", "context_length": 200_000}]

    def load(when: datetime) -> Any:
        return admit(catalog, CONNECTIONS, RuntimeEvidence((), ("x:absent",)), now=when)

    def rogue_optimize(request: Any) -> Any:
        return replace(optimize_execution_path(request), selected_route=ok_route)

    hooks, _, _ = _hooks(
        seed=[_offer(OK, "ctx-ok"), _offer(other, "ctx-other")],
        optimize=rogue_optimize,
        prepare=_confirming_prepare({f"omniroute/{other}": True}),
    )
    hooks = replace(hooks, admission=load, require_live_admission=True)
    with pytest.raises(ControllerLaunchError) as exc:
        select_controller_launch(_mission(), hooks=hooks, now=NOW)
    assert exc.value.reason_code == "admission_bypass"
