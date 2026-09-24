"""Compose the live golden path: goal -> plan -> DAG run -> review -> receipt.

This is the only module that touches live OmniRoute discovery. Everything it
wires together is independently tested against fakes.

Controller survival (BOD-159..166, minimal interview-safe slice):
- the frontier *planning* call goes through the same select -> classify ->
  cooldown -> reassign loop as workers, so controller-model quota exhaustion
  moves planning to another eligible frontier model instead of hanging;
- the run writes ``progress.json`` after every event so an external supervisor
  (``verdict.orchestration.supervisor``) can detect a stalled controller process;
- state is durable (``graph.json`` + ``events.jsonl``) so a restarted controller
  resumes: nodes already VALIDATED with a commit are not re-executed.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.orchestration.contracts import (
    FailureClassifier,
    ModelSelector,
    NodeState,
    OrchestrationError,
    Reviewer,
    RunOutcome,
    TaskRequirements,
    WorkerExecutor,
    WorkGraph,
    WorkNode,
    route_provider,
)
from verdict.orchestration.planner import FrontierPlanner, hydrate_node_prompt
from verdict.orchestration.receipt import (
    GRAPH_FILE,
    EventLog,
    completion_verdict,
    write_run_receipt,
)
from verdict.orchestration.runtime import DagRuntime, RuntimePolicy

DEFAULT_GATEWAY = "http://127.0.0.1:20128"
PROGRESS_FILE = "progress.json"
_CONNECTION_FIELDS = ("provider", "authType", "isActive", "testStatus", "backoffLevel")
_PLAN_FIELDS = ("plan", "subscriptionTier", "tier", "organizationType", "organizationRateLimitTier")


_RISK_ORDER: Mapping[str, int] = {"low": 0, "medium": 1, "high": 2}


def _task_profile(goal: str, repo: Path, graph: WorkGraph | None) -> dict[str, Any]:
    """Deterministic UNDERSTAND profile: goal text + graph only, never a model call.

    Called before planning, so ``risk`` is "unknown" until a ``WorkGraph`` exists
    (either passed in directly or produced by ``plan_with_failover``).
    """
    risk = "unknown"
    if graph is not None and graph.nodes:
        risk = max((n.risk for n in graph.nodes), key=lambda r: _RISK_ORDER.get(r, 0))
    return {
        "goal_chars": len(goal),
        "risk": risk,
        "proof_requirements": ["node verification", "integration barrier", "independent review"],
        "scope": f"repo: {repo.name}",
    }


def resolve_api_key(env_name: str = "VERDICT_OMNIROUTE_API_KEY") -> str | None:
    """Gateway key from the environment; never logged, never persisted by Verdict."""
    value = os.environ.get(env_name, "").strip()
    return value or None


def _get_json(url: str, *, api_key: str | None, timeout: float) -> Any:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        raise OrchestrationError(f"gateway URL must be http(s): {url!r}")
    request = urllib.request.Request(url, headers=headers)
    # Scheme validated above; the gateway URL is operator configuration.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read(64 * 1024 * 1024))


def fetch_inventory(
    gateway: str, *, api_key: str | None, timeout: float = 30
) -> list[dict[str, Any]]:
    """DISCOVERED: live OmniRoute /v1/models rows (discovery only, not availability)."""
    raw = _get_json(gateway.rstrip("/") + "/v1/models", api_key=api_key, timeout=timeout)
    data = raw.get("data") if isinstance(raw, Mapping) else None
    if not isinstance(data, list):
        raise OrchestrationError("OmniRoute /v1/models returned no data array")
    return [row for row in data if isinstance(row, dict)]


def sanitize_connections(raw: Any) -> list[dict[str, Any]]:
    """ENTITLED evidence: allowlisted, credential-free projection of /api/providers."""
    rows = raw.get("connections") if isinstance(raw, Mapping) else raw
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        item: dict[str, Any] = {k: row.get(k) for k in _CONNECTION_FIELDS}
        psd = row.get("providerSpecificData")
        psd = psd if isinstance(psd, Mapping) else {}
        labels = [str(psd[k]) for k in _PLAN_FIELDS if isinstance(psd.get(k), str) and psd.get(k)]
        item["plan_label"] = " / ".join(dict.fromkeys(labels))
        limited = psd.get("codexScopeRateLimitedUntil")
        item["rate_limited_until"] = (
            {str(k): str(v) for k, v in limited.items()} if isinstance(limited, Mapping) else None
        )
        item["import_free_only"] = psd.get("importFreeModelsOnly") is True
        out.append(item)
    return out


def fetch_connections(
    gateway: str, *, api_key: str | None, timeout: float = 30
) -> list[dict[str, Any]]:
    raw = _get_json(gateway.rstrip("/") + "/api/providers", api_key=api_key, timeout=timeout)
    return sanitize_connections(raw)


class _Progress:
    """EventLog wrapper that also publishes a controller liveness/progress file."""

    def __init__(self, log: EventLog, path: Path, run_id: str) -> None:
        self.log, self.path, self.run_id = log, path, run_id
        self.pid = os.getpid()

    def emit(self, type: str, node_id: str = "", **data: Any) -> Any:
        event = self.log.emit(type, node_id=node_id, **data)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "pid": self.pid,
                    "seq": event.seq,
                    "last_event": type,
                    "last_progress_at": event.at,
                    "monotonic": time.monotonic(),
                }
            )
        )
        tmp.replace(self.path)
        return event


@dataclass(frozen=True)
class GoldenRunResult:
    run_dir: Path
    outcome: str
    reason: str
    receipt_path: Path


async def plan_with_failover(
    goal: str,
    *,
    repo: Path,
    selector: ModelSelector,
    executor: WorkerExecutor,
    classifier: FailureClassifier,
    events: Any,
    max_attempts: int = 6,
    timeout_seconds: float = 600,
    constraints: str = "",
    max_parallel: int = 3,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> WorkGraph:
    """Frontier decomposition with the same controller-model failover as workers.

    The planning model is the run's controller intelligence. When its capacity
    fails (quota, rate limit, auth, no final answer, ...) the failure is
    classified from the structured terminal, the route or whole provider is
    cooled down with any parsed reset time, and the NEXT eligible frontier model
    is selected dynamically. Nothing waits on the exhausted model.
    """
    from verdict.orchestration.planner import PlanningExecutorError

    tried: set[str] = set()
    last = ""
    for attempt in range(1, max_attempts + 1):
        requirements = TaskRequirements(
            required_capabilities=frozenset({"tools"}),
            coding=True,
            reasoning=True,
            frontier_worthy=True,
            min_context_tokens=100_000,
            exclude_routes=frozenset(tried),
        )
        choice, _ = selector.select(requirements, now=now())
        if choice is None:
            break
        events.emit(
            "plan_started",
            route_id=choice.route_id,
            provider=choice.provider,
            capacity_class=choice.capacity_class.value,
            attempt=attempt,
        )
        try:
            graph, terminal = await FrontierPlanner().plan(
                goal,
                repo=repo,
                executor=executor,
                route_id=choice.route_id,
                timeout_seconds=timeout_seconds,
                constraints=constraints,
            )
        except OrchestrationError as exc:
            last = f"{choice.route_id}: {exc}"
            source = (
                exc.terminal
                if isinstance(exc, PlanningExecutorError)
                else _terminal_from_error(choice.route_id, str(exc))
            )
            failure = classifier.classify(source, now=now())
            state = (
                "QUOTA"
                if failure.category in {"quota_exhausted", "rate_limited"}
                else "PLANNER_FAILED"
            )
            events.emit(
                "controller",
                state=state,
                route_id=choice.route_id,
                category=failure.category,
                detail=str(exc)[:300],
                fault_injected=source.session_ref.startswith("fault-injected"),
            )
            if failure.scope != "none" and failure.cooldown_seconds > 0:
                selector.record_failure(choice.route_id, failure, now=now())
                until = datetime.fromtimestamp(
                    now().timestamp() + failure.cooldown_seconds, timezone.utc
                )
                events.emit(
                    "cooldown",
                    key=choice.route_id
                    if failure.scope == "route"
                    else route_provider(choice.route_id),
                    scope=failure.scope,
                    category=failure.category,
                    until=until.isoformat(timespec="seconds"),
                )
            tried.add(choice.route_id)
            events.emit(
                "controller",
                state="REPLACING",
                detail=f"reselecting planner after {failure.category}",
            )
            continue
        del terminal
        events.emit("controller", state="HEALTHY", route_id=choice.route_id, detail="plan produced")
        if max_parallel != graph.max_parallel:
            graph = WorkGraph(
                graph.goal,
                graph.nodes,
                graph.topology,
                graph.rationale,
                min(max_parallel, graph.max_parallel),
            )
        return graph
    raise OrchestrationError(
        f"planning failed on every eligible frontier model; last: {last or 'none eligible'}"
    )


def _terminal_from_error(route_id: str, text: str) -> Any:
    from verdict.orchestration.contracts import WorkerTerminal

    code = None
    for token in ("429", "401", "402", "403", "400", "500", "502", "503", "504"):
        if token in text:
            code = int(token)
            break
    return WorkerTerminal(ok=False, model=route_id, error=text, status_code=code)


def load_or_create_run(root: Path, run_id: str | None) -> Path:
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:6]
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def prior_validated(run_dir: Path) -> dict[str, tuple[str, str]]:
    """Resume support: node_id -> (commit, implementer route) for VALIDATED nodes.

    The implementer route is restored so reviewer independence survives a
    controller restart (a resumed run must still exclude who wrote the code).
    """
    path = run_dir / "events.jsonl"
    if not path.exists():
        return {}
    done: dict[str, tuple[str, str]] = {}
    for event in EventLog(path).read():
        if event.type == "node_state" and event.data.get("state") == NodeState.VALIDATED.value:
            commit = str(event.data.get("commit") or "")
            if commit:
                done[event.node_id] = (commit, str(event.data.get("route_id") or ""))
    return done


def prior_attempts(run_dir: Path) -> dict[str, int]:
    """node_id -> highest attempt number dispatched in previous controller lives."""
    path = run_dir / "events.jsonl"
    if not path.exists():
        return {}
    highest: dict[str, int] = {}
    for event in EventLog(path).read():
        if event.type == "dispatch":
            number = event.data.get("attempt")
            if isinstance(number, int):
                highest[event.node_id] = max(highest.get(event.node_id, 0), number)
    return highest


async def run_golden_path(
    goal: str,
    *,
    repo: Path,
    runs_root: Path,
    selector: ModelSelector,
    executor: WorkerExecutor,
    classifier: FailureClassifier,
    reviewer: Reviewer | None,
    graph: WorkGraph | None = None,
    run_id: str | None = None,
    policy: RuntimePolicy = RuntimePolicy(),
    constraints: str = "",
    summary: Callable[[], Mapping[str, Any]] | None = None,
    inflight: dict[str, str] | None = None,
) -> GoldenRunResult:
    run_dir = load_or_create_run(runs_root, run_id)
    log = EventLog(run_dir / "events.jsonl")
    events = _Progress(log, run_dir / PROGRESS_FILE, run_dir.name)
    resumed = prior_validated(run_dir)
    events.emit(
        "run_started", run_id=run_dir.name, goal=goal, repo=str(repo), resumed_nodes=sorted(resumed)
    )
    if resumed:
        events.emit(
            "controller", state="RESUMED", detail=f"{len(resumed)} validated node(s) reused"
        )
    if summary is not None:
        counts = {k: v for k, v in summary().items() if isinstance(v, int) and k != "node_id"}
        events.emit("eligibility", "", **counts)
    graph_path = run_dir / GRAPH_FILE
    events.emit("understand", **_task_profile(goal, repo, graph))
    try:
        if graph is None and graph_path.exists():
            graph = WorkGraph.from_dict(
                {k: v for k, v in json.loads(graph_path.read_text()).items() if k != "run_id"}
            )
        if graph is None:
            graph = await plan_with_failover(
                goal,
                repo=repo,
                selector=selector,
                executor=executor,
                classifier=classifier,
                events=events,
                constraints=constraints,
                max_parallel=policy.max_parallel,
            )
    except OrchestrationError as exc:
        events.emit("run_finished", outcome=RunOutcome.BLOCKED.value, reason=str(exc)[:500])
        graph_path.write_text(json.dumps({"goal": goal, "nodes": [], "run_id": run_dir.name}))
        return GoldenRunResult(
            run_dir, RunOutcome.BLOCKED.value, str(exc), run_dir / "receipt.json"
        )
    graph_path.write_text(json.dumps({**graph.to_dict(), "run_id": run_dir.name}, indent=2))
    events.emit(
        "plan_ready",
        nodes=[n.to_dict() for n in graph.nodes],
        layers=[list(layer) for layer in graph.layers()],
        topology=graph.topology.value,
        rationale=list(graph.rationale),
    )

    def prompt_for(node: WorkNode, cwd: Path) -> str:
        return hydrate_node_prompt(node, repo=cwd, goal=goal)

    runtime = DagRuntime(
        repo=repo,
        run_dir=run_dir,
        graph=graph,
        selector=selector,
        executor=executor,
        classifier=classifier,
        events=events,
        prompt_for=prompt_for,
        reviewer=reviewer,
        policy=policy,
        inflight=inflight,
    )
    # Attempts dispatched by an earlier controller life keep their numbers; the
    # next attempt continues after them so the receipt never merges two lives.
    for node_id, attempts in prior_attempts(run_dir).items():
        if node_id in runtime.nodes and node_id not in resumed:
            runtime.nodes[node_id].attempt = attempts
            if attempts:
                events.emit(
                    "controller",
                    state="ABANDONED_ATTEMPT",
                    node_id=node_id,
                    detail=f"{attempts} attempt(s) from a previous controller life",
                )
    for node_id, (commit, route_id) in resumed.items():
        if node_id in runtime.nodes:
            runtime.nodes[node_id].state = NodeState.VALIDATED
            runtime.nodes[node_id].commit = commit
            runtime.nodes[node_id].route_id = route_id
    result = await runtime.run()
    try:
        receipt_path = write_run_receipt(run_dir)
        receipt = json.loads(receipt_path.read_text())
        outcome, reason = completion_verdict(receipt)
    except Exception as exc:  # evidence failure is BLOCKED, never a crash
        events.emit(
            "controller", state="RECEIPT_FAILED", detail=f"{type(exc).__name__}: {exc}"[:300]
        )
        return GoldenRunResult(
            run_dir,
            RunOutcome.BLOCKED.value,
            f"receipt could not be built: {type(exc).__name__}: {exc}",
            run_dir / "receipt.json",
        )
    if outcome != result.outcome.value:
        # The receipt is authoritative: a runtime COMPLETE without evidence is BLOCKED.
        events.emit(
            "controller",
            state="VERDICT_OVERRIDE",
            detail=f"runtime={result.outcome.value} receipt={outcome}: {reason}",
        )
    return GoldenRunResult(
        run_dir, outcome, reason if outcome != "COMPLETE" else result.reason, receipt_path
    )
