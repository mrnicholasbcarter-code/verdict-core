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
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, cast

from verdict.contracts import AvailabilitySnapshot, RoutingDecisionContract, TaskSpec
from verdict.dispatcher import SwarmDispatcher
from verdict.gate import Gate

DEFAULT_FIXTURE_PATH = Path("benchmarks/fixtures/reproducible.json")
REPORT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    iterations: int
    warmup_iterations: int
    notes: str
    func: Callable[[], None]


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _fixture_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


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
    return cast(dict[str, Any], json.loads(fixture_path.read_text()))


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=2, check=False
        )
    except Exception:
        return None
    commit = result.stdout.strip()
    return commit or None


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

    return {
        "name": case.name,
        "iterations": case.iterations,
        "warmup_iterations": case.warmup_iterations,
        "notes": case.notes,
        "summary": _summarize(samples_ns),
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

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": mode,
        "live_provider": live_provider,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "fixture_path": str(Path(fixture_path)),
        "fixture_digest_sha256": _fixture_digest(fixture),
        "policy_version": fixture.get("policy_version"),
        "benchmarks": [_run_case(case) for case in cases],
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
