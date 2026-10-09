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


def _patch_catalog(monkeypatch: pytest.MonkeyPatch, rows: Any) -> None:
    """cli_run reads the catalog once through OmniRouteHTTPTransport; fake it."""

    class FakeTransport:
        base_url = "http://127.0.0.1:20128"

        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def catalog(self) -> Any:
            return {"data": [dict(r) for r in rows]}

        def runtime(self) -> Any:
            return {}

    monkeypatch.setattr(worker_runtime, "OmniRouteHTTPTransport", FakeTransport)


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
    _patch_catalog(monkeypatch, CATALOG)
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


# ---------------------------------------------------------------- launch gate
class FailingProbe(SpyProbe):
    def __call__(self, candidate: LaunchCandidate) -> HealthResult:
        self.calls.append(candidate.route_id)
        return HealthResult(False, "rate_limited", 429)


def _fresh_host_set(tmp_path: Path) -> Any:
    from verdict.admission import default_runtime_evidence

    runtime = default_runtime_evidence(now=NOW, state_dir=tmp_path / "no-state")
    return admit([row(DEAD)], CONNECTIONS, runtime, now=NOW)


def test_unverified_dead_route_gets_one_probe_then_zero_spawns(tmp_path: Path) -> None:
    admitted = _fresh_host_set(tmp_path)
    assert DEAD in admitted and not admitted.launchable(DEAD)
    probe, adapter = FailingProbe(), SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=[row(DEAD)],
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=probe,
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        now=lambda: NOW,
        admitted=admitted,
        require_admission=True,
    )
    outcome = asyncio.run(ctrl.run("task"))
    assert outcome.state == "FAIL_CLOSED"
    assert probe.calls == [DEAD] and adapter.spawns == []
    record = ctrl.admitted.first_failure(DEAD)  # type: ignore[union-attr]
    assert record.first_failed_stage is AdmissionStage.HEALTHY
    assert record.reason == "rate_limited"
    assert record.source == worker_runtime.CONFIRMATION_SOURCE
    assert record.observed_at == NOW.isoformat()
    confirms = [e for e in ctrl.events if e["event"] == "confirmation"]
    assert confirms and confirms[0]["confirmed"] is False


def test_cached_healthy_is_not_confirmation_for_unverified_route(tmp_path: Path) -> None:
    admitted = _fresh_host_set(tmp_path)
    cache = HealthCache(tmp_path / "h.json")
    cache.record(f"omniroute/{DEAD}", HealthResult(True, "healthy"), now=NOW)
    probe, adapter = FailingProbe(), SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=[row(DEAD)],
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=probe,
        adapter=adapter,
        cache=cache,
        now=lambda: NOW,
        admitted=admitted,
        require_admission=True,
    )
    asyncio.run(ctrl.run("task"))
    assert probe.calls == [DEAD] and adapter.spawns == []


def test_unverified_route_with_successful_probe_launches_confirmed(tmp_path: Path) -> None:
    admitted = admit([row(OK_ROUTE)], CONNECTIONS, RuntimeEvidence((), ("x:absent",)), now=NOW)
    probe, adapter = SpyProbe(), SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=[row(OK_ROUTE)],
        prime_selectors=[f"omniroute/{OK_ROUTE}"],
        probe=probe,
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        now=lambda: NOW,
        admitted=admitted,
        require_admission=True,
    )
    outcome = asyncio.run(ctrl.run("task"))
    assert outcome.state == "SUCCESS" and adapter.spawns == [f"omniroute/{OK_ROUTE}"]
    authority = ctrl.admitted.launch_authority(OK_ROUTE)  # type: ignore[union-attr]
    assert authority["basis"] == "live_confirmation"
    assert authority["source"] == worker_runtime.CONFIRMATION_SOURCE


def test_spawn_gate_rejects_admitted_but_unconfirmed_route(tmp_path: Path) -> None:
    """Asserted precondition: skipping confirmation cannot reach spawn."""
    admitted = _fresh_host_set(tmp_path)
    adapter = SpyAdapter()
    ctrl = WorkerController(
        WorkerTask(),
        inventory_rows=[row(DEAD)],
        prime_selectors=[f"omniroute/{DEAD}"],
        probe=SpyProbe(),
        adapter=adapter,
        cache=HealthCache(tmp_path / "h.json"),
        now=lambda: NOW,
        admitted=admitted,
        require_admission=True,
    )

    class Reverting:
        """Undo every confirmation, as a buggy caller could."""

        def __get__(self, obj: Any, owner: Any) -> Any:
            return admitted

        def __set__(self, obj: Any, value: Any) -> None:
            pass

    type(ctrl).admitted = Reverting()  # type: ignore[assignment]
    try:
        outcome = asyncio.run(ctrl.run("task"))
    finally:
        del type(ctrl).admitted
    assert outcome.state == "FAIL_CLOSED" and adapter.spawns == []
    assert "worker_runtime.spawn" in outcome.diagnostic


def test_execute_with_worker_failover_applies_launch_gate(tmp_path: Path) -> None:
    from verdict.subagent_selection import execute_with_worker_failover

    executed: list[str] = []

    async def execute(model: str) -> WorkerTerminal:
        executed.append(model)
        return WorkerTerminal("done", "OK", True, stop_reason="stop")

    probe = FailingProbe()
    with pytest.raises(NoHealthyWorkerModelError):
        asyncio.run(
            execute_with_worker_failover(
                WorkerTask(),
                inventory_rows=[row(DEAD)],
                prime_selectors=[f"omniroute/{DEAD}"],
                probe=probe,
                execute=execute,
                cache=HealthCache(tmp_path / "h.json"),
                now=lambda: NOW,
                admitted=_fresh_host_set(tmp_path),
                require_admission=True,
            )
        )
    assert probe.calls == [DEAD] and executed == []


def test_cli_run_end_to_end_rejects_dead_route_with_zero_spawns(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Real worker_runtime.cli_run: patched fetchers, no sockets, spawn spy."""
    from verdict.orchestration import run as orch_run

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _patch_gateway(monkeypatch, tmp_path)  # cc connection active; ladder: cc/dead cooling
    monkeypatch.setattr(orch_run, "resolve_api_key", lambda *a, **k: None)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    _patch_catalog(monkeypatch, (row(DEAD), row(OK_ROUTE)))

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            # Only the dead route is Prime-visible: without admission it would launch.
            return (f"PROVIDER MODEL\nomniroute {DEAD}".encode(), b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Proc:
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    probes: list[str] = []

    def probe_factory(*a: Any, **k: Any) -> Any:
        def probe(candidate: LaunchCandidate) -> HealthResult:
            probes.append(candidate.route_id)
            return HealthResult(True, "healthy")

        return probe

    monkeypatch.setattr(worker_runtime, "openai_health_probe", probe_factory)
    spawns: list[str] = []

    async def spawn(self: Any, prompt: str, *, name: str, model: str) -> Any:
        spawns.append(model)
        return {"rlm_child_id": name, "model": model}

    async def collect(self: Any, handle: Any) -> WorkerTerminal:
        return WorkerTerminal("done", "OK", True, stop_reason="stop")

    async def delete(self: Any, handle: Any) -> None:
        return None

    monkeypatch.setattr(worker_runtime.PrimeFileAdapter, "spawn", spawn)
    monkeypatch.setattr(worker_runtime.PrimeFileAdapter, "collect", collect)
    monkeypatch.setattr(worker_runtime.PrimeFileAdapter, "delete", delete)

    import socket

    def no_socket(*a: Any, **k: Any) -> Any:
        raise AssertionError("no network in this test")

    monkeypatch.setattr(socket, "create_connection", no_socket)
    monkeypatch.setattr(socket.socket, "connect", no_socket)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))
    code = asyncio.run(worker_runtime.cli_run(run_dir))

    assert code == 1
    assert spawns == []
    assert DEAD not in probes
    receipt = json.loads((run_dir / "admission.json").read_text())
    assert DEAD not in receipt["admitted"]
    dead = next(c for c in receipt["candidates"] if c["route_id"] == DEAD)
    assert dead["admitted"] is False
    assert dead["first_failed_stage"] == "AVAILABLE"
    assert dead["source"].startswith("ladder_state")
    outcome = json.loads((run_dir / "outcome.json").read_text())
    assert outcome["state"] == "FAIL_CLOSED"


def test_worker_cli_admission_records_capability_drops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_gateway(monkeypatch, tmp_path)
    monkeypatch.delenv("VERDICT_ACTIVE_CONTROLLER_ROUTE", raising=False)
    small = {**row("kr/small"), "context_length": 8_000}
    no_tools = {**row("kr/no-tools"), "capabilities": {}}
    config = {"task": {"required_capabilities": ["tools"], "min_context_tokens": 100_000}}
    admitted = worker_runtime._worker_admission(config, [*CATALOG, small, no_tools])
    assert admitted.first_failure("kr/small").first_failed_stage is AdmissionStage.CAPABILITY
    assert admitted.first_failure("kr/small").reason == "insufficient_context"
    assert admitted.first_failure("kr/no-tools").reason == "missing_capability:tools"
    assert OK_ROUTE in admitted


def test_worker_admission_gateway_comes_from_the_bootstrap_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no explicit runtime gateway, the worker uses the contract's gateway_url."""
    import verdict.admission as adm
    import verdict.worker_runtime as wr

    home = tmp_path / "home"
    cfg = home / ".config" / "verdict"
    cfg.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("OMNIROUTE_BASE_URL", raising=False)
    (cfg / "verdict.yaml").write_text(
        "gateway_url: http://127.0.0.1:29999\n"
        "providers:\n  omniroute:\n    base_url: http://127.0.0.1:29999/v1\n",
        encoding="utf-8",
    )
    seen: list[str] = []

    def fake_load(gateway: str, **kwargs: Any) -> Any:
        seen.append(gateway)
        raise adm.AdmissionUnavailableError("connection_evidence_unavailable", "stop")

    monkeypatch.setattr(wr, "load_live_admission", fake_load)
    with pytest.raises(adm.AdmissionUnavailableError):
        wr._worker_admission({"task": {}}, [])
    assert seen == ["http://127.0.0.1:29999"]


def test_worker_cli_default_never_syncs_visibility_flag_opts_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """cli_run(sync_visibility=False) (the default) never calls
    refresh_omniroute_visibility; cli_run(sync_visibility=True) calls it
    exactly once. Both runs otherwise fail closed on the same patched
    gateway (fetch_connections raises), so only the visibility call itself
    is under test."""
    import verdict.harness_prime as harness_prime
    from verdict.prime_inventory import PrimeInventoryStatus

    _patch_gateway(monkeypatch, tmp_path, fail=True)

    class Proc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            lines = ["PROVIDER MODEL"] + [f"omniroute {r['id']}" for r in CATALOG]
            return ("\n".join(lines).encode(), b"")

    async def fake_exec(*args: Any, **kwargs: Any) -> Proc:
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    _patch_catalog(monkeypatch, CATALOG)

    calls: list[bool] = []

    def recorder(*, fetch_rows: Any, source: str, force: bool = False, **kwargs: Any) -> Any:
        calls.append(force)
        return PrimeInventoryStatus(None, None, refreshed=False, fresh=False)

    monkeypatch.setattr(harness_prime, "refresh_omniroute_visibility", recorder)

    default_dir = tmp_path / "run-default"
    default_dir.mkdir()
    (default_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))
    asyncio.run(worker_runtime.cli_run(default_dir))
    assert calls == []  # default: read-only, never calls the sync helper

    flagged_dir = tmp_path / "run-flagged"
    flagged_dir.mkdir()
    (flagged_dir / "config.json").write_text(json.dumps({"prompt": "p", "task": {}}))
    asyncio.run(worker_runtime.cli_run(flagged_dir, sync_visibility=True))
    assert calls == [True]  # --sync-visibility: calls it once, force=True
