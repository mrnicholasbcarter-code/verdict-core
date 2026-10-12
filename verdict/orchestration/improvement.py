"""One bounded improvement cycle above a supervised run (ADR-038 increment 1).

``verdict improve --once`` plans a cycle, runs one supervised orchestrate run,
measures it only from evidence Verdict can recompute (the run receipt and its
claims), classifies the first blocker into one layer, and records a lesson the
next cycle passes to the planner through ``--constraints-file``. A cycle whose
inputs match a previous NOT_MET cycle is refused before anything runs.

The cycle log is append-only JSONL in ``<cycles-dir>/cycles.jsonl``; each
cycle's lesson text lives beside it as ``<cycle-id>.lesson.txt``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.claims import derive_claims
from verdict.orchestration.receipt import completion_verdict, verify_run_receipt
from verdict.security import redact_text

CYCLES_FILE = "cycles.jsonl"
REQUIRED_CLAIMS = ("validation_passed", "independent_review", "receipt_integrity")
MAX_LESSON_BYTES = 2048

# First matching rule wins; the order puts the most specific evidence first.
_LAYER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("environment", ("401", "403", "unauthorized", "credential", "cgroup", "delegat")),
    ("selection", ("no eligible", "no_eligible_route", "quota", "cooldown", "429", "rate limit")),
    ("planning", ("planning failed", "plan invalid", "planner")),
    ("context", ("context length", "input is too long", "context budget")),
    ("review", ("review",)),
    ("verification", ("verify", "verification", "barrier", "integration")),
    ("controller", ("controller", "stalled", "failed_closed", "deadline")),
    ("execution", ("not validated", "attempt", "worker", "terminal")),
)
# Lessons the planner cannot act on: they need a change to the run's arguments or
# environment, so they are recorded but never passed on as planner constraints.
_OPERATOR_LAYERS = frozenset({"environment", "review_independence"})
_LAYER_LESSONS = {
    "review_independence": (
        "Review independence was not shown: the reviewer shared a model family or pool "
        "with the workers. Widen --scope to a second provider family."
    ),
    "environment": "Fix the reported environment prerequisite before relaunching; do not retry unchanged.",
    "selection": "Prefer routes with measured capacity; avoid the cooled-down or exhausted pool.",
    "planning": "Plan at most three nodes, each with a focused verification command.",
    "context": "Keep each node's owned files and required context small.",
    "review": "Make each node's acceptance criteria explicit and testable so review can pass.",
    "verification": "Each node must name a focused test command that passes on its own.",
    "controller": "Split the goal into smaller nodes that finish well inside the deadline.",
    "execution": "Give each node one owned file set and a concrete, checkable objective.",
}


class CycleError(Exception):
    """The cycle cannot run (refused or misconfigured); nothing was launched."""


@dataclass(frozen=True)
class CycleResult:
    cycle_id: str
    outcome: str  # MET | NOT_MET | REFUSED
    reason: str
    run_id: str = ""
    layer: str = ""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_cycles(cycles_dir: Path) -> list[dict[str, Any]]:
    path = cycles_dir / CYCLES_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append(cycles_dir: Path, record: Mapping[str, Any]) -> None:
    cycles_dir.mkdir(parents=True, exist_ok=True)
    with (cycles_dir / CYCLES_FILE).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def repo_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def input_fingerprint(goal: str, head: str, args: Sequence[str], constraints: str) -> str:
    """Everything that decides what a run will do; equal fingerprints mean an unchanged relaunch."""
    payload = json.dumps(
        {
            "goal": goal,
            "head": head,
            "args": list(args),
            "constraints": _sha256(constraints.encode()),
        },
        sort_keys=True,
    )
    return _sha256(payload.encode())


def lessons_to_constraints(cycles: Sequence[Mapping[str, Any]], cycles_dir: Path) -> str:
    """Distinct lessons from earlier NOT_MET cycles, newest first, within MAX_LESSON_BYTES."""
    seen: list[str] = []
    for record in reversed(cycles):
        if record.get("stage") != "learned" or record.get("operator_action"):
            continue
        path = cycles_dir / f"{record['cycle_id']}.lesson.txt"
        text = path.read_text().strip() if path.exists() else ""
        if text and text not in seen:
            seen.append(text)
    out = ""
    for text in seen:
        candidate = f"{out}- {text}\n"
        if len(candidate.encode()) > MAX_LESSON_BYTES:
            break
        out = candidate
    return out


def classify_blocker(reason: str, run_dir: Path) -> tuple[str, str]:
    """(layer, evidence) for the run's first concrete blocker; deterministic."""
    text = reason.lower()
    evidence = f"run_finished: {redact_text(reason)[:300]}"
    if "independent_review=" in text:
        return "review_independence", f"claims: {redact_text(reason)[:300]}"
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            event = json.loads(line)
            data = event.get("data") or {}
            if event.get("type") == "failure" and data.get("category"):
                text = f"{text} {data['category']}".lower()
                evidence = f"event:{event.get('seq')} failure {data['category']}"
                break
    for layer, needles in _LAYER_RULES:
        if any(needle in text for needle in needles):
            return layer, evidence
    return "execution", evidence


def measure_run(run_dir: Path) -> dict[str, Any]:
    """Outcome and claims recomputed from the run's files; a missing receipt is NOT_MET."""
    problems = verify_run_receipt(run_dir)
    receipt_path = run_dir / "receipt.json"
    if problems or not receipt_path.exists():
        return {"metric_met": False, "reason": "; ".join(problems) or "missing receipt.json"}
    receipt = json.loads(receipt_path.read_text())
    outcome, reason = completion_verdict(receipt)
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    claims = {c.id: c.status for c in derive_claims(events, receipt, run_dir=run_dir)}
    unmet = [c for c in REQUIRED_CLAIMS if claims.get(c) != "VERIFIED"]
    met = outcome == "COMPLETE" and not unmet
    if outcome == "COMPLETE" and unmet:
        reason = "claims not verified: " + ", ".join(
            f"{c}={claims.get(c, 'NOT_OBSERVED')}" for c in unmet
        )
    return {
        "metric_met": met,
        "outcome": outcome,
        "reason": reason,
        "claims": {c: claims.get(c, "NOT_OBSERVED") for c in REQUIRED_CLAIMS},
        "receipt_sha256": _sha256(receipt_path.read_bytes()),
    }


Supervise = Callable[[str, list[str]], Awaitable[Any]]


async def run_cycle(
    goal: str,
    *,
    repo: Path,
    runs_dir: Path,
    cycles_dir: Path,
    orchestrate_args: Sequence[str],
    supervise: Supervise,
) -> CycleResult:
    """Plan, act, measure and learn once. ``supervise(run_id, argv)`` runs the run."""
    cycles = read_cycles(cycles_dir)
    constraints = lessons_to_constraints(cycles, cycles_dir)
    fingerprint = input_fingerprint(goal, repo_head(repo), orchestrate_args, constraints)
    cycle_id = datetime.now(timezone.utc).strftime("c%Y%m%dT%H%M%S%fZ")
    prior = [
        r for r in cycles if r.get("stage") == "measured" and r.get("fingerprint") == fingerprint
    ]
    if prior and not prior[-1].get("metric_met"):
        reason = f"unchanged relaunch of cycle {prior[-1]['cycle_id']}: same goal, repo HEAD, arguments and lessons"
        _append(
            cycles_dir, {"stage": "refused", "cycle_id": cycle_id, "at": _now(), "reason": reason}
        )
        return CycleResult(cycle_id, "REFUSED", reason)
    run_id = f"improve-{cycle_id}"
    argv = [goal, "--repo", str(repo), *orchestrate_args]
    if constraints:
        constraints_path = cycles_dir / f"{cycle_id}.constraints.txt"
        cycles_dir.mkdir(parents=True, exist_ok=True)
        constraints_path.write_text(constraints)
        argv += ["--constraints-file", str(constraints_path)]
    _append(
        cycles_dir,
        {
            "stage": "planned",
            "cycle_id": cycle_id,
            "at": _now(),
            "run_id": run_id,
            "fingerprint": fingerprint,
            "constraints_sha256": _sha256(constraints.encode()) if constraints else "",
            "metric": {"completion": "COMPLETE", "claims_verified": list(REQUIRED_CLAIMS)},
        },
    )
    await supervise(run_id, argv)
    run_dir = runs_dir / run_id
    measured = measure_run(run_dir)
    _append(
        cycles_dir,
        {
            "stage": "measured",
            "cycle_id": cycle_id,
            "at": _now(),
            "run_id": run_id,
            "fingerprint": fingerprint,
            **measured,
        },
    )
    if measured["metric_met"]:
        return CycleResult(cycle_id, "MET", str(measured.get("reason", "")), run_id)
    layer, evidence = classify_blocker(str(measured.get("reason", "")), run_dir)
    (cycles_dir / f"{cycle_id}.lesson.txt").write_text(_LAYER_LESSONS[layer] + "\n")
    _append(
        cycles_dir,
        {
            "stage": "learned",
            "cycle_id": cycle_id,
            "at": _now(),
            "run_id": run_id,
            "layer": layer,
            "evidence": evidence,
            "operator_action": layer in _OPERATOR_LAYERS,
        },
    )
    return CycleResult(cycle_id, "NOT_MET", str(measured.get("reason", "")), run_id, layer)


def verify_cycles(cycles_dir: Path, runs_dir: Path) -> list[str]:
    """Recompute every measured cycle from its run; a self-reported result never counts."""
    problems: list[str] = []
    for record in read_cycles(cycles_dir):
        if record.get("stage") != "measured":
            continue
        fresh = measure_run(runs_dir / str(record.get("run_id")))
        if bool(fresh["metric_met"]) != bool(record.get("metric_met")):
            problems.append(f"cycle {record['cycle_id']}: metric_met does not match its run")
        if record.get("receipt_sha256") and fresh.get("receipt_sha256") != record["receipt_sha256"]:
            problems.append(f"cycle {record['cycle_id']}: run receipt changed after measurement")
    return problems


def run_once_with_supervisor(
    goal: str,
    *,
    repo: Path,
    runs_dir: Path,
    cycles_dir: Path,
    orchestrate_args: Sequence[str],
    supervisor_kwargs: Mapping[str, Any],
) -> CycleResult:
    from verdict.orchestration.supervisor import ControllerSupervisor, build_command_factory

    async def supervise(run_id: str, argv: list[str]) -> Any:
        supervisor = ControllerSupervisor(
            build_command_factory(run_id, argv, runs_dir), runs_dir / run_id, **supervisor_kwargs
        )
        return await supervisor.run()

    return asyncio.run(
        run_cycle(
            goal,
            repo=repo,
            runs_dir=runs_dir,
            cycles_dir=cycles_dir,
            orchestrate_args=orchestrate_args,
            supervise=supervise,
        )
    )
