"""Retry after a verification failure continues from the failed attempt's owned files
for in-place editing workers (harness "opencode"); everything else keeps the clean base."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from tests.test_orch_salvage import NOW, Events, Selector, _node
from verdict.orchestration.contracts import RunOutcome, WorkerTerminal, WorkGraph
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy


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


class _Executor:
    """First attempt writes ``first_content`` (and optional extra files); later attempts
    record what the owned file held at start, then write passing content."""

    def __init__(
        self,
        harness: str,
        *,
        first_content: str = "FAIL: bad implementation\n",
        first_error: str = "",
        extra_files: tuple[str, ...] = (),
    ) -> None:
        self.harness = harness
        self.first_content = first_content
        self.first_error = first_error
        self.extra_files = extra_files
        self.attempt = 0
        self.second_start_content: str | None = None

    async def run(
        self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
    ) -> WorkerTerminal:
        node_id = prompt.split(":", 1)[0]
        self.attempt += 1
        owned = cwd / f"{node_id}.txt"
        if self.attempt == 1:
            owned.write_text(self.first_content)
            for extra in self.extra_files:
                (cwd / extra).write_text("outside ownership\n")
            if self.first_error:
                return WorkerTerminal(
                    ok=False, model=route_id, error=self.first_error, harness=self.harness
                )
        else:
            self.second_start_content = owned.read_text() if owned.exists() else ""
            owned.write_text("done\n")
        return WorkerTerminal(
            ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop", harness=self.harness
        )


def _run(repo_path: Path, executor: _Executor) -> tuple[Events, RunOutcome, str]:
    events = Events()
    runtime = DagRuntime(
        repo=repo_path,
        run_dir=repo_path.parent / "run-retry-failed-tree",
        graph=WorkGraph("g", (_node("a"),)),
        selector=Selector(["r1/m1", "r2/m2"]),
        executor=executor,
        classifier=FailureIntelligence(),
        events=events,
        prompt_for=lambda n, cwd, **kw: f"{n.node_id}: {n.objective}",
        reviewer=None,
        policy=RuntimePolicy(require_review=False, run_repo_gates=False),
        now=lambda: NOW,
    )
    result = asyncio.run(runtime.run())
    return events, result.outcome, result.reason


def test_opencode_verification_failure_restores_failed_tree(repo: Path) -> None:
    executor = _Executor("opencode")
    events, outcome, reason = _run(repo, executor)
    assert outcome is RunOutcome.COMPLETE, reason

    assert events.of("salvage", "a")
    restores = events.of("restore", "a")
    assert len(restores) == 1, restores
    assert restores[0]["reason"] == "verification_failed"
    assert "a.txt" in restores[0]["restored"]
    assert executor.second_start_content == "FAIL: bad implementation\n"


def test_other_harness_verification_failure_starts_from_clean_base(repo: Path) -> None:
    executor = _Executor("")
    events, outcome, reason = _run(repo, executor)
    assert outcome is RunOutcome.COMPLETE, reason

    assert not events.of("salvage")
    restores = events.of("restore", "a")
    assert len(restores) == 1, restores
    assert restores[0]["reason"] == "clean_base"
    assert restores[0]["restored"] == []
    assert executor.second_start_content == ""


def test_opencode_diff_rejected_starts_from_clean_base(repo: Path) -> None:
    executor = _Executor(
        "opencode",
        first_content="partial diff work\n",
        first_error="diff_rejected: SEARCH text not found",
    )
    events, outcome, reason = _run(repo, executor)
    assert outcome is RunOutcome.COMPLETE, reason

    assert not events.of("salvage")
    restores = events.of("restore", "a")
    assert len(restores) == 1, restores
    assert restores[0]["reason"] == "clean_base"
    assert executor.second_start_content == ""


def test_opencode_ownership_violation_starts_from_clean_base(repo: Path) -> None:
    executor = _Executor("opencode", first_content="fine\n", extra_files=("stray.txt",))
    events, outcome, reason = _run(repo, executor)
    assert outcome is RunOutcome.COMPLETE, reason

    failures = events.of("failure", "a")
    assert failures and failures[0]["category"] == "ownership_violation"
    assert not events.of("salvage")
    restores = events.of("restore", "a")
    assert len(restores) == 1, restores
    assert restores[0]["reason"] == "clean_base"
    assert executor.second_start_content == ""
