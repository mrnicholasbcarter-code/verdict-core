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
    from verdict.orchestration.verification import classify_verify_env_error

    assert classify_verify_env_error(_RUN_ARGV, 2, _RUN_20261126T020427Z_TAIL) is None


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
