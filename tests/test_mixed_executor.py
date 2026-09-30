"""Offline test for MixedExecutor: two nodes, two harnesses, receipt and TUI checks.

node-1 is routed through a ScriptedExecutor named 'prime-headless',
node-2 through a ScriptedExecutor named 'direct-gateway'.

Assertions:
- terminal events for node-1 and node-2 carry different executor_kind values;
- receipt attempt rows capture executor_kind for each node;
- run-receipt verifies with integrity OK;
- cockpit render text mentions both harness names;
- --executor-map with an unknown backend produces a clear CLI error.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from verdict.orchestration.contracts import (
    NodeKind,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.demo_scenario import (
    _OFFLINE_OCR_ENV,
    CONNECTIONS,
    INVENTORY,
    _HealthyProbe,
    _init_repo,
    _PassingOcrRunner,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.executors import MixedExecutor, ScriptedExecutor
from verdict.orchestration.receipt import build_run_receipt, verify_run_receipt
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OpenCodeReviewer
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy
from verdict.orchestration.tui import read_events, render_text

# Two fake executor names that mirror live names.
_KIND_A = "prime-headless"
_KIND_B = "direct-gateway"

ROUTE_A = "alpha/claude-a"
ROUTE_B = "beta/gpt-b"

MIXED_GRAPH = WorkGraph(
    goal="mixed-executor offline test",
    nodes=(
        WorkNode(
            node_id="node-1",
            objective="write node-1",
            kind=NodeKind.IMPLEMENT,
            depends_on=(),
            owned_files=("node-1.txt",),
            verification_command=("sh", "-c", "test -f node-1.txt"),
        ),
        WorkNode(
            node_id="node-2",
            objective="write node-2",
            kind=NodeKind.IMPLEMENT,
            depends_on=(),
            owned_files=("node-2.txt",),
            verification_command=("sh", "-c", "test -f node-2.txt"),
        ),
    ),
    max_parallel=2,
)


def _scripted() -> ScriptedExecutor:
    """A ScriptedExecutor that writes the expected file."""

    def _script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        node_id = cwd.name.rsplit("-a", 1)[0]
        (cwd / f"{node_id}.txt").write_text(f"{node_id} done\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")

    return ScriptedExecutor(_script)


def _run_mixed(tmp_path: Path) -> dict:
    """Run through run_golden_path with a MixedExecutor; return events."""
    runs_root = tmp_path / "runs"
    workspace = tmp_path / "ws"
    runs_root.mkdir()
    workspace.mkdir()
    repo = _init_repo(workspace)

    ladder = EligibilityLadder(
        INVENTORY,
        CONNECTIONS,
        _HealthyProbe(),
        workspace / "health.json",
        allow_unknown_capacity=True,
    )

    executor = MixedExecutor(
        node_map={"node-1": _scripted(), "node-2": _scripted()},
        default=_scripted(),
        node_kind_map={"node-1": _KIND_A, "node-2": _KIND_B},
        default_kind=_KIND_A,
    )
    reviewer = OpenCodeReviewer(
        ladder,
        api_key_env=_OFFLINE_OCR_ENV,
        out_dir=workspace / "review",
        runner=_PassingOcrRunner(),
    )

    old_key = os.environ.get(_OFFLINE_OCR_ENV)
    os.environ[_OFFLINE_OCR_ENV] = "offline-scripted-no-network"
    try:
        result = asyncio.run(
            run_golden_path(
                "mixed-executor offline test",
                repo=repo,
                runs_root=runs_root,
                selector=ladder,
                executor=executor,
                classifier=FailureIntelligence(),
                reviewer=reviewer,
                graph=MIXED_GRAPH,
                run_id="mixed-offline",
                policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
                mode="offline-scenario",
            )
        )
    finally:
        if old_key is None:
            os.environ.pop(_OFFLINE_OCR_ENV, None)
        else:
            os.environ[_OFFLINE_OCR_ENV] = old_key

    assert result.outcome == RunOutcome.COMPLETE.value, (
        f"mixed run did not COMPLETE: {result.outcome} — {result.reason}"
    )
    return result.run_dir


def test_mixed_executor_events_show_both_kinds(tmp_path: Path) -> None:
    """Terminal events carry the executor_kind from whichever delegate ran."""
    run_dir = _run_mixed(tmp_path)
    events_path = run_dir / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    terminal_kinds = {
        e["node_id"]: e["data"].get("executor_kind", "")
        for e in events
        if e["type"] == "terminal" and e["data"].get("ok")
    }
    assert terminal_kinds.get("node-1") == _KIND_A, terminal_kinds
    assert terminal_kinds.get("node-2") == _KIND_B, terminal_kinds


def test_mixed_executor_receipt_captures_executor_kind(tmp_path: Path) -> None:
    """Each attempt row in the receipt includes executor_kind."""
    run_dir = _run_mixed(tmp_path)
    receipt = build_run_receipt(run_dir)

    node_kinds: dict[str, str] = {}
    for node in receipt.get("nodes", []):
        for attempt in node.get("attempts", []):
            nid = node["node_id"]
            ek = attempt.get("executor_kind", "")
            if attempt.get("outcome") == "success" and ek:
                node_kinds[nid] = ek

    assert node_kinds.get("node-1") == _KIND_A, node_kinds
    assert node_kinds.get("node-2") == _KIND_B, node_kinds


def test_mixed_executor_receipt_integrity(tmp_path: Path) -> None:
    """verdict run-receipt verifies with integrity OK after a mixed run."""
    run_dir = _run_mixed(tmp_path)
    problems = verify_run_receipt(run_dir)
    assert problems == [], f"receipt integrity problems: {problems}"


def test_mixed_executor_cockpit_shows_both_harnesses(tmp_path: Path) -> None:
    """Rendered cockpit text contains both executor_kind labels."""
    run_dir = _run_mixed(tmp_path)
    events = read_events(run_dir / "events.jsonl")
    text = render_text(events, plain=True)
    assert _KIND_A in text, f"{_KIND_A!r} not found in cockpit render:\n{text[:2000]}"
    assert _KIND_B in text, f"{_KIND_B!r} not found in cockpit render:\n{text[:2000]}"


def test_mixed_executor_unknown_backend_cli_error() -> None:
    """An unknown executor backend in --executor-map produces a clear CLI error."""
    from verdict.orchestration.cli import _parse_executor_map

    with pytest.raises(SystemExit) as exc_info:
        _parse_executor_map("node-1=bogus-backend")
    assert exc_info.value.code != 0
    # The error was emitted via sys.exit() which receives the message string directly.
    assert "bogus-backend" in str(exc_info.value.code)


def test_mixed_executor_node_id_from_cwd() -> None:
    """MixedExecutor correctly extracts node_id from attempt worktree paths."""
    inner_a = _scripted()
    inner_b = _scripted()
    mixed = MixedExecutor(node_map={"node-1": inner_a}, default=inner_b)

    executor, _ = mixed._pick(Path("/runs/node-1-a1"))
    assert executor is inner_a
    executor, _ = mixed._pick(Path("/runs/node-2-a1"))
    assert executor is inner_b
    # Planning calls (bare repo path, no -aN suffix)
    executor, _ = mixed._pick(Path("/runs/repo"))
    assert executor is inner_b
    # Edge: node id with digits in it
    mixed2 = MixedExecutor(node_map={"setup-db2": inner_a}, default=inner_b)
    executor, _ = mixed2._pick(Path("/runs/setup-db2-a1"))
    assert executor is inner_a


def test_mixed_executor_run_label_in_run_started(tmp_path: Path) -> None:
    """run_started event records executor as 'mixed' when MixedExecutor is used."""
    run_dir = _run_mixed(tmp_path)
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]
    run_started = next(e for e in events if e["type"] == "run_started")
    assert run_started["data"]["executor"] == "mixed", run_started["data"]
