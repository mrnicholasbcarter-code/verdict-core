"""Tests for verification-command resilience: FileNotFoundError handling and python resolver."""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

import pytest

from tests.test_orch_runtime import repo as repo
from tests.test_repo_gates_runtime import gate_bin as gate_bin
from tests.test_repo_gates_runtime import make_runtime
from verdict.orchestration import verification
from verdict.orchestration.contracts import NodeState, OrchestrationError, RunOutcome
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
        async def changed_files(
            self, worktree: Path, base: str, *, owned_files: tuple[str, ...] = ()
        ) -> list[str]:
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


# ------------------------------------------------------------------ classify_verify_env_error: collection


# The verify tail of run 20261126T020427Z (event seq 30, node deliver-e2e, command
# ``python -m pytest -q tests -k deliver``): 59 collection errors, every one in a test file
# the node does not own. The first line is cut mid-path because the event keeps the last 600
# characters of the output.
_RUN_20261126T020427Z_TAIL = (
    "ts/test_security.py\n"
    "ERROR tests/test_serve_failure_feedback.py\n"
    "ERROR tests/test_serve_management_token.py\n"
    "ERROR tests/test_serve_route_pinning.py\n"
    "ERROR tests/test_serve_worker_assignment_auth.py\n"
    "ERROR tests/test_serve_worker_route_proof.py\n"
    "ERROR tests/test_stream_relay_wiring.py\n"
    "ERROR tests/test_tui_completion.py\n"
    "ERROR tests/test_tui_completion_surfaces.py\n"
    "ERROR tests/test_verified_models_surfaces.py\n"
    "ERROR tests/test_visual_system.py\n"
    "!!!!!!!!!!!!!!!!!!! Interrupted: 59 errors during collection !!!!!!!!!!!!!!!!!!!\n"
    "=============== 1 skipped, 14805 deselected, 59 errors in 10.92s ===============\n"
)
_RUN_ARGV = ("python", "-m", "pytest", "-q", "tests", "-k", "deliver")
_RUN_OWNED = (
    "tests/test_deliver_e2e.py",
    "verdict/commands/dispatch.py",
    "verdict/orchestration/deliver_service.py",
    "verdict/orchestration/deliver_publish.py",
)


def _collection_output(*paths: str, count: int | None = None) -> str:
    errors = count if count is not None else len(paths)
    return (
        "".join(f"ERROR {path}\n" for path in paths)
        + f"!!!!!!!!!!!!!!!!!!! Interrupted: {errors} errors during collection !!!!!!!!!!!!!!!!!!!\n"
        + f"=============== 1 skipped, 14805 deselected, {errors} errors in 10.92s ===============\n"
    )


def test_collection_errors_in_unowned_files_are_an_environment_error() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    reason = classify_verify_env_error(
        _RUN_ARGV, 2, _RUN_20261126T020427Z_TAIL, owned_files=_RUN_OWNED
    )
    assert reason is not None
    assert reason.startswith("environment_error:")
    assert "python -m pytest -q tests -k deliver" in reason
    assert "tests/test_serve_failure_feedback.py" in reason
    assert "tests/test_serve_management_token.py" in reason
    # Only the first few paths are named, so the reason stays short.
    assert "tests/test_visual_system.py" not in reason
    assert len(reason) < 600


def test_the_exact_run_tail_is_unchanged_without_the_node_owned_files() -> None:
    """``owned_files`` is optional; leaving it out, or passing ``None``, keeps today's result.

    The three-argument call is compatibility coverage and passes on ``main`` by design. The
    explicit ``owned_files=None`` is the part that did not exist on ``main`` (a ``TypeError``
    there): it pins the default of the new argument, so a caller that has no node (for example a
    verification run outside the DAG runtime) never gets the collection rule by accident.
    """
    from verdict.orchestration.verification import classify_verify_env_error

    assert classify_verify_env_error(_RUN_ARGV, 2, _RUN_20261126T020427Z_TAIL) is None
    assert (
        classify_verify_env_error(_RUN_ARGV, 2, _RUN_20261126T020427Z_TAIL, owned_files=None)
        is None
    )
    # The same output with the node's files is the environment error, so the ``None`` above is
    # the argument's doing and not an output that the rule cannot read.
    assert (
        classify_verify_env_error(_RUN_ARGV, 2, _RUN_20261126T020427Z_TAIL, owned_files=_RUN_OWNED)
        is not None
    )


def test_collection_errors_are_an_environment_error_for_a_node_that_owns_nothing() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    reason = classify_verify_env_error(
        _RUN_ARGV, 2, _collection_output("tests/test_a.py"), owned_files=()
    )
    assert reason is not None and reason.startswith("environment_error:")


@pytest.mark.parametrize(
    ("owned", "paths"),
    [
        pytest.param(
            ("tests/test_deliver_e2e.py", "verdict/a.py"),
            ("tests/test_other.py", "tests/test_deliver_e2e.py"),
            id="one-of-the-errors-is-an-owned-file",
        ),
        pytest.param(
            ("tests/test_deliver_e2e.py",), ("tests/test_deliver_e2e.py",), id="only-an-owned-file"
        ),
        pytest.param(
            ("tests/deliver/",), ("tests/deliver/test_x.py",), id="file-under-an-owned-directory"
        ),
        pytest.param(
            ("tests/test_a.py",), ("./tests/test_a.py",), id="owned-file-written-with-a-dot-prefix"
        ),
    ],
)
def test_a_collection_error_in_an_owned_file_stays_a_verification_failure(
    owned: tuple[str, ...], paths: tuple[str, ...]
) -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    assert (
        classify_verify_env_error(_RUN_ARGV, 2, _collection_output(*paths), owned_files=owned)
        is None
    )


def test_an_erroring_path_that_cannot_be_read_keeps_the_failure_a_verification_failure() -> None:
    """A path with a space is not a plain path: it may be the owned file, so stay conservative."""
    from verdict.orchestration.verification import classify_verify_env_error

    out = (
        "ERROR tests/test_other.py\n"
        "ERROR tests/test owned.py\n"
        "!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!\n"
    )
    assert (
        classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test owned.py",)) is None
    )
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=()) is None


def test_a_cut_error_list_is_not_trusted_when_the_runner_capped_the_output() -> None:
    """The runner keeps the last VERIFY_OUTPUT_TAIL_CHARS; an owned error may be in the cut part."""
    from verdict.orchestration.verification import (
        VERIFY_OUTPUT_TAIL_CHARS,
        classify_verify_env_error,
    )

    padding = "x" * VERIFY_OUTPUT_TAIL_CHARS + "\n"
    shown = _collection_output("tests/test_other.py", count=59)
    capped = (padding + shown)[-VERIFY_OUTPUT_TAIL_CHARS:]
    assert len(capped) == VERIFY_OUTPUT_TAIL_CHARS and "tests/test_other.py" in capped
    owned = ("tests/test_deliver_e2e.py",)
    # 59 errors counted, 1 shown, output at the cap: the other 58 may include an owned file.
    assert classify_verify_env_error(_RUN_ARGV, 2, capped, owned_files=owned) is None
    # The same count and paths in a short output (the event tail of run 20261126T020427Z)
    # are not capped, so the visible paths decide.
    assert classify_verify_env_error(_RUN_ARGV, 2, shown, owned_files=owned) is not None


def test_a_full_error_list_in_a_capped_output_still_decides() -> None:
    from verdict.orchestration.verification import (
        VERIFY_OUTPUT_TAIL_CHARS,
        classify_verify_env_error,
    )

    paths = tuple(f"tests/test_{n:03d}.py" for n in range(100))
    out = "x" * VERIFY_OUTPUT_TAIL_CHARS + "\n" + _collection_output(*paths)
    out = out[-VERIFY_OUTPUT_TAIL_CHARS:]
    assert len(out) == VERIFY_OUTPUT_TAIL_CHARS
    assert out.count("ERROR tests/test_") == 100
    owned = ("tests/test_deliver_e2e.py",)
    reason = classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=owned)
    assert reason is not None and reason.startswith("environment_error:")
    # ... and an owned file anywhere in that full list stays a verification failure.
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_042.py",)) is None


def test_a_collection_error_header_in_an_owned_file_stays_a_verification_failure() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    out = (
        "==================================== ERRORS ====================================\n"
        "_______________ ERROR collecting tests/test_deliver_e2e.py _______________\n"
        "ImportError while importing test module\n"
        "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!\n"
    )
    assert (
        classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_deliver_e2e.py",))
        is None
    )
    # The same header for a file the node does not own is an environment error.
    reason = classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_other.py",))
    assert reason is not None and "tests/test_deliver_e2e.py" in reason


def test_errors_with_a_reason_suffix_are_parsed_as_paths() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    out = (
        "ERROR tests/test_a.py - ModuleNotFoundError: No module named 'fastapi'\n"
        "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!\n"
    )
    reason = classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("verdict/a.py",))
    assert reason is not None and "tests/test_a.py" in reason
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_a.py",)) is None


_COLLECTION_INTERRUPTED = (
    "!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!\n"
)


@pytest.mark.parametrize(
    "spelling",
    [
        pytest.param("/repo/tests/test_a.py", id="absolute"),
        pytest.param("../repo/tests/test_a.py", id="parent-relative"),
        pytest.param("tests/../tests/test_a.py", id="parent-in-the-middle"),
    ],
)
@pytest.mark.parametrize("form", ["summary", "header"])
def test_a_non_repo_relative_erroring_path_is_inconclusive_not_unowned(
    spelling: str, form: str
) -> None:
    """The node owns ``tests/test_a.py``; pytest names that file with a path that is not
    repo-relative. Without a repository root it cannot be matched to the owned file, so it must
    not count as unowned: the worker gets another attempt, as for any owned-file failure."""
    from verdict.orchestration.verification import classify_verify_env_error

    line = (
        f"ERROR {spelling}\n"
        if form == "summary"
        else f"_______________ ERROR collecting {spelling} _______________\n"
    )
    out = line + _COLLECTION_INTERRUPTED
    owned = ("tests/test_a.py",)
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=owned) is None
    # A node that owns nothing gets the same answer: the path cannot be judged either way.
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=()) is None


def test_one_non_repo_relative_path_makes_the_whole_error_list_inconclusive() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    out = (
        "ERROR tests/test_other.py\n"
        "ERROR /repo/tests/test_a.py\n"
        "!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!\n"
    )
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_a.py",)) is None
    # The same list with both paths repo-relative and neither owned is still decided.
    relative = out.replace("/repo/tests/test_a.py", "tests/test_a.py")
    reason = classify_verify_env_error(_RUN_ARGV, 2, relative, owned_files=("verdict/a.py",))
    assert reason is not None and reason.startswith("environment_error:")


def test_a_dot_slash_path_is_still_matched_to_the_owned_file() -> None:
    """``./tests/test_a.py`` is repo-relative: it normalizes to the owned file, not to doubt."""
    from verdict.orchestration.verification import classify_verify_env_error

    out = "ERROR ./tests/test_a.py\n" + _COLLECTION_INTERRUPTED
    assert classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("tests/test_a.py",)) is None
    reason = classify_verify_env_error(_RUN_ARGV, 2, out, owned_files=("verdict/a.py",))
    assert reason is not None and reason.startswith("environment_error:")


@pytest.mark.parametrize(
    ("code", "out", "why"),
    [
        pytest.param(
            4,
            "ERROR: file or directory not found: tests/test_deliver_e2e.py\n",
            "exit 4 for an owned test file the worker did not write",
            id="exit-4-owned-file-missing",
        ),
        pytest.param(
            1,
            "FAILED tests/test_deliver_e2e.py::test_a - assert 1 == 2\n"
            "========= 1 failed, 3 passed in 0.31s =========\n",
            "ordinary test failures",
            id="exit-1-test-failures",
        ),
        pytest.param(
            5,
            "============ no tests ran in 0.02s ============\n",
            "no tests collected",
            id="exit-5-no-tests",
        ),
        pytest.param(
            1,
            _collection_output("tests/test_other.py"),
            "collection text under a non-collection exit code",
            id="exit-1-with-collection-text",
        ),
        pytest.param(
            2,
            "!!!!!!!!!!!!!!!!! KeyboardInterrupt !!!!!!!!!!!!!!!!!\n",
            "an interrupted run with no collection errors",
            id="exit-2-interrupted",
        ),
        pytest.param(
            2,
            "!!!!!!!!!!!!!!!!!!! Interrupted: 3 errors during collection !!!!!!!!!!!!!!!!!!!\n",
            "collection errors whose files cannot be read from the output",
            id="exit-2-no-error-paths",
        ),
        pytest.param(
            2,
            "ERROR tests/test_other.py\n=============== 1 error in 0.10s ===============\n",
            "error lines without the collection marker",
            id="exit-2-no-collection-marker",
        ),
    ],
)
def test_other_pytest_outcomes_stay_verification_failures(code: int, out: str, why: str) -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    assert classify_verify_env_error(_RUN_ARGV, code, out, owned_files=_RUN_OWNED) is None, why


def test_collection_output_from_a_command_that_is_not_pytest_is_not_reclassified() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    assert (
        classify_verify_env_error(
            ("make", "check"), 2, _collection_output("tests/test_other.py"), owned_files=_RUN_OWNED
        )
        is None
    )


@pytest.mark.parametrize(
    "argv",
    [
        ("pytest", "-q", "tests"),
        ("/usr/bin/pytest", "tests"),
        ("python3", "-m", "pytest", "tests"),
        ("uv", "run", "--frozen", "python", "-m", "pytest", "-q"),
    ],
)
def test_the_collection_rule_covers_the_usual_pytest_invocations(argv: tuple[str, ...]) -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    reason = classify_verify_env_error(
        argv, 2, _collection_output("tests/test_other.py"), owned_files=_RUN_OWNED
    )
    assert reason is not None and reason.startswith("environment_error:")


def test_existing_environment_rules_are_unchanged_when_owned_files_are_given() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    assert (
        classify_verify_env_error(("nope",), 127, "not found", owned_files=_RUN_OWNED) or ""
    ).startswith("environment_error: exit 127")
    assert (
        classify_verify_env_error(
            ("python", "-m", "pytest"), 1, "python: No module named pytest", owned_files=_RUN_OWNED
        )
        == "environment_error: No module named pytest"
    )
    assert (
        classify_verify_env_error(("python", "-m", "pytest"), 1, "boom", owned_files=_RUN_OWNED)
        is None
    )


def test_the_reason_names_at_most_three_paths_and_counts_the_rest() -> None:
    from verdict.orchestration.verification import classify_verify_env_error

    paths = tuple(f"tests/test_{n}.py" for n in "abcdef")
    reason = classify_verify_env_error(_RUN_ARGV, 2, _collection_output(*paths), owned_files=())
    assert reason is not None
    assert "tests/test_a.py" in reason and "tests/test_c.py" in reason
    assert "tests/test_d.py" not in reason
    assert "3 more" in reason


# ------------------------------------------------------------------ gate environment (#1363)
#
# ``resolve_gate_argv`` turned a bare ``mypy`` into ``uv run --frozen mypy``, which in a fresh
# worktree builds an environment WITHOUT the project's optional dependencies: mypy then reports
# every typed third-party import as missing.  The fourth self-build run was blocked at
# integration by exactly that (50 errors in 16 files on a tree that passes in the project's
# environment).

_EXTRAS_PYPROJECT = """\
[project]
name = "extraproj"
version = "0.0.0"
dependencies = []

[project.optional-dependencies]
typing = ["types-PyYAML"]

[tool.mypy]
strict = true
"""
_UV = "/usr/bin/uv"


@pytest.fixture()
def fake_uv(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(verification.shutil, "which", lambda exe: _UV if exe == "uv" else None)
    return _UV


def _uv_project(root: Path, pyproject: str = _EXTRAS_PYPROJECT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text(pyproject)
    (root / "uv.lock").write_text("# lock\n")
    return root


def test_the_mypy_gate_runs_with_the_projects_extras(tmp_path: Path, fake_uv: str) -> None:
    worktree = _uv_project(tmp_path)
    assert verification.resolve_gate_argv(("mypy", "--strict", "verdict"), worktree) == [
        fake_uv,
        "run",
        "--frozen",
        "--all-extras",
        "mypy",
        "--strict",
        "verdict",
    ]


def test_the_ruff_gates_resolve_as_before_when_the_project_has_extras(
    tmp_path: Path, fake_uv: str
) -> None:
    """ruff reads source and imports nothing from the project: no environment to build."""
    worktree = _uv_project(tmp_path)
    assert verification.resolve_gate_argv(("ruff", "check", "."), worktree) == [
        fake_uv,
        "run",
        "--frozen",
        "ruff",
        "check",
        ".",
    ]
    assert verification.resolve_gate_argv(("ruff", "format", "--check", "."), worktree) == [
        fake_uv,
        "run",
        "--frozen",
        "ruff",
        "format",
        "--check",
        ".",
    ]


@pytest.mark.parametrize(
    "pyproject",
    [
        pytest.param('[project]\nname = "p"\nversion = "0"\n', id="no-extras-table"),
        pytest.param(
            '[project]\nname = "p"\nversion = "0"\n\n[project.optional-dependencies]\n',
            id="empty-extras-table",
        ),
        pytest.param(
            '[project]\nname = "p"\nversion = "0"\n\n'
            '[project.optional-dependencies]\na = ["x"]\nb = ["y"]\n\n'
            '[tool.uv]\nconflicts = [[{ extra = "a" }, { extra = "b" }]]\n',
            id="conflicting-extras",
        ),
        pytest.param("this is [not toml", id="unreadable-pyproject"),
    ],
)
def test_the_mypy_command_is_unchanged_when_there_are_no_usable_extras(
    tmp_path: Path, fake_uv: str, pyproject: str
) -> None:
    """No extras (nothing to add), declared conflicts (uv would refuse ``--all-extras``) and a
    pyproject that cannot be read keep today's command, and never raise."""
    worktree = _uv_project(tmp_path, pyproject)
    assert verification.resolve_gate_argv(("mypy", "--strict", "verdict"), worktree) == [
        fake_uv,
        "run",
        "--frozen",
        "mypy",
        "--strict",
        "verdict",
    ]


def test_a_worktree_without_a_pyproject_keeps_the_uv_command(tmp_path: Path, fake_uv: str) -> None:
    (tmp_path / "uv.lock").write_text("# lock\n")
    assert verification.resolve_gate_argv(("mypy", "pkg"), tmp_path) == [
        fake_uv,
        "run",
        "--frozen",
        "mypy",
        "pkg",
    ]


def test_a_worktree_venv_still_wins_when_the_project_has_extras(
    tmp_path: Path, fake_uv: str
) -> None:
    worktree = _uv_project(tmp_path)
    mypy = worktree / ".venv" / "bin" / "mypy"
    mypy.parent.mkdir(parents=True)
    mypy.touch()
    assert verification.resolve_gate_argv(("mypy", "--strict", "verdict"), worktree) == [
        str(mypy),
        "--strict",
        "verdict",
    ]


def _venv_with_mypy(worktree: Path, *, made_by_uv: bool) -> Path:
    mypy = worktree / ".venv" / "bin" / "mypy"
    mypy.parent.mkdir(parents=True, exist_ok=True)
    mypy.touch()
    cfg = "home = /usr/bin\nversion_info = 3.12\n" + ("uv = 0.13.7\n" if made_by_uv else "")
    (worktree / ".venv" / "pyvenv.cfg").write_text(cfg)
    return mypy


def test_a_venv_uv_made_without_the_extras_is_completed_not_used_as_it_is(
    tmp_path: Path, fake_uv: str
) -> None:
    """The ``uv run --frozen ruff`` gate before mypy builds a venv of the base dependencies;
    if that has mypy, using it would repeat the failure with the fix in place."""
    worktree = _uv_project(tmp_path)
    _venv_with_mypy(worktree, made_by_uv=True)
    assert verification.resolve_gate_argv(("mypy", "--strict", "verdict"), worktree) == [
        fake_uv,
        "run",
        "--frozen",
        "--all-extras",
        "mypy",
        "--strict",
        "verdict",
    ]


def test_a_venv_not_made_by_uv_is_used_as_it_is(tmp_path: Path, fake_uv: str) -> None:
    worktree = _uv_project(tmp_path)
    mypy = _venv_with_mypy(worktree, made_by_uv=False)
    assert verification.resolve_gate_argv(("mypy", "pkg"), worktree) == [str(mypy), "pkg"]


def test_a_uv_made_venv_is_used_when_uv_is_not_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The venv rule never turns a runnable gate into an error."""
    monkeypatch.setattr(verification.shutil, "which", lambda exe: None)
    worktree = _uv_project(tmp_path)
    mypy = _venv_with_mypy(worktree, made_by_uv=True)
    assert verification.resolve_gate_argv(("mypy", "pkg"), worktree) == [str(mypy), "pkg"]


def test_a_uv_made_venv_is_used_by_ruff_and_by_projects_without_extras(
    tmp_path: Path, fake_uv: str
) -> None:
    worktree = _uv_project(tmp_path)
    ruff = worktree / ".venv" / "bin" / "ruff"
    _venv_with_mypy(worktree, made_by_uv=True)
    ruff.touch()
    assert verification.resolve_gate_argv(("ruff", "check", "."), worktree) == [
        str(ruff),
        "check",
        ".",
    ]
    plain = _uv_project(tmp_path / "plain", '[project]\nname = "p"\nversion = "0"\n')
    mypy = _venv_with_mypy(plain, made_by_uv=True)
    assert verification.resolve_gate_argv(("mypy", "pkg"), plain) == [str(mypy), "pkg"]


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs the uv binary")
async def test_the_resolved_mypy_gate_passes_in_a_real_uv_project_whose_stubs_are_in_an_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance check, with the real uv: ``import yaml`` needs ``types-PyYAML``, which only
    the ``typing`` extra installs.  Today's command fails with ``import-untyped``; the resolved
    gate passes.

    The one step that needs the package index is filling the cache (``uv sync``); a project
    that cannot be synced there skips the test.  The two commands under test then run offline.
    """
    project = tmp_path / "proj"
    (project / "pkg").mkdir(parents=True)
    (project / "pkg" / "__init__.py").write_text("")
    (project / "pkg" / "mod.py").write_text(
        "import yaml\n\n\ndef load(text: str) -> object:\n    return yaml.safe_load(text)\n"
    )
    (project / "pyproject.toml").write_text(
        _EXTRAS_PYPROJECT.replace("dependencies = []", 'dependencies = ["mypy"]')
    )
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("UV_OFFLINE", raising=False)
    try:
        warmed = subprocess.run(
            ["uv", "sync", "--all-extras"],
            cwd=project,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.skip("uv could not fill its cache in time (no package index reachable?)")
    if warmed.returncode != 0:
        pytest.skip(f"uv could not sync the temp project (no index?): {warmed.stderr[-200:]}")
    shutil.rmtree(project / ".venv")  # a fresh worktree has none
    monkeypatch.setenv("UV_OFFLINE", "1")

    gate = ("mypy", "--strict", "pkg")
    today = [shutil.which("uv") or "uv", "run", "--frozen", *gate]
    today_code, today_out = await subprocess_runner(today, project, 120)
    assert today_code == 1, today_out
    assert "import-untyped" in today_out, today_out

    # today's command left a venv of the base dependencies (mypy, no stubs) behind, as the
    # ruff gate before mypy does in a real worktree: the resolved gate completes it
    resolved = verification.resolve_gate_argv(gate, project)
    code, out = await subprocess_runner(resolved, project, 120)
    assert code == 0, f"{shlex.join(resolved)}: {out}"
    assert "Success: no issues found" in out


# ------------------------------------------------------------------ gate environment errors
#
# The tail of the fourth self-build run's mypy gate (``mypy --strict verdict`` with no extras):
# 24 of the 50 errors are unresolved imports and 26 follow from them (an untyped decorator
# from a missing library, a ``no-any-return`` from an untyped call).
_MYPY_ENV_OUTPUT = """\
verdict/api.py:12: error: Cannot find implementation or library stub for module named "fastapi"  [import-not-found]
verdict/home.py:1236: error: Untyped decorator makes function "escape" untyped  [untyped-decorator]
verdict/home.py:1245: error: Cannot find implementation or library stub for module named "prompt_toolkit"  [import-not-found]
verdict/home.py:1246: error: Cannot find implementation or library stub for module named "prompt_toolkit.history"  [import-not-found]
verdict/cli.py:14: error: Library stubs not installed for "yaml"  [import-untyped]
verdict/cli.py:14: note: Hint: "python3 -m pip install types-PyYAML"
verdict/cli.py:14: note: (or run "mypy --install-types" to install all missing stub packages)
verdict/util.py:9: error: Returning Any from function declared to return "str"  [no-any-return]
pyproject.toml: note: unused section(s): module = ['opentelemetry', 'opentelemetry.*', 'scripts.prime_workflow']
Found 50 errors in 16 files (checked 381 source files)
"""
_MYPY_ARGV = ("mypy", "--strict", "verdict")
# The same fault in a small project: the output is whole, every error is listed.
_MYPY_SHORT_ENV_OUTPUT = """\
verdict/cli.py:14: error: Library stubs not installed for "yaml"  [import-untyped]
verdict/cli.py:14: note: Hint: "python3 -m pip install types-PyYAML"
verdict/util.py:9: error: Returning Any from function declared to return "str"  [no-any-return]
Found 2 errors in 2 files (checked 3 source files)
"""


def _first_party_tree(root: Path) -> Path:
    (root / "verdict").mkdir(parents=True, exist_ok=True)
    (root / "verdict" / "__init__.py").write_text("")
    return root


def _imports(out: str, root: Path, argv: Sequence[str] = _MYPY_ARGV, code: int = 1) -> Any:
    return verification.unresolved_third_party_imports(argv, code, out, root)


def test_unresolved_third_party_imports_are_read_from_the_mypy_output(tmp_path: Path) -> None:
    imports = _imports(_MYPY_ENV_OUTPUT, _first_party_tree(tmp_path))
    assert imports is not None
    assert imports.packages == ("fastapi", "prompt_toolkit", "yaml")
    assert imports.error_count == 50
    # 7 error lines are kept of the 50 the summary counts: the runner cut the head off
    assert imports.complete is False


def test_a_short_output_that_lists_every_error_is_complete(tmp_path: Path) -> None:
    imports = _imports(_MYPY_SHORT_ENV_OUTPUT, _first_party_tree(tmp_path))
    assert imports is not None
    assert imports.packages == ("yaml",)
    assert imports.error_count == 2
    assert imports.complete is True


def test_a_missing_first_party_module_is_never_an_environment_error(tmp_path: Path) -> None:
    """A renamed or deleted module of the project itself is the implementation's fault, even
    next to unresolved third-party imports."""
    root = _first_party_tree(tmp_path)
    only = 'verdict/a.py:3: error: Cannot find implementation or library stub for module named "verdict.gone"  [import-not-found]\n'
    assert _imports(only, root) is None
    assert _imports(_MYPY_ENV_OUTPUT + only, root) is None


def test_a_src_layout_first_party_module_is_never_an_environment_error(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    out = 'src/pkg/a.py:3: error: Cannot find implementation or library stub for module named "pkg.gone"  [import-not-found]\n'
    assert _imports(out, tmp_path, ("mypy", "src")) is None


def test_a_module_in_a_configured_mypy_path_is_first_party(tmp_path: Path) -> None:
    """A project that keeps its packages under ``lib/`` and says so in ``mypy_path``: a missing
    module of its own there is the implementation's, not the environment's."""
    (tmp_path / "lib" / "pkg").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text('[tool.mypy]\nmypy_path = ["lib"]\n')
    out = 'lib/pkg/a.py:3: error: Cannot find implementation or library stub for module named "pkg.missing"  [import-not-found]\n'
    assert _imports(out, tmp_path, ("mypy", "lib")) is None
    # and without the configuration the same output names a third-party package: the config decides
    (tmp_path / "pyproject.toml").write_text("[tool.mypy]\n")
    assert _imports(out, tmp_path, ("mypy", "lib")) is not None


def test_a_module_in_the_mypypath_environment_variable_is_first_party(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "lib" / "pkg").mkdir(parents=True)
    monkeypatch.setenv("MYPYPATH", "other:lib")
    out = 'a.py:3: error: Cannot find implementation or library stub for module named "pkg.missing"  [import-not-found]\n'
    assert _imports(out, tmp_path, ("mypy", ".")) is None


def test_ordinary_type_errors_have_no_unresolved_imports(tmp_path: Path) -> None:
    out = (
        'verdict/a.py:3: error: Incompatible return value type (got "int", expected "str")  [return-value]\n'
        "Found 1 error in 1 file (checked 381 source files)\n"
    )
    assert _imports(out, _first_party_tree(tmp_path)) is None


def test_only_the_mypy_gate_is_read_for_unresolved_imports(tmp_path: Path) -> None:
    root = _first_party_tree(tmp_path)
    assert _imports(_MYPY_ENV_OUTPUT, root, ("ruff", "check", ".")) is None


def test_a_passing_gate_has_no_unresolved_imports(tmp_path: Path) -> None:
    assert _imports(_MYPY_ENV_OUTPUT, tmp_path, code=0) is None


def _fault(
    *packages: str, count: int | None = None, complete: bool = True, other: tuple[str, ...] = ()
) -> Any:
    return verification.UnresolvedImports(tuple(packages), count, complete, frozenset(other))


def test_the_same_unresolved_imports_on_the_untouched_base_are_the_environment() -> None:
    candidate = _fault("yaml", count=2)
    assert verification.environment_fault_refusal(candidate, _fault("yaml", count=2)) is None
    # the base may fail on more than the candidate does
    assert verification.environment_fault_refusal(candidate, _fault("yaml", "fastapi")) is None


def test_an_import_that_the_base_resolves_is_not_the_environment() -> None:
    """A typo (``reqeusts``) and a deleted top-level package both resolve, or never existed, on
    the untouched base: the implementation introduced the missing import."""
    refusal = verification.environment_fault_refusal(_fault("reqeusts"), _fault("yaml"))
    assert refusal is not None and "reqeusts" in refusal
    # one new name next to the base's own is still a new unresolved import
    mixed = verification.environment_fault_refusal(_fault("yaml", "reqeusts"), _fault("yaml"))
    assert mixed is not None and "reqeusts" in mixed and "yaml" not in mixed


def test_other_errors_are_compared_by_file_code_and_message_not_by_line(tmp_path: Path) -> None:
    root = _first_party_tree(tmp_path)
    here = f"{root}/verdict/a.py"
    moved = (
        _import_error("yaml")
        + f'{here}:40: error: Returning Any from function declared to return "str"  [no-any-return]\n'
    )
    base = (
        _import_error("yaml")
        + 'verdict/a.py:7: error: Returning Any from function declared to return "str"  [no-any-return]\n'
    )
    candidate_imports = _imports(moved, root)
    base_imports = _imports(base, root)
    assert candidate_imports is not None and base_imports is not None
    assert candidate_imports.other_errors == base_imports.other_errors
    assert verification.environment_fault_refusal(candidate_imports, base_imports) is None


def test_an_error_the_base_does_not_report_is_not_explained_by_the_environment() -> None:
    """Review finding of #1364 (round 2): the same unresolved imports beside a new, ordinary type
    error do not make the whole failure the environment's."""
    base = _fault("yaml", other=("verdict/a.py [no-any-return] Returning Any",))
    same = _fault("yaml", other=("verdict/a.py [no-any-return] Returning Any",))
    assert verification.environment_fault_refusal(same, base) is None
    assert verification.environment_fault_refusal(_fault("yaml"), base) is None  # fewer is fine
    worse = _fault(
        "yaml",
        other=(
            "verdict/a.py [no-any-return] Returning Any",
            "verdict/b.py [return-value] Incompatible return value type",
        ),
    )
    refusal = verification.environment_fault_refusal(worse, base)
    assert refusal is not None
    assert "verdict/b.py [return-value]" in refusal and "no-any-return" not in refusal


def test_more_unresolved_imports_of_a_package_the_base_cannot_resolve_are_explained() -> None:
    """New code that imports a library the environment lacks is the environment's: the base
    shows the library is missing there."""
    assert verification.environment_fault_refusal(_fault("yaml", "yaml"), _fault("yaml")) is None


def test_a_base_without_the_fault_is_no_evidence() -> None:
    refusal = verification.environment_fault_refusal(_fault("yaml"), None)
    assert refusal is not None and "untouched base" in refusal


def test_a_cut_output_needs_no_more_errors_than_the_base() -> None:
    """The runner keeps the last 4000 characters, so a new unresolved import in the cut-off head
    is not seen: the totals mypy prints still are."""
    cut = _fault("yaml", count=52, complete=False)
    assert verification.environment_fault_refusal(cut, _fault("yaml", count=50)) is not None
    assert (
        verification.environment_fault_refusal(
            _fault("yaml", count=50, complete=False), _fault("yaml", count=50)
        )
        is None
    )
    assert verification.environment_fault_refusal(cut, _fault("yaml", count=None)) is not None
    assert (
        verification.environment_fault_refusal(
            _fault("yaml", count=None, complete=False), _fault("yaml", count=50)
        )
        is not None
    )


def test_the_environment_reason_names_at_most_three_packages() -> None:
    reason = verification.describe_environment_fault(
        _MYPY_ARGV, _fault("a", "b", "c", "d", "e", count=9, complete=False)
    )
    assert "a, b, c and 2 more" in reason
    assert "no-any-return" not in reason  # no output text beyond the package names


# ------------------------------------------------------------------ gates through the runtime

_MYPY_CONFIG = "[tool.mypy]\nfiles = ['a.txt']\n"


def _mypy_runner(
    runtime: Any, *, candidate: tuple[int, str], base: tuple[int, str] | None
) -> list[Path]:
    """Make mypy fail with ``candidate`` in the integration worktree and answer ``base`` in the
    scratch worktree of the run's untouched base (passing when ``base`` is None).  Returns the
    worktrees mypy ran in at the base."""
    normal_runner = runtime.runner
    base_runs: list[Path] = []

    async def runner(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "mypy":
            if cwd.name.startswith("i-"):
                return candidate
            if cwd.name.startswith("integration-base-"):
                base_runs.append(cwd)
                return base if base is not None else (0, "Success: no issues found")
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = runner
    return base_runs


def _import_error(module: str) -> str:
    return (
        f'a.py:1: error: Cannot find implementation or library stub for module named "{module}"'
        "  [import-not-found]\n"
    )


async def _worktrees_and_branches(repo: Path) -> tuple[str, str]:
    worktrees = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    branches = subprocess.run(
        ["git", "branch", "--list", "verdict/integration-base-*"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    return worktrees, branches


async def test_an_environment_fault_at_integration_is_an_environment_error(
    repo: Path, gate_bin: Path
) -> None:
    """The fourth self-build run: the integration mypy gate failed on a tree that passes in the
    project's environment, and was recorded as ``verification_failed -> CORRECT_IMPLEMENTATION``.
    The same gate fails the same way on the run's untouched base, so the environment is the
    cause."""
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    base_runs = _mypy_runner(runtime, candidate=(1, _MYPY_ENV_OUTPUT), base=(1, _MYPY_ENV_OUTPUT))
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert result.nodes["i"].state is NodeState.BLOCKED
    failure = events.of("failure", "i")[0]
    assert failure["category"] == "environment_error"
    assert failure["action"] == "BLOCK"
    assert result.nodes["i"].reason.startswith("environment_error: repo_gate_failed: mypy exit 1")
    assert "verification_failed" not in result.nodes["i"].reason
    gate = next(e for e in events.of("repo_gate", "i") if e["name"] == "mypy")
    assert gate["ok"] is False and gate["exit_code"] == 1
    assert "fastapi" in gate["environment_error"]
    assert [e["state"] for e in events.of("node_state", "i")][-2:] == ["REJECTED", "BLOCKED"]
    # Not a judged failure: nothing about the implementation was found wrong.
    assert result.nodes["i"].judged_failure_count == 0
    assert result.nodes["i"].history[-1]["outcome"] == "environment_error"
    assert result.nodes["i"].history[-1]["rejected"] is False
    # The evidence came from one run of the gate at the base, whose worktree and branch are gone.
    assert len(base_runs) == 1
    worktrees, branches = await _worktrees_and_branches(repo)
    assert "integration-base-" not in worktrees and branches.strip() == ""


@pytest.mark.parametrize(
    ("candidate", "base", "named"),
    [
        pytest.param(  # a typo of a library: the base never imported it
            (1, _import_error("reqeusts") + "Found 1 error in 1 file (checked 2 source files)\n"),
            None,
            "reqeusts",
            id="typo-and-the-base-passes",
        ),
        pytest.param(  # the base has the environment fault, and the candidate one more name
            (
                1,
                _import_error("yaml")
                + _import_error("reqeusts")
                + "Found 2 errors in 1 file (checked 2 source files)\n",
            ),
            (1, _import_error("yaml") + "Found 1 error in 1 file (checked 2 source files)\n"),
            "reqeusts",
            id="typo-beside-the-base-fault",
        ),
        pytest.param(  # the cut-off head of the candidate hides what the base does not have
            (1, _MYPY_ENV_OUTPUT.replace("Found 50 errors", "Found 52 errors")),
            (1, _MYPY_ENV_OUTPUT),
            "52 errors",
            id="cut-output-with-more-errors-than-the-base",
        ),
    ],
)
async def test_a_missing_import_the_implementation_introduced_is_a_verification_failure(
    repo: Path, gate_bin: Path, candidate: tuple[int, str], base: tuple[int, str] | None, named: str
) -> None:
    """Review finding 1 of #1364: an unresolved name that is not first-party in the tree is not
    proof of a broken environment.  A typo (or a deleted top-level package) is unresolved only
    in the candidate, so the run says the implementation is wrong, with the reason recorded."""
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    base_runs = _mypy_runner(runtime, candidate=candidate, base=base)
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    failure = events.of("failure", "i")[0]
    assert failure["category"] == "verification_failed"
    assert failure["action"] == "CORRECT_IMPLEMENTATION"
    assert result.nodes["i"].reason.startswith("repo_gate_failed: mypy exit 1")
    assert result.nodes["i"].judged_failure_count == 1
    gate = next(e for e in events.of("repo_gate", "i") if e["name"] == "mypy")
    assert "environment_error" not in gate
    assert named in gate["environment_ruled_out"]
    assert len(base_runs) == 1
    worktrees, branches = await _worktrees_and_branches(repo)
    assert "integration-base-" not in worktrees and branches.strip() == ""


async def test_a_new_type_error_beside_the_environment_fault_is_a_verification_failure(
    repo: Path, gate_bin: Path
) -> None:
    """Review finding of #1364 (round 2): the base only lacks ``yaml``; the candidate lacks it too
    and also returns the wrong type.  The same unresolved import does not absolve that."""
    base_out = _import_error("yaml") + "Found 1 error in 1 file (checked 2 source files)\n"
    candidate_out = (
        _import_error("yaml")
        + 'a.py:5: error: Incompatible return value type (got "int", expected "str")'
        "  [return-value]\n"
        "Found 2 errors in 1 file (checked 2 source files)\n"
    )
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    _mypy_runner(runtime, candidate=(1, candidate_out), base=(1, base_out))
    result = await runtime.run()
    assert events.of("failure", "i")[0]["category"] == "verification_failed"
    assert events.of("failure", "i")[0]["action"] == "CORRECT_IMPLEMENTATION"
    assert result.nodes["i"].judged_failure_count == 1
    gate = next(e for e in events.of("repo_gate", "i") if e["name"] == "mypy")
    assert "environment_error" not in gate
    assert "a.py [return-value]" in gate["environment_ruled_out"]


async def test_new_code_using_a_library_the_base_lacks_is_still_the_environment(
    repo: Path, gate_bin: Path
) -> None:
    base_out = _import_error("yaml") + "Found 1 error in 1 file (checked 2 source files)\n"
    candidate_out = (
        _import_error("yaml")
        + 'b.py:2: error: Library stubs not installed for "yaml"  [import-untyped]\n'
        + "Found 2 errors in 2 files (checked 3 source files)\n"
    )
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    _mypy_runner(runtime, candidate=(1, candidate_out), base=(1, base_out))
    await runtime.run()
    assert events.of("failure", "i")[0]["category"] == "environment_error"


async def test_an_environment_fault_without_evidence_from_the_base_is_a_verification_failure(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No base worktree, no evidence: the failure stays the implementation's."""
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    base_runs = _mypy_runner(runtime, candidate=(1, _MYPY_ENV_OUTPUT), base=(1, _MYPY_ENV_OUTPUT))
    real_add = runtime.git.add_worktree

    async def refuse_the_base(path: Path, branch: str, base: str) -> None:
        if path.name.startswith("integration-base-"):
            raise OrchestrationError("git worktree add failed (128): no space left")
        await real_add(path, branch, base)

    monkeypatch.setattr(runtime.git, "add_worktree", refuse_the_base)
    result = await runtime.run()
    assert events.of("failure", "i")[0]["category"] == "verification_failed"
    assert result.nodes["i"].judged_failure_count == 1
    gate = next(e for e in events.of("repo_gate", "i") if e["name"] == "mypy")
    assert "environment_error" not in gate and "untouched base" in gate["environment_ruled_out"]
    assert base_runs == []


async def test_a_real_gate_failure_at_integration_stays_a_verification_failure(
    repo: Path, gate_bin: Path
) -> None:
    runtime, events, calls = make_runtime(repo, _MYPY_CONFIG)
    normal_runner = runtime.runner

    async def real_type_error(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "mypy" and cwd.name.startswith("i-"):
            return 1, "verdict/a.py:3: error: Incompatible return value type  [return-value]\n"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = real_type_error
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    assert events.of("failure", "i")[0]["category"] == "verification_failed"
    assert result.nodes["i"].reason.startswith("repo_gate_failed: mypy exit 1")
    assert result.nodes["i"].judged_failure_count == 1
    # no signal of a fault of the environment, so the base is never run (a cold install)
    assert not [c for _, c, _ in calls if c.name.startswith("integration-base-")]


async def test_a_real_failure_in_another_gate_wins_over_an_environment_fault(
    repo: Path, gate_bin: Path
) -> None:
    """ruff fails for real and mypy for the environment: the implementation is wrong, so the
    run says so."""
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n" + _MYPY_CONFIG)
    normal_runner = runtime.runner

    async def both(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        name = Path(argv[0]).name
        if name == "mypy" and cwd.name.startswith(("i-", "integration-base-")):
            return 1, _MYPY_ENV_OUTPUT  # the environment fault, proven on the base too
        if cwd.name.startswith("i-") and name == "ruff" and "check" in argv:
            return 1, "a.py:1:1: F401 unused import"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = both
    result = await runtime.run()
    assert events.of("failure", "i")[0]["category"] == "verification_failed"
    assert result.nodes["i"].reason.startswith("repo_gate_failed: ruff-check exit 1")


async def test_the_same_fault_at_node_level_is_still_left_to_integration(
    repo: Path, gate_bin: Path
) -> None:
    """Why the node gates passed in the run and the integration gate did not: a gate that
    already fails on the node's base is existing debt, never the node's fault, so the same
    environment fault that hit every worktree equally blocked only the integration barrier."""
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    normal_runner = runtime.runner

    async def broken_everywhere(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "mypy":
            return 1, _MYPY_ENV_OUTPUT
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = broken_everywhere
    result = await runtime.run()
    assert result.nodes["a"].state is NodeState.VALIDATED
    assert not events.of("failure", "a")
    node_gate = next(e for e in events.of("repo_gate", "a") if e["name"] == "mypy")
    assert node_gate["ok"] is False
    assert events.of("failure", "i")[0]["category"] == "environment_error"


async def test_node_base_and_integration_gates_all_run_with_the_projects_extras(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolver serves the node worktree, the base worktree the node is compared with, and
    the integration worktree, so the fix reaches all three."""
    monkeypatch.setattr(verification.shutil, "which", lambda exe: _UV if exe == "uv" else None)
    (repo / "uv.lock").write_text("# lock\n")
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    config = _EXTRAS_PYPROJECT.replace(
        "[tool.mypy]\nstrict = true", "[tool.mypy]\nfiles = ['a.txt']"
    )
    runtime, _, calls = make_runtime(repo, config)
    normal_runner = runtime.runner

    async def mypy_fails_on_the_node(
        argv: Sequence[str], cwd: Path, timeout: float
    ) -> tuple[int, str]:
        if "mypy" in argv and cwd.name.startswith(("a-", "gate-base-")):
            calls.append((list(argv), cwd, timeout))
            return 1, "the same failure on the node and on its base"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = mypy_fails_on_the_node
    await runtime.run()
    mypy_runs = {
        (
            "node"
            if cwd.name.startswith("a-")
            else "base"
            if cwd.name.startswith("gate-base-")
            else "integration"
        ): argv
        for argv, cwd, _ in calls
        if "mypy" in argv
    }
    assert set(mypy_runs) == {"node", "base", "integration"}, calls
    for where, argv in mypy_runs.items():
        assert argv[:5] == [_UV, "run", "--frozen", "--all-extras", "mypy"], (where, argv)


# ------------------------------------------------------------------ gates that cannot run


def _failure_is_environment(result: Any, events: Any) -> None:
    run = result.nodes["i"]
    failure = events.of("failure", "i")[0]
    assert failure["category"] == "environment_error"
    assert failure["action"] == "BLOCK"
    assert run.reason.startswith("environment_error: repo_gate_failed: ")
    assert run.judged_failure_count == 0
    assert run.history[-1]["rejected"] is False


async def test_a_gate_executable_that_cannot_be_found_is_an_environment_error_at_integration(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review finding 2 of #1364: ``resolve_gate_argv`` raised (exit 127), and the barrier
    recorded ``verification_failed -> CORRECT_IMPLEMENTATION`` for a missing tool."""
    (gate_bin / "ruff").unlink()
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    result = await runtime.run()
    assert result.outcome is RunOutcome.BLOCKED
    _failure_is_environment(result, events)
    assert "ruff-check exit 127" in result.nodes["i"].reason
    assert "gate executable not found: ruff" in result.nodes["i"].reason
    # the node level leaves it to the barrier, as before
    assert result.nodes["a"].state is NodeState.VALIDATED
    assert not events.of("failure", "a")


async def test_uv_missing_from_the_path_is_an_environment_error_at_integration(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv.lock`` is present and ``uv`` is not on PATH: the gate cannot run."""
    (repo / "uv.lock").write_text("# lock\n")
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    runtime, events, _ = make_runtime(repo, _MYPY_CONFIG)
    result = await runtime.run()
    _failure_is_environment(result, events)
    assert "uv not found" in result.nodes["i"].reason
    assert events.of("failure", "i")[0]["evidence"].startswith("environment_error:")


@pytest.mark.parametrize("code", [126, 127])
async def test_a_gate_command_that_could_not_start_is_an_environment_error_at_integration(
    repo: Path, gate_bin: Path, code: int
) -> None:
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n")
    normal_runner = runtime.runner

    async def cannot_start(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == "ruff" and cwd.name.startswith("i-"):
            return code, f"verification command not found: {argv[0]}: [Errno 2]"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = cannot_start
    result = await runtime.run()
    _failure_is_environment(result, events)
    assert f"ruff-check exit {code}" in result.nodes["i"].reason


@pytest.mark.parametrize(("missing", "real"), [("ruff", "mypy"), ("mypy", "ruff")])
async def test_a_real_failure_wins_over_a_gate_that_cannot_run_in_either_order(
    repo: Path, gate_bin: Path, monkeypatch: pytest.MonkeyPatch, missing: str, real: str
) -> None:
    """The missing tool must not hide a gate that really failed: the implementation is wrong
    whichever gate comes first (``ruff-check`` runs before ``mypy``)."""
    monkeypatch.setattr(verification.shutil, "which", lambda name: None)
    (gate_bin / missing).unlink()
    runtime, events, _ = make_runtime(repo, "[tool.ruff]\n" + _MYPY_CONFIG)
    normal_runner = runtime.runner

    async def fails(argv: Sequence[str], cwd: Path, timeout: float) -> tuple[int, str]:
        if Path(argv[0]).name == real and cwd.name.startswith("i-"):
            return 1, "a.py:1:1: a real failure"
        return await normal_runner(argv, cwd, timeout)

    runtime.runner = fails
    result = await runtime.run()
    assert events.of("failure", "i")[0]["category"] == "verification_failed"
    assert result.nodes["i"].reason.startswith("repo_gate_failed: ")
    assert f"{real if real == 'mypy' else 'ruff-check'} exit 1" in result.nodes["i"].reason
    assert result.nodes["i"].judged_failure_count == 1
