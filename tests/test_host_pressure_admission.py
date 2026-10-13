"""Host pressure reaches every admission point (ADR-039)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_runtime import NOW, Classifier, Events, Executor, Reviewer, Selector, node
from verdict.host_pressure import HostDecision, HostSample, Level, govern
from verdict.orchestration import improvement
from verdict.orchestration.concurrency_governor import ResourcePressure
from verdict.orchestration.contracts import RunOutcome, WorkGraph
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.story_footprint import StoryFootprintV1
from verdict.orchestration.supervisor_admission import (
    AdmissionState,
    SupervisorGovernorConfig,
    evaluate_admission,
)

GIB = 1024**3


def decision(level: Level, *, missing: bool = False) -> HostDecision:
    psi = {} if missing else {"memory": {"full_avg10": 0.0}, "cpu": {}, "io": {}}
    s = HostSample(t=0.0, psi=psi, mem_total=None if missing else 16 * GIB, mem_available=12 * GIB)
    _, d = govern(s, {"level": level.value, "since": 0.0, "calm": 0})
    return d


def _admit(pressure: ResourcePressure | None, running: int = 0) -> Any:
    state = AdmissionState()
    from verdict.orchestration.supervisor_admission import RunningStory

    for i in range(running):
        fp = StoryFootprintV1(story_id=f"r{i}", write_paths=frozenset({f"d{i}/f.py"}))
        state.add(RunningStory(story_id=f"r{i}", footprint=fp))
    kwargs = {} if pressure is None else {"pressure": pressure}
    return evaluate_admission(
        story_id="new",
        labels=frozenset(),
        deps=[],
        main_sha="abc",
        footprint=StoryFootprintV1(story_id="new", write_paths=frozenset({"new/f.py"})),
        admission_state=state,
        config=SupervisorGovernorConfig(max_stories=4, max_coding_workers=4),
        **kwargs,
    )


def test_supervisor_admission_without_evidence_fails_closed() -> None:
    result = _admit(None)
    assert result.admit is False and result.reason_code == "DEFER"
    assert "no evidence" in result.reason


@pytest.mark.parametrize(
    ("level", "running", "admit"),
    [
        (Level.GREEN, 3, True),
        (Level.YELLOW, 1, True),
        (Level.YELLOW, 2, False),  # caps of 4 halved to 2
        (Level.RED, 0, False),  # no new story at RED, even with nothing running
    ],
)
def test_supervisor_admission_follows_the_host_level(
    level: Level, running: int, admit: bool
) -> None:
    from verdict.host_pressure import resource_pressure

    assert _admit(resource_pressure(decision(level)), running).admit is admit


class _ScriptedHost:
    """Host levels by call count; records nothing about the runtime."""

    def __init__(self, levels: Callable[[int], Level]) -> None:
        self.levels, self.calls = levels, 0

    def __call__(self) -> HostDecision:
        self.calls += 1
        return decision(self.levels(self.calls))


async def test_orchestrate_parallelism_shrinks_and_grows_back_without_cancelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from verdict.orchestration import runtime as runtime_mod

    monkeypatch.setattr(runtime_mod, "HOST_REFRESH_SECONDS", 0.0)
    repo = _repo(tmp_path)
    graph = WorkGraph("g", tuple(node(n) for n in "abcdefgh"), max_parallel=4)
    ex = Executor({}, delay=0.15)
    # GREEN for the first four slots, then RED while those run, then GREEN again.
    host = _ScriptedHost(lambda n: Level.RED if 6 <= n <= 30 else Level.GREEN)
    events = Events()
    rt = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run1",
        graph=graph,
        selector=Selector(["cc/s", "cx/g"]),
        executor=ex,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(max_parallel=4, follow_host_pressure=True),
        now=lambda: NOW,
        host=host,
    )
    result = await rt.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    changes = [
        (e["level"], e["max_parallel"])
        for e in events.of("controller")
        if e.get("state") == "HOST_PRESSURE"
    ]
    assert changes[0] == ("GREEN", 4) and ("RED", 1) in changes and changes[-1] == ("GREEN", 4)
    assert ex.max_active == 4  # the four running before RED were never cancelled
    assert not any(e.get("state") == "BLOCKED" for e in events.of("node_state"))


async def test_orchestrate_without_host_policy_keeps_configured_parallelism(tmp_path: Path) -> None:
    def never() -> HostDecision:
        raise AssertionError("host sampled without follow_host_pressure")

    repo = _repo(tmp_path)
    graph = WorkGraph("g", tuple(node(n) for n in "abc"), max_parallel=3)
    ex = Executor({}, delay=0.1)
    rt = DagRuntime(
        repo=repo,
        run_dir=tmp_path / "run1",
        graph=graph,
        selector=Selector(["cc/s"]),
        executor=ex,
        classifier=Classifier(),
        events=Events(),
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(max_parallel=3),
        now=lambda: NOW,
        host=never,
    )
    assert (await rt.run()).outcome is RunOutcome.COMPLETE and ex.max_active == 3


@pytest.mark.parametrize("missing", [False, True])
async def test_improve_cycle_is_refused_at_full_pressure(tmp_path: Path, missing: bool) -> None:
    ran: list[str] = []

    async def supervise(run_id: str, argv: list[str]) -> None:
        ran.append(run_id)

    result = await improvement.run_cycle(
        "g",
        repo=_repo(tmp_path),
        runs_dir=tmp_path / "runs",
        cycles_dir=tmp_path / "cycles",
        orchestrate_args=[],
        supervise=supervise,
        clock=lambda: datetime(2026, 10, 13, tzinfo=timezone.utc),
        host=lambda: decision(Level.YELLOW if missing else Level.RED, missing=missing),
    )
    assert result.outcome == "REFUSED" and "host pressure" in result.reason and not ran
    assert result.wait_until == "2026-10-13T00:02:00+00:00"


def test_prover_agentic_concurrency_follows_the_host_level(tmp_path: Path) -> None:
    from tests.test_prove_at_rest_agentic_budget import NOW as PNOW
    from tests.test_prove_at_rest_agentic_budget import _dead_chat, _free, _turn
    from verdict.orchestration.health_cache import HealthCache
    from verdict.prove_at_rest import CycleStats, Prober

    routes = [_free(f"p{i}/m") for i in range(4)]
    stats = CycleStats()
    Prober(
        HealthCache(tmp_path / "cache.json", bucket_capacity=100),
        lambda: routes,
        _dead_chat,
        agentic_transport=lambda route, payload, timeout: _turn(route, payload),
        agentic_max_routes=4,
        agentic_concurrency=4,
        clock=lambda: PNOW,
        host=lambda: decision(Level.YELLOW),
    ).run_agentic_probes(stats, routes=routes)
    assert stats.agentic_concurrency == 2 and stats.agentic_passed == 4


@pytest.mark.parametrize(
    ("level", "missing", "issue"),
    [
        (Level.GREEN, False, False),
        (Level.YELLOW, False, False),
        (Level.RED, False, True),
        (Level.YELLOW, True, True),
    ],
)
def test_doctor_fails_on_red_or_missing_evidence(
    monkeypatch: pytest.MonkeyPatch, level: Level, missing: bool, issue: bool
) -> None:
    from verdict import doctor_diagnostics as dd
    from verdict import host_pressure

    monkeypatch.setattr(host_pressure, "current", lambda: decision(level, missing=missing))
    diag = dd.DoctorDiagnostics()
    dd._doctor_host_pressure(diag)
    assert bool(diag.issues) is issue
    assert diag.host_pressure["level"] == level.value
    assert diag.sections[0][0] == "Host pressure"


def test_host_pressure_cli_json_and_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from verdict import cli, host_pressure

    monkeypatch.setattr(host_pressure, "current", lambda: decision(Level.YELLOW))
    cli.cmd_host("pressure", output_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data["level"] == "YELLOW" and data["pressure"] == 0.5
    assert data["ram"]["source"] == "MemAvailable"
    monkeypatch.setattr(host_pressure, "current", lambda: decision(Level.RED))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_host("pressure", output_json=True)
    assert exc.value.code == 1


def _repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    (root / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root
