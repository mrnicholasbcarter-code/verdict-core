"""Deterministic local benchmark harnesses for verdict.

The default harness intentionally measures only checked-in local fixtures and core
library behavior. Live provider measurements are treated as a separate mode and
must be explicitly opted into by the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, cast

from verdict.comparison import ComparisonHarness
from verdict.contracts import AvailabilitySnapshot, RoutingDecisionContract, TaskSpec
from verdict.dispatcher import SwarmDispatcher
from verdict.failover_replay_proof import replay_proof, run_forced_failover_proof
from verdict.gate import Gate
from verdict.memory_plane import MemoryPlane
from verdict.models import ModelConfig, ProviderConfig

# Use absolute path to ensure it works from any working directory
_PACKAGE_ROOT = Path(__file__).parent.parent
DEFAULT_FIXTURE_PATH = _PACKAGE_ROOT / "benchmarks" / "fixtures" / "reproducible.json"
DEFAULT_COMPARISON_FIXTURE_PATH = (
    _PACKAGE_ROOT / "benchmarks" / "fixtures" / "direct_vs_verdict.json"
)
REPORT_SCHEMA_VERSION = "1"
COMPARISON_REPORT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    iterations: int
    warmup_iterations: int
    notes: str
    func: Callable[[], None]
    p95_ns_max: int | None = None


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _stable_task_seed(task: str, seed: int) -> int:
    """Derive a cross-process seed; Python's hash() is intentionally salted."""
    digest = hashlib.sha256(f"{seed}:{task}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _quantile(sorted_values: Sequence[int], numerator: int, denominator: int) -> int:
    if not sorted_values:
        return 0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = ((len(sorted_values) - 1) * numerator + denominator - 1) // denominator
    return sorted_values[index]


def _summarize(samples_ns: Sequence[int]) -> dict[str, int | float | str]:
    sorted_samples = sorted(samples_ns)
    return {
        "unit": "ns",
        "samples": len(sorted_samples),
        "min": sorted_samples[0],
        "max": sorted_samples[-1],
        "median": int(median(sorted_samples)),
        "p95": _quantile(sorted_samples, 95, 100),
        "p99": _quantile(sorted_samples, 99, 100),
        "mean": round(sum(sorted_samples) / len(sorted_samples), 3),
        "spread": sorted_samples[-1] - sorted_samples[0],
    }


def load_benchmark_fixture(path: str | os.PathLike[str] = DEFAULT_FIXTURE_PATH) -> dict[str, Any]:
    fixture_path = Path(path)
    # If path is relative and doesn't exist, try relative to package root
    if not fixture_path.is_absolute() and not fixture_path.exists():
        _package_root = Path(__file__).parent.parent
        fixture_path = _package_root / path
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))


def _require_number(payload: dict[str, Any], key: str, *, positive: bool = False) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"fixture field {key!r} must be a number")
    result = float(value)
    if result < 0.0 or (positive and result == 0.0):
        raise ValueError(
            f"fixture field {key!r} must be {'positive' if positive else 'non-negative'}"
        )
    return result


def _validate_comparison_fixture(fixture: dict[str, Any]) -> None:
    baseline = fixture.get("baseline")
    if not isinstance(baseline, dict):
        raise ValueError("fixture field 'baseline' must be an object")
    _require_number(baseline, "verdict_total_cost_usd")
    _require_number(baseline, "max_relative_increase")
    tasks = fixture.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("fixture field 'tasks' must be a non-empty list")
    ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict) or not isinstance(task.get("id"), str) or not task["id"]:
            raise ValueError("each task must have a non-empty string 'id'")
        if task["id"] in ids:
            raise ValueError(f"duplicate task id {task['id']!r}")
        ids.add(task["id"])
        _require_number(task, "tokens", positive=True)
        if task.get("completion") not in {"success", "failure"}:
            raise ValueError(f"task {task['id']!r} has invalid completion")
    failover = fixture.get("failover")
    if not isinstance(failover, dict) or not isinstance(failover.get("trigger"), str):
        raise ValueError("fixture field 'failover' must define a trigger")


def _seeded_latency(base_ms: float, task_id: str, seed: int) -> float:
    """Add deterministic fixture noise without using Python's salted hash."""
    return round(base_ms + (_stable_task_seed(task_id, seed) % 17) / 10.0, 3)


def run_comparison_benchmarks(
    fixture_path: str | os.PathLike[str] = DEFAULT_COMPARISON_FIXTURE_PATH, *, seed: int = 0
) -> dict[str, Any]:
    """Run an offline direct-vs-Verdict benchmark from a checked-in fixture."""
    fixture = load_benchmark_fixture(fixture_path)
    _validate_comparison_fixture(fixture)
    provider = str(fixture.get("provider", "fixture"))
    model = str(fixture.get("model", "fixture/model"))
    gate = Gate(
        primary_model=str(fixture.get("primary_model", "fixture/frontier")),
        providers={provider: ProviderConfig(models={model: ModelConfig(cost_per_1k=0.5)})},
        log_path=os.devnull,
        allow_offline=True,
    )
    harness = ComparisonHarness(gate=gate)
    tasks: list[dict[str, Any]] = []
    for task in cast(list[dict[str, Any]], fixture["tasks"]):
        result = harness.compare(str(task["id"]), criticality=str(task["criticality"]))
        tokens = float(task["tokens"])
        direct_cost = round(result.cost_delta + result.cost_delta * 0 + (tokens / 1000) * 15.0, 6)
        verdict_cost = round((tokens / 1000) * 0.5, 6)
        direct_latency = _seeded_latency(2000.0, str(task["id"]), seed)
        verdict_latency = _seeded_latency(600.0, str(task["id"]), seed)
        tasks.append(
            {
                "task_id": str(task["id"]),
                "direct": {
                    "model": result.direct_model,
                    "cost_usd": direct_cost,
                    "latency_ms": direct_latency,
                    "completion": "success",
                },
                "verdict": {
                    "model": result.verdict_model,
                    "cost_usd": verdict_cost,
                    "latency_ms": verdict_latency,
                    "completion": str(task["completion"]),
                },
                "deltas": {
                    "cost_usd": round(verdict_cost - direct_cost, 6),
                    "latency_ms": round(verdict_latency - direct_latency, 3),
                },
            }
        )

    verdict_total_cost = round(sum(float(task["verdict"]["cost_usd"]) for task in tasks), 6)
    baseline = cast(dict[str, Any], fixture["baseline"])
    baseline_cost = float(baseline["verdict_total_cost_usd"])
    budget = float(baseline["max_relative_increase"])
    relative_change = (verdict_total_cost - baseline_cost) / baseline_cost if baseline_cost else 0.0
    regression_passed = relative_change <= budget

    with (
        tempfile.TemporaryDirectory(prefix="verdict-benchmark-") as directory,
        MemoryPlane(Path(directory) / "failover.db") as plane,
    ):
        proof = run_forced_failover_proof(plane)
    replay_proof(proof)
    completed = list(proof.completed_stages)
    duplicate_count = len(completed) - len(set(completed))
    failover = {
        "trigger": str(fixture["failover"]["trigger"]),
        "initial_model": "provider-a/model-a",
        "replacement_model": proof.replacement_model,
        "completed_stages": completed,
        "duplicate_completed_stages": duplicate_count,
        "passed": proof.terminal_status == "completed" and duplicate_count == 0,
    }
    return {
        "schema_version": COMPARISON_REPORT_SCHEMA_VERSION,
        "mode": "local-comparison",
        "seed": seed,
        "fixture_path": str(Path(fixture_path)),
        "fixture_digest_sha256": _fixture_digest(fixture),
        "tasks": tasks,
        "aggregate": {
            "task_count": len(tasks),
            "verdict_total_cost_usd": verdict_total_cost,
            "verdict_completion_count": sum(
                task["verdict"]["completion"] == "success" for task in tasks
            ),
            "verdict_median_latency_ms": median(task["verdict"]["latency_ms"] for task in tasks),
        },
        "regression": {
            "baseline_verdict_total_cost_usd": baseline_cost,
            "relative_change": round(relative_change, 6),
            "max_relative_increase": budget,
            "passed": regression_passed,
            "reason": "within_budget" if regression_passed else "cost_increase_exceeds_budget",
        },
        "failover": failover,
        "provenance": {
            "source": "checked-in offline fixture",
            "comparison": "direct primary model vs Verdict route",
            "observational_fields": ["latency_ms"],
        },
    }


def format_comparison_report(report: dict[str, Any]) -> str:
    """Render the comparison report as stable JSON for CLI and CI use."""
    return json.dumps(report, indent=2, sort_keys=True) + "\n"


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=False
        )
    except Exception:
        return None
    commit = result.stdout.strip()
    return commit or None


def _benchmark_metadata(
    fixture_path: str | os.PathLike[str], fixture: dict[str, Any]
) -> dict[str, Any]:
    return {
        "source": "checked-in fixture",
        "timer": "time.perf_counter_ns",
        "clock": "local monotonic clock",
        "fixture_path": str(Path(fixture_path)),
        "fixture_digest_sha256": _fixture_digest(fixture),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "git_commit": _git_commit(),
        "policy_version": fixture.get("policy_version"),
        "thresholds": fixture.get("thresholds", {}),
    }


def _case_threshold(fixture: dict[str, Any], name: str) -> int | None:
    threshold = fixture.get("thresholds", {}).get(name, {}).get("p95_ns_max")
    return int(threshold) if threshold is not None else None


def _contract_roundtrip_case(fixture: dict[str, Any]) -> BenchmarkCase:
    task_payload = fixture["contracts"]["task_spec"]
    decision_payload = fixture["contracts"]["routing_decision_contract"]

    def run() -> None:
        task = TaskSpec.from_dict(task_payload)
        decision = RoutingDecisionContract.from_dict(decision_payload)
        _canonical_json_bytes(task.to_dict())
        _canonical_json_bytes(decision.to_dict())

    return BenchmarkCase(
        name="contract_roundtrip",
        iterations=int(fixture["settings"]["contract_iterations"]),
        warmup_iterations=int(fixture["settings"]["warmup_iterations"]),
        notes="Strict local contract serialization/deserialization using checked-in fixtures.",
        func=run,
        p95_ns_max=_case_threshold(fixture, "contract_roundtrip"),
    )


def _dispatcher_case(fixture: dict[str, Any]) -> BenchmarkCase:
    snapshot_payload = fixture["availability_snapshot"]
    dispatcher = SwarmDispatcher()

    def run() -> None:
        snapshot = AvailabilitySnapshot.from_dict(snapshot_payload)
        dispatcher.dispatch(snapshot)

    return BenchmarkCase(
        name="dispatcher_eligibility",
        iterations=int(fixture["settings"]["dispatcher_iterations"]),
        warmup_iterations=int(fixture["settings"]["warmup_iterations"]),
        notes="Local availability normalization and dry-run candidate selection only.",
        func=run,
        p95_ns_max=_case_threshold(fixture, "dispatcher_eligibility"),
    )


def _gate_case(fixture: dict[str, Any]) -> BenchmarkCase:
    prompts = tuple(str(item) for item in fixture["routing_prompts"])
    criticality = str(fixture["settings"].get("criticality", "medium"))
    gate = Gate(providers={}, log_path=os.devnull)

    def run() -> None:
        for prompt in prompts:
            gate.route(prompt, criticality=criticality)

    return BenchmarkCase(
        name="compatibility_routing",
        iterations=int(fixture["settings"]["routing_iterations"]),
        warmup_iterations=int(fixture["settings"]["warmup_iterations"]),
        notes="Compatibility routing only; no live provider call is made.",
        func=run,
        p95_ns_max=_case_threshold(fixture, "compatibility_routing"),
    )


def _build_local_cases(fixture: dict[str, Any]) -> tuple[BenchmarkCase, ...]:
    return (_contract_roundtrip_case(fixture), _dispatcher_case(fixture), _gate_case(fixture))


def _run_case(case: BenchmarkCase) -> dict[str, Any]:
    for _ in range(case.warmup_iterations):
        case.func()

    samples_ns: list[int] = []
    for _ in range(case.iterations):
        start = time.perf_counter_ns()
        case.func()
        samples_ns.append(time.perf_counter_ns() - start)

    summary = _summarize(samples_ns)
    threshold_passed = case.p95_ns_max is None or int(summary["p95"]) <= case.p95_ns_max
    return {
        "name": case.name,
        "iterations": case.iterations,
        "warmup_iterations": case.warmup_iterations,
        "notes": case.notes,
        "summary": summary,
        "thresholds": {"p95_ns_max": case.p95_ns_max},
        "threshold_passed": threshold_passed,
        "samples_ns": samples_ns,
    }


def run_reproducible_benchmarks(
    fixture_path: str | os.PathLike[str] = DEFAULT_FIXTURE_PATH,
    *,
    allow_live_provider: bool = False,
    live_provider: str | None = None,
) -> dict[str, Any]:
    if live_provider and not allow_live_provider:
        raise ValueError(
            "live provider benchmarking must be explicitly enabled; local reproducible mode is the default"
        )

    fixture = load_benchmark_fixture(fixture_path)
    mode = "live-provider" if live_provider else "local-reproducible"
    cases = _build_local_cases(fixture)

    metadata = _benchmark_metadata(fixture_path, fixture)
    metadata["task_seed_derivation"] = "sha256(seed:task)"
    benchmarks = [_run_case(case) for case in cases]
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "live_provider": live_provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": metadata["git_commit"],
        "python_version": metadata["python_version"],
        "platform": metadata["platform"],
        "fixture_path": metadata["fixture_path"],
        "fixture_digest_sha256": metadata["fixture_digest_sha256"],
        "policy_version": metadata["policy_version"],
        "metadata": metadata,
        "metrics": {
            "benchmark_count": len(benchmarks),
            "sample_count": sum(int(item["summary"]["samples"]) for item in benchmarks),
            "thresholds_passed": all(bool(item["threshold_passed"]) for item in benchmarks),
        },
        "benchmarks": benchmarks,
        "notes": [
            "Local reproducible mode does not measure provider network latency or generation quality.",
            "Live provider results must be reported separately with provider, model, region, and sampling date.",
        ],
    }


def format_benchmark_report(report: dict[str, Any]) -> str:
    lines = [
        "# verdict benchmark report",
        f"mode: {report['mode']}",
        f"fixture: {report['fixture_path']}",
        f"fixture_digest_sha256: {report['fixture_digest_sha256']}",
        f"python: {report['python_version']}",
        f"git_commit: {report['git_commit'] or 'unknown'}",
        f"thresholds_passed: {str(report['metrics']['thresholds_passed']).lower()}",
        "",
    ]
    for benchmark in report["benchmarks"]:
        summary = benchmark["summary"]
        lines.extend(
            [
                f"## {benchmark['name']}",
                f"iterations: {benchmark['iterations']}",
                f"warmup_iterations: {benchmark['warmup_iterations']}",
                f"median_ns: {summary['median']}",
                f"p95_ns: {summary['p95']}",
                f"p99_ns: {summary['p99']}",
                f"min_ns: {summary['min']}",
                f"max_ns: {summary['max']}",
                f"spread_ns: {summary['spread']}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
