"""One bounded improvement cycle above a supervised run (ADR-038 increment 1).

``verdict improve --once`` plans a cycle, runs one supervised orchestrate run,
measures it only from evidence Verdict can recompute (the run receipt and its
claims), classifies the first blocker into one layer, and records a lesson the
next cycle passes to the planner through ``--constraints-file``. A cycle whose
inputs match a previous NOT_MET cycle is refused before anything runs.

The cycle log is JSONL in ``<cycles-dir>/cycles.jsonl``. It is a working record,
not tamper-evident storage: the unchanged-relaunch guard stops Verdict from
wasting a run, it is not a boundary against the operator, and ``verify_cycles``
cannot prove that no record was deleted. Measured results are trusted only after
recomputing them from each run's digest-verified receipt. Lesson text comes from
a fixed per-layer table, so the log can only select among Verdict's own lessons.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import subprocess
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.claims import derive_claims
from verdict.orchestration.receipt import capture_producer, completion_verdict, verify_run_receipt
from verdict.security import redact_text

CYCLES_FILE = "cycles.jsonl"
REQUIRED_CLAIMS = ("validation_passed", "independent_review", "receipt_integrity")
MAX_LESSON_BYTES = 2048
CAPACITY_CATEGORIES = frozenset({"quota_exhausted", "rate_limited"})

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
    wait_until: str = ""  # set when the next cycle can run only after a capacity reset


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


def input_fingerprint(
    goal: str, head: str, args: Sequence[str], constraints: str, producer: Mapping[str, Any]
) -> str:
    """Everything that decides what a run will do; equal fingerprints mean an unchanged relaunch.

    The Verdict build (version, commit, dirty flag) is part of it: new Verdict code
    is a changed input even when the goal, repo and lessons are the same.
    """
    payload = json.dumps(
        {
            "goal": goal,
            "head": head,
            "args": list(args),
            "constraints": _sha256(constraints.encode()),
            "verdict": dict(producer),
        },
        sort_keys=True,
    )
    return _sha256(payload.encode())


def lessons_to_constraints(cycles: Sequence[Mapping[str, Any]]) -> str:
    """Distinct planner lessons from earlier NOT_MET cycles, newest first.

    The text comes only from Verdict's fixed per-layer table, keyed by the
    recorded layer, so nothing written to disk can put new text into a prompt.
    """
    seen: list[str] = []
    for record in reversed(cycles):
        layer = str(record.get("layer", ""))
        # Operator lessons and capacity waits are not things a planner can act on.
        if (
            record.get("stage") != "learned"
            or layer in _OPERATOR_LAYERS
            or record.get("wait_until")
        ):
            continue
        text = _LAYER_LESSONS.get(layer)
        if text and text not in seen:
            seen.append(text)
    out = ""
    for text in seen:
        candidate = f"{out}- {text}\n"
        if len(candidate.encode()) > MAX_LESSON_BYTES:
            break
        out = candidate
    return out


_NODE_TOKEN_CHAR = r"[a-zA-Z0-9_\-]"  # no ".": a sentence-final id must still match


def _real_node_ids(run_dir: Path, events: Sequence[Mapping[str, Any]]) -> set[str]:
    """Real node IDs belonging to the run, extracted from graph.json or node_state events."""
    nodes: set[str] = set()
    graph_path = run_dir / "graph.json"
    if graph_path.exists():
        try:
            gdata = json.loads(graph_path.read_text())
            for n in gdata.get("nodes", []):
                if isinstance(n, Mapping) and n.get("node_id"):
                    nodes.add(str(n["node_id"]))
        except (json.JSONDecodeError, OSError):
            pass
    for e in events:
        if e.get("type") == "node_state" and e.get("node_id"):
            nodes.add(str(e["node_id"]))
        elif e.get("type") == "plan_ready":
            for n in (e.get("data") or {}).get("nodes", []):
                if isinstance(n, Mapping) and n.get("node_id"):
                    nodes.add(str(n["node_id"]))
    return nodes


def _blocked_node(reason: str, known_nodes: set[str]) -> str | None:
    """Extract the node_id that blocked the run from the run_finished reason, if any.

    Requires membership in the run's real node IDs as a whole token.
    """
    if not known_nodes:
        return None

    # 1. "node <id>" as a whole token
    for m in re.finditer(
        rf"\bnode\s+['\"]?({_NODE_TOKEN_CHAR}+)['\"]?(?!{_NODE_TOKEN_CHAR})", reason, re.IGNORECASE
    ):
        candidate = m.group(1)
        if candidate in known_nodes:
            return candidate

    # 2. "<id> not VALIDATED"
    for m in re.finditer(
        rf"(?<!{_NODE_TOKEN_CHAR})({_NODE_TOKEN_CHAR}+)\s+not\s+VALIDATED\b", reason, re.IGNORECASE
    ):
        candidate = m.group(1)
        if candidate in known_nodes:
            return candidate

    # 3. "<id>: ..." at start of string or after semicolon/newline
    for part in re.split(r"[;\n]", reason):
        part = part.strip()
        if part.lower().startswith("run_finished:"):
            part = part[len("run_finished:") :].strip()
        m_part = re.match(rf"^({_NODE_TOKEN_CHAR}+):", part)
        if m_part:
            candidate = m_part.group(1)
            if candidate in known_nodes:
                return candidate

    # 4. Whole-token match for known nodes (longest first)
    for nid in sorted(known_nodes, key=len, reverse=True):
        pattern = rf"(?<!{_NODE_TOKEN_CHAR}){re.escape(nid)}(?!{_NODE_TOKEN_CHAR})"
        if len(nid) == 1 and not re.search(r"\b(node|validated)\b|:", reason, re.IGNORECASE):
            continue
        if re.search(pattern, reason):
            return nid

    return None


def classify_blocker(reason: str, run_dir: Path) -> tuple[str, str]:
    """(layer, evidence) for the run's decisive blocker; deterministic."""
    text = reason.lower()
    evidence = f"run_finished: {redact_text(reason)[:300]}"
    if "independent_review=" in text:
        return "review_independence", f"claims: {redact_text(reason)[:300]}"
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
        known_nodes = _real_node_ids(run_dir, events)
        blocked_node = _blocked_node(reason, known_nodes)
        last_failure: dict[str, Any] | None = None
        if blocked_node is not None:
            for event in reversed(events):
                if event.get("type") == "failure":
                    data = event.get("data") or {}
                    if data.get("category"):
                        ev_node = str(event.get("node_id") or data.get("node_id") or "")
                        if ev_node == blocked_node:
                            last_failure = event
                            break
        if last_failure is None:
            for event in reversed(events):
                if event.get("type") == "failure":
                    data = event.get("data") or {}
                    if data.get("category"):
                        last_failure = event
                        break
        if last_failure is not None:
            data = last_failure.get("data") or {}
            category = str(data.get("category", ""))
            if category:
                text = f"{text} {category}".lower()
                evidence = f"event:{last_failure.get('seq')} failure {category}"
    for layer, needles in _LAYER_RULES:
        if any(needle in text for needle in needles):
            return layer, evidence
    return "execution", evidence


def capacity_reset(run_dir: Path, after: datetime) -> datetime | None:
    """Earliest credential-capacity reset after ``after`` recorded by the run, if any."""
    resets: list[datetime] = []
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            event = json.loads(line)
            data = event.get("data") or {}
            # Only credential capacity is worth waiting for; a short route cooldown
            # after a crash is not a reason to hold the next cycle.
            if event.get("type") != "cooldown" or data.get("category") not in CAPACITY_CATEGORIES:
                continue
            try:
                until = datetime.fromisoformat(str(data.get("until", "")))
            except ValueError:
                continue
            if until.tzinfo is not None and until > after:
                resets.append(until)
    return min(resets, default=None)


def _only_capacity_failures(run_dir: Path) -> bool:
    """True when the run failed attempts, and every failure was a capacity limit."""
    categories = []
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in events_path.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "failure":
                categories.append((event.get("data") or {}).get("category"))
    return bool(categories) and all(c in CAPACITY_CATEGORIES for c in categories)


def _blocker_reason(run_dir: Path, measured: Mapping[str, Any]) -> str:
    """The run's own recorded cause; the receipt verdict reason only restates it."""
    events_path = run_dir / "events.jsonl"
    if events_path.exists():
        for line in reversed(events_path.read_text().splitlines()):
            event = json.loads(line)
            if event.get("type") == "run_finished":
                reason = str((event.get("data") or {}).get("reason") or "")
                if reason:
                    return f"{reason}; {measured.get('reason', '')}"
                break
    return str(measured.get("reason", ""))


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
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CycleResult:
    """Plan, act, measure and learn once. ``supervise(run_id, argv)`` runs the run."""
    cycles = read_cycles(cycles_dir)
    constraints = lessons_to_constraints(cycles)
    fingerprint = input_fingerprint(
        goal, repo_head(repo), orchestrate_args, constraints, capture_producer()
    )
    cycle_id = datetime.now(timezone.utc).strftime("c%Y%m%dT%H%M%S%fZ")
    prior = [
        r for r in cycles if r.get("stage") == "measured" and r.get("fingerprint") == fingerprint
    ]
    if prior and not prior[-1].get("metric_met"):
        # Time is an input too: after a capacity reset the same inputs may succeed.
        learned = next(
            (
                r
                for r in cycles
                if r.get("stage") == "learned" and r.get("cycle_id") == prior[-1]["cycle_id"]
            ),
            {},
        )
        wait_until = str(learned.get("wait_until") or "")
        if not wait_until or clock() < datetime.fromisoformat(wait_until):
            reason = (
                f"waiting for capacity until {wait_until} (cycle {prior[-1]['cycle_id']})"
                if wait_until
                else f"unchanged relaunch of cycle {prior[-1]['cycle_id']}: "
                "same goal, repo HEAD, arguments and lessons"
            )
            _append(
                cycles_dir,
                {
                    "stage": "refused",
                    "cycle_id": cycle_id,
                    "at": _now(),
                    "reason": reason,
                    "wait_until": wait_until,
                },
            )
            return CycleResult(cycle_id, "REFUSED", reason, wait_until=wait_until)
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
    layer, evidence = classify_blocker(_blocker_reason(run_dir, measured), run_dir)
    # Wait only when capacity is what stopped the run: the blocker is a selection
    # failure, or every failed attempt was a quota/rate limit, and a cooldown on
    # that capacity is still in force. A capacity cooldown next to an unrelated
    # failure (bad code, review) is not a reason to rerun the same inputs later.
    reset = capacity_reset(run_dir, clock())
    if (
        reset is not None
        and layer not in _OPERATOR_LAYERS
        and (layer == "selection" or _only_capacity_failures(run_dir))
    ):
        layer, evidence = "selection", f"cooldown until {reset.isoformat()}"
        wait_until = reset.isoformat()
    else:
        wait_until = ""
    _append(
        cycles_dir,
        {
            "stage": "learned",
            "cycle_id": cycle_id,
            "at": _now(),
            "run_id": run_id,
            "layer": layer,
            "evidence": evidence,
            "lesson": _LAYER_LESSONS[layer],
            "operator_action": layer in _OPERATOR_LAYERS,
            "wait_until": wait_until,
        },
    )
    return CycleResult(
        cycle_id, "NOT_MET", str(measured.get("reason", "")), run_id, layer, wait_until
    )


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


MAX_CYCLES = 5
MAX_WAIT_SECONDS = 900.0


async def run_cycles(
    goal: str,
    *,
    repo: Path,
    runs_dir: Path,
    cycles_dir: Path,
    orchestrate_args: Sequence[str],
    supervise: Supervise,
    cycles: int,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> list[CycleResult]:
    """Up to ``cycles`` runs (at most MAX_CYCLES). Stops at the first cycle that is
    MET, refused, or whose lesson needs the operator rather than the planner. A
    cycle that must wait for a capacity reset of at most MAX_WAIT_SECONDS waits
    once and tries again; a longer wait ends the loop and reports the reset time."""
    if not 1 <= cycles <= MAX_CYCLES:
        raise CycleError(f"--cycles must be between 1 and {MAX_CYCLES}")
    results: list[CycleResult] = []
    runs, waited = 0, False
    while runs < cycles:
        result = await run_cycle(
            goal,
            repo=repo,
            runs_dir=runs_dir,
            cycles_dir=cycles_dir,
            orchestrate_args=orchestrate_args,
            supervise=supervise,
            clock=clock,
        )
        results.append(result)
        if result.outcome == "REFUSED" and result.wait_until and not waited:
            delay = (datetime.fromisoformat(result.wait_until) - clock()).total_seconds()
            if delay <= MAX_WAIT_SECONDS:
                waited = True
                await sleep(max(0.0, delay) + 1)
                continue
        if result.outcome != "NOT_MET" or result.layer in _OPERATOR_LAYERS:
            break
        runs, waited = runs + 1, False
    return results


def run_with_supervisor(
    goal: str,
    *,
    repo: Path,
    runs_dir: Path,
    cycles_dir: Path,
    orchestrate_args: Sequence[str],
    supervisor_kwargs: Mapping[str, Any],
    cycles: int = 1,
) -> list[CycleResult]:
    from verdict.orchestration.supervisor import ControllerSupervisor, build_command_factory

    async def supervise(run_id: str, argv: list[str]) -> Any:
        supervisor = ControllerSupervisor(
            build_command_factory(run_id, argv, runs_dir), runs_dir / run_id, **supervisor_kwargs
        )
        return await supervisor.run()

    return asyncio.run(
        run_cycles(
            goal,
            repo=repo,
            runs_dir=runs_dir,
            cycles_dir=cycles_dir,
            orchestrate_args=orchestrate_args,
            supervise=supervise,
            cycles=cycles,
        )
    )
