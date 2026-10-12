"""Review phase bounded lifecycle regressions. No provider/model calls."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_orch_review import FakeRunner, FakeSelector, _load_fixture, _run, _verdict
from tests.test_orch_runtime import Classifier, Events, Executor, Selector, node
from verdict.orchestration import controller_operations as operations
from verdict.orchestration.contracts import ReviewResult
from verdict.orchestration.review import (
    OcrRun,
    OpenCodeReviewer,
    _run_runner_bounded,
    _run_with_watchdog,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy
from verdict.orchestration.supervisor import ControllerSupervisor


class EmptyPipe:
    def read1(self, size: int) -> bytes:
        return b""


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
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


class HangingReviewer:
    async def review(self, **kwargs: Any) -> ReviewResult:
        await asyncio.sleep(60)
        return ReviewResult("PASS", "late", "cx/reviewer")


class NoIndependentReviewer:
    def __init__(self) -> None:
        self.calls = 0

    async def review(self, **kwargs: Any) -> ReviewResult:
        self.calls += 1
        return ReviewResult("ERROR", "fake", "", detail="no independent reviewer eligible")


class SlowPassReviewer:
    def __init__(self, done: asyncio.Event) -> None:
        self.done = done

    async def review(self, **kwargs: Any) -> ReviewResult:
        await self.done.wait()
        return ReviewResult("PASS", "fake", "cx/reviewer")


class BlockingSelector(FakeSelector):
    def __init__(self, chosen: Any, *, release: threading.Event | None = None) -> None:
        super().__init__(chosen)
        self.release = release or threading.Event()
        self.calls = 0

    def select(self, *args: Any, **kwargs: Any) -> tuple[Any, tuple[Any, ...]]:
        self.calls += 1
        self.release.wait(5)
        return super().select(*args, **kwargs)


class StopAwareRunner:
    def __init__(self, stopped: threading.Event, release: threading.Event | None = None) -> None:
        self.stopped = stopped
        self.release = release or threading.Event()
        self.returned = threading.Event()
        self.timeouts: list[float] = []

    def __call__(
        self,
        argv: list[str],
        *,
        env: dict[str, str],
        timeout: float,
        stop_event: threading.Event,
    ) -> OcrRun:
        self.timeouts.append(timeout)
        while not stop_event.is_set() and not self.release.is_set():
            time.sleep(0.005)
        if stop_event.is_set():
            self.stopped.set()
        self.returned.set()
        return OcrRun(exit_code=124, stderr="stopped", timed_out=True)


def make_runtime(repo: Path, reviewer: Any, **policy: Any) -> DagRuntime:
    graph = __import__("verdict.orchestration.contracts", fromlist=["WorkGraph"]).WorkGraph(
        "g", (node("a"),)
    )
    return DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=graph,
        selector=Selector(["cc/s", "cx/g"]),
        executor=Executor({}, delay=0),
        classifier=Classifier(),
        events=Events(),
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=reviewer,
        policy=RuntimePolicy(**policy),
    )


def review_lease(owner: dict[str, Any], *, op_id: str = "op", deadline: float = 900.0) -> dict[str, Any]:
    return {
        "id": op_id,
        "token": "token",
        "owner": owner,
        "phase": "review",
        "attempt_id": "review-round-1",
        "started": 0.0,
        "deadline": deadline,
        "status": "active",
    }


def test_review_lease_quiet_wait_beats_supervisor_stall(tmp_path: Path) -> None:
    async def main() -> None:
        ticks = [0.0]
        owner = operations.process_identity(os.getpid())
        assert owner is not None
        operations.activate_generation(tmp_path, "token", 1200.0)
        with operations._locked_state(tmp_path) as state:
            state["operations"]["review"] = review_lease(owner, op_id="review")

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
        assert record["operation"]["phase"] == "review"

    asyncio.run(main())


def test_review_lease_expired_and_rekey_fail_closed() -> None:
    owner = operations.process_identity(os.getpid())
    assert owner is not None
    state = {"token": "token", "active": True, "operations": {"review": review_lease(owner, op_id="review")}}
    guard = operations.OperationLeaseGuard()

    result, _ = guard.inspect(state, token="token", owner=owner, now=900, overall_deadline=1200)
    assert result == "EXPIRED"

    state["operations"]["review"]["status"] = "ended"
    guard.inspect(state, token="token", owner=owner, now=10, overall_deadline=1200)
    state["operations"] = {
        "replacement": {
            **review_lease(owner, op_id="replacement"),
            "started": 10.0,
            "deadline": 910.0,
        }
    }
    result, operation = guard.inspect(state, token="token", owner=owner, now=10, overall_deadline=1200)
    assert result == "NONE"
    assert operation is None


async def test_runtime_review_timeout_fails_closed_without_provider_cooldown(repo: Path) -> None:
    runtime = make_runtime(repo, HangingReviewer(), review_timeout_seconds=0.01)

    result = await runtime.run()

    assert result.outcome.name == "BLOCKED"
    assert result.review is not None
    assert result.review.status == "ERROR"
    assert "review-round-1 timed out" in result.review.detail
    assert result.review.attempts[0]["scope"] == "none"
    assert runtime.selector.cool == {}


async def test_review_scope_closes_after_timeout(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(repo.parent / "run1"))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    operations.activate_generation(repo.parent / "run1", "token", time.monotonic() + 30)
    runtime = make_runtime(repo, HangingReviewer(), review_timeout_seconds=0.01)

    result = await runtime.run()

    assert result.outcome.name == "BLOCKED"
    state = operations.read_state(repo.parent / "run1")
    review_ops = [op for op in state["operations"].values() if op["phase"] == "review"]
    assert len(review_ops) == 1
    assert review_ops[0]["attempt_id"] == "review-round-1"
    assert review_ops[0]["status"] == "ended"


async def test_blocking_reviewer_selector_does_not_block_event_loop_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    release = threading.Event()
    selector = BlockingSelector(_verdict("cx/reviewer"), release=release)
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=_load_fixture("sample-clean.json"))
    reviewer = OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
    )
    started = time.monotonic()
    ticks = 0

    task = asyncio.create_task(
        reviewer.review(
            repo=tmp_path,
            base_ref="main",
            head_ref="feature",
            background="context",
            exclude_routes=frozenset(),
            exclude_families=frozenset(),
            timeout_seconds=0.1,
        )
    )
    while not task.done():
        ticks += 1
        await asyncio.sleep(0.01)
    result = await task
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 1.0
    assert ticks > 0
    assert result.status == "ERROR"
    assert "selector ownership/work unknown" in result.detail
    assert result.attempts[0]["scope"] == "none"
    assert runner.calls == []


async def test_ocr_runner_stop_event_settles_before_reporting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    stopped = threading.Event()
    runner = StopAwareRunner(stopped)
    selector = FakeSelector(_verdict("cx/reviewer"))
    failures: list[Any] = []
    selector.record_failure = lambda route_id, failure, *, now: failures.append(failure)  # type: ignore[method-assign]
    reviewer = OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,  # type: ignore[arg-type]
    )

    result = await reviewer.review(
        repo=tmp_path,
        base_ref="main",
        head_ref="feature",
        background="context",
        exclude_routes=frozenset(),
        exclude_families=frozenset(),
        timeout_seconds=0.3,
    )

    assert stopped.is_set()
    assert runner.returned.is_set()
    assert result.status == "ERROR"
    assert result.detail == "ocr review stopped after cancellation"
    assert result.attempts == (
        {
            "route_id": "cx/reviewer",
            "status": "ERROR",
            "category": "",
            "scope": "none",
            "duration_seconds": result.attempts[0]["duration_seconds"],
        },
    )
    assert failures == []
    assert runner.timeouts[0] < 0.3


async def test_ocr_runner_cleanup_unknown_is_fail_closed_without_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")

    def stuck_runner(
        argv: list[str],
        *,
        env: dict[str, str],
        timeout: float,
        stop_event: threading.Event,
    ) -> OcrRun:
        stop_event.wait(5)
        time.sleep(5)
        return OcrRun(exit_code=0)

    selector = FakeSelector(_verdict("cx/reviewer"))
    failures: list[Any] = []
    selector.record_failure = lambda route_id, failure, *, now: failures.append(failure)  # type: ignore[method-assign]
    reviewer = OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=stuck_runner,  # type: ignore[arg-type]
    )

    result = await reviewer.review(
        repo=tmp_path,
        base_ref="main",
        head_ref="feature",
        background="context",
        exclude_routes=frozenset(),
        exclude_families=frozenset(),
        timeout_seconds=0.1,
    )

    assert result.status == "ERROR"
    assert "cleanup UNKNOWN" in result.detail
    assert result.attempts == (
        {
            "route_id": "cx/reviewer",
            "status": "ERROR",
            "category": "",
            "scope": "none",
            "duration_seconds": result.attempts[0]["duration_seconds"],
        },
    )
    assert failures == []


async def test_no_independent_reviewer_can_fall_back_to_route_level_under_bound(repo: Path) -> None:
    reviewer = NoIndependentReviewer()
    runtime = make_runtime(repo, reviewer, review_timeout_seconds=30)

    result = await runtime.run()

    assert result.outcome.name == "BLOCKED"
    assert reviewer.calls == 2
    assert result.review is not None
    assert "no independent reviewer" in result.review.detail


async def test_runtime_review_lease_keeps_supervisor_quiet_at_240(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = repo.parent / "run1"
    operations.activate_generation(run_dir, "token", time.monotonic() + 1200)
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(run_dir))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    done = asyncio.Event()
    runtime = make_runtime(repo, SlowPassReviewer(done), review_timeout_seconds=900)

    task = asyncio.create_task(runtime.run())
    try:
        operation = None
        for _ in range(200):
            state = operations.read_state(run_dir)
            operation = next(
                (
                    op
                    for op in state.get("operations", {}).values()
                    if op.get("phase") == "review" and op.get("status") == "active"
                ),
                None,
            )
            if operation is not None:
                break
            await asyncio.sleep(0.01)
        assert operation is not None
        owner = operation["owner"]
        guard = operations.OperationLeaseGuard()
        result, watched = guard.inspect(
            state,
            token="token",
            owner=owner,
            now=operation["started"] + 240,
            overall_deadline=operation["deadline"] + 300,
        )
        assert result == "WAITING_BOUNDED"
        assert watched is not None
        assert watched["phase"] == "review"
    finally:
        done.set()
    result = await task
    assert result.outcome.name == "COMPLETE"


def test_ocr_runner_wraps_supervised_review_in_owned_launch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []

    class FakeProc:
        pid = 123
        returncode = 0
        stdout = EmptyPipe()
        stderr = EmptyPipe()

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    def fake_owned(command: list[str], environment: dict[str, str]) -> list[str]:
        calls.append((command, environment))
        environment[operations.OPERATION_ENV] = "review-op"
        return ["owned", *command]

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        assert argv[:2] == ["owned", "ocr"]
        assert kwargs["env"][operations.OPERATION_ENV] == "review-op"
        return FakeProc()

    monkeypatch.setattr("verdict.orchestration.review.owned_worker_command", fake_owned)
    monkeypatch.setattr("verdict.orchestration.review.subprocess.Popen", fake_popen)
    argv = ["ocr", "review", "--output", str(tmp_path / "out.json")]

    run = _run_with_watchdog(argv, env={operations.RUN_DIR_ENV: str(tmp_path)}, timeout=1, idle_seconds=None)

    assert isinstance(run, OcrRun)
    assert run.exit_code == 0
    assert calls and calls[0][0] == argv


def test_ocr_owned_launch_refusal_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def refuse(command: list[str], environment: dict[str, str]) -> list[str]:
        raise operations.OwnershipError("generation revoked")

    monkeypatch.setattr("verdict.orchestration.review.owned_worker_command", refuse)

    run = _run_with_watchdog(
        ["ocr", "review", "--output", str(tmp_path / "out.json")],
        env={operations.RUN_DIR_ENV: str(tmp_path)},
        timeout=1,
        idle_seconds=None,
    )

    assert run.exit_code == 125
    assert "generation revoked" in run.stderr


def test_ocr_owned_launch_refusal_is_not_provider_cooldown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    runner = FakeRunner(
        OcrRun(exit_code=125, stderr="owned review launch refused: generation revoked"),
        review_payload=_load_fixture("sample-clean.json"),
    )
    selector = FakeSelector(_verdict("cx/reviewer"))
    failures: list[Any] = []
    selector.record_failure = lambda route_id, failure, *, now: failures.append(failure)  # type: ignore[method-assign]
    reviewer = OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
    )

    result = _run(reviewer)

    assert result.status == "ERROR"
    assert "owned review launch refused" in result.detail
    assert result.attempts[0]["category"] == ""
    assert failures == []


def test_scope_review_failure_does_not_poison_provider_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_OCR_KEY", "sk-secret-value")
    payload = json.loads(_load_fixture("sample-clean.json"))
    payload["comments"] = []
    payload["manifest"]["coverage"]["failed"] = payload["manifest"]["coverage"]["selected"]
    runner = FakeRunner(OcrRun(exit_code=0), review_payload=json.dumps(payload))
    selector = FakeSelector(_verdict("cx/reviewer"))
    failures: list[Any] = []
    selector.record_failure = lambda route_id, failure, *, now: failures.append(failure)  # type: ignore[method-assign]
    reviewer = OpenCodeReviewer(
        selector,
        api_key_env="TEST_OCR_KEY",
        out_dir=tmp_path / "out",
        runner=runner,
    )

    result = _run(reviewer)

    assert result.status == "ERROR"
    assert "every reviewed item failed" in result.detail
    assert result.attempts[0]["category"] == ""
    assert failures == []


async def test_bounded_runner_copies_review_operation_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("verdict.orchestration.review.executor_timeout_within", lambda timeout: timeout)
    run_dir = tmp_path / "run"
    cgroup_root = tmp_path / "cgroup"
    run_dir.mkdir()
    (cgroup_root / "cgroup.procs").parent.mkdir(parents=True)
    (cgroup_root / "cgroup.procs").write_text("")
    operations.activate_generation(run_dir, "token", time.monotonic() + 30)
    with operations._locked_state(run_dir) as state:
        state["containment"] = {"mode": "test"}
    monkeypatch.setenv(operations.RUN_DIR_ENV, str(run_dir))
    monkeypatch.setenv(operations.TOKEN_ENV, "token")
    seen: list[str] = []

    class FakeProc:
        pid = 789
        returncode = 0
        stdout = EmptyPipe()
        stderr = EmptyPipe()

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            return 0

    def fake_popen(argv: list[str], **kwargs: Any) -> FakeProc:
        environment = kwargs["env"]
        seen.append(environment[operations.OPERATION_ENV])
        return FakeProc()

    monkeypatch.setattr("verdict.orchestration.review.subprocess.Popen", fake_popen)

    with operations.operation_scope(
        run_dir,
        phase="review",
        attempt_id="review-round-1",
        timeout_seconds=1,
    ):
        run = await _run_runner_bounded(
            lambda argv, *, env, timeout: _run_with_watchdog(
                argv,
                env=env,
                timeout=timeout,
                idle_seconds=None,
            ),
            ["ocr", "--version"],
            env={operations.RUN_DIR_ENV: str(run_dir), operations.TOKEN_ENV: "token"},
            timeout_seconds=1,
        )

    assert run.exit_code == 0
    assert seen and seen[0]


async def test_run_with_watchdog_stop_event_kills_and_reaps_process_group() -> None:
    script = (
        "import signal,time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(30)\n"
    )
    stop_event = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(
            _run_with_watchdog,
            [sys.executable, "-c", script],
            env={},
            timeout=30,
            idle_seconds=None,
            stop_event=stop_event,
        )
    )
    await asyncio.sleep(0.05)
    stop_event.set()
    run = await asyncio.wait_for(task, 3)

    assert run.timed_out
    assert run.exit_code == 124


def test_ocr_timeout_kills_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    killed: list[int] = []

    class FakeProc:
        pid = 456
        returncode: int | None = None
        stdout = EmptyPipe()
        stderr = EmptyPipe()

        def poll(self) -> None:
            return None if self.returncode is None else self.returncode

        def wait(self) -> int:
            self.returncode = -9
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    monkeypatch.setattr("verdict.orchestration.review.subprocess.Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr("verdict.orchestration.review.os.killpg", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("verdict.orchestration.review.time.sleep", lambda delay: None)

    run = _run_with_watchdog(
        ["ocr", "review", "--output", str(tmp_path / "out.json")],
        env={},
        timeout=0,
        idle_seconds=None,
    )

    assert run.timed_out
    assert killed == [456]
