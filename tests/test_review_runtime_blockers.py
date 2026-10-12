"""Focused regressions for self-hosting review blockers. No provider/model calls."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from verdict.orchestration import controller_operations as operations
from verdict.orchestration.contracts import WorkerTerminal
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy


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
