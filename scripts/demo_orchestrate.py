"""Deterministic, credential-free orchestration demo.

Runs the real orchestration pipeline (frontier planner parsing, WorkGraph,
canonical admission, the eligibility ladder, the DAG runtime with per-node
git worktrees, failure classification, same-node reassignment, the
integration barrier, the reviewer-independence policy and the run receipt)
against a fixture gateway inventory. Nothing calls a model or the network:

* the inventory, provider connections and runtime evidence are fixtures below;
* health probes are a fixture that reports every probed route healthy;
* workers are a scripted executor that writes the owned file and answers
  ``RESULT: DONE``;
* faults (quota, rate limit, no final answer) are injected with the same
  ``FaultInjectingExecutor`` that ``verdict orchestrate --inject`` uses;
* the reviewer is a fixture that selects an independent route through the
  same ladder and returns PASS without a model call.

Usage::

    python scripts/demo_orchestrate.py --out docs/proof/demo-run
    verdict run-receipt docs/proof/demo-run

The copied run directory also holds ``admission.json``, the canonical
admission receipt for the fixture inventory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from verdict.admission import RuntimeEvidence, RuntimeObservation, admit
from verdict.decision_signals.contracts import (
    DecisionQuestionV1,
    DecisionSignalSetV1,
    compute_input_digest,
)
from verdict.orchestration.contracts import ReviewResult, TaskRequirements, WorkerTerminal
from verdict.orchestration.eligibility import EligibilityLadder
from verdict.orchestration.executors import FaultInjectingExecutor, ScriptedExecutor
from verdict.orchestration.recovery import FailureIntelligence
from verdict.orchestration.run import run_golden_path
from verdict.orchestration.runtime import RuntimePolicy
from verdict.orchestration.tui import event_line
from verdict.subagent_selection import HealthResult

GOAL = "Add a --json flag to the invoice CLI, with a parser module and tests"
RUN_ID = "demo-run"

# Faults injected per node, popped one per attempt (same syntax as --inject @node=...).
FAULTS: dict[str, list[str]] = {"@parser": ["quota", "rate_limit"], "@cli_flag": ["no_final"]}


def _row(route_id: str, *, tools: bool = True, price: float | None = None) -> dict[str, Any]:
    pricing = {"input": price, "output": price} if price is not None else {}
    return {
        "id": route_id,
        "owned_by": route_id.split("/", 1)[0],
        "context_length": 200_000,
        "max_input_tokens": 200_000,
        "max_output_tokens": 32_000,
        "capabilities": {"tool_calling": tools, "reasoning": True},
        "pricing": pricing,
    }


def _conn(provider: str, *, auth: str, plan: str, limited: bool = False) -> dict[str, Any]:
    until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    return {
        "provider": provider,
        "authType": auth,
        "isActive": True,
        "testStatus": "ok",
        "plan_label": plan,
        "rate_limited_until": {"*": until} if limited else None,
        "import_free_only": plan == "free",
    }


def fixture_inventory() -> list[dict[str, Any]]:
    """Fixture gateway ``/v1/models`` rows (not a live inventory)."""
    return [
        _row("auto/best-coding"),  # opaque router: never a candidate
        _row("demo-sub/atlas-coder"),  # prepaid subscription
        _row("demo-free/birch-coder", price=0.0),
        _row("demo-free/cedar-coder", price=0.0),
        _row("demo-free/hazel-coder", price=0.0),  # runtime evidence: unhealthy
        _row("demo-free2/elm-coder", price=0.0),
        _row("demo-free2/fir-mini", tools=False, price=0.0),  # no tool calling
        _row("demo-metered/delta-coder", price=3.0),  # pay per token
        _row("demo-busy/juniper-coder", price=0.0),  # provider rate-limited
        _row("demo-orphan/gum-coder", price=0.0),  # no connected account
    ]


def fixture_connections() -> list[dict[str, Any]]:
    """Fixture gateway provider connections (not live accounts)."""
    return [
        _conn("demo-sub", auth="oauth", plan="pro"),
        _conn("demo-free", auth="apikey", plan="free"),
        _conn("demo-free2", auth="apikey", plan="free"),
        _conn("demo-metered", auth="apikey", plan="pay-as-you-go"),
        _conn("demo-busy", auth="apikey", plan="free", limited=True),
    ]


def fixture_runtime(now: datetime) -> RuntimeEvidence:
    obs = RuntimeObservation(
        "route:demo-free/hazel-coder", "unhealthy", "model_unavailable", "fixture", now.isoformat()
    )
    return RuntimeEvidence((obs,), ("fixture",))


PLAN = {
    "nodes": [
        {
            "node_id": "parser",
            "objective": "Add invoice/parser.py with parse_invoice()",
            "kind": "implement",
            "owned_files": ["invoice/parser.py"],
            "verification_command": [
                "python3",
                "-c",
                "import invoice.parser as p; assert p.parse_invoice('a,1') == {'a': 1}",
            ],
        },
        {
            "node_id": "cli_flag",
            "objective": "Add a --json flag to invoice/cli.py",
            "kind": "implement",
            "owned_files": ["invoice/cli.py"],
            "verification_command": [
                "python3",
                "-c",
                "import invoice.cli as c; assert c.FLAGS == ('--json',)",
            ],
        },
        {
            "node_id": "integrate",
            "objective": "Merge both nodes and run the combined check",
            "kind": "integrate",
            "depends_on": ["parser", "cli_flag"],
            "verification_command": ["python3", "-c", "import invoice.cli, invoice.parser"],
        },
    ]
}

_FILES = {
    "parser": (
        "invoice/parser.py",
        "def parse_invoice(text):\n"
        "    key, value = text.split(',')\n"
        "    return {key: int(value)}\n",
    ),
    "cli_flag": ("invoice/cli.py", "FLAGS = ('--json',)\n"),
}


def _script(prompt: str, route_id: str, cwd: Path) -> WorkerTerminal:
    if "decomposing a goal" in prompt:
        return WorkerTerminal(ok=True, output=json.dumps(PLAN), model=route_id)
    node = cwd.name.rsplit("-a", 1)[0]
    rel, body = _FILES[node]
    (cwd / rel).write_text(body, encoding="utf-8")
    return WorkerTerminal(ok=True, output="RESULT: DONE", model=route_id)


class FixtureReviewer:
    """Selects an independent reviewer route through the ladder; returns PASS, no model call."""

    def __init__(self, selector: EligibilityLadder) -> None:
        self._selector = selector

    async def review(
        self,
        *,
        repo: Path,
        base_ref: str,
        head_ref: str,
        background: str,
        exclude_routes: frozenset[str],
        exclude_families: frozenset[str],
    ) -> ReviewResult:
        chosen, _ = self._selector.select(
            TaskRequirements(
                required_capabilities=frozenset({"tools"}),
                coding=True,
                reasoning=True,
                frontier_worthy=True,
                exclude_routes=exclude_routes,
                exclude_families=exclude_families,
            ),
            now=datetime.now(timezone.utc),
        )
        if chosen is None:
            return ReviewResult("ERROR", "fixture-reviewer", "", detail="no independent reviewer")
        return ReviewResult("PASS", "fixture-reviewer (no model call)", chosen.route_id)


class FixtureShadowSignals:
    """Fixture decision-signal provider in SHADOW mode: recorded, never used for selection."""

    def signals(self, question: DecisionQuestionV1, *, now: datetime) -> DecisionSignalSetV1:
        return DecisionSignalSetV1(
            schema_version="decision-signals/v1",
            provider="fixture",
            model="fixture",
            version="0",
            request_id="demo",
            purpose=question.purpose,
            signals={"frontier_worthy": 0.2, "complexity": 0.3},
            confidence=0.9,
            latency_ms=0,
            usage={"input_tokens": 0, "output_tokens": 0},
            input_digest=compute_input_digest(question),
            observed_at=now.isoformat(),
            failure_class=None,
            mode="SHADOW",
        )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(root: Path) -> Path:
    repo = root / "invoice-repo"
    (repo / "invoice").mkdir(parents=True)
    (repo / "invoice" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "README.md").write_text("# invoice fixture\n", encoding="utf-8")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "demo@example.invalid")
    _git(repo, "config", "user.name", "demo")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


async def run_demo(out: Path) -> int:
    os.environ["VERDICT_DECISION_SIGNALS_MODE"] = "SHADOW"
    now = datetime.now(timezone.utc)
    # A fixed scratch path keeps the recorded event log free of random temp names.
    root = Path(tempfile.gettempdir()) / "verdict-demo"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    try:
        repo = _make_repo(root)
        rows, conns = fixture_inventory(), fixture_connections()
        admitted = admit(rows, conns, fixture_runtime(now), now=now, inventory_source="fixture")
        inflight: dict[str, str] = {}
        ladder = EligibilityLadder(
            rows,
            conns,
            lambda _route: HealthResult(healthy=True, category=""),
            root / "state" / "health.json",
            prefer_providers=("demo-sub",),
            load=lambda route: sum(1 for r in inflight.values() if r == route),
            admitted=admitted,
        )
        print(f"goal: {GOAL}")
        print(f"admission: {len(admitted)} of {len(rows)} fixture routes admitted")
        for record in admitted.records:
            if not record.admitted and record.first_failed_stage is not None:
                stage = record.first_failed_stage.value
                print(f"  drop {record.route_id:<26} {stage:<11} {record.reason}")
        result = await run_golden_path(
            GOAL,
            repo=repo,
            runs_root=root / "runs",
            selector=ladder,
            executor=FaultInjectingExecutor(ScriptedExecutor(_script), FAULTS),
            classifier=FailureIntelligence(),
            reviewer=FixtureReviewer(ladder),
            run_id=RUN_ID,
            policy=RuntimePolicy(max_parallel=2, attempt_timeout_seconds=60),
            summary=ladder.summary,
            inflight=inflight,
            decision_signal_provider=FixtureShadowSignals(),
        )
        run_dir = result.run_dir
        for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event["type"] in {"node_state", "heartbeat", "hydrate"}:
                continue
            print(event_line(event))
        print(f"\nVERDICT {result.outcome}: {result.reason}")
        if out.exists():
            shutil.rmtree(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(run_dir, out, ignore=shutil.ignore_patterns("worktrees", "*.tmp"))
        admitted.write_receipt(out / "admission.json")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if result.outcome == "COMPLETE" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--out", default="docs/proof/demo-run", help="Where to copy the run dir")
    args = parser.parse_args(argv)
    return asyncio.run(run_demo(Path(args.out)))


if __name__ == "__main__":
    raise SystemExit(main())
