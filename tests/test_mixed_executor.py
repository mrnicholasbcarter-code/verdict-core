"""Offline test for MixedExecutor: two nodes, two harnesses, receipt and TUI checks.

node-1 is routed through a ScriptedExecutor named 'prime-headless',
node-2 through a ScriptedExecutor named 'direct-gateway'.

Assertions:
- terminal events carry 'harness' (not executor_kind) from MixedExecutor;
- receipt attempt rows capture 'harness' for each node;
- run-receipt verifies with integrity OK;
- cockpit render text shows both harness names;
- --executor-map with an unknown backend produces a clear CLI error;
- fault-injected attempt retains executor_kind='fault-injected' (not overwritten);
- scripted delegate keeps executor_kind='scripted';
- cwd-name parsing handles node ids that contain '-a'.
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
from verdict.orchestration.executors import FaultInjectingExecutor, MixedExecutor, ScriptedExecutor
from verdict.orchestration.receipt import build_run_receipt, verify_run_receipt
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OpenCodeReviewer
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy
from verdict.orchestration.tui import read_events, render_text

# Two fake harness names that mirror live names.
_KIND_A = "prime-headless"
_KIND_B = "direct-gateway"

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


def _run_mixed(tmp_path: Path) -> Path:
    """Run through run_golden_path with a MixedExecutor; return run_dir."""
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


def _load_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Core contract: harness field on terminal events and receipt
# ---------------------------------------------------------------------------


def test_mixed_executor_events_show_harness_field(tmp_path: Path) -> None:
    """Terminal events carry the 'harness' field from MixedExecutor, not executor_kind."""
    run_dir = _run_mixed(tmp_path)
    events = _load_events(run_dir)
    terminal_harnesses = {
        e["node_id"]: e["data"].get("harness", "")
        for e in events
        if e["type"] == "terminal" and e["data"].get("ok")
    }
    assert terminal_harnesses.get("node-1") == _KIND_A, terminal_harnesses
    assert terminal_harnesses.get("node-2") == _KIND_B, terminal_harnesses


def test_mixed_executor_executor_kind_is_scripted(tmp_path: Path) -> None:
    """executor_kind stays 'scripted' for scripted delegates — MixedExecutor must not
    overwrite it, so fault_injected detection and replay provenance are unaffected."""
    run_dir = _run_mixed(tmp_path)
    events = _load_events(run_dir)
    terminal_kinds = {
        e["node_id"]: e["data"].get("executor_kind", "")
        for e in events
        if e["type"] == "terminal" and e["data"].get("ok")
    }
    assert terminal_kinds.get("node-1") == "scripted", terminal_kinds
    assert terminal_kinds.get("node-2") == "scripted", terminal_kinds


def test_mixed_executor_receipt_captures_harness(tmp_path: Path) -> None:
    """Each attempt row in the receipt includes 'harness'."""
    run_dir = _run_mixed(tmp_path)
    receipt = build_run_receipt(run_dir)

    node_harnesses: dict[str, str] = {}
    for node in receipt.get("nodes", []):
        for attempt in node.get("attempts", []):
            nid = node["node_id"]
            h = attempt.get("harness", "")
            if attempt.get("outcome") == "success" and h:
                node_harnesses[nid] = h

    assert node_harnesses.get("node-1") == _KIND_A, node_harnesses
    assert node_harnesses.get("node-2") == _KIND_B, node_harnesses


def test_mixed_executor_receipt_integrity(tmp_path: Path) -> None:
    """verdict run-receipt verifies with integrity OK after a mixed run."""
    run_dir = _run_mixed(tmp_path)
    problems = verify_run_receipt(run_dir)
    assert problems == [], f"receipt integrity problems: {problems}"


def test_mixed_executor_cockpit_shows_both_harnesses(tmp_path: Path) -> None:
    """Rendered cockpit text contains both harness labels."""
    run_dir = _run_mixed(tmp_path)
    events = read_events(run_dir / "events.jsonl")
    text = render_text(events, plain=True)
    assert _KIND_A in text, f"{_KIND_A!r} not found in cockpit render:\n{text[:2000]}"
    assert _KIND_B in text, f"{_KIND_B!r} not found in cockpit render:\n{text[:2000]}"


# ---------------------------------------------------------------------------
# fault-inject interaction: fault-injected keeps executor_kind='fault-injected'
# ---------------------------------------------------------------------------


def test_fault_inject_inside_mixed_keeps_fault_injected_kind(tmp_path: Path) -> None:
    """FaultInjectingExecutor wrapping MixedExecutor: injected attempt has
    executor_kind='fault-injected' so fault_injected detection is correct.
    The 'harness' field is set by MixedExecutor before FaultInjector fires,
    but FaultInjector replaces the whole terminal, so harness may be empty
    on the injected attempt — that is acceptable since it did not actually
    run on any real harness."""
    inner = _scripted()
    mixed = MixedExecutor(
        node_map={"node-1": inner},
        default=inner,
        node_kind_map={"node-1": _KIND_A},
        default_kind=_KIND_A,
    )
    # Wrap with FaultInjectingExecutor: inject a server error for node-1-a1
    fault_ex = FaultInjectingExecutor(mixed, {"@node-1": ["server"]})

    cwd_node1 = tmp_path / "node-1-a1"
    cwd_node1.mkdir()

    import asyncio as _asyncio

    t = _asyncio.run(
        fault_ex.run("prompt", route_id="alpha/claude-a", cwd=cwd_node1, timeout_seconds=5)
    )
    # The fault was injected — executor_kind must be 'fault-injected'
    assert t.executor_kind == "fault-injected", t.executor_kind
    assert t.ok is False
    # harness is empty on a fault-injected terminal (no real harness ran)
    assert t.harness == "", t.harness


def test_mixed_inside_fault_inject_harness_on_real_attempt(tmp_path: Path) -> None:
    """When FaultInjectingExecutor wraps MixedExecutor and no fault fires,
    the delegate runs and harness is set correctly."""
    inner = _scripted()
    mixed = MixedExecutor(
        node_map={"node-1": inner},
        default=inner,
        node_kind_map={"node-1": _KIND_A},
        default_kind=_KIND_A,
    )
    fault_ex = FaultInjectingExecutor(mixed, {"@node-1": ["server"]})

    cwd_node1 = tmp_path / "node-1-a1"
    cwd_node1.mkdir()

    import asyncio as _asyncio

    # First call: fault fires
    t1 = _asyncio.run(
        fault_ex.run("prompt", route_id="alpha/claude-a", cwd=cwd_node1, timeout_seconds=5)
    )
    assert t1.executor_kind == "fault-injected"
    # Second call: fault queue drained, real executor runs
    t2 = _asyncio.run(
        fault_ex.run("prompt", route_id="alpha/claude-a", cwd=cwd_node1, timeout_seconds=5)
    )
    assert t2.executor_kind == "scripted"
    assert t2.harness == _KIND_A, t2.harness


# ---------------------------------------------------------------------------
# CLI error for unknown backend
# ---------------------------------------------------------------------------


def test_mixed_executor_unknown_backend_cli_error() -> None:
    """An unknown executor backend in --executor-map produces a clear CLI error."""
    from verdict.orchestration.cli import _parse_executor_map

    with pytest.raises(SystemExit) as exc_info:
        _parse_executor_map("node-1=bogus-backend")
    assert exc_info.value.code != 0
    assert "bogus-backend" in str(exc_info.value.code)


# ---------------------------------------------------------------------------
# cwd-name parsing
# ---------------------------------------------------------------------------


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
    # Edge: node id with digits
    mixed2 = MixedExecutor(node_map={"setup-db2": inner_a}, default=inner_b)
    executor, _ = mixed2._pick(Path("/runs/setup-db2-a1"))
    assert executor is inner_a


def test_mixed_executor_node_id_containing_hyphen_a() -> None:
    """Node id 'data-a1b' at attempt 2 (worktree 'data-a1b-a2') maps correctly.

    rsplit('-a', 1) splits at the LAST '-a', so the attempt suffix is stripped
    and the node id is preserved even when it contains '-a' internally.
    """
    inner_a = _scripted()
    inner_b = _scripted()
    mixed = MixedExecutor(node_map={"data-a1b": inner_a}, default=inner_b)

    executor, _ = mixed._pick(Path("/runs/data-a1b-a2"))
    assert executor is inner_a, "node id 'data-a1b' at attempt 2 must resolve to inner_a"

    # 'data-a1b' at attempt 1
    executor, _ = mixed._pick(Path("/runs/data-a1b-a1"))
    assert executor is inner_a

    # A different node that happens to start with 'data-a' but is a different id
    executor, _ = mixed._pick(Path("/runs/data-a2c-a1"))
    assert executor is inner_b  # not in map, falls back to default


# ---------------------------------------------------------------------------
# run_started executor label
# ---------------------------------------------------------------------------


def test_mixed_executor_run_label_in_run_started(tmp_path: Path) -> None:
    """run_started event records executor as 'mixed' when MixedExecutor is used."""
    run_dir = _run_mixed(tmp_path)
    events = _load_events(run_dir)
    run_started = next(e for e in events if e["type"] == "run_started")
    assert run_started["data"]["executor"] == "mixed", run_started["data"]
