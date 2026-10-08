"""Hermetic BOD-205 OpenSpec conformance and receipt gates."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.test_openspec_resume import _Classifier, _make_repo, _NoExec, _Reviewer, _Sel
from tests.test_orch_runtime import Executor
from verdict import openspec_lifecycle as lifecycle
from verdict.openspec_lifecycle import OpenSpecChange, check_conformance
from verdict.orchestration.contracts import WorkGraph, WorkNode
from verdict.orchestration.receipt import EventLog, completion_verdict, verify_run_receipt
from verdict.orchestration.run import run_golden_path


def _fixture(repo: Path) -> Path:
    source = Path(__file__).parent / "fixtures" / "openspec" / "valid-change"
    destination = repo / "openspec" / "changes" / "valid-change"
    shutil.copytree(source, destination)
    return destination


def _cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    valid: bool = True,
    available: bool = True,
    mutate: bool = False,
) -> None:
    def fake(
        args: list[str], cwd: Path | None = None, **kwargs: Any
    ) -> subprocess.CompletedProcess[str] | None:
        if not available:
            return None
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="1.13.2", stderr="")
        assert args == ["validate", "valid-change", "--strict", "--json"]
        if mutate:
            assert cwd is not None
            (cwd / "openspec" / "changes" / "valid-change" / "proposal.md").write_text("changed")
        payload = {
            "items": [
                {
                    "id": "valid-change",
                    "type": "change",
                    "valid": valid,
                    "issues": [] if valid else [{"message": "invalid"}],
                }
            ]
        }
        return subprocess.CompletedProcess(
            args, 0 if valid else 1, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(lifecycle, "_run_openspec_command", fake)


@pytest.mark.parametrize(
    "case,expected",
    [("pass", "COMPLETE"), ("fail", "BLOCKED"), ("unavailable", "BLOCKED"), ("changed", "BLOCKED")],
)
async def test_conformance_run_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, expected: str
) -> None:
    repo = _make_repo(tmp_path)
    change_dir = _fixture(repo)
    _cli(
        monkeypatch, valid=case != "fail", available=case != "unavailable", mutate=case == "changed"
    )
    # Real vendored admission executes against the copied valid fixture.
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )

    class NamedExecutor(Executor):
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> Any:
            (cwd / "a.txt").write_text("updated\n")
            from verdict.orchestration.contracts import WorkerTerminal

            return WorkerTerminal(
                ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop"
            )

    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=tmp_path / "runs",
        graph=graph,
        selector=_Sel(),
        executor=NamedExecutor({}),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        openspec_change_dir=change_dir,
    )
    assert result.outcome == expected, (
        result.reason,
        [
            (e.type, e.data.get("reason"), e.data.get("error"), e.data.get("ok"))
            for e in EventLog(result.run_dir / "events.jsonl").read()
            if e.type in ("node_state", "failure", "verify", "terminal", "run_finished")
        ],
    )
    block = json.loads((result.run_dir / "graph.json").read_text())["openspec"]
    receipt = json.loads(result.receipt_path.read_text())
    assert block["linear_issue"] is None
    assert block["repo"] == str(repo)
    assert block["worktree"] == str(result.run_dir / "worktrees" / "a-a1")
    assert block["branch"] == "main"
    assert block["mapping_source"] == "node_id"
    assert block["completed_tasks"] == ["a"]
    assert block["proof_state"] in ("COMPLETE", "BLOCKED")
    assert receipt["openspec"]["conformance_result"] == block["conformance_result"]
    assert verify_run_receipt(result.run_dir) == []
    assert any(
        e.type == "openspec_admission" and e.data["admitted"]
        for e in EventLog(result.run_dir / "events.jsonl").read()
    )
    checks = [
        e
        for e in EventLog(result.run_dir / "events.jsonl").read()
        if e.type == "openspec_conformance"
    ]
    assert len(checks) == 1
    assert checks[0].data["result"]["status"] == (
        "PASS" if case == "pass" else "FAIL" if case == "fail" else "BLOCKED"
    )
    if case == "changed":
        assert "spec_changed" in result.reason


async def test_invalid_change_blocks_before_executor(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    path = repo / "openspec" / "changes" / "invalid-change"
    path.mkdir(parents=True)
    (path / "proposal.md").write_text("# placeholder")
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    result = await run_golden_path(
        "g",
        repo=repo,
        runs_root=tmp_path / "runs",
        graph=graph,
        selector=_Sel(),
        executor=_NoExec(),
        classifier=_Classifier(),
        reviewer=_Reviewer(),
        openspec_change_dir=path,
    )
    assert result.outcome == "BLOCKED"
    assert "Validation failed" in result.reason
    assert not any(e.type == "dispatch" for e in EventLog(result.run_dir / "events.jsonl").read())


def test_conformance_malformed_json_or_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _make_repo(tmp_path)
    change_dir = _fixture(repo)
    change = OpenSpecChange("valid-change", None, "verdict-change-v1", change_dir, {})

    def malformed(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args, 0, stdout="1.13.2" if args == ["--version"] else "bad", stderr=""
        )

    monkeypatch.setattr(lifecycle, "_run_openspec_command", malformed)
    assert check_conformance(repo, change)["status"] == "BLOCKED"
    _cli(monkeypatch, available=False)
    assert "unavailable" in check_conformance(repo, change)["reason"]


def test_receipt_blocks_fake_pass_digest() -> None:
    receipt = {
        "schema": "verdict.run-receipt/v1",
        "openspec": {
            "spec_revision_digest": "a",
            "conformance_result": {"status": "PASS", "spec_revision_digest": "b"},
        },
    }
    assert completion_verdict(receipt)[0] == "BLOCKED"
    assert "spec_changed" in completion_verdict(receipt)[1]


def test_orchestrate_flag_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse

    from verdict.orchestration import cli

    # Parser accepts the new id, and _orchestrate passes its resolved directory.
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    from verdict.orchestration import supervisor

    monkeypatch.setattr(supervisor, "add_parser", lambda subs: None)
    cli.add_parsers(subparsers)
    args = parser.parse_args(
        [
            "orchestrate",
            "goal",
            "--openspec-change",
            "valid-change",
            "--repo",
            str(tmp_path),
            "--json",
        ]
    )
    assert args.openspec_change == "valid-change"
    assert (tmp_path / "openspec" / "changes" / args.openspec_change).name == "valid-change"


def test_orchestrate_resolves_change_into_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    from types import SimpleNamespace

    from verdict.orchestration import cli, recovery, run

    change_dir = tmp_path / "openspec" / "changes" / "valid-change"
    change_dir.mkdir(parents=True)
    captured: dict[str, Any] = {}

    async def fake_run(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        target = kwargs["runs_root"] / kwargs["run_id"]
        (target / "receipt.json").write_text("{}")
        return SimpleNamespace(
            outcome="BLOCKED", reason="test", run_dir=target, receipt_path=target / "receipt.json"
        )

    monkeypatch.setattr(run, "run_golden_path", fake_run)
    monkeypatch.setattr(run, "resolve_api_key", lambda: "fake")
    monkeypatch.setattr(cli, "build_selector", lambda *a, **kw: SimpleNamespace(summary=lambda: {}))
    monkeypatch.setattr(cli, "_executor", lambda args: object())
    monkeypatch.setattr(recovery, "FailureIntelligence", lambda: object())
    args = argparse.Namespace(
        repo=str(tmp_path),
        runs_dir=str(tmp_path / "runs"),
        goal="goal",
        graph=None,
        resume=None,
        openspec_change="valid-change",
        gateway="x",
        scope="",
        prefer="",
        state_file=None,
        inject=[],
        max_parallel=1,
        attempt_timeout=1,
        run_deadline=1,
        no_review=True,
        json=True,
        executor_map="",
    )
    assert cli._orchestrate(args) == 1
    assert captured["openspec_change_dir"] == change_dir


def test_receipt_conformance_uses_events_not_mutable_graph(tmp_path: Path) -> None:
    from verdict.orchestration.receipt import build_run_receipt

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        "g", (WorkNode("a", "write a", owned_files=("a.txt",), verification_command=("true",)),)
    )
    (run_dir / "graph.json").write_text(
        json.dumps(
            {
                **graph.to_dict(),
                "openspec": {
                    "spec_revision_digest": "rev",
                    "mapping_source": "node_id",
                    "conformance_result": {"status": "PASS", "spec_revision_digest": "rev"},
                },
            }
        )
    )
    EventLog(run_dir / "events.jsonl").emit("run_started", run_id="run", goal="g")
    receipt = build_run_receipt(run_dir)
    assert receipt["openspec"]["conformance_result"] is None
    assert receipt["outcome"] == "BLOCKED"


def test_openspec_progress_maps_task_item(tmp_path: Path) -> None:
    from verdict.orchestration.run import _update_openspec_progress

    change = _fixture(_make_repo(tmp_path))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    graph = WorkGraph(
        "g",
        (
            WorkNode(
                "1.1",
                "Verify OpenSpec 1.13.2 is latest version with `npm view @fission-ai/openspec version`",
                owned_files=("a.txt",),
                verification_command=("true",),
            ),
        ),
    )
    block: dict[str, Any] = {"change_dir": str(change), "proof_state": "PENDING"}
    path = run_dir / "graph.json"
    path.write_text(json.dumps({**graph.to_dict(), "openspec": block}))
    log = EventLog(run_dir / "events.jsonl")
    log.emit("node_state", "1.1", state="VALIDATED")
    _update_openspec_progress(path, block, graph, log)
    assert block["mapping_source"] == "tasks.md_exact_or_node_id"
    assert block["task_mapping"] == {"1.1": "1.1"}
    assert block["completed_tasks"] == ["1.1"]
    assert block["current_task"] is None
