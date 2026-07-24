"""
Swarm observability, explainability, and completion metrics (Issue #47 / Slice 37.5).

This module provides:
- Telemetry emission as redacted JSONL with correlation IDs
- Explain endpoint for assignment/exclusion reasons
- Completion metrics and deterministic reports
- Sequential baseline vs swarm mode comparison
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SwarmTelemetryEvent:
    """Single telemetry event emitted as JSONL."""

    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: str = ""
    event_type: str = ""  # task_planned, task_assigned, task_started, task_completed, task_failed, verification, merge_decision
    task_id: str | None = None
    attempt_id: str | None = None
    model_id: str | None = None
    runtime_id: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_jsonl(self) -> str:
        """Serialize to JSONL format."""
        return json.dumps(self.__dict__, separators=(",", ":"), sort_keys=True)


class SwarmTelemetrySink:
    """Thread-safe sink for swarm telemetry events."""

    def __init__(self, output_path: Path | str, redact_sensitive: bool = True):
        self.output_path = Path(output_path)
        self.redact_sensitive = redact_sensitive
        self._lock = threading.Lock()
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: SwarmTelemetryEvent) -> None:
        """Emit a telemetry event."""
        line = event.to_jsonl()
        if self.redact_sensitive:
            line = self._redact(line)
        with self._lock, self.output_path.open("a") as f:
            f.write(line + "\n")

    def _redact(self, line: str) -> str:
        """Redact sensitive content from JSONL line."""
        import re

        patterns = [
            (re.compile(r"sk-[\w-]{20,}", re.I), "[REDACTED:openai_key]"),
            (re.compile(r"gh[pousr]_[A-Za-z0-9_]{36,}", re.I), "[REDACTED:github_token]"),
            (re.compile(r"api[_-]?key\s*[:=]\s*[\w-]+", re.I), "[REDACTED:api_key]"),
            (re.compile(r"secret\s*[:=]\s*[\w-]+", re.I), "[REDACTED:secret]"),
            (re.compile(r"password\s*[:=]\s*\S+", re.I), "[REDACTED:password]"),
            (re.compile(r"token\s*[:=]\s*[\w.-]+", re.I), "[REDACTED:token]"),
            (re.compile(r"authorization\s*[:=]\s*\S+", re.I), "[REDACTED:authorization]"),
            (re.compile(r"bearer\s+[\w.-]+", re.I), "[REDACTED:bearer_token]"),
        ]
        for pattern, replacement in patterns:
            line = pattern.sub(replacement, line)
        return line


@dataclass(frozen=True)
class ExplainReason:
    """Reason for a routing/assignment decision."""

    reason_type: str  # eligible, excluded, budget_exceeded, capability_mismatch, protected_path
    description: str
    details: dict[str, Any] = field(default_factory=dict)


def explain_assignment(
    task_id: str,
    candidate_id: str,
    eligible: bool,
    reasons: list[ExplainReason],
    candidate_cost_usd: float | None = None,
    budget_usd: float | None = None,
    required_capabilities: list[str] | None = None,
    candidate_capabilities: list[str] | None = None,
) -> dict[str, Any]:
    """Generate an explanation for why a candidate was assigned or not."""
    if eligible:
        primary = "assigned"
        summary = f"Candidate {candidate_id} assigned to task {task_id}"
    else:
        primary = "excluded"
        summary = f"Candidate {candidate_id} excluded from task {task_id}"

    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "decision": primary,
        "summary": summary,
        "reasons": [r.__dict__ for r in reasons],
        "cost_analysis": {
            "candidate_cost_usd": candidate_cost_usd,
            "budget_usd": budget_usd,
            "within_budget": candidate_cost_usd is None
            or budget_usd is None
            or candidate_cost_usd <= budget_usd,
        }
        if candidate_cost_usd is not None or budget_usd is not None
        else None,
        "capability_analysis": {
            "required": required_capabilities or [],
            "candidate_has": candidate_capabilities or [],
            "missing": list(set(required_capabilities or []) - set(candidate_capabilities or [])),
        }
        if required_capabilities or candidate_capabilities
        else None,
    }


@dataclass
class CompletionMetrics:
    """Aggregated completion metrics for a swarm run."""

    run_id: str
    started_at: str
    completed_at: str | None = None
    total_tasks: int = 0
    planned: int = 0
    assigned: int = 0
    completed: int = 0
    verified: int = 0
    blocked: int = 0
    failed: int = 0
    first_pass_verification_rate: float = 0.0
    rework_rate: float = 0.0
    avg_retry_count: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    cost_per_verified_task: float = 0.0
    total_budget_usd: float = 0.0
    budget_utilization: float = 0.0
    queue_wait_p50_ms: float = 0.0
    queue_wait_p95_ms: float = 0.0
    concurrency_utilization: float = 0.0
    model_availability_rate: float = 0.0
    fallback_rate: float = 0.0
    escaped_defects: int = 0


def generate_baseline_report(metrics: CompletionMetrics) -> str:
    """Generate a deterministic baseline report from metrics."""
    lines = [
        "Swarm Completion Metrics Report",
        "=" * 50,
        f"Run ID: {metrics.run_id}",
        f"Started: {metrics.started_at}",
        f"Completed: {metrics.completed_at or 'N/A'}",
        "",
        "Task Summary:",
        f"  Total:        {metrics.total_tasks}",
        f"  Planned:      {metrics.planned}",
        f"  Assigned:     {metrics.assigned}",
        f"  Completed:    {metrics.completed}",
        f"  Verified:     {metrics.verified}",
        f"  Blocked:      {metrics.blocked}",
        f"  Failed:       {metrics.failed}",
        "",
        "Quality Metrics:",
        f"  First-pass rate:   {metrics.first_pass_verification_rate:.1%}",
        f"  Rework rate:       {metrics.rework_rate:.1%}",
        f"  Avg retries:       {metrics.avg_retry_count:.2f}",
        f"  Escaped defects:   {metrics.escaped_defects}",
        "",
        "Latency (ms):",
        f"  p50: {metrics.p50_latency_ms:.1f}",
        f"  p95: {metrics.p95_latency_ms:.1f}",
        "",
        "Cost:",
        f"  Cost per verified: ${metrics.cost_per_verified_task:.4f}",
        f"  Budget utilization: {metrics.budget_utilization:.1%} (${metrics.total_budget_usd:.2f})",
        "",
        "Queue:",
        f"  p50 wait: {metrics.queue_wait_p50_ms:.1f}ms",
        f"  p95 wait: {metrics.queue_wait_p95_ms:.1f}ms",
        "",
        "Concurrency:",
        f"  Utilization: {metrics.concurrency_utilization:.1%}",
        "",
        "Models:",
        f"  Availability: {metrics.model_availability_rate:.1%}",
        f"  Fallback rate: {metrics.fallback_rate:.1%}",
        "",
        "Escaped Defects:",
        f"  Count: {metrics.escaped_defects}",
    ]
    return "\n".join(lines)


@dataclass
class SwarmBaseline:
    """Baseline comparison between sequential and swarm execution."""

    run_id: str
    fixture_name: str
    sequential: CompletionMetrics
    swarm: CompletionMetrics

    def comparison(self) -> dict[str, Any]:
        """Generate comparison dict."""

        def pct_change(old: float, new: float) -> float:
            if old == 0:
                return float("inf") if new > 0 else 0.0
            return (new - old) / old

        return {
            "fixture": self.fixture_name,
            "tasks_completed": {
                "sequential": self.sequential.completed,
                "swarm": self.swarm.completed,
                "change_pct": pct_change(self.sequential.completed, self.swarm.completed),
            },
            "first_pass_rate": {
                "sequential": self.sequential.first_pass_verification_rate,
                "swarm": self.swarm.first_pass_verification_rate,
                "change_pct": pct_change(
                    self.sequential.first_pass_verification_rate,
                    self.swarm.first_pass_verification_rate,
                ),
            },
            "p95_latency_ms": {
                "sequential": self.sequential.p95_latency_ms,
                "swarm": self.swarm.p95_latency_ms,
                "change_pct": pct_change(self.sequential.p95_latency_ms, self.swarm.p95_latency_ms),
            },
            "cost_per_verified": {
                "sequential": self.sequential.cost_per_verified_task,
                "swarm": self.swarm.cost_per_verified_task,
                "change_pct": pct_change(
                    self.sequential.cost_per_verified_task,
                    self.swarm.cost_per_verified_task,
                ),
            },
            "budget_utilization": {
                "sequential": self.sequential.budget_utilization,
                "swarm": self.swarm.budget_utilization,
            },
        }


def generate_baseline_comparison(baseline: SwarmBaseline) -> str:
    """Generate human-readable comparison report."""
    comp = baseline.comparison()
    lines = [
        "Swarm vs Sequential Baseline Comparison",
        "=" * 50,
        f"Fixture: {baseline.fixture_name}",
        f"Run ID: {baseline.run_id}",
        "",
        "Tasks Completed:",
        f"  Sequential: {comp['tasks_completed']['sequential']}",
        f"  Swarm:      {comp['tasks_completed']['swarm']}",
        f"  Change:     {comp['tasks_completed']['change_pct']:.1%}",
        "",
        "First-Pass Verification Rate:",
        f"  Sequential: {comp['first_pass_rate']['sequential']:.1%}",
        f"  Swarm:      {comp['first_pass_rate']['swarm']:.1%}",
        f"  Change:     {comp['first_pass_rate']['change_pct']:.1%}",
        "",
        "P95 Latency (ms):",
        f"  Sequential: {comp['p95_latency_ms']['sequential']:.1f}",
        f"  Swarm:      {comp['p95_latency_ms']['swarm']:.1f}",
        f"  Change:     {comp['p95_latency_ms']['change_pct']:.1%}",
        "",
        "Cost Per Verified Task:",
        f"  Sequential: ${comp['cost_per_verified']['sequential']:.4f}",
        f"  Swarm:      ${comp['cost_per_verified']['swarm']:.4f}",
        f"  Change:     {comp['cost_per_verified']['change_pct']:.1%}",
        "",
        "Budget Utilization:",
        f"  Sequential: {comp['budget_utilization']['sequential']:.1%}",
        f"  Swarm:      {comp['budget_utilization']['swarm']:.1%}",
    ]
    return "\n".join(lines)


@dataclass
class SwarmMetricsCollector:
    """Collects metrics during a swarm run."""

    run_id: str
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _task_states: dict[str, list[str]] = field(default_factory=dict)  # task_id -> list of states
    _task_timings: dict[str, dict[str, float]] = field(default_factory=dict)
    _verifications: list[dict[str, Any]] = field(default_factory=list)
    _model_calls: list[dict[str, Any]] = field(default_factory=list)
    _queue_waits: list[float] = field(default_factory=list)
    _start_time: float = field(default_factory=time.time)

    def record_task_event(self, task_id: str, state: str, **kwargs: Any) -> None:
        """Record a task state transition."""
        with self._lock:
            if task_id not in self._task_states:
                self._task_states[task_id] = []
            self._task_states[task_id].append(state)
            if task_id not in self._task_timings:
                self._task_timings[task_id] = {}
            self._task_timings[task_id][state] = time.time()

    def record_verification(
        self,
        task_id: str,
        attempt_id: str,
        passed: bool,
        latency_ms: float,
        retry_count: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Record a verification result."""
        with self._lock:
            self._verifications.append(
                {
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "passed": passed,
                    "latency_ms": latency_ms,
                    "retry_count": retry_count,
                    "cost_usd": cost_usd,
                }
            )

    def record_model_call(self, fallback: bool = False) -> None:
        """Record a model call (for fallback rate tracking)."""
        with self._lock:
            self._model_calls.append({"fallback": fallback})

    def record_queue_wait(self, wait_ms: float) -> None:
        """Record queue wait time."""
        with self._lock:
            self._queue_waits.append(wait_ms)

    def compute_metrics(self) -> CompletionMetrics:
        """Compute aggregated metrics from collected data."""
        with self._lock:
            completed_tasks = [
                t for t, states in self._task_states.items() if "completed" in states
            ]
            assigned_tasks = [
                t
                for t, states in self._task_states.items()
                if any(s in states for s in ("assigned", "started", "completed"))
            ]

            if not completed_tasks:
                return CompletionMetrics(
                    run_id=self.run_id,
                    started_at=datetime.fromtimestamp(
                        self._start_time, tz=timezone.utc
                    ).isoformat(),
                )

            # Compute latency percentiles
            latencies = [v["latency_ms"] for v in self._verifications if v["passed"]]
            latencies.sort()
            p50 = latencies[len(latencies) // 2] if latencies else 0.0
            p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0

            # Retry count
            total_retries = sum(v["retry_count"] for v in self._verifications)
            total_verifications = len(self._verifications)
            avg_retry = total_retries / total_verifications if total_verifications else 0.0

            # First pass rate
            first_pass = sum(
                1 for v in self._verifications if v["passed"] and v["retry_count"] == 0
            )
            rework = total_verifications - first_pass
            first_pass_rate = first_pass / total_verifications if total_verifications else 0.0
            rework_rate = rework / total_verifications if total_verifications else 0.0

            # Cost
            total_cost = sum(v["cost_usd"] for v in self._verifications)
            cost_per_verified = total_cost / len(completed_tasks) if completed_tasks else 0.0

            # Fallback rate
            total_calls = len(self._model_calls)
            fallback_calls = sum(1 for c in self._model_calls if c["fallback"])
            fallback_rate = fallback_calls / total_calls if total_calls else 0.0

            # Queue wait percentiles
            queue_waits = sorted(self._queue_waits)
            q50 = queue_waits[len(queue_waits) // 2] if queue_waits else 0.0
            q95 = queue_waits[int(len(queue_waits) * 0.95)] if queue_waits else 0.0

            # Concurrency utilization
            max_concurrent = max(
                len([t for t, ts in self._task_timings.items() if "started" in ts]), 1
            )
            concurrency_util = (
                len(completed_tasks) / (max_concurrent * 2) if max_concurrent else 0.0
            )

            completed_at = datetime.now(timezone.utc).isoformat()

            return CompletionMetrics(
                run_id=self.run_id,
                started_at=datetime.fromtimestamp(self._start_time, tz=timezone.utc).isoformat(),
                completed_at=completed_at,
                total_tasks=len(self._task_states),
                planned=len(self._task_states),
                assigned=len(assigned_tasks),
                completed=len(completed_tasks),
                verified=sum(1 for v in self._verifications if v["passed"]),
                blocked=len([t for t, states in self._task_states.items() if "blocked" in states]),
                failed=len([t for t, states in self._task_states.items() if "failed" in states]),
                first_pass_verification_rate=first_pass_rate,
                rework_rate=rework_rate,
                avg_retry_count=avg_retry,
                p50_latency_ms=p50,
                p95_latency_ms=p95,
                cost_per_verified_task=cost_per_verified,
                total_budget_usd=100.0,  # Would be injected
                budget_utilization=total_cost / 100.0 if 100.0 else 0.0,
                queue_wait_p50_ms=q50,
                queue_wait_p95_ms=q95,
                concurrency_utilization=min(concurrency_util, 1.0),
                model_availability_rate=1.0 - fallback_rate,
                fallback_rate=fallback_rate,
                escaped_defects=0,  # Would be injected from post-deployment
            )


def create_swarm_sink(output_dir: Path, run_id: str) -> SwarmTelemetrySink:
    """Create a telemetry sink for a swarm run."""
    return SwarmTelemetrySink(output_dir / f"telemetry-{run_id}.jsonl")


def create_explain_endpoint() -> dict[str, Any]:
    """Document the explain endpoint specification."""
    return {
        "endpoint": "/v1/swarm/explain",
        "method": "POST",
        "description": "Explain why a candidate was assigned or excluded for a task",
        "request": {
            "task_id": "string",
            "candidate_id": "string",
            "context": "object (optional)",
        },
        "response": {
            "task_id": "string",
            "candidate_id": "string",
            "decision": "assigned | excluded",
            "summary": "string",
            "reasons": "array of {reason_type, description, details}",
            "cost_analysis": "object (optional)",
            "capability_analysis": "object (optional)",
        },
    }


__all__ = [
    "CompletionMetrics",
    "ExplainReason",
    "SwarmBaseline",
    "SwarmMetricsCollector",
    "SwarmTelemetryEvent",
    "SwarmTelemetrySink",
    "create_explain_endpoint",
    "create_swarm_sink",
    "explain_assignment",
    "generate_baseline_comparison",
    "generate_baseline_report",
]
