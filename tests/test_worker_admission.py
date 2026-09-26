"""Worker selection consumes the canonical admitted set before ranking or probing."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from verdict import worker_runtime
from verdict.admission import (
    AdmissionStage,
    AdmissionUnavailableError,
    RuntimeEvidence,
    RuntimeObservation,
    admit,
)
from verdict.subagent_selection import (
    HealthCache,
    HealthResult,
    LaunchCandidate,
    NoHealthyWorkerModelError,
    WorkerTask,
    WorkerTerminal,
    eligible_worker_candidates,
    select_worker_model,
)
from verdict.worker_runtime import WorkerController

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
DEAD = "cc/dead"  # catalog-advertised, connected, live-exhausted (fixture only)
OK_ROUTE = "kr/claude-ok"
CTRL = "kr/controller-route"


def row(route_id: str) -> dict[str, Any]:
    return {
        "id": route_id,
        "owned_by": route_id.split("/", 1)[0],
        "context_length": 200_000,
        "capabilities": {"tool_calling": True},
        "pricing": {"input": 0, "output": 0},
    }


CATALOG = [row(DEAD), row(OK_ROUTE), row(CTRL)]
CONNECTIONS = [
    {"provider": "cc", "isActive": True, "testStatus": "ok"},
    {"provider": "kr", "isActive": True, "testStatus": "ok"},
]
SELECTORS = [f"omniroute/{r['id']}" for r in CATALOG]
EXHAUSTED = RuntimeEvidence(
    (
        RuntimeObservation(
            f"route:{DEAD}", "exhausted", "quota_exhausted", "fixture:live", NOW.isoformat()
        ),
    ),
    ("fixture:live",),
)


class SpyProbe:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, candidate: LaunchCandidate) -> HealthResult:
        self.calls.append(candidate.route_id)
        return HealthResult(True, "healthy")


class SpyAdapter:
    def __init__(self) -> None:
        self.spawns: list[str] = []

    async def spawn(self, prompt: str, *, name: str, model: str) -> dict[str, Any]:
        self.spawns.append(model)
        return {"rlm_child_id": name, "model": model}

    async def collect(self, handle: Any) -> WorkerTerminal:
        return WorkerTerminal("done", "OK", True, stop_reason="stop")

    async def delete(self, handle: Any) -> None:
        return None


def admitted_set() -> Any:
    return admit(CATALOG, CONNECTIONS, EXHAUSTED, now=NOW)


def test_proof_worker_rejects_dead_route_before_any_launch(tmp_path: Path) -> None:
    admitted = admitted_set().exclude_controller(CTRL)
    probe, adapter = SpyProbe(), SpyAdapter()
    # Only the dead route is Prime-visible: without admission it would be probed and spawned.
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=CATALOG,
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=probe,
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        admitted=admitted,
        require_admission=True,
    )
    outcome = asyncio.run(ctrl.run("task"))
    assert outcome.state == "FAIL_CLOSED"
    assert probe.calls == [] and adapter.spawns == []
    record = admitted.first_failure(DEAD)
    assert record.first_failed_stage is AdmissionStage.AVAILABLE
    assert record.source == "fixture:live"


def test_without_admission_the_dead_route_would_be_launched(tmp_path: Path) -> None:
    """Control: the bypass the admitted set closes (legacy compat path)."""
    probe, adapter = SpyProbe(), SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=CATALOG,
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=probe,
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
    )
    asyncio.run(ctrl.run("task"))
    assert adapter.spawns == [f"omniroute/{DEAD}"]


def test_worker_starts_from_admitted_set_then_excludes_controller(tmp_path: Path) -> None:
    admitted = admitted_set().restrict_prefixes(["kr/"]).exclude_controller(CTRL)
    probe, adapter = SpyProbe(), SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=CATALOG,
        prime_selectors=SELECTORS,
        probe=probe,
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        admitted=admitted,
        require_admission=True,
    )
    assert [c.route_id for c in ctrl.candidates] == [OK_ROUTE]
    outcome = asyncio.run(ctrl.run("task"))
    assert outcome.state == "SUCCESS"
    assert adapter.spawns == [f"omniroute/{OK_ROUTE}"]
    assert admitted.first_failure(CTRL).first_failed_stage is AdmissionStage.CONTROLLER_EXCLUDED


def test_require_admission_without_set_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(AdmissionUnavailableError):
        eligible_worker_candidates(WorkerTask(), CATALOG, SELECTORS, require_admission=True)
    with pytest.raises(AdmissionUnavailableError):
        WorkerController(
            WorkerTask(),
            inventory_rows=CATALOG,
            prime_selectors=SELECTORS,
            probe=SpyProbe(),
            adapter=SpyAdapter(),
            cache=HealthCache(tmp_path / "h.json"),
            require_admission=True,
        )


def test_select_worker_model_never_probes_excluded_route(tmp_path: Path) -> None:
    probe = SpyProbe()
    with pytest.raises(NoHealthyWorkerModelError):
        select_worker_model(
            WorkerTask(),
            inventory_rows=CATALOG,
            prime_selectors=[f"omniroute/{DEAD}"],
            probe=probe,
            cache=HealthCache(tmp_path / "h.json"),
            now=NOW,
            admitted=admitted_set(),
        )
    assert probe.calls == []


def test_healthy_cache_cannot_readmit_excluded_route(tmp_path: Path) -> None:
    cache = HealthCache(tmp_path / "h.json")
    cache.record(f"omniroute/{DEAD}", HealthResult(True, "healthy"), now=NOW)
    candidates = eligible_worker_candidates(
        WorkerTask(), CATALOG, SELECTORS, admitted=admitted_set()
    )
    assert DEAD not in {c.route_id for c in candidates}


def test_replacement_never_leaves_admitted_set(tmp_path: Path) -> None:
    class FailingAdapter(SpyAdapter):
        async def collect(self, handle: Any) -> WorkerTerminal:
            return WorkerTerminal("done", None, True, error="HTTP 429 quota")

    admitted = admitted_set()
    adapter = FailingAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=CATALOG,
        prime_selectors=SELECTORS,
        probe=SpyProbe(),
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        admitted=admitted,
        require_admission=True,
    )
    asyncio.run(ctrl.run("task"))
    assert adapter.spawns and all(m.removeprefix("omniroute/") in admitted for m in adapter.spawns)
    assert f"omniroute/{DEAD}" not in adapter.spawns


def test_mutated_candidate_list_is_caught_before_spawn(tmp_path: Path) -> None:
    admitted = admitted_set()
    adapter = SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=CATALOG,
        prime_selectors=SELECTORS,
        probe=SpyProbe(),
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        admitted=admitted,
        require_admission=True,
    )
    legacy = eligible_worker_candidates(WorkerTask(), CATALOG, SELECTORS)
    ctrl.candidates = tuple(c for c in legacy if c.route_id == DEAD)
    outcome = asyncio.run(ctrl.run("task"))
    assert outcome.state == "FAIL_CLOSED" and adapter.spawns == []


def _patch_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, fail: bool = False) -> None:
    from verdict.orchestration import run as orch_run

    def fetch_connections(gateway: str, *, api_key: str | None, timeout: float = 30) -> list:
        if fail:
            raise OSError("gateway down")
        return CONNECTIONS

    monkeypatch.setattr(orch_run, "fetch_connections", fetch_connections)
    monkeypatch.setenv("VERDICT_HOME", str(tmp_path))
    (tmp_path / "orchestration-health.json").write_text(
        json.dumps(
            {
                "health": {},
                "cooldowns": {
                    f"route:{DEAD}": {
                        "until": "2999-01-01T00:00:00+00:00",
                        "category": "quota_exhausted",
                    }
                },
            }
        )
    )


def test_worker_cli_admission_applies_scope_and_active_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    monkeypatch.setenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", f"omniroute/{CTRL}")
    admitted = worker_runtime._worker_admission({"route_prefixes": "kr/,cc/"}, CATALOG)
    assert admitted.ids == frozenset({OK_ROUTE})
    receipt = admitted.receipt()
    assert receipt["controller_identity"] == CTRL
    by_id = {c["route_id"]: c for c in receipt["candidates"]}
    assert by_id[DEAD]["first_failed_stage"] == "AVAILABLE"
    assert by_id[DEAD]["source"].startswith("ladder_state")


def test_worker_cli_admission_unknown_controller_is_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    admitted = worker_runtime._worker_admission({}, CATALOG)
    assert admitted.receipt()["controller_identity"] == "unknown"
    assert OK_ROUTE in admitted and CTRL in admitted


def test_worker_cli_fails_closed_when_live_admission_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_gateway(monkeypatch, tmp_path, fail=True)

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            lines = ["PROVIDER MODEL"] + [f"omniroute {r['id']}" for r in CATALOG]
            return ("\n".join(lines).encode(), b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Proc:
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(worker_runtime, "fetch_omniroute_inventory", lambda: tuple(CATALOG))
    spawned: list[str] = []

    async def spawn(self: Any, prompt: str, *, name: str, model: str) -> Any:
        spawned.append(model)
        raise AssertionError("must not spawn")

    monkeypatch.setattr(worker_runtime.PrimeFileAdapter, "spawn", spawn)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))
    code = asyncio.run(worker_runtime.cli_run(run_dir))
    assert code == 1 and spawned == []
    assert "connection_evidence_unavailable" in capsys.readouterr().out
