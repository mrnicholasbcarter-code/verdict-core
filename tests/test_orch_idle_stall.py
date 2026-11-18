"""BOD-312: a worker that goes silent is killed as ``stalled`` and rerouted.

The fake Prime binary prints one line and then sleeps 600 s on the first route,
and answers correctly on the second. Failure modes caught:

* a silent worker holds its attempt until the 900 s attempt timeout (no idle kill);
* the kill leaves the worker's process group alive;
* the attempt is recorded as a generic ``timeout``/``unknown`` instead of ``stalled``;
* recovery retries the stalled route instead of moving to a different one.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_orch_runtime import Reviewer, Selector
from verdict.orchestration.contracts import NodeKind, RunOutcome, WorkGraph, WorkNode
from verdict.orchestration.executors import (
    DEFAULT_IDLE_OUTPUT_SECONDS,
    PrimeHeadlessExecutor,
    idle_output_seconds_from_env,
)
from verdict.orchestration.receipt import EventLog, build_run_receipt
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

SILENT = "cc/silent-route"
GOOD = "cx/good-route"
IDLE = 2.0


def _fake_prime(tmp_path: Path, pid_file: Path) -> str:
    good = {
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "RESULT: DONE"}],
                "provider": "omniroute",
                "model": GOOD,
                "usage": {"input": 1, "output": 1},
                "stopReason": "stop",
            }
        ]
    }
    script = tmp_path / "fake-prime"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys, time\n"
        "model = sys.argv[sys.argv.index('--model') + 1]\n"
        "cwd = sys.argv[sys.argv.index('--cwd') + 1]\n"
        f"if model == {SILENT!r}:\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "    print('starting', flush=True)\n"
        "    time.sleep(600)\n"
        "open(os.path.join(cwd, 'a.txt'), 'w').write('done\\n')\n"
        f"print({json.dumps(good)!r})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


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


async def test_silent_worker_is_killed_as_stalled_and_rerouted(repo: Path, tmp_path: Path) -> None:
    pid_file = tmp_path / "silent.pid"
    graph = WorkGraph(
        "g",
        (
            WorkNode(
                "a",
                "write a",
                kind=NodeKind.IMPLEMENT,
                owned_files=("a.txt",),
                verification_command=("test", "-f", "a.txt"),
            ),
        ),
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "graph.json").write_text(json.dumps(graph.to_dict()))
    events = EventLog(run_dir / "events.jsonl")
    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=Selector([SILENT, GOOD]),
        executor=PrimeHeadlessExecutor(
            prime_bin=_fake_prime(tmp_path, pid_file), idle_output_seconds=IDLE
        ),
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=RuntimePolicy(attempt_timeout_seconds=120),
        now=lambda: datetime.now(timezone.utc),
    )
    started = time.monotonic()
    result = await runtime.run()
    elapsed = time.monotonic() - started
    assert result.outcome is RunOutcome.COMPLETE, result.reason

    log = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    terminals = [e for e in log if e["type"] == "terminal"]
    first = terminals[0]["data"]
    assert first["route_id"] == SILENT
    assert first["error"].startswith("stalled:")
    # Killed within N + 5 s of going silent, not at the 120 s attempt bound.
    assert first["duration_seconds"] < IDLE + 5
    assert elapsed < 60
    failures = [e["data"]["category"] for e in log if e["type"] == "failure"]
    assert failures[0] == "stalled"

    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        os.kill(pid, signal.SIGKILL)
        pytest.fail("stalled worker survived the process-group kill")

    receipt = build_run_receipt(run_dir)
    attempts = receipt["nodes"][0]["attempts"]
    assert attempts[0]["route_id"] == SILENT
    assert attempts[0]["failure_category"] == "stalled"
    assert attempts[1]["route_id"] == GOOD
    assert attempts[1]["route_id"] != attempts[0]["route_id"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20", 20.0),
        ("", DEFAULT_IDLE_OUTPUT_SECONDS),
        ("0", DEFAULT_IDLE_OUTPUT_SECONDS),
        ("-5", DEFAULT_IDLE_OUTPUT_SECONDS),
        ("nan", DEFAULT_IDLE_OUTPUT_SECONDS),
        ("off", DEFAULT_IDLE_OUTPUT_SECONDS),
    ],
)
def test_idle_bound_env_never_disables_the_kill(raw: str, expected: float) -> None:
    """A missing, zero, negative or malformed setting keeps the safe default."""
    assert idle_output_seconds_from_env({"VERDICT_WORKER_IDLE_SECONDS": raw}) == expected
