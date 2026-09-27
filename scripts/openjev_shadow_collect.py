#!/usr/bin/env python3
"""Collect OpenJev SHADOW signals for labeled Verdict tasks (BOD-203).

Input: JSONL task records. Each has ``task_id``, ``task_class``, ``summary``,
the observed outcome fields of :class:`CalibrationRecord`, and a
``label_source`` saying who decided the ground truth.

For each task this asks the configured OpenJev provider for signals in SHADOW
mode and writes one calibration record per line. It is offline measurement:
it never routes, admits or plans anything. A provider failure is recorded as
"no usable signal" (``frontier_worthy=None``), never as a cheap prediction.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from verdict.decision_signals.calibration import CalibrationRecord
from verdict.decision_signals.contracts import DecisionQuestionV1, DecisionSignalProvider

OUTCOME_FIELDS = (
    "frontier_needed",
    "security_relevant",
    "planner_was_frontier",
    "verified",
    "first_pass",
    "retries",
    "escalations",
    "total_cost_usd",
    "time_to_green_s",
)


def collect(tasks: list[dict[str, Any]], provider: DecisionSignalProvider) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for task in tasks:
        question = DecisionQuestionV1(
            purpose="frontier_planning",
            task_summary=str(task["summary"])[:500],
            complexity_hints={"task_class": task["task_class"]},
        )
        frontier = security = confidence = None
        latency: int | None = None
        failure: str | None = None
        try:
            signal_set = provider.signals(question, now=datetime.now(timezone.utc))
            latency = signal_set.latency_ms
            if signal_set.failure_class is None and signal_set.signals:
                frontier = signal_set.signals.get("frontier_worthy")
                security = signal_set.signals.get("security_sensitive")
                confidence = signal_set.confidence
            else:
                failure = signal_set.failure_class.value if signal_set.failure_class else "empty"
        except Exception as exc:  # measurement must not stop on one task
            failure = type(exc).__name__
        record = CalibrationRecord(
            task_id=str(task["task_id"]),
            task_class=str(task["task_class"]),
            frontier_worthy=frontier,
            confidence=confidence,
            security_sensitive=security,
            signal_latency_ms=latency,
            **{name: task[name] for name in OUTCOME_FIELDS if name in task},
        )
        row = {**record.__dict__, "label_source": task.get("label_source", "unknown")}
        if failure:
            row["signal_failure"] = failure
        out.append(row)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks", type=Path, help="JSONL labeled tasks")
    parser.add_argument("output", type=Path, help="JSONL calibration records to write")
    args = parser.parse_args(argv)

    from verdict.decision_signals.factory import provider_from_env

    provider = provider_from_env()
    if provider is None:
        print("Error: no OpenJev provider (TYPESAFE_API_KEY unset)", file=sys.stderr)
        return 2
    tasks = [json.loads(line) for line in args.tasks.read_text().splitlines() if line.strip()]
    rows = collect(tasks, provider)
    args.output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows))
    usable = sum(1 for row in rows if row["frontier_worthy"] is not None)
    print(f"wrote {len(rows)} records ({usable} with usable signals) to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
