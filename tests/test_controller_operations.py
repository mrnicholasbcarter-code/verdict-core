"""Offline causal operation/ownership regressions. No provider/model calls."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration import controller_operations as operations
from verdict.orchestration.supervisor import ControllerSupervisor


def lease(owner: dict[str, Any], deadline: float = 600.0) -> dict[str, Any]:
    return {
        "id": "op",
        "token": "token",
        "owner": owner,
        "phase": "planner",
        "attempt_id": "plan-1",
        "started": 0.0,
        "deadline": deadline,
        "status": "active",
    }


def test_fake_clock_planner_600_survives_stall_240(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]
        owner = operations.process_identity(os.getpid())
        assert owner is not None
        operations.activate_generation(tmp_path, "token", 1200.0)
        with operations._locked_state(tmp_path) as state:
            state["operations"]["op"] = lease(owner)

        async def advance(delay: float) -> None:
            ticks[0] += delay
            if ticks[0] >= 500:
                (tmp_path / "events.jsonl").write_text(
                    json.dumps({"seq": 1, "type": "run_finished", "data": {"outcome": "COMPLETE"}})
                    + "\n"
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
        assert ticks[0] == 500
        assert record["wait_state"] == "WAITING_BOUNDED"
        assert not (tmp_path / "progress.json").exists()

    asyncio.run(main())


@pytest.mark.parametrize(
    "deadline, expected, at",
    [
        (600.0, "EXPIRED", 600),
        (601.0, "NONE", 240),
        (float("inf"), "NONE", 240),
        (-1.0, "NONE", 240),
    ],
)
def test_lease_expiry_and_untrusted_first_read(deadline: float, expected: str, at: float) -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    state = {"token": "token", "active": True, "operations": {"op": lease(owner, deadline)}}
    result, _ = operations.OperationLeaseGuard().inspect(
        state, token="token", owner=owner, now=at, overall_deadline=1200
    )
    assert result == expected


def test_lease_cannot_extend_or_reopen() -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    operation = lease(owner)
    state = {"token": "token", "active": True, "operations": {"op": operation}}
    guard = operations.OperationLeaseGuard()

    def inspect() -> str:
        return guard.inspect(state, token="token", owner=owner, now=240, overall_deadline=1200)[0]

    assert inspect() == "WAITING_BOUNDED"
    operation["deadline"] = 601
    assert inspect() == "NONE"
    operation["deadline"] = 600
    assert inspect() == "NONE"
    operation["status"] = "ended"
    assert inspect() == "NONE"
    operation["status"] = "active"
    assert inspect() == "NONE"


def test_scope_closes_exact_lease_on_error(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    operations.activate_generation(tmp_path, "token", 1200)
    with (
        pytest.raises(RuntimeError, match="body failed"),
        operations.operation_scope(
            tmp_path, phase="planner", attempt_id="plan-1", timeout_seconds=600, clock=lambda: 0
        ) as op_id,
    ):
        snapshot = operations.read_state(tmp_path)
        assert snapshot["operations"][op_id]["deadline"] == 600
        raise RuntimeError("body failed")
    assert operations.read_state(tmp_path)["operations"][op_id]["status"] == "ended"


def test_deadline_wins_over_late_completion(tmp_path: Path) -> None:
    async def main() -> None:
        (tmp_path / "events.jsonl").write_text(
            json.dumps({"seq": 1, "type": "run_finished", "data": {"outcome": "COMPLETE"}}) + "\n"
        )
        supervisor = ControllerSupervisor(lambda gen: [], tmp_path, clock=lambda: 601)
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        result = await supervisor._watch({}, tmp_path / "log", waiter, 600)
        assert result.kind == "DEADLINE"

    asyncio.run(main())


def test_no_lease_stalls_with_fake_clock(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]

        async def advance(delay: float) -> None:
            ticks[0] += delay

        supervisor = ControllerSupervisor(
            lambda gen: [],
            tmp_path,
            clock=lambda: ticks[0],
            sleep=advance,
            poll_seconds=10,
            stall_seconds=240,
        )
        waiter: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        result = await supervisor._watch({}, tmp_path / "log", waiter, 1200)
        assert result.kind == "STALLED" and ticks[0] == 240

    asyncio.run(main())


def test_revoked_generation_prevents_payload_exec(tmp_path: Path, monkeypatch: Any) -> None:
    async def fake_kill(record: dict[str, Any], token: str, timeout: float) -> None:
        return None

    monkeypatch.setattr(operations, "kill_containment", fake_kill)

    async def main() -> None:
        operations.activate_generation(tmp_path, "token", operations.time.monotonic() + 10)
        env = {**os.environ, operations.RUN_DIR_ENV: str(tmp_path), operations.TOKEN_ENV: "token"}
        marker = tmp_path / "payload"
        with operations._locked_state(tmp_path) as state:
            state["containment"] = {"fixture": True}
        command = operations.owned_worker_command(
            [sys.executable, "-c", f"from pathlib import Path;Path({str(marker)!r}).touch()"], env
        )
        operations.revoke_generation(tmp_path, "token")
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            start_new_session=True,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate()
        assert process.returncode == 125
        assert not marker.exists()
        with pytest.raises(operations.OwnershipError, match="pending"):
            await operations.cleanup_workers(tmp_path)

    asyncio.run(main())


def test_pid_identity_mismatch_never_signals_unrelated(tmp_path: Path, monkeypatch: Any) -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    # A stale PID record for this test process must not kill the test process.
    stale = {**owner, "start": owner["start"] - 1, "pgid": os.getpid(), "sid": os.getpid()}
    operations.activate_generation(tmp_path, "token", 1200)
    operations.revoke_generation(tmp_path, "token")
    with operations._locked_state(tmp_path) as state:
        state["workers"] = [{"token": "token", "identity": stale}]
    signalled: list[Any] = []
    monkeypatch.setattr(signal, "pidfd_send_signal", lambda *args: signalled.append(args))
    with pytest.raises(operations.OwnershipError, match="identity changed"):
        asyncio.run(operations.cleanup_workers(tmp_path))
    assert signalled == []


def test_detached_known_process_without_containment_is_killed_but_blocks(tmp_path: Path) -> None:
    async def main() -> None:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time;time.sleep(60)", start_new_session=True
        )
        try:
            identity = operations.process_identity(process.pid)
            assert identity is not None
            operations.activate_generation(tmp_path, "token", 1200)
            operations.revoke_generation(tmp_path, "token")
            with operations._locked_state(tmp_path) as state:
                state["workers"] = [{"token": "token", "identity": identity}]
            with pytest.raises(operations.OwnershipError, match="containment absence unverified"):
                await operations.cleanup_workers(tmp_path)
            await asyncio.wait_for(process.wait(), 1)
            assert operations.read_state(tmp_path)["workers"]
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(main())


def test_unresolved_launch_blocks_replacement(tmp_path: Path) -> None:
    async def main() -> None:
        operations.activate_generation(tmp_path, "token", 1200)
        with operations._locked_state(tmp_path) as state:
            state["pending"].append("spawn-registration-race")
        operations.revoke_generation(tmp_path, "token")
        with pytest.raises(operations.OwnershipError, match="pending"):
            await operations.cleanup_workers(tmp_path)
        with pytest.raises(operations.OwnershipError, match="cleaned"):
            operations.activate_generation(tmp_path, "next", 1200)

    asyncio.run(main())


@pytest.mark.parametrize("locked", [False, True])
def test_contained_cleanup_precedes_pending_or_lock_error(
    tmp_path: Path, monkeypatch: Any, locked: bool
) -> None:
    operations.activate_generation(tmp_path, "token", 1200)
    with operations._locked_state(tmp_path) as state:
        state["containment"] = {"fixture": True}
        state["pending"].append("unresolved")
    snapshot = operations.read_state(tmp_path)
    killed: list[str] = []

    async def fake_kill(record: dict[str, Any], token: str, timeout: float) -> None:
        killed.append(token)

    monkeypatch.setattr(operations, "kill_containment", fake_kill)
    if locked:
        import fcntl

        with (tmp_path / ".controller-operations.lock").open("a") as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with pytest.raises(operations.OwnershipError, match="lock unavailable"):
                operations.revoke_generation(tmp_path, "token")
            with pytest.raises(operations.OwnershipError, match="revocation unverified"):
                asyncio.run(
                    operations.cleanup_workers(
                        tmp_path, snapshot=snapshot, revocation_verified=False
                    )
                )
    else:
        operations.revoke_generation(tmp_path, "token")
        with pytest.raises(operations.OwnershipError, match="pending"):
            asyncio.run(operations.cleanup_workers(tmp_path))
    assert killed == ["token"]


def test_bounded_executor_call_times_out_whole_call(tmp_path: Path) -> None:
    from verdict.orchestration.contracts import WorkerTerminal

    class SlowExecutor:
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            await asyncio.sleep(60)
            return WorkerTerminal(ok=True)

    terminal = asyncio.run(
        operations.bounded_executor_call(
            SlowExecutor(),
            "test",
            route_id="fake",
            cwd=tmp_path,
            timeout_seconds=0.01,
            run_dir=None,
            phase="planner",
            attempt_id="plan-1",
        )
    )
    assert not terminal.ok and terminal.error == "timeout"


def test_same_logical_attempt_cannot_rekey() -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    original = lease(owner)
    state = {"token": "token", "active": True, "operations": {"op": original}}
    guard = operations.OperationLeaseGuard()
    assert (
        guard.inspect(state, token="token", owner=owner, now=10, overall_deadline=1200)[0]
        == "WAITING_BOUNDED"
    )
    original["status"] = "ended"
    guard.inspect(state, token="token", owner=owner, now=10, overall_deadline=1200)
    state["operations"] = {
        "replacement": {
            **original,
            "id": "replacement",
            "status": "active",
            "started": 10,
            "deadline": 610,
        }
    }
    assert (
        guard.inspect(state, token="token", owner=owner, now=10, overall_deadline=1200)[0] == "NONE"
    )


def test_supervised_prime_missing_explicit_delegation_fails_closed(tmp_path: Path) -> None:
    operations.activate_generation(tmp_path, "token", operations.time.monotonic() + 10)
    env = {operations.RUN_DIR_ENV: str(tmp_path), operations.TOKEN_ENV: "token"}
    with pytest.raises(operations.OwnershipError, match="explicit"):
        operations.owned_worker_command(["must-not-execute"], env)
    assert not operations.read_state(tmp_path)["pending"]


def test_regular_directory_cannot_spoof_cgroup(tmp_path: Path) -> None:
    for name in ("cgroup.type", "cgroup.kill", "cgroup.procs", "cgroup.events"):
        (tmp_path / name).touch()
    with pytest.raises(operations.OwnershipError, match="cgroup2 mount"):
        operations.prepare_containment(tmp_path, "a" * 32)
    assert not (tmp_path / ("verdict-" + "a" * 32)).exists()


def test_fixture_cgroup_kill_waits_for_populated_zero(tmp_path: Path, monkeypatch: Any) -> None:
    # Fake filesystem contract; this is NOT live cgroup evidence.
    (tmp_path / "cgroup.kill").touch()
    (tmp_path / "cgroup.events").write_text("populated 1\n")
    monkeypatch.setattr(operations, "_containment_path", lambda record, token: tmp_path)
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float) -> None:
        assert (tmp_path / "cgroup.kill").read_text() == "1"
        (tmp_path / "cgroup.events").write_text("populated 0\n")
        await real_sleep(0)

    monkeypatch.setattr(operations.asyncio, "sleep", fake_sleep)
    asyncio.run(operations.kill_containment({}, "fixture", 0.1))
    assert (tmp_path / "cgroup.events").read_text() == "populated 0\n"


def test_fixture_cgroup_never_empty_fails_closed(tmp_path: Path, monkeypatch: Any) -> None:
    (tmp_path / "cgroup.kill").touch()
    (tmp_path / "cgroup.events").write_text("populated 1\n")
    monkeypatch.setattr(operations, "_containment_path", lambda record, token: tmp_path)
    with pytest.raises(operations.OwnershipError, match="absence"):
        asyncio.run(operations.kill_containment({}, "fixture", 0.01))


def test_cancel_during_spawn_revokes_and_terminates(tmp_path: Path) -> None:
    async def main() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        class FakeProcess:
            pid = None
            returncode: int | None = None

            def __init__(self) -> None:
                self.done: asyncio.Future[int] = asyncio.get_running_loop().create_future()

            async def wait(self) -> int:
                return await self.done

            def terminate(self) -> None:
                self.returncode = -15
                self.done.set_result(-15)

        child = FakeProcess()

        async def spawn(argv: list[str], **kwargs: Any) -> FakeProcess:
            entered.set()
            await release.wait()
            return child

        supervisor = ControllerSupervisor(
            lambda gen: ["fake"], tmp_path, spawner=spawn, kill_grace_seconds=0.1
        )
        task = asyncio.create_task(supervisor.run())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert child.returncode == -15
        assert operations.read_state(tmp_path)["active"] is False

    asyncio.run(main())


@pytest.mark.parametrize("cause", ["pending_spawn", "live_owner", "revoke_error"])
def test_startup_uncertainty_still_kills_known_containment(
    tmp_path: Path, monkeypatch: Any, cause: str
) -> None:
    operations.activate_generation(tmp_path, "token", 1200)
    with operations._locked_state(tmp_path) as state:
        state["containment"] = {"fixture": True}
        if cause == "live_owner":
            state["controller_spawn_pending"] = False
            state["controller"] = operations.process_identity(os.getpid())
    killed: list[str] = []

    async def fake_kill(record: dict[str, Any], token: str, timeout: float) -> None:
        killed.append(token)

    monkeypatch.setattr(operations, "kill_containment", fake_kill)
    if cause == "revoke_error":
        from verdict.orchestration import supervisor as module

        def failed_revoke(run_dir: Path, token: str) -> None:
            raise operations.OwnershipError("fixture lock unavailable")

        monkeypatch.setattr(module, "revoke_generation", failed_revoke)
    outcome = asyncio.run(ControllerSupervisor(lambda gen: ["must-not-start"], tmp_path).run())
    assert outcome.state == "BLOCKED" and outcome.restarts == 0
    assert killed == ["token"]
    assert not outcome.generations


def test_launcher_joins_before_exec_gate_lock(tmp_path: Path, monkeypatch: Any) -> None:
    import fcntl

    operations.activate_generation(tmp_path, "token", operations.time.monotonic() + 10)
    with operations._locked_state(tmp_path) as state:
        state["containment"] = {"fixture": True}
        state["pending"] = ["launch"]
    joined: list[str] = []
    monkeypatch.setattr(operations, "join_containment", lambda record, token: joined.append(token))
    actual = operations.process_identity(os.getpid())
    assert actual is not None
    monkeypatch.setattr(
        operations, "process_identity", lambda pid: {**actual, "pgid": pid, "sid": pid}
    )
    with (tmp_path / ".controller-operations.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        with pytest.raises(operations.OwnershipError, match="lock unavailable"):
            operations.register_worker(tmp_path, "token", "", "launch")
    assert joined == ["token"]
    assert operations.read_state(tmp_path)["pending"] == ["launch"]


@pytest.mark.parametrize("first_ended", [False, True])
def test_rekey_owner_state_and_extra_keys_do_not_renew(first_ended: bool) -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    operation = lease(owner)
    if first_ended:
        operation["status"] = "ended"
    state = {"token": "token", "active": True, "operations": {"op": operation}}
    guard = operations.OperationLeaseGuard()
    guard.inspect(state, token="token", owner=owner, now=20, overall_deadline=1200)
    replacement = {
        **operation,
        "id": "new",
        "status": "active",
        "started": 20,
        "deadline": 620,
        "owner": {**owner, "state": "S", "extra": "not identity"},
    }
    state["operations"] = {"new": replacement}
    assert (
        guard.inspect(state, token="token", owner=owner, now=20, overall_deadline=1200)[0] == "NONE"
    )


def test_startup_verified_revoke_reads_revoked_state_before_cleanup(
    tmp_path: Path, monkeypatch: Any
) -> None:
    operations.activate_generation(tmp_path, "token", 1200)
    operations.bind_controller(tmp_path, "token", None)
    with operations._locked_state(tmp_path) as state:
        state["containment"] = {"fixture": True}
    killed: list[str] = []

    async def fake_kill(record: dict[str, Any], token: str, timeout: float) -> None:
        killed.append(token)
        raise operations.OwnershipError("fixture verification stops here")

    monkeypatch.setattr(operations, "kill_containment", fake_kill)
    outcome = asyncio.run(ControllerSupervisor(lambda gen: ["must-not-start"], tmp_path).run())
    assert killed == ["token"]
    assert "fixture verification" in outcome.reason
    assert outcome.state == "BLOCKED"
