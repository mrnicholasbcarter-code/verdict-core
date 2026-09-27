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


# --------------------------------------------------------------------------- #
# DagRuntime bind and OCR reviewer launch are gated on the ladder's admission.
# --------------------------------------------------------------------------- #

from tests.test_orch_runtime import Classifier, Events, Executor, node  # noqa: E402
from tests.test_orch_runtime import Reviewer as FakeReviewer  # noqa: E402
from tests.test_orch_runtime import repo as repo  # noqa: E402
from verdict.orchestration.contracts import (  # noqa: E402
    CapacityClass,
    RouteVerdict,
    RunOutcome,
    WorkGraph,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy  # noqa: E402


def _runtime(repo: Path, selector: Any, executor: Executor, events: Events) -> DagRuntime:
    graph = WorkGraph("g", (node("a"), node("b"), node("c")), max_parallel=1)
    return DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=graph,
        selector=selector,
        executor=executor,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=FakeReviewer(),
        policy=RuntimePolicy(),
        now=lambda: NOW,
    )


class _ProbeLog(FakeProbe):
    """Fake probe that shares an ordered log with the executor."""

    def __init__(self, log: list[str], results: dict[str, HealthResult]) -> None:
        super().__init__(results)
        self.log = log

    def __call__(self, route_id: str) -> HealthResult:
        self.log.append(f"probe:{route_id}")
        return super().__call__(route_id)


class _LoggingExecutor(Executor):
    def __init__(self, log: list[str]) -> None:
        super().__init__({}, delay=0.0)
        self.log = log

    async def run(self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float) -> Any:
        self.log.append(f"launch:{route_id}")
        return await super().run(
            prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
        )


async def test_fresh_host_orchestrate_three_nodes_each_launch_is_confirmed(
    repo: Path, tmp_path: Path
) -> None:
    log: list[str] = []
    probe = _ProbeLog(log, {DEAD: HealthResult(False, "rate_limited")})
    ladder = _ladder(tmp_path, probe, tmp_path / "orchestration-health.json")
    executor = _LoggingExecutor(log)
    result = await _runtime(repo, ladder, executor, Events()).run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    launches = [e for e in log if e.startswith("launch:")]
    assert len(launches) == 3
    assert f"launch:{DEAD}" not in log  # the route whose confirm failed never launches
    assert ladder.admitted is not None
    for entry in launches:
        route = entry.split(":", 1)[1]
        # Each launched route was confirmed live before its first launch.
        assert log.index(f"probe:{route}") < log.index(entry)
        record = ladder.admitted.record_for(route)
        assert record is not None
        assert record.confirmation_source == LADDER_CONFIRMATION_SOURCE
    assert DEAD not in ladder.admitted
    receipt = json.loads((tmp_path / "admission-latest.json").read_text())
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id[DEAD]["first_failed_stage"] == "HEALTHY"
    assert by_id[OK]["confirmation_source"] == LADDER_CONFIRMATION_SOURCE


async def test_state_file_inject_shape_does_not_launch_dead_without_confirmation(
    repo: Path, tmp_path: Path
) -> None:
    # --state-file / --inject: ladder state is chaos-health.json with cached healthy.
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD, OK)
    log: list[str] = []
    probe = _ProbeLog(log, {DEAD: HealthResult(False, "rate_limited")})
    ladder = _ladder(tmp_path, probe, state)
    result = await _runtime(repo, ladder, _LoggingExecutor(log), Events()).run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert log[0] == f"probe:{DEAD}"
    assert f"launch:{DEAD}" not in log
    assert [e for e in log if e.startswith("launch:")] == [f"launch:{OK}"] * 3


class _UnconfirmedSelector:
    """Selector that hands back an admitted-but-unverified route with no confirm."""

    def __init__(self, admitted: Any, route: str) -> None:
        self.admitted, self.route = admitted, route

    def _verdict(self) -> RouteVerdict:
        return RouteVerdict(
            self.route,
            self.route.split("/")[0],
            EligibilityStage.SELECTED,
            None,
            "ok",
            CapacityClass.SUBSCRIPTION,
            rank=0,
        )

    def evaluate(self, requirements: Any, *, now: Any) -> tuple[RouteVerdict, ...]:
        return (self._verdict(),)

    def select(
        self, requirements: Any, *, now: Any
    ) -> tuple[RouteVerdict, tuple[RouteVerdict, ...]]:
        return self._verdict(), (self._verdict(),)

    def record_failure(self, route_id: str, failure: Any, *, now: Any) -> None:
        return None

    def record_success(self, route_id: str, *, now: Any) -> None:
        return None

    def require_launchable(self, route_id: str, *, surface: str) -> None:
        self.admitted.require_launchable(route_id, surface=surface)


async def test_dag_runtime_bind_requires_launchable(repo: Path) -> None:
    log: list[str] = []
    executor = _LoggingExecutor(log)
    selector = _UnconfirmedSelector(_fresh_admitted(), DEAD)
    result = await _runtime(repo, selector, executor, Events()).run()
    # The bind gate raises AdmissionBypassError; the runtime fails the node closed.
    assert result.outcome is RunOutcome.BLOCKED
    assert "AdmissionBypassError: DagRuntime.bind" in result.reason
    assert log == []  # nothing launched


def test_reviewer_launch_requires_launchable(tmp_path: Path) -> None:
    import asyncio

    from verdict.orchestration.review import OpenCodeReviewer

    calls: list[Any] = []

    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        calls.append(list(argv))
        raise AssertionError("OCR must not run on an unconfirmed route")

    reviewer = OpenCodeReviewer(
        _UnconfirmedSelector(_fresh_admitted(), OK),
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
    )
    with pytest.raises(AdmissionBypassError) as err:
        asyncio.run(
            reviewer.review(
                repo=tmp_path,
                base_ref="main",
                head_ref="feature",
                background="",
                exclude_routes=frozenset(),
                exclude_families=frozenset(),
            )
        )
    assert err.value.surface == "OpenCodeReviewer.launch"
    assert calls == []


def test_reviewer_through_ladder_is_confirmed_before_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from verdict.orchestration.review import OcrRun, OpenCodeReviewer

    monkeypatch.setenv("TEST_OCR_KEY", "k")
    state = tmp_path / "chaos-health.json"
    _cached_healthy(state, DEAD, OK)
    log: list[str] = []
    probe = _ProbeLog(log, {DEAD: HealthResult(False, "rate_limited")})
    ladder = _ladder(tmp_path, probe, state)

    def runner(argv: Any, *, env: Any, timeout: float) -> Any:
        log.append("ocr")
        return OcrRun(exit_code=1, stdout="", stderr="boom")

    reviewer = OpenCodeReviewer(
        ladder, api_key_env="TEST_OCR_KEY", out_dir=tmp_path / "out", runner=runner
    )
    asyncio.run(
        reviewer.review(
            repo=tmp_path,
            base_ref="main",
            head_ref="feature",
            background="",
            exclude_routes=frozenset(),
            exclude_families=frozenset(),
        )
    )
    assert log[:2] == [f"probe:{DEAD}", f"probe:{OK}"]
    assert "ocr" in log and log.index("ocr") > log.index(f"probe:{OK}")
