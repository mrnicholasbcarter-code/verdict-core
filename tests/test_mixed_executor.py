"""Offline test for MixedExecutor: two nodes, two harnesses, receipt and TUI checks.

node-1 is routed through a ScriptedExecutor that self-labels with
harness='scripted:prime-headless'; node-2 uses 'scripted:direct-gateway'.
These labels are distinct from live-harness names so no receipt or cockpit
frame can claim a live harness that did not actually run.

Design decisions (per reviewer):
- MixedExecutor preserves the delegate's harness as-is (no override).
- Attempt worktree detection requires parent directory name == 'worktrees'.
- When --graph is provided, unknown --executor-map keys are a hard error.
- When no graph exists (frontier planning), a stderr warning is printed.
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

# Distinct labels that prove provenance without claiming a live harness.
_KIND_A = "scripted:prime-headless"
_KIND_B = "scripted:direct-gateway"

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


def _scripted(harness_label: str) -> ScriptedExecutor:
    """A ScriptedExecutor that writes the expected file and self-stamps harness.

    The label is intentionally prefixed 'scripted:' to distinguish it from live
    harness names ('prime-headless', 'direct-gateway').
    """
    from dataclasses import replace as _replace

    def _script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
        node_id = cwd.name.rsplit("-a", 1)[0]
        (cwd / f"{node_id}.txt").write_text(f"{node_id} done\n")
        return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")

    # ScriptedExecutor.run() stamps executor_kind='scripted'; wrap it to also
    # set harness so the receipt and cockpit carry both provenance fields.
    inner = ScriptedExecutor(_script)

    class _Wrapper:
        async def run(
            self, prompt: str, *, route_id: str, cwd: Path, timeout_seconds: float
        ) -> WorkerTerminal:
            terminal = await inner.run(
                prompt, route_id=route_id, cwd=cwd, timeout_seconds=timeout_seconds
            )
            return _replace(terminal, harness=harness_label)

    return _Wrapper()  # type: ignore[return-value]


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
        node_map={"node-1": _scripted(_KIND_A), "node-2": _scripted(_KIND_B)},
        default=_scripted(_KIND_A),
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
# Core contract: harness on terminal events and receipt
# ---------------------------------------------------------------------------


def test_mixed_executor_events_show_harness_field(tmp_path: Path) -> None:
    """Terminal events carry the delegate-attested 'harness' field unchanged."""
    run_dir = _run_mixed(tmp_path)
    events = _load_events(run_dir)
    terminal_harnesses = {
        e["node_id"]: e["data"].get("harness", "")
        for e in events
        if e["type"] == "terminal" and e["data"].get("ok")
    }
    assert terminal_harnesses.get("node-1") == _KIND_A, terminal_harnesses
    assert terminal_harnesses.get("node-2") == _KIND_B, terminal_harnesses


def test_mixed_executor_executor_kind_stays_scripted(tmp_path: Path) -> None:
    """executor_kind stays 'scripted' for scripted delegates.

    MixedExecutor must not overwrite it so fault_injected detection and
    replay provenance in tui.py work correctly.
    """
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
    assert _KIND_A in text, f"{_KIND_A!r} not found in cockpit render"
    assert _KIND_B in text, f"{_KIND_B!r} not found in cockpit render"


# ---------------------------------------------------------------------------
# fault-inject INSIDE mix: executor_kind='fault-injected', harness is empty
# ---------------------------------------------------------------------------


def test_fault_inject_inside_mixed_keeps_fault_injected_kind(tmp_path: Path) -> None:
    """FaultInjectingExecutor wrapping MixedExecutor:
    - injected attempt: executor_kind='fault-injected', harness='' (no live harness).
    - real attempt after fault drains: executor_kind='scripted', harness=_KIND_A.
    """
    inner = _scripted(_KIND_A)
    mixed = MixedExecutor(node_map={"node-1": inner}, default=inner)
    fault_ex = FaultInjectingExecutor(mixed, {"@node-1": ["server"]})

    worktrees = tmp_path / "worktrees"
    worktrees.mkdir()
    cwd = worktrees / "node-1-a1"
    cwd.mkdir()

    # First call: fault fires
    t1 = asyncio.run(fault_ex.run("p", route_id="alpha/claude-a", cwd=cwd, timeout_seconds=5))
    assert t1.executor_kind == "fault-injected", t1.executor_kind
    assert t1.ok is False
    assert t1.harness == "", f"fault-injected terminal must have empty harness, got {t1.harness!r}"

    # Second call: fault queue drained, real executor runs
    t2 = asyncio.run(fault_ex.run("p", route_id="alpha/claude-a", cwd=cwd, timeout_seconds=5))
    assert t2.executor_kind == "scripted", t2.executor_kind
    assert t2.harness == _KIND_A, t2.harness


# ---------------------------------------------------------------------------
# Fix 3: stale harness cleared on subsequent empty-harness terminal
# ---------------------------------------------------------------------------


def test_tui_harness_cleared_by_later_empty_harness_terminal() -> None:
    """A live attempt followed by a fault-injected attempt must show no harness.

    The fault-injected terminal carries harness='' (no real harness ran).
    After applying it, node.harness must be '' — not the stale live label.
    """
    from verdict.orchestration.tui import RunView

    events = [
        {
            "seq": 1,
            "at": "2026-01-01T00:00:01+00:00",
            "type": "run_started",
            "data": {"goal": "test"},
        },
        {
            "seq": 2,
            "at": "2026-01-01T00:00:02+00:00",
            "type": "dispatch",
            "node_id": "node-1",
            "data": {"attempt": 1, "route_id": "cc/model"},
        },
        {
            "seq": 3,
            "at": "2026-01-01T00:00:03+00:00",
            "type": "terminal",
            "node_id": "node-1",
            "data": {
                "ok": False,
                "route_id": "cc/model",
                "reported_model": "cc/model",
                "executor_kind": "live",
                "harness": "prime-headless",  # live attempt: harness set
                "error": "rate_limit",
                "attempt": 1,
                "duration_seconds": 1.0,
            },
        },
        {
            "seq": 4,
            "at": "2026-01-01T00:00:04+00:00",
            "type": "dispatch",
            "node_id": "node-1",
            "data": {"attempt": 2, "route_id": "cc/model"},
        },
        {
            "seq": 5,
            "at": "2026-01-01T00:00:05+00:00",
            "type": "terminal",
            "node_id": "node-1",
            "data": {
                "ok": False,
                "route_id": "cc/model",
                "reported_model": "cc/model",
                "executor_kind": "fault-injected",
                "harness": "",  # fault-injected: no live harness
                "error": "timeout",
                "attempt": 2,
                "duration_seconds": 0.0,
            },
        },
    ]
    view = RunView.from_events(events)
    node = view.node("node-1")
    assert node.harness == "", (
        f"after fault-injected attempt, harness must be '' (not stale 'prime-headless'), "
        f"got {node.harness!r}"
    )


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
# Fix 3: unknown node ids in --executor-map rejected against known graph
# ---------------------------------------------------------------------------


def test_executor_map_unknown_node_id_exits_2_via_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When --graph is provided, unknown node ids in --executor-map cause exit 2
    via the real _orchestrate() path with the error text on stderr."""
    import argparse
    import json

    from verdict.orchestration.cli import _orchestrate
    from verdict.orchestration.contracts import NodeKind, WorkGraph, WorkNode

    graph = WorkGraph(
        goal="test",
        nodes=(
            WorkNode(
                node_id="real-node",
                objective="x",
                kind=NodeKind.IMPLEMENT,
                depends_on=(),
                owned_files=("x.txt",),
                verification_command=("true",),
            ),
        ),
    )
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps(graph.to_dict()))

    # Patch the API key check so _orchestrate does not bail at the key guard.
    monkeypatch.setenv("VERDICT_OMNIROUTE_API_KEY", "test-key-for-validation")

    args = argparse.Namespace(
        repo=str(tmp_path),
        runs_dir=str(tmp_path / "runs"),
        goal="test",
        graph=str(graph_file),
        resume=None,
        executor="prime",
        executor_map="typo-node=prime",
        gateway="http://127.0.0.1:20128",
        scope="",
        prefer="",
        state_file=None,
        inject=[],
        max_parallel=1,
        attempt_timeout=900.0,
        run_deadline=3600.0,
        no_review=True,
        plain=True,
        json=False,
        node=None,
        panel=None,
    )

    code = _orchestrate(args)
    assert code == 2, f"expected exit 2, got {code}"
    captured = capsys.readouterr()
    assert "typo-node" in captured.err, f"error text not in stderr: {captured.err!r}"
    assert "real-node" in captured.err, f"known ids not in stderr: {captured.err!r}"


def test_executor_map_unmatched_warning_via_supplied_graph(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unmatched map keys surface as event + stderr when graph= is supplied directly.
    This tests the supplied-graph code path where plan_ready fires immediately."""
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
        node_map={"does-not-exist": _scripted(_KIND_A), "node-1": _scripted(_KIND_A)},
        default=_scripted(_KIND_A),
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
                graph=MIXED_GRAPH,  # graph IS provided -- supplied-graph path
                run_id="mixed-supplied-graph-warn",
                policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
                mode="offline-scenario",
            )
        )
    finally:
        if old_key is None:
            os.environ.pop(_OFFLINE_OCR_ENV, None)
        else:
            os.environ[_OFFLINE_OCR_ENV] = old_key

    assert result.outcome == RunOutcome.COMPLETE.value, result.reason
    events = _load_events(result.run_dir)
    unmatched_events = [e for e in events if e["type"] == "executor_map_unmatched"]
    assert unmatched_events, "no executor_map_unmatched event found in events.jsonl"
    keys = unmatched_events[0]["data"].get("unmatched_keys", [])
    assert "does-not-exist" in keys, keys
    assert "node-1" not in keys, keys
    captured = capsys.readouterr()
    assert "does-not-exist" in captured.err, captured.err


def test_executor_map_unmatched_warning_via_frontier_planning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unmatched map keys surface as event + stderr when graph= is OMITTED (frontier path).

    Frontier planning calls plan_with_failover() which needs a real model.  We
    monkeypatch verdict.orchestration.run.plan_with_failover to return MIXED_GRAPH
    directly so the test is offline and fast, but the real code path
    (graph is None  ->  plan_with_failover  ->  plan_ready  ->  unmatched check)
    is exercised end-to-end.
    """
    import verdict.orchestration.run as _run_mod

    async def _scripted_planner(*_args: object, **_kwargs: object) -> WorkGraph:
        return MIXED_GRAPH

    monkeypatch.setattr(_run_mod, "plan_with_failover", _scripted_planner)

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
        node_map={"does-not-exist": _scripted(_KIND_A), "node-1": _scripted(_KIND_A)},
        default=_scripted(_KIND_A),
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
                graph=None,  # omitted -- exercises the frontier planning path
                run_id="mixed-frontier-planning-warn",
                policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
                mode="offline-scenario",
            )
        )
    finally:
        if old_key is None:
            os.environ.pop(_OFFLINE_OCR_ENV, None)
        else:
            os.environ[_OFFLINE_OCR_ENV] = old_key

    assert result.outcome == RunOutcome.COMPLETE.value, result.reason

    # 1. executor_map_unmatched event must be in events.jsonl
    events = _load_events(result.run_dir)
    unmatched_events = [e for e in events if e["type"] == "executor_map_unmatched"]
    assert unmatched_events, "no executor_map_unmatched event emitted on frontier planning path"
    keys = unmatched_events[0]["data"].get("unmatched_keys", [])
    assert "does-not-exist" in keys, keys
    assert "node-1" not in keys, keys

    # 2. stderr warning must mention the typo key
    captured = capsys.readouterr()
    assert "does-not-exist" in captured.err, f"warning not found in stderr: {captured.err!r}"


def test_executor_map_unmatched_fires_through_fault_injecting_wrapper(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """FaultInjectingExecutor(MixedExecutor(...unmatched...)) still emits the event.

    When --inject is combined with --executor-map, cli.py wraps MixedExecutor
    inside FaultInjectingExecutor.  run_golden_path must unwrap the outer layer
    to reach MixedExecutor and check its mapped_node_ids.
    """
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
    mix = MixedExecutor(
        node_map={"does-not-exist": _scripted(_KIND_A), "node-1": _scripted(_KIND_A)},
        default=_scripted(_KIND_A),
    )
    # Wrap in FaultInjectingExecutor (empty faults) as --inject would do
    executor = FaultInjectingExecutor(mix, {})

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
                run_id="mixed-wrapped-fault",
                policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
                mode="offline-scenario",
            )
        )
    finally:
        if old_key is None:
            os.environ.pop(_OFFLINE_OCR_ENV, None)
        else:
            os.environ[_OFFLINE_OCR_ENV] = old_key

    assert result.outcome == RunOutcome.COMPLETE.value, result.reason

    events = _load_events(result.run_dir)
    unmatched_events = [e for e in events if e["type"] == "executor_map_unmatched"]
    assert unmatched_events, (
        "executor_map_unmatched event not emitted when MixedExecutor is "
        "wrapped inside FaultInjectingExecutor"
    )
    keys = unmatched_events[0]["data"].get("unmatched_keys", [])
    assert "does-not-exist" in keys, keys

    captured = capsys.readouterr()
    assert "does-not-exist" in captured.err, captured.err


# ---------------------------------------------------------------------------
# cwd-name parsing: worktrees parent required
# ---------------------------------------------------------------------------


def test_mixed_executor_node_id_from_cwd_requires_worktrees_parent() -> None:
    """MixedExecutor._node_id_from_cwd requires parent dir named 'worktrees'.

    A repo root named 'node-1-a1' (not under worktrees/) must NOT be treated as
    an attempt worktree and must map to the default executor.
    """
    inner_a = _scripted(_KIND_A)
    inner_b = _scripted(_KIND_B)
    mixed = MixedExecutor(node_map={"node-1": inner_a}, default=inner_b)

    # Under worktrees/ -> correctly identified as attempt worktree
    worktrees = Path("/runs/worktrees")
    assert mixed._pick(worktrees / "node-1-a1") is inner_a

    # NOT under worktrees/ -> maps to default, even if basename looks like attempt
    assert mixed._pick(Path("/some/node-1-a1")) is inner_b  # wrong parent
    assert mixed._pick(Path("/node-1-a1")) is inner_b  # no parent at all
    assert mixed._pick(Path("node-1-a1")) is inner_b  # bare name, parent is "."

    # Planning call: repo root under worktrees parent that isn't worktrees-named
    assert mixed._pick(Path("/runs/repo")) is inner_b


def test_mixed_executor_repo_named_like_worktree_maps_to_default() -> None:
    """A repository root named 'node-1-a1' (not under worktrees/) maps to default."""
    inner_a = _scripted(_KIND_A)
    inner_b = _scripted(_KIND_B)
    mixed = MixedExecutor(node_map={"node-1": inner_a}, default=inner_b)

    # This is the exact scenario from the reviewer: a repo root named node-1-a1
    repo_root = Path("/home/user/node-1-a1")
    assert mixed._pick(repo_root) is inner_b, (
        "a repo root named like a worktree must route to default, not to mapped node"
    )


def test_mixed_executor_node_id_containing_hyphen_a() -> None:
    """Node id 'data-a1b' at attempt 2 (worktree 'data-a1b-a2') maps correctly."""
    inner_a = _scripted(_KIND_A)
    inner_b = _scripted(_KIND_B)
    mixed = MixedExecutor(node_map={"data-a1b": inner_a}, default=inner_b)

    worktrees = Path("/runs/worktrees")
    assert mixed._pick(worktrees / "data-a1b-a2") is inner_a
    assert mixed._pick(worktrees / "data-a1b-a1") is inner_a
    assert mixed._pick(worktrees / "data-a2c-a1") is inner_b


# ---------------------------------------------------------------------------
# Conditional harness column: only shown when >=1 node has harness data
# ---------------------------------------------------------------------------


def test_workers_table_no_harness_column_when_no_harness_data() -> None:
    """Runs with no harness data render the WORKERS table without a 'harness' column."""
    events = [
        {
            "seq": 1,
            "at": "2026-01-01T00:00:01+00:00",
            "type": "run_started",
            "data": {"goal": "test"},
        },
        {
            "seq": 2,
            "at": "2026-01-01T00:00:02+00:00",
            "type": "node_state",
            "node_id": "node-1",
            "data": {"state": "VALIDATED"},
        },
        {
            "seq": 3,
            "at": "2026-01-01T00:00:03+00:00",
            "type": "terminal",
            "node_id": "node-1",
            "data": {
                "ok": True,
                "route_id": "cc/claude-a",
                "reported_model": "cc/claude-a",
                "executor_kind": "live",
                # no 'harness' key -- pre-BOD-284 event
                "duration_seconds": 1.0,
                "attempt": 1,
            },
        },
    ]
    text = render_text(events, plain=True)
    assert "harness" not in text, "harness column appeared in a run with no harness data"
    assert "node" in text


def test_workers_table_shows_harness_column_in_mixed_run(tmp_path: Path) -> None:
    """Mixed runs (with harness field on terminals) render the 'harness' column."""
    run_dir = _run_mixed(tmp_path)
    events = read_events(run_dir / "events.jsonl")
    text = render_text(events, plain=True)
    assert "harness" in text, "harness column missing in a mixed run with harness data"
    assert _KIND_A in text
    assert _KIND_B in text


# ---------------------------------------------------------------------------
# run_started executor label
# ---------------------------------------------------------------------------


def test_mixed_executor_run_label_in_run_started(tmp_path: Path) -> None:
    """run_started event records executor as 'mixed' when MixedExecutor is used."""
    run_dir = _run_mixed(tmp_path)
    events = _load_events(run_dir)
    run_started = next(e for e in events if e["type"] == "run_started")
    assert run_started["data"]["executor"] == "mixed", run_started["data"]
