"""Focused regressions for self-hosting review blockers. No provider/model calls."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration import controller_operations as operations
from verdict.orchestration.contracts import (
    CapacityClass,
    EligibilityStage,
    OrchestrationError,
    RouteVerdict,
    WorkerTerminal,
)
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.run import plan_with_failover
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy, subprocess_runner
from verdict.orchestration.supervisor import ControllerSupervisor


class RecordingExecutor:
    def __init__(self) -> None:
        self.timeout_seconds: float | None = None

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        self.timeout_seconds = timeout_seconds
        state = operations.read_state(Path(os.environ[operations.RUN_DIR_ENV]))
        active = [op for op in state["operations"].values() if op["status"] == "active"]
        assert len(active) == 1
        operation = active[0]
        assert operation["phase"] == "worker"
        assert operation["attempt_id"] == "node-1-a2"
        assert operation["deadline"] <= operation["started"] + 900
        return WorkerTerminal(ok=False, model=route_id, error="fixture")


def test_runtime_worker_attempt_publishes_bounded_worker_lease(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    operations.activate_generation(tmp_path, "token", operations.time.monotonic() + 1200)
    executor = RecordingExecutor()
    runtime = object.__new__(DagRuntime)
    runtime.executor = executor
    runtime.run_dir = tmp_path
    runtime.policy = RuntimePolicy(attempt_timeout_seconds=1200)

    terminal = asyncio.run(
        runtime._execute("prompt", "route", tmp_path, node_id="node-1", attempt=2)
    )

    assert terminal.error == "fixture"
    assert executor.timeout_seconds == operations.executor_timeout_within(900.0)
    state = operations.read_state(tmp_path)
    operations_list = list(state["operations"].values())
    assert len(operations_list) == 1
    assert operations_list[0]["status"] == "ended"


def test_worker_lease_validates_and_earliest_deadline_wins() -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    state = {
        "token": "token",
        "active": True,
        "operations": {
            "late": {
                "id": "late",
                "token": "token",
                "owner": owner,
                "phase": "worker",
                "attempt_id": "node-a1",
                "started": 0.0,
                "deadline": 900.0,
                "status": "active",
            },
            "early": {
                "id": "early",
                "token": "token",
                "owner": owner,
                "phase": "worker",
                "attempt_id": "node-a2",
                "started": 0.0,
                "deadline": 500.0,
                "status": "active",
            },
        },
    }

    result, operation = operations.OperationLeaseGuard().inspect(
        state, token="token", owner=owner, now=240, overall_deadline=1200
    )

    assert result == "WAITING_BOUNDED"
    assert operation is not None
    assert operation["id"] == "early"


def test_worker_lease_above_cap_is_invalid() -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    state = {
        "token": "token",
        "active": True,
        "operations": {
            "op": {
                "id": "op",
                "token": "token",
                "owner": owner,
                "phase": "worker",
                "attempt_id": "node-a1",
                "started": 0.0,
                "deadline": 901.0,
                "status": "active",
            }
        },
    }

    result, operation = operations.OperationLeaseGuard().inspect(
        state, token="token", owner=owner, now=240, overall_deadline=1200
    )

    assert result == "NONE"
    assert operation is None


def _set_fencing_env(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    monkeypatch.setenv(operations.DEADLINE_ENV, "123")
    monkeypatch.setenv(operations.LAUNCH_ENV, "launch")
    monkeypatch.setenv(operations.OPERATION_ENV, "op")


def test_unsupervised_worker_command_scrubs_inherited_fencing_env(tmp_path: Path) -> None:
    env = {key: "leaked" for key in operations.FENCING_ENV_KEYS if key != operations.RUN_DIR_ENV}

    command = operations.owned_worker_command(["payload"], env)

    assert command == ["payload"]
    assert not any(key in env for key in operations.FENCING_ENV_KEYS)


def test_launcher_preserves_fencing_env_until_registration_then_scrubs_payload(
    tmp_path: Path, monkeypatch: Any
) -> None:
    operations.activate_generation(tmp_path, "token", operations.time.monotonic() + 10)
    with operations._locked_state(tmp_path) as state:
        state["containment"] = {"fixture": True}
        state["pending"] = ["launch"]
    _set_fencing_env(monkeypatch, tmp_path)
    joined: list[str] = []
    captured: dict[str, str] = {}
    actual = operations.process_identity(os.getpid())
    assert actual is not None
    monkeypatch.setattr(operations, "join_containment", lambda record, token: joined.append(token))
    monkeypatch.setattr(
        operations, "process_identity", lambda pid: {**actual, "pgid": pid, "sid": pid}
    )

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> None:
        captured.update(env)
        raise RuntimeError("stop before exec")

    monkeypatch.setattr(operations.os, "execvpe", fake_execvpe)

    try:
        operations.register_worker(tmp_path, "token", "op", "launch", ["payload"])
    except RuntimeError as exc:
        assert str(exc) == "stop before exec"

    assert joined == ["token"]
    assert not any(key in captured for key in operations.FENCING_ENV_KEYS)


def test_runtime_subprocess_runner_scrubs_fencing_env(tmp_path: Path, monkeypatch: Any) -> None:
    _set_fencing_env(monkeypatch, tmp_path)
    code = (
        "import os; "
        f"keys={list(operations.FENCING_ENV_KEYS)!r}; "
        "print(any(key in os.environ for key in keys))"
    )

    code_exit, output = asyncio.run(subprocess_runner([sys.executable, "-c", code], tmp_path, 5))

    assert code_exit == 0
    assert output.strip() == "False"


def _worker_operation(
    owner: dict[str, Any],
    *,
    op_id: str = "op",
    attempt_id: str = "node-a1",
    deadline: float = 600.0,
) -> dict[str, Any]:
    return {
        "id": op_id,
        "token": "token",
        "owner": owner,
        "phase": "worker",
        "attempt_id": attempt_id,
        "started": 0.0,
        "deadline": deadline,
        "status": "active",
    }


def test_supervisor_worker_lease_prevents_false_stall(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]
        owner = operations.process_identity(os.getpid())
        assert owner is not None
        operations.activate_generation(tmp_path, "token", 1200.0)
        with operations._locked_state(tmp_path) as state:
            state["operations"]["op"] = _worker_operation(owner, deadline=600.0)

        async def advance(delay: float) -> None:
            ticks[0] += delay
            if ticks[0] >= 500:
                (tmp_path / "events.jsonl").write_text(
                    '{"seq":1,"type":"run_finished","data":{"outcome":"COMPLETE"}}\n'
                )

        supervisor = ControllerSupervisor(
            lambda gen: [],
            tmp_path,
            stall_seconds=240,
            poll_seconds=10,
            clock=lambda: ticks[0],
            sleep=advance,
        )
        supervisor._token = "token"
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        record: dict[str, Any] = {"identity": owner}

        result = await supervisor._watch(record, tmp_path / "log", waiter, 1200)

        assert result.kind == "FINISHED"
        assert record["wait_state"] == "WAITING_BOUNDED"

    asyncio.run(main())


def test_supervisor_worker_lease_expiry_stops_wait(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]
        owner = operations.process_identity(os.getpid())
        assert owner is not None
        operations.activate_generation(tmp_path, "token", 1200.0)
        with operations._locked_state(tmp_path) as state:
            state["operations"]["op"] = _worker_operation(owner, deadline=200.0)

        async def advance(delay: float) -> None:
            ticks[0] += delay

        supervisor = ControllerSupervisor(
            lambda gen: [],
            tmp_path,
            stall_seconds=240,
            poll_seconds=10,
            clock=lambda: ticks[0],
            sleep=advance,
        )
        supervisor._token = "token"
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        record: dict[str, Any] = {"identity": owner}

        result = await supervisor._watch(record, tmp_path / "log", waiter, 1200)

        assert result.kind == "OPERATION_EXPIRED"
        assert ticks[0] == 200

    asyncio.run(main())


def test_supervisor_worker_lease_rekey_does_not_extend_wait(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]
        owner = operations.process_identity(os.getpid())
        assert owner is not None
        operations.activate_generation(tmp_path, "token", 1200.0)
        with operations._locked_state(tmp_path) as state:
            state["operations"]["op"] = _worker_operation(owner, deadline=600.0)

        async def advance(delay: float) -> None:
            ticks[0] += delay
            if ticks[0] == 10:
                with operations._locked_state(tmp_path) as state:
                    original = state["operations"]["op"]
                    original["status"] = "ended"
                    state["operations"] = {
                        "replacement": {
                            **original,
                            "id": "replacement",
                            "status": "active",
                            "started": 10.0,
                            "deadline": 610.0,
                        }
                    }

        supervisor = ControllerSupervisor(
            lambda gen: [],
            tmp_path,
            stall_seconds=240,
            poll_seconds=10,
            clock=lambda: ticks[0],
            sleep=advance,
        )
        supervisor._token = "token"
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        record: dict[str, Any] = {"identity": owner}

        result = await supervisor._watch(record, tmp_path / "log", waiter, 1200)

        assert result.kind == "STALLED"
        assert ticks[0] == 240

    asyncio.run(main())


def test_ownership_refused_classifies_local_block() -> None:
    failure = FailureIntelligence().classify(
        WorkerTerminal(ok=False, error="ownership_refused: cgroup unavailable", status_code=125),
        now=datetime.now(timezone.utc),
    )

    assert failure.category == "ownership_refused"
    assert failure.action == "BLOCK"
    assert failure.scope == "none"
    assert failure.cooldown_seconds == 0


def test_runtime_execute_keeps_ownership_refused_local(tmp_path: Path) -> None:
    class RefusingExecutor:
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            raise operations.OwnershipError("fixture refusal")

    runtime = object.__new__(DagRuntime)
    runtime.executor = RefusingExecutor()
    runtime.run_dir = tmp_path
    runtime.policy = RuntimePolicy(attempt_timeout_seconds=1)

    terminal = asyncio.run(runtime._execute("prompt", "route", tmp_path, node_id="node", attempt=1))

    assert terminal.error == "ownership_refused: fixture refusal"
    assert terminal.status_code == 125


def test_plan_with_failover_blocks_on_ownership_refused_without_retry_or_cooldown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    class Selector:
        def __init__(self) -> None:
            self.selects = 0
            self.failures = 0

        def select(
            self, requirements: object, *, now: object
        ) -> tuple[RouteVerdict | None, list[RouteVerdict]]:
            del requirements, now
            self.selects += 1
            choice = RouteVerdict(
                route_id="cc/model",
                provider="cc",
                reached=EligibilityStage.SELECTED,
                failed_stage=None,
                reason="fixture",
                capacity_class=CapacityClass.SUBSCRIPTION,
                context_window=200_000,
            )
            return choice, [choice]

        def record_failure(self, route_id: str, failure: object, *, now: object) -> None:
            del route_id, failure, now
            self.failures += 1

    class RefusingExecutor:
        async def run(
            self,
            prompt: str,
            *,
            route_id: str,
            cwd: Path,
            timeout_seconds: float,
            tools: bool = True,
        ) -> WorkerTerminal:
            del prompt, route_id, cwd, timeout_seconds, tools
            return WorkerTerminal(ok=False, error="ownership_refused: fixture", status_code=125)

    class Events:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict[str, Any]]] = []

        def emit(self, event_type: str, **data: Any) -> None:
            self.rows.append((event_type, data))

    async def fake_plan(
        self: object,
        goal: str,
        *,
        repo: Path,
        executor: object,
        route_id: str,
        timeout_seconds: float,
        constraints: str = "",
        events: object | None = None,
        run_dir: Path | None = None,
        attempt: int = 1,
        max_attempts: int = 6,
    ) -> object:
        from verdict.orchestration.planner import PlanningExecutorError

        del (
            self,
            goal,
            repo,
            executor,
            route_id,
            timeout_seconds,
            constraints,
            events,
            run_dir,
            attempt,
            max_attempts,
        )
        terminal = WorkerTerminal(ok=False, error="ownership_refused: fixture", status_code=125)
        raise PlanningExecutorError("fixture refused", terminal)

    from verdict.orchestration import run as run_module

    monkeypatch.setattr(run_module.FrontierPlanner, "plan", fake_plan)
    selector = Selector()
    events = Events()

    try:
        asyncio.run(
            plan_with_failover(
                "goal",
                repo=tmp_path,
                selector=selector,
                executor=RefusingExecutor(),
                classifier=FailureIntelligence(),
                events=events,
                run_dir=tmp_path / "run",
                max_attempts=3,
                timeout_seconds=1,
            )
        )
    except OrchestrationError as exc:
        assert "ownership_refused" in str(exc)
    else:
        raise AssertionError("plan_with_failover should block on ownership_refused")

    assert selector.selects == 1
    assert selector.failures == 0
    assert not [row for row in events.rows if row[0] == "cooldown"]


def test_supervisor_preflights_containment_before_provider_work(tmp_path: Path) -> None:
    async def spawn(argv: list[str], *, log_path: Path, env: dict[str, str] | None) -> object:
        raise AssertionError("controller spawned before containment preflight")

    outcome = asyncio.run(
        ControllerSupervisor(
            lambda gen: ["must-not-start"], tmp_path, spawner=spawn, require_worker_containment=True
        ).run()
    )

    assert outcome.state == "BLOCKED"
    assert "VERDICT_CONTROLLER_CGROUP_ROOT" in outcome.reason
    assert outcome.generations
    assert outcome.generations[0]["state"] == "CLEANUP_BLOCKED"


def test_controller_lock_wait_is_bounded(tmp_path: Path, monkeypatch: Any) -> None:
    attempts = [0]
    ticks = [0.0]

    def fake_monotonic() -> float:
        return ticks[0]

    def fake_sleep(delay: float) -> None:
        ticks[0] += delay

    def fake_flock(lock: object, flags: int) -> None:
        attempts[0] += 1
        raise BlockingIOError

    monkeypatch.setattr(operations.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(operations.time, "sleep", fake_sleep)
    monkeypatch.setattr(operations.fcntl, "flock", fake_flock)

    try:
        with operations._locked_state(tmp_path):
            raise AssertionError("lock should not be acquired")
    except operations.OwnershipError as exc:
        assert "lock unavailable" in str(exc)

    assert 2.0 <= ticks[0] <= 2.05
    assert attempts[0] > 1
