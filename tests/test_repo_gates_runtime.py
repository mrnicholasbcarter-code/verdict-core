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
from verdict.orchestration.contracts import (
    NodeKind,
    NodeState,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.controls import ControlRequest
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, NodeRun, RuntimePolicy, subprocess_runner


@pytest.fixture()
def gate_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "verdict-env" / "bin"
    bin_dir.mkdir(parents=True)
    for executable in ("python", "ruff", "mypy"):
        (bin_dir / executable).touch()
    monkeypatch.setattr(verification.sys, "executable", str(bin_dir / "python"))
    return bin_dir


def make_runtime(
    repo: Path,
    config: str,
    *,
    fail: str = "",
    run_repo_gates: bool | None = None,
    owned_file: str = "a.txt",
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
        # These tests exercise the integration barrier; node-level gates are
        # covered by test_implement_node_gate_failure_is_correction_feedback.
        if name == fail and cwd.name.startswith("i-"):
            return 1, "old output\n" * 150 + "gate failure: actionable diagnostic"
        return 0, "passed"

    worker = WorkNode(
        "a", "write a", owned_files=(owned_file,), verification_command=("python", "-m", "pytest")
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
    # When a non-default owned file is used, substitute a minimal executor that
    # writes that file so git.changed_files picks it up with the right extension.
    if owned_file == "a.txt":
        executor: object = Executor({}, delay=0)
    else:
        _of = owned_file

        class _FileExecutor:
            async def run(
                self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
            ) -> WorkerTerminal:
                (cwd / _of).write_text(f"a by {route_id}\n")
                return WorkerTerminal(
                    ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
                )

        executor = _FileExecutor()
    runtime = DagRuntime(
        repo=repo,
        run_dir=repo.parent / "run1",
        graph=WorkGraph("g", (worker, integrate)),
        selector=Selector(["cc/s"]),
        executor=executor,
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


def test_resolve_uses_uv_run_when_uv_lock_present(
    tmp_path: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv.lock in the worktree → uv run --frozen, never the global tool."""
    (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(
        verification.shutil, "which", lambda exe: "/usr/bin/uv" if exe == "uv" else None
    )
    result = verification.resolve_gate_argv(("mypy", "--strict", "verdict"), tmp_path)
    assert result == ["/usr/bin/uv", "run", "--frozen", "mypy", "--strict", "verdict"]


def test_resolve_uses_uv_run_for_ruff_when_uv_lock_present(
    tmp_path: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv.lock in the worktree → uv run --frozen for ruff too."""
    (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(
        verification.shutil, "which", lambda exe: "/usr/local/bin/uv" if exe == "uv" else None
    )
    result = verification.resolve_gate_argv(("ruff", "check", "."), tmp_path)
    assert result == ["/usr/local/bin/uv", "run", "--frozen", "ruff", "check", "."]


def test_resolve_raises_when_uv_lock_present_but_uv_missing(
    tmp_path: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv.lock present but uv not on PATH → named environment failure, not a silent pass."""
    (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(verification.shutil, "which", lambda exe: None)
    with pytest.raises(FileNotFoundError, match="uv"):
        verification.resolve_gate_argv(("mypy", "--strict", "verdict"), tmp_path)


def test_resolve_uv_lock_takes_precedence_over_verdict_env(
    tmp_path: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uv.lock present and Verdict env has mypy → still use uv run, not the Verdict-env binary."""
    (tmp_path / "uv.lock").write_text("# lock\n")
    monkeypatch.setattr(
        verification.shutil, "which", lambda exe: "/usr/bin/uv" if exe == "uv" else None
    )
    # gate_bin has mypy (Verdict env), but uv.lock should win
    result = verification.resolve_gate_argv(("mypy", "--strict", "verdict"), tmp_path)
    assert result[0] == "/usr/bin/uv"
    assert "run" in result
    assert "--frozen" in result


def test_resolve_no_uv_lock_still_uses_verdict_env(tmp_path: Path, gate_bin: Path) -> None:
    """Without uv.lock, Verdict-env fallback still works (non-uv-managed repos)."""
    # gate_bin has mypy via monkeypatching sys.executable in the fixture
    result = verification.resolve_gate_argv(("mypy", "--strict", "pkg"), tmp_path)
    assert result == [str(gate_bin / "mypy"), "--strict", "pkg"]


async def test_gate_uses_uv_run_when_worktree_has_uv_lock(repo: Path, gate_bin: Path) -> None:
    """Integration: when the worktree has uv.lock the executed_command includes uv run --frozen."""
    runtime, events, _calls = make_runtime(repo, "[tool.mypy]\nfiles=['verdict']\nstrict=true\n")
    # Plant a fake uv binary in a dir on PATH and wire up sys.executable
    uv_bin_dir = gate_bin.parent.parent / "uv_bin"
    uv_bin_dir.mkdir(parents=True, exist_ok=True)
    fake_uv = uv_bin_dir / "uv"
    fake_uv.touch()
    fake_uv.chmod(0o755)

    captured: list[list[str]] = []
    original_runner = runtime.runner

    async def recording_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        captured.append(list(argv))
        if argv[0] == "git":
            return await original_runner(argv, cwd, timeout)
        return 0, "passed"

    runtime.runner = recording_runner

    # Plant uv.lock in the integration worktree by patching resolve_gate_argv
    import verdict.orchestration.verification as _vrf

    orig_resolve = _vrf.resolve_gate_argv

    def _resolve_with_uv_lock(argv_in: Sequence[str], worktree: Path) -> list[str]:
        # Create uv.lock and wire uv to fake_uv for this call
        (worktree / "uv.lock").write_text("# lock\n")
        import unittest.mock as _mock

        with _mock.patch.object(
            _vrf.shutil, "which", lambda exe: str(fake_uv) if exe == "uv" else None
        ):
            return orig_resolve(argv_in, worktree)

    import unittest.mock as mock

    with mock.patch("verdict.orchestration.runtime._resolve_gate_argv", _resolve_with_uv_lock):
        await runtime.run()

    gate_evts = [e for e in events.of("repo_gate") if e.get("name") == "mypy"]
    assert gate_evts, "no mypy repo_gate event"
    executed = gate_evts[0].get("executed_command", "")
    assert str(fake_uv) in executed, f"expected uv binary in executed_command, got: {executed!r}"
    assert "run" in executed
    assert "--frozen" in executed


async def test_gate_diagnostics_do_not_change_verification_failure_category(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    git_runner = runtime.runner

    async def timeout_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff" and cwd.name.startswith("i-"):
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
    assert not events.of("repo_gate", "i")
    assert not any(
        Path(argv[0]).name in {"ruff", "mypy"} and cwd.name.startswith("i-")
        for argv, cwd, _ in calls
    )


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
    # Gates run for the implement node and again at the integration barrier.
    summaries = receipt["repo_gates"]
    assert [s["node_id"] for s in summaries] == ["a", "i"]
    summary = summaries[-1]
    assert summary["declared"] is bool(config)
    if config:
        assert [(g["node_id"], g["name"]) for g in receipt["repo_gate_results"]] == [
            ("a", "ruff-check"),
            ("a", "ruff-format"),
            ("i", "ruff-check"),
            ("i", "ruff-format"),
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


async def test_implement_node_gate_failure_is_correction_feedback(
    repo: Path, gate_bin: Path
) -> None:
    """Node-level lint failures that survive autofix still go back to the worker.

    The autofix step runs ``ruff check --fix`` before the gate.  In this test the
    runner's mock cannot actually rewrite files, so the fix is a no-op; the
    subsequent gate ``ruff check .`` (without ``--fix``) still fails on the first
    attempt and sends correction feedback.  On the second attempt the gate passes.
    """
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    normal_runner = runtime.runner
    seen = {"check": 0}

    async def lint_fails_once(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        # Only intercept the gate check (no --fix), not the proactive autofix call.
        if (
            Path(argv[0]).name == "ruff"
            and "check" in argv
            and "--fix" not in argv
            and cwd.name.startswith("a-")
        ):
            seen["check"] += 1
            if seen["check"] == 1:
                return 1, "F401 unused import in a.txt"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = lint_fails_once
    result = await runtime.run()
    assert result.outcome is RunOutcome.COMPLETE, result.reason
    failure = events.of("failure", "a")[0]
    assert failure["category"] == "verification_failed"
    node_gates = [e for e in events.of("repo_gate", "a") if e["name"] == "ruff-check"]
    assert [g["ok"] for g in node_gates] == [False, True]
    assert events.of("rehydrate", "a")


async def test_node_level_gates_leave_environment_problems_to_integration(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (gate_bin / "ruff").unlink()
    # CI images ship ruff on PATH; hide it so the gate executable is really missing.
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    runtime, _, _ = make_runtime(repo, "[tool.ruff]\n")
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED
    assert "exit 127" in result.nodes["i"].reason


async def test_existing_gate_debt_is_not_blamed_on_the_node(repo: Path, gate_bin: Path) -> None:
    """Review finding: a gate that already fails on the node's base is not the
    node's fault; it is left to the integration barrier."""
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    normal_runner = runtime.runner

    async def debt(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff" and "check" in argv:
            return 1, "existing lint debt in untouched.py"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = debt
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED
    assert not events.of("failure", "a")
    assert "existing lint debt" in result.nodes["i"].reason


async def test_existing_debt_in_one_gate_does_not_hide_a_new_failure_in_another(
    repo: Path, gate_bin: Path
) -> None:
    """Existing ruff-check debt on the base does not mask a new mypy failure.

    ruff-check fails everywhere (existing debt); mypy fails only in the node's
    worktree on the first attempt.  The ruff-check failure is attributed to the
    base and ignored; the mypy failure is attributed to the node and reported.
    Format issues are proactively fixed by autofix, so they cannot be the
    ``new_failure_in_another_gate`` for this scenario.
    """
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n[tool.mypy]\nfiles=['a.txt']\n")
    normal_runner = runtime.runner
    seen = {"mypy": 0}

    async def debt_plus_new(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        name = Path(argv[0]).name
        # ruff-check always fails (existing debt in base and node alike)
        if name == "ruff" and "check" in argv and "--fix" not in argv:
            return 1, "existing lint debt"
        # mypy fails once only in the node worktree (new failure)
        if name == "mypy" and cwd.name.startswith("a-"):
            seen["mypy"] += 1
            if seen["mypy"] == 1:
                return 1, "N818: exception name should end in Error"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = debt_plus_new
    await runtime.run()
    failure = events.of("failure", "a")[0]
    assert failure["category"] == "verification_failed"
    assert "N818" in failure.get("evidence", ""), (
        f"expected N818 in failure evidence; got: {failure.get('evidence', '')!r}"
    )


async def test_formatting_only_failure_is_repaired_with_the_formatter(
    repo: Path, gate_bin: Path
) -> None:
    """Self-host run 2026-10-12: two models' edits passed their tests but failed
    only ruff format, and neither fixed it.

    Formatting is now applied proactively before verification and gates via
    _apply_lint_autofix.  The gate sees already-formatted code and passes.
    A lint_autofix event records the autofix run.
    """
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n", owned_file="a.py")
    normal_runner = runtime.runner
    formatted = {"done": False}

    async def needs_format(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        name = Path(argv[0]).name
        if (
            name == "ruff"
            and list(argv[1:3]) == ["format", "--check"]
            and cwd.name.startswith("a-")
        ):
            return (0, "") if formatted["done"] else (1, "Would reformat: a.py")
        if name == "ruff" and argv[1] == "format" and "--check" not in argv:
            formatted["done"] = True
            return 0, "1 file reformatted"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = needs_format
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason
    # Proactive autofix emits a lint_autofix event; no failure events.
    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted"
    assert autofix_evts[0].get("ruff_format_exit") == 0
    assert not events.of("failure", "a")


async def test_lint_failures_are_never_auto_fixed(repo: Path, gate_bin: Path) -> None:
    """ruff check --fix is proactively run, but when it cannot fix the error
    (exit 1), the gate still fails and the node is not validated."""
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    normal_runner = runtime.runner

    async def lint_fails(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff" and "check" in argv and cwd.name.startswith("a-"):
            return 1, "F401 unused import"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = lint_fails
    await runtime.run()
    # Autofix ran (lint_autofix event exists) but changed no files.
    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted"
    assert autofix_evts[0].get("changed_files") == []
    assert events.of("failure", "a")[0]["category"] == "verification_failed"


async def test_lint_autofix_runs_before_verification_and_gate(repo: Path, gate_bin: Path) -> None:
    """Autofix commands run before verification_command and before repo gates.

    The contract: _apply_lint_autofix is called in _validate before both the
    verification command and _run_repo_gates, so both see the corrected code.
    """
    runtime, _events, _ = make_runtime(repo, "[tool.ruff]\n", owned_file="a.py")
    normal_runner = runtime.runner
    call_order: list[str] = []

    async def ordered_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        name = Path(argv[0]).name
        if argv[0] == "git":
            return await normal_runner(argv, cwd, timeout)
        if name == "ruff" and "check" in argv and "--fix" in argv:
            call_order.append("autofix_check")
        elif name == "ruff" and "format" in argv and "--check" not in argv:
            call_order.append("autofix_format")
        elif name == "python" and "-m" in argv and "pytest" in argv:
            call_order.append("verify")
        elif name == "ruff" and "check" in argv and "--fix" not in argv:
            call_order.append("gate_check")
        elif name == "ruff" and "format" in argv and "--check" in argv:
            call_order.append("gate_format")
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = ordered_runner
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason
    # Only look at the first occurrence of each label (from the implement node "a").
    idx: dict[str, int] = {}
    for i, label in enumerate(call_order):
        idx.setdefault(label, i)
    assert "autofix_check" in idx or "autofix_format" in idx, "no autofix calls recorded"
    assert "verify" in idx, "no verify call recorded"
    assert "gate_check" in idx or "gate_format" in idx, "no gate calls recorded"
    autofix_last = max(idx.get("autofix_check", -1), idx.get("autofix_format", -1))
    verify_first = idx.get("verify", len(call_order))
    gate_first = min(
        idx.get("gate_check", len(call_order)), idx.get("gate_format", len(call_order))
    )
    assert autofix_last < verify_first, f"autofix must run before verification; order: {call_order}"
    assert autofix_last < gate_first, f"autofix must run before gates; order: {call_order}"


@pytest.mark.parametrize("config", ["[tool.ruff]\n"])
async def test_lint_autofix_event_in_node_receipt(repo: Path, gate_bin: Path, config: str) -> None:
    """lint_autofix data is included in the per-attempt node record in the receipt."""
    import json

    from verdict.orchestration.receipt import EventLog, write_run_receipt

    runtime, _, _ = make_runtime(repo, config)
    (runtime.run_dir / "graph.json").parent.mkdir(parents=True, exist_ok=True)
    (runtime.run_dir / "graph.json").write_text(json.dumps(runtime.graph.to_dict()))
    runtime.events = EventLog(runtime.run_dir / "events.jsonl")
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason
    receipt = json.loads(write_run_receipt(runtime.run_dir).read_text())
    node_a = next((n for n in receipt.get("nodes", []) if n["node_id"] == "a"), None)
    assert node_a is not None, "node 'a' missing from receipt nodes"
    assert any(a.get("lint_autofix") is not None for a in node_a.get("attempts", [])), (
        f"lint_autofix missing from node 'a' attempts: {node_a.get('attempts')}"
    )
    attempt_with_autofix = next(a for a in node_a["attempts"] if a.get("lint_autofix") is not None)
    lint_rec = attempt_with_autofix["lint_autofix"]
    assert "files_changed" in lint_rec
    assert "ruff_missing" in lint_rec
    assert "skipped" in lint_rec
    assert lint_rec["ruff_missing"] is False
    assert lint_rec["skipped"] is False


async def test_ruff_missing_records_event_gates_proceed(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ruff is missing in the autofix path, the lint_autofix event records
    ruff_missing=True, gates still run (via gate_bin which has ruff), and the
    node validates because the gates themselves succeed."""
    import verdict.orchestration.runtime as _rt_mod

    orig = _rt_mod._resolve_gate_argv

    def missing_ruff_fix_only(argv: Sequence[str], cwd: Path) -> list[str]:
        # Raise only for the proactive autofix calls (--fix flag present).
        # Gate calls (ruff check . / ruff format --check .) are resolved normally.
        if argv and argv[0] == "ruff" and "--fix" in argv:
            raise FileNotFoundError("gate executable not found: ruff")
        return orig(argv, cwd)

    monkeypatch.setattr(_rt_mod, "_resolve_gate_argv", missing_ruff_fix_only)
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n", owned_file="a.py")
    result = await runtime.run()
    # Autofix path could not find ruff → ruff_missing=True, skipped=False.
    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted when ruff is missing"
    assert autofix_evts[0].get("ruff_missing") is True
    assert autofix_evts[0].get("skipped") is False
    # Gates still ran: repo_gate events exist for node "a".
    gate_evts = events.of("repo_gate", "a")
    assert gate_evts, "gates did not run after autofix ruff_missing"
    # Node validates because gates (via gate_bin) passed.
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason


async def test_autofix_skipped_when_no_ruff_gate(repo: Path, gate_bin: Path) -> None:
    """When no ruff gate is declared, _apply_lint_autofix emits a skipped event
    and returns without calling ruff."""
    runtime, events, _ = make_runtime(repo, "[tool.mypy]\nfiles=['a.txt']\n")
    normal_runner = runtime.runner
    ruff_calls: list[tuple[list[str], Path]] = []

    async def track_ruff(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff":
            ruff_calls.append((list(argv), cwd))
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = track_ruff
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason
    # lint_autofix event emitted with skipped=True.
    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted"
    assert autofix_evts[0].get("skipped") is True
    assert autofix_evts[0].get("reason") == "no_ruff_gate"
    # No ruff calls for the implement node's worktree: match on cwd.name and argv elements.
    ruff_in_node = [(av, cw) for av, cw in ruff_calls if cw.name.startswith("a-") or "a.txt" in av]
    assert not ruff_in_node, f"unexpected ruff calls in node worktree: {ruff_in_node}"


async def test_fixable_f401_is_corrected_in_file_and_node_validates(
    repo: Path, gate_bin: Path
) -> None:
    """A runner that simulates ruff check --fix by writing clean content to the
    target file proves the full contract: file digest changes, changed_files is
    non-empty in the event, the gate then sees clean code, and the node validates.
    """
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n", owned_file="a.py")
    normal_runner = runtime.runner

    async def fixing_runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        name = Path(argv[0]).name
        if name == "ruff" and "check" in argv and "--fix" in argv:
            # argv = [resolved_ruff, "check", "--fix", target1, ...]
            # Simulate the fix by rewriting each target file with clean content.
            for target in argv[3:]:
                target_path = cwd / target
                if target_path.is_file():
                    target_path.write_text("# fixed by autofix\n")
            return 0, "Fixed 1 error (F401)."
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = fixing_runner
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED, result.reason
    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted"
    changed = autofix_evts[0].get("changed_files", [])
    assert changed, f"expected at least one file in changed_files; got {changed!r}"
    assert not events.of("failure", "a"), "unexpected failure after autofix"


async def test_unowned_changed_file_is_never_passed_to_ruff(
    repo: Path, gate_bin: Path, tmp_path: Path
) -> None:
    """A file changed by the worker but not in owned_files must never appear in
    the ruff --fix argv; only owned files are auto-fixed.

    _apply_lint_autofix is called directly to isolate the ownership filter from
    the runtime's ownership-violation gate, which runs earlier in the full flow
    and would block the node before autofix is ever reached.
    """
    runtime, _events, _ = make_runtime(repo, "")
    fix_argvs: list[list[str]] = []
    normal_runner = runtime.runner

    async def track_fix(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff" and "--fix" in argv:
            fix_argvs.append(list(argv))
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = track_fix

    # Fake worktree: a.py is owned, b.py is not; both present and in changed.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[tool.ruff]\n")
    (worktree / "a.py").write_text("import os  # F401 candidate\n")
    (worktree / "b.py").write_text("import sys  # unowned\n")

    run = NodeRun(
        node=WorkNode(
            "a", "write a", owned_files=("a.py",), verification_command=("python", "-m", "pytest")
        )
    )
    await runtime._apply_lint_autofix(run, worktree, ["a.py", "b.py"])

    # At least one ruff --fix call must have happened (a.py is owned and exists).
    assert fix_argvs, "ruff --fix was never called"
    # b.py must never appear as an argv element in any --fix invocation.
    for argv in fix_argvs:
        assert "b.py" not in argv, f"unowned b.py was passed to ruff --fix; full argv: {argv}"


async def test_non_python_owned_files_are_never_passed_to_ruff(
    repo: Path, gate_bin: Path, tmp_path: Path
) -> None:
    """Regression: ruff must not receive YAML or Markdown files even when they
    are owned and changed.  When ruff gets an explicitly named non-.py file it
    silently reformats it as Python syntax, corrupting the content.

    Live failure (2026-11-20): probe_spec node passed
    ``openspec/changes/doctor-capability-gateway-probe/.openspec.yaml`` to ruff
    format and it rewrote ``schema: verdict-change-v1`` to
    ``schema: verdict - change - v1``.

    _apply_lint_autofix is called directly to test the extension filter in
    isolation.
    """
    runtime, events, _ = make_runtime(repo, "")
    all_ruff_argvs: list[list[str]] = []
    normal_runner = runtime.runner

    async def track_ruff(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff":
            all_ruff_argvs.append(list(argv))
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = track_ruff

    # Fake worktree: .py is owned and Python; .yaml and .md are owned but not Python.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[tool.ruff]\n")
    (worktree / "a.py").write_text("import os\n")
    yaml_content = "schema: verdict-change-v1\n"
    (worktree / "config.yaml").write_text(yaml_content)
    (worktree / "docs.md").write_text("# heading\n")

    run = NodeRun(
        node=WorkNode(
            "probe",
            "write probe",
            owned_files=("a.py", "config.yaml", "docs.md"),
            verification_command=("python", "-m", "pytest"),
        )
    )
    await runtime._apply_lint_autofix(run, worktree, ["a.py", "config.yaml", "docs.md"])

    # Only a.py should appear in ruff invocations; .yaml and .md must not.
    for argv in all_ruff_argvs:
        assert "config.yaml" not in argv, f"config.yaml passed to ruff; full argv: {argv}"
        assert "docs.md" not in argv, f"docs.md passed to ruff; full argv: {argv}"

    # The yaml file's bytes must be byte-for-byte identical after autofix.
    assert (worktree / "config.yaml").read_text() == yaml_content, (
        "config.yaml was corrupted by autofix"
    )

    # The lint_autofix event records the skipped non-Python files.
    autofix_evts = events.of("lint_autofix", "probe")
    assert autofix_evts, "lint_autofix event not emitted"
    skipped = autofix_evts[0].get("skipped_non_python", [])
    assert "config.yaml" in skipped, f"config.yaml not in skipped_non_python: {skipped}"
    assert "docs.md" in skipped, f"docs.md not in skipped_non_python: {skipped}"


async def test_all_non_python_owned_files_emit_no_ruff_calls(
    repo: Path, gate_bin: Path, tmp_path: Path
) -> None:
    """When every owned changed file is non-Python, ruff must not be called at
    all, and the event records the skipped files with files=[]."""
    runtime, events, _ = make_runtime(repo, "")
    ruff_argvs: list[list[str]] = []
    normal_runner = runtime.runner

    async def track_ruff(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff":
            ruff_argvs.append(list(argv))
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = track_ruff

    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "pyproject.toml").write_text("[tool.ruff]\n")
    (worktree / "spec.yaml").write_text("schema: verdict-change-v1\n")
    (worktree / "README.md").write_text("# docs\n")

    run = NodeRun(
        node=WorkNode(
            "a",
            "write a",
            owned_files=("spec.yaml", "README.md"),
            verification_command=("python", "-m", "pytest"),
        )
    )
    await runtime._apply_lint_autofix(run, worktree, ["spec.yaml", "README.md"])

    assert not ruff_argvs, f"ruff was called but no Python files exist: {ruff_argvs}"

    autofix_evts = events.of("lint_autofix", "a")
    assert autofix_evts, "lint_autofix event not emitted"
    evt = autofix_evts[0]
    assert evt.get("files") == [], f"expected files=[], got {evt.get('files')!r}"
    assert evt.get("changed_files") == [], (
        f"expected changed_files=[], got {evt.get('changed_files')!r}"
    )
    skipped = evt.get("skipped_non_python", [])
    assert "spec.yaml" in skipped
    assert "README.md" in skipped
