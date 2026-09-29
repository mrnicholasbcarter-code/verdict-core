"""Shared CLI/cockpit control parity and recovery safety."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from verdict.actions import run_action
from verdict.commands.dispatch import dispatch
from verdict.commands.parsers_models import register
from verdict.orchestration import cockpit_controls as cockpit
from verdict.orchestration import controls, recovery
from verdict.orchestration.receipt import EventLog, write_run_receipt
from verdict.orchestration.tui import RunView


def _run_dir(
    tmp_path: Path,
    *,
    node_state: str = "BLOCKED",
    attempts: int = 1,
    limit: int = 4,
    failure_action: str = "REROUTE",
) -> Path:
    root = tmp_path / "run-123"
    root.mkdir()
    fixture = Path("docs/proof/demo-run")
    shutil.copyfile(fixture / "graph.json", root / "graph.json")
    events = EventLog(root / "events.jsonl")
    events.emit(
        "run_started",
        run_id="run-123",
        goal="control parity",
        retry_budget={"max_attempts_per_node": limit},
    )
    events.emit("node_state", "parser", state=node_state, attempt=attempts)
    if node_state == "BLOCKED":
        events.emit(
            "failure", "parser", category="timeout", action=failure_action, attempt=attempts
        )
    return root


def _state(run_dir: Path) -> tuple[RunView, cockpit.ControlCockpitState]:
    view = RunView()
    view.node("parser")
    state = cockpit.ControlCockpitState(run_dir=run_dir)
    state.sync_order(["parser"])
    return view, state


@pytest.mark.parametrize(
    "key,kind,initial",
    [
        ("x", "cancel_run", "BLOCKED"),
        ("X", "cancel_node", "RUNNING"),
        ("t", "retry_node", "BLOCKED"),
    ],
)
def test_every_control_through_cockpit_hits_domain_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, kind: str, initial: str
) -> None:
    run_dir = _run_dir(tmp_path, node_state=initial)
    view, state = _state(run_dir)
    calls: list[tuple[str, str | None]] = []
    real = controls.RunControl.submit

    def observed(
        self: controls.RunControl,
        kind_arg: str,
        *,
        node_id: str | None = None,
        requested_by: str = "operator",
    ) -> controls.ControlRequest:
        calls.append((kind_arg, node_id))
        return real(self, kind_arg, node_id=node_id, requested_by=requested_by)

    monkeypatch.setattr(controls.RunControl, "submit", observed)
    assert cockpit.dispatch_key(key, state, view)
    assert not (run_dir / "controls.jsonl").exists()
    assert cockpit.dispatch_key("x" if key == "x" else "ENTER", state, view)
    assert calls == [(kind, None if kind == "cancel_run" else "parser")]
    assert state.control_result is not None and state.control_result.ok
    requests = (run_dir / "controls.jsonl").read_text().splitlines()
    assert len(requests) == 1
    assert json.loads(requests[0])["kind"] == kind


@pytest.mark.parametrize(
    "key,kind,initial",
    [
        ("x", "cancel_run", "BLOCKED"),
        ("X", "cancel_node", "BLOCKED"),
        ("t", "retry_node", "RUNNING"),
    ],
)
def test_cockpit_refusals_from_domain_policy_not_ui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, kind: str, initial: str
) -> None:
    run_dir = _run_dir(tmp_path, node_state=initial)
    if kind == "cancel_run":
        with (run_dir / "events.jsonl").open("a") as file:
            file.write(
                json.dumps(
                    {
                        "seq": 4,
                        "at": "2026-09-29T00:00:00Z",
                        "type": "run_finished",
                        "node_id": "",
                        "data": {"outcome": "COMPLETE"},
                    }
                )
                + "\n"
            )
    view, state = _state(run_dir)
    calls: list[str] = []
    real = controls.RunControl.submit

    def observed(
        self: controls.RunControl,
        kind_arg: str,
        *,
        node_id: str | None = None,
        requested_by: str = "operator",
    ) -> controls.ControlRequest:
        calls.append(kind_arg)
        return real(self, kind_arg, node_id=node_id, requested_by=requested_by)

    monkeypatch.setattr(controls.RunControl, "submit", observed)
    cockpit.dispatch_key(key, state, view)
    cockpit.dispatch_key("x" if key == "x" else "ENTER", state, view)
    assert calls == [kind]
    assert state.control_result is not None and not state.control_result.ok
    assert state.control_result.data["reason"]
    assert not (run_dir / "controls.jsonl").exists()


def test_retry_exhausted_attempts_refused_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = _run_dir(tmp_path, attempts=4, limit=4)
    view, state = _state(run_dir)
    decisions: list[tuple[str, int | None]] = []
    real = recovery.RecoveryBudget.decide

    def observed(
        self: recovery.RecoveryBudget,
        node_id: str,
        history: list[Any],
        *,
        attempts: int | None = None,
    ) -> tuple[str, str]:
        decisions.append((node_id, attempts))
        return real(self, node_id, history, attempts=attempts)

    monkeypatch.setattr(recovery.RecoveryBudget, "decide", observed)
    cockpit.dispatch_key("t", state, view)
    cockpit.dispatch_key("ENTER", state, view)
    assert decisions == [("parser", 4)]
    assert state.control_result is not None and not state.control_result.ok
    assert "exhausted 4 attempts" in state.control_result.data["reason"]
    assert not (run_dir / "controls.jsonl").exists()


def test_retry_rejects_blocked_nonrecoverable_failure(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, failure_action="BLOCK")
    result = run_action("run.retry-node", {"run_dir": run_dir, "node_id": "parser"})
    assert not result.ok
    assert "BLOCK" in result.data["reason"]
    assert not (run_dir / "controls.jsonl").exists()


def test_cli_cancel_and_retry_call_same_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run_dir(tmp_path)
    real = controls.RunControl.submit
    seen: list[str] = []

    def observed(
        self: controls.RunControl,
        kind_arg: str,
        *,
        node_id: str | None = None,
        requested_by: str = "operator",
    ) -> controls.ControlRequest:
        seen.append(kind_arg)
        return real(self, kind_arg, node_id=node_id, requested_by=requested_by)

    monkeypatch.setattr(controls.RunControl, "submit", observed)
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    for cmd in (["run", "cancel", "run-123"], ["run", "retry", "run-123", "parser"]):
        args = parser.parse_args([*cmd, "--runs-dir", str(tmp_path)])
        with pytest.raises(SystemExit) as exit_info:
            dispatch(parser, args)
        assert exit_info.value.code == 0
    assert seen == ["cancel_run", "retry_node"]
    assert capsys.readouterr().out.count("queued") == 2


def test_cli_refusal_reports_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _run_dir(tmp_path, attempts=4, limit=4)
    parser = argparse.ArgumentParser()
    register(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["run", "retry", "run-123", "parser", "--runs-dir", str(tmp_path)])
    with pytest.raises(SystemExit) as exit_info:
        dispatch(parser, args)
    assert exit_info.value.code == 2
    assert "exhausted 4 attempts" in capsys.readouterr().err


def test_cancelled_receipt_verifies_control_event(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, node_state="RUNNING")
    events = EventLog(run_dir / "events.jsonl")
    events.emit("control", kind="cancel_run", accepted=True, reason="run cancelled by operator")
    events.emit("node_state", "parser", state="BLOCKED", reason="cancelled by operator")
    events.emit("run_finished", outcome="CANCELLED", reason="run cancelled by operator")
    receipt_path = write_run_receipt(run_dir)
    receipt = json.loads(receipt_path.read_text())
    assert receipt["outcome"] == "CANCELLED"
    assert receipt["claimed_outcome"] == "CANCELLED"
    assert receipt["events_digest"]


@pytest.fixture()
def runtime_repo(tmp_path: Path) -> Path:
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(command, cwd=root, check=True)
    (root / "README.md").write_text("test\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, check=True)
    return root


@pytest.mark.asyncio
async def test_runtime_cancel_is_prompt_and_writes_cancelled_receipt(
    runtime_repo: Path, tmp_path: Path
) -> None:
    """A worker that never finishes must still be cancelled before the receipt."""
    import asyncio

    from test_run_controls import Classifier, Selector, StubReviewer, _graph, _node

    from verdict.orchestration.contracts import RunOutcome, WorkerTerminal
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    graph = _graph(_node("a"))
    root = tmp_path / "run"
    root.mkdir()
    (root / "graph.json").write_text(json.dumps({**graph.to_dict(), "run_id": "run"}))
    events = EventLog(root / "events.jsonl")
    events.emit("run_started", run_id="run", retry_budget={"max_attempts_per_node": 4})

    class NeverFinish:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    worker = NeverFinish()
    rt = DagRuntime(
        repo=runtime_repo,
        run_dir=root,
        graph=graph,
        selector=Selector(["a/model-1"]),
        executor=worker,
        classifier=Classifier(),
        events=events,
        prompt_for=lambda node, worktree: f"{node.node_id}: work",
        reviewer=StubReviewer(),
        policy=RuntimePolicy(require_review=False, attempt_timeout_seconds=5),
    )
    task = asyncio.create_task(rt.run())
    try:
        await asyncio.wait_for(worker.started.wait(), timeout=4)
        request = controls.RunControl(root).submit("cancel_run")
        result = await asyncio.wait_for(task, timeout=4)
        assert result.outcome is RunOutcome.CANCELLED
        assert worker.cancelled.is_set()
        rows = events.read()
        event = next(e for e in rows if e.type == "control" and e.data.get("id") == request.id)
        assert event.data["accepted"] is True
        assert rows[-1].type == "run_finished"
        assert rows[-1].data["outcome"] == "CANCELLED"
        receipt = json.loads(write_run_receipt(root).read_text())
        assert receipt["outcome"] == "CANCELLED"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_runtime_retry_over_budget_refused_with_observed_reason(
    runtime_repo: Path, tmp_path: Path
) -> None:
    from test_run_controls import (
        Classifier,
        Events,
        ScriptedExecutor,
        Selector,
        StubReviewer,
        _graph,
        _node,
    )

    from verdict.orchestration.contracts import FailureClassification, NodeState
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    graph = _graph(_node("a"))
    root = tmp_path / "run"
    root.mkdir()
    rt = DagRuntime(
        repo=runtime_repo,
        run_dir=root,
        graph=graph,
        selector=Selector(["a/model-1"]),
        executor=ScriptedExecutor({}),
        classifier=Classifier(),
        events=Events(),
        prompt_for=lambda node, worktree: f"{node.node_id}: work",
        reviewer=StubReviewer(),
        policy=RuntimePolicy(require_review=False, max_attempts_per_node=2),
    )
    node = rt.nodes["a"]
    node.state = NodeState.BLOCKED
    node.attempt = 2
    node.failures.append(FailureClassification("timeout", "REROUTE", 0, "route", ""))
    req = controls.request_control(root, "retry_node", node_id="a", graph=graph)
    rt._poll_controls({})
    records = rt.events.of("control")
    assert records[-1]["id"] == req.id
    assert records[-1]["accepted"] is False
    assert "exhausted 2 attempts" in records[-1]["reason"]
    assert node.state is NodeState.BLOCKED


@pytest.mark.asyncio
async def test_runtime_retry_reuses_selector_admission_and_exclusions(
    runtime_repo: Path, tmp_path: Path
) -> None:
    from test_run_controls import (
        Classifier,
        Events,
        ScriptedExecutor,
        Selector,
        StubReviewer,
        _graph,
        _node,
    )

    from verdict.orchestration.contracts import FailureClassification, NodeState, TaskRequirements
    from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

    graph = _graph(_node("a"))
    root = tmp_path / "run"
    root.mkdir()
    selector = Selector(["a/old", "b/new"])
    rt = DagRuntime(
        repo=runtime_repo,
        run_dir=root,
        graph=graph,
        selector=selector,
        executor=ScriptedExecutor({}),
        classifier=Classifier(),
        events=Events(),
        prompt_for=lambda node, worktree: f"{node.node_id}: work",
        reviewer=StubReviewer(),
        policy=RuntimePolicy(require_review=False, max_attempts_per_node=4),
    )
    run = rt.nodes["a"]
    run.state = NodeState.BLOCKED
    run.attempt = 1
    run.history.append({"attempt": 1, "route_id": "a/old", "outcome": "timeout"})
    run.failures.append(FailureClassification("timeout", "REROUTE", 600, "route", "timeout"))
    run.excluded_routes.add("a/old")
    selector.cool["a/old"] = rt.now().timestamp() + 600
    req = controls.request_control(root, "retry_node", node_id="a", graph=graph)
    rt._poll_controls({})
    assert rt.events.of("control")[-1]["id"] == req.id
    assert rt.events.of("control")[-1]["accepted"] is True
    assert run.state is NodeState.PLANNED
    assert run.excluded_routes == {"a/old"}
    assert run.failures
    requirements = TaskRequirements.for_node(
        run.node, exclude_routes=frozenset(run.excluded_routes)
    )
    choice, considered = selector.select(requirements, now=rt.now())
    assert choice is not None and choice.route_id == "b/new"
    assert any(v.route_id == "a/old" and v.reason == "excluded" for v in considered)
