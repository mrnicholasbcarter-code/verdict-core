"""Controller survival tests: fake controller processes, bounded waits, fast suite.

Every test drives ``ControllerSupervisor`` against a small python script written
into ``tmp_path``. No network, no real models, no secrets. ``stall_seconds`` and
``poll_seconds`` stay well under one second so the whole module runs in a few
seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any

import pytest

from verdict.orchestration.supervisor import (
    CAPACITY_MARKERS,
    ControllerSupervisor,
    SupervisorOutcome,
    add_parser,
    build_command_factory,
    dispatch,
    read_events,
    redact_argv,
)

FAST = {"stall_seconds": 0.4, "poll_seconds": 0.02, "kill_grace_seconds": 0.5}

CONTROLLER_HEAD = """
import json, os, sys, time
run_dir = Path = __import__("pathlib").Path(sys.argv[1])
run_dir.mkdir(parents=True, exist_ok=True)
events = run_dir / "events.jsonl"


def _seq():
    n = 0
    if events.exists():
        for line in events.read_text().splitlines():
            if line.strip():
                n = max(n, json.loads(line)["seq"])
    return n


def emit(type, node_id="", **data):
    seq = _seq() + 1
    at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    row = {"seq": seq, "at": at, "type": type, "node_id": node_id, "data": data}
    with events.open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\\n")
    tmp = run_dir / "progress.tmp"
    tmp.write_text(json.dumps({
        "run_id": run_dir.name, "pid": os.getpid(), "seq": seq, "last_event": type,
        "last_progress_at": at, "monotonic": time.monotonic(),
    }))
    tmp.replace(run_dir / "progress.json")


def validated():
    done = []
    if events.exists():
        for line in events.read_text().splitlines():
            row = json.loads(line) if line.strip() else {}
            if row.get("type") == "node_state" and row.get("data", {}).get("state") == "VALIDATED":
                done.append(row["node_id"])
    return done
"""


def write_controller(tmp_path: Path, name: str, body: str) -> Path:
    """Write a fake controller script; ``sys.argv[1]`` is always the run dir."""
    path = tmp_path / name
    path.write_text(CONTROLLER_HEAD + body)
    return path


def argv_for(script: Path, run_dir: Path, *extra: str) -> list[str]:
    return [sys.executable, str(script), str(run_dir), *extra]


def controller_events(run_dir: Path) -> list[dict[str, Any]]:
    return [e for e in read_events(run_dir / "events.jsonl") if e["type"] == "controller"]


def states(run_dir: Path) -> list[str]:
    return [str(e["data"].get("state")) for e in controller_events(run_dir)]


def run_supervisor(supervisor: ControllerSupervisor, budget: float = 25.0) -> SupervisorOutcome:
    """Run the supervisor under a hard outer timeout so a hang fails the test."""

    async def main() -> SupervisorOutcome:
        return await asyncio.wait_for(supervisor.run(), budget)

    return asyncio.run(main())


# ------------------------------------------------------------------ happy path


def test_complete_on_first_try(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    script = write_controller(
        tmp_path,
        "ok.py",
        """
emit("run_started", run_id=run_dir.name)
emit("node_state", state="VALIDATED")
emit("run_finished", outcome="COMPLETE", reason="all nodes validated")
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=2, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "COMPLETE"
    assert outcome.reason == "all nodes validated"
    assert outcome.restarts == 0
    assert len(outcome.generations) == 1
    assert (run_dir / "controller-g0.log").exists()
    assert "FAILED_CLOSED" not in states(run_dir)


def test_blocked_outcome_is_reported_without_restart(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-blocked"
    script = write_controller(
        tmp_path,
        "blocked.py",
        """
emit("run_started", run_id=run_dir.name)
emit("run_finished", outcome="BLOCKED", reason="no eligible model")
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=2, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "BLOCKED"
    assert outcome.reason == "no eligible model"
    assert outcome.restarts == 0


# ------------------------------------------------------------------ restarts


def test_crash_then_restart_resumes_validated_nodes(tmp_path: Path) -> None:
    """Generation 0 dies mid-run; generation 1 reuses the VALIDATED node and finishes."""
    run_dir = tmp_path / "run-crash"
    script = write_controller(
        tmp_path,
        "crash.py",
        """
done = validated()
if not done:
    emit("run_started", run_id=run_dir.name)
    emit("node_state", "n1", state="VALIDATED")
    raise SystemExit(7)
emit("controller", state="RESUMED", detail=str(len(done)))
emit("run_finished", outcome="COMPLETE", reason="resumed " + ",".join(done))
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=2, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "COMPLETE"
    assert outcome.reason == "resumed n1"
    assert outcome.restarts == 1
    assert [g["generation"] for g in outcome.generations] == [0, 1]
    assert outcome.generations[0]["state"] == "CRASHED"
    assert outcome.generations[0]["exit_code"] == 7
    assert "CRASHED" in states(run_dir)
    assert (run_dir / "controller-g1.log").exists()


def test_stall_kills_and_restarts(tmp_path: Path) -> None:
    """A controller that stops publishing progress is killed and replaced."""
    run_dir = tmp_path / "run-stall"
    script = write_controller(
        tmp_path,
        "stall.py",
        """
if not validated():
    emit("run_started", run_id=run_dir.name)
    emit("node_state", "n1", state="VALIDATED")
    time.sleep(120)  # hang: no further progress
emit("run_finished", outcome="COMPLETE", reason="second life finished")
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=2, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "COMPLETE"
    assert outcome.restarts == 1
    assert outcome.generations[0]["state"] == "STALLED"
    stalled = [e for e in controller_events(run_dir) if e["data"].get("state") == "STALLED"]
    assert stalled and stalled[0]["data"]["generation"] == 0
    assert "no progress" in str(stalled[0]["data"]["detail"])


def test_quota_message_is_classified_and_triggers_replacement(tmp_path: Path) -> None:
    """A 429/usage-limit death is QUOTA, and generation 1 uses the replacement argv."""
    run_dir = tmp_path / "run-quota"
    dying = write_controller(
        tmp_path,
        "quota.py",
        """
emit("run_started", run_id=run_dir.name)
sys.stderr.write("Error 429 usage limit reached for the controller model\\n")
sys.stderr.flush()
raise SystemExit(1)
""",
    )
    healthy = write_controller(
        tmp_path,
        "healthy.py",
        """
emit("run_finished", outcome="COMPLETE", reason="replacement controller finished")
""",
    )

    def factory(generation: int) -> list[str]:
        script = dying if generation == 0 else healthy
        return argv_for(script, run_dir, f"--planner-scope=gen{generation}")

    supervisor = ControllerSupervisor(factory, run_dir, max_restarts=2, **FAST)
    outcome = run_supervisor(supervisor)
    assert outcome.state == "COMPLETE"
    assert outcome.generations[0]["state"] == "QUOTA"
    assert outcome.generations[0]["category"] in {"quota_exhausted", "rate_limited"}
    assert outcome.generations[0]["marker"] in CAPACITY_MARKERS
    replaced = [e for e in controller_events(run_dir) if e["data"].get("state") == "REPLACED"]
    assert replaced and "--planner-scope=gen1" in str(replaced[0]["data"]["argv"])
    assert "QUOTA" in states(run_dir)


def test_restart_budget_exhausted_fails_closed(tmp_path: Path) -> None:
    """Budget spent: FAILED_CLOSED plus a synthesized BLOCKED run_finished event."""
    run_dir = tmp_path / "run-budget"
    script = write_controller(
        tmp_path,
        "always_crash.py",
        """
emit("heartbeat", note="alive")
sys.stderr.write("Traceback (most recent call last): boom\\n")
raise SystemExit(3)
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=1, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "BLOCKED"
    assert outcome.restarts == 1
    assert "restart budget" in outcome.reason
    assert len(outcome.generations) == 2
    assert "FAILED_CLOSED" in states(run_dir)
    finished = [e for e in read_events(run_dir / "events.jsonl") if e["type"] == "run_finished"]
    assert finished and finished[-1]["data"]["outcome"] == "BLOCKED"
    assert finished[-1]["data"]["reason"] == outcome.reason


def test_total_deadline_fails_closed(tmp_path: Path) -> None:
    """The wall-clock deadline stops a controller that is still making progress."""
    run_dir = tmp_path / "run-deadline"
    script = write_controller(
        tmp_path,
        "forever.py",
        """
while True:
    emit("heartbeat", note="working")
    time.sleep(0.02)
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir),
        run_dir,
        max_restarts=5,
        total_deadline_seconds=0.5,
        **FAST,
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "BLOCKED"
    assert "deadline" in outcome.reason
    assert outcome.generations[-1]["state"] == "DEADLINE"
    assert "FAILED_CLOSED" in states(run_dir)


# ------------------------------------------------------------------ kill semantics


def test_process_group_is_killed_including_grandchild(tmp_path: Path) -> None:
    """Killing a stalled controller must also kill the workers it spawned."""
    run_dir = tmp_path / "run-group"
    child = tmp_path / "grandchild.py"
    child.write_text(
        "import sys, time, pathlib\npathlib.Path(sys.argv[1]).write_text('up')\ntime.sleep(120)\n"
    )
    marker = tmp_path / "grandchild.pid"
    script = write_controller(
        tmp_path,
        "with_child.py",
        f"""
import subprocess
emit("run_started", run_id=run_dir.name)
kid = subprocess.Popen([sys.executable, {str(child)!r}, {str(marker)!r}])
(run_dir / "kid.pid").write_text(str(kid.pid))
time.sleep(120)
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=0, **FAST
    )
    outcome = run_supervisor(supervisor)
    assert outcome.state == "BLOCKED"
    assert outcome.generations[0]["state"] == "STALLED"
    kid_pid = int((run_dir / "kid.pid").read_text())
    deadline = time.monotonic() + 5.0
    alive = True
    while time.monotonic() < deadline:
        try:
            os.kill(kid_pid, 0)
        except OSError:
            alive = False
            break
        time.sleep(0.05)
    if alive:  # pragma: no cover - only on a leaked grandchild
        os.kill(kid_pid, signal.SIGKILL)
    assert not alive, "grandchild survived the process-group kill"


def test_sigkill_used_when_sigterm_is_ignored(tmp_path: Path) -> None:
    """A controller that ignores SIGTERM is still reaped inside the grace window."""
    run_dir = tmp_path / "run-stubborn"
    script = write_controller(
        tmp_path,
        "stubborn.py",
        """
import signal as _signal
_signal.signal(_signal.SIGTERM, _signal.SIG_IGN)
emit("run_started", run_id=run_dir.name)
time.sleep(120)
""",
    )
    supervisor = ControllerSupervisor(
        lambda gen: argv_for(script, run_dir), run_dir, max_restarts=0, **FAST
    )
    started = time.monotonic()
    outcome = run_supervisor(supervisor, budget=15.0)
    assert outcome.state == "BLOCKED"
    assert time.monotonic() - started < 10.0
    assert outcome.generations[0]["exit_code"] in {-signal.SIGKILL, signal.SIGKILL, None}


# ------------------------------------------------------------------ safety / units


def test_replaced_event_redacts_secrets(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-secret"
    script = write_controller(
        tmp_path,
        "quiet.py",
        """
if validated():
    emit("run_finished", outcome="COMPLETE", reason="done")
else:
    emit("node_state", "n1", state="VALIDATED")
    raise SystemExit(2)
""",
    )

    def factory(generation: int) -> list[str]:
        return argv_for(
            script, run_dir, "--api-key", "sk-live-SHOULD-NOT-APPEAR", "--token=sk-other-secret"
        )

    supervisor = ControllerSupervisor(factory, run_dir, max_restarts=1, **FAST)
    outcome = run_supervisor(supervisor)
    assert outcome.state == "COMPLETE"
    raw = (run_dir / "events.jsonl").read_text()
    assert "sk-live-SHOULD-NOT-APPEAR" not in raw
    assert "sk-other-secret" not in raw
    assert "[REDACTED]" in raw
    assert all("sk-" not in gen["argv"] for gen in outcome.generations)


def test_redact_argv_unit() -> None:
    summary = redact_argv(
        ["verdict", "orchestrate", "--api-key", "sk-abc123", "--api-key=sk-x", "sk-inline", "--ok"]
    )
    assert "sk-abc123" not in summary
    assert "sk-x" not in summary
    assert "sk-inline" not in summary
    assert summary.endswith("--ok")
    assert summary.count("[REDACTED]") == 3


def test_empty_command_factory_fails_closed(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-empty"
    supervisor = ControllerSupervisor(lambda gen: [], run_dir, max_restarts=2, **FAST)
    outcome = run_supervisor(supervisor, budget=5.0)
    assert outcome.state == "BLOCKED"
    assert "empty command" in outcome.reason
    assert "FAILED_CLOSED" in states(run_dir)


def test_unspawnable_command_is_bounded(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-nospawn"
    missing = str(tmp_path / "definitely-not-a-binary")
    supervisor = ControllerSupervisor(lambda gen: [missing], run_dir, max_restarts=1, **FAST)
    outcome = run_supervisor(supervisor, budget=10.0)
    assert outcome.state == "BLOCKED"
    assert len(outcome.generations) == 2
    assert "FAILED_CLOSED" in states(run_dir)


def test_injected_spawner_and_clock_avoid_real_processes(tmp_path: Path) -> None:
    """The supervisor is fully injectable: fake clock, fake sleep, fake process."""
    run_dir = tmp_path / "run-fake"
    run_dir.mkdir(parents=True)
    ticks = {"now": 0.0}
    spawned: list[list[str]] = []

    class FakeProcess:
        pid = None
        returncode = 0

        def __init__(self) -> None:
            self._done = asyncio.get_running_loop().create_future()

        def wait(self) -> Awaitable[int]:
            return self._done

    async def spawner(
        argv: list[str], *, log_path: Path, env: Mapping[str, str] | None
    ) -> FakeProcess:
        spawned.append(argv)
        events = run_dir / "events.jsonl"
        events.write_text(
            json.dumps(
                {
                    "seq": 1,
                    "at": "2024-01-01T00:00:00Z",
                    "type": "run_finished",
                    "node_id": "",
                    "data": {"outcome": "COMPLETE", "reason": "fake run"},
                }
            )
            + "\n"
        )
        return FakeProcess()

    async def sleep(delay: float) -> None:
        ticks["now"] += delay

    supervisor = ControllerSupervisor(
        lambda gen: ["fake", f"gen{gen}"],
        run_dir,
        stall_seconds=1.0,
        poll_seconds=0.1,
        clock=lambda: ticks["now"],
        sleep=sleep,
        spawner=spawner,
        kill_grace_seconds=0.1,
    )
    outcome = asyncio.run(supervisor.run())
    assert outcome.state == "COMPLETE"
    assert outcome.reason == "fake run"
    assert spawned == [["fake", "gen0"]]


def test_stall_detected_with_fake_clock_without_waiting(tmp_path: Path) -> None:
    """Stall detection uses the injected clock, so no real time passes."""
    run_dir = tmp_path / "run-fakestall"
    run_dir.mkdir(parents=True)
    ticks = {"now": 0.0}
    killed: list[int] = []

    class FakeProcess:
        pid = None
        returncode = None

        def __init__(self) -> None:
            self._done: asyncio.Future[int] = asyncio.get_running_loop().create_future()

        def wait(self) -> Awaitable[int]:
            return self._done

        def terminate(self) -> None:
            killed.append(signal.SIGTERM)
            if not self._done.done():
                self._done.set_result(-signal.SIGTERM)

    async def spawner(
        argv: list[str], *, log_path: Path, env: Mapping[str, str] | None
    ) -> FakeProcess:
        return FakeProcess()

    async def sleep(delay: float) -> None:
        ticks["now"] += delay

    supervisor = ControllerSupervisor(
        lambda gen: ["fake"],
        run_dir,
        stall_seconds=30.0,
        poll_seconds=1.0,
        max_restarts=0,
        clock=lambda: ticks["now"],
        sleep=sleep,
        spawner=spawner,
        kill_grace_seconds=1.0,
    )
    outcome = asyncio.run(supervisor.run())
    assert outcome.state == "BLOCKED"
    assert outcome.generations[0]["state"] == "STALLED"
    assert killed == [signal.SIGTERM]
    assert ticks["now"] >= 30.0


def test_cli_parser_builds_resume_command(tmp_path: Path) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(
        [
            "supervise",
            "--run-id",
            "run-42",
            "--runs-dir",
            str(tmp_path),
            "--stall-seconds",
            "0.3",
            "--max-restarts",
            "1",
            "--",
            "--goal",
            "ship it",
        ]
    )
    assert args.run_id == "run-42"
    assert args.stall_seconds == pytest.approx(0.3)
    assert args.max_restarts == 1
    assert args.func is dispatch
    argv = build_command_factory("run-42", ["--goal", "ship it"])(0)
    assert argv[:4] == [sys.executable, "-m", "verdict", "orchestrate"]
    assert argv[-2:] == ["--resume", "run-42"]
    assert build_command_factory("run-42", [])(3)[-2:] == ["--resume", "run-42"]


def test_dispatch_returns_nonzero_when_blocked(tmp_path: Path, monkeypatch: Any) -> None:
    import argparse

    from verdict.orchestration import supervisor as module

    def fake_factory(run_id: str, extra: list[str]) -> Any:
        return lambda generation: []

    monkeypatch.setattr(module, "build_command_factory", fake_factory)
    args = argparse.Namespace(
        run_id="run-9",
        runs_dir=str(tmp_path),
        stall_seconds=0.2,
        poll_seconds=0.02,
        max_restarts=0,
        total_deadline_seconds=5.0,
        orchestrate_args=["--", "--goal", "x"],
    )
    assert dispatch(args) == 1
    assert (tmp_path / "run-9" / "events.jsonl").exists()


def test_read_events_ignores_torn_tail(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    good = {"seq": 1, "at": "2024-01-01T00:00:00Z", "type": "heartbeat", "node_id": "", "data": {}}
    path.write_text(json.dumps(good) + "\n" + '{"seq": 2, "type": "run_fin')
    events = read_events(path)
    assert [e["seq"] for e in events] == [1]
    assert read_events(tmp_path / "missing.jsonl") == []
