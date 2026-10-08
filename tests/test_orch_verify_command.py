"""Tests for verification-command resilience: FileNotFoundError handling and python resolver."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import pytest

from verdict.orchestration.runtime import _resolve_verify_argv, subprocess_runner

# ------------------------------------------------------------------ subprocess_runner


@pytest.mark.asyncio
async def test_subprocess_runner_nonexistent_binary(tmp_path: Path) -> None:
    """A missing binary returns exit 127 with a clear message, not FileNotFoundError."""
    code, out = await subprocess_runner(
        ["__no_such_binary_xyzzy__", "--version"], tmp_path, timeout=10.0
    )
    assert code == 127
    assert "verification command not found" in out
    assert "__no_such_binary_xyzzy__" in out


@pytest.mark.asyncio
async def test_subprocess_runner_permission_error(tmp_path: Path) -> None:
    """A non-executable file returns exit 127, not PermissionError."""
    script = tmp_path / "not_exec.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o644)  # not executable
    code, out = await subprocess_runner([str(script)], tmp_path, timeout=10.0)
    assert code == 127
    assert "verification command not found" in out


@pytest.mark.asyncio
async def test_subprocess_runner_existing_binary_runs(tmp_path: Path) -> None:
    """An existing binary is executed normally (sanity check)."""
    code, out = await subprocess_runner(["echo", "hello"], tmp_path, timeout=10.0)
    assert code == 0
    assert "hello" in out


# ------------------------------------------------------------------ _resolve_verify_argv


def test_resolve_python_missing_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When 'python' is not on PATH, resolve to sys.executable."""
    # Point PATH to an empty directory so shutil.which('python') returns None
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved, resolved_argv0 = _resolve_verify_argv(["python", "-m", "pytest", "-q"])
    assert resolved[0] == sys.executable
    assert resolved[1:] == ["-m", "pytest", "-q"]
    assert resolved_argv0 == sys.executable


def test_resolve_python3_missing_from_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When 'python3' is not on PATH, resolve to sys.executable."""
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved, resolved_argv0 = _resolve_verify_argv(["python3", "-m", "pytest"])
    assert resolved[0] == sys.executable
    assert resolved_argv0 == sys.executable


def test_resolve_python_present_on_path(tmp_path: Path) -> None:
    """When 'python' IS on PATH, do not rewrite it."""
    # Create a fake 'python' in a temp dir on PATH
    import os
    import stat

    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(stat.S_IRWXU)
    orig_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{tmp_path}:{orig_path}"
    try:
        resolved, resolved_argv0 = _resolve_verify_argv(["python", "-m", "pytest"])
        assert resolved[0] == "python"
        assert resolved_argv0 == ""
    finally:
        os.environ["PATH"] = orig_path


def test_resolve_other_binary_never_rewritten() -> None:
    """A non-python argv[0] is never rewritten, even if missing."""
    resolved, resolved_argv0 = _resolve_verify_argv(["ruff", "check", "."])
    assert resolved[0] == "ruff"
    assert resolved_argv0 == ""


def test_resolve_empty_argv() -> None:
    """Empty argv returns empty list without error."""
    resolved, resolved_argv0 = _resolve_verify_argv([])
    assert resolved == []
    assert resolved_argv0 == ""


# ------------------------------------------------------------------ integration: DagRuntime verify


@pytest.mark.asyncio
async def test_runtime_verify_missing_binary_rejected_not_blocked(tmp_path: Path) -> None:
    """A node whose verification_command binary is missing gets REJECTED, not driver-crash BLOCKED.

    This is the core regression test for the BOD-70 dogfood bug.
    """
    from datetime import datetime, timezone

    from verdict.orchestration.contracts import (
        CapacityClass,
        EligibilityStage,
        FailureClassification,
        NodeKind,
        RouteVerdict,
        WorkerTerminal,
        WorkGraph,
        WorkNode,
    )
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    now = datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc)

    class Events:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def emit(self, type: str, node_id: str = "", **data: Any) -> None:
            self.rows.append({"type": type, "node_id": node_id, **data})

    class FixedSelector:
        last_select_stats: ClassVar[dict[str, int]] = {}

        def select(self, req: Any, now: Any) -> tuple[Any, list[Any]]:

            rv = RouteVerdict(
                route_id="test/model-a",
                provider="test",
                reached=EligibilityStage.SELECTED,
                failed_stage=None,
                reason="",
                capacity_class=CapacityClass.FREE,
                rank=1,
                plan_label="free",
            )
            return rv, [rv]

        def record_success(self, route_id: str, now: Any = None) -> None:
            pass

        def record_failure(self, route_id: str, failure: Any, now: Any = None) -> None:
            pass

        def probe_status(self, route_id: str) -> str:
            return "healthy"

        def is_healthy(self, route_id: str) -> bool:
            return True

    class OkExecutor:
        async def run(
            self, prompt: str, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            # Write a file so there's a change
            (cwd / "result.txt").write_text("done")
            return WorkerTerminal(ok=True, model="test/model-a", output="RESULT: OK")

    class SimpleClassifier:
        def classify(self, terminal: WorkerTerminal, now: Any = None) -> FailureClassification:
            return FailureClassification(
                category="verification_failed",
                action="REROUTE",
                scope="route",
                cooldown_seconds=0,
                evidence=terminal.error[:200],
            )

    # Set up a git repo
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init", "--no-verify"], check=True
    )

    node = WorkNode(
        node_id="impl-a",
        kind=NodeKind.IMPLEMENT,
        objective="do stuff",
        owned_files=["result.txt"],
        acceptance=["result.txt exists"],
        verification_command=["__no_such_verify_binary__", "--check"],
    )
    graph = WorkGraph(goal="test goal", nodes=[node])
    events = Events()
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rt = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=FixedSelector(),  # type: ignore[arg-type]
        executor=OkExecutor(),  # type: ignore[arg-type]
        classifier=SimpleClassifier(),  # type: ignore[arg-type]
        events=events,
        prompt_for=lambda n, w: "do the thing",
        policy=RuntimePolicy(max_attempts_per_node=1),
        now=lambda: now,
    )

    await rt.run()

    # The verify event should show exit_code=127, not a driver crash
    verify_events = [e for e in events.rows if e["type"] == "verify"]
    assert len(verify_events) >= 1
    ve = verify_events[0]
    assert ve["ok"] is False
    assert ve["exit_code"] == 127

    # No 'driver crash' in any event
    all_reasons = " ".join(str(e.get("reason", "")) for e in events.rows)
    assert "driver crash" not in all_reasons

    # The node should be REJECTED or BLOCKED via normal failure path, not driver crash
    node_states = [e for e in events.rows if e["type"] == "node_state" and e["node_id"] == "impl-a"]
    state_values = [e["state"] for e in node_states]
    # Should see TERMINAL_FAILURE or REJECTED (normal failure classification), not raw BLOCKED from driver crash
    assert "TERMINAL_FAILURE" in state_values or "REJECTED" in state_values


@pytest.mark.asyncio
async def test_runtime_verify_python_resolved_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When PATH lacks 'python', the resolver substitutes sys.executable and the check runs."""
    # Point PATH to a dir with only git (needed by DagRuntime)
    import shutil as _shutil

    # Build a PATH that contains ONLY a git symlink. Using git's own directory
    # is not enough: on CI runners git lives in /usr/bin next to python.
    git_path = _shutil.which("git")
    assert git_path is not None
    restricted_bin = tmp_path / "bin"
    restricted_bin.mkdir()
    (restricted_bin / "git").symlink_to(git_path)
    monkeypatch.setenv("PATH", str(restricted_bin))
    assert _shutil.which("python") is None
    assert _shutil.which("python3") is None

    resolved, resolved_argv0 = _resolve_verify_argv(["python", "-m", "pytest", "--co", "-q"])
    assert resolved[0] == sys.executable
    assert resolved_argv0 == sys.executable

    # Actually run it to prove it executes
    code, _out = await subprocess_runner(resolved, tmp_path, timeout=10.0)
    # It will fail (no tests) but the point is it didn't raise FileNotFoundError
    # exit code 5 = no tests collected, which is fine
    assert code != 127  # not "command not found"


@pytest.mark.parametrize("binary", ["python", "python3"])
@pytest.mark.parametrize("on_path", [False, True])
async def test_hydrated_command_matches_runtime_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, binary: str, on_path: bool
) -> None:
    import shlex

    from verdict.orchestration import verification
    from verdict.orchestration.contracts import WorkNode
    from verdict.orchestration.planner import hydrate_node_prompt
    from verdict.orchestration.runtime import DagRuntime, NodeRun, RuntimePolicy

    monkeypatch.setattr(
        verification.shutil, "which", lambda name: "/known/python" if on_path else None
    )
    node = WorkNode(
        "a",
        "check",
        owned_files=("a.py",),
        verification_command=(binary, "-c", "print('hello world')"),
    )
    prompt = hydrate_node_prompt(node, repo=tmp_path, goal="g")
    shown = next(
        line.removeprefix("VERIFICATION_COMMAND: ")
        for line in prompt.splitlines()
        if line.startswith("VERIFICATION_COMMAND:")
    )
    calls: list[list[str]] = []

    async def runner(argv: Any, cwd: Path, timeout: float) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, "ok"

    class Git:
        async def changed_files(self, worktree: Path, base: str) -> list[str]:
            return ["a.py"]

    class Events:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def emit(self, type: str, node_id: str = "", **data: Any) -> None:
            self.rows.append({"type": type, **data})

    runtime = object.__new__(DagRuntime)
    runtime.git = Git()  # type: ignore[assignment]
    runtime.runner = runner
    runtime.events = events = Events()
    runtime.policy = RuntimePolicy()
    assert await runtime._validate(NodeRun(node), tmp_path, "base") is None
    assert calls == [shlex.split(shown)]
    assert calls[0][0] == (binary if on_path else sys.executable)
    verify = next(e for e in events.rows if e["type"] == "verify")
    assert verify["executed_command"] == shown
    assert verify.get("resolved_argv0", "") == ("" if on_path else sys.executable)
