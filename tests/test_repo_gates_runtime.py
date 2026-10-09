"""Runtime-owned repo gates run after integration tests, with bounded feedback."""

from __future__ import annotations

import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.test_orch_runtime import NOW, Events, Executor, Reviewer, Selector
from tests.test_orch_runtime import repo as repo
from verdict.orchestration import verification
from verdict.orchestration.contracts import NodeKind, NodeState, RunOutcome, WorkGraph, WorkNode
from verdict.orchestration.controls import ControlRequest
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy, subprocess_runner


@pytest.fixture()
def gate_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "verdict-env" / "bin"
    bin_dir.mkdir(parents=True)
    for executable in ("python", "ruff", "mypy"):
        (bin_dir / executable).touch()
    monkeypatch.setattr(verification.sys, "executable", str(bin_dir / "python"))
    return bin_dir


def make_runtime(
    repo: Path, config: str, *, fail: str = "", run_repo_gates: bool | None = None
) -> tuple[DagRuntime, Events, list[tuple[list[str], Path, float]]]:
    if config:
        (repo / "pyproject.toml").write_text(config)
        subprocess.run(["git", "add", "pyproject.toml"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "declare gates"], cwd=repo, check=True)
    calls: list[tuple[list[str], Path, float]] = []

    async def runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if argv[0] == "git":
            return await subprocess_runner(argv, cwd, timeout)
        calls.append((list(argv), cwd, timeout))
        name = Path(argv[0]).name
        if name == fail:
            return 1, "old output\n" * 150 + "gate failure: actionable diagnostic"
        return 0, "passed"

    worker = WorkNode(
        "a", "write a", owned_files=("a.txt",), verification_command=("python", "-m", "pytest")
    )
    integrate = WorkNode(
        "i",
        "integrate",
        kind=NodeKind.INTEGRATE,
        depends_on=("a",),
        verification_command=("python", "-m", "pytest"),
    )
    events = Events()
    policy = (
        RuntimePolicy() if run_repo_gates is None else RuntimePolicy(run_repo_gates=run_repo_gates)
    )
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=WorkGraph("g", (worker, integrate)),
        selector=Selector(["cc/s"]),
        executor=Executor({}, delay=0),
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda n, cwd: f"{n.node_id}: {n.objective}",
        reviewer=Reviewer(),
        policy=policy,
        runner=runner,
        now=lambda: NOW,
    )
    return runtime, events, calls


@pytest.mark.parametrize(
    ("config", "executable", "gate_name"),
    [("[tool.ruff]\n", "ruff", "ruff-check"), ("[tool.mypy]\nfiles=['a.txt']\n", "mypy", "mypy")],
)
async def test_passing_pytest_cannot_complete_with_failing_gate(
    repo: Path, gate_bin: Path, config: str, executable: str, gate_name: str
) -> None:
    runtime, events, _ = make_runtime(repo, config, fail=executable)
    result = await runtime.run()
    assert all(event["ok"] for event in events.of("verify"))
    assert result.outcome is RunOutcome.BLOCKED
    assert result.nodes["i"].state is NodeState.BLOCKED
    assert f"repo_gate_failed: {gate_name} exit 1" in result.reason
    gate = next(event for event in events.of("repo_gate", "i") if event["name"] == gate_name)
    assert gate["ok"] is False
    assert gate["exit_code"] == 1
    assert len(gate["tail"]) <= 600
    assert "actionable diagnostic" in result.nodes["i"].reason
    assert len(result.nodes["i"].reason) < 800
    states = [event["state"] for event in events.of("node_state", "i")]
    assert states[-2:] == ["REJECTED", "BLOCKED"]
    assert not events.of("integrate")


async def test_gate_failure_feedback_uses_existing_recovery_path(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n", fail="ruff")
    result = await runtime.run()
    run = result.nodes["i"]
    assert "actionable diagnostic" in run.failure_feedback
    assert run.failures[-1].category == "verification_failed"
    assert run.failures[-1].action == "CORRECT_IMPLEMENTATION"
    assert run.history[-1]["outcome"] == "verification_failed"
    assert "actionable diagnostic" in run.history[-1]["evidence"]
    assert events.of("failure", "i")[-1]["category"] == "verification_failed"
    request = ControlRequest("retry-gate", "retry_node", "i", NOW.isoformat(), "test")
    runtime._handle_retry_node(request, {})
    assert events.of("control", "i")[-1]["accepted"] is True
    assert run.state is NodeState.PLANNED
    assert "actionable diagnostic" in run.failure_feedback


async def test_all_gates_pass_and_record_declared_and_executed_commands(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, calls = make_runtime(
        repo, "[tool.ruff]\n[tool.mypy]\nstrict=true\nfiles=['a.txt']\n"
    )
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    gates = events.of("repo_gate", "i")
    assert [event["name"] for event in gates] == ["ruff-check", "ruff-format", "mypy"]
    assert all(event["ok"] and event["exit_code"] == 0 for event in gates)
    assert gates[0]["command"] == "ruff check ."
    assert gates[0]["executed_command"] == shlex.join([str(gate_bin / "ruff"), "check", "."])
    assert gates[0]["source"] == "pyproject:tool.ruff"
    assert gates[-1]["command"] == "mypy --strict a.txt"
    [summary] = events.of("repo_gates", "i")
    assert summary["declared"] is True
    assert [gate["name"] for gate in summary["gates"]] == [event["name"] for event in gates]
    integration_calls = [call for call in calls if call[1].name == "i-a1"]
    assert [Path(call[0][0]).name for call in integration_calls] == [
        "python",
        "ruff",
        "ruff",
        "mypy",
    ]
    assert all(call[2] == runtime.policy.verify_timeout_seconds for call in integration_calls)
    assert not (runtime.run_dir / "worktrees" / "i-a1").exists()


async def test_no_declared_gates_is_explicit(repo: Path) -> None:
    runtime, events, _ = make_runtime(repo, "")
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    [summary] = events.of("repo_gates", "i")
    assert summary["declared"] is False
    assert summary["note"] == "none declared"
    assert summary["gates"] == []
    assert not events.of("repo_gate")


@pytest.mark.parametrize("config", ["[tool.mypy]\n", "not valid TOML = ["])
async def test_unresolvable_config_blocks(repo: Path, config: str) -> None:
    runtime, events, _ = make_runtime(repo, config)
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    run = result.nodes["i"]
    assert run.reason.startswith("repo_gates_config_error:")
    assert len(run.reason) <= 300
    assert events.of("barrier", "i")[-1]["ok"] is False
    assert not events.of("repo_gate")


async def test_missing_executable_fails_with_exit_127(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (gate_bin / "ruff").unlink()
    monkeypatch.setattr(verification.shutil, "which", lambda executable: None)
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert "repo_gate_failed: ruff-check exit 127" in result.reason
    assert all(event["exit_code"] == 127 for event in events.of("repo_gate", "i"))
    assert events.of("repo_gate", "i")[0]["tail"] == "gate executable not found: ruff"


async def test_explicit_policy_opt_out(repo: Path) -> None:
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n", run_repo_gates=False)
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    assert not events.of("repo_gate")
    assert events.of("repo_gates", "i")[0]["disabled"] is True


def test_resolve_prefers_worktree_venv(tmp_path: Path, gate_bin: Path) -> None:
    worktree_bin = tmp_path / ".venv" / "bin"
    worktree_bin.mkdir(parents=True)
    (worktree_bin / "ruff").touch()
    assert verification.resolve_gate_argv(("ruff", "check", "."), tmp_path) == [
        str(worktree_bin / "ruff"),
        "check",
        ".",
    ]


def test_resolve_uses_verdict_env_before_path(tmp_path: Path, gate_bin: Path) -> None:
    assert verification.resolve_gate_argv(("mypy", "--strict", "pkg"), tmp_path) == [
        str(gate_bin / "mypy"),
        "--strict",
        "pkg",
    ]


def test_resolve_falls_back_to_path_and_leaves_npm_unchanged(
    tmp_path: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (gate_bin / "ruff").unlink()
    monkeypatch.setattr(verification.shutil, "which", lambda executable: "/path/ruff")
    assert verification.resolve_gate_argv(("ruff", "check", "."), tmp_path) == [
        "/path/ruff",
        "check",
        ".",
    ]
    assert verification.resolve_gate_argv(("npm", "run", "lint"), tmp_path) == [
        "npm",
        "run",
        "lint",
    ]


async def test_gate_diagnostics_do_not_change_verification_failure_category(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    git_runner = runtime.runner

    async def timeout_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff":
            return 124, "timeout after 600s"
        return await git_runner(argv, cwd, timeout)

    runtime.runner = timeout_runner
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert result.nodes["i"].failures[-1].category == "verification_failed"
    assert result.nodes["i"].failures[-1].scope == "none"
    assert "timeout after 600s" in result.nodes["i"].failure_feedback
    assert not events.of("cooldown")


async def test_gates_are_not_run_before_passing_integration_tests(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, calls = make_runtime(repo, "[tool.ruff]\n")
    normal_runner = runtime.runner

    async def failed_pytest(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if "pytest" in argv and cwd.name == "i-a1":
            return 1, "combined pytest failed"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = failed_pytest
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert "combined pytest failed" in result.nodes["i"].failure_feedback
    assert not events.of("repo_gate")
    assert not any(Path(argv[0]).name in {"ruff", "mypy"} for argv, _, _ in calls)


@pytest.mark.parametrize("config", ["", "[tool.ruff]\n"])
async def test_real_receipt_records_gate_discovery_and_results(
    repo: Path, gate_bin: Path, config: str
) -> None:
    import json

    from verdict.orchestration.receipt import EventLog, verify_run_receipt, write_run_receipt

    runtime, _, _ = make_runtime(repo, config)
    (runtime.run_dir / "graph.json").parent.mkdir(parents=True, exist_ok=True)
    (runtime.run_dir / "graph.json").write_text(json.dumps(runtime.graph.to_dict()))
    runtime.events = EventLog(runtime.run_dir / "events.jsonl")
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    receipt = json.loads(write_run_receipt(runtime.run_dir).read_text())
    [summary] = receipt["repo_gates"]
    assert summary["declared"] is bool(config)
    if config:
        assert [gate["name"] for gate in receipt["repo_gate_results"]] == [
            "ruff-check",
            "ruff-format",
        ]
        assert all(gate["exit_code"] == 0 for gate in receipt["repo_gate_results"])
        assert receipt["repo_gate_results"][0]["executed_command"].startswith(
            str(gate_bin / "ruff")
        )
    else:
        assert summary["note"] == "none declared"
        assert receipt["repo_gate_results"] == []
    assert verify_run_receipt(runtime.run_dir) == []


def test_committed_legacy_receipt_keeps_bytes_and_field_set() -> None:
    import json

    from verdict.orchestration.receipt import build_run_receipt, verify_run_receipt

    run_dir = Path(__file__).resolve().parent.parent / "docs" / "proof" / "demo-run"
    path = run_dir / "receipt.json"
    before = path.read_bytes()
    stored = json.loads(before)
    fresh = build_run_receipt(run_dir)
    assert "repo_gates" not in fresh
    assert "repo_gate_results" not in fresh
    assert fresh == stored
    assert set(fresh) == set(stored)
    assert verify_run_receipt(run_dir) == []
    assert path.read_bytes() == before
