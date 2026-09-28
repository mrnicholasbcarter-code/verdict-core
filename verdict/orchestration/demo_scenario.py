"""Credential-free flagship failover scenario for tests and recorded demos.

This module exercises the production orchestration run loop with a static inventory
at its I/O boundary. Worker execution, one 429-style failure, and the OCR response
are scripted. Selection, eligibility, recovery, event logging, review interpretation,
and receipt generation are the same components used for a normal run.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    NodeKind,
    RunOutcome,
    WorkerTerminal,
    WorkGraph,
    WorkNode,
)
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.executors import FaultInjectingExecutor, ScriptedExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.review import OcrRun, OpenCodeReviewer
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy
from verdict.subagent_selection import HealthResult

FLAGSHIP_GOAL = "flagship failover test"
FLAGSHIP_RUN_ID = "offline-flagship-failover"
ROUTE_A = "alpha/model-a"
ROUTE_B = "beta/model-b"
ROUTE_C = "gamma/model-c"
_OFFLINE_OCR_ENV = "VERDICT_OFFLINE_DEMO_OCR_KEY"


def _inventory_row(route_id: str, *, owned_by: str) -> dict[str, Any]:
    """A route as returned by the inventory boundary, with no fabricated score."""
    return {
        "id": route_id,
        "owned_by": owned_by,
        "context_length": 200_000,
        "max_input_tokens": 200_000,
        "max_output_tokens": 32_000,
        "capabilities": {"tool_calling": True, "reasoning": True},
        "pricing": {"input": 1.0, "output": 2.0},
    }


def _connection(provider: str) -> dict[str, Any]:
    return {
        "provider": provider,
        "authType": "offline-scenario",
        "isActive": True,
        "testStatus": "ok",
        "backoffLevel": 0,
        "plan_label": "offline-scenario",
        "rate_limited_until": None,
        "import_free_only": False,
    }


INVENTORY = [
    _inventory_row(ROUTE_A, owned_by="alpha"),
    _inventory_row(ROUTE_B, owned_by="beta"),
    _inventory_row(ROUTE_C, owned_by="gamma"),
]
CONNECTIONS = [_connection("alpha"), _connection("beta"), _connection("gamma")]


def _impl_node(node_id: str, deps: tuple[str, ...] = ()) -> WorkNode:
    check = ("sh", "-c", f"test -f {node_id}.txt && ! grep -q FAIL {node_id}.txt")
    return WorkNode(
        node_id=node_id,
        objective=f"write {node_id}",
        kind=NodeKind.IMPLEMENT,
        depends_on=deps,
        owned_files=(f"{node_id}.txt",),
        verification_command=check,
    )


GRAPH = WorkGraph(
    goal=FLAGSHIP_GOAL,
    nodes=(
        _impl_node("node-1"),
        _impl_node("node-2"),
        WorkNode(
            node_id="integrate",
            objective="merge and verify",
            kind=NodeKind.INTEGRATE,
            depends_on=("node-1", "node-2"),
            owned_files=(),
            verification_command=("sh", "-c", "test -f node-1.txt && test -f node-2.txt"),
            barrier="integration",
        ),
    ),
    max_parallel=2,
)


class _HealthyProbe:
    """Offline inventory-boundary probe: all listed routes are healthy."""

    def __call__(self, route_id: str) -> HealthResult:
        return HealthResult(healthy=True, category="")


def _worker_script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
    """Scripted execution boundary that makes the real run loop's requested edit."""
    node_id = cwd.name.rsplit("-a", 1)[0]
    (cwd / f"{node_id}.txt").write_text(f"{node_id} implemented by {route_id}\n")
    return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id, stop_reason="stop")


class _PassingOcrRunner:
    """Scripted OCR process boundary; OpenCodeReviewer still parses and validates it."""

    def __call__(self, argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> OcrRun:
        args = list(argv)
        if "--version" in args:
            return OcrRun(exit_code=0, stdout="open-code-review offline-scenario\n")
        model = args[args.index("--model") + 1]
        payload = {
            "status": "complete",
            "comments": [],
            "manifest": {
                "execution": {"ocr_version": "offline-scenario", "model": model},
                "coverage": {
                    "selected": [{"item_id": "offline-scenario", "path": "node-1.txt"}],
                    "completed": [{"item_id": "offline-scenario", "path": "node-1.txt"}],
                    "failed": [],
                },
            },
        }
        out = Path(args[args.index("--output") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, sort_keys=True))
        return OcrRun(exit_code=0)


@dataclass(frozen=True)
class FlagshipScenarioResult:
    run_dir: Path
    events: list[dict[str, Any]]
    receipt: dict[str, Any]
    ladder: EligibilityLadder


def _init_repo(workspace_root: Path) -> Path:
    root = workspace_root / "repo"
    root.mkdir(parents=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "offline-demo@verdict.invalid"],
        ["config", "user.name", "Verdict Offline Demo"],
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "README.md").write_text("Offline flagship scenario workspace.\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "initialize offline scenario"], cwd=root, check=True, capture_output=True)
    return root


def _load_events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def run_flagship_scenario(
    runs_root: Path,
    *,
    workspace_root: Path,
    run_id: str = FLAGSHIP_RUN_ID,
) -> FlagshipScenarioResult:
    """Run the offline flagship through ``run_golden_path`` and return its artifacts.

    ``run_id`` is fixed by default. Event timestamps, cooldown expiry times, elapsed
    durations, and git commit hashes remain runtime-derived because the production
    loop does not currently expose a clock or git-object injection seam.
    """
    runs_root = Path(runs_root).resolve()
    workspace_root = Path(workspace_root).resolve()
    run_dir = runs_root / run_id
    if run_dir.exists():
        raise FileExistsError(f"refusing to append to existing demo run: {run_dir}")
    repo_dir = workspace_root / "repo"
    if repo_dir.exists():
        raise FileExistsError(f"refusing to reuse existing demo workspace: {repo_dir}")
    runs_root.mkdir(parents=True, exist_ok=True)
    workspace_root.mkdir(parents=True, exist_ok=True)
    repo = _init_repo(workspace_root)
    ladder = EligibilityLadder(
        INVENTORY,
        CONNECTIONS,
        _HealthyProbe(),
        workspace_root / "ladder-state.json",
    )
    executor = FaultInjectingExecutor(
        ScriptedExecutor(_worker_script),
        {ROUTE_A: ["rate_limit"]},
    )
    reviewer = OpenCodeReviewer(
        ladder,
        api_key_env=_OFFLINE_OCR_ENV,
        out_dir=workspace_root / "review",
        runner=_PassingOcrRunner(),
    )

    old_key = os.environ.get(_OFFLINE_OCR_ENV)
    os.environ[_OFFLINE_OCR_ENV] = "offline-scripted-no-network"
    try:
        result = asyncio.run(
            run_golden_path(
                FLAGSHIP_GOAL,
                repo=repo,
                runs_root=runs_root,
                selector=ladder,
                executor=executor,
                classifier=FailureIntelligence(),
                reviewer=reviewer,
                graph=GRAPH,
                run_id=run_id,
                policy=RuntimePolicy(max_parallel=2, max_attempts_per_node=4),
            )
        )
    finally:
        if old_key is None:
            os.environ.pop(_OFFLINE_OCR_ENV, None)
        else:
            os.environ[_OFFLINE_OCR_ENV] = old_key

    if result.outcome != RunOutcome.COMPLETE.value:
        raise RuntimeError(f"offline flagship did not COMPLETE: {result.outcome} — {result.reason}")
    return FlagshipScenarioResult(
        run_dir=result.run_dir,
        events=_load_events(result.run_dir),
        receipt=json.loads((result.run_dir / "receipt.json").read_text()),
        ladder=ladder,
    )
